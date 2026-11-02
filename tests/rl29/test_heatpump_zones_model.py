"""Multi-zone space heating model (eos-plus fork, phase 1).

Two zones like the reference house: upstairs a heavy wet screed (slow), downstairs
plus basement a light dry screed (fast), both on one heating circuit.
"""

from __future__ import annotations

import pytest

from akkudoktoreos.devices.thermal.building import BuildingModel, BuildingParameters
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel
from akkudoktoreos.devices.thermal.dhw import DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import (
    DhwMode,
    HeatingMode,
    HeatPumpSystem,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.devices.thermal.zones import (
    MultiZoneBuildingModel,
    MultiZoneHeatPump,
    ZoneCoupling,
    ZoneParameters,
    aggregate_heating_mode,
)

HOUSE = BuildingParameters(
    slab_capacity_wh_per_k=3500,
    room_capacity_wh_per_k=10000,
    loss_w_per_k=180,
    slab_room_w_per_k=1500,
    emitter_w_per_k=1000,
)
UP = BuildingParameters(  # wet screed, 75 m2: heavy
    slab_capacity_wh_per_k=2500,
    room_capacity_wh_per_k=4000,
    loss_w_per_k=80,
    slab_room_w_per_k=600,
    emitter_w_per_k=400,
)
DOWN = BuildingParameters(  # dry screed, 115 m2: light
    slab_capacity_wh_per_k=700,
    room_capacity_wh_per_k=6000,
    loss_w_per_k=100,
    slab_room_w_per_k=900,
    emitter_w_per_k=600,
)
TANK = DhwTankParameters(volume_l=190, loss_w_per_k=1.5, ambient_c=18)


def system(building: BuildingParameters = HOUSE) -> HeatPumpSystem:
    return HeatPumpSystem(building, TANK, CopModel(WBB12_RATING_POINTS), slot_hours=0.25)


def inputs(**kw) -> SlotInputs:
    base = {"outdoor_c": 2.0, "comfort_min_c": 20.0, "setpoint_c": 21.0, "comfort_max_c": 22.0}
    base.update(kw)
    return SlotInputs(**base)


def two_zones(coupling: float = 150.0) -> MultiZoneHeatPump:
    zones = [ZoneParameters("og", UP, weight=75), ZoneParameters("eg", DOWN, weight=115)]
    couplings = [ZoneCoupling(0, 1, coupling)] if coupling else []
    return MultiZoneHeatPump(system(), zones, couplings)


def run(hp: MultiZoneHeatPump, state, modes, slots, dhw=DhwMode.HOLD, slot_inputs=None):
    results = []
    for k in range(slots):
        zone_modes = modes(k) if callable(modes) else modes
        result = hp.step(state, slot_inputs or inputs(), zone_modes, dhw)
        state = result.state
        results.append(result)
    return state, results


# ----------------------------------------------------------------------
# Building model
# ----------------------------------------------------------------------


def test_one_zone_equals_the_single_zone_model():
    single = BuildingModel(HOUSE, 0.25)
    multi = MultiZoneBuildingModel([ZoneParameters("all", HOUSE)], slot_hours=0.25)
    s1, r1 = single.step(25.0, 20.0, 3000.0, 2.0, 100.0)
    (s2,), (r2,) = multi.step([25.0], [20.0], [3000.0], 2.0, [100.0])
    assert s2 == pytest.approx(s1, abs=1e-9)
    assert r2 == pytest.approx(r1, abs=1e-9)


def test_coupling_moves_heat_from_the_warm_to_the_cold_zone():
    coupled = MultiZoneBuildingModel(
        [ZoneParameters("og", UP), ZoneParameters("eg", DOWN)], [ZoneCoupling(0, 1, 200.0)]
    )
    apart = MultiZoneBuildingModel([ZoneParameters("og", UP), ZoneParameters("eg", DOWN)])
    slabs, rooms = [20.0, 22.0], [19.0, 22.0]
    _, warm = coupled.step(slabs, rooms, [0.0, 0.0], 2.0, [0.0, 0.0])
    _, alone = apart.step(slabs, rooms, [0.0, 0.0], 2.0, [0.0, 0.0])
    assert warm[0] > alone[0]  # the cold zone gets heat from the warm one
    assert warm[1] < alone[1]


def test_invalid_couplings_are_rejected():
    zones = [ZoneParameters("a", UP), ZoneParameters("b", DOWN)]
    with pytest.raises(ValueError):
        MultiZoneBuildingModel(zones, [ZoneCoupling(0, 0, 10.0)])
    with pytest.raises(ValueError):
        MultiZoneBuildingModel(zones, [ZoneCoupling(0, 2, 10.0)])
    with pytest.raises(ValueError):
        MultiZoneBuildingModel(zones, [ZoneCoupling(0, 1, -1.0)])
    with pytest.raises(ValueError):
        MultiZoneBuildingModel([])


def test_the_dry_screed_zone_reacts_faster():
    """Same heat per emitter, the light screed lifts its room sooner."""
    model = MultiZoneBuildingModel([ZoneParameters("og", UP), ZoneParameters("eg", DOWN)])
    slabs, rooms = [20.0, 20.0], [20.0, 20.0]
    for _ in range(8):  # two hours, heat in proportion to the losses
        slabs, rooms = model.step(slabs, rooms, [80 * 20.0, 100 * 20.0], 2.0, [0.0, 0.0])
    assert rooms[1] - 20.0 > rooms[0] - 20.0


# ----------------------------------------------------------------------
# Heat pump mode from the zone modes
# ----------------------------------------------------------------------


def test_aggregate_heating_mode():
    R, N, A, P, F = (
        HeatingMode.REDUCED,
        HeatingMode.NORMAL,
        HeatingMode.RAISED,
        HeatingMode.PREHEAT,
        HeatingMode.FORCED,
    )
    assert aggregate_heating_mode([R, R]) == R
    assert aggregate_heating_mode([R, N]) == N
    assert aggregate_heating_mode([N, A]) == A
    assert aggregate_heating_mode([A, P]) == P
    assert aggregate_heating_mode([R, F]) == F  # one zone forced, the other closed
    assert aggregate_heating_mode([]) == N


# ----------------------------------------------------------------------
# Distribution of the heat
# ----------------------------------------------------------------------


def test_a_forced_zone_gets_the_heat_while_the_warm_zone_closes():
    hp = two_zones()
    state = hp.initial_state([19.3, 21.6], dhw_c=45.0, outdoor_c=2.0)
    state, results = run(hp, state, [HeatingMode.FORCED, HeatingMode.NORMAL], 8)
    heat_up = sum(r.zone_heat_wh[0] for r in results)
    heat_down = sum(r.zone_heat_wh[1] for r in results)
    assert results[0].heating_mode == HeatingMode.FORCED
    assert heat_up > 3 * heat_down  # the warm zone above its setpoint takes little
    assert state.rooms[0] > 19.3
    assert state.rooms[1] < 21.7  # no overheating downstairs


def test_the_heat_pump_limit_is_shared():
    hp = two_zones()
    state = hp.initial_state([18.0, 18.0], dhw_c=45.0, outdoor_c=-10.0)
    cold = inputs(outdoor_c=-10.0)
    result = hp.step(state, cold, [HeatingMode.FORCED, HeatingMode.FORCED])
    assert result.heat_space_wh / hp.system.dt <= hp.system.hp.max_heat_w + 1e-6
    assert result.electric_wh / hp.system.dt <= hp.system.hp.max_electric_w + 50.0


def test_a_hot_water_charge_takes_capacity_from_space_heating():
    hp = two_zones()
    state = hp.initial_state([19.0, 19.0], dhw_c=42.0, outdoor_c=-5.0)
    cold = inputs(outdoor_c=-5.0)
    hold = hp.step(state, cold, [HeatingMode.FORCED] * 2, DhwMode.HOLD)
    charge = hp.step(state, cold, [HeatingMode.FORCED] * 2, DhwMode.HEAT_PUMP)
    assert charge.heat_dhw_wh > 0
    assert charge.heat_space_wh < hold.heat_space_wh


def test_reduced_zone_cools_down_while_the_other_holds():
    hp = two_zones()
    state = hp.initial_state([21.0, 21.0], dhw_c=45.0, outdoor_c=2.0)
    band = inputs(comfort_min_c=18.0, setpoint_c=21.0)
    state, _ = run(hp, state, [HeatingMode.REDUCED, HeatingMode.NORMAL], 24, slot_inputs=band)
    assert state.rooms[0] < state.rooms[1]


def test_comfort_violation_is_weighted_by_zone():
    hp = two_zones(coupling=0.0)
    state = hp.initial_state([18.0, 21.0], dhw_c=45.0, outdoor_c=2.0)
    result = hp.step(state, inputs(), [HeatingMode.NORMAL, HeatingMode.NORMAL])
    assert result.zone_comfort_violation_kh[0] > 0
    assert result.zone_comfort_violation_kh[1] == 0
    assert result.comfort_violation_kh == pytest.approx(
        result.zone_comfort_violation_kh[0] * 75 / 190
    )


def test_one_zone_follows_the_single_zone_simulator():
    """A one-zone setup heats like the existing model (same law, same limits)."""
    single = system()
    multi = MultiZoneHeatPump(system(), [ZoneParameters("all", HOUSE)])
    s_state = ThermalState(slab_c=24.0, room_c=20.5, dhw_c=45.0)
    m_state = multi.initial_state([20.5], dhw_c=45.0, outdoor_c=2.0, slabs=[24.0])
    for _ in range(16):
        s = single.step(s_state, inputs(), HeatingMode.NORMAL, DhwMode.HOLD)
        m = multi.step(m_state, inputs(), [HeatingMode.NORMAL])
        s_state, m_state = s.state, m.state
        assert m.heat_space_wh == pytest.approx(s.heat_space_wh, rel=0.02, abs=5.0)
    assert m_state.rooms[0] == pytest.approx(s_state.room_c, abs=0.02)
    assert m.electric_wh == pytest.approx(s.electric_wh, rel=0.03)


def test_energy_balance_of_the_zones():
    hp = two_zones()
    state = hp.initial_state([20.0, 20.5], dhw_c=45.0, outdoor_c=2.0)
    start = sum(
        z.building.slab_capacity_wh_per_k * s + z.building.room_capacity_wh_per_k * r
        for z, s, r in zip(hp.zones, state.slabs, state.rooms)
    )
    heat_in = 0.0
    loss = 0.0
    for _ in range(4):
        result = hp.step(state, inputs(), [HeatingMode.RAISED, HeatingMode.NORMAL])
        heat_in += result.heat_space_wh
        # losses at the mean of start and end room temperature
        loss += sum(
            z.building.loss_w_per_k * ((r0 + r1) / 2 - 2.0) * hp.system.dt
            for z, r0, r1 in zip(hp.zones, state.rooms, result.state.rooms)
        )
        state = result.state
    end = sum(
        z.building.slab_capacity_wh_per_k * s + z.building.room_capacity_wh_per_k * r
        for z, s, r in zip(hp.zones, state.slabs, state.rooms)
    )
    assert end - start == pytest.approx(heat_in - loss, rel=0.02, abs=20.0)


def test_wrong_number_of_zone_modes():
    hp = two_zones()
    state = hp.initial_state([20.0, 20.0], dhw_c=45.0, outdoor_c=2.0)
    with pytest.raises(ValueError):
        hp.step(state, inputs(), [HeatingMode.NORMAL])
