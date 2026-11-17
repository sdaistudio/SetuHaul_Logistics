# Facility flow: from a coordinator tool to agents

Live page: [Facility flow](facility.html) · evaluation: [facility-eval.html](facility-eval.html)

## Parties
Shipper (orders the move) → dispatcher / carrier (`carriers`) → driver (`drivers`, WhatsApp, own language) → **SetuHaul coordinator** (control tower, the user) ↔ warehouse coordinator (`facility_contacts`) → customer / consignee (`shipments.customer_name`).

## Stage 1: give the coordinator a scheduling algorithm (`setuhaul/coordinator.py`)
1. **See the day**: `facility_day.as_of(T)` rebuilds the database as it was known at T (no future data), `snapshot()` shows six docks, every slot, bookings, work on the docks, breakdowns and every inbound truck with a deterministic status (will miss / no booking / at risk / in yard / unloading / on time).
2. **Find slots**: exact branch and bound over real slots (`solver.py`), cost from `rules.yaml`, fixed node budget so the same snapshot gives the same plan. Two tiers: *repair* (working bookings locked, only trucks that need a slot move: safe) and *improve* (everything may move; saving shown against keeping every working booking). Dispatch rules (FCFS, EDD, weighted, commitment-first) are scored on the same snapshot.
3. **Decide**: one action card per change (rebook, new, yard → dock, move a working booking, escalate) with reasons, cost, approvals needed and dependencies ("waits for SHP1017 to move").
4. **Book**: approved cards go through the capacity layer at the planned start (hold → confirm, revalidated, idempotent), dependencies first.
5. **Tell everyone**: driver (WhatsApp, own language; held until booked), warehouse coordinator, each dispatcher, customer if more than 60 minutes later; all pass the grounding guard.
6. **Check**: `coordinator.verify()` re-derives the rules from raw tables: one booking per slot, compatible dock, no overlap, nothing on a down dock.

## Stage 2: the same process run by agents (`setuhaul/agents.py`)
IntakeAgent → MonitorAgent → PlannerAgent → VerifierAgent → PolicyGate → ExecutorAgent → EscalationAgent → CommsAgent → VerifierAgent. Each agent has a tool allow-list; a call outside it stops the run and pages a person. The PolicyGate (`rules.yaml → agent_autonomy`) lets agents book only the safe class (rebook / new / yard) and never: moving a working booking, low-confidence ETA, data conflict, start after 21:00, anything that depends on an approval, more than 6 bookings per event. Shadow mode executes nothing. An LLM may only help read an unusual message (schema-validated) or reword a reply (grounding-checked); it never chooses a slot.

## Evaluation and guardrails (`evals/facility_evals.py`, run in CI)
40 seeded disruption days (ETA slips bootstrapped from the real data, random breakdowns, the reefer case) with hard gates, plan quality against dispatch rules, an OR-Tools CP-SAT cross-check, 17 red-team attacks and 12 language-detection cases. See [facility-eval.html](facility-eval.html).
