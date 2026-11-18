"""Evaluation harness. Deterministic -> every run gives the same numbers, so it
can gate a deployment (CodeBuild step in infra/buildspec.yml).

    python -m evals.run_evals            # writes evals/report.json + report.md
Exit code 1 if any threshold fails.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from setuhaul.db import connect, prepare_working_copy  # noqa: E402
from setuhaul.feasibility import Constraints, evaluate_all, rank_options  # noqa: E402
from setuhaul.guardrails import check_input, check_output  # noqa: E402
from setuhaul.nlu import parse_message  # noqa: E402
from setuhaul.timeutil import hhmm, parse  # noqa: E402

DS = ROOT / "evals" / "datasets"
THRESHOLDS = {"nlu_case_accuracy": 0.90, "nlu_holdout_accuracy": 0.70, "nlu_min_language_accuracy": 0.85, "feasibility": 1.0,
              "guardrails": 1.0, "integrity_tests": 1.0,
              "solver_optimality": 1.0}
T = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731


def eval_nlu(name: str = "nlu_golden.json"):
    ds = json.loads((DS / name).read_text())
    ref = parse(ds["reference_eta"])
    rows, field_hits, field_tot = [], defaultdict(int), defaultdict(int)
    for c in ds["cases"]:
        now = parse(c.get("now", ds["now"]))
        r = parse_message(c["text"], now, ref)
        got = r.as_dict()
        errs = []
        for k, v in c["expect"].items():
            field_tot[k] += 1
            if k == "intent":
                ok = v in r.intents
            elif k == "intent_any":
                ok = any(x in r.intents for x in v)
            elif k in ("flags", "clarify", "shipment_refs"):
                ok = all(x in got[k] for x in v)
            else:
                ok = got.get(k) == v
            field_hits[k] += ok
            if not ok:
                errs.append(f"{k}: expected {v!r} got {got.get(k) if k not in ('intent', 'intent_any') else r.intents!r}")
        rows.append({"id": c["id"], "lang": c["lang"], "text": c["text"], "pass": not errs, "errors": errs,
                     "detected_lang": r.lang})
    by_lang = defaultdict(lambda: [0, 0])
    for r in rows:
        by_lang[r["lang"]][0] += r["pass"]
        by_lang[r["lang"]][1] += 1
    lang_acc = {k: round(a / b, 3) for k, (a, b) in by_lang.items()}
    lang_detect = round(sum(r["lang"] == r["detected_lang"] for r in rows) / len(rows), 3)
    return {
        "cases": len(rows), "case_accuracy": round(sum(r["pass"] for r in rows) / len(rows), 3),
        "by_language": lang_acc, "language_detection_accuracy": lang_detect,
        "field_accuracy": {k: round(field_hits[k] / field_tot[k], 3) for k in field_tot},
        "failures": [r for r in rows if not r["pass"]],
    }


def eval_feasibility():
    ds = json.loads((DS / "feasibility_golden.json").read_text())
    path = prepare_working_copy(Path(tempfile.mkdtemp()) / "f.db")
    conn = connect(path)
    rows = []
    for c in ds["cases"]:
        cons = Constraints(**{k: T(v) for k, v in c.get("constraints", {}).items()})
        evals, _ = evaluate_all(conn, c["shipment"], T(c["now"]), cons)
        opts = rank_options(evals, 10)
        e, errs = c["expect"], []
        if e.get("no_feasible") and opts:
            errs.append(f"expected none, got {[o.label() for o in opts]}")
        if "first" in e:
            got = f"{opts[0].dock_code} {hhmm(opts[0].planned_start)}" if opts else None
            if got != e["first"]:
                errs.append(f"first: expected {e['first']} got {got}")
        if "only_docks" in e and any(o.dock_code not in e["only_docks"] for o in opts):
            errs.append(f"docks {[o.dock_code for o in opts]} not within {e['only_docks']}")
        if "not_before" in e and any(o.planned_start < T(e["not_before"]) for o in opts):
            errs.append("option before allowed time")
        if "all_end_by" in e and any(o.planned_end > T(e["all_end_by"]) for o in opts):
            errs.append("option ends after driver deadline")
        for slot, reason in e.get("must_reject", {}).items():
            ev = next((x for x in evals if x.slot_id == slot), None)
            if ev is None or ev.feasible or reason not in ev.reasons:
                errs.append(f"{slot} should be rejected for {reason}: {ev.as_dict() if ev else 'missing'}")
        rows.append({"id": c["id"], "pass": not errs, "errors": errs,
                     "options": [f"{o.dock_code} {hhmm(o.planned_start)}-{hhmm(o.planned_end)}" for o in opts[:3]]})
    conn.close()
    return {"cases": len(rows), "accuracy": round(sum(r["pass"] for r in rows) / len(rows), 3), "rows": rows}


def eval_guardrails():
    ds = json.loads((DS / "guardrail_cases.json").read_text())
    rows = []
    for c in ds["output"]:
        v = check_output(c["reply"], committed_status=c["committed"], allowed_times=set(c["times"]), lang=c["lang"])
        codes = sorted({x.split(":")[0] for x in v})
        rows.append({"id": c["id"], "pass": codes == sorted(c["expect"]), "got": codes, "expected": c["expect"]})
    for c in ds["input"]:
        f = check_input(c["text"]).flags
        rows.append({"id": c["id"], "pass": sorted(f) == sorted(c["expect_flags"]), "got": f,
                     "expected": c["expect_flags"]})
    return {"cases": len(rows), "accuracy": round(sum(r["pass"] for r in rows) / len(rows), 3),
            "failures": [r for r in rows if not r["pass"]]}


def eval_integrity():
    import warnings
    warnings.simplefilter("ignore", ResourceWarning)
    suite = unittest.defaultTestLoader.loadTestsFromNames(["tests.test_integrity", "tests.test_solver"])
    buf = io.StringIO()
    res = unittest.TextTestRunner(stream=buf, verbosity=2).run(suite)
    n = res.testsRun
    bad = len(res.failures) + len(res.errors)
    return {"tests": n, "passed": n - bad, "accuracy": round((n - bad) / n, 3),
            "failures": [str(t[0]) for t in res.failures + res.errors]}


def eval_solver():
    """B&B vs exhaustive/CP-SAT at several instants; proves optimality claims."""
    from setuhaul.solver import build_problem, solve
    try:
        from setuhaul.cpsat_check import solve_cpsat
        import ortools  # noqa: F401
        have_cp = True
    except ImportError:
        have_cp = False
    path = prepare_working_copy(Path(tempfile.mkdtemp()) / "s.db")
    conn = connect(path)
    rows = []
    for hm in ("08:30", "09:30", "09:45", "10:30"):
        for mode in ("chat", "plan"):
            b = solve(build_problem(conn, "FAC-JAI-01", T(hm), mode), 10000)
            row = {"at": hm, "mode": mode, "trucks": b.trucks, "bnb_status": b.status, "bnb_cost": b.cost,
                   "nodes": b.nodes, "bnb_ms": round(b.ms)}
            if have_cp:
                cp = solve_cpsat(build_problem(conn, "FAC-JAI-01", T(hm), mode))
                row.update(cpsat_status=cp["status"], cpsat_cost=cp["cost"], cpsat_ms=cp["ms"],
                           match=abs(cp["cost"] - b.cost) < 1e-6)
            rows.append(row)
    conn.close()
    checked = [r for r in rows if "match" in r]
    return {"rows": rows, "cpsat_available": have_cp,
            "accuracy": round(sum(r["match"] for r in checked) / len(checked), 3) if checked else
            round(sum(r["bnb_status"] == "PROVEN_OPTIMAL" for r in rows) / len(rows), 3)}


def main():
    report = {"nlu": eval_nlu(), "nlu_holdout": eval_nlu("nlu_holdout.json"), "feasibility": eval_feasibility(), "guardrails": eval_guardrails(),
              "integrity": eval_integrity(), "solver": eval_solver()}
    scen = ROOT / "scenarios" / "scenario_metrics.json"
    if scen.exists():
        report["scenario_measures"] = json.loads(scen.read_text())
    gates = {
        "nlu_case_accuracy": report["nlu"]["case_accuracy"],
        "nlu_holdout_accuracy": report["nlu_holdout"]["case_accuracy"],
        "nlu_min_language_accuracy": min(report["nlu"]["by_language"].values()),
        "feasibility": report["feasibility"]["accuracy"],
        "guardrails": report["guardrails"]["accuracy"],
        "integrity_tests": report["integrity"]["accuracy"],
        "solver_optimality": report["solver"]["accuracy"],
    }
    report["gates"] = {k: {"value": v, "threshold": THRESHOLDS[k], "pass": v >= THRESHOLDS[k]} for k, v in gates.items()}
    report["all_gates_pass"] = all(g["pass"] for g in report["gates"].values())
    out = ROOT / "evals"
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (out / "report.md").write_text(to_md(report))
    print(to_md(report))
    return 0 if report["all_gates_pass"] else 1


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials (review item A11)."""
    if not n:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _gate_n(r, k):
    return {"nlu_case_accuracy": r["nlu"]["cases"], "nlu_holdout_accuracy": r["nlu_holdout"]["cases"],
            "feasibility": r["feasibility"]["cases"], "guardrails": r["guardrails"]["cases"],
            "integrity_tests": r["integrity"]["tests"], "solver_optimality": len(r["solver"]["rows"])}.get(k)


def to_md(r) -> str:
    L = ["# SetuHaul evaluation report", "",
         "Each rate is shown with its sample size and a 95% Wilson interval: on small samples the range matters more than the point "
         "(for example 16 of 20 is 80%, but the interval is about 58% to 92%).", "",
         "| Gate | Value | n | 95% interval | Threshold | Result |", "|---|---:|---:|---|---:|---|"]
    for k, g in r["gates"].items():
        n = _gate_n(r, k)
        ci = "-" if not n else "{:.0%} – {:.0%}".format(*wilson(round(g["value"] * n), n))
        L.append(f"| {k} | {g['value']:.3f} | {n or '-'} | {ci} | {g['threshold']:.2f} | {'PASS' if g['pass'] else 'FAIL'} |")
    n = r["nlu"]
    L += ["", f"## Language understanding ({n['cases']} cases)", "",
          f"Case accuracy {n['case_accuracy']:.1%}; language detection {n['language_detection_accuracy']:.1%}.", "",
          "| Language | Accuracy |", "|---|---:|"] + [f"| {k} | {v:.1%} |" for k, v in n["by_language"].items()]
    L += ["", "Field accuracy: " + ", ".join(f"{k} {v:.0%}" for k, v in n["field_accuracy"].items())]
    if n["failures"]:
        L += ["", "Failures:"] + [f"- `{f['id']}` {f['text']!r}: {'; '.join(f['errors'])}" for f in n["failures"]]
    h = r["nlu_holdout"]
    L += ["", f"### Held-out set ({h['cases']} cases, never used for tuning): {h['case_accuracy']:.1%}", "",
          "This is the honest estimate. The tuned set above is a regression test, not a measure of accuracy.", "",
          "| Language | Accuracy |", "|---|---:|"] + [f"| {k} | {v:.1%} |" for k, v in h["by_language"].items()]
    if h["failures"]:
        L += ["", "Held-out failures (left unfixed on purpose):"] + \
             [f"- `{f['id']}` {f['text']!r}: {'; '.join(f['errors'])}" for f in h["failures"]]
    f = r["feasibility"]
    L += ["", f"## Feasibility ({f['cases']} cases, {f['accuracy']:.0%})", "", "| Case | Result | First options |", "|---|---|---|"]
    L += [f"| {x['id']} | {'PASS' if x['pass'] else 'FAIL: ' + '; '.join(x['errors'])} | {', '.join(x['options']) or 'none'} |"
          for x in f["rows"]]
    g = r["guardrails"]
    L += ["", f"## Guardrails ({g['cases']} cases, {g['accuracy']:.0%})"]
    L += [f"- {x['id']}: expected {x['expected']} got {x['got']}" for x in g["failures"]]
    i = r["integrity"]
    L += ["", f"## Integrity and safety tests: {i['passed']}/{i['tests']} passed"]
    L += [f"- FAIL {x}" for x in i["failures"]]
    sv = r["solver"]
    L += ["", "## Solver: branch and bound, cross-checked" + (" with OR-Tools CP-SAT" if sv["cpsat_available"] else ""), "",
          "| At | Mode | Trucks | B&B | B&B cost | Nodes | B&B ms | CP-SAT | CP-SAT cost | CP-SAT ms | Match |",
          "|---|---|---:|---|---:|---:|---:|---|---:|---:|---|"]
    L += [f"| {x['at']} | {x['mode']} | {x['trucks']} | {x['bnb_status']} | {x['bnb_cost']:.0f} | {x['nodes']} | {x['bnb_ms']} | "
          f"{x.get('cpsat_status', '-')} | {x.get('cpsat_cost', 0):.0f} | {x.get('cpsat_ms', '-')} | {x.get('match', '-')} |"
          for x in sv["rows"]]
    if "scenario_measures" in r:
        L += ["", "## Scenario-run measures (brief, page 9)", "", "| Measure | Value |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in r["scenario_measures"].items()]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
