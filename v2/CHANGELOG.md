# Change log: the 18 November 2026 review

Source: the colleague's review pack (`deliverables/00_SetuHaul_Review_Pack.docx`),
the six hand-ins and the prototype. "v2" means this folder; "add-ons" means the
rest of the repository (Demo 2, facility flow, solver, evals, website), which we
keep as the scale path and updated to agree with the review.

## Decisions taken on the review

| Decision | Outcome |
|---|---|
| Which version is the hand-in? | The colleague's prototype and seven documents (`v2/`). Our earlier site is archived at `/v1/` and in commit `492a08d`. |
| Which scheduling engine? | Exact branch and bound (proven optimal), with priority list + repair as the first, explainable answer and a reason per truck. Both are benchmarked against exhaustive search. |

## The 12 reconciled changes

| # | Change | In v2 | In our add-ons |
|---|---|---|---|
| 1 | Pick = book in one checked transaction; no "Reply YES" | Built in | Demo 2: sending a slot books it and the WhatsApp text confirms it; "Reply YES" removed (EN and Hindi) |
| 2 | Long unloads are intervals that may run past the slot end; never two slots | Built in | Already so (start-window model) |
| 3 | Database trigger forbids overlapping time on a dock | Built in (SQLite stand-in for Postgres EXCLUDE) | Postgres schema in `infra/` already has EXCLUDE |
| 4 | Earmarks for HIGH/CRITICAL and deadline-limited trucks | Built in; now driven by the exact plan | Unchanged |
| 5 | Coordinator-approved atomic swaps | Built in | Facility flow already uses coordinator approval |
| 6 | Booking health monitor | Built in | Unchanged |
| 7 | ETA plausibility checks | Built in | Unchanged |
| 8 | Time-travel replay from 08:20 | Built in | Unchanged |
| 9 | Rules first; Claude Haiku only when rules fail | Built in | Already so (fallback off by default) |
| 10 | Lean pilot: one container + managed Postgres, about US$22–30/month | Documented | Solution design now says pilot first; AWS design is the scale path |
| 11 | Pending deadline = min(15 min, slot start − 30 min) | Built in | `setuhaul/capacity.py` `pending_deadline()`, with a 5-min floor; `rules.yaml` updated; driver message shows the real minutes |
| 12 | Weights 8:4:2:1 with a sensitivity test | Built in | Noted; plan identical under all three weight sets |

## Review of Doc A (our earlier design)

| Item | Finding | What we did |
|---|---|---|
| A1 | ~12 AWS services, about US$170/month floor | Pilot-first note in the solution design; AWS kept as the scale path |
| A2 | Race proof only on SQLite with one writer | v2 races 10 threads, each with its own connection, 50 times, with a negative control that shows the test can fail, and an overlap trigger that binds every writer. Still SQLite: running the same test on managed Postgres is the first pilot task |
| A3 | Audit A03 counted 7 long unloads; 4 are real | Fixed: minutes now rounded, not truncated (`audit/data_audit.sql`); findings and problem definition updated |
| A4 | Audit missed behaviour-changing issues | Covered by the v2 audit (deliverable 01) |
| A5 | Branch and bound hard to explain | Kept an exact engine (the brief asks for an optimal deterministic solver), but its score is now three plain measures and each truck gets a written reason; priority + repair is shown first. Decision register D8, review pack A5 and §6, prototype §6 and scenario run updated |
| A6 | Weights never validated | 8:4:2:1 with sensitivity test; client question kept open |
| A7 | Fixed 20-min pending TTL | Replaced (change 11) |
| A8 | Start window vs no-show rule | Settled in v2 decision register |
| A9 | Priority protected only by ranking and swaps | Earmarks (change 4) |
| A10 | Simulated location data | Still simulated; labelled as such |
| A11 | 80% on 20 messages has a wide range | Evaluation report now shows n and a 95% Wilson interval for every rate (80% on 20 is about 58–92%) |
| A12 | 18 web pages vs 6 hand-ins | Home page now leads with the v2 hand-ins; other pages are listed as add-ons |

Doc B items (B1–B10) are addressed inside v2 by the reconciled decisions above
(no LLM on every message, pick = book, no two-slot unloads, a reproducible worked
example, no impossible reefer fallback, plain state machine).

## Edits to the colleague's documents

| Document | Edit |
|---|---|
| 00 Review pack (v1.1) | Results row, A5 recommendation, §6 Planner row and question 37 now describe the exact engine and its proof (5,425 of 40,320 orders) |
| 02 Decision register | Policy table gains the engine row; D8 method and rejected alternatives rewritten |
| 03 Working prototype | Run line (7 policies) and §6 planner description and FDE note |
| 04 Scenario run (v1.1) | Policy table gains the EXACT row; race turn at 09:36 now reads "reserved for a higher-priority load" with option D1 14:00; priority-protection check now records that refusal |
| 01, 05, 06 | Unchanged |

## Prototype code changes (`v2/prototype`)

- `planner.py`: `schedule()` split into `_context` / `_best` / `_commit` (same behaviour); added `exact()`, `explain()`, policy `EXACT`; `plan_facility()` uses `ENGINE = "EXACT"` by default.
- `config.py`: `EXACT_NODE_LIMIT = 200_000`.
- `run_scheduler.py`: prints the engine's proof status and reasons; adds `engine` to `scheduler_results.json`.
- `out/`: scheduler and scenario outputs regenerated. Integrity outputs kept as received (the race counts are random per run and do not use the planner).
- 12/12 tests pass.
