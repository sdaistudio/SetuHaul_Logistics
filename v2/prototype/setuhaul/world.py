"""Scenario world: rewind the snapshot to a start time, then replay events.

The snapshot mixes rows from 07:10 to 16:30 (audit F15). To replay a day
honestly we rewind a copy to 08:20: rows created later are removed and are
re-applied at their own time (gate check-ins, the D3 breakdown, ETA updates,
the SHP1019 cancellation). Driver messages are replayed through the workflow.

One thing cannot be rewound: SLOT-JAI-003 held two CONFIRMED bookings until
09:40 (audit F08). Restoring both would violate the unique index, so APT1012A
stays cancelled. That refusal is itself evidence that the index works.
"""
from .clock import ts, iso, add
from .db import write_txn, one, rows


def rewind(con, as_of):
    A = iso(as_of)
    notes = []
    with write_txn(con):
        con.execute("DELETE FROM operational_messages WHERE sent_at > ? OR appointment_id IN (SELECT appointment_id FROM appointments WHERE booked_at > ?)", (A, A))
        for a in rows(con, "SELECT appointment_id FROM appointments WHERE booked_at > ?", A):
            notes.append(f"removed {a['appointment_id']} (booked after {A[11:16]})")
        con.execute("DELETE FROM appointments WHERE booked_at > ?", (A,))
        for a in rows(con, "SELECT * FROM appointments WHERE appointment_status='CANCELLED' AND cancelled_at > ?", A):
            clash = one(con, """SELECT appointment_id FROM appointments WHERE slot_id=? AND appointment_status IN
                                ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""", a["slot_id"])
            if clash:
                notes.append(f"kept {a['appointment_id']} cancelled: restoring it would double-book {a['slot_id']} with {clash['appointment_id']} (audit F08)")
                continue
            con.execute("""UPDATE appointments SET appointment_status='CONFIRMED', is_current=1, cancelled_at=NULL,
                           cancellation_reason=NULL, updated_at=booked_at WHERE appointment_id=?""", (a["appointment_id"],))
            notes.append(f"restored {a['appointment_id']} (cancelled later at {a['cancelled_at'][11:16]})")
        con.execute("UPDATE appointments SET appointment_status='CONFIRMED', is_current=1, updated_at=booked_at WHERE appointment_status='NO_SHOW' AND updated_at > ?", (A,))
        con.execute("UPDATE appointments SET appointment_status='IN_PROGRESS', updated_at=? WHERE appointment_status='COMPLETED' AND updated_at > ?", (A, A))
        con.execute("""UPDATE shipments SET current_status='ASSIGNED' WHERE current_status='CANCELLED' AND updated_at > ?
                       AND shipment_id NOT IN (SELECT shipment_id FROM appointments WHERE cancelled_at <= ?)""", (A, A))
        con.execute("UPDATE shipments SET actual_departure_ts=NULL, current_status='ASSIGNED' WHERE actual_departure_ts > ?", (A,))
        con.execute("""UPDATE shipments SET current_status='IN_TRANSIT' WHERE shipment_id IN
                       (SELECT shipment_id FROM facility_checkins WHERE gate_in_ts > ?)""", (A,))
        con.execute("UPDATE shipments SET current_status='IN_DOCK' WHERE shipment_id IN (SELECT shipment_id FROM facility_checkins WHERE dock_in_ts<=? AND (unload_end_ts IS NULL OR unload_end_ts > ?))", (A, A))
        con.execute("DELETE FROM facility_checkins WHERE gate_in_ts > ?", (A,))
        for col in ("unload_end_ts", "gate_out_ts", "dock_in_ts", "unload_start_ts"):
            con.execute(f"UPDATE facility_checkins SET {col}=NULL WHERE {col} > ?", (A,))
        con.execute("UPDATE facility_checkins SET queue_state='IN_DOCK' WHERE dock_in_ts IS NOT NULL AND unload_end_ts IS NULL")
        con.execute("DELETE FROM dock_status_events WHERE created_at > ?", (A,))
        con.execute("DELETE FROM eta_updates WHERE created_at > ?", (A,))
        for s in rows(con, "SELECT * FROM shipments"):
            u = one(con, "SELECT declared_eta_ts FROM eta_updates WHERE shipment_id=? ORDER BY created_at DESC LIMIT 1", s["shipment_id"])
            if u:
                con.execute("UPDATE shipments SET latest_eta_ts=? WHERE shipment_id=?", (u["declared_eta_ts"], s["shipment_id"]))
            elif s["updated_at"] > A:
                con.execute("UPDATE shipments SET latest_eta_ts=original_eta_ts WHERE shipment_id=?", (s["shipment_id"],))
        con.execute("DELETE FROM operational_messages WHERE sent_at > ?", (A,))
    return notes


# ---------------------------------------------------------------- world events
def gate_in(con, sid, at, arrival_state):
    fid = one(con, "SELECT destination_facility_id AS f FROM shipments WHERE shipment_id=?", sid)["f"]
    with write_txn(con):
        n = one(con, "SELECT COUNT(*) AS n FROM facility_checkins")["n"] + 1
        con.execute("""INSERT INTO facility_checkins(checkin_id, shipment_id, facility_id, gate_in_ts, yard_queue_enter_ts, arrival_state,
                       queue_state, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (f"CHK-W{n:03d}", sid, fid, iso(at), iso(add(at, 1)), arrival_state, "WAITING_" + ("EARLY" if arrival_state == "EARLY" else "LATE"), iso(at)))
        con.execute("UPDATE shipments SET current_status='WAITING', updated_at=? WHERE shipment_id=?", (iso(at), sid))


def dock_in(con, sid, dock_id, at):
    with write_txn(con):
        con.execute("""UPDATE facility_checkins SET dock_in_ts=?, unload_start_ts=?, actual_dock_id=?, queue_state='IN_DOCK', updated_at=?
                       WHERE shipment_id=?""", (iso(at), iso(add(at, 3)), dock_id, iso(at), sid))
        con.execute("UPDATE shipments SET current_status='IN_DOCK', updated_at=? WHERE shipment_id=?", (iso(at), sid))
        con.execute("""UPDATE appointments SET appointment_status='IN_PROGRESS', updated_at=? WHERE shipment_id=? AND is_current=1
                       AND appointment_status='CONFIRMED'""", (iso(at), sid))


def unload_done(con, sid, at, gate_out_after=8):
    with write_txn(con):
        con.execute("""UPDATE facility_checkins SET unload_end_ts=?, gate_out_ts=?, queue_state='COMPLETED', updated_at=? WHERE shipment_id=?""",
                    (iso(at), iso(add(at, gate_out_after)), iso(at), sid))
        con.execute("UPDATE shipments SET current_status='COMPLETED', updated_at=? WHERE shipment_id=?", (iso(at), sid))
        con.execute("UPDATE appointments SET appointment_status='COMPLETED', updated_at=? WHERE shipment_id=? AND is_current=1", (iso(at), sid))


def dock_event(con, eid, dock_id, etype, start, end, reason, created):
    with write_txn(con):
        con.execute("INSERT INTO dock_status_events VALUES (?,?,?,?,?,?,?)", (eid, dock_id, etype, iso(start), iso(end) if end else None, reason, iso(created)))


def eta_update(con, eid, sid, source, driver, eta, conf, reason, note, at, window=None):
    with write_txn(con):
        con.execute("INSERT INTO eta_updates VALUES (?,?,?,?,?,?,?,?,?)", (eid, sid, source, driver, iso(eta), conf, reason, note, iso(at)))
        con.execute("UPDATE shipments SET latest_eta_ts=?, updated_at=? WHERE shipment_id=?", (iso(eta), iso(at), sid))
        if window:
            con.execute("INSERT INTO eta_windows VALUES (?,?,?,?,?)", (sid, iso(window[0]), iso(window[1]), note, iso(at)))


def depart(con, sid, at):
    with write_txn(con):
        con.execute("UPDATE shipments SET actual_departure_ts=?, current_status='IN_TRANSIT', updated_at=? WHERE shipment_id=?", (iso(at), iso(at), sid))
