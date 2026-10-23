## Load Prediction Configuration

<!-- pyml disable line-length -->
:::{table} load
:widths: 10 20 10 5 5 30
:align: left

| Name | Environment Variable | Type | Read-Only | Default | Description |
| ---- | -------------------- | ---- | --------- | ------- | ----------- |
| loadakkudoktor | `EOS_LOAD__LOADAKKUDOKTOR` | `LoadAkkudoktorCommonSettings` | `rw` | `required` | LoadAkkudoktor provider settings. |
| loadimport | `EOS_LOAD__LOADIMPORT` | `LoadImportCommonSettings` | `rw` | `required` | LoadImport provider settings. |
| loadmeasured | `EOS_LOAD__LOADMEASURED` | `LoadMeasuredCommonSettings` | `rw` | `required` | LoadMeasured provider settings (RL29 fork: own measured history). |
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
               "LoadMeasured"
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

### Settings of the measured-history load provider (RL29 fork)

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
