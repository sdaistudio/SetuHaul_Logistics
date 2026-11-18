"""Demo 2 (MVP 2): coordinator control tower -> site/demo2.html

* Simulated GPS (data/gps_sim.yaml) along real corridors, OpenStreetMap basemap.
* Planned vs driver-declared vs GPS ETA for every truck, at several moments of the day.
* Green / amber / red status from rules.yaml `control_tower` (deterministic).
* One-click WhatsApp (wa.me) with a predefined, editable message per situation.
* Summary dashboard of every driver <-> system conversation from the scenario run.

    python tools/build_demo2.py        (called by tools/build_site.py)
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import warnings
from datetime import timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.simplefilter("ignore")

from setuhaul.db import connect, prepare_working_copy  # noqa: E402
from setuhaul.eta import EtaEstimate  # noqa: E402
from setuhaul.feasibility import Constraints, evaluate_all, rank_options  # noqa: E402
from setuhaul.solver import rank_for_driver  # noqa: E402
from setuhaul.location import toll_watchdog  # noqa: E402
from setuhaul.rules import R  # noqa: E402
from setuhaul.timeutil import hhmm, parse  # noqa: E402

TIMES = ["07:30", "08:30", "09:30", "09:50", "10:30", "11:30", "13:00", "16:30"]
DEFAULT_T = "09:50"
T = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731
SIM = yaml.safe_load((ROOT / "data" / "gps_sim.yaml").read_text())
CT = R()["control_tower"]
GATE = R()["timing"]["gate_to_dock_min"]
BUF = R()["timing"]["eta_buffer_min"]


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h)) * 1.2  # road factor


def route(origin_city, fac):
    pts = [SIM["places"][origin_city]]
    for w in SIM["routes"].get(origin_city, []):
        for p in SIM["corridors"].get(w, [w]):
            pts.append(SIM["places"][p])
    pts.append(SIM["dc"][fac])
    seg = [hav(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return pts, seg


def point_at(pts, seg, frac):
    total = sum(seg)
    d = frac * total
    for i, s in enumerate(seg):
        if d <= s or i == len(seg) - 1:
            f = 0 if s == 0 else min(1, d / s)
            return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f], total - d
        d -= s
    return pts[-1], 0


def slot_options(conn, sh, t, phase, gps_eta, b_eta, conf, bk, ready, timing):
    """Feasible slots this truck can take now (deterministic feasibility R1-R8, same as the chat).
    Suggestions follow the look-ahead order (best for the whole facility), then earliest start;
    the coordinator may pick any feasible slot and any start inside its window."""
    sid = sh["shipment_id"]
    if phase == "IN_YARD":
        eta = EtaEstimate(point=t, confidence="HIGH", source="gate check-in", at_facility=True)
    elif gps_eta:
        eta = EtaEstimate(point=gps_eta, confidence="HIGH", source="GPS")
    else:
        eta = EtaEstimate(point=b_eta, confidence=conf, source="driver/plan")
    evals, _ = evaluate_all(conn, sid, t, Constraints(), eta)
    feas = sorted((e for e in evals if e.feasible), key=lambda e: (e.planned_start, e.dock_code))
    cur = bk["slot_id"] if bk else None
    if timing == "EARLY":                       # only slots that start before the current booking
        feas = [e for e in feas if e.slot_id != cur and e.planned_start < parse(bk["slot_start_ts"])]
    elif cur:
        feas = [e for e in feas if e.slot_id != cur]
    order = feas
    try:
        ranked = rank_for_driver(conn, sid, t, eta, Constraints(), [e.slot_id for e in feas])
        by = {e.slot_id: e for e in feas}
        least = min((len(r.others_unscheduled) for r in ranked), default=0)
        good = [by[r.slot_id] for r in ranked if len(r.others_unscheduled) == least]
        strand = {r.slot_id for r in ranked if len(r.others_unscheduled) > least}
        order = good + [e for e in feas if e not in good]
    except Exception:  # noqa: BLE001
        strand = set()
    sugg, seen = [], set()
    for e in order:
        k = hhmm(e.planned_start)
        if k in seen or e.slot_id in strand:
            continue
        seen.add(k)
        sugg.append(e)
        if len(sugg) >= CT["slot_suggestions"]:
            break
    def item(e, tag=""):
        return {"slot_id": e.slot_id, "dock": e.dock_code, "window": f"{hhmm(e.slot_start)}-{hhmm(e.slot_end)}",
                "start": hhmm(e.planned_start), "end": hhmm(e.planned_end), "latest": hhmm(e.slot_end - timedelta(minutes=5)),
                "approval": e.needs_approval, "strands": e.slot_id in strand, "tag": tag}
    out = {"eta": hhmm(eta.point), "eta_source": eta.source, "dock_ready": hhmm(ready),
           "suggest": [item(e, "recommended" if i == 0 else "") for i, e in enumerate(sugg)],
           "all": [item(e) for e in sorted(feas, key=lambda e: (e.planned_start, e.dock_code))[: CT["slot_choices_max"]]],
           "keep": None}
    if bk and timing in ("EARLY", "LATE", "YARD_WAIT"):
        me = next((e for e in evals if e.slot_id == cur), None)
        if me and me.feasible:
            out["keep"] = item(me, "keep current")
    return out


def main():
    conn = connect(prepare_working_copy(Path(tempfile.mkdtemp()) / "d2.db"))
    q = lambda s, *a: [dict(r) for r in conn.execute(s, a)]  # noqa: E731
    ships = q("""select s.*, d.driver_name, d.phone from shipments s join drivers d using(driver_id)
                 order by s.shipment_id""")
    chk = {r["shipment_id"]: r for r in q("select * from facility_checkins")}
    etas = q("select * from eta_updates order by created_at")
    appts = q("""select a.*, sl.slot_start_ts, sl.slot_end_ts, dk.dock_code from appointments a
                 join appointment_slots sl using(slot_id) join docks dk on dk.dock_id = sl.dock_id""")
    final_eta = {r["shipment_id"]: r for r in q("select * from v_latest_eta")}
    events = q("select * from dock_status_events")
    routes = {s["shipment_id"]: route(s["origin_city"], s["destination_facility_id"]) for s in ships}

    def believed_eta(sid, t):
        ups = [e for e in etas if e["shipment_id"] == sid and parse(e["created_at"]) <= t]
        if ups:
            u = ups[-1]
            return parse(u["declared_eta_ts"]), u["confidence_code"], "driver", u["created_at"]
        s = next(x for x in ships if x["shipment_id"] == sid)
        return parse(s["original_eta_ts"]), "MEDIUM", "plan", None

    def booking_at(sid, t):
        live = []
        for a in appts:
            if a["shipment_id"] != sid or parse(a["booked_at"]) > t:
                continue
            ended = a["cancelled_at"] or (a["updated_at"] if a["appointment_status"] in ("NO_SHOW", "REJECTED") else None)
            if a["appointment_status"] in ("CANCELLED", "NO_SHOW", "REJECTED") and ended and parse(ended) <= t:
                continue
            live.append(a)
        return max(live, key=lambda a: a["booked_at"]) if live else None

    snaps = {}
    for hm in TIMES:
        t = T(hm)
        watch = {w.shipment_id: w for w in toll_watchdog(t)}
        rows = []
        for s in ships:
            sid = s["shipment_id"]
            pts, seg = routes[sid]
            c = chk.get(sid, {})
            g = lambda k: parse(c[k]) if c.get(k) else None  # noqa: E731
            dep = parse(s["actual_departure_ts"]) if s["actual_departure_ts"] else None
            plan_eta = parse(s["original_eta_ts"])
            b_eta, conf, src, _ = believed_eta(sid, t)
            fe = final_eta[sid]
            truth = g("gate_in_ts") or (parse(fe["effective_eta_ts"]) + timedelta(minutes=SIM["truth_offset_min"].get(sid, 0)))
            silent_since = parse(SIM["gps_silent"][sid]) if sid in SIM.get("gps_silent", {}) else None
            # phase
            if s["current_status"] == "CANCELLED":
                phase = "CANCELLED"
            elif g("gate_out_ts") and g("gate_out_ts") <= t:
                phase = "DEPARTED_DC"
            elif g("unload_end_ts") and g("unload_end_ts") <= t:
                phase = "UNLOADED"
            elif g("dock_in_ts") and g("dock_in_ts") <= t:
                phase = "UNLOADING"
            elif g("gate_in_ts") and g("gate_in_ts") <= t:
                phase = "IN_YARD"
            elif dep and dep <= t:
                phase = "IN_TRANSIT"
            else:
                phase = "NOT_DEPARTED"
            # position + GPS ETA
            pos, rem_km, gps_eta, gps_silent_min = pts[0], sum(seg), None, 0
            if phase == "IN_TRANSIT":
                t_fix = t
                if silent_since and silent_since < t:
                    t_fix = silent_since
                    gps_silent_min = int((t - silent_since).total_seconds() // 60)
                span = (truth - dep).total_seconds()
                frac = max(0.0, min(0.995, (t_fix - dep).total_seconds() / span)) if span > 0 else 0.99
                pos, rem_km = point_at(pts, seg, frac)
                if not gps_silent_min:
                    gps_eta = truth
            elif phase in ("IN_YARD", "UNLOADING", "UNLOADED", "DEPARTED_DC"):
                pos, rem_km = pts[-1], 0
            bk = booking_at(sid, t)
            # dock-ready estimate
            if phase == "IN_YARD":
                ready = t
            elif phase == "NOT_DEPARTED":
                ready = b_eta + timedelta(minutes=GATE)
            elif gps_eta:
                ready = gps_eta + timedelta(minutes=GATE)
            else:
                ready = b_eta + timedelta(minutes=GATE + BUF.get(conf, 15))
            reasons, status = [], "GREY"
            tol = CT["on_time_tolerance_min"]
            w = watch.get(sid)
            slip = int((max(gps_eta or b_eta, b_eta) - plan_eta).total_seconds() // 60)
            next_best = None
            if phase in ("CANCELLED", "DEPARTED_DC", "UNLOADED"):
                status = "GREY"
                reasons.append({"CANCELLED": "shipment cancelled", "DEPARTED_DC": "completed and left the DC",
                                "UNLOADED": "unloaded"}[phase])
            elif phase == "NOT_DEPARTED":
                bk0 = booking_at(sid, t)
                late_dep = parse(s["planned_departure_ts"]) < t
                status = "AMBER" if late_dep else ("GREEN" if bk0 else "GREY")
                reasons.append("planned departure " + hhmm(parse(s["planned_departure_ts"])) + (" passed, truck not moving" if late_dep else " (not yet due)"))
                if not bk0:
                    reasons.append("no booking yet")
            elif phase == "UNLOADING":
                status = "BLUE"
                started = g("unload_start_ts") or g("dock_in_ts")
                mins = int((t - started).total_seconds() // 60)
                reasons.append(f"unloading for {mins} min of {s['expected_unload_min']} planned")
                if mins > s["expected_unload_min"]:
                    status = "AMBER"
                    reasons.append("overrunning: next truck on this dock will be late")
            else:
                ss = parse(bk["slot_start_ts"]) if bk else None
                se = parse(bk["slot_end_ts"]) if bk else None
                yard_wait = int((t - g("gate_in_ts")).total_seconds() // 60) if phase == "IN_YARD" else 0
                dock_down = bool(bk and any(e["dock_id"].endswith(bk["dock_code"]) and parse(e["event_start_ts"]) <= t
                                            and (not e["event_end_ts"] or parse(e["event_end_ts"]) > t) and e["event_type"] != "CAPACITY_REDUCTION"
                                            for e in events))
                feas = None
                if not bk or (se and ready >= se):
                    evals, _ = evaluate_all(conn, sid, t, Constraints())
                    feas = rank_options(evals, 1)
                    next_best = f"{feas[0].dock_code} {hhmm(feas[0].planned_start)}" if feas else None
                if bk and ready >= se and phase != "IN_YARD":
                    status = "RED"; reasons.append(f"cannot reach booked {bk['dock_code']} {hhmm(ss)}-{hhmm(se)} (dock-ready ~{hhmm(ready)})")
                elif not bk and phase != "NOT_DEPARTED" and not feas:
                    status = "RED"; reasons.append("no booking and no feasible slot left today")
                elif w and w.action == "OPS_REVIEW":
                    status = "RED"; reasons.append(f"GPS silent {gps_silent_min} min and {w.next_toll} toll overdue {w.overdue_min} min")
                elif phase == "IN_YARD" and (yard_wait > CT["yard_wait_red_min"] or dock_down):
                    status = "RED"; reasons.append("assigned dock unavailable" if dock_down else f"waiting in yard {yard_wait} min")
                elif bk and ready > ss + timedelta(minutes=tol) and phase != "IN_YARD":
                    status = "AMBER"; reasons.append(f"late for {bk['dock_code']} {hhmm(ss)}: dock-ready ~{hhmm(ready)}")
                elif not bk and feas:
                    status = "AMBER" if phase != "NOT_DEPARTED" else "GREY"
                    reasons.append(f"needs a booking; best slot now {next_best}" if phase != "NOT_DEPARTED" else "not departed yet; no booking yet")
                elif bk and bk["appointment_status"] == "PENDING_CONFIRMATION":
                    status = "AMBER"; reasons.append("booking waiting for warehouse approval")
                elif conf == "LOW" or gps_silent_min:
                    status = "AMBER"; reasons.append("ETA uncertain" + (f" (GPS silent {gps_silent_min} min)" if gps_silent_min else " (LOW confidence)"))
                elif phase == "NOT_DEPARTED" and parse(s["planned_departure_ts"]) < t:
                    status = "AMBER"; reasons.append("planned departure passed, truck not moving")
                elif phase == "IN_YARD" and yard_wait > CT["yard_wait_amber_min"]:
                    status = "AMBER"; reasons.append(f"waiting in yard {yard_wait} min")
                elif bk:
                    status = "GREEN"; reasons.append(f"on time for {bk['dock_code']} {hhmm(ss)}" + (" (in yard)" if phase == "IN_YARD" else ""))
                elif phase == "NOT_DEPARTED":
                    status = "GREY"; reasons.append("not departed yet")
                if s["priority_code"] in ("CRITICAL", "HIGH") and slip > CT["critical_slip_red_min"] and status == "AMBER":
                    status = "RED"; reasons.append(f"{s['priority_code']} load slipping {slip} min vs plan")
            # ---- possible slots for trucks running late or early (or with no booking)
            timing, slots = None, None
            if phase in ("IN_TRANSIT", "IN_YARD") and status != "GREY":
                ss0 = parse(bk["slot_start_ts"]) if bk else None
                se0 = parse(bk["slot_end_ts"]) if bk else None
                early = timedelta(minutes=CT["early_min"])
                if not bk:
                    timing = "NO_BOOKING"
                elif phase == "IN_YARD":
                    if ss0 - t >= early:
                        timing = "EARLY"                       # at the gate well before its slot
                    elif t >= se0 or status == "RED":
                        timing = "YARD_WAIT"                   # window passed while waiting / dock down
                elif ss0 - ready >= early:
                    timing = "EARLY"
                elif ready > ss0 + timedelta(minutes=CT["on_time_tolerance_min"]):
                    timing = "LATE"
                if timing:
                    slots = slot_options(conn, s, t, phase, gps_eta, b_eta, conf, bk, ready, timing)
                    # one answer everywhere: the "best slot now" hint is the recommended possible slot
                    rec = slots["suggest"][0] if slots["suggest"] else None
                    old_best, next_best = next_best, (f"{rec['dock']} {rec['start']}" if rec else None)
                    if old_best and next_best:
                        reasons = [x.replace(f"best slot now {old_best}", f"best slot now {next_best}") for x in reasons]
            rows.append({
                "timing": timing, "slots": slots,
                "sid": sid, "driver": s["driver_name"], "driver_id": s["driver_id"], "phone": s["phone"],
                "priority": s["priority_code"], "origin": s["origin_city"], "dest": s["destination_facility_id"],
                "phase": phase, "status": status, "reasons": reasons, "pos": [round(pos[0], 5), round(pos[1], 5)],
                "remaining_km": round(rem_km), "route": [[round(a, 4), round(b, 4)] for a, b in pts],
                "planned_eta": hhmm(plan_eta), "declared_eta": hhmm(b_eta) if src == "driver" else None, "eta_conf": conf,
                "gps_eta": hhmm(gps_eta) if gps_eta else None, "gps_silent_min": gps_silent_min,
                "slip_min": slip, "dock_ready": hhmm(ready) if phase not in ("UNLOADING", "UNLOADED", "DEPARTED_DC", "CANCELLED") else None,
                "booking": f"{bk['dock_code']} {hhmm(parse(bk['slot_start_ts']))}-{hhmm(parse(bk['slot_end_ts']))}" if bk else None,
                "booking_status": bk["appointment_status"] if bk else None, "next_best": next_best,
                "unload_min": s["expected_unload_min"], "weight_t": round(s["load_weight_kg"] / 1000, 1),
            })
        snaps[hm] = rows

    # ---------------- conversation summary from the scenario run
    log = json.loads((ROOT / "scenarios" / "transcript.json").read_text())
    turns = [e for e in log if e["kind"] == "turn"]
    threads = {}
    for e in turns:
        th = threads.setdefault(e["thread"], {"thread": e["thread"], "driver": e["driver"], "first": e["time"], "turns": [],
                                              "langs": set(), "clar": 0, "dups": 0, "esc": None, "commit": None})
        th["turns"].append(e)
        th["last"] = e["time"]
        th["langs"].add(e["lang"])
        th["clar"] += int(bool(e.get("clarification")))
        th["dups"] += int(bool(e.get("duplicate")))
        if e.get("escalation"):
            th["esc"] = {"reason": e["escalation"]["reason"], "role": e["escalation"]["routed_to"]["role"],
                         "name": e["escalation"]["routed_to"]["name"], "rec": e["escalation"].get("recommendation", "")}
        if e.get("committed"):
            th["commit"] = {"status": e["committed"]["status"], "dock": e["committed"]["dock"],
                            "start": e["committed"]["planned_start"][11:16]}
    drivers = {s["driver_id"]: s["driver_name"] for s in ships}
    sys_events = [e for e in log if e["kind"] == "system"]
    convo = []
    for th in threads.values():
        last = [x for x in th["turns"] if not x["duplicate"]][-1]
        outcome = ("Escalated" if th["esc"] else "Booked" if th["commit"] and th["commit"]["status"] == "CONFIRMED"
                   else "Pending approval" if th["commit"] or last["state"] == "PENDING_WAREHOUSE"
                   else "Closed" if last["state"] == "CLOSED" else "Current booking kept" if last["state"] == "CONFIRMED"
                   else "Options shown, no booking")
        sid = (last.get("committed") or {}).get("slot_id")
        convo.append({"thread": th["thread"], "driver_id": th["driver"], "driver": drivers.get(th["driver"], th["driver"]),
                      "first": th["first"], "last": th["last"], "turns": len(th["turns"]), "langs": sorted(th["langs"]),
                      "clar": th["clar"], "dups": th["dups"], "outcome": outcome, "esc": th["esc"], "commit": th["commit"],
                      "messages": [{"t": x["time"], "text": x["text"], "reply": x["reply"], "dup": x["duplicate"],
                                    "intent": x["intent"], "state": x["state"], "lang": x["lang"]} for x in th["turns"]]})
    convo.sort(key=lambda c: c["first"])
    metrics = json.loads((ROOT / "scenarios" / "scenario_metrics.json").read_text())

    templates = {
        "RED": {"en": "Hi {name}, this is SetuHaul Jaipur DC. Your truck ({sid}) cannot make its {booking} slot at the current ETA. {next}Please reply with your exact arrival time or call the coordinator. Do not come to the dock until we confirm a new slot.",
                "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC से। आपकी गाड़ी ({sid}) मौजूदा समय पर {booking} स्लॉट नहीं पकड़ पाएगी। {next}कृपया सही पहुँचने का समय बताएं या कोऑर्डिनेटर को कॉल करें। नया स्लॉट पक्का होने तक डॉक पर न आएं।"},
        "AMBER": {"en": "Hi {name}, SetuHaul Jaipur DC here. We see {sid} running about {slip} min behind plan. Are you still on track for {booking}? Reply with your ETA so we can hold or move your slot.",
                  "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC से। {sid} योजना से लगभग {slip} मिनट पीछे दिख रही है। क्या आप {booking} के लिए समय पर हैं? अपना ETA भेजें ताकि हम स्लॉट रख सकें या बदल सकें।"},
        "GREEN": {"en": "Hi {name}, SetuHaul Jaipur DC. You are on time for {booking}. Please report at the gate 15 minutes before. Drive safe.",
                  "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। आप {booking} के लिए समय पर हैं। कृपया 15 मिनट पहले गेट पर पहुँचें। सुरक्षित चलाएं।"},
        "PROPOSE_LATE": {"en": "Hi {name}, SetuHaul Jaipur DC. With your truck ({sid}) reaching the dock around {ready}, your {booking} slot will not work, so we have booked dock {dock} for you at about {start} (slot {window}) and released the old slot. Please be at the gate by {gate}. If you cannot make it, reply with your time or call the coordinator.",
                         "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। आपकी गाड़ी ({sid}) लगभग {ready} तक डॉक पर पहुँचेगी, इसलिए {booking} स्लॉट नहीं चलेगा। इसलिए हमने आपके लिए डॉक {dock} लगभग {start} बजे (स्लॉट {window}) बुक कर दिया है और पुराना स्लॉट छोड़ दिया है। कृपया {gate} तक गेट पर पहुँचें। न पहुँच सकें तो अपना समय भेजें या कोऑर्डिनेटर को कॉल करें।"},
        "PROPOSE_EARLY": {"en": "Hi {name}, SetuHaul Jaipur DC. You are early for your {booking} slot. A dock is free earlier, so we have moved you to dock {dock} at about {start} (slot {window}). Please be at the gate by {gate}. If you cannot make it, reply with your time and we will find you the best slot.",
                          "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। आप अपने {booking} स्लॉट से पहले पहुँच रहे हैं। पहले एक डॉक खाली है, इसलिए हमने आपको डॉक {dock}, लगभग {start} बजे (स्लॉट {window}) पर कर दिया है। कृपया {gate} तक गेट पर पहुँचें। न पहुँच सकें तो अपना समय भेजें, हम सबसे अच्छा स्लॉट देंगे।"},
        "PROPOSE_EARLY_YARD": {"en": "Hi {name}, SetuHaul Jaipur DC. You are in the yard ahead of your {booking} slot. A dock is free earlier, so we have booked dock {dock} for you at about {start}. Please stay in the yard; we will call you to the dock.",
                               "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। आप अपने {booking} स्लॉट से पहले यार्ड में हैं। पहले एक डॉक खाली है, इसलिए हमने लगभग {start} बजे डॉक {dock} बुक कर दिया है। कृपया यार्ड में रुकें, हम डॉक पर बुलाएँगे।"},
        "PROPOSE_YARD": {"en": "Hi {name}, SetuHaul Jaipur DC. Sorry for the wait in the yard. We have booked dock {dock} for you at about {start} (slot {window}). Please stay in the yard; we will call you to the dock.",
                         "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। यार्ड में इंतज़ार के लिए माफ़ी। अब आपका डॉक {dock}, लगभग {start} बजे (स्लॉट {window}) तय है। कृपया यार्ड में रुकें, हम डॉक पर बुलाएँगे।"},
        "PROPOSE_NEW": {"en": "Hi {name}, SetuHaul Jaipur DC. Your truck ({sid}) had no dock booking, so we have booked dock {dock} for you at about {start} (slot {window}). Please be at the gate by {gate}. If you cannot make it, reply with your time or call the coordinator.",
                        "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। आपकी गाड़ी ({sid}) की कोई डॉक बुकिंग नहीं थी, इसलिए हमने डॉक {dock} लगभग {start} बजे (स्लॉट {window}) बुक कर दिया है। कृपया {gate} तक गेट पर पहुँचें। न पहुँच सकें तो अपना समय भेजें या कोऑर्डिनेटर को कॉल करें।"},
        "SILENT": {"en": "Hi {name}, SetuHaul Jaipur DC. We have not received your truck's location for {mins} minutes. Are you okay? Please share your live location on WhatsApp or reply with where you are.",
                   "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। {mins} मिनट से आपकी गाड़ी की लोकेशन नहीं मिली। सब ठीक है? कृपया WhatsApp पर लाइव लोकेशन भेजें या बताएं आप कहाँ हैं।"},
        "YARD": {"en": "Hi {name}, SetuHaul Jaipur DC. Thank you for waiting in the yard. Your dock is being arranged; we will call you to the dock shortly.",
                 "hi": "नमस्ते {name}, सेतुहॉल जयपुर DC। यार्ड में इंतज़ार के लिए धन्यवाद। आपका डॉक तय किया जा रहा है, जल्द बुलाया जाएगा।"},
    }
    data = {"times": TIMES, "default": DEFAULT_T, "snaps": snaps, "dc": SIM["dc"], "convo": convo, "metrics": metrics,
            "sys": sys_events, "templates": templates, "rules": CT["rules"]}
    tpl = (ROOT / "tools" / "demo2_template.html").read_text(encoding="utf-8")
    out = ROOT / "site" / "demo2.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(tpl.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")), encoding="utf-8")
    cnt = {}
    for r in snaps[DEFAULT_T]:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    print("demo2 written", out, "at", DEFAULT_T, cnt)
    for r in snaps[DEFAULT_T]:
        print(f"  {r['sid']} {r['driver'][:14]:14s} {r['phase']:12s} {r['status']:5s} plan {r['planned_eta']} decl {r['declared_eta']} gps {r['gps_eta']} ready {r['dock_ready']} bk {r['booking']} | {'; '.join(r['reasons'])}")


if __name__ == "__main__":
    main()
