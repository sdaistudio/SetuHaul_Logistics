"""Contracts of the three layers (model / tool / database) and the facility scheduling tool."""
import tempfile
import unittest
from pathlib import Path

from setuhaul import database as D, model as M, tool as T
from setuhaul.guardrails import GuardrailViolation
from setuhaul.scheduling_tool import OBJECTIVE, _rank_key, propose_sequence
from setuhaul.timeutil import parse

at = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731


def fresh():
    return D.open_working_copy(Path(tempfile.mkdtemp()) / "t.db")


class ModelLayer(unittest.TestCase):
    def test_understands_three_languages(self):
        for text, lang in [("Reaching around 11:20", "en"), ("मैं 45 मिनट लेट हो जाऊँगा", "hi"),
                           ("lagbhag 11:30 tak pahunch jaunga", "hinglish")]:
            p = M.understand(text, at("09:40"))
            self.assertEqual(p.lang, lang, text)
            self.assertIn("REPORT_DELAY", p.intents, text)

    def test_asks_before_tool_call(self):
        p = M.understand("thoda late ho jaunga", at("09:53"))
        self.assertIn("arrival_time", M.missing_facts(p, 1, True))
        p = M.understand("I will be late by 45 minutes.", at("09:31"))
        self.assertEqual(M.missing_facts(p, 2, False), ["which_shipment"])   # DRV004 has two trips

    def test_model_has_no_database_or_write_access(self):
        import ast
        tree = ast.parse(Path(M.__file__).read_text())
        imported = {(n.module or "") + ":" + a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        for bad in ("sqlite3", "database", "capacity", "solver", "feasibility", "hold_slot", "confirm_booking"):
            self.assertFalse(any(bad in i for i in imported), bad)
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("execute", calls)


class ToolLayer(unittest.TestCase):
    def test_contracts_are_complete(self):
        for s in T.SPECS.values():
            self.assertIn(s.kind, ("READ", "WRITE"))
            self.assertTrue(s.purpose and s.output and s.inputs)

    def test_unknown_tool_and_wrong_caller(self):
        conn = fresh()
        with self.assertRaises(T.ToolError):
            T.call(conn, "book_anything")
        with self.assertRaises(T.ToolError):   # only a coordinator decides pending bookings
            T.call(conn, "decide_pending_booking", state="OPTIONS_SHOWN", appointment_id="X", approve=True,
                   approver="bot", now=at("10:00"))

    def test_write_tool_blocked_in_wrong_state(self):
        conn = fresh()
        with self.assertRaises(GuardrailViolation):
            T.call(conn, "hold_slot", state="NEW", shipment_id="SHP1017", slot_id="SLOT-JAI-001", now=at("10:00"))

    def test_asking_never_writes_capacity(self):
        conn = fresh()
        before = conn.execute("SELECT (SELECT COUNT(*) FROM appointments) + (SELECT COUNT(*) FROM slot_holds)").fetchone()[0]
        out = T.list_slot_options(conn, "SHP1017", at("10:02"))
        after = conn.execute("SELECT (SELECT COUNT(*) FROM appointments) + (SELECT COUNT(*) FROM slot_holds)").fetchone()[0]
        self.assertTrue(out["options"])
        self.assertEqual(before, after)

    def test_trace_records_tool_calls(self):
        conn = fresh()
        with T.capture() as calls:
            T.call(conn, "get_eta", shipment_id="SHP1006")
        self.assertEqual([c.name for c in calls], ["get_eta"])


class DatabaseLayer(unittest.TestCase):
    def test_named_queries_run(self):
        conn = fresh()
        args = {"driver": "DRV004", "driver_open_shipments": "DRV004", "shipment": "SHP1015", "vehicle": "VEH001",
                "thread": "THR003", "thread_messages": "THR003", "agent_state": "X", "eta_updates": "SHP1006",
                "latest_eta_view": "SHP1006", "checkin": "SHP1003", "current_appointment": "SHP1006",
                "appointment": "APT1008", "facility_slots": "FAC-JAI-01", "slot_availability_view": "FAC-JAI-01",
                "dock_events": "FAC-JAI-01", "facility_rules": "FAC-JAI-01", "facility_queue_view": "FAC-JAI-01",
                "contacts": "FAC-JAI-01", "ops_messages": "SHP1015"}
        for name in D.Q:
            D.q(conn, name, *([args[name]] if name in args else []))
        self.assertEqual(len(D.q(conn, "driver_open_shipments", "DRV004")), 2)

    def test_capture_sql_sees_tables(self):
        conn = fresh()
        with D.capture_sql(conn) as cap:
            D.q(conn, "current_appointment", "SHP1006")
        self.assertTrue({"appointments", "appointment_slots", "docks"} <= set(cap.tables()["read"]))


class FacilitySchedulingTool(unittest.TestCase):
    def test_proposes_without_writing(self):
        conn = fresh()
        p = propose_sequence(conn, "FAC-JAI-01", at("09:50"), "test")
        self.assertFalse(p.wrote_to_database)
        self.assertTrue(any(x["policy"].startswith("OPTIMAL") for x in p.policies))
        self.assertTrue(any("SHP1002" in x["what"] for x in p.protected))      # unloading truck is protected
        best = min(_rank_key(x["metrics"]) for x in p.policies)
        self.assertEqual(_rank_key(p.recommended["metrics"]), best)
        self.assertEqual(OBJECTIVE[0], "unscheduled")


if __name__ == "__main__":
    unittest.main()
