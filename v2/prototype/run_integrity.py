"""Integrity report: unit tests + 50 repeats of the 10-driver race + negative controls.

Run:  python3 -I run_integrity.py out/
"""
import json, os, shutil, sys, time, unittest, io
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
import test_integrity as TI
from setuhaul import db

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
REPEATS = int(os.environ.get("RACE_REPEATS", "50"))

stream = io.StringIO()
suite = unittest.defaultTestLoader.loadTestsFromTestCase(TI.Integrity)
t0 = time.time()
res = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
tests_s = round(time.time() - t0, 1)
print(stream.getvalue())

runs, t0 = [], time.time()
for i in range(REPEATS):
    path, d = TI.fresh(f"rep{i}")
    out, errs = TI.race(path)
    con = db.connect(path)
    winners = [s for s, r in out.items() if r["ok"]]
    runs.append({"winner": winners[0] if winners else None, "winners": len(winners),
                 "clean_refusals": sum(1 for r in out.values() if not r["ok"] and r["code"] == "OPTION_GONE"),
                 "leaked_errors": len(errs), "active_on_slot": TI.active_on(con, TI.RACE_SLOT),
                 "invariant_violations": sum(TI.invariants(con).values())})
    con.close()
    shutil.rmtree(d)
race_s = round(time.time() - t0, 1)

neg = []
for keep in (False, True):
    for i in range(5):
        path, d = TI.fresh(f"neg{keep}{i}")
        out = TI.naive_race(path, keep_index=keep)
        neg.append({"unique_index": keep, "booked": sum(1 for v in out.values() if v == "BOOKED"),
                    "raw_errors": sum(1 for v in out.values() if v.startswith("RAW_ERROR"))})
        shutil.rmtree(d)

report = {
    "unit_tests": {"ran": res.testsRun, "failures": len(res.failures), "errors": len(res.errors), "seconds": tests_s,
                   "detail": [l for l in stream.getvalue().splitlines() if " ... " in l]},
    "race_repeats": {"repeats": REPEATS, "racers_per_run": len(TI.RACERS), "slot": TI.RACE_SLOT, "seconds": race_s,
                     "runs_with_exactly_one_winner": sum(1 for r in runs if r["winners"] == 1),
                     "total_double_bookings": sum(max(0, r["active_on_slot"] - 1) for r in runs),
                     "total_leaked_errors": sum(r["leaked_errors"] for r in runs),
                     "total_clean_refusals": sum(r["clean_refusals"] for r in runs),
                     "distinct_winners": sorted({r["winner"] for r in runs if r["winner"]}),
                     "invariant_violations": sum(r["invariant_violations"] for r in runs)},
    "negative_controls": neg,
    "stress": getattr(TI.Integrity, "stress_stats", None),
    "guard_messages": getattr(TI.Integrity, "guard_results", None),
}
json.dump(report, open(os.path.join(OUT, "integrity_results.json"), "w"), indent=1)
print(json.dumps({k: v for k, v in report.items() if k != "unit_tests"}, indent=1))
print("UNIT TESTS:", res.testsRun, "ran,", len(res.failures), "failures,", len(res.errors), "errors")
