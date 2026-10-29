"""Standby in the plan and the manual lock of the reduction (fork).

The plan states per slot whether the heat pump is in standby (``<id>_standby``), so a
controller no longer has to derive it from REDUCED + HOLD + holiday itself. The
controller reports a manual lock of the reduction (``<id>-reduce-lock-until-ts``,
Unix time, 0 = none); until then EOS plans as the controller runs: REDUCED is NORMAL
and the heat pump does not go into standby.
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
from akkudoktoreos.optimization.genetic.geneticsolution import (
    HEAT_PUMP_SOLUTION_COLUMNS,
)
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
    plan_result,
)

TZ = "Europe/Berlin"
T0 = pendulum.datetime(2026, 11, 3, 10, 0, tz=TZ)


def device(**away) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings.model_validate(
        {
            "device_id": "hp1",
            "unit": {"standby_w": 32.0, "off_w": 10.0},
            "away": {"standby_min_c": 16.0, **away},
        }
    )


def plan(hours=12, away=True, room=18.8, lock_hours=None, **kwargs):
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=kwargs.pop("settings", device()),
        initial_room_c=room,
        initial_dhw_c=29.0,
        away=away,
        away_until=T0.add(hours=40) if away else None,
        away_room_c=18.0,
        reduce_locked_until=T0.add(hours=lock_hours) if lock_hours is not None else None,
        **kwargs,
    )
    starts = [T0.add(hours=i) for i in range(hours)]
    return build_run_plan(params, starts, 1.0, [8.0] * hours, [0.0] * hours, [0.0003] * hours)


def schedule(run, heating):
    n = run.n_periods
    return plan_result(run, run.genes_for_modes([heating] * n, [DhwMode.HOLD] * n))


# ----------------------------------------------------------------------
# Standby column
# ----------------------------------------------------------------------


def test_solution_has_a_standby_column():
    assert ("standby", "standby", float) in HEAT_PUMP_SOLUTION_COLUMNS


def test_reduced_while_away_is_standby_in_the_plan():
    result = schedule(plan(), HeatingMode.REDUCED)
    assert result["standby"] == [1.0] * 12
    assert result["heating_mode"] == ["REDUCED"] * 12


def test_heating_or_hot_water_is_no_standby():
    run = plan()
    assert schedule(run, HeatingMode.NORMAL)["standby"] == [0.0] * 12
    n = run.n_periods
    charge = plan_result(
        run, run.genes_for_modes([HeatingMode.REDUCED] * n, [DhwMode.HEAT_PUMP] * n)
    )
    assert charge["standby"][0] == 0.0


def test_no_standby_at_home():
    result = schedule(plan(away=False, room=21.0), HeatingMode.REDUCED)
    assert result["standby"] == [0.0] * 12


def test_standby_ends_at_the_room_floor():
    """Room just above the floor of 16 degC: the controller restarts the heat pump."""
    result = schedule(plan(room=16.2), HeatingMode.REDUCED)
    assert result["standby"][0] == 0.0


# ----------------------------------------------------------------------
# Lock of the reduction
# ----------------------------------------------------------------------


def test_measurement_key_for_the_lock():
    assert "hp1-reduce-lock-until-ts" in device().measurement_keys


def test_lock_marks_the_slots_before_its_end():
    run = plan(lock_hours=3)
    assert [i.reduce_locked for i in run.inputs[:4]] == [True, True, True, False]
    assert not run.initial.standby  # locked: not in standby at the start
    assert plan().initial.standby


def test_locked_reduced_runs_as_normal_without_standby():
    run = plan(lock_hours=3)
    result = schedule(run, HeatingMode.REDUCED)
    assert result["heating_mode"][:3] == ["NORMAL"] * 3
    assert result["heating_mode"][3] == "REDUCED"
    assert result["standby"][:3] == [0.0] * 3
    n = run.n_periods
    locked = run.evaluate(run.genes_for_modes([HeatingMode.REDUCED] * n, [DhwMode.HOLD] * n))
    normal = run.evaluate(run.genes_for_modes([HeatingMode.NORMAL] * n, [DhwMode.HOLD] * n))
    # REDUCED in the locked slots costs the same as NORMAL there
    assert locked.electric_wh[:3] == pytest.approx(normal.electric_wh[:3])


def test_lock_holds_the_room_at_the_normal_target():
    """At home: a locked REDUCED does not let the room cool below the setpoint."""
    run = plan(away=False, room=21.0, hours=6, lock_hours=6)
    result = schedule(run, HeatingMode.REDUCED)
    free = schedule(plan(away=False, room=21.0, hours=6), HeatingMode.REDUCED)
    assert min(result["room_c"]) > min(free["room_c"]) + 0.1


def test_past_lock_changes_nothing():
    run = plan(lock_hours=0)
    assert not any(i.reduce_locked for i in run.inputs)


def test_milp_room_floor_respects_the_lock():
    from akkudoktoreos.optimization.milp.thermal import thermal_block

    run = plan(away=False, room=21.0, hours=6, lock_hours=2)
    block = thermal_block(run)
    assert block is not None
    assert block.room_floor_c[0] is not None and block.room_floor_c[0] >= 20.9
    assert block.room_floor_c[3] is None


# ----------------------------------------------------------------------
# Request
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_request_reads_the_lock(config_eos: ConfigEOS, isolated_measurement: Measurement):
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
                "heat_pumps": {"hp1": {}},
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "hp1-room-temperature", 20.5)
    await isolated_measurement.update_value(now, "hp1-dhw-temperature", 45.0)

    async def lock_until():
        heat_pumps = (await request().resolve()).heat_pumps
        assert heat_pumps is not None
        return heat_pumps[0].reduce_locked_until

    def request() -> ConfigOptimizationRequest:
        return ConfigOptimizationRequest.model_validate(
            {
                "forecasts": {
                    "pv_forecast_wh": [0.0] * 48,
                    "total_load": [300.0] * 48,
                    "electricity_price_per_wh": [0.0003] * 48,
                    "feed_in_tariff_per_wh": [0.00007] * 48,
                    "temperature_forecast": [4.0] * 48,
                    "ghi_forecast": [0.0] * 48,
                }
            }
        )

    assert await lock_until() is None
    until = now.add(hours=2)
    await isolated_measurement.update_value(now, "hp1-reduce-lock-until-ts", until.timestamp())
    locked = await lock_until()
    assert locked is not None
    assert locked.timestamp() == pytest.approx(until.timestamp())
    # 0 (no lock) and a time in the past are ignored
    await isolated_measurement.update_value(now, "hp1-reduce-lock-until-ts", 0.0)
    assert await lock_until() is None
    await isolated_measurement.update_value(
        now, "hp1-reduce-lock-until-ts", now.subtract(hours=1).timestamp()
    )
    assert await lock_until() is None
