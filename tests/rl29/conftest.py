"""Shared fixtures for the RL29 fork tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from akkudoktoreos.optimization.genetic import configrequest
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.utils.datetimeutil import to_datetime


@pytest.fixture
def configured_request(config_eos, monkeypatch):
    """Battery + inverter configuration with 15 min GENETIC slots.

    Mirrors the fixture of the same name in tests/test_config_optimization_request.py.
    """
    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 24},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "interval_sec": 900,
                    "horizon_hours": 1,
                    "tail_horizon_hours": 0,
                    "terminal_value_mode": "FIXED",
                    "terminal_value_euro_per_kwh": 0.23,
                    "individuals": 10,
                    "generations": 10,
                    "seed": 42,
                },
            },
            "devices": {
                "max_batteries": 1,
                "max_electric_vehicles": 0,
                "max_inverters": 1,
                "max_home_appliances": 2,
                "batteries": {
                    "storage": {
                        "capacity_wh": 2000,
                        "max_charge_power_w": 1000,
                        "levelized_cost_of_storage_amt_kwh": 0.01,
                    }
                },
                "electric_vehicles": {},
                "inverters": {"inverter": {"battery_id": "storage", "max_power_w": 1000}},
                "home_appliances": {},
            },
        }
    )
    start = to_datetime("2026-09-12T10:15:00Z", in_timezone="UTC")
    ems = SimpleNamespace(
        start_datetime=start, observation_datetime=start, genetic_solution=lambda: None
    )
    monkeypatch.setattr(configrequest, "get_ems", lambda: ems)
    measurement = Mock(key_to_lists=AsyncMock(return_value=([], [])))
    monkeypatch.setattr(ConfigOptimizationRequest, "measurement", measurement)
    data = {
        "soc": {"storage": 42},
        "forecasts": {
            "pv_forecast_wh": [100.0] * 96,
            "total_load": [200.0] * 96,
            "electricity_price_per_wh": [0.0003] * 96,
            "feed_in_tariff_per_wh": [0.00008] * 96,
        },
    }
    return config_eos, ems, measurement, data
