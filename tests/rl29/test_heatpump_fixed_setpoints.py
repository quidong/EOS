"""Fixed room setpoints (RL29 fork).

Nothing changes the room controllers' setpoint: EOS acts only through SG Ready and
forced valve positions. RAISED then keeps the setpoint, and a single zone cannot be
reduced on its own.
"""

from __future__ import annotations

import pytest

import pendulum

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
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
    plan_result,
)

MONDAY = pendulum.datetime(2026, 11, 2, 0, 0, tz="Europe/Berlin")
ZONES = {
    "enabled": True,
    "zones": [
        {
            "zone_id": "og",
            "share": 0.33,
            "building": {
                "slab_capacity_wh_per_k": 2200,
                "room_capacity_wh_per_k": 3300,
                "loss_w_per_k": 60,
                "slab_room_w_per_k": 500,
                "emitter_w_per_k": 330,
            },
        },
        {
            "zone_id": "eg",
            "share": 0.67,
            "building": {
                "slab_capacity_wh_per_k": 1300,
                "room_capacity_wh_per_k": 6700,
                "loss_w_per_k": 120,
                "slab_room_w_per_k": 1000,
                "emitter_w_per_k": 670,
            },
        },
    ],
    "couplings": [{"zone_a": "og", "zone_b": "eg", "w_per_k": 100}],
}


def device(**settings) -> HeatPumpCommonSettings:
    settings.setdefault("optimization", {"heating_modes": [0, 1, 2, 3, 4]})
    return HeatPumpCommonSettings.model_validate({"device_id": "hp1", "zones": ZONES, **settings})


def zone_plan(settings=None, hours=24, **kwargs):
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=settings or device(),
        initial_room_c=20.6,
        initial_dhw_c=45.0,
        **kwargs,
    )
    start = MONDAY.add(hours=12)
    starts = [start.add(hours=i) for i in range(hours)]
    return build_run_plan(params, starts, 1.0, [2.0] * hours, [0.0] * hours, [0.0003] * hours)


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


def test_preheat_through_the_controllers_keeps_the_setpoint():
    assert system(True)._room_target(P, inputs()) == pytest.approx(21.0)
    assert system(False)._room_target(P, inputs()) == pytest.approx(23.0)


def test_other_modes_are_unchanged():
    for mode in (R, N, F):
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


# ----------------------------------------------------------------------
# SG Ready 3/4 as heating curve shift (unit.raised/forced_flow_offset_k)
# ----------------------------------------------------------------------


def shifted(
    raised: float | None = None, forced: float | None = None, preheat_forced: bool = False
) -> HeatPumpSystem:
    return HeatPumpSystem(
        HOUSE,
        TANK,
        CopModel(WBB12_RATING_POINTS),
        slot_hours=0.25,
        heat_pump=HeatPumpParameters(
            raised_flow_offset_k=raised,
            forced_flow_offset_k=forced,
            preheat_valves_forced=preheat_forced,
        ),
        comfort=ComfortParameters(fixed_room_setpoints=True),
    )


def space_w(hp: HeatPumpSystem, mode: HeatingMode, slab_c: float, room_c: float, **kw) -> float:
    state = ThermalState(slab_c=slab_c, room_c=room_c, dhw_c=55.0)
    return hp.step(state, inputs(**kw), mode).heat_space_wh / hp.dt


def test_without_offsets_raised_equals_normal_with_fixed_setpoints():
    hp = shifted()
    assert space_w(hp, A, 24.0, 20.5) == pytest.approx(space_w(hp, N, 24.0, 20.5))


def test_raised_offset_adds_emitter_heat_while_the_valves_are_open():
    hp = shifted(raised=3.0)
    normal = space_w(hp, N, 24.0, 20.5)
    assert space_w(hp, A, 24.0, 20.5) == pytest.approx(normal + 3.0 * HOUSE.emitter_w_per_k)


def test_raised_offset_adds_nothing_once_the_room_reached_the_setpoint():
    hp = shifted(raised=3.0)
    assert space_w(hp, A, 24.0, 21.2) == pytest.approx(space_w(hp, N, 24.0, 21.2))


def test_forced_offset_heats_along_the_curve_instead_of_full_power():
    hp = shifted(forced=0.5)
    mean = hp.curve_mean_flow_c(inputs(), 0.5)
    expected = (mean - 22.0) * HOUSE.emitter_w_per_k
    assert space_w(hp, F, 22.0, 21.5) == pytest.approx(expected)
    full = space_w(shifted(), F, 22.0, 21.5)
    assert expected < full


def test_forced_curve_heat_shrinks_with_a_warm_screed():
    hp = shifted(forced=0.5)
    assert space_w(hp, F, 28.0, 21.5) < space_w(hp, F, 23.0, 21.5)


def test_forced_stops_at_comfort_maximum_plus_overshoot():
    hp = shifted(forced=0.5)
    assert space_w(hp, F, 22.0, 24.1) == pytest.approx(0.0)


def test_curve_follows_the_outdoor_temperature():
    hp = shifted()
    assert hp.curve_mean_flow_c(inputs(outdoor_c=-5.0), 0.0) > hp.curve_mean_flow_c(inputs(), 0.0)
    assert hp.curve_mean_flow_c(inputs(), 3.0) == pytest.approx(
        hp.curve_mean_flow_c(inputs(), 0.0) + 3.0
    )


def test_preheat_with_forced_valves_heats_past_the_setpoint():
    open_valves = shifted(raised=3.0)
    forced = shifted(raised=3.0, preheat_forced=True)
    # Room above the setpoint: the controllers close, forced valves keep heating.
    assert space_w(open_valves, P, 22.0, 21.6) == pytest.approx(space_w(open_valves, N, 22.0, 21.6))
    assert space_w(forced, P, 22.0, 21.6) > space_w(forced, N, 22.0, 21.6) + 1000.0


def test_zones_share_one_curve_with_forced_valves():
    zones = [ZoneParameters("og", UP, weight=80), ZoneParameters("eg", DOWN, weight=160)]
    hp = MultiZoneHeatPump(shifted(forced=0.5), zones, [ZoneCoupling(0, 1, 100.0)])
    state = hp.initial_state([21.0, 21.0], dhw_c=55.0, outdoor_c=2.0, slabs=[23.0, 24.0])
    result = hp.step(state, inputs(), [F, F])
    mean = hp.system.curve_mean_flow_c(inputs(), 0.5)
    heats = [wh / hp.system.dt for wh in result.zone_heat_wh]
    assert heats[0] == pytest.approx((mean - 23.0) * UP.emitter_w_per_k)
    assert heats[1] == pytest.approx((mean - 24.0) * DOWN.emitter_w_per_k)


def test_flow_offsets_reach_the_simulator():
    settings = device(
        unit={"raised_flow_offset_k": 3.0, "forced_flow_offset_k": 0.5},
        comfort={"fixed_room_setpoints": True},
    )
    hp = zone_plan(settings=settings).system.hp
    assert hp.raised_flow_offset_k == pytest.approx(3.0)
    assert hp.forced_flow_offset_k == pytest.approx(0.5)
    assert not hp.preheat_valves_forced
    assert zone_plan().system.hp.forced_flow_offset_k is None


def test_milp_mode_heat_follows_the_curve_shift():
    from akkudoktoreos.optimization.milp.thermal import FORCED_HEAT_W, mode_control

    def control(unit):
        settings = HeatPumpCommonSettings.model_validate(
            {
                "device_id": "hp1",
                "unit": unit,
                "comfort": {"fixed_room_setpoints": True},
                "optimization": {"heating_modes": [0, 1, 2, 3, 4]},
            }
        )
        plan = zone_plan(settings=settings, hours=4)
        return plan, mode_control(plan, [20.6] * plan.n_slots)

    plan, plain = control({})
    _, offset = control({"raised_flow_offset_k": 3.0, "forced_flow_offset_k": 0.5})
    modes = plain.modes
    emitter = plan.system.building.params.emitter_w_per_k
    raised, forced = modes.index(int(A)), modes.index(int(F))
    assert plain.const_w[0][forced] == FORCED_HEAT_W
    assert offset.const_w[0][forced] < FORCED_HEAT_W
    assert offset.const_w[0][raised] == pytest.approx(plain.const_w[0][raised] + 3.0 * emitter)
