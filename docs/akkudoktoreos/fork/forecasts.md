% SPDX-License-Identifier: Apache-2.0
(fork-forecasts-page)=

# Forecasts: base load, PV, prices

## Base load from the own meter (`LoadMeasured`)

`load.provider: "LoadMeasured"` forecasts the load from the history of the own load meter
(`measurement.load_emr_keys`): median per weekday and hour (or working day / weekend) of the last weeks,
days with gaps left out. Until 14 healthy days exist it behaves like `LoadAkkudoktorAdjusted`.

Feed the **base load** (house minus the consumers EOS plans itself, e.g. heat pump, washer, heating rod)
when those devices are planned separately; otherwise they are counted twice.

## Base load with gradient boosting (`LoadML`)

`load.provider: "LoadML"` trains one LightGBM quantile model per quantile (`load.loadml.quantiles`,
default 10/50/90 %) on: hour, weekday, day type (working day / Saturday / Sunday or public holiday of
`state`), bridge days, season, outdoor temperature (`temperature_key`), away mode (`away_key`,
`away_return_key`), the load of the same hour a week before and the level of the last week.

- `subtract_power_keys`: power measurements removed from the load meter (empty if the meter already
  measures the base load).
- Used only if it beats the `LoadMeasured` profile on the last `holdout_days`; otherwise `LoadMeasured`
  stays in charge. Needs `min_days` (21) healthy days. Report: `<data>/rl29/load_ml.json`.
- `plan_quantile` > 0.5 plans with more load than expected (reserve). The quantile spread is used by
  the MILP scenarios.

## PV ensemble (`PVForecastEnsemble`)

`pvforecast.provider: "PVForecastEnsemble"` combines PVLib with another forecast (e.g. one delivered by
Home Assistant, `pvforecast.homeassistant`). Weights ∝ 1/MAE² of the last `ensemble.days` days against
the PV meter, then a calibration factor = quantile of the daily ratio measured/forecast (bounded
0.6–1.3; `scale_quantile` < 0.5 plans more cautiously). Report: `<data>/rl29/pv_ensemble.json`.

## Prices after the published day-ahead (`elecprice.ml`)

Day-ahead prices are known until midnight, from about 13:00 until midnight of the next day. The hours
after that used to come from an exponential smoothing (ETS) of the price history. With
`elecprice.ml.enabled: true` a LightGBM model forecasts them from

- calendar: time of day, weekday, day type, season, lead time after the last known price;
- price level: same time on the last published day and a week before, mean/min/max/spread of the last
  published day, mean of the last week;
- weather of the site at the target time and on the last published day (irradiance, wind, temperature,
  clouds) as a proxy for solar and wind generation.

Training samples mimic the real situation: for every past midnight the following `horizon_hours` (72) are
forecast from what was known then. The model is used only if it beats the provider's own forecast on the last
`holdout_days` (7); it is retrained every `refit_hours` (6). Report: `<data>/rl29/price_ml.json`.

| Provider | History | Notes |
| --- | --- | --- |
| `ElecPriceEnergyCharts`, `ElecPriceSMARD` | fetched for `history_days` (180) | raw market price |
| `ElecPriceAkkudoktor` | what the store holds | hourly |
| `ElecPriceTibber` | what the store holds | Tibber's price incl. fees; reference = Tibber's own forecast |

Backtest on real German day-ahead prices (15 minutes, site weather, 14 holdout days): mean absolute error
4.0–4.3 ct/kWh instead of 5.1 ct/kWh with ETS (−15 to −25 %).
