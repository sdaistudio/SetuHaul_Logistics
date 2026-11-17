"""Independent optimality check with Google OR-Tools CP-SAT (optional dependency).

Same problem, same costs, different engine. If CP-SAT's optimum equals the
branch-and-bound optimum, the B&B result is confirmed. CP-SAT runs with one
worker and a fixed seed, so it is deterministic too.

Why both: B&B is ours, small and explainable line by line to a planner; CP-SAT
(or OptaPlanner / Timefold on the JVM) is the industrial engine to switch to
when a facility outgrows exact tree search. Keeping the model identical makes
that switch a configuration change (rules.yaml solver.algorithm).
"""
from __future__ import annotations

from .solver import Problem
from .timeutil import minutes


def _m(p: Problem, dt) -> int:
    return int((dt - p.fs.now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() // 60)


def solve_cpsat(p: Problem, time_limit_s: float = 20.0) -> dict:
    from ortools.sat.python import cp_model
    m = cp_model.CpModel()
    H = 24 * 60 + 600
    lead = _m(p, p.lead)
    close = _m(p, p.fs.close_at)
    lns = _m(p, p.fs.last_new_start) if p.fs.last_new_start else None
    per_dock = {d: [] for d in p.static}
    for d, lst in p.static.items():
        merged = []  # fixed work may overlap itself (overrun + dock event): merge first
        for b in sorted(lst, key=lambda b: b.start):
            s, e = max(0, _m(p, b.start)), min(H, _m(p, b.end))
            if e <= s:
                continue
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in merged:
            per_dock[d].append(m.NewIntervalVar(s, e - s, e, f"fix_{d}_{s}"))
    slot_use: dict[str, list] = {}
    cost_terms = []
    for t in p.trucks:
        lits = []
        rel = _m(p, t.release)
        for k, cd in enumerate(t.cands):
            x = m.NewBoolVar(f"x_{t.sid}_{k}")
            lits.append(x)
            lo = max(_m(p, cd.s_start), rel, lead)
            hi = _m(p, cd.s_end) - 1
            hi = min(hi, close - t.dur)
            if t.deadline is not None:
                hi = min(hi, _m(p, t.deadline) - p.exit_min - t.dur)
            if hi < lo:
                m.Add(x == 0)
                continue
            st = m.NewIntVar(lo, hi, f"s_{t.sid}_{k}")
            iv = m.NewOptionalIntervalVar(st, t.dur, st + t.dur, x, f"i_{t.sid}_{k}")
            per_dock[cd.dock_id].append(iv)
            slot_use.setdefault(cd.slot_id, []).append(x)
            # cost of this choice (only counts when x is true)
            wait = m.NewIntVar(0, H, f"w_{t.sid}_{k}")
            m.Add(wait == st - rel).OnlyEnforceIf(x)
            m.Add(wait == 0).OnlyEnforceIf(x.Not())
            cost_terms.append(p.c_wait * t.weight * wait)
            if t.promise_start is not None:
                due = _m(p, t.promise_start) + p.grace
                late = m.NewIntVar(0, H, f"l_{t.sid}_{k}")
                m.Add(late >= st - due).OnlyEnforceIf(x)
                m.Add(late == 0).OnlyEnforceIf(x.Not())
                cost_terms.append(p.c_late * t.weight * late)
                if t.promise_reachable and cd.slot_id != t.promise_slot:
                    cost_terms.append(p.c_move * x)
            if lns is not None:
                a = m.NewBoolVar(f"a_{t.sid}_{k}")
                m.Add(st <= lns).OnlyEnforceIf(a.Not())
                m.Add(a <= x)
                cost_terms.append(p.c_appr * a)
        u = m.NewBoolVar(f"u_{t.sid}")
        m.AddExactlyOne(lits + [u])
        cost_terms.append(p.c_unsched * t.weight * u)
    for xs in slot_use.values():
        m.AddAtMostOne(xs)
    for d, ivs in per_dock.items():
        m.AddNoOverlap(ivs)
    m.Minimize(sum(cost_terms))
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = time_limit_s
    s.parameters.num_workers = 1
    s.parameters.random_seed = 7
    r = s.Solve(m)
    return {"status": s.StatusName(r), "cost": s.ObjectiveValue(), "bound": s.BestObjectiveBound(),
            "ms": round(s.WallTime() * 1000, 1)}
