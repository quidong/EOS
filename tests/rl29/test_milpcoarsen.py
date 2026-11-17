"""Coarse slots for the far horizon of the MILP dispatch (RL29 fork, P4)."""

from __future__ import annotations

import math
import time
from dataclasses import replace

import numpy as np
import pytest

from akkudoktoreos.optimization.milp.coarsen import (
    coarsen_block,
    fine_slots_needed,
    slot_groups,
)
from akkudoktoreos.optimization.milp.dispatch import (
    ApplianceRun,
    BatteryModel,
    DispatchInput,
    SwitchingModel,
    UncertaintyBudget,
    solve_dispatch,
)
from akkudoktoreos.optimization.milp.thermal import thermal_block

from .test_milp_thermal import make_plan, valley_prices

CENT = 0.01 / 1000.0


def two_days(**kw) -> DispatchInput:
    n = 192
    pv = [max(0.0, 3000 * math.sin(math.pi * ((t % 96) - 28) / 48)) / 4 for t in range(n)]
    load = [120.0 + (200.0 if 68 <= t % 96 < 88 else 0.0) for t in range(n)]
    hourly = [(25 + 10 * math.sin(2 * math.pi * h / 24)) * CENT for h in range(48)]
    price = [p for p in hourly for _ in range(4)]
    values = dict(
        pv_wh=pv,
        load_wh=load,
        price_per_wh=price,
        feed_in_per_wh=[8 * CENT] * n,
        battery=BatteryModel(
            capacity_wh=11000,
            soc_wh=3000,
            min_soc_wh=500,
            max_soc_wh=11000,
            max_charge_wh=2500,
            max_discharge_wh=2500,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
        ),
        terminal_segments=[(5000, 15 * CENT)],
    )
    values.update(kw)
    return DispatchInput(**values)


def test_slot_groups():
    assert slot_groups(10, 4, 4) == [(0, 1), (1, 1), (2, 1), (3, 1), (4, 4), (8, 2)]
    assert slot_groups(3, 8, 4) == [(0, 1), (1, 1), (2, 1)]
    assert slot_groups(5, 0, 1) == [(i, 1) for i in range(5)]


def test_fine_part_covers_appliances_and_shared_slots():
    data = two_days(
        appliances=[ApplianceRun(allowed_starts=[10, 60], profile_wh=[300] * 8)],
        scenarios=[(1.0, [0.0] * 192, [0.0] * 192)],
        shared_slots=30,
    )
    assert fine_slots_needed(data, 20) == 68


def test_coarse_horizon_is_close_and_faster():
    fine = solve_dispatch(two_days())
    started = time.perf_counter()
    coarse = solve_dispatch(two_days(coarsen_after_slots=48))
    seconds = time.perf_counter() - started
    assert coarse.success and fine.success
    assert seconds < 10
    # Expanded back to the original slots, energy balanced, close in cost.
    assert len(coarse.grid_import_wh) == 192 and len(coarse.soc_wh) == 193
    assert coarse.objective == pytest.approx(fine.objective, abs=0.03 * abs(fine.objective) + 0.05)
    # The near hours (what is executed) agree.
    assert coarse.discharge_wh[:24] == pytest.approx(fine.discharge_wh[:24], abs=50)
    pv = np.array(two_days().pv_wh)
    load = np.array(two_days().load_wh)
    flows = (
        pv
        + np.array(coarse.grid_import_wh)
        + np.array(coarse.discharge_wh)
        - np.array(coarse.charge_pv_wh)
        - np.array(coarse.charge_grid_wh)
        - np.array(coarse.grid_export_wh)
    )
    assert (flows - load).sum() == pytest.approx(0, abs=1)  # curtailment-free day
    # SoC follows the flows inside coarse slots.
    soc = np.array(coarse.soc_wh)
    assert np.all(soc >= 500 - 1e-6) and np.all(soc <= 11000 + 1e-6)


def test_power_limits_scale_with_the_slot_length():
    # Only the far part is cheap: a coarse slot may take four slots of charge power.
    n = 16
    data = DispatchInput(
        pv_wh=[0.0] * n,
        load_wh=[0.0] * 12 + [3000.0] * 4,
        price_per_wh=[40 * CENT] * 8 + [10 * CENT] * 4 + [40 * CENT] * 4,
        feed_in_per_wh=[0.0] * n,
        battery=BatteryModel(
            capacity_wh=20000,
            soc_wh=0,
            min_soc_wh=0,
            max_soc_wh=20000,
            max_charge_wh=2500,
            max_discharge_wh=2500,
        ),
        coarsen_after_slots=8,
    )
    result = solve_dispatch(data)
    assert sum(result.charge_grid_wh[8:12]) == pytest.approx(10000, abs=1)
    assert max(result.charge_grid_wh) <= 2500 + 1e-6


def test_switching_and_hedge_run_on_coarse_slots():
    data = two_days(
        coarsen_after_slots=48,
        switching=SwitchingModel(change_cost=0.015, hold_slots=3, hold_cost=0.5),
        uncertainty=UncertaintyBudget(pv_dev=0.3, load_dev=0.2, budget_slots=24),
    )
    result = solve_dispatch(data)
    assert result.success
    assert result.mode_changes is not None


def test_coarse_building_and_tank_match_four_fine_steps():
    plan, _ = make_plan(hours=2)
    block = thermal_block(plan)
    assert block is not None
    block = replace(block, vent_w_per_k=[12.0] * block.n)
    groups = [(0, 4), (4, 4)]
    coarse = coarsen_block(block, groups)
    assert coarse.n == 2 and coarse.slot_dt == [pytest.approx(block.dt * 4)] * 2
    a = np.array(block.a)
    bm = np.array(block.b)
    x = np.array([block.slab0, block.room0])
    q, out, gains, vent = 3000.0, -2.0, 150.0, 12.0
    for _ in range(4):
        a_eff = a.copy()
        a_eff[:, 1] -= bm[:, 2] * vent
        x = a_eff @ x + bm @ np.array([q, out, gains + vent * out])
    ak = np.array(coarse.a_slots[0])
    bk = np.array(coarse.b_slots[0])
    xc = ak @ np.array([block.slab0, block.room0]) + bk @ np.array([q, out, gains])
    assert xc == pytest.approx(x, abs=1e-9)
    assert coarse.vent_w_per_k == [0.0, 0.0]
    # Tank: four decays and the gain of four held inputs.
    tank = block.tank0
    for _ in range(4):
        tank = block.tank_ambient_c + (tank - block.tank_ambient_c) * block.tank_decay + (
            2000.0
        ) * block.tank_gain
    decay, gain = coarse.tank_step(0)
    assert block.tank_ambient_c + (block.tank0 - block.tank_ambient_c) * decay + 2000.0 * gain == (
        pytest.approx(tank)
    )


def test_heat_pump_on_coarse_slots_is_close_to_the_fine_model():
    plan, price = make_plan(hours=24, prices=valley_prices(96))
    block = thermal_block(plan)
    assert block is not None
    n = plan.n_slots
    base = DispatchInput(
        pv_wh=[0.0] * n,
        load_wh=[100.0] * n,
        price_per_wh=price,
        feed_in_per_wh=[0.00007] * n,
        thermal=[block],
        time_limit_s=60,
    )
    fine = solve_dispatch(base)
    coarse = solve_dispatch(replace(base, coarsen_after_slots=32))
    assert fine.success and coarse.success
    assert len(coarse.thermal[0].room_c) == n
    fine_kwh = sum(fine.thermal[0].electric_wh) / 1000
    coarse_kwh = sum(coarse.thermal[0].electric_wh) / 1000
    assert coarse_kwh == pytest.approx(fine_kwh, rel=0.1)
    assert coarse.objective == pytest.approx(fine.objective, rel=0.1)
    # Comfort holds on the expanded room trajectory (soft, small slack).
    assert min(coarse.thermal[0].room_c) >= min(block.room_min_c) - 0.3
