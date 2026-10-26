"""PV forecast ensemble with self-calibration (RL29 fork)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncGenerator

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement, get_prediction
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.prediction.pvforecastensemble import (
    MEMBER_KEYS,
    PVForecastEnsemble,
    PVForecastEnsembleCommonSettings,
    calibration_factor,
    hourly_mae,
    member_weights,
    parse_ha_forecast,
)

TZ = "Europe/Berlin"


def test_member_weights():
    assert member_weights({}, ["pvlib", "homeassistant"]) == {"pvlib": 0.5, "homeassistant": 0.5}
    weights = member_weights({"pvlib": 200.0, "homeassistant": 100.0}, ["pvlib", "homeassistant"])
    assert weights["homeassistant"] == pytest.approx(0.8)
    assert weights["pvlib"] == pytest.approx(0.2)
    assert member_weights({"pvlib": 50.0}, ["pvlib"]) == {"pvlib": 1.0}
    assert member_weights({}, []) == {}


def test_calibration_factor_quantile_bounds_and_filters():
    actual = [8000, 9000, 10000, 100, 400]
    forecast = [10000, 10000, 10000, 10000, 300]  # day 4 outage, day 5 tiny forecast
    assert calibration_factor(actual, forecast, 0.5, 0.6, 1.3, 0.15, 3) == pytest.approx(0.9)
    assert calibration_factor(actual, forecast, 0.5, 0.95, 1.3, 0.15, 3) == pytest.approx(0.95)
    assert calibration_factor(actual, forecast, 0.5, 0.6, 1.3, 0.15, 4) is None
    low = calibration_factor(actual, forecast, 0.25, 0.6, 1.3, 0.15, 3)
    assert low < 0.9


def test_hourly_mae_and_parsing():
    assert hourly_mae([100.0, None, 300.0], [110.0, 5.0, 280.0]) == pytest.approx(15.0)
    assert hourly_mae([None], [1.0]) is None
    parsed = parse_ha_forecast(
        [{"datetime": "2026-10-26T12:00:00+01:00", "value": 2.5}, {"bad": 1}],
        "datetime",
        "value",
        "kW",
        TZ,
    )
    assert len(parsed) == 1 and parsed[0][1] == 2500.0


def test_settings_validation():
    assert PVForecastEnsembleCommonSettings().members == ["pvlib"]
    with pytest.raises(ValueError):
        PVForecastEnsembleCommonSettings(members=["other"])
    with pytest.raises(ValueError):
        PVForecastEnsembleCommonSettings(scale_min=1.5, scale_max=1.0)


# --------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    provider = PVForecastEnsemble()
    await provider.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)
        await provider.delete_by_datetime(None, None)


def bell(hour: int) -> float:
    """Clear-sky like PV power [W] per local hour."""
    return max(0.0, 6000.0 * (1 - ((hour - 13) / 6.0) ** 2))


def test_provider_is_registered(config_eos: ConfigEOS):
    assert any(p.provider_id() == "PVForecastEnsemble" for p in get_prediction().providers)


@pytest.mark.asyncio
async def test_weights_and_calibration_from_history(
    config_eos: ConfigEOS, isolated: Measurement, tmp_path: Path, monkeypatch
):
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ, "data_folder_path": str(tmp_path)},
            "prediction": {"hours": 24, "historic_hours": 24 * 30},
            "measurement": {"pv_production_emr_keys": ["pv_mr"]},
            "pvforecast": {
                "provider": "PVForecastEnsemble",
                "ensemble": {"members": ["pvlib", "homeassistant"], "days": 7, "min_days": 3},
            },
        }
    )
    today = pendulum.datetime(2026, 10, 26, 0, tz=TZ)
    get_ems().set_start_datetime(today)
    provider = PVForecastEnsemble()

    # History: actual = 0.8 x bell; PVLib overestimates by 25 %, HA is exact but noisy.
    reading = 100.0
    t = today.subtract(days=7)
    while t < today:
        local = t.in_timezone(TZ)
        actual = 0.8 * bell(local.hour)
        await isolated.update_value(t, "pv_mr", reading)
        reading += actual / 1000.0
        await provider.update_value(t, {MEMBER_KEYS["pvlib"]: actual * 1.25})
        noisy = actual * (1.15 if local.hour % 2 else 0.85)
        await provider.update_value(t, {MEMBER_KEYS["homeassistant"]: noisy})
        t = t.add(hours=1)
    await isolated.update_value(today, "pv_mr", reading)

    async def fake_pvlib(self):
        for h in range(24):
            await self._update_value(today.add(hours=h), MEMBER_KEYS["pvlib"], bell(h))
        return True

    async def fake_ha(self):
        for h in range(24):
            await self._update_value(
                today.add(hours=h), MEMBER_KEYS["homeassistant"], 0.8 * bell(h)
            )
        return True

    monkeypatch.setattr(PVForecastEnsemble, "_member_pvlib", fake_pvlib)
    monkeypatch.setattr(PVForecastEnsemble, "_member_homeassistant", fake_ha)
    await provider._update_data()

    summary = json.loads((tmp_path / "rl29" / "pv_ensemble.json").read_text())
    assert summary["calibrated"] is True
    # PVLib: |0.25 x actual|; HA: |0.15 x actual| -> HA gets more weight.
    assert summary["weights"]["homeassistant"] > summary["weights"]["pvlib"]
    dates, values = await provider.key_to_lists(
        "pvforecast_ac_power", today.add(hours=13), today.add(hours=13, minutes=1)
    )
    blended = summary["weights"]["pvlib"] * bell(13) + summary["weights"][
        "homeassistant"
    ] * 0.8 * bell(13)
    assert values[0] == pytest.approx(blended * summary["scale"], rel=0.02)
    # The calibrated blend at noon is close to the true 0.8 x bell.
    assert values[0] == pytest.approx(0.8 * bell(13), rel=0.08)


@pytest.mark.asyncio
async def test_member_failure_uses_the_other(
    config_eos: ConfigEOS, isolated: Measurement, tmp_path: Path, monkeypatch
):
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ, "data_folder_path": str(tmp_path)},
            "prediction": {"hours": 24},
            "pvforecast": {
                "provider": "PVForecastEnsemble",
                "ensemble": {"members": ["pvlib", "homeassistant"]},
            },
        }
    )
    today = pendulum.datetime(2026, 10, 26, 0, tz=TZ)
    get_ems().set_start_datetime(today)
    provider = PVForecastEnsemble()

    async def failed(self):
        return False

    async def fake_ha(self):
        for h in range(24):
            await self._update_value(today.add(hours=h), MEMBER_KEYS["homeassistant"], bell(h))
        return True

    monkeypatch.setattr(PVForecastEnsemble, "_member_pvlib", failed)
    monkeypatch.setattr(PVForecastEnsemble, "_member_homeassistant", fake_ha)
    await provider._update_data()
    _, values = await provider.key_to_lists(
        "pvforecast_ac_power", today.add(hours=12), today.add(hours=12, minutes=1)
    )
    assert values[0] == pytest.approx(bell(12))

    monkeypatch.setattr(PVForecastEnsemble, "_member_homeassistant", failed)
    with pytest.raises(ValueError):
        await provider._update_data()
