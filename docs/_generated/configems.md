## Energy Management Configuration

<!-- pyml disable line-length -->
:::{table} ems
:widths: 10 20 10 5 5 30
:align: left

| Name | Environment Variable | Type | Read-Only | Default | Description |
| ---- | -------------------- | ---- | --------- | ------- | ----------- |
| interval | `EOS_EMS__INTERVAL` | `float` | `rw` | `300.0` | Intervall between EOS energy management runs [seconds]. |
| mode | `EOS_EMS__MODE` | `<enum 'EnergyManagementMode'>` | `rw` | `required` | Energy management mode [DISABLED | PREDICTION | OPTIMIZATION]. Defaults to DISABLED. |
| modes | | `list[str]` | `ro` | `N/A` | Available energy management modes. |
| notify_url | `EOS_EMS__NOTIFY_URL` | `Optional[str]` | `rw` | `None` | URL that receives an HTTP POST with a small JSON event after every completed optimization, so a client can fetch the new solution right away instead of polling for it. None = off. |
| replan_min_interval_sec | `EOS_EMS__REPLAN_MIN_INTERVAL_SEC` | `float` | `rw` | `300.0` | Minimum time between two runs [seconds]. |
| replan_on_deviation | `EOS_EMS__REPLAN_ON_DEVIATION` | `bool` | `rw` | `False` | eos-plus fork: start an extra run between slot starts when the battery SoC or a heat pump temperature deviates from the plan (polled every `interval`). |
| replan_soc_deviation_pct | `EOS_EMS__REPLAN_SOC_DEVIATION_PCT` | `float` | `rw` | `10.0` | SoC deviation that triggers a run [%-points]. |
| replan_temperature_deviation_k | `EOS_EMS__REPLAN_TEMPERATURE_DEVIATION_K` | `float` | `rw` | `0.8` | Room temperature deviation that triggers a run [K]; hot water uses four times this value. |
| run_on_slot_start | `EOS_EMS__RUN_ON_SLOT_START` | `bool` | `rw` | `False` | Run the energy management right after every optimization slot starts (plus slot_start_offset_sec) instead of every `interval` seconds since start-up. `interval` then is only the polling period (60 s recommended), so the first slot of every plan is (almost) complete instead of partly elapsed. |
| slot_start_offset_sec | `EOS_EMS__SLOT_START_OFFSET_SEC` | `float` | `rw` | `20.0` | Delay after the slot start before the aligned run [seconds]; gives the measurements of the slot boundary time to arrive. |
| startup_delay | `EOS_EMS__STARTUP_DELAY` | `float` | `rw` | `5` | Startup delay in seconds for EOS energy management runs. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "ems": {
           "startup_delay": 5.0,
           "interval": 300.0,
           "mode": "OPTIMIZATION",
           "run_on_slot_start": true,
           "slot_start_offset_sec": 20.0,
           "replan_on_deviation": true,
           "replan_soc_deviation_pct": 10.0,
           "replan_temperature_deviation_k": 0.8,
           "replan_min_interval_sec": 300.0,
           "notify_url": null
       }
   }
```
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "ems": {
           "startup_delay": 5.0,
           "interval": 300.0,
           "mode": "OPTIMIZATION",
           "run_on_slot_start": true,
           "slot_start_offset_sec": 20.0,
           "replan_on_deviation": true,
           "replan_soc_deviation_pct": 10.0,
           "replan_temperature_deviation_k": 0.8,
           "replan_min_interval_sec": 300.0,
           "notify_url": null,
           "modes": [
               "DISABLED",
               "PREDICTION",
               "OPTIMIZATION"
           ]
       }
   }
```
<!-- pyml enable line-length -->
