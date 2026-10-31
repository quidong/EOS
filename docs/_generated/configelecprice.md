## Electricity Price Prediction Configuration

<!-- pyml disable line-length -->
:::{table} elecprice
:widths: 10 20 10 5 5 30
:align: left

| Name | Environment Variable | Type | Read-Only | Default | Description |
| ---- | -------------------- | ---- | --------- | ------- | ----------- |
| akkudoktor | `EOS_ELECPRICE__AKKUDOKTOR` | `ElecPriceAkkudoktorCommonSettings` | `rw` | `required` | Akkudoktor electricity price provider settings. |
| elecpricefixed | `EOS_ELECPRICE__ELECPRICEFIXED` | `ElecPriceFixedCommonSettings` | `rw` | `required` | Fixed electricity price provider settings. |
| elecpriceimport | `EOS_ELECPRICE__ELECPRICEIMPORT` | `ElecPriceImportCommonSettings` | `rw` | `required` | Electricity price import provider settings. |
| energycharts | `EOS_ELECPRICE__ENERGYCHARTS` | `ElecPriceEnergyChartsCommonSettings` | `rw` | `required` | Energy Charts provider settings. |
| ml | `EOS_ELECPRICE__ML` | `ElecPriceMLSettings` | `rw` | `required` | RL29 fork: LightGBM forecast of the hours after the published day-ahead prices (Energy-Charts provider). |
| provider | `EOS_ELECPRICE__PROVIDER` | `Optional[str]` | `rw` | `None` | Electricity price provider id of provider to be used. |
| providers | | `list[str]` | `ro` | `N/A` | Available electricity price provider ids. |
| smard | `EOS_ELECPRICE__SMARD` | `ElecPriceSMARDCommonSettings` | `rw` | `required` | SMARD electricity price provider settings. |
| tibber | `EOS_ELECPRICE__TIBBER` | `ElecPriceTibberCommonSettings` | `rw` | `required` | Tibber electricity price provider settings. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
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
           "ml": {
               "enabled": false,
               "history_days": 180,
               "min_days": 28,
               "holdout_days": 7,
               "horizon_hours": 72,
               "refit_hours": 6.0,
               "state": "BW",
               "num_leaves": 15,
               "n_estimators": 300,
               "learning_rate": 0.05,
               "min_data_in_leaf": 40
           },
           "tibber": {
               "access_token": null,
               "home_id": null
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
           "ml": {
               "enabled": false,
               "history_days": 180,
               "min_days": 28,
               "holdout_days": 7,
               "horizon_hours": 72,
               "refit_hours": 6.0,
               "state": "BW",
               "num_leaves": 15,
               "n_estimators": 300,
               "learning_rate": 0.05,
               "min_data_in_leaf": 40
           },
           "tibber": {
               "access_token": null,
               "home_id": null
           },
           "providers": [
               "ElecPriceAkkudoktor",
               "ElecPriceEnergyCharts",
               "ElecPriceFixed",
               "ElecPriceImport",
               "ElecPriceSMARD",
               "ElecPriceTibber"
           ]
       }
   }
```
<!-- pyml enable line-length -->

### Common settings for the Tibber electricity price provider

<!-- pyml disable line-length -->
:::{table} elecprice::tibber
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| access_token | `Optional[str]` | `rw` | `None` | Tibber API access token. |
| home_id | `Optional[str]` | `rw` | `None` | Optional Tibber home id. If omitted, the first home with a subscription is used. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "elecprice": {
           "tibber": {
               "access_token": "tibber_pat_...",
               "home_id": "00000000-0000-0000-0000-000000000000"
           }
       }
   }
```
<!-- pyml enable line-length -->

### Common settings for the direct SMARD electricity-price provider

<!-- pyml disable line-length -->
:::{table} elecprice::smard
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| filter_id | `int` | `rw` | `4169` | SMARD filter id for the German/Luxembourg day-ahead price. |
| region | `str` | `rw` | `DE` | SMARD market region used in the chart-data endpoint. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "elecprice": {
           "smard": {
               "filter_id": 4169,
               "region": "DE"
           }
       }
   }
```
<!-- pyml enable line-length -->

### Machine-learning forecast of the prices after the published day-ahead (RL29 fork)

<!-- pyml disable line-length -->
:::{table} elecprice::ml
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| enabled | `bool` | `rw` | `False` | Forecast the hours after the last published price with LightGBM (used only when it beats the ETS forecast on the holdout days). |
| history_days | `int` | `rw` | `180` | Price history fetched and used for training [days]. |
| holdout_days | `int` | `rw` | `7` | The model must beat ETS on these last days to be used. |
| horizon_hours | `int` | `rw` | `72` | Longest lead time after the last known price that is trained [h]. |
| learning_rate | `float` | `rw` | `0.05` | None |
| min_data_in_leaf | `int` | `rw` | `40` | None |
| min_days | `int` | `rw` | `28` | Days of history needed before training. |
| n_estimators | `int` | `rw` | `300` | None |
| num_leaves | `int` | `rw` | `15` | None |
| refit_hours | `float` | `rw` | `6.0` | Retrain after this many hours. |
| state | `str` | `rw` | `BW` | Federal state for public holidays. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "elecprice": {
           "ml": {
               "enabled": true,
               "history_days": 180,
               "min_days": 28,
               "holdout_days": 7,
               "horizon_hours": 72,
               "refit_hours": 6.0,
               "state": "BW",
               "num_leaves": 15,
               "n_estimators": 300,
               "learning_rate": 0.05,
               "min_data_in_leaf": 40
           }
       }
   }
```
<!-- pyml enable line-length -->

### Common settings for Energy Charts electricity price provider

<!-- pyml disable line-length -->
:::{table} elecprice::energycharts
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| bidding_zone | `<enum 'EnergyChartsBiddingZones'>` | `rw` | `DE-LU` | Bidding Zone: 'AT', 'BE', 'CH', 'CZ', 'DE-LU', 'DE-AT-LU', 'DK1', 'DK2', 'FR', 'HU', 'IT-NORTH', 'NL', 'NO2', 'PL', 'SE4' or 'SI' |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "elecprice": {
           "energycharts": {
               "bidding_zone": "AT"
           }
       }
   }
```
<!-- pyml enable line-length -->

### Common settings for elecprice data import from file or JSON String

<!-- pyml disable line-length -->
:::{table} elecprice::elecpriceimport
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| import_file_path | `Union[str, pathlib.Path, NoneType]` | `rw` | `None` | Path to the file to import elecprice data from. |
| import_json | `Optional[str]` | `rw` | `None` | JSON string, dictionary of electricity price forecast value lists. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "elecprice": {
           "elecpriceimport": {
               "import_file_path": null,
               "import_json": "{\"elecprice_marketprice_wh\": [0.0003384, 0.0003318, 0.0003284]}"
           }
       }
   }
```
<!-- pyml enable line-length -->

### Common configuration settings for fixed electricity pricing

This model defines a fixed electricity price schedule using a sequence
of time windows. Each window specifies a time interval and the electricity
price applicable during that interval.

<!-- pyml disable line-length -->
:::{table} elecprice::elecpricefixed
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
| elecprice_marketprice_amt_kwh | `ValueTimeWindowSequence` | `rw` | `required` | Sequence of time windows defining the fixed price schedule. If not provided, no fixed pricing is applied. |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "elecprice": {
           "elecpricefixed": {
               "elecprice_marketprice_amt_kwh": {
                   "windows": [
                       {
                           "start_time": "00:00:00.000000",
                           "duration": "8 hours",
                           "end_time": null,
                           "day_of_week": null,
                           "date": null,
                           "month": null,
                           "locale": null,
                           "value": 0.288
                       },
                       {
                           "start_time": "08:00:00.000000",
                           "duration": "16 hours",
                           "end_time": null,
                           "day_of_week": null,
                           "date": null,
                           "month": null,
                           "locale": null,
                           "value": 0.34
                       }
                   ]
               }
           }
       }
   }
```
<!-- pyml enable line-length -->

### Common configuration settings for Akkodoktor electricity pricing

<!-- pyml disable line-length -->
:::{table} elecprice::akkudoktor
:widths: 10 10 5 5 30
:align: left

| Name | Type | Read-Only | Default | Description |
| ---- | ---- | --------- | ------- | ----------- |
:::
<!-- pyml enable line-length -->

<!-- pyml disable no-emphasis-as-heading -->
**Example Input/Output**
<!-- pyml enable no-emphasis-as-heading -->

<!-- pyml disable line-length -->
```json
   {
       "elecprice": {
           "akkudoktor": {}
       }
   }
```
<!-- pyml enable line-length -->
