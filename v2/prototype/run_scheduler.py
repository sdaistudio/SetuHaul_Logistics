"""Optional deliverable: the facility scheduling tool against simple dispatch rules.

Builds the 'competing trucks' snapshot for Jaipur at 09:45 (every delay of the
morning wave is known, nothing has been re-booked yet), then plans it with six
policies and three weight sets. Draws Gantt charts of FCFS vs our plan.

Run:  python3 -I run_scheduler.py out/
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from setuhaul import db, world as Wd, planner as P, timeline as T, config as C
from setuhaul.clock import ts, iso, hhmm, add

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
D = lambda hm: ts(f"2026-08-04T{hm}:00+05:30")
path = db.fresh_copy(os.path.join(OUT, "scheduler_snapshot.db"))
con = db.connect(path)
Wd.rewind(con, D("08:20"))
# The morning as it happened in the seed data (no re-booking by our service)
Wd.eta_update(con, "ETA003", "SHP1004", "DRIVER_DECLARED", "DRV004", D("09:20"), "HIGH", "TRAFFIC", "Traffic at Tonk Road.", D("08:30"))
Wd.unload_done(con, "SHP1001", D("08:42"))
Wd.dock_in(con, "SHP1003", "DOCK-JAI-D1", D("08:55"))
Wd.eta_update(con, "ETA005", "SHP1006", "DRIVER_DECLARED", "DRV006", D("10:50"), "MEDIUM", "TRAFFIC", "Traffic after Shahpura.", D("08:50"))
Wd.gate_in(con, "SHP1005", D("09:05"), "LATE")
Wd.dock_event(con, "DEVT001", "DOCK-JAI-D3", "BREAKDOWN", D("09:15"), D("13:00"), "Hydraulic dock leveller failure", D("09:16"))
Wd.eta_update(con, "ETA008", "SHP1013", "DRIVER_DECLARED", "DRV013", D("11:00"), "LOW", "OTHER", "Driver said only: late by one hour.", D("09:20"))
Wd.gate_in(con, "SHP1004", D("09:25"), "LATE")
Wd.unload_done(con, "SHP1002", D("09:28"))
Wd.eta_update(con, "ETA007", "SHP1012", "DRIVER_DECLARED", "DRV012", D("11:10"), "HIGH", "BREAKDOWN", "Tyre repaired; moving again.", D("09:30"))
Wd.eta_update(con, "ETA006", "SHP1006", "DRIVER_DECLARED", "DRV006", D("11:20"), "HIGH", "TRAFFIC", "Traffic worsened; latest estimate.", D("09:35"))
Wd.dock_in(con, "SHP1004", "DOCK-JAI-D2", D("09:38"))
Wd.eta_update(con, "ETA011", "SHP1016", "DRIVER_DECLARED", "DRV007", D("12:00"), "HIGH", "ROUTE_ISSUE", "Diversion for heavy vehicle.", D("09:40"))
Wd.eta_update(con, "ETA009", "SHP1014", "DRIVER_DECLARED", "DRV014", D("11:25"), "HIGH", "LOADING_DELAY", "Origin loading completed late.", D("09:45"))
with db.write_txn(con):
    con.execute("INSERT INTO driver_constraints VALUES ('SHP1012','LATEST_GATE_OUT',?, 'leave before 1:30', ?)", (iso(D("13:30")), iso(D("09:30"))))
    con.execute("INSERT INTO driver_constraints VALUES ('SHP1006','EARLIEST_START',?, 'any slot after 12', ?)", (iso(D("12:00")), iso(D("09:35"))))
    # requests arrive in this order (message times), which is what first-come-first-served follows
AS_OF = D("09:45")

results = P.compare(con, "FAC-JAI-01", AS_OF)
table = [k for k, _ in results]
plans = {k["policy"]: {sid: (v and {"dock": v["dock_code"], "start": hhmm(v["start"]), "wait": v["wait"], "approval": v["approval"]})
                       for sid, v in r["plan"].items()} for k, r in results}
jobs = [{"shipment": j["ship"]["shipment_id"], "priority": j["ship"]["priority_code"], "dock_need": j["ship"]["required_dock_type"],
         "weight_t": j["ship"]["load_weight_kg"] / 1000, "unload_min": j["ship"]["expected_unload_min"],
         "release": hhmm(j["eta"].release), "eta_conf": j["eta"].confidence, "limits": {k: hhmm(v) for k, v in j["limits"].items()},
         "current": j["current"] and j["current"]["slot_id"], "current_status": j["current_status"],
         "requested_at": hhmm(j["requested_at"])} for j in results[0][1]["jobs"]]
fixed = {d: [(hhmm(b.start), hhmm(b.end), b.kind, b.ref, b.shipment_id) for b in bl] for d, bl in results[0][1]["timeline"].busy.items()}

sens = []
for name, w in (("Ours 8:4:2:1", C.WAIT_WEIGHT), ("Doc A 100:40:15:5", {"CRITICAL": 100, "HIGH": 40, "NORMAL": 15, "LOW": 5}),
                ("Doc B 4:2:1:0.5", {"CRITICAL": 4, "HIGH": 2, "NORMAL": 1, "LOW": 0.5})):
    r = P.plan_facility(con, "FAC-JAI-01", AS_OF, "PRIORITY+REPAIR", weights=w)
    sens.append({"weights": name, "plan": {s: (v and f"{v['dock_code']} {hhmm(v['start'])}") for s, v in r["plan"].items()}})
same = all(x["plan"] == sens[0]["plan"] for x in sens)

ex = next(r for k, r in results if k["policy"] == "EXACT")
engine = {"engine": "EXACT (branch and bound over truck orders)", "status": ex["status"], "nodes": ex["nodes"],
          "first_answer": ex["first_answer"], "first_answer_score": ex["first_answer_score"], "score": ex["score"],
          "reasons": ex["reasons"]}
print("engine:", ex["note"])
for sid, why in ex["reasons"].items():
    print(f"  {sid}: {why}")
json.dump({"as_of": iso(AS_OF), "jobs": jobs, "fixed": fixed, "kpis": table, "plans": plans, "sensitivity": sens,
           "plan_identical_across_weights": same, "engine": engine}, open(os.path.join(OUT, "scheduler_results.json"), "w"), indent=1, default=str)
for k in table:
    print(k)
print("sensitivity identical:", same)
for x in sens:
    print(x)

# ---------------- Gantt charts
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
COL = {"CRITICAL": "#C0392B", "HIGH": "#E67E22", "NORMAL": "#2E86C1", "LOW": "#7F8C8D"}
prio = {j["shipment"]: j["priority"] for j in jobs}
for s in db.rows(con, "SELECT shipment_id, priority_code FROM shipments"):
    prio.setdefault(s["shipment_id"], s["priority_code"])
docks = ["DOCK-JAI-D1", "DOCK-JAI-D2", "DOCK-JAI-D3", "DOCK-JAI-D4", "DOCK-JAI-D5", "DOCK-JAI-D6"]
hrs = lambda t: int(t[:2]) + int(t[3:]) / 60
X0, X1 = 9.5, 19.5


def gantt(policy, fname):
    r = dict(results)[[k for k in dict(results) if k["policy"] == policy][0]] if False else [rr for kk, rr in results if kk["policy"] == policy][0]
    k = [kk for kk, rr in results if kk["policy"] == policy][0]
    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=150)
    for i, d in enumerate(docks):
        y = len(docks) - 1 - i
        for (a, b, kind, ref, sid) in fixed[d]:
            lo, hi = max(hrs(a), X0), min(hrs(b), X1)
            if hi <= lo:
                continue
            if kind in ("DOCK_EVENT", "BLOCKED_SLOT"):
                ax.barh(y, hi - lo, left=lo, height=0.6, color="#D5D8DC", hatch="///", edgecolor="#95A5A6", linewidth=0.5)
            else:
                ax.barh(y, hi - lo, left=lo, height=0.6, color="#ECF0F1", edgecolor="#5D6D7E", linewidth=0.8)
                if hi - lo > 0.3:
                    ax.text((lo + hi) / 2, y, (sid or "")[-4:], ha="center", va="center", fontsize=6.5, color="#2C3E50")
        for sid, v in r["plan"].items():
            if v and T.build(con, "FAC-JAI-01", AS_OF).docks and v["dock_code"] == d[-2:]:
                st = hrs(hhmm(v["start"]))
                job = [j for j in r["jobs"] if j["ship"]["shipment_id"] == sid][0]
                du = (job["ship"]["expected_unload_min"] + C.DOCK_CHANGEOVER_MIN) / 60
                ax.barh(y, du, left=st, height=0.6, color=COL[prio[sid]], edgecolor="white")
                ax.text(st + du / 2, y, f"{sid[-4:]}", ha="center", va="center", fontsize=7, color="white", fontweight="bold")
    ax.set_yticks(range(len(docks)))
    ax.set_yticklabels([f"{d[-2:]} {lab}" for d, lab in zip(reversed(docks), ["Heavy", "Reefer", "Std 25t", "Std 20t", "Std 25t", "Std 20t"])], fontsize=8)
    ax.set_xlim(X0, X1)
    ax.set_xticks(range(10, 20))
    ax.set_xticklabels([f"{h:02d}:00" for h in range(10, 20)], fontsize=8)
    ax.grid(axis="x", color="#EAEDED", linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    un = ", ".join(k["unscheduled"]) or "none"
    ax.set_title(f"{policy}: priority-weighted wait {k['weighted_wait']}, CRITICAL wait {k['critical_wait']} min, no dock today: {un}",
                 fontsize=9, loc="left")
    leg = [Patch(color=COL[p], label=p) for p in COL] + [Patch(facecolor="#ECF0F1", edgecolor="#5D6D7E", label="Fixed (kept booking / in dock)"),
                                                     Patch(facecolor="#D5D8DC", hatch="///", edgecolor="#95A5A6", label="Breakdown / maintenance")]
    ax.legend(handles=leg, fontsize=6.5, ncol=6, loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, fname))
    plt.close(fig)


gantt("FCFS", "gantt_fcfs.png")
gantt("EXACT", "gantt_ours.png")
print("charts written")
