"""Solver proofs: optimal, deterministic, respects closures, anytime, looks ahead."""
from __future__ import annotations

import itertools
import tempfile
import unittest
import warnings
from pathlib import Path

from setuhaul.capacity import decide_pending
from setuhaul.db import connect, prepare_working_copy
from setuhaul.eta import resolve_eta
from setuhaul.feasibility import Constraints, evaluate_all
from setuhaul.location import toll_watchdog, value_of_information
from setuhaul.solver import _total, build_problem, rank_for_driver, solve
from setuhaul.timeutil import parse

warnings.simplefilter("ignore", ResourceWarning)
T = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731


def fresh():
    return connect(prepare_working_copy(Path(tempfile.mkdtemp()) / "s.db"))


class SolverProofs(unittest.TestCase):
    def test_matches_brute_force_on_trimmed_instance(self):
        c = fresh()
        p = build_problem(c, "FAC-JAI-01", T("09:45"), "plan")
        p.trucks = sorted(p.trucks, key=lambda t: t.sid)[:6]
        for t in p.trucks:
            t.cands = t.cands[:3]
        best = float("inf")
        for combo in itertools.product(*[t.cands + [None] for t in p.trucks]):
            cost = _total(p, {t.sid: cd for t, cd in zip(p.trucks, combo)}, p.trucks)
            if cost is not None:
                best = min(best, cost)
        sol = solve(p, 10000)
        self.assertEqual(sol.status, "PROVEN_OPTIMAL")
        self.assertAlmostEqual(sol.cost, best)

    def test_matches_cpsat_on_full_facility(self):
        try:
            from setuhaul.cpsat_check import solve_cpsat
        except ImportError:
            self.skipTest("ortools not installed")
        try:
            import ortools  # noqa: F401
        except ImportError:
            self.skipTest("ortools not installed")
        c = fresh()
        for hm in ("09:30", "09:45"):
            sol = solve(build_problem(c, "FAC-JAI-01", T(hm), "plan"), 10000)
            cp = solve_cpsat(build_problem(c, "FAC-JAI-01", T(hm), "plan"))
            self.assertEqual(sol.status, "PROVEN_OPTIMAL")
            self.assertEqual(cp["status"], "OPTIMAL")
            self.assertAlmostEqual(sol.cost, cp["cost"])

    def test_deterministic(self):
        c = fresh()
        a = solve(build_problem(c, "FAC-JAI-01", T("09:45"), "plan")).as_dict()
        b = solve(build_problem(c, "FAC-JAI-01", T("09:45"), "plan")).as_dict()
        for d in (a, b):
            d.pop("ms")
        self.assertEqual(a, b)

    def test_closed_dock_is_never_used(self):
        c = fresh()
        p = build_problem(c, "FAC-JAI-01", T("09:45"), "plan")
        sol = solve(p)
        events = [(e["dock_id"], parse(e["event_start_ts"]), parse(e["event_end_ts"]))
                  for e in c.execute("SELECT * FROM dock_status_events")]
        dock_of = {r["slot_id"]: r["dock_id"] for r in c.execute("SELECT slot_id, dock_id FROM appointment_slots")}
        for pl in sol.placements.values():
            if pl.start is None:
                continue
            for d, s, e in events:
                if dock_of[pl.slot_id] == d:
                    self.assertFalse(pl.start < e and pl.end > s, f"{pl.sid} on closed {d}")
        self.assertIsNone(sol.placements["SHP1015"].slot_id)  # reefer: nothing left -> person

    def test_anytime_budget_returns_feasible_with_bound(self):
        c = fresh()
        sol = solve(build_problem(c, "FAC-JAI-01", T("12:00"), "plan"), time_budget_ms=50, node_limit=600)
        self.assertEqual(sol.status, "BEST_WITHIN_BUDGET")
        self.assertGreaterEqual(sol.cost, sol.lower_bound)
        self.assertEqual(len(sol.placements), sol.trucks)

    def test_look_ahead_protects_driver_with_deadline(self):
        """SHP1012 must leave by 13:30 (EXC002); its only slot is D4 12:00-13:00.
        Earliest-first would give that slot to SHP1006; the solver does not."""
        c = fresh()
        cons = Constraints(earliest_start=T("12:00"))
        evals, eta = evaluate_all(c, "SHP1006", T("09:34"), cons)
        feas = sorted((e for e in evals if e.feasible), key=lambda e: e.planned_start)
        self.assertEqual(feas[0].slot_id, "SLOT-JAI-047")  # earliest-first would pick it
        ranked = rank_for_driver(c, "SHP1006", T("09:34"), eta, cons, [e.slot_id for e in feas])
        self.assertNotEqual(ranked[0].slot_id, "SLOT-JAI-047")
        took_it = next(r for r in ranked if r.slot_id == "SLOT-JAI-047")
        self.assertIn("SHP1012", took_it.others_unscheduled)

    def test_human_approval_is_still_validated(self):
        c = fresh()
        res = decide_pending(c, "APT1014A", True, "CON001", T("09:46"))  # 21 t on a 20 t dock
        self.assertEqual(res["status"], "REFUSED_INFEASIBLE")
        self.assertIn("WEIGHT", res["reasons"])


class LocationOnDemand(unittest.TestCase):
    def test_watchdog_flags_overdue_toll_with_silent_gps(self):
        sig = {w.shipment_id: w for w in toll_watchdog(T("09:50"))}
        self.assertEqual(sig["SHP1013"].action, "OPS_REVIEW")
        self.assertEqual(sig["SHP1006"].action, "OK")

    def test_voi_buys_only_decision_changing_fixes_within_budget(self):
        c = fresh()
        adv = value_of_information(c, "FAC-JAI-01", T("09:50"))
        bought = [a for a in adv if a.buy]
        self.assertTrue(all(a.plan_changes for a in bought))
        self.assertLess(len(bought), len(adv))
        self.assertEqual(next(a for a in bought if a.shipment_id == "SHP1013").source, "fastag_last_toll_ulip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
