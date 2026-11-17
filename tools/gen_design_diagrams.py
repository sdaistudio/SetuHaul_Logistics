"""Draw the L1 / L2 design diagrams as SVG (docs/design/*.svg). Plain SVG, no dependencies."""
from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "design"
INK, MU, LN = "#1c1917", "#6b6560", "#d6d3d1"
C = {"model": ("#f3e8ff", "#7e22ce"), "tool": ("#fce7f3", "#be185d"), "db": ("#e0f2fe", "#0369a1"),
     "person": ("#fef3c7", "#b45309"), "ext": ("#f5f5f4", "#57534e"), "core": ("#ecfdf5", "#047857"),
     "dark": ("#0b1020", "#0b1020")}


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.el = w, h, []
        self.el.append(f'<text x="24" y="34" font-size="19" font-weight="700" fill="{INK}">{escape(title)}</text>')

    def box(self, x, y, w, h, title, lines=(), kind="ext", fs=12.5, head=None):
        bg, fg = C[kind]
        dark = kind == "dark"
        self.el.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{bg}" stroke="{fg}" stroke-width="1.4"/>')
        if head:
            self.el.append(f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="10" fill="{fg}"/>'
                           f'<rect x="{x}" y="{y + 16}" width="{w}" height="10" fill="{fg}"/>'
                           f'<text x="{x + 12}" y="{y + 18}" font-size="12.5" font-weight="700" fill="#fff">{escape(head)}</text>')
            y += 26
        self.el.append(f'<text x="{x + 12}" y="{y + 21}" font-size="{fs + 1}" font-weight="700" '
                       f'fill="{"#fff" if dark else fg}">{escape(title)}</text>')
        for i, ln in enumerate(lines):
            self.el.append(f'<text x="{x + 12}" y="{y + 41 + i * (fs + 4.5)}" font-size="{fs}" '
                           f'fill="{"#e7e5e4" if dark else INK}">{escape(ln)}</text>')

    def arrow(self, pts, label="", color=MU, dash=False, lx=None, ly=None, anchor="middle"):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        self.el.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6" marker-end="url(#a)"'
                       f'{" stroke-dasharray=\"5 4\"" if dash else ""}/>')
        if label:
            (x1, y1), (x2, y2) = pts[0], pts[-1]
            lx = lx if lx is not None else (x1 + x2) / 2
            ly = ly if ly is not None else (y1 + y2) / 2 - 6
            for i, part in enumerate(label.split("\n")):
                self.el.append(f'<text x="{lx}" y="{ly + i * 14}" font-size="11.5" fill="{color}" text-anchor="{anchor}" '
                               f'paint-order="stroke" stroke="#fff" stroke-width="4">{escape(part)}</text>')

    def text(self, x, y, s, size=12, color=MU, weight=400, anchor="start"):
        self.el.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" '
                       f'text-anchor="{anchor}">{escape(s)}</text>')

    def band(self, x, y, w, h, label, color):
        self.el.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="none" stroke="{color}" '
                       f'stroke-width="1.3" stroke-dasharray="6 5"/>'
                       f'<text x="{x + 14}" y="{y + 18}" font-size="12" font-weight="700" fill="{color}">{escape(label)}</text>')

    def save(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" '
               f'font-family="Inter, Segoe UI, Helvetica, Arial, sans-serif"><defs><marker id="a" viewBox="0 0 10 10" refX="9" '
               f'refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{MU}"/>'
               f'</marker></defs><rect width="100%" height="100%" fill="#fff"/>' + "".join(self.el) + "</svg>")
        (OUT / name).write_text(svg, encoding="utf-8")


def workflow():
    s = Svg(1240, 330, "How the work flows: five steps, each ends in a hand-in")
    steps = [("1 Frame", ["Problem Definition Sheet", "+ data audit (20 issues,", "each with its SQL)"], "#be185d"),
             ("2 Design", ["Decision register D1-D13", "+ L1 / L2 architecture", "(this document)"], "#be185d"),
             ("3 Build", ["Service, tools, slot state:", "model.py / tool.py /", "database.py + workflow"], "#be185d"),
             ("4 Break", ["Concurrency + failure tests", "on our own system", "(10-driver race, replays)"], "#be185d"),
             ("5 Defend", ["Scenario run (ends in", "escalation) + client", "readout"], "#0b1020")]
    for i, (t, ls, col) in enumerate(steps):
        x = 24 + i * 244
        s.box(x, 60, 220, 120, t, ls, "dark" if i == 4 else "tool", fs=12.5)
        if i < 4:
            s.arrow([(x + 220, 120), (x + 244, 120)])
    s.el.append('<rect x="490" y="205" width="490" height="54" rx="12" fill="#fdf2f8" stroke="#be185d" stroke-dasharray="6 5"/>')
    s.text(506, 228, "OPTIONAL EXTENSION (between Build and Break)", 12, "#be185d", 700)
    s.text(506, 247, "Facility-level scheduling tool, called by the workflow as a controlled READ tool", 12, INK)
    s.arrow([(735, 180), (735, 205)], dash=True)
    s.text(24, 300, "Hand-ins: 1 Problem sheet + audit  ·  2 Decision register  ·  3 Working prototype  ·  4 Scenario run"
           "  ·  5 Integrity proof  ·  6 Client readout  ·  + Scheduling tool", 12.5, MU)
    s.save("workflow.svg")


def l1_context():
    s = Svg(1240, 560, "L1 · System context: who talks to the service and what it decides")
    s.box(24, 70, 220, 110, "Driver (WhatsApp)", ["EN · Hindi · Hinglish", "reports delay, asks options,", "picks a slot, early arrival"], "person")
    s.box(24, 220, 220, 100, "Gate / yard staff", ["gate-in, dock-in, unload", "start/end (check-ins)"], "person")
    s.box(24, 360, 220, 120, "Warehouse coordinator", ["approves PENDING bookings,", "handles escalations, sees", "control tower (Demo 2)"], "person")
    s.band(300, 56, 640, 470, "SetuHaul driver exception service (deterministic workflow)", "#047857")
    s.box(326, 90, 180, 110, "Model layer", ["understand words", "ask what is missing", "phrase reviewed replies"], "model", head="model.py")
    s.box(530, 90, 180, 110, "Tool layer", ["feasibility, look-ahead,", "hold / confirm / release,", "escalate, re-plan"], "tool", head="tool.py")
    s.box(734, 90, 180, 110, "Data layer", ["named queries,", "transactions, unique", "indexes, idempotency"], "db", head="database.py")
    s.arrow([(506, 145), (530, 145)])
    s.arrow([(710, 145), (734, 145)])
    s.box(326, 230, 588, 96, "Workflow (state machine)",
          ["NEW → NEED_SHIPMENT → NEED_ETA → OPTIONS_SHOWN → CONFIRMED | PENDING",
           "any → ESCALATED: safety, asks for a person, no feasible slot, unclear x2",
           "Guardrails on input, on every tool call and on every reply"], "core")
    s.box(326, 350, 280, 150, "Facility scheduling tool", ["one facility, all trucks, one objective;",
                                                             "FCFS / EDD / weighted / commitment-",
                                                             "first vs optimal (branch & bound);",
                                                             "proposes, never books"], "tool", head="scheduling_tool.py")
    s.box(634, 350, 280, 150, "rules/rules.yaml", ["priority weights, buffers, hold TTL,",
                                                   "dock compatibility, 21:00 rule,", "solver budgets, RAG thresholds:",
                                                   "every variable configurable"], "ext", head="configuration")
    s.box(996, 70, 220, 120, "Freight operations DB", ["shipments, slots, docks,", "appointments, check-ins,", "ETA updates, rules, contacts",
                                                       "(+ holds, idempotency, audit)"], "db")
    s.box(996, 230, 220, 100, "WhatsApp Business API", ["inbound webhook,", "approved templates outbound"], "ext")
    s.box(996, 360, 220, 120, "GPS / FASTag / OSRM", ["location on demand (GPS first,", "FASTag toll watchdog second,", "ask driver last)"], "ext")
    s.arrow([(244, 120), (326, 140)], "message")
    s.arrow([(326, 175), (244, 150)], "reply", lx=285, ly=180)
    s.arrow([(244, 250), (276, 250), (276, 46), (1106, 46), (1106, 70)], "check-in events", dash=True, lx=1010, ly=42)
    s.arrow([(326, 290), (244, 410)], "escalation hand-off", lx=270, ly=338, anchor="start")
    s.arrow([(244, 450), (326, 450)], "approve /\nreject", lx=285, ly=470)
    s.arrow([(914, 145), (996, 130)])
    s.arrow([(996, 280), (914, 290)], "webhook", lx=955, ly=275)
    s.arrow([(996, 420), (914, 420)], "ETA evidence", lx=955, ly=412)
    s.text(24, 548, "Rule: the model never decides a slot. Tools decide with rules from rules.yaml and the database enforces "
           "no double booking. People decide what no safe rule can.", 12.5, INK, 600)
    s.save("l1_context.svg")


def l2_components():
    s = Svg(1240, 640, "L2 · Components: model.py / tool.py / database.py")
    s.band(20, 54, 380, 560, "model.py — LANGUAGE (no data, no decisions)", "#7e22ce")
    s.box(40, 86, 340, 92, "screen_input()", ["length cap, control chars, injection phrases,", "PII redaction for logs / fallback"], "model")
    s.box(40, 190, 340, 110, "understand()", ["rule parser nlu.py: intent, language, ETA,", "window, delay, constraints, option, shipment",
                                               "hints, evidence span; LLM fallback only on", "UNKNOWN (Bedrock, JSON schema, validated)"], "model")
    s.box(40, 312, 340, 70, "missing_facts()", ["which shipment? what time? → ask first"], "model")
    s.box(40, 394, 340, 90, "phrase()", ["reviewed templates EN / HI / Hinglish,", "filled only with tool outputs"], "model")
    s.box(40, 496, 340, 100, "guard_reply()", ["no booking claim without a commit,", "no time no tool produced, no internal ids",
                                                "→ violation = escalate, never send"], "model")
    s.band(430, 54, 380, 560, "tool.py — DECISIONS (deterministic, contracts)", "#be185d")
    reads = [("get_driver_context / find_driver_shipments", "READ"), ("get_eta (check-in > ops > WH > driver > plan)", "READ"),
             ("check_slot_feasibility (R1–R8, reasons)", "READ"), ("rank_options_lookahead (B&B board cost)", "READ"),
             ("get_booking_status", "READ"), ("propose_facility_sequence", "READ")]
    writes = [("record_eta", "WRITE"), ("hold_slot (5 min)", "WRITE"), ("confirm_booking (revalidate, idem key)", "WRITE"),
              ("release_hold / notify_warehouse", "WRITE"), ("escalate_to_human", "WRITE"),
              ("decide_pending_booking (person only)", "WRITE")]
    y = 86
    for name, k in reads + writes:
        bg, fg = C["tool"] if k == "WRITE" else ("#fff", "#be185d")
        s.el.append(f'<rect x="446" y="{y}" width="348" height="34" rx="7" fill="{bg}" stroke="{fg}"/>'
                    f'<text x="458" y="{y + 22}" font-size="12.5" fill="{INK}">{escape(name)}</text>'
                    f'<text x="782" y="{y + 22}" font-size="11" font-weight="700" fill="{fg}" text-anchor="end">{k}</text>')
        y += 42
    s.text(446, 600, "call(): known tool · allowed caller · allowed in state · inputs", 11.5, "#be185d", 600)
    s.band(840, 54, 380, 560, "database.py — FACTS + INTEGRITY", "#0369a1")
    s.box(860, 86, 340, 150, "Named queries Q[...]", ["driver, driver_open_shipments, shipment,", "eta_updates, checkin, current_appointment,",
                                                       "facility_slots, live_holds, dock_events,", "facility_rules, contacts, ops_messages,",
                                                       "thread, thread_messages, agent_state …"], "db")
    s.box(860, 248, 340, 150, "Integrity (enforced by the DB)", ["BEGIN IMMEDIATE write transactions", "unique: one live hold per slot / shipment",
                                                                 "idempotency_keys (retry = same result)", "inbound_dedupe (re-delivery = once)",
                                                                 "prod: Postgres EXCLUDE on dock interval"], "db")
    s.box(860, 410, 340, 90, "Tables added", ["slot_holds, appointment_plan, agent_conversations,", "escalations, idempotency_keys, inbound_dedupe,",
                                              "audit_log"], "db")
    s.box(860, 512, 340, 84, "capture_sql()", ["statement trace → tables read / written", "per turn (situation walkthrough)"], "db")
    s.arrow([(380, 245), (446, 190)], "facts", lx=412, ly=208)
    s.arrow([(446, 300), (380, 430)], "values to\nphrase", lx=412, ly=350)
    s.arrow([(794, 200), (860, 160)], "read", lx=828, ly=172)
    s.arrow([(794, 400), (860, 320)], "write txn", lx=828, ly=352)
    s.save("l2_components.svg")


def l2_turn():
    s = Svg(1240, 560, "L2 · One driver message, end to end (sequence)")
    lanes = [("WhatsApp", 70), ("workflow.py", 230), ("model.py", 420), ("tool.py", 640), ("database.py", 860), ("Person", 1090)]
    for n, x in lanes:
        kind = {"model.py": "model", "tool.py": "tool", "database.py": "db", "Person": "person"}.get(n, "ext")
        s.box(x - 75, 52, 150, 34, n, (), kind, fs=12)
        s.el.append(f'<line x1="{x}" y1="86" x2="{x}" y2="545" stroke="{LN}" stroke-dasharray="4 4"/>')
    X = dict(lanes)
    steps = [("WhatsApp", "workflow.py", "message + external id"),
             ("workflow.py", "database.py", "dedupe? (inbound_dedupe)"),
             ("workflow.py", "model.py", "screen_input + understand"),
             ("model.py", "workflow.py", "intent, ETA, constraints, missing facts"),
             ("workflow.py", "tool.py", "get_driver_context / find shipments, get_eta"),
             ("tool.py", "database.py", "named reads"),
             ("workflow.py", "tool.py", "check_slot_feasibility → rank_options_lookahead"),
             ("workflow.py", "model.py", "phrase(options) + guard_reply"),
             ("workflow.py", "WhatsApp", "options (nothing booked yet)"),
             ("WhatsApp", "workflow.py", "'1'"),
             ("workflow.py", "tool.py", "hold_slot → confirm_booking (revalidate)"),
             ("tool.py", "database.py", "BEGIN IMMEDIATE · unique index · idem key"),
             ("workflow.py", "Person", "PENDING approval / escalation hand-off"),
             ("workflow.py", "WhatsApp", "confirmed / pending / option gone + fresh options")]
    y = 112
    for a, b, lab in steps:
        x1, x2 = X[a], X[b]
        col = "#b45309" if b == "Person" else MU
        s.arrow([(x1, y), (x2 + (-6 if x2 > x1 else 6), y)], lab, color=col, lx=(x1 + x2) / 2, ly=y - 6)
        y += 31
    s.save("l2_turn.svg")


def l2_states():
    s = Svg(1240, 430, "L2 · State machines: conversation and capacity")
    conv = [("NEW", 30, 80), ("NEED_SHIPMENT", 200, 80), ("NEED_ETA", 400, 80), ("OPTIONS_SHOWN", 580, 80),
            ("CONFIRMED", 800, 50), ("PENDING_WAREHOUSE", 800, 120), ("ESCALATED", 400, 190), ("CLOSED", 30, 190)]
    for n, x, y in conv:
        kind = "person" if n == "ESCALATED" else "core" if n in ("CONFIRMED",) else "model" if n.startswith("NEED") else "ext"
        s.box(x, y, 160 if n != "PENDING_WAREHOUSE" else 190, 44, n, (), kind, fs=12)
    s.arrow([(190, 102), (200, 102)])
    s.arrow([(360, 102), (400, 102)], "shipment\nknown", lx=380, ly=125)
    s.arrow([(560, 102), (580, 102)], "ETA known", lx=570, ly=74)
    s.arrow([(740, 95), (800, 72)], "pick + commit", lx=765, ly=50)
    s.arrow([(740, 110), (800, 142)], "LOW conf / conflict /\nafter 21:00", lx=700, ly=150)
    s.arrow([(640, 124), (480, 190)], "no feasible slot", color="#b45309", lx=520, ly=150)
    s.arrow([(110, 124), (400, 212)], "", color="#b45309")
    s.text(220, 262, "safety · asks for a person · unclear after 2 tries · guard block", 11.5, "#b45309")
    s.arrow([(800, 158), (900, 200)], "", dash=True)
    s.text(860, 215, "coordinator approves (re-validated) / rejects", 11.5, "#b45309")
    s.text(30, 280, "Capacity", 15, INK, 700)
    cap = [("SHOWN", 30), ("HELD 5 min", 230), ("PENDING ≤20 min", 450), ("CONFIRMED", 690), ("RELEASED / EXPIRED", 910)]
    for n, x in cap:
        s.box(x, 300, 180, 44, n, (), "db" if n != "CONFIRMED" else "core", fs=12)
    s.arrow([(210, 322), (230, 322)], "hold_slot", lx=220, ly=360)
    s.arrow([(410, 322), (450, 322)], "confirm\n(approval)", lx=430, ly=360)
    s.arrow([(630, 322), (690, 322)], "approve", lx=660, ly=360)
    s.arrow([(870, 322), (910, 322)], "TTL / reject /\ncertain miss", lx=890, ly=362)
    s.text(30, 395, "Options shown hold nothing. Only a hold reserves capacity; every commit re-checks feasibility inside a write transaction,", 12.5, INK)
    s.text(30, 413, "so a stale option fails safely (OPTION_GONE → fresh options). With no approval needed, HELD goes straight to CONFIRMED.", 12.5, INK)
    s.save("l2_states.svg")


if __name__ == "__main__":
    workflow(); l1_context(); l2_components(); l2_turn(); l2_states()
    print("diagrams:", sorted(p.name for p in OUT.glob("*.svg")))
