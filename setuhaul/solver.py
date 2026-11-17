"""Deterministic dock-slot solver: branch and bound over the decision tree.

This is the heart of the system. No model is involved. Given the full state
of a facility (every truck that still needs a dock, every dock closure, every
truck already on a dock, every promise already made) it searches the tree of
possible assignments and returns the plan with the lowest total cost under
rules/rules.yaml.

How it searches (think of a chess engine):
  * Each level of the tree is one truck; each branch is one of its feasible
    slots, or "no dock today" (escalate). Most constrained trucks go first.
  * Each leaf is a complete plan; its score is the total cost from
    rules.yaml: priority-weighted waiting, lateness against promises,
    moving a promise, needing approval, leaving a truck without a dock.
  * Bound: cost so far + for every truck not yet placed the cheapest cost it
    could ever get on its own (ignoring the others). Other trucks can only
    delay it, so this bound never over-estimates (admissible, as in A*).
    Any branch whose bound is not better than the best plan found so far is
    cut. That is what makes an exact search affordable.
  * Warm start: the greedy plan is the first incumbent, so pruning starts at
    once. Branches are tried cheapest-first, so good plans appear early.
  * Anytime: it stops at the time or node budget in rules.yaml and returns
    the best plan found plus a proven lower bound. If the tree was fully
    explored the result is PROVEN OPTIMAL.

Hard constraints are never traded for cost: a closed dock is closed (a toll
lane under repair takes no cars however long the queue), a slot holds one
booking, a start must fall inside its window, unloading must finish before
closing, a driver's deadline is a wall.

Determinism: no randomness; every sort has a total order (cost, time, ids).
Same state + same rules.yaml -> same plan, every run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .eta import EtaEstimate, resolve_eta
from .feasibility import Busy, Constraints, FacilityState, _earliest_free, certain_miss, evaluate_slot
from .rules import R
from .timeutil import diff_min, hhmm, iso, minutes, parse

UNSCHEDULED = "UNSCHEDULED"


@dataclass(frozen=True)
class Cand:
    slot_id: str
    dock_id: str
    dock_code: str
    s_start: datetime
    s_end: datetime


@dataclass
class Truck:
    sid: str
    priority: str
    weight: int
    release: datetime              # earliest time on the dock
    dur: int                       # unload minutes
    cands: list[Cand]
    promise_slot: str | None = None
    promise_start: datetime | None = None
    promise_reachable: bool = False
    deadline: datetime | None = None   # latest gate-out
    lb: float = 0.0                    # cheapest standalone cost (bound)


@dataclass
class Placement:
    sid: str
    slot_id: str | None
    dock_code: str | None
    start: datetime | None
    end: datetime | None
    cost: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Solution:
    status: str                    # PROVEN_OPTIMAL | BEST_WITHIN_BUDGET
    cost: float
    lower_bound: float
    placements: dict[str, Placement]
    nodes: int
    ms: float
    trucks: int

    def as_dict(self):
        return {"status": self.status, "cost": round(self.cost, 1), "lower_bound": round(self.lower_bound, 1),
                "gap": round(self.cost - self.lower_bound, 1), "nodes": self.nodes, "ms": round(self.ms, 1),
                "trucks": self.trucks,
                "plan": [{"shipment": p.sid, "dock": p.dock_code, "slot": p.slot_id, "start": hhmm(p.start),
                          "end": hhmm(p.end), "cost": round(p.cost, 1), "why": p.reasons}
                         for p in sorted(self.placements.values(),
                                         key=lambda p: (p.start is None, p.start.timestamp() if p.start else 0, p.sid))]}


class Problem:
    """Static part (docks, closures, locked work) + the trucks to decide."""

    def __init__(self, fs: FacilityState, trucks: list[Truck]):
        self.fs = fs
        self.trucks = trucks
        self.static = {d: list(v) for d, v in fs.busy.items()}
        c = R()["costs"]
        self.c_wait, self.c_late = c["wait_per_min"], c["late_vs_promise_per_min"]
        self.grace, self.c_move = c["promise_grace_min"], c["move_kept_promise"]
        self.c_appr, self.c_unsched = c["needs_approval"], c["unscheduled"]
        self.exit_min = config.DOCK_TO_GATE_OUT_MIN
        self.lead = fs.now + minutes(config.MIN_LEAD_FROM_NOW_MIN)

    # -- cost of one truck at one start -----------------------------------
    def truck_cost(self, t: Truck, cand: Cand | None, start: datetime | None) -> tuple[float, list[str]]:
        if cand is None:
            return self.c_unsched * t.weight, ["no dock today -> person decides"]
        why = []
        wait = max(0, diff_min(start, t.release))
        cost = self.c_wait * t.weight * wait
        if wait:
            why.append(f"waits {wait} min")
        if t.promise_start is not None:
            late = max(0, diff_min(start, t.promise_start + minutes(self.grace)))
            if late:
                cost += self.c_late * t.weight * late
                why.append(f"{late} min past promise+grace")
            if t.promise_reachable and cand.slot_id != t.promise_slot:
                cost += self.c_move
                why.append("moves a promise that still worked")
        if self.fs.last_new_start and start > self.fs.last_new_start:
            cost += self.c_appr
            why.append("start after last-new-start: approval")
        return cost, why

    # -- sweep one dock: exact start times for a set of assignments ---------
    def sweep(self, dock: str, assigned: list[tuple[Truck, Cand]]) -> tuple[bool, float, dict]:
        busy = list(self.static[dock])
        total, out = 0.0, {}
        for t, cd in sorted(assigned, key=lambda x: (x[1].s_start, x[0].sid)):
            dur = minutes(t.dur)
            st, _ = _earliest_free(busy, max(cd.s_start, t.release, self.lead), dur)
            en = st + dur
            if st >= cd.s_end or en > self.fs.close_at:
                return False, 0, {}
            if t.deadline and en + minutes(self.exit_min) > t.deadline:
                return False, 0, {}
            busy.append(Busy(st, en, "PLAN", t.sid, t.sid))
            busy.sort(key=lambda b: b.start)
            c, why = self.truck_cost(t, cd, st)
            total += c
            out[t.sid] = Placement(t.sid, cd.slot_id, cd.dock_code, st, en, c, why)
        return True, total, out

    def standalone(self, t: Truck, cd: Cand) -> float | None:
        ok, c, _ = self.sweep(cd.dock_id, [(t, cd)])
        return c if ok else None


def _order_options(p: Problem, t: Truck) -> list[tuple[float, Cand | None]]:
    opts = []
    for cd in t.cands:
        c = p.standalone(t, cd)
        if c is not None:
            opts.append((c, cd))
    opts.sort(key=lambda x: (x[0], x[1].s_start, x[1].dock_code, x[1].slot_id))
    opts.append((p.truck_cost(t, None, None)[0], None))
    t.lb = opts[0][0]
    return opts


def solve(p: Problem, time_budget_ms: int | None = None, node_limit: int | None = None,
          pinned: dict[str, str] | None = None) -> Solution:
    """Branch and bound. `pinned` forces truck -> slot_id (used to evaluate a driver's choice)."""
    s = R()["solver"]
    budget = (time_budget_ms if time_budget_ms is not None else s["plan_time_budget_ms"]) / 1000
    node_limit = node_limit or s["node_limit"]
    t0 = time.perf_counter()
    pinned = pinned or {}
    trucks = list(p.trucks)
    options = {}
    for t in trucks:
        opts = _order_options(p, t)
        if t.sid in pinned:
            opts = [o for o in opts if o[1] is not None and o[1].slot_id == pinned[t.sid]] or [o for o in opts if o[1] is None]
            t.lb = opts[0][0]
        options[t.sid] = opts
    # most constrained first, then heavier weight, then id (total order)
    trucks.sort(key=lambda t: (len(options[t.sid]), -t.weight, t.sid))
    suffix_lb = [0.0] * (len(trucks) + 1)
    for i in range(len(trucks) - 1, -1, -1):
        suffix_lb[i] = suffix_lb[i + 1] + trucks[i].lb
    root_lb = suffix_lb[0]

    # warm start: greedy in the same order (take each truck's first option that still fits)
    best_cost, best_assign = float("inf"), None
    greedy, ok_all = {}, True
    for t in trucks:
        for c, cd in options[t.sid]:
            trial = dict(greedy)
            trial[t.sid] = cd
            if _feasible(p, trial, trucks):
                greedy = trial
                break
    gc = _total(p, greedy, trucks)
    if gc is not None:
        best_cost, best_assign = gc, dict(greedy)

    nodes, complete = 0, True
    by_dock: dict[str, list[tuple[Truck, Cand]]] = {d: [] for d in p.static}
    dock_cost: dict[str, float] = {d: 0.0 for d in p.static}
    used: set[str] = set()
    assign: dict[str, Cand | None] = {}

    def rec(i: int, cost_unsched: float):
        nonlocal best_cost, best_assign, nodes, complete
        nodes += 1
        if nodes % 512 == 0 and (time.perf_counter() - t0 > budget or nodes > node_limit):
            complete = False
            raise _Stop
        cur = cost_unsched + sum(dock_cost.values())
        if cur + suffix_lb[i] >= best_cost - 1e-9:
            return
        if i == len(trucks):
            best_cost, best_assign = cur, dict(assign)
            return
        t = trucks[i]
        for _, cd in options[t.sid]:
            if cd is None:
                assign[t.sid] = None
                rec(i + 1, cost_unsched + p.truck_cost(t, None, None)[0])
                del assign[t.sid]
                continue
            if cd.slot_id in used:
                continue
            by_dock[cd.dock_id].append((t, cd))
            ok, dc, _ = p.sweep(cd.dock_id, by_dock[cd.dock_id])
            if ok:
                prev = dock_cost[cd.dock_id]
                dock_cost[cd.dock_id] = dc
                used.add(cd.slot_id)
                assign[t.sid] = cd
                rec(i + 1, cost_unsched)
                del assign[t.sid]
                used.discard(cd.slot_id)
                dock_cost[cd.dock_id] = prev
            by_dock[cd.dock_id].pop()

    try:
        rec(0, 0.0)
    except _Stop:
        pass
    placements = _materialise(p, best_assign or {}, trucks)
    return Solution("PROVEN_OPTIMAL" if complete else "BEST_WITHIN_BUDGET", best_cost,
                    best_cost if complete else root_lb, placements, nodes,
                    (time.perf_counter() - t0) * 1000, len(trucks))


class _Stop(Exception):
    pass


def _feasible(p, assign, trucks) -> bool:
    return _total(p, assign, trucks, partial=True) is not None


def _total(p: Problem, assign: dict, trucks, partial=False):
    if not partial and len(assign) < len(trucks):
        return None
    by_dock: dict[str, list] = {}
    cost, used = 0.0, set()
    tmap = {t.sid: t for t in trucks}
    for sid, cd in assign.items():
        if cd is None:
            cost += p.truck_cost(tmap[sid], None, None)[0]
            continue
        if cd.slot_id in used:
            return None
        used.add(cd.slot_id)
        by_dock.setdefault(cd.dock_id, []).append((tmap[sid], cd))
    for d, lst in by_dock.items():
        ok, c, _ = p.sweep(d, lst)
        if not ok:
            return None
        cost += c
    return cost


def _materialise(p: Problem, assign: dict, trucks) -> dict[str, Placement]:
    tmap = {t.sid: t for t in trucks}
    by_dock: dict[str, list] = {}
    out = {}
    for sid, cd in assign.items():
        if cd is None:
            c, why = p.truck_cost(tmap[sid], None, None)
            out[sid] = Placement(sid, None, None, None, None, c, why)
        else:
            by_dock.setdefault(cd.dock_id, []).append((tmap[sid], cd))
    for d, lst in by_dock.items():
        _, _, pl = p.sweep(d, lst)
        out.update(pl)
    return out


# =========================================================== problem build ==
def _truck_from_db(conn, fs: FacilityState, sh, eta: EtaEstimate, cons: Constraints, max_cands: int) -> Truck:
    arrive = eta.planning_eta if eta.at_facility else eta.planning_eta + minutes(config.GATE_TO_DOCK_MIN)
    if cons.earliest_start:
        arrive = max(arrive, cons.earliest_start)
    cands = []
    for sl in fs.slots:
        if parse(sl["slot_end_ts"]) <= fs.now:
            continue
        ev = evaluate_slot(fs, sh, sl, eta, cons)
        if ev.feasible:
            cands.append((ev.planned_start, Cand(sl["slot_id"], sl["dock_id"], ev.dock_code, ev.slot_start, ev.slot_end)))
    cands.sort(key=lambda x: (x[0], x[1].dock_code, x[1].slot_id))
    appt = conn.execute("""SELECT sl.* FROM appointments a JOIN appointment_slots sl USING(slot_id)
                           WHERE a.shipment_id=? AND a.is_current=1
                           AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')""",
                        (sh["shipment_id"],)).fetchone()
    t = Truck(sh["shipment_id"], sh["priority_code"], config.PRIORITY_WEIGHT[sh["priority_code"]],
              max(arrive, fs.now), sh["expected_unload_min"], [c for _, c in cands[:max_cands]],
              deadline=cons.latest_gate_out)
    if appt:
        t.promise_slot, t.promise_start = appt["slot_id"], parse(appt["slot_start_ts"])
        t.promise_reachable = arrive < parse(appt["slot_end_ts"])
        if t.promise_reachable and all(c.slot_id != appt["slot_id"] for c in t.cands):
            # keep the promised slot in the tree even if it is not among the earliest N
            for _, c in cands:
                if c.slot_id == appt["slot_id"]:
                    t.cands.append(c)
    return t


def _driver_constraints(conn, sid: str) -> Constraints:
    """Constraints a driver gave in chat (latest open exception / conversation)."""
    r = conn.execute("""SELECT earliest_acceptable_ts, latest_acceptable_ts FROM driver_exceptions
                        WHERE shipment_id=? AND exception_status NOT IN ('DUPLICATE','CANCELLED')
                        ORDER BY reported_at DESC LIMIT 1""", (sid,)).fetchone()
    cons = Constraints()
    if r:
        cons.earliest_start = parse(r["earliest_acceptable_ts"]) if r["earliest_acceptable_ts"] else None
        cons.latest_gate_out = parse(r["latest_acceptable_ts"]) if r["latest_acceptable_ts"] else None
    try:
        import json
        c = conn.execute("SELECT data_json FROM agent_conversations WHERE json_extract(data_json,'$.shipment_id')=? "
                         "ORDER BY updated_at DESC LIMIT 1", (sid,)).fetchone()
        if c:
            d = json.loads(c["data_json"])
            cons.earliest_start = parse(d.get("earliest_start")) or cons.earliest_start
            cons.latest_gate_out = parse(d.get("latest_gate_out")) or cons.latest_gate_out
    except Exception:  # noqa: BLE001 - json1 missing: exceptions table is enough
        pass
    return cons


def _needs_slot(conn, fs: FacilityState, sid: str) -> bool:
    """A truck needs a slot if it has no reachable current booking."""
    sh = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (sid,)).fetchone()
    if sh["current_status"] in ("COMPLETED", "CANCELLED", "IN_DOCK"):
        return False
    appt = conn.execute("""SELECT sl.slot_end_ts, a.appointment_status FROM appointments a JOIN appointment_slots sl USING(slot_id)
                           WHERE a.shipment_id=? AND a.is_current=1
                           AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""",
                        (sid,)).fetchone()
    if appt is None:
        return sh["current_status"] in ("IN_TRANSIT", "WAITING", "AT_GATE")
    if appt["appointment_status"] == "IN_PROGRESS":
        return False
    return certain_miss(resolve_eta(conn, sid), parse(appt["slot_end_ts"]))


def build_problem(conn, facility_id: str, now: datetime, mode: str = "plan", focus: str | None = None,
                  focus_eta: EtaEstimate | None = None, focus_cons: Constraints | None = None,
                  eta_overrides: dict[str, EtaEstimate] | None = None) -> Problem:
    """mode='plan': every open truck is movable (promises cost to move).
    mode='chat': trucks whose bookings still work are locked; only trucks that
    need a slot (plus `focus`, the driver chatting) are decided."""
    s = R()["solver"]
    rows = conn.execute("""SELECT * FROM shipments WHERE destination_facility_id=?
                           AND current_status NOT IN ('COMPLETED','CANCELLED','IN_DOCK')""", (facility_id,)).fetchall()
    if mode == "plan":
        movable = {r["shipment_id"] for r in rows}
    else:
        movable = {r["shipment_id"] for r in rows if _needs_slot(conn, None, r["shipment_id"])}
        if focus:
            movable.add(focus)
    # movable trucks are left out BEFORE the cascade, so locked bookings sit where they really would
    fs = FacilityState(conn, facility_id, now, movable=movable)
    trucks = []
    for r in rows:
        if r["shipment_id"] not in movable:
            continue
        sid = r["shipment_id"]
        eta = (eta_overrides or {}).get(sid) or (focus_eta if sid == focus and focus_eta else resolve_eta(conn, sid))
        cons = focus_cons if (sid == focus and focus_cons) else _driver_constraints(conn, sid)
        trucks.append(_truck_from_db(conn, fs, r, eta, cons, s["candidate_slots_per_truck"]))
    return Problem(fs, trucks)


# ======================================================= driver look-ahead ==
@dataclass
class RankedOption:
    slot_id: str
    dock_code: str
    start: datetime
    end: datetime
    own_cost: float
    facility_cost: float
    others_unscheduled: list[str]
    status: str


def rank_for_driver(conn, shipment_id: str, now: datetime, eta: EtaEstimate, cons: Constraints,
                    feasible_slot_ids: list[str]) -> list[RankedOption]:
    """Look ahead like a chess engine: for each slot this driver could take,
    solve the rest of the facility as if the driver took it, and score the
    whole board. Rank the driver's slots by total facility cost."""
    s = R()["solver"]
    sh = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
    out = []
    for slot_id in feasible_slot_ids[: s["chat_candidates_evaluated"]]:
        prob = build_problem(conn, sh["destination_facility_id"], now, "chat", shipment_id, eta, cons)
        sol = solve(prob, s["chat_time_budget_ms"], pinned={shipment_id: slot_id})
        me = sol.placements.get(shipment_id)
        if me is None or me.slot_id != slot_id:
            continue
        out.append(RankedOption(slot_id, me.dock_code, me.start, me.end, me.cost, sol.cost,
                                sorted(k for k, v in sol.placements.items() if v.slot_id is None and k != shipment_id),
                                sol.status))
    out.sort(key=lambda o: (o.facility_cost, o.start, o.dock_code, o.slot_id))
    return out


def plan_facility(conn, facility_id: str, now: datetime, budget_ms: int | None = None) -> Solution:
    return solve(build_problem(conn, facility_id, now, "plan"), budget_ms)
