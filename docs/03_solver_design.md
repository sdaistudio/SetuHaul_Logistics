# The deterministic solver: the heart of SetuHaul

**Principle.** No model chooses a slot. Ever. A driver's words are turned into numbers (ETA, deadline, "after 12"); from that point everything is a search over a decision tree with a score, configured in `rules/rules.yaml`, repeatable to the last minute.

## 1. The problem, stated as a solver sees it

| Business word | Solver object | Source |
|---|---|---|
| Truck needing a dock | job | shipments with no reachable booking (chat mode) or all open trucks (plan mode) |
| Dock door | machine | `docks` |
| Appointment slot | start window on a machine; one job per window | `appointment_slots` |
| Latest ETA + gate-to-dock | release time | `eta.py` + `timing.gate_to_dock_min` |
| Unload minutes | processing time | `shipments.expected_unload_min` |
| Truck already unloading, dock breakdown, maintenance | fixed blocks on a machine: a closed toll lane | `facility_checkins`, `dock_status_events` |
| Reefer / heavy / weight | which machines a job may use | `compatibility.rules` |
| Driver "must leave by 13:30" | hard deadline | chat → `driver_exceptions` |
| Promise already made | soft: costs to move | `costs.move_kept_promise` |
| Priority | weight on every minute of waiting | `priority_weight` |

**Hard constraints (never traded):** compatible dock, weight limit, slot open, one booking per slot, start inside the window, no overlap with fixed blocks or other jobs, finish before close, driver deadline. A closed dock is closed: no amount of queue opens it.

**Objective (lower is better), all weights in YAML:**

```
cost = Σ trucks  weight × wait_per_min × (dock start − arrival)
              + weight × late_per_min × max(0, start − promise − grace)
              + move_kept_promise       if a booking that still worked is moved
              + needs_approval          if start after LAST_NEW_START_TIME
              + weight × unscheduled    if no dock today (→ a person decides)
```

## 2. The algorithm: branch and bound (a chess engine for docks)

1. **Tree.** Level *i* = truck *i*. Branches = its feasible slots, cheapest first, plus "no dock today". Most constrained trucks first (fewest options), then higher priority.
2. **Leaf score.** Assignments are swept dock by dock in time order; each truck starts at the earliest moment its window, its arrival, the fixed blocks and the trucks before it allow. Costs are added.
3. **Bound.** Cost so far + for every unplaced truck the cheapest it could ever do alone. Other trucks can only delay it, so the bound never over-estimates (admissible, the same property that makes A* optimal).
4. **Prune.** If the bound is not better than the best complete plan found, cut the branch. The greedy plan is the first incumbent, so cutting starts immediately.
5. **Anytime.** Stops at `solver.plan_time_budget_ms` / `node_limit`. Fully explored → `PROVEN_OPTIMAL`. Budget hit → `BEST_WITHIN_BUDGET` with a proven lower bound, so the gap is known.
6. **Deterministic.** No randomness, every tie broken by (cost, time, dock, id). Same state + same YAML = same plan.

**Look-ahead for one driver (chat).** For each slot the driver could take, pin it, solve the rest of the facility optimally, and score the whole board. Rank the driver's options by total facility cost. This is the chess move: "if I play this, what is the best the rest of the board can do?"

**Proof it is optimal.** `tests/test_solver.py` compares B&B with exhaustive enumeration on a trimmed instance, and with Google OR-Tools CP-SAT (single worker, fixed seed) on the full facility. Eval gate `solver_optimality`: 8/8 instances identical cost.

| Instance (Jaipur) | Trucks | B&B | Nodes | ms | CP-SAT ms |
|---|---:|---|---:|---:|---:|
| 09:45 chat (who still needs a dock) | 4 | proven optimal | 25 | 1 | 9 |
| 09:45 full re-plan | 16 | proven optimal | 12,091 | 226 | 495 |
| 10:30 full re-plan | 16 | proven optimal | 56,554 | 1,144 | 2,773 |
| 12:00 full re-plan (hard) | 16 | best within 5 s (2.4% above optimum); proven optimal at 10 s | 525,298 | 10,259 | 35,707 |

## 3. What it changed in the replay

Earliest-first offered DRV006 the D4 12:30 slot, the only slot DRV012 (tyre repaired, must leave by 13:30) could use. DRV006 took it and DRV012 had to be escalated. With look-ahead, DRV006 is offered D2 first because taking D4 would strand DRV012; DRV012 gets D4 12:30 and the only remaining escalation is the reefer that truly has no dock. The CRITICAL load is offered D2 13:15 rather than D4 12:30 for the same reason: 45 more minutes for one truck beats leaving another truck with no dock (cost 4,500 vs 15,000 under the YAML weights; a coordinator can still approve a swap).

## 4. Engine ladder (when to change engine)

| Size | Engine | Why |
|---|---|---|
| ≤ ~20 trucks per facility window (SetuHaul today) | Branch and bound (ours) | explainable line by line, proven optimal in ms–s |
| Larger, or more constraint types (labour, yard lanes) | OR-Tools CP-SAT | industrial solver, same model already written (`cpsat_check.py`) |
| JVM shops, continuous re-planning | OptaPlanner / Timefold | constraint streams, incremental score, real-time planning |

The model and the YAML stay the same; only `solver.algorithm` changes.

## 5. Location on demand (no polling)

Polling every truck's GPS every minute is the classic way to burn a maps/telematics budget; teams that fixed it computed locally and called paid APIs only on triggers. SetuHaul does the same, deterministically:

- **Value of information.** For each truck with an uncertain ETA, solve the facility at its early and late ETA edge. Same plan → a position is worth nothing, do not buy it. Different plan, big enough cost difference → buy one fix, within the daily budget. At 09:50 on 4 Aug: 6 uncertain trucks, **1 fix bought** (₹1.5).
- **Source ladder:** GPS / telematics first; FASTag last-toll (ULIP) second, which still works in GPS dead zones and when a device is switched off; driver-shared WhatsApp live location last (free, needs the driver).
- **Toll-checkpoint watchdog.** Trucks on tolled corridors cross FASTag plazas at predictable times. Next crossing overdue beyond tolerance → ETA downgraded to LOW. Overdue and GPS silent → ops review (breakdown, dead zone, or a toll bypass that will add 15–20 minutes of city traffic). The system never accuses a driver; a person looks.
- **Free routing.** A position becomes an ETA through self-hosted OSRM on OpenStreetMap data: no per-call map fees.

Corridor data in `data/corridors_sim.yaml` is simulated for the demo (plaza names real, timings illustrative). Production: NHAI plaza master + segment times learned from historical FASTag crossings.

Common cases first: most trucks never trigger either rule and cost nothing.
