"""Policy constants, read from rules/rules.yaml (no business number lives in code).

Kept as module attributes so the rest of the code reads `config.X`.
docs/02_decision_register.md records the reason for each value.
"""
from __future__ import annotations

from .rules import R

_r = R()
_t, _c, _s = _r["timing"], _r["conversation"], _r["solver"]

PRIORITY_WEIGHT = dict(_r["priority_weight"])

GATE_TO_DOCK_MIN = _t["gate_to_dock_min"]
DOCK_TO_GATE_OUT_MIN = _t["dock_to_gate_out_min"]
MIN_LEAD_FROM_NOW_MIN = _t["min_lead_from_now_min"]
MEDIUM_CONF_ETA_BUFFER_MIN = _t["eta_buffer_min"]["MEDIUM"]
LOW_CONF_ETA_BUFFER_MIN = _t["eta_buffer_min"]["LOW"]

HOLD_TTL_MIN = _c["hold_ttl_min"]
PENDING_CONFIRMATION_TTL_MIN = _c["pending_confirmation_ttl_min"]
PENDING_LEAD_BEFORE_START_MIN = _c.get("pending_lead_before_start_min", 30)
PENDING_MIN_DECISION_MIN = _c.get("pending_min_decision_min", 5)
MAX_OPTIONS_SHOWN = _c["max_options_shown"]
AUTO_CONFIRM = _c["auto_confirm"]
MAX_CLARIFICATION_QUESTIONS = _c["max_clarification_questions"]
DUPLICATE_WINDOW_MIN = _c["duplicate_window_min"]
MAX_MESSAGE_CHARS = _c["max_message_chars"]
NIGHT_SHIFT_START = _c["night_shift_start"]

DEFAULT_HEAVY_THRESHOLD_KG = _r["compatibility"]["default_heavy_threshold_kg"]
BLOCKING_EVENTS = set(_r["hard"]["blocking_dock_events"])
OPTION_RANKING = _s["option_ranking"]
