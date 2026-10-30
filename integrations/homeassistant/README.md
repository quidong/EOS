# EOS Control – Home Assistant integration (design)

Status: design, no code yet. Target: a custom integration (`custom_components/eos_control`) that
executes EOS heat pump plans in Home Assistant and keeps the safety rules next to the actuators.
It replaces controller automations and Jinja templates with tested Python. It must work for other
installations: every entity is mapped in the config flow, nothing is hard-coded, optional inputs
stay optional.

## Split of responsibilities

| Layer | Does |
| --- | --- |
| EOS | Optimisation: modes per slot, comfort band, holiday, zone set-back, airing, room margins, humidity |
| EOS Control | Measurements to EOS, plan from EOS, execution, safety rules, diagnostics |
| Home Assistant | Device integrations (KNX, Modbus, …), dashboards, notifications |

Rules that change the plan belong into EOS; rules that must hold without or against EOS stay in
the integration.

## Configuration flow

Step 1 – EOS: URL, device id (`hp1`), poll interval, plan max age.

Step 2 – Heat pump actuators, chosen per transport:

| Transport | Mapping |
| --- | --- |
| SG Ready contacts | two switches (contact 1, contact 2), optional: hot water push switch, push duration |
| Modbus / native entities | select or number for SG Ready, hot water setpoints (normal, reduced), operating mode select with the values for automatic / standby |

Step 3 – Mode mapping: SG Ready state per EOS mode (REDUCED, NORMAL, RAISED, PREHEAT, FORCED),
optional room setpoint per mode, optional "force heating circuits open" for PREHEAT/FORCED.

Step 4 – Rooms (repeatable, any number):

| Field | Required | Meaning |
| --- | --- | --- |
| climate entity or temperature sensor | yes | room temperature |
| lower limit, upper limit | no (defaults) | per-room limits for the margins and the emergency stop |
| window/door sensors | no | airing detection, run-on after closing |
| humidity sensor | no | humidity protection |
| include in mean | no (yes) | room counts for `<id>-room-temperature` |
| set room setpoint | no | climate entity receives the room setpoint per mode |

Step 5 – Optional measurements, each may stay empty: tank temperature, hot water setpoint, slab
temperature, outdoor temperature, electric power, heat power, flow temperature, §14a dimming,
cooling season, dew point.

Step 6 – Safety (all with defaults): max switchings per day, minimum holding time, max reduction
duration, max hot water charges per day, pause after a manual change, emergency limits (room too
cold / too warm, separate limit while away), hysteresis.

Options flow: every step can be changed later; no restart needed.

## What it sends to EOS

Measurements (only those that are mapped): `<id>-room-temperature` (mean), `<id>-dhw-temperature`,
`<id>-room-min-margin`, `<id>-room-max-margin` (rooms with open windows or within the run-on left
out), `<id>-humidity` (highest), `<id>-window-open` (share), holiday (`<id>-away`, `<id>-return-ts`,
`<id>-away-room-temperature`, `<id>-away-min-temperature`), calibration values, and the end of a manual
lock of the reduction (`<id>-reduce-lock-until-ts`, so EOS plans NORMAL until then).

## Execution and safety

- Plan missing, older than the limit or EOS unreachable → NORMAL (SG 2) and a repair issue. While
  EOS does not answer for a short time (it is busy optimising), the last plan stays valid until it is
  older than the limit.
- Settle time (default 10 s): a changed result (SG target, standby, hot water charge, setpoint
  restore) is accepted only after it stayed the same for this time; until then the previous one
  stays in force and the new one is shown as `pending`. The integration re-evaluates by itself
  when the time is up.
- Coldest room below its lower limit → no REDUCED; warmest room above its upper limit → no RAISED.
- Switching limit per day, minimum holding time, maximum reduction duration.
- Manual change at the unit → pause and notification event.
- Watchdogs for timed contacts (hot water push, SG contact 2); time logic in the actuator stays
  recommended.
- Emergency stop: main switch off when rooms stay too cold/warm (holiday: own limit).
- Holiday: standby as EOS plans it (`<id>_standby`; older EOS: REDUCED without hot water), never
  below the holiday room floor and as a reserve when the plan is missing; lowers the hot water setpoints
  (normal must stay above reduced) and restores them before a planned charge and at the return.
- All timestamps and counters are restored after a Home Assistant restart; restart time stamps of
  window sensors do not count as airing.

## Entities

Main switch, holiday switch and return date, transport and mapping as select entities, sensors
for the current EOS mode, the target and actual SG state, the reason of the last decision
("NORMAL: plan older than 45 min"), counters, and diagnostic attributes.

## Comfort learning

Each room can name its setpoint entity (the wall controller's climate entity or a number). A
setpoint raised by hand (not by an automation) and a newly set lock of the reduction are recorded
with the room temperature at that moment; a lock counts for the room closest to its lower limit.
After three such events in one period of the day (night, morning, afternoon, evening) the sensor
"Comfort suggestions" proposes a higher lower limit for that room and period (median temperature at
the interventions + 0.3 K, in 0.5 K steps, at most 2 K above the current limit). Nothing is changed
automatically; adopt a suggestion in the room's settings. Events are kept for 90 days, locally in
Home Assistant's storage; a button resets them. Interventions while away are ignored.

## Structure

One entry per EOS instance (URL, poll interval). Everything else is a subentry, read from the EOS
configuration (`/v1/config/devices`) instead of typed ids:

| Device in Home Assistant | Source | Entities |
| --- | --- | --- |
| EOS | the entry | plan, forecast, planned house load per hour, history, history hours |
| Heat pump (one per entry) | "Add a device from EOS" | SG Ready target, reason, EOS mode, standby/hot water targets, room aggregates, shadow comparison, comfort suggestions, planned heat pump energy, room and hot water temperature |
| Battery | "Add a device from EOS" | operation mode (EOS `BatteryOperationMode`, with next change), mode factor, planned state of charge, charge power target (factor × `max_charge_power_w` while charging from the grid), charging from the grid, discharging allowed, battery export, plan |
| Electric vehicle / wallbox | "Add a device from EOS" | operation mode, mode factor, planned state of charge, charge power target, charging, plan |
| Home appliance | "Add a device from EOS" | running as planned, next start, plan |
| Room | "Add room" | room temperature, margin to the lower limit valid now (holiday: holiday floor), airing, humidity high, comfort suggestion |

The setup offers all devices of EOS at once (the first heat pump and all batteries, EVs and
appliances preselected); more can be added later. The rules and context entities (options) belong
to the heat pump; rooms feed its decision.

Device targets are targets: switching the inverter, wallbox or appliance stays with Home Assistant
automations, as for the heat pump. The settle time applies to a changed mode, too. Without a valid
plan (EOS unreachable longer than the plan age limit) the device entities are unavailable, so an
automation can fall back to a safe default.

Migration from 0.4 (entry = one heat pump): the heat pump becomes a device subentry, the entry is
renamed "EOS". All entity ids stay as they were.

## Data flow between Home Assistant and EOS

| Direction | How |
| --- | --- |
| HA → EOS | The integration sends the measurements itself (`PUT /v1/measurement/data`, every 5 min by default): the heat pump values it computes (room mean, margins, humidity, share of open windows, holiday, return time, reduced setpoint, holiday floor, lock of the reduction) and every other EOS measurement key mapped to an entity — per device in the device's settings ("Configure"), energy meters and keys of devices not added in the options of EOS. The mapping of EOS's own Home Assistant adapter is offered as suggestion. EOS then needs no access to Home Assistant. |
| EOS → HA | The integration reads the plan (`/optimization/solution`) and publishes targets, charts, the plan columns as entities `sensor.eos_<column>` (value of the current slot, unavailable without a valid plan; option "plan columns as entities") and a calendar. A `sensor.eos_<column>` already published by EOS's adapter is not taken over (no `_2`); switch the adapter's `solution_entity_ids` off and restart Home Assistant. |

### Repairs

Settings → System → Repairs shows: EOS unreachable for more than 30 minutes, plan older than the plan
age limit while EOS answers, an added device missing in the EOS configuration, measurements not
accepted by EOS for more than 30 minutes.

### Long-term statistics of the plan

For every completed hour the integration writes the planned values (last plan for each slot) as
external statistics: `eos_control:plan_house_load`, `plan_grid_import`, `plan_grid_export` (kWh per
hour) and for the heat pump `plan_<id>_heat_pump_energy`, `plan_<id>_room_temperature`,
`plan_<id>_dhw_temperature`. A statistics graph shows them next to the meters (stat type mean).

### Actions

| Action | Effect |
| --- | --- |
| `eos_control.lock_reduction` (`until` or `duration`) / `clear_reduction_lock` | no reduction until then (combined with the lock entity: the later one counts) |
| `eos_control.set_holiday` (`return_at`, optional `start`) / `end_holiday` | holiday without helper entities (or-combined with the holiday entity) |
| `eos_control.request_run` (`device`, `finish_by`, optional `duration`) / `cancel_run` | a home appliance run: EOS gets `<id>-requested`, `-deadline`, `-duration` and plans the cheapest start |

State set by actions is stored and survives restarts; with several EOS entries name the entry.

### Calendar

`calendar.eos_plan`: raised heating, preheating, forced heating, standby, hot water charges (heat pump
or rod), appliance runs, charging from the grid and battery export.

## Charts

The integration publishes what EOS planned and forecast, so dashboards need no REST or template
sensors of their own:

| Sensor | State | Attributes |
| --- | --- | --- |
| Plan | time the plan was made | `data`: slot rows (`t`, `load_wh`, `import_wh`, `export_wh`, `costs_amt`, `revenue_amt`, `heat_pump_wh`, `rod_wh`, `heating_mode`, `dhw_mode`, `standby`, `room_c`, `slab_c`, `dhw_c`, `<battery>_soc_pct`); `hourly`: energies per hour in kWh (`load_kwh`, `heat_pump_kwh`, …); `slot_minutes` |
| Forecast | time of the plan | `data`: slot rows (`pv_wh`, `load_wh`, `price_amt_kwh`, `feed_in_amt_kwh`, `outdoor_c`); `hourly`: PV and load in kWh, prices as mean |
| Planned house load / heat pump energy (hour) | kWh planned for the current hour | – (long-term statistics: plan vs. meter per hour) |
| Planned room / hot water temperature | value of the current slot | – (long-term statistics) |
| History | time of the summary | `kpi` (MAE and bias per series and lead time), `plan` (last run, saving against the self-consumption baseline), `days` (net grid costs per day and algorithm), `ab` |
| History hours | time of the query | `data`: forecast/plan and actual value per hour |

Passed slots keep the values of the last plan that contained them (24 hours in `data`, 48 hours
stored, restored after a restart), so a chart shows plan and actual side by side for the last day
and keeps showing the rest of the last plan while EOS is unreachable. The series attributes are not
written to the recorder. The history sensors need the history endpoints of the RL29 fork
(`/v1/energy-management/history/...`); an EOS without them is asked once, the sensors then stay
unavailable.

`dashboard/eos-charts.yaml` is an example view with these charts (needs the custom card
`apexcharts-card` from HACS). The integration does not create dashboards itself.

## Privacy

No personal data leaves Home Assistant except the measurements above, and only to the configured
EOS URL. Room names are not sent; margins and means are aggregated in the integration. Diagnostics
redact the EOS URL.

## Migration from automations

1. Shadow mode: the integration computes targets and reasons but writes nothing; compare with the
   existing automations for a few days.
2. Switch over: turn the integration on, disable the old automations (keep them).
3. Remove the old automations and helpers after a trial period.

## Tests

pytest with `pytest-homeassistant-custom-component`: config flow, mapping validation, every safety
rule, restart restore, holiday sequence (standby, hot water setpoints, return), shadow mode.
