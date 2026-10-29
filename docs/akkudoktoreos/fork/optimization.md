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

## A/B operation

`optimization.alternate_algorithms: ["GENETIC", "MILP"]` switches the algorithm every local day. The history
endpoints (see {doc}`history`) report the realised grid costs per day and algorithm. Days differ in weather and
consumption, so compare over several weeks, then set `algorithm` to the winner and clear the list.

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

