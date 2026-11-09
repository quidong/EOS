"""Heating modes planned inside the MILP (RL29 fork, ``milp.joint_modes``)."""

from __future__ import annotations

import itertools

import pytest

from akkudoktoreos.devices.thermal.heatpump import DhwMode, HeatingMode
from akkudoktoreos.optimization.milp.dispatch import DispatchInput
from akkudoktoreos.optimization.milp.milpoptimization import solve_joint_modes
from akkudoktoreos.optimization.milp.thermal import (
    FORCED_HEAT_W,
    mode_heat_coefficients,
    project_dhw,
    thermal_block,
)

from .test_milp_thermal import _run_milp, make_plan, valley_prices


def clipped_heat(block, t, mode_index, room, slab, dhw_w=0.0):
    """The heat the MILP rows force for a mode and state (reference implementation)."""
    mc = block.mode_control
    d = mc.const_w[t][mode_index] + mc.k_room[t] * room + mc.k_slab[t] * slab
    cap = block.q_cap_w[t] * (1.0 - dhw_w / block.dhw_heat_w)
    return min(max(d, 0.0), cap)


@pytest.mark.parametrize("outdoor", [-8.0, 2.0, 9.0])
def test_mode_heat_matches_the_exact_controller(outdoor):
    """For every mode the affine heat law reproduces ``HeatPumpSystem.step``."""
    plan, _ = make_plan(hours=2, outdoor=outdoor, dhw=50.0, settings=ALL_MODES)
    block = thermal_block(plan, joint_modes=True)
    assert block is not None and block.mode_control is not None
    system = plan.system
    slot = plan.inputs[0]
    modes = block.mode_control.modes
    for (index, mode), room, slab in itertools.product(
        enumerate(modes), (19.6, 20.4, 21.0, 21.6), (22.0, 26.0, 31.0)
    ):
        state = plan.initial.copy()
        state.room_c, state.slab_c, state.dhw_c = room, slab, 50.0  # no hot water charge
        _, _, room_max = system.comfort_band(slot)
        if room >= room_max and mode != int(HeatingMode.FORCED):
            continue  # the controller stops above the comfort maximum (not modelled)
        result = system.step(state, slot, HeatingMode(mode), DhwMode.HOLD)
        cap, _, _ = system._space_heat_capacity_w(slot.outdoor_c, slab)
        expected_w = result.heat_space_wh / system.dt
        mc = block.mode_control
        d = mc.const_w[0][index] + mc.k_room[0] * room + mc.k_slab[0] * slab
        assert min(max(d, 0.0), cap) == pytest.approx(expected_w, rel=1e-6, abs=1e-3), (
            mode,
            room,
            slab,
        )


ALL_MODES = {"optimization": {"heating_modes": [0, 1, 2, 3, 4]}}


def test_forced_runs_at_full_capacity():
    plan, _ = make_plan(hours=2, settings=ALL_MODES)
    block = thermal_block(plan, joint_modes=True)
    mc = block.mode_control
    forced = mc.modes.index(int(HeatingMode.FORCED))
    assert mc.const_w[0][forced] == FORCED_HEAT_W
    assert clipped_heat(block, 0, forced, 21.0, 30.0) == pytest.approx(block.q_cap_w[0])


def test_coefficients_are_affine_in_the_target():
    plan, _ = make_plan(hours=1)
    system, slot = plan.system, plan.inputs[0]
    c20, kr, ks = mode_heat_coefficients(system, slot, 20.0, 21.0)
    c21, kr2, ks2 = mode_heat_coefficients(system, slot, 21.0, 21.0)
    assert (kr, ks) == (kr2, ks2)  # the state slopes do not depend on the mode
    assert c21 > c20  # a higher room target asks for more heat


def _solve_joint(plan, price, window_periods: int = 6, time_limit_s: float = 30.0):
    n = plan.n_slots
    block = thermal_block(plan, joint_modes=True)
    data = DispatchInput(
        pv_wh=[0.0] * n,
        load_wh=[100.0] * n,
        price_per_wh=price,
        feed_in_per_wh=[0.00007] * n,
        thermal=[block],
        mip_rel_gap=0.005,
    )
    result, solves = solve_joint_modes(data, window_periods, time_limit_s)
    assert result is not None and result.success, result
    assert solves == -(-plan.n_periods // window_periods)
    # After the last window every period is fixed.
    assert set(block.mode_control.fixed_periods) | block.mode_control.integer_periods == set(
        range(plan.n_periods)
    )
    return block, result


def test_joint_model_heat_follows_the_selected_modes():
    """In the solution every slot's heat is exactly the controller's heat of its mode."""
    plan, price = make_plan(hours=12, prices=valley_prices(48))
    block, result = _solve_joint(plan, price)
    traj = result.thermal[0]
    assert traj.heating_genes is not None and len(traj.heating_genes) == plan.n_periods
    rooms = [plan.initial.room_c] + traj.room_c
    slabs = [plan.initial.slab_c] + traj.slab_c
    mc = block.mode_control
    deviations = []
    for t in range(plan.n_slots):
        mode_index = traj.heating_genes[mc.period_of_slot[t]]
        expected = clipped_heat(block, t, mode_index, rooms[t], slabs[t], traj.dhw_heat_w[t])
        deviations.append(abs(traj.space_heat_w[t] - expected))
    # The coupling is soft (an expensive deviation keeps every mode choice feasible).
    # Windows that were solved with their binaries and then fixed follow the
    # controller exactly; the last window may stop at the time limit.
    first_window = [t for t in range(plan.n_slots) if mc.period_of_slot[t] < 6]
    assert first_window
    assert max(deviations[t] for t in first_window) < 1.0


def test_joint_modes_follow_the_price_valley_and_beat_the_status_quo():
    plan, price = make_plan(prices=valley_prices(96))
    _, result = _solve_joint(plan, price)
    traj = result.thermal[0]
    genes = project_dhw(plan, traj, traj.heating_genes)
    assert plan.valid(genes)

    def cost(g):
        outcome = plan.evaluate(g)
        return sum(e * p for e, p in zip(outcome.electric_wh, price)) + outcome.extra_cost

    assert cost(genes) < cost(plan.default_genes())


def test_mode_change_penalty_limits_switching():
    cheap, price = make_plan(prices=valley_prices(96))
    _, free = _solve_joint(cheap, price)
    costly, _ = make_plan(
        prices=valley_prices(96),
        settings={"optimization": {"mode_change_penalty_amt": 5.0}},
    )
    _, damped = _solve_joint(costly, price)

    def changes(genes):
        return sum(1 for a, b in zip(genes, genes[1:]) if a != b)

    assert changes(damped.thermal[0].heating_genes) <= changes(free.thermal[0].heating_genes)


def test_milp_uses_the_joint_candidate(config_eos):
    """End to end in MilpOptimization: the joint candidate is built, scored and logged."""
    config_eos.merge_settings_from_dict(
        {"optimization": {"milp": {"joint_modes": True, "joint_time_limit_s": 30}}}
    )
    optimizer = _run_milp(config_eos, thermal=True, search=0)
    joint = optimizer.milp_summary["joint"]
    assert joint.get("seconds") is not None, joint
    assert joint["seconds"] < 60
    reference = _run_milp(config_eos, thermal=True, search=0)
    # Disabled by default: no joint summary without the switch.
    config_eos.merge_settings_from_dict({"optimization": {"milp": {"joint_modes": False}}})
    plain = _run_milp(config_eos, thermal=True, search=0)
    assert plain.milp_summary["joint"] == {}
    # One more candidate can only help the best start.
    assert reference.milp_summary["best_cost"] <= plain.milp_summary["best_cost"] + 1e-6
