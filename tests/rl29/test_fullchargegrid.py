"""Periodic full charge in MILP and its grid charging exception (RL29 fork).

With grid charging disabled (``max_ac_charge_power_w`` = 0), a due and enforced full
charge may still draw ``full_charge_grid_power_w`` from the grid, but only up to its
deadline. MILP plans the full charge itself (soft target) instead of leaving it to
the GENETIC polish.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.optimization.genetic import configrequest
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import (
    GeneticOptimizationParameters,
)
from akkudoktoreos.optimization.milp.dispatch import (
    BatteryModel,
    DispatchInput,
    solve_dispatch,
)
from akkudoktoreos.optimization.milp.milpoptimization import MilpOptimization
from akkudoktoreos.utils.datetimeutil import DateTime, to_datetime

CENT = 0.01 / 1000.0  # 1 ct/kWh in currency per Wh


def battery(**kw: Any) -> BatteryModel:
    values: dict[str, Any] = dict(
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


def dispatch(bat: BatteryModel, slots: int = 4) -> Any:
    return solve_dispatch(
        DispatchInput(
            pv_wh=[0.0] * slots,
            load_wh=[0.0] * slots,
            price_per_wh=[30 * CENT] * slots,
            feed_in_per_wh=[8 * CENT] * slots,
            battery=bat,
        )
    )


# ---------------------------------------------------------------------------
# Dispatch model
# ---------------------------------------------------------------------------


def test_without_target_no_unprofitable_grid_charge():
    result = dispatch(battery())
    assert result.success
    assert sum(result.charge_grid_wh) == pytest.approx(0.0)


def test_target_is_reached_from_the_grid_within_the_power_limit():
    bat = battery(
        grid_charge_max_wh=[3000.0] * 4,
        target_soc_wh=10000.0,
        target_slot=4,
        target_shortfall_per_wh=10 * 100 / 10000,  # 10 per percent, as GENETIC
    )
    result = dispatch(bat)
    assert result.success
    assert result.soc_wh[4] == pytest.approx(10000.0)
    assert max(result.charge_grid_wh) <= 3000.0 + 1e-6


def test_grid_charge_ends_at_the_window_and_the_rest_is_shortfall():
    bat = battery(
        grid_charge_max_wh=[3000.0, 3000.0, 0.0, 0.0],
        target_soc_wh=10000.0,
        target_slot=4,
        target_shortfall_per_wh=0.1,
    )
    result = dispatch(bat)
    assert result.success
    assert result.charge_grid_wh[2:] == pytest.approx([0.0, 0.0])
    assert result.soc_wh[4] == pytest.approx(6000.0)


def test_target_without_grid_uses_pv_only():
    bat = battery(
        allow_grid_charge=False,
        target_soc_wh=10000.0,
        target_slot=2,
        target_shortfall_per_wh=0.1,
    )
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[6000.0, 6000.0],
            load_wh=[1000.0, 1000.0],
            price_per_wh=[30 * CENT] * 2,
            feed_in_per_wh=[8 * CENT] * 2,
            battery=bat,
        )
    )
    assert result.success
    assert sum(result.charge_grid_wh) == pytest.approx(0.0)
    assert result.soc_wh[2] == pytest.approx(10000.0)


# ---------------------------------------------------------------------------
# Config request: the exception is applied to this run only
# ---------------------------------------------------------------------------


@pytest.fixture
def full_charge_request(config_eos: ConfigEOS, monkeypatch: pytest.MonkeyPatch):
    """Battery without grid charging, full charge every 21 days, overdue (no history)."""
    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "interval_sec": 900,
                    "horizon_hours": 30,
                    "tail_horizon_hours": 0,
                    "terminal_value_mode": "FIXED",
                    "individuals": 10,
                    "generations": 10,
                    "seed": 42,
                },
            },
            "devices": {
                "max_batteries": 1,
                "max_electric_vehicles": 0,
                "max_inverters": 1,
                "max_home_appliances": 0,
                "batteries": {
                    "storage": {
                        "capacity_wh": 10000,
                        "max_charge_power_w": 5000,
                        "full_charge_interval_days": 21,
                        "full_charge_allow_grid": True,
                    }
                },
                "electric_vehicles": {},
                "inverters": {
                    "inverter": {
                        "battery_id": "storage",
                        "max_power_w": 5000,
                        "max_ac_charge_power_w": 0.0,
                    }
                },
                "home_appliances": {},
            },
        }
    )
    start = to_datetime("2026-09-12T10:15:00Z", in_timezone="UTC")
    ems = SimpleNamespace(
        start_datetime=start, observation_datetime=start, genetic_solution=lambda: None
    )
    monkeypatch.setattr(configrequest, "get_ems", lambda: ems)
    dates = [start.subtract(hours=hour) for hour in range(30 * 24, 0, -1)]
    measurement = Mock(key_to_lists=AsyncMock(return_value=(dates, [0.4] * len(dates))))
    monkeypatch.setattr(ConfigOptimizationRequest, "measurement", measurement)
    data = {
        "soc": {"storage": 40},
        "forecasts": {
            "pv_forecast_wh": [0.0] * 192,
            "total_load": [200.0] * 192,
            "electricity_price_per_wh": [0.0003] * 192,
            "feed_in_tariff_per_wh": [0.00008] * 192,
        },
    }
    return config_eos, data


@pytest.mark.asyncio
async def test_full_charge_power_opens_grid_charging_for_this_run(full_charge_request):
    config, data = full_charge_request
    config.devices.batteries["storage"].full_charge_grid_power_w = 3000
    parameters = await ConfigOptimizationRequest.model_validate(data).resolve()
    assert parameters.inverter is not None and parameters.pv_battery is not None
    assert parameters.inverter.max_ac_charge_power_w == 3000.0
    assert parameters.pv_battery.full_charge_grid_power_w == 3000.0
    assert parameters.pv_battery.full_charge_deadline_datetime is not None
    # The configuration itself stays without grid charging.
    assert config.devices.inverters["inverter"].max_ac_charge_power_w == 0.0


@pytest.mark.asyncio
async def test_without_full_charge_power_grid_charging_stays_off(full_charge_request):
    _, data = full_charge_request
    parameters = await ConfigOptimizationRequest.model_validate(data).resolve()
    assert parameters.inverter is not None and parameters.pv_battery is not None
    assert parameters.inverter.max_ac_charge_power_w == 0.0
    assert parameters.pv_battery.full_charge_grid_power_w is None


@pytest.mark.asyncio
async def test_not_enforced_full_charge_keeps_grid_charging_off(full_charge_request):
    config, data = full_charge_request
    config.devices.batteries["storage"].full_charge_grid_power_w = 3000
    config.devices.batteries["storage"].full_charge_allow_grid = False
    parameters = await ConfigOptimizationRequest.model_validate(data).resolve()
    assert parameters.inverter is not None and parameters.pv_battery is not None
    assert parameters.inverter.max_ac_charge_power_w == 0.0
    assert parameters.pv_battery.full_charge_grid_power_w is None


@pytest.mark.asyncio
async def test_allowed_grid_charging_keeps_the_inverter_limit(full_charge_request):
    config, data = full_charge_request
    config.devices.batteries["storage"].full_charge_grid_power_w = 3000
    config.devices.inverters["inverter"].max_ac_charge_power_w = 4000
    parameters = await ConfigOptimizationRequest.model_validate(data).resolve()
    assert parameters.inverter is not None and parameters.pv_battery is not None
    assert parameters.inverter.max_ac_charge_power_w == 4000.0
    assert parameters.pv_battery.full_charge_grid_power_w is None


# ---------------------------------------------------------------------------
# Optimizers: grid charging only until the deadline
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def local_clock(set_other_timezone: Callable[[str], str]) -> None:
    set_other_timezone("Europe/Berlin")


START = "2026-11-10T00:00:00"


def configure(config: ConfigEOS, algorithm: str, hours: int = 8) -> None:
    config.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": algorithm,
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 20,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                },
                "milp": {"hp_search_seconds": 5},
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime(START))


def parameters(
    hours: int = 8, deadline_hours: Optional[int] = 4, grid_power_w: Optional[float] = 3000.0
) -> GeneticOptimizationParameters:
    deadline: Optional[DateTime] = (
        to_datetime(START).add(hours=deadline_hours) if deadline_hours is not None else None
    )
    pv_battery: dict[str, Any] = {
        "device_id": "byd",
        "capacity_wh": 10000,
        "initial_soc_percentage": 43,  # 2 x 2.85 kWh to full
        "min_soc_percentage": 5,
        "max_soc_percentage": 100,
        "max_charge_power_w": 5000,
        "charging_efficiency": 0.95,
        "discharging_efficiency": 0.95,
        "charge_rates": [0.25, 0.5, 1.0],
    }
    if deadline is not None:
        pv_battery["full_charge_soc_percentage"] = 100
        pv_battery["full_charge_deadline_datetime"] = deadline
        pv_battery["full_charge_grid_power_w"] = grid_power_w
    return GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * hours,
                "gesamtlast": [300.0] * hours,
                "strompreis_euro_pro_wh": [0.0003] * hours,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "pv_battery": pv_battery,
            "inverter": {
                "device_id": "inv",
                "max_power_wh": 10000,
                "battery_id": "byd",
                "max_ac_charge_power_w": grid_power_w if deadline is not None else 0.0,
            },
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [2.0] * hours,
            "home_appliances": None,
            "ev": None,
        }
    )


def all_grid_charge_individual(optimizer: GeneticOptimization) -> list[int]:
    """Individual that asks for grid charging at full rate in every slot."""
    n_levels = len(optimizer.bat_possible_charge_values)
    genes = np.full(optimizer.control_end_slot, 2 * n_levels + n_levels - 1, dtype=int)
    return list(optimizer.merge_individual(genes, None, [], None))


@pytest.mark.parametrize("algorithm", ["GENETIC", "MILP"])
def test_simulation_grid_charges_only_before_the_deadline(
    config_eos: ConfigEOS, algorithm: str
) -> None:
    configure(config_eos, algorithm)
    optimizer = (
        MilpOptimization(fixed_seed=1) if algorithm == "MILP" else GeneticOptimization(fixed_seed=42)
    )
    optimizer.optimize_ems(parameters(), ngen=0, individuals=12)
    assert optimizer._ac_charge_until_slot == 4
    optimizer.evaluate_inner(all_grid_charge_individual(optimizer))
    ac = np.asarray(optimizer.simulation.ac_charge_hours)
    assert ac[:4].min() > 0.0, ac
    assert ac[4:].sum() == 0.0, ac


def test_without_grid_exception_the_simulation_keeps_grid_charging_off(
    config_eos: ConfigEOS,
) -> None:
    configure(config_eos, "GENETIC")
    optimizer = GeneticOptimization(fixed_seed=42)
    optimizer.optimize_ems(parameters(deadline_hours=None), ngen=0, individuals=12)
    assert optimizer._ac_charge_until_slot is None
    optimizer.evaluate_inner(all_grid_charge_individual(optimizer))
    assert np.asarray(optimizer.simulation.ac_charge_hours).sum() == 0.0


def test_milp_reaches_the_full_charge_from_the_grid_before_the_deadline(
    config_eos: ConfigEOS,
) -> None:
    configure(config_eos, "MILP")
    optimizer = MilpOptimization(fixed_seed=1)
    solution = optimizer.optimize_ems(parameters(), ngen=0, individuals=20)
    ac = np.asarray(solution.ac_charge)
    soc = np.asarray(solution.result.battery_soc_per_hour)
    assert ac[:4].sum() > 0, ac
    assert ac[4:].sum() == 0, ac
    assert soc[:5].max() >= 99.0, soc


def test_milp_full_charge_terms(config_eos: ConfigEOS) -> None:
    configure(config_eos, "MILP")
    optimizer = MilpOptimization(fixed_seed=1)
    optimizer._ac_charge_until_slot = 2
    optimizer._full_charge_target_soc = 100
    optimizer._full_charge_deadline_slot = 2
    cap, target, slot, per_wh = optimizer._full_charge_terms(4, 3000.0, 10000.0)
    assert cap == pytest.approx([3000.0, 3000.0, 0.0, 0.0])
    assert target == pytest.approx(10000.0)
    assert slot == 2
    assert per_wh > 0.0
    optimizer._full_charge_target_soc = None
    optimizer._ac_charge_until_slot = None
    cap, target, slot, per_wh = optimizer._full_charge_terms(2, None, 10000.0)
    assert cap is None and target is None and slot is None and per_wh == 0.0
