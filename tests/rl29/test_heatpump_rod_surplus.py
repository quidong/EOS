"""Heating rod with its own PV surplus controller (fork, e.g. a PV power diverter).

The rod heats on its own with the PV surplus left after the heat pump, up to
``dhw.rod_surplus_c``. EOS models this so it does not plan a heat pump charge the rod
covers anyway; a planned ROD slot stays a boost from the grid.
"""

from __future__ import annotations

import pendulum
import pytest

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import (
    DhwMode,
    HeatingMode,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
)

TZ = "Europe/Berlin"
NOON = pendulum.datetime(2026, 6, 6, 10, 0, tz=TZ)


def device(rod_surplus_c=60.0, rod_power_w=3000.0) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings.model_validate(
        {
            "device_id": "hp1",
            "dhw": {"rod_power_w": rod_power_w, "rod_surplus_c": rod_surplus_c},
        }
    )


def system(**kwargs):
    return device(**kwargs).to_system(0.25)


def slot(surplus_w: float) -> SlotInputs:
    return SlotInputs(
        outdoor_c=20.0,
        comfort_min_c=20.0,
        setpoint_c=21.0,
        comfort_max_c=23.0,
        pv_surplus_w=surplus_w,
    )


def test_rod_uses_the_surplus_up_to_its_limit():
    sys_ = system()
    state = ThermalState(slab_c=22.0, room_c=22.0, dhw_c=46.0)
    result = sys_.step(state, slot(2000.0), HeatingMode.NORMAL, DhwMode.HOLD)
    assert result.rod_electric_wh == pytest.approx(2000.0 * 0.25, rel=0.05)
    assert result.state.dhw_c > state.dhw_c
    full = sys_.step(state, slot(8000.0), HeatingMode.NORMAL, DhwMode.HOLD)
    assert full.rod_electric_wh == pytest.approx(3000.0 * 0.25, rel=0.01)  # rod power


def test_no_surplus_rod_or_limit_reached_means_no_rod():
    state = ThermalState(slab_c=22.0, room_c=22.0, dhw_c=46.0)
    assert system().step(state, slot(0.0), HeatingMode.NORMAL, DhwMode.HOLD).rod_electric_wh == 0
    hot = ThermalState(slab_c=22.0, room_c=22.0, dhw_c=60.5)
    assert system().step(hot, slot(3000.0), HeatingMode.NORMAL, DhwMode.HOLD).rod_electric_wh == 0
    assert (
        system(rod_surplus_c=None)
        .step(state, slot(3000.0), HeatingMode.NORMAL, DhwMode.HOLD)
        .rod_electric_wh
        == 0
    )


def test_heat_pump_goes_first():
    """Surplus the heat pump uses is not available to the rod."""
    sys_ = system()
    state = ThermalState(slab_c=20.0, room_c=19.5, dhw_c=46.0)
    heating = sys_.step(state, slot(2000.0), HeatingMode.PREHEAT, DhwMode.HOLD)
    idle = sys_.step(
        ThermalState(slab_c=22.0, room_c=22.0, dhw_c=46.0),
        slot(2000.0),
        HeatingMode.NORMAL,
        DhwMode.HOLD,
    )
    assert heating.hp_electric_wh > idle.hp_electric_wh
    assert heating.rod_electric_wh < idle.rod_electric_wh


def test_planned_rod_boost_is_unchanged():
    sys_ = system()
    state = ThermalState(slab_c=22.0, room_c=22.0, dhw_c=46.0)
    boost = sys_.step(state, slot(0.0), HeatingMode.NORMAL, DhwMode.ROD, 60.0)
    assert boost.rod_electric_wh == pytest.approx(3000.0 * 0.25, rel=0.01)


def test_run_plan_passes_the_surplus_and_avoids_a_heat_pump_charge():
    n = 8
    params = GeneticHeatPumpParameters(
        device_id="hp1", settings=device(), initial_room_c=21.5, initial_dhw_c=41.5
    )
    starts = [NOON.add(hours=i) for i in range(n)]
    run = build_run_plan(params, starts, 1.0, [20.0] * n, [600.0] * n, [0.0003] * n, [3000.0] * n)
    assert run.inputs[0].pv_surplus_w == 3000.0
    plain = build_run_plan(
        GeneticHeatPumpParameters(
            device_id="hp1",
            settings=device(rod_surplus_c=None),
            initial_room_c=21.5,
            initial_dhw_c=41.5,
        ),
        starts,
        1.0,
        [20.0] * n,
        [600.0] * n,
        [0.0003] * n,
        [3000.0] * n,
    )
    assert plain.inputs[0].pv_surplus_w == 0.0
    genes = run.genes_for_modes(
        [HeatingMode.NORMAL] * run.n_periods, [DhwMode.HOLD] * run.n_periods
    )
    with_rod = run.evaluate(genes, detail=True)
    detail = with_rod.detail or {}
    assert sum(detail["rod_electric_wh"]) > 0
    assert max(detail["dhw_c"]) > 50.0  # the rod charged the tank with surplus
