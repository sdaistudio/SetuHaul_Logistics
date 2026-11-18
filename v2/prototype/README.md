# SetuHaul Driver-Exception Agent: working prototype

FDE Academy, Mini Project 2. A WhatsApp-style driver assistant for dock rescheduling at Jaipur DC,
built on the supplied snapshot of 4 August 2026.

The language layer reads words. Code decides capacity. The database refuses anything unsafe.

## Requirements

* Python 3.11 or later (tested on 3.13.18) with the built-in `sqlite3` (tested on SQLite 3.53.1)
* `matplotlib` only for the Gantt charts in `run_scheduler.py` (tested on 3.11.2)
* No network and no other packages

## Run

```
python3 -I scripts/audit.py data/setuhaul_freight_operations.db out/   # deliverable 1: 36 data findings
python3 -I run_scenarios.py out/                                      # deliverable 4: the replayed morning (~1 s)
python3 -I run_integrity.py out/                                      # deliverable 5: 12 tests + 50 races (~15 s)
python3 -I run_scheduler.py out/                                      # scheduling tool: exact engine + 6 comparison policies (~80 s)
```

Every run works on a fresh copy of `data/setuhaul_freight_operations.db`; the supplied file is never changed.
`-I` (isolated mode) stops Python importing stray files from the working folder.
`RACE_REPEATS=500 python3 -I run_integrity.py out/` gives a longer soak.

## Layout

| Path | What it is |
|---|---|
| `setuhaul/config.py` | Every business number in one place (each one is in the Decision Register) |
| `setuhaul/clock.py` | Explicit as-of time; no hidden "now" |
| `setuhaul/db.py` | Connection, `BEGIN IMMEDIATE` write transaction, schema additions and triggers |
| `setuhaul/nlu.py` | Language detection and rules-first reading of driver messages (English, Hinglish, Hindi) |
| `setuhaul/eta.py` | The ETA we plan on: source order, confidence, late edge, plausibility checks |
| `setuhaul/timeline.py` | Busy intervals per dock and the 15-point feasibility check |
| `setuhaul/capacity.py` | Commit, hold, approve, sweep, cancel and swap, each in one transaction |
| `setuhaul/planner.py` | Facility scheduling tool: exact branch-and-bound engine (`exact`, warm-started by priority + repair), plain-language reasons (`explain`), earmarks, health monitor, what-ifs, swap proposals |
| `setuhaul/escalation.py` | Routing to the right person by a working channel |
| `setuhaul/replies.py` | Reply catalogue in three languages and the output guard |
| `setuhaul/workflow.py` | The conversation state machine: one message in, one reply out |
| `setuhaul/world.py` | Rewind and replay of the snapshot for the scenario run |
| `tests/test_integrity.py` | The 12 integrity tests |
| `run_*.py` | Runners for the scenario, the integrity proof and the scheduler comparison |
| `scripts/audit.py` | The data audit: one proof query per finding |
| `out/` | The results quoted in the Word documents |

## Results in `out/`

| File | Contents |
|---|---|
| `audit_results.json`, `audit_queries.sql` | 36 findings with their rows; every proof query in one file |
| `scenario_run.json`, `scenario_console.txt` | Every turn of the replayed morning: message, tools called, reply, state |
| `scenario_metrics.json` | The brief's measures and four invariants |
| `integrity_results.json`, `integrity_console.txt` | 12 tests, 50 races, negative controls, stress, guard messages |
| `scheduler_results.json`, `gantt_fcfs.png`, `gantt_ours.png` | The exact engine (proof status, nodes searched, reason per truck) and six comparison policies on the 09:45 snapshot, with sensitivity to the weights |
| `scenario_world.db`, `scheduler_snapshot.db` | The databases at the end of each run, for inspection |

## What the prototype does not do

* No live WhatsApp or LLM call. Where the design calls the LLM fallback, the prototype asks the driver a clarifying question instead.
* Coordinator actions are function calls (`capacity.decide`, `capacity.swap`), not a screen.
* SQLite only. The Postgres DDL and the plan to re-run the proof on Postgres are in deliverables 3 and 5.
