"""Reading the driver's words: deterministic first, model only as a fallback.

detect_lang(): Unicode script share + a Hinglish word list. No model call.
extract():     regular expressions turn text into a fact sheet. Every value
               keeps the text it came from (evidence), so a reviewer can see
               why the system believed it.

When extract() returns intent UNKNOWN or a time it cannot place, the design
calls an LLM with a strict JSON schema (see deliverable 3, section 'LLM
fallback'). The prototype runs without network access, so here that branch
asks the driver a clarifying question instead. A wrong guess is never booked.
"""
import re
from dataclasses import dataclass, field
from . import config as C
from .clock import at, add

DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
HINGLISH = set("""hai hain hoon hu nahi nahin bhai bhaiya kitna kitne der deri pahunch pahuncha pahunchunga pahunch jaunga
pahunchenge gaadi gadi jaldi kal abhi ghanta ghante mein ke baad wala wali shaam sham subah raat milega milegi chahiye
karo theek thik haan accha acha aaj kya kab tak lagbhag thoda puncture gaya gayi raha rahi hoga jaunga jayega lagi
lag ko se pe par wale doosra dusra pehla teesra""".split())


def detect_lang(text, previous="en"):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return previous
    dev = sum("ऀ" <= c <= "ॿ" for c in letters) / len(letters)
    if dev > 0.3:
        return "hi"
    words = re.findall(r"[a-z]+", text.lower())
    hits = sum(w in HINGLISH for w in words)
    if len(words) <= 2 and hits == 0:
        return previous            # "ok", "yes", "1": keep the thread's language
    if hits >= 2 or (hits == 1 and len(words) <= 4):
        return "hi-Latn"
    return "en"


@dataclass
class Facts:
    intent: str = "UNKNOWN"
    eta: object = None
    eta_window: tuple = None
    eta_confidence: str = None
    delay_min: int = None
    earliest_start: object = None
    limits: dict = field(default_factory=dict)
    reason: str = None
    choice: int = None
    decline: bool = False
    yes: bool = False
    order_ref: str = None
    hint: str = None
    urgency_claim: bool = False
    safety: bool = False
    asks_human: bool = False
    vague_delay: bool = False
    next_commitment: bool = False
    evidence: dict = field(default_factory=dict)


def _clock(h, m, ap, as_of):
    h, m = int(h), int(m or 0)
    if ap:
        ap = ap.replace(".", "").lower()
        h = h % 12 + (12 if ap == "pm" else 0)
        return at(as_of, h, m)
    if h > 12:
        return at(as_of, h, m)
    cands = sorted({at(as_of, h % 12, m), at(as_of, h % 12 + 12, m)})
    future = [c for c in cands if c >= add(as_of, -60)]
    return future[0] if future else cands[-1]


TIME_PATTERNS = [
    r"(?P<h>\d{1,2})[:.](?P<m>\d{2})\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?",
    r"(?P<h>\d{1,2})\s*(?P<ap>am|pm|a\.m\.|p\.m\.)",
    r"(?:after|before|by|at|till|until|around|eta|reach(?:ing)?|arrive|for)\s+(?P<h>\d{1,2})(?![\d.:])(?!\s*(?:min|minute|hour|hr|ghant|ton|t\b|kg))",
    r"(?P<h>\d{1,2})\s*(?:baje|bje|बजे)",
    r"(?P<h>\d{1,2})\s*(?:ke baad|ke bad|के बाद)",
]
DUR_WORDS = [
    (r"one and a half hours?|1\.5 hours?|dedh ghant[ae]|डेढ़ घंट[ाे]", 90),
    (r"half an hour|aadha ghanta|आधा घंटा", 30),
    (r"two hours?|2 hours?|do ghante|दो घंटे", 120),
    (r"dhai ghante|ढाई घंटे", 150),
    (r"(?:one|an|1) hour|ek ghanta|एक घंटा", 60),
]


def extract(text, as_of):
    raw = text.translate(DEV_DIGITS)
    t = raw.lower()
    f = Facts()

    f.safety = any(w in t for w in C.SAFETY_WORDS)
    f.asks_human = bool(re.search(r"talk to (a )?(person|human|someone|manager)|call me|insaan se|manager se baat", t))
    m = re.search(r"\bord-\d{6}-\d{3}\b|\bshp\d{4}\b", t)
    if m:
        f.order_ref = m.group(0).upper()

    s = t.strip(" .!?")
    m = re.fullmatch(r"(?:option\s*)?#?([1-9])", s)
    ordinals = {"first": 1, "pehla": 1, "pehli": 1, "1st": 1, "second": 2, "doosra": 2, "dusra": 2, "2nd": 2,
                "third": 3, "teesra": 3, "tisra": 3, "3rd": 3}
    if m:
        f.choice = int(m.group(1))
    else:
        for w, n in ordinals.items():
            if re.search(rf"\b{w}\b", t) and len(t.split()) <= 6:
                f.choice = n
    f.decline = bool(re.search(r"\b(no thanks|no thank you|not now|nahi chahiye|rehne do|no need|keep my (current )?slot|i will keep|i'll keep)\b", t)) \
        or s in ("no", "nahi", "nahin", "नहीं")
    f.yes = s in ("yes", "ok", "okay", "haan", "han", "ha", "theek", "thik", "sure", "confirm", "हाँ")

    if re.search(r"\bevening\b|\bshaam\b|\bsham\b|शाम", t):
        f.hint = "EVENING"
    elif re.search(r"\bmorning\b|\bsubah\b|सुबह", t):
        f.hint = "MORNING"
    if re.search(r"\bheavy\b|\btrailer\b", t):
        f.hint = "HEAVY"
    if re.search(r"\breefer\b|\bcold\b|\bfrozen\b|\bdairy\b|\bchilled\b", t):
        f.hint = f.hint or "REEFER"
    f.urgency_claim = bool(re.search(r"\burgent\b|\bcritical\b|\bemergency\b|\basap\b|first possible|\bjaldi\b", t))

    for pat, code in [(r"traffic|jam|\bjaam\b", "TRAFFIC"), (r"tyre|tire|puncture|breakdown|broke down|repair", "BREAKDOWN"),
                      (r"rain|barish|baarish|fog|flood", "WEATHER"), (r"released me late|loading|origin", "LOADING_DELAY"),
                      (r"diversion|route|road closed", "ROUTE_ISSUE")]:
        if re.search(pat, t):
            f.reason = code
            break

    # ---- durations ("late by 45 minutes", "1.5 ghanta late") ----
    for pat, mins in DUR_WORDS:
        mm = re.search(pat, t)
        if mm:
            f.delay_min, f.evidence["delay"] = mins, mm.group(0)
            break
    if f.delay_min is None:
        mm = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|hr|ghante|ghanta|घंटे|घंटा)", t)
        if mm:
            f.delay_min, f.evidence["delay"] = int(float(mm.group(1)) * 60), mm.group(0)
        else:
            mm = re.search(r"(\d+)\s*(minutes?|mins?|min|मिनट)", t)
            if mm:
                f.delay_min, f.evidence["delay"] = int(mm.group(1)), mm.group(0)

    # ---- clock times, each given a role by the words around it ----
    used, times = [], []
    for pat in TIME_PATTERNS:
        for mm in re.finditer(pat, t):
            if any(a < mm.end() and mm.start() < b for a, b in used):
                continue
            g = mm.groupdict()
            if f.evidence.get("delay") and mm.group(0) in f.evidence["delay"]:
                continue
            used.append((mm.start(), mm.end()))
            times.append((mm.start("h"), mm.end(), _clock(g["h"], g.get("m"), g.get("ap"), as_of), mm.group(0)))
    times.sort()
    win = re.search(r"between\s+(\S+)\s+and\s+(\S+)", t)
    for st, en, dt, txt in times:
        before, after = t[max(0, st - 32):st], t[en:en + 20]
        if win and win.start() <= st <= win.end():
            f.eta_window = (f.eta_window[0], dt) if f.eta_window else (dt, None)
            continue
        if re.search(r"waiting for|my\s*$|\bmy \w* ?$|instead of", before) or re.search(r"^\s*(?:pm|am)?\s*slot", after):
            continue                                        # a reference to the existing slot, not new information
        if re.search(r"after\s*$", before) or re.search(r"^\s*(ke baad|ke bad|के बाद)", after) \
                or re.search(r"ke baad|ke bad|के बाद", txt):
            f.earliest_start = dt
            f.evidence["earliest_start"] = txt
        elif re.search(r"(leave|nikal|exit|gate.?out|depart)[^.]{0,25}$", before) or re.search(r"^\s*tak nikal", after):
            f.limits["LATEST_GATE_OUT"] = dt
            f.evidence["LATEST_GATE_OUT"] = txt
        elif re.search(r"(finish|unload(ed|ing)? by|khatam)[^.]{0,15}$", before):
            f.limits["LATEST_UNLOAD_END"] = dt
            f.evidence["LATEST_UNLOAD_END"] = txt
        elif re.search(r"(before|by)\s*$", before) and not re.search(r"reach|arrive|pahunch", before):
            f.limits["LATEST_START"] = dt
            f.evidence["LATEST_START"] = txt
        elif f.eta is None:
            f.eta = dt
            f.evidence["eta"] = txt
            approx = re.search(r"(around|about|approx|approximately|lagbhag|~|tak)\s*$", before) or "around" in t
            f.eta_confidence = "MEDIUM" if approx else "HIGH"
    if f.eta_window and f.eta_window[1]:
        f.eta, f.eta_confidence = f.eta_window[0] + (f.eta_window[1] - f.eta_window[0]) / 2, "LOW"

    f.next_commitment = bool(re.search(r"another pickup|next pickup|other pickup|another load|next load|doosra load|agla (pickup|load)|dusri pickup", t))

    # ---- intent ----
    if f.safety:
        f.intent = "SAFETY"
    elif f.choice and not f.eta and not f.delay_min:
        f.intent = "CHOOSE"
    elif f.decline:
        f.intent = "DECLINE"
    elif re.search(r"already at (the )?gate|at the gate now|reached early|pahunch gaya.*(pehle|jaldi)|dock now", t):
        f.intent = "EARLY_ARRIVAL"
    elif re.search(r"in the yard|in yard|yard mein|reached late", t):
        f.intent = "AT_YARD_STATUS"
    elif re.search(r"still active|status|has a new dock|been assigned|kab milega|confirmed\?", t) and not f.eta:
        f.intent = "CHECK_STATUS"
    elif re.search(r"what slots|which slots|slots? (are )?possible|kaun ?sa slot|show (me )?options|what about after", t) \
            and not re.search(r"\blate\b|delay|traffic|puncture|लेट", t):
        f.intent = "ASK_OPTIONS"
    elif re.search(r"\blate\b|delay|\bder\b|traffic|jam|puncture|tyre|breakdown|stuck|लेट|देर|diversion|reaching|\beta\b|will miss|be missed|pahunch", t) \
            or f.eta or f.delay_min:
        f.intent = "REPORT_DELAY"
    elif f.hint or f.order_ref or f.choice:
        f.intent = "ANSWER"
    elif f.asks_human:
        f.intent = "ASK_HUMAN"
    if f.intent == "REPORT_DELAY" and not f.eta and not f.delay_min:
        f.vague_delay = True
    return f
