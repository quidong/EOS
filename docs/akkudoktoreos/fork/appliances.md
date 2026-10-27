% SPDX-License-Identifier: Apache-2.0
(fork-appliances-page)=

# Request-driven appliances

A washing machine or dishwasher should only be planned when it is loaded and released for a remote
start. With `devices.home_appliances.<id>.request_driven: true` EOS plans the appliance only while the
controller reports a fresh request (measurements older than `request_max_age_seconds` count as none):

| Measurement | Meaning |
| --- | --- |
| `<id>-requested` | 1 = a run is waiting (e.g. remote start allowed), 0 = nothing |
| `<id>-deadline` | Unix time by which the run must be finished (0 = no deadline) |
| `<id>-duration` | program duration [h] (with `consumption_wh`/`duration_h`) |

Solution key `<id>_next_start_ts`: Unix time of the next planned start (0 = none), plus `<id>_energy_wh`,
`<id>_run_op_mode` and the DDBC instruction `<id>`.

```json
{
  "devices": {
    "max_home_appliances": 1,
    "home_appliances": {
      "washer": {
        "device_id": "washer",
        "request_driven": true,
        "learn_profiles": true,
        "num_cycles": 1,
        "load_profile_power_w": [1400, 250, 150, 100],
        "load_profile_interval_seconds": 3600,
        "deadline_policy": "BEST_EFFORT"
      }
    }
  }
}
```

## Controller (example)

1. Request detected (remote start allowed, appliance ready) → fix a deadline once, e.g. "finished by
   19:30 if possible, otherwise by 06:00".
2. A plan newer than the request and younger than 45 min → program the start as start delay at the
   appliance (it then starts on its own even if the controller fails).
3. No plan 30 min after the request → fallback: finish by the deadline.
4. EOS moves the start by more than an hour while both times are more than 30 min ahead → adjust the
   start delay (limit the number of adjustments).

## Learned load profiles

`learn_profiles: true` learns one load profile per program (5-minute grid) from `<id>-power`
(power [W], with the full Home Assistant history via `device_measurement_history_keys`) and
`<id>-program` (a fixed number per program):

- runs: power above `profile_power_threshold_w` (20 W), pauses below 15 min belong to the run;
- learning: equal-weight mean of the first runs, then a moving average (`profile_learning_rate`, 0.3),
  aligned at the start; longer runs extend the profile with the mean of its last fifth;
- outliers: from the third run, a run whose energy or duration deviates by more than
  `profile_outlier_tolerance` (40 %) from the median is rejected; three rejections in a row restart the
  profile (the program changed);
- planning duration: the `profile_duration_quantile` (0.8) of the last 20 run times, so that deadlines
  hold in most cases.

Storage: `<data>/plus/appliance_profiles.json`.
