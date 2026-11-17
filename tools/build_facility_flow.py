"""Facility flow page -> site/facility.html

One facility (Jaipur DC, six docks), one day. For each event: the dock board as it was
known, Stage 1 (coordinator console: algorithm -> action cards -> approval -> booked and
verified) and Stage 2 (the same process run by agents with guardrails), plus the
evaluation report from evals/facility_evals.py.

    python tools/build_facility_flow.py      (called by tools/build_site.py)
"""
from __future__ import annotations

import json
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.simplefilter("ignore")

from setuhaul import coordinator as C, database as D  # noqa: E402
from setuhaul.agents import PERMISSIONS, Orchestrator  # noqa: E402
from setuhaul.facility_day import ANOMALIES, as_of, snapshot  # noqa: E402
from setuhaul.rules import R  # noqa: E402
from setuhaul.timeutil import iso, parse  # noqa: E402

FAC = "FAC-JAI-01"
T = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731

EVENTS = [
    {"id": "E0830", "at": "08:30", "since": "08:00", "kind": "real", "title": "08:30 · morning, D2 overrunning",
     "what": "D2 is slowed by an overrun from the previous shift; one early truck is at the gate; several trucks have no booking yet."},
    {"id": "E0930", "at": "09:30", "since": "08:30", "kind": "real", "title": "09:30 · D3 breaks down",
     "what": "D3's dock leveller failed at 09:15 (until 13:00). Two drivers have reported delays; three trucks are waiting in the yard."},
    {"id": "E0950", "at": "09:50", "since": "09:30", "kind": "real", "title": "09:50 · the delay spike",
     "what": "Within 20 minutes five drivers report delays (tyre, traffic, late release); a heavy trailer and a critical load need docks."},
    {"id": "W_REEFER", "at": "09:50", "since": "09:30", "kind": "what-if", "title": "What-if · reefer will arrive 18:30",
     "what": "Same 09:50 state, plus the reefer SHP1015 reports ETA 18:30. The only reefer dock (D5) is under maintenance 18:00-22:00.",
     "inject": [("eta", "SHP1015", "DRV015", "18:30", "HIGH")]},
    {"id": "W_D4", "at": "09:50", "since": "09:30", "kind": "what-if", "title": "What-if · D4 fails 10:30-13:30",
     "what": "Same 09:50 state, plus D4 (a 25 t standard dock) breaks down from 10:30 to 13:30 while D3 is also down.",
     "inject": [("dock", "DOCK-JAI-D4", "10:30", "13:30")]},
]


def event_db(ev):
    conn = as_of(T(ev["at"]), Path(tempfile.mkdtemp()) / f"{ev['id']}.db")
    with D.write_txn(conn):
        for inj in ev.get("inject", []):
            if inj[0] == "eta":
                _, sid, drv, hm, conf = inj
                conn.execute("INSERT INTO eta_updates VALUES (?,?,?,?,?,?,?,?,?)",
                             (f"ETA-WI-{sid}", sid, "DRIVER_DECLARED", drv, iso(T(hm)), conf, "TRAFFIC", "what-if", iso(T(ev["at"]))))
                conn.execute("""INSERT INTO chat_messages(chat_message_id, thread_id, sender_type, message_ts, message_text)
                                SELECT 'MSG-WI-1', thread_id, 'DRIVER', ?, 'Evening traffic. ETA 6:30. Can the reefer unload tonight?'
                                FROM chat_threads WHERE shipment_id=? LIMIT 1""", (iso(T(ev["at"])), sid))
            if inj[0] == "dock":
                _, dock, a, b = inj
                conn.execute("INSERT INTO dock_status_events VALUES ('DEVT-WI',?, 'BREAKDOWN', ?, ?, 'What-if: hydraulic failure', ?)",
                             (dock, iso(T(a)), iso(T(b)), iso(T(ev["at"]))))
    return conn


def acts(xs):
    return [a.as_dict() for a in xs]


def build_event(ev):
    t, since = T(ev["at"]), T(ev["since"])
    c0 = event_db(ev)
    before = snapshot(c0, FAC, t)
    # ---------- Stage 1: coordinator console
    c1 = event_db(ev)
    rep = C.plan(c1, FAC, t, "repair")
    keep = C.status_quo_cost(c1, FAC, t)
    imp = C.plan(c1, FAC, t, "improve", with_baselines=False)
    groups = C.approval_groups(rep["actions"])
    msgs = C.party_messages(c1, FAC, ev["at"], rep["actions"]) if rep["actions"] else {}
    res1 = C.execute(c1, rep["actions"], t, "coordinator:CON001", ev["id"] + "-A")
    after1 = snapshot(c1, FAC, t)
    v1 = C.verify(c1, FAC)
    saving = round(keep - imp["solver"]["cost"])
    stage1 = {"repair": {**{k: v for k, v in rep.items() if k != "actions"}, "actions": acts(rep["actions"]),
                         "groups": groups, "messages": msgs, "executed": res1, "after": after1, "verify": v1},
              "improve": {"solver": imp["solver"], "keep_cost": round(keep), "saving": saving,
                          "threshold": R()["agent_autonomy"]["propose_improvement_if_saving_over"],
                          "actions": acts(imp["actions"])}}
    if saving > 0:
        c2 = event_db(ev)
        imp2 = C.plan(c2, FAC, t, "improve", with_baselines=False)
        res2 = C.execute(c2, imp2["actions"], t, "coordinator:CON001", ev["id"] + "-B")
        stage1["improve"].update(executed=res2, after=snapshot(c2, FAC, t), verify=C.verify(c2, FAC))
    # ---------- Stage 2: agents
    c3 = event_db(ev)
    run = Orchestrator(c3, FAC, "assist").run(t, since, ev["id"])
    stage2 = {"steps": [s.__dict__ for s in run.steps], "auto": run.auto, "queue": run.queue, "escalations": run.escalations,
              "messages": run.messages, "intake": run.intake, "plan": run.plan, "improvement": run.improvement,
              "verify_before": run.verify_before, "verify_after": run.verify_after, "open_with_person": run.open_with_person,
              "stopped": run.stopped,
              "after": snapshot(c3, FAC, t)}
    print(f"{ev['id']}: repair {len(rep['actions'])} actions ({rep['solver']['status']}), exec ok "
          f"{sum(x['ok'] for x in res1)}/{len(res1)}, verify {len(v1)}; improve saving {saving}; agents auto {len(run.auto)} "
          f"queue {len(run.queue)} esc {len(run.escalations)} verify {len(run.verify_after)}")
    return {**{k: v for k, v in ev.items() if k != "inject"}, "before": before, "stage1": stage1, "stage2": stage2}


def main():
    events = [build_event(ev) for ev in EVENTS]
    rep_path = ROOT / "evals" / "facility_report.json"
    evals = json.loads(rep_path.read_text()) if rep_path.exists() else None
    if evals:
        evals.pop("runs", None)
    data = {"events": events, "evals": evals, "permissions": {k: sorted(v) for k, v in PERMISSIONS.items()},
            "autonomy": R()["agent_autonomy"], "costs": R()["costs"], "weights": R()["priority_weight"],
            "anomalies": sorted({a for v in ANOMALIES.values() for a in v})}
    tpl = (ROOT / "tools" / "facility_template.html").read_text(encoding="utf-8")
    out = ROOT / "site" / "facility.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(tpl.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")),
                   encoding="utf-8")
    print("facility page written", out, round(out.stat().st_size / 1024), "KB")


if __name__ == "__main__":
    main()
