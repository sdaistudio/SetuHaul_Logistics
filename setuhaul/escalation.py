"""Human hand-off (Decisions D10, D12).

An escalation is a complete record a coordinator can act on without reading the
chat: facts, what the system tried, why each option failed, what it recommends,
and who it was routed to. Routing skips contacts with no usable channel
(e.g. CON005 has no email and the role is vacant) and never treats a FAILED
message as delivered.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from . import config
from .db import audit
from .timeutil import at, hhmm, iso

# Trigger -> who must approve / take over
TRIGGERS = {
    "SAFETY": ("SHIFT_MANAGER", "Driver safety: a person must take over immediately."),
    "NO_FEASIBLE_SLOT": ("WAREHOUSE_COORDINATOR", "No compatible slot after declared ETA."),
    "HUMAN_REQUESTED": ("WAREHOUSE_COORDINATOR", "Driver asked for a person."),
    "LATE_START_RULE": ("SHIFT_MANAGER", "Start after LAST_NEW_START_TIME needs approval."),
    "DATA_CONFLICT": ("WAREHOUSE_COORDINATOR", "Contradictory shipment / booking data."),
    "CLARIFICATION_LIMIT": ("WAREHOUSE_COORDINATOR", "Driver could not give usable ETA/shipment after 2 tries."),
    "RATE_LIMIT": ("WAREHOUSE_COORDINATOR", "Unusual message volume from one driver."),
    "PENALTY_OR_DISPUTE": ("SHIFT_MANAGER", "Detention, penalty or compensation question."),
}


def route(conn, facility_id: str, preferred_role: str, now: datetime) -> dict:
    roles = [preferred_role]
    if now >= at(now, config.NIGHT_SHIFT_START):
        roles.insert(0, "NIGHT_SHIFT_COORDINATOR")
    roles += ["SHIFT_MANAGER", "WAREHOUSE_COORDINATOR"]
    contacts = conn.execute("SELECT * FROM facility_contacts WHERE facility_id=? AND active_flag=1",
                            (facility_id,)).fetchall()
    skipped = []
    for role in roles:
        for c in contacts:
            if c["contact_role"] != role:
                continue
            if "vacant" in (c["contact_name"] or "").lower():
                skipped.append(f"{c['contact_id']} vacant")
                continue
            channel = "EMAIL" if c["email"] else ("PHONE" if c["phone"] else None)
            if channel is None:
                skipped.append(f"{c['contact_id']} no channel")
                continue
            return {"role": role, "contact_id": c["contact_id"], "name": c["contact_name"], "channel": channel,
                    "address": c["email"] or c["phone"], "skipped": skipped}
    return {"role": "OPERATIONS", "contact_id": None, "name": "Ops queue", "channel": "QUEUE",
            "address": "ops-queue", "skipped": skipped}


def escalate(conn, *, reason: str, thread_id: str | None, shipment_id: str | None, facility_id: str,
             now: datetime, facts: dict, tried: list[dict] | None = None, recommendation: str = "") -> dict:
    role, why = TRIGGERS.get(reason, ("WAREHOUSE_COORDINATOR", reason))
    dest = route(conn, facility_id, role, now)
    esc_id = f"ESC-{uuid.uuid4().hex[:8].upper()}"
    handoff = {
        "escalation_id": esc_id, "reason": reason, "why": why, "raised_at": iso(now),
        "thread_id": thread_id, "shipment_id": shipment_id, "facility_id": facility_id,
        "facts": facts, "options_tried": tried or [], "recommendation": recommendation,
        "routed_to": dest, "sla": "acknowledge within 10 min (5 min for SAFETY)",
        "driver_told": "a person will contact you; do not go to the dock until then",
    }
    conn.execute("INSERT INTO escalations VALUES (?,?,?,?,?,?, 'OPEN', ?)",
                 (esc_id, thread_id, shipment_id, reason, dest["role"], json.dumps(handoff, default=str), iso(now)))
    audit(conn, iso(now), "agent", "ESCALATED", esc_id, json.dumps({"reason": reason, "to": dest["role"]}))
    return handoff


def summarise_tried(evals, limit: int = 8) -> list[dict]:
    out = []
    for e in evals:
        if e.dock_type and not e.feasible and "DOCK_TYPE" not in e.reasons and "WEIGHT" not in e.reasons:
            out.append({"slot": e.label(), "rejected_because": e.reasons, "notes": e.notes})
    return out[:limit] or [{"slot": e.label(), "rejected_because": e.reasons} for e in evals[:3]]


def _hh(d):
    return hhmm(d)
