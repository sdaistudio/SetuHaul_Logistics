"""model.py: the LANGUAGE layer. What "the model" is allowed to do, and nothing more.

The model handles words, never capacity:

  understand()  driver text (EN / Hindi / Hinglish) -> structured facts:
                intent(s), language, ETA (clock / relative / window) + confidence,
                driver constraints (earliest start, must leave by), option picked,
                shipment hints, what is still missing, and the evidence span for each.
                Primary: deterministic rule-based parser (nlu.py), same input -> same output.
                Fallback: a small LLM on Bedrock (temperature 0, forced JSON schema,
                PII-redacted input, Bedrock Guardrail) only when the parser returns
                UNKNOWN; its output is re-validated and then treated exactly like a parse.

  missing_facts() which facts are needed before any tool may be called
                ("ask before you call a tool": ladder level 2).

  phrase()      reviewed reply templates per language, filled ONLY with values
                returned by tools (dock, times, role); no free text generation.

  guard_reply() output guardrail: no claimed booking that a tool did not
                commit, no time that a tool did not produce, no internal ids.

The model may NOT: choose or rank a slot, call a WRITE tool, read the
database, change priority because a driver says "urgent", or invent a time.
Those belong to tool.py (deterministic) and to people (escalation).
"""
from __future__ import annotations

from datetime import datetime

from . import templates as _templates
from .guardrails import InputCheck, check_input, check_output
from .llm_fallback import NullFallback, validate
from .nlu import ParseResult, parse_message
from .tool import traced

MAY = ["detect language", "extract intent", "extract ETA / delay / window + confidence", "extract driver constraints",
       "extract option choice", "extract shipment hints", "list missing facts", "fill a reviewed template"]
MAY_NOT = ["pick or rank a slot", "call a WRITE tool", "read or write the database", "promise a booking",
           "change priority on request", "produce a time no tool returned", "decide an escalation outcome"]


def _s_parse(p: ParseResult) -> str:
    bits = [p.lang, "/".join(p.intents) or "UNKNOWN"]
    if p.eta:
        bits.append(f"eta {p.eta:%H:%M} {p.eta_kind} {p.eta_confidence}")
    if p.delay_min:
        bits.append(f"delay {p.delay_min}m")
    if p.earliest_start:
        bits.append(f"not before {p.earliest_start:%H:%M}")
    if p.latest_gate_out:
        bits.append(f"leave by {p.latest_gate_out:%H:%M}")
    if p.option_choice:
        bits.append(f"option {p.option_choice}")
    if p.clarify:
        bits.append(f"clarify {p.clarify}")
    return ", ".join(bits)


# traced so each turn shows the model step next to the tool steps
understand_text = traced("understand", "LANGUAGE", layer="MODEL", summarise=_s_parse, arg_names=("text",))(parse_message)


def screen_input(text: str) -> InputCheck:
    """Input guardrail before the model sees anything: length cap, control chars,
    prompt-injection phrases flagged, PII redacted for logs/fallback."""
    return check_input(text)


def understand(text: str, now: datetime, reference_eta: datetime | None = None, fallback=None) -> ParseResult:
    ic = screen_input(text)
    p = understand_text(ic.text, now, reference_eta)
    if not p.understood():
        fb = validate((fallback or NullFallback()).parse(ic.redacted) or {})
        if fb:
            p.intents = [fb["intent"]]
            p.evidence.append("llm_fallback (validated)")
    return p


def missing_facts(p: ParseResult, n_live_shipments: int, has_shipment_ref: bool) -> list[str]:
    """Facts that must be known before a feasibility tool is called."""
    need = []
    if n_live_shipments > 1 and not has_shipment_ref:
        need.append("which_shipment")                         # THR010 / DRV004 has two trips
    if ("REPORT_DELAY" in p.intents and p.eta is None and p.delay_min is None) or "ETA_VAGUE" in p.clarify:
        need.append("arrival_time")                           # "thoda late ho jaunga"
    # "late by one hour" (THR003) is NOT a missing fact: it is anchored to the
    # shipment's planned ETA and the assumption is stated back to the driver.
    return need


class _Templates:
    """Template access for the workflow: `render` is traced as the model's phrasing step."""
    def __getattr__(self, n):
        return getattr(_templates, n)

    render = staticmethod(traced("phrase", "LANGUAGE", layer="MODEL", arg_names=("template", "lang"),
                                 summarise=lambda s: s.replace("\n", " ")[:70])(_templates.render))


tpl = _Templates()
phrase = tpl.render


def guard_reply(reply: str, *, committed_status: str | None, allowed_times: set[str], lang: str) -> list[str]:
    return check_output(reply, committed_status=committed_status, allowed_times=allowed_times, lang=lang)
