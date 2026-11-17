"""tool.py: the TOOL layer. Deterministic functions with structured inputs and outputs.

Rule of the system: the model understands and phrases; tools decide. Every
number a driver sees (dock, start time, window) comes out of a tool, and only
WRITE tools change capacity, always inside a database transaction.

Each tool has a contract (`SPECS`): name, READ/WRITE, who may call it, required
inputs, output fields, and the guardrail that applies. `call()` validates the
input against the contract and the conversation state before running it.

The production workflow (workflow.py) calls the same functions; they are
wrapped with `traced()` so every turn records which tools ran. The situation
walkthrough (tools/situation_walkthrough.py) prints that record per ladder level.

    READ  get_driver_context, find_driver_shipments, get_eta, check_slot_feasibility,
          rank_options_lookahead, get_booking_status, propose_facility_sequence
    WRITE record_eta, hold_slot, confirm_booking, release_hold, notify_warehouse,
          escalate_to_human, decide_pending_booking (human only)
"""
from __future__ import annotations

import contextvars
import functools
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import database as D
from .capacity import CapacityError, commit_hold, decide_pending, place_hold
from .escalation import escalate as _escalate
from .eta import EtaEstimate, resolve_eta
from .feasibility import Constraints, evaluate_all, rank_options, summarise_rejections
from .guardrails import ALLOWED_ACTIONS, GuardrailViolation
from .solver import rank_for_driver
from .timeutil import hhmm, iso

# ------------------------------------------------------------------ trace --
_TRACE: contextvars.ContextVar[list | None] = contextvars.ContextVar("tool_trace", default=None)


@dataclass
class CallRecord:
    layer: str          # MODEL | TOOL
    name: str
    kind: str           # READ | WRITE | LANGUAGE
    args: dict
    result: str
    ms: float
    ok: bool = True


@contextmanager
def capture():
    """Collect every MODEL/TOOL call made inside the block (per conversation turn)."""
    buf: list[CallRecord] = []
    tok = _TRACE.set(buf)
    try:
        yield buf
    finally:
        _TRACE.reset(tok)


def _short(v: Any) -> Any:
    if isinstance(v, datetime):
        return hhmm(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v if not isinstance(v, str) or len(v) < 60 else v[:57] + "..."
    if hasattr(v, "keys") and "shipment_id" in v.keys():
        return v["shipment_id"]
    if isinstance(v, EtaEstimate):
        return f"eta {hhmm(v.point)} ({v.confidence}, {v.source})"
    if isinstance(v, Constraints):
        return {k: hhmm(x) if isinstance(x, datetime) else x for k, x in v.as_dict().items() if x}
    return type(v).__name__


def traced(name: str, kind: str, layer: str = "TOOL", summarise: Callable[[Any], str] | None = None,
           arg_names: tuple[str, ...] = ()):
    """Decorator: record a call (name, key args, result summary, ms) when a capture is active.
    Behaviour of the wrapped function is unchanged."""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            buf = _TRACE.get()
            if buf is None:
                return fn(*a, **kw)
            t0 = time.perf_counter()
            args = {}
            pos = [x for x in a if not hasattr(x, "execute")]          # drop the connection
            if pos and type(pos[0]).__name__ == "DriverAgent":
                pos = pos[1:]
            for n, v in zip(arg_names, pos):
                if n:
                    args[n] = _short(v)
            for k, v in kw.items():
                if k in arg_names or k in ("reason", "approve"):
                    args[k] = _short(v)
            try:
                out = fn(*a, **kw)
            except Exception as e:  # recorded, then re-raised unchanged
                buf.append(CallRecord(layer, name, kind, args, f"{type(e).__name__}: {getattr(e, 'code', e)}",
                                      round((time.perf_counter() - t0) * 1000, 1), ok=False))
                raise
            buf.append(CallRecord(layer, name, kind, args, summarise(out) if summarise else _short(out),
                                  round((time.perf_counter() - t0) * 1000, 1)))
            return out
        return wrap
    return deco


# -------------------------------------------------------------- summaries --
def _s_eval(out) -> str:
    evals, eta = out
    feas = [e for e in evals if e.feasible]
    rej = summarise_rejections(evals)
    top = ", ".join(f"{k}x{v}" for k, v in list(rej.items())[:3])
    return f"{len(evals)} slots checked, {len(feas)} feasible; rejected: {top or 'none'}; planning ETA {hhmm(eta.planning_eta)}"


def _s_rank(out) -> str:
    return "; ".join(f"{o.dock_code} {hhmm(o.start)} board cost {o.facility_cost:.0f}"
                     + (f" strands {o.others_unscheduled}" if o.others_unscheduled else "") for o in out[:3]) or "nothing ranked"


def _s_hold(out) -> str:
    return f"{out['hold_id']} on {out['slot_id']} until {out['expires_at'][11:16]}"


def _s_commit(out) -> str:
    return f"{out['status']} {out.get('dock')} {out.get('planned_start', '')[11:16]}" + \
        (f" (replaces {out['replaced']})" if out.get("replaced") else "") + \
        (f" approval: {out['approval_reasons']}" if out.get("approval_reasons") else "") + \
        (" [idempotent replay]" if out.get("idempotent_replay") else "")


def _s_esc(out) -> str:
    r = out["routed_to"]
    return f"{out['escalation_id']} {out['reason']} -> {r['role']} {r['name']} via {r['channel']}" + \
        (f" (skipped {r['skipped']})" if r.get("skipped") else "")


# ---------------------------------------------------- traced primitives ----
# These are exactly the functions the workflow uses (workflow.py imports them from here).
get_eta = traced("get_eta", "READ", summarise=lambda e: _short(e), arg_names=("shipment_id",))(resolve_eta)
check_slot_feasibility = traced("check_slot_feasibility", "READ", summarise=_s_eval,
                                arg_names=("shipment_id", "now", "constraints", "eta"))(evaluate_all)
rank_options_lookahead = traced("rank_options_lookahead", "READ", summarise=_s_rank,
                                arg_names=("shipment_id", "now", "eta", "constraints"))(rank_for_driver)
hold_slot = traced("hold_slot", "WRITE", summarise=_s_hold, arg_names=("shipment_id", "slot_id", "now"))(place_hold)
confirm_booking = traced("confirm_booking", "WRITE", summarise=_s_commit,
                         arg_names=("hold_id", "now", "idem_key", "approval_reasons"))(commit_hold)
escalate_to_human = traced("escalate_to_human", "WRITE", summarise=_s_esc)(_escalate)
decide_pending_booking = traced("decide_pending_booking", "WRITE", summarise=_s_commit,
                                arg_names=("appointment_id", "approve", "approver"))(decide_pending)


def release_hold(conn, hold_id: str, now: datetime) -> dict:
    with D.write_txn(conn):
        n = conn.execute("UPDATE slot_holds SET hold_status='RELEASED', closed_at=? WHERE hold_id=? AND hold_status='HELD'",
                         (iso(now), hold_id)).rowcount
        D.audit(conn, iso(now), "tool", "HOLD_RELEASED", hold_id)
    return {"hold_id": hold_id, "released": bool(n)}


release_hold = traced("release_hold", "WRITE", arg_names=("hold_id",))(release_hold)


def get_driver_context(conn, driver_id: str, thread_id: str | None = None) -> dict:
    return {"driver": D.one(conn, "driver", driver_id),
            "open_shipments": D.q(conn, "driver_open_shipments", driver_id),
            "thread": D.one(conn, "thread", thread_id) if thread_id else None,
            "history": D.q(conn, "thread_messages", thread_id) if thread_id else []}


get_driver_context = traced("get_driver_context", "READ", arg_names=("driver_id", "thread_id"),
                            summarise=lambda o: f"{len(o['open_shipments'])} live shipment(s)")(get_driver_context)


def get_booking_status(conn, shipment_id: str) -> dict:
    sh = D.one(conn, "shipment", shipment_id)
    return {"shipment_status": sh["current_status"] if sh else None,
            "appointment": D.one(conn, "current_appointment", shipment_id),
            "checkin": D.one(conn, "checkin", shipment_id)}


get_booking_status = traced("get_booking_status", "READ", arg_names=("shipment_id",),
                            summarise=lambda o: f"{o['shipment_status']}, booking "
                            f"{(o['appointment'] or {}).get('appointment_status', 'none')}")(get_booking_status)


def propose_facility_sequence(conn, facility_id: str, now: datetime, trigger: str = "") -> dict:
    from .scheduling_tool import propose_sequence
    return propose_sequence(conn, facility_id, now, trigger).as_dict()


propose_facility_sequence = traced("propose_facility_sequence", "READ", arg_names=("facility_id", "now", "trigger"),
                                   summarise=lambda o: f"recommend {o['recommended']['policy']}: "
                                   f"{o['recommended']['metrics']}")(propose_facility_sequence)


# ---------------------------------------------------------------- contracts --
@dataclass
class ToolSpec:
    name: str
    kind: str                     # READ | WRITE
    caller: str                   # workflow | coordinator | scheduler
    purpose: str
    inputs: dict[str, str]
    output: str
    guard: str
    states: set[str] = field(default_factory=set)   # conversation states where the workflow may call it
    fn: Callable | None = None


ALL = set(ALLOWED_ACTIONS)
SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("get_driver_context", "READ", "workflow", "Who is writing, which shipments are live, thread history",
             {"driver_id": "str", "thread_id": "str?"}, "driver, open_shipments[], thread, history[]",
             "Driver can only see own shipments", ALL, get_driver_context),
    ToolSpec("get_eta", "READ", "workflow", "Best ETA: gate check-in > ops override > warehouse > driver > plan",
             {"shipment_id": "str"}, "EtaEstimate(point, confidence, source, window, evidence)",
             "LOW confidence adds a buffer; plan is MEDIUM not HIGH", ALL, get_eta),
    ToolSpec("check_slot_feasibility", "READ", "workflow", "Hard rules R1-R8 on every slot (dock type, reefer, weight, "
             "maintenance, occupancy, holds, 21:00 rule, driver constraints)",
             {"shipment_id": "str", "now": "datetime", "constraints": "Constraints?", "eta": "EtaEstimate?"},
             "SlotEval[] with feasible, reasons[], planned_start/end", "Read-only; asking never books",
             ALL - {"ESCALATED"}, check_slot_feasibility),
    ToolSpec("rank_options_lookahead", "READ", "workflow", "Order the driver's feasible slots by the optimal plan "
             "for every other truck (branch & bound look-ahead)",
             {"shipment_id": "str", "now": "datetime", "eta": "EtaEstimate", "constraints": "Constraints",
              "feasible_slot_ids": "list[str]"}, "RankedOption[] (facility cost, trucks stranded)",
             "Deterministic tie-break; time budget from rules.yaml", ALL - {"ESCALATED"}, rank_options_lookahead),
    ToolSpec("get_booking_status", "READ", "workflow", "Live booking, check-in, queue position",
             {"shipment_id": "str"}, "shipment_status, appointment, checkin", "-", ALL, get_booking_status),
    ToolSpec("record_eta", "WRITE", "workflow", "Store the driver's new ETA as an eta_updates row",
             {"shipment_id": "str", "eta": "datetime", "confidence": "HIGH|MEDIUM|LOW"}, "eta_update_id",
             "Only after the model's parse is validated; never overwrites plan", {"NEW", "NEED_ETA", "OPTIONS_SHOWN"}),
    ToolSpec("hold_slot", "WRITE", "workflow", "Reserve one slot for 5 minutes while the driver decides/commits",
             {"shipment_id": "str", "slot_id": "str (must be one of the options shown)", "now": "datetime"},
             "hold_id, expires_at", "Unique index: one live hold per slot and per shipment", {"OPTIONS_SHOWN"}, hold_slot),
    ToolSpec("confirm_booking", "WRITE", "workflow", "Re-check feasibility inside BEGIN IMMEDIATE, then write the "
             "appointment (CONFIRMED or PENDING_CONFIRMATION)",
             {"hold_id": "str", "idem_key": "str", "approval_reasons": "list[str]"},
             "appointment_id, status, dock, planned_start/end, replaced", "Idempotency key; revalidation; "
             "LOW-confidence ETA or data conflict -> PENDING, never auto-confirmed", {"OPTIONS_SHOWN"}, confirm_booking),
    ToolSpec("release_hold", "WRITE", "workflow", "Give a held slot back (driver declined / timeout)",
             {"hold_id": "str"}, "released", "-", ALL, release_hold),
    ToolSpec("notify_warehouse", "WRITE", "workflow", "Queue an internal/e-mail message about a booking",
             {"shipment_id": "str", "subject": "str"}, "operational_message_id", "Delivery status tracked (OM004 FAILED)",
             ALL),
    ToolSpec("escalate_to_human", "WRITE", "workflow", "Hand the case to a named person with facts, options tried "
             "and a recommendation", {"reason": "SAFETY|HUMAN_REQUESTED|NO_FEASIBLE_SLOT|CLARIFICATION_LIMIT|"
                                      "DATA_CONFLICT|GUARDRAIL_BLOCK|RATE_LIMIT", "facts": "dict"},
             "escalation_id, routed_to(role, name, channel), sla", "Vacant roles skipped; night shift after 18:00",
             ALL, escalate_to_human),
    ToolSpec("decide_pending_booking", "WRITE", "coordinator", "Approve/reject a PENDING booking (re-validated first)",
             {"appointment_id": "str", "approve": "bool", "approver": "str"}, "status",
             "Human only; approval of an infeasible slot is refused", set(), decide_pending_booking),
    ToolSpec("propose_facility_sequence", "READ", "scheduler", "Facility-level re-plan after an ETA update or gate "
             "check-in: compare dispatch rules with the optimal plan on one objective",
             {"facility_id": "str", "now": "datetime", "trigger": "str"},
             "Proposal(objective, protected work, policies[], recommended plan, moves vs today)",
             "Proposes only; a coordinator approves before any booking changes", ALL, propose_facility_sequence),
]}


class ToolError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code


def call(conn, name: str, state: str = "NEW", caller: str = "workflow", **args) -> dict:
    """Validated tool call: known tool, allowed caller, allowed in this state, required inputs present."""
    spec = SPECS.get(name)
    if spec is None or spec.fn is None:
        raise ToolError("UNKNOWN_TOOL", name)
    if spec.caller != caller and not (caller == "coordinator" and spec.kind == "READ"):
        raise ToolError("CALLER_NOT_ALLOWED", f"{caller} may not call {name}")
    if caller == "workflow" and state not in spec.states:
        raise GuardrailViolation("ACTION_NOT_ALLOWED", f"{name} in state {state}")
    missing = [k for k, t in spec.inputs.items() if not t.endswith("?") and k not in args
               and k not in ("constraints", "eta", "approval_reasons", "trigger", "thread_id")]
    if missing:
        raise ToolError("MISSING_INPUT", f"{name}: {missing}")
    if "constraints" in args:
        args["cons"] = args.pop("constraints")
    try:
        return {"ok": True, "tool": name, "kind": spec.kind, "data": spec.fn(conn, **args)}
    except CapacityError as ce:
        return {"ok": False, "tool": name, "kind": spec.kind, "error": ce.code, "detail": str(ce)}


# ---------------------------------------------------- convenience (tests) --
def list_slot_options(conn, shipment_id: str, now: datetime, constraints: Constraints | None = None,
                      eta: EtaEstimate | None = None, limit: int = 3) -> dict:
    """Read-only answer to 'what slots can I get?': feasibility + look-ahead order.
    Used by tests and the walkthrough to show that asking never writes."""
    cons = constraints or Constraints()
    evals, eta = check_slot_feasibility(conn, shipment_id, now, cons, eta)
    feas = sorted((e for e in evals if e.feasible), key=lambda e: (e.planned_start, e.dock_code))
    ranked = rank_options_lookahead(conn, shipment_id, now, eta, cons, [e.slot_id for e in feas]) if feas else []
    by = {e.slot_id: e for e in feas}
    order = [by[r.slot_id] for r in ranked] or rank_options(evals, limit)
    return {"eta": eta.as_dict(), "options": [{"slot_id": e.slot_id, "dock": e.dock_code, "start": hhmm(e.planned_start),
                                               "end": hhmm(e.planned_end), "needs_approval": e.needs_approval}
                                              for e in order[:limit]],
            "rejections": summarise_rejections(evals)}
