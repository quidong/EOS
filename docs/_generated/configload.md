## Load Prediction Configuration

<!-- pyml disable line-length -->
:::{table} load
:widths: 10 20 10 5 5 30
:align: left

| Name | Environment Variable | Type | Read-Only | Default | Description |
| ---- | -------------------- | ---- | --------- | ------- | ----------- |
| loadakkudoktor | `EOS_LOAD__LOADAKKUDOKTOR` | `LoadAkkudoktorCommonSettings` | `rw` | `required` | LoadAkkudoktor provider settings. |
| loadimport | `EOS_LOAD__LOADIMPORT` | `LoadImportCommonSettings` | `rw` | `required` | LoadImport provider settings. |
| loadmeasured | `EOS_LOAD__LOADMEASURED` | `LoadMeasuredCommonSettings` | `rw` | `required` | LoadMeasured provider settings (eos-plus fork: own measured history). |
| loadml | `EOS_LOAD__LOADML` | `LoadMLCommonSettings` | `rw` | `required` | LoadML provider settings (eos-plus fork: gradient boosting base load). |
| provider | `EOS_LOAD__PROVIDER` | `Optional[str]` | `rw` | `None` | Load provider id of provider to be used. |
| providers | | `list[str]` | `ro` | `N/A` | Available load provider ids. |
| vrm | `EOS_LOAD__VRM` | `LoadVrmCommonSettings` | `rw` | `required` | Victron Remote Management (VRM) provider settings. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "load": {
           "provider": "LoadAkkudoktor",
           "loadakkudoktor": {
               "loadakkudoktor_year_energy_kwh": null
           },
           "loadmeasured": {
               "weeks": 6,
               "min_days": 14,
               "min_days_per_type": 3,
               "quantile": 0.5,
               "gap_threshold": 0.3
           },
           "loadml": {
               "history_weeks": 52,
               "min_days": 21,
               "holdout_days": 7,
               "quantiles": [
                   0.1,
                   0.5,
                   0.9
               ],
               "plan_quantile": 0.5,
               "subtract_power_keys": [],
               "subtract_max_hold_hours": 24.0,
               "temperature_key": null,
               "away_key": null,
               "away_return_key": null,
               "holiday_region": null,
               "state": null,
               "refit_hours": 24.0,
               "gap_threshold": 0.3,
               "num_leaves": 15,
               "n_estimators": 300,
               "learning_rate": 0.05,
               "min_data_in_leaf": 20
           },
           "loadimport": {
               "import_file_path": null,
               "import_json": null
           },
           "vrm": {
               "token": "your-token",
               "site_id": 12345
           }
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
       "load": {
           "provider": "LoadAkkudoktor",
           "loadakkudoktor": {
               "loadakkudoktor_year_energy_kwh": null
           },
           "loadmeasured": {
               "weeks": 6,
               "min_days": 14,
               "min_days_per_type": 3,
               "quantile": 0.5,
               "gap_threshold": 0.3
           },
           "loadml": {
               "history_weeks": 52,
               "min_days": 21,
               "holdout_days": 7,
               "quantiles": [
                   0.1,
                   0.5,
                   0.9
               ],
               "plan_quantile": 0.5,
               "subtract_power_keys": [],
               "subtract_max_hold_hours": 24.0,
               "temperature_key": null,
               "away_key": null,
               "away_return_key": null,
               "holiday_region": null,
               "state": null,
               "refit_hours": 24.0,
               "gap_threshold": 0.3,
               "num_leaves": 15,
               "n_estimators": 300,
               "learning_rate": 0.05,
               "min_data_in_leaf": 20
           },
           "loadimport": {
               "import_file_path": null,
               "import_json": null
           },
           "vrm": {
               "token": "your-token",
               "site_id": 12345
           },
           "providers": [
               "LoadAkkudoktor",
               "LoadAkkudoktorAdjusted",
               "LoadImport",
               "LoadVrm",
               "LoadMeasured",
               "LoadML"
           ]
       }
   }
```
<!-- pyml enable line-length -->

### Common settings for load forecast VRM API

<!-- pyml disable line-length -->
:::{table} load::vrm
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| site_id | `int` | `rw` | `12345` | VRM-Installation-ID |
| token | `str` | `rw` | `your-token` | Access token for connecting to the Victron Remote Management (VRM) API |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "load": {
           "vrm": {
               "token": "your-token",
               "site_id": 12345
           }
       }
   }
```
<!-- pyml enable line-length -->

### Settings of the gradient boosting base load provider (eos-plus fork)

<!-- pyml disable line-length -->
:::{table} load::loadml
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| away_key | `Optional[str]` | `rw` | `None` | Measurement 1 = away / holiday mode. |
| away_return_key | `Optional[str]` | `rw` | `None` | Measurement with the return time (Unix seconds) of the away mode. |
| gap_threshold | `float` | `rw` | `0.3` | Days below this share of the median daily base load are left out. |
| history_weeks | `int` | `rw` | `52` | Training window [weeks]. |
| holdout_days | `int` | `rw` | `7` | The model must beat the LoadMeasured profile on these last days (trained without them) to be used. |
| holiday_region | `Optional[str]` | `rw` | `None` | Public holidays as ISO 3166 code: country ('NL') or country-subdivision ('DE-BW', 'AT-9', 'CH-ZH'). Empty: no public holidays, weekends only. |
| learning_rate | `float` | `rw` | `0.05` | LightGBM learning rate. |
| min_data_in_leaf | `int` | `rw` | `20` | LightGBM min_data_in_leaf. |
| min_days | `int` | `rw` | `21` | Healthy days needed before the model is used. |
| n_estimators | `int` | `rw` | `300` | LightGBM trees. |
| num_leaves | `int` | `rw` | `15` | LightGBM num_leaves. |
| plan_quantile | `float` | `rw` | `0.5` | Quantile EOS plans with (must be in `quantiles`). Above 0.5 plans with more load than expected, i.e. keeps reserve for uncertain hours. |
| quantiles | `list[float]` | `rw` | `required` | Quantiles that are modelled. |
| refit_hours | `float` | `rw` | `24.0` | Models are retrained after this many hours. |
| state | `Optional[str]` | `rw` | `None` | Deprecated: German federal state, use holiday_region, e.g. 'DE-BW' for 'BW'. |
| subtract_max_hold_hours | `float` | `rw` | `24.0` | A power value is held until the next sample, at most this long; hours without a held value are not used for training. |
| subtract_power_keys | `list[str]` | `rw` | `required` | Power measurements [W] of consumers EOS plans itself; their energy is removed from the load meter to get the base load. |
| temperature_key | `Optional[str]` | `rw` | `None` | Measured outdoor temperature for the history (the forecast uses `weather_temp_air`). None: feature from the weather prediction store only. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "load": {
           "loadml": {
               "history_weeks": 52,
               "min_days": 21,
               "holdout_days": 7,
               "quantiles": [
                   0.1,
                   0.5,
                   0.9
               ],
               "plan_quantile": 0.5,
               "subtract_power_keys": [
                   "wbb12-electric-power",
                   "waschmaschine-power"
               ],
               "subtract_max_hold_hours": 24.0,
               "temperature_key": "wbb12-outdoor-temperature",
               "away_key": "wbb12-away",
               "away_return_key": "wbb12-return-ts",
               "holiday_region": "DE-BW",
               "state": null,
               "refit_hours": 24.0,
               "gap_threshold": 0.3,
               "num_leaves": 15,
               "n_estimators": 300,
               "learning_rate": 0.05,
               "min_data_in_leaf": 20
           }
       }
   }
```
<!-- pyml enable line-length -->

### Settings of the measured-history load provider (eos-plus fork)

<!-- pyml disable line-length -->
:::{table} load::loadmeasured
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| gap_threshold | `float` | `rw` | `0.3` | A day with less than this share of the median daily energy is treated as a measurement gap and left out. |
| min_days | `int` | `rw` | `14` | Healthy days needed before the own history replaces the adjusted standard profile. |
| min_days_per_type | `int` | `rw` | `3` | Each weekday needs this many healthy days for a per-weekday profile; otherwise working days / weekend are pooled. |
| quantile | `float` | `rw` | `0.5` | Statistic per hour: 0.5 = median; higher is more conservative. |
| weeks | `int` | `rw` | `6` | History window [weeks]. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "load": {
           "loadmeasured": {
               "weeks": 6,
               "min_days": 14,
               "min_days_per_type": 3,
               "quantile": 0.5,
               "gap_threshold": 0.3
           }
       }
   }
```
<!-- pyml enable line-length -->

### Common settings for load data import from file or JSON string

<!-- pyml disable line-length -->
:::{table} load::loadimport
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| import_file_path | `Union[str, pathlib.Path, NoneType]` | `rw` | `None` | Path to the file to import load data from. |
| import_json | `Optional[str]` | `rw` | `None` | JSON string, dictionary of load forecast value lists. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "load": {
           "loadimport": {
               "import_file_path": null,
               "import_json": "{\"loadforecast_power_w\": [676.71, 876.19, 527.13]}"
           }
       }
   }
```
<!-- pyml enable line-length -->

### Common settings for load data import from file

<!-- pyml disable line-length -->
:::{table} load::loadakkudoktor
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| loadakkudoktor_year_energy_kwh | `Optional[float]` | `rw` | `None` | Yearly energy consumption (kWh). |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "load": {
           "loadakkudoktor": {
               "loadakkudoktor_year_energy_kwh": 40421.0
           }
       }
   }
```
<!-- pyml enable line-length -->
