"""Holiday mode of the heat pump (eos-plus fork).

Until the return the comfort minimum and NORMAL setpoint are the away
temperature and hot water is off; from the return the normal band applies,
so the optimizer heats building and tank up before the return.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import (
    DhwMode,
    HeatingMode,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
    plan_result,
)

T0 = pendulum.datetime(2026, 11, 3, 10, 0, tz="Europe/Berlin")


def plan(
    hours=24, return_in_h=18, away=True, room_c=17.0, prices=None, away_settings=None, **settings
):
    if away_settings is not None:
        settings["away"] = away_settings
    device = HeatPumpCommonSettings(device_id="wbb12", **settings)
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device,
        initial_room_c=room_c,
        initial_dhw_c=25.0,
        away=away,
        away_until=T0.add(hours=return_in_h) if return_in_h is not None else None,
        away_room_c=17.0,
    )
    starts = [T0.add(hours=i) for i in range(hours)]
    return build_run_plan(
        params, starts, 1.0, [2.0] * hours, [0.0] * hours, prices or [0.0003] * hours
    )


def test_slots_before_the_return_use_the_away_settings():
    run = plan()
    assert run.away_slots == 18
    before, after = run.inputs[17], run.inputs[18]
    assert before.comfort_min_c == 17.0 and before.setpoint_c == 17.0
    assert before.comfort_max_c == after.comfort_max_c == 22.0  # PREHEAT may heat up
    assert before.dhw_off and before.draw_wh == 0.0
    assert not after.dhw_off and after.comfort_min_c > 19.0 and after.setpoint_c == 21.0
    # unknown return: the whole horizon is away
    assert plan(return_in_h=None).away_slots == 24
    # hot water stays on (heat pump keeps its hysteresis), but nobody taps while away
    on = plan(away_settings={"dhw_off": False})
    assert not on.inputs[0].dhw_off and on.inputs[0].draw_wh == 0.0
    # not away: nothing changes
    assert plan(away=False).away_slots == 0


def test_hot_water_off_means_no_reheat_and_no_tank_penalty():
    run = plan()
    state = ThermalState(slab_c=20.0, room_c=17.0, dhw_c=25.0)
    off = run.inputs[0]
    hold = run.system.step(state, off, HeatingMode.NORMAL, DhwMode.HOLD)
    assert hold.heat_dhw_hp_wh == 0.0 and hold.dhw_violation_kh == 0.0
    charge = run.system.step(state, off, HeatingMode.NORMAL, DhwMode.HEAT_PUMP)
    assert charge.heat_dhw_hp_wh > 0.0
    on = SlotInputs(outdoor_c=2.0, comfort_min_c=20.5, setpoint_c=21.0, comfort_max_c=22.0)
    normal = run.system.step(state, on, HeatingMode.NORMAL, DhwMode.HOLD)
    assert normal.heat_dhw_hp_wh > 0.0 and normal.forced_dhw_wh > 0.0  # own controller reheats


def test_status_quo_misses_the_return_and_heating_up_before_it_is_rewarded():
    run = plan()
    stay = run.evaluate(run.genes_for_modes([HeatingMode.NORMAL] * 24, [DhwMode.HOLD] * 24))
    heat_up = run.evaluate(
        run.genes_for_modes(
            [HeatingMode.NORMAL] * 8 + [HeatingMode.PREHEAT] * 10 + [HeatingMode.NORMAL] * 6,
            [DhwMode.HOLD] * 14 + [DhwMode.HEAT_PUMP] * 3 + [DhwMode.HOLD] * 7,
        )
    )
    # NORMAL while away only holds 17 degC: the room and the tank miss the return
    assert stay.comfort_kh > 1.0 and stay.dhw_kh > 1.0
    assert heat_up.comfort_kh < stay.comfort_kh
    assert heat_up.dhw_kh < 0.1 * stay.dhw_kh
    assert heat_up.extra_cost < stay.extra_cost
    detail = plan_result(run, run.genes_for_modes([HeatingMode.NORMAL] * 24, [DhwMode.HOLD] * 24))
    assert detail["away"] == [1.0] * 18 + [0.0] * 6
    assert detail["summary"]["away_slots"] == 18


def test_raised_heats_back_to_the_normal_setpoint_while_away():
    run = plan()
    assert run.inputs[0].raised_c == 21.0
    assert run.system._room_target(HeatingMode.RAISED, run.inputs[0]) == 21.0
    assert run.system._room_target(HeatingMode.NORMAL, run.inputs[0]) == 17.0
    assert run.system._room_target(HeatingMode.RAISED, run.inputs[20]) == 22.0  # at home
    # with the default modes (no PREHEAT) RAISED before the return is enough
    raised = run.evaluate(
        run.genes_for_modes(
            [HeatingMode.NORMAL] * 6 + [HeatingMode.RAISED] * 12 + [HeatingMode.NORMAL] * 6,
            [DhwMode.HOLD] * 15 + [DhwMode.HEAT_PUMP] * 2 + [DhwMode.HOLD] * 7,
        )
    )
    stay = run.evaluate(run.genes_for_modes([HeatingMode.NORMAL] * 24, [DhwMode.HOLD] * 24))
    assert raised.comfort_kh < 0.5 * stay.comfort_kh


def test_optimizer_plans_the_heat_up_before_the_return(config_eos: ConfigEOS):
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import (
        GeneticOptimizationParameters,
    )
    from akkudoktoreos.utils.datetimeutil import to_datetime

    hours = 24
    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 60,
                    "generations": 60,
                    "terminal_value_mode": "FIXED",
                }
            },
        }
    )
    start = to_datetime("2026-11-03T00:00:00")
    get_ems(init=True).set_start_datetime(start)
    device = HeatPumpCommonSettings(device_id="wbb12")
    prices = [0.0004] * 8 + [0.0001] * 8 + [0.0004] * 8  # cheap 8-16 h, return at 18 h
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * hours,
                "gesamtlast": [300.0] * hours,
                "strompreis_euro_pro_wh": prices,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [2.0] * hours,
            "pv_battery": None,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "wbb12",
                    "settings": device,
                    "initial_room_c": 17.0,
                    "initial_dhw_c": 25.0,
                    "away": True,
                    "away_until": start.add(hours=18),
                    "away_room_c": 17.0,
                }
            ],
        }
    )
    solution = GeneticOptimization(fixed_seed=4).optimize_ems(params, ngen=60, individuals=60)
    schedule = solution.heat_pumps["wbb12"]
    room, dhw, modes = schedule["room_c"], schedule["dhw_c"], schedule["heating_mode"]
    assert schedule["away"][:18] == [1.0] * 18
    # heated up and charged by the return, in the hours before it
    assert room[17] >= 20.0
    assert dhw[17] >= 38.0
    assert all(m == "NORMAL" or m == "REDUCED" for m in modes[:4])  # no early heat-up
    assert any(m == "RAISED" for m in modes[8:18])


def test_cooling_season_tolerates_a_warmer_house_while_away():
    run = plan(cooling={"enabled": True})
    assert run.inputs[0].comfort_max_c == 22.0  # heating season by default
    device = HeatPumpCommonSettings(device_id="wbb12", cooling={"enabled": True})
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device,
        initial_room_c=26.0,
        initial_dhw_c=45.0,
        cooling_season=True,
        away=True,
        away_until=T0.add(hours=10),
    )
    starts = [T0.add(hours=i) for i in range(24)]
    summer = build_run_plan(params, starts, 1.0, [30.0] * 24, [600.0] * 24, [0.0003] * 24)
    assert summer.inputs[0].setpoint_c == 28.0 and summer.inputs[0].comfort_max_c == 28.0
    assert summer.inputs[10].setpoint_c == 24.0


# ----------------------------------------------------------------------
# Measurements -> request
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


def _configure(config: ConfigEOS) -> None:
    config.merge_settings_from_dict(
        {
            "general": {"timezone": "Europe/Berlin"},
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
                "heat_pumps": {"wbb12": {}},
            },
        }
    )


def _request() -> ConfigOptimizationRequest:
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


@pytest.mark.asyncio
async def test_request_reads_holiday_mode_from_the_measurements(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    _configure(config_eos)
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz="Europe/Berlin")
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "wbb12-room-temperature", 18.0)
    await isolated_measurement.update_value(now, "wbb12-dhw-temperature", 30.0)
    # not away
    state = (await _request().resolve()).heat_pumps[0]
    assert not state.away
    # away with return in 30 h and the heating circuit's reduced setpoint
    await isolated_measurement.update_value(now.subtract(hours=20), "wbb12-away", 1.0)
    ret = now.add(hours=30)
    await isolated_measurement.update_value(
        now.subtract(hours=20), "wbb12-return-ts", ret.timestamp()
    )
    await isolated_measurement.update_value(
        now.subtract(days=3), "wbb12-away-room-temperature", 20.0
    )
    state = (await _request().resolve()).heat_pumps[0]
    assert state.away and state.away_room_c == 20.0
    assert state.away_until is not None and state.away_until.timestamp() == pytest.approx(
        ret.timestamp()
    )
    # return already passed: at home
    await isolated_measurement.update_value(
        now.subtract(minutes=1), "wbb12-return-ts", now.subtract(hours=1).timestamp()
    )
    assert not (await _request().resolve()).heat_pumps[0].away
    # return unknown (0): away for the whole horizon
    await isolated_measurement.update_value(now, "wbb12-return-ts", 0.0)
    state = (await _request().resolve()).heat_pumps[0]
    assert state.away and state.away_until is None
    # a stale away flag (older than max_age_hours) is ignored
    await isolated_measurement.delete_by_datetime(None, None)
    await isolated_measurement.update_value(now, "wbb12-room-temperature", 18.0)
    await isolated_measurement.update_value(now.subtract(hours=60), "wbb12-away", 1.0)
    assert not (await _request().resolve()).heat_pumps[0].away


# ----------------------------------------------------------------------
# Heat pump in standby while away (REDUCED may cool the house to a floor)
# ----------------------------------------------------------------------


def standby_plan(away_min_c=None, **away_settings):
    device = HeatPumpCommonSettings(device_id="wbb12", away=away_settings)
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device,
        initial_room_c=19.0,
        initial_dhw_c=40.0,
        away=True,
        away_until=T0.add(hours=40),
        away_room_c=19.0,
        away_min_c=away_min_c,
    )
    starts = [T0.add(hours=i) for i in range(48)]
    return build_run_plan(params, starts, 1.0, [0.0] * 48, [0.0] * 48, [0.0003] * 48)


def test_standby_floor_lets_reduced_cool_the_house_down():
    held = standby_plan()
    assert held.inputs[0].comfort_min_c == 19.0  # floor = away temperature
    floor = standby_plan(away_min_c=16.0)
    assert floor.inputs[0].comfort_min_c == 16.0
    assert floor.inputs[0].setpoint_c == 19.0  # NORMAL still holds the away temperature
    assert floor.inputs[40].comfort_min_c > 19.0  # at home again
    configured = standby_plan(standby_min_c=15.0)
    assert configured.inputs[0].comfort_min_c == 15.0
    # REDUCED in standby: hardly any heat while the house is above the floor
    reduced = floor.evaluate(floor.genes_for_modes([HeatingMode.REDUCED] * 48, [DhwMode.HOLD] * 48))
    normal = floor.evaluate(floor.genes_for_modes([HeatingMode.NORMAL] * 48, [DhwMode.HOLD] * 48))
    assert sum(reduced.electric_wh[:20]) < 0.5 * sum(normal.electric_wh[:20])
    room = plan_result(
        floor, floor.genes_for_modes([HeatingMode.REDUCED] * 48, [DhwMode.HOLD] * 48)
    )["room_c"]
    assert min(room[:40]) >= 15.5  # the floor holds while away
    assert room[20] < 18.5  # and the house really cools down


@pytest.mark.asyncio
async def test_request_reads_the_standby_floor(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    _configure(config_eos)
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz="Europe/Berlin")
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "wbb12-room-temperature", 18.0)
    await isolated_measurement.update_value(now, "wbb12-dhw-temperature", 30.0)
    await isolated_measurement.update_value(
        now.subtract(days=10), "wbb12-away-min-temperature", 16.0
    )
    # at home: no floor
    assert (await _request().resolve()).heat_pumps[0].away_min_c is None
    await isolated_measurement.update_value(now.subtract(hours=2), "wbb12-away", 1.0)
    state = (await _request().resolve()).heat_pumps[0]
    assert state.away and state.away_min_c == 16.0
    await isolated_measurement.update_value(now, "wbb12-away-min-temperature", 2.0)  # implausible
    assert (await _request().resolve()).heat_pumps[0].away_min_c is None
