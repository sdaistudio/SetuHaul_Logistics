"""Deterministic slot feasibility (Decision D4).

A slot is a *start window* on one dock. A booking is feasible for a shipment when
ALL of these hold (each failure has a reason code the driver/ops can read):

  R1  SHIPMENT_ACTIVE      shipment not cancelled / completed / already docked
  R2  DOCK_TYPE            reefer load -> refrigerated dock; > heavy threshold
                           -> HEAVY dock; STANDARD load -> STANDARD dock
  R3  WEIGHT               load_weight_kg <= dock.max_vehicle_weight_kg
  R4  SLOT_OPEN            slot_status = OPEN
  R5  START_IN_WINDOW      planned start (after ETA + gate-to-dock, after any
                           earlier work on the dock) falls inside the window
  R6  DOCK_FREE            [start, start+unload] overlaps no appointment, live
                           hold, in-progress overrun or dock event
  R7  HOURS                start >= open, end <= close; start after
                           LAST_NEW_START_TIME -> allowed only with approval
  R8  DRIVER_CONSTRAINTS   driver's earliest start / latest start / latest
                           gate-out are respected

The language layer never decides feasibility. It only supplies constraints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config
from .eta import EtaEstimate, resolve_eta
from .timeutil import at, hhmm, iso, minutes, parse

FAR_FUTURE = timedelta(days=3650)
ACTIVE_APPT = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")

REASON_TEXT = {
    "SHIPMENT_NOT_ACTIVE": "shipment is not eligible for rebooking",
    "DOCK_TYPE": "dock type does not match the load",
    "WEIGHT": "load is heavier than the dock allows",
    "SLOT_BLOCKED": "slot is blocked",
    "ETA_AFTER_WINDOW": "truck cannot reach the dock before this window ends",
    "DOCK_BUSY": "dock is occupied in this window",
    "DOCK_EVENT": "dock is under breakdown / maintenance",
    "AFTER_CLOSE": "unloading would run past facility closing time",
    "DRIVER_DEADLINE": "unloading would end after the driver's deadline",
    "BEFORE_DRIVER_EARLIEST": "starts before the time the driver asked for",
    "PAST": "window is already over",
    "SLOT_TAKEN": "slot already has a booking",
}


@dataclass
class Constraints:
    earliest_start: datetime | None = None   # "any slot after 12"
    latest_start: datetime | None = None     # "must start by 13:00"
    latest_gate_out: datetime | None = None  # "need to leave before 1:30"

    def as_dict(self):
        return {k: iso(v) for k, v in self.__dict__.items()}


@dataclass
class Busy:
    start: datetime
    end: datetime
    kind: str        # APPOINTMENT | HOLD | IN_PROGRESS | DOCK_EVENT | PLAN
    label: str
    sid: str | None = None


@dataclass
class SlotEval:
    slot_id: str
    dock_id: str
    dock_code: str
    dock_type: str
    slot_start: datetime
    slot_end: datetime
    feasible: bool
    needs_approval: bool = False
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.dock_code} {hhmm(self.slot_start)}-{hhmm(self.slot_end)}"

    def as_dict(self):
        return {
            "slot_id": self.slot_id, "dock": self.dock_code, "dock_type": self.dock_type,
            "window": f"{hhmm(self.slot_start)}-{hhmm(self.slot_end)}",
            "feasible": self.feasible, "needs_approval": self.needs_approval,
            "planned_start": iso(self.planned_start), "planned_end": iso(self.planned_end),
            "reasons": self.reasons, "notes": self.notes,
        }


class FacilityState:
    """Everything the checker needs for one facility at one instant."""

    def __init__(self, conn, facility_id: str, now: datetime, exclude_shipment: str | None = None,
                 movable: set[str] | None = None):
        self.conn, self.facility_id, self.now = conn, facility_id, now
        self.facility = conn.execute("SELECT * FROM facilities WHERE facility_id=?", (facility_id,)).fetchone()
        self.docks = {r["dock_id"]: r for r in conn.execute(
            "SELECT * FROM docks WHERE facility_id=? AND dock_status='ACTIVE'", (facility_id,))}
        self.slots = conn.execute(
            "SELECT * FROM appointment_slots WHERE facility_id=? ORDER BY slot_start_ts, dock_id", (facility_id,)
        ).fetchall()
        self.rules = {r["rule_type"]: r["rule_value"] for r in conn.execute(
            "SELECT rule_type, rule_value FROM facility_rules WHERE facility_id=? AND active_flag=1", (facility_id,))}
        self.open_at = at(now, self.facility["open_time"])
        self.close_at = at(now, self.facility["close_time"])
        lns = self.rules.get("LAST_NEW_START_TIME")
        self.last_new_start = at(now, lns) if lns else None
        self.heavy_kg = int(self.rules.get("HEAVY_DOCK_REQUIRED_KG", config.DEFAULT_HEAVY_THRESHOLD_KG))
        self.busy: dict[str, list[Busy]] = {d: [] for d in self.docks}
        self._movable = set(movable or ())
        self._load_busy(exclude_shipment)

    def _load_busy(self, exclude: str | None):
        c, now = self.conn, self.now
        self.taken_slots: dict[str, str] = {}
        self.missed: dict[str, tuple[str, str]] = {}
        rows = c.execute(f"""
            SELECT a.appointment_id, a.shipment_id, a.appointment_status, sl.dock_id, sl.slot_id,
                   sl.slot_start_ts, sl.slot_end_ts, s.expected_unload_min,
                   p.planned_start_ts, p.planned_end_ts,
                   fc.unload_start_ts, fc.dock_in_ts, fc.unload_end_ts
            FROM appointments a
            JOIN appointment_slots sl ON sl.slot_id = a.slot_id
            JOIN shipments s ON s.shipment_id = a.shipment_id
            LEFT JOIN appointment_plan p ON p.appointment_id = a.appointment_id
            LEFT JOIN facility_checkins fc ON fc.shipment_id = a.shipment_id
            WHERE sl.facility_id = ? AND a.appointment_status IN {ACTIVE_APPT}
        """, (self.facility_id,)).fetchall()
        for r in rows:
            if r["shipment_id"] == exclude or r["shipment_id"] in self._movable or r["dock_id"] not in self.busy:
                continue
            unload = minutes(r["expected_unload_min"])
            if r["appointment_status"] == "IN_PROGRESS":
                st = parse(r["unload_start_ts"] or r["dock_in_ts"] or r["slot_start_ts"])
                en = st + unload
                if r["unload_end_ts"] is None and en < now + minutes(10):
                    en = now + minutes(10)  # overrun: still on the dock
                self.busy[r["dock_id"]].append(Busy(st, en, "IN_PROGRESS", f"{r['shipment_id']} unloading", r["shipment_id"]))
            elif r["planned_start_ts"]:
                self.busy[r["dock_id"]].append(Busy(parse(r["planned_start_ts"]), parse(r["planned_end_ts"]),
                                                    "APPOINTMENT", f"{r['shipment_id']} {r['appointment_status']}",
                                                    r["shipment_id"]))
            else:
                # Expected service interval of an existing booking: it starts when its
                # truck can be on the dock (inside its window), not blindly at slot start.
                s_start, s_end = parse(r["slot_start_ts"]), parse(r["slot_end_ts"])
                other = resolve_eta(c, r["shipment_id"])
                if certain_miss(other, s_end):
                    # Even the optimistic ETA cannot reach this window: the booking is dead
                    # capacity. It does not block; a commit on this slot cancels it (capacity.py).
                    self.missed[r["slot_id"]] = (r["appointment_id"], r["shipment_id"])
                    continue
                arrive = other.planning_eta if other.at_facility else other.planning_eta + minutes(config.GATE_TO_DOCK_MIN)
                st = max(s_start, arrive) if arrive < s_end else s_start
                self.busy[r["dock_id"]].append(Busy(st, st + unload, "APPOINTMENT",
                                                    f"{r['shipment_id']} {r['appointment_status']}", r["shipment_id"]))
            self.taken_slots[r["slot_id"]] = r["shipment_id"]
        # Physical truth beats the booking: a truck on a dock occupies it until it
        # leaves, whatever its appointment says (covers snapshot rows already
        # marked COMPLETED but still on the dock at `now`).
        for fc in c.execute("""SELECT fc.*, s.expected_unload_min FROM facility_checkins fc
                               JOIN shipments s USING(shipment_id)
                               WHERE fc.facility_id=? AND fc.actual_dock_id IS NOT NULL
                               AND fc.dock_in_ts IS NOT NULL""", (self.facility_id,)).fetchall():
            if fc["shipment_id"] == exclude or fc["actual_dock_id"] not in self.busy:
                continue
            din = parse(fc["dock_in_ts"])
            done = parse(fc["unload_end_ts"]) if fc["unload_end_ts"] else None
            if din <= now and (done is None or done > now):
                end = done or max(din + minutes(fc["expected_unload_min"]), now + minutes(10))
                self.busy[fc["actual_dock_id"]].append(Busy(din, end, "IN_PROGRESS", f"{fc['shipment_id']} on dock",
                                                            fc["shipment_id"]))
        holds = c.execute("""
            SELECT h.*, sl.dock_id FROM slot_holds h JOIN appointment_slots sl ON sl.slot_id=h.slot_id
            WHERE sl.facility_id=? AND h.hold_status='HELD' AND h.expires_at > ?
        """, (self.facility_id, iso(now))).fetchall()
        for h in holds:
            if h["shipment_id"] != exclude and h["shipment_id"] not in self._movable and h["dock_id"] in self.busy:
                self.taken_slots[h["slot_id"]] = h["shipment_id"]
                self.busy[h["dock_id"]].append(Busy(parse(h["planned_start_ts"]), parse(h["planned_end_ts"]),
                                                    "HOLD", f"hold {h['hold_id']}", h["shipment_id"]))
        for e in c.execute("SELECT * FROM dock_status_events").fetchall():
            if e["dock_id"] in self.busy and e["event_type"] in config.BLOCKING_EVENTS:
                end = parse(e["event_end_ts"]) if e["event_end_ts"] else now + FAR_FUTURE
                self.busy[e["dock_id"]].append(Busy(parse(e["event_start_ts"]), end, "DOCK_EVENT",
                                                    f"{e['event_type'].lower()}: {e['reason']}"))
        for d, lst in self.busy.items():
            # Cascade: a booked truck cannot start while the dock is still occupied
            # by an overrun, a dock event or the booking before it.
            fixed = sorted([b for b in lst if b.kind != "APPOINTMENT"], key=lambda b: b.start)
            appts = sorted([b for b in lst if b.kind == "APPOINTMENT"], key=lambda b: b.start)
            placed: list[Busy] = []
            for b in appts:
                dur = b.end - b.start
                st, _ = _earliest_free([x for x in fixed + placed if x.kind != "DOCK_EVENT"], b.start, dur)
                placed.append(Busy(st, st + dur, b.kind, b.label, b.sid))
            self.busy[d] = sorted(fixed + placed, key=lambda b: b.start)

    # ---- compatibility ------------------------------------------------------
    def release(self, shipment_ids: set[str]):
        """Treat these trucks' bookings/holds as movable: drop them from busy and taken slots."""
        for d in self.busy:
            self.busy[d] = [b for b in self.busy[d] if not (b.sid in shipment_ids and b.kind in ("APPOINTMENT", "HOLD"))]
        self.taken_slots = {k: v for k, v in self.taken_slots.items() if v not in shipment_ids}

    def compatible(self, shipment, dock) -> tuple[bool, list[str]]:
        """Hard compatibility rules come from rules/rules.yaml (compatibility.rules)."""
        from .rules import compatible
        return compatible(shipment, dock, self.heavy_kg)


def certain_miss(eta: EtaEstimate, slot_end: datetime) -> bool:
    """Booking cannot be kept even if the truck arrives at the EARLY edge of its ETA."""
    if eta.at_facility:
        return False
    optimistic = (eta.earliest or eta.point) + minutes(config.GATE_TO_DOCK_MIN)
    return optimistic >= slot_end


def _earliest_free(busy: list[Busy], start: datetime, dur: timedelta) -> tuple[datetime, Busy | None]:
    blocker = None
    changed = True
    while changed:
        changed = False
        for b in busy:
            if b.start < start + dur and b.end > start:
                start, blocker, changed = b.end, b, True
    return start, blocker


def evaluate_slot(fs: FacilityState, shipment, slot, eta: EtaEstimate, cons: Constraints) -> SlotEval:
    dock = fs.docks.get(slot["dock_id"])
    s_start, s_end = parse(slot["slot_start_ts"]), parse(slot["slot_end_ts"])
    ev = SlotEval(slot["slot_id"], slot["dock_id"], dock["dock_code"] if dock else "?",
                  dock["dock_type"] if dock else "?", s_start, s_end, feasible=False)
    if dock is None:
        ev.reasons.append("DOCK_TYPE")
        return ev
    if shipment["current_status"] in ("CANCELLED", "COMPLETED", "IN_DOCK"):
        ev.reasons.append("SHIPMENT_NOT_ACTIVE")
    ok, why = fs.compatible(shipment, dock)
    ev.reasons += why
    if slot["slot_status"] != "OPEN":
        ev.reasons.append("SLOT_BLOCKED")
        ev.notes.append(slot["block_reason"] or slot["slot_status"])
    if s_end <= fs.now:
        ev.reasons.append("PAST")
    if slot["slot_id"] in fs.taken_slots:
        # One active booking per slot (same rule as ux_active_appointment_per_slot),
        # even if the dock has idle minutes left inside the window.
        ev.reasons.append("SLOT_TAKEN")
        ev.notes.append(f"booked by {fs.taken_slots[slot['slot_id']]}")
    if ev.reasons:
        return ev

    unload = minutes(shipment["expected_unload_min"])
    arrive = eta.planning_eta if eta.at_facility else eta.planning_eta + minutes(config.GATE_TO_DOCK_MIN)
    cand = [s_start, arrive, fs.now + minutes(config.MIN_LEAD_FROM_NOW_MIN), fs.open_at]
    if cons.earliest_start:
        cand.append(cons.earliest_start)
    start0 = max(cand)
    if start0 >= s_end:
        if cons.earliest_start and cons.earliest_start >= s_end:
            ev.reasons.append("BEFORE_DRIVER_EARLIEST")
        else:
            ev.reasons.append("ETA_AFTER_WINDOW")
            ev.notes.append(f"earliest dock time {hhmm(arrive)} (planning ETA {hhmm(eta.planning_eta)})")
        return ev
    start, blocker = _earliest_free(fs.busy[slot["dock_id"]], start0, unload)
    if start >= s_end:
        ev.reasons.append("DOCK_EVENT" if blocker and blocker.kind == "DOCK_EVENT" else "DOCK_BUSY")
        if blocker:
            ev.notes.append(blocker.label)
        return ev
    end = start + unload
    if end > fs.close_at:
        ev.reasons.append("AFTER_CLOSE")
    if cons.latest_start and start > cons.latest_start:
        ev.reasons.append("DRIVER_DEADLINE")
    if cons.latest_gate_out and end + minutes(config.DOCK_TO_GATE_OUT_MIN) > cons.latest_gate_out:
        ev.reasons.append("DRIVER_DEADLINE")
        ev.notes.append(f"gate-out ~{hhmm(end + minutes(config.DOCK_TO_GATE_OUT_MIN))} > {hhmm(cons.latest_gate_out)}")
    ev.planned_start, ev.planned_end = start, end
    if ev.reasons:
        return ev
    if fs.last_new_start and start > fs.last_new_start:
        ev.needs_approval = True
        ev.notes.append(f"start after {hhmm(fs.last_new_start)} needs manual approval (RULE LAST_NEW_START_TIME)")
    if blocker:
        ev.notes.append(f"starts after {blocker.label}")
    ev.feasible = True
    return ev


def evaluate_all(conn, shipment_id: str, now: datetime, cons: Constraints | None = None,
                 eta: EtaEstimate | None = None) -> tuple[list[SlotEval], EtaEstimate]:
    cons = cons or Constraints()
    sh = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
    eta = eta or resolve_eta(conn, shipment_id)
    fs = FacilityState(conn, sh["destination_facility_id"], now, exclude_shipment=shipment_id)
    evals = [evaluate_slot(fs, sh, sl, eta, cons) for sl in fs.slots if parse(sl["slot_end_ts"]) > now]
    return evals, eta


def rank_options(evals: list[SlotEval], limit: int = config.MAX_OPTIONS_SHOWN) -> list[SlotEval]:
    """Transparent ordering: no-approval first, then earliest planned start,
    then dock code. Distinct start times so the driver gets real choices."""
    feas = sorted((e for e in evals if e.feasible),
                  key=lambda e: (e.needs_approval, e.planned_start, e.dock_code))
    out, seen = [], set()
    for e in feas:
        key = hhmm(e.planned_start)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) == limit:
            break
    return out


def summarise_rejections(evals: list[SlotEval]) -> dict[str, int]:
    agg: dict[str, int] = {}
    for e in evals:
        if not e.feasible:
            for r in set(e.reasons):
                agg[r] = agg.get(r, 0) + 1
    return dict(sorted(agg.items(), key=lambda kv: -kv[1]))
