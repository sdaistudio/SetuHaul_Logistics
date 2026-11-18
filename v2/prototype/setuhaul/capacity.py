"""The capacity layer: the only code that writes bookings.

States of an option, from the driver's point of view:
    SHOWN            read-only; reserves nothing
    PICKED -> CONFIRMED            policy allows it: one transaction re-checks and books
    PICKED -> HELD_FOR_APPROVAL    a rule needs a person: the slot is held until a decision deadline
           -> CONFIRMED | REJECTED | EXPIRED
A promise to the driver ("Booked") is only ever sent after COMMIT returns.

Every write: BEGIN IMMEDIATE -> idempotency check -> fresh feasibility check ->
release dead bookings -> retire the old booking -> insert -> DB guards fire ->
history + outbox + audit -> COMMIT.
"""
import json, sqlite3
from . import config as C
from .clock import ts, iso, add, hhmm, minutes_between
from .db import write_txn, one, rows
from . import timeline as T
from . import escalation as E


class GuardTripped(Exception):
    """A database guard rejected a write that the feasibility code allowed. Never expected; always logged."""


def _next_id(con, table, col, prefix):
    n = one(con, f"SELECT COUNT(*) AS n FROM {table} WHERE {col} LIKE ?", prefix + "%")["n"]
    return f"{prefix}{n + 1:04d}"


def _idem_get(con, key):
    if not key:
        return None
    r = one(con, "SELECT result_json FROM idempotency_keys WHERE key=?", key)
    return dict(json.loads(r["result_json"]), replayed=True) if r else None


def _idem_put(con, key, result, as_of):
    if key:
        con.execute("INSERT INTO idempotency_keys VALUES (?,?,?)", (key, json.dumps(result), iso(as_of)))


def _audit(con, as_of, actor, action, detail):
    con.execute("INSERT INTO audit_log(at, actor, action, detail_json) VALUES (?,?,?,?)",
                (iso(as_of), actor, action, json.dumps(detail, default=str)))


def _event(con, appt_id, frm, to, actor, reason, as_of):
    con.execute("INSERT INTO appointment_events(appointment_id, from_status, to_status, actor, reason, at) VALUES (?,?,?,?,?,?)",
                (appt_id, frm, to, actor, reason, iso(as_of)))


def outbox(con, as_of, channel, recipient, kind, body):
    mid = _next_id(con, "outbox", "msg_id", "OUT-")
    con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?)", (mid, channel, recipient, kind, body, "QUEUED", iso(as_of)))
    return mid


def _cancel(con, appt_id, reason, actor, as_of):
    a = one(con, "SELECT * FROM appointments WHERE appointment_id=?", appt_id)
    con.execute("""UPDATE appointments SET appointment_status='CANCELLED', is_current=0, cancelled_at=?,
                   cancellation_reason=?, updated_at=? WHERE appointment_id=?""", (iso(as_of), reason, iso(as_of), appt_id))
    con.execute("UPDATE dock_reservations SET live=0 WHERE ref_id=?", (appt_id,))
    _event(con, appt_id, a["appointment_status"], "CANCELLED", actor, reason, as_of)
    return a


def refresh_guard_rows(con, facility_id, as_of, exclude=()):
    """Mirror the fresh timeline into dock_reservations so the overlap trigger
    sees every interval the feasibility code saw (in Postgres, EXCLUDE does this natively)."""
    con.execute("""UPDATE dock_reservations SET live=0 WHERE live=1 AND kind IN ('KEPT_BOOKING','IN_DOCK','DOCK_EVENT','BOOKING')
                   AND dock_id IN (SELECT dock_id FROM docks WHERE facility_id=?)""", (facility_id,))
    con.execute("""UPDATE dock_reservations SET live=0 WHERE kind='HOLD' AND ref_id IN
                   (SELECT hold_id FROM slot_holds WHERE hold_status NOT IN ('ACTIVE','PENDING_APPROVAL') OR expires_at<=?)""", (iso(as_of),))
    tl = T.build(con, facility_id, as_of, exclude=exclude)
    n = one(con, "SELECT COUNT(*) AS n FROM dock_reservations")["n"]
    for dock_id, ivs in tl.busy.items():
        for iv in ivs:
            if iv.kind in ("HOLD",):
                continue
            n += 1
            kind = {"BLOCKED_SLOT": "DOCK_EVENT"}.get(iv.kind, iv.kind)
            con.execute("INSERT INTO dock_reservations VALUES (?,?,?,?,?,?,1,?)",
                        (f"R{n:06d}", dock_id, iso(iv.start), iso(iv.end), kind, iv.ref, iso(as_of)))
    return tl


def current_appointment(con, shipment_id):
    return one(con, f"""SELECT a.*, sl.dock_id, sl.slot_start_ts, sl.slot_end_ts, d.dock_code FROM appointments a
                        JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
                        WHERE a.shipment_id=? AND a.is_current=1 AND a.appointment_status IN {T.ACTIVE}""", shipment_id)


def _why_gone(con, shipment_id, slot_id, as_of):
    """Plain reason a picked slot is no longer feasible (for the refusal message)."""
    occ = one(con, f"SELECT appointment_id, shipment_id FROM appointments WHERE slot_id=? AND appointment_status IN {T.ACTIVE}", slot_id)
    if occ and occ["shipment_id"] != shipment_id:
        return "TAKEN_BY_ANOTHER_TRUCK"
    if one(con, "SELECT 1 FROM slot_holds WHERE slot_id=? AND hold_status IN ('ACTIVE','PENDING_APPROVAL') AND expires_at>?", slot_id, iso(as_of)):
        return "HELD_FOR_ANOTHER_TRUCK"
    if one(con, "SELECT 1 FROM slot_earmarks WHERE slot_id=? AND shipment_id<>? AND expires_at>?", slot_id, shipment_id, iso(as_of)):
        return "PROTECTED_FOR_PRIORITY_LOAD"
    return "NO_LONGER_FITS"


def driver_limits(con, shipment_id, as_of):
    """Stored driver limits (typed: EARLIEST_START, LATEST_GATE_OUT, ...). Re-applied at every commit."""
    out = {}
    for r in rows(con, "SELECT * FROM driver_constraints WHERE shipment_id=? AND created_at<=? ORDER BY created_at", shipment_id, iso(as_of)):
        out[r["constraint_type"]] = ts(r["limit_ts"])
    earliest = out.pop("EARLIEST_START", None)
    return out, earliest


def _commit_core(con, shipment_id, slot_id, as_of, source, actor, approved=False):
    lim, earliest = driver_limits(con, shipment_id, as_of)
    opts, _ = T.options(con, shipment_id, as_of, limit=None, respect_earmarks=not approved, constraints=lim, earliest_start=earliest)
    match = [o for o in opts if o["slot_id"] == slot_id]
    if not match:
        return {"ok": False, "code": "OPTION_GONE", "why": _why_gone(con, shipment_id, slot_id, as_of), "slot_id": slot_id}
    o = match[0]
    if o["needs_approval"] and not approved:
        return {"ok": False, "code": "NEEDS_APPROVAL", "option": o}
    ship = T.shipment(con, shipment_id)
    released = []
    for dead in o["releases"]:
        a = _cancel(con, dead, "Released by planner: truck cannot reach this window", actor, as_of)
        dship = T.shipment(con, a["shipment_id"])
        carrier = one(con, "SELECT * FROM carriers WHERE carrier_id=?", dship["carrier_id"])
        outbox(con, as_of, "EMAIL", carrier["contact_email"], "DISPATCH_NOTICE",
               f"{a['shipment_id']}: booking {dead} released at {hhmm(as_of)} because the truck cannot reach its window. Re-planning has started.")
        released.append(dead)
    old = current_appointment(con, shipment_id)
    if old:
        _cancel(con, old["appointment_id"], "Rescheduled", actor, as_of)
    refresh_guard_rows(con, ship["destination_facility_id"], as_of, exclude=(shipment_id,))
    appt_id = _next_id(con, "appointments", "appointment_id", "APT-N")
    try:
        con.execute("""INSERT INTO appointments VALUES (?,?,?, 'CONFIRMED', ?, 1, ?, ?, NULL, NULL, ?, ?, ?)""",
                    (appt_id, shipment_id, slot_id, source, iso(as_of), iso(as_of),
                     old["appointment_id"] if old else None, f"SVC-{appt_id}", iso(as_of)))
        n = one(con, "SELECT COUNT(*) AS n FROM dock_reservations")["n"] + 1
        con.execute("INSERT INTO dock_reservations VALUES (?,?,?,?,'BOOKING',?,1,?)",
                    (f"R{n:06d}", o["dock_id"], o["start"], o["occupancy_end"], appt_id, iso(as_of)))
    except sqlite3.IntegrityError as ex:
        raise GuardTripped(str(ex))
    _event(con, appt_id, None, "CONFIRMED", actor, f"{source}: {o['dock_code']} start {hhmm(ts(o['start']))}", as_of)
    con.execute("UPDATE shipments SET updated_at=? WHERE shipment_id=?", (iso(as_of), shipment_id))
    con.execute("DELETE FROM slot_earmarks WHERE shipment_id=? OR slot_id=?", (shipment_id, slot_id))
    coord = one(con, "SELECT * FROM facility_contacts WHERE facility_id=? AND contact_role='WAREHOUSE_COORDINATOR'", ship["destination_facility_id"])
    if coord and coord["email"]:
        outbox(con, as_of, "EMAIL", coord["email"], "BOOKING_NOTICE",
               f"{shipment_id} booked on {o['dock_code']}, planned start {hhmm(ts(o['start']))} (slot {slot_id})" +
               (f", replaces {old['appointment_id']}" if old else ""))
    _audit(con, as_of, actor, "BOOK", {"shipment": shipment_id, "appointment": appt_id, "option": o, "released": released})
    return {"ok": True, "code": "CONFIRMED", "appointment_id": appt_id, "option": o,
            "replaced": old["appointment_id"] if old else None, "released": released}


def commit(con, shipment_id, slot_id, as_of, source="DRIVER_CHAT", actor="workflow", idem_key=None, approved=False):
    """Book one slot. Safe to retry: the same idem_key returns the first result."""
    with write_txn(con):
        prior = _idem_get(con, idem_key)
        if prior:
            return prior
        res = _commit_core(con, shipment_id, slot_id, as_of, source, actor, approved)
        _idem_put(con, idem_key, res, as_of)
        return res


def hold_for_approval(con, shipment_id, slot_id, as_of, driver_id, reasons, idem_key=None):
    """Driver picked an option that a rule says a person must approve."""
    with write_txn(con):
        prior = _idem_get(con, idem_key)
        if prior:
            return prior
        opts, _ = T.options(con, shipment_id, as_of, limit=None)
        match = [o for o in opts if o["slot_id"] == slot_id]
        if not match:
            res = {"ok": False, "code": "OPTION_GONE", "why": _why_gone(con, shipment_id, slot_id, as_of)}
            _idem_put(con, idem_key, res, as_of)
            return res
        o = match[0]
        deadline = min(add(as_of, C.APPROVAL_DEADLINE_MAX_MIN), add(ts(o["start"]), -C.APPROVAL_LEAD_BEFORE_START_MIN))
        if deadline <= add(as_of, 5):
            res = {"ok": False, "code": "TOO_LATE_FOR_APPROVAL", "option": o}
            _idem_put(con, idem_key, res, as_of)
            return res
        ship = T.shipment(con, shipment_id)
        refresh_guard_rows(con, ship["destination_facility_id"], as_of, exclude=(shipment_id,))
        hold_id = _next_id(con, "slot_holds", "hold_id", "HLD-")
        con.execute("INSERT INTO slot_holds VALUES (?,?,?,?,?,?,?, 'PENDING_APPROVAL', ?, ?, ?, NULL)",
                    (hold_id, slot_id, shipment_id, driver_id, o["dock_id"], o["start"], o["occupancy_end"],
                     ",".join(reasons), iso(as_of), iso(deadline)))
        n = one(con, "SELECT COUNT(*) AS n FROM dock_reservations")["n"] + 1
        con.execute("INSERT INTO dock_reservations VALUES (?,?,?,?,'HOLD',?,1,?)",
                    (f"R{n:06d}", o["dock_id"], o["start"], o["occupancy_end"], hold_id, iso(as_of)))
        esc = E.escalate(con, "APPROVAL_NEEDED", "HIGH", shipment_id, driver_id, as_of, {
            "decision_needed": f"Approve {ship['shipment_id']} on {o['dock_code']} at {hhmm(ts(o['start']))}?",
            "rule_triggered": reasons, "hold_id": hold_id, "decide_by": iso(deadline), "option": o,
            "if_no_decision": "Hold expires, slot returns to the pool, driver is told and re-offered."})
        res = {"ok": True, "code": "HELD_FOR_APPROVAL", "hold_id": hold_id, "deadline": iso(deadline), "option": o,
               "escalation_id": esc["escalation_id"]}
        _audit(con, as_of, "workflow", "HOLD_FOR_APPROVAL", res)
        _idem_put(con, idem_key, res, as_of)
        return res


def decide(con, hold_id, approve, coordinator, as_of, note=""):
    """A coordinator approves or rejects a held option. Approval is re-checked, never trusted blindly."""
    with write_txn(con):
        h = one(con, "SELECT * FROM slot_holds WHERE hold_id=?", hold_id)
        if h["hold_status"] != "PENDING_APPROVAL" or ts(h["expires_at"]) <= as_of:
            return {"ok": False, "code": "HOLD_NOT_LIVE", "hold_status": h["hold_status"]}
        con.execute("UPDATE dock_reservations SET live=0 WHERE ref_id=?", (hold_id,))
        if not approve:
            con.execute("UPDATE slot_holds SET hold_status='REJECTED', closed_at=?, reason=reason||' | rejected: '||? WHERE hold_id=?",
                        (iso(as_of), note, hold_id))
            return {"ok": True, "code": "REJECTED", "hold": h}
        con.execute("UPDATE slot_holds SET hold_status='CONVERTED', closed_at=? WHERE hold_id=?", (iso(as_of), hold_id))
        res = _commit_core(con, h["shipment_id"], h["slot_id"], as_of, "WAREHOUSE", coordinator, approved=True)
        if not res["ok"]:
            con.execute("UPDATE slot_holds SET hold_status='REJECTED', reason=reason||' | re-check failed' WHERE hold_id=?", (hold_id,))
        return res


def sweep(con, as_of):
    """Expire holds whose decision deadline passed. Returns the holds expired (the workflow tells the drivers)."""
    with write_txn(con):
        gone = rows(con, "SELECT * FROM slot_holds WHERE hold_status IN ('ACTIVE','PENDING_APPROVAL') AND expires_at<=?", iso(as_of))
        for h in gone:
            con.execute("UPDATE slot_holds SET hold_status='EXPIRED', closed_at=? WHERE hold_id=?", (iso(as_of), h["hold_id"]))
            con.execute("UPDATE dock_reservations SET live=0 WHERE ref_id=?", (h["hold_id"],))
            _audit(con, as_of, "sweeper", "HOLD_EXPIRED", h)
        con.execute("DELETE FROM slot_earmarks WHERE expires_at<=?", (iso(as_of),))
        return gone


def cancel_shipment(con, shipment_id, as_of, reason, actor="dispatch"):
    """Dispatcher cancels a shipment: its booking is cancelled and the capacity returns to the pool."""
    with write_txn(con):
        con.execute("UPDATE shipments SET current_status='CANCELLED', updated_at=? WHERE shipment_id=?", (iso(as_of), shipment_id))
        a = current_appointment(con, shipment_id)
        if a:
            _cancel(con, a["appointment_id"], reason, actor, as_of)
        _audit(con, as_of, actor, "CANCEL_SHIPMENT", {"shipment": shipment_id, "appointment": a and a["appointment_id"]})
        return a


def _swap_core(con, high, low, as_of, actor):
    """High-priority truck takes the low-priority truck's slot; the low truck gets the best remaining option."""
    hi, lo = current_appointment(con, high), current_appointment(con, low)
    if not hi or not lo:
        return {"ok": False, "code": "NO_BOOKINGS_TO_SWAP"}
    _cancel(con, lo["appointment_id"], f"Moved by coordinator for priority load {high}", actor, as_of)
    r1 = _commit_core(con, high, lo["slot_id"], as_of, "MANUAL_OVERRIDE", actor, approved=True)
    if not r1["ok"]:
        return {"ok": False, "code": "HIGH_CANNOT_TAKE_SLOT", "detail": r1}
    lim, earliest = driver_limits(con, low, as_of)
    opts, _ = T.options(con, low, as_of, limit=None, respect_earmarks=False, constraints=lim, earliest_start=earliest)
    if not opts:
        why = ", ".join(f"{k.lower().replace('_', ' ')} {hhmm(v)}" for k, v in lim.items()) or "no compatible slot left"
        return {"ok": False, "code": "LOW_HAS_NO_ALTERNATIVE", "why": f"{low} has no other slot that keeps its limits ({why})"}
    pref = [o for o in opts if o["slot_id"] == hi["slot_id"]] or opts
    r2 = _commit_core(con, low, pref[0]["slot_id"], as_of, "MANUAL_OVERRIDE", actor, approved=True)
    if not r2["ok"]:
        return {"ok": False, "code": "LOW_REBOOK_FAILED", "detail": r2}
    return {"ok": True, "code": "SWAPPED", "high": r1, "low": r2, "high_old": hi, "low_old": lo}


def trial_swap(con, high, low, as_of):
    """Run the swap inside a transaction that is always rolled back: exact answer, no side effects."""
    con.execute("BEGIN IMMEDIATE")
    try:
        return _swap_core(con, high, low, as_of, "trial")
    except GuardTripped as ex:
        return {"ok": False, "code": "GUARD", "detail": str(ex)}
    finally:
        con.execute("ROLLBACK")


def swap(con, high, low, as_of, actor, idem_key=None):
    """Coordinator-approved swap: both bookings change in ONE transaction, or neither does."""
    with write_txn(con):
        prior = _idem_get(con, idem_key)
        if prior:
            return prior
        res = _swap_core(con, high, low, as_of, actor)
        if not res["ok"]:
            raise RuntimeError(res["code"])
        _audit(con, as_of, actor, "SWAP", {"high": high, "low": low})
        _idem_put(con, idem_key, res, as_of)
        return res
