"""Builds prototype/index.html (discussion prototype) from scenario + eval outputs."""
import json, html
from pathlib import Path
R = Path(__file__).resolve().parent.parent
data = {
  "log": json.loads((R/"scenarios/transcript.json").read_text()),
  "sched": json.loads((R/"scenarios/scheduler_runs.json").read_text()),
  "metrics": json.loads((R/"scenarios/scenario_metrics.json").read_text()),
  "board": json.loads((R/"scenarios/dock_board.json").read_text()),
  "evals": json.loads((R/"evals/report.json").read_text()),
  "counters": json.loads((R/"scenarios/telemetry_counters.json").read_text()),
  "trace": [json.loads(l) for l in (R/"scenarios/telemetry_sample.jsonl").read_text().splitlines()[:14]],
}
for k in ("nlu","nlu_holdout"):
    data["evals"][k].pop("failures_full", None)
import sys, tempfile, warnings
warnings.simplefilter("ignore")
sys.path.insert(0, str(R))
from setuhaul.db import prepare_working_copy, connect
from setuhaul.feasibility import Constraints, evaluate_all
from setuhaul.solver import rank_for_driver
from setuhaul.location import value_of_information, toll_watchdog
from setuhaul.timeutil import parse, hhmm
T = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")
c = connect(prepare_working_copy(Path(tempfile.mkdtemp())/"v.db"))
cons = Constraints(earliest_start=T("12:00"))
ev, eta = evaluate_all(c, "SHP1006", T("09:34"), cons)
feas = sorted((e for e in ev if e.feasible), key=lambda e: (e.planned_start, e.dock_code))
rk = rank_for_driver(c, "SHP1006", T("09:34"), eta, cons, [e.slot_id for e in feas])
pos = {r.slot_id: i + 1 for i, r in enumerate(rk)}
data["lookahead"] = [{"earliest_rank": i + 1, "solver_rank": pos.get(e.slot_id), "slot": f"{e.dock_code} {hhmm(e.slot_start)}-{hhmm(e.slot_end)}",
                      "start": hhmm(e.planned_start), "facility_cost": next((round(r.facility_cost) for r in rk if r.slot_id == e.slot_id), None),
                      "strands": next((r.others_unscheduled for r in rk if r.slot_id == e.slot_id), [])} for i, e in enumerate(feas[:8])]
data["voi"] = [a.__dict__ for a in value_of_information(c, "FAC-JAI-01", T("09:50"))]
data["watch"] = [{**w.__dict__, "expected_by": hhmm(w.expected_by)} for w in toll_watchdog(T("09:50"))]
data["rules_yaml"] = (R/"rules/rules.yaml").read_text()
svg = (R/"docs/architecture.svg").read_text()
tpl = (R/"prototype/viewer_template.html").read_text()
out = tpl.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")).replace("<!--SVG-->", svg)
(R/"prototype/index.html").write_text(out)
print("written", len(out))
