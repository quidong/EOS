## Full example Config

<!-- pyml disable line-length -->
```json
   {
       "adapter": {
           "provider": [
               "HomeAssistant"
           ],
           "homeassistant": {
               "base_url": null,
               "token": null,
               "config_entity_ids": null,
               "load_emr_entity_ids": null,
               "grid_export_emr_entity_ids": null,
               "grid_import_emr_entity_ids": null,
               "pv_production_emr_entity_ids": null,
               "device_measurement_entity_ids": null,
               "device_measurement_history_keys": null,
               "device_instruction_entity_ids": null,
               "solution_entity_ids": null
           },
           "nodered": {
               "host": "127.0.0.1",
               "port": 1880
           }
       },
       "cache": {
           "subpath": "cache",
           "cleanup_interval": 300.0
       },
       "database": {
           "provider": "LMDB",
           "compression_level": 0,
           "initial_load_window_h": 48,
           "keep_duration_h": 48,
           "autosave_interval_sec": 5,
           "compaction_interval_sec": 3600,
           "batch_size": 100
       },
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
                       ]
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
                   "power_limit_w": null
               }
           },
           "max_heat_pumps": 1
       },
       "elecfee": {
           "provider": "ElecFeeFixed",
           "elecfeefixed": {
               "consumption_amt_kwh": {
                   "windows": []
               },
               "consumption_percent_amt": {
                   "windows": []
               },
               "feedin_amt_kwh": {
                   "windows": []
               },
               "feedin_percent_amt": {
                   "windows": []
               }
           },
           "elecfeeimport": {
               "import_file_path": null,
               "import_json": null
           },
           "grid_control_14a": {
               "module": "none",
               "controllable_device_ids": [],
               "regular_network_amt_kwh": null,
               "module2_reduction_percent": 60.0,
               "module3_network_amt_kwh": {
                   "windows": []
               },
               "dimming_power_w": 4200.0,
               "dimming_hold_hours": 1.0
           }
       },
       "elecprice": {
           "provider": "ElecPriceAkkudoktor",
           "akkudoktor": {},
           "elecpricefixed": {
               "elecprice_marketprice_amt_kwh": {
                   "windows": []
               }
           },
           "elecpriceimport": {
               "import_file_path": null,
               "import_json": null
           },
           "energycharts": {
               "bidding_zone": "DE-LU"
           },
           "smard": {
               "filter_id": 4169,
               "region": "DE"
           },
           "tibber": {
               "access_token": null,
               "home_id": null
           }
       },
       "ems": {
           "startup_delay": 5.0,
           "interval": 300.0,
           "mode": "OPTIMIZATION",
           "notify_url": null
       },
       "feedintariff": {
           "direct_marketing_enabled": false,
           "provider": "FeedInTariffFixed",
           "feedintarifffixed": {
               "feed_in_tariff_amt_kwh": {
                   "windows": []
               }
           },
           "feedintariffimport": {
               "import_file_path": null,
               "import_json": null
           },
           "dvhubonline": {
               "base_url": "https://dvhub.online",
               "zone": "DE-LU"
           },
           "energycharts": {
               "bidding_zone": "DE-LU"
           },
           "smard": {}
       },
       "general": {
           "config_save_mode": "AUTOMATIC",
           "config_save_interval_sec": 60,
           "version": "0.0.0",
           "data_folder_path": "/home/user/.local/share/net.akkudoktoreos.net",
           "data_output_subpath": "output",
           "latitude": 52.52,
           "longitude": 13.405,
           "timezone_override": null
       },
       "load": {
           "provider": "LoadAkkudoktor",
           "loadakkudoktor": {
               "loadakkudoktor_year_energy_kwh": null
           },
           "loadimport": {
               "import_file_path": null,
               "import_json": null
           },
           "vrm": {
               "token": "your-token",
               "site_id": 12345
           }
       },
       "logging": {
           "api_level": "TRACE",
           "console_level": "TRACE",
           "file_level": "TRACE"
       },
       "measurement": {
           "historic_hours": 17520,
           "channels": {},
           "household": null,
           "energy_context_seconds": 86400,
           "load_emr_keys": [
               "load0_emr"
           ],
           "grid_export_emr_keys": [
               "grid_export_emr"
           ],
           "grid_import_emr_keys": [
               "grid_import_emr"
           ],
           "pv_production_emr_keys": [
               "pv1_emr"
           ]
       },
       "optimization": {
           "algorithm": "GENETIC",
           "genetic": {
               "interval_sec": 3600,
               "horizon_hours": 24,
               "individuals": 400,
               "generations": 400,
               "seed": null,
               "fitness_cache_max_entries": null,
               "workers": null,
               "pin_workers": true,
               "measurement_max_age_seconds": 300,
               "tail_horizon_hours": 48,
               "terminal_value_mode": "AUTO",
               "terminal_value_euro_per_kwh": 0.0,
               "terminal_value_window_hours": 24,
               "penalties": {
                   "ev_soc_miss": 10
               }
           },
           "genetic0": {
               "horizon_hours": 24,
               "individuals": 400,
               "generations": 400,
               "seed": null,
               "penalties": {
                   "ev_soc_miss": 10
               }
           },
           "self_consumption_interpolator": "scipy"
       },
       "prediction": {
           "hours": 48,
           "historic_hours": 48
       },
       "pvforecast": {
           "provider": "PVForecastAkkudoktor",
           "akkudoktor": {
               "backend": "remote",
               "resolution_minutes": 15,
               "forecast_days": null,
               "past_days": null,
               "weather_models": [
                   "best_match"
               ],
               "transposition_model": "perez",
               "albedo": 0.25,
               "inverter_efficiency": 0.96,
               "temperature_coefficient": -0.36,
               "apply_iam": true,
               "shift_to_interval_start": true,
               "calibration_enabled": false,
               "calibration_days": 30,
               "calibration_reference_days": 30,
               "calibration_outage_filter_enabled": true,
               "calibration_outage_threshold": 0.55,
               "calibration_min_healthy_days": 3,
               "calibration_azimuth_bin_degrees": 45,
               "calibration_prior_kwh": 5.0,
               "calibration_min_factor": 0.5,
               "calibration_max_factor": 1.5
           },
           "pvforecastimport": {
               "import_file_path": null,
               "import_json": null
           },
           "vrm": {
               "token": "your-token",
               "site_id": 12345
           },
           "homeassistant": {
               "entity_id": "sensor.pv_forecast",
               "attribute": "forecast",
               "datetime_key": "datetime",
               "value_key": "watts",
               "value_unit": "W",
               "base_url": null,
               "token": null
           },
           "pvlib": {
               "max_ac_power_w": null
           },
           "pvnode": {
               "api_key": "",
               "site_id": null,
               "forecast_days": 2
           },
           "forecastsolar": {
               "api_key": null
           },
           "solcast": {
               "api_key": "",
               "site_id": ""
           },
           "planes": [
               {
                   "surface_tilt": 10.0,
                   "surface_azimuth": 180.0,
                   "userhorizon": [
                       10.0,
                       20.0,
                       30.0
                   ],
                   "peakpower": 5.0,
                   "pvtechchoice": "crystSi",
                   "mountingplace": "free",
                   "loss": 14.0,
                   "trackingtype": 0,
                   "optimal_surface_tilt": false,
                   "optimalangles": false,
                   "albedo": null,
                   "module_model": null,
                   "inverter_model": null,
                   "inverter_paco": 6000,
                   "modules_per_string": 20,
                   "strings_per_inverter": 2
               },
               {
                   "surface_tilt": 20.0,
                   "surface_azimuth": 90.0,
                   "userhorizon": [
                       5.0,
                       15.0,
                       25.0
                   ],
                   "peakpower": 3.5,
                   "pvtechchoice": "crystSi",
                   "mountingplace": "free",
                   "loss": 14.0,
                   "trackingtype": 1,
                   "optimal_surface_tilt": false,
                   "optimalangles": false,
                   "albedo": null,
                   "module_model": null,
                   "inverter_model": null,
                   "inverter_paco": 4000,
                   "modules_per_string": 20,
                   "strings_per_inverter": 2
               }
           ],
           "max_planes": 1
       },
       "server": {
           "host": "127.0.0.1",
           "port": 8503,
           "verbose": false,
           "startup_eosdash": true,
           "eosdash_host": "127.0.0.1",
           "eosdash_port": 8504,
           "eosdash_public_url": "https://energy.example.com/dashboard",
           "eosdash_supervise_interval_sec": 10,
           "run_as_user": null,
           "reload": true
       },
       "utils": {},
       "weather": {
           "provider": "WeatherImport",
           "weatherimport": {
               "import_file_path": null,
               "import_json": null
           }
       }
   }
```
<!-- pyml enable line-length -->
