# SetuHaul v2: hand-ins after the 18 November 2026 review

This folder holds the reconciled design. A colleague reviewed our GitHub design
("Doc A") against a second design ("Doc B"), built a lean prototype, and wrote
seven documents. Those are now the hand-ins of record, with one change we made:
the scheduling engine is an exact, proven-optimal search (see below).

The previous version is kept as a backup: commit `492a08d`, the static snapshot in
[`../archive/v1-docA/`](../archive/v1-docA/) and `/v1/` on the website.

## What is here

| Path | What it is |
|---|---|
| `deliverables/00_SetuHaul_Review_Pack.docx` | Review of Doc A and Doc B, conflicts and decisions, cost comparison, questions for the client (v1.1) |
| `deliverables/01_…Problem_Definition_and_Data_Audit.docx` | Facts, assumptions, unknowns; data audit with the SQL behind each finding |
| `deliverables/02_SetuHaul_Decision_Register.docx` | D1–D18 decisions with reasons and evidence (engine updated) |
| `deliverables/03_SetuHaul_Working_Prototype.docx` | How the prototype works and how to run it (engine updated) |
| `deliverables/04_SetuHaul_Scenario_Run.docx` | The 13-case Jaipur replay, turn by turn, and the scheduling tool results (v1.1) |
| `deliverables/05_SetuHaul_Integrity_Proof.docx` | Race test (50 rounds × 10 drivers), overlap trigger, holds, idempotency |
| `deliverables/06_SetuHaul_Client_Readout.docx` | One-page readout and the 7 policy questions for the client |
| `deliverables/original_from_review/` | The four documents we edited, exactly as received |
| `prototype/` | The working prototype: plain Python + SQLite, 12 integrity tests |
| `html/` | The documents rendered for the website (`python tools/render_v2.py`) |
| `CHANGELOG.md` | Every review item and what changed, in v2 and in our add-ons |

## Run it

```bash
cd v2/prototype
python3 -I -m unittest discover -s tests -v        # 12 integrity tests
python3 -I run_integrity.py out/                   # race test, 50 rounds x 10 drivers
python3 -I run_scenarios.py out/                   # 13-case replay from 08:20
python3 -I run_scheduler.py out/                   # exact engine + 6 comparison policies (~80 s)
python3 -I scripts/audit.py                        # data audit queries
```

No packages beyond the Python standard library (matplotlib only for the Gantt PNGs).

## The engine decision

The review recommended a priority list plus repair as the planner, benchmarked
against exhaustive search. We kept both, in two roles:

- **Engine of record: exact branch and bound** over the order in which trucks get
  their earliest feasible door (`planner.exact`). It is warm-started by the
  priority + repair answer. A branch is cut when the trucks already placed, plus
  each remaining truck's best placement on the door time still free, cannot beat
  the best plan found. It stops at a node count (`EXACT_NODE_LIMIT = 200,000`), not
  a clock, so every run gives the same answer.
- **First answer: priority list + repair.** The coordinator sees it at once, and
  `planner.explain` gives each truck a one-line reason ("ready 11:40; fits D2, D4;
  gets D2 12:20; waits 40 min because SHP1005, SHP1009 use those docks first").

On the 09:45 snapshot (8 trucks): **proven optimal after 5,425 nodes, out of
40,320 possible orders.** The exhaustive benchmark and priority + repair give the
same plan: 1 truck with no door (the reefer, whose only door is in maintenance),
priority-weighted wait 1,120, CRITICAL wait 40 min, longest HIGH/CRITICAL wait
65 min. The plan stays the same under all three weight sets tested.

One visible effect in the scenario replay: at 09:36, DRV013 (LOW) picks D4 12:40
in a race with DRV006. The exact plan had earmarked that slot for HIGH SHP1006, so
the commit now refuses DRV013 with "reserved for a higher-priority load" (before:
"just taken by another truck") and offers D1 at 14:00. Every other turn and every
scenario measure is unchanged.
