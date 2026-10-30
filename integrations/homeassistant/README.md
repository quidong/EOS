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
