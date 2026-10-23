## Configuration for all controllable devices in the simulation

Every device collection is a ``dict[str, <Settings>]`` keyed by
``device_id``.  This makes config paths stable regardless of
declaration order and lets each device settings class build its own
config path from ``self.device_id`` without needing an external index.

<!-- pyml disable line-length -->
:::{table} devices
:widths: 10 20 10 5 5 30
:align: left

| Name | Environment Variable | Type | Read-Only | Default | Description |
| ---- | -------------------- | ---- | --------- | ------- | ----------- |
| batteries | `EOS_DEVICES__BATTERIES` | `Optional[dict[str, akkudoktoreos.devices.settings.batterysettings.BatteriesCommonSettings]]` | `rw` | `None` | Stationary battery storage devices, keyed by device_id. |
| electric_vehicles | `EOS_DEVICES__ELECTRIC_VEHICLES` | `Optional[dict[str, akkudoktoreos.devices.settings.batterysettings.BatteriesCommonSettings]]` | `rw` | `None` | Electric vehicle battery packs, keyed by device_id. |
| heat_pumps | `EOS_DEVICES__HEAT_PUMPS` | `dict[str, akkudoktoreos.devices.settings.heatpumpsettings.HeatPumpCommonSettings]` | `rw` | `required` | Heat pumps with building, hot water tank and heating rod, keyed by device_id. Optimized by GENETIC (RL29 fork). |
| home_appliances | `EOS_DEVICES__HOME_APPLIANCES` | `dict[str, akkudoktoreos.devices.settings.homeappliancesettings.HomeApplianceCommonSettings]` | `rw` | `required` | Shiftable home appliance devices, keyed by device_id. |
| inverters | `EOS_DEVICES__INVERTERS` | `Optional[dict[str, akkudoktoreos.devices.settings.invertersettings.InverterCommonSettings]]` | `rw` | `None` | Inverter devices, keyed by device_id. |
| max_batteries | `EOS_DEVICES__MAX_BATTERIES` | `Optional[int]` | `rw` | `None` | Maximum number of batteries allowed. |
| max_electric_vehicles | `EOS_DEVICES__MAX_ELECTRIC_VEHICLES` | `Optional[int]` | `rw` | `None` | Maximum number of EVs allowed. |
| max_heat_pumps | `EOS_DEVICES__MAX_HEAT_PUMPS` | `Optional[int]` | `rw` | `None` | Maximum number of heat pumps allowed. |
| max_home_appliances | `EOS_DEVICES__MAX_HOME_APPLIANCES` | `Optional[int]` | `rw` | `None` | Maximum number of home appliances allowed. |
| max_inverters | `EOS_DEVICES__MAX_INVERTERS` | `Optional[int]` | `rw` | `None` | Maximum number of inverters allowed. |
| measurement_keys | | `list[str]` | `ro` | `N/A` | All measurement keys across all configured devices. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "devices": {
           "batteries": {
               "bat0": {
                   "device_id": "bat0",
                   "capacity_estimation": null,
                   "capacity_estimate": null,
                   "capacity_wh": 8000,
                   "charging_efficiency": 0.88,
                   "discharging_efficiency": 0.88,
                   "levelized_cost_of_storage_amt_kwh": 0.0,
                   "max_charge_power_w": 5000,
                   "min_charge_power_w": 50,
                   "charge_rates": [
                       0.0,
                       0.1,
                       0.2,
                       0.3,
                       0.4,
                       0.5,
                       0.6,
                       0.7,
                       0.8,
                       0.9,
                       1.0
                   ],
                   "min_soc_percentage": 0,
                   "max_soc_percentage": 100,
                   "grid_export_rates": [
                       0.25,
                       0.5,
                       0.75,
                       1.0
                   ],
                   "min_soc_deadline_datetime": null,
                   "min_soc_max_duration_h": null
               }
           },
           "max_batteries": 1,
           "electric_vehicles": {
               "ev0": {
                   "device_id": "ev0",
                   "capacity_estimation": null,
                   "capacity_estimate": null,
                   "capacity_wh": 60000,
                   "charging_efficiency": 0.88,
                   "discharging_efficiency": 0.88,
                   "levelized_cost_of_storage_amt_kwh": 0.0,
                   "max_charge_power_w": 5000,
                   "min_charge_power_w": 50,
                   "charge_rates": [
                       0.0,
                       0.1,
                       0.2,
                       0.3,
                       0.4,
                       0.5,
                       0.6,
                       0.7,
                       0.8,
                       0.9,
                       1.0
                   ],
                   "min_soc_percentage": 0,
                   "max_soc_percentage": 100,
                   "grid_export_rates": [
                       0.25,
                       0.5,
                       0.75,
                       1.0
                   ],
                   "min_soc_deadline_datetime": null,
                   "min_soc_max_duration_h": null
               }
           },
           "max_electric_vehicles": 1,
           "inverters": {},
           "max_inverters": 1,
           "home_appliances": {
               "dishwasher": {
                   "device_id": "dishwasher",
                   "consumption_wh": 1500,
                   "duration_h": 2,
                   "num_cycles": 1,
                   "cycle_time_windows": null,
                   "min_cycle_gap_h": 0,
                   "cycles_completed_measurement_key": null,
                   "load_profile_power_w": null,
                   "load_profile_interval_seconds": null,
                   "schedule_mode": "ONCE",
                   "time_windows": null,
                   "earliest_start_datetime": null,
                   "deadline_datetime": null,
                   "deadline_policy": "BEST_EFFORT",
                   "request_driven": false,
                   "request_max_age_seconds": 3600,
                   "learn_profiles": false,
                   "profile_learning_rate": 0.3,
                   "profile_power_threshold_w": 20.0,
                   "profile_duration_quantile": 0.8,
                   "profile_outlier_tolerance": 0.4
               }
           },
           "max_home_appliances": 3,
           "heat_pumps": {
               "wbb12": {
                   "device_id": "wbb12",
                   "building": {
                       "slab_capacity_wh_per_k": 3500.0,
                       "room_capacity_wh_per_k": 10000.0,
                       "loss_w_per_k": 180.0,
                       "slab_room_w_per_k": 1500.0,
                       "emitter_w_per_k": 1000.0,
                       "solar_aperture_m2": 0.0,
                       "internal_gains_w": 0.0
                   },
                   "unit": {
                       "cop_points": [
                           {
                               "outdoor_c": 7.0,
                               "flow_c": 35.0,
                               "cop": 4.85
                           },
                           {
                               "outdoor_c": 2.0,
                               "flow_c": 35.0,
                               "cop": 4.11
                           },
                           {
                               "outdoor_c": -7.0,
                               "flow_c": 35.0,
                               "cop": 3.28
                           },
                           {
                               "outdoor_c": 7.0,
                               "flow_c": 55.0,
                               "cop": 3.1
                           },
                           {
                               "outdoor_c": -7.0,
                               "flow_c": 55.0,
                               "cop": 2.47
                           }
                       ],
                       "cop_scale": 1.0,
                       "cop_max": 7.0,
                       "max_heat_w": 10000.0,
                       "max_electric_w": 5500.0,
                       "standby_w": 22.0,
                       "max_flow_c": 45.0,
                       "flow_spread_k": 3.0,
                       "dhw_heat_w": 5000.0,
                       "dhw_flow_overshoot_k": 7.0,
                       "max_dhw_c": 60.0,
                       "forced_overshoot_k": 1.0
                   },
                   "dhw": {
                       "volume_l": 190.0,
                       "loss_w_per_k": 1.5,
                       "ambient_c": 18.0,
                       "setpoint_c": 45.0,
                       "hysteresis_k": 5.0,
                       "min_c": 38.0,
                       "raised_setpoint_c": null,
                       "forced_setpoint_c": null,
                       "rod_power_w": 3000.0,
                       "rod_targets_c": [
                           60.0,
                           70.0
                       ],
                       "rod_max_c": 70.0,
                       "legionella_c": 60.0,
                       "legionella_interval_days": 7,
                       "draw_profile_wh": [
                           20.0,
                           10.0,
                           10.0,
                           10.0,
                           20.0,
                           80.0,
                           450.0,
                           500.0,
                           250.0,
                           120.0,
                           100.0,
                           120.0,
                           200.0,
                           150.0,
                           100.0,
                           100.0,
                           120.0,
                           200.0,
                           350.0,
                           450.0,
                           350.0,
                           200.0,
                           80.0,
                           40.0
                       ],
                       "draw_profile_weekend_wh": null
                   },
                   "comfort": {
                       "setpoint_c": 21.0,
                       "max_c": 22.0,
                       "min_c": {
                           "windows": [
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "6 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": 19.5
                               },
                               {
                                   "start_time": "06:00:00.000000",
                                   "duration": "16 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": 20.5
                               },
                               {
                                   "start_time": "22:00:00.000000",
                                   "duration": "2 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": 19.5
                               }
                           ]
                       },
                       "raised_offset_k": 1.0
                   },
                   "optimization": {
                       "heating_modes": [
                           0,
                           1,
                           2
                       ],
                       "dhw_modes": [
                           0,
                           1,
                           2
                       ],
                       "comfort_penalty_amt_per_kh": 1.0,
                       "overheat_penalty_amt_per_kh": 0.5,
                       "active_mode_penalty_amt_per_h": 0.01,
                       "dhw_penalty_amt_per_kh": 0.5,
                       "legionella_penalty_amt": 2.0,
                       "mode_change_penalty_amt": 0.002,
                       "stored_heat_price_quantile": 0.25,
                       "load_forecast_includes_heat_pump": false
                   },
                   "cooling": {
                       "enabled": false,
                       "setpoint_c": 24.0,
                       "max_c": 26.0,
                       "max_cool_w": 6000.0,
                       "eer": 4.5,
                       "eer_rated_outdoor_c": 35.0,
                       "eer_rated_flow_c": 18.0,
                       "eer_constant": false,
                       "min_flow_c": 18.0,
                       "dewpoint_margin_k": 2.0,
                       "pv_only": true,
                       "min_pv_surplus_w": 500.0
                   },
                   "calibration": {
                       "enabled": false,
                       "days": 30,
                       "refit_hours": 24.0,
                       "fit_building": true,
                       "fit_cop": true,
                       "fit_dhw": true,
                       "fit_emitter": true,
                       "fit_draw_profile": true,
                       "draw_min_days": 14,
                       "draw_quantile": null,
                       "fit_ventilation_profile": true,
                       "weather_from_prediction": true,
                       "min_days": 5,
                       "prior_weight_days": 7.0,
                       "outage_threshold": 0.4,
                       "loss_min_factor": 0.3,
                       "loss_max_factor": 3.0,
                       "cop_min_heat_w": 3000.0,
                       "cop_prior_hours": 24.0,
                       "cop_min_scale": 0.5,
                       "cop_max_scale": 1.5
                   },
                   "away": {
                       "room_c": 20.0,
                       "dhw_off": true,
                       "cooling_max_c": 28.0,
                       "max_age_hours": 48.0
                   },
                   "zone_setback": {
                       "enabled": false,
                       "share": 0.4,
                       "room_c": 20.0,
                       "min_offset_k": 0.5,
                       "windows": {
                           "windows": [
                               {
                                   "start_time": "22:00:00.000000",
                                   "duration": "2 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 0,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 1,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 2,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 3,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 4,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "8 hours",
                                   "day_of_week": 5,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "8 hours",
                                   "day_of_week": 6,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               }
                           ]
                       }
                   },
                   "ventilation": {
                       "enabled": false,
                       "loss_w_per_k": 1000.0,
                       "profile": [
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.1,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.1,
                           0.0,
                           0.0
                       ],
                       "profile_weekend": null,
                       "recommend": false,
                       "recommend_start_hour": 11,
                       "recommend_end_hour": 15,
                       "recommend_open_hours": 0.15
                   },
                   "power_limit_w": null
               }
           },
           "max_heat_pumps": 1
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
       "devices": {
           "batteries": {
               "bat0": {
                   "device_id": "bat0",
                   "capacity_estimation": null,
                   "capacity_estimate": null,
                   "capacity_wh": 8000,
                   "charging_efficiency": 0.88,
                   "discharging_efficiency": 0.88,
                   "levelized_cost_of_storage_amt_kwh": 0.0,
                   "max_charge_power_w": 5000,
                   "min_charge_power_w": 50,
                   "charge_rates": [
                       0.0,
                       0.1,
                       0.2,
                       0.3,
                       0.4,
                       0.5,
                       0.6,
                       0.7,
                       0.8,
                       0.9,
                       1.0
                   ],
                   "min_soc_percentage": 0,
                   "max_soc_percentage": 100,
                   "grid_export_rates": [
                       0.25,
                       0.5,
                       0.75,
                       1.0
                   ],
                   "min_soc_deadline_datetime": null,
                   "min_soc_max_duration_h": null,
                   "measurement_key_soc_factor": "bat0-soc-factor",
                   "measurement_key_power_l1_w": "bat0-power-l1-w",
                   "measurement_key_power_l2_w": "bat0-power-l2-w",
                   "measurement_key_power_l3_w": "bat0-power-l3-w",
                   "measurement_key_power_3_phase_sym_w": "bat0-power-3-phase-sym-w",
                   "measurement_keys": [
                       "bat0-soc-factor",
                       "bat0-power-l1-w",
                       "bat0-power-l2-w",
                       "bat0-power-l3-w",
                       "bat0-power-3-phase-sym-w"
                   ]
               }
           },
           "max_batteries": 1,
           "electric_vehicles": {
               "ev0": {
                   "device_id": "ev0",
                   "capacity_estimation": null,
                   "capacity_estimate": null,
                   "capacity_wh": 60000,
                   "charging_efficiency": 0.88,
                   "discharging_efficiency": 0.88,
                   "levelized_cost_of_storage_amt_kwh": 0.0,
                   "max_charge_power_w": 5000,
                   "min_charge_power_w": 50,
                   "charge_rates": [
                       0.0,
                       0.1,
                       0.2,
                       0.3,
                       0.4,
                       0.5,
                       0.6,
                       0.7,
                       0.8,
                       0.9,
                       1.0
                   ],
                   "min_soc_percentage": 0,
                   "max_soc_percentage": 100,
                   "grid_export_rates": [
                       0.25,
                       0.5,
                       0.75,
                       1.0
                   ],
                   "min_soc_deadline_datetime": null,
                   "min_soc_max_duration_h": null,
                   "measurement_key_soc_factor": "ev0-soc-factor",
                   "measurement_key_power_l1_w": "ev0-power-l1-w",
                   "measurement_key_power_l2_w": "ev0-power-l2-w",
                   "measurement_key_power_l3_w": "ev0-power-l3-w",
                   "measurement_key_power_3_phase_sym_w": "ev0-power-3-phase-sym-w",
                   "measurement_keys": [
                       "ev0-soc-factor",
                       "ev0-power-l1-w",
                       "ev0-power-l2-w",
                       "ev0-power-l3-w",
                       "ev0-power-3-phase-sym-w"
                   ]
               }
           },
           "max_electric_vehicles": 1,
           "inverters": {},
           "max_inverters": 1,
           "home_appliances": {
               "dishwasher": {
                   "device_id": "dishwasher",
                   "consumption_wh": 1500,
                   "duration_h": 2,
                   "num_cycles": 1,
                   "cycle_time_windows": null,
                   "min_cycle_gap_h": 0,
                   "cycles_completed_measurement_key": null,
                   "load_profile_power_w": null,
                   "load_profile_interval_seconds": null,
                   "schedule_mode": "ONCE",
                   "time_windows": null,
                   "earliest_start_datetime": null,
                   "deadline_datetime": null,
                   "deadline_policy": "BEST_EFFORT",
                   "request_driven": false,
                   "request_max_age_seconds": 3600,
                   "learn_profiles": false,
                   "profile_learning_rate": 0.3,
                   "profile_power_threshold_w": 20.0,
                   "profile_duration_quantile": 0.8,
                   "profile_outlier_tolerance": 0.4,
                   "measurement_key_power": "dishwasher-power",
                   "measurement_key_program": "dishwasher-program",
                   "measurement_key_requested": "dishwasher-requested",
                   "measurement_key_deadline": "dishwasher-deadline",
                   "measurement_key_duration": "dishwasher-duration",
                   "effective_num_cycles": 1,
                   "measurement_keys": [
                       "dishwasher.cycles_completed"
                   ]
               }
           },
           "max_home_appliances": 3,
           "heat_pumps": {
               "wbb12": {
                   "device_id": "wbb12",
                   "building": {
                       "slab_capacity_wh_per_k": 3500.0,
                       "room_capacity_wh_per_k": 10000.0,
                       "loss_w_per_k": 180.0,
                       "slab_room_w_per_k": 1500.0,
                       "emitter_w_per_k": 1000.0,
                       "solar_aperture_m2": 0.0,
                       "internal_gains_w": 0.0
                   },
                   "unit": {
                       "cop_points": [
                           {
                               "outdoor_c": 7.0,
                               "flow_c": 35.0,
                               "cop": 4.85
                           },
                           {
                               "outdoor_c": 2.0,
                               "flow_c": 35.0,
                               "cop": 4.11
                           },
                           {
                               "outdoor_c": -7.0,
                               "flow_c": 35.0,
                               "cop": 3.28
                           },
                           {
                               "outdoor_c": 7.0,
                               "flow_c": 55.0,
                               "cop": 3.1
                           },
                           {
                               "outdoor_c": -7.0,
                               "flow_c": 55.0,
                               "cop": 2.47
                           }
                       ],
                       "cop_scale": 1.0,
                       "cop_max": 7.0,
                       "max_heat_w": 10000.0,
                       "max_electric_w": 5500.0,
                       "standby_w": 22.0,
                       "max_flow_c": 45.0,
                       "flow_spread_k": 3.0,
                       "dhw_heat_w": 5000.0,
                       "dhw_flow_overshoot_k": 7.0,
                       "max_dhw_c": 60.0,
                       "forced_overshoot_k": 1.0
                   },
                   "dhw": {
                       "volume_l": 190.0,
                       "loss_w_per_k": 1.5,
                       "ambient_c": 18.0,
                       "setpoint_c": 45.0,
                       "hysteresis_k": 5.0,
                       "min_c": 38.0,
                       "raised_setpoint_c": null,
                       "forced_setpoint_c": null,
                       "rod_power_w": 3000.0,
                       "rod_targets_c": [
                           60.0,
                           70.0
                       ],
                       "rod_max_c": 70.0,
                       "legionella_c": 60.0,
                       "legionella_interval_days": 7,
                       "draw_profile_wh": [
                           20.0,
                           10.0,
                           10.0,
                           10.0,
                           20.0,
                           80.0,
                           450.0,
                           500.0,
                           250.0,
                           120.0,
                           100.0,
                           120.0,
                           200.0,
                           150.0,
                           100.0,
                           100.0,
                           120.0,
                           200.0,
                           350.0,
                           450.0,
                           350.0,
                           200.0,
                           80.0,
                           40.0
                       ],
                       "draw_profile_weekend_wh": null
                   },
                   "comfort": {
                       "setpoint_c": 21.0,
                       "max_c": 22.0,
                       "min_c": {
                           "windows": [
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "6 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": 19.5
                               },
                               {
                                   "start_time": "06:00:00.000000",
                                   "duration": "16 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": 20.5
                               },
                               {
                                   "start_time": "22:00:00.000000",
                                   "duration": "2 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": 19.5
                               }
                           ]
                       },
                       "raised_offset_k": 1.0
                   },
                   "optimization": {
                       "heating_modes": [
                           0,
                           1,
                           2
                       ],
                       "dhw_modes": [
                           0,
                           1,
                           2
                       ],
                       "comfort_penalty_amt_per_kh": 1.0,
                       "overheat_penalty_amt_per_kh": 0.5,
                       "active_mode_penalty_amt_per_h": 0.01,
                       "dhw_penalty_amt_per_kh": 0.5,
                       "legionella_penalty_amt": 2.0,
                       "mode_change_penalty_amt": 0.002,
                       "stored_heat_price_quantile": 0.25,
                       "load_forecast_includes_heat_pump": false
                   },
                   "cooling": {
                       "enabled": false,
                       "setpoint_c": 24.0,
                       "max_c": 26.0,
                       "max_cool_w": 6000.0,
                       "eer": 4.5,
                       "eer_rated_outdoor_c": 35.0,
                       "eer_rated_flow_c": 18.0,
                       "eer_constant": false,
                       "min_flow_c": 18.0,
                       "dewpoint_margin_k": 2.0,
                       "pv_only": true,
                       "min_pv_surplus_w": 500.0
                   },
                   "calibration": {
                       "enabled": false,
                       "days": 30,
                       "refit_hours": 24.0,
                       "fit_building": true,
                       "fit_cop": true,
                       "fit_dhw": true,
                       "fit_emitter": true,
                       "fit_draw_profile": true,
                       "draw_min_days": 14,
                       "draw_quantile": null,
                       "fit_ventilation_profile": true,
                       "weather_from_prediction": true,
                       "min_days": 5,
                       "prior_weight_days": 7.0,
                       "outage_threshold": 0.4,
                       "loss_min_factor": 0.3,
                       "loss_max_factor": 3.0,
                       "cop_min_heat_w": 3000.0,
                       "cop_prior_hours": 24.0,
                       "cop_min_scale": 0.5,
                       "cop_max_scale": 1.5
                   },
                   "away": {
                       "room_c": 20.0,
                       "dhw_off": true,
                       "cooling_max_c": 28.0,
                       "max_age_hours": 48.0
                   },
                   "zone_setback": {
                       "enabled": false,
                       "share": 0.4,
                       "room_c": 20.0,
                       "min_offset_k": 0.5,
                       "windows": {
                           "windows": [
                               {
                                   "start_time": "22:00:00.000000",
                                   "duration": "2 hours",
                                   "day_of_week": null,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 0,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 1,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 2,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 3,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "12 hours",
                                   "day_of_week": 4,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "8 hours",
                                   "day_of_week": 5,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               },
                               {
                                   "start_time": "00:00:00.000000",
                                   "duration": "8 hours",
                                   "day_of_week": 6,
                                   "date": null,
                                   "months": null,
                                   "locale": null,
                                   "value": null
                               }
                           ]
                       }
                   },
                   "ventilation": {
                       "enabled": false,
                       "loss_w_per_k": 1000.0,
                       "profile": [
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.1,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.0,
                           0.1,
                           0.0,
                           0.0
                       ],
                       "profile_weekend": null,
                       "recommend": false,
                       "recommend_start_hour": 11,
                       "recommend_end_hour": 15,
                       "recommend_open_hours": 0.15
                   },
                   "power_limit_w": null,
                   "measurement_key_room_temperature": "wbb12-room-temperature",
                   "measurement_key_slab_temperature": "wbb12-slab-temperature",
                   "measurement_key_dhw_temperature": "wbb12-dhw-temperature",
                   "measurement_key_dhw_setpoint": "wbb12-dhw-setpoint",
                   "measurement_key_power_limited": "wbb12-power-limited",
                   "measurement_keys": [
                       "wbb12-room-temperature",
                       "wbb12-slab-temperature",
                       "wbb12-dhw-temperature",
                       "wbb12-dhw-setpoint",
                       "wbb12-power-limited",
                       "wbb12-away",
                       "wbb12-return-ts",
                       "wbb12-away-room-temperature"
                   ],
                   "calibration_measurement_keys": {
                       "outdoor_c": "wbb12-outdoor-temperature",
                       "room_c": "wbb12-room-temperature",
                       "heat_w": "wbb12-heat-power",
                       "electric_w": "wbb12-electric-power",
                       "flow_c": "wbb12-flow-temperature",
                       "dhw_c": "wbb12-dhw-temperature",
                       "dhw_heat_w": "wbb12-dhw-heat-power",
                       "ghi_w_per_m2": "wbb12-ghi",
                       "window_open": "wbb12-window-open"
                   },
                   "measurement_key_cooling_season": "wbb12-cooling-season",
                   "measurement_key_away": "wbb12-away",
                   "measurement_key_return_ts": "wbb12-return-ts",
                   "measurement_key_away_room_temperature": "wbb12-away-room-temperature",
                   "measurement_key_setback_room_temperature": "wbb12-setback-room-temperature",
                   "measurement_key_window_open": "wbb12-window-open",
                   "measurement_key_dewpoint": "wbb12-dewpoint"
               }
           },
           "max_heat_pumps": 1,
           "measurement_keys": [
               "bat0-soc-factor",
               "bat0-power-l1-w",
               "bat0-power-l2-w",
               "bat0-power-l3-w",
               "bat0-power-3-phase-sym-w",
               "ev0-soc-factor",
               "ev0-power-l1-w",
               "ev0-power-l2-w",
               "ev0-power-l3-w",
               "ev0-power-3-phase-sym-w",
               "dishwasher.cycles_completed",
               "wbb12-room-temperature",
               "wbb12-slab-temperature",
               "wbb12-dhw-temperature",
               "wbb12-dhw-setpoint",
               "wbb12-power-limited",
               "wbb12-away",
               "wbb12-return-ts",
               "wbb12-away-room-temperature"
           ]
       }
   }
```
<!-- pyml enable line-length -->
