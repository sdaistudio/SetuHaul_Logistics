"""Stage 1: give the coordinator more power with a scheduling algorithm.

The SetuHaul coordinator (control tower) opens the console for one facility:

  1. sees the day: every dock, every slot, protected work, dock events, every inbound truck
     and its status against its booking (facility_day.snapshot);
  2. presses "find slots": the exact solver (branch and bound over real slots, rules.yaml
     cost, proven optimal or best-within-budget with a gap) re-plans ALL open trucks at once,
     and the same snapshot is scored with simple dispatch rules (FCFS, EDD, weighted) so the
     coordinator sees what the algorithm buys;
  3. gets one action card per truck that changes: what, why, cost, which approval it needs,
     and the messages for driver, warehouse coordinator, dispatcher and customer;
  4. approves cards; approved cards are booked through the capacity layer (hold -> confirm,
     revalidated inside a write transaction, idempotent), then the facility is verified by an
     independent checker.

Nothing here lets a model choose a slot. The solver proposes; a person (or, in Stage 2, a
policy gate written in rules.yaml) decides what is executed.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import comms, config, database as D
from .capacity import CapacityError, book
from .eta import resolve_eta
from .feasibility import Constraints
from .rules import R
from .scheduler import compare
from .solver import build_problem, solve
from .timeutil import diff_min, hhmm, minutes, parse

ROLE = {"en": "warehouse coordinator", "hi": "वेयरहाउस कोऑर्डिनेटर", "hinglish": "warehouse coordinator"}


@dataclass
class Action:
    shipment: str
    kind: str            # REBOOK | NEW | YARD | MOVE_PROMISE | ESCALATE
    priority: str
    driver: str
    driver_id: str
    phone: str
    carrier: str
    customer: str
    order: str
    product: str
    from_: str | None
    to: str | None
    slot_id: str | None
    start: str | None
    end: str | None
    cost: float
    why: list[str]
    approval: list[str] = field(default_factory=list)    # reasons a person must approve
    eta_conf: str = ""
    lang: str = "en"
    depends_on: list[str] = field(default_factory=list)   # trucks that must move first (their slot / dock time)
    blocked_by_approval: list[str] = field(default_factory=list)  # dependencies that need a person
    messages: dict = field(default_factory=dict)
    guard: list[str] = field(default_factory=list)
    executed: dict | None = None

    def as_dict(self):
        d = asdict(self)
        d["from"] = d.pop("from_")
        return d


def driver_language(conn, driver_id: str) -> str:
    r = conn.execute("""SELECT m.message_text FROM chat_messages m JOIN chat_threads t USING(thread_id)
                        WHERE t.driver_id=? AND m.sender_type='DRIVER' ORDER BY m.message_ts DESC LIMIT 1""",
                     (driver_id,)).fetchone()
    return comms.detect(r["message_text"]) if r else "en"


def data_conflict(conn, sh) -> bool:
    n_drv = conn.execute("SELECT COUNT(*) FROM shipments WHERE driver_id=? AND current_status='IN_TRANSIT' AND vehicle_id<>?",
                         (sh["driver_id"], sh["vehicle_id"])).fetchone()[0]
    n_veh = conn.execute("SELECT COUNT(*) FROM shipments WHERE vehicle_id=? AND shipment_id<>? AND current_status IN "
                         "('IN_TRANSIT','WAITING','IN_DOCK','AT_GATE')", (sh["vehicle_id"], sh["shipment_id"])).fetchone()[0]
    return bool(n_drv or n_veh)


def plan(conn, facility_id: str, now: datetime, scope: str = "repair", budget_ms: int | None = None,
         with_baselines: bool = True) -> dict:
    """Run the solver and turn the result into action cards.

    scope="repair"   every booking that still works is LOCKED; only trucks that need a slot
                     (missed / no booking / window passed) are placed. Safe to automate.
    scope="improve"  every open truck may move (moving a working booking costs `move_kept_promise`);
                     shows what a full re-plan would gain. Every promise move needs a person.
    """
    prob = build_problem(conn, facility_id, now, "chat" if scope == "repair" else "plan")
    # deterministic budget: node count, not wall-clock time
    sol = solve(prob, budget_ms or 10 ** 9, node_limit=R()["solver"]["replan_node_limit"])
    fac = conn.execute("SELECT city FROM facilities WHERE facility_id=?", (facility_id,)).fetchone()["city"]
    last_new = prob.fs.last_new_start
    actions = []
    for t in prob.trucks:
        p = sol.placements.get(t.sid)
        sh = conn.execute("""SELECT s.*, d.driver_name, d.phone, c.carrier_name FROM shipments s JOIN drivers d USING(driver_id)
                             JOIN carriers c USING(carrier_id) WHERE shipment_id=?""", (t.sid,)).fetchone()
        cur = D.one(conn, "current_appointment", t.sid)
        from .solver import booking_dock_down
        if p and cur and p.slot_id == cur["slot_id"] and not booking_dock_down(conn, t.sid):
            continue                                          # booking kept: nothing to do
        # same slot but its dock is partly down: re-book in place so the new start time is recorded
        eta = resolve_eta(conn, t.sid)
        frm = f"{cur['dock_code']} {hhmm(parse(cur['slot_start_ts']))}-{hhmm(parse(cur['slot_end_ts']))}" if cur else None
        if p is None or p.slot_id is None:
            kind = "ESCALATE"
        elif eta.at_facility:
            kind = "YARD"
        elif cur is None:
            kind = "NEW"
        elif t.promise_reachable:
            kind = "MOVE_PROMISE"
        else:
            kind = "REBOOK"
        a = Action(t.sid, kind, t.priority, sh["driver_name"], sh["driver_id"], sh["phone"], sh["carrier_name"],
                   sh["customer_name"], sh["order_reference"], sh["product_category"], frm,
                   f"{p.dock_code} {hhmm(p.start)}" if p and p.slot_id else None, p.slot_id if p else None,
                   hhmm(p.start) if p and p.start else None, hhmm(p.end) if p and p.end else None,
                   round(p.cost if p else 0), _reasons(conn, t.sid, p, cur),
                   eta_conf=eta.confidence, lang=driver_language(conn, sh["driver_id"]))
        if kind == "MOVE_PROMISE":
            a.approval.append("moves a booking that still works (promise)")
        if kind == "ESCALATE":
            a.approval.append("no compatible dock time today: a person decides")
        if p and p.start and last_new and p.start > last_new:
            a.approval.append(f"starts after {hhmm(last_new)} (RULE005)")
        if eta.confidence == "LOW" and not eta.at_facility:
            a.approval.append("arrival time is uncertain (LOW confidence)")
        if data_conflict(conn, sh):
            a.approval.append("shipment data conflict (driver or truck on two live trips)")
        _draft(conn, a, fac, executed=False)
        actions.append(a)
    _dependencies(conn, actions)
    order = {"ESCALATE": 0, "MOVE_PROMISE": 1, "REBOOK": 2, "NEW": 3, "YARD": 4}
    actions.sort(key=lambda a: (order[a.kind], -config.PRIORITY_WEIGHT[a.priority], a.start or "99"))
    baselines = [{"policy": r.policy, "metrics": {k: v for k, v in r.metrics.items() if k != "swap_proposals"}}
                 for r in compare(conn, facility_id, now)] if with_baselines else []
    return {"facility": facility_id, "at": hhmm(now), "scope": scope,
            "solver": {"status": sol.status, "cost": round(sol.cost),
            "lower_bound": round(sol.lower_bound), "nodes": sol.nodes, "ms": round(sol.ms), "trucks": sol.trucks},
            "objective": R()["costs"], "weights": R()["priority_weight"], "baselines": baselines,
            "actions": [a for a in actions],
            "kept": [r["shipment_id"] for r in conn.execute(
                """SELECT a.shipment_id FROM appointments a JOIN shipments s USING(shipment_id)
                   WHERE s.destination_facility_id=? AND a.is_current=1
                   AND a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION')""", (facility_id,))
                if r["shipment_id"] not in {a.shipment for a in actions}]}


def _reasons(conn, sid, p, cur) -> list[str]:
    from .solver import booking_dock_down
    out = []
    down = booking_dock_down(conn, sid) if cur else None
    if down:
        out.append(f"current booking unusable: {down}")
    elif cur:
        out.append("current booking can no longer be reached")
    else:
        out.append("has no booking")
    return out + (p.reasons if p else ["no compatible dock time today"])


def status_quo_cost(conn, facility_id: str, now: datetime) -> float:
    """Cost (same rules.yaml cost function) of keeping every current booking, with only the
    trucks that need a slot placed: the yardstick for 'is a full re-plan worth it?'."""
    rep = build_problem(conn, facility_id, now, "plan")
    pinned = {}
    for t in rep.trucks:
        cur = D.one(conn, "current_appointment", t.sid)
        if cur and t.promise_reachable:
            pinned[t.sid] = cur["slot_id"]
    sol = solve(rep, 10 ** 9, node_limit=R()["solver"]["replan_node_limit"], pinned=pinned)
    return sol.cost


def _dependencies(conn, actions: list[Action]):
    """An action can only be booked once every truck now occupying its target dock time has moved.
    If one of those moves needs a person, this action waits for that approval."""
    moving = {a.shipment: a for a in actions}
    cur = {}
    for a in actions:
        r = D.one(conn, "current_appointment", a.shipment)
        if r:
            cur[a.shipment] = (r["dock_code"], parse(r["slot_start_ts"]), parse(r["slot_end_ts"]), r["slot_id"])
    for a in actions:
        if not a.slot_id:
            continue
        dock = a.to.split()[0]
        st = parse(f"2026-08-04T{a.start}:00+05:30")
        en = parse(f"2026-08-04T{a.end}:00+05:30")
        for sid, (d, s0, s1, slot) in cur.items():
            if sid != a.shipment and (slot == a.slot_id or (d == dock and s0 < en and st < s1)):
                a.depends_on.append(sid)
    def blockers(a, seen):
        out = []
        for sid in a.depends_on:
            if sid in seen or sid not in moving:
                continue
            seen.add(sid)
            b = moving[sid]
            if b.approval:
                out.append(sid)
            out += blockers(b, seen)
        return out
    for a in actions:
        a.blocked_by_approval = sorted(set(blockers(a, {a.shipment})))


def approval_groups(actions: list[Action]) -> dict[str, list[str]]:
    """Approving a card also releases the cards that only waited for it."""
    groups = {}
    for a in actions:
        if a.approval:
            groups[a.shipment] = [b.shipment for b in actions if a.shipment in b.blocked_by_approval and not b.approval]
    return groups


def auto_ok(a: Action) -> bool:
    """Executable without a person: no own approval and nothing it depends on needs one."""
    return a.kind != "ESCALATE" and not a.approval and not a.blocked_by_approval


def _draft(conn, a: Action, fac: str, executed: bool):
    """Messages for each party, each checked by the grounding guard."""
    times = {x for x in (a.start, a.end) if x}
    docks = set()
    gate = None
    if a.start:
        st = parse(f"2026-08-04T{a.start}:00+05:30")
        gate = hhmm(st - minutes(config.GATE_TO_DOCK_MIN))
        times.add(gate)
        docks.add(a.to.split()[0])
    if a.from_:
        docks.add(a.from_.split()[0])
        times |= set(a.from_.split()[1].split("-"))
    old = a.from_.split()[1] if a.from_ else ""
    # Driver messages are HELD until the booking is executed (or sent at once for an escalation),
    # so the guard checks them in that send context.
    kind = "ESCALATE" if a.kind == "ESCALATE" else {"YARD": "YARD", "NEW": "NEW"}.get(a.kind, "REBOOK")
    text, used, translate = comms.driver_message(kind, a.lang, fac=fac, old=old, dock=a.to.split()[0] if a.to else "",
                                                 start=a.start or "", gate=gate or "", role=ROLE.get(a.lang if a.lang in ROLE else "en"))
    a.messages = {"driver": {"channel": "WHATSAPP", "lang": used, "detected": comms.NAMES.get(a.lang, a.lang),
                             "needs_translation": translate, "text": text,
                             "send": "now" if kind == "ESCALATE" else ("sent" if executed else "held until the booking is made")}}
    planned = parse(f"2026-08-04T{a.start}:00+05:30") if a.start else None
    orig = conn.execute("SELECT original_eta_ts FROM shipments WHERE shipment_id=?", (a.shipment,)).fetchone()[0]
    late = diff_min(planned, parse(orig)) if planned else None
    if (late is None or late > R()["agent_autonomy"]["notify_customer_if_later_than_min"]) and a.kind != "YARD":
        cm = comms.customer_message(a.customer, fac, {"product": a.product, "order": a.order, "start": a.start or "a time to be confirmed"},
                                    late or 0) if planned else (f"{a.customer}: your inbound {a.product.lower()} ({a.order}) "
                                                               f"cannot be unloaded today as planned; our coordinator will contact you.")
        if planned:
            times.add(a.start)
        a.messages["customer"] = {"channel": "EMAIL", "text": cm}
    a.guard = []
    for party, m in a.messages.items():
        sent_after_booking = kind != "ESCALATE"
        v = comms.ground_check(m["text"], allowed_times=times, allowed_docks=docks,
                               executed=sent_after_booking, external=party == "customer")
        a.guard += [f"{party}:{x}" for x in v]


def party_messages(conn, facility_id: str, at: str, actions: list[Action]) -> dict:
    fac = conn.execute("SELECT city FROM facilities WHERE facility_id=?", (facility_id,)).fetchone()["city"]
    rows = [{"shipment": a.shipment, "priority": a.priority, "product": a.product, "order": a.order, "change": a.kind,
             "from": a.from_, "to": a.to, "approval": bool(a.approval)} for a in actions]
    by_carrier = {}
    for a, r in zip(actions, rows):
        by_carrier.setdefault(a.carrier, []).append(r)
    return {"warehouse": comms.warehouse_message(fac, at, rows),
            "dispatchers": {c: comms.dispatcher_message(c, fac, rs) for c, rs in by_carrier.items()}}


def execute(conn, actions: list[Action], now: datetime, actor: str, event_id: str) -> list[dict]:
    """Book approved actions. Multi-pass: a truck whose target slot is still held by another
    truck that is moving in this plan is retried after that truck has moved."""
    todo = [a for a in actions if a.slot_id and a.kind != "ESCALATE"]
    rank = {}
    def depth(a, seen=()):
        if a.shipment in rank:
            return rank[a.shipment]
        ids = {b.shipment: b for b in todo}
        d = 1 + max([depth(ids[x], seen + (a.shipment,)) for x in a.depends_on if x in ids and x not in seen] or [-1])
        rank[a.shipment] = d
        return d
    todo.sort(key=lambda a: depth(a))
    done = []
    for _ in range(len(todo) + 1):          # each pass books whatever is now free; dependencies first
        nxt = []
        for a in todo:
            idem = "plan:" + hashlib.sha1(f"{event_id}|{a.shipment}|{a.slot_id}".encode()).hexdigest()[:16]
            try:
                # book at the solver's planned start (not "earliest free"), so executing the plan one truck
                # at a time cannot hand one truck's dock time to another; feasibility is still re-checked
                pin = Constraints(earliest_start=parse(f"{now.date().isoformat()}T{a.start}:00+05:30"))
                res = book(conn, a.shipment, a.slot_id, now, idem_key=idem, actor=actor, cons=pin)
                a.executed = res
                fac = conn.execute("""SELECT f.city FROM shipments s JOIN facilities f ON f.facility_id=s.destination_facility_id
                                      WHERE shipment_id=?""", (a.shipment,)).fetchone()["city"]
                _draft(conn, a, fac, executed=True)
                done.append({"shipment": a.shipment, "ok": True, "status": res["status"], "dock": res["dock"],
                             "start": res["planned_start"][11:16], "replaced": res.get("replaced")})
            except CapacityError as ce:
                a.last_error = f"{ce.code}: {str(ce)[:100]}"
                nxt.append(a)
        if not nxt or len(nxt) == len(todo):
            todo = nxt
            break
        todo = nxt
    for a in todo:
        done.append({"shipment": a.shipment, "ok": False, "error": "NOT_BOOKABLE_NOW",
                     "detail": getattr(a, "last_error", "") + " (re-checked at commit; left for the coordinator)"})
    return done


def verify(conn, facility_id: str) -> list[str]:
    """Independent checker: the rules a correct dock plan must satisfy, re-derived from raw tables."""
    v = []
    rows = conn.execute("""SELECT a.appointment_id, a.shipment_id, a.slot_id, a.appointment_status, sl.dock_id, sl.slot_status,
                                  sl.slot_start_ts, sl.slot_end_ts, d.dock_type, d.supports_refrigerated, d.max_vehicle_weight_kg,
                                  s.load_weight_kg, s.required_dock_type, s.temperature_control_required, s.expected_unload_min,
                                  p.planned_start_ts, p.planned_end_ts
                           FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN docks d ON d.dock_id=sl.dock_id
                           JOIN shipments s USING(shipment_id) LEFT JOIN appointment_plan p USING(appointment_id)
                           WHERE sl.facility_id=? AND a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION')""",
                        (facility_id,)).fetchall()
    seen_slot, seen_ship = {}, {}
    for r in rows:
        if r["slot_id"] in seen_slot:
            v.append(f"DOUBLE_BOOKED_SLOT {r['slot_id']}: {seen_slot[r['slot_id']]} and {r['shipment_id']}")
        seen_slot[r["slot_id"]] = r["shipment_id"]
        if r["shipment_id"] in seen_ship:
            v.append(f"TWO_LIVE_BOOKINGS {r['shipment_id']}")
        seen_ship[r["shipment_id"]] = r["slot_id"]
        from .solver import booking_dock_down
        down = booking_dock_down(conn, r["shipment_id"])
        if down:
            v.append(f"BOOKED_ON_DOWN_DOCK {r['shipment_id']}: {down}")
        if r["planned_start_ts"] is None:
            continue                                    # seed bookings: compatibility checked by the data audit
        if r["temperature_control_required"] and not r["supports_refrigerated"]:
            v.append(f"REEFER_ON_NON_REEFER {r['shipment_id']}")
        if r["load_weight_kg"] > r["max_vehicle_weight_kg"]:
            v.append(f"OVERWEIGHT {r['shipment_id']} on {r['dock_id']}")
        if r["required_dock_type"] == "HEAVY" and r["dock_type"] != "HEAVY":
            v.append(f"HEAVY_ON_WRONG_DOCK {r['shipment_id']}")
        if r["slot_status"] != "OPEN":
            v.append(f"BLOCKED_SLOT {r['slot_id']}")
        if not (r["slot_start_ts"] <= r["planned_start_ts"] < r["slot_end_ts"]):
            v.append(f"START_OUTSIDE_WINDOW {r['shipment_id']}")
    ours = [r for r in rows if r["planned_start_ts"]]
    busy = []
    for e in conn.execute("SELECT e.* FROM dock_status_events e JOIN docks d USING(dock_id) WHERE d.facility_id=? "
                          "AND e.event_type IN ('BREAKDOWN','MAINTENANCE','BLOCKED')", (facility_id,)):
        busy.append((e["dock_id"], e["event_start_ts"], e["event_end_ts"] or "2026-08-04T23:59", f"event {e['dock_event_id']}"))
    for r in ours:
        busy.append((r["dock_id"], r["planned_start_ts"], r["planned_end_ts"], r["shipment_id"]))
    for i, a in enumerate(busy):
        for b in busy[i + 1:]:
            if a[0] == b[0] and a[1] < b[2] and b[1] < a[2] and not (a[3].startswith("event") and b[3].startswith("event")):
                if a[3] in {r["shipment_id"] for r in ours} or b[3] in {r["shipment_id"] for r in ours}:
                    v.append(f"OVERLAP on {a[0]}: {a[3]} {a[1][11:16]}-{a[2][11:16]} vs {b[3]} {b[1][11:16]}-{b[2][11:16]}")
    return v
