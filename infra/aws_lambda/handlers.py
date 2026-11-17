"""AWS Lambda entry points. Same code as the local prototype; only I/O differs.

WebhookFn    API Gateway  -> verify signature -> SQS FIFO (group = driver, dedupe = message id)
WorkerFn     SQS FIFO     -> DriverAgent.handle -> reply via WhatsApp (AWS End User Messaging Social)
SweeperFn    every 1 min  -> expire holds / pending confirmations -> notify drivers
SchedulerFn  EventBridge  -> facility plan comparison -> S3 + SNS to coordinator
OpsApiFn     API (Cognito)-> coordinator approves / rejects a pending booking

Demo profile: SQLite on EFS with WorkerFn reserved concurrency = 1 (single
writer). At SetuHaul's peak (20-35 messages / 30 min) one writer has >100x
headroom. Production profile swaps db.connect() for Aurora PostgreSQL
(infra/postgres_schema.sql) and lifts the concurrency limit; the EXCLUDE
constraint then guards overlap across writers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime
from pathlib import Path

from setuhaul.capacity import decide_pending, expire_stale
from setuhaul.db import connect, prepare_working_copy, write_txn
from setuhaul.obs import Telemetry
from setuhaul.timeutil import IST

DB_PATH = Path(os.environ.get("DB_PATH", "/mnt/data/setuhaul.db"))
_conn = None


def _db():
    global _conn
    if _conn is None:
        if not DB_PATH.exists():
            prepare_working_copy(DB_PATH)  # first run seeds EFS from the bundled snapshot
        _conn = connect(DB_PATH)
    return _conn


def _now() -> datetime:
    # SIMULATED_NOW lets the demo replay 4 Aug 2026; unset in production.
    sim = os.environ.get("SIMULATED_NOW")
    return datetime.fromisoformat(sim) if sim else datetime.now(IST)


def _secret(name: str) -> str:
    import boto3
    return boto3.client("secretsmanager").get_secret_value(SecretId=os.environ[name])["SecretString"]


# ---------------------------------------------------------------- webhook --
def webhook_handler(event, _ctx):
    if event.get("requestContext", {}).get("http", {}).get("method") == "GET":
        # WhatsApp webhook verification handshake
        q = event.get("queryStringParameters") or {}
        if q.get("hub.verify_token") == os.environ.get("VERIFY_TOKEN"):
            return {"statusCode": 200, "body": q.get("hub.challenge", "")}
        return {"statusCode": 403, "body": "forbidden"}
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    sig = (event.get("headers") or {}).get("x-hub-signature-256", "")
    if os.environ.get("APP_SECRET_ARN"):
        expected = "sha256=" + hmac.new(_secret("APP_SECRET_ARN").encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return {"statusCode": 401, "body": "bad signature"}
    import boto3
    sqs = boto3.client("sqs")
    payload = json.loads(raw)
    for msg in _extract_messages(payload):
        sqs.send_message(QueueUrl=os.environ["INBOUND_QUEUE_URL"], MessageBody=json.dumps(msg),
                         MessageGroupId=msg["driver_phone"], MessageDeduplicationId=msg["external_id"][:128])
    return {"statusCode": 200, "body": "ok"}


def _extract_messages(payload: dict):
    """WhatsApp Cloud API shape -> {driver_phone, text, external_id}. Also accepts the
    simple test shape {driver_phone, text, external_id}."""
    if "text" in payload and "driver_phone" in payload:
        yield payload
        return
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            for m in ch.get("value", {}).get("messages", []):
                if m.get("type") == "text":
                    yield {"driver_phone": "+" + m["from"].lstrip("+"), "text": m["text"]["body"], "external_id": m["id"]}


# ----------------------------------------------------------------- worker --
def worker_handler(event, _ctx):
    from setuhaul.llm_fallback import get_fallback
    from setuhaul.workflow import DriverAgent
    conn = _db()
    tel = Telemetry(echo=True)  # JSON lines -> CloudWatch Logs; EMF -> metrics
    agent = DriverAgent(conn, tel, get_fallback())
    failures = []
    for rec in event.get("Records", []):
        try:
            m = json.loads(rec["body"])
            drv = conn.execute("SELECT driver_id FROM drivers WHERE phone=?", (m["driver_phone"],)).fetchone()
            if drv is None:
                tel.metric("UnknownSender")
                continue
            thread = _open_thread(conn, drv["driver_id"])
            turn = agent.handle(drv["driver_id"], m["text"], _now(), thread, m["external_id"])
            if turn.reply:
                _send_whatsapp(m["driver_phone"], turn.reply)
        except Exception:  # noqa: BLE001 - partial batch failure -> SQS retry -> DLQ
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}


def _open_thread(conn, driver_id: str) -> str:
    r = conn.execute("SELECT thread_id FROM agent_conversations WHERE driver_id=? AND state NOT IN "
                     "('CLOSED','CONFIRMED','ESCALATED') ORDER BY updated_at DESC LIMIT 1", (driver_id,)).fetchone()
    return r["thread_id"] if r else f"WA-{driver_id}-{_now():%Y%m%d%H%M%S}"


def _send_whatsapp(phone: str, text: str):
    if not os.environ.get("WHATSAPP_PHONE_NUMBER_ID"):
        print(json.dumps({"type": "outbound_stub", "to": phone[-4:], "text": text}, ensure_ascii=False))
        return
    import boto3
    msg = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
    boto3.client("socialmessaging").send_whatsapp_message(
        originationPhoneNumberId=os.environ["WHATSAPP_PHONE_NUMBER_ID"],
        message=json.dumps(msg).encode(), metaApiVersion="v20.0")


# ---------------------------------------------------------------- sweeper --
def sweeper_handler(_event, _ctx):
    conn = _db()
    tel = Telemetry(echo=True, service="sweeper")
    with write_txn(conn):
        out = expire_stale(conn, _now())
    for k, v in out.items():
        tel.metric(k, v)
    return out


# -------------------------------------------------------------- scheduler --
def scheduler_handler(event, _ctx):
    from setuhaul.scheduler import compare, plan_table
    conn = _db()
    fac = (event.get("detail") or {}).get("facility_id", "FAC-JAI-01")
    res = compare(conn, fac, _now())
    proposal = {"facility": fac, "at": _now().isoformat(),
                "policies": [{"policy": r.policy, "metrics": r.metrics} for r in res],
                "recommended": plan_table(res[-1])}
    import boto3
    key = f"proposals/{fac}/{_now():%Y%m%dT%H%M}.json"
    boto3.client("s3").put_object(Bucket=os.environ["ARTIFACT_BUCKET"], Key=key,
                                  Body=json.dumps(proposal, default=str).encode())
    swaps = res[3].metrics.get("swap_proposals") if len(res) > 3 else None
    if swaps:
        boto3.client("sns").publish(TopicArn=os.environ["ESCALATION_TOPIC_ARN"],
                                    Subject=f"Swap proposal needs approval ({fac})",
                                    Message=json.dumps(swaps, indent=2))
    return {"s3_key": key, "swaps": len(swaps or [])}


# ---------------------------------------------------------------- ops api --
def ops_api_handler(event, _ctx):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    approver = claims.get("email", "unknown")
    body = json.loads(event.get("body") or "{}")
    conn = _db()
    if event.get("routeKey", "").startswith("GET"):
        rows = conn.execute("SELECT escalation_id, reason_code, route_to, status, created_at FROM escalations "
                            "WHERE status='OPEN' ORDER BY created_at").fetchall()
        pend = conn.execute("SELECT appointment_id, shipment_id, slot_id, booked_at FROM appointments "
                            "WHERE appointment_status='PENDING_CONFIRMATION'").fetchall()
        return {"statusCode": 200, "body": json.dumps({"escalations": [dict(r) for r in rows],
                                                       "pending": [dict(r) for r in pend]})}
    res = decide_pending(conn, body["appointment_id"], bool(body.get("approve")), approver, _now())
    return {"statusCode": 200, "body": json.dumps(res)}
