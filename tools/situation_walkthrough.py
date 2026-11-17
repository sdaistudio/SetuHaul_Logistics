"""Run the brief's ladder of situations through the real workflow and record,
for every driver message, what the MODEL did, which TOOLS ran (read/write),
and which DATABASE tables were read and written.

    python tools/situation_walkthrough.py   ->  docs/walkthrough/situations.json
                                                docs/08_situation_walkthrough.md
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

from setuhaul import database as D, tool as T  # noqa: E402
from setuhaul.capacity import CapacityError  # noqa: E402
from setuhaul.obs import Telemetry  # noqa: E402
from setuhaul.timeutil import parse  # noqa: E402
from setuhaul.workflow import DriverAgent  # noqa: E402

at = lambda hm: parse(f"2026-08-04T{hm}:00+05:30")  # noqa: E731

# level, title, where-to-look ids, what it proves, evidence queries, script
LADDER = [
    (1, "A driver only asks about options", "THR011, SHP1017", "Asking is separated from booking: only READ tools run; no hold, no appointment.",
     [("thread", "THR011"), ("thread_messages", "THR011"), ("shipment", "SHP1017"), ("current_appointment", "SHP1017")],
     [("10:02", "DRV001", "L1", "What slots are possible after 1 PM if I reach around 12:45?", "wa-l1a"),
      ("10:04", "DRV001", "L1", "aur 3 baje ke baad?", "wa-l1b"),
      ("10:05", "DRV001", "L1", "nahi, abhi rehne do", "wa-l1c")]),
    (2, "The message misses key facts", "THR003, SHP1013, THR010, DRV004", "The workflow asks before it calls a capacity tool (which shipment? what time?), and states any assumption it makes.",
     [("thread_messages", "THR003"), ("thread_messages", "THR010"), ("driver_open_shipments", "DRV004"), ("eta_updates", "SHP1013")],
     [("09:39", "DRV013", "L2a", "I am late by one hour.", "wa-l2a"),
      ("09:41", "DRV004", "L2b", "मैं 45 मिनट लेट हो जाऊँगा", "wa-l2b"),
      ("09:42", "DRV004", "L2b", "शाम वाला", "wa-l2c"),
      ("09:53", "DRV010", "L2c", "thoda late ho jaunga", "wa-l2d"),
      ("09:54", "DRV010", "L2c", "lagbhag 11:30 tak", "wa-l2e")]),
    (3, "Rules and compatibility narrow the choices", "SHP1010, SHP1015, SHP1016, RULE005", "Feasibility is more than an empty row: dock type, reefer to D5, weight limit, maintenance and the 21:00 rule each remove slots, with a reason code.",
     [("facility_rules", "FAC-JAI-01"), ("shipment", "SHP1016"), ("shipment", "SHP1010"), ("dock_events", "FAC-JAI-01")],
     [("09:50", "DRV007", "L3a", "Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?", "wa-l3a"),
      ("09:51", "DRV007", "L3a", "016 wala", "wa-l3b"),
      ("09:54", "DRV010", "L3b", "lagbhag 11:30 tak reach karunga, reefer hai", "wa-l3c")]),
    (4, "Several delayed trucks chase nearby slots", "SHP1006, SHP1012, SHP1013, SHP1014, SHP1009", "One allocation policy for everyone: hard rules, then look-ahead cost over all trucks (priority weights from rules.yaml), never 'who shouts loudest'.",
     [("eta_updates", "SHP1006"), ("eta_updates", "SHP1012"), ("eta_updates", "SHP1014"), ("current_appointment", "SHP1009")],
     [("09:34", "DRV006", "L4a", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-l4a"),
      ("09:37", "DRV012", "L4b", "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.", "wa-l4b"),
      ("09:39", "DRV013", "L4c", "I am late by one hour.", "wa-l4c"),
      ("09:44", "DRV014", "L4d", "भाई 11:25 तक पहुँचूंगा, लोड बहुत ज़रूरी है, पहला डॉक दे दो", "wa-l4d"),
      ("09:46", "DRV014", "L4d", "1", "wa-l4e")]),
    (5, "The world changes mid-conversation", "SHP1005, APT1008, THR001, THR009, SHP1002", "Re-delivered messages are processed once, a slot taken by another driver is re-checked at commit and fresh options are offered, and a retried confirm replays its first result.",
     [("thread_messages", "THR009"), ("appointment", "APT1008"), ("checkin", "SHP1005"), ("checkin", "SHP1002")],
     [("09:34", "DRV006", "L5a", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-r9001"),
      ("09:35", "DRV006", "L5a", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "wa-r9009"),
      ("09:37", "DRV012", "L5b", "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.", "wa-l5b"),
      ("09:46", "@TAKE", "L5b", "SHP1013", "parallel conversation books the slot DRV012 was just shown"),
      ("09:48", "DRV012", "L5b", "option 1", "wa-l5d")]),
    (6, "No feasible slot exists", "SHP1015, THR005, OM004", "The workflow escalates to a named person with facts, options tried and a recommendation, instead of inventing an answer.",
     [("thread_messages", "THR005"), ("ops_messages", "SHP1015"), ("contacts", "FAC-JAI-01"), ("dock_events", "FAC-JAI-01")],
     [("16:24", "DRV015", "L6", "Evening traffic. ETA 6:30. Can the reefer unload tonight?", "wa-l6")]),
]


def run_level(level):
    n, title, where, proves, evidence, script = level
    conn = D.open_working_copy(Path(tempfile.mkdtemp()) / f"l{n}.db")
    agent = DriverAgent(conn, Telemetry(echo=False))
    ev = []
    for name, arg in evidence:
        rows = D.q(conn, name, arg)
        ev.append({"query": name, "arg": arg, "purpose": D.Q[name][0], "sql": D.Q[name][1], "rows": len(rows),
                   "sample": [{k: v for k, v in r.items() if v is not None} for r in rows[:3]]})
    turns = []
    for hm, drv, thr, text, ext in script:
        if drv == "@TAKE":   # the world changes: another booking lands on the slot shown to this driver
            st = json.loads(conn.execute("SELECT data_json FROM agent_conversations WHERE thread_id=?",
                                         (f"WALK-{thr}",)).fetchone()[0])
            slot = st["shown"][0]
            with T.capture() as calls, D.capture_sql(conn) as sql:
                h = T.hold_slot(conn, text, slot["slot_id"], at(hm))
                r = T.confirm_booking(conn, h["hold_id"], at(hm), f"walk-take-{thr}")
            turns.append({"time": hm, "driver": "OTHER", "thread": thr, "text": f"[{ext}] {text} takes {slot['dock']} {slot['start']}",
                          "lang": "-", "intent": "-", "state": r["status"], "reply": None, "duplicate": False,
                          "clarification": False, "escalation": None, "committed": r, "model": [],
                          "tools": [c.__dict__ for c in calls], "tables_read": sql.tables()["read"],
                          "tables_written": sql.tables()["write"], "sql_statements": len(sql.statements)})
            continue
        with T.capture() as calls, D.capture_sql(conn) as sql:
            t = agent.handle(drv, text, at(hm), thread_id=f"WALK-{thr}", external_id=ext)
        tabs = sql.tables()
        turns.append({"time": hm, "driver": drv, "thread": thr, "text": text, "lang": t.lang, "intent": t.intent,
                      "state": t.state, "reply": t.reply, "duplicate": t.duplicate, "clarification": t.clarification,
                      "escalation": (t.escalation or {}).get("reason"), "committed": t.committed,
                      "model": [c.__dict__ for c in calls if c.layer == "MODEL"],
                      "tools": [c.__dict__ for c in calls if c.layer == "TOOL"],
                      "tables_read": tabs["read"], "tables_written": tabs["write"], "sql_statements": len(sql.statements)})
    extra = {}
    if n == 5:   # retry of a confirm with the same idempotency key returns the first result
        from setuhaul.feasibility import Constraints, rank_options
        evals, eta = T.check_slot_feasibility(conn, "SHP1013", at("09:56"), Constraints())
        opt = rank_options(evals, 1)[0]
        with T.capture() as calls, D.capture_sql(conn) as sql:
            h = T.hold_slot(conn, "SHP1013", opt.slot_id, at("09:56"))
            r1 = T.confirm_booking(conn, h["hold_id"], at("09:56"), "walk-idem-1")
            r2 = T.confirm_booking(conn, h["hold_id"], at("09:56"), "walk-idem-1")
        extra["idempotent_retry"] = {"first": r1["appointment_id"], "second": r2["appointment_id"],
                                     "replay": bool(r2.get("idempotent_replay")),
                                     "tools": [c.__dict__ for c in calls], "tables_written": sql.tables()["write"]}
        # two different trucks race for the same slot through the tool layer
        a = {e.slot_id for e in T.check_slot_feasibility(conn, "SHP1017", at("09:57"), Constraints())[0] if e.feasible}
        b = [e.slot_id for e in T.check_slot_feasibility(conn, "SHP1018", at("09:57"), Constraints())[0]
             if e.feasible and e.slot_id in a]
        if b:
            results = {}
            for sid in ("SHP1017", "SHP1018"):
                try:
                    results[sid] = T.hold_slot(conn, sid, b[0], at("09:57"))["hold_id"]
                except CapacityError as ce:
                    results[sid] = ce.code
            extra["race_same_slot"] = {"slot": b[0], **results}
    if n == 1:
        appts = conn.execute("SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'APT-%'").fetchone()[0]
        holds = conn.execute("SELECT COUNT(*) FROM slot_holds").fetchone()[0]
        extra["writes_to_capacity"] = {"appointments_created": appts, "holds_created": holds}
    return {"level": n, "title": title, "where": where, "proves": proves, "evidence": ev, "turns": turns, "extra": extra}


def run_scheduler():
    from setuhaul.scheduling_tool import what_if
    conn = D.open_working_copy(Path(tempfile.mkdtemp()) / "lp.db")
    with T.capture() as calls, D.capture_sql(conn) as sql:
        prop = T.propose_facility_sequence(conn, "FAC-JAI-01", at("09:50"), "09:50 snapshot")
    w1 = what_if("FAC-JAI-01", at("09:50"), eta={"SHP1009": "12:20"})
    w2 = what_if("FAC-JAI-01", at("10:45"), gate={"SHP1014": "10:45"})
    return {"level": "+", "title": "An early truck, a late truck and work in progress share one facility",
            "where": "SHP1002, SHP1003, SHP1004, SHP1006",
            "proves": "Facility-level objective with protected work: the tool proposes, a coordinator approves.",
            "proposal": prop, "tools": [c.__dict__ for c in calls], "tables_read": sql.tables()["read"],
            "tables_written": sql.tables()["write"],
            "what_if": [{"events": w["events"], "changes": w["sequence_changes"],
                         "recommended": w["after"]["recommended"]["policy"]} for w in (w1, w2)]}


def to_md(levels, sched) -> str:
    L = ["# Situation walkthrough: how each case is handled and where the data comes from", "",
         "Generated by `tools/situation_walkthrough.py`. Each ladder level runs on a fresh copy of the supplied "
         "database through the production workflow. For every driver message the run records the **model** step "
         "(language only), the **tool** calls (READ / WRITE) and the **tables** read and written (captured from "
         "SQLite's statement trace, not written by hand).", ""]
    for lv in levels:
        L += [f"## Level {lv['level']}: {lv['title']}", "",
              f"**Where to look:** `{lv['where']}`  ", f"**What it proves:** {lv['proves']}", "",
              "**Data fetched to frame the case**", "", "| Query | Purpose | Rows |", "|---|---|---|"]
        for e in lv["evidence"]:
            L.append(f"| `{e['query']}({e['arg']})` | {e['purpose']} | {e['rows']} |")
        L += ["", "**Turn by turn**", ""]
        for t in lv["turns"]:
            L.append(f"**{t['time']} {t['driver']}** ({t['lang']}): _{t['text']}_" if t["driver"] != "OTHER" else
                     f"**{t['time']} world change**: {t['text']}")
            L.append("")
            if t["duplicate"]:
                L.append("- Duplicate delivery: suppressed before the model runs (`inbound_dedupe`). No tool called.")
            for m in t["model"]:
                if m["name"] == "understand":
                    L.append(f"- MODEL understand: {m['result']}")
            for c in t["tools"]:
                L.append(f"- TOOL `{c['name']}` [{c['kind']}]{'' if c['ok'] else ' FAILED'}: {c['result']}")
            ph = [m["args"].get("template") for m in t["model"] if m["name"] == "phrase"]
            if ph:
                L.append(f"- MODEL phrase: templates {', '.join(dict.fromkeys(p for p in ph if p))}")
            L.append(f"- DB read: {', '.join(t['tables_read']) or '-'}")
            L.append(f"- DB write: {', '.join(t['tables_written']) or '-'}")
            L.append(f"- State: `{t['state']}`" + (f", escalation `{t['escalation']}`" if t["escalation"] else "")
                     + (f", committed {t['committed']['status']} {t['committed']['dock']} {t['committed']['planned_start'][11:16]}"
                        if t["committed"] else ""))
            L.append("")
            L.append("> " + (t["reply"] or "(no reply: duplicate suppressed)").replace("\n", "  \n> "))
            L.append("")
        if lv["extra"]:
            L += ["**Extra checks**", "", "```json", json.dumps(lv["extra"], indent=1, default=str)[:1800], "```", ""]
    p = sched["proposal"]
    L += [f"## Level +: {sched['title']}", "", f"**Where to look:** `{sched['where']}`  ",
          f"**What it proves:** {sched['proves']}", "",
          f"Tool: `propose_facility_sequence` [READ]. Tables read: {', '.join(sched['tables_read'])}. "
          f"Tables written: {', '.join(sched['tables_written']) or 'none'}.", "",
          "Objective: " + " > ".join(p["objective"]), "",
          "| Policy | Unscheduled | Missed appts | Weighted wait | Changes | Avg wait (min) |", "|---|---|---|---|---|---|"]
    for x in p["policies"]:
        m = x["metrics"]
        L.append(f"| {x['policy']} | {m['unscheduled']} | {m['missed_appointments']} | {m['weighted_wait']} | "
                 f"{m['schedule_changes']} | {m['avg_wait_min']} |")
    L += ["", f"Recommended: **{p['recommended']['policy']}**. Protected work: "
          + "; ".join(f"{x['dock']} {x['from']}-{x['to']} {x['what']}" for x in p["protected"]), "",
          "Needs a person: " + "; ".join(f"{a['shipment']}: {a['why']}" for a in p["needs_approval"]), ""]
    for w in sched["what_if"]:
        L.append(f"- After **{'; '.join(w['events'])}**: " + ("; ".join(f"{c['shipment']} {c['before']} -> {c['after']}"
                                                                 for c in w["changes"]) or "no change"))
    return "\n".join(L) + "\n"


def main():
    levels = [run_level(lv) for lv in LADDER]
    sched = run_scheduler()
    out = ROOT / "docs" / "walkthrough"
    out.mkdir(parents=True, exist_ok=True)
    (out / "situations.json").write_text(json.dumps({"levels": levels, "scheduler": sched}, indent=1, default=str,
                                                    ensure_ascii=False), encoding="utf-8")
    (ROOT / "docs" / "08_situation_walkthrough.md").write_text(to_md(levels, sched), encoding="utf-8")
    for lv in levels:
        print(f"L{lv['level']}:", " | ".join(f"{t['driver']} {t['state']} tools={[c['name'] for c in t['tools']]} "
                                             f"W={t['tables_written']}" for t in lv["turns"]))
        if lv["extra"]:
            print("   extra:", {k: (v if not isinstance(v, dict) else {a: b for a, b in v.items() if a != 'tools'})
                                for k, v in lv["extra"].items()})
    print("L+:", sched["proposal"]["recommended"]["policy"], sched["tables_written"], [w["changes"][:2] for w in sched["what_if"]])


if __name__ == "__main__":
    main()
