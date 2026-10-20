"""Outdoor temperature forecast for configuration-owned GENETIC runs (RL29 fork).

Upstream left ``temperature_forecast`` empty unless the HTTP request supplied it, so
automatic optimization runs had no temperature for the heat pump model.
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest

from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest


def _series(values, start="2026-09-12T00:00:00Z", freq="h"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq=freq))


def _prediction(temperature: pd.Series | None):
    provider = {
        "pvforecast_ac_power": 1000.0,
        "loadforecast_power_w": 2000.0,
        "elecprice_marketprice_wh": 0.0003,
        "feed_in_tariff_wh": 0.00007,
    }

    async def read(key, **kwargs):
        if key == "weather_temp_air":
            if temperature is None:
                raise KeyError(key)
            return temperature
        return _series([provider[key]] * 48)

    return Mock(update_data=AsyncMock(), key_to_raw_series=AsyncMock(side_effect=read))


@pytest.mark.asyncio
async def test_temperature_read_from_weather_provider(configured_request, monkeypatch):
    _, _, _, data = configured_request
    data["forecasts"] = {}
    # 0 degC at midnight rising by 1 K per hour.
    monkeypatch.setattr(
        ConfigOptimizationRequest, "prediction", _prediction(_series([float(h) for h in range(48)]))
    )
    params = await ConfigOptimizationRequest.model_validate(data).resolve()

    temps = params.temperature_forecast
    assert temps is not None
    assert len(temps) == len(params.ems.pv_forecast_wh)
    # Quarter-hour slots inherit the hourly interval average.
    assert temps[0] == pytest.approx(0.0)
    assert temps[3] == pytest.approx(0.0)
    assert temps[4] == pytest.approx(1.0)
    assert temps[4 * 10] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_temperature_gaps_stay_none(configured_request, monkeypatch):
    _, _, _, data = configured_request
    data["forecasts"] = {}
    values = [5.0] * 48
    values[2] = math.nan
    monkeypatch.setattr(ConfigOptimizationRequest, "prediction", _prediction(_series(values)))
    params = await ConfigOptimizationRequest.model_validate(data).resolve()
    temps = params.temperature_forecast
    assert temps[8:12] == [None, None, None, None]  # 02:00-03:00 missing
    assert temps[12] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_missing_weather_provider_does_not_cancel(configured_request, monkeypatch):
    _, _, _, data = configured_request
    data["forecasts"] = {}
    monkeypatch.setattr(ConfigOptimizationRequest, "prediction", _prediction(None))
    params = await ConfigOptimizationRequest.model_validate(data).resolve()
    assert params.temperature_forecast is None
    assert len(params.ems.pv_forecast_wh) > 0


@pytest.mark.asyncio
async def test_supplied_temperature_takes_precedence(configured_request, monkeypatch):
    _, _, _, data = configured_request
    data["forecasts"]["temperature_forecast"] = [-3.0] * 10
    prediction = _prediction(_series([20.0] * 48))
    monkeypatch.setattr(ConfigOptimizationRequest, "prediction", prediction)
    params = await ConfigOptimizationRequest.model_validate(data).resolve()
    temps = params.temperature_forecast
    assert temps[:10] == [-3.0] * 10
    assert temps[10:] == [None] * (len(temps) - 10)
    prediction.key_to_raw_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_supplied_forecasts_do_not_trigger_provider_update(configured_request, monkeypatch):
    """With all energy series supplied, the temperature is read from stored data only."""
    _, _, _, data = configured_request
    prediction = _prediction(_series([7.0] * 48))
    monkeypatch.setattr(ConfigOptimizationRequest, "prediction", prediction)
    params = await ConfigOptimizationRequest.model_validate(data).resolve()
    prediction.update_data.assert_not_awaited()
    assert params.temperature_forecast[0] == pytest.approx(7.0)
