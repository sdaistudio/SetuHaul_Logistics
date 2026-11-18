"""Facility-level scheduling tool.

Reads structured data only, never free text. Proposes; never books.
The agent and the coordinator console call it as a controlled tool.

Mapping (from the brief): truck = job, dock = machine, planning ETA = release
time, unload + changeover = processing time, appointment window = due window,
truck already unloading or dock event = fixed task, priority = weight on waiting.

Objective, in plain words, in this order:
  1. every truck that can be served today gets a dock (fewest left without one)
  2. promises already made are kept (kept bookings are fixed; moving one is a coordinator decision)
  3. least priority-weighted waiting
  4. fewest changes to what drivers were already told
Hard rules are never traded for cost.

Policies compared: FCFS, EDD, WSPT, PRIORITY, PRIORITY+REPAIR (ours), and
BEST_ORDER (exhaustive search over job orders, used as a benchmark when n <= 8).
"""
import itertools, json
from . import config as C
from .clock import ts, iso, add, minutes_between, hhmm, ampm
from .db import one, rows, write_txn
from . import timeline as T
from .eta import resolve, ceil5

INF = 10 ** 9
ENGINE = "EXACT"      # engine of record: exact branch and bound, warm-started by PRIORITY+REPAIR (the first answer)


def _limits(con, sid, as_of):
    out = {}
    for r in rows(con, "SELECT * FROM driver_constraints WHERE shipment_id=? AND created_at<=? ORDER BY created_at", sid, iso(as_of)):
        out[r["constraint_type"]] = ts(r["limit_ts"])
    return out


def jobs(con, fid, as_of):
    """Trucks that need a dock decision: unbooked, certain-miss or invalid bookings."""
    tl = T.build(con, fid, as_of)
    J = []
    for s in rows(con, "SELECT * FROM shipments WHERE destination_facility_id=? AND current_status NOT IN ('COMPLETED','CANCELLED') ORDER BY shipment_id", fid):
        sid = s["shipment_id"]
        if sid in tl.in_dock:
            continue
        cur = one(con, f"""SELECT a.*, sl.slot_start_ts, sl.slot_end_ts FROM appointments a JOIN appointment_slots sl ON sl.slot_id=a.slot_id
                           WHERE a.shipment_id=? AND a.is_current=1 AND a.appointment_status IN {T.ACTIVE}""", sid)
        st = tl.booking_status.get(cur["appointment_id"]) if cur else None
        if cur and st and st["status"] in ("OK", "AT_RISK"):
            continue
        hold = one(con, "SELECT 1 FROM slot_holds WHERE shipment_id=? AND hold_status IN ('ACTIVE','PENDING_APPROVAL') AND expires_at>?", sid, iso(as_of))
        if hold:
            continue
        e = resolve(con, s, as_of)
        first_report = one(con, """SELECT MIN(created_at) AS t FROM eta_updates WHERE shipment_id=? AND source_type='DRIVER_DECLARED'
                                   AND created_at<=?""", sid, iso(as_of))["t"]
        J.append({"ship": s, "eta": e, "current": cur, "current_status": st and st["status"], "limits": _limits(con, sid, as_of),
                  "requested_at": ts(first_report) if first_report else e.release,
                  "earliest": _limits(con, sid, as_of).get("EARLIEST_START")})
    return tl, J


def _ncompat(con, j, tl, rules):
    return sum(1 for d in tl.docks.values() if not T.dock_compatible(j["ship"], d, rules))


def _context(con, fid, as_of, tl):
    """Everything list scheduling needs, built once: busy intervals, taken slots, open slots, limits."""
    fac = tl.facility
    rules = T.facility_rules(con, fid)
    ctx = {"rules": rules, "close_t": T.at_clock(as_of, fac["close_time"]),
           "last_start": T.at_clock(as_of, rules["LAST_NEW_START_TIME"]) if "LAST_NEW_START_TIME" in rules else None,
           "busy": {d: list(v) for d, v in tl.busy.items()}, "tl": tl}
    taken = {r["slot_id"] for r in rows(con, f"SELECT a.slot_id, a.appointment_id FROM appointments a WHERE a.appointment_status IN {T.ACTIVE}")
             if tl.booking_status.get(r["appointment_id"], {}).get("status") not in ("CERTAIN_MISS", "INVALID")}
    taken |= {r["slot_id"] for r in rows(con, "SELECT slot_id FROM slot_holds WHERE hold_status IN ('ACTIVE','PENDING_APPROVAL') AND expires_at>?", iso(as_of))}
    ctx["taken"] = taken
    ctx["slots"] = rows(con, "SELECT * FROM appointment_slots WHERE facility_id=? AND slot_status='OPEN' ORDER BY slot_start_ts", fid)
    return ctx


def _best(ctx, j, busy, taken):
    """Earliest feasible placement of one truck given the dock time already used (no side effects)."""
    s_, e = j["ship"], j["eta"]
    tl, rules = ctx["tl"], ctx["rules"]
    dur = s_["expected_unload_min"] + C.DOCK_CHANGEOVER_MIN
    best = None
    for sl in ctx["slots"]:
        if sl["slot_id"] in taken:
            continue
        dock = tl.docks[sl["dock_id"]]
        if T.dock_compatible(s_, dock, rules):
            continue
        ss, se = ts(sl["slot_start_ts"]), ts(sl["slot_end_ts"])
        if se <= e.release:
            continue
        s0 = max(ss, e.release, j["earliest"] or ss)
        st = T.earliest_fit(busy[sl["dock_id"]], s0, dur, latest_start=se)
        if st is None or add(st, s_["expected_unload_min"]) > ctx["close_t"]:
            continue
        lim = j["limits"]
        end = add(st, s_["expected_unload_min"])
        if ("LATEST_GATE_OUT" in lim and add(end, C.YARD_TO_DOCK_MIN) > lim["LATEST_GATE_OUT"]) or \
           ("LATEST_UNLOAD_END" in lim and end > lim["LATEST_UNLOAD_END"]) or ("LATEST_START" in lim and st > lim["LATEST_START"]):
            continue
        appr = bool(ctx["last_start"] and st >= ctx["last_start"])
        key = (appr, st, dock["dock_code"])
        if best is None or key < best[0]:
            best = (key, sl, dock, st, appr)
    return best


def _commit(j, best, busy, taken):
    _, sl, dock, st, appr = best
    s_ = j["ship"]
    dur = s_["expected_unload_min"] + C.DOCK_CHANGEOVER_MIN
    busy[sl["dock_id"]].append(T.Busy(st, add(st, dur), "PLANNED", s_["shipment_id"], s_["shipment_id"]))
    taken.add(sl["slot_id"])
    return {"slot_id": sl["slot_id"], "dock_code": dock["dock_code"], "start": st,
            "wait": max(0, minutes_between(j["eta"].release, st)), "approval": appr}


def schedule(con, fid, as_of, order, tl, weights=None, ctx=None):
    """List scheduling: take jobs in the given order; each gets its earliest feasible start."""
    ctx = ctx or _context(con, fid, as_of, tl)
    busy = {d: list(v) for d, v in ctx["busy"].items()}
    taken = set(ctx["taken"])
    plan = {}
    for j in order:
        best = _best(ctx, j, busy, taken)
        plan[j["ship"]["shipment_id"]] = _commit(j, best, busy, taken) if best else None
    return plan


def score(plan, J, weights=None):
    weights = weights or C.WAIT_WEIGHT
    un = [j for j in J if plan.get(j["ship"]["shipment_id"]) is None]
    ww = sum(weights[j["ship"]["priority_code"]] * plan[j["ship"]["shipment_id"]]["wait"] for j in J if plan.get(j["ship"]["shipment_id"]))
    changes = sum(1 for j in J if j["current"] and plan.get(j["ship"]["shipment_id"]) and plan[j["ship"]["shipment_id"]]["slot_id"] != j["current"]["slot_id"])
    unw = sum(C.PRIORITY_RANK[j["ship"]["priority_code"]] for j in un)
    return (unw, ww, changes)


POLICIES = ["FCFS", "EDD", "WSPT", "PRIORITY", "PRIORITY+REPAIR", "EXACT", "BEST_ORDER"]


def order_for(policy, J, con, tl, rules):
    pr = lambda j: -C.PRIORITY_RANK[j["ship"]["priority_code"]]
    sid = lambda j: j["ship"]["shipment_id"]
    if policy == "FCFS":
        return sorted(J, key=lambda j: (j["requested_at"], sid(j)))
    if policy == "EDD":
        due = lambda j: ts(j["current"]["slot_end_ts"]) if j["current"] else j["eta"].release
        return sorted(J, key=lambda j: (due(j), sid(j)))
    if policy == "WSPT":
        return sorted(J, key=lambda j: (-C.WAIT_WEIGHT[j["ship"]["priority_code"]] / j["ship"]["expected_unload_min"], sid(j)))
    # PRIORITY: priority, then most constrained (fewest compatible docks, a driver deadline), then release time
    return sorted(J, key=lambda j: (pr(j), _ncompat(con, j, tl, rules), 0 if j["limits"] else 1, j["eta"].release, sid(j)))


def exact(con, fid, as_of, weights=None, tl=None, J=None, node_limit=None):
    """Exact branch and bound over the order in which trucks are given their earliest feasible dock.

    Search space: every order of the trucks that need a decision (the same space BEST_ORDER
    enumerates, n!). Warm start: the PRIORITY+REPAIR plan, which is also the first answer shown
    to the coordinator. Bound: the score of the trucks already placed, plus for each remaining
    truck its best possible placement on the dock time still free (it can only get worse as more
    trucks are placed). Score order: (unscheduled priority weight, priority-weighted wait, changes).
    A branch whose bound is not better than the best plan found is cut. Node budget is a count,
    not a clock, so the result is reproducible. Returns PROVEN_OPTIMAL when the tree is exhausted.
    """
    weights = weights or C.WAIT_WEIGHT
    if tl is None:
        tl, J = jobs(con, fid, as_of)
    warm = plan_facility(con, fid, as_of, "PRIORITY+REPAIR", weights)
    ctx = _context(con, fid, as_of, tl)
    rules = ctx["rules"]
    base = order_for("PRIORITY", J, con, tl, rules)
    best = {"score": score(warm["plan"], J, weights), "plan": warm["plan"], "order": warm["order"]}
    limit = node_limit or C.EXACT_NODE_LIMIT
    nodes = 0
    exhausted = True

    def contrib(j, p):
        if p is None:
            return (C.PRIORITY_RANK[j["ship"]["priority_code"]], 0, 0)
        ch = 1 if j["current"] and p["slot_id"] != j["current"]["slot_id"] else 0
        return (0, weights[j["ship"]["priority_code"]] * p["wait"], ch)

    def dfs(rem, busy, taken, plan, sc, order):
        nonlocal nodes, exhausted
        if not rem:
            if sc < best["score"]:
                best.update(score=sc, plan=dict(plan), order=list(order))
            return
        # bound: each remaining truck alone on the dock time still free
        lb = list(sc)
        for j in rem:
            b = _best(ctx, j, busy, taken)
            c = contrib(j, None if b is None else {"slot_id": b[1]["slot_id"], "wait": max(0, minutes_between(j["eta"].release, b[3]))})
            lb[0] += c[0]
            lb[1] += c[1]
        if tuple(lb) >= best["score"]:
            return
        for i, j in enumerate(rem):
            if nodes >= limit:
                exhausted = False
                return
            nodes += 1
            b = _best(ctx, j, busy, taken)
            busy2 = {d: list(v) for d, v in busy.items()}
            taken2 = set(taken)
            p = _commit(j, b, busy2, taken2) if b else None
            c = contrib(j, p)
            sc2 = (sc[0] + c[0], sc[1] + c[1], sc[2] + c[2])
            if sc2 >= best["score"]:
                continue
            plan[j["ship"]["shipment_id"]] = p
            dfs(rem[:i] + rem[i + 1:], busy2, taken2, plan, sc2, order + [j["ship"]["shipment_id"]])
            del plan[j["ship"]["shipment_id"]]

    dfs(base, {d: list(v) for d, v in ctx["busy"].items()}, set(ctx["taken"]), {}, (0, 0, 0), [])
    sc = best["score"]
    status = "PROVEN_OPTIMAL" if exhausted else "BEST_WITHIN_BUDGET"
    improved = sc < score(warm["plan"], J, weights)
    note = (f"{status.lower().replace('_', ' ')} over all {len(J)}! truck orders: {nodes:,} nodes searched "
            f"(of {_fact(len(J)):,} orders); " + ("improved on" if improved else "confirmed") + " the priority + repair answer")
    res = {"policy": "EXACT", "as_of": iso(as_of), "jobs": J, "plan": best["plan"], "order": best["order"],
           "score": {"unscheduled_weight": sc[0], "weighted_wait": sc[1], "changes": sc[2]}, "note": note, "timeline": tl,
           "status": status, "nodes": nodes, "first_answer": "PRIORITY+REPAIR", "first_answer_score": warm["score"]}
    res["reasons"] = explain(res, rules)
    return res


def _fact(n):
    f = 1
    for k in range(2, n + 1):
        f *= k
    return f


def explain(res, rules):
    """One plain-language line per truck: when it is ready, where it goes, and why it waits."""
    tl, out = res["timeline"], {}
    plan = res["plan"]
    for j in res["jobs"]:
        s_ = j["ship"]
        sid = s_["shipment_id"]
        p = plan.get(sid)
        ok = [d["dock_code"] for d in tl.docks.values() if not T.dock_compatible(s_, d, rules)]
        fits = f"fits {', '.join(sorted(ok))}" if len(ok) < len(tl.docks) else "fits any dock"
        if p is None:
            out[sid] = f"ready {hhmm(j['eta'].release)}; {fits}; no compatible dock time left today, so a person decides"
            continue
        line = f"ready {hhmm(j['eta'].release)}; {fits}; gets {p['dock_code']} {hhmm(p['start'])}"
        if p["wait"] > 0:
            ahead = sorted({x for x, q in plan.items() if q and x != sid and q["dock_code"] in ok
                            and q["start"] < p["start"] and add(q["start"], 60) > j["eta"].release})
            fixed = sorted({(b.shipment_id or b.ref) for d in tl.docks.values() if d["dock_code"] in ok
                            for b in tl.busy.get(d["dock_id"], []) if b.start < p["start"] and b.end > j["eta"].release
                            and (b.shipment_id or "").startswith("SHP")})
            blocked = sorted({d["dock_code"] for d in tl.docks.values() if d["dock_code"] in ok
                              for b in tl.busy.get(d["dock_id"], []) if b.kind not in ("PLANNED",) and not (b.shipment_id or "").startswith("SHP")
                              and b.start < p["start"] and b.end > j["eta"].release})
            who = (ahead + [f for f in fixed if f not in ahead])[:3]
            why = []
            if who:
                why.append(f"{', '.join(who)} {'uses' if len(who) == 1 else 'use'} those docks first")
            if blocked:
                why.append(f"{', '.join(blocked)} {'is' if len(blocked) == 1 else 'are'} out of service")
            line += f"; waits {p['wait']} min" + (" because " + " and ".join(why) if why else "")
        if j["limits"]:
            line += "; meets the driver's own limit"
        out[sid] = line
    return out


def plan_facility(con, fid, as_of, policy=None, weights=None):
    policy = policy or ENGINE
    tl, J = jobs(con, fid, as_of)
    rules = T.facility_rules(con, fid)
    if policy == "EXACT":
        return exact(con, fid, as_of, weights, tl=tl, J=J)
    base = "PRIORITY" if policy in ("PRIORITY+REPAIR", "BEST_ORDER") else policy
    order = order_for(base, J, con, tl, rules)
    plan = schedule(con, fid, as_of, order, tl, weights)
    best = score(plan, J, weights)
    note = ""
    if policy == "PRIORITY+REPAIR":       # deterministic pairwise-swap repair until no improvement
        improved = True
        while improved:
            improved = False
            for i, k in itertools.combinations(range(len(order)), 2):
                o2 = list(order)
                o2[i], o2[k] = o2[k], o2[i]
                p2 = schedule(con, fid, as_of, o2, tl, weights)
                s2 = score(p2, J, weights)
                if s2 < best:
                    order, plan, best, improved = o2, p2, s2, True
                    break
    if policy == "BEST_ORDER":
        if len(J) <= 8:
            for perm in itertools.permutations(order):
                p2 = schedule(con, fid, as_of, list(perm), tl, weights)
                s2 = score(p2, J, weights)
                if s2 < best:
                    plan, best, order = p2, s2, list(perm)
            note = f"exhaustive over {len(J)}! orders"
        else:
            note = f"skipped: {len(J)} jobs > 8"
    return {"policy": policy, "as_of": iso(as_of), "jobs": J, "plan": plan, "order": [j["ship"]["shipment_id"] for j in order],
            "score": {"unscheduled_weight": best[0], "weighted_wait": best[1], "changes": best[2]}, "note": note, "timeline": tl}


def kpis(res):
    J, plan = res["jobs"], res["plan"]
    served = [j for j in J if plan.get(j["ship"]["shipment_id"])]
    waits = [plan[j["ship"]["shipment_id"]]["wait"] for j in served]
    crit = [plan[j["ship"]["shipment_id"]]["wait"] for j in served if j["ship"]["priority_code"] == "CRITICAL"]
    high = [plan[j["ship"]["shipment_id"]]["wait"] for j in served if j["ship"]["priority_code"] in ("CRITICAL", "HIGH")]
    return {"policy": res["policy"], "jobs": len(J), "served": len(served),
            "unscheduled": [j["ship"]["shipment_id"] for j in J if not plan.get(j["ship"]["shipment_id"])],
            "weighted_wait": res["score"]["weighted_wait"], "avg_wait": round(sum(waits) / len(waits), 1) if waits else 0,
            "critical_wait": max(crit) if crit else 0, "high_plus_max_wait": max(high) if high else 0,
            "approvals": sum(1 for v in plan.values() if v and v["approval"]), "note": res["note"]}


def compare(con, fid, as_of):
    out = []
    for p in POLICIES:
        r = plan_facility(con, fid, as_of, p)
        out.append((kpis(r), r))
    return out


def replan(con, fid, as_of):
    """Run the facility plan and protect capacity for HIGH/CRITICAL trucks with short-lived earmarks."""
    r = plan_facility(con, fid, as_of)
    run_id = f"PLAN-{as_of.strftime('%H%M%S')}"
    with write_txn(con):
        con.execute("DELETE FROM slot_earmarks WHERE slot_id IN (SELECT slot_id FROM appointment_slots WHERE facility_id=?)", (fid,))
        for j in r["jobs"]:
            p = r["plan"].get(j["ship"]["shipment_id"])
            if p and (C.PRIORITY_RANK[j["ship"]["priority_code"]] >= C.PRIORITY_RANK["HIGH"] or j["limits"]):
                con.execute("INSERT OR REPLACE INTO slot_earmarks VALUES (?,?,?,?,?)",
                            (p["slot_id"], j["ship"]["shipment_id"], j["ship"]["priority_code"], run_id, iso(add(as_of, C.EARMARK_TTL_MIN))))
    return r


def health(con, fid, as_of):
    """Booking health monitor: every current booking that is not OK, with a suggested action."""
    tl = T.build(con, fid, as_of)
    act = {"CERTAIN_MISS": "Release the slot (truck cannot check in within grace); tell dispatch; re-plan the truck.",
           "INVALID": "Booking breaks a hard rule; re-plan now and tell the driver before departure.",
           "AT_RISK": "Ask the driver to confirm arrival (proactive message); coordinator watches the dock."}
    return [dict(st, action=act[st["status"]]) for st in tl.booking_status.values() if st["status"] != "OK"]


def what_if(con, shipment_id, as_of):
    """For a no-slot case: why, and what a person could change to make it work."""
    ship = T.shipment(con, shipment_id)
    fid = ship["destination_facility_id"]
    fac = one(con, "SELECT * FROM facilities WHERE facility_id=?", fid)
    rules = T.facility_rules(con, fid)
    tl = T.build(con, fid, as_of, exclude=(shipment_id,))
    e = resolve(con, ship, as_of)
    close_t = T.at_clock(as_of, fac["close_time"])
    dur = ship["expected_unload_min"] + C.DOCK_CHANGEOVER_MIN
    why, recs, why_times, why_docks = [], [], [], []
    for d in tl.docks.values():
        if T.dock_compatible(ship, d, rules):
            continue
        for iv in tl.busy[d["dock_id"]]:
            if iv.kind == "DOCK_EVENT" and iv.end > e.release and iv.start < close_t:
                ev = one(con, "SELECT * FROM dock_status_events WHERE dock_event_id=?", iv.ref)
                label = (ev["event_type"].lower() if ev else "blocked")
                why.append(f"Dock {d['dock_code']} {label} {ampm(iv.start)}-{ampm(iv.end)}")
                why_times += [iv.start, iv.end]
                why_docks.append(d["dock_code"])
                need_end = add(e.release, dur)
                if ev and ev["event_type"] == "MAINTENANCE" and add(e.release, ship["expected_unload_min"]) <= close_t:
                    recs.append(f"Ask maintenance on Dock {d['dock_code']} to start at {ampm(need_end)} instead of {ampm(iv.start)}: "
                                f"the truck could unload {ampm(e.release)}-{ampm(add(e.release, ship['expected_unload_min']))}.")
    if "LAST_NEW_START_TIME" in rules:
        ls = T.at_clock(as_of, rules["LAST_NEW_START_TIME"])
        why.append(f"no new unloading starts after {ampm(ls)} without approval")
        why_times.append(ls)
    later = one(con, "SELECT MAX(slot_end_ts) AS m FROM appointment_slots WHERE facility_id=?", fid)["m"]
    if ts(later) <= close_t:
        recs.append("No slots are published for tomorrow (data gap F23). Park overnight in the yard"
                    + (" with the reefer unit running" if ship["temperature_control_required"] else "")
                    + ", take the first compatible slot tomorrow, and check the e-way bill is still valid (it can be extended within 8 hours of expiry).")
    for other in rows(con, "SELECT * FROM facilities WHERE facility_id<>?", fid):
        ok_docks = [d for d in rows(con, "SELECT * FROM docks WHERE facility_id=?", other["facility_id"])
                    if not T.dock_compatible(ship, d, T.facility_rules(con, other["facility_id"]))]
        if ok_docks:
            recs.append(f"Divert to {other['facility_name']} ({', '.join(d['dock_code'] for d in ok_docks)} accepts this load): "
                        f"it closes at {other['close_time']}; road time is not in the data, so check before offering.")
    if ship["required_dock_type"] in ("STANDARD", "ANY"):
        recs.append("Allow this standard load on an idle heavy or reefer dock (flex-dock policy, needs approval).")
    short = "; ".join(why[:2]) if why else "every compatible slot is taken or closed"
    return {"why": why, "why_short": short, "why_times": why_times, "why_docks": why_docks, "recommendations": recs}


def swap_proposal(con, sid, as_of, min_gain_min=30, notice_min=30, min_weighted_gain=100):
    """For a HIGH/CRITICAL truck that booked a late slot: is there a lower-priority booking
    whose slot would serve it earlier, at an acceptable cost? Proposes only."""
    from . import capacity as K
    ship = T.shipment(con, sid)
    fid = ship["destination_facility_id"]
    tl = T.build(con, fid, as_of)
    mine = K.current_appointment(con, sid)
    if not mine:
        return None
    my_start = ts(tl.booking_status.get(mine["appointment_id"], {}).get("planned_start") or mine["slot_start_ts"])
    rank = C.PRIORITY_RANK[ship["priority_code"]]
    best, rejected = None, []
    for st in tl.booking_status.values():
        if st["status"] != "OK" or st["shipment_id"] == sid:
            continue
        other = T.shipment(con, st["shipment_id"])
        chk = one(con, "SELECT gate_in_ts FROM facility_checkins WHERE shipment_id=? AND gate_in_ts<=?", other["shipment_id"], iso(as_of))
        if C.PRIORITY_RANK[other["priority_code"]] >= rank or chk:
            continue
        if ts(st["planned_start"]) >= add(my_start, -min_gain_min) or ts(st["planned_start"]) < add(as_of, notice_min):
            continue
        r = K.trial_swap(con, sid, other["shipment_id"], as_of)
        if not r or not r.get("ok"):
            rejected.append(f"{other['shipment_id']}: {(r or {}).get('why') or (r or {}).get('code')}")
            continue
        gain = minutes_between(ts(r["high"]["option"]["start"]), my_start)
        loss = minutes_between(ts(st["planned_start"]), ts(r["low"]["option"]["start"]))
        net = C.WAIT_WEIGHT[ship["priority_code"]] * gain - C.WAIT_WEIGHT[other["priority_code"]] * loss
        if gain >= min_gain_min and net >= min_weighted_gain and (best is None or net > best["weighted_gain"]):
            best = {"high": sid, "low": other["shipment_id"], "low_priority": other["priority_code"],
                    "high_from": hhmm(my_start), "high_to": hhmm(ts(r["high"]["option"]["start"])), "high_dock": r["high"]["option"]["dock_code"],
                    "low_from": hhmm(ts(st["planned_start"])), "low_to": hhmm(ts(r["low"]["option"]["start"])), "low_dock": r["low"]["option"]["dock_code"],
                    "high_gain_min": gain, "low_loss_min": loss, "weighted_gain": net,
                    "explain": f"{sid} ({ship['priority_code']}) starts {gain} min sooner; {other['shipment_id']} ({other['priority_code']}) "
                               f"starts {loss} min later; priority-weighted gain {net} (weights {C.WAIT_WEIGHT})."}
    return {"best": best, "rejected": rejected}
