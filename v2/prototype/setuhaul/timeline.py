"""The dock timeline and the feasibility check.

A dock is a machine that can serve one truck at a time. We model what each
dock is doing as a list of busy intervals:

    DOCK_EVENT    breakdown, maintenance or manual block (from dock_status_events)
    BLOCKED_SLOT  a published slot that is not OPEN
    IN_DOCK       a truck unloading now; if it has run past its expected end,
                  we assume OVERRUN_BUFFER_MIN more (audit F34)
    HOLD          a live hold awaiting a coordinator
    KEPT_BOOKING  an existing booking we intend to keep. Its interval starts at
                  max(slot start, the truck's planned dock start), and is pushed
                  later if the dock is still busy (this is how an overrun
                  cascades, audit F05).

A booking whose truck cannot reach the window even on its EARLY edge is a
CERTAIN_MISS and stops blocking capacity. A booking that breaks a hard rule
(weight, dock type, broken dock) is INVALID. Both still need a dock, so the
planner re-plans them; the commit that takes their slot cancels them.

Slot meaning (our working assumption, see grey-area question Q1): a slot is a
START WINDOW. The truck may start any time inside [slot_start, slot_end), and
the unload may run past slot_end as long as the dock is free.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from . import config as C
from .clock import ts, iso, add, hhmm, minutes_between
from .db import one, rows
from .eta import resolve, ceil5

BLOCKING_EVENTS = ("BREAKDOWN", "MAINTENANCE", "MANUAL_BLOCK", "OUT_OF_SERVICE")
ACTIVE = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")


@dataclass
class Busy:
    start: object
    end: object
    kind: str
    ref: str
    shipment_id: str = None


@dataclass
class Timeline:
    facility: dict
    as_of: object
    docks: dict
    busy: dict
    booking_status: dict = field(default_factory=dict)
    in_dock: set = field(default_factory=set)
    etas: dict = field(default_factory=dict)


# ---------------------------------------------------------------- helpers
def facility_rules(con, fid):
    out = {}
    for r in rows(con, "SELECT rule_type, rule_value FROM facility_rules WHERE facility_id=? AND active_flag=1", fid):
        v = r["rule_value"]
        out[r["rule_type"]] = int(v) if v.isdigit() else v
    return out


def at_clock(day, hhmm_text):
    h, m = map(int, hhmm_text.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def shipment(con, sid):
    return one(con, "SELECT * FROM shipments WHERE shipment_id=?", sid)


def dock_compatible(ship, dock, rules):
    """Return [] if the dock accepts this load, else a list of reason codes."""
    why = []
    need = ship["required_dock_type"]
    heavy_kg = rules.get("HEAVY_DOCK_REQUIRED_KG")
    if ship["temperature_control_required"] or need == "REEFER":
        if not (dock["supports_refrigerated"] and dock["dock_type"] == "REEFER"):
            why.append("REEFER_DOCK_REQUIRED")
    elif need == "HEAVY" or (heavy_kg and ship["load_weight_kg"] > heavy_kg):
        if dock["dock_type"] != "HEAVY":
            why.append("HEAVY_DOCK_REQUIRED")
    else:
        if dock["dock_type"] == "HEAVY" and not C.ALLOW_STANDARD_ON_HEAVY_DOCK:
            why.append("HEAVY_DOCK_RESERVED")
        if dock["dock_type"] == "REEFER" and not C.ALLOW_STANDARD_ON_REEFER_DOCK:
            why.append("REEFER_DOCK_RESERVED")
    if ship["load_weight_kg"] > dock["max_vehicle_weight_kg"]:
        why.append("OVER_DOCK_WEIGHT")
    return why


def earliest_fit(intervals, s0, dur_min, latest_start=None):
    """Earliest start >= s0 (on a 5-minute grid) where [s, s+dur) overlaps nothing."""
    s = ceil5(s0)
    for iv in sorted(intervals, key=lambda b: b.start):
        if iv.end <= s:
            continue
        if iv.start >= add(s, dur_min):
            break
        s = ceil5(iv.end)
    if latest_start is not None and s >= latest_start:
        return None
    return s


def overlaps(intervals, s, e):
    return [iv for iv in intervals if iv.start < e and s < iv.end]


# ---------------------------------------------------------------- timeline
def build(con, facility_id, as_of, exclude=()):
    fac = one(con, "SELECT * FROM facilities WHERE facility_id=?", facility_id)
    rules = facility_rules(con, facility_id)
    grace = int(rules.get("NO_SHOW_GRACE_MIN", fac["checkin_grace_min"]))
    day = as_of
    close = at_clock(day, fac["close_time"])
    docks = {d["dock_id"]: d for d in rows(con, "SELECT * FROM docks WHERE facility_id=? ORDER BY dock_code", facility_id)}
    busy = {d: [] for d in docks}
    tl = Timeline(fac, as_of, docks, busy)

    for e in rows(con, """SELECT * FROM dock_status_events WHERE dock_id IN (SELECT dock_id FROM docks WHERE facility_id=?)
                          AND created_at<=?""", facility_id, iso(as_of)):
        if e["event_type"] in BLOCKING_EVENTS:
            busy[e["dock_id"]].append(Busy(ts(e["event_start_ts"]), ts(e["event_end_ts"]) if e["event_end_ts"] else close,
                                           "DOCK_EVENT", e["dock_event_id"]))
    for sl in rows(con, "SELECT * FROM appointment_slots WHERE facility_id=? AND slot_status<>'OPEN'", facility_id):
        busy[sl["dock_id"]].append(Busy(ts(sl["slot_start_ts"]), ts(sl["slot_end_ts"]), "BLOCKED_SLOT", sl["slot_id"]))

    for c in rows(con, """SELECT fc.*, s.expected_unload_min FROM facility_checkins fc JOIN shipments s USING(shipment_id)
                          WHERE fc.facility_id=? AND fc.actual_dock_id IS NOT NULL AND fc.dock_in_ts IS NOT NULL""", facility_id):
        din = ts(c["dock_in_ts"])
        if din > as_of or (c["unload_end_ts"] and ts(c["unload_end_ts"]) <= as_of):
            continue
        start = ts(c["unload_start_ts"] or c["dock_in_ts"])
        exp_end = add(start, c["expected_unload_min"])
        if exp_end <= as_of:
            exp_end = add(as_of, C.OVERRUN_BUFFER_MIN)
        busy[c["actual_dock_id"]].append(Busy(din, add(exp_end, C.DOCK_CHANGEOVER_MIN), "IN_DOCK", c["checkin_id"], c["shipment_id"]))
        tl.in_dock.add(c["shipment_id"])

    for h in rows(con, """SELECT * FROM slot_holds WHERE hold_status IN ('ACTIVE','PENDING_APPROVAL') AND expires_at>?
                          AND dock_id IN (SELECT dock_id FROM docks WHERE facility_id=?)""", iso(as_of), facility_id):
        if h["shipment_id"] in exclude:
            continue
        busy[h["dock_id"]].append(Busy(ts(h["planned_start_ts"]), ts(h["planned_end_ts"]), "HOLD", h["hold_id"], h["shipment_id"]))

    appts = rows(con, f"""SELECT a.*, sl.dock_id, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status
                          FROM appointments a JOIN appointment_slots sl ON sl.slot_id=a.slot_id
                          WHERE sl.facility_id=? AND a.is_current=1 AND a.appointment_status IN {ACTIVE}
                          ORDER BY sl.slot_start_ts, a.appointment_id""", facility_id)
    for a in appts:
        sid = a["shipment_id"]
        if sid in exclude or sid in tl.in_dock:
            continue
        ship = shipment(con, sid)
        e = tl.etas.get(sid) or resolve(con, ship, as_of)
        tl.etas[sid] = e
        dock = docks[a["dock_id"]]
        sstart, send = ts(a["slot_start_ts"]), ts(a["slot_end_ts"])
        dur = ship["expected_unload_min"] + C.DOCK_CHANGEOVER_MIN
        st = {"appointment_id": a["appointment_id"], "shipment_id": sid, "slot_id": a["slot_id"],
              "dock_code": dock["dock_code"], "slot_start": iso(sstart), "status": "OK", "reasons": []}
        problems = dock_compatible(ship, dock, rules)
        if a["slot_status"] != "OPEN":
            problems.append("SLOT_NOT_OPEN")
        if not e.at_facility and e.earliest > add(sstart, grace):
            st.update(status="CERTAIN_MISS", reasons=[f"earliest arrival {hhmm(e.earliest)} is after check-in limit {hhmm(add(sstart, grace))}"])
        elif problems:
            st.update(status="INVALID", reasons=problems)
        else:
            s = earliest_fit(busy[a["dock_id"]], max(sstart, e.release), dur)
            if add(s, ship["expected_unload_min"]) > close:
                ev = [iv.ref for iv in overlaps(busy[a["dock_id"]], max(sstart, e.release), add(max(sstart, e.release), dur)) if iv.kind == "DOCK_EVENT"]
                st.update(status="INVALID", reasons=["DOCK_UNAVAILABLE_UNTIL_CLOSE" + (f" ({', '.join(ev)})" if ev else "")])
            else:
                busy[a["dock_id"]].append(Busy(s, add(s, dur), "KEPT_BOOKING", a["appointment_id"], sid))
                st["planned_start"] = iso(s)
                if s >= send:
                    st.update(status="AT_RISK", reasons=[f"dock busy; planned start {hhmm(s)} is after window end {hhmm(send)}"])
                elif e.confidence == "LOW" and minutes_between(e.release, send) < 15:
                    st.update(status="AT_RISK", reasons=["low-confidence ETA close to window end"])
        tl.booking_status[a["appointment_id"]] = st
    for d in busy:
        busy[d].sort(key=lambda b: b.start)
    return tl


# ---------------------------------------------------------------- options
def options(con, shipment_id, as_of, limit=C.MAX_OPTIONS_SHOWN, constraints=None, earliest_start=None,
            respect_earmarks=True, before=None, exclude_slots=(), eta_override=None):
    """Feasible (dock, slot, start) choices for one shipment, best first.

    Returns (options, rejections). Reads only: showing options reserves nothing.
    """
    ship = shipment(con, shipment_id)
    rej = {}
    if ship["current_status"] in ("CANCELLED", "COMPLETED"):
        return [], {"SHIPMENT_" + ship["current_status"]: 1}
    fid = ship["destination_facility_id"]
    fac = one(con, "SELECT * FROM facilities WHERE facility_id=?", fid)
    rules = facility_rules(con, fid)
    tl = build(con, fid, as_of, exclude=(shipment_id,))
    e = eta_override or resolve(con, ship, as_of)
    open_t, close_t = at_clock(as_of, fac["open_time"]), at_clock(as_of, fac["close_time"])
    last_start = at_clock(as_of, rules["LAST_NEW_START_TIME"]) if "LAST_NEW_START_TIME" in rules else None
    unload = ship["expected_unload_min"]
    dur = unload + C.DOCK_CHANGEOVER_MIN
    pr = C.PRIORITY_RANK[ship["priority_code"]]
    limits = constraints or {}

    occupant = {r["slot_id"]: r for r in rows(con, f"""SELECT a.slot_id, a.appointment_id, a.shipment_id FROM appointments a
                 WHERE a.appointment_status IN {ACTIVE}""")}
    held = {r["slot_id"]: r["shipment_id"] for r in rows(con, """SELECT slot_id, shipment_id FROM slot_holds
                 WHERE hold_status IN ('ACTIVE','PENDING_APPROVAL') AND expires_at>?""", iso(as_of))}
    earmark = {r["slot_id"]: r for r in rows(con, "SELECT * FROM slot_earmarks WHERE expires_at>?", iso(as_of))}

    cands = []
    for dock_id, dock in tl.docks.items():
        why = dock_compatible(ship, dock, rules)
        if why:
            for w in why:
                rej[w] = rej.get(w, 0) + 1
            continue
        for sl in rows(con, "SELECT * FROM appointment_slots WHERE dock_id=? AND slot_status='OPEN' ORDER BY slot_start_ts", dock_id):
            sid_ = sl["slot_id"]
            sstart, send = ts(sl["slot_start_ts"]), ts(sl["slot_end_ts"])
            if send <= max(as_of, e.release) or sid_ in exclude_slots:
                continue
            releases = []
            occ = occupant.get(sid_)
            if occ and occ["shipment_id"] != shipment_id:
                status = tl.booking_status.get(occ["appointment_id"], {}).get("status")
                if status in ("CERTAIN_MISS", "INVALID"):
                    releases.append(occ["appointment_id"])
                else:
                    rej["SLOT_BOOKED"] = rej.get("SLOT_BOOKED", 0) + 1
                    continue
            if held.get(sid_) and held[sid_] != shipment_id:
                rej["SLOT_HELD"] = rej.get("SLOT_HELD", 0) + 1
                continue
            em = earmark.get(sid_)
            if respect_earmarks and em and em["shipment_id"] != shipment_id and C.PRIORITY_RANK[em["priority_code"]] >= pr:
                rej["PROTECTED_FOR_PRIORITY_LOAD"] = rej.get("PROTECTED_FOR_PRIORITY_LOAD", 0) + 1
                continue
            s0 = max(sstart, e.release, open_t)
            if earliest_start:
                s0 = max(s0, earliest_start)
            s = earliest_fit(tl.busy[dock_id], s0, dur, latest_start=send)
            if s is None:
                rej["DOCK_BUSY_IN_WINDOW"] = rej.get("DOCK_BUSY_IN_WINDOW", 0) + 1
                continue
            unload_end = add(s, unload)
            if unload_end > close_t:
                rej["AFTER_CLOSING"] = rej.get("AFTER_CLOSING", 0) + 1
                continue
            if before and s >= before:
                continue
            if "LATEST_START" in limits and s > limits["LATEST_START"]:
                rej["DRIVER_LIMIT"] = rej.get("DRIVER_LIMIT", 0) + 1
                continue
            if "LATEST_UNLOAD_END" in limits and unload_end > limits["LATEST_UNLOAD_END"]:
                rej["DRIVER_LIMIT"] = rej.get("DRIVER_LIMIT", 0) + 1
                continue
            if "LATEST_GATE_OUT" in limits and add(unload_end, C.YARD_TO_DOCK_MIN) > limits["LATEST_GATE_OUT"]:
                rej["DRIVER_LIMIT"] = rej.get("DRIVER_LIMIT", 0) + 1
                continue
            approval = [("DATA_CONFLICT:" + fl) for fl in e.flags if fl.startswith(("VEHICLE_BUSY", "DEPARTED_BEFORE"))]
            if last_start and s >= last_start:
                approval.append("START_AFTER_" + rules["LAST_NEW_START_TIME"].replace(":", ""))
            # Fragment rule: do not leave a dead gap of 1..54 minutes after this truck.
            nxt = [iv.start for iv in tl.busy[dock_id] if iv.start >= add(s, dur)]
            gap_after = minutes_between(add(s, dur), min(nxt)) if nxt else 999
            dead_gap = 0 < gap_after < 55
            cands.append({
                "slot_id": sid_, "dock_id": dock_id, "dock_code": dock["dock_code"], "dock_type": dock["dock_type"],
                "slot_start": iso(sstart), "slot_end": iso(send), "start": iso(s), "unload_end": iso(unload_end),
                "occupancy_end": iso(add(s, dur)), "wait_min": max(0, minutes_between(e.release, s)),
                "needs_approval": approval, "releases": releases, "dead_gap": dead_gap,
                "eta_confidence": e.confidence,
            })
    cands.sort(key=lambda o: (bool(o["needs_approval"]), o["start"], o["dead_gap"], o["dock_code"]))
    if limit is None:
        return cands, rej
    chosen = []
    for o in cands:   # one option per distinct start time (+-10 min) so the driver gets a real choice
        if all(abs(minutes_between(ts(c["start"]), ts(o["start"]))) > 10 for c in chosen):
            chosen.append(o)
        if len(chosen) == limit:
            break
    for o in cands:
        if len(chosen) == limit:
            break
        if o not in chosen:
            chosen.append(o)
    chosen.sort(key=lambda o: (bool(o["needs_approval"]), o["start"]))
    return chosen, rej
