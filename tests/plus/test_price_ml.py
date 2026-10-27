"""Price forecast after the published day-ahead prices (eos-plus fork, elecpriceml.py)."""

from __future__ import annotations

import json
import math
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from akkudoktoreos.prediction.elecpriceabc import ElecPriceProvider
from akkudoktoreos.prediction.elecpriceml import (
    FEATURES,
    ElecPriceMLSettings,
    PriceSeries,
    feature_row,
    predict_prices,
    train_price_model,
)
from akkudoktoreos.prediction.priceabc import PricePredictionProviderBase

TZ = ZoneInfo("Europe/Berlin")
STEP = 3600


class _Ets:
    """The provider's ETS forecast without a provider instance."""

    _cap_outliers = PricePredictionProviderBase._cap_outliers
    _predict_ets = PricePredictionProviderBase._predict_ets
    _predict_median = PricePredictionProviderBase._predict_median
    _predict = PricePredictionProviderBase._predict


def ets(history: np.ndarray, count: int) -> np.ndarray:
    return _Ets()._predict(history, count, slots_per_hour=1)


def synthetic(days: int = 70, weather: bool = True, seed: int = 1):
    """Hourly prices driven by daytime shape, weekday, sun and wind (+ noise)."""
    rng = np.random.default_rng(seed)
    start = datetime(2026, 7, 20, tzinfo=TZ).timestamp()
    n = days * 24
    ts = start + np.arange(n) * STEP
    sun_day = rng.uniform(0.1, 1.0, days)
    wind_day = rng.uniform(1.0, 12.0, days)
    level = 0.00010 + np.cumsum(rng.normal(0, 0.000004, days))
    ghi = np.zeros(n)
    wind = np.zeros(n)
    price = np.zeros(n)
    for i, t in enumerate(ts):
        local = datetime.fromtimestamp(t, TZ)
        d = i // 24
        h = local.hour
        ghi[i] = (
            max(0.0, math.sin(math.pi * (h - 6) / 14)) * 800 * sun_day[d] if 6 <= h <= 20 else 0.0
        )
        wind[i] = wind_day[d] + rng.normal(0, 0.5)
        peaks = 0.00004 * (math.exp(-((h - 8) ** 2) / 4) + 1.4 * math.exp(-((h - 19) ** 2) / 4))
        weekend = -0.00002 if local.weekday() >= 5 else 0.0
        price[i] = (
            level[d]
            + peaks
            + weekend
            - 0.00011 * ghi[i] / 800
            - 0.000006 * wind[i]
            + rng.normal(0, 0.000004)
        )
    getters = {}
    if weather:
        getters = {
            "weather_ghi": lambda x: float(np.interp(x, ts, ghi)),
            "weather_wind_speed": lambda x: float(np.interp(x, ts, wind)),
        }
    return PriceSeries(start=float(start), step=STEP, price=price, weather=getters)


def test_feature_row_uses_only_known_prices():
    series = synthetic(days=30)
    cutoff = series.end - 36 * 3600
    target = cutoff + 30 * 3600
    row = dict(zip(FEATURES, feature_row(series, cutoff, target, TZ, "DE-BW")))
    assert row["lead_h"] == pytest.approx(30.0)
    # same time on the last known day: two days back (one day back would be unknown)
    assert row["p_last_day"] == pytest.approx(series.at(target - 2 * 86400))
    assert row["p_week"] == pytest.approx(series.at(target - 7 * 86400))
    known = series.price[: int((cutoff - series.start) / STEP) + 1]
    assert row["day_mean"] == pytest.approx(known[-24:].mean())
    assert row["ghi"] == pytest.approx(series.weather["weather_ghi"](target))


def test_model_beats_ets_when_weather_drives_prices():
    series = synthetic()
    settings = ElecPriceMLSettings(enabled=True, n_estimators=200)
    model = train_price_model(series, settings, TZ, ets, now=0.0)
    assert model is not None
    assert model.use, model.summary()
    assert model.holdout_mae_ml < 0.8 * model.holdout_mae_ets
    forecast = predict_prices(model, series, 48, TZ, "DE-BW")
    assert forecast.shape == (48,)
    assert np.all(np.isfinite(forecast))
    # Plausible range of the synthetic prices
    assert forecast.min() > series.price.min() - 0.0001
    assert forecast.max() < series.price.max() + 0.0001


def test_gate_keeps_ets_for_a_pure_weekly_pattern():
    """Without any driver ETS reproduces a strictly weekly series; the model is not used."""
    start = datetime(2026, 7, 20, tzinfo=TZ).timestamp()
    n = 70 * 24
    hours = np.arange(n)
    price = (
        0.0001
        + 0.00005 * np.sin(2 * np.pi * hours / 24)
        + 0.00002 * np.sin(2 * np.pi * hours / 168)
    )
    series = PriceSeries(start=float(start), step=STEP, price=price)
    model = train_price_model(series, ElecPriceMLSettings(enabled=True, n_estimators=100), TZ, ets)
    assert model is not None
    assert not model.use
    assert model.holdout_mae_ets < 1e-6


def test_too_little_history():
    series = synthetic(days=20)
    assert train_price_model(series, ElecPriceMLSettings(enabled=True), TZ, ets) is None


@pytest.mark.asyncio
async def test_provider_predict_tail_uses_model_and_falls_back(tmp_path):
    series = synthetic()
    index = pd.DatetimeIndex(
        [
            pd.Timestamp(series.start + i * STEP, unit="s", tz="UTC")
            for i in range(len(series.price))
        ]
    )
    raw = pd.Series(series.price, index=index)

    async def key_to_array(key, start_datetime, end_datetime, interval, fill_method=None):
        s = raw[
            (raw.index >= pd.Timestamp(start_datetime)) & (raw.index < pd.Timestamp(end_datetime))
        ]
        return s.to_numpy()

    async def key_to_raw_series(key, start_datetime=None, end_datetime=None, dropna=True):
        return raw[
            (raw.index >= pd.Timestamp(start_datetime)) & (raw.index < pd.Timestamp(end_datetime))
        ]

    weather_ts = np.arange(series.start, series.end + 80 * 3600, STEP)

    async def weather_series(key, start_datetime=None, end_datetime=None, dropna=True):
        getter = series.weather.get(key)
        if getter is None:
            raise KeyError(key)
        idx = pd.DatetimeIndex([pd.Timestamp(t, unit="s", tz="UTC") for t in weather_ts])
        return pd.Series([getter(t) for t in weather_ts], index=idx)

    settings = ElecPriceMLSettings(enabled=True, n_estimators=150)
    fake = SimpleNamespace(
        config=SimpleNamespace(
            elecprice=SimpleNamespace(ml=settings),
            general=SimpleNamespace(timezone="Europe/Berlin", data_folder_path=str(tmp_path)),
        ),
        key_to_array=key_to_array,
        key_to_raw_series=key_to_raw_series,
        prediction=SimpleNamespace(key_to_raw_series=weather_series),
        _raw_key="elecprice_marketprice_raw_wh",
        _price_model=None,
        _predict=lambda values, n, slots_per_hour=1: ets(values, n),
    )
    fake._ml_settings = lambda: ElecPriceProvider._ml_settings(fake)
    fake._market_country = lambda: "DE"  # bidding zone DE-LU
    fake._ml_holiday_region = lambda s: ElecPriceProvider._ml_holiday_region(fake, s)
    fake._weather_getters = lambda s, e: ElecPriceProvider._weather_getters(fake, s, e)
    fake._write_price_report = lambda *a: ElecPriceProvider._write_price_report(fake, *a)
    last = pd.Timestamp(series.end, unit="s", tz="UTC")
    out = await ElecPriceProvider._predict_tail(fake, series.price[:-1], 40, 1, STEP, last)
    assert len(out) == 40
    assert fake._price_model is not None and fake._price_model.use
    report = json.loads((tmp_path / "plus" / "price_ml.json").read_text())
    assert report["used"] is True and report["weather_keys"] == 2
    expected = predict_prices(fake._price_model, series, 40, TZ, "DE")
    assert np.allclose(out, expected)

    # Disabled: plain ETS on the history passed in.
    settings.enabled = False
    fallback = await ElecPriceProvider._predict_tail(fake, series.price[:-1], 40, 1, STEP, last)
    assert np.allclose(fallback, ets(series.price[:-1], 40))


@pytest.mark.asyncio
async def test_predict_tail_uses_the_provider_reference_when_disabled():
    """Providers with their own forecast (Tibber) keep it when the model is off."""
    fake = SimpleNamespace(
        config=SimpleNamespace(elecprice=SimpleNamespace(ml=ElecPriceMLSettings(enabled=False)))
    )
    fake._ml_settings = lambda: ElecPriceProvider._ml_settings(fake)
    out = await ElecPriceProvider._predict_tail(
        fake,
        np.ones(10),
        5,
        4,
        900,
        pd.Timestamp("2026-10-27T00:00:00Z"),
        key="elecprice_marketprice_wh",
        reference=lambda values, n: np.full(n, 7.0),
    )
    assert list(out) == [7.0] * 5


def test_holiday_region_of_the_price_model():
    """Without a holiday_region the country of the market is used."""
    fake = SimpleNamespace(_market_country=lambda: "AT")
    assert ElecPriceProvider._ml_holiday_region(fake, ElecPriceMLSettings()) == "AT"
    explicit = ElecPriceMLSettings(holiday_region="DE-BY")
    assert ElecPriceProvider._ml_holiday_region(fake, explicit) == "DE-BY"
    fake = SimpleNamespace(_market_country=lambda: None)  # e.g. Tibber
    assert ElecPriceProvider._ml_holiday_region(fake, ElecPriceMLSettings()) is None
    fake = SimpleNamespace(_market_country=lambda: "XX")  # unknown country: no holidays
    assert ElecPriceProvider._ml_holiday_region(fake, ElecPriceMLSettings()) is None


@pytest.mark.parametrize(
    ("zone", "country"),
    [
        ("DE-LU", "DE"),
        ("DE-AT-LU", "DE"),
        ("AT", "AT"),
        ("DK1", "DK"),
        ("IT-NORTH", "IT"),
        ("NO2", "NO"),
    ],
)
def test_energycharts_market_country(zone, country):
    from akkudoktoreos.prediction.elecpriceenergycharts import ElecPriceEnergyCharts

    fake = SimpleNamespace(_bidding_zone=lambda: zone)
    assert ElecPriceEnergyCharts._market_country(fake) == country
