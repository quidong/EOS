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
`<id>-away-room-temperature`, `<id>-away-min-temperature`), calibration values.

## Execution and safety

- Plan missing, older than the limit or EOS unreachable → NORMAL (SG 2) and a repair issue.
- Coldest room below its lower limit → no REDUCED; warmest room above its upper limit → no RAISED.
- Switching limit per day, minimum holding time, maximum reduction duration.
- Manual change at the unit → pause and notification event.
- Watchdogs for timed contacts (hot water push, SG contact 2); time logic in the actuator stays
  recommended.
- Emergency stop: main switch off when rooms stay too cold/warm (holiday: own limit).
- Holiday: standby while EOS plans REDUCED without hot water; lowers the hot water setpoints
  (normal must stay above reduced) and restores them before a planned charge and at the return.
- All timestamps and counters are restored after a Home Assistant restart; restart time stamps of
  window sensors do not count as airing.

## Entities

Main switch, holiday switch and return date, transport and mapping as select entities, sensors
for the current EOS mode, the target and actual SG state, the reason of the last decision
("NORMAL: plan older than 45 min"), counters, and diagnostic attributes.

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
