"""Heat pump inside the MILP (RL29 fork): linear model, projection to modes, integration."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import HeatingMode
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
)
from akkudoktoreos.optimization.milp.dispatch import (
    BatteryModel,
    DispatchInput,
    solve_dispatch,
)
from akkudoktoreos.optimization.milp.thermal import (
    project_modes,
    thermal_block,
)
from akkudoktoreos.utils.datetimeutil import to_datetime

SLOT_H = 0.25


def make_plan(
    hours: int = 24,
    outdoor: float = -2.0,
    settings: dict[str, Any] | None = None,
    prices: list[float] | None = None,
    **state: Any,
):
    n = int(hours / SLOT_H)
    device = HeatPumpCommonSettings(device_id="wbb12", **(settings or {}))
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device,
        initial_room_c=state.pop("room", 21.0),
        initial_dhw_c=state.pop("dhw", 45.0),
        **state,
    )
    start = to_datetime("2026-11-03T00:00:00", in_timezone="Europe/Berlin")
    starts = [start.add(minutes=15 * i) for i in range(n)]
    price = prices or [0.0003] * n
    return build_run_plan(params, starts, SLOT_H, [outdoor] * n, None, price), price


def valley_prices(n: int) -> list[float]:
    """Expensive morning and evening, a cheap (partly negative) midday."""
    hourly = (
        [0.00030] * 6
        + [0.00040] * 3
        + [0.00010] * 2
        + [-0.00002] * 3
        + [0.00015] * 2
        + [0.00045] * 4
        + [0.00032] * 4
    )
    per_slot = [p for p in hourly for _ in range(int(1 / SLOT_H))]
    return (per_slot * 4)[:n]


def dispatch_with(plan, price: list[float], battery: bool = False):
    n = plan.n_slots
    block = thermal_block(plan)
    assert block is not None
    data = DispatchInput(
        pv_wh=[0.0] * n,
        load_wh=[100.0] * n,
        price_per_wh=price,
        feed_in_per_wh=[0.00007] * n,
        battery=(
            BatteryModel(
                capacity_wh=10000,
                soc_wh=2000,
                min_soc_wh=500,
                max_soc_wh=10000,
                max_charge_wh=1250,
                max_discharge_wh=1250,
            )
            if battery
            else None
        ),
        thermal=[block],
    )
    result = solve_dispatch(data)
    assert result.success, result.status
    return block, result


def test_block_matches_the_exact_building_and_tank_equations():
    """Feeding the MILP heat into the exact models reproduces the MILP states."""
    plan, price = make_plan(prices=valley_prices(96))
    block, result = dispatch_with(plan, price)
    traj = result.thermal[0]
    system = plan.system
    slab, room, tank = plan.initial.slab_c, plan.initial.room_c, plan.initial.dhw_c
    for t, inputs in enumerate(plan.inputs):
        gains = system.building.gains_w(inputs.ghi_w_per_m2)
        slab, room = system.building.step(slab, room, traj.space_heat_w[t], inputs.outdoor_c, gains)
        tank = system.tank.step(tank, traj.dhw_heat_w[t] + traj.rod_w[t], inputs.draw_wh)
        assert slab == pytest.approx(traj.slab_c[t], abs=1e-4)
        assert room == pytest.approx(traj.room_c[t], abs=1e-4)
        assert tank == pytest.approx(traj.tank_c[t], abs=1e-4)


def test_electricity_follows_the_cop_curve_and_comfort_holds():
    plan, price = make_plan(prices=valley_prices(96))
    block, result = dispatch_with(plan, price)
    traj = result.thermal[0]
    system = plan.system
    for t in range(plan.n_slots):
        q = traj.space_heat_w[t]
        xs = [x for x, _ in block.space_points[t]]
        ys = [y for _, y in block.space_points[t]]
        space_w = (
            traj.electric_wh[t] / SLOT_H
            - block.standby_w
            - traj.dhw_heat_w[t] / block.cop_dhw[t]
            - traj.rod_w[t]
        )
        if price[t] > 0:
            # Positive price: the cheapest combination, i.e. on the interpolated curve.
            assert space_w == pytest.approx(float(np.interp(q, xs, ys)), abs=1.0)
        assert q <= block.q_cap_w[t] + 1e-6
        low, _, _ = system.comfort_band(plan.inputs[t])
        # Soft comfort: only small dips where the comfort band steps up.
        assert traj.room_c[t] >= low - 0.1
    assert min(traj.tank_c) >= plan.system.dhw.min_c - 0.05


def test_heat_moves_into_cheap_and_negative_hours():
    plan, price = make_plan(prices=valley_prices(96))
    _, result = dispatch_with(plan, price)
    traj = result.thermal[0]
    cheap = [t for t, p in enumerate(price) if p <= 0.0001]
    expensive = [t for t, p in enumerate(price) if p >= 0.0004]
    assert np.mean([traj.electric_wh[t] for t in cheap]) > 2 * np.mean(
        [traj.electric_wh[t] for t in expensive]
    )
    # Screed is charged in the valley, above where it starts.
    assert max(traj.slab_c) > plan.initial.slab_c + 1.0


def test_negative_prices_stay_bounded():
    """No electricity is "burnt": the heat pump energy is the curve, not a lower bound."""
    n = 96
    plan, _ = make_plan()
    price = [-0.0005] * n
    block, result = dispatch_with(plan, price)
    traj = result.thermal[0]
    for t in range(n):
        top = max(e for _, e in block.space_points[t])
        dhw = block.dhw_heat_w / block.cop_dhw[t]
        assert (
            traj.electric_wh[t] <= (top + dhw + block.rod_power_w + block.standby_w) * SLOT_H + 1e-6
        )


def test_battery_and_heat_pump_together():
    plan, price = make_plan(prices=valley_prices(96))
    _, result = dispatch_with(plan, price, battery=True)
    hp = result.thermal[0].electric_wh
    # The house balance includes the heat pump: import covers base load + heat pump
    # less battery discharge plus battery charging.
    for t in range(96):
        lhs = result.grid_import_wh[t] + result.discharge_wh[t]
        rhs = 100.0 + hp[t] + result.charge_grid_wh[t] + result.charge_pv_wh[t]
        assert lhs == pytest.approx(rhs, abs=1e-3)


def test_legionella_deadline_with_rod():
    plan, price = make_plan(
        settings={"dhw": {"rod_power_w": 3000, "rod_targets_c": [60, 70]}},
        prices=valley_prices(96),
        hours_since_legionella=24 * 6.9,
    )
    assert plan.legionella_deadline_slot is not None
    block, result = dispatch_with(plan, price)
    assert block.rod_power_w == 3000
    tank = result.thermal[0].tank_c
    deadline = plan.legionella_deadline_slot
    assert max(tank[:deadline]) >= plan.legionella_c - 0.5 - 1e-6
    genes = project_modes(plan, result.thermal[0])
    outcome = plan.evaluate(genes)
    assert outcome.legionella_ok


def test_cooling_season_is_out_of_scope():
    plan, _ = make_plan(outdoor=30.0)
    plan.inputs[3].cooling_season = True
    assert thermal_block(plan) is None


def test_projection_beats_status_quo():
    plan, price = make_plan(prices=valley_prices(96))
    _, result = dispatch_with(plan, price)
    genes = project_modes(plan, result.thermal[0])
    assert plan.valid(genes)

    def cost(g):
        outcome = plan.evaluate(g)
        return sum(e * p for e, p in zip(outcome.electric_wh, price)) + outcome.extra_cost

    assert cost(genes) < cost(plan.default_genes())
    heating, _ = plan.modes(genes)
    assert any(mode > HeatingMode.NORMAL for mode in heating)


def _run_milp(config_eos, thermal: bool, search: float):
    """24 h at 15 min with battery, heat pump and a price valley (MilpOptimization)."""
    from akkudoktoreos.optimization.genetic.geneticparams import (
        GeneticOptimizationParameters,
    )
    from akkudoktoreos.optimization.milp.milpoptimization import MilpOptimization

    from .test_milp_optimization import configure, heat_pump, parameters

    configure(
        config_eos, hours=24, polish_generations=0, hp_search_seconds=search, thermal_model=thermal
    )
    config_eos.merge_settings_from_dict({"optimization": {"genetic": {"interval_sec": 900}}})
    n = 96
    pv_hourly = [0] * 8 + [300, 900, 1600, 2000, 2000, 1600, 900, 300] + [0] * 8
    pv = [v / 4 for v in pv_hourly for _ in range(4)]
    data = parameters(
        hours=n,
        prices=valley_prices(n),
        pv=[float(v) for v in pv],
        heat_pumps=[heat_pump()],
        temperature_forecast=[-2.0] * n,
        forecast_interval_seconds=900,
    ).model_dump()
    data["ems"]["total_load"] = [125.0] * n
    data["heat_pumps"] = [heat_pump()]
    params = GeneticOptimizationParameters.model_validate(data)
    optimizer = MilpOptimization(fixed_seed=1)
    optimizer.optimize_ems(params, ngen=0, individuals=20)
    return optimizer


def test_milp_with_heat_pump_model_at_15_minutes(config_eos):
    without = _run_milp(config_eos, thermal=False, search=0)
    with_model = _run_milp(config_eos, thermal=True, search=0)
    summary = with_model.milp_summary
    assert summary["thermal"]["candidates"] >= 1
    assert summary["thermal"]["seconds"] < 10
    # Without any local search the heat pump model gives the better plan.
    assert summary["thermal_best_start"]
    assert summary["best_cost"] < without.milp_summary["best_cost"] - 0.1


def test_local_search_keeps_the_better_result(config_eos):
    searched = _run_milp(config_eos, thermal=False, search=5)
    combined = _run_milp(config_eos, thermal=True, search=5)
    assert combined.milp_summary["best_cost"] <= searched.milp_summary["best_cost"] + 1e-6


# ----------------------------------------------------------------------
# Rod and legionella: tight binaries (fork, solver time)
# ----------------------------------------------------------------------


def _rod_plan(hours: int = 24, prices: list[float] | None = None, **state: Any):
    """Rod with the legionella deadline at the end of the horizon."""
    return make_plan(
        hours=hours,
        settings={"dhw": {"rod_power_w": 3000, "rod_targets_c": [60, 70]}},
        prices=prices or valley_prices(int(hours / SLOT_H)),
        hours_since_legionella=24 * 7 - hours,
        **state,
    )


def _solve_block(block, price: list[float], legionella_slots=None, time_limit: float = 60.0):
    """Dispatch with one thermal block; ``legionella_slots`` overrides the candidates."""
    from akkudoktoreos.optimization.milp import dispatch as dispatch_module

    n = block.n
    data = DispatchInput(
        pv_wh=[0.0] * n,
        load_wh=[100.0] * n,
        price_per_wh=price,
        feed_in_per_wh=[0.00007] * n,
        thermal=[block],
        time_limit_s=time_limit,
    )
    if legionella_slots is None:
        return solve_dispatch(data)
    original = dispatch_module.legionella_candidate_slots
    dispatch_module.legionella_candidate_slots = lambda *args, **kwargs: list(legionella_slots)
    try:
        return solve_dispatch(data)
    finally:
        dispatch_module.legionella_candidate_slots = original


def test_tank_bounds_contain_the_milp_trajectory():
    from akkudoktoreos.optimization.milp.thermal import tank_bounds

    plan, price = _rod_plan()
    block = thermal_block(plan)
    assert block is not None
    bounds = tank_bounds(block)
    result = _solve_block(block, price)
    assert result.success
    tank = [block.tank0] + list(result.thermal[0].tank_c)
    for t, value in enumerate(tank):
        assert bounds.low[t] - 1e-6 <= value <= bounds.high[t] + 1e-6
        assert bounds.hp_high[t] <= bounds.high[t] + 1e-9
    # The rod lifts the reachable range above the heat pump's own reach.
    assert max(bounds.high) >= plan.legionella_c
    assert max(bounds.hp_high) < plan.legionella_c


def test_no_tank_binaries_where_the_rod_cannot_lift_the_tank():
    """Without rod heat (surplus-only rod, no surplus) the tank model is binary free."""
    from akkudoktoreos.optimization.milp.dispatch import _Builder
    from akkudoktoreos.optimization.milp.thermal import add_thermal

    plan, _ = make_plan(settings={"dhw": {"rod_power_w": 3000, "rod_planned_surplus_only": True}})
    block = thermal_block(plan)
    assert block is not None
    assert block.rod_power_w == 3000
    assert block.rod_cap_w and max(block.rod_cap_w) == 0.0
    builder = _Builder()
    add_thermal(builder, block)
    assert sum(builder.integer) == 0


def test_legionella_candidates_are_cheap_and_spaced():
    from akkudoktoreos.optimization.milp.thermal import (
        LEGIONELLA_CANDIDATE_SPACING,
        legionella_candidate_slots,
    )

    n = 96
    price = [0.0004] * n
    for t in range(40, 52):  # one cheap window of three hours
        price[t] = 0.0001
    plan, _ = _rod_plan(prices=price)
    block = thermal_block(plan)
    assert block is not None
    block.rod_cap_w = [block.rod_power_w] * n  # rod heat everywhere: only the price decides
    slots = legionella_candidate_slots(block, price, count=3)
    assert 1 <= len(slots) <= 3
    assert slots == sorted(slots)
    assert all(b - a >= LEGIONELLA_CANDIDATE_SPACING for a, b in zip(slots, slots[1:]))
    # The cheapest hit ends the heat-up inside or right after the cheap window.
    assert any(44 <= slot <= 55 for slot in slots)
    # No candidate after the deadline.
    assert max(slots) < plan.legionella_deadline_slot


def test_legionella_candidates_keep_the_optimum_and_solve_fast():
    """The live case of 18.11.2026: rod with grid heat in every slot ran into the time limit.

    With the candidates the model is solved to optimality quickly and reaches the same
    objective as the model with a binary in every slot (short horizon, so the latter
    is solvable in the test too).
    """
    import time

    plan, price = _rod_plan(hours=8, dhw=44.5)
    block = thermal_block(plan)
    assert block is not None
    block.rod_cap_w = [block.rod_power_w] * block.n  # grid heat for the rod in every slot
    deadline = min(plan.legionella_deadline_slot or block.n, block.n)
    every_slot = list(range(deadline))
    full = _solve_block(block, price, legionella_slots=every_slot)
    started = time.perf_counter()
    fast = _solve_block(block, price)
    seconds = time.perf_counter() - started
    assert fast.success and fast.optimal
    assert full.success
    assert fast.objective == pytest.approx(full.objective, abs=0.02)
    assert max(fast.thermal[0].tank_c[:deadline]) >= plan.legionella_c - 0.5 - 1e-6
    assert seconds < 20.0


def test_legionella_with_grid_rod_all_day_is_solved_to_optimality():
    """24 h, rod allowed from the grid in every slot (legionella fallback window)."""
    plan, price = _rod_plan(hours=24, dhw=44.5)
    block = thermal_block(plan)
    assert block is not None
    block.rod_cap_w = [block.rod_power_w] * block.n  # grid heat for the rod in every slot
    result = _solve_block(block, price, time_limit=60.0)
    assert result.success
    assert result.optimal, f"gap {result.mip_gap}"
    deadline = min(plan.legionella_deadline_slot or block.n, block.n)
    assert max(result.thermal[0].tank_c[:deadline]) >= plan.legionella_c - 0.5 - 1e-6
