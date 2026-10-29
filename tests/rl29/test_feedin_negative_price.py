"""No feed-in compensation at negative market prices (fork, § 51 EEG).

Plants commissioned in Germany from 2025-02-25 get no feed-in tariff while the
market price is negative. With ``feedintariff.zero_at_negative_price`` the
optimizer values feed-in in these slots at 0, so flexible loads move there.
"""

from __future__ import annotations

import math

import numpy as np
import pendulum
import pytest

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_prediction
from akkudoktoreos.optimization.genetic import configrequest
from akkudoktoreos.optimization.genetic.configrequest import (
    ConfigOptimizationRequest,
    zero_tariff_at_negative_price,
)

TZ = "Europe/Berlin"


def test_zero_only_where_the_raw_price_is_negative():
    tariffs = [0.07, 0.07, 0.07, 0.07]
    raw = [0.05, -0.001, 0.0, -0.2]
    assert zero_tariff_at_negative_price(tariffs, raw) == [0.07, 0.0, 0.07, 0.0]
    assert tariffs == [0.07] * 4  # input untouched


def test_missing_raw_prices_keep_the_tariff():
    assert zero_tariff_at_negative_price([0.07, 0.07, 0.07], [math.nan, -0.1]) == [
        0.07,
        0.0,
        0.07,
    ]


def _configure(config_eos: ConfigEOS, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "feedintariff": {"zero_at_negative_price": enabled},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 24,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                },
            },
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "max_home_appliances": 0,
                "home_appliances": {},
                "max_heat_pumps": 0,
                "heat_pumps": {},
            },
        }
    )
    now = pendulum.datetime(2026, 6, 20, 0, 0, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)

    # Raw market price 10 ct/kWh, negative from 11:00 to 15:00; gross price incl. fees
    # stays positive; feed-in tariff 7.21 ct/kWh.
    raw = np.array([-0.00002 if 11 <= h % 24 < 15 else 0.0001 for h in range(48)])
    series = {
        "elecprice_marketprice_raw_wh": raw,
        "elecprice_marketprice_wh": raw + 0.0003,
        "feed_in_tariff_wh": np.full(48, 0.0000721),
    }
    calls: list[str] = []

    async def fake_forecast(prediction, key, **kwargs):
        calls.append(key)
        return series[key].copy()

    async def no_update(*args, **kwargs):
        return None

    monkeypatch.setattr(configrequest, "bounded_forecast_array", fake_forecast)
    monkeypatch.setattr(type(get_prediction()), "update_data", no_update)


def _request(**extra) -> ConfigOptimizationRequest:
    return ConfigOptimizationRequest.model_validate(
        {
            "forecasts": {
                "pv_forecast_wh": [0.0] * 48,
                "total_load": [300.0] * 48,
                "temperature_forecast": [15.0] * 48,
                **extra,
            }
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_request_zeroes_the_tariff_in_negative_hours(
    config_eos: ConfigEOS, monkeypatch: pytest.MonkeyPatch, enabled: bool
):
    _configure(config_eos, monkeypatch, enabled)
    params = await _request().resolve()
    tariffs = params.ems.feed_in_tariff_per_wh
    assert isinstance(tariffs, list)
    noon = [tariffs[h] for h in range(11, 15)]
    morning = tariffs[8]
    assert morning == pytest.approx(0.0721 / 1000)
    if enabled:
        assert noon == [0.0] * 4
        assert tariffs[15] == pytest.approx(0.0721 / 1000)
    else:
        assert noon == [pytest.approx(0.0721 / 1000)] * 4


@pytest.mark.asyncio
async def test_supplied_tariffs_are_not_changed(
    config_eos: ConfigEOS, monkeypatch: pytest.MonkeyPatch
):
    _configure(config_eos, monkeypatch, True)
    params = await _request(feed_in_tariff_per_wh=[0.0001] * 48).resolve()
    tariffs = params.ems.feed_in_tariff_per_wh
    assert isinstance(tariffs, list)
    assert tariffs == [0.0001] * len(tariffs)
