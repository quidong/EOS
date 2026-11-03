"""Fixed room setpoints (RL29 fork).

Nothing changes the room controllers' setpoint: EOS acts only through SG Ready and
forced valve positions. RAISED then keeps the setpoint, and a single zone cannot be
reduced on its own.
"""

from __future__ import annotations

import pytest

from akkudoktoreos.devices.thermal.building import BuildingParameters
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel
from akkudoktoreos.devices.thermal.dhw import DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import (
    ComfortParameters,
    HeatingMode,
    HeatPumpSystem,
    SlotInputs,
)
from akkudoktoreos.devices.thermal.zones import (
    MultiZoneHeatPump,
    ZoneCoupling,
    ZoneParameters,
)

from tests.rl29.test_heatpump_zones_plan import device, plan_result, zone_plan

HOUSE = BuildingParameters(
    slab_capacity_wh_per_k=3500,
    room_capacity_wh_per_k=10000,
    loss_w_per_k=180,
    slab_room_w_per_k=1500,
    emitter_w_per_k=1000,
)
UP = BuildingParameters(2200, 3300, 60, 500, 330)
DOWN = BuildingParameters(1300, 6700, 120, 1000, 670)
TANK = DhwTankParameters(volume_l=190, loss_w_per_k=1.5, ambient_c=18)
R, N, A, P, F = (
    HeatingMode.REDUCED,
    HeatingMode.NORMAL,
    HeatingMode.RAISED,
    HeatingMode.PREHEAT,
    HeatingMode.FORCED,
)


def system(fixed: bool) -> HeatPumpSystem:
    return HeatPumpSystem(
        HOUSE,
        TANK,
        CopModel(WBB12_RATING_POINTS),
        slot_hours=0.25,
        comfort=ComfortParameters(fixed_room_setpoints=fixed),
    )


def inputs(**kw) -> SlotInputs:
    base = {"outdoor_c": 2.0, "comfort_min_c": 19.5, "setpoint_c": 21.0, "comfort_max_c": 23.0}
    base.update(kw)
    return SlotInputs(**base)


def two_zones(fixed: bool) -> MultiZoneHeatPump:
    zones = [ZoneParameters("og", UP, weight=80), ZoneParameters("eg", DOWN, weight=160)]
    return MultiZoneHeatPump(system(fixed), zones, [ZoneCoupling(0, 1, 100.0)])


# ----------------------------------------------------------------------
# Single zone
# ----------------------------------------------------------------------


def test_raised_keeps_the_setpoint():
    assert system(True)._room_target(A, inputs()) == pytest.approx(21.0)
    assert system(False)._room_target(A, inputs()) == pytest.approx(22.0)


def test_other_modes_are_unchanged():
    for mode in (R, N, P, F):
        assert system(True)._room_target(mode, inputs()) == pytest.approx(
            system(False)._room_target(mode, inputs())
        )


# ----------------------------------------------------------------------
# Zones
# ----------------------------------------------------------------------


def test_a_single_reduced_zone_runs_normal():
    hp = two_zones(True)
    state = hp.initial_state([20.0, 21.0], dhw_c=45.0, outdoor_c=2.0)
    result = hp.step(state, inputs(), [N, R])
    assert result.zone_modes == [N, N]
    assert result.heating_mode == N


def test_a_reduced_zone_next_to_a_forced_one_regulates():
    hp = two_zones(True)
    state = hp.initial_state([19.0, 21.0], dhw_c=45.0, outdoor_c=2.0)
    result = hp.step(state, inputs(), [F, R])
    assert result.zone_modes == [F, N]
    assert result.heating_mode == F


def test_all_zones_reduced_stays_reduced():
    hp = two_zones(True)
    state = hp.initial_state([21.0, 21.0], dhw_c=45.0, outdoor_c=2.0)
    result = hp.step(state, inputs(), [R, R])
    assert result.zone_modes == [R, R]
    assert result.heating_mode == R


def test_without_fixed_setpoints_a_zone_can_be_reduced():
    hp = two_zones(False)
    state = hp.initial_state([21.0, 21.0], dhw_c=45.0, outdoor_c=2.0)
    result = hp.step(state, inputs(), [N, R])
    assert result.zone_modes == [N, R]


def test_a_reduced_zone_gets_the_same_heat_as_a_normal_one():
    hp = two_zones(True)
    state = hp.initial_state([20.0, 20.5], dhw_c=45.0, outdoor_c=0.0)
    cold = inputs(outdoor_c=0.0)
    reduced = hp.step(state, cold, [N, R])
    normal = hp.step(state, cold, [N, N])
    assert reduced.zone_heat_wh == pytest.approx(normal.zone_heat_wh)


# ----------------------------------------------------------------------
# Settings and solution
# ----------------------------------------------------------------------


def test_setting_reaches_the_simulator():
    settings = device(comfort={"fixed_room_setpoints": True})
    plan = zone_plan(settings=settings)
    assert plan.system.comfort.fixed_room_setpoints
    assert not zone_plan().system.comfort.fixed_room_setpoints


def test_solution_reports_what_the_zone_runs():
    plan = zone_plan(
        settings=device(comfort={"fixed_room_setpoints": True}),
        zone_room_c={"og": 19.3, "eg": 21.0},
    )
    n = plan.n_periods
    og = [plan.heating_index(F)] * 4 + [plan.heating_index(N)] * (n - 4)
    eg = [plan.heating_index(R)] * n
    result = plan_result(plan, og + eg + [0] * n)
    assert result["zone_og_heating_mode"][:4] == ["FORCED"] * 4
    assert result["zone_eg_heating_mode"][:4] == ["NORMAL"] * 4
    assert "REDUCED" not in result["zone_eg_heating_mode"][4:]
