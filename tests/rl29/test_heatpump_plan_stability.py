"""Plan stability of the heat pump schedule (RL29 fork).

On 2026-11-02 the GENETIC plan changed the current heat pump mode with nearly every
15 minute run (SG 2 <-> 3, 4 -> 3 -> 2 -> 1 -> 0 in steps). Causes pinned here:

- a change against the mode in effect now cost nothing (the mode change penalty
  only counted changes inside the plan);
- the hourly gene periods started at the run start, so every run moved all period
  boundaries by one quarter hour and re-decoded the warm start shifted;
- a planned hot water charge topped the tank up again for every few tenths of a
  kelvin, which drew a saw tooth into the planned hot water temperature.
"""

from __future__ import annotations

from types import SimpleNamespace

import pendulum
import pytest

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import DhwMode, HeatingMode
from akkudoktoreos.optimization.genetic import configrequest
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    HeatPumpGeneLayout,
    build_run_plan,
)

TZ = "Europe/Berlin"
DAY = pendulum.datetime(2026, 11, 2, 0, 0, tz=TZ)


def device(**settings) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings.model_validate({"device_id": "hp1", **settings})


def quarter_plan(start, hours=24, **kwargs):
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=kwargs.pop("settings", None) or device(),
        initial_room_c=kwargs.pop("room", 21.0),
        initial_dhw_c=kwargs.pop("dhw", 45.0),
        **kwargs,
    )
    n = hours * 4
    starts = [start.add(minutes=15 * i) for i in range(n)]
    return build_run_plan(params, starts, 0.25, [2.0] * n, [0.0] * n, [0.0003] * n)


# ----------------------------------------------------------------------
# Gene periods on full hours
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("minute", "offset"), [(0, 0), (15, 1), (30, 2), (45, 3)])
def test_periods_start_on_full_hours(minute, offset):
    plan = quarter_plan(DAY.add(hours=8, minutes=minute))
    assert plan.period_offset == offset
    assert plan.n_periods == 24  # the gene count does not depend on the start
    covered = [s for p in range(plan.n_periods) for s in plan.period_slots(p)]
    assert covered == list(range(plan.n_slots))  # every slot exactly once
    assert len(plan.period_slots(0)) == 4 - offset  # until the next full hour
    for slot in plan.period_slots(1):
        assert plan.inputs[slot] is not None
        assert plan.period_of(slot) == 1
    # period 1 starts at 09:00 whatever the run start was
    first = plan.period_slots(1).start
    assert DAY.add(hours=8, minutes=minute).add(minutes=15 * first).minute == 0


def test_warm_start_shift_follows_the_hour_boundaries():
    at_0845 = quarter_plan(DAY.add(hours=8, minutes=45))
    at_0900 = quarter_plan(DAY.add(hours=9))
    at_0830 = quarter_plan(DAY.add(hours=8, minutes=30))
    # 08:45 -> 09:00: the 08:00 period of the earlier run has ended
    assert at_0900.elapsed_periods(1) == 1
    # 08:30 -> 08:45: still in the same period
    assert at_0845.elapsed_periods(1) == 0
    # 08:30 -> 09:45: the 08:00 period has ended, the 09:00 period still runs
    assert quarter_plan(DAY.add(hours=9, minutes=45)).elapsed_periods(5) == 1
    assert at_0830.elapsed_periods(0) == 0
    layout = HeatPumpGeneLayout([at_0900])
    genes = list(range(5)) * 4 + [0] * 4 + [1] * 24
    shifted = layout.shift(genes, 1)
    assert shifted[:23] == genes[1:24]  # one period dropped, the last one repeated
    assert shifted[23] == genes[23]


# ----------------------------------------------------------------------
# Change against the mode in effect
# ----------------------------------------------------------------------


def test_leaving_the_active_mode_costs_the_change_penalty():
    opt = {"optimization": {"mode_change_penalty_amt": 0.05}}
    reduced = [HeatingMode.REDUCED] * 24
    hold = [DhwMode.HOLD] * 24
    free = quarter_plan(DAY.add(hours=20), settings=device(**opt))
    running_normal = quarter_plan(
        DAY.add(hours=20),
        settings=device(**opt),
        active_heating_mode=int(HeatingMode.NORMAL),
        active_dhw_mode=int(DhwMode.HOLD),
    )
    running_reduced = quarter_plan(
        DAY.add(hours=20),
        settings=device(**opt),
        active_heating_mode=int(HeatingMode.REDUCED),
        active_dhw_mode=int(DhwMode.HOLD),
    )
    a = free.evaluate(free.genes_for_modes(reduced, hold))
    b = running_normal.evaluate(running_normal.genes_for_modes(reduced, hold))
    c = running_reduced.evaluate(running_reduced.genes_for_modes(reduced, hold))
    assert a.mode_changes == 0 and c.mode_changes == 0
    assert b.mode_changes == 1
    assert b.extra_cost == pytest.approx(a.extra_cost + 0.05)
    assert c.extra_cost == pytest.approx(a.extra_cost)


def test_a_marginal_saving_no_longer_flips_the_running_mode():
    """A one hour excursion from the running mode now costs two changes."""
    opt = {"optimization": {"mode_change_penalty_amt": 0.05, "active_mode_penalty_amt_per_h": 0.0}}
    plan = quarter_plan(
        DAY.add(hours=20),
        settings=device(**opt),
        active_heating_mode=int(HeatingMode.NORMAL),
        active_dhw_mode=int(DhwMode.HOLD),
    )
    hold = [DhwMode.HOLD] * 24
    keep = plan.evaluate(plan.genes_for_modes([HeatingMode.NORMAL] * 24, hold))
    flip = plan.evaluate(
        plan.genes_for_modes([HeatingMode.REDUCED] + [HeatingMode.NORMAL] * 23, hold)
    )
    assert keep.mode_changes == 0 and flip.mode_changes == 2

    def total(outcome):
        return outcome.extra_cost + 0.0003 * sum(outcome.electric_wh)

    # the excursion pays 2 x 0.05 on top of its energy effect
    assert (
        total(flip) - total(keep)
        >= 0.1 + 0.0003 * (sum(flip.electric_wh) - sum(keep.electric_wh)) - 0.02
    )  # comfort and stored heat effects of one reduced hour stay small


def _previous(schedule):
    return SimpleNamespace(genetic_solution=lambda: SimpleNamespace(heat_pumps=schedule))


def test_active_modes_come_from_the_previous_plan(monkeypatch):
    start = DAY.add(hours=8, minutes=45)
    schedule = {
        "hp1": {
            "start_datetime": start.isoformat(),
            "slot_seconds": 900,
            "heating_mode": ["FORCED", "PREHEAT", "NORMAL", "REDUCED"],
            "dhw_mode": ["HOLD", "HEAT_PUMP", "HOLD", "ROD_60"],
        }
    }
    monkeypatch.setattr(configrequest, "get_ems", lambda: _previous(schedule))
    modes = ConfigOptimizationRequest._active_heat_pump_modes
    assert modes("hp1", start.add(minutes=20)) == (int(HeatingMode.PREHEAT), int(DhwMode.HEAT_PUMP))
    assert modes("hp1", start.add(minutes=59)) == (int(HeatingMode.REDUCED), int(DhwMode.ROD))
    assert modes("hp1", start.add(minutes=60)) == (None, None)  # past the plan
    assert modes("hp1", start.subtract(minutes=1)) == (None, None)
    assert modes("other", start) == (None, None)


def test_active_modes_without_a_previous_plan(monkeypatch):
    monkeypatch.setattr(configrequest, "get_ems", lambda: _previous({}))
    assert ConfigOptimizationRequest._active_heat_pump_modes("hp1", DAY) == (None, None)

    def broken():
        raise RuntimeError("no ems")

    monkeypatch.setattr(configrequest, "get_ems", broken)
    assert ConfigOptimizationRequest._active_heat_pump_modes("hp1", DAY) == (None, None)


# ----------------------------------------------------------------------
# One planned hot water charge per block
# ----------------------------------------------------------------------


def test_a_planned_charge_is_one_charge_not_a_saw_tooth():
    plan = quarter_plan(DAY.add(hours=15), dhw=41.0)
    heating = [HeatingMode.NORMAL] * 24
    dhw = [DhwMode.HEAT_PUMP] * 3 + [DhwMode.HOLD] * 21
    outcome = plan.evaluate(plan.genes_for_modes(heating, dhw), detail=True)
    temps = outcome.detail["dhw_c"]
    heat = outcome.detail["heat_dhw_wh"]
    setpoint = plan.system.dhw.setpoint_c
    reached = next(i for i, t in enumerate(temps) if t >= setpoint - 0.2)
    assert reached < 4  # charged at the start of the block
    # after the charge the tank only cools down: no top-up for a few tenths of a kelvin
    assert all(h == 0.0 for h in heat[reached + 1 : 12])
    assert all(b <= a + 1e-9 for a, b in zip(temps[reached:12], temps[reached + 1 : 12]))


def test_a_new_block_charges_again():
    plan = quarter_plan(DAY.add(hours=6), dhw=41.0)
    heating = [HeatingMode.NORMAL] * 24
    dhw = [DhwMode.HEAT_PUMP] + [DhwMode.HOLD] * 5 + [DhwMode.HEAT_PUMP] + [DhwMode.HOLD] * 17
    outcome = plan.evaluate(plan.genes_for_modes(heating, dhw), detail=True)
    heat = outcome.detail["heat_dhw_wh"]
    assert sum(heat[0:4]) > 0
    second = plan.period_slots(6)
    assert sum(heat[second.start : second.stop]) > 0  # morning draw, second push


def test_the_own_controller_still_reheats_below_its_switch_on_temperature():
    plan = quarter_plan(DAY.add(hours=6), dhw=45.0)
    heating = [HeatingMode.NORMAL] * 24
    dhw = [DhwMode.HEAT_PUMP] * 24  # push done at once (tank full), then the draws
    outcome = plan.evaluate(plan.genes_for_modes(heating, dhw), detail=True)
    temps = outcome.detail["dhw_c"]
    switch_on = plan.system.dhw.setpoint_c - plan.system.dhw.hysteresis_k
    assert min(temps) >= switch_on - 1.0  # never far below the switch-on temperature


@pytest.mark.asyncio
async def test_the_solution_keeps_the_slot_grid_for_the_next_run(config_eos):
    from akkudoktoreos.core.coreabc import get_ems
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
    from akkudoktoreos.utils.datetimeutil import to_datetime

    hours = 24
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                }
            },
        }
    )
    start = to_datetime("2026-11-02T00:00:00", in_timezone=TZ)
    get_ems(init=True).set_start_datetime(start)
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * hours,
                "gesamtlast": [300.0] * hours,
                "strompreis_euro_pro_wh": [0.0003] * hours,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [2.0] * hours,
            "pv_battery": None,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "hp1",
                    "settings": device().model_dump(),
                    "initial_room_c": 20.8,
                    "initial_dhw_c": 45.0,
                    "active_heating_mode": int(HeatingMode.NORMAL),
                    "active_dhw_mode": int(DhwMode.HOLD),
                }
            ],
        }
    )
    solution = GeneticOptimization(fixed_seed=3).optimize_ems(params, ngen=3, individuals=10)
    schedule = solution.heat_pumps["hp1"]
    assert to_datetime(schedule["start_datetime"]) == start
    assert schedule["slot_seconds"] == 3600
    assert len(schedule["heating_mode"]) >= hours
