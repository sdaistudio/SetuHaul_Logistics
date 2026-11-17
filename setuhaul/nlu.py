"""Deterministic multilingual message parser (English, Hindi in Devanagari,
romanised Hindi / Hinglish).

No model is called here. Hindi words are first mapped onto a small romanised
vocabulary, then one rule set handles all three languages. Every extracted
field carries the text span that produced it (`evidence`), so a reviewer can
see why the parser decided what it decided, and the eval suite can score it.

An optional LLM fallback (setuhaul/llm_fallback.py) is consulted ONLY when this
parser finds nothing usable; its output is validated against the same schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .timeutil import hhmm

# ---------------------------------------------------------------- lexicons --
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Hindi (Devanagari) -> romanised token. Longest phrases first.
HI_TO_ROMAN = [
    ("के बाद", " ke baad "), ("से पहले", " se pehle "), ("ठीक है", " theek hai "), ("कौन सा", " kaunsa "),
    ("कौनसा", " kaunsa "), ("बात करनी", " baat karni "), ("बात करना", " baat karna "),
    ("घंटों", " ghanton "), ("घंटे", " ghante "), ("घंटा", " ghanta "), ("मिनट", " minute "), ("बजे", " baje "),
    ("लेट", " late "), ("देर", " der "), ("बाद", " baad "), ("पहले", " pehle "), ("तक", " tak "),
    ("साढ़े", " saade "), ("साढे", " saade "), ("सवा", " sawa "), ("पौने", " paune "), ("डेढ़", " dedh "),
    ("डेढ", " dedh "), ("ढाई", " dhai "), ("आधा", " aadha "), ("आधे", " aadha "),
    ("एक", " ek "), ("दो", " do "), ("तीन", " teen "), ("चार", " chaar "), ("पांच", " paanch "),
    ("पाँच", " paanch "), ("छह", " chhe "), ("सात", " saat "), ("आठ", " aath "), ("नौ", " nau "),
    ("दस", " das "), ("ग्यारह", " gyarah "), ("बारह", " barah "),
    ("शाम", " shaam "), ("सुबह", " subah "), ("दोपहर", " dopahar "), ("रात", " raat "),
    ("लगभग", " lagbhag "), ("करीब", " kareeb "), ("शायद", " shayad "),
    ("हाँ", " haan "), ("हां", " haan "), ("जी", " ji "), ("नहीं", " nahi "), ("नही", " nahi "),
    ("पहुँचूंगा", " pahunchunga "), ("पहुंचूंगा", " pahunchunga "), ("पहुँच", " pahunch "), ("पहुंच", " pahunch "),
    ("गया", " gaya "), ("गेट", " gate "), ("पर", " par "), ("पे", " pe "), ("ट्रैफिक", " traffic "), ("जाम", " jam "),
    ("टायर", " tyre "), ("पंचर", " puncture "), ("बारिश", " barish "), ("कोहरा", " fog "),
    ("दुर्घटना", " accident "), ("एक्सीडेंट", " accident "), ("घायल", " injured "), ("चोट", " injured "),
    ("आग", " fire "), ("पुलिस", " police "), ("स्लॉट", " slot "), ("डॉक", " dock "), ("विकल्प", " option "),
    ("पहला", " pehla "), ("पहली", " pehli "), ("दूसरा", " doosra "), ("दूसरी", " doosri "), ("तीसरा", " teesra "),
    ("जल्दी", " jaldi "), ("निकलना", " nikalna "), ("निकलना है", " nikalna "), ("स्टेटस", " status "),
    ("कैंसल", " cancel "), ("मैनेजर", " manager "), ("इंसान", " insaan "), ("मिलेगा", " milega "),
    ("चाहिए", " chahiye "), ("ज़रूरी", " zaroori "), ("जरूरी", " zaroori "), ("में", " mein "), ("मैं", " main "),
    ("वाला", " wala "), ("वाली", " wali "), ("हुआ", " hua "), ("हूँ", " hoon "), ("हूं", " hoon "), ("है", " hai "), ("क्या", " kya "), ("मेरा", " mera "), ("अभी", " abhi "),
]

HI_TO_ROMAN.sort(key=lambda kv: -len(kv[0]))  # longest first: "एक्सीडेंट" before "एक"

HINGLISH_MARKERS = {
    "hai", "hoon", "hu", "ho", "gaya", "gayi", "ghanta", "ghante", "baje", "der", "pahunch", "pahuch",
    "raha", "rahi", "kya", "nahi", "nahin", "haan", "ji", "bhai", "milega", "chahiye", "abhi", "kitne",
    "baad", "pehle", "mein", "dedh", "dhai", "saade", "sawa", "paune", "kaunsa", "jaldi", "tak", "wala",
    "doosra", "dusra", "pehla", "teesra", "mera", "meri", "karo", "kar", "sakta", "aaunga", "jaunga",
    "pahunchunga", "theek", "thik", "nikalna", "lagbhag", "kareeb", "shaam", "subah", "raat", "dopahar",
    "mujhe", "se", "karni", "karna", "baat", "pe", "bhi", "koi", "kab", "kitna", "hoga", "hua", "yaar",
}

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "an": 1, "a": 1,
    "ek": 1, "do": 2, "teen": 3, "chaar": 4, "char": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12,
}
FRACTIONS = {"dedh": 1.5, "dhai": 2.5, "aadha": 0.5, "adha": 0.5, "half": 0.5, "half an": 0.5}
MODIFIERS = {"saade": 0.5, "sawa": 0.25, "paune": -0.25}
NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted(NUM_WORDS, key=len, reverse=True)) + ")"

ORDINALS = {
    "first": 1, "1st": 1, "pehla": 1, "pehli": 1, "pehla wala": 1,
    "second": 2, "2nd": 2, "doosra": 2, "dusra": 2, "doosri": 2, "dusri": 2,
    "third": 3, "3rd": 3, "teesra": 3, "tisra": 3, "teesri": 3,
}

RX = {
    "safety": re.compile(r"\b(accident|injur\w*|hurt|fire|robbery|robbed|loot\w*|hijack\w*|police|ambulance|"
                         r"emergency|bleeding|crash\w*|ghayal|chot|aag lag)\b"),
    "human": re.compile(r"(talk|speak|baat)\s*(to|with|karni|karna|karo|se)?\s*(a\s+|kisi\s+)?"
                        r"(human|person|manager|someone|insaan|officer|supervisor|agent)|\bcall me\b|"
                        r"\bmanager\b|\binsaan\b"),
    "cancel": re.compile(r"\bcancel\w*\b"),
    "confirm": re.compile(r"^\s*(yes|y|yeah|yep|ok|okay|okk|haan|haa|han|ha|ji|ji haan|theek hai|thik hai|"
                          r"sahi hai|done|book it|book karo|confirm|chalega|perfect|👍)\b"),
    "decline": re.compile(r"^\s*(no|nope|nahi|nahin|na|none|neither|koi nahi)\b"),
    "option_num": re.compile(r"\b(?:option|opt|vikalp|no\.?)\s*#?\s*(\d)\b"),
    "bare_digit": re.compile(r"^\s*#?\s*(\d)\s*[.)]?\s*$"),
    "early": re.compile(r"\b(at the gate|already (here|at|reached|arrived)|reached early|arrived early|"
                        r"came early|gate (pe|par)|jaldi (aa|pahunch)|early)\b"),
    "status": re.compile(r"\b(status|still active|still on|assigned|kya hua|any update|update\?|"
                         r"which dock|kaunsa dock|new dock|is my .* slot)\b|\b(mera|my) (slot|dock|booking|appointment)\b.*\?"),
    "ask": re.compile(r"\b(what slots?|which slots?|slots? (possible|available|free|open)|any slot|options?(?!\s*#?\s*\d)|"
                      r"kaunsa slot|slot milega|slot mil|next slot|earliest slot|first possible|"
                      r"what is next|what next|next kya|ab kya)\b"),
    "delay": re.compile(r"\b(late|delay\w*|der|peeche|behind|stuck|traffic|jam|breakdown|puncture|tyre|"
                        r"diversion|missed|miss)\b"),
    "eta_cue": re.compile(r"\b(reach\w*|eta|arriv\w*|pahunch\w*|aaunga|aa jaunga|aa jayenge|get there|be there)\b"),
    "approx": re.compile(r"\b(around|about|approx\w*|roughly|maybe|probably|lagbhag|kareeb|karib|takriban|"
                         r"shayad|or so|ish)\b|~"),
    "vague": re.compile(r"\b(thoda|thodi|a bit|little|some time|kuch der|pata nahi|not sure|don'?t know|"
                        r"no idea|soon)\b"),
    "priority_claim": re.compile(r"\b(urgent|critical|vip|top priority|priority load|very important|"
                                 r"zaroori|bahut zaroori|jaldi chahiye)\b"),
    "injection": re.compile(r"(ignore (all |any )?(the )?(previous|prior|above|your|earlier) (instructions|rules|"
                            r"prompts?)|system prompt|you are now|act as an?|developer mode|jailbreak|"
                            r"forget (your|all|the) (rules|instructions)|override (the )?(rules|system|policy)|"
                            r"admin mode|<\s*script|\{\{.*\}\})"),
    "shipment": re.compile(r"\b(shp\d{4})\b"),
    "order": re.compile(r"\b(ord-\d{6}-\d{3})\b"),
    "order_suffix": re.compile(r"(?<![\d:.-])(0\d{2})(?![\d:.])"),
    "phone": re.compile(r"(\+91[\s-]?)?\b[6-9]\d{9}\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "aadhaar": re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"),
}

REASONS = [
    ("BREAKDOWN", r"\b(tyre|tire|puncture|breakdown|broke down|engine|repair\w*|gaadi kharab)\b"),
    ("WEATHER", r"\b(rain|barish|fog|storm|flood|weather)\b"),
    ("LOADING_DELAY", r"\b(loading|origin released|released me late|late departure|late release)\b"),
    ("ROUTE_ISSUE", r"\b(diversion|road closed|route|detour|closure)\b"),
    ("TRAFFIC", r"\b(traffic|jam|congestion)\b"),
]

CLOCK_HHMM = re.compile(r"(?<![\d:.])(\d{1,2})[:.](\d{2})(?![\d])\s*(a\.?m\.?|p\.?m\.?)?")
CLOCK_AMPM = re.compile(r"(?<![\d:.])(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])")
CLOCK_BAJE = re.compile(r"(?<![a-z])(?:(saade|sawa|paune)\s+)?" + NUM + r"\s*baje")
CLOCK_CUE = re.compile(r"\b(after|before|by|around|at|till|until|eta|reaching|reach|leave by|from)\s+(\d{1,2})"
                       r"(?![\d:.]|\s*(?:min|hour|hr|ghant|baje|am|pm|a\.m|p\.m|kg|ton))")
CLOCK_HI_CUE = re.compile(r"(?<![\d:.])(\d{1,2})\s+(ke baad|baad|se pehle|tak)\b")
DUR_HOURS = re.compile(r"(?<![a-z])(?:(saade|sawa|paune)\s+)?(half an?|dedh|dhai|aadha|adha|" + NUM[1:-1] + r")"
                       r"\s*(hours?|hrs?|hr|h|ghante|ghanta|ghanton)\b")
DUR_MIN = re.compile(r"(?<![a-z])" + NUM + r"\s*(minutes?|mins?|min|minute)\b")
RANGE = re.compile(r"between\s+(.+?)\s+(?:and|to|-)\s+(.+?)(?:[.,?!]|$)|(\d{1,2}[:.]\d{2}|\d{1,2})\s*(?:-|to|se)\s*"
                   r"(\d{1,2}[:.]\d{2}|\d{1,2})\s*(?:ke beech|beech|ke darmiyan)?")


@dataclass
class TimeMention:
    when: datetime
    role: str          # ETA | EARLIEST_START | LATEST_GATE_OUT | UNSPECIFIED
    text: str
    pos: int


@dataclass
class ParseResult:
    text: str
    lang: str
    intents: list[str] = field(default_factory=list)
    eta: datetime | None = None
    eta_window: tuple[datetime, datetime] | None = None
    eta_confidence: str | None = None
    eta_kind: str | None = None             # CLOCK | RELATIVE_DELAY | RELATIVE_NOW | WINDOW
    delay_min: int | None = None
    earliest_start: datetime | None = None
    latest_gate_out: datetime | None = None
    option_choice: int | None = None
    option_time: datetime | None = None
    shipment_refs: list[str] = field(default_factory=list)
    order_suffixes: list[str] = field(default_factory=list)
    delay_reason: str | None = None
    flags: list[str] = field(default_factory=list)
    clarify: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def primary_intent(self) -> str:
        return self.intents[0] if self.intents else "UNKNOWN"

    def understood(self) -> bool:
        return self.primary_intent != "UNKNOWN"

    def as_dict(self):
        f = lambda d: hhmm(d) if d else None
        return {
            "lang": self.lang, "intents": self.intents, "eta": f(self.eta),
            "eta_window": [f(self.eta_window[0]), f(self.eta_window[1])] if self.eta_window else None,
            "eta_confidence": self.eta_confidence, "eta_kind": self.eta_kind, "delay_min": self.delay_min,
            "earliest_start": f(self.earliest_start), "latest_gate_out": f(self.latest_gate_out),
            "option_choice": self.option_choice, "option_time": f(self.option_time),
            "shipment_refs": self.shipment_refs, "order_suffixes": self.order_suffixes,
            "delay_reason": self.delay_reason, "flags": self.flags, "clarify": self.clarify,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------- helpers ----
def detect_language(text: str) -> str:
    if DEVANAGARI.search(text):
        return "hi"
    toks = re.findall(r"[a-z]+", text.lower())
    hits = sum(1 for t in toks if t in HINGLISH_MARKERS)
    return "hinglish" if hits >= 2 or (hits == 1 and len(toks) <= 3) else "en"


def normalise(text: str) -> str:
    t = text.translate(DEV_DIGITS)
    for hi, ro in HI_TO_ROMAN:
        t = t.replace(hi, ro)
    t = t.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _num(tok: str) -> float:
    tok = tok.strip()
    if tok in FRACTIONS:
        return FRACTIONS[tok]
    if tok in NUM_WORDS:
        return NUM_WORDS[tok]
    return float(tok)


def _resolve_clock(h: int, m: int, mer: str | None, ctx: str, now: datetime) -> datetime | None:
    if h > 23 or m > 59:
        return None
    mer = (mer or "").replace(".", "")
    if mer == "pm" and h < 12:
        h += 12
    elif mer == "am" and h == 12:
        h = 0
    elif not mer and h <= 12:
        if re.search(r"\b(shaam|raat|evening|night|tonight)\b", ctx) and h < 12:
            h += 12
        elif re.search(r"\b(dopahar|afternoon)\b", ctx) and h < 7:
            h += 12
        elif not re.search(r"\b(subah|morning)\b", ctx):
            # Operational-day rule: the first reading not already in the past
            # (15 min tolerance) is the one the driver means.
            base = now.replace(hour=h % 24, minute=m, second=0, microsecond=0)
            if base <= now - timedelta(minutes=15) and h < 12:
                h += 12
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _role(t: str, start: int, end: int) -> str:
    before = t[max(0, start - 40):start]
    after = t[end:end + 25]
    if re.search(r"(reach\w*|arriv\w*|pahunch\w*|eta|be there|get there)\s+(by|around|at|before|till|until)?\s*$",
                 before) or re.match(r"\s*(tak|se pehle|by)?\s*(pahunch|aa |aa$|aaunga|aa jaunga|reach|arriv)", after):
        return "ETA"
    if re.search(r"(leave|nikal\w*|exit|gate[- ]?out|go back|wapas|another pickup|finish|done by|khali)\b[^.?!]*$",
                 before) or re.match(r"\s*(se pehle|tak)\s*(nikal|jaana|leave)", after):
        return "LATEST_GATE_OUT"
    if re.search(r"(before|by|until|till|latest)\s*$", before) or re.match(r"\s*(se pehle|tak)\b", after):
        return "LATEST_GATE_OUT"
    if re.search(r"(after|from|not before|anything after|slots? after)\s*$", before) or \
            re.match(r"\s*(ke baad|baad)\b", after):
        return "EARLIEST_START"
    if RX["eta_cue"].search(before[-25:]) or RX["eta_cue"].search(after[:15]) or \
            re.search(r"(eta|reaching|reach|arrive|around|at)\s*$", before):
        return "ETA"
    return "UNSPECIFIED"


def _clock_mentions(t: str, now: datetime) -> list[TimeMention]:
    found: list[TimeMention] = []
    taken: list[tuple[int, int]] = []

    def add(start, end, h, m, mer):
        if any(s < end and start < e for s, e in taken):
            return
        when = _resolve_clock(h, m, mer, t[max(0, start - 30):end + 20], now)
        if when:
            taken.append((start, end))
            found.append(TimeMention(when, _role(t, start, end), t[start:end].strip(), start))

    for mt in CLOCK_HHMM.finditer(t):
        add(mt.start(), mt.end(), int(mt.group(1)), int(mt.group(2)), mt.group(3))
    for mt in CLOCK_AMPM.finditer(t):
        add(mt.start(), mt.end(), int(mt.group(1)), 0, mt.group(2))
    for mt in CLOCK_BAJE.finditer(t):
        base = _num(mt.group(2))
        mod = MODIFIERS.get(mt.group(1) or "", 0.0)
        val = base + mod
        add(mt.start(), mt.end(), int(val), int(round((val - int(val)) * 60)), None)
    for mt in CLOCK_CUE.finditer(t):
        add(mt.start(2), mt.end(2), int(mt.group(2)), 0, None)
    for mt in CLOCK_HI_CUE.finditer(t):
        add(mt.start(1), mt.end(1), int(mt.group(1)), 0, None)
    found.sort(key=lambda x: x.pos)
    return found


def _durations(t: str) -> list[tuple[int, str, int]]:
    out = []
    for mt in DUR_HOURS.finditer(t):
        mod = MODIFIERS.get(mt.group(1) or "", 0.0)
        tok = mt.group(2)
        val = _num(tok) + (mod if tok not in FRACTIONS else 0)
        out.append((int(round(val * 60)), mt.group(0), mt.start()))
    for mt in DUR_MIN.finditer(t):
        out.append((int(_num(mt.group(1))), mt.group(0), mt.start()))
    return out


# ------------------------------------------------------------------ main ----
def parse_message(text: str, now: datetime, reference_eta: datetime | None = None) -> ParseResult:
    """`reference_eta` = the shipment's current ETA/appointment, used to turn
    "late by 45 minutes" into a clock time. Without it, delays are relative to now."""
    lang = detect_language(text)
    t = normalise(text)
    r = ParseResult(text=text, lang=lang)

    # ---- flags (never change control flow except SAFETY) --------------------
    for name in ("injection", "priority_claim"):
        if RX[name].search(t):
            r.flags.append(name.upper())
    if RX["phone"].search(t) or RX["email"].search(t) or RX["aadhaar"].search(t):
        r.flags.append("PII")

    # ---- references ---------------------------------------------------------
    r.shipment_refs = [s.upper() for s in RX["shipment"].findall(t)]
    r.shipment_refs += [o.upper() for o in RX["order"].findall(t)]
    r.order_suffixes = RX["order_suffix"].findall(t)

    # ---- times --------------------------------------------------------------
    mentions = _clock_mentions(t, now)
    rng = RANGE.search(t)
    if rng:
        a_txt, b_txt = (rng.group(1), rng.group(2)) if rng.group(1) else (rng.group(3), rng.group(4))

        def one(txt: str):
            ms = _clock_mentions(txt, now)
            if ms:
                return ms[0].when
            txt = txt.strip()
            return _resolve_clock(int(txt), 0, None, t, now) if txt.isdigit() else None

        wa, wb = one(a_txt), one(b_txt)
        if wa and wb and wb < wa and wb.hour < 12:
            wb = wb + timedelta(hours=12)  # "11:30 to 1" -> 13:00
        if wa and wb and wb > wa:
            r.eta_window = (wa, wb)
            r.evidence.append(f"window: {rng.group(0).strip()!r}")
            mentions = [m for m in mentions if not (rng.start() <= m.pos < rng.end())]

    approx = bool(RX["approx"].search(t))
    for m in mentions:
        if m.role == "EARLIEST_START" and r.earliest_start is None:
            r.earliest_start = m.when
            r.evidence.append(f"earliest_start: {m.text!r}")
        elif m.role == "LATEST_GATE_OUT" and r.latest_gate_out is None:
            r.latest_gate_out = m.when
            r.evidence.append(f"latest_gate_out: {m.text!r}")

    eta_mentions = [m for m in mentions if m.role == "ETA"]
    unspecified = [m for m in mentions if m.role == "UNSPECIFIED"]
    delay_ctx = bool(RX["delay"].search(t))
    if r.eta_window:
        r.eta, r.eta_kind = r.eta_window[0] + (r.eta_window[1] - r.eta_window[0]) / 2, "WINDOW"
        r.eta = r.eta.replace(second=0, microsecond=0)
        r.eta_confidence = "LOW"
    elif eta_mentions or (unspecified and (delay_ctx or RX["eta_cue"].search(t))):
        m = (eta_mentions or unspecified)[0]
        r.eta, r.eta_kind = m.when, "CLOCK"
        r.eta_confidence = "MEDIUM" if approx else "HIGH"
        r.evidence.append(f"eta: {m.text!r}")
    elif unspecified:
        r.option_time = unspecified[0].when  # e.g. "the 12:00 one"

    durs = _durations(t)
    if r.eta is None and durs:
        total = sum(d for d, _, _ in durs)
        rel_now = re.search(r"\b(in|mein|me|baad)\b", t) and not delay_ctx
        if rel_now:
            r.eta, r.eta_kind = now + timedelta(minutes=total), "RELATIVE_NOW"
        else:
            r.delay_min = total
            r.eta_kind = "RELATIVE_DELAY"
            base = reference_eta or now
            r.eta = base + timedelta(minutes=total)
        r.eta = r.eta.replace(second=0, microsecond=0)
        r.eta_confidence = "LOW" if approx else "MEDIUM"
        r.evidence.append(f"duration: {', '.join(x for _, x, _ in durs)!r}")
    elif durs and delay_ctx:
        r.delay_min = sum(d for d, _, _ in durs)

    if delay_ctx and r.eta is None and RX["vague"].search(t):
        r.clarify.append("ETA_VAGUE")

    for code, pat in REASONS:
        if re.search(pat, t):
            r.delay_reason = code
            break

    # ---- option choice --------------------------------------------------------
    mo = RX["option_num"].search(t) or RX["bare_digit"].search(t)
    if mo:
        r.option_choice = int(mo.group(1))
    else:
        short = len(t.split()) <= 4
        for word, n in sorted(ORDINALS.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{re.escape(word)}\b(?!\s+(hour|ghant|possible|available|free|time))", t) and (
                    short or re.search(rf"\b{re.escape(word)}\s+(one|option|wala|wali|slot|dock|choice)\b", t)):
                r.option_choice = n
                break

    # ---- intents (ordered by precedence) --------------------------------------
    it = r.intents
    if RX["safety"].search(t):
        it.append("SAFETY")
    if RX["human"].search(t):
        it.append("HUMAN")
    if RX["cancel"].search(t):
        it.append("CANCEL")
    if r.option_choice is not None or (r.option_time and not delay_ctx and len(t.split()) <= 6):
        it.append("SELECT_OPTION")
    if RX["confirm"].search(t) and len(t.split()) <= 5:
        it.append("CONFIRM")
    if RX["decline"].search(t) and len(t.split()) <= 8:
        it.append("DECLINE")
    if RX["early"].search(t) and not delay_ctx:
        it.append("EARLY_ARRIVAL")
    if delay_ctx or r.eta is not None or "ETA_VAGUE" in r.clarify:
        it.append("REPORT_DELAY")
    if RX["ask"].search(t) or r.earliest_start is not None:
        it.append("ASK_OPTIONS")
    if RX["status"].search(t) and "REPORT_DELAY" not in it:
        it.append("CHECK_STATUS")
    if (r.latest_gate_out or r.earliest_start) and not it:
        it.append("ADD_CONSTRAINT")
    if not it and (r.shipment_refs or r.order_suffixes):
        it.append("IDENTIFY_SHIPMENT")
    seen = set()
    r.intents = [x for x in it if not (x in seen or seen.add(x))]
    return r


def redact(text: str) -> str:
    """PII redaction for logs and LLM calls."""
    t = RX["phone"].sub("[PHONE]", text)
    t = RX["email"].sub("[EMAIL]", t)
    t = RX["aadhaar"].sub("[ID]", t)
    return t
