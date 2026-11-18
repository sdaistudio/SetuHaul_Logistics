"""Driver conversation workflow.

This is a *workflow*, not an autonomous agent (Anthropic, "Building effective
agents"): the steps are fixed code paths chosen by a state machine. Language
understanding is the only fuzzy step, and it is deterministic too (nlu.py).

States
  NEW -> NEED_SHIPMENT -> NEED_ETA -> OPTIONS_SHOWN -> CONFIRMED
                                    `-> PENDING_WAREHOUSE -> CONFIRMED/REJECTED
  any -> ESCALATED (safety, human asked, no feasible slot, clarification limit,
                    guardrail block)
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .capacity import CapacityError, pending_deadline
from .db import audit, write_txn
from .escalation import summarise_tried
from .eta import EtaEstimate
from .feasibility import Constraints, rank_options, summarise_rejections
from .guardrails import GuardrailViolation, check_action, check_input, check_option_choice, check_output
from .llm_fallback import NullFallback
from .nlu import ParseResult, normalise
from .obs import Telemetry
# Language goes through the model layer, capacity and data through the tool layer
# (see model.py / tool.py / database.py). Same functions, now traced per turn.
from .model import tpl, understand_text as parse_message
from .tool import (check_slot_feasibility as evaluate_all, confirm_booking as commit_hold,
                   escalate_to_human as escalate, get_eta as resolve_eta, hold_slot as place_hold,
                   rank_options_lookahead as rank_for_driver, traced)
from .timeutil import diff_min, hhmm, iso, minutes, parse

STATIC_TIMES = {"11:30", "12:30", "13:00", "21:00"}  # example times inside templates


@dataclass
class Turn:
    thread_id: str
    reply: str | None
    state: str
    lang: str
    intent: str
    actions: list[str] = field(default_factory=list)
    committed: dict | None = None
    escalation: dict | None = None
    guard_violations: list[str] = field(default_factory=list)
    duplicate: bool = False
    clarification: bool = False
    parse: dict | None = None
    options: list[dict] = field(default_factory=list)
    trace_id: str | None = None


class DriverAgent:
    def __init__(self, conn, telemetry: Telemetry | None = None, fallback=None):
        self.conn = conn
        self.tel = telemetry or Telemetry(echo=False)
        self.fallback = fallback or NullFallback()

    # ================================================================ entry ==
    def handle(self, driver_id: str, text: str, now: datetime, thread_id: str | None = None,
               external_id: str | None = None) -> Turn:
        thread_id = thread_id or f"THR-{uuid.uuid4().hex[:6].upper()}"
        trace = self.tel.new_trace(thread_id=thread_id, driver_id=driver_id)
        self.tel.metric("MessagesReceived")
        with self.tel.span("ingest") as sp:
            ic = check_input(text)
            sp["flags"] = ic.flags
            sp["text_redacted"] = ic.redacted
            for f in ic.flags:
                self.tel.metric("GuardrailInputFlag", flag=f)
            if self._is_duplicate(driver_id, ic.text, now, external_id):
                self.tel.metric("DuplicateSuppressed")
                st = self._load(thread_id, driver_id, now)
                self._log_msg(thread_id, driver_id, "DRIVER", ic.text, now, external_id, dup=True)
                return Turn(thread_id, None, st["state"], st["lang"], "DUPLICATE", ["dedupe"], duplicate=True,
                            trace_id=trace)

        st = self._load(thread_id, driver_id, now)
        d = st["data"]
        turn = Turn(thread_id, None, st["state"], st["lang"], "UNKNOWN", trace_id=trace)

        # ---- understand ------------------------------------------------------
        ref_eta = self._reference_eta(d.get("shipment_id"))
        with self.tel.span("nlu") as sp:
            p = parse_message(ic.text, now, ref_eta)
            if not p.understood():
                fb = self.fallback.parse(ic.redacted)
                if fb:
                    self.tel.metric("LLMFallbackUsed")
                    p = self._merge_fallback(p, fb, now)
            sp.update(intent=p.primary_intent, lang=p.lang, eta=hhmm(p.eta) if p.eta else None,
                      evidence=p.evidence)
        if p.lang == "hi" or len(normalise(ic.text).split()) >= 2:
            st["lang"] = p.lang
        lang = st["lang"]
        turn.lang, turn.intent, turn.parse = lang, p.primary_intent, p.as_dict()
        self._log_msg(thread_id, driver_id, "DRIVER", ic.text, now, external_id, intent=p.primary_intent,
                      eta=p.eta, review=bool(p.clarify) or not p.understood())

        times = set(STATIC_TIMES)
        try:
            reply = self._route(st, p, ic, now, turn, times)
        except GuardrailViolation as gv:
            self.tel.metric("GuardrailActionBlock", code=gv.code)
            turn.guard_violations.append(gv.code)
            if st["state"] == "ESCALATED":
                reply = tpl.render("human", st["lang"], role=tpl.word(tpl.ROLE_NAMES, "WAREHOUSE_COORDINATOR", st["lang"]))
            else:
                reply = self._unknown_or_pick(st, p, st["lang"], turn)

        # ---- output guard ----------------------------------------------------
        with self.tel.span("output_guard") as sp:
            committed = turn.committed["status"] if turn.committed else None
            v = check_output(reply, committed_status=committed, allowed_times=times, lang=lang)
            sp["violations"] = v
            if v:
                for code in v:
                    self.tel.metric("GuardrailOutputBlock", code=code.split(":")[0])
                turn.guard_violations += v
                esc = self._escalate(st, "GUARDRAIL_BLOCK", now, {"blocked_reply": reply, "violations": v})
                turn.escalation = esc
                reply = tpl.render("human", lang, role=tpl.word(tpl.ROLE_NAMES, esc["routed_to"]["role"], lang))
                st["state"] = "ESCALATED"

        turn.reply, turn.state = reply, st["state"]
        self._save(st, now)
        self._log_msg(thread_id, driver_id, "AGENT", reply, now)
        self.tel.event("turn_done", state=st["state"], intent=turn.intent, actions=turn.actions,
                       clarification=turn.clarification)
        if turn.clarification:
            self.tel.metric("ClarificationQuestion")
        return turn

    # =============================================================== router ==
    def _route(self, st, p: ParseResult, ic, now, turn: Turn, times: set) -> str:
        d, lang = st["data"], st["lang"]

        if "SAFETY" in p.intents:
            esc = self._escalate(st, "SAFETY", now, {"message": ic.redacted})
            turn.escalation, turn.actions = esc, ["escalate:SAFETY"]
            st["state"] = "ESCALATED"
            return tpl.render("safety", lang)
        if "HUMAN" in p.intents:
            esc = self._escalate(st, "HUMAN_REQUESTED", now, {"message": ic.redacted})
            turn.escalation, turn.actions = esc, ["escalate:HUMAN_REQUESTED"]
            st["state"] = "ESCALATED"
            return tpl.render("human", lang, role=tpl.word(tpl.ROLE_NAMES, esc["routed_to"]["role"], lang))

        # "pehla dock de do" before any options exist is a request, not a choice of option
        if st["state"] != "OPTIONS_SHOWN" and "SELECT_OPTION" in p.intents and len(p.intents) > 1:
            p.intents = [i for i in p.intents if i != "SELECT_OPTION"]
            p.option_choice = None
            turn.intent, turn.parse = p.primary_intent, p.as_dict()

        d.setdefault("msg_times", []).append(iso(now))
        d["msg_times"] = [t for t in d["msg_times"] if parse(t) > now - minutes(30)]
        if len(d["msg_times"]) > 15:
            esc = self._escalate(st, "RATE_LIMIT", now, {"count_30min": len(d["msg_times"])})
            turn.escalation = esc
            st["state"] = "ESCALATED"
            return tpl.render("human", lang, role=tpl.word(tpl.ROLE_NAMES, esc["routed_to"]["role"], lang))

        # We just asked "what time will you reach?": a bare time is the answer,
        # even if phrased "11:30 tak" (which elsewhere means a deadline).
        if st["state"] == "NEED_ETA" and p.eta is None:
            cand = p.latest_gate_out or p.option_time or p.earliest_start
            if cand:
                p.eta, p.eta_kind = cand, "CLOCK"
                p.eta_confidence = "MEDIUM" if "lagbhag" in normalise(p.text) or "around" in p.text.lower() else "HIGH"
                p.latest_gate_out = p.earliest_start = p.option_time = None
                p.intents = ["REPORT_DELAY"] + [i for i in p.intents if i not in ("ADD_CONSTRAINT", "SELECT_OPTION")]
                p.evidence.append("time given in answer to ETA question")
                turn.parse, turn.intent = p.as_dict(), p.primary_intent
        # remember constraints / ETA facts even if we must ask something first
        self._absorb(d, p)

        # ---- which shipment? ---------------------------------------------------
        sid, ask = self._resolve_shipment(st, p, now)
        if ask is not None:
            times |= getattr(self, "_extra_times", set())
            turn.clarification = ask[0] == "ask"
            turn.actions.append("identify")
            if ask[0] == "cancelled":
                st["state"] = "CLOSED"
            return ask[1]
        sh = self.conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (sid,)).fetchone()
        if d.get("eta_kind") == "RELATIVE_DELAY" and d.get("delay_min") and d.get("delay_ref") != sid:
            # "late by 45 min" is measured from the original plan of THIS shipment
            d["eta"] = iso(parse(sh["original_eta_ts"]) + minutes(d["delay_min"]))
            d["delay_ref"] = sid
            d["eta_new"] = True
        intents = p.intents or (d.pop("pending_intents", None) or ["UNKNOWN"])
        if st["state"] == "NEED_SHIPMENT":
            intents = d.pop("pending_intents", intents)
            st["state"] = "NEW"
            if turn.intent in ("UNKNOWN", "SELECT_OPTION"):
                turn.intent = "IDENTIFY_SHIPMENT"

        # ---- choose an offered option ---------------------------------------
        if st["state"] == "OPTIONS_SHOWN" and ("SELECT_OPTION" in intents or "CONFIRM" in intents):
            choice = p.option_choice
            if choice is None and p.option_time:
                for i, o in enumerate(d.get("shown", []), 1):
                    if o["start"] == hhmm(p.option_time) or o["window"].startswith(hhmm(p.option_time)):
                        choice = i
            if choice is None and "CONFIRM" in intents and len(d.get("shown", [])) == 1:
                choice = 1
            check_action(st["state"], "book")
            check_option_choice(choice, d.get("shown", []))
            return self._book(st, sh, choice, now, turn, times, ic)

        if st["state"] == "OPTIONS_SHOWN" and "DECLINE" in intents and p.eta is None:
            d["shown"] = []
            st["state"] = "NEED_ETA"
            turn.actions.append("decline")
            return tpl.render("declined", lang)

        if "CHECK_STATUS" in intents and p.eta is None and "ASK_OPTIONS" not in intents:
            turn.actions.append("status")
            return self._status(sh, lang, times)

        if "EARLY_ARRIVAL" in intents:
            turn.actions.append("early")
            return self._early(st, sh, now, turn, times)

        if set(intents) & {"REPORT_DELAY", "ASK_OPTIONS", "ADD_CONSTRAINT", "IDENTIFY_SHIPMENT", "SELECT_OPTION"}:
            return self._options(st, sh, p, now, turn, times)

        return self._unknown_or_pick(st, p, lang, turn)

    # ============================================================ helpers ==
    def _absorb(self, d: dict, p: ParseResult):
        if p.eta:
            d["eta"] = iso(p.eta)
            d["eta_conf"] = p.eta_confidence
            d["eta_kind"] = p.eta_kind
            d["delay_min"] = p.delay_min
            d["eta_window"] = [iso(p.eta_window[0]), iso(p.eta_window[1])] if p.eta_window else None
            d["eta_reason"] = p.delay_reason
            d["eta_new"] = True
        if "ETA_VAGUE" in p.clarify:
            d["eta_vague"] = True
        if p.earliest_start:
            d["earliest_start"] = iso(p.earliest_start)
        if p.latest_gate_out:
            d["latest_gate_out"] = iso(p.latest_gate_out)
            d["gateout_new"] = True
        if "PRIORITY_CLAIM" in p.flags:
            d["priority_claim"] = True

    @traced("find_driver_shipments", "READ", summarise=lambda o: o[0] or (o[1][0] if o[1] else "none"))
    def _resolve_shipment(self, st, p: ParseResult, now):
        d, lang = st["data"], st["lang"]
        rows = self.conn.execute(
            "SELECT * FROM shipments WHERE driver_id=? ORDER BY original_eta_ts", (st["driver_id"],)).fetchall()
        active = [r for r in rows if r["current_status"] not in ("COMPLETED", "CANCELLED")]

        def match(ref: str):
            for r in rows:
                if ref in (r["shipment_id"], r["order_reference"]) or r["order_reference"].endswith("-" + ref):
                    return r
            return None

        picked = None
        for ref in p.shipment_refs + p.order_suffixes:
            picked = match(ref.upper()) or picked
        if picked is None and st["state"] == "NEED_SHIPMENT":
            cands = d.get("candidates", [])
            if p.option_choice and 1 <= p.option_choice <= len(cands):
                picked = match(cands[p.option_choice - 1])
        if picked is None and st["state"] == "NEED_SHIPMENT":
            cands = d.get("candidates", [])
            t = normalise(p.text)
            import re as _re
            if _re.search(r"\b(later|latter|last|baad wala|baad wali|shaam wala|evening)\b", t) and cands:
                picked = match(cands[-1])
            elif _re.search(r"\b(earlier|morning|pehle wala|pehle wali|subah wala|current)\b", t) and cands:
                picked = match(cands[0])
        if picked is not None:
            d["shipment_id"] = picked["shipment_id"]
        at_fac = {r["shipment_id"]: r["gate_in_ts"] for r in self.conn.execute(
            "SELECT shipment_id, gate_in_ts FROM facility_checkins WHERE gate_in_ts IS NOT NULL AND gate_out_ts IS NULL")}
        if d.get("shipment_id") is None and len(active) > 1:
            # Load-type words in the message ("heavy trailer", "reefer") that match exactly one
            # live shipment identify it; the assumption is stated back to the driver.
            t = normalise(p.text)
            import re as _re
            kinds = {"HEAVY": bool(_re.search(r"\b(heavy|bhari|bhaari|trailer)\b|भारी|हेवी", t)),
                     "REEFER": bool(_re.search(r"\b(reefer|cold|thanda|refrigerat\w*)\b|रीफर|ठंडा", t))}
            for kind, said in kinds.items():
                if not said:
                    continue
                hit = [r for r in active if self._load_kind(r) == kind]
                if len(hit) == 1:
                    d["shipment_id"] = hit[0]["shipment_id"]
                    d["assumed_trip"] = kind
        if d.get("shipment_id") is None and len(active) > 1:
            # Deterministic narrowing with evidence, never a guess between equals:
            if "EARLY_ARRIVAL" in p.intents or "CHECK_STATUS" in p.intents:
                here = [r for r in active if r["shipment_id"] in at_fac]
                if len(here) == 1:
                    d["shipment_id"] = here[0]["shipment_id"]
            elif set(p.intents) & {"REPORT_DELAY", "ASK_OPTIONS"}:
                moving = [r for r in active if r["current_status"] == "IN_TRANSIT"]
                if len(moving) == 1:
                    d["shipment_id"] = moving[0]["shipment_id"]
        if d.get("shipment_id") is None:
            if len(active) == 1:
                d["shipment_id"] = active[0]["shipment_id"]
            elif len(active) > 1:
                d["candidates"] = [r["shipment_id"] for r in active]
                d["pending_intents"] = [i for i in p.intents if i != "SELECT_OPTION"]
                st["state"] = "NEED_SHIPMENT"
                d["clarifications"] = d.get("clarifications", 0) + 1
                def tag(r):
                    bits = []
                    if self._load_kind(r) != "STANDARD":
                        bits.append(tpl.word(tpl.TRIP_TAG, self._load_kind(r), lang))
                    if r["shipment_id"] in at_fac:
                        bits.append(tpl.word(tpl.TRIP_TAG, "AT_DC", lang).format(t=hhmm(parse(at_fac[r["shipment_id"]]))))
                    else:
                        bits.append(f"{'ETA' if lang != 'hi' else 'ईटीए'} {hhmm(parse(r['original_eta_ts']))}")
                    return ", ".join(bits)
                trips = "\n".join(f"{i}) {r['order_reference']} ({r['origin_city']} → {self._fac_name(r)}, {tag(r)})"
                                  for i, r in enumerate(active, 1))
                self._extra_times = {hhmm(parse(r["original_eta_ts"])) for r in active} | \
                    {hhmm(parse(at_fac[r["shipment_id"]])) for r in active if r["shipment_id"] in at_fac}
                return None, ("ask", tpl.render("ask_shipment", lang, n=len(active), trips=trips))
            else:
                cancelled = [r for r in rows if r["current_status"] == "CANCELLED"]
                if cancelled:
                    d["shipment_id"] = cancelled[-1]["shipment_id"]
        sh = self.conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (d.get("shipment_id"),)).fetchone()
        if sh is None:
            return None, ("info", tpl.render("status_none", lang))
        if sh["current_status"] == "CANCELLED":
            return None, ("cancelled", tpl.render("cancelled_shipment", lang))
        return sh["shipment_id"], None

    @staticmethod
    def _load_kind(sh) -> str:
        if sh["temperature_control_required"]:
            return "REEFER"
        return "HEAVY" if sh["required_dock_type"] == "HEAVY" else "STANDARD"

    def _fac_name(self, sh) -> str:
        r = self.conn.execute("SELECT city FROM facilities WHERE facility_id=?", (sh["destination_facility_id"],)).fetchone()
        return r["city"] if r else sh["destination_facility_id"]

    def _current_appt(self, shipment_id):
        return self.conn.execute("""
            SELECT a.*, sl.slot_start_ts, sl.slot_end_ts, sl.dock_id, dk.dock_code
            FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN docks dk ON dk.dock_id=sl.dock_id
            WHERE a.shipment_id=? AND a.is_current=1
              AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""",
                                 (shipment_id,)).fetchone()

    def _reference_eta(self, shipment_id):
        """'Late by X' is measured against the original plan."""
        if not shipment_id:
            return None
        s = self.conn.execute("SELECT original_eta_ts FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
        return parse(s["original_eta_ts"]) if s else None

    def _eta_estimate(self, d, sh) -> EtaEstimate | None:
        sys_eta = resolve_eta(self.conn, sh["shipment_id"])
        if sys_eta.at_facility:
            return sys_eta
        if d.get("eta"):
            w = d.get("eta_window")
            return EtaEstimate(point=parse(d["eta"]), confidence=d.get("eta_conf") or "MEDIUM", source="DRIVER_CHAT",
                               earliest=parse(w[0]) if w else None, latest=parse(w[1]) if w else None,
                               evidence=["driver message in this thread"])
        return None

    def _constraints(self, d) -> Constraints:
        return Constraints(earliest_start=parse(d.get("earliest_start")),
                           latest_gate_out=parse(d.get("latest_gate_out")))

    # --------------------------------------------------------- options -----
    def _options(self, st, sh, p: ParseResult, now, turn: Turn, times: set) -> str:
        d, lang = st["data"], st["lang"]
        check_action(st["state"] if st["state"] in ("NEW", "NEED_ETA", "OPTIONS_SHOWN", "CONFIRMED",
                                                    "PENDING_WAREHOUSE", "CLOSED") else "NEW", "options")
        fac = self._fac_name(sh)
        eta = self._eta_estimate(d, sh)
        ask_only = "ASK_OPTIONS" in p.intents and "REPORT_DELAY" not in p.intents
        if eta is None and ask_only:
            eta = resolve_eta(self.conn, sh["shipment_id"])
        if eta is None:
            d["clarifications"] = d.get("clarifications", 0) + 1
            if d["clarifications"] > config.MAX_CLARIFICATION_QUESTIONS:
                esc = self._escalate(st, "CLARIFICATION_LIMIT", now, {"last_message": p.text})
                turn.escalation = esc
                st["state"] = "ESCALATED"
                return tpl.render("human", lang, role=tpl.word(tpl.ROLE_NAMES, esc["routed_to"]["role"], lang))
            st["state"] = "NEED_ETA"
            turn.clarification = True
            turn.actions.append("ask_eta")
            return tpl.render("ask_eta_vague" if d.get("eta_vague") else "ask_eta", lang, facility=fac)

        if d.pop("eta_new", False) and eta.source == "DRIVER_CHAT":
            self._record_eta(st, sh, eta, d, now)
        cons = self._constraints(d)
        with self.tel.span("feasibility", shipment_id=sh["shipment_id"]) as sp:
            evals, eta = evaluate_all(self.conn, sh["shipment_id"], now, cons, eta)
            opts = rank_options(evals)
            sp.update(candidates=len(evals), feasible=sum(e.feasible for e in evals), shown=len(opts),
                      planning_eta=hhmm(eta.planning_eta), rejections=summarise_rejections(evals))
        look = {}
        if config.OPTION_RANKING == "solver" and opts:
            # Deterministic look-ahead: score each slot this driver could take by the
            # optimal plan for every other truck still needing a dock (solver.py).
            with self.tel.span("solver_rank", shipment_id=sh["shipment_id"]) as sp:
                feas = sorted((e for e in evals if e.feasible), key=lambda e: (e.planned_start, e.dock_code))
                ranked = rank_for_driver(self.conn, sh["shipment_id"], now, eta, cons, [e.slot_id for e in feas])
                by_id = {e.slot_id: e for e in feas}
                if ranked:
                    # Fairness rule: do not offer a slot that leaves another truck with no dock
                    # today when this driver has options that strand nobody extra. Then show
                    # distinct start times only, so the driver gets real choices.
                    least = min(len(r.others_unscheduled) for r in ranked)
                    keep, seen = [], set()
                    for r in ranked:
                        k = hhmm(by_id[r.slot_id].planned_start)
                        if len(r.others_unscheduled) == least and k not in seen:
                            seen.add(k)
                            keep.append(r)
                    opts = [by_id[r.slot_id] for r in keep[: config.MAX_OPTIONS_SHOWN]]
                    first = min(o.planned_start for o in opts)
                    d["protected_earlier"] = any(len(r.others_unscheduled) > least and
                                                 by_id[r.slot_id].planned_start < first for r in ranked)
                    look = {r.slot_id: r for r in ranked}
                sp.update(evaluated=len(ranked), ranking=[f"{r.dock_code} {hhmm(r.start)} cost={r.facility_cost:.0f}"
                                                          f" strands={r.others_unscheduled}" for r in ranked[:5]])
            self.tel.metric("SolverRanked")
        turn.actions.append("options")

        # Is the current booking still good? Then do not churn the schedule.
        cur = self._current_appt(sh["shipment_id"])
        if cur and cur["appointment_status"] in ("CONFIRMED", "PENDING_CONFIRMATION") and not d.get("earliest_start"):
            mine = next((e for e in evals if e.slot_id == cur["slot_id"]), None)
            if mine and mine.feasible and not mine.needs_approval:
                times |= {hhmm(parse(cur["slot_start_ts"])), hhmm(parse(cur["slot_end_ts"])),
                          hhmm(eta.point), hhmm(mine.planned_start)}
                st["state"] = "CONFIRMED" if cur["appointment_status"] == "CONFIRMED" else "PENDING_WAREHOUSE"
                pre = ""
                base = self._reference_eta(sh["shipment_id"])
                if d.get("eta_kind") == "RELATIVE_DELAY" and d.get("delay_min") and base:
                    pre = tpl.render("assume_relative", lang, delay=d["delay_min"], eta=hhmm(eta.point), base=hhmm(base))
                    times.add(hhmm(base))
                return pre + tpl.render("current_ok", lang, status=tpl.word(tpl.STATUS_NAME, cur["appointment_status"], lang),
                                  window=f"{hhmm(parse(cur['slot_start_ts']))}-"
                                  f"{hhmm(parse(cur['slot_end_ts']))}", dock=cur["dock_code"],
                                  eta=hhmm(eta.point), start=hhmm(mine.planned_start))

        if not opts:
            rej = summarise_rejections([e for e in evals if "DOCK_TYPE" not in e.reasons and "WEIGHT" not in e.reasons])
            top = next(iter(rej), "DEFAULT")
            esc = self._escalate(st, "NO_FEASIBLE_SLOT", now, {
                "shipment": dict(sh), "eta": eta.as_dict(), "constraints": cons.as_dict(),
                "rejection_summary": rej}, tried=summarise_tried(evals),
                recommendation=self._no_slot_reco(sh, eta))
            turn.escalation = esc
            st["state"] = "ESCALATED"
            self._set_exception_status(st, "ESCALATED")
            times |= {hhmm(eta.point)}
            dock_type = "REEFER" if sh["temperature_control_required"] else sh["required_dock_type"]
            why = tpl.word(tpl.WHY, top, lang)
            blocked = [e for e in evals if "SLOT_BLOCKED" in e.reasons and "DOCK_TYPE" not in e.reasons
                       and "WEIGHT" not in e.reasons and e.slot_end > eta.planning_eta]
            if blocked:      # say which dock and when, in clock times the driver can check
                b0, b1 = min(e.slot_start for e in blocked), max(e.slot_end for e in blocked)
                why += f" ({blocked[0].dock_code} {hhmm(b0)}-{hhmm(b1)})"
                times |= {hhmm(b0), hhmm(b1)}
            return tpl.render("no_slot", lang, dock_type=tpl.word(tpl.DOCK_TYPE_NAME, dock_type, lang),
                              eta=hhmm(eta.point), why=why,
                              role=tpl.word(tpl.ROLE_NAMES, esc["routed_to"]["role"], lang))

        d["shown"] = [{"slot_id": o.slot_id, "dock": o.dock_code, "start": hhmm(o.planned_start),
                       "end": hhmm(o.planned_end), "window": f"{hhmm(o.slot_start)}-{hhmm(o.slot_end)}",
                       "needs_approval": o.needs_approval,
                       "facility_cost": round(look[o.slot_id].facility_cost) if o.slot_id in look else None,
                       "strands": look[o.slot_id].others_unscheduled if o.slot_id in look else []} for o in opts]
        d["shown_at"] = iso(now)
        st["state"] = "OPTIONS_SHOWN"
        turn.options = d["shown"]
        self._set_exception_status(st, "SLOT_OPTIONS_SHARED")
        self.tel.metric("OptionsShown", len(opts))

        assumption = ""
        if d.get("eta_kind") == "RELATIVE_DELAY" and d.get("delay_min") and self._reference_eta(sh["shipment_id"]):
            base = self._reference_eta(sh["shipment_id"])
            assumption = tpl.render("assume_delay", lang, base=hhmm(base), delay=d["delay_min"])
            times.add(hhmm(base))
        if eta.planning_eta != eta.point and not eta.at_facility:
            assumption += tpl.render("assume_low", lang, planning=hhmm(eta.planning_eta))
        assumption = assumption.replace(") (", "; ")          # one bracket, not two in a row
        times |= {hhmm(eta.point), hhmm(eta.planning_eta)}
        lines = []
        if d.pop("assumed_trip", None):
            lines.append(tpl.render("assume_trip", lang, what=tpl.word(tpl.TRIP_TAG, self._load_kind(sh), lang),
                                    order=sh["order_reference"], route=f"{sh['origin_city']} → {fac}"))
        if cur and cur["appointment_status"] in ("CONFIRMED", "PENDING_CONFIRMATION"):
            mine = next((e for e in evals if e.slot_id == cur["slot_id"]), None)
            if (mine is None or not mine.feasible) and d.get("missed_told") != cur["appointment_id"]:
                d["missed_told"] = cur["appointment_id"]      # say it once per booking, not every turn
                w = f"{hhmm(parse(cur['slot_start_ts']))}-{hhmm(parse(cur['slot_end_ts']))}"
                lines.append(tpl.render("current_missed", lang, window=w, dock=cur["dock_code"]))
                times |= set(w.split("-"))
        lines.append(tpl.render("options_head", lang, eta=hhmm(eta.point), assumption=assumption, facility=fac))
        for i, o in enumerate(d["shown"], 1):
            flag = tpl.render("flag_approval", lang) if o["needs_approval"] else ""
            lines.append(tpl.render("option_line", lang, n=i, dock=o["dock"], start=o["start"], end=o["end"], flag=flag))
            times |= {o["start"], o["end"]}
        if cons.latest_gate_out and d.pop("gateout_new", False):
            lines.append(tpl.render("assume_gateout", lang, t=hhmm(cons.latest_gate_out)))
            times.add(hhmm(cons.latest_gate_out))
        if d.get("priority_claim"):
            lines.append(tpl.render("priority_note", lang))
        if d.pop("protected_earlier", False):
            lines.append(tpl.render("protect_note", lang))
        starts = [o["start"] for o in d["shown"]]
        if len(starts) > 1 and starts[0] != min(starts):
            lines.append(tpl.render("order_note", lang))
        lines.append(tpl.render("options_tail", lang))
        return "\n".join(lines)

    def _no_slot_reco(self, sh, eta) -> str:
        if sh["temperature_control_required"]:
            return ("Options for a person to decide: (a) approve reefer unload after maintenance window, "
                    "(b) next-morning first reefer slot with cold-chain check, (c) divert to another reefer facility.")
        return "Consider next-day first slot, overtime approval, or a manual swap with a lower-priority booking."

    # ----------------------------------------------------------- booking ---
    def _book(self, st, sh, choice: int, now, turn: Turn, times: set, ic) -> str:
        d, lang = st["data"], st["lang"]
        opt = d["shown"][choice - 1]
        eta = self._eta_estimate(d, sh) or resolve_eta(self.conn, sh["shipment_id"])
        cons = self._constraints(d)
        approval = []
        if eta.confidence == "LOW" and eta.latest is None and not eta.at_facility:
            approval.append("LOW_CONFIDENCE_ETA")
        if self._data_conflict(sh):
            approval.append("DATA_CONFLICT")
        idem = "chat:" + hashlib.sha1(f"{st['thread_id']}|{opt['slot_id']}|{d.get('shown_at')}".encode()).hexdigest()[:16]
        with self.tel.span("commit", slot_id=opt["slot_id"], shipment_id=sh["shipment_id"]) as sp:
            try:
                check_action(st["state"], "book")
                hold = place_hold(self.conn, sh["shipment_id"], opt["slot_id"], now, st["thread_id"], cons, eta)
                self.tel.metric("HoldPlaced")
                res = commit_hold(self.conn, hold["hold_id"], now, idem, approval, cons, eta)
                sp["result"] = res["status"]
            except CapacityError as ce:
                sp["result"] = ce.code
                self.tel.metric("OptionGone", reason=ce.code)
                turn.actions.append(f"book_failed:{ce.code}")
                times |= {opt["start"]}
                gone = tpl.render("option_gone", lang, dock=opt["dock"], start=opt["start"])
                d["shown"] = []
                st["state"] = "NEED_ETA"
                fresh = self._options(st, sh, ParseResult(text="", lang=lang, intents=["ASK_OPTIONS"]), now, turn, times)
                return gone + "\n" + fresh
        turn.committed = res
        turn.actions.append(f"book:{res['status']}")
        self.tel.metric("BookingCommitted", status=res["status"])
        d["appointment_id"] = res["appointment_id"]
        times |= {hhmm(parse(res["planned_start"]))}
        moved = ""
        new_start = hhmm(parse(res["planned_start"]))
        if abs(diff_min(parse(res["planned_start"]), parse(f"{res['planned_start'][:11]}{opt['start']}:00{res['planned_start'][19:]}"))) >= 5:
            moved = tpl.render("time_changed", lang, old=opt["start"], new=new_start)
            times |= {opt["start"], new_start}
        if res["status"] == "CONFIRMED":
            st["state"] = "CONFIRMED"
            self._set_exception_status(st, "RESOLVED")
            self._notify(sh, res, now, "INTERNAL", "Dock booking confirmed via driver chat")
            released = moved
            if res.get("replaced"):
                old = self.conn.execute("SELECT sl.slot_start_ts, sl.slot_end_ts FROM appointments a JOIN "
                                        "appointment_slots sl USING(slot_id) WHERE appointment_id=?",
                                        (res["replaced"],)).fetchone()
                ow = f"{hhmm(parse(old['slot_start_ts']))}-{hhmm(parse(old['slot_end_ts']))}"
                released = moved + tpl.render("released_old", lang, old=ow)
                times |= set(ow.split("-"))
            sh_eta = self._eta_estimate(d, sh)
            if sh_eta and sh_eta.at_facility:
                return tpl.render("booked_yard", lang, dock=res["dock"], start=hhmm(parse(res["planned_start"])),
                                  released=released)
            report_by = parse(res["planned_start"]) - minutes(config.GATE_TO_DOCK_MIN)
            times.add(hhmm(report_by))
            return tpl.render("booked", lang, dock=res["dock"], start=hhmm(parse(res["planned_start"])),
                              released=released, report_by=hhmm(report_by))
        st["state"] = "PENDING_WAREHOUSE"
        self._set_exception_status(st, "WAITING_CONFIRMATION")
        self._notify(sh, res, now, "EMAIL", "Approval needed: " + ", ".join(res["approval_reasons"]))
        why = ", ".join(tpl.word(tpl.APPROVAL_WHY, r, lang) for r in res["approval_reasons"])
        if "LATE_START_RULE" in res["approval_reasons"]:
            times.add("21:00")
        return tpl.render("pending", lang, dock=res["dock"], start=hhmm(parse(res["planned_start"])), why=why,
                          ttl=int((pending_deadline(now, parse(res["planned_start"])) - now).total_seconds() // 60))

    def _data_conflict(self, sh) -> bool:
        # Same driver on two trucks in transit at once, or a vehicle shared by two live shipments.
        n_drv = self.conn.execute("SELECT COUNT(*) FROM shipments WHERE driver_id=? AND current_status='IN_TRANSIT' "
                                  "AND vehicle_id<>?", (sh["driver_id"], sh["vehicle_id"])).fetchone()[0]
        n_veh = self.conn.execute("SELECT COUNT(*) FROM shipments WHERE vehicle_id=? AND shipment_id<>? AND "
                                  "current_status IN ('IN_TRANSIT','WAITING','IN_DOCK','AT_GATE')",
                                  (sh["vehicle_id"], sh["shipment_id"])).fetchone()[0]
        return bool(n_drv or n_veh)

    # ------------------------------------------------------------ status ---
    @traced("get_booking_status", "READ", arg_names=("shipment",), summarise=lambda o: "status reply built")
    def _status(self, sh, lang, times: set) -> str:
        cur = self._current_appt(sh["shipment_id"])
        if not cur:
            return tpl.render("status_none", lang)
        w = f"{hhmm(parse(cur['slot_start_ts']))}-{hhmm(parse(cur['slot_end_ts']))}"
        times |= set(w.split("-"))
        q = self.conn.execute("SELECT queue_position FROM facility_checkins WHERE shipment_id=?",
                              (sh["shipment_id"],)).fetchone()
        queue = tpl.render("status_queue", lang, pos=q["queue_position"]) if q and q["queue_position"] else ""
        return tpl.render("status", lang, dock=cur["dock_code"], window=w,
                          status=tpl.word(tpl.STATUS_NAME, cur["appointment_status"], lang), queue=queue)

    def _early(self, st, sh, now, turn, times: set) -> str:
        d, lang = st["data"], st["lang"]
        cur = self._current_appt(sh["shipment_id"])
        evals, eta = evaluate_all(self.conn, sh["shipment_id"], now, self._constraints(d))
        cur_start = parse(cur["slot_start_ts"]) if cur else None
        better = [e for e in rank_options(evals, 5) if cur_start is None or e.planned_start < cur_start]
        if not better:
            w = f"{hhmm(parse(cur['slot_start_ts']))}-{hhmm(parse(cur['slot_end_ts']))}" if cur else "-"
            times |= set(w.split("-"))
            return tpl.render("early_none", lang, window=w)
        o = better[0]
        d["shown"] = [{"slot_id": o.slot_id, "dock": o.dock_code, "start": hhmm(o.planned_start),
                       "end": hhmm(o.planned_end), "window": f"{hhmm(o.slot_start)}-{hhmm(o.slot_end)}",
                       "needs_approval": o.needs_approval}]
        d["shown_at"] = iso(now)
        st["state"] = "OPTIONS_SHOWN"
        turn.options = d["shown"]
        times |= {hhmm(o.planned_start), hhmm(o.planned_end)}
        return "\n".join([tpl.render("early_offer_head", lang),
                          tpl.render("option_line", lang, n=1, dock=o.dock_code, start=hhmm(o.planned_start),
                                     end=hhmm(o.planned_end), flag=""),
                          tpl.render("options_tail", lang)])

    def _unknown_or_pick(self, st, p, lang, turn) -> str:
        shown = st["data"].get("shown") or []
        if st["state"] == "OPTIONS_SHOWN" and shown and (p.option_choice or "CONFIRM" in p.intents):
            return tpl.render("pick_valid", lang, nums=", ".join(str(i) for i in range(1, len(shown) + 1)))
        d = st["data"]
        d["clarifications"] = d.get("clarifications", 0) + 1
        turn.clarification = True
        return tpl.render("unknown", lang)

    # ------------------------------------------------------- persistence ---
    def _is_duplicate(self, driver_id, text, now, external_id) -> bool:
        key = hashlib.sha1(f"{driver_id}|{normalise(text)}".encode()).hexdigest()[:20]
        with write_txn(self.conn):
            if external_id and self.conn.execute(
                    "SELECT 1 FROM chat_messages WHERE external_message_id=?", (external_id,)).fetchone():
                return True
            if len(normalise(text).split()) < 4:
                return False  # "1", "haan": short answers repeat legitimately; only exact-id dedupe applies
            row = self.conn.execute("SELECT * FROM inbound_dedupe WHERE dedupe_key=?", (key,)).fetchone()
            if row and parse(row["first_seen_at"]) > now - minutes(config.DUPLICATE_WINDOW_MIN):
                self.conn.execute("UPDATE inbound_dedupe SET hit_count=hit_count+1 WHERE dedupe_key=?", (key,))
                return True
            self.conn.execute("INSERT OR REPLACE INTO inbound_dedupe VALUES (?,?,?,1)", (key, external_id, iso(now)))
            return False

    def _load(self, thread_id, driver_id, now) -> dict:
        r = self.conn.execute("SELECT * FROM agent_conversations WHERE thread_id=?", (thread_id,)).fetchone()
        if r:
            return {"thread_id": thread_id, "driver_id": r["driver_id"], "state": r["state"], "lang": r["lang"],
                    "data": json.loads(r["data_json"]), "version": r["version"], "new": False}
        ct = self.conn.execute("SELECT * FROM chat_threads WHERE thread_id=?", (thread_id,)).fetchone()
        data = {"shipment_id": ct["shipment_id"]} if ct and ct["shipment_id"] else {}
        return {"thread_id": thread_id, "driver_id": driver_id, "state": "NEW", "lang": "en", "data": data,
                "version": 0, "new": True}

    def _save(self, st, now):
        with write_txn(self.conn):
            if st["new"]:
                if not self.conn.execute("SELECT 1 FROM chat_threads WHERE thread_id=?", (st["thread_id"],)).fetchone():
                    self.conn.execute("INSERT INTO chat_threads VALUES (?,?,?,?,NULL,'OPEN','UNKNOWN')",
                                      (st["thread_id"], st["driver_id"], st["data"].get("shipment_id"), iso(now)))
                self.conn.execute("INSERT INTO agent_conversations VALUES (?,?,?,?,?,1,?)",
                                  (st["thread_id"], st["driver_id"], st["state"], st["lang"],
                                   json.dumps(st["data"], default=str), iso(now)))
                st["new"], st["version"] = False, 1
            else:
                n = self.conn.execute(
                    "UPDATE agent_conversations SET state=?, lang=?, data_json=?, version=version+1, updated_at=? "
                    "WHERE thread_id=? AND version=?",
                    (st["state"], st["lang"], json.dumps(st["data"], default=str), iso(now), st["thread_id"],
                     st["version"])).rowcount
                if n != 1:
                    raise RuntimeError("conversation state changed concurrently (optimistic lock)")
                st["version"] += 1
            tstatus = {"ESCALATED": "ESCALATED", "CONFIRMED": "RESOLVED", "PENDING_WAREHOUSE": "WAITING_FOR_WAREHOUSE",
                       "NEED_SHIPMENT": "WAITING_FOR_DRIVER", "NEED_ETA": "WAITING_FOR_DRIVER",
                       "OPTIONS_SHOWN": "WAITING_FOR_DRIVER", "CLOSED": "CLOSED"}.get(st["state"], "OPEN")
            self.conn.execute("UPDATE chat_threads SET thread_status=?, shipment_id=COALESCE(shipment_id, ?) "
                              "WHERE thread_id=?", (tstatus, st["data"].get("shipment_id"), st["thread_id"]))

    def _log_msg(self, thread_id, driver_id, sender, text, now, external_id=None, intent=None, eta=None,
                 review=False, dup=False):
        with write_txn(self.conn):
            if not self.conn.execute("SELECT 1 FROM chat_threads WHERE thread_id=?", (thread_id,)).fetchone():
                self.conn.execute("INSERT INTO chat_threads VALUES (?,?,NULL,?,NULL,'OPEN','UNKNOWN')",
                                  (thread_id, driver_id, iso(now)))
            self.conn.execute(
                "INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"MSG-{uuid.uuid4().hex[:8].upper()}", thread_id, sender, driver_id if sender == "DRIVER" else "agent",
                 text, iso(now), external_id if not dup else None, int(dup), intent, iso(eta), int(review)))

    @traced("record_eta", "WRITE", arg_names=("", "shipment", "eta"), summarise=lambda o: "eta_updates row written")
    def _record_eta(self, st, sh, eta: EtaEstimate, d, now):
        reason = d.get("eta_reason")
        with write_txn(self.conn):
            self.conn.execute(
                "INSERT INTO eta_updates VALUES (?,?, 'DRIVER_DECLARED', ?,?,?,?,?,?)",
                (f"ETA-{uuid.uuid4().hex[:8].upper()}", sh["shipment_id"], st["driver_id"], iso(eta.point),
                 eta.confidence, reason or "OTHER", "via driver chat", iso(now)))
            dk = f"{st['driver_id']}-{sh['shipment_id']}-{hhmm(eta.point)}"
            if not self.conn.execute("SELECT 1 FROM driver_exceptions WHERE dedupe_key=?", (dk,)).fetchone():
                etype = reason if reason in ("TRAFFIC", "BREAKDOWN", "WEATHER") else "DELAY"
                ref = self._reference_eta(sh["shipment_id"])
                delay = max(0, int((eta.point - ref).total_seconds() // 60)) if ref else d.get("delay_min")
                sev = {"CRITICAL": "CRITICAL", "HIGH": "HIGH"}.get(sh["priority_code"], "MEDIUM")
                exc_id = f"EXC-{uuid.uuid4().hex[:8].upper()}"
                self.conn.execute(
                    "INSERT INTO driver_exceptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (exc_id, sh["shipment_id"], st["driver_id"], st["thread_id"], etype, iso(now), delay,
                     iso(eta.point), d.get("earliest_start"), d.get("latest_gate_out"), sev, "OPEN",
                     "Driver-reported ETA change (chat)", dk))
                d["exception_id"] = exc_id

    def _set_exception_status(self, st, status):
        eid = st["data"].get("exception_id")
        if eid:
            with write_txn(self.conn):
                self.conn.execute("UPDATE driver_exceptions SET exception_status=? WHERE exception_id=?", (status, eid))

    @traced("notify_warehouse", "WRITE", arg_names=("shipment", "", "", "channel", "subject"), summarise=lambda o: "operational_messages row queued")
    def _notify(self, sh, res, now, channel, subject):
        with write_txn(self.conn):
            self.conn.execute(
                "INSERT INTO operational_messages VALUES (?,?,?,?,?,?,?,?,?, 'QUEUED', NULL)",
                (f"OM-{uuid.uuid4().hex[:8].upper()}", sh["shipment_id"], res["appointment_id"], channel,
                 "driver-agent@setuhaul.example", "warehouse-coordinator", subject, json.dumps(res), iso(now)))

    def _escalate(self, st, reason, now, facts, tried=None, recommendation="") -> dict:
        sid = st["data"].get("shipment_id")
        fac = "FAC-JAI-01"
        if sid:
            r = self.conn.execute("SELECT destination_facility_id FROM shipments WHERE shipment_id=?", (sid,)).fetchone()
            fac = r["destination_facility_id"] if r else fac
        with write_txn(self.conn):
            h = escalate(self.conn, reason=reason, thread_id=st["thread_id"], shipment_id=sid, facility_id=fac,
                         now=now, facts=facts, tried=tried, recommendation=recommendation)
        self.tel.metric("Escalation", reason=reason)
        return h

    def _merge_fallback(self, p: ParseResult, fb: dict, now) -> ParseResult:
        def t(v):
            if not v:
                return None
            hh, mm = map(int, v.split(":"))
            return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        p.intents = [fb["intent"]] if fb["intent"] != "UNKNOWN" else []
        p.eta = t(fb.get("eta_hhmm")) or p.eta
        if p.eta:
            p.eta_confidence, p.eta_kind = "MEDIUM", "CLOCK"
        p.earliest_start = t(fb.get("earliest_start_hhmm")) or p.earliest_start
        p.latest_gate_out = t(fb.get("latest_gate_out_hhmm")) or p.latest_gate_out
        p.option_choice = fb.get("option_number") or p.option_choice
        p.flags.append("LLM_FALLBACK")
        return p
