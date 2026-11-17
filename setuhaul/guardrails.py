"""Guardrails in three places: what comes in, what the system may do, what goes out.

INPUT   length cap, control-character strip, prompt-injection flag, PII
        redaction for logs/LLM, safety keywords -> human immediately.
ACTION  a tool may be called only from an allowed conversation state; a chosen
        option must be one we actually showed; priority claims in text are
        ignored; per-driver rate limit; commit always re-validates (capacity.py).
OUTPUT  no "confirmed/booked" claim unless this turn committed a CONFIRMED
        booking; every clock time in the reply must come from tool evidence;
        no internal IDs or other drivers' data; language must match.

Guardrails are deterministic code, so they are testable (evals/guardrail_cases).
In AWS, Amazon Bedrock Guardrails is added in front of the optional LLM
fallback only (prompt-attack + PII filters); it does not replace these checks.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import config
from .nlu import RX, redact

ALLOWED_ACTIONS = {
    # state -> actions the workflow may take in that state
    "NEW": {"identify", "status", "options", "escalate", "early"},
    "NEED_SHIPMENT": {"identify", "escalate"},
    "NEED_ETA": {"options", "status", "escalate", "identify"},
    "OPTIONS_SHOWN": {"book", "options", "status", "escalate", "decline", "identify"},
    "PENDING_WAREHOUSE": {"status", "escalate", "options"},
    "CONFIRMED": {"status", "options", "escalate"},
    "ESCALATED": {"status", "escalate"},
    "CLOSED": {"status", "identify", "options", "escalate"},
}

CLAIM_PATTERNS = [
    r"booking confirmed", r"\b(booking|slot|dock)\s+(is\s+|has been\s+)?(now\s+)?(confirmed|booked)\b", r"✅",
    r"बुकिंग पक्की", r"पक्का हो गया", r"कन्फर्म हो गया", r"confirm ho gaya", r"book ho gaya",
]
INTERNAL_ID = re.compile(r"\b(APT-[0-9A-F]{8}|HOLD-[0-9A-F]{8}|SLOT-[A-Z]{3}-\d{3}|DRV\d{3}|EXC\d{3}|THR\d{3})\b")
TIME_IN_TEXT = re.compile(r"(?<![\d.])(\d{1,2}:\d{2})(?!\d)")


class GuardrailViolation(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass
class InputCheck:
    text: str
    redacted: str
    flags: list[str] = field(default_factory=list)


def check_input(text: str) -> InputCheck:
    flags = []
    clean = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")
    if len(clean) > config.MAX_MESSAGE_CHARS:
        clean = clean[: config.MAX_MESSAGE_CHARS]
        flags.append("TRUNCATED")
    low = clean.lower()
    if RX["injection"].search(low):
        flags.append("INJECTION")
    red = redact(clean)
    if red != clean:
        flags.append("PII")
    return InputCheck(clean, red, flags)


def check_action(state: str, action: str):
    if action not in ALLOWED_ACTIONS.get(state, set()):
        raise GuardrailViolation("ACTION_NOT_ALLOWED", f"{action} in state {state}")


def check_option_choice(choice: int | None, shown: list[dict]):
    if choice is None or not (1 <= choice <= len(shown)):
        raise GuardrailViolation("OPTION_NOT_SHOWN", f"choice={choice} shown={len(shown)}")


def check_output(reply: str, *, committed_status: str | None, allowed_times: set[str], lang: str) -> list[str]:
    """Return violation codes ([] = safe to send)."""
    v = []
    low = reply.lower()
    if committed_status != "CONFIRMED" and any(re.search(p, low) for p in CLAIM_PATTERNS):
        v.append("UNBACKED_BOOKING_CLAIM")
    norm = {t.zfill(5) for t in allowed_times}
    for t in TIME_IN_TEXT.findall(reply):
        if t.zfill(5) not in norm:
            v.append(f"UNGROUNDED_TIME:{t}")
    if INTERNAL_ID.search(reply):
        v.append("INTERNAL_ID_LEAK")
    if RX["phone"].search(reply) or RX["email"].search(reply):
        v.append("PII_LEAK")
    has_dev = bool(re.search(r"[ऀ-ॿ]", reply))
    if lang == "hi" and not has_dev:
        v.append("LANGUAGE_MISMATCH")
    if lang in ("en", "hinglish") and has_dev:
        v.append("LANGUAGE_MISMATCH")
    if len(reply) > 900:
        v.append("TOO_LONG")
    return v
