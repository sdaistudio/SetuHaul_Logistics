"""Facility-level scheduling tool (optional hand-in; scenario point 7).

The driver workflow looks after one driver at a time. This tool looks at ONE
facility and ALL its inbound trucks at once and PROPOSES a sequence after an
ETA update or a gate check-in. It is called by the workflow / coordinator as a
controlled READ tool (tool.propose_facility_sequence): it never writes a
booking. A coordinator approves moves, which then go through capacity.book().

Scheduling model
  job      = inbound truck            release = planning ETA + gate-to-dock (or now, if in the yard)
  machine  = dock door                processing = expected_unload_min
  due      = start of current booking weight = priority (rules.yaml priority_weight)
  fixed    = truck already unloading (protected work) and dock maintenance / blocks
  hard     = dock type, reefer -> D5, weight limit, 21:00 last new start (needs approval)

Explicit objective (lexicographic, then weighted, all from rules.yaml)
  1. nobody left without a dock if any compatible dock can take them
  2. keep reachable promises (missed appointments caused by the plan)
  3. priority-weighted waiting  (CRITICAL 100, HIGH 40, NORMAL 15, LOW 5 per minute)
  4. fewest schedule changes (each change = a driver message + yard re-sequence)

Compared on the same snapshot: FCFS, EDD, WEIGHTED, COMMITMENT_FIRST_WEIGHTED
(+ approved swaps) and OPTIMAL (branch & bound, cross-checked by OR-Tools CP-SAT).

    python -m setuhaul.scheduling_tool --at 09:50
    python -m setuhaul.scheduling_tool --at 09:50 --eta SHP1006=11:20 --gate SHP1012=10:55
"""
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import database as D
from .rules import R
from .scheduler import build_jobs, compare, plan_table
from .timeutil import hhmm, iso, parse

OBJECTIVE = ["unscheduled", "missed_appointments", "weighted_wait", "schedule_changes"]


@dataclass
class Proposal:
    facility_id: str
    at: str
    trigger: str
    objective: list[str]
    weights: dict
    protected: list[dict]
    blocked: list[dict]
    trucks: list[dict]
    policies: list[dict]
    recommended: dict
    moves: list[dict]
    needs_approval: list[dict]
    wrote_to_database: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


def _rank_key(m: dict) -> tuple:
    return tuple(m.get(k, 0) for k in OBJECTIVE)


def propose_sequence(conn, facility_id: str, now: datetime, trigger: str = "") -> Proposal:
    before = conn.total_changes
    jobs, fs = build_jobs(conn, facility_id, now)
    protected, blocked = [], []
    for dock, items in fs.busy.items():
        code = fs.docks[dock]["dock_code"] if dock in fs.docks else dock
        for b in items:
            row = {"dock": code, "from": hhmm(b.start), "to": hhmm(b.end), "what": b.label}
            (protected if b.kind == "IN_PROGRESS" else blocked).append(row)
    results = compare(conn, facility_id, now)
    pols = [{"policy": r.policy, "metrics": {k: v for k, v in r.metrics.items() if k != "swap_proposals"},
             "swap_proposals": r.metrics.get("swap_proposals", [])} for r in results]
    best = min(range(len(results)), key=lambda i: (_rank_key(results[i].metrics), i))
    rec = results[best]
    code = lambda d: fs.docks[d]["dock_code"] if d in fs.docks else d  # noqa: E731
    by_job = {j.shipment_id: j for j in jobs}
    table = plan_table(rec)
    for row in table:
        row["dock"] = code(row["dock"]) if row["dock"] else None
        j = by_job[row["shipment"]]
        row["priority"] = j.priority
        row["release"] = hhmm(j.release)
    moves, approvals = [], []
    for p in rec.placements:
        j = by_job[p.shipment_id]
        if j.due is None:
            if p.start:
                moves.append({"shipment": p.shipment_id, "change": "NEW BOOKING",
                              "to": f"{code(p.dock_id)} {hhmm(p.start)}"})
            continue
        now_s = f"{code(j.current_dock)} {hhmm(j.due)}-{hhmm(j.due_end)}"
        if p.start is None:
            moves.append({"shipment": p.shipment_id, "change": "NO DOCK TODAY", "from": now_s})
            approvals.append({"shipment": p.shipment_id, "why": "no compatible dock time left: escalate"})
        elif not p.kept_commitment:
            moves.append({"shipment": p.shipment_id, "change": "MOVE", "from": now_s,
                          "to": f"{code(p.dock_id)} {hhmm(p.start)}"})
            if j.release <= j.due_end and j.current_dock in j.compatible:   # the truck could still have made it: we are breaking a promise
                approvals.append({"shipment": p.shipment_id, "why": "reachable booking moved: coordinator decides"})
        if p.needs_approval:
            approvals.append({"shipment": p.shipment_id, "why": "start after 21:00 (RULE005)"})
    for sw in next((x["swap_proposals"] for x in pols if x["swap_proposals"]), []):
        approvals.append({"shipment": sw["promote"], "why": f"swap with {sw['displace']} on {code(sw['dock'])}: {sw['needs']}"})
    prop = Proposal(
        facility_id=facility_id, at=iso(now), trigger=trigger or "scheduled re-plan",
        objective=["1 nobody left without a dock", "2 fewest missed appointments", "3 least priority-weighted wait",
                   "4 fewest schedule changes"],
        weights=R()["priority_weight"], protected=protected, blocked=blocked,
        trucks=[{"shipment": j.shipment_id, "priority": j.priority, "release": hhmm(j.release), "unload_min": j.duration,
                 "booked": f"{code(j.current_dock)} {hhmm(j.due)}" if j.due else None, "eta_conf": j.eta_conf,
                 "docks": [code(d) for d in j.compatible]} for j in sorted(jobs, key=lambda j: j.release)],
        policies=pols, recommended={"policy": rec.policy, "metrics": {k: v for k, v in rec.metrics.items()
                                                                     if k != "swap_proposals"}, "plan": table},
        moves=moves, needs_approval=approvals)
    prop.wrote_to_database = conn.total_changes != before
    prop.notes.append("Proposal only: approved moves are booked through capacity.book() with revalidation.")
    return prop


def what_if(facility_id: str, now: datetime, eta: dict[str, str] | None = None, gate: dict[str, str] | None = None) -> dict:
    """Run the tool before and after an ETA update / gate check-in on a scratch copy."""
    conn = D.open_working_copy(Path(tempfile.mkdtemp()) / "sched.db")
    before = propose_sequence(conn, facility_id, now, "before event")
    day = now.strftime("%Y-%m-%dT")
    events = []
    for sid, hm in (eta or {}).items():
        D.record_eta(conn, eta_update_id=f"ETA-WI-{sid}", shipment_id=sid, driver_id=None,
                     declared_eta_ts=f"{day}{hm}:00+05:30", confidence="HIGH", reason="OTHER", note="what-if",
                     created_at=iso(now), source="DRIVER_DECLARED")
        events.append(f"ETA update {sid} -> {hm}")
    for sid, hm in (gate or {}).items():
        D.record_gate_in(conn, sid, facility_id, f"{day}{hm}:00+05:30")
        events.append(f"gate check-in {sid} at {hm}")
    after = propose_sequence(conn, facility_id, now, "; ".join(events))
    b = {r["shipment"]: r for r in before.recommended["plan"]}
    changed = [{"shipment": s, "before": f"{b[s]['dock']} {b[s]['start']}" if s in b else None,
                "after": f"{r['dock']} {r['start']}"} for s, r in ((r["shipment"], r) for r in after.recommended["plan"])
               if s not in b or (b[s]["dock"], b[s]["start"]) != (r["dock"], r["start"])]
    return {"events": events, "before": before.as_dict(), "after": after.as_dict(), "sequence_changes": changed}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--facility", default="FAC-JAI-01")
    ap.add_argument("--at", default="09:50", help="HH:MM on 2026-08-04")
    ap.add_argument("--eta", action="append", default=[], help="SHP=HH:MM")
    ap.add_argument("--gate", action="append", default=[], help="SHP=HH:MM")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = parse(f"2026-08-04T{a.at}:00+05:30")
    kv = lambda xs: dict(x.split("=") for x in xs)  # noqa: E731
    if a.eta or a.gate:
        out = what_if(a.facility, now, kv(a.eta), kv(a.gate))
        rec = out["after"]
    else:
        conn = D.open_working_copy(Path(tempfile.mkdtemp()) / "sched.db")
        out = propose_sequence(conn, a.facility, now).as_dict()
        rec = out
    if a.json:
        print(json.dumps(out, indent=1, default=str))
        return
    print(f"Facility {a.facility} at {a.at}. Trigger: {rec['trigger']}")
    print("Objective:", " > ".join(rec["objective"]))
    print(f"{'policy':48s} " + " ".join(f"{k[:12]:>12s}" for k in OBJECTIVE + ["avg_wait_min"]))
    for p in rec["policies"]:
        print(f"{p['policy'][:48]:48s} " + " ".join(f"{p['metrics'].get(k, 0):>12}" for k in OBJECTIVE + ["avg_wait_min"]))
    print("Recommended:", rec["recommended"]["policy"])
    for r in rec["recommended"]["plan"]:
        print(f"  {r['shipment']} {r['priority']:8s} ready {r['release']}  -> {r['dock'] or '-':4s} {r['start'] or '--:--'}"
              f"-{r['end'] or '--:--'} {'kept' if r['kept'] else ''} {'APPROVAL' if r['approval'] else ''} {r['note']}")
    print("Moves vs today:", rec["moves"] or "none")
    print("Needs a person:", rec["needs_approval"] or "nothing")
    if "sequence_changes" in out:
        print("Sequence changes caused by the event:", out["sequence_changes"] or "none")
    print("Wrote to database:", rec["wrote_to_database"])


if __name__ == "__main__":
    main()
