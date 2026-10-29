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

RAISED/FORCED can raise the hot water setpoint (`dhw.raised_setpoint_c`, `dhw.forced_setpoint_c`).
Legionella runs (`dhw.legionella_c`, `dhw.legionella_interval_days`) are planned with the cheapest
option that reaches the temperature; without one, no run is planned and the log says so.

The objective contains, besides the energy costs: comfort shortfall (`optimization.comfort_penalty_amt_per_kh`),
overheating, hot water below its minimum, a missed legionella run, mode changes and active modes without
effect, and the **value of the heat stored at the horizon end** (it replaces heat produced later in the
cheapest hours: mean of the cheapest `stored_heat_price_quantile` of the marginal prices, divided by the COP).

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
| `<id>-slab-temperature` | optional; otherwise estimated from steady state |
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
- no reduction while a room is below a lower limit (with hysteresis; rooms with open windows excluded),
  no raising while the warmest room is above an upper limit – report the margins to EOS as well
  (see room limits below), so the plan already respects them;
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
publishes `<id>_ventilate`.

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
diverter's boost or setpoint input. The MILP model plans without this rod; its final check uses the
model above.

Rods with relay stages instead of stepless control: `dhw.rod_power_steps_w` (e.g. `[1000, 2000, 3000]`).
In surplus mode the rod then uses the highest stage the surplus covers, never a partial stage.

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
