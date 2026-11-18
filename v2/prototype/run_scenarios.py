"""Scripted scenario run for 4 August 2026, Jaipur DC.

Replays seed driver messages (with their seed IDs) plus a few new ones, interleaved
with world events from the snapshot, through the real workflow and database.
Two drivers choose the same slot in a true two-thread race.

Run:  python3 -I run_scenarios.py out/
Writes out/scenario_run.json (full trace) and out/scenario_metrics.json.
"""
import json, os, sys, threading
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from setuhaul import db, world as Wd, workflow as W, capacity as K, planner as P, timeline as T, config as C, escalation as E
from setuhaul.clock import ts, iso, hhmm, ampm, add, minutes_between
from setuhaul.eta import resolve

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
DBP = db.fresh_copy(os.path.join(OUT, "scenario_world.db"))
con = db.connect(DBP)
D = lambda hm: ts(f"2026-08-04T{hm}:00+05:30")
PH = {r["driver_id"]: r["phone"] for r in db.rows(con, "SELECT driver_id, phone FROM drivers")}
NAME = {r["driver_id"]: r["driver_name"] for r in db.rows(con, "SELECT driver_id, driver_name FROM drivers")}
LOG = []
QUEUE = []   # world events wait here and run in time order before each step


def advance(hm):
    QUEUE.sort(key=lambda q: q[0])
    while QUEUE and QUEUE[0][0] <= hm:
        t, title, fn = QUEUE.pop(0)
        if fn:
            fn()
        LOG.append(dict(t=t, kind="world", title=title))


def world(hm, title, fn=None):
    QUEUE.append((hm, title, fn))


def say(hm, drv, text, ext, case, seed=None):
    advance(hm)
    r = W.handle(con, PH[drv], text, D(hm), ext)
    LOG.append(dict(t=hm, kind="driver", who=drv, name=NAME[drv], text=text, ext=ext, case=case, seed=seed, **r))
    return r


def system(hm, title, detail, case=None):
    advance(hm)
    LOG.append(dict(t=hm, kind="system", title=title, detail=detail, case=case))


def notify(hm, n, case):
    LOG.append(dict(t=hm, kind="notify", who=n["driver_id"], name=NAME[n["driver_id"]], case=case, **n))


def offers(drv):
    return json.loads(db.one(con, "SELECT offers_json FROM conversations WHERE driver_id=?", drv)["offers_json"])


# ------------------------------------------------------------------ fixed world events from the snapshot
world("08:30", "ETA update from carrier: SHP1004 now 09:20 (seed ETA003)",
      lambda: Wd.eta_update(con, "ETA003", "SHP1004", "DRIVER_DECLARED", "DRV004", D("09:20"), "HIGH", "TRAFFIC", "Traffic at Tonk Road.", D("08:30")))
world("08:42", "SHP1001 finishes at D1; gate-out 08:50 (seed CHK1001)", lambda: Wd.unload_done(con, "SHP1001", D("08:42")))
world("08:50", "ETA update: SHP1006 now 10:50, medium confidence (seed ETA005)",
      lambda: Wd.eta_update(con, "ETA005", "SHP1006", "DRIVER_DECLARED", "DRV006", D("10:50"), "MEDIUM", "TRAFFIC", "Traffic after Shahpura.", D("08:50")))
world("09:05", "SHP1005 gates in for its 09:00 D3 slot (seed CHK1005)", lambda: Wd.gate_in(con, "SHP1005", D("09:05"), "LATE"))
world("09:15", "Dock D3 hydraulic leveller breaks down until 13:00 (seed DEVT001, logged 09:16)",
      lambda: Wd.dock_event(con, "DEVT001", "DOCK-JAI-D3", "BREAKDOWN", D("09:15"), D("13:00"), "Hydraulic dock leveller failure", D("09:16")))
world("09:25", "SHP1004 gates in, 25 minutes into its 09:00 D2 slot (seed CHK1004)", lambda: Wd.gate_in(con, "SHP1004", D("09:25"), "LATE"))
world("09:28", "SHP1002 finally finishes at D2 (ran 10 minutes past its 70-minute unload)", lambda: Wd.unload_done(con, "SHP1002", D("09:28")))
world("09:38", "Yard team calls SHP1004 to D2", lambda: Wd.dock_in(con, "SHP1004", "DOCK-JAI-D2", D("09:38")))
world("09:55", "SHP1017's driver phones dispatch: arrival between 12:30 and 13:00 (seed ETA012, low confidence)",
      lambda: Wd.eta_update(con, "ETA012", "SHP1017", "DRIVER_DECLARED", "DRV001", D("12:45"), "LOW", "TRAFFIC",
                            "Driver expects arrival between 12:30 and 13:00.", D("09:55"), window=(D("12:30"), D("13:00"))))
world("10:10", "Dispatch cancels SHP1019; its 16:00 D4 booking returns to the pool", lambda: K.cancel_shipment(con, "SHP1019", D("10:10"), "Shipment cancelled"))
world("12:20", "SHP1015 (reefer, from Delhi) departs 20 minutes late (seed actual departure)", lambda: Wd.depart(con, "SHP1015", D("12:20")))

# ------------------------------------------------------------------ the day
notes = Wd.rewind(con, D("08:20"))
LOG.append(dict(t="08:20", kind="system", title="World rewound to 08:20; later rows are replayed at their own time", detail=notes))

say("08:22", "DRV003", "I am already at the gate. Can I get a dock now instead of waiting for 9?", "wa-9007", "C01", "THR007/MSG012")
r = say("08:23", "DRV003", "1", "wa-9007-r1", "C01")
if r.get("booking"):
    o = r["booking"]["option"]
    world(hhmm(ts(o["start"])), f"SHP1003 docks at {o['dock_code']} (its new early booking)", lambda: Wd.dock_in(con, "SHP1003", o["dock_id"], ts(o["start"])))
    world(hhmm(add(ts(o["start"]), 45)), "SHP1003 finishes unloading; gate-out 8 minutes later",
          lambda: Wd.unload_done(con, "SHP1003", add(ts(o["start"]), 45)))

advance("09:16")
h = P.health(con, "FAC-JAI-01", D("09:16"))
system("09:16", "Booking health monitor (event-driven re-check after the D3 breakdown)", h, "C02")
wi = P.what_if(con, "SHP1015", D("09:16"))
system("09:16", "Proactive alert to coordinator: the reefer booking APT1015A (17:00) cannot work", {
    "why": "D5 maintenance from 18:00 was planned on 1 Aug at 14:00, after this booking (audit F07). Planned arrival 17:30 + 60-min unload cannot finish before 18:00.",
    "suggested": ["Ask the carrier to reach Jaipur by 16:45 so unloading runs 17:00-18:00"] + wi["recommendations"][:1]}, "C14")
plan = P.plan_facility(con, "FAC-JAI-01", D("09:16"))
system("09:16", "Scheduling tool proposal after the dock event (proposes only)", {
    "fixed": "trucks in docks, kept bookings, D3 breakdown 09:15-13:00",
    "proposal": {k: (v and {"dock": v["dock_code"], "start": hhmm(v["start"]), "wait_min": v["wait"]}) for k, v in plan["plan"].items()},
    "order": plan["order"], "score": plan["score"]}, "C02")
p5 = plan["plan"].get("SHP1005")
res5 = K.commit(con, "SHP1005", p5["slot_id"], D("09:17"), source="SCHEDULING_TOOL", actor="CON001 Neha Bansal (approved planner move)",
                approved=True, idem_key="console:CON001:SHP1005:0917")
system("09:17", "Coordinator approves the move for SHP1005 (in the yard; D3 broken; 20.5 t also exceeds D3's 20 t limit)",
       {"result": res5["code"], "appointment": res5.get("appointment_id"), "dock": res5["option"]["dock_code"],
        "start": hhmm(ts(res5["option"]["start"])), "released": res5.get("released")}, "C02")
o5 = res5["option"]
world(hhmm(ts(o5["start"])), f"SHP1005 docks at {o5['dock_code']}", lambda: Wd.dock_in(con, "SHP1005", o5["dock_id"], ts(o5["start"])))

say("09:19", "DRV013", "I am late by one hour.", "wa-9003", "C03", "THR003/MSG005")
say("09:27", "DRV004", "I reached late and am in the yard. Has a new dock been assigned?", "wa-9008", "C04", "THR008/MSG014")
say("09:29", "DRV012", "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.", "wa-9002", "C05", "THR002/MSG003")
say("09:30", "DRV012", "1", "wa-9002-r1", "C05")
say("09:31", "DRV004", "I will be late by 45 minutes.", "wa-9010", "C06", "THR010/MSG016")
say("09:32", "DRV004", "शाम वाला", "wa-9010-r1", "C06")
say("09:33", "DRV004", "1", "wa-9010-r2", "C06")
say("09:34", "DRV006", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-9001", "C07", "THR001/MSG001")
say("09:35", "DRV006", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-9009", "C07", "THR009/MSG015 (retry)")

oa, ob = offers("DRV013"), offers("DRV006")
common = [o for o in oa if o["slot_id"] in {x["slot_id"] for x in ob}]
slot = common[0]["slot_id"] if common else None
ia = [o["slot_id"] for o in oa].index(slot) + 1 if slot else 1
ib = [o["slot_id"] for o in ob].index(slot) + 1 if slot else 1
system("09:36", "Race: both drivers choose the same slot at the same instant (two threads, two database connections)",
       {"slot": slot, "DRV013_choice": ia, "DRV013_offer_shown_at": "09:19", "DRV006_choice": ib, "DRV006_offer_shown_at": "09:34",
        "same_slot_offered_to_both": bool(common)})
barrier, results = threading.Barrier(2), {}


def go(drv, idx, ext):
    c2 = db.connect(DBP)
    barrier.wait()
    results[drv] = W.handle(c2, PH[drv], str(idx), D("09:36"), ext)
    c2.close()


th = [threading.Thread(target=go, args=("DRV013", ia, "wa-9003-r1")), threading.Thread(target=go, args=("DRV006", ib, "wa-9001-r1"))]
[t.start() for t in th]
[t.join() for t in th]
for drv, case, ext, idx in (("DRV013", "C03", "wa-9003-r1", ia), ("DRV006", "C07", "wa-9001-r1", ib)):
    LOG.append(dict(t="09:36", kind="driver", who=drv, name=NAME[drv], text=str(idx), ext=ext, case=case, seed=None, race=True, **results[drv]))
loser = [d for d, r in results.items() if r["state"] == "OPTIONS_SHOWN"]
if loser:
    say("09:37", loser[0], "1", "wa-race-r2", "C03" if loser[0] == "DRV013" else "C07")

say("09:39", "DRV007", "Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?", "wa-9006", "C08", "THR006/MSG011")
say("09:40", "DRV007", "1", "wa-9006-r1", "C08")
hold = db.one(con, "SELECT * FROM slot_holds WHERE hold_status='PENDING_APPROVAL' ORDER BY created_at LIMIT 1")
if hold:
    r = K.decide(con, hold["hold_id"], True, "CON001 Neha Bansal", D("09:41"))
    system("09:41", f"Coordinator approves {hold['hold_id']} for {hold['shipment_id']}; the approval is re-checked before booking",
           {"result": r["code"], "appointment": r.get("appointment_id")}, "C06")
    notify("09:41", W.notify_decision(con, hold["driver_id"], r, D("09:41")), "C06")
say("09:42", "DRV010", "thoda late hoon bhai, traffic hai", "wa-9101", "C09")
say("09:43", "DRV010", "11:30 tak pahunch jaunga", "wa-9102", "C09")
say("09:44", "DRV014", "Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?", "wa-9004", "C10", "THR004/MSG007")
r14 = say("09:45", "DRV014", "1", "wa-9004-r1", "C10")
prop = r14.get("swap_proposal")
if prop:
    sw = K.swap(con, prop["high"], prop["low"], D("09:50"), "CON001 Neha Bansal (approved swap)", idem_key="console:swap:0950")
    system("09:50", f"Coordinator approves the swap proposal {prop['escalation_id']}: one transaction moves both bookings", {
        "explain": prop["explain"], "high_new": {"dock": sw["high"]["option"]["dock_code"], "start": hhmm(ts(sw["high"]["option"]["start"]))},
        "low_new": {"dock": sw["low"]["option"]["dock_code"], "start": hhmm(ts(sw["low"]["option"]["start"]))}}, "C10")
    for n in W.notify_swap(con, sw, D("09:50")):
        notify("09:50", n, "C10" if n["driver_id"] == "DRV014" else "C03")

advance("09:56")
h2 = [x for x in P.health(con, "FAC-JAI-01", D("09:56")) if x["shipment_id"] == "SHP1017"]
system("09:56", "Booking health monitor: SHP1017's 12:00 booking is at risk, so a proactive WhatsApp check-in goes out", {
    "status": h2, "message": W.R.t("proactive", "en", name="Rajesh", order="ORD-260804-017", facility="Jaipur DC", slot="12:00 PM")}, "C11")
say("10:02", "DRV001", "What slots are possible after 1 PM if I reach around 12:45?", "wa-9011", "C11", "THR011/MSG018")
say("10:03", "DRV001", "what about after 3 PM?", "wa-9011-r1", "C11")
say("10:04", "DRV001", "ok no thanks, I will keep my slot", "wa-9011-r2", "C11")
say("10:05", "DRV011", "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya", "wa-9105", "C12")
say("10:12", "DRV008", "Is my 4 PM slot still active?", "wa-9012", "C13", "THR012/MSG019")

util = db.one(con, f"""SELECT COUNT(*) AS open_slots,
    SUM(CASE WHEN EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id=sl.slot_id AND a.appointment_status IN ('CONFIRMED','IN_PROGRESS','COMPLETED','PENDING_CONFIRMATION')) THEN 1 ELSE 0 END) AS used
    FROM appointment_slots sl WHERE sl.facility_id='FAC-JAI-01' AND sl.slot_status='OPEN'
    AND substr(sl.slot_start_ts,12,5) >= '10:00' AND substr(sl.slot_start_ts,12,5) < '16:00'""")

say("16:24", "DRV015", "Evening traffic. ETA 6:30. Can the reefer unload tonight?", "wa-9005", "C14", "THR005/MSG009")

json.dump({"log": LOG, "utilisation_10_16": util}, open(os.path.join(OUT, "scenario_run.json"), "w"), indent=1, default=str)

# ------------------------------------------------------------------ metrics
cases = {}
for e in LOG:
    if e.get("case") and e["kind"] in ("driver", "notify"):
        cases.setdefault(e["case"], []).append(e)
OUTCOME = {"BOOKED", "ESCALATED", "CLOSED"}
rows_ = []
for cid, ev in sorted(cases.items()):
    first = ev[0]["t"]
    outcome_t, outcome = None, None
    for e in ev:
        if e.get("state") in OUTCOME and e.get("reply") is not None:
            outcome_t, outcome = e["t"], e["state"]
            if e["state"] == "BOOKED" or e["state"] in ("ESCALATED", "CLOSED"):
                break
    tools = [t for e in ev for t in (e.get("tools") or [])]
    msgs = [e for e in ev if e["kind"] == "driver"]
    human = any(t.startswith(("escalate:NO_FEASIBLE", "escalate:SAFETY", "hold_for_approval", "escalate:HUMAN", "escalate:CLARIFICATION")) for t in tools)
    rows_.append({"case": cid, "driver": ev[0]["who"], "shipment": next((e.get("shipment_id") for e in reversed(ev) if e.get("shipment_id")), None),
                  "driver_messages": len(msgs), "duplicates_suppressed": sum(1 for t in tools if t.startswith("dedupe:")),
                  "clarifications": sum(1 for t in tools if t.startswith("ask:")),
                  "first_msg": first, "outcome_at": outcome_t, "minutes_to_outcome": minutes_between(D(first), D(outcome_t)) if outcome_t else None,
                  "outcome": outcome, "human_involved": human, "option_gone": sum(1 for t in tools if t.startswith("book:OPTION_GONE")),
                  "bookings": sum(1 for t in tools if t.startswith("book:CONFIRMED")) + sum(1 for t in tools if t.startswith("notify_driver:CONFIRMED"))})

inv = {
    "slots_with_two_active_bookings": db.one(con, "SELECT COUNT(*) AS n FROM (SELECT slot_id FROM appointments WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*)>1)")["n"],
    "shipments_with_two_current_bookings": db.one(con, "SELECT COUNT(*) AS n FROM (SELECT shipment_id FROM appointments WHERE is_current=1 AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY shipment_id HAVING COUNT(*)>1)")["n"],
    "overlapping_service_bookings": db.one(con, """SELECT COUNT(*) AS n FROM dock_reservations a JOIN dock_reservations b ON a.dock_id=b.dock_id AND a.reservation_id<b.reservation_id
            AND a.live=1 AND b.live=1 AND a.kind='BOOKING' AND b.kind IN ('BOOKING','HOLD')
            AND julianday(a.start_ts)<julianday(b.end_ts) AND julianday(b.start_ts)<julianday(a.end_ts)""")["n"],
    "service_bookings_breaking_compatibility": db.one(con, """SELECT COUNT(*) AS n FROM appointments a JOIN shipments s USING(shipment_id)
            JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
            WHERE a.appointment_id LIKE 'APT-N%' AND (s.load_weight_kg>d.max_vehicle_weight_kg OR (s.required_dock_type IN ('REEFER','HEAVY') AND s.required_dock_type<>d.dock_type))""")["n"],
}
picks = [t for e in LOG for t in (e.get("tools") or []) if t.startswith(("book:", "hold_for_approval"))]
waits = []
for e in LOG:
    b = e.get("booking")
    if b and b.get("ok"):
        sid = e.get("shipment_id")
        er = db.one(con, "SELECT declared_eta_ts FROM eta_updates WHERE shipment_id=? AND created_at<=? ORDER BY created_at DESC LIMIT 1", sid, iso(D(e["t"])))
        chk = db.one(con, "SELECT gate_in_ts FROM facility_checkins WHERE shipment_id=? AND gate_in_ts<=?", sid, iso(D(e["t"])))
        arrival = ts(chk["gate_in_ts"]) if chk else (ts(er["declared_eta_ts"]) if er else None)
        pref = db.one(con, "SELECT MAX(limit_ts) AS t FROM driver_constraints WHERE shipment_id=? AND constraint_type='EARLIEST_START' AND created_at<=?", sid, iso(D(e["t"])))
        if arrival and pref and pref["t"] and ts(pref["t"]) > arrival:
            arrival = ts(pref["t"])          # the driver asked not to start before this time
        if arrival:
            waits.append({"shipment": sid, "arrival": hhmm(arrival), "dock_start": hhmm(ts(b["option"]["start"])),
                          "wait_min": max(0, minutes_between(arrival, ts(b["option"]["start"])))})
esc = db.rows(con, "SELECT escalation_id, reason_code, urgency, routed_to, channel, handoff_json FROM escalations")
need = ["driver_said", "eta", "options_tried", "recommendations", "driver_told", "routed_to", "respond_by"]
nos = [x for x in esc if x["reason_code"] == "NO_FEASIBLE_SLOT"]
complete = sum(1 for x in nos if all(k in json.loads(x["handoff_json"]) for k in need))
metrics = {
    "cases": rows_,
    "summary": {
        "driver_cases": len(rows_),
        "median_minutes_to_outcome": sorted(r["minutes_to_outcome"] for r in rows_ if r["minutes_to_outcome"] is not None)[len([r for r in rows_ if r["minutes_to_outcome"] is not None]) // 2],
        "resolved_without_manual_takeover": sum(1 for r in rows_ if not r["human_involved"] and r["outcome"]),
        "conflicting_or_duplicate_allocations": sum(inv.values()),
        "options_gone_at_commit": f"{sum(1 for p in picks if 'OPTION_GONE' in p)} of {len(picks)} picks",
        "no_slot_escalations_complete": f"{complete} of {len(nos)}",
        "avg_wait_after_reschedule_min": round(sum(w['wait_min'] for w in waits) / len(waits), 1) if waits else None,
        "slot_utilisation_10_16": f"{util['used']} of {util['open_slots']} open slots",
        "clarification_questions": sum(r["clarifications"] for r in rows_),
        "median_driver_messages": sorted(r["driver_messages"] for r in rows_)[len(rows_) // 2],
        "duplicates_suppressed": sum(r["duplicates_suppressed"] for r in rows_),
    },
    "invariants": inv, "waits": waits,
    "escalations": [{k: x[k] for k in ("escalation_id", "reason_code", "urgency", "routed_to", "channel")} for x in esc],
}
json.dump(metrics, open(os.path.join(OUT, "scenario_metrics.json"), "w"), indent=1, default=str)

for e in LOG:
    if e["kind"] == "driver":
        print(f"\n[{e['t']}] {e['case']} {e['who']} ({e.get('lang')}): {e['text']}")
        print("   tools:", e.get("tools"))
        print("   reply:", (e.get("reply") or "<no reply>").replace("\n", "\n          "))
        print("   state:", e.get("state"))
    elif e["kind"] == "notify":
        print(f"\n[{e['t']}] NOTIFY {e['who']}: {e['reply']}")
    else:
        print(f"\n[{e['t']}] {e['kind'].upper()}: {e['title']}")
        if e.get("detail"):
            print("   ", json.dumps(e["detail"], default=str)[:900])
print("\nSUMMARY", json.dumps(metrics["summary"], indent=1))
print("INVARIANTS", inv)
