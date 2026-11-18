"""Hand-off to a person: rule-based triggers, channel-aware routing, complete record.

A FAILED message is not a hand-off (audit F29): routing skips vacant roles,
contacts with no usable channel, and e-mail addresses whose last message failed.
"""
import json
from .clock import ts, iso, add
from .db import one, rows

URGENCY_SLA_MIN = {"CRITICAL": 5, "HIGH": 15, "MEDIUM": 30, "LOW": 60}

ROUTES = {
    "SAFETY":              ["SHIFT_MANAGER", "WAREHOUSE_COORDINATOR", "GATE_SUPERVISOR"],
    "NO_FEASIBLE_SLOT":    ["WAREHOUSE_COORDINATOR", "SHIFT_MANAGER"],
    "APPROVAL_NEEDED":     ["WAREHOUSE_COORDINATOR", "SHIFT_MANAGER"],
    "PLAN_CHANGE":         ["WAREHOUSE_COORDINATOR", "SHIFT_MANAGER"],
    "DATA_CONFLICT":       ["WAREHOUSE_COORDINATOR", "SHIFT_MANAGER"],
    "CLARIFICATION_LIMIT": ["WAREHOUSE_COORDINATOR"],
    "HUMAN_REQUESTED":     ["WAREHOUSE_COORDINATOR", "SHIFT_MANAGER"],
    "BOOKING_AT_RISK":     ["WAREHOUSE_COORDINATOR"],
}
NIGHT_FROM_HOUR = 18


def route(con, facility_id, reason, as_of):
    order = list(ROUTES.get(reason, ["WAREHOUSE_COORDINATOR"]))
    if as_of.hour >= NIGHT_FROM_HOUR:
        order.insert(0, "NIGHT_SHIFT_COORDINATOR")
    skipped = []
    for role in order:
        for c in rows(con, "SELECT * FROM facility_contacts WHERE facility_id=? AND contact_role=? AND active_flag=1", facility_id, role):
            if c["contact_name"].lower().startswith("vacant"):
                skipped.append(f"{c['contact_id']} {role}: role vacant")
                continue
            failed = c["email"] and one(con, """SELECT 1 FROM operational_messages WHERE recipient_address=? AND delivery_status='FAILED'
                                                AND sent_at<=?""", c["email"], iso(as_of))
            if c["email"] and not failed and reason not in ("SAFETY",):
                return {"contact_id": c["contact_id"], "name": c["contact_name"], "role": role, "channel": "EMAIL+DASHBOARD",
                        "address": c["email"], "backup_phone": c["phone"], "skipped": skipped}
            if c["phone"]:
                return {"contact_id": c["contact_id"], "name": c["contact_name"], "role": role, "channel": "PHONE_CALL+WHATSAPP",
                        "address": c["phone"], "skipped": skipped + ([f"{c['contact_id']}: last e-mail FAILED"] if failed else [])}
            skipped.append(f"{c['contact_id']} {role}: no usable channel")
    return {"contact_id": None, "name": "Operations queue", "role": "OPS_QUEUE", "channel": "DASHBOARD_ALARM",
            "address": "ops-dashboard", "skipped": skipped}


def escalate(con, reason, urgency, shipment_id, driver_id, as_of, handoff):
    """Write the escalation (inside the caller's transaction) and queue the notification."""
    ship = one(con, "SELECT * FROM shipments WHERE shipment_id=?", shipment_id) if shipment_id else None
    fid = ship["destination_facility_id"] if ship else "FAC-JAI-01"
    r = route(con, fid, reason, as_of)
    n = one(con, "SELECT COUNT(*) AS n FROM escalations")["n"] + 1
    eid = f"ESC-{n:04d}"
    record = dict(handoff)
    record.update({"escalation_id": eid, "reason": reason, "urgency": urgency, "routed_to": r,
                   "shipment": ship and {k: ship[k] for k in ("shipment_id", "order_reference", "priority_code", "required_dock_type",
                                                               "load_weight_kg", "expected_unload_min", "customer_name", "product_category")},
                   "driver": driver_id, "created_at": iso(as_of),
                   "respond_by": iso(add(as_of, URGENCY_SLA_MIN[urgency]))})
    con.execute("INSERT INTO escalations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (eid, shipment_id, driver_id, reason, urgency, r["name"], r["channel"], "OPEN",
                 json.dumps(record, default=str), iso(as_of), record["respond_by"]))
    m = one(con, "SELECT COUNT(*) AS n FROM outbox")["n"] + 1
    con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
                (f"OUT-{m:04d}", r["channel"], r["address"], "ESCALATION_" + reason,
                 f"[{urgency}] {reason} {shipment_id or ''}: see {eid}. Respond by {record['respond_by'][11:16]}.", "QUEUED", iso(as_of)))
    return record
