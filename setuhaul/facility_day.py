"""One facility, one day: the data a coordinator works from.

* `as_of(T)`      a working copy of the supplied database as it was KNOWN at time T
                  (later ETA updates, check-ins, dock events, bookings and cancellations
                  are removed or rolled back), so a replay never uses the future.
* `snapshot()`    docks (type, weight limit, reefer), every slot on every dock with its
                  status, bookings, unloading in progress, dock events, and every inbound
                  truck with its ETA and a deterministic status against its booking.
* `classify()`    ON_TIME / AT_RISK / WILL_MISS / NO_BOOKING / IN_YARD / UNLOADING /
                  NOT_DEPARTED / DONE / CANCELLED (thresholds in rules.yaml).
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from . import config, database as D
from .eta import resolve_eta
from .feasibility import FacilityState
from .timeutil import hhmm, iso, minutes, parse

ANOMALIES: dict[str, list[str]] = {}     # replay notes per as-of time (data issues met while rolling back)
STATUS_ORDER = ["WILL_MISS", "NO_BOOKING", "AT_RISK", "IN_YARD", "UNLOADING", "ON_TIME", "NOT_DEPARTED", "DONE", "CANCELLED"]


def as_of(t: datetime, dest: Path | None = None):
    """Working copy of the snapshot as known at `t`."""
    conn = D.open_working_copy(dest or Path(tempfile.mkdtemp()) / "asof.db")
    T = iso(t)
    with D.write_txn(conn):
        conn.execute("DELETE FROM eta_updates WHERE created_at > ?", (T,))
        conn.execute("DELETE FROM dock_status_events WHERE created_at > ?", (T,))
        conn.execute("DELETE FROM chat_messages WHERE message_ts > ?", (T,))
        conn.execute("DELETE FROM operational_messages WHERE sent_at > ?", (T,))
        conn.execute("DELETE FROM driver_exceptions WHERE reported_at > ?", (T,))
        for col in ("gate_out_ts", "unload_end_ts", "unload_start_ts", "dock_in_ts", "yard_queue_enter_ts"):
            conn.execute(f"UPDATE facility_checkins SET {col}=NULL WHERE {col} > ?", (T,))
        conn.execute("DELETE FROM facility_checkins WHERE gate_in_ts IS NULL OR gate_in_ts > ?", (T,))
        conn.execute("DELETE FROM appointments WHERE booked_at > ?", (T,))
        # roll back changes that happened after T
        anomalies = []
        for a in conn.execute("""SELECT appointment_id, slot_id, confirmed_at FROM appointments
                                 WHERE cancelled_at > ? OR (appointment_status='NO_SHOW' AND updated_at > ?)""", (T, T)).fetchall():
            st = "CONFIRMED" if a["confirmed_at"] and a["confirmed_at"] <= T else "PENDING_CONFIRMATION"
            try:
                conn.execute("""UPDATE appointments SET appointment_status=?, is_current=1, cancelled_at=NULL,
                                cancellation_reason=NULL WHERE appointment_id=?""", (st, a["appointment_id"]))
            except Exception:   # the seed data booked this slot again before cancelling it (audit finding)
                anomalies.append(f"{a['appointment_id']} on {a['slot_id']} overlaps a booking made before its cancellation")
        for a in conn.execute("""SELECT a.appointment_id, c.dock_in_ts, c.unload_end_ts, c.gate_out_ts FROM appointments a
                                 LEFT JOIN facility_checkins c USING(shipment_id)
                                 WHERE a.appointment_status IN ('COMPLETED','IN_PROGRESS')""").fetchall():
            st = "COMPLETED" if a["unload_end_ts"] else "IN_PROGRESS" if a["dock_in_ts"] else "CONFIRMED"
            conn.execute("UPDATE appointments SET appointment_status=? WHERE appointment_id=?", (st, a["appointment_id"]))
        for s in conn.execute("SELECT * FROM shipments").fetchall():
            c = conn.execute("SELECT * FROM facility_checkins WHERE shipment_id=?", (s["shipment_id"],)).fetchone()
            cancelled = conn.execute("""SELECT 1 FROM appointments WHERE shipment_id=? AND appointment_status='CANCELLED'
                                        AND cancellation_reason IN ('Customer cancelled receipt','Shipment cancelled')""",
                                     (s["shipment_id"],)).fetchone()
            if cancelled:
                st = "CANCELLED"
            elif c and (c["gate_out_ts"] or c["unload_end_ts"]):
                st = "COMPLETED"
            elif c and c["dock_in_ts"]:
                st = "IN_DOCK"
            elif c:
                st = "WAITING"
            elif s["actual_departure_ts"] and s["actual_departure_ts"] <= T:
                st = "IN_TRANSIT"
            else:
                st = "ASSIGNED"
            conn.execute("UPDATE shipments SET current_status=? WHERE shipment_id=?", (st, s["shipment_id"]))
    ANOMALIES[iso(t)] = anomalies
    return conn


def classify(conn, sh, now: datetime, appt) -> tuple[str, str, dict]:
    """Deterministic status of one truck against its current booking."""
    st = sh["current_status"]
    if st == "CANCELLED":
        return "CANCELLED", "shipment cancelled", {}
    if st == "COMPLETED":
        return "DONE", "unloaded", {}
    if st == "IN_DOCK":
        return "UNLOADING", "on the dock (protected work)", {}
    eta = resolve_eta(conn, sh["shipment_id"])
    ready = eta.point if eta.at_facility else eta.planning_eta + minutes(config.GATE_TO_DOCK_MIN)
    info = {"eta": hhmm(eta.point), "eta_conf": eta.confidence, "eta_source": eta.source, "dock_ready": hhmm(max(ready, now))}
    if st == "WAITING":
        from .solver import booking_dock_down
        down = booking_dock_down(conn, sh["shipment_id"]) if appt else None
        if down:
            return "WILL_MISS", f"in the yard, but its booked dock is down ({down})", info
        return "IN_YARD", f"in the yard since {hhmm(eta.point)}", info
    if st == "ASSIGNED" and (not sh["actual_departure_ts"] or parse(sh["actual_departure_ts"]) > now):
        if appt is None:
            return "NO_BOOKING", "not departed and no booking", info
        return "NOT_DEPARTED", f"planned departure {hhmm(parse(sh['planned_departure_ts']))}", info
    if appt is None:
        return "NO_BOOKING", "on the road with no dock booking", info
    s0, s1 = parse(appt["slot_start_ts"]), parse(appt["slot_end_ts"])
    from .solver import booking_dock_down
    down = booking_dock_down(conn, sh["shipment_id"])
    if down:
        return "WILL_MISS", f"booked dock is down ({down})", info
    if ready >= s1:
        return "WILL_MISS", f"dock-ready ~{hhmm(ready)}, booking ends {hhmm(s1)}", info
    if ready > s0 + minutes(15):
        return "AT_RISK", f"dock-ready ~{hhmm(ready)}, {int((ready - s0).total_seconds() // 60)} min into its window", info
    return "ON_TIME", f"dock-ready ~{hhmm(ready)} for {hhmm(s0)}", info


def snapshot(conn, facility_id: str, now: datetime) -> dict:
    fs = FacilityState(conn, facility_id, now)
    fac = dict(conn.execute("SELECT * FROM facilities WHERE facility_id=?", (facility_id,)).fetchone())
    docks = [dict(r) for r in conn.execute("SELECT * FROM docks WHERE facility_id=? ORDER BY dock_code", (facility_id,))]
    code = {d["dock_id"]: d["dock_code"] for d in docks}
    appts = {r["slot_id"]: dict(r) for r in conn.execute(
        """SELECT a.*, s.priority_code, s.driver_id FROM appointments a JOIN shipments s USING(shipment_id)
           WHERE a.is_current=1 AND a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION','IN_PROGRESS','COMPLETED')""")}
    slots = []
    for s in D.q(conn, "facility_slots", facility_id):
        a = appts.get(s["slot_id"])
        slots.append({"slot": s["slot_id"], "dock": s["dock_code"], "start": hhmm(parse(s["slot_start_ts"])),
                      "end": hhmm(parse(s["slot_end_ts"])), "status": s["slot_status"], "why": s["block_reason"],
                      "booked": a["shipment_id"] if a else None, "booking_status": a["appointment_status"] if a else None,
                      "priority": a["priority_code"] if a else None})
    events = [{"dock": code.get(e["dock_id"]), "type": e["event_type"], "from": hhmm(parse(e["event_start_ts"])),
               "to": hhmm(parse(e["event_end_ts"])) if e["event_end_ts"] else "22:00", "why": e["reason"]}
              for e in D.q(conn, "dock_events", facility_id)]
    work = []
    for c in conn.execute("""SELECT c.*, s.priority_code FROM facility_checkins c JOIN shipments s USING(shipment_id)
                             WHERE c.facility_id=? AND c.dock_in_ts IS NOT NULL""", (facility_id,)):
        appt = conn.execute("""SELECT sl.dock_id FROM appointments a JOIN appointment_slots sl USING(slot_id)
                               WHERE a.shipment_id=? AND a.is_current=1""", (c["shipment_id"],)).fetchone()
        dock = code.get(c["actual_dock_id"] or (appt["dock_id"] if appt else None))
        sh = conn.execute("SELECT expected_unload_min FROM shipments WHERE shipment_id=?", (c["shipment_id"],)).fetchone()
        end = parse(c["unload_end_ts"]) if c["unload_end_ts"] else max(now, parse(c["unload_start_ts"] or c["dock_in_ts"])
                                                                        + minutes(sh["expected_unload_min"]))
        work.append({"dock": dock, "shipment": c["shipment_id"], "from": hhmm(parse(c["dock_in_ts"])), "to": hhmm(end),
                     "done": bool(c["unload_end_ts"]), "priority": c["priority_code"]})
    bookings = []
    for r in conn.execute("""SELECT a.shipment_id, a.appointment_status, a.booking_source, sl.slot_start_ts, sl.slot_end_ts,
                                    d.dock_code, s.priority_code, s.expected_unload_min, p.planned_start_ts, p.planned_end_ts
                             FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN docks d ON d.dock_id=sl.dock_id
                             JOIN shipments s USING(shipment_id) LEFT JOIN appointment_plan p USING(appointment_id)
                             WHERE sl.facility_id=? AND a.is_current=1 AND a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION')""",
                          (facility_id,)):
        st = parse(r["planned_start_ts"]) if r["planned_start_ts"] else parse(r["slot_start_ts"])
        en = parse(r["planned_end_ts"]) if r["planned_end_ts"] else st + minutes(r["expected_unload_min"])
        bookings.append({"dock": r["dock_code"], "shipment": r["shipment_id"], "from": hhmm(st), "to": hhmm(en),
                         "window": f"{hhmm(parse(r['slot_start_ts']))}-{hhmm(parse(r['slot_end_ts']))}",
                         "status": r["appointment_status"], "priority": r["priority_code"],
                         "new": bool(r["planned_start_ts"])})
    trucks = []
    for sh in conn.execute("""SELECT s.*, d.driver_name, d.phone, ca.carrier_name FROM shipments s
                              JOIN drivers d USING(driver_id) JOIN carriers ca USING(carrier_id)
                              WHERE destination_facility_id=? ORDER BY original_eta_ts""", (facility_id,)).fetchall():
        appt = D.one(conn, "current_appointment", sh["shipment_id"])
        status, why, info = classify(conn, sh, now, appt)
        trucks.append({"shipment": sh["shipment_id"], "order": sh["order_reference"], "driver": sh["driver_name"],
                       "driver_id": sh["driver_id"], "phone": sh["phone"], "carrier": sh["carrier_name"],
                       "customer": sh["customer_name"], "product": sh["product_category"], "origin": sh["origin_city"],
                       "priority": sh["priority_code"], "weight_t": round(sh["load_weight_kg"] / 1000, 1),
                       "dock_type": "REEFER" if sh["temperature_control_required"] else sh["required_dock_type"],
                       "unload_min": sh["expected_unload_min"], "planned_eta": hhmm(parse(sh["original_eta_ts"])),
                       "booking": f"{appt['dock_code']} {hhmm(parse(appt['slot_start_ts']))}-{hhmm(parse(appt['slot_end_ts']))}"
                       if appt else None, "booking_status": appt["appointment_status"] if appt else None,
                       "status": status, "why": why, **info})
    trucks.sort(key=lambda t: (STATUS_ORDER.index(t["status"]), t.get("dock_ready") or "99"))
    return {"facility": {"id": facility_id, "name": fac["facility_name"], "open": fac["open_time"], "close": fac["close_time"]},
            "at": hhmm(now), "docks": [{"code": d["dock_code"], "type": d["dock_type"], "max_t": d["max_vehicle_weight_kg"] / 1000,
                                         "reefer": bool(d["supports_refrigerated"]), "status": d["dock_status"]} for d in docks],
            "slots": slots, "events": events, "work": work, "trucks": trucks, "bookings": bookings,
            "rules": [dict(r) for r in conn.execute("SELECT rule_type, rule_value, description FROM facility_rules "
                                                    "WHERE facility_id=? AND active_flag=1", (facility_id,))],
            "last_new_start": hhmm(fs.last_new_start) if fs.last_new_start else None}
