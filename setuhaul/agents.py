"""Stage 2: the coordinator's process run by agents, with guardrails at every hand-off.

The agents do the coordinator's legwork; they never invent a schedule. Each agent has a
fixed role, an allow-list of tools and a step budget. Language is the only place a model
(LLM) may help, and its output is validated before anything uses it.

    IntakeAgent      driver messages -> structured facts (model.understand; LLM fallback
                     optional, schema-validated); records ETAs (facts only); flags injection
                     and priority claims (logged, never obeyed)
    MonitorAgent     classifies every truck against its booking; decides whether to re-plan
    PlannerAgent     calls the exact solver tool (branch and bound over real slots); READ only
    PolicyGate       deterministic rules from rules.yaml `agent_autonomy`: which actions may
                     run without a person, which wait for one, which are escalated
    VerifierAgent    independent plan checker before execution and after it
    ExecutorAgent    the ONLY agent allowed to book: hold -> confirm, revalidated, idempotent
    CommsAgent       messages to driver (own language), warehouse coordinator, dispatcher,
                     customer; reviewed templates; optional LLM polish must pass the grounding
                     guard or the template is sent instead
    EscalationAgent  no dock / safety / guard failure -> named person with the full record

Modes: shadow (agents propose, people execute everything) and assist (agents execute the
safe class only). There is no mode in which an agent moves a booking that still works.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import comms, coordinator as C, database as D
from .escalation import escalate
from .facility_day import snapshot
from .guardrails import check_input
from .model import understand
from .rules import R
from .timeutil import hhmm, iso, minutes, parse

PERMISSIONS = {
    "IntakeAgent": {"understand", "record_eta", "read"},
    "MonitorAgent": {"snapshot", "read"},
    "PlannerAgent": {"plan_facility", "read"},
    "PolicyGate": set(),
    "VerifierAgent": {"verify", "read"},
    "ExecutorAgent": {"book", "verify", "read"},
    "CommsAgent": {"draft", "ground_check", "queue_message"},
    "EscalationAgent": {"escalate"},
}


class AgentGuardError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass
class Step:
    n: int
    agent: str
    tool: str
    detail: str
    result: str
    guard: list[str] = field(default_factory=list)
    ok: bool = True
    ms: float = 0.0


@dataclass
class Run:
    event_id: str
    at: str
    mode: str
    steps: list[Step] = field(default_factory=list)
    auto: list[dict] = field(default_factory=list)
    queue: list[dict] = field(default_factory=list)
    escalations: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    intake: list[dict] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    verify_before: list[str] = field(default_factory=list)
    verify_after: list[str] = field(default_factory=list)
    stopped: str | None = None
    improvement: dict = field(default_factory=dict)
    open_with_person: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, conn, facility_id: str, mode: str | None = None, llm_polish: Callable[[str], str] | None = None,
                 llm_intake: Callable[[str], dict] | None = None, rogue: dict | None = None):
        self.conn, self.fac = conn, facility_id
        self.cfg = R()["agent_autonomy"]
        self.mode = mode or self.cfg["mode"]
        self.llm_polish, self.llm_intake = llm_polish, llm_intake
        self.rogue = rogue or {}          # red-team hooks used only by the evaluation suite
        self.calls = 0

    # ------------------------------------------------------------- plumbing --
    def use(self, run: Run, agent: str, tool: str, fn: Callable[[], Any], detail: str = "",
            summarise: Callable[[Any], str] = lambda x: "") -> Any:
        if tool not in PERMISSIONS[agent]:
            run.steps.append(Step(len(run.steps) + 1, agent, tool, detail, "BLOCKED", [f"TOOL_NOT_ALLOWED for {agent}"], False))
            raise AgentGuardError("TOOL_NOT_ALLOWED", f"{agent} may not call {tool}")
        self.calls += 1
        if self.calls > self.cfg["max_tool_calls_per_event"]:
            run.steps.append(Step(len(run.steps) + 1, agent, tool, detail, "STOPPED", ["STEP_BUDGET_EXCEEDED"], False))
            raise AgentGuardError("STEP_BUDGET_EXCEEDED", f"more than {self.cfg['max_tool_calls_per_event']} tool calls")
        t0 = time.perf_counter()
        out = fn()
        run.steps.append(Step(len(run.steps) + 1, agent, tool, detail, summarise(out), ms=round((time.perf_counter() - t0) * 1000, 1)))
        return out

    # ---------------------------------------------------------------- agents --
    def intake(self, run: Run, now: datetime, since: datetime):
        msgs = self.conn.execute("""SELECT m.*, t.driver_id, t.shipment_id FROM chat_messages m JOIN chat_threads t USING(thread_id)
                                    WHERE m.sender_type='DRIVER' AND m.message_ts > ? AND m.message_ts <= ?
                                    ORDER BY m.message_ts""", (iso(since), iso(now))).fetchall()
        msgs = [dict(m) for m in msgs] + self.rogue.get("extra_messages", [])
        seen = {}
        for m in msgs:
            ic = check_input(m["message_text"])
            p = self.use(run, "IntakeAgent", "understand",
                         lambda: understand(m["message_text"], parse(m["message_ts"])), f"{m['driver_id']}: {m['message_text'][:60]}",
                         lambda p: f"{p.lang}, {'/'.join(p.intents) or 'UNKNOWN'}" + (f", eta {hhmm(p.eta)}" if p.eta else ""))
            rec = {"time": m["message_ts"][11:16], "driver": m["driver_id"], "shipment": m.get("shipment_id"),
                   "text": m["message_text"], "lang": comms.detect(m["message_text"]), "intents": p.intents,
                   "eta": hhmm(p.eta) if p.eta else None, "flags": ic.flags + p.flags, "action": ""}
            if self.llm_intake and not p.understood():
                raw = self.llm_intake(ic.redacted) or {}
                allowed = {"intent", "eta_hhmm", "delay_minutes", "language"}
                dropped = sorted(set(raw) - allowed)
                if dropped:
                    run.steps[-1].guard.append(f"LLM_FIELDS_DROPPED:{','.join(dropped)}")
                rec["llm"] = {k: v for k, v in raw.items() if k in allowed}
            if "EARLY_ARRIVAL" in p.intents:
                rec["action"] = "early arrival: the chat workflow offers idle dock time that displaces nobody"
            elif "CHECK_STATUS" in p.intents or (not p.eta and "REPORT_DELAY" in p.intents):
                rec["action"] = "no new time in the message: answered by the chat workflow, nothing recorded"
            if "PRIORITY_CLAIM" in rec["flags"]:
                rec["action"] = "priority claim logged, not used: priority comes from the shipment record"
            key = (m["driver_id"], " ".join(m["message_text"].lower().split()))
            if key in seen:
                rec["action"] = f"repeat of the {seen[key]} message: ignored (dedupe)"
                run.intake.append(rec)
                continue
            seen[key] = rec["time"]
            if p.eta and p.eta_kind == "RELATIVE_DELAY" and p.delay_min and m.get("shipment_id"):
                plan_eta = self.conn.execute("SELECT original_eta_ts FROM shipments WHERE shipment_id=?",
                                             (m["shipment_id"],)).fetchone()[0]
                p.eta = parse(plan_eta) + minutes(p.delay_min)          # 'late by 1 hour' = plan + 60, not now + 60
                rec["eta"] = hhmm(p.eta) + " (plan + delay)"
            if p.eta and not m.get("shipment_id"):
                rec["action"] = "shipment unclear (driver has more than one trip): the chat workflow asks which; nothing recorded"
            rec["flags"] = list(dict.fromkeys(rec["flags"]))
            if "INJECTION" in rec["flags"]:
                rec["action"] = "quarantined: instructions ignored, nothing recorded from this message; coordinator reviews it"
                run.intake.append(rec)
                continue
            if p.eta and m.get("shipment_id"):
                dup = self.conn.execute("""SELECT 1 FROM eta_updates WHERE shipment_id=? AND declared_eta_ts=?""",
                                        (m["shipment_id"], iso(p.eta))).fetchone()
                if dup:
                    rec["action"] = (rec["action"] + "; " if rec["action"] else "") + "ETA already on record (idempotent)"
                else:
                    self.use(run, "IntakeAgent", "record_eta", lambda: D.record_eta(
                        self.conn, eta_update_id=f"ETA-AG-{hashlib.sha1((m.get('chat_message_id') or m.get('message_id') or m['message_text']).encode()).hexdigest()[:8]}",
                        shipment_id=m["shipment_id"], driver_id=m["driver_id"], declared_eta_ts=iso(p.eta),
                        confidence=p.eta_confidence or "MEDIUM", reason=None, note="agent intake", created_at=m["message_ts"]),
                        m["shipment_id"], lambda _: "eta_updates row written")
                    rec["action"] = (rec["action"] + "; " if rec["action"] else "") + "ETA recorded"
            run.intake.append(rec)

    def monitor(self, run: Run, now: datetime) -> tuple[bool, dict]:
        snap = self.use(run, "MonitorAgent", "snapshot", lambda: snapshot(self.conn, self.fac, now), self.fac,
                        lambda s: ", ".join(f"{k} {v}" for k, v in _count(s["trucks"]).items()))
        trig = R()["agent_autonomy"]["replan_triggers"]
        hits = [t for t in snap["trucks"] if t["status"] in trig or (t["status"] == "AT_RISK" and "AT_RISK_OVER_30" in trig
                and int(t["why"].split(", ")[-1].split()[0]) > 30 if "min into" in t["why"] else False)]
        hits += [t for t in snap["trucks"] if t["status"] == "IN_YARD" and t["booking"] and t["booking"].split("-")[-1] <= hhmm(now)]
        return bool(hits) or bool(snap["events"]), snap

    def run(self, now: datetime, since: datetime, event_id: str | None = None) -> Run:
        event_id = event_id or f"EVT-{hhmm(now).replace(':', '')}"
        run = Run(event_id, hhmm(now), self.mode)
        try:
            self.intake(run, now, since)
            need, snap = self.monitor(run, now)
            if not need:
                run.stopped = "no trigger: nothing to re-plan"
                return run
            plan = self.use(run, "PlannerAgent", "plan_facility", lambda: C.plan(self.conn, self.fac, now, "repair"),
                            "repair: trucks that need a slot; working bookings locked",
                            lambda p: f"{p['solver']['status'].lower()}, cost {p['solver']['cost']}, "
                                      f"{len(p['actions'])} change(s), {p['solver']['nodes']} nodes")
            actions = plan["actions"]
            if "tamper" in self.rogue:                   # red team: a modified plan must be caught before execution
                self.rogue["tamper"](actions)
            run.plan = {"solver": plan["solver"], "baselines": plan["baselines"], "kept": plan["kept"]}
            # ---- verify the proposal against the hard rules before anything is executed
            problems = self.use(run, "VerifierAgent", "verify", lambda: _check_actions(self.conn, self.fac, actions, now),
                                f"{len(actions)} proposed changes", lambda v: "ok" if not v else f"{len(v)} problem(s)")
            run.verify_before = problems
            bad = {p.split()[1] for p in problems}
            # ---- policy gate (deterministic, rules.yaml)
            auto, queue, esc = [], [], []
            for a in actions:
                why = gate(a, self.cfg, self.mode)
                if a.shipment in bad:
                    why = ["failed the plan check: " + "; ".join(p for p in problems if p.split()[1] == a.shipment)]
                if a.kind == "ESCALATE":
                    esc.append(a)
                elif why:
                    queue.append((a, why))
                else:
                    auto.append(a)
            if len(auto) > self.cfg["max_auto_bookings_per_event"]:
                for a in auto[self.cfg["max_auto_bookings_per_event"]:]:
                    queue.append((a, [f"over the {self.cfg['max_auto_bookings_per_event']} automatic bookings per event limit"]))
                auto = auto[: self.cfg["max_auto_bookings_per_event"]]
            run.steps.append(Step(len(run.steps) + 1, "PolicyGate", "-", f"mode {self.mode}",
                                  f"{len(auto)} automatic, {len(queue)} to the coordinator, {len(esc)} escalated"))
            # ---- execute the safe class
            if auto:
                if "executor_override" in self.rogue:
                    self.rogue["executor_override"](self, run)
                res = self.use(run, "ExecutorAgent", "book", lambda: C.execute(self.conn, auto, now, "agent:executor", event_id),
                               f"{len(auto)} booking(s)", lambda r: f"{sum(x['ok'] for x in r)} booked, "
                                                                     f"{sum(not x['ok'] for x in r)} left for the coordinator")
                for r in res:
                    a = next(x for x in auto if x.shipment == r["shipment"])
                    (run.auto if r["ok"] else run.queue).append(
                        {**_card(a), "result": r} if r["ok"] else {**_card(a), "why": [r["detail"]]})
            for a, why in queue:
                run.queue.append({**_card(a), "why": why, "group": [b.shipment for b in actions
                                                                    if a.shipment in b.blocked_by_approval and not b.approval]})
            # ---- escalate what no rule can solve
            for a in esc:
                h = self.use(run, "EscalationAgent", "escalate", lambda: escalate(
                    self.conn, reason="NO_FEASIBLE_SLOT", thread_id=None, shipment_id=a.shipment, facility_id=self.fac,
                    now=now, facts={"plan_cost": a.cost, "from": a.from_, "eta_conf": a.eta_conf},
                    recommendation="next-morning first compatible slot, overtime approval, or divert"), a.shipment,
                    lambda h: f"{h['escalation_id']} -> {h['routed_to']['role']} {h['routed_to']['name']}")
                run.escalations.append({**_card(a), "escalation": h["escalation_id"], "to": h["routed_to"]["name"],
                                        "role": h["routed_to"]["role"]})
            # ---- messages
            self.communicate(run, actions)
            # ---- tier 2: would moving working bookings help enough to bother a person?
            keep = self.use(run, "PlannerAgent", "plan_facility", lambda: C.status_quo_cost(self.conn, self.fac, now),
                            "cost of keeping every working booking", lambda c: f"cost {round(c)}")
            imp = self.use(run, "PlannerAgent", "plan_facility", lambda: C.plan(self.conn, self.fac, now, "improve",
                                                                                with_baselines=False),
                           "improve: every open truck may move", lambda p: f"cost {p['solver']['cost']}, {len(p['actions'])} change(s)")
            saving = round(keep - imp["solver"]["cost"])
            worth = saving >= self.cfg["propose_improvement_if_saving_over"]
            run.improvement = {"keep_cost": round(keep), "improved_cost": imp["solver"]["cost"], "saving": saving,
                               "proposed": worth, "status": imp["solver"]["status"],
                               "moves": [_card(a) | {"why": a.why, "group": [b.shipment for b in imp["actions"]
                                                                              if a.shipment in b.blocked_by_approval and not b.approval]}
                                         for a in imp["actions"] if a.kind == "MOVE_PROMISE"] if worth else []}
            run.steps.append(Step(len(run.steps) + 1, "PolicyGate", "-", "improvement proposal",
                                  (f"saves {saving} cost units: {len(run.improvement['moves'])} promise move(s) proposed to the coordinator"
                                   if worth else (f"saves only {saving}: not worth disturbing working bookings" if saving > 0 else
                                                  "no better plan found within the node budget: working bookings left alone"))))
            after = self.use(run, "VerifierAgent", "verify", lambda: C.verify(self.conn, self.fac), "facility after execution",
                             lambda v: "ok: no rule broken" if not v else f"{len(v)} open problem(s)")
            waiting = {x["shipment"] for x in run.queue} | {x["shipment"] for x in run.escalations}
            run.open_with_person = [v for v in after if v.startswith("BOOKED_ON_DOWN_DOCK") and v.split()[1].rstrip(":") in waiting]
            run.verify_after = [v for v in after if v not in run.open_with_person]
            if run.open_with_person:
                run.steps[-1].result = (f"no rule broken by the agents; {len(run.open_with_person)} truck(s) still on a down dock, "
                                        f"waiting for the coordinator")
        except AgentGuardError as g:
            run.stopped = f"{g.code}: {g}"
            try:
                h = escalate(self.conn, reason="GUARDRAIL_BLOCK", thread_id=None, shipment_id=None, facility_id=self.fac,
                             now=now, facts={"guard": g.code, "detail": str(g)})
                run.escalations.append({"shipment": "-", "escalation": h["escalation_id"], "to": h["routed_to"]["name"],
                                        "role": h["routed_to"]["role"], "why": g.code})
            except Exception:  # noqa: BLE001
                pass
        return run

    def communicate(self, run: Run, actions: list):
        executed = {x["shipment"] for x in run.auto}
        for a in actions:
            if a.shipment not in executed and a.kind != "ESCALATE":
                continue          # held until a person approves; sent by the console after execution
            m = a.messages["driver"]
            text = m["text"]
            polished = None
            if self.llm_polish:
                polished = self.llm_polish(text)
            allowed_t = {x for x in (a.start, a.end) if x}
            if a.start:
                from .timeutil import minutes
                allowed_t.add(hhmm(parse(f"2026-08-04T{a.start}:00+05:30") - minutes(15)))
            if a.from_:
                allowed_t |= set(a.from_.split()[1].split("-"))
            docks = {a.to.split()[0]} if a.to else set()
            if a.from_:
                docks.add(a.from_.split()[0])
            guard = []
            if polished is not None:
                guard = self.use(run, "CommsAgent", "ground_check", lambda: comms.ground_check(
                    polished, allowed_times=allowed_t, allowed_docks=docks, executed=a.kind != "ESCALATE"),
                    f"LLM polish for {a.shipment}", lambda v: "ok" if not v else "rejected: " + ", ".join(v))
                text = text if guard else polished
            run.messages.append({"shipment": a.shipment, "to": f"driver {a.driver}", "channel": "WHATSAPP",
                                 "lang": m["lang"], "detected": m["detected"], "needs_translation": m["needs_translation"],
                                 "text": text, "llm_rejected": guard})
            if "customer" in a.messages:
                run.messages.append({"shipment": a.shipment, "to": f"customer {a.customer}", "channel": "EMAIL", "lang": "en",
                                     "text": a.messages["customer"]["text"]})
        done = [a for a in actions if a.shipment in executed]
        if done:
            pm = C.party_messages(self.conn, self.fac, run.at, done)
            run.messages.append({"shipment": "-", "to": "warehouse coordinator", "channel": "EMAIL", "lang": "en", "text": pm["warehouse"]})
            for carrier, txt in pm["dispatchers"].items():
                run.messages.append({"shipment": "-", "to": f"dispatcher {carrier}", "channel": "EMAIL", "lang": "en", "text": txt})
        run.steps.append(Step(len(run.steps) + 1, "CommsAgent", "queue_message", "all parties",
                              f"{len(run.messages)} message(s) queued"))


def gate(a, cfg: dict, mode: str) -> list[str]:
    """Reasons an action may NOT run without a person (empty list = may run automatically)."""
    why = []
    if mode == "shadow":
        why.append("shadow mode: every change is executed by a person")
    if a.kind in cfg["never_auto"] and not a.approval:
        why.append({"MOVE_PROMISE": "moves a booking that still works", "ESCALATE": "no dock"}[a.kind])
    if a.kind not in cfg["auto_execute_kinds"] and a.kind not in cfg["never_auto"]:
        why.append(f"{a.kind} is not in the automatic class")
    why += a.approval
    if a.blocked_by_approval:
        why.append("waits for approval of " + ", ".join(a.blocked_by_approval))
    return list(dict.fromkeys(why))


def _check_actions(conn, fac: str, actions, now: datetime) -> list[str]:
    """Hard-rule check of a PROPOSED plan (before execution), independent of the solver."""
    v = []
    docks = {r["dock_code"]: dict(r) for r in conn.execute("SELECT * FROM docks WHERE facility_id=?", (fac,))}
    ev = conn.execute("SELECT e.*, d.dock_code FROM dock_status_events e JOIN docks d USING(dock_id) WHERE d.facility_id=? "
                      "AND e.event_type IN ('BREAKDOWN','MAINTENANCE','BLOCKED')", (fac,)).fetchall()
    iv = []
    for a in actions:
        if not a.slot_id:
            continue
        sh = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (a.shipment,)).fetchone()
        d = docks[a.to.split()[0]]
        slot = conn.execute("SELECT * FROM appointment_slots WHERE slot_id=?", (a.slot_id,)).fetchone()
        if slot is None or slot["dock_id"] != d["dock_id"]:
            v.append(f"SLOT_MISMATCH {a.shipment}")
            continue
        st, en = f"2026-08-04T{a.start}:00+05:30", f"2026-08-04T{a.end}:00+05:30"
        if sh["temperature_control_required"] and not d["supports_refrigerated"]:
            v.append(f"REEFER {a.shipment}")
        if sh["load_weight_kg"] > d["max_vehicle_weight_kg"]:
            v.append(f"OVERWEIGHT {a.shipment}")
        if sh["required_dock_type"] == "HEAVY" and d["dock_type"] != "HEAVY":
            v.append(f"HEAVY {a.shipment}")
        if not (slot["slot_start_ts"] <= st < slot["slot_end_ts"]):
            v.append(f"WINDOW {a.shipment}")
        if slot["slot_status"] != "OPEN":
            v.append(f"BLOCKED_SLOT {a.shipment}")
        if st < iso(now):
            v.append(f"IN_THE_PAST {a.shipment}")
        for e in ev:
            if e["dock_code"] == d["dock_code"] and e["event_start_ts"] < en and st < (e["event_end_ts"] or "2026-08-04T23:59"):
                v.append(f"DOCK_EVENT {a.shipment}")
        iv.append((d["dock_code"], st, en, a.shipment))
    for i, x in enumerate(iv):
        for y in iv[i + 1:]:
            if x[0] == y[0] and x[1] < y[2] and y[1] < x[2]:
                v.append(f"OVERLAP {y[3]} with {x[3]} on {x[0]}")
    return v


def _card(a) -> dict:
    return {"shipment": a.shipment, "kind": a.kind, "priority": a.priority, "driver": a.driver, "from": a.from_,
            "to": a.to, "start": a.start, "approval": a.approval, "blocked_by": a.blocked_by_approval, "eta_conf": a.eta_conf}


def _count(trucks) -> dict:
    out = {}
    for t in trucks:
        out[t["status"]] = out.get(t["status"], 0) + 1
    return out
