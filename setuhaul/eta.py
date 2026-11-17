"""ETA resolution (Decision D3).

Rule, in order:
1. A gate check-in is a fact: the truck is here. ETA = gate_in, confidence HIGH.
2. Otherwise the most recent ETA record wins (created_at), ties broken by
   source rank OPERATIONS_OVERRIDE > WAREHOUSE_ESTIMATE > DRIVER_DECLARED > plan.
3. Confidence turns the point estimate into a *planning* ETA:
   HIGH -> point, MEDIUM -> point + 15, LOW -> upper bound of the window
   (or point + 30 when there is no window). We plan for the late edge so we
   never promise a slot the truck is likely to miss.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .timeutil import minutes, parse

SOURCE_RANK = {"OPERATIONS_OVERRIDE": 4, "WAREHOUSE_ESTIMATE": 3, "DRIVER_DECLARED": 2, "ORIGINAL_PLAN": 1}


@dataclass
class EtaEstimate:
    point: datetime
    confidence: str
    source: str
    earliest: datetime | None = None
    latest: datetime | None = None
    at_facility: bool = False
    note: str | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def planning_eta(self) -> datetime:
        if self.at_facility:
            return self.point
        if self.latest is not None:
            return max(self.latest, self.point)
        if self.confidence == "LOW":
            return self.point + minutes(config.LOW_CONF_ETA_BUFFER_MIN)
        if self.confidence == "MEDIUM":
            return self.point + minutes(config.MEDIUM_CONF_ETA_BUFFER_MIN)
        return self.point

    def as_dict(self):
        from .timeutil import iso
        return {
            "point": iso(self.point), "planning_eta": iso(self.planning_eta),
            "earliest": iso(self.earliest), "latest": iso(self.latest),
            "confidence": self.confidence, "source": self.source,
            "at_facility": self.at_facility, "evidence": self.evidence,
        }


def resolve_eta(conn, shipment_id: str) -> EtaEstimate:
    s = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
    chk = conn.execute(
        "SELECT gate_in_ts FROM facility_checkins WHERE shipment_id=? AND gate_in_ts IS NOT NULL", (shipment_id,)
    ).fetchone()
    if chk:
        return EtaEstimate(point=parse(chk["gate_in_ts"]), confidence="HIGH", source="GATE_CHECKIN",
                           at_facility=True, evidence=[f"facility_checkins.gate_in_ts={chk['gate_in_ts']}"])

    rows = conn.execute("SELECT * FROM eta_updates WHERE shipment_id=?", (shipment_id,)).fetchall()
    if not rows:
        # The plan is a plan, not an observation: MEDIUM, not HIGH (the view says HIGH).
        return EtaEstimate(point=parse(s["original_eta_ts"]), confidence="MEDIUM", source="ORIGINAL_PLAN",
                           evidence=[f"shipments.original_eta_ts={s['original_eta_ts']}"])

    best = max(rows, key=lambda r: (r["created_at"], SOURCE_RANK.get(r["source_type"], 0)))
    est = EtaEstimate(point=parse(best["declared_eta_ts"]), confidence=best["confidence_code"],
                      source=best["source_type"], note=best["note"],
                      evidence=[f"eta_updates.{best['eta_update_id']} declared={best['declared_eta_ts']} "
                                f"conf={best['confidence_code']} created={best['created_at']}"])
    # A free-text note may carry a window ("between 12:30 and 13:00"): reuse the NLU.
    if best["note"]:
        from .nlu import parse_message
        p = parse_message(best["note"], now=parse(best["created_at"]))
        if p.eta_window:
            est.earliest, est.latest = p.eta_window
            est.evidence.append(f"window from note: {best['note']!r}")
    return est
