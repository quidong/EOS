"""Heat reserve before a weather warning (fork).

The controller reports a warned period (``<id>-warning-start-ts`` / ``-end-ts``).
For its slots the comfort minimum rises by ``raise_room_k`` and the hot water minimum
to ``dhw_min_c``, so the building and the tank are charged beforehand.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import DhwMode, HeatingMode
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
)

TZ = "Europe/Berlin"
NOON = pendulum.datetime(2026, 11, 6, 12, 0, tz=TZ)  # comfort minimum 20.5 by day
ON = {"weather_warning": {"enabled": True}}


def device(**settings) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings.model_validate({"device_id": "hp1", **settings})


def run_plan(settings=None, start_h=None, end_h=None, hours=24, **kwargs):
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=settings or device(**ON),
        initial_room_c=21.0,
        initial_dhw_c=45.0,
        warning_start=NOON.add(hours=start_h) if start_h is not None else None,
        warning_end=NOON.add(hours=end_h) if end_h is not None else None,
        **kwargs,
    )
    starts = [NOON.add(hours=i) for i in range(hours)]
    return build_run_plan(params, starts, 1.0, [-5.0] * hours, [0.0] * hours, [0.0003] * hours)


def test_measurement_keys_only_when_enabled():
    assert "hp1-warning-start-ts" not in device().measurement_keys
    assert {"hp1-warning-start-ts", "hp1-warning-end-ts"} <= set(device(**ON).measurement_keys)


def test_warning_raises_room_and_hot_water_minimum_in_its_slots():
    settings = device(weather_warning={"enabled": True}, dhw={"raised_setpoint_c": 55.0})
    run = run_plan(settings=settings, start_h=6, end_h=10)
    plain = run_plan(settings=device(dhw={"raised_setpoint_c": 55.0}))
    for i in range(24):
        inside = 6 <= i < 10
        expected = plain.inputs[i].comfort_min_c + (1.0 if inside else 0.0)
        assert run.inputs[i].comfort_min_c == pytest.approx(
            min(expected, plain.system.comfort_band(plain.inputs[i])[2])
        )
        assert run.inputs[i].dhw_min_c == (50.0 if inside else None)


def test_hot_water_minimum_is_capped_at_what_the_heat_pump_reaches():
    settings = device(weather_warning={"enabled": True, "dhw_min_c": 75.0})
    run = run_plan(settings=settings, start_h=0, end_h=2)
    reachable = run.system.reachable_dhw_c(tuple(settings.optimization.heating_modes))
    assert run.inputs[0].dhw_min_c == pytest.approx(reachable)


def test_disabled_or_missing_warning_changes_nothing():
    plain = run_plan(settings=device())
    for run in (run_plan(settings=device(), start_h=0, end_h=4), run_plan()):
        assert [i.comfort_min_c for i in run.inputs] == [i.comfort_min_c for i in plain.inputs]
        assert all(i.dhw_min_c is None for i in run.inputs)


def test_long_warnings_are_cut_to_max_hours():
    settings = device(weather_warning={"enabled": True, "max_hours": 3})
    run = run_plan(settings=settings, start_h=2, end_h=20)
    assert [i.dhw_min_c is not None for i in run.inputs[:7]] == [
        False,
        False,
        True,
        True,
        True,
        False,
        False,
    ]


def test_away_with_hot_water_off_only_raises_the_room():
    run = run_plan(
        start_h=0,
        end_h=4,
        away=True,
        away_until=NOON.add(hours=40),
        away_room_c=17.0,
        away_min_c=16.0,
    )
    assert run.inputs[0].dhw_off and run.inputs[0].dhw_min_c is None
    assert run.inputs[0].comfort_min_c == pytest.approx(17.0)  # floor 16 + 1


def test_plan_charges_before_the_warning():
    """Hot water below the raised minimum during the warning is penalised."""
    run = run_plan(start_h=3, end_h=6, hours=8)
    n = run.n_periods
    hold = run.evaluate(run.genes_for_modes([HeatingMode.NORMAL] * n, [DhwMode.HOLD] * n))
    charge = run.evaluate(
        run.genes_for_modes(
            [HeatingMode.NORMAL] * n, [DhwMode.HEAT_PUMP] * 3 + [DhwMode.HOLD] * (n - 3)
        )
    )
    assert hold.dhw_kh > 0
    assert charge.dhw_kh < hold.dhw_kh


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_request_reads_the_warning(config_eos: ConfigEOS, isolated_measurement: Measurement):
    from akkudoktoreos.optimization.genetic.configrequest import (
        ConfigOptimizationRequest,
    )

    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 8,
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
                "max_heat_pumps": 1,
                "heat_pumps": {"hp1": ON},
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "hp1-room-temperature", 20.5)
    await isolated_measurement.update_value(now, "hp1-dhw-temperature", 45.0)

    def request() -> ConfigOptimizationRequest:
        return ConfigOptimizationRequest.model_validate(
            {
                "forecasts": {
                    "pv_forecast_wh": [0.0] * 48,
                    "total_load": [300.0] * 48,
                    "electricity_price_per_wh": [0.0003] * 48,
                    "feed_in_tariff_per_wh": [0.00007] * 48,
                    "temperature_forecast": [-4.0] * 48,
                    "ghi_forecast": [0.0] * 48,
                }
            }
        )

    async def warning():
        heat_pumps = (await request().resolve()).heat_pumps
        assert heat_pumps is not None
        return heat_pumps[0].warning_start, heat_pumps[0].warning_end

    assert await warning() == (None, None)
    begin, end = now.add(hours=5), now.add(hours=12)
    await isolated_measurement.update_value(now, "hp1-warning-start-ts", begin.timestamp())
    await isolated_measurement.update_value(now, "hp1-warning-end-ts", end.timestamp())
    start, stop = await warning()
    assert start is not None and stop is not None
    assert (start.timestamp(), stop.timestamp()) == (begin.timestamp(), end.timestamp())
    # cleared warning (0) and an already expired one are ignored
    await isolated_measurement.update_value(now, "hp1-warning-start-ts", 0.0)
    assert await warning() == (None, None)
    await isolated_measurement.update_value(
        now, "hp1-warning-start-ts", now.subtract(hours=10).timestamp()
    )
    await isolated_measurement.update_value(
        now, "hp1-warning-end-ts", now.subtract(hours=1).timestamp()
    )
    assert await warning() == (None, None)
