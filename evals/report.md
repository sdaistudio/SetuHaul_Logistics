# SetuHaul evaluation report

| Gate | Value | Threshold | Result |
|---|---:|---:|---|
| nlu_case_accuracy | 1.000 | 0.90 | PASS |
| nlu_holdout_accuracy | 0.800 | 0.70 | PASS |
| nlu_min_language_accuracy | 1.000 | 0.85 | PASS |
| feasibility | 1.000 | 1.00 | PASS |
| guardrails | 1.000 | 1.00 | PASS |
| integrity_tests | 1.000 | 1.00 | PASS |
| solver_optimality | 1.000 | 1.00 | PASS |

## Language understanding (52 cases)

Case accuracy 100.0%; language detection 100.0%.

| Language | Accuracy |
|---|---:|
| en | 100.0% |
| hi | 100.0% |
| hinglish | 100.0% |

Field accuracy: intent 100%, eta 100%, earliest_start 100%, eta_confidence 100%, delay_reason 100%, latest_gate_out 100%, delay_min 100%, flags 100%, option_choice 100%, intent_any 100%, eta_window 100%, clarify 100%, shipment_refs 100%

### Held-out set (20 cases, never used for tuning): 80.0%

This is the honest estimate. The tuned set above is a regression test, not a measure of accuracy.

| Language | Accuracy |
|---|---:|
| en | 75.0% |
| hi | 80.0% |
| hinglish | 85.7% |

Held-out failures (left unfixed on purpose):
- `ho04` 'can I get something after lunch, like 2 or later': earliest_start: expected '14:00' got None
- `ho07` 'please connect me to someone': intent: expected 'HUMAN' got []
- `ho12` 'ढाई बजे के बाद का स्लॉट चाहिए': earliest_start: expected '14:30' got None
- `ho14` 'bhai 1 ghanta lagega abhi': eta: expected '10:30' got '11:00'

## Feasibility (11 cases, 100%)

| Case | Result | First options |
|---|---|---|
| F01-reefer-no-slot | PASS | none |
| F02-heavy-only-D6 | PASS | D6 12:40-14:10, D6 14:00-15:30, D6 15:30-17:00 |
| F03-21t-not-on-20t-docks | PASS | D4 12:30-13:45, D2 12:45-14:00, D2 13:00-14:15 |
| F04-driver-after-12 | PASS | D4 12:30-13:15, D2 12:45-13:30, D2 13:00-13:45 |
| F05-gate-out-13-30 | PASS | D4 12:30-13:15 |
| F06-overweight-confirmed-D3 | PASS | D4 09:35-10:50, D4 10:00-11:15, D4 12:30-13:45 |
| F07-cancelled | PASS | none |
| F08-reefer-only-D5 | PASS | D5 11:20-12:15, D5 12:00-12:55, D5 13:00-13:55 |
| F09-early-truck-not-before-dock-free | PASS | D4 08:30-09:15, D1 08:42-09:27, D1 09:00-09:45 |
| F10-breakdown-window | PASS | D4 12:30-13:15, D2 12:45-13:30, D2 13:00-13:45 |
| F11-ask-only-after-1pm | PASS | D1 13:15-14:05, D1 14:00-14:50, D1 15:00-15:50 |

## Guardrails (15 cases, 100%)

## Integrity and safety tests: 21/21 passed

## Solver: branch and bound, cross-checked with OR-Tools CP-SAT

| At | Mode | Trucks | B&B | B&B cost | Nodes | B&B ms | CP-SAT | CP-SAT cost | CP-SAT ms | Match |
|---|---|---:|---|---:|---:|---:|---|---:|---:|---|
| 08:30 | chat | 5 | PROVEN_OPTIMAL | 54575 | 39 | 1 | OPTIMAL | 54575 | 18.4 | True |
| 08:30 | plan | 16 | PROVEN_OPTIMAL | 55050 | 4795 | 83 | OPTIMAL | 55050 | 230.7 | True |
| 09:30 | chat | 5 | PROVEN_OPTIMAL | 55175 | 39 | 1 | OPTIMAL | 55175 | 17.3 | True |
| 09:30 | plan | 16 | PROVEN_OPTIMAL | 56025 | 4725 | 97 | OPTIMAL | 56025 | 359.9 | True |
| 09:45 | chat | 5 | PROVEN_OPTIMAL | 56375 | 53 | 1 | OPTIMAL | 56375 | 13.5 | True |
| 09:45 | plan | 16 | PROVEN_OPTIMAL | 59125 | 12091 | 222 | OPTIMAL | 59125 | 731.0 | True |
| 10:30 | chat | 5 | PROVEN_OPTIMAL | 77375 | 76 | 1 | OPTIMAL | 77375 | 15.8 | True |
| 10:30 | plan | 16 | PROVEN_OPTIMAL | 72350 | 56554 | 1214 | OPTIMAL | 72350 | 2819.2 | True |

## Scenario-run measures (brief, page 9)

| Measure | Value |
|---|---|
| cases (conversations) | 12 |
| time from first message to usable outcome (median min) | 2 |
| turns to outcome (median driver messages) | 2 |
| share resolved without manual takeover | 10/12 |
| conflicting or duplicate allocations | 0 |
| options later found infeasible at commit | 1 of 24 shown |
| no-slot escalations with complete hand-off record | 1/1 |
| avg driver wait after rescheduling (min, arrival->dock) | 52.6 |
| warehouse slot utilisation 10:00-16:00 (Jaipur) | 10/31 open slots booked |
| promises displaced by another driver's booking | 0 |
| driver clarification questions | 2 across 12 cases |
| duplicate messages suppressed | 1 |
