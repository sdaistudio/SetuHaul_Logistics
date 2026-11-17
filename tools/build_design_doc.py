"""Build docs/07_system_design_L1_L2.md.

Prose is written here; the tool catalogue, the named-query catalogue and the
per-situation data-fetch table are generated from the code (tool.SPECS,
database.Q) and from the walkthrough run (docs/walkthrough/situations.json),
so the document cannot drift from what the system actually does.

    python tools/situation_walkthrough.py && python tools/build_design_doc.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from setuhaul import database as D, model as M, tool as T  # noqa: E402
from setuhaul.rules import R  # noqa: E402

W = json.loads((ROOT / "docs" / "walkthrough" / "situations.json").read_text(encoding="utf-8"))


def tools_table() -> str:
    rows = ["| Tool | Kind | Caller | Purpose | Inputs | Output | Guardrail |", "|---|---|---|---|---|---|---|"]
    for s in T.SPECS.values():
        ins = ", ".join(f"{k}: {v}" for k, v in s.inputs.items())
        rows.append(f"| `{s.name}` | {s.kind} | {s.caller} | {s.purpose} | {ins} | {s.output} | {s.guard} |")
    return "\n".join(rows)


def queries_table() -> str:
    rows = ["| Named query | What it answers | Tables |", "|---|---|---|"]
    import re
    for k, (purpose, sql) in D.Q.items():
        tabs = sorted(set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", sql, re.I)))
        rows.append(f"| `{k}` | {purpose} | {', '.join(tabs)} |")
    return "\n".join(rows)


def ladder_table() -> str:
    rows = ["| Level | Case (driver) | Model step (language only) | Tools called, in order | Key tool result | Tables read | Tables written | States |",
            "|---|---|---|---|---|---|---|---|"]
    for lv in W["levels"]:
        groups = {}
        for t in lv["turns"]:
            groups.setdefault(t["thread"] + ("~" if t["driver"] == "OTHER" else ""), []).append(t)
        for thr, ts in groups.items():
            und, tools, rd, wr, outs, key = [], [], [], [], [], []
            who = next((t["driver"] for t in ts if t["driver"] != "OTHER"), "OTHER")
            for t in ts:
                und += [m["result"] for m in t["model"] if m["name"] == "understand"]
                for c in t["tools"]:
                    n = c["name"] + ("" if c["ok"] else " ✗")
                    if not tools or tools[-1] != n:
                        tools.append(n)
                    if c["name"] in ("check_slot_feasibility", "escalate_to_human", "confirm_booking") or not c["ok"]:
                        key.append(c["result"])
                rd += [x for x in t["tables_read"] if x not in rd]
                wr += [x for x in t["tables_written"] if x not in wr]
                outs.append("duplicate suppressed" if t["duplicate"] else
                            ("parallel booking " + t["state"].lower()) if t["driver"] == "OTHER" else t["state"].replace("_", " ").lower())
            label = str(lv["level"]) if len(groups) == 1 else f"{lv['level']}{thr.rstrip('~')[-1]}" + ("′ parallel booking" if thr.endswith("~") else "")
            title = lv["title"] if (len(groups) == 1 or label.endswith("a")) else "〃"
            rows.append(f"| {label} | {title} ({who}) | {' / '.join(und) or '-'} | "
                        f"{' → '.join(f'`{x}`' for x in tools) or 'none'} | {' · '.join(dict.fromkeys(key)) or '-'} | "
                        f"{', '.join(rd[:8])}{' …' if len(rd) > 8 else ''} | {', '.join(wr)} | {' → '.join(outs)} |")
    s = W["scheduler"]
    rows.append(f"| + | {s['title']} | none (structured data only) | `propose_facility_sequence` | "
                f"recommend {s['proposal']['recommended']['policy']} | {', '.join(s['tables_read'][:8])} … | "
                f"{', '.join(s['tables_written']) or 'none'} | proposal for a coordinator |")
    return "\n".join(rows)


def evidence_table() -> str:
    rows = ["| Level | Where to look | Named queries that frame the case |", "|---|---|---|"]
    for lv in W["levels"]:
        rows.append(f"| {lv['level']} | `{lv['where']}` | " + ", ".join(f"`{e['query']}({e['arg']})` → {e['rows']} row(s)"
                                                                for e in lv["evidence"]) + " |")
    return "\n".join(rows)


def main():
    pw = R()["priority_weight"]
    tm = R()["timing"]
    sched = W["scheduler"]["proposal"]
    pol_rows = "\n".join(f"| {p['policy']} | {p['metrics']['unscheduled']} | {p['metrics']['missed_appointments']} | "
                         f"{p['metrics']['weighted_wait']} | {p['metrics']['schedule_changes']} | {p['metrics']['avg_wait_min']} |"
                         for p in sched["policies"])
    l5 = next(x for x in W["levels"] if x["level"] == 5)["extra"]
    l1 = next(x for x in W["levels"] if x["level"] == 1)["extra"]
    doc = f"""# SetuHaul: L1 and L2 system design

This document has two levels of detail, so each reader can stop where they have what they need.

* **L1 (what the system does, and who decides what)** is for the head of operations, the warehouse planner and anyone deciding whether to trust it.
* **L2 (how it works)** is for engineers: the components, contracts, data access, concurrency and tests. It names the three code layers: [`setuhaul/model.py`](https://github.com/sdaistudio/SetuHaul_Logistics/blob/main/setuhaul/model.py), [`setuhaul/tool.py`](https://github.com/sdaistudio/SetuHaul_Logistics/blob/main/setuhaul/tool.py) and [`setuhaul/database.py`](https://github.com/sdaistudio/SetuHaul_Logistics/blob/main/setuhaul/database.py), plus the facility scheduling tool [`setuhaul/scheduling_tool.py`](https://github.com/sdaistudio/SetuHaul_Logistics/blob/main/setuhaul/scheduling_tool.py).

Every table marked *generated* below is produced from the code and from a recorded run (`tools/build_design_doc.py`), not typed by hand.

## How the brief maps to this design

| Brief: Learn (FDE judgement) | Where it is handled |
|---|---|
| Frame an unclear operations problem from a messy data package | Problem definition sheet + 20 data issues, each proved by SQL ([problem.html](problem.html), [audit.html](audit.html)) |
| Decide which choices belong to a model, a tool, a database or a person | L1 §3 decision split; L2 §6 to §8 layer contracts |
| Reason about stale reads, concurrent requests and promises | L2 §10: holds, revalidation at commit, unique indexes, idempotency, dedupe |
| Weigh priority, waiting time, fairness and appointment commitments | L2 §11 allocation policy; L2 §12 facility objective |
| Design safe failure and escalation | L1 §5; L2 §13 escalation routes and hand-off record |

| Brief: Apply (tools you already have) | Where it is handled |
|---|---|
| Tool calling with structured inputs and outputs | `tool.py` contracts (L2 §7), validated by `tool.call()` |
| Conversation state and memory across turns | `agent_conversations` row with a version number (L2 §9, state machine) |
| The choice between a fixed workflow and an agent | Fixed workflow (L1 §2); the model never chooses the next step |
| Human approval checkpoints | PENDING bookings, swaps and moved promises go to a coordinator (L2 §13) |
| SQL, constraints and transactions | `database.py`: named queries, BEGIN IMMEDIATE, partial unique indexes (L2 §8, §10) |
| Multi-turn evaluation of agent behaviour | Scenario run + eval gates (L2 §15) |

| Brief: Build (what you ship) | Where it is |
|---|---|
| A conversational exception service for drivers | `workflow.py` on top of the three layers; [Prototype](prototype.html) |
| A capacity layer that holds, confirms and releases slots | `hold_slot` / `confirm_booking` / `release_hold` (L2 §10) |
| An escalation path to a human coordinator | `escalate_to_human` + [Demo 2](demo2.html#conversations) queue |
| A test that tries to double-book capacity and fails | `tests/test_integrity.py` (10-driver race, replays) + level 5 below |
| Optional: a facility-level scheduling tool | `scheduling_tool.py` (L2 §12) |

## The workflow we followed

![How the work flows](design/workflow.svg)

| Step | Hand-in | Proves | Link |
|---|---|---|---|
| 1 Frame | Problem Definition Sheet and data audit | Facts vs estimates vs assumptions vs unknowns; every data issue with the query that proves it | [Problem](problem.html) · [Audit](audit.html) |
| 2 Design | Decision register + this L1/L2 design | Answer, reason and evidence for each decision D1 to D13 | [Decisions](decisions.html) · this page |
| 3 Build | Working prototype | Conversation layer, the tools it calls and the state it keeps (versions in README) | [Prototype](prototype.html) · [code](https://github.com/sdaistudio/SetuHaul_Logistics) |
| + Optional | Facility scheduling tool | Explicit objective vs simple dispatch rules on the competing-trucks snapshot | L2 §12 |
| 4 Break | Integrity proof | Competing requests for the same slot, no double booking | `tests/test_integrity.py`, L2 §10 |
| 5 Defend | Scenario run + client readout | Demonstration list ending in an escalation; policy, limits and open risks | [Scenario run](scenarios.html) · [Readout](readout.html) |

The scenario run covers all seven points the brief lists: (1) a delay plus clarification answers (DRV004, DRV010); (2) later options asked for and compared (DRV001); (3) several requests on one facility schedule; (4) competition for the same capacity (DRV006 and DRV012 on D4 12:30); (5) an option that disappears (OPTION_GONE, then fresh options); (6) an escalation because no safe automatic outcome exists (SHP1015 reefer); (7) the scheduling tool re-planning before and after an ETA update or gate check-in.

---

# L1: what the system does

## 1. Problem and scope

On 4 August at the Jaipur DC, many inbound drivers report delays at once on WhatsApp, in English, Hindi or Hinglish. Each wants a dock slot. Docks have hard limits: dock type, the reefer dock D5, weight limits, maintenance and the 21:00 last-start rule. Some trucks are already in the yard or unloading. The service must answer each driver quickly, offer only slots the truck can really make, never promise the same capacity twice, and hand anything unsafe to a person.

**In scope:** inbound driver exceptions (delay, early arrival, options, status) and slot rebooking at one facility; coordinator approvals and escalations; a facility re-plan proposal.
**Out of scope:** route planning, outbound dispatch, payments and penalties (escalated as `PENALTY_OR_DISPUTE`).

## 2. A fixed workflow, not an autonomous agent

The next step is always chosen by code (a state machine), never by a language model. The only uncertain step is reading the driver's words, and that is done by a deterministic parser with a narrow, validated LLM fallback. Reasons: a wrong slot costs a missed unload or a double booking; behaviour must be explainable to a planner; and the same input must give the same answer so it can be tested.

## 3. Who decides what

![L1 system context](design/l1_context.svg)

| Decision | Belongs to | Why |
|---|---|---|
| What did the driver mean (ETA, delay, constraint, choice, language)? | **Model** (`model.py`) | Only language is fuzzy; output is structured, with evidence spans |
| Is something missing (which shipment, what time)? | **Model** proposes, **workflow** asks | Ask before calling a tool (ladder level 2) |
| Can this truck use this slot? | **Tool** `check_slot_feasibility` | Hard rules R1 to R8 from data and `rules.yaml`; every rejection has a reason code |
| Which slot is best for the driver and the facility? | **Tool** `rank_options_lookahead` | Branch-and-bound look-ahead across all trucks; priority weights CRITICAL {pw['CRITICAL']}, HIGH {pw['HIGH']}, NORMAL {pw['NORMAL']}, LOW {pw['LOW']} |
| Is the slot still free right now? | **Database** | Unique indexes and a write transaction, not the model's memory |
| Book it | **Tool** `hold_slot` → `confirm_booking` | Re-checks feasibility at commit; idempotent |
| A low-confidence ETA, conflicting data, a start after 21:00, moving someone's reachable booking, no feasible slot, safety | **Person** | Policy and accountability decisions; the system prepares the facts and a recommendation |

## 4. Containers on AWS

The production layout is in the [architecture diagram](architecture.svg) and [solution design](design.html). In short: API Gateway receives the WhatsApp webhook; a FIFO SQS queue keyed per driver keeps one driver's messages in order; a worker Lambda runs the workflow; Aurora PostgreSQL stores state (an EXCLUDE constraint on dock intervals); EventBridge runs the sweeper (expires holds and pending bookings); a coordinator API sits behind Cognito; SNS sends notifications; CloudWatch holds metrics, traces and alarms. Bedrock (with a Guardrail) is used only for the language fallback.

## 5. Safe failure and escalation (L1)

* If unsure what the driver meant, ask (at most 2 clarification questions), then hand over to a person.
* If no feasible slot exists, say so, escalate with the facts and the options tried, and tell the driver not to go to the dock until a person confirms.
* If a chosen slot was taken meanwhile, say so and offer fresh options. Never confirm a stale option.
* If a reply would claim a booking that was not committed, or contain a time no tool produced, block it and escalate.
* Safety messages (accident, injury) go straight to the shift manager.
* If a notification fails (OM004 e-mail FAILED in the seed data), the escalation stays OPEN in the queue and is visible on the coordinator dashboard; it is not lost.

## 6. Non-functional targets

| Concern | Target / mechanism |
|---|---|
| Latency | median time to a usable answer 2 min in the scenario run; solver look-ahead budget {R()['solver']['chat_time_budget_ms']} ms per candidate |
| Consistency | one live hold per slot and per shipment; revalidation at commit; zero double bookings in the race test |
| Determinism | same message + same database → same reply; ties broken by start time, then dock code, then slot id |
| Configurability | every threshold in `rules/rules.yaml` (hold TTL {tm.get('hold_ttl_min', 5)} min, gate-to-dock {tm['gate_to_dock_min']} min, ETA buffers, weights, solver budgets) |
| Observability | trace spans per turn, tool-call record, metrics (OptionsShown, OptionGone, Escalations, GuardrailBlocks) |
| Privacy | PII redacted before any LLM call and in logs; drivers only see their own shipments |

---

# L2: how it works

## 7. Components

![L2 components](design/l2_components.svg)

* **`model.py` (language).** It **may**: {', '.join(M.MAY)}. It **may not**: {', '.join(M.MAY_NOT)}. A unit test checks that `model.py` imports no database, capacity or solver module and runs no SQL.
* **`tool.py` (decisions).** Deterministic functions with structured inputs and outputs. `call()` checks that the tool exists, that the caller may use it (drivers' workflow vs coordinator vs scheduler), that the conversation state allows it, and that required inputs are present. The workflow uses the same traced functions, so each turn records which tools ran.
* **`database.py` (facts and integrity).** Named read queries, write helpers that only run inside `write_txn` (BEGIN IMMEDIATE), and a SQL capture that lists the tables each turn touched.
* **`workflow.py`** is the state machine that calls the model and the tools in a fixed order. **`scheduling_tool.py`** is the facility-level re-plan.

### 7.1 Tool catalogue *(generated from `tool.SPECS`)*

{tools_table()}

## 8. Data access *(generated from `database.Q`)*

Every fact comes from a named, parameterised query. The supplied views (`v_latest_eta`, `v_slot_availability`) are only used for cross-checks, because the audit showed they can mislead (for example `v_latest_eta` reports the original plan as HIGH confidence).

{queries_table()}

Tables added to the supplied schema: `slot_holds`, `appointment_plan`, `agent_conversations`, `escalations`, `idempotency_keys`, `inbound_dedupe`, `audit_log` (see `setuhaul/db.py`). The supplied snapshot is never modified; each run works on a copy.

## 9. One message, end to end

![L2 turn sequence](design/l2_turn.svg)

![L2 state machines](design/l2_states.svg)

Conversation memory is one `agent_conversations` row per thread: state, language, remembered facts (shipment, ETA, constraints, options shown, clarification count) and a version number that is checked on save (optimistic concurrency). Facts said in an earlier turn are kept, so "शाम वाला" (the evening one) after a question resolves the shipment without the driver repeating the delay.

## 10. Stale reads, concurrent requests and promises

| Risk | Mechanism | Evidence |
|---|---|---|
| Two drivers pick the same slot | Hold placed inside BEGIN IMMEDIATE; partial unique index `ux_live_hold_per_slot`; the second gets OPTION_GONE | 10-driver race in `tests/test_integrity.py`; level 5 race: {l5.get('race_same_slot')} |
| Options shown, then the world changes | Options hold nothing; `confirm_booking` re-runs feasibility inside the write transaction | Level 5: DRV012 picks D4 12:30 after a parallel booking took it → "just taken" + fresh options |
| A retried confirm (network retry) | `idempotency_keys`: same key returns the first result | Level 5: first {l5.get('idempotent_retry', {}).get('first')} and retry {l5.get('idempotent_retry', {}).get('second')}, replay = {l5.get('idempotent_retry', {}).get('replay')} |
| WhatsApp re-delivers a message (THR001 / THR009) | `inbound_dedupe` on external id and normalised text within a window | Level 5: second copy suppressed, no tool called |
| Asking is mistaken for booking | Options are READ only; a booking needs an explicit choice | Level 1: appointments created {l1.get('writes_to_capacity', {}).get('appointments_created')}, holds created {l1.get('writes_to_capacity', {}).get('holds_created')} |
| A booking that can no longer be reached blocks a slot | Certain-miss release: if the owner's ETA is past the slot end, the slot is released (and the owner's dispatcher is told) | `capacity.commit_hold`, solver tests |
| Holds or pending bookings left forever | TTL: hold {tm.get('hold_ttl_min', 5)} min, pending approval 20 min; the sweeper expires them | `capacity.expire_stale` |
| Production | Postgres `EXCLUDE USING gist (dock_id WITH =, tstzrange(start, end) WITH &&)` | `infra/postgres_schema.sql` |

## 11. Allocation policy: priority, waiting, fairness, commitments

1. **Hard rules first** (R1 to R8): compatible dock type, reefer to D5, weight limit, dock maintenance or blocks, physical occupancy, live holds, the 21:00 rule (needs approval), and the driver's own constraints (not before X, must leave by Y).
2. **Keep promises:** a reachable booking is not moved because someone else asks. Moving one is a coordinator decision (a swap proposal).
3. **Priority comes from data only** (`shipments.priority_code`), never from the message. "Load bahut zaroori hai" gets a polite note, not a higher rank.
4. **Look-ahead, not first-come:** each feasible slot for this driver is scored by solving the rest of the facility as if the driver took it (branch and bound). The driver gets the slots that strand nobody else and keep total priority-weighted waiting lowest.
5. **Fairness:** the same rules, weights and tie-breaks for every driver; LOW-priority trucks are delayed only when the weighted cost says so, and never left without a dock if one exists.

## 12. Facility-level scheduling tool

`scheduling_tool.propose_sequence()` is called (through `tool.propose_facility_sequence`) after an ETA update or a gate check-in, or by a coordinator. It reads structured data only and **never writes**: the run below checked `wrote_to_database = {sched['wrote_to_database']}`.

**Model:** job = truck; machine = dock; release = planning ETA + {tm['gate_to_dock_min']} min gate-to-dock (or now if already in the yard); processing = expected unload minutes; due = current booking; fixed = trucks already unloading (protected work) and dock events.

**Objective, in order:** {' > '.join(sched['objective'])}. Weights per minute of waiting: CRITICAL {pw['CRITICAL']}, HIGH {pw['HIGH']}, NORMAL {pw['NORMAL']}, LOW {pw['LOW']}.

**Comparison on the 09:50 snapshot** *(generated)*:

| Policy | Unscheduled | Missed appointments | Weighted wait | Schedule changes | Avg wait (min) |
|---|---|---|---|---|---|
{pol_rows}

Recommended: **{sched['recommended']['policy']}**. Protected work: {'; '.join(f"{x['dock']} {x['from']}-{x['to']} {x['what']}" for x in sched['protected'])}. Every move of a reachable booking, every swap and every start after 21:00 is listed for a coordinator. Before and after an event:

{chr(10).join(f"* {'; '.join(w['events'])}: " + ('; '.join(f"{c['shipment']} {c['before']} → {c['after']}" for c in w['changes']) or 'no change') for w in W['scheduler']['what_if'])}

Run it: `python -m setuhaul.scheduling_tool --at 09:50` or `python -m setuhaul.scheduling_tool --at 09:50 --eta SHP1009=12:20`.

## 13. Human approval checkpoints and escalation

| Checkpoint | Trigger | Who | What they receive |
|---|---|---|---|
| Pending booking | LOW-confidence ETA, data conflict, start after 21:00 | Warehouse coordinator | Booking, reasons, 20-minute TTL; approval is re-validated (an infeasible slot is refused) |
| Moved promise / swap | Scheduler proposes moving a reachable booking | Warehouse coordinator | Before/after, who is displaced, driver notice needed |
| No feasible slot | Feasibility returns nothing | Coordinator (night shift after 18:00) | Facts, ETA, constraints, rejection summary, options tried, recommendation |
| Safety | Accident or injury words | Shift manager | Redacted message; SLA 5 min |
| Unclear after 2 tries / guard block / rate limit | Workflow | Coordinator | Last message, what was asked |

Routing skips vacant roles and contacts without a channel (`facility_contacts`), and falls back to the ops queue.

## 14. How data is fetched in each situation *(generated from a recorded run)*

Each level of the brief's ladder was run through the production workflow on a fresh copy of the database. The model column shows what the language layer extracted; the other columns come from the tool-call record and SQLite's statement trace. Full turn-by-turn detail, including replies, is on the [situation walkthrough](walkthrough.html) page.

{ladder_table()}

Note on level 2a: "late by one hour" is parsed by the model as now + 60 min; the workflow then anchors a relative delay to the shipment's planned ETA (10:00 + 60 = 11:00) and states that assumption in the reply, so the driver can correct it.

**Data used to frame each case:**

{evidence_table()}

## 15. Evaluation and break tests

* **Unit and integrity tests** (`tests/`): 10-driver race on one slot, replayed confirms, duplicate deliveries, certain-miss release, solver optimality, and the layer contracts in `tests/test_layers.py` (model has no data access, asking writes nothing, write tools blocked in the wrong state, scheduler writes nothing).
* **Eval gates** (`evals/run_evals.py`, run in CI): NLU on tuned and held-out sets (EN, HI, Hinglish), feasibility golden cases, guardrail cases, solver vs OR-Tools CP-SAT. See [evals](evals.html).
* **Multi-turn scenario run** (`scenarios/run_scenarios.py`): 12 conversations replayed against one facility, ending in an escalation, with metrics (resolved without takeover, time to usable answer, double bookings = 0). See [scenario run](scenarios.html).

## 16. Library versions

Python 3.10+ (tested on 3.13), PyYAML 6.0, OR-Tools 9.15 (optional, for the CP-SAT cross-check), Markdown 3.x (site build only). No other runtime dependencies: SQLite is from the Python standard library.
"""
    out = ROOT / "docs" / "07_system_design_L1_L2.md"
    out.write_text(doc, encoding="utf-8")
    print("written", out, len(doc), "chars")


if __name__ == "__main__":
    main()
