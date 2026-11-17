"""Facility flow: replay as-of, coordinator plan/execute/verify, agent policy and guardrails."""
import tempfile
import unittest
from pathlib import Path

from setuhaul import comms, coordinator as C
from setuhaul.agents import Orchestrator
from setuhaul.facility_day import as_of, snapshot
from setuhaul.timeutil import parse

T = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731
FAC = "FAC-JAI-01"


def db(hm):
    return as_of(T(hm), Path(tempfile.mkdtemp()) / "f.db")


class Replay(unittest.TestCase):
    def test_no_future_data(self):
        c = db("09:30")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM eta_updates WHERE created_at > '2026-08-04T09:30:00+05:30'").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM appointments WHERE booked_at > '2026-08-04T09:30:00+05:30'").fetchone()[0], 0)
        s = snapshot(c, FAC, T("09:30"))
        self.assertEqual(len(s["docks"]), 6)
        self.assertTrue(any(e["type"] == "BREAKDOWN" for e in s["events"]))          # D3 failed at 09:15
        self.assertFalse(any(e["type"] == "BREAKDOWN" for e in snapshot(db("08:30"), FAC, T("08:30"))["events"]))


class Console(unittest.TestCase):
    def test_plan_execute_verify(self):
        c = db("09:50")
        p = C.plan(c, FAC, T("09:50"), "repair", with_baselines=False)
        self.assertTrue(p["actions"])
        self.assertFalse([a for a in p["actions"] if a.kind == "MOVE_PROMISE"])     # repair never moves working bookings
        res = C.execute(c, p["actions"], T("09:50"), "coordinator:test", "t1")
        self.assertTrue(all(r["ok"] for r in res))
        self.assertEqual(C.verify(c, FAC), [])

    def test_deterministic(self):
        a = [(x.shipment, x.slot_id) for x in C.plan(db("09:50"), FAC, T("09:50"), "improve", with_baselines=False)["actions"]]
        b = [(x.shipment, x.slot_id) for x in C.plan(db("09:50"), FAC, T("09:50"), "improve", with_baselines=False)["actions"]]
        self.assertEqual(a, b)


class Agents(unittest.TestCase):
    def test_assist_never_moves_working_bookings(self):
        c = db("08:30")
        r = Orchestrator(c, FAC, "assist").run(T("08:30"), T("08:00"))
        self.assertFalse([x for x in r.auto if x["kind"] not in ("REBOOK", "NEW", "YARD")])
        self.assertEqual(r.verify_after, [])

    def test_shadow_writes_nothing(self):
        c = db("09:50")
        n = c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        Orchestrator(c, FAC, "shadow").run(T("09:50"), T("09:30"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], n)

    def test_language_by_script(self):
        self.assertEqual(comms.detect("நான் தாமதமாக வருவேன்"), "ta")
        self.assertEqual(comms.detect("मैं लेट हूँ"), "hi")
        self.assertEqual(comms.detect("bhai late ho jaunga yaar"), "hinglish")


if __name__ == "__main__":
    unittest.main()
