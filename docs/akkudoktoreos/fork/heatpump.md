% SPDX-License-Identifier: Apache-2.0
(fork-heatpump-page)=

# Heat pump, hot water and heating rod

EOS plans air/water heat pumps with underfloor heating together with battery, PV, grid and
appliances. Each heat pump is configured under `devices.heat_pumps.<device_id>` (with
`devices.max_heat_pumps`). All values below are defaults unless marked as example.

## Model

Per slot the model simulates

- the **building** as two thermal nodes: screed (incl. heating water and buffer) and room
  (air, furniture, inner walls), with heat loss to the outside, solar gains (global irradiance from the
  weather forecast) and internal gains (`building.*`);
- the **hot water tank** as a mixed volume with standby loss and a tapping profile (`dhw.*`);
- the **heat pump**: thermal and electric limits, flow temperature from heat and screed temperature,
  COP from outdoor and flow temperature (Carnot efficiency fitted to the rating points `unit.cop_points`,
  scaled by `unit.cop_scale`, capped by `unit.cop_max`), hot water charging at its own COP;
- an optional **heating rod** (`dhw.rod_power_w`, 0 = none) that charges the tank to a target;
  with its own PV surplus controller (`dhw.rod_surplus_c`, see below) it also heats on its own;
- the **controller logic**: each mode sets a room target, the heat pump delivers the heat a heating
  curve with room thermostats would deliver; the room never falls below the comfort minimum (the
  controller would heat), the tank is reheated below setpoint − hysteresis;
- **idle power**: `unit.standby_w` while ready but not running (controller, outdoor unit, pump) and
  `unit.off_w` in the heat pump's own standby operating mode (see holiday mode).

The default rating points are those of a Weishaupt WBB 12 (EN 14511). Replace them with the data
sheet of your unit:

```json
{"unit": {"cop_points": [
  {"outdoor_c": 7, "flow_c": 35, "cop": 4.85},
  {"outdoor_c": 2, "flow_c": 35, "cop": 4.11},
  {"outdoor_c": -7, "flow_c": 35, "cop": 3.28},
  {"outdoor_c": 7, "flow_c": 55, "cop": 3.10},
  {"outdoor_c": -7, "flow_c": 55, "cop": 2.47}]}}
```

## Modes

| Space heating (`optimization.heating_modes`) | Meaning |
| --- | --- |
| 0 REDUCED | room may cool down to the comfort minimum |
| 1 NORMAL | heating curve as without EOS (setpoint) |
| 2 RAISED | setpoint + `comfort.raised_offset_k` (SG Ready "increased operation") |
| 3 PREHEAT | room target = comfort maximum (charge the screed) |
| 4 FORCED | full power up to comfort maximum + `unit.forced_overshoot_k` (SG Ready "forced on") |
| 5 COOL | cooling season only |

| Hot water (`optimization.dhw_modes`) | Meaning |
| --- | --- |
| 0 HOLD | heat pump reheats on its own below setpoint − hysteresis |
| 1 HEAT_PUMP | charge to the heat pump setpoint now |
| 2 ROD | heating rod to a target (`dhw.rod_targets_c`, one option per target, e.g. `ROD_60`) |

### SG Ready 3/4 as heating curve shift

Many heat pumps implement SG Ready 3/4 as a shift of the heating curve, not as a new room
target. With fixed room setpoints (`comfort.fixed_room_setpoints`) the room controllers stop at
their setpoint, so the target model gives RAISED no effect at all. The unit settings model what
the heat pump really does:

| Setting | Effect |
| --- | --- |
| `unit.raised_flow_offset_k` | RAISED/PREHEAT: flow this much above the normal curve; open valves take `emitter_w_per_k * offset` more heat while the room is below the controllers' target |
| `unit.forced_flow_offset_k` | FORCED: valves forced open, the screed takes `emitter_w_per_k * (curve + offset - screed)` (capacity and `max_flow_c` apply) until comfort maximum + `forced_overshoot_k` |
| `unit.preheat_valves_forced` | PREHEAT forces the valves too (e.g. KNX forced positions) and heats like FORCED with the raised offset |

The normal curve is the weather-compensated mean water temperature that holds the setpoint in
steady state. Read the offsets off the flow setpoint of the heat pump right before and after a
switch. `None` keeps the old behaviour (FORCED at full capacity). The MILP linearises the curve
heat at the steady screed of the setpoint; the GENETIC polish uses the exact law.

RAISED/FORCED can raise the hot water setpoint (`dhw.raised_setpoint_c`, `dhw.forced_setpoint_c`).
Legionella runs (`dhw.legionella_c`, `dhw.legionella_interval_days`) are planned with the cheapest
option that reaches the temperature; without one, no run is planned and the log says so.

The objective contains, besides the energy costs: comfort shortfall (`optimization.comfort_penalty_amt_per_kh`),
overheating, hot water below its minimum, a missed legionella run, mode changes (also against the mode the
previous plan set for now, see plan stability) and active modes without effect, and the **value of the heat stored at the horizon end** (it replaces heat produced later in the
cheapest hours: mean of the cheapest `stored_heat_price_quantile` of the marginal prices, divided by the COP).

The stored heat is used after the horizon, so with at least 6 h of forecast beyond it the value
takes the outdoor temperature (COP) and prices of the next 24 h: a cold next day makes heat kept
today worth more, a mild one less. PV surplus counts as free heat only after the house battery
is full (its usable capacity takes the first surplus of every day). The battery outlook beyond
the horizon (terminal value curve, tail) includes the heat pump: the status quo consumption in
the horizon and the steady demand at the setpoint plus the typical hot water draw after it.

## Interface to the controller

Solution keys (Home Assistant: `sensor.eos_<key>`), per slot:

| Key | Meaning |
| --- | --- |
| `<id>_heating_mode` | 0…5 as above |
| `<id>_dhw_mode` | 0 HOLD, 1 HEAT_PUMP, 2 ROD |
| `<id>_rod_target_c` | rod target, 0 = off |
| `<id>_energy_wh`, `<id>_rod_energy_wh` | planned electric energy (heat pump + rod, rod alone) |
| `<id>_room_temperature_c`, `<id>_slab_temperature_c`, `<id>_dhw_temperature_c` | expected temperatures |
| `<id>_away`, `<id>_zone_setback`, `<id>_ventilate` | holiday slot, set-back zone, airing recommendation |
| `<id>_standby` | 1 = heat pump in its standby operating mode (holiday, see below) |
| `<id>_kpi_*` | plan vs actual and calibration figures (see below) |

Additionally FRBC instructions per actuator: `<id>_space_heating` (mode) and `<id>_dhw` (`HOLD`,
`HEAT_PUMP`, `ROD_<temperature>`).

Measurements EOS reads (via `adapter.homeassistant.device_measurement_entity_ids`):

| Key | Meaning |
| --- | --- |
| `<id>-room-temperature`, `<id>-dhw-temperature` | initial state (required for good plans) |
| `<id>-slab-temperature` | optional; otherwise `slab_estimate` (below) or the steady state |
| `<id>-return-temperature` | optional: return of the heating circuit, corrects the carried screed (`slab_estimate`) |
| `<id>-dhw-setpoint` | hot water setpoint read from the unit |
| `<id>-power-limited` | 1 = §14a dimming active now |
| `<id>-cooling-season`, `<id>-dewpoint` | cooling operation and room dew point |
| `<id>-away`, `<id>-return-ts`, `<id>-away-room-temperature`, `<id>-away-min-temperature` | holiday mode |
| `<id>-setback-room-temperature`, `<id>-window-open` | zone set-back temperature, share of open windows |
| `<id>-room-min-margin`, `<id>-room-max-margin` | optional: closest room to its own lower / upper limit [K] |
| `<id>-humidity` | optional: highest relative humidity of the monitored rooms [%] |
| `<id>-reduce-lock-until-ts` | optional: Unix time until which the controller does not reduce, 0 = no lock |
| `<id>-warning-start-ts`, `<id>-warning-end-ts` | optional: weather warning period (Unix times, 0 = none) |
| `<id>-outdoor-temperature`, `<id>-heat-power`, `<id>-electric-power`, `<id>-flow-temperature` | calibration |

Screed estimate (`slab_estimate.enabled`): without a screed sensor each run starts from the
screed the previous plan predicted for now (house and every zone, interpolated within the slot,
plan not older than `max_age_hours`). While the heat pump heats the floor (space heat =
`<id>-heat-power` − `<id>-dhw-heat-power` ≥ `min_heat_w`, no hot water charge) the return
gives the screed: `(flow + return) / 2 − space heat / emitter_w_per_k` (without flow
`return + flow_spread_k / 2`); `return_gain` of the difference corrects the estimate, and the
same shift applies to every zone. The log line "screed estimate" shows both values.

Use a power meter that sees all consumers of the heat pump (pumps, controller, cooling) for
`<id>-electric-power`; internal counters of some units miss parts of it.

### Example: Home Assistant as controller

One working mapping (SG Ready contacts plus room thermostats):

| EOS mode | SG Ready | Room thermostats |
| --- | --- | --- |
| REDUCED | 1 (reduced operation of the heating circuit) | normal setpoint |
| NORMAL | 2 | normal setpoint |
| RAISED | 3 | setpoint + 1 K |
| PREHEAT | 3, all heating circuits forced open | comfort maximum |
| FORCED | 4 | comfort maximum + 1 K |
| HEAT_PUMP | hot water push contact for 60 min | – |

Recommended rules in the controller, independent of EOS:

- plan missing or older than 45 min → NORMAL;
- report the per-room margins, humidity and a manual reduction lock to EOS (see room limits below)
  and execute a fresh plan as it is: EOS plans with these limits. A controller that overrides the
  plan (e.g. no reduction while a room is cold, with its own hysteresis) disagrees with the plan and
  makes the executed mode jump; report such rooms instead;
- maximum duration of a reduction, minimum holding time per mode, a daily limit of switchings and
  hot water charges;
- manual changes at the unit pause the automation and notify;
- an emergency switch-off when rooms get too warm or too cold;
- time logic in the actuator (e.g. staircase timers) so that contacts open even if the controller fails.

Check the plan during commissioning: `scripts/rl29/check_heatpump_plan.py --url http://<eos>:8503`
(exit 0 = plausible, 1 = warning, 2 = implausible). `scripts/rl29/compare_heatpump_plan.py` compares
plans with measured values from Home Assistant.

## Calibration

`calibration.enabled: true` fits the model to the measurements of a rolling window (`calibration.days`,
refit every `refit_hours`). The configured values stay the prior; results are shrunk towards them and
bounded. The configuration file is never changed; EOS plans with the fitted values.

| Fit | Data | Method |
| --- | --- | --- |
| `building.*` loss, aperture, gains | daily means: room, outdoor, space heat, irradiance | ridge regression (\*) |
| `unit.cop_scale` | hours with continuous operation | energy-weighted measured/rated COP, idle power removed |
| `building.emitter_w_per_k` | hours with space heating, no hot water | from measured flow temperature and heat |
| `dhw.loss_w_per_k` | night hours without charge or tapping | exponential decay |
| `dhw.draw_profile_wh` | raw tank temperatures, ≥ 14 days | tapping per hour of day (working day / weekend) |
| `ventilation.profile` | `<id>-window-open` | airing per hour of day |

(\*) `loss_w_per_k`, `solar_aperture_m2`, `internal_gains_w` from `Q = H·ΔT − A·G − g`;
days with drifting room temperature, outages and holiday days are left out.

Missing outdoor temperature or irradiance is filled from the weather forecast
(`weather_from_prediction`). Results: `<data>/rl29/heatpump_calibration.json` and the log.
`scripts/rl29/calibrate_heatpump.py` does a first calibration from Home Assistant history before EOS has
collected its own.

### Plan vs actual

With calibration enabled, EOS records the first 24 h of every heat pump plan and compares the plans of
the last day with the measurements for two lead times (1 h: does the controller follow?, 6 h: does the
model predict?). Figures are published as solution columns:

| Column | Content |
| --- | --- |
| `kpi_room_bias_1h_k`, `kpi_room_mae_1h_k`, `kpi_room_bias_6h_k`, `kpi_room_mae_6h_k` | room plan − actual [K] |
| `kpi_dhw_bias_6h_k` | tank plan − actual [K] |
| `kpi_energy_ratio_6h` | measured / planned heat pump energy |
| `kpi_loss_w_per_k`, `kpi_solar_aperture_m2`, `kpi_internal_gains_w`, `kpi_building_days` | building fit |
| `kpi_cop_scale`, `kpi_measured_cop`, `kpi_emitter_w_per_k`, `kpi_dhw_loss_w_per_k` | unit and tank fits |
| `kpi_draw_kwh_per_day`, `kpi_draw_days`, `kpi_airing_open_h_per_day`, `kpi_calibrated` | profiles, status |

A growing room bias at 6 h points to building parameters, an energy ratio far from 1 to the COP or the
idle power, a large deviation at 1 h only to the controller.

## Holiday mode

Measurements `<id>-away` (1 = away), `<id>-return-ts` (Unix time, 0 = unknown → whole horizon away)
and `<id>-away-room-temperature`. Until the return: the comfort minimum and the NORMAL setpoint are the
away temperature, RAISED heats back to the normal setpoint, no tapping, and with `away.dhw_off` no
hot water minimum and no reheating by the unit. From the return on the normal band applies; a cold tank
at the return is penalised, so the optimizer heats building and tank up before the return in cheap hours.

**Standby operating mode.** If the controller switches the unit into its own standby (frost
protection) while EOS plans REDUCED, set the room floor `away.standby_min_c` (or measurement
`<id>-away-min-temperature`). The model then follows this rule:

- standby while REDUCED without hot water charge is planned and the room is above the floor:
  no heating, idle power `unit.off_w`;
- any other mode, or the room at its floor, ends standby: idle power `unit.standby_w`;
- if the unit charges hot water on every restart, set `away.standby_exit_dhw_c` to that temperature
  (empty = no charge, e.g. when the controller lowers the hot water setpoints while away).

The solution key `<id>_standby` states per slot whether the unit is in standby under this rule, so
the controller switches standby on and off from the plan instead of deriving it from the modes. It
should still keep its own room floor as a safety net and leave standby when the plan is missing.

`away.internal_gains_share` scales the internal gains while nobody is home (people, cooking and most
devices are missing). Tank heat has no value at the horizon end while still away with hot water off.

| Setting | Meaning | Default | Example |
| --- | --- | --- | --- |
| `unit.standby_w` | idle power, ready | 22 W | 32 W |
| `unit.off_w` | idle power in standby | = `standby_w` | 10 W |
| `away.standby_min_c` | room floor in standby | none | 16 °C |
| `away.standby_exit_dhw_c` | hot water charge on restart | none | none |
| `away.internal_gains_share` | internal gains while away | 1.0 | 0.4 |

## Zone set-back and airing

`zone_setback`: part of the house (`share` of the rooms) runs at a set-back temperature in configured
windows (`windows`, time windows with weekdays). EOS models one zone with the mean room temperature and
lowers setpoint, comfort band and minimum accordingly, so the cooler rooms are not read as comfort
violations. Solution key `<id>_zone_setback` tells the controller when.

`ventilation`: `<id>-window-open` (share of open windows, 0…1) is an extra room loss
(`loss_w_per_k` × share × (room − outdoor)); the expected airing per hour (`profile`) is learned by the
calibration. With `recommend`, EOS picks one slot per day for airing (warmest, most PV surplus) and
publishes `<id>_ventilate`. The slot is remembered in `<data folder>/rl29/heatpump_ventilation.json`:
until it starts a newer forecast may move it, once it has started it is final and later runs of the
day recommend nothing else (before, the afternoon runs marked the current slot again and again). A day
on which at least `recommend_skip_aired_share` (default 0.7) of the windows were already open in the
window (`recommend_start_hour` … `recommend_end_hour`) gets no recommendation; `None` turns that off.

## Zones (multi-zone heating)

Houses with floors that react differently (a heavy wet screed upstairs, a light dry screed downstairs)
can be planned per zone (`zones.enabled`, at least two `zones`):

- every zone has its own two-node model: `building` or, without it, the house model × `share`;
- `couplings` exchange heat between the rooms of two zones (ceiling, stairs) [W/K];
- the optimizer picks a mode per zone and hour. REDUCED to PREHEAT set the zone's room target (the
  room controllers regulate and close the valves on their own), FORCED opens all valves of the zone;
- the zones share the heating circuit: one flow temperature and one heat pump. The heat pump mode is
  derived per slot: FORCED if any zone is forced, the strongest raise otherwise, REDUCED only when all
  zones are reduced. Space heat is distributed by the zones' demands; if the heat pump cannot deliver
  the sum, every zone gets the same share. Hot water, standby, §14a and cooling use the house model;
- comfort is judged per zone, weighted by `weight` (default `share`);
- `setback_windows` / `setback_room_c` per zone replace the single `zone_setback`
  (`<device_id>-setback-room-temperature` overrides the temperature);
- measurements per zone: `<device_id>-<zone_id>-room-temperature` and, with `room_limits`,
  `<device_id>-<zone_id>-room-min-margin` / `-room-max-margin`; a missing zone uses the house mean;
- solution columns per zone: `<device_id>_zone_<zone_id>_heating_mode` and
  `<device_id>_zone_<zone_id>_room_temperature_c`; `<device_id>_heating_mode` stays the heat pump mode.

```yaml
devices:
  heat_pumps:
    hp1:
      zones:
        enabled: true
        zones:
          - zone_id: og          # wet screed, set back at night
            share: 0.4
            building: {slab_capacity_wh_per_k: 2500, room_capacity_wh_per_k: 4000,
                       loss_w_per_k: 80, slab_room_w_per_k: 600, emitter_w_per_k: 400}
            setback_windows: {windows: [{start_time: "22:00", duration: "8 hours"}]}
          - zone_id: eg          # dry screed, ground floor and basement
            share: 0.6
        couplings:
          - {zone_a: og, zone_b: eg, w_per_k: 150}
```

### Fixed room setpoints

`comfort.fixed_room_setpoints: true` describes a controller that never changes the room
controllers' setpoints: EOS acts only through SG Ready and forced valve positions. The model then
follows what such a house can actually do:

- RAISED keeps the room target at the setpoint (SG Ready 3 only raises the flow temperature; the
  controllers still close at their setpoint);
- a single zone cannot be reduced on its own. REDUCED needs SG Ready "reduced" for the whole circuit,
  so a REDUCED zone runs NORMAL unless every zone is reduced;
- PREHEAT and FORCED act through forced valve positions per zone and are unchanged. This is how one
  zone is heated harder than another to even out the floors;
- the solution columns `<device_id>_zone_<zone_id>_heating_mode` report the mode the zone actually
  runs, so a controller can force exactly the zones shown as PREHEAT/FORCED.

The single `zone_setback` lowers room setpoints and does not fit this mode.

The MILP solves the house as one zone and its projection gives every zone the same mode; the
GENETIC polish then separates the zones. Calibration still fits the house model (per-zone
calibration is planned).

## Plan stability

EOS replans every few minutes. Three rules keep the running mode from flipping with every run:

- **Change against the running mode.** The mode the previous plan set for now (heating and hot
  water) is passed to the run; a plan that starts with another mode pays
  `optimization.mode_change_penalty_amt` like any other change (example: 0.05). A switch therefore
  has to save more than the penalty; marginal differences of a stochastic search no longer flip it.
- **Gene periods on full hours.** With 15 minute slots one gene controls one hour. The periods start
  on full hours (the first one is shorter, the last one longer), so runs at :15, :30 and :45 keep the
  same period boundaries and the warm start of the previous run fits slot for slot.
- **One planned hot water charge per block.** `HEAT_PUMP` charges the tank once to the setpoint, as the
  controller switches the push contact once per block; the model no longer tops the tank up for every
  few tenths of a kelvin (that drew a saw tooth into the planned hot water temperature). Below
  setpoint − hysteresis the heat pump's own controller still reheats.

The controller's measurements must be younger than `optimization.genetic.measurement_max_age_seconds`;
set it above the controller's sending interval (example: 900 s for a 5 minute interval), otherwise
runs start from model defaults (room setpoint, tank setpoint − hysteresis/2) and the plan jumps.

### Minimum hold time

`optimization.min_hold_minutes` (example: 55; 0 = off, GENETIC only) is the shortest time between two
changes of the executed level. The level of a mode is `optimization.sg_ready_of_mode` (default SG Ready:
REDUCED 1, NORMAL 2, RAISED 3, PREHEAT 3, FORCED 4, COOL 2), so RAISED ↔ PREHEAT or which zone is forced
change freely. The plan's modes per gene period are applied slot by slot: a change to another level waits
until the hold has passed since the last change, the modes in effect stay meanwhile; a return to the level
of NORMAL is always allowed and starts a new hold. The plan rows (`heating_mode`, `zone_<z>_heating_mode`)
show the executed modes, and the simulation (costs, comfort) uses them, so the optimizer plans with the hold.

At the start of a plan the hold counts from the last change the controller reports: measurements
`<id>-sg-ready-actual` (level switched now) and `<id>-sg-ready-changed-ts` (Unix time of the last switch).
Without them the previous plan's mode is the current level and the hold starts with the first change in
the plan. The plausibility check (`scripts/rl29/check_heatpump_plan.py`) warns if a plan violates the hold.

## Room limits and humidity

EOS models the mean of all rooms. Two optional inputs let the controller pass on what it knows
about single rooms; without them nothing changes.

`room_limits` (`enabled`, `horizon_hours` 6, `max_raise_k` 2): the controller compares every room
with its own limits (rooms with open windows or just aired left out) and reports the smallest
margins, `<id>-room-min-margin` = min(room − lower limit) and `<id>-room-max-margin` =
min(upper limit − room). For the next `horizon_hours` EOS assumes the rooms move with the mean:

- comfort minimum of the mean ≥ room now − min margin (at most `max_raise_k` above the band and
  not above the NORMAL setpoint), so REDUCED stops before the coldest room reaches its limit;
- comfort maximum and RAISED target ≤ room now + max margin (not below the NORMAL setpoint).

While away the margins apply until the return only (the controller reports them against the
limits valid now). Give rooms that are cold by design (a basement office with a lower setpoint)
their own lower limit in the controller instead of raising the whole house.

`humidity` (`enabled`, `max_pct` 65, `hold_hours` 2, `max_drop_k` 0.5): while `<id>-humidity`
is at or above `max_pct`, the comfort minimum for the next `hold_hours` is at most `max_drop_k`
below the NORMAL setpoint – REDUCED then does not cool damp rooms. The controller may send its own
debounced flag instead (100 = high, 0 = normal).

## Heating rod with its own surplus controller

Rods behind a PV power diverter (e.g. a controller that reads the grid meter or the inverter) heat
on their own with PV surplus, second by second and stepless. `dhw.rod_surplus_c` (e.g. 60 °C) tells
EOS about it: in every slot the rod uses the forecast PV surplus (PV minus household load) left after
the heat pump, up to this temperature. EOS then plans no heat pump charge the rod covers anyway, and
values the rod energy like any other load (lost feed-in). A planned `ROD_<target>` slot remains a
boost from the grid (negative prices, legionella) that the controller has to start, e.g. via the
diverter's boost or setpoint input (or, with `rod_planned_surplus_only`, its surplus mode – see
below). The MILP model plans without this rod; its final check uses the model above.

Rods with relay stages instead of stepless control: `dhw.rod_power_steps_w` (e.g. `[1000, 2000, 3000]`).
In surplus mode the rod then uses the highest stage the surplus covers, never a partial stage.

### Planned rod in surplus mode

`dhw.rod_planned_surplus_only: true` is for a rod that only runs when EOS plans it, but then in its
controller's PV surplus mode (no grid, no battery). A planned `ROD_<target>` slot (e.g. 70 °C for a
large hot water reserve, 60 °C for legionella) then uses only the forecast PV surplus left after the
heat pump, up to the planned target; without surplus it does nothing. Outside planned slots the rod
stays off. The MILP model caps the rod per slot at the forecast surplus. Typical use: the heat pump
keeps the tank at its own setpoint (`dhw_modes: [0, 2]`, no heat pump boost), the rod lifts it above.
Execution: start the controller's surplus mode with the planned target while `ROD` is planned. Without
PV surplus the legionella deadline can pass; the plan then reports `legionella_ok: false`.

Legionella fallback: `dhw.rod_legionella_grid_hours` (e.g. 24) lets a planned `ROD` slot heat at full
power from the grid in the last hours before the legionella deadline, or at once when it is overdue.
EOS still prefers PV slots and only falls back to the cheapest grid hours of that window. Such slots
carry `<id>_rod_grid = 1` in the plan: start the controller's forced mode there, its surplus mode
elsewhere.

## Weather warnings

`weather_warning` (`enabled`, `raise_room_k` 1 K, `dhw_min_c` 50 °C, `max_hours` 48): the controller
reports a warning that may cut the power (storm, ice, heavy frost) as `<id>-warning-start-ts` and
`<id>-warning-end-ts` (Unix times, 0 = none). For the slots of the warning the comfort minimum rises
by `raise_room_k` (at most to the comfort maximum) and the hot water minimum to `dhw_min_c` (at most
what heat pump or rod reach). EOS charges building and tank beforehand, in the cheapest hours before
the warning. While away with hot water off only the room part applies.

## Lock of the reduction

A controller may block reducing for a while, e.g. after somebody felt cold. It reports the end as
`<id>-reduce-lock-until-ts` (Unix time; 0 or a past time = no lock). Until then EOS plans what the
controller will run: REDUCED counts as NORMAL (room target, energy, no standby), and the plan shows
NORMAL in these slots. Plans therefore no longer count on savings that will not happen.

## Cooling

`cooling.enabled` with measurement `<id>-cooling-season` = 1: EOS chooses per hour between NORMAL and
COOL. The EER is scaled with outdoor and flow temperature (`eer`, `eer_rated_*`; `eer_constant` disables
that), the flow temperature stays above the dew point (`<id>-dewpoint` + `dewpoint_margin_k`), and with
`pv_only` cooling uses PV surplus only.

## §14a EnWG (Germany)

`elecfee.grid_control_14a`: `none`, `module1` (lump sum, dimming), `module2` (own meter for the
controllable devices: reduced price, not part of the house balance) or `module1_3` (time-variable grid
fee: the regular grid fee in the fixed fee provider is replaced per quarter hour by the band prices,
`module3_network_amt_kwh`). `controllable_device_ids`, `regular_network_amt_kwh`, `dimming_power_w`
(4200 W). `<id>-power-limited` = 1 limits the controllable devices to the dimming power from now.
`scripts/rl29/gridfee_14a.py` writes these settings (`--module …`, `--apply http://<eos>:8503`).

Module 3 with the import fee provider (`ElecFeeImport`): imported fees are used as they are. If they
contain only the regular grid fee, set `elecfee.elecfeeimport.module3_included: false`; EOS then adds
the band price minus the regular price, like the fixed provider does.

## Negative prices (Germany, § 51 EEG)

Plants commissioned from 2025-02-25 get no feed-in tariff while the market price is negative.
`feedintariff.zero_at_negative_price: true` sets the feed-in value of these slots to 0 (raw market
price < 0, also in the price forecast after the published day-ahead). Feed-in then earns nothing, so
the optimizer moves the heat pump, hot water and appliances into these slots.

## PDF report

`GET /v1/energy-management/optimization/solution/GENETIC/pdf` adds per heat pump: temperatures with the
comfort band, hot water, energy and COP, the mode sequence, plan vs actual, the last calibration and
learned appliance profiles.

## Minimal configuration

```json
{
  "devices": {
    "max_heat_pumps": 1,
    "heat_pumps": {
      "hp1": {
        "comfort": {"setpoint_c": 21.0, "max_c": 22.0},
        "dhw": {"volume_l": 200, "setpoint_c": 45},
        "optimization": {"heating_modes": [0, 1, 2], "dhw_modes": [0, 1]},
        "calibration": {"enabled": true}
      }
    }
  },
  "adapter": {"homeassistant": {
    "device_measurement_entity_ids": {
      "hp1-room-temperature": "sensor.<mean_room_temperature>",
      "hp1-dhw-temperature": "sensor.<tank_temperature>",
      "hp1-outdoor-temperature": "sensor.<outdoor_temperature>",
      "hp1-electric-power": "sensor.<heat_pump_power_meter>",
      "hp1-heat-power": "sensor.<heat_output>",
      "hp1-flow-temperature": "sensor.<flow_temperature>"
    },
    "device_measurement_history_keys": ["hp1-electric-power", "hp1-heat-power", "hp1-flow-temperature"],
    "solution_entity_ids": ["sensor.eos_hp1_heating_mode", "sensor.eos_hp1_dhw_mode"]
  }}
}
```

If the load forecast still contains the heat pump, set `optimization.load_forecast_includes_heat_pump`
(EOS then removes the status quo consumption), better forecast the base load without it
(see {doc}`forecasts`).
