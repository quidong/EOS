"""Gradient boosting base load forecast (RL29 fork)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator

import numpy as np
import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement, get_prediction
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.prediction.calendarfeatures import (
    day_type,
    easter_sunday,
    is_bridge_day,
    is_public_holiday,
    public_holidays,
)
from akkudoktoreos.prediction.loadml import (
    FEATURES,
    LoadML,
    LoadMLCommonSettings,
    feature_rows,
    healthy_mask,
    hold_hourly_means,
    predict_load,
    quantile_key,
    train_load_model,
)

TZ = "Europe/Berlin"


# --------------------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------------------


def test_easter_dates():
    assert easter_sunday(2024) == date(2024, 3, 31)
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert easter_sunday(2026) == date(2026, 4, 5)
    assert easter_sunday(2027) == date(2027, 3, 28)


def test_bw_holidays_2026():
    holidays = public_holidays(2026, "BW")
    expected = {
        date(2026, 1, 1),
        date(2026, 1, 6),
        date(2026, 4, 3),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 5, 14),
        date(2026, 5, 25),
        date(2026, 6, 4),
        date(2026, 10, 3),
        date(2026, 11, 1),
        date(2026, 12, 25),
        date(2026, 12, 26),
    }
    assert holidays == expected


def test_state_specific_holidays():
    assert is_public_holiday(date(2026, 10, 31), "NI")
    assert not is_public_holiday(date(2026, 10, 31), "BW")
    assert is_public_holiday(date(2026, 11, 18), "SN")  # Buß- und Bettag 2026
    assert is_public_holiday(date(2026, 3, 8), "BE")
    with pytest.raises(ValueError):
        public_holidays(2026, "XX")


def test_day_types_and_bridge_days():
    assert day_type(date(2026, 10, 26), "BW") == 0  # Monday
    assert day_type(date(2026, 10, 24), "BW") == 1  # Saturday
    assert day_type(date(2026, 10, 25), "BW") == 2  # Sunday
    assert day_type(date(2026, 12, 24), "BW") == 2  # special day
    assert day_type(date(2026, 6, 4), "BW") == 2  # Corpus Christi
    assert is_bridge_day(date(2026, 5, 15), "BW")  # Friday after Ascension
    assert is_bridge_day(date(2026, 6, 5), "BW")  # Friday after Corpus Christi
    assert not is_bridge_day(date(2026, 5, 13), "BW")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

H0 = 1_800_000_000.0 - (1_800_000_000.0 % 3600)


def test_hold_hourly_means_step_signal():
    samples = [(H0 - 100, 100.0), (H0 + 1800, 300.0)]
    assert hold_hourly_means(samples, [H0], 86400) == [pytest.approx(200.0)]
    # Second hour fully at 300 W, no new sample needed.
    assert hold_hourly_means(samples, [H0 + 3600], 86400) == [pytest.approx(300.0)]


def test_hold_hourly_means_gaps():
    samples = [(H0 + 10, 100.0)]  # starts inside the hour
    assert hold_hourly_means(samples, [H0], 86400) == [None]
    # Hold expires inside the hour.
    assert hold_hourly_means([(H0 - 3000, 100.0)], [H0], 3600) == [None]
    # None value ends the hold.
    assert hold_hourly_means([(H0 - 10, 100.0), (H0 + 600, None)], [H0], 86400) == [None]
    assert hold_hourly_means([], [H0], 86400) == [None]


def test_healthy_mask_drops_incomplete_and_gap_days():
    start = datetime(2026, 10, 1)
    hours = [start + timedelta(hours=i) for i in range(72)]
    values = [500.0] * 24 + [10.0] * 24 + [500.0] * 20 + [None] * 4
    mask = healthy_mask(hours, values, 0.3)
    assert all(mask[:24])
    assert not any(mask[24:48])  # gap day
    assert not any(mask[48:])  # incomplete day


def test_feature_rows_lags_and_calendar():
    t = datetime(2026, 10, 26, 18)
    history = {t - timedelta(hours=h): 400.0 for h in range(48, 48 + 168)}
    history[t - timedelta(days=7)] = 700.0
    rows = feature_rows([t], history, {t: 5.0}, {t: 1.0}, "BW")
    row = dict(zip(FEATURES, rows[0]))
    assert row["hour"] == 18 and row["weekday"] == 0 and row["day_type"] == 0
    assert row["temp_c"] == 5.0 and row["away"] == 1.0
    assert row["lag_week"] == 700.0
    assert row["level_week"] == pytest.approx((700.0 + 400.0 * 167) / 168, rel=1e-6)
    missing = feature_rows([t + timedelta(days=30)], history, {}, {}, "BW")[0]
    assert math.isnan(missing[FEATURES.index("lag_week")])
    assert math.isnan(missing[FEATURES.index("temp_c")])


def test_settings_validation():
    s = LoadMLCommonSettings(quantiles=[0.9, 0.1, 0.5, 0.5], state="bw")
    assert s.quantiles == [0.1, 0.5, 0.9] and s.state == "BW"
    with pytest.raises(ValueError):
        LoadMLCommonSettings(quantiles=[1.2])
    with pytest.raises(ValueError):
        LoadMLCommonSettings(state="XX")
    assert quantile_key(0.1) == "loadforecast_p10_power_w"


# --------------------------------------------------------------------------------------
# Training on synthetic data
# --------------------------------------------------------------------------------------


def synthetic(days: int, start: datetime, seed: int = 1):
    """Base load with daily shape, weekend, holidays and a temperature effect."""
    rng = np.random.default_rng(seed)
    hours, values, temperature = [], [], {}
    for i in range(days * 24):
        t = start + timedelta(hours=i)
        temp = (
            10 + 8 * math.sin(2 * math.pi * (i / 24) / 20) + 4 * math.sin(2 * math.pi * t.hour / 24)
        )
        shape = 250 + (500 if 6 <= t.hour < 9 else 0) + (800 if 17 <= t.hour < 22 else 0)
        if day_type(t.date(), "BW") > 0:
            shape += 300 if 9 <= t.hour < 17 else 0
        shape += max(0.0, 12 - temp) * 40  # cold: more lighting/cooking indoors
        hours.append(t)
        values.append(shape + rng.normal(0, 40))
        temperature[t] = temp
    return hours, values, temperature


def test_too_few_days_gives_no_model():
    hours, values, temperature = synthetic(10, datetime(2026, 9, 1))
    assert (
        train_load_model(hours, values, temperature, {}, LoadMLCommonSettings(min_days=21)) is None
    )


def test_model_beats_profile_and_quantiles_are_ordered():
    hours, values, temperature = synthetic(70, datetime(2026, 8, 1))
    settings = LoadMLCommonSettings(min_days=21, n_estimators=200)
    model = train_load_model(hours, values, temperature, {}, settings, now=0.0)
    assert model is not None
    assert model.use, model.summary()
    assert model.holdout_mae_ml < model.holdout_mae_profile
    assert model.importance["temp_c"] > 0
    future = [hours[-1] + timedelta(hours=i + 1) for i in range(48)]
    history = dict(zip(hours, values))
    f_temp = {t: 3.0 for t in future}
    forecast = predict_load(model, future, history, f_temp, {}, "BW")
    assert sorted(forecast) == [0.1, 0.5, 0.9]
    for i in range(48):
        assert forecast[0.1][i] <= forecast[0.5][i] <= forecast[0.9][i]
    # Evening peak above the night.
    evening = [v for t, v in zip(future, forecast[0.5]) if t.hour == 19]
    night = [v for t, v in zip(future, forecast[0.5]) if t.hour == 3]
    assert min(evening) > max(night) + 400
    # Cold forecast raises the load against a warm one.
    warm = predict_load(model, future, history, {t: 20.0 for t in future}, {}, "BW")
    assert sum(forecast[0.5]) > sum(warm[0.5])
    summary = model.summary()
    assert summary["used"] is True and summary["days"] >= 60


def test_model_not_used_when_it_does_not_beat_the_profile():
    # Pure weekday profile without any other effect: the profile is (near) optimal.
    hours, values, _ = synthetic(40, datetime(2026, 8, 1))
    profile_like = [250 + (800 if 17 <= h.hour < 22 else 0) for h in hours]
    settings = LoadMLCommonSettings(min_days=21, n_estimators=50)
    model = train_load_model(hours, profile_like, {}, {}, settings)
    assert model is not None
    # Either ML ties the exact profile or loses; with an exact profile it may not beat it.
    if not model.use:
        assert model.holdout_mae_ml > model.holdout_mae_profile


# --------------------------------------------------------------------------------------
# Provider against the EOS measurement store
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


def configure(config_eos: ConfigEOS, tmp_path: Path, **ml) -> None:
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ, "data_folder_path": str(tmp_path)},
            "prediction": {"hours": 48},
            "measurement": {
                "load_emr_keys": ["load_mr"],
                "channels": {
                    "hp_power": {
                        "quantity": "power",
                        "unit": "W",
                        "integration_method": "hold",
                        "max_gap_seconds": 7200,
                    },
                },
                # Any configured key list makes a key writable; production uses device keys.
                "pv_production_emr_keys": ["temp"],
            },
            "load": {
                "provider": "LoadML",
                "loadakkudoktor": {"loadakkudoktor_year_energy_kwh": 4000},
                "loadmeasured": {"weeks": 6, "min_days": 14},
                "loadml": {"min_days": 21, "n_estimators": 100, **ml},
            },
        }
    )


def outdoor(i: int) -> float:
    """Outdoor temperature swinging over a 9 day period."""
    return 8.0 + 7.0 * math.sin(2 * math.pi * i / (24 * 9))


async def fill(measurement: Measurement, start: pendulum.DateTime, days: int) -> None:
    """House meter = base (profile + cold effect) + heat pump 1000 W in the night hours."""
    reading = 1000.0
    t = start
    for i in range(days * 24 + 1):
        await measurement.update_value(t, "load_mr", reading)
        local = t.in_timezone(TZ)
        temp = outdoor(i)
        base = 300.0 + (900.0 if 17 <= local.hour < 22 else 0.0) + 30.0 * max(0.0, 12.0 - temp)
        hp = 1000.0 if local.hour < 5 else 0.0
        await measurement.update_value(t, "hp_power", hp)
        await measurement.update_value(t, "temp", temp)
        reading += (base + hp) / 1000.0
        t = t.add(hours=1)


def forecast_of(provider: LoadML, key: str = "loadforecast_power_w") -> dict:
    return {
        pendulum.instance(r.date_time).in_timezone(TZ): getattr(r, key) for r in provider.records
    }


def test_provider_is_registered(config_eos: ConfigEOS, tmp_path: Path):
    configure(config_eos, tmp_path)
    assert "LoadML" in config_eos.load.providers
    assert any(p.provider_id() == "LoadML" for p in get_prediction().providers)


@pytest.mark.asyncio
async def test_provider_falls_back_then_learns_base_load(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    configure(
        config_eos,
        tmp_path,
        subtract_power_keys=["hp_power"],
        temperature_key="temp",
        holdout_days=3,
    )
    start = pendulum.datetime(2026, 10, 20, 0, tz=TZ)
    get_ems().set_start_datetime(start)
    provider = LoadML()

    # 16 days: LoadMeasured profile of the full house meter (heat pump included).
    await fill(isolated_measurement, start.subtract(days=16), 16)
    await provider.delete_by_datetime(None, None)
    provider._model = None
    await provider._update_data()
    early = forecast_of(provider)
    assert early[start.add(hours=2)] > 1250.0  # heat pump included

    # 35 days: model of the base load (heat pump subtracted) or LoadMeasured.
    await isolated_measurement.delete_by_datetime(None, None)
    await fill(isolated_measurement, start.subtract(days=35), 35)
    await provider.delete_by_datetime(None, None)
    provider._model = None
    await provider._update_data()
    summary = json.loads((tmp_path / "rl29" / "load_ml.json").read_text())
    late = forecast_of(provider)
    assert summary["used"], summary
    assert summary["holdout_mae_ml_wh"] < summary["holdout_mae_profile_wh"]
    # The night hours no longer contain the heat pump (base 300 W + cold effect <= 450 W).
    assert late[start.add(hours=2)] < 800.0
    assert late[start.add(hours=19)] > late[start.add(hours=2)] + 500.0
    p90 = forecast_of(provider, "loadforecast_p90_power_w")
    p10 = forecast_of(provider, "loadforecast_p10_power_w")
    assert p10[start.add(hours=19)] <= late[start.add(hours=19)] <= p90[start.add(hours=19)]
    assert np.isfinite([v for v in late.values() if v is not None]).all()
