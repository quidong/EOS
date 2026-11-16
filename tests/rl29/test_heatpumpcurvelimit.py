"""Heating curve limits open valves (RL29 fork, ``unit.curve_limits_open_valves``).

With fixed room setpoints the heat pump holds its weather-compensated curve: a room
deficit opens the valves but does not raise the flow. Open valves therefore take at
most ``emitter_w_per_k * (curve - screed)``; SG Ready 3 adds its curve shift, forced
valves (PREHEAT with forced valves, FORCED) are unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.building import BuildingParameters
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel
from akkudoktoreos.devices.thermal.dhw import DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import (
    ComfortParameters,
    HeatingMode,
    HeatPumpParameters,
    HeatPumpSystem,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.devices.thermal.zones import (
    MultiZoneHeatPump,
    ZoneCoupling,
    ZoneParameters,
)

HOUSE = BuildingParameters(
    slab_capacity_wh_per_k=3500,
    room_capacity_wh_per_k=10000,
    loss_w_per_k=180,
    slab_room_w_per_k=1500,
    emitter_w_per_k=1000,
)
UP = BuildingParameters(2200, 3300, 80, 500, 330)
DOWN = BuildingParameters(1300, 6700, 60, 1000, 670)
TANK = DhwTankParameters(volume_l=190, loss_w_per_k=1.5, ambient_c=18)
N, A, P = HeatingMode.NORMAL, HeatingMode.RAISED, HeatingMode.PREHEAT


def system(limit: bool, raised: float | None = 3.0, preheat_forced: bool = True) -> HeatPumpSystem:
    return HeatPumpSystem(
        HOUSE,
        TANK,
        CopModel(WBB12_RATING_POINTS),
        slot_hours=0.25,
        heat_pump=HeatPumpParameters(
            raised_flow_offset_k=raised,
            forced_flow_offset_k=3.0,
            preheat_valves_forced=preheat_forced,
            curve_limits_open_valves=limit,
            max_flow_c=45.0,
        ),
        comfort=ComfortParameters(fixed_room_setpoints=True),
    )


def inputs(**kw: float) -> SlotInputs:
    base: dict[str, Any] = {"outdoor_c": 2.0, "comfort_min_c": 20.5, "setpoint_c": 21.0, "comfort_max_c": 23.0}
    base.update(kw)
    return SlotInputs(**base)


def space_w(hp: HeatPumpSystem, mode: HeatingMode, slab_c: float, room_c: float) -> float:
    state = ThermalState(slab_c=slab_c, room_c=room_c, dhw_c=55.0)
    return hp.step(state, inputs(), mode).heat_space_wh / hp.dt


def curve_w(hp: HeatPumpSystem, slab_c: float, shift: float = 0.0) -> float:
    return max(0.0, (hp.curve_mean_flow_c(inputs(), shift) - slab_c) * HOUSE.emitter_w_per_k)


# ----------------------------------------------------------------------
# Single zone
# ----------------------------------------------------------------------


def test_normal_recovers_a_cold_room_only_along_the_curve():
    """After airing (room 19 degC) NORMAL takes the curve heat, not up to max flow."""
    limited, free = system(True), system(False)
    slab = 24.0
    assert space_w(limited, N, slab, 19.0) == pytest.approx(curve_w(limited, slab))
    assert space_w(free, N, slab, 19.0) > space_w(limited, N, slab, 19.0) + 2000.0


def test_raised_adds_the_curve_shift_to_a_cold_room():
    hp = system(True)
    slab = 24.0
    assert space_w(hp, A, slab, 19.0) == pytest.approx(curve_w(hp, slab, 3.0))
    assert space_w(hp, A, slab, 19.0) > space_w(hp, N, slab, 19.0) + 2900.0


def test_preheat_with_forced_valves_heats_along_the_shifted_curve():
    hp = system(True)
    slab = 24.0
    assert space_w(hp, P, slab, 19.0) == pytest.approx(curve_w(hp, slab, 3.0))
    # Above the setpoint the controllers close, forced valves keep heating.
    assert space_w(hp, P, slab, 21.6) > space_w(hp, N, slab, 21.6) + 1000.0


def test_controllers_still_throttle_a_warm_room():
    """Near the setpoint the controller demand is below the curve: no change."""
    limited, free = system(True), system(False)
    slab = hp_slab = limited.building.steady_slab_c(21.0, 2.0, 0.0)
    assert space_w(limited, N, slab, 21.0) == pytest.approx(space_w(free, N, hp_slab, 21.0))


def test_comfort_floor_does_not_raise_the_flow():
    """The repair floor below comfort minimum is limited by the curve too."""
    hp = system(True)
    slab = 23.0
    state = ThermalState(slab_c=slab, room_c=19.0, dhw_c=55.0)
    result = hp.step(state, inputs(), HeatingMode.REDUCED)
    assert result.heat_space_wh / hp.dt <= curve_w(hp, slab) + 1e-6


def test_slower_recovery_makes_raised_worth_it():
    """Three hours after airing: RAISED leaves the room warmer than NORMAL."""
    hp = system(True)
    rooms = {}
    for mode in (N, A):
        state = ThermalState(slab_c=24.0, room_c=19.0, dhw_c=55.0)
        for _ in range(12):
            state = hp.step(state, inputs(), mode).state
        rooms[mode] = state.room_c
    assert rooms[A] > rooms[N] + 0.1


# ----------------------------------------------------------------------
# Zones and settings
# ----------------------------------------------------------------------


def two_zones(limit: bool) -> MultiZoneHeatPump:
    zones = [ZoneParameters("og", UP, weight=80), ZoneParameters("eg", DOWN, weight=160)]
    return MultiZoneHeatPump(system(limit), zones, [ZoneCoupling(0, 1, 100.0)])


def test_each_zone_takes_at_most_its_curve_heat():
    hp = two_zones(True)
    state = hp.initial_state([19.0, 21.0], dhw_c=55.0, outdoor_c=2.0, slabs=[24.0, 24.0])
    result = hp.step(state, inputs(), [N, N])
    mean = hp.system.curve_mean_flow_c(inputs(), 0.0)
    heats = [wh / hp.system.dt for wh in result.zone_heat_wh]
    assert heats[0] <= (mean - 24.0) * UP.emitter_w_per_k + 1e-6
    assert heats[1] <= (mean - 24.0) * DOWN.emitter_w_per_k + 1e-6


def test_cold_upper_zone_recovers_slower_with_the_curve_limit():
    rooms = {}
    for limit in (True, False):
        hp = two_zones(limit)
        state = hp.initial_state([19.0, 21.0], dhw_c=55.0, outdoor_c=2.0, slabs=[24.0, 24.0])
        for _ in range(8):
            state = hp.step(state, inputs(), [N, N]).state
        rooms[limit] = state.rooms[0]
    assert rooms[True] < rooms[False] - 0.1


def test_setting_reaches_the_simulator():
    settings = HeatPumpCommonSettings.model_validate(
        {
            "device_id": "hp1",
            "unit": {"raised_flow_offset_k": 3.0, "curve_limits_open_valves": True},
            "comfort": {"fixed_room_setpoints": True},
        }
    )
    assert settings.unit.curve_limits_open_valves is True
    assert settings.to_system(slot_hours=0.25).hp.curve_limits_open_valves is True
    default = HeatPumpCommonSettings.model_validate({"device_id": "hp1"})
    assert default.unit.curve_limits_open_valves is False


def test_off_by_default_keeps_the_controller_law():
    hp = system(False)
    assert hp.open_valve_cap_w(N, inputs(), 24.0, HOUSE.emitter_w_per_k) is None
