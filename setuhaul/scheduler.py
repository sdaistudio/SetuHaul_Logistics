"""Optional facility-level scheduling tool.

The driver agent looks after one driver. This tool looks at one facility and
all its trucks at once and PROPOSES a sequence. It reads structured data only,
never chat text, and never writes bookings: a coordinator (or a later approved
step) turns a proposal into appointments through capacity.book().

Scheduling model (from the brief):
  job = truck, machine = dock door, release = planning ETA + gate-to-dock,
  processing = expected_unload_min, due = current appointment start,
  fixed task = truck already unloading, weight = priority.

Objective, in plain words, in this order:
  1. nobody is left without a dock if any compatible dock can take them;
  2. keep promises we already made (a booking that is still reachable stays);
  3. less priority-weighted waiting;
  4. fewer missed appointments.

Three policies are compared on the same snapshot: FCFS, EDD and our
COMMITMENT_FIRST_WEIGHTED rule. A CP-SAT model is not used: with 6 docks and
~20 trucks the greedy rule is within reach of optimal, explainable to a
planner, and fast; docs/02_decision_register.md (D8) records when we would
switch to OR-Tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .eta import resolve_eta
from .feasibility import Busy, FacilityState, _earliest_free
from .timeutil import diff_min, hhmm, minutes, parse

GRACE_MIN = 30


@dataclass
class Job:
    shipment_id: str
    priority: str
    weight: int
    release: datetime
    duration: int
    compatible: list[str]
    due: datetime | None
    due_end: datetime | None
    current_dock: str | None
    at_facility: bool
    eta_conf: str


@dataclass
class Placement:
    shipment_id: str
    dock_id: str | None
    start: datetime | None
    end: datetime | None
    kept_commitment: bool = False
    needs_approval: bool = False
    note: str = ""


@dataclass
class PlanResult:
    policy: str
    placements: list[Placement]
    metrics: dict = field(default_factory=dict)


def build_jobs(conn, facility_id: str, now: datetime) -> tuple[list[Job], FacilityState]:
    fs = FacilityState(conn, facility_id, now)
    # remove appointment-based busy (we are re-planning); keep events + physical occupancy
    for d in fs.busy:
        fs.busy[d] = [b for b in fs.busy[d] if b.kind in ("DOCK_EVENT", "IN_PROGRESS")]
    jobs = []
    rows = conn.execute("""SELECT * FROM shipments WHERE destination_facility_id=?
                           AND current_status NOT IN ('COMPLETED','CANCELLED','IN_DOCK')""", (facility_id,)).fetchall()
    for s in rows:
        eta = resolve_eta(conn, s["shipment_id"])
        rel = max(now, eta.point + minutes(5)) if eta.at_facility else eta.planning_eta + minutes(config.GATE_TO_DOCK_MIN)
        appt = conn.execute("""SELECT sl.* FROM appointments a JOIN appointment_slots sl USING(slot_id)
                               WHERE a.shipment_id=? AND a.is_current=1
                               AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')""",
                            (s["shipment_id"],)).fetchone()
        comp = [d for d, row in fs.docks.items() if fs.compatible(s, row)[0]]
        jobs.append(Job(s["shipment_id"], s["priority_code"], config.PRIORITY_WEIGHT[s["priority_code"]], rel,
                        s["expected_unload_min"], comp, parse(appt["slot_start_ts"]) if appt else None,
                        parse(appt["slot_end_ts"]) if appt else None, appt["dock_id"] if appt else None,
                        eta.at_facility, eta.confidence))
    return jobs, fs


def _place(fs: FacilityState, busy: dict, job: Job, earliest: datetime, docks: list[str]) -> Placement:
    best = None
    dur = minutes(job.duration)
    for d in docks:
        st, _ = _earliest_free(busy[d], max(earliest, fs.open_at), dur)
        en = st + dur
        if en > fs.close_at:
            continue
        if best is None or (en, d) < (best[2], best[0]):
            best = (d, st, en)
    if best is None:
        return Placement(job.shipment_id, None, None, None, note="no compatible dock time left today")
    d, st, en = best
    busy[d].append(Busy(st, en, "PLAN", job.shipment_id))
    busy[d].sort(key=lambda b: b.start)
    return Placement(job.shipment_id, d, st, en,
                     needs_approval=bool(fs.last_new_start and st > fs.last_new_start))


def plan(conn, facility_id: str, now: datetime, policy: str = "COMMITMENT_FIRST_WEIGHTED",
         release_commitments: set[str] | None = None) -> PlanResult:
    jobs, fs = build_jobs(conn, facility_id, now)
    busy = {d: list(v) for d, v in fs.busy.items()}
    out: list[Placement] = []
    todo = list(jobs)
    if policy == "COMMITMENT_FIRST_WEIGHTED":
        # 1) keep reachable commitments in priority order
        rel = release_commitments or set()
        keep = sorted([j for j in jobs if j.due and j.current_dock in j.compatible and j.shipment_id not in rel],
                      key=lambda j: (-j.weight, j.due))
        for j in keep:
            dur = minutes(j.duration)
            st, _ = _earliest_free(busy[j.current_dock], max(j.release, j.due), dur)
            if st < j.due_end and st + dur <= fs.close_at:
                busy[j.current_dock].append(Busy(st, st + dur, "PLAN", j.shipment_id))
                busy[j.current_dock].sort(key=lambda b: b.start)
                out.append(Placement(j.shipment_id, j.current_dock, st, st + dur, kept_commitment=True))
                todo.remove(j)
        order = sorted(todo, key=lambda j: (-j.weight, j.release))
    elif policy == "WEIGHTED":
        order = sorted(todo, key=lambda j: (-j.weight, j.release))
    elif policy == "FCFS":
        order = sorted(todo, key=lambda j: j.release)
    elif policy == "EDD":
        order = sorted(todo, key=lambda j: (j.due or j.release, j.release))
    else:
        raise ValueError(policy)
    for j in order:
        out.append(_place(fs, busy, j, j.release, j.compatible))
    res = PlanResult(policy, out)
    res.metrics = score(jobs, out)
    if policy == "COMMITMENT_FIRST_WEIGHTED":
        res.metrics["swap_proposals"] = swap_proposals(jobs, out)
    return res


def swap_proposals(jobs: list[Job], placements: list[Placement], max_wait: int = 30) -> list[dict]:
    """A high-priority truck waiting > 30 min behind a kept LOW/NORMAL promise on a
    compatible dock -> propose a swap. Breaking a promise is a human decision
    (brief: 'how scarce capacity is ranked when rules conflict'), so this is
    only a proposal for the coordinator."""
    by = {j.shipment_id: j for j in jobs}
    pl = {p.shipment_id: p for p in placements}
    out = []
    for p in placements:
        j = by[p.shipment_id]
        if p.start is None or j.priority not in ("CRITICAL", "HIGH"):
            continue
        wait = diff_min(p.start, j.release)
        if wait <= max_wait:
            continue
        for q in placements:
            k = by[q.shipment_id]
            if q.kept_commitment and k.weight < j.weight and q.dock_id in j.compatible and q.start and \
                    q.start < p.start and q.end > j.release:
                out.append({"promote": j.shipment_id, "priority": j.priority, "waits_min": wait,
                            "displace": k.shipment_id, "displaced_priority": k.priority,
                            "dock": q.dock_id, "at": hhmm(max(q.start, j.release)),
                            "needs": "coordinator approval + driver notice to " + k.shipment_id})
                break
    return out


def plan_optimal(conn, facility_id: str, now: datetime) -> PlanResult:
    """Branch-and-bound optimum (solver.py) under rules.yaml costs, scored with the
    same metrics as the dispatch rules so the comparison is like for like."""
    from .solver import plan_facility
    sol = plan_facility(conn, facility_id, now)
    jobs, fs = build_jobs(conn, facility_id, now)
    slot_dock = {r["slot_id"]: r["dock_id"] for r in fs.slots}
    pls = []
    for j in jobs:
        p = sol.placements.get(j.shipment_id)
        if p is None or p.start is None:
            pls.append(Placement(j.shipment_id, None, None, None, note="no compatible dock time left today"))
        else:
            pls.append(Placement(j.shipment_id, slot_dock[p.slot_id], p.start, p.end,
                                 kept_commitment=(slot_dock[p.slot_id] == j.current_dock and j.due is not None
                                                  and j.due <= p.start < j.due_end),
                                 needs_approval=bool(fs.last_new_start and p.start > fs.last_new_start)))
    res = PlanResult(f"OPTIMAL (branch & bound, {sol.status.lower().replace('_', ' ')})", pls)
    res.metrics = score(jobs, pls)
    res.metrics["solver_cost"] = round(sol.cost)
    res.metrics["solver_nodes"] = sol.nodes
    res.metrics["solver_ms"] = round(sol.ms)
    return res


def score(jobs: list[Job], placements: list[Placement]) -> dict:
    by = {j.shipment_id: j for j in jobs}
    w_wait = waits = missed = changes = unsched = 0
    crit_wait = 0
    for p in placements:
        j = by[p.shipment_id]
        if p.start is None:
            unsched += 1
            continue
        wait = max(0, diff_min(p.start, j.release))
        waits += wait
        w_wait += wait * j.weight
        if j.priority == "CRITICAL":
            crit_wait += wait
        # only count a miss the plan caused: the truck could have made its window
        if j.due and j.release <= j.due + minutes(GRACE_MIN) and p.start > j.due + minutes(GRACE_MIN):
            missed += 1
        if j.due and (p.dock_id != j.current_dock or not (j.due <= p.start < j.due_end)):
            changes += 1
    n = len([p for p in placements if p.start])
    return {"trucks": len(jobs), "scheduled": n, "unscheduled": unsched,
            "avg_wait_min": round(waits / n, 1) if n else 0, "weighted_wait": w_wait,
            "critical_wait_min": crit_wait, "missed_appointments": missed, "schedule_changes": changes}


def compare(conn, facility_id: str, now: datetime) -> list[PlanResult]:
    out = [plan(conn, facility_id, now, p) for p in ("FCFS", "EDD", "WEIGHTED", "COMMITMENT_FIRST_WEIGHTED")]
    swaps = out[-1].metrics.get("swap_proposals", [])
    if swaps:
        r = plan(conn, facility_id, now, "COMMITMENT_FIRST_WEIGHTED", {s["displace"] for s in swaps})
        r.policy = "COMMITMENT_FIRST_WEIGHTED + approved swaps"
        r.metrics.pop("swap_proposals", None)
        out.append(r)
    out.append(plan_optimal(conn, facility_id, now))
    return out


def plan_table(res: PlanResult, fs_docks: dict | None = None) -> list[dict]:
    return [{"shipment": p.shipment_id, "dock": p.dock_id, "start": hhmm(p.start), "end": hhmm(p.end),
             "kept": p.kept_commitment, "approval": p.needs_approval, "note": p.note}
            for p in sorted(res.placements, key=lambda p: (p.start is None, hhmm(p.start) if p.start else ""))]
