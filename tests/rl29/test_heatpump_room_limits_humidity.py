"""Per-room margins and humidity protection reported by the controller (fork).

EOS models the mean of all rooms. The controller reports how close the coldest room
is to its own lower limit (``<id>-room-min-margin``) and the warmest room to its
upper limit (``<id>-room-max-margin``), plus the highest relative humidity
(``<id>-humidity``). All three are optional.
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


def device(**settings) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings(device_id="hp1", **settings)


def run_plan(hours=24, slot_hours=1.0, room=21.0, **kwargs):
    settings = kwargs.pop("settings", device())
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=settings,
        initial_room_c=room,
        initial_dhw_c=45.0,
        **kwargs,
    )
    n = int(hours / slot_hours)
    starts = [NOON.add(minutes=int(i * slot_hours * 60)) for i in range(n)]
    return build_run_plan(params, starts, slot_hours, [5.0] * n, [0.0] * n, [0.0003] * n)


ROOMS = {"room_limits": {"enabled": True}}


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def test_measurement_keys_only_with_the_features():
    assert "hp1-room-min-margin" not in device().measurement_keys
    assert "hp1-humidity" not in device().measurement_keys
    both = device(room_limits={"enabled": True}, humidity={"enabled": True})
    assert {"hp1-room-min-margin", "hp1-room-max-margin", "hp1-humidity"} <= set(
        both.measurement_keys
    )


def test_unreported_margins_change_nothing():
    plain = run_plan()
    with_feature = run_plan(settings=device(**ROOMS))
    assert [i.comfort_min_c for i in plain.inputs] == [i.comfort_min_c for i in with_feature.inputs]
    assert [i.comfort_max_c for i in plain.inputs] == [i.comfort_max_c for i in with_feature.inputs]


# ----------------------------------------------------------------------
# Room margins
# ----------------------------------------------------------------------


def test_cold_room_raises_the_minimum_of_the_mean_near_now():
    """Mean 21.0, coldest room 0.2 K above its limit: the mean must stay >= 20.8."""
    run = run_plan(settings=device(**ROOMS), room_min_margin_k=0.2)
    assert run.inputs[0].comfort_min_c == pytest.approx(20.8)
    assert run.inputs[5].comfort_min_c == pytest.approx(20.8)
    assert run.inputs[6].comfort_min_c == pytest.approx(20.5)  # after horizon_hours
    plain = run_plan()
    genes = plain.genes_for_modes([HeatingMode.REDUCED] * 24, [DhwMode.HOLD] * 24)
    # REDUCED may only cool the mean to 20.8 now: the controller logic heats earlier
    assert sum(run.evaluate(genes).electric_wh[:6]) > sum(plain.evaluate(genes).electric_wh[:6])


def test_ample_margin_keeps_the_band():
    run = run_plan(settings=device(**ROOMS), room_min_margin_k=1.5)
    assert run.inputs[0].comfort_min_c == pytest.approx(20.5)  # never lowers the band


def test_room_below_its_limit_is_capped_by_max_raise_and_setpoint():
    settings = device(room_limits={"enabled": True, "max_raise_k": 0.3})
    run = run_plan(settings=settings, room_min_margin_k=-2.0)
    assert run.inputs[0].comfort_min_c == pytest.approx(20.8)  # 20.5 + 0.3
    run = run_plan(settings=device(**ROOMS), room_min_margin_k=-2.0)
    assert run.inputs[0].comfort_min_c == pytest.approx(21.0)  # not above the setpoint


def test_warm_room_lowers_the_maximum_and_raised_target():
    """Mean 21.0, warmest room 0.4 K below its upper limit: mean at most 21.4."""
    run = run_plan(settings=device(**ROOMS), room_max_margin_k=0.4)
    first = run.inputs[0]
    assert first.comfort_max_c == pytest.approx(21.4)
    assert first.raised_c == pytest.approx(21.4)
    assert run.inputs[8].comfort_max_c == pytest.approx(22.0)
    # never below the NORMAL setpoint
    run = run_plan(settings=device(**ROOMS), room_max_margin_k=-1.0)
    assert run.inputs[0].comfort_max_c == pytest.approx(21.0)


def test_quarter_hour_slots_use_hours_for_the_horizon():
    run = run_plan(settings=device(**ROOMS), slot_hours=0.25, room_min_margin_k=0.2)
    assert run.inputs[23].comfort_min_c == pytest.approx(20.8)
    assert run.inputs[24].comfort_min_c == pytest.approx(20.5)


def test_margins_stop_at_the_return_from_holiday():
    run = run_plan(
        settings=device(**ROOMS),
        room=18.0,
        room_min_margin_k=0.3,
        away=True,
        away_until=NOON.add(hours=2),
        away_room_c=18.0,
        away_min_c=16.0,
    )
    assert run.inputs[0].comfort_min_c == pytest.approx(17.7)  # holiday floor 16 -> 17.7
    assert run.inputs[2].comfort_min_c == pytest.approx(20.5)  # back home, normal band


# ----------------------------------------------------------------------
# Humidity
# ----------------------------------------------------------------------


def test_high_humidity_holds_the_rooms_near_the_setpoint():
    settings = device(humidity={"enabled": True, "max_pct": 65.0})
    run = run_plan(settings=settings, humidity_pct=70.0)
    assert run.inputs[0].comfort_min_c == pytest.approx(20.5)  # setpoint 21 - 0.5
    night = run_plan(settings=settings, hours=24, humidity_pct=70.0)
    assert night.inputs[2].comfort_min_c == pytest.approx(20.5)  # only hold_hours
    assert run_plan(settings=settings, humidity_pct=60.0).inputs[0].comfort_min_c == 20.5


def test_humidity_hold_blocks_reduction_at_night():
    midnight = pendulum.datetime(2026, 11, 6, 0, 0, tz=TZ)  # comfort minimum 19.5 at night
    settings = device(humidity={"enabled": True, "hold_hours": 3.0})
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=settings,
        initial_room_c=21.0,
        initial_dhw_c=45.0,
        humidity_pct=100.0,  # debounced flag from the controller
    )
    starts = [midnight.add(hours=i) for i in range(6)]
    run = build_run_plan(params, starts, 1.0, [5.0] * 6, [0.0] * 6, [0.0003] * 6)
    assert [i.comfort_min_c for i in run.inputs[:3]] == [pytest.approx(20.5)] * 3
    assert run.inputs[3].comfort_min_c == pytest.approx(19.5)
    off = run_plan(settings=device(), humidity_pct=100.0)
    assert off.inputs[0].comfort_min_c == pytest.approx(20.5)  # feature disabled: band only


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
async def test_request_reads_margins_and_humidity(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest

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
                "heat_pumps": {
                    "hp1": {"room_limits": {"enabled": True}, "humidity": {"enabled": True}}
                },
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "hp1-room-temperature", 20.5)
    await isolated_measurement.update_value(now, "hp1-dhw-temperature", 45.0)
    await isolated_measurement.update_value(now, "hp1-room-min-margin", 0.4)
    await isolated_measurement.update_value(now, "hp1-humidity", 71.0)

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

    state = (await request().resolve()).heat_pumps[0]
    assert state.room_min_margin_k == 0.4
    assert state.room_max_margin_k is None  # optional, not reported
    assert state.humidity_pct == 71.0
    # implausible values are ignored
    await isolated_measurement.update_value(now, "hp1-room-min-margin", 40.0)
    await isolated_measurement.update_value(now, "hp1-humidity", 140.0)
    state = (await request().resolve()).heat_pumps[0]
    assert state.room_min_margin_k is None and state.humidity_pct is None
