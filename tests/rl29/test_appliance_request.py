"""Request-driven home appliances (RL29 fork).

The washing machine is planned only when Home Assistant reports a pending
request (remote start enabled) together with the deadline by which the run has
to be finished. The solution exposes the next planned start so HA can program
a delayed start on the machine.
"""

from __future__ import annotations

from typing import Any

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.settings.homeappliancesettings import HomeApplianceCommonSettings
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.utils.datetimeutil import to_datetime

START = "2026-11-03T10:00:00"


def washer(**kwargs: Any) -> dict[str, Any]:
    return {
        "device_id": "waschmaschine",
        "consumption_wh": 1200,
        "duration_h": 3,
        "num_cycles": 1,
        "request_driven": True,
        **kwargs,
    }


def configure(config_eos, appliance: dict[str, Any], hours: int = 24) -> None:
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": "Europe/Berlin"},
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": hours,
                    "interval_sec": 3600,
                    "tail_horizon_hours": 0,
                    "individuals": 40,
                    "generations": 30,
                },
            },
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "max_home_appliances": 1,
                "home_appliances": {"waschmaschine": appliance},
                "max_heat_pumps": 0,
                "heat_pumps": {},
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime(START, in_timezone="Europe/Berlin"))


def request(prices: list[float] | None = None, hours: int = 48) -> ConfigOptimizationRequest:
    return ConfigOptimizationRequest.model_validate(
        {
            "forecasts": {
                "pv_forecast_wh": [0.0] * hours,
                "total_load": [300.0] * hours,
                "electricity_price_per_wh": prices or [0.0003] * hours,
                "feed_in_tariff_per_wh": [0.00007] * hours,
                "temperature_forecast": [5.0] * hours,
            }
        }
    )


async def report(values: dict[str, float], minutes_ago: int = 2) -> None:
    measurement = get_measurement()
    when = get_ems().start_datetime.subtract(minutes=minutes_ago)
    for key, value in values.items():
        await measurement.update_value(when, key, value)


@pytest_asyncio.fixture
async def clean_measurements():
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    yield
    await measurement.delete_by_datetime(None, None)


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def test_request_keys_are_registered_only_when_request_driven():
    plain = HomeApplianceCommonSettings(**washer(request_driven=False))
    driven = HomeApplianceCommonSettings(**washer())
    assert plain.measurement_keys == ["waschmaschine.cycles_completed"]
    assert driven.measurement_keys == [
        "waschmaschine.cycles_completed",
        "waschmaschine-requested",
        "waschmaschine-deadline",
        "waschmaschine-duration",
    ]


# ----------------------------------------------------------------------
# Configuration-owned request
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_request_the_appliance_is_not_planned(config_eos, clean_measurements):
    configure(config_eos, washer())
    prepared = await request().resolve()
    assert prepared.resolved_home_appliances() == []


@pytest.mark.asyncio
async def test_request_withdrawn_is_not_planned(config_eos, clean_measurements):
    configure(config_eos, washer())
    await report({"waschmaschine-requested": 0.0})
    prepared = await request().resolve()
    assert prepared.resolved_home_appliances() == []


@pytest.mark.asyncio
async def test_stale_request_is_ignored(config_eos, clean_measurements):
    configure(config_eos, washer(request_max_age_seconds=1800))
    await report({"waschmaschine-requested": 1.0}, minutes_ago=45)
    prepared = await request().resolve()
    assert prepared.resolved_home_appliances() == []


@pytest.mark.asyncio
async def test_request_sets_deadline_and_duration(config_eos, clean_measurements):
    configure(config_eos, washer())
    deadline = pendulum.datetime(2026, 11, 3, 19, 30, tz="Europe/Berlin")
    await report(
        {
            "waschmaschine-requested": 1.0,
            "waschmaschine-deadline": float(deadline.timestamp()),
            "waschmaschine-duration": 3.65,
        }
    )
    prepared = await request().resolve()
    (appliance,) = prepared.resolved_home_appliances()
    assert appliance.deadline_datetime is not None
    assert appliance.deadline_datetime.timestamp() == deadline.timestamp()
    assert appliance.duration_h == 4
    assert appliance.consumption_wh == 1600  # average power of the configured run kept


@pytest.mark.asyncio
async def test_request_without_deadline_keeps_configuration(config_eos, clean_measurements):
    configure(config_eos, washer())
    await report({"waschmaschine-requested": 1.0, "waschmaschine-deadline": 0.0})
    prepared = await request().resolve()
    (appliance,) = prepared.resolved_home_appliances()
    assert appliance.deadline_datetime is None
    assert appliance.duration_h == 3


@pytest.mark.asyncio
async def test_plain_appliance_is_unchanged(config_eos, clean_measurements):
    configure(config_eos, washer(request_driven=False))
    prepared = await request().resolve()
    (appliance,) = prepared.resolved_home_appliances()
    assert appliance.device_id == "waschmaschine"
    assert appliance.deadline_datetime is None


# ----------------------------------------------------------------------
# Optimization and solution
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_is_placed_in_cheap_hours_before_the_deadline(config_eos, clean_measurements):
    """Night request: deadline 06:00, cheapest hours 02:00-06:00 (MVV module 3)."""
    start = "2026-11-03T20:00:00"
    configure(config_eos, washer(), hours=12)
    get_ems().set_start_datetime(to_datetime(start, in_timezone="Europe/Berlin"))
    deadline = pendulum.datetime(2026, 11, 4, 6, 0, tz="Europe/Berlin")
    await report(
        {
            "waschmaschine-requested": 1.0,
            "waschmaschine-deadline": float(deadline.timestamp()),
        }
    )
    # 20:00 ... 07:00: expensive evening, cheap 02:00-06:00, expensive morning.
    prices = [0.00045] * 6 + [0.00025] * 4 + [0.00048] * 38
    params = await request(prices).resolve()
    optimizer = GeneticOptimization(fixed_seed=11)
    solution = optimizer.optimize_ems(params, ngen=30, individuals=40)

    (run_start,) = solution.appliance_starts["waschmaschine"]
    assert run_start.in_timezone("Europe/Berlin").hour in (2, 3)
    assert run_start.add(hours=3).timestamp() <= deadline.timestamp()
    assert solution.appliance_deadline_missed["waschmaschine"] is False

    table = (await solution.optimization_solution()).solution.to_dataframe()
    next_start = table["waschmaschine_next_start_ts"]
    assert next_start.iloc[0] == pytest.approx(run_start.timestamp())
    after = [ts for t, ts in next_start.items() if t.timestamp() > run_start.timestamp()]
    assert all(ts == 0.0 for ts in after)


@pytest.mark.asyncio
async def test_cycle_count_survives_records_of_other_keys(config_eos, clean_measurements):
    """A later record with only the request keys must not hide the cycle count."""
    configure(config_eos, washer())
    await report({"waschmaschine.cycles_completed": 1.0}, minutes_ago=10)
    await report({"waschmaschine-requested": 1.0}, minutes_ago=2)
    prepared = await request().resolve()
    (appliance,) = prepared.resolved_home_appliances()
    assert appliance.completed_cycles == 1
