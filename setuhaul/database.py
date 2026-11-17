"""database.py: the DATA layer. Facts in, facts out; no language, no decisions.

What lives here
  * Named read queries (`Q`): every fact the service needs, by name, so a
    reviewer can see exactly which table and column each answer comes from.
  * Write helpers that only run inside `write_txn` (BEGIN IMMEDIATE): holds,
    bookings, ETA records, escalations, audit. The integrity rules that stop a
    double booking are enforced HERE, by the database, not by the model:
      - ux_live_hold_per_slot / ux_live_hold_per_shipment (partial unique indexes)
      - idempotency_keys (a retried confirm returns the first result)
      - inbound_dedupe (a re-delivered WhatsApp message is processed once)
      - production: Postgres EXCLUDE USING gist (dock_id, tstzrange) WITH &&
  * `capture_sql`: records every statement the service runs, so the situation
    walkthrough can show which tables were read and written for each case.

Who calls it
  tool.py (and through it the workflow). model.py never touches the database.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .db import MIGRATIONS, SNAPSHOT, audit, connect, prepare_working_copy, write_txn  # noqa: F401  (re-export)

# ----------------------------------------------------------- named queries --
# One line of purpose per query. `?` placeholders only (no string building).
Q: dict[str, tuple[str, str]] = {
    # who / what
    "driver": ("Driver profile and phone",
               "SELECT * FROM drivers WHERE driver_id=?"),
    "driver_open_shipments": ("All of a driver's shipments that are still live today (ambiguity check, DRV004)",
                              "SELECT shipment_id, order_reference, current_status, original_eta_ts, destination_facility_id "
                              "FROM shipments WHERE driver_id=? AND current_status NOT IN ('COMPLETED','CANCELLED') "
                              "ORDER BY original_eta_ts"),
    "shipment": ("One shipment: load, priority, dock type, reefer, weight, unload minutes",
                 "SELECT * FROM shipments WHERE shipment_id=?"),
    "vehicle": ("Vehicle type and gross weight (weight-limit rule)",
                "SELECT v.*, vt.* FROM vehicles v JOIN vehicle_types vt USING(vehicle_type_code) WHERE v.vehicle_id=?"),
    # conversation
    "thread": ("Seed chat thread (status, intent, linked shipment)",
               "SELECT * FROM chat_threads WHERE thread_id=?"),
    "thread_messages": ("Messages already exchanged on a thread",
                        "SELECT sender_type, message_ts, message_text FROM chat_messages WHERE thread_id=? ORDER BY message_ts"),
    "agent_state": ("Our conversation state machine row (state, language, remembered facts, version)",
                    "SELECT * FROM agent_conversations WHERE thread_id=?"),
    # time
    "eta_updates": ("Every ETA statement for a shipment (driver, warehouse, ops override)",
                    "SELECT * FROM eta_updates WHERE shipment_id=? ORDER BY created_at"),
    "latest_eta_view": ("Supplied view v_latest_eta (used for comparison only; its confidence is not trusted)",
                        "SELECT * FROM v_latest_eta WHERE shipment_id=?"),
    "checkin": ("Gate-in, yard queue, dock-in, unload start/end (ground truth once the truck is here)",
                "SELECT * FROM facility_checkins WHERE shipment_id=?"),
    # capacity
    "current_appointment": ("The shipment's live booking and its slot window",
                            "SELECT a.*, sl.slot_start_ts, sl.slot_end_ts, sl.dock_id, d.dock_code FROM appointments a "
                            "JOIN appointment_slots sl USING(slot_id) JOIN docks d ON d.dock_id = sl.dock_id "
                            "WHERE a.shipment_id=? AND a.is_current=1 "
                            "AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')"),
    "appointment": ("One appointment by id (APT1008 cancelled mid-conversation)",
                    "SELECT * FROM appointments WHERE appointment_id=?"),
    "facility_slots": ("All slots at a facility with dock attributes",
                       "SELECT sl.*, d.dock_code, d.dock_type, d.supports_refrigerated, d.max_vehicle_weight_kg, d.dock_status "
                       "FROM appointment_slots sl JOIN docks d USING(dock_id) WHERE sl.facility_id=? ORDER BY slot_start_ts"),
    "slot_availability_view": ("Supplied view v_slot_availability (cross-checked, not trusted alone)",
                               "SELECT * FROM v_slot_availability WHERE facility_id=?"),
    "live_holds": ("Holds still alive (5-minute reservations from other conversations)",
                   "SELECT * FROM slot_holds WHERE hold_status='HELD'"),
    "dock_events": ("Maintenance, blockages, capacity reductions on docks",
                    "SELECT e.* FROM dock_status_events e JOIN docks d USING(dock_id) WHERE d.facility_id=?"),
    "facility_rules": ("Facility rules (reefer dock, heavy dock, last new start 21:00, no-show grace)",
                       "SELECT * FROM facility_rules WHERE facility_id=? AND active_flag=1"),
    "facility_queue_view": ("Supplied view v_current_facility_queue (who is at the DC now)",
                            "SELECT * FROM v_current_facility_queue WHERE facility_id=?"),
    # people
    "contacts": ("Who to escalate to (role, name, channel); vacant posts are skipped",
                 "SELECT * FROM facility_contacts WHERE facility_id=? AND active_flag=1"),
    "ops_messages": ("Messages already sent about a shipment (OM004: escalation e-mail FAILED)",
                     "SELECT * FROM operational_messages WHERE shipment_id=? ORDER BY sent_at"),
    "open_escalations": ("Escalations waiting for a person",
                         "SELECT * FROM escalations WHERE status='OPEN' ORDER BY created_at"),
}


def q(conn: sqlite3.Connection, name: str, *args) -> list[dict]:
    """Run a named read query and return plain dicts."""
    return [dict(r) for r in conn.execute(Q[name][1], args)]


def one(conn: sqlite3.Connection, name: str, *args) -> dict | None:
    rows = q(conn, name, *args)
    return rows[0] if rows else None


# --------------------------------------------------------------- writes ----
def record_eta(conn, *, eta_update_id: str, shipment_id: str, driver_id: str | None, declared_eta_ts: str,
               confidence: str, reason: str | None, note: str | None, created_at: str, source: str = "DRIVER_DECLARED"):
    with write_txn(conn):
        conn.execute("INSERT INTO eta_updates VALUES (?,?,?,?,?,?,?,?,?)",
                     (eta_update_id, shipment_id, source, driver_id, declared_eta_ts, confidence, reason, note, created_at))
        audit(conn, created_at, "database", "ETA_RECORDED", shipment_id, f'{{"eta": "{declared_eta_ts}"}}')


def record_gate_in(conn, shipment_id: str, facility_id: str, ts: str):
    """Gate check-in event (used by the facility scheduling tool's what-if). Arrival state is
    recorded as LATE/WAITING_LATE; the scheduler only uses gate_in_ts."""
    with write_txn(conn):
        row = conn.execute("SELECT checkin_id FROM facility_checkins WHERE shipment_id=?", (shipment_id,)).fetchone()
        if row:
            conn.execute("UPDATE facility_checkins SET gate_in_ts=?, yard_queue_enter_ts=?, arrival_state='LATE',"
                         " queue_state='WAITING_LATE', updated_at=? WHERE shipment_id=?", (ts, ts, ts, shipment_id))
        else:
            conn.execute("INSERT INTO facility_checkins(checkin_id, shipment_id, facility_id, gate_in_ts, "
                         "yard_queue_enter_ts, arrival_state, queue_state, updated_at) VALUES (?,?,?,?,?, 'LATE','WAITING_LATE',?)",
                         (f"CHK-{shipment_id}", shipment_id, facility_id, ts, ts, ts))
        audit(conn, ts, "database", "GATE_IN", shipment_id)


# ---------------------------------------------------------- SQL capture ----
_WRITE = re.compile(r"^\s*(INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)
_READ = re.compile(r"\b(?:FROM|JOIN)\s+(\w+)", re.I)
_SKIP = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|PRAGMA|SAVEPOINT|RELEASE)", re.I)


@dataclass
class SqlCapture:
    statements: list[str] = field(default_factory=list)

    def _on(self, sql: str):
        if not _SKIP.match(sql):
            self.statements.append(" ".join(sql.split()))

    def tables(self) -> dict[str, list[str]]:
        reads, writes = {}, {}
        for s in self.statements:
            w = _WRITE.match(s)
            if w:
                writes[w.group(2)] = writes.get(w.group(2), 0) + 1
                body = s[w.end():]
            else:
                body = s
            for t in _READ.findall(body):
                if t.lower() not in ("select",):
                    reads[t] = reads.get(t, 0) + 1
        return {"read": sorted(reads, key=lambda t: -reads[t]), "write": sorted(writes, key=lambda t: -writes[t]),
                "read_counts": reads, "write_counts": writes}


@contextmanager
def capture_sql(conn: sqlite3.Connection):
    cap = SqlCapture()
    conn.set_trace_callback(cap._on)
    try:
        yield cap
    finally:
        conn.set_trace_callback(None)


def open_working_copy(path: str | Path) -> sqlite3.Connection:
    """Copy the supplied snapshot (left untouched), add our tables, connect."""
    return connect(prepare_working_copy(path))
