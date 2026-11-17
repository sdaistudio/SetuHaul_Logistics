"""Messages to every party after a re-plan, and the checks they must pass.

Parties (who hears what):
  driver                 WhatsApp, in the driver's own language: new dock, time, when to be at the gate
  warehouse coordinator  e-mail: the changes at their docks, what needs their approval
  dispatcher (carrier)   e-mail: their trucks' new times
  customer (consignee)   e-mail, only when unloading moves later than planned by more than a threshold

Language is detected deterministically from the driver's latest message by Unicode
script (no LLM): Devanagari, Tamil, Telugu, Kannada, Malayalam, Bengali, Gujarati,
Gurmukhi, Odia; Latin text is split into English / Hinglish by marker words.
Reviewed templates exist for English, Hindi and Hinglish. For other scripts the
message is sent in English and flagged for translation (Amazon Translate with a
reviewed glossary in production) and a coordinator call.

`ground_check()` is the output guardrail: every clock time and dock code in a message
must come from the plan for that truck; no "confirmed" wording unless the booking was
executed; no phone numbers, internal ids or other drivers' details for external parties.
"""
from __future__ import annotations

import re

from .nlu import detect_language

SCRIPTS = [("hi", r"[ऀ-ॿ]"), ("bn", r"[ঀ-৿]"), ("pa", r"[਀-੿]"), ("gu", r"[઀-૿]"),
           ("or", r"[଀-୿]"), ("ta", r"[஀-௿]"), ("te", r"[ఀ-౿]"), ("kn", r"[ಀ-೿]"),
           ("ml", r"[ഀ-ൿ]")]
NAMES = {"en": "English", "hi": "Hindi", "hinglish": "Hinglish", "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati",
         "or": "Odia", "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam"}
REVIEWED = {"en", "hi", "hinglish"}


def detect(text: str) -> str:
    """Script first (most characters wins), then English vs Hinglish for Latin text."""
    counts = {code: len(re.findall(rx, text)) for code, rx in SCRIPTS}
    code, n = max(counts.items(), key=lambda kv: kv[1])
    return code if n else detect_language(text)


DRIVER = {
    "REBOOK": {
        "en": "SetuHaul {fac}: your {old} slot can no longer be reached. New dock: {dock} at about {start}. Please be at the gate by {gate}.",
        "hi": "सेतुहॉल {fac}: आपका {old} स्लॉट अब नहीं मिल पाएगा। नया डॉक: {dock}, लगभग {start} बजे। कृपया {gate} तक गेट पर पहुँचें।",
        "hinglish": "SetuHaul {fac}: aapka {old} slot ab nahi mil payega. Naya dock: {dock}, lagbhag {start} baje. Please {gate} tak gate par pahunchein.",
    },
    "NEW": {
        "en": "SetuHaul {fac}: dock {dock} is booked for you at about {start}. Please be at the gate by {gate}.",
        "hi": "सेतुहॉल {fac}: आपके लिए डॉक {dock} लगभग {start} बजे बुक है। कृपया {gate} तक गेट पर पहुँचें।",
        "hinglish": "SetuHaul {fac}: aapke liye dock {dock} lagbhag {start} baje book hai. Please {gate} tak gate par pahunchein.",
    },
    "YARD": {
        "en": "SetuHaul {fac}: you will be called to dock {dock} at about {start}. Please wait in the yard.",
        "hi": "सेतुहॉल {fac}: आपको लगभग {start} बजे डॉक {dock} पर बुलाया जाएगा। कृपया यार्ड में रुकें।",
        "hinglish": "SetuHaul {fac}: aapko lagbhag {start} baje dock {dock} par bulaya jayega. Yard mein rukiye.",
    },
    "ESCALATE": {
        "en": "SetuHaul {fac}: no dock fits your truck today at the current arrival time. The {role} will call you. Please do not go to the dock until then.",
        "hi": "सेतुहॉल {fac}: मौजूदा समय पर आज आपके ट्रक के लिए कोई डॉक नहीं है। {role} आपको कॉल करेंगे। तब तक डॉक पर न जाएँ।",
        "hinglish": "SetuHaul {fac}: abhi ke time par aaj aapke truck ke liye koi dock nahi hai. {role} aapko call karenge. Tab tak dock par mat jaiye.",
    },
}


def driver_message(kind: str, lang: str, **kw) -> tuple[str, str, bool]:
    """(text, language used, needs_translation)."""
    use = lang if lang in REVIEWED else "en"
    return DRIVER[kind][use].format(**kw), use, lang not in REVIEWED


def warehouse_message(fac: str, at: str, rows: list[dict]) -> str:
    lines = [f"Re-plan for {fac} at {at}: {len(rows)} change(s) at your docks."]
    for r in rows:
        lines.append(f"- {r['shipment']} ({r['priority']}, {r['product']}): {r['change']} {r.get('from') or '-'} -> "
                     f"{r.get('to') or 'no dock'}{' [needs your approval]' if r.get('approval') else ''}")
    lines.append("Please confirm dock readiness for the new times.")
    return "\n".join(lines)


def dispatcher_message(carrier: str, fac: str, rows: list[dict]) -> str:
    lines = [f"{carrier}: updated unloading times at {fac}."]
    for r in rows:
        lines.append(f"- {r['shipment']} ({r['order']}): {r.get('to') or 'no dock today, coordinator will call'}")
    return "\n".join(lines)


def customer_message(customer: str, fac: str, r: dict, late_min: int) -> str:
    return (f"{customer}: your inbound {r['product'].lower()} ({r['order']}) is now expected to unload at {fac} at about "
            f"{r['start']}, about {late_min} minutes later than planned. We will update you if this changes.")


TIME = re.compile(r"(?<![\d.])(\d{1,2}:\d{2})(?!\d)")
DOCK = re.compile(r"\b(?:D|dock\s*|डॉक\s*)(\d)\b", re.I)
PHONE = re.compile(r"\+\d{2}[\d\s-]{8,}\d|\b[6-9]\d{9}\b")     # +91-..., or a bare 10-digit mobile
INTERNAL = re.compile(r"\b(APT-[0-9A-F]{8}|HOLD-[0-9A-F]{8}|SLOT-[A-Z]{3}-\d{3}|DRV\d{3})\b")
CLAIM = re.compile(r"\bbooked\b|\bconfirmed\b|बुक है|book hai|पक्की", re.I)


def ground_check(text: str, *, allowed_times: set[str], allowed_docks: set[str], executed: bool,
                 external: bool = False) -> list[str]:
    v = []
    for t in TIME.findall(text):
        if t.zfill(5) not in allowed_times:
            v.append(f"TIME_NOT_IN_PLAN:{t}")
    for d in DOCK.findall(text):
        if f"D{d}" not in allowed_docks:
            v.append(f"DOCK_NOT_IN_PLAN:D{d}")
    if CLAIM.search(text) and not executed:
        v.append("CLAIMS_BOOKING_NOT_EXECUTED")
    if INTERNAL.search(text):
        v.append("INTERNAL_ID")
    if external and PHONE.search(text):
        v.append("PHONE_NUMBER_TO_EXTERNAL_PARTY")
    return v
