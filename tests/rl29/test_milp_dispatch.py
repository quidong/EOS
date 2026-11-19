"""MILP dispatch model (RL29 fork)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from akkudoktoreos.optimization.milp.dispatch import (
    ApplianceRun,
    BatteryModel,
    DispatchInput,
    SwitchingModel,
    battery_states,
    solve_dispatch,
)

CENT = 0.01 / 1000.0  # 1 ct/kWh in currency per Wh


def battery(**kw) -> BatteryModel:
    values = dict(
        capacity_wh=10000,
        soc_wh=0,
        min_soc_wh=0,
        max_soc_wh=10000,
        max_charge_wh=5000,
        max_discharge_wh=5000,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
    )
    values.update(kw)
    return BatteryModel(**values)


def test_no_battery_balance():
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[1000, 0],
            load_wh=[400, 500],
            price_per_wh=[30 * CENT] * 2,
            feed_in_per_wh=[8 * CENT] * 2,
        )
    )
    assert result.success
    assert result.grid_export_wh == pytest.approx([600, 0])
    assert result.grid_import_wh == pytest.approx([0, 500])
    assert result.objective == pytest.approx(500 * 30 * CENT - 600 * 8 * CENT)


def test_pv_surplus_stored_for_the_evening():
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[3000, 0],
            load_wh=[500, 2000],
            price_per_wh=[30 * CENT] * 2,
            feed_in_per_wh=[8 * CENT] * 2,
            battery=battery(),
        )
    )
    assert result.charge_pv_wh[0] == pytest.approx(2000)
    assert result.discharge_wh[1] == pytest.approx(2000)
    assert result.grid_import_wh == pytest.approx([0, 0])
    assert result.soc_wh[0] == 0 and result.soc_wh[-1] == pytest.approx(0)


def test_grid_charging_in_cheap_slot_with_losses():
    # 10 ct now, 40 ct later: buy now, losses 0.9 x 0.9 still pay off.
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[0, 0],
            load_wh=[0, 2000],
            price_per_wh=[10 * CENT, 40 * CENT],
            feed_in_per_wh=[0, 0],
            battery=battery(charge_efficiency=0.9, discharge_efficiency=0.9),
        )
    )
    assert result.charge_grid_wh[0] == pytest.approx(2000 / 0.81, rel=1e-4)
    assert result.grid_import_wh[1] == pytest.approx(0, abs=1e-3)
    # Not worth it at 30 ct with lcos 15 ct/kWh: 10/0.81 + 15 = 27.3 < 30 still worth.
    result2 = solve_dispatch(
        DispatchInput(
            pv_wh=[0, 0],
            load_wh=[0, 2000],
            price_per_wh=[10 * CENT, 20 * CENT],
            feed_in_per_wh=[0, 0],
            battery=battery(charge_efficiency=0.9, discharge_efficiency=0.9, lcos_per_wh=15 * CENT),
        )
    )
    assert result2.charge_grid_wh[0] == pytest.approx(0, abs=1e-3)


def test_battery_does_not_export_and_respects_min_soc():
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[0, 0],
            load_wh=[100, 100],
            price_per_wh=[30 * CENT] * 2,
            feed_in_per_wh=[50 * CENT] * 2,  # feed-in above price
            battery=battery(soc_wh=8000, min_soc_wh=2000),
        )
    )
    assert sum(result.grid_export_wh) == pytest.approx(0, abs=1e-6)
    assert min(result.soc_wh) >= 2000 - 1e-6
    assert result.discharge_wh == pytest.approx([100, 100])


def test_terminal_value_keeps_energy():
    common = dict(pv_wh=[0], load_wh=[1000], price_per_wh=[20 * CENT], feed_in_per_wh=[0])
    empty = solve_dispatch(DispatchInput(**common, battery=battery(soc_wh=5000)))
    assert empty.discharge_wh[0] == pytest.approx(1000)
    kept = solve_dispatch(
        DispatchInput(
            **common, battery=battery(soc_wh=5000), terminal_segments=[(10000, 30 * CENT)]
        )
    )
    assert kept.discharge_wh[0] == pytest.approx(0, abs=1e-6)
    # Credit flat beyond the last breakpoint: energy above 3 kWh is used now.
    partial = solve_dispatch(
        DispatchInput(**common, battery=battery(soc_wh=5000), terminal_segments=[(3000, 30 * CENT)])
    )
    assert partial.discharge_wh[0] == pytest.approx(1000)


def test_appliance_starts_in_pv_hours():
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[0, 0, 2000, 2000, 0],
            load_wh=[100] * 5,
            price_per_wh=[30 * CENT] * 5,
            feed_in_per_wh=[8 * CENT] * 5,
            appliances=[ApplianceRun(allowed_starts=[0, 1, 2, 3], profile_wh=[1000, 800])],
        )
    )
    assert result.appliance_starts == [2]
    assert result.appliance_wh == pytest.approx([0, 0, 1000, 800, 0])


def test_import_limit_and_infeasible_horizon():
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[0],
            load_wh=[1000],
            price_per_wh=[30 * CENT],
            feed_in_per_wh=[0],
            max_grid_import_wh=500,
        )
    )
    assert not result.success
    # Fork (P5): with an excess price the limit is soft and the overshoot is reported.
    soft = solve_dispatch(
        DispatchInput(
            pv_wh=[0],
            load_wh=[1000],
            price_per_wh=[30 * CENT],
            feed_in_per_wh=[0],
            max_grid_import_wh=500,
            import_excess_cost_per_wh=100 * CENT,
        )
    )
    assert soft.success
    assert soft.import_excess_wh == pytest.approx([500])
    assert soft.objective == pytest.approx(1000 * 30 * CENT + 500 * 100 * CENT)
    with pytest.raises(ValueError):
        solve_dispatch(DispatchInput(pv_wh=[], load_wh=[], price_per_wh=[], feed_in_per_wh=[]))


def test_battery_states_encoding():
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[0, 3000, 0, 0],
            load_wh=[0, 500, 1000, 0],
            price_per_wh=[10 * CENT, 30 * CENT, 40 * CENT, 30 * CENT],
            feed_in_per_wh=[0] * 4,
            battery=battery(max_charge_wh=4000),
        )
    )
    states = battery_states(result, max_charge_wh=4000, charge_levels=[0.25, 0.5, 1.0])
    # Slot 2 discharges; slot 1 charges from PV (idle state); slot 3 idle.
    assert states[2] == 3
    assert states[1] == 0
    assert states[3] == 0
    forced = battery_states(
        type(result)("ok", True, 0.0, [], [], [0, 0], [1500, 4000], [0, 0], [], [], [], 0.0),
        max_charge_wh=4000,
        charge_levels=[0.25, 0.5, 1.0],
    )
    assert forced == [2 * 3 + 1, 2 * 3 + 2]  # 37.5 % -> 50 % level, 100 % -> 100 %


def test_horizon_of_192_slots_is_fast():
    import math

    n = 192
    pv = [max(0.0, 3000 * math.sin(math.pi * ((t % 96) - 28) / 48)) for t in range(n)]
    load = [400 + (800 if 68 <= t % 96 < 88 else 0) for t in range(n)]
    price = [(25 + 10 * math.sin(2 * math.pi * t / 96)) * CENT for t in range(n)]
    result = solve_dispatch(
        DispatchInput(
            pv_wh=pv,
            load_wh=load,
            price_per_wh=price,
            feed_in_per_wh=[8 * CENT] * n,
            battery=battery(
                max_charge_wh=1250,
                max_discharge_wh=1250,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
            ),
            appliances=[ApplianceRun(allowed_starts=list(range(0, 150)), profile_wh=[300] * 8)],
            terminal_segments=[(5000, 20 * CENT), (5000, 10 * CENT)],
        )
    )
    assert result.success
    assert result.solve_seconds < 10


def test_scenarios_keep_reserve_and_share_first_decisions():
    # Slot 0 cheap, slot 1 expensive with uncertain load (500 or 3000 Wh).
    common = dict(
        pv_wh=[0, 0],
        load_wh=[0, 500],
        price_per_wh=[10 * CENT, 40 * CENT],
        feed_in_per_wh=[0, 0],
        battery=battery(),
    )
    single = solve_dispatch(DispatchInput(**common))
    assert single.charge_grid_wh[0] == pytest.approx(500)
    robust = solve_dispatch(
        DispatchInput(
            **common,
            scenarios=[(0.5, [0, 0], [0, 500]), (0.5, [0, 0], [0, 3000])],
            shared_slots=1,
        )
    )
    assert robust.success
    # Buying for the high-load case pays off on average (10 ct vs. 40 ct).
    assert robust.charge_grid_wh[0] == pytest.approx(3000)
    assert robust.objective == pytest.approx(3000 * 10 * CENT, rel=1e-4)


def test_scenario_validation_and_export_limit():
    with pytest.raises(ValueError):
        solve_dispatch(
            DispatchInput(
                pv_wh=[0, 0],
                load_wh=[0, 0],
                price_per_wh=[0, 0],
                feed_in_per_wh=[0, 0],
                scenarios=[(1.0, [0], [0])],
            )
        )
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[5000],
            load_wh=[0],
            price_per_wh=[30 * CENT],
            feed_in_per_wh=[8 * CENT],
            max_grid_export_wh=3000,
        )
    )
    assert result.grid_export_wh == pytest.approx([3000])


# --------------------------------------------------------------------------------------
# Fork: battery mode changes (P1) and charge/discharge lock (P5)
# --------------------------------------------------------------------------------------


def zigzag_input(**kw: Any) -> DispatchInput:
    """Prices alternate by 2 ct: pure cost cycles the battery every slot."""
    n = 8
    values: dict[str, Any] = dict(
        pv_wh=[0.0] * n,
        load_wh=[1000.0] * n,
        price_per_wh=[(30 if t % 2 else 20) * CENT for t in range(n)],
        feed_in_per_wh=[0.0] * n,
        battery=battery(soc_wh=5000, max_charge_wh=2000, max_discharge_wh=2000),
    )
    values.update(kw)
    return DispatchInput(**values)


def modes(result) -> list[int]:
    """Switched mode per slot like ``battery_mode_signature``: grid charge level ignored."""
    states = battery_states(result, max_charge_wh=2000, charge_levels=[0.5, 1.0])
    return [2 if s >= 4 else 1 if s == 2 else 0 for s in states]


def changes(result) -> int:
    m = modes(result)
    return sum(1 for a, b in zip(m, m[1:]) if a != b)


class TestSwitching:
    def test_change_cost_must_be_earned(self):
        free = solve_dispatch(zigzag_input(switching=SwitchingModel(tie_break=False)))
        assert free.mode_changes is not None
        assert free.mode_changes == changes(free) and free.mode_changes >= 4
        priced = solve_dispatch(
            zigzag_input(switching=SwitchingModel(change_cost=1.0, tie_break=False))
        )
        assert priced.mode_changes == changes(priced) <= 1
        # The change penalty is part of the objective, the grid cost is higher.
        assert priced.grid_cost([0.0] * 8, [0.0] * 8) == 0.0
        assert sum(priced.grid_import_wh) >= sum(free.grid_import_wh) - 1e-6

    def test_tie_break_keeps_cost_and_removes_changes(self):
        # Flat price: discharging 2 kWh anywhere costs the same.
        data = DispatchInput(
            pv_wh=[0.0] * 6,
            load_wh=[500.0] * 6,
            price_per_wh=[30 * CENT] * 6,
            feed_in_per_wh=[0.0] * 6,
            battery=battery(soc_wh=2000),
            switching=SwitchingModel(),
        )
        result = solve_dispatch(data)
        plain = solve_dispatch(replace(data, switching=None))
        assert result.objective == pytest.approx(plain.objective, abs=1e-4)
        assert result.mode_changes is not None and result.mode_changes <= 1
        assert result.tie_break is not None

    def test_hold_time_penalises_short_modes(self):
        data = zigzag_input(
            switching=SwitchingModel(change_cost=0.001, hold_slots=3, hold_cost=1.0)
        )
        result = solve_dispatch(data)
        states = modes(result)
        starts = [t for t in range(1, len(states)) if states[t] != states[t - 1]]
        assert all(b - a >= 3 for a, b in zip(starts, starts[1:]))

    def test_initial_state_counts_first_change(self):
        data = zigzag_input(
            price_per_wh=[30 * CENT] * 8,
            switching=SwitchingModel(initial_state=2, change_cost=0.1, tie_break=False),
        )
        result = solve_dispatch(data)
        assert result.mode_changes == 1  # grid charge -> discharge in the first slot


def test_no_charge_and_discharge_at_once_at_negative_prices():
    n = 4
    data = DispatchInput(
        pv_wh=[0.0] * n,
        load_wh=[1000.0] * n,
        price_per_wh=[-10 * CENT] * n,
        feed_in_per_wh=[0.0] * n,
        battery=battery(
            soc_wh=5000, charge_efficiency=0.9, discharge_efficiency=0.9, max_charge_wh=2000,
            max_discharge_wh=2000,
        ),
        lock_charge_discharge="never",
    )
    cycling = solve_dispatch(data)
    assert any(
        c > 1 and d > 1 for c, d in zip(cycling.charge_grid_wh, cycling.discharge_wh)
    )  # the unlocked model burns energy for money
    locked = solve_dispatch(replace(data, lock_charge_discharge="negative_prices"))
    for c_pv, c_grid, d in zip(locked.charge_pv_wh, locked.charge_grid_wh, locked.discharge_wh):
        assert c_pv + c_grid < 1e-6 or d < 1e-6


# --------------------------------------------------------------------------------------
# Fork: budgeted hedge against forecast errors (P2)
# --------------------------------------------------------------------------------------


class TestUncertaintyBudget:
    def test_worst_deviation_budget_and_window(self):
        from akkudoktoreos.optimization.milp.dispatch import (
            UncertaintyBudget,
            worst_deviation,
        )

        hedge = UncertaintyBudget(pv_dev=0.0, load_dev=0.5, budget_slots=2, day_slots=3)
        worst = worst_deviation([0.0] * 5, [100, 400, 200, 300, 0], hedge, 1000.0)
        # Largest two deviations of the last three slots.
        assert worst == pytest.approx([50, 250, 300, 350, 250])
        half = UncertaintyBudget(pv_dev=0.0, load_dev=0.5, budget_slots=1.5, day_slots=96)
        assert worst_deviation([0.0] * 2, [400, 200], half, 1000.0) == pytest.approx([200, 250])

    def test_surplus_the_battery_cannot_take_is_no_risk(self):
        from akkudoktoreos.optimization.milp.dispatch import (
            UncertaintyBudget,
            worst_deviation,
        )

        hedge = UncertaintyBudget(pv_dev=0.3, load_dev=0.0, budget_slots=1)
        assert worst_deviation([5000], [0], hedge, 1000.0) == [0.0]

    def test_hedge_buys_reserve_for_an_expensive_evening(self):
        from akkudoktoreos.optimization.milp.dispatch import UncertaintyBudget

        data = DispatchInput(
            pv_wh=[0.0] * 5,
            load_wh=[0.0] + [1000.0] * 4,
            price_per_wh=[10 * CENT] + [40 * CENT] * 4,
            feed_in_per_wh=[0.0] * 5,
            battery=battery(soc_wh=0, max_charge_wh=8000, max_discharge_wh=8000),
        )
        plain = solve_dispatch(data)
        hedged = solve_dispatch(
            replace(data, uncertainty=UncertaintyBudget(pv_dev=0.0, load_dev=0.2, budget_slots=4))
        )
        assert plain.charge_grid_wh[0] == pytest.approx(4000, abs=1)
        assert hedged.charge_grid_wh[0] == pytest.approx(4800, abs=1)
        # The reported grid cost is the plan's; the premium is the extra charge.
        assert hedged.grid_cost(data.price_per_wh, data.feed_in_per_wh) == pytest.approx(
            4800 * 10 * CENT, abs=1e-6
        )

    def test_flat_tariff_buys_no_reserve(self):
        from akkudoktoreos.optimization.milp.dispatch import UncertaintyBudget

        data = DispatchInput(
            pv_wh=[0.0] * 4,
            load_wh=[1000.0] * 4,
            price_per_wh=[30 * CENT] * 4,
            feed_in_per_wh=[0.0] * 4,
            # With losses a reserve bought now costs more than importing the shortfall.
            battery=battery(soc_wh=0, charge_efficiency=0.95, discharge_efficiency=0.95),
            uncertainty=UncertaintyBudget(pv_dev=0.0, load_dev=0.3, budget_slots=4),
        )
        assert sum(solve_dispatch(data).charge_grid_wh) == pytest.approx(0, abs=1e-6)

    def test_off_without_budget(self):
        from akkudoktoreos.optimization.milp.dispatch import UncertaintyBudget

        base = zigzag_input()
        plain = solve_dispatch(base)
        off = solve_dispatch(
            replace(base, uncertainty=UncertaintyBudget(budget_slots=0))
        )
        assert off.objective == pytest.approx(plain.objective)


# --------------------------------------------------------------------------------------
# Fork: electric vehicle (F1)
# --------------------------------------------------------------------------------------


def ev_model(**kw: Any) -> Any:
    from akkudoktoreos.optimization.milp.dispatch import EVModel

    values: dict[str, Any] = dict(
        capacity_wh=50000,
        soc_wh=10000,
        max_soc_wh=50000,
        max_charge_wh=11000,
        min_rate=0.375,
        charge_efficiency=0.9,
        target_wh=20000,
        target_slot=6,
        shortfall_per_wh=10 * 100 / 50000,
    )
    values.update(kw)
    return EVModel(**values)


class TestElectricVehicle:
    def test_charges_in_the_cheapest_slots_before_the_deadline(self):
        prices = [30, 30, 10, 12, 30, 30, 5, 5]
        result = solve_dispatch(
            DispatchInput(
                pv_wh=[0.0] * 8,
                load_wh=[0.0] * 8,
                price_per_wh=[p * CENT for p in prices],
                feed_in_per_wh=[0.0] * 8,
                ev=ev_model(),
            )
        )
        assert result.success
        charge = result.ev_charge_wh
        # 10 kWh stored = 11.1 kWh raw in the two cheap slots; the remainder of 111 Wh
        # is below the minimum rate (4125 Wh), so the cheaper slot gives way.
        assert charge[2] + charge[3] == pytest.approx(10000 / 0.9, abs=1)
        assert charge[3] == pytest.approx(0.375 * 11000, abs=1)
        assert result.ev_soc_wh[6] == pytest.approx(20000, abs=1)
        assert sum(charge[:2]) + sum(charge[4:]) == pytest.approx(0, abs=1e-6)

    def test_minimum_rate_is_semi_continuous(self):
        result = solve_dispatch(
            DispatchInput(
                pv_wh=[0.0] * 4,
                load_wh=[0.0] * 4,
                price_per_wh=[10 * CENT] * 4,
                feed_in_per_wh=[0.0] * 4,
                ev=ev_model(soc_wh=19000, target_slot=4),
            )
        )
        positive = [c for c in result.ev_charge_wh if c > 1e-6]
        assert positive and all(c >= 0.375 * 11000 - 1e-6 for c in positive)

    def test_pv_surplus_goes_into_the_car(self):
        result = solve_dispatch(
            DispatchInput(
                pv_wh=[6000.0] * 4,
                load_wh=[500.0] * 4,
                price_per_wh=[30 * CENT] * 4,
                feed_in_per_wh=[8 * CENT] * 4,
                # Target needs exactly the PV surplus of 4 x 5.5 kWh.
                ev=ev_model(target_wh=10000 + 22000 * 0.9, target_slot=None),
            )
        )
        assert sum(result.ev_charge_wh) == pytest.approx(4 * 5500, abs=1)
        assert sum(result.grid_import_wh) == pytest.approx(0, abs=1e-6)

    def test_unreachable_target_is_soft_and_dimming_caps(self):
        result = solve_dispatch(
            DispatchInput(
                pv_wh=[0.0] * 4,
                load_wh=[0.0] * 4,
                price_per_wh=[30 * CENT] * 4,
                feed_in_per_wh=[0.0] * 4,
                ev=ev_model(target_wh=50000, target_slot=2, slot_cap_wh=[4200, 4200]),
            )
        )
        assert result.success
        assert result.ev_charge_wh[:2] == pytest.approx([4200, 4200], abs=1)

    def test_start_cost_keeps_one_charging_block(self):
        prices = [10, 11, 30, 10, 11, 30, 10, 11]
        result = solve_dispatch(
            DispatchInput(
                pv_wh=[0.0] * 8,
                load_wh=[0.0] * 8,
                price_per_wh=[p * CENT for p in prices],
                feed_in_per_wh=[0.0] * 8,
                ev=ev_model(
                    soc_wh=0, target_wh=29700, target_slot=8, max_charge_wh=11000,
                    min_rate=0.375, start_cost=5.0,
                ),
            )
        )
        on = [c > 1 for c in result.ev_charge_wh]
        starts = sum(1 for t, x in enumerate(on) if x and (t == 0 or not on[t - 1]))
        assert starts == 1

    def test_house_battery_may_serve_the_car_and_coarse_slots_keep_it(self):
        data = DispatchInput(
            pv_wh=[0.0] * 12,
            load_wh=[0.0] * 12,
            price_per_wh=[40 * CENT] * 4 + [10 * CENT] * 8,
            feed_in_per_wh=[0.0] * 12,
            ev=ev_model(target_slot=12),
            coarsen_after_slots=4,
        )
        result = solve_dispatch(data)
        assert len(result.ev_charge_wh) == 12 and len(result.ev_soc_wh) == 13
        assert sum(result.ev_charge_wh[:4]) == pytest.approx(0, abs=1e-6)
        assert result.ev_soc_wh[-1] == pytest.approx(20000, abs=1)


def test_terminal_credit_values_usable_energy_like_the_simulation():
    """Fork: only (soc - min_soc) * discharge efficiency is credited, as in GENETIC."""
    segments = [(1000.0, 50 * CENT), (9000.0, 30 * CENT)]
    data = DispatchInput(
        pv_wh=[0.0],
        load_wh=[1000.0],
        price_per_wh=[40 * CENT],
        feed_in_per_wh=[0.0],
        battery=battery(soc_wh=3000, min_soc_wh=1000, discharge_efficiency=0.9),
        terminal_segments=segments,
        terminal_dc_to_ac=0.9,
        terminal_offset_wh=1000,
    )
    result = solve_dispatch(data)
    # Usable AC content without discharge: (3000 - 1000) * 0.9 = 1800 Wh: the first
    # 1000 Wh at 50 ct, the next 800 at 30 ct. A DC Wh delivers 0.9 Wh at 40 ct (36 ct):
    # worth more than the 30 ct segment (27 ct per DC Wh), less than the 50 ct one
    # (45 ct): the battery delivers exactly the 800 AC Wh of the 30 ct segment.
    assert result.discharge_wh[0] == pytest.approx(800, abs=1)
    # Without the offset the minimum SoC would earn the 50 ct segment for free.
    unshifted = solve_dispatch(replace(data, terminal_offset_wh=0.0))
    assert unshifted.objective < result.objective - 0.1
    # A start below the minimum SoC stays feasible.
    low = solve_dispatch(replace(data, battery=battery(soc_wh=500, min_soc_wh=1000)))
    assert low.success


# --------------------------------------------------------------------------------------
# Fork: preferences at equal cost (P5), battery export (P12), EV battery lock (P8)
# --------------------------------------------------------------------------------------


class TestPreferences:
    def flat(self, **kw: Any) -> DispatchInput:
        values: dict[str, Any] = dict(
            pv_wh=[0.0] * 8,
            load_wh=[0.0] * 8,
            price_per_wh=[30 * CENT] * 8,
            feed_in_per_wh=[0.0] * 8,
            battery=battery(soc_wh=0, max_charge_wh=4000),
            ev=ev_model(
                soc_wh=0, target_wh=8000 * 0.9, target_slot=8, min_rate=0.0, start_cost=0.0
            ),
        )
        values.update(kw)
        return DispatchInput(**values)

    def test_demand_peaks_are_flattened_at_the_same_cost(self):
        plain = solve_dispatch(self.flat())
        flat = solve_dispatch(self.flat(preference="attenuate_demand_peaks"))
        assert flat.objective == pytest.approx(plain.objective, abs=2e-3)
        # 8 kWh over 8 slots: 1 kWh each instead of whatever the solver picked.
        assert max(flat.grid_import_wh) == pytest.approx(1000, abs=1)
        assert flat.preference is not None and "attenuate_demand_peaks" in flat.preference

    def test_charge_before_export_fills_the_battery_first(self):
        n = 4
        data = DispatchInput(
            pv_wh=[3000.0] * n,
            load_wh=[0.0] * n,
            price_per_wh=[30 * CENT] * n,
            feed_in_per_wh=[8 * CENT] * n,
            battery=battery(soc_wh=0, capacity_wh=6000, max_soc_wh=6000, max_charge_wh=3000),
            terminal_segments=[(6000.0, 8 * CENT)],
            preference="charge_before_export",
        )
        result = solve_dispatch(data)
        # Storing is worth exactly the feed-in: a tie; the battery fills first.
        # (within the cost tolerance of 0.1 ct, i.e. about 12 Wh at 8 ct)
        assert result.charge_pv_wh[:2] == pytest.approx([3000, 3000], abs=20)
        assert result.grid_export_wh[:2] == pytest.approx([0, 0], abs=20)

    def test_unknown_preference_keeps_the_plan(self):
        result = solve_dispatch(self.flat(preference="bogus"))
        assert result.success and result.preference == "bogus: unknown"


class TestBatteryExport:
    def test_battery_exports_at_a_high_feed_in_and_never_serves_the_house_then(self):
        data = DispatchInput(
            pv_wh=[0.0] * 3,
            load_wh=[500.0] * 3,
            price_per_wh=[30 * CENT] * 3,
            feed_in_per_wh=[0.0, 60 * CENT, 0.0],
            battery=battery(soc_wh=3000, max_discharge_wh=2000, allow_grid_export=True),
        )
        result = solve_dispatch(data)
        assert result.battery_export_wh == pytest.approx([0, 2000, 0], abs=1)
        assert result.discharge_wh[1] == pytest.approx(0, abs=1e-6)
        assert result.grid_import_wh[1] == pytest.approx(500, abs=1)
        assert result.grid_export_wh[1] == pytest.approx(2000, abs=1)

    def test_without_export_only_pv_is_fed_in(self):
        data = DispatchInput(
            pv_wh=[0.0],
            load_wh=[0.0],
            price_per_wh=[30 * CENT],
            feed_in_per_wh=[60 * CENT],
            battery=battery(soc_wh=3000),
        )
        result = solve_dispatch(data)
        assert result.battery_export_wh == []
        assert result.grid_export_wh == pytest.approx([0])


def test_battery_does_not_feed_the_car():
    data = DispatchInput(
        pv_wh=[0.0] * 2,
        load_wh=[500.0] * 2,
        price_per_wh=[40 * CENT] * 2,
        feed_in_per_wh=[0.0] * 2,
        battery=battery(soc_wh=10000, max_discharge_wh=11000),
        ev=ev_model(soc_wh=10000, target_wh=10000 + 11000 * 0.9, target_slot=1),
    )
    free = solve_dispatch(data)
    assert free.discharge_wh[0] > 5000  # the house battery charges the car
    blocked = solve_dispatch(
        replace(data, ev=ev_model(soc_wh=10000, target_wh=10000 + 11000 * 0.9, target_slot=1,
                                  block_battery_discharge=True))
    )
    assert blocked.ev_charge_wh[0] == pytest.approx(11000, abs=1)
    assert blocked.discharge_wh[0] == pytest.approx(0, abs=1e-6)
    assert blocked.discharge_wh[1] == pytest.approx(500, abs=1)
