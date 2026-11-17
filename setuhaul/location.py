"""Location on demand: look only when it can change a decision.

Two deterministic mechanisms, both configured in rules.yaml `location`:

1. Value of information (VOI). For every truck whose ETA is uncertain, solve
   the facility twice: once with the truck at the early edge of its ETA, once
   at the late edge. If the optimal plan is the same, a position fix is worth
   nothing: do not buy it. If the plan changes and the cost difference is
   large enough, buy ONE fix, from the first source in the ladder that can
   answer (GPS -> FASTag -> driver-shared WhatsApp location), within the
   daily budget. This is the same idea used by ride-hailing and fleet apps
   that cut map/GPS bills: compute locally, call paid APIs only on triggers.

2. Toll-checkpoint watchdog. Trucks on tolled corridors cross FASTag plazas
   at predictable intervals. If the next expected crossing is overdue beyond
   tolerance, the ETA is downgraded to LOW. If GPS is also silent (dead zone,
   or device switched off), the case goes to ops review: possible breakdown,
   dead zone or toll bypass. The system never accuses a driver; a person looks.

Common case first: most trucks never trigger either rule and cost nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .eta import EtaEstimate, resolve_eta
from .rules import R, ROOT
from .solver import build_problem, solve
from .timeutil import diff_min, hhmm, minutes, parse


@dataclass
class WatchdogSignal:
    shipment_id: str
    last_toll: str
    next_toll: str
    expected_by: datetime
    overdue_min: int
    gps_silent_min: int
    action: str           # OK | DOWNGRADE_ETA | OPS_REVIEW


def _corridors() -> dict:
    import yaml
    f = ROOT / R()["location"]["toll_watchdog"]["corridor_file"]
    return yaml.safe_load(Path(f).read_text()) if Path(f).exists() else {"corridors": {}, "signals": {}}


def toll_watchdog(now: datetime) -> list[WatchdogSignal]:
    cfg = R()["location"]
    tw = cfg["toll_watchdog"]
    data = _corridors()
    out = []
    for sid, sig in sorted(data.get("signals", {}).items()):
        plazas = data["corridors"][sig["corridor"]]["plazas"]
        names = [p["name"] for p in plazas]
        i = names.index(sig["last_toll"])
        if i + 1 >= len(plazas):
            continue
        seg = plazas[i + 1]["minutes_from_prev"]
        expected = parse(sig["last_toll_ts"]) + minutes(seg)
        tol = max(tw["overdue_min_floor"], seg * tw["overdue_tolerance_pct"] / 100)
        overdue = max(0, diff_min(now, expected + minutes(tol)))
        gps_silent = max(0, diff_min(now, parse(sig["gps_last_fix_ts"]))) if sig.get("gps_last_fix_ts") else 9999
        silent = gps_silent >= cfg["gps_stale_after_min"]
        action = "OK"
        if overdue:
            action = "OPS_REVIEW" if silent else "DOWNGRADE_ETA"
        out.append(WatchdogSignal(sid, sig["last_toll"], names[i + 1], expected, overdue,
                                  gps_silent if silent else 0, action))
    return out


@dataclass
class PingAdvice:
    shipment_id: str
    eta_range: str
    plan_changes: bool
    value: float
    source: str
    cost_inr: float
    buy: bool
    why: str


def _assignment(sol) -> dict:
    return {k: v.slot_id for k, v in sol.placements.items()}


def value_of_information(conn, facility_id: str, now: datetime) -> list[PingAdvice]:
    cfg = R()["location"]
    watch = {w.shipment_id: w for w in toll_watchdog(now)}
    gps_silent = {sid for sid, w in watch.items() if w.gps_silent_min}
    advice = []
    moving = [r["shipment_id"] for r in conn.execute(
        "SELECT shipment_id FROM shipments WHERE destination_facility_id=? AND current_status='IN_TRANSIT' "
        "ORDER BY shipment_id", (facility_id,))]
    for sid in moving:
        t = type("T", (), {"sid": sid})
        eta = resolve_eta(conn, t.sid)
        if eta.at_facility:
            continue
        flagged = watch.get(t.sid) and watch[t.sid].action != "OK"
        if eta.confidence == "HIGH" and not flagged:
            continue
        early = EtaEstimate(point=eta.point, confidence="HIGH", source="VOI_EARLY")
        late_pt = eta.planning_eta + (minutes(30) if flagged else minutes(0))
        if late_pt == eta.point:
            late_pt = eta.point + minutes(30)
        late = EtaEstimate(point=late_pt, confidence="HIGH", source="VOI_LATE")
        budget = R()["solver"]["chat_time_budget_ms"]
        s_e = solve(build_problem(conn, facility_id, now, "chat", focus=t.sid, focus_eta=early), budget)
        s_l = solve(build_problem(conn, facility_id, now, "chat", focus=t.sid, focus_eta=late), budget)
        changes = _assignment(s_e) != _assignment(s_l)
        value = abs(s_l.cost - s_e.cost)
        src = next(s for s in cfg["sources"]
                   if not (s["name"] == "gps_telematics" and t.sid in gps_silent))
        worth = value * cfg["inr_per_cost_unit"]
        buy = changes and value >= cfg["min_decision_value"] and worth >= src["cost_inr"]
        why = ("plan differs between early and late ETA" if changes else "same plan either way: a fix is worthless")
        if t.sid in gps_silent:
            why += "; GPS silent -> FASTag first"
        advice.append(PingAdvice(t.sid, f"{hhmm(early.point)}-{hhmm(late.point)}", changes, round(value, 1),
                                 src["name"], src["cost_inr"], buy, why))
    # spend the daily budget on the most valuable fixes first (deterministic order)
    spent = 0.0
    for a in sorted(advice, key=lambda a: (-a.value, a.shipment_id)):
        if a.buy and spent + a.cost_inr > cfg["daily_budget_inr"]:
            a.buy, a.why = False, a.why + "; over daily budget"
        elif a.buy:
            spent += a.cost_inr
    return sorted(advice, key=lambda a: (-a.buy, -a.value, a.shipment_id))
