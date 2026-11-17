"""Scripted scenario run (hand-in #4) on the 4 August 2026 Jaipur snapshot.

Covers every item the brief asks the scenario run to show:
 1 delay + clarification questions      (DRV004 ambiguous shipment, DRV010 vague ETA)
 2 driver asks for later options, compares (DRV001 ask-only, two rounds)
 3 several requests on one facility      (09:34-09:50 disruption spike)
 4 two requests compete for capacity     (DRV006 vs DRV014 on D2 12:30; D4 12:30 protected for DRV012)
 5 driver sees an option change / vanish (DRV006 start moves 12:45 -> 12:30; DRV014 "taken by another truck")
 6 escalation, no safe automatic outcome (DRV015 reefer; DRV011 safety)
 7 scheduling tool after ETA update / gate check-in (before vs after spike)

    python -m scenarios.run_scenarios
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from setuhaul.capacity import decide_pending  # noqa: E402
from setuhaul.db import connect, prepare_working_copy, write_txn  # noqa: E402
from setuhaul.obs import Telemetry  # noqa: E402
from setuhaul.scheduler import compare, plan_table  # noqa: E402
from setuhaul.timeutil import diff_min, hhmm, iso, parse  # noqa: E402
from setuhaul.workflow import DriverAgent  # noqa: E402

T = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731

SCRIPT = [
    # time, driver, thread, text, external id, note (what the run is meant to show; checked against the reply)
    ("08:22", "DRV003", "S-EARLY", "Bhai main gate pe pahunch gaya, abhi dock mil sakta hai kya? 9 baje ka slot hai", "wa-s01",
     "Early arrival (Hinglish), truck already through the gate. Idle capacity on D4 is offered only because no scheduled truck is displaced."),
    ("08:23", "DRV003", "S-EARLY", "1", "wa-s02",
     "Driver accepts; start is re-checked at commit (08:28, one minute later than shown). The old 09:00-10:00 slot is released."),
    ("@SCHED", "09:30", "Scheduler BEFORE the spike"),
    ("09:34", "DRV006", "S-A", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-r9001",
     "Spike starts. ETA ('around' = MEDIUM, planned at the late end 11:35) and 'after 12' constraint in one message. The current 10:00 slot is unreachable and the driver is told. Look-ahead withholds D4 12:30, the only slot that fits another truck's deadline."),
    ("09:35", "DRV006", "S-A", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-r9009",
     "WhatsApp re-delivers with a NEW external id: caught by content dedupe. No reply, no second exception."),
    ("09:37", "DRV012", "S-B", "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.",
     "wa-r9002", "Gate-out deadline (13:30) read and stated back, no extra question. Only D4 12:30 finishes in time, which is why it was kept out of other drivers' lists."),
    ("09:39", "DRV013", "S-C", "I am late by one hour.", "wa-r9003",
     "Relative delay anchored to the plan (10:00 + 60 = 11:00) and the assumption is stated so the driver can correct it. The pending D2 11:00 slot still works: no churn."),
    ("09:41", "DRV004", "S-D", "मैं 45 मिनट लेट हो जाऊँगा", "wa-r9010",
     "Hindi. Driver has two trips: the system asks which, and shows that trip 004 is already at the DC (gate-in 09:25)."),
    ("09:42", "DRV004", "S-D", "शाम वाला", "wa-s03",
     "'The evening one' resolves trip 020; delay anchored to its plan (18:00 + 45). Its 18:00-19:00 slot is now unreachable, so new options are shown."),
    ("09:43", "DRV004", "S-D", "1", "wa-s03b",
     "Booking goes to PENDING, not confirmed: the same truck is still in the yard on trip 004 (data conflict), so a coordinator checks it."),
    ("09:44", "DRV014", "S-E", "भाई 11:25 तक पहुँचूंगा, लोड बहुत ज़रूरी है, पहला डॉक दे दो", "wa-r9004",
     "CRITICAL shipment, Hindi. 'Give me the first dock' is logged as a priority claim, not obeyed; priority comes from the shipment record. Options hold nothing."),
    ("09:45", "DRV006", "S-A", "1", "wa-s04",
     "Competing request: DRV006 confirms D2 first. Commit re-checks the dock: it frees earlier (DRV013's update), so the start moves 12:45 -> 12:30 and the driver is told."),
    ("09:46", "DRV014", "S-E", "1", "wa-s05",
     "The option DRV014 picked (D2 12:30) is gone: re-check at commit fails safely, the driver is told and gets fresh options. D4 12:30 stays protected for DRV012."),
    ("09:47", "DRV014", "S-E", "1", "wa-s06",
     "Books the look-ahead's first option (D2 13:15). The 10:00 scheduler run then proposes a swap to bring this CRITICAL load forward: a coordinator decision."),
    ("09:48", "DRV012", "S-B", "option 1", "wa-s07",
     "D4 12:30 is still free because the look-ahead kept it out of other lists. Confirmed; finishes before the 13:30 gate-out."),
    ("@APPROVE", "09:49", "Coordinator reviews pending requests"),
    ("09:50", "DRV007", "S-F", "Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?", "wa-r9006",
     "Driver is on two in-transit trucks (a data conflict). 'Heavy trailer' matches only trip 016, so it is chosen and the assumption is stated. Heavy load -> D6 only."),
    ("09:52", "DRV007", "S-F", "1", "wa-s10", "Data conflict -> PENDING for a coordinator, never auto-confirmed."),
    ("09:53", "DRV010", "S-G", "thoda late ho jaunga", "wa-s11", "Vague delay (Hinglish): ask for a clock time before calling any tool."),
    ("09:54", "DRV010", "S-G", "lagbhag 11:30 tak", "wa-s12",
     "'11:30 tak' answers the ETA question (elsewhere 'tak' means a deadline). Reefer -> D5 only; current 11:00 slot unreachable."),
    ("09:55", "DRV010", "S-G", "1", "wa-s12b", "Books the reefer dock D5 12:00."),
    ("@APPROVE", "09:56", "Coordinator approves pending bookings"),
    ("@CHECKIN", "09:58", "SHP1007 gate check-in"),
    ("@SCHED", "10:00", "Scheduler AFTER the spike + check-in"),
    ("10:02", "DRV001", "S-H", "What slots are possible after 1 PM if I reach around 12:45?", "wa-r9011",
     "Ask-only: options shown, nothing reserved."),
    ("10:04", "DRV001", "S-H", "aur 3 baje ke baad?", "wa-s13", "Compares later options (Hinglish). Still nothing reserved."),
    ("10:05", "DRV001", "S-H", "nahi, abhi rehne do", "wa-s14", "Declines: nothing booked, no hold left behind."),
    ("10:12", "DRV008", "S-I", "Is my 4 PM slot still active?", "wa-r9012", "Shipment and booking were cancelled: say so, do not offer slots."),
    ("10:30", "DRV011", "S-J", "Truck ka accident ho gaya, helper ko chot lagi hai", "wa-s15",
     "Safety words -> shift manager now (5-minute SLA). No scheduling talk."),
    ("16:24", "DRV015", "S-K", "Evening traffic. ETA 6:30. Can the reefer unload tonight?", "wa-r9005",
     "Reefer dock D5 is under maintenance 18:00-22:00 and nothing new starts after 21:00: no safe automatic outcome -> escalation with facts, options tried and a recommendation."),
]


SEED_CANCELLED: set[str] = set()      # cancellations already in the supplied snapshot


def run(out_dir: Path = ROOT / "scenarios"):
    db = prepare_working_copy(Path(tempfile.mkdtemp()) / "scenario.db")
    conn = connect(db)
    tel = Telemetry(echo=False)
    agent = DriverAgent(conn, tel)
    SEED_CANCELLED.clear()
    SEED_CANCELLED.update(r[0] for r in conn.execute(
        "SELECT appointment_id FROM appointments WHERE appointment_status='CANCELLED'"))
    log, sched_runs, threads = [], [], {}
    for step in SCRIPT:
        if step[0] == "@SCHED":
            now = T(step[1])
            res = compare(conn, "FAC-JAI-01", now)
            sched_runs.append({"at": step[1], "label": step[2],
                               "policies": [{"policy": r.policy, "metrics": r.metrics} for r in res],
                               "recommended_plan": plan_table(res[-1])})
            log.append({"kind": "system", "time": step[1], "text": step[2]})
            continue
        if step[0] == "@APPROVE":
            now = T(step[1])
            rows = conn.execute("SELECT appointment_id, shipment_id FROM appointments WHERE "
                                "appointment_status='PENDING_CONFIRMATION' AND booking_source='DRIVER_CHAT'").fetchall()
            for r in rows:
                res = decide_pending(conn, r["appointment_id"], True, "CON001", now)
                txt = (f"Coordinator CON001 approved pending booking for {r['shipment_id']}" if res["status"] == "CONFIRMED"
                       else f"Coordinator tried to approve {r['shipment_id']}'s pending request; system refused: "
                            f"{', '.join(res['reasons'])}")
                log.append({"kind": "system", "time": step[1], "text": txt})
            continue
        if step[0] == "@CHECKIN":
            with write_txn(conn):
                conn.execute("INSERT INTO facility_checkins(checkin_id, shipment_id, facility_id, gate_in_ts, "
                             "yard_queue_enter_ts, arrival_state, queue_state, queue_position, updated_at) VALUES "
                             "('CHK-S1','SHP1007','FAC-JAI-01',?,?,'LATE','WAITING_LATE',4,?)",
                             (iso(T(step[1])), iso(T(step[1])), iso(T(step[1]))))
                conn.execute("UPDATE shipments SET current_status='WAITING' WHERE shipment_id='SHP1007'")
            log.append({"kind": "system", "time": step[1], "text": "Gate check-in: SHP1007 arrived (late) and is in the yard"})
            continue
        hm, drv, thr, text, ext, note = step
        t = agent.handle(drv, text, T(hm), thr, ext)
        threads.setdefault(thr, []).append(t)
        log.append({"kind": "turn", "time": hm, "driver": drv, "thread": thr, "text": text, "note": note,
                    "lang": t.lang, "intent": t.intent, "reply": t.reply, "state": t.state, "actions": t.actions,
                    "options": t.options, "committed": t.committed, "duplicate": t.duplicate,
                    "clarification": t.clarification, "guard": t.guard_violations,
                    "escalation": t.escalation, "parse": {k: v for k, v in (t.parse or {}).items() if v not in
                                                          (None, [], False)}})

    # final booking status per conversation (after coordinator decisions), for the replay's outcome chips
    final = {}
    for r in conn.execute("SELECT thread_id, data_json FROM agent_conversations"):
        sid = json.loads(r["data_json"]).get("shipment_id")
        a = conn.execute("""SELECT appointment_status FROM appointments WHERE shipment_id=? AND is_current=1
                            ORDER BY updated_at DESC LIMIT 1""", (sid,)).fetchone() if sid else None
        final[r["thread_id"]] = a["appointment_status"] if a else None
    for e in log:
        if e["kind"] == "turn":
            e["thread_final"] = final.get(e["thread"])
    metrics = measures(conn, threads, log)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript.json").write_text(json.dumps(log, indent=2, ensure_ascii=False, default=str))
    (out_dir / "scheduler_runs.json").write_text(json.dumps(sched_runs, indent=2, default=str))
    (out_dir / "scenario_metrics.json").write_text(json.dumps(metrics, indent=2))
    (out_dir / "telemetry_sample.jsonl").write_text(
        "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in tel.records[:60]))
    (out_dir / "telemetry_counters.json").write_text(json.dumps(tel.counters(), indent=2))
    (out_dir / "dock_board.json").write_text(json.dumps(dock_board(conn), indent=2))
    (out_dir / "transcript.md").write_text(to_md(log, sched_runs, metrics))
    return log, sched_runs, metrics


def measures(conn, threads, log) -> dict:
    cases = {k: v for k, v in threads.items()}
    resolved_auto = escalated = 0
    turns, minutes_to_outcome, clar = [], [], 0
    for thr, ts in cases.items():
        real = [t for t in ts if not t.duplicate]
        clar += sum(t.clarification for t in real)
        final = real[-1].state
        if final in ("CONFIRMED", "PENDING_WAREHOUSE", "CLOSED") or (final == "OPTIONS_SHOWN" and thr == "S-H") or \
                (final == "NEED_ETA" and thr == "S-H"):
            resolved_auto += 1
        if any(t.escalation for t in real):
            escalated += 1
    first = {}
    for e in log:
        if e["kind"] == "turn":
            first.setdefault(e["thread"], e["time"])
            if e["committed"] or e["escalation"] or e["state"] in ("CLOSED",) or \
                    (e["state"] == "CONFIRMED" and "options" in e["actions"]):
                if e["thread"] not in [x[0] for x in minutes_to_outcome]:
                    minutes_to_outcome.append((e["thread"], diff_min(T(e["time"]), T(first[e["thread"]]))))
                    turns.append((e["thread"], sum(1 for x in log if x["kind"] == "turn" and x["thread"] == e["thread"]
                                                   and x["time"] <= e["time"] and not x["duplicate"])))
    double = conn.execute("""SELECT COUNT(*) FROM (SELECT slot_id, COUNT(*) n FROM appointments WHERE appointment_status
                             IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING n>1)""").fetchone()[0]
    gone = sum(1 for e in log if e["kind"] == "turn" and any(a.startswith("book_failed") for a in e["actions"]))
    shown = sum(len(e["options"]) for e in log if e["kind"] == "turn")
    esc = conn.execute("SELECT reason_code, handoff_json FROM escalations").fetchall()
    noslot = [json.loads(r["handoff_json"]) for r in esc if r["reason_code"] == "NO_FEASIBLE_SLOT"]
    complete = sum(all(h.get(k) for k in ("facts", "options_tried", "recommendation", "routed_to")) for h in noslot)
    waits = []
    for e in log:
        if e["kind"] == "turn" and e["committed"]:
            sid = conn.execute("SELECT shipment_id FROM appointments WHERE appointment_id=?",
                               (e["committed"]["appointment_id"],)).fetchone()[0]
            from setuhaul.eta import resolve_eta
            eta = resolve_eta(conn, sid)
            arr = eta.point
            waits.append(max(0, diff_min(parse(e["committed"]["planned_start"]), arr)))
    open_slots = conn.execute("""SELECT COUNT(*) FROM appointment_slots WHERE facility_id='FAC-JAI-01' AND slot_status='OPEN'
                                 AND slot_start_ts >= '2026-08-04T10:00' AND slot_start_ts < '2026-08-04T16:00'""").fetchone()[0]
    used = conn.execute("""SELECT COUNT(DISTINCT a.slot_id) FROM appointments a JOIN appointment_slots s USING(slot_id)
                           WHERE s.facility_id='FAC-JAI-01' AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
                           AND s.slot_start_ts >= '2026-08-04T10:00' AND s.slot_start_ts < '2026-08-04T16:00'""").fetchone()[0]
    # A reachable booking may only end because its own driver rebooked, or because its truck can
    # no longer reach it (certain miss). Anything else is a promise broken by someone else's booking.
    displaced = sum(1 for r in conn.execute(
        "SELECT appointment_id, cancellation_reason FROM appointments WHERE appointment_status='CANCELLED'")
        if r["appointment_id"] not in SEED_CANCELLED and r["cancellation_reason"] not in
        ("Replaced after driver ETA change", "Released: truck ETA beyond window (certain miss)"))
    n_cases = len(cases)
    return {
        "cases (conversations)": n_cases,
        "time from first message to usable outcome (median min)": sorted(m for _, m in minutes_to_outcome)[len(minutes_to_outcome) // 2] if minutes_to_outcome else None,
        "turns to outcome (median driver messages)": sorted(t for _, t in turns)[len(turns) // 2] if turns else None,
        "share resolved without manual takeover": f"{resolved_auto}/{n_cases}",
        "conflicting or duplicate allocations": double,
        "options later found infeasible at commit": f"{gone} of {shown} shown",
        "no-slot escalations with complete hand-off record": f"{complete}/{len(noslot)}",
        "avg driver wait after rescheduling (min, arrival->dock)": round(sum(waits) / len(waits), 1) if waits else None,
        "warehouse slot utilisation 10:00-16:00 (Jaipur)": f"{used}/{open_slots} open slots booked",
        "promises displaced by another driver's booking": displaced,
        "driver clarification questions": f"{clar} across {n_cases} cases",
        "duplicate messages suppressed": sum(1 for e in log if e["kind"] == "turn" and e["duplicate"]),
    }


def dock_board(conn) -> dict:
    """Final dock board for the prototype viewer."""
    docks = [dict(r) for r in conn.execute("SELECT dock_id, dock_code, dock_type, max_vehicle_weight_kg FROM docks "
                                            "WHERE facility_id='FAC-JAI-01' ORDER BY dock_code")]
    slots = [dict(r) for r in conn.execute("""
        SELECT sl.slot_id, sl.dock_id, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status, sl.block_reason,
               a.shipment_id, a.appointment_status, a.booking_source, s.priority_code
        FROM appointment_slots sl
        LEFT JOIN appointments a ON a.slot_id=sl.slot_id AND a.appointment_status IN
             ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','COMPLETED')
        LEFT JOIN shipments s ON s.shipment_id=a.shipment_id
        WHERE sl.facility_id='FAC-JAI-01' ORDER BY sl.dock_id, sl.slot_start_ts""")]
    events = [dict(r) for r in conn.execute("SELECT * FROM dock_status_events")]
    return {"docks": docks, "slots": slots, "events": events}


def to_md(log, sched, metrics) -> str:
    L = ["# Scenario run — Jaipur DC, 4 August 2026", "",
         "Generated by `python -m scenarios.run_scenarios`. Every reply below came from the deterministic workflow.", ""]
    for e in log:
        if e["kind"] == "system":
            L += [f"**[{e['time']}] SYSTEM:** {e['text']}", ""]
            continue
        L += [f"**[{e['time']}] {e['driver']} ({e['thread']}, {e['lang']}):** {e['text']}  ",
              f"_{e['note']}_", ""]
        if e["duplicate"]:
            L += ["> (duplicate suppressed — no reply, no second exception)", ""]
        else:
            L += ["> " + (e["reply"] or "").replace("\n", "  \n> "), "",
                  f"`state={e['state']} intent={e['intent']} actions={e['actions']}`" +
                  (f" `escalation={e['escalation']['reason']}→{e['escalation']['routed_to']['role']}`" if e["escalation"] else ""), ""]
    L += ["## Scheduling tool: policy comparison", ""]
    for s in sched:
        L += [f"### {s['label']} ({s['at']})", "", "| Policy | Scheduled | Avg wait | Weighted wait | Critical wait | Missed | Changes |",
              "|---|---:|---:|---:|---:|---:|---:|"]
        for p in s["policies"]:
            m = p["metrics"]
            L.append(f"| {p['policy']} | {m['scheduled']}/{m['trucks']} | {m['avg_wait_min']} | {m['weighted_wait']} | "
                     f"{m['critical_wait_min']} | {m['missed_appointments']} | {m['schedule_changes']} |")
        sw = next((p["metrics"].get("swap_proposals") for p in s["policies"] if p["metrics"].get("swap_proposals")), None)
        if sw:
            L += ["", "Swap proposals for coordinator approval:"] + [f"- {x}" for x in sw]
        L.append("")
    L += ["## Measures", "", "| Measure | Value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in metrics.items()]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    _, _, m = run()
    print(json.dumps(m, indent=2))
