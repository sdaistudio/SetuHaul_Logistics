"""Capacity layer: SHOWN -> HELD -> PENDING_CONFIRMATION | CONFIRMED (Decisions D5, D6, D13).

* SHOWN   options computed and shown. Nothing is reserved. Any reader may see
          the same slot. Every option is re-checked before it is used.
* HELD    the driver picked an option. A row in slot_holds with a 5-minute TTL.
          ux_live_hold_per_slot makes a second hold on the slot impossible.
* PENDING_CONFIRMATION  appointment written, slot occupied (DB unique index),
          waiting for a coordinator because an approval trigger fired. Expires at
          pending_deadline(): min(booked + 15 min, slot start - 30 min), never sooner
          than 5 min after booking (review change 11).
* CONFIRMED  appointment written and committed in the system of record.

Every write runs inside BEGIN IMMEDIATE (single writer per database; in AWS the
per-facility SQS FIFO group plus a Postgres EXCLUDE constraint play this role)
and re-runs the full feasibility check *inside* the transaction. The two
partial unique indexes shipped with the database are the last line of defence.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

from . import config
from .db import audit, write_txn
from .eta import EtaEstimate, resolve_eta
from .feasibility import Constraints, FacilityState, evaluate_slot
from .timeutil import iso, minutes, parse


class CapacityError(Exception):
    def __init__(self, code: str, detail: str = "", reasons: list[str] | None = None):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.reasons = code, detail, reasons or []


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _dt(v: str) -> datetime:
    return parse(v)


def pending_deadline(booked_at: datetime, planned_start: datetime | None) -> datetime:
    """When an unanswered approval lapses: min(booked + 15, start - 30), floor booked + 5."""
    d = booked_at + minutes(config.PENDING_CONFIRMATION_TTL_MIN)
    if planned_start is not None:
        d = min(d, planned_start - minutes(config.PENDING_LEAD_BEFORE_START_MIN))
    return max(d, booked_at + minutes(config.PENDING_MIN_DECISION_MIN))


def expire_stale(conn, now: datetime) -> dict:
    """Called inside a write txn and by the 1-minute sweeper."""
    n_h = conn.execute(
        "UPDATE slot_holds SET hold_status='EXPIRED', closed_at=? WHERE hold_status='HELD' AND expires_at<=?",
        (iso(now), iso(now))).rowcount
    rows = [r for r in conn.execute(
        "SELECT a.appointment_id, a.booked_at, p.planned_start_ts FROM appointments a "
        "LEFT JOIN appointment_plan p ON p.appointment_id=a.appointment_id "
        "WHERE a.appointment_status='PENDING_CONFIRMATION' AND a.booking_source='DRIVER_CHAT'").fetchall()
        if pending_deadline(_dt(r["booked_at"]), _dt(r["planned_start_ts"]) if r["planned_start_ts"] else None) <= now]
    for r in rows:
        conn.execute("UPDATE appointments SET appointment_status='REJECTED', is_current=0, "
                     "cancellation_reason='Warehouse confirmation timed out', cancelled_at=?, updated_at=? "
                     "WHERE appointment_id=?", (iso(now), iso(now), r["appointment_id"]))
        audit(conn, iso(now), "sweeper", "PENDING_EXPIRED", r["appointment_id"])
    return {"holds_expired": n_h, "pending_expired": len(rows)}


def _check(conn, shipment_id, slot_id, now, cons, eta):
    sh = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
    slot = conn.execute("SELECT * FROM appointment_slots WHERE slot_id=?", (slot_id,)).fetchone()
    if sh is None or slot is None:
        raise CapacityError("NOT_FOUND", f"{shipment_id}/{slot_id}")
    fs = FacilityState(conn, sh["destination_facility_id"], now, exclude_shipment=shipment_id)
    ev = evaluate_slot(fs, sh, slot, eta or resolve_eta(conn, shipment_id), cons or Constraints())
    ev.released = fs.missed.get(slot_id)
    return sh, slot, ev


def place_hold(conn, shipment_id: str, slot_id: str, now: datetime, thread_id: str | None = None,
               cons: Constraints | None = None, eta: EtaEstimate | None = None) -> dict:
    try:
        with write_txn(conn):
            expire_stale(conn, now)
            conn.execute("UPDATE slot_holds SET hold_status='RELEASED', closed_at=? "
                         "WHERE shipment_id=? AND hold_status='HELD'", (iso(now), shipment_id))
            _, _, ev = _check(conn, shipment_id, slot_id, now, cons, eta)
            if not ev.feasible:
                raise CapacityError("OPTION_GONE", ev.label(), ev.reasons)
            hold_id = _new_id("HOLD")
            conn.execute(
                "INSERT INTO slot_holds(hold_id, slot_id, shipment_id, thread_id, hold_status, planned_start_ts,"
                " planned_end_ts, created_at, expires_at) VALUES (?,?,?,?, 'HELD', ?,?,?,?)",
                (hold_id, slot_id, shipment_id, thread_id, iso(ev.planned_start), iso(ev.planned_end),
                 iso(now), iso(now + minutes(config.HOLD_TTL_MIN))))
            audit(conn, iso(now), "capacity", "HOLD_PLACED", hold_id,
                  json.dumps({"slot": slot_id, "shipment": shipment_id}))
            return {"hold_id": hold_id, "slot_id": slot_id, "eval": ev,
                    "expires_at": iso(now + minutes(config.HOLD_TTL_MIN))}
    except sqlite3.IntegrityError as e:
        raise CapacityError("OPTION_GONE", f"race lost on {slot_id}: {e}") from e


def commit_hold(conn, hold_id: str, now: datetime, idem_key: str, approval_reasons: list[str] | None = None,
                cons: Constraints | None = None, eta: EtaEstimate | None = None, actor: str = "driver_chat") -> dict:
    approval_reasons = approval_reasons or []
    try:
        with write_txn(conn):
            prev = conn.execute("SELECT result_json FROM idempotency_keys WHERE idem_key=?", (idem_key,)).fetchone()
            if prev:
                return json.loads(prev["result_json"]) | {"idempotent_replay": True}
            expire_stale(conn, now)
            h = conn.execute("SELECT * FROM slot_holds WHERE hold_id=?", (hold_id,)).fetchone()
            if h is None or h["hold_status"] != "HELD":
                raise CapacityError("HOLD_EXPIRED", f"{hold_id} is {h['hold_status'] if h else 'missing'}")
            sh, slot, ev = _check(conn, h["shipment_id"], h["slot_id"], now, cons, eta)
            if not ev.feasible:
                conn.execute("UPDATE slot_holds SET hold_status='REJECTED', closed_at=? WHERE hold_id=?",
                             (iso(now), hold_id))
                raise CapacityError("OPTION_GONE", ev.label(), ev.reasons)
            if ev.needs_approval:
                approval_reasons = approval_reasons + ["LATE_START_RULE"]
            old = conn.execute(
                "SELECT appointment_id FROM appointments WHERE shipment_id=? AND is_current=1 "
                "AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')", (sh["shipment_id"],)).fetchone()
            if old:
                conn.execute("UPDATE appointments SET appointment_status='CANCELLED', is_current=0, cancelled_at=?,"
                             " cancellation_reason='Replaced after driver ETA change', updated_at=? "
                             "WHERE appointment_id=?", (iso(now), iso(now), old["appointment_id"]))
            if getattr(ev, "released", None):
                dead_appt, dead_sid = ev.released
                conn.execute("UPDATE appointments SET appointment_status='CANCELLED', is_current=0, cancelled_at=?,"
                             " cancellation_reason='Released: truck ETA beyond window (certain miss)', updated_at=?"
                             " WHERE appointment_id=? AND appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION')",
                             (iso(now), iso(now), dead_appt))
                conn.execute("INSERT INTO operational_messages VALUES (?,?,?,'INTERNAL','capacity@setuhaul.example',"
                             "'dispatch',?,?,?, 'QUEUED', NULL)",
                             (_new_id("OM"), dead_sid, dead_appt, "Missed slot released - re-plan truck",
                              f"{dead_sid}'s booking on {slot['slot_id']} released: ETA cannot reach the window.",
                              iso(now)))
                audit(conn, iso(now), "capacity", "CERTAIN_MISS_RELEASED", dead_appt, json.dumps({"for": sh["shipment_id"]}))
            status = "PENDING_CONFIRMATION" if (approval_reasons or not config.AUTO_CONFIRM) else "CONFIRMED"
            appt_id = _new_id("APT")
            conn.execute(
                "INSERT INTO appointments(appointment_id, shipment_id, slot_id, appointment_status, booking_source,"
                " is_current, booked_at, confirmed_at, replaced_appointment_id, warehouse_confirmation_ref, updated_at)"
                " VALUES (?,?,?,?, 'DRIVER_CHAT', 1, ?,?,?,?,?)",
                (appt_id, sh["shipment_id"], slot["slot_id"], status, iso(now),
                 iso(now) if status == "CONFIRMED" else None, old["appointment_id"] if old else None,
                 f"AUTO-{appt_id}" if status == "CONFIRMED" else None, iso(now)))
            conn.execute("INSERT INTO appointment_plan VALUES (?,?,?,?)",
                         (appt_id, slot["dock_id"], iso(ev.planned_start), iso(ev.planned_end)))
            conn.execute("UPDATE slot_holds SET hold_status='CONVERTED', closed_at=? WHERE hold_id=?",
                         (iso(now), hold_id))
            result = {"appointment_id": appt_id, "status": status, "slot_id": slot["slot_id"],
                      "dock": ev.dock_code, "planned_start": iso(ev.planned_start),
                      "planned_end": iso(ev.planned_end), "replaced": old["appointment_id"] if old else None,
                      "approval_reasons": approval_reasons}
            conn.execute("INSERT INTO idempotency_keys VALUES (?,?,?)", (idem_key, json.dumps(result), iso(now)))
            audit(conn, iso(now), actor, f"BOOKING_{status}", appt_id, json.dumps(result))
            return result
    except sqlite3.IntegrityError as e:
        raise CapacityError("SLOT_TAKEN", str(e)) from e


def book(conn, shipment_id: str, slot_id: str, now: datetime, idem_key: str | None = None, **kw) -> dict:
    """Hold + commit in one call (used by the agent once the driver chose, and by tests)."""
    cons, eta = kw.get("cons"), kw.get("eta")
    hold = place_hold(conn, shipment_id, slot_id, now, kw.get("thread_id"), cons, eta)
    return commit_hold(conn, hold["hold_id"], now, idem_key or f"book:{hold['hold_id']}",
                       kw.get("approval_reasons"), cons, eta, kw.get("actor", "driver_chat"))


def decide_pending(conn, appointment_id: str, approve: bool, approver: str, now: datetime) -> dict:
    """Coordinator decision on a PENDING_CONFIRMATION booking (human-in-the-loop)."""
    with write_txn(conn):
        a = conn.execute("SELECT * FROM appointments WHERE appointment_id=?", (appointment_id,)).fetchone()
        if a is None or a["appointment_status"] != "PENDING_CONFIRMATION":
            raise CapacityError("NOT_PENDING", appointment_id)
        if approve:
            # A person's approval is still checked: feasibility is code, not opinion.
            sl = conn.execute("SELECT * FROM appointment_slots WHERE slot_id=?", (a["slot_id"],)).fetchone()
            sh = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (a["shipment_id"],)).fetchone()
            fs = FacilityState(conn, sh["destination_facility_id"], now, exclude_shipment=sh["shipment_id"])
            ev = evaluate_slot(fs, sh, sl, resolve_eta(conn, sh["shipment_id"]), Constraints())
            if not ev.feasible:
                audit(conn, iso(now), approver, "APPROVAL_REFUSED_INFEASIBLE", appointment_id, json.dumps(ev.reasons))
                return {"appointment_id": appointment_id, "status": "REFUSED_INFEASIBLE", "reasons": ev.reasons}
            conn.execute("UPDATE appointments SET appointment_status='CONFIRMED', confirmed_at=?, "
                         "warehouse_confirmation_ref=?, updated_at=? WHERE appointment_id=?",
                         (iso(now), f"WH-{approver}", iso(now), appointment_id))
        else:
            conn.execute("UPDATE appointments SET appointment_status='REJECTED', is_current=0, cancelled_at=?, "
                         "cancellation_reason='Rejected by warehouse', updated_at=? WHERE appointment_id=?",
                         (iso(now), iso(now), appointment_id))
        audit(conn, iso(now), approver, "PENDING_APPROVED" if approve else "PENDING_REJECTED", appointment_id)
        return {"appointment_id": appointment_id, "status": "CONFIRMED" if approve else "REJECTED"}
