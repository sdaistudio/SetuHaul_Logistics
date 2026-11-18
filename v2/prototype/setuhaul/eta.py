"""Which arrival time do we plan on, and how sure are we?

Order of evidence (strongest first):
  1. Gate check-in            -> the truck is here; confidence HIGH
  2. Latest ETA record <= as_of (driver, warehouse or operations), plus any ETA window
  3. The plan (shipments.latest_eta_ts or original_eta_ts) -> MEDIUM, never HIGH (audit F17)

Then three plausibility checks downgrade confidence or move the ETA:
  * not departed yet, but the planned departure has passed -> the full transit
    time still lies ahead (this is how SHP1018's 'no-show' becomes predictable)
  * the same vehicle is busy on another trip -> LOW (audit F12, F14)
  * the vehicle 'departed' before it left another facility -> LOW (audit F13)

Planning uses the LATE edge (protects promises); certain-miss tests use the
EARLY edge (never release a slot a truck could still make).
"""
from dataclasses import dataclass, field
from . import config as C
from .clock import ts, iso, add
from .db import one, rows


@dataclass
class ETA:
    shipment_id: str
    point: object            # best single estimate of gate arrival
    earliest: object         # optimistic gate arrival
    latest: object           # pessimistic gate arrival (what we plan on)
    confidence: str
    source: str
    at_facility: bool = False
    release: object = None   # earliest dock start we plan on (latest + gate-to-dock, or yard call time)
    flags: list = field(default_factory=list)

    def as_dict(self):
        return {"point": iso(self.point), "earliest": iso(self.earliest), "latest": iso(self.latest),
                "confidence": self.confidence, "source": self.source, "at_facility": self.at_facility,
                "release": iso(self.release), "flags": self.flags}


def ceil5(d):
    m = d.minute % 5
    d = d.replace(second=0, microsecond=0)
    return add(d, 5 - m) if m else d


DOWNGRADE = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}


def resolve(con, shipment, as_of):
    sid = shipment["shipment_id"]
    chk = one(con, "SELECT * FROM facility_checkins WHERE shipment_id=? AND gate_in_ts IS NOT NULL", sid)
    if chk and ts(chk["gate_in_ts"]) <= as_of:
        g = ts(chk["gate_in_ts"])
        rel = max(add(as_of, C.YARD_TO_DOCK_MIN), add(g, C.GATE_TO_DOCK_MIN))
        return ETA(sid, g, g, g, "HIGH", "GATE_CHECKIN", True, ceil5(rel))

    flags = []
    upd = one(con, """SELECT * FROM eta_updates WHERE shipment_id=? AND created_at<=?
                      ORDER BY created_at DESC, eta_update_id DESC LIMIT 1""", sid, iso(as_of))
    if upd:
        point, conf, source = ts(upd["declared_eta_ts"]), upd["confidence_code"], upd["source_type"]
    else:
        point = ts(shipment["latest_eta_ts"] or shipment["original_eta_ts"])
        conf, source = "MEDIUM", "PLAN"
        if shipment["latest_eta_ts"] and shipment["latest_eta_ts"] != shipment["original_eta_ts"]:
            flags.append("ETA_WITHOUT_LINEAGE")

    # Not departed although the planned departure has passed: the whole trip is still ahead.
    if shipment["actual_departure_ts"] is None and shipment["current_status"] in ("PLANNED", "ASSIGNED"):
        dep = ts(shipment["planned_departure_ts"])
        if dep < as_of:
            transit = ts(shipment["original_eta_ts"]) - dep
            if as_of + transit > point:
                point = as_of + transit
                flags.append("NOT_DEPARTED_ON_TIME")
                conf = DOWNGRADE[conf]

    # The same vehicle is busy on another trip right now.
    busy = rows(con, """SELECT o.shipment_id, o.current_status FROM shipments o
                        WHERE o.vehicle_id=? AND o.shipment_id<>? AND o.current_status IN ('IN_TRANSIT','AT_GATE','WAITING','IN_DOCK')""",
                shipment["vehicle_id"], sid)
    if busy and shipment["current_status"] in ("PLANNED", "ASSIGNED"):
        flags.append("VEHICLE_BUSY_ON_" + busy[0]["shipment_id"])
        conf = "LOW"

    # The vehicle 'departed' before it left another facility (physically impossible).
    if shipment["actual_departure_ts"]:
        clash = one(con, """SELECT o.shipment_id, fc.gate_out_ts FROM shipments o JOIN facility_checkins fc ON fc.shipment_id=o.shipment_id
                            WHERE o.vehicle_id=? AND o.shipment_id<>? AND fc.gate_out_ts IS NOT NULL
                              AND julianday(fc.gate_out_ts) > julianday(?) AND julianday(fc.gate_in_ts) < julianday(?)""",
                    shipment["vehicle_id"], sid, shipment["actual_departure_ts"], shipment["actual_departure_ts"])
        if clash:
            flags.append("DEPARTED_BEFORE_LEAVING_" + clash["shipment_id"])
            conf = "LOW"

    # Overdue and not at the gate: it is at least 'now'.
    if point < as_of:
        point = as_of
        flags.append("OVERDUE")
        conf = DOWNGRADE[conf]

    earliest = add(point, -C.ETA_OPTIMISTIC_MIN[conf])
    latest = add(point, C.ETA_BUFFER_MIN[conf])
    win = one(con, """SELECT * FROM eta_windows WHERE shipment_id=? AND created_at<=?
                      ORDER BY created_at DESC LIMIT 1""", sid, iso(as_of))
    if win and (not upd or win["created_at"] >= upd["created_at"]):
        earliest, latest = ts(win["earliest_ts"]), ts(win["latest_ts"])
        point = earliest + (latest - earliest) / 2
    earliest = max(earliest, as_of)
    latest = max(latest, as_of)
    return ETA(sid, point, earliest, latest, conf, source, False,
               ceil5(add(latest, C.GATE_TO_DOCK_MIN)), flags)
