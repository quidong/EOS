% SPDX-License-Identifier: Apache-2.0
(fork-optimization-page)=

# Optimization: MILP, rolling horizon, re-planning

## MILP algorithm

`optimization.algorithm: "MILP"` (back: `"GENETIC"`). The MILP reuses the GENETIC machinery for
everything except the search: parameter preparation, the exact device simulation that judges every
plan, the solution and plan formats, the PDF report.

1. **Joint MILP** (HiGHS via SciPy, no extra solver): battery, grid import/export, appliance starts,
   the terminal value of the battery energy and, with `milp.thermal_model` (default on), every heat pump
   as a linear model (building and tank state equations, COP curve interpolated between breakpoints,
   comfort band, hot water floor, legionella deadline, §14a limit, value of stored heat).
2. **Modes from the heat schedule**: the optimal heat schedule is turned into operating modes per hour by
   following it with the exact simulation. These candidates, the status quo, the previous plan and
   PV/price heuristics are scored exactly (dispatch MILP for each heat pump schedule); a coordinate search
   over the modes improves the best start of each family within `milp.hp_search_seconds`.
3. **Polish**: the best candidates lead the initial population of `milp.polish_generations` GENETIC
   generations on the exact simulation (0 = take the MILP plan as it is).

The result is never worse than the best MILP candidate under the exact simulation. Not in the MILP:
electric vehicles (plain GENETIC search then) and battery export for direct marketing (polish only).
Cooling seasons use the mode search without the linear heat pump model.

| Setting (`optimization.milp.*`) | Default | Meaning |
| --- | --- | --- |
| `thermal_model` | true | heat pumps inside the MILP |
| `hp_search_seconds` | 20 | time budget of the mode search |
| `polish_generations` / `polish_individuals` | 30 / 60 | GENETIC polish |
| `time_limit_s` / `mip_rel_gap` | 10 / 1e-4 | limits of one MILP solve |
| `scenarios` | false | expected cost over three scenarios (see below) |
| `shared_slots` | 4 | slots decided jointly for all scenarios |
| `pv_scenario_spread` / `load_scenario_spread` | 0.25 / LoadML or 0.2 | spread of the outer scenarios |
| `max_grid_import_w` / `max_grid_export_w` | none | grid limits; PV above the export limit is curtailed |

**Scenarios**: central (50 %), little PV and high load (25 %), much PV and low load (25 %). Appliance starts
and the first `shared_slots` battery decisions are common, afterwards each scenario adapts (two-stage).
**Negative feed-in tariffs** lead to curtailment instead of export.

`scripts/rl29/compare_milp_genetic.py` compares both algorithms on a synthetic day. On a 96-slot day with
battery, heat pump and washer the MILP reached the GENETIC result (300 individuals × 400 generations) in about
a fifth of the time; a long GENETIC run after the MILP found practically no further improvement.

## Joint mode model (experimental)

`optimization.milp.joint_modes: true` plans the heating modes inside the MILP instead of projecting a free heat
schedule onto modes afterwards. One binary per gene period and mode selects the room target; the controller's
heat for that target is affine in room and screed temperature (feed-forward of the steady demand plus the
screed correction, the comfort minimum as floor, FORCED at full capacity) and is clipped to zero and to the
capacity left by a hot water charge with two binaries per slot. Battery, grid, appliances and hot water are
optimised in the same model. Hard state bounds keep the big-M constants small; the coupling of the heat to the
controller is soft (an expensive deviation), so every mode choice is feasible.

The model is solved by relax-and-fix over windows of `joint_window_hours` (default 6 h) within
`joint_time_limit_s` (default 30 s). Its modes are one more candidate next to the projected heat schedule and
the heuristics; the exact simulation scores all of them. On the synthetic test day it clearly beats the status
quo but not (yet) the projection, also with 120 s; compare it on real inputs with the snapshot replay before
enabling it.

## A/B operation

`optimization.alternate_algorithms: ["GENETIC", "MILP"]` switches the algorithm every local day. The history
endpoints (see {doc}`history`) report the realised grid costs per day and algorithm. Days differ in weather and
consumption, so compare over several weeks, then set `algorithm` to the winner and clear the list.

## Shadow comparison

`optimization.shadow_algorithms: ["GENETIC", "MILP"]` removes the weather noise of the A/B comparison: after
every automatic GENETIC or MILP run, each listed algorithm other than the active one optimizes a copy of the
*same* prepared inputs in the background. The shadow plan is never published or executed. Both plans are scored
with the GENETIC fitness (grid costs minus revenues plus heat pump comfort, legionella and switching penalties
minus the value of the stored heat; MILP polishes with the same evaluator), so the scores are comparable.

- The log shows one `Shadow comparison:` line per run with both objectives and the delta (shadow - active;
  negative means the shadow algorithm would have planned cheaper).
- With the history database the results go to the table `eos_shadow`; `/v1/energy-management/history/summary`
  (MCP `eos_kpis`) reports per algorithm pair the runs, the mean delta and the share of runs the shadow was
  better (`shadow`).
- The next energy management run waits until a running comparison has finished, because both share the
  optimizer state. Keep the combined runtime well below the run interval.
- Runs requested explicitly (REST with own parameters or a fixed algorithm) are not shadowed.

## Input snapshots and replay

`optimization.snapshots.enabled: true` stores the fully prepared inputs of every automatic GENETIC or MILP run
(forecasts, prices, device and heat pump states, warm start), the configuration at that moment (secrets masked)
and the score of the executed plan as gzip JSON below `<data_folder>/rl29/snapshots/<day>/`
(`retention_days`, default 30).

`scripts/rl29/replay_snapshots.py` optimizes the snapshots again with other algorithms or settings and compares
the scores (GENETIC fitness, lower is better) with the executed plans:

```bash
uv run python scripts/rl29/replay_snapshots.py --dir /data/rl29/snapshots --days 14 --every 4 \
    --variant GENETIC \
    --variant "MILP:optimization/milp/time_limit_s=60,optimization/milp/hp_search_seconds=120" \
    --csv replay.csv
```

Replaying the executed algorithm with the stored seed reproduces the recorded score, so every difference comes
from the algorithm or the settings, not from the inputs. The script runs CPU bound; on the EOS host run it at
quiet times or copy the snapshot folder to another machine.

## Rolling horizon

EOS re-plans every `ems.interval` seconds with fresh measurements. The fork adds:

- `ems.run_on_slot_start: true`: runs start `ems.slot_start_offset_sec` (20 s) after each slot start, so the first
  slot of every plan is (almost) complete; `ems.interval` becomes the check interval (60 s recommended).
- At every slot start the values of the *existing* plan for the new slot are pushed to the adapters at once; the
  new run follows after its computation.
- When no fresh battery SoC is measured, the SoC the last plan expected for this moment is used (plans up to
  3 h old) instead of 0 %.
- `ems.replan_on_deviation: true`: between slot starts, re-plan at once when the battery SoC deviates by more
  than `replan_soc_deviation_pct` (10 points), the room by more than `replan_temperature_deviation_k` (0.8 K) or
  the hot water by four times that, at most every `replan_min_interval_sec` (300 s).
- `ems.replan_pv_deviation_w` (optional, e.g. 1500 W): re-plan as well when the mean PV power of the last
  complete slot (`measurement.pv_production_emr_keys`, interpolated meter readings) differs from the forecast
  the plan used by more than this.

## 15-minute slots

`optimization.genetic.interval_sec: 900` plans battery and appliances per quarter hour (day-ahead prices are
quarter-hourly); heat pump modes stay hourly (four slots per gene). With the MILP a run takes well under a minute;
GENETIC runs grow with the number of slots, reduce `genetic.generations` if needed.

## Planning horizon

`optimization.genetic.horizon_hours` (control) plus `tail_horizon_hours` (lookahead without commands). The
battery already sees the tail through its terminal value; the heat pump values stored heat at the horizon end with
a price quantile. A longer control horizon mainly helps heat storage across days; beyond the published day-ahead
prices it relies on the price forecast (see {doc}`forecasts`).

## Grid connection limits

`optimization.grid` applies to GENETIC and MILP (`milp.max_grid_*_w` override it for MILP):

- `max_export_w`: feed-in cap, e.g. 60 % of the PV peak power for plants without a smart meter
  (Germany, Solarspitzengesetz). PV above it is curtailed and earns nothing; the optimizer therefore keeps
  room in the battery for the midday peak instead of filling it in the morning, and moves loads there.
- `max_import_w` and `import_excess_penalty_amt_kwh` (1 €/kWh): import cap for the main fuse, contracted
  power or peak shaving. GENETIC penalises energy above it; MILP treats it as a hard limit (and drops it when
  the fixed load alone exceeds it).

## Batteries and electric vehicles

- **§14a dimming:** batteries and EVs listed in `elecfee.grid_control_14a.controllable_device_ids` read
  `<id>-power-limited` (1 = dimmed). From now on, for `dimming_hold_hours`, the EV may draw at most
  `dimming_power_w` and the battery may grid-charge at most that power (PV charging is not limited). GENETIC
  only.
- **Weather reserve:** `devices.batteries.<id>.weather_reserve` (`enabled`, `min_soc_percentage` 50,
  `max_hours` 48) with `<id>-warning-start-ts` / `<id>-warning-end-ts` (Unix times, 0 = none): the battery
  is kept at or above the reserve during the warning. The largest shortfall costs
  `genetic.penalties.battery_reserve_miss` (default 10) per percentage point. GENETIC only.
- **No grid charging:** `devices.inverters.<id>.max_ac_charge_power_w: 0` disables charging from the
  grid. The simulation ignores the AC charge genes then, and the battery modes of the plan say the same
  (no `GRID_SUPPORT_IMPORT` / `FORCED_CHARGE`; such slots become `NON_EXPORT` or `IDLE`), so a
  controller that executes the plan never charges from the grid.
- **Plausibility check:** `scripts/rl29/check_battery_plan.py --url http://<eos>:8503 --device <id>`
  checks the battery part of the current plan (exit 0 = plausible, 1 = warning, 2 = implausible): one
  operation mode per slot, state of charge within the limits, grid charging only when the price after
  round-trip losses and storage cost is below a later price, no battery export, no blocked PV charging
  while feeding in, and the first planned state of charge against the measured `<id>-soc-factor`.

