"""Statistical profile of the SetuHaul snapshot -> results.json (then build_report.py -> report.html).

    pip install pandas scipy numpy
    python analysis/analyze.py [path/to/db]      # default: data/setuhaul_freight_operations.db
    python analysis/build_report.py
"""
import json, sqlite3
import numpy as np, pandas as pd
from scipy import stats

import sys, os
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "setuhaul_freight_operations.db")
c = sqlite3.connect(DB)
q = lambda s: pd.read_sql(s, c)
R = {}
ts = lambda s: s if pd.api.types.is_datetime64_any_dtype(s) else pd.to_datetime(s.astype("string").str.slice(0, 19))
mins = lambda a, b: (a - b).dt.total_seconds() / 60

# ---------------------------------------------------------------- inventory
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite%' order by name")]
inv, nulls = [], []
for t in tables:
    df = q(f'select * from "{t}"')
    inv.append({"table": t, "rows": len(df), "columns": df.shape[1]})
    for col in df.columns:
        n = int(df[col].isna().sum())
        if n:
            nulls.append({"table": t, "column": col, "null_pct": round(100 * n / len(df), 1), "nulls": n, "rows": len(df)})
R["inventory"] = inv
R["nulls"] = sorted(nulls, key=lambda x: -x["null_pct"])

# ---------------------------------------------------------------- shipments
sh = q("select * from shipments")
for col in ["original_eta_ts", "latest_eta_ts", "planned_departure_ts", "actual_departure_ts"]:
    sh[col] = ts(sh[col])
R["shipments_by"] = {k: sh[k].value_counts().to_dict() for k in
                     ["destination_facility_id", "priority_code", "required_dock_type", "current_status", "product_category", "carrier_id"]}
R["weight"] = sh["load_weight_kg"].describe().round(0).to_dict()
R["unload_by_dock_type"] = sh.groupby("required_dock_type")["expected_unload_min"].agg(["count", "mean", "min", "max"]).round(1).reset_index().to_dict("records")
R["unload_weight_corr"] = dict(zip(["spearman_rho", "p"], [round(x, 3) for x in stats.spearmanr(sh.load_weight_kg, sh.expected_unload_min)]))

# weight headroom against the best compatible dock
docks = q("select * from docks")
def best_dock_limit(r):
    pool = docks[docks.facility_id == r.destination_facility_id]
    if r.temperature_control_required:
        pool = pool[pool.dock_type == "REEFER"]
    elif r.load_weight_kg > 25000 or r.required_dock_type == "HEAVY":
        pool = pool[pool.dock_type == "HEAVY"]
    else:
        pool = pool[pool.dock_type == "STANDARD"]
    return pool.max_vehicle_weight_kg.max(), pool.max_vehicle_weight_kg.min()
sh[["dock_max", "dock_min"]] = sh.apply(lambda r: pd.Series(best_dock_limit(r)), axis=1)
sh["headroom_pct_weakest"] = (100 * (sh.dock_min - sh.load_weight_kg) / sh.dock_min).round(1)
R["weight_headroom"] = sh[["shipment_id", "destination_facility_id", "required_dock_type", "load_weight_kg", "dock_min", "dock_max", "headroom_pct_weakest"]] \
    .sort_values("headroom_pct_weakest").to_dict("records")
R["n_loads_exceeding_weakest_compatible_dock"] = int((sh.load_weight_kg > sh.dock_min).sum())

# ---------------------------------------------------------------- ETA slip
le = q("select * from v_latest_eta")
le["orig"], le["eff"] = ts(le.original_eta_ts), ts(le.effective_eta_ts)
le["slip_min"] = mins(le.eff, le.orig)
le = le.merge(sh[["shipment_id", "priority_code", "carrier_id", "origin_city", "load_weight_kg", "current_status", "destination_facility_id"]], on="shipment_id")
upd = le[le.eta_source != "ORIGINAL_PLAN"]
R["eta_slip_all"] = le.slip_min.describe().round(1).to_dict()
R["eta_slip_updated"] = upd.slip_min.describe().round(1).to_dict()
R["eta_slip_rows"] = le[["shipment_id", "priority_code", "eta_source", "eta_confidence", "delay_reason_code", "slip_min"]].sort_values("slip_min", ascending=False).to_dict("records")
R["share_shipments_with_update"] = round(len(upd) / len(le), 3)
R["share_late_over_30"] = round(float((le.slip_min > 30).mean()), 3)
R["slip_by_reason"] = upd.groupby(upd.delay_reason_code.fillna("NONE"))["slip_min"].agg(["count", "mean", "max"]).round(1).reset_index().to_dict("records")
R["slip_by_priority"] = le.groupby("priority_code")["slip_min"].agg(["count", "mean", "median"]).round(1).reset_index().to_dict("records")
R["slip_by_carrier"] = le.groupby("carrier_id")["slip_min"].agg(["count", "mean", "median"]).round(1).reset_index().to_dict("records")
kw = stats.kruskal(*[g.slip_min.values for _, g in le.groupby("carrier_id") if len(g) > 1])
R["slip_carrier_kruskal"] = {"H": round(kw.statistic, 2), "p": round(kw.pvalue, 3)}
R["slip_weight_spearman"] = dict(zip(["rho", "p"], [round(x, 3) for x in stats.spearmanr(le.load_weight_kg, le.slip_min)]))
# bootstrap CI for mean slip among updated shipments
rng = np.random.default_rng(42)
bs = [rng.choice(upd.slip_min.values, len(upd), replace=True).mean() for _ in range(10000)]
R["slip_updated_mean_ci95"] = [round(float(np.percentile(bs, 2.5)), 1), round(float(np.percentile(bs, 97.5)), 1)]

# ETA revisions + driver accuracy
eu = q("select * from eta_updates")
eu["declared"], eu["created"] = ts(eu.declared_eta_ts), ts(eu.created_at)
eu["horizon_min"] = mins(eu.declared, eu.created)
R["eta_updates_by_confidence"] = eu.confidence_code.value_counts().to_dict()
R["eta_update_horizon"] = eu.horizon_min.describe().round(1).to_dict()
R["revisions_per_shipment"] = eu.groupby("shipment_id").size().value_counts().to_dict()
chk = q("select * from facility_checkins")
chk["gate_in"] = ts(chk.gate_in_ts)
lastdecl = eu.sort_values("created").groupby("shipment_id").last().reset_index()
acc = chk.merge(lastdecl[["shipment_id", "declared", "created", "confidence_code"]], on="shipment_id")
acc["error_min"] = mins(acc.gate_in, acc.declared)
acc["horizon_min"] = mins(acc.declared, acc.created)
R["driver_eta_accuracy"] = acc[["shipment_id", "confidence_code", "horizon_min", "error_min"]].to_dict("records")
R["driver_eta_mae"] = round(float(acc.error_min.abs().mean()), 2)
# label vs text consistency
R["confidence_label_issues"] = [{"shipment_id": "SHP1006", "note": "driver said 'around 11:20' but stored HIGH"}]

# ---------------------------------------------------------------- capacity
sl = q("select * from v_slot_availability")
sl["start"], sl["end"] = ts(sl.slot_start_ts), ts(sl.slot_end_ts)
sl["hour"] = sl.start.dt.hour
sl["len_min"] = mins(sl.end, sl.start)
R["slots_by_status"] = sl.groupby(["facility_id", "availability_status"]).size().unstack(fill_value=0).reset_index().to_dict("records")
R["slots_by_dock_type"] = sl.groupby(["facility_id", "dock_type"]).agg(slots=("slot_id", "count"), minutes=("len_min", "sum")).reset_index().to_dict("records")
jai = sl[sl.facility_id == "FAC-JAI-01"]
hourly = jai.groupby(["hour", "availability_status"]).size().unstack(fill_value=0)
for col in ["AVAILABLE", "OCCUPIED", "BLOCKED", "CLOSED"]:
    if col not in hourly:
        hourly[col] = 0
hourly["usable"] = hourly.AVAILABLE + hourly.OCCUPIED
hourly["utilisation"] = (hourly.OCCUPIED / hourly.usable.replace(0, np.nan)).round(2)
R["jaipur_hourly_slots"] = hourly.reset_index().fillna(0).to_dict("records")
R["jaipur_utilisation_overall"] = round(float(hourly.OCCUPIED.sum() / hourly.usable.sum()), 3)

# demand (effective arrival + 15 min to dock) vs supply of matching slot starts, Jaipur, by hour
open_ship = le[(le.destination_facility_id == "FAC-JAI-01") & (~le.current_status.isin(["CANCELLED", "COMPLETED"]))].copy()
open_ship["dock_hour"] = (open_ship.eff + pd.Timedelta(minutes=15)).dt.hour
dem = open_ship.groupby("dock_hour").size().rename("trucks_arriving")
sup = jai[jai.availability_status != "BLOCKED"].groupby("hour").size().rename("slot_starts")
dvs = pd.concat([dem, sup], axis=1).fillna(0).astype(int)
dvs["ratio"] = (dvs.trucks_arriving / dvs.slot_starts.replace(0, np.nan)).round(2)
R["jaipur_demand_vs_supply"] = dvs.reset_index().rename(columns={"index": "hour"}).fillna(0).to_dict("records")
# standard docks only, the contested pool
std = sl[(sl.facility_id == "FAC-JAI-01") & (sl.dock_type == "STANDARD")]
std_free_10_14 = int(((std.availability_status == "AVAILABLE") & (std.hour >= 10) & (std.hour < 14)).sum())
os_std = open_ship.merge(sh[["shipment_id", "required_dock_type"]], on="shipment_id")
os_std = os_std[os_std.required_dock_type == "STANDARD"]
std_need_10_14 = int(((os_std.dock_hour >= 10) & (os_std.dock_hour < 14)).sum())
std_usable = int(((std.availability_status.isin(["AVAILABLE", "OCCUPIED"])) & (std.hour >= 10) & (std.hour < 14)).sum())
std_need_11 = int((os_std.dock_hour == 11).sum()); std_slots_11 = int(((std.hour == 11) & std.availability_status.isin(["AVAILABLE", "OCCUPIED"])).sum())
R["standard_pool_10_14"] = {"standard_trucks_reaching_dock": std_need_10_14, "usable_standard_slot_starts": std_usable,
                            "still_available": std_free_10_14, "hour_11_trucks": std_need_11, "hour_11_slots": std_slots_11}
std_after14 = int(((std.hour >= 14) & (std.availability_status == "AVAILABLE")).sum())
R["standard_available_after_14"] = std_after14

# unload length vs slot length
ap = q("""select a.*, s.expected_unload_min, sl.slot_start_ts, sl.slot_end_ts from appointments a
          join shipments s using(shipment_id) join appointment_slots sl using(slot_id)""")
ap["slot_len"] = mins(ts(ap.slot_end_ts), ts(ap.slot_start_ts))
ap["spill_min"] = ap.expected_unload_min - ap.slot_len
R["appointments_by_status"] = ap.appointment_status.value_counts().to_dict()
R["unload_spill"] = {"bookings_spilling": int((ap.spill_min > 0).sum()), "of": len(ap),
                     "spill_minutes_mean_when_spilling": round(float(ap.spill_min[ap.spill_min > 0].mean()), 1)}

# ---------------------------------------------------------------- yard / unload
now = pd.Timestamp("2026-08-04T09:30:00")
chk["dock_in"], chk["u0"], chk["u1"] = ts(chk.dock_in_ts), ts(chk.unload_start_ts), ts(chk.unload_end_ts)
chk = chk.merge(sh[["shipment_id", "expected_unload_min"]], on="shipment_id")
chk["yard_wait_min"] = np.where(chk.dock_in.notna(), mins(chk.dock_in, chk.gate_in), mins(pd.Series([now] * len(chk)), chk.gate_in))
chk["unload_actual_min"] = mins(chk.u1, chk.u0)
chk["unload_running_at_0910"] = np.where(chk.u0.notna() & chk.u1.isna(), mins(pd.Series([pd.Timestamp("2026-08-04T09:10:00")] * len(chk)), chk.u0), np.nan)
R["checkins"] = chk[["shipment_id", "arrival_state", "queue_state", "yard_wait_min", "expected_unload_min", "unload_actual_min", "unload_running_at_0910"]].round(1).to_dict("records")

# ---------------------------------------------------------------- conversations
m = q("select * from chat_messages")
m["t"] = ts(m.message_ts)
th = q("select * from chat_threads")
ex = q("select * from driver_exceptions")
R["messages_by_sender"] = m.sender_type.value_counts().to_dict()
drv = m[m.sender_type == "DRIVER"]
R["driver_messages"] = len(drv)
R["driver_intents"] = drv.parsed_intent.fillna("NONE").value_counts().to_dict()
R["duplicate_rate"] = round(float(drv.is_duplicate.mean()), 3)
R["human_review_rate"] = round(float(drv.requires_human_review.mean()), 3)
R["eta_extracted_rate"] = round(float(drv.extracted_eta_ts.notna().mean()), 3)
R["devanagari_messages"] = int(drv.message_text.str.contains(r"[ऀ-ॿ]").sum())
# replies: time from driver message to next non-driver message in same thread
rt = []
for tid, g in m.sort_values("t").groupby("thread_id"):
    g = g.reset_index(drop=True)
    for i, r in g.iterrows():
        if r.sender_type == "DRIVER":
            nxt = g[(g.index > i) & (g.sender_type != "DRIVER")]
            if len(nxt):
                rt.append((nxt.iloc[0].t - r.t).total_seconds() / 60)
R["reply_latency_min"] = pd.Series(rt).describe().round(1).to_dict() if rt else {}
drv_t = drv.t.sort_values()
R["driver_msgs_by_30min"] = drv_t.dt.floor("30min").dt.strftime("%H:%M").value_counts().sort_index().to_dict()
spike = drv[(drv.t >= "2026-08-04 09:20") & (drv.t < "2026-08-04 10:00")]
R["spike_0920_1000"] = len(spike)
R["threads_by_intent"] = th.thread_intent.value_counts().to_dict()
R["threads_by_status"] = th.thread_status.value_counts().to_dict()
R["exceptions_by_type"] = ex.exception_type.value_counts().to_dict()
R["exceptions_by_status"] = ex.exception_status.value_counts().to_dict()
R["exceptions_by_severity"] = ex.severity_code.value_counts().to_dict()
R["exception_fields_filled"] = {k: round(float(ex[k].notna().mean()), 2) for k in ["shipment_id", "declared_eta_ts", "earliest_acceptable_ts", "latest_acceptable_ts", "reported_delay_min", "dedupe_key"]}
om = q("select * from operational_messages")
R["operational_messages"] = om.groupby(["channel", "delivery_status"]).size().reset_index(name="n").to_dict("records")

# ---------------------------------------------------------------- Monte Carlo
# ASSUMPTIONS (the snapshot is too small to fit distributions; documented, change and re-run):
#   arrival noise ~ Normal(0, sigma) by confidence: HIGH 10, MEDIUM 20, LOW 35 min
#   unload time ~ expected x LogNormal(mean 1, CV); CV 0.1 / 0.2 / 0.3
SIG = {"HIGH": 10, "MEDIUM": 20, "LOW": 35}
base = pd.Timestamp("2026-08-04T00:00:00")
tomin = lambda s: ((s - base).dt.total_seconds() / 60).values

# MC1: fragility of each still-reachable Jaipur booking (as booked in the snapshot)
book = q("""select a.shipment_id, sl.dock_id, sl.slot_start_ts, sl.slot_end_ts, s.expected_unload_min, s.priority_code
            from appointments a join appointment_slots sl using(slot_id) join shipments s using(shipment_id)
            where a.appointment_status in ('CONFIRMED','PENDING_CONFIRMATION') and a.is_current=1 and sl.facility_id='FAC-JAI-01'""")
book = book.merge(le[["shipment_id", "eff", "eta_confidence"]], on="shipment_id")
book = book.merge(chk[["shipment_id", "gate_in"]], on="shipment_id", how="left")
book["s0"], book["s1"] = ts(book.slot_start_ts), ts(book.slot_end_ts)
book["arr"] = book.gate_in.fillna(book.eff) + pd.Timedelta(minutes=15)
book["reachable"] = book.arr < book.s1
R["bookings_already_unreachable"] = book[~book.reachable].shipment_id.tolist()
B = book[book.reachable].sort_values(["dock_id", "s0"]).reset_index(drop=True)
def mc1(cv=0.2, n=20000, seed=11):
    rng = np.random.default_rng(seed)
    arr0, s0, s1 = tomin(B.arr), tomin(B.s0), tomin(B.s1)
    sig = np.where(B.gate_in.notna(), 0, B.eta_confidence.map(SIG).fillna(15).values)
    dur, dock = B.expected_unload_min.values, B.dock_id.values
    s2 = np.sqrt(np.log(1 + cv ** 2))
    miss = np.zeros(len(B)); wait = np.zeros(len(B)); late15 = np.zeros(len(B))
    for _ in range(n):
        arr = arr0 + rng.normal(0, 1, len(B)) * sig
        u = dur * rng.lognormal(-s2 ** 2 / 2, s2, len(B))
        free = {}
        for i in range(len(B)):
            st = max(s0[i], arr[i], free.get(dock[i], 0))
            miss[i] += st >= s1[i]
            late15[i] += st > s0[i] + 15
            wait[i] += max(0, st - arr[i])
            free[dock[i]] = st + u[i]
    out = B[["shipment_id", "dock_id", "slot_start_ts", "priority_code", "eta_confidence"]].copy()
    out["p_miss_window"] = (miss / n).round(3); out["p_start_15min_late"] = (late15 / n).round(3); out["exp_wait_min"] = (wait / n).round(1)
    out["slot_start_ts"] = out.slot_start_ts.str.slice(11, 16); out["dock_id"] = out.dock_id.str.replace("DOCK-JAI-", "")
    return out.sort_values("p_miss_window", ascending=False).to_dict("records")
R["mc1_booking_fragility"] = mc1()

# MC2: one dock, 8 back-to-back 60-min slots from 10:00, every truck on time on average (HIGH, sigma 10).
# How do mean unload and its variability propagate delay through the day?
def mc2(mean_unload, cv, n=20000, seed=5, k=8, sigma=10):
    rng = np.random.default_rng(seed)
    s2 = np.sqrt(np.log(1 + cv ** 2))
    s0 = 600 + 60 * np.arange(k)
    late_last = 0; misses = 0; mean_delay = np.zeros(k)
    for _ in range(n):
        arr = s0 - 15 + rng.normal(0, sigma, k)
        u = mean_unload * rng.lognormal(-s2 ** 2 / 2, s2, k)
        free = 0
        for i in range(k):
            st = max(s0[i], arr[i], free)
            mean_delay[i] += st - s0[i]
            misses += st >= s0[i] + 60
            free = st + u[i]
        late_last += (st - s0[-1]) > 15
    return {"mean_unload": mean_unload, "cv": cv, "utilisation": round(mean_unload / 60, 2),
            "p_8th_truck_15min_late": round(late_last / n, 3), "missed_windows_per_day": round(misses / n, 2),
            "delay_truck1": round(mean_delay[0] / n, 1), "delay_truck8": round(mean_delay[-1] / n, 1)}
R["mc2_cascade"] = [mc2(m_, cv) for m_ in (45, 55, 60, 70, 75) for cv in (0.1, 0.2, 0.3)]

# planning buffer per confidence for P(on dock by planned start) >= 80% / 90%
R["buffer_by_confidence"] = [{"confidence": k, "sigma_assumed": v, "buffer_p80": round(stats.norm.ppf(0.8) * v),
                              "buffer_p90": round(stats.norm.ppf(0.9) * v),
                              "current_rules_yaml": {"HIGH": 0, "MEDIUM": 15, "LOW": 30}[k]} for k, v in SIG.items()]

# unload model (planned unload vs weight, standard loads)
stdl = sh[sh.required_dock_type == "STANDARD"]
lr = stats.linregress(stdl.load_weight_kg / 1000, stdl.expected_unload_min)
pr = stdl.pallet_count.notna()
lr2 = stats.linregress(stdl[pr].pallet_count, stdl[pr].expected_unload_min)
R["unload_model_standard"] = {"intercept": round(lr.intercept, 1), "per_tonne": round(lr.slope, 2), "r2": round(lr.rvalue ** 2, 2),
                              "n": len(stdl), "pallets_r2": round(lr2.rvalue ** 2, 2)}
R["unload_scatter"] = stdl[["shipment_id", "load_weight_kg", "expected_unload_min", "pallet_count"]].to_dict("records")

import os as _os
json.dump(R, open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "results.json"), "w"), indent=1, default=str)
print(json.dumps({k: R[k] for k in ["eta_slip_updated", "slip_updated_mean_ci95", "slip_carrier_kruskal", "slip_weight_spearman", "driver_eta_mae", "jaipur_utilisation_overall", "standard_pool_10_14", "unload_spill", "spike_0920_1000", "human_review_rate", "reply_latency_min", "standard_available_after_14"]}, indent=1, default=str))
print(R["bookings_already_unreachable"]); print(pd.DataFrame(R["mc1_booking_fragility"]).to_string()); print(pd.DataFrame(R["mc2_cascade"]).to_string()); print(R["buffer_by_confidence"])
print(pd.DataFrame(R["jaipur_demand_vs_supply"]).to_string())
