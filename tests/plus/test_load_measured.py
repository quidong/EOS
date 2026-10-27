"""Load forecast from the own measured history (eos-plus fork, B2).

The provider forecasts from the household's own load meter history once enough
healthy days exist and falls back to ``LoadAkkudoktorAdjusted`` before that.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta

import numpy as np
import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement, get_prediction
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.prediction.loadmeasured import (
    LoadMeasured,
    LoadMeasuredCommonSettings,
    build_profile,
    profile_value,
)

TZ = "Europe/Berlin"


def workday_wh(hour: int) -> float:
    return 1000.0 if 18 <= hour < 21 else 300.0


def weekend_wh(hour: int) -> float:
    return 800.0 if 10 <= hour < 14 else 400.0


def series(days: int, start: date = date(2026, 9, 1), gaps: tuple[int, ...] = ()):
    """Hourly (stamp, Wh) pairs: workday/weekend pattern, optional gap days (zero)."""
    out = []
    for d in range(days):
        day = start + timedelta(days=d)
        for h in range(24):
            stamp = datetime(day.year, day.month, day.day, h)
            if d in gaps:
                wh = 0.0
            else:
                wh = weekend_wh(h) if day.weekday() >= 5 else workday_wh(h)
            out.append((stamp, wh))
    return out


# ----------------------------------------------------------------------
# Profile
# ----------------------------------------------------------------------


def test_not_enough_days_gives_no_profile():
    settings = LoadMeasuredCommonSettings(min_days=14)
    assert build_profile(series(13), settings) is None
    assert build_profile([], settings) is None


def test_weekday_profile_when_every_weekday_is_covered():
    settings = LoadMeasuredCommonSettings(min_days=14, min_days_per_type=3)
    profile = build_profile(series(21), settings)
    assert profile is not None and profile["mode"] == "weekday" and profile["days"] == 21
    # 2026-09-01 is a Tuesday; Saturday is weekday 5
    assert profile_value(profile, 1, 19) == pytest.approx(1000.0)
    assert profile_value(profile, 1, 3) == pytest.approx(300.0)
    assert profile_value(profile, 5, 11) == pytest.approx(800.0)
    assert profile_value(profile, 6, 3) == pytest.approx(400.0)


def test_pooled_workday_weekend_profile_with_fewer_days_per_weekday():
    settings = LoadMeasuredCommonSettings(min_days=14, min_days_per_type=3)
    profile = build_profile(series(16), settings)  # Saturdays/Sundays only twice
    assert profile is not None and profile["mode"] == "workday_weekend"
    assert profile_value(profile, 2, 19) == pytest.approx(1000.0)
    assert profile_value(profile, 6, 12) == pytest.approx(800.0)


def test_gap_days_are_left_out_and_median_resists_outliers():
    settings = LoadMeasuredCommonSettings(min_days=14, min_days_per_type=3)
    data = series(28, gaps=(3, 4))
    # one party evening on a Tuesday
    data = [
        (s, wh * 5 if (s.date() == date(2026, 9, 8) and s.hour == 19) else wh) for s, wh in data
    ]
    profile = build_profile(data, settings)
    assert profile is not None and profile["days"] == 26
    assert profile_value(profile, 1, 19) == pytest.approx(1000.0)  # median, not the party
    # only gaps would leave too few days
    assert build_profile(series(16, gaps=(1, 2, 3)), settings) is None


def test_incomplete_days_and_invalid_values_are_ignored():
    settings = LoadMeasuredCommonSettings(min_days=3, min_days_per_type=1)
    data = series(7)
    data += [(datetime(2026, 9, 8, h), 500.0) for h in range(10)]  # partial day
    data.append((datetime(2026, 9, 2, 5), float("nan")))
    profile = build_profile(data, settings)
    assert profile is not None and profile["days"] == 7


def test_quantile_is_configurable():
    data = series(21)
    data = [(s, wh * (1.0 + 0.1 * (s.day % 3))) for s, wh in data]
    low = build_profile(data, LoadMeasuredCommonSettings(quantile=0.2))
    high = build_profile(data, LoadMeasuredCommonSettings(quantile=0.8))
    assert low is not None and high is not None
    assert profile_value(high, 1, 19) > profile_value(low, 1, 19)


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


async def fill_meter(measurement: Measurement, start: pendulum.DateTime, days: int) -> None:
    reading = 1000.0  # kWh
    t = start
    for _ in range(days * 24 + 1):
        await measurement.update_value(t, "load_mr", reading)
        local = t.in_timezone(TZ)
        wh = weekend_wh(local.hour) if local.day_of_week >= 5 else workday_wh(local.hour)
        reading += wh / 1000.0
        t = t.add(hours=1)


def configure(config_eos: ConfigEOS) -> None:
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "measurement": {"load_emr_keys": ["load_mr"]},
            "load": {
                "provider": "LoadMeasured",
                "loadakkudoktor": {"loadakkudoktor_year_energy_kwh": 4000},
                "loadmeasured": {"weeks": 6, "min_days": 14},
            },
        }
    )


def forecast(provider: LoadMeasured) -> dict[pendulum.DateTime, float]:
    return {
        pendulum.instance(r.date_time).in_timezone(TZ): r.loadforecast_power_w
        for r in provider.records
    }


def test_provider_is_registered(config_eos: ConfigEOS):
    configure(config_eos)
    assert config_eos.load.provider == "LoadMeasured"
    assert "LoadMeasured" in config_eos.load.providers
    assert any(p.provider_id() == "LoadMeasured" for p in get_prediction().providers)


@pytest.mark.asyncio
async def test_provider_switches_over_once_enough_days_exist(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    configure(config_eos)
    start_of_forecast = pendulum.datetime(2026, 10, 20, 0, tz=TZ)  # Tuesday
    get_ems().set_start_datetime(start_of_forecast)

    # 7 days of history: fallback to the adjusted standard profile
    await fill_meter(isolated_measurement, start_of_forecast.subtract(days=7), 7)
    provider = LoadMeasured()
    await provider.delete_by_datetime(None, None)
    await provider._update_data()
    early = forecast(provider)
    tuesday_evening = start_of_forecast.add(hours=19)
    assert early[tuesday_evening] != pytest.approx(1000.0)

    # 21 days of history: own profile
    await isolated_measurement.delete_by_datetime(None, None)
    await fill_meter(isolated_measurement, start_of_forecast.subtract(days=21), 21)
    await provider.delete_by_datetime(None, None)
    await provider._update_data()
    late = forecast(provider)
    assert late[tuesday_evening] == pytest.approx(1000.0, rel=0.02)
    assert late[start_of_forecast.add(hours=3)] == pytest.approx(300.0, rel=0.02)
    saturday_noon = pendulum.datetime(2026, 10, 24, 12, tz=TZ)
    if saturday_noon in late:
        assert late[saturday_noon] == pytest.approx(800.0, rel=0.02)
    # the standard profile columns stay available for the dashboard
    assert all(r.loadakkudoktor_std_power_w is not None for r in provider.records)
    assert np.isfinite(list(late.values())).all()
