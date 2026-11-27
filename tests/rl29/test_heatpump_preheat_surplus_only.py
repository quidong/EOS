"""PREHEAT only with forecast PV surplus (fork, ``optimization.preheat_planned_surplus_only``).

PREHEAT stores heat for the hours after PV surplus. Without the setting the optimizer picks
it whenever the comfort penalty makes it the cheapest mode, and it becomes a permanent
state. With the setting a slot whose forecast PV surplus is below
``optimization.preheat_min_pv_surplus_w`` is locked: a planned PREHEAT runs as RAISED in the
simulation, in the MILP model and in the plan the controller reads.
"""

from __future__ import annotations

from typing import Any, Optional

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
T0 = pendulum.datetime(2026, 11, 24, 6, 0, tz=TZ)
SURPLUS_SLOTS = 4
SLOTS = 8
SURPLUS_W = [3000.0] * SURPLUS_SLOTS + [200.0] * (SLOTS - SURPLUS_SLOTS)


def device(surplus_only: bool = True, **optimization: Any) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings.model_validate(
        {
            "device_id": "hp1",
            "unit": {"preheat_valves_forced": True, "raised_flow_offset_k": 2.0},
            "optimization": {
                "heating_modes": [0, 1, 2, 3],
                "preheat_planned_surplus_only": surplus_only,
                **optimization,
            },
        }
    )


def run_plan(settings: HeatPumpCommonSettings, surplus_w: Optional[list[float]] = SURPLUS_W):
    params = GeneticHeatPumpParameters(
        device_id="hp1", settings=settings, initial_room_c=19.8, initial_dhw_c=46.0
    )
    starts = [T0.add(hours=i) for i in range(SLOTS)]
    return build_run_plan(
        params, starts, 1.0, [-2.0] * SLOTS, [0.0] * SLOTS, [0.0003] * SLOTS, surplus_w
    )


def locked(run) -> list[bool]:
    return [i.preheat_locked for i in run.inputs]


def test_settings_default_keeps_the_old_behaviour():
    opt = HeatPumpCommonSettings.model_validate({"device_id": "x"}).optimization
    assert opt.preheat_planned_surplus_only is False
    assert opt.preheat_min_pv_surplus_w == 1000.0
    assert device(preheat_min_pv_surplus_w=1500.0).optimization.preheat_min_pv_surplus_w == 1500.0


def test_negative_threshold_is_rejected():
    with pytest.raises(ValueError):
        device(preheat_min_pv_surplus_w=-1.0)


def test_effective_mode_of_a_locked_slot():
    system = device().to_system(1.0)
    free = SlotInputs(outdoor_c=0.0, comfort_min_c=19.5)
    lock = SlotInputs(outdoor_c=0.0, comfort_min_c=19.5, preheat_locked=True)
    assert system.effective_mode(HeatingMode.PREHEAT, free) == HeatingMode.PREHEAT
    assert system.effective_mode(HeatingMode.PREHEAT, lock) == HeatingMode.RAISED
    for mode in (HeatingMode.NORMAL, HeatingMode.RAISED, HeatingMode.FORCED):
        assert system.effective_mode(mode, lock) == mode


def test_reduce_lock_still_runs_as_normal():
    system = device().to_system(1.0)
    both = SlotInputs(outdoor_c=0.0, comfort_min_c=19.5, reduce_locked=True, preheat_locked=True)
    assert system.effective_mode(HeatingMode.REDUCED, both) == HeatingMode.NORMAL
    assert system.effective_mode(HeatingMode.PREHEAT, both) == HeatingMode.RAISED


def test_slots_without_surplus_are_locked():
    run = run_plan(device())
    assert locked(run) == [False] * SURPLUS_SLOTS + [True] * (SLOTS - SURPLUS_SLOTS)


def test_threshold_is_the_limit():
    run = run_plan(device(preheat_min_pv_surplus_w=3000.0))
    assert locked(run) == [False] * SURPLUS_SLOTS + [True] * (SLOTS - SURPLUS_SLOTS)
    run = run_plan(device(preheat_min_pv_surplus_w=3001.0))
    assert all(locked(run))


def test_missing_surplus_forecast_locks_every_slot():
    assert all(locked(run_plan(device(), None)))
    assert all(locked(run_plan(device(), [])))


def test_nothing_is_locked_without_the_setting_or_without_preheat():
    assert not any(locked(run_plan(device(surplus_only=False))))
    assert not any(locked(run_plan(device(heating_modes=[0, 1, 2]))))


def test_locked_preheat_runs_as_raised_in_the_step():
    system = device().to_system(1.0)
    state = ThermalState(slab_c=21.0, room_c=19.8, dhw_c=46.0)
    free = SlotInputs(outdoor_c=-2.0, comfort_min_c=19.5, setpoint_c=21.0, comfort_max_c=23.0)
    lock = SlotInputs(
        outdoor_c=-2.0,
        comfort_min_c=19.5,
        setpoint_c=21.0,
        comfort_max_c=23.0,
        preheat_locked=True,
    )
    preheat = system.step(state, free, HeatingMode.PREHEAT, DhwMode.HOLD)
    blocked = system.step(state, lock, HeatingMode.PREHEAT, DhwMode.HOLD)
    raised = system.step(state, lock, HeatingMode.RAISED, DhwMode.HOLD)
    assert blocked.heat_space_wh == pytest.approx(raised.heat_space_wh)
    assert blocked.hp_electric_wh == pytest.approx(raised.hp_electric_wh)
    assert abs(preheat.heat_space_wh - blocked.heat_space_wh) > 100.0  # PREHEAT is another mode


def test_plan_reports_the_executed_mode():
    run = run_plan(device())
    genes = run.genes_for_modes(
        [HeatingMode.PREHEAT] * run.n_periods, [DhwMode.HOLD] * run.n_periods
    )
    detail = run.evaluate(genes, detail=True).detail or {}
    assert detail["heating_mode"] == ["PREHEAT"] * SURPLUS_SLOTS + ["RAISED"] * (
        SLOTS - SURPLUS_SLOTS
    )


def test_plan_without_the_setting_keeps_preheat_everywhere():
    run = run_plan(device(surplus_only=False))
    genes = run.genes_for_modes(
        [HeatingMode.PREHEAT] * run.n_periods, [DhwMode.HOLD] * run.n_periods
    )
    detail = run.evaluate(genes, detail=True).detail or {}
    assert detail["heating_mode"] == ["PREHEAT"] * SLOTS


def test_locked_hours_run_another_mode_in_the_simulation():
    """The simulation no longer credits PREHEAT in the locked hours."""
    free = run_plan(device(surplus_only=False))
    gated = run_plan(device())
    genes_free = free.genes_for_modes(
        [HeatingMode.PREHEAT] * free.n_periods, [DhwMode.HOLD] * free.n_periods
    )
    genes_gated = gated.genes_for_modes(
        [HeatingMode.PREHEAT] * gated.n_periods, [DhwMode.HOLD] * gated.n_periods
    )
    room_free = (free.evaluate(genes_free, detail=True).detail or {})["room_c"]
    room_gated = (gated.evaluate(genes_gated, detail=True).detail or {})["room_c"]
    assert room_gated[:SURPLUS_SLOTS] == pytest.approx(room_free[:SURPLUS_SLOTS])
    assert abs(room_gated[-1] - room_free[-1]) > 0.05  # the locked hours run another mode


def test_milp_model_treats_a_locked_preheat_like_raised():
    run = run_plan(device())
    block = thermal_block(run, joint_modes=True)
    assert block is not None and block.mode_control is not None
    mc = block.mode_control
    preheat = mc.modes.index(int(HeatingMode.PREHEAT))
    raised = mc.modes.index(int(HeatingMode.RAISED))
    for t in range(SURPLUS_SLOTS):
        assert mc.const_w[t][preheat] != pytest.approx(mc.const_w[t][raised])
    for t in range(SURPLUS_SLOTS, SLOTS):
        assert mc.const_w[t][preheat] == pytest.approx(mc.const_w[t][raised])


def test_milp_model_keeps_preheat_without_the_setting():
    run = run_plan(device(surplus_only=False))
    block = thermal_block(run, joint_modes=True)
    assert block is not None and block.mode_control is not None
    mc = block.mode_control
    preheat = mc.modes.index(int(HeatingMode.PREHEAT))
    raised = mc.modes.index(int(HeatingMode.RAISED))
    assert all(mc.const_w[t][preheat] != pytest.approx(mc.const_w[t][raised]) for t in range(SLOTS))
