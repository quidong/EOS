"""Planned heating rod in PV surplus mode (fork, ``dhw.rod_planned_surplus_only``).

A planned ROD slot (higher tank temperature, legionella) runs the rod controller in its
PV surplus mode: the rod only uses the forecast PV surplus left after the heat pump -
never grid or battery - up to the planned target. Without a planned ROD slot the rod
stays off (unlike ``rod_surplus_c``, where the controller is always in surplus mode).
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
from akkudoktoreos.optimization.milp.thermal import thermal_block

TZ = "Europe/Berlin"
NOON = pendulum.datetime(2026, 6, 6, 10, 0, tz=TZ)


def device(planned_surplus: bool = True, **dhw) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings.model_validate(
        {
            "device_id": "hp1",
            "dhw": {
                "rod_power_w": 3000.0,
                "rod_targets_c": [60.0, 70.0],
                "rod_planned_surplus_only": planned_surplus,
                **dhw,
            },
            "optimization": {"dhw_modes": [0, 2]},
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


IDLE = ThermalState(slab_c=22.0, room_c=22.0, dhw_c=46.0)


def test_setting_reaches_the_system():
    assert system().dhw.rod_planned_surplus_only is True
    assert system(planned_surplus=False).dhw.rod_planned_surplus_only is False
    assert (
        HeatPumpCommonSettings.model_validate({"device_id": "x"}).dhw.rod_planned_surplus_only
        is False
    )


def test_planned_rod_uses_only_the_surplus():
    sys_ = system()
    part = sys_.step(IDLE, slot(1200.0), HeatingMode.NORMAL, DhwMode.ROD, 70.0)
    assert part.rod_electric_wh == pytest.approx(1200.0 * 0.25, rel=0.05)
    full = sys_.step(IDLE, slot(9000.0), HeatingMode.NORMAL, DhwMode.ROD, 70.0)
    assert full.rod_electric_wh == pytest.approx(3000.0 * 0.25, rel=0.01)


def test_planned_rod_without_surplus_does_nothing():
    result = system().step(IDLE, slot(0.0), HeatingMode.NORMAL, DhwMode.ROD, 70.0)
    assert result.rod_electric_wh == 0.0
    assert result.state.dhw_c < IDLE.dhw_c  # only losses


def test_rod_off_without_a_planned_slot():
    """Unlike rod_surplus_c the controller is not permanently in surplus mode."""
    result = system().step(IDLE, slot(5000.0), HeatingMode.NORMAL, DhwMode.HOLD)
    assert result.rod_electric_wh == 0.0


def test_planned_target_limits_the_rod():
    hot = ThermalState(slab_c=22.0, room_c=22.0, dhw_c=60.2)
    sys_ = system()
    assert sys_.step(hot, slot(5000.0), HeatingMode.NORMAL, DhwMode.ROD, 60.0).rod_electric_wh == 0
    assert sys_.step(hot, slot(5000.0), HeatingMode.NORMAL, DhwMode.ROD, 70.0).rod_electric_wh > 0


def test_heat_pump_goes_first():
    sys_ = system()
    cold = ThermalState(slab_c=20.0, room_c=19.5, dhw_c=46.0)
    heating = sys_.step(cold, slot(2000.0), HeatingMode.PREHEAT, DhwMode.ROD, 70.0)
    idle = sys_.step(IDLE, slot(2000.0), HeatingMode.NORMAL, DhwMode.ROD, 70.0)
    assert heating.hp_electric_wh > idle.hp_electric_wh
    assert heating.rod_electric_wh < idle.rod_electric_wh


def test_grid_boost_unchanged_without_the_setting():
    boost = system(planned_surplus=False).step(
        IDLE, slot(0.0), HeatingMode.NORMAL, DhwMode.ROD, 70.0
    )
    assert boost.rod_electric_wh == pytest.approx(3000.0 * 0.25, rel=0.01)


def run_plan(settings, surplus_w, n=8):
    params = GeneticHeatPumpParameters(
        device_id="hp1", settings=settings, initial_room_c=21.5, initial_dhw_c=46.0
    )
    starts = [NOON.add(hours=i) for i in range(n)]
    return build_run_plan(params, starts, 1.0, [20.0] * n, [600.0] * n, [0.0003] * n, surplus_w)


def test_run_plan_passes_the_surplus():
    run = run_plan(device(), [2500.0] * 8)
    assert run.inputs[0].pv_surplus_w == 2500.0
    assert run_plan(device(planned_surplus=False), [2500.0] * 8).inputs[0].pv_surplus_w == 0.0


def test_planned_rod_heats_to_target_with_surplus_only():
    surplus = [3000.0] * 4 + [0.0] * 4
    run = run_plan(device(), surplus)
    modes = [DhwMode.ROD] * run.n_periods
    genes = run.genes_for_modes([HeatingMode.NORMAL] * run.n_periods, modes)
    detail = run.evaluate(genes, detail=True).detail or {}
    rod_wh = detail["rod_electric_wh"]
    assert sum(rod_wh[:4]) > 0
    assert sum(rod_wh[4:]) == 0  # no surplus, no rod (no grid)
    assert max(detail["dhw_c"]) > 55.0
    assert max(detail["dhw_c"]) <= 70.5


def test_milp_rod_cap_follows_the_surplus():
    surplus = [3000.0, 1000.0, 0.0, 5000.0]
    run = run_plan(device(), surplus, n=4)
    block = thermal_block(run)
    assert block is not None
    assert block.rod_cap_w == [3000.0, 1000.0, 0.0, 3000.0]
    plain = thermal_block(run_plan(device(planned_surplus=False), surplus, n=4))
    assert plain is not None and plain.rod_cap_w == []


# --- Legionella fallback from the grid (dhw.rod_legionella_grid_hours) ---------------


def test_grid_allowed_slot_heats_at_full_power():
    grid_slot = slot(0.0)
    grid_slot.rod_grid_allowed = True
    result = system().step(IDLE, grid_slot, HeatingMode.NORMAL, DhwMode.ROD, 60.0)
    assert result.rod_electric_wh == pytest.approx(3000.0 * 0.25, rel=0.01)


def legionella_plan(hours_since: float, grid_hours, n: int = 48):
    settings = device(rod_legionella_grid_hours=grid_hours, legionella_interval_days=7)
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=settings,
        initial_room_c=21.5,
        initial_dhw_c=46.0,
        hours_since_legionella=hours_since,
    )
    starts = [NOON.add(hours=i) for i in range(n)]
    return build_run_plan(params, starts, 1.0, [10.0] * n, [0.0] * n, [0.0003] * n, [0.0] * n)


def test_grid_window_before_the_deadline():
    # deadline in 30 h, window 24 h -> slots 6..29 may use the grid
    run = legionella_plan(7 * 24 - 30, 24.0)
    assert run.legionella_deadline_slot == 30
    allowed = [i.rod_grid_allowed for i in run.inputs]
    assert not any(allowed[:6])
    assert all(allowed[6:30])
    assert not any(allowed[30:])


def test_grid_window_at_once_when_overdue_or_close():
    run = legionella_plan(7 * 24 + 5, 24.0)
    assert all(i.rod_grid_allowed for i in run.inputs[: run.legionella_deadline_slot])
    close = legionella_plan(7 * 24 - 10, 24.0)
    assert all(i.rod_grid_allowed for i in close.inputs[: close.legionella_deadline_slot])


def test_no_grid_window_without_the_setting():
    run = legionella_plan(7 * 24 - 10, None)
    assert run.legionella_deadline_slot is not None
    assert not any(i.rod_grid_allowed for i in run.inputs)


def test_legionella_without_pv_heats_from_the_grid_and_marks_the_slots():
    run = legionella_plan(7 * 24 - 10, 24.0, n=16)
    genes = run.genes_for_modes([HeatingMode.NORMAL] * run.n_periods, [DhwMode.ROD] * run.n_periods)
    res = run.evaluate(genes, detail=True)
    detail = res.detail or {}
    assert res.legionella_ok is True
    assert sum(detail["rod_electric_wh"]) > 0
    assert any(v == 1.0 for v in detail["rod_grid"])
    held = run.evaluate(
        run.genes_for_modes([HeatingMode.NORMAL] * run.n_periods, [DhwMode.HOLD] * run.n_periods),
        detail=True,
    )
    assert all(v == 0.0 for v in (held.detail or {})["rod_grid"])


def test_milp_cap_is_full_power_in_the_grid_window():
    run = legionella_plan(7 * 24 - 10, 24.0, n=16)
    block = thermal_block(run)
    assert block is not None
    assert (
        block.rod_cap_w[: run.legionella_deadline_slot] == [3000.0] * run.legionella_deadline_slot
    )
