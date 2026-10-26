"""MILP dispatch model (RL29 fork)."""

from __future__ import annotations

import pytest

from akkudoktoreos.optimization.milp.dispatch import (
    ApplianceRun,
    BatteryModel,
    DispatchInput,
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
