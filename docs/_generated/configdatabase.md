## Configuration model for database settings

Attributes:
    provider: Optional provider identifier (e.g. "LMDB").
    max_records_in_memory: Maximum records kept in memory before auto-save.
    auto_save: Whether to auto-save when threshold exceeded.
    batch_size: Batch size for batch operations.

<!-- pyml disable line-length -->
:::{table} database
:widths: 10 20 10 5 5 30
:align: left

| Name | Environment Variable | Type | Read-Only | Default | Description |
| ---- | -------------------- | ---- | --------- | ------- | ----------- |
| autosave_interval_sec | `EOS_DATABASE__AUTOSAVE_INTERVAL_SEC` | `Optional[int]` | `rw` | `10` | Automatic saving interval [seconds].
Set to None to disable automatic saving. |
| batch_size | `EOS_DATABASE__BATCH_SIZE` | `int` | `rw` | `100` | Number of records to process in batch operations. |
| compaction_interval_sec | `EOS_DATABASE__COMPACTION_INTERVAL_SEC` | `Optional[int]` | `rw` | `3600` | Interval in between automatic tiered compaction runs [seconds].
Compaction downsamples old records to reduce storage while retaining coverage. Set to None to disable automatic compaction. |
| compression_level | `EOS_DATABASE__COMPRESSION_LEVEL` | `int` | `rw` | `9` | Compression level for database record data. |
| history | `EOS_DATABASE__HISTORY` | `HistorySettings` | `rw` | `required` | Long-term history in PostgreSQL / TimescaleDB (eos-plus fork). |
| initial_load_window_h | `EOS_DATABASE__INITIAL_LOAD_WINDOW_H` | `Optional[int]` | `rw` | `None` | Specifies the default duration of the initial load window when loading records from the database, in hours. If set to None, the full available range is loaded. The window is centered around the current time by default, unless a different center time is specified. Different database namespaces may define their own default windows. |
| keep_duration_h | `EOS_DATABASE__KEEP_DURATION_H` | `Optional[int]` | `rw` | `None` | Default maximum duration records shall be kept in database [hours, none].
None indicates forever. Database namespaces may have diverging definitions. |
| provider | `EOS_DATABASE__PROVIDER` | `Optional[str]` | `rw` | `None` | Database provider id of provider to be used. |
| providers | | `List[str]` | `ro` | `N/A` | Return available database provider ids. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "database": {
           "provider": "LMDB",
           "compression_level": 0,
           "initial_load_window_h": 48,
           "keep_duration_h": 48,
           "autosave_interval_sec": 5,
           "compaction_interval_sec": 3600,
           "batch_size": 100,
           "history": {
               "enabled": false,
               "dsn": null,
               "retention_days": 1095,
               "forecast_hours": 48,
               "min_store_interval_minutes": 14,
               "actuals": [],
               "accuracy": [],
               "lead_bands_h": [
                   1.0,
                   6.0,
                   24.0
               ],
               "accuracy_days": 14
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
       "database": {
           "provider": "LMDB",
           "compression_level": 0,
           "initial_load_window_h": 48,
           "keep_duration_h": 48,
           "autosave_interval_sec": 5,
           "compaction_interval_sec": 3600,
           "batch_size": 100,
           "history": {
               "enabled": false,
               "dsn": null,
               "retention_days": 1095,
               "forecast_hours": 48,
               "min_store_interval_minutes": 14,
               "actuals": [],
               "accuracy": [],
               "lead_bands_h": [
                   1.0,
                   6.0,
                   24.0
               ],
               "accuracy_days": 14
           },
           "providers": [
               "LMDB",
               "SQLite",
               "NoDB"
           ]
       }
   }
```
<!-- pyml enable line-length -->

### A forecast or plan series and the actual series it is judged against

<!-- pyml disable line-length -->
:::{table} database::history::accuracy::list
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| actual | `str` | `rw` | `required` | Series in eos_actual (hourly). |
| kind | `str` | `rw` | `energy` | energy: predicted slot values are summed per hour; level: predicted slot values are averaged per hour. |
| name | `str` | `rw` | `required` | Name of the pair in reports. |
| predicted | `str` | `rw` | `required` | Series in eos_forecast (prediction columns) or eos_plan (solution columns). |
| source | `str` | `rw` | `forecast` | Where the predicted series lives [forecast | plan]. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "database": {
           "history": {
               "accuracy": [
                   {
                       "name": "pv",
                       "predicted": "pvforecast_ac_energy_wh",
                       "source": "forecast",
                       "actual": "pv_wh",
                       "kind": "energy"
                   }
               ]
           }
       }
   }
```
<!-- pyml enable line-length -->

### How an hourly actual series is derived from EOS measurements

<!-- pyml disable line-length -->
:::{table} database::history::actuals::list
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| keys | `list[str]` | `rw` | `required` | Measurement keys. kind=meter: cumulative energy meters in kWh (summed); kind=mean: values averaged per hour (summed over keys). |
| kind | `str` | `rw` | `meter` | meter | mean | household |
| name | `str` | `rw` | `required` | Series name in eos_actual. |
| scale | `float` | `rw` | `1000.0` | Factor applied to the derived value (meter kWh → Wh: 1000). |
| subtract | `list[str]` | `rw` | `required` | Other actual series (by name, derived before this one) subtracted per hour, e.g. heat pump energy from the household meter to get the base load. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "database": {
           "history": {
               "actuals": [
                   {
                       "name": "pv_wh",
                       "keys": [
                           "sensor.energie_pv_erzeugung_gesamt"
                       ],
                       "kind": "meter",
                       "subtract": [
                           "heatpump_wh"
                       ],
                       "scale": 1000.0
                   }
               ]
           }
       }
   }
```
<!-- pyml enable line-length -->

### Long-term history in PostgreSQL / TimescaleDB (eos-plus fork)

<!-- pyml disable line-length -->
:::{table} database::history
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| accuracy | `list[akkudoktoreos.core.history.HistoryAccuracyPair]` | `rw` | `required` | Forecast/plan series judged against actuals. |
| accuracy_days | `int` | `rw` | `14` | Days evaluated for the published accuracy. |
| actuals | `list[akkudoktoreos.core.history.HistoryActualSource]` | `rw` | `required` | Hourly actual series derived from measurements. |
| dsn | `Optional[str]` | `rw` | `None` | libpq connection string, e.g. postgresql://eos:secret@eos-history:5432/eos. Prefer the environment variable EOS_DATABASE__HISTORY__DSN for the password. |
| enabled | `bool` | `rw` | `False` | Write runs, forecasts, plans and actuals. |
| forecast_hours | `int` | `rw` | `48` | Hours of every forecast and plan that are stored. |
| lead_bands_h | `list[float]` | `rw` | `required` | Lead time bands for the accuracy: for each band the newest prediction issued at least this many hours before the target hour is used. |
| min_store_interval_minutes | `int` | `rw` | `14` | A run is stored at most this often (EOS may optimize every few minutes; the lead time analysis needs one plan per slot length, not every run). |
| retention_days | `int` | `rw` | `1095` | Rows older than this are dropped. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "database": {
           "history": {
               "enabled": true,
               "dsn": "postgresql://eos@eos-history:5432/eos",
               "retention_days": 1095,
               "forecast_hours": 48,
               "min_store_interval_minutes": 14,
               "actuals": [],
               "accuracy": [],
               "lead_bands_h": [
                   1.0,
                   6.0,
                   24.0
               ],
               "accuracy_days": 14
           }
       }
   }
```
<!-- pyml enable line-length -->
