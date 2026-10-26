"""MILP optimization on the GENETIC machinery (RL29 fork)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from deap import creator

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import (
    GeneticOptimizationParameters,
)
from akkudoktoreos.optimization.milp.milpoptimization import (
    MilpOptimization,
    terminal_segments,
)
from akkudoktoreos.optimization.optimization import OptimizationAlgorithm
from akkudoktoreos.utils.datetimeutil import to_datetime


def configure(config: ConfigEOS, hours: int = 12, **milp: Any) -> None:
    config.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "MILP",
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 40,
                    "generations": 20,
                    "terminal_value_mode": "FIXED",
                },
                "milp": {"hp_search_seconds": 5, **milp},
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-03T00:00:00"))


def parameters(hours: int = 12, prices=None, pv=None, battery=True, **kwargs: Any):
    data: dict[str, Any] = {
        "ems": {
            "pv_prognose_wh": pv or [0.0] * hours,
            "gesamtlast": [500.0] * hours,
            "strompreis_euro_pro_wh": prices or [0.0003] * hours,
            "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
            "preis_euro_pro_wh_akku": 0.0001,
        },
        "inverter": {
            "device_id": "inv",
            "max_power_wh": 10000,
            "battery_id": "bat" if battery else None,
        },
        "forecast_interval_seconds": 3600,
        "temperature_forecast": [2.0] * hours,
        "pv_battery": (
            {
                "device_id": "bat",
                "capacity_wh": 10000,
                "initial_soc_percentage": 20,
                "min_soc_percentage": 5,
                "max_charge_power_w": 5000,
                "charging_efficiency": 0.95,
                "discharging_efficiency": 0.95,
                "charge_rates": [0.25, 0.5, 1.0],
            }
            if battery
            else None
        ),
        "ev": None,
        **kwargs,
    }
    return GeneticOptimizationParameters.model_validate(data)


def fitness(optimizer: GeneticOptimization, solution, params) -> float:
    return optimizer.evaluate(creator.Individual(list(solution.start_solution)), params, 0, False)[
        0
    ]


def test_algorithm_is_selectable(config_eos: ConfigEOS):
    configure(config_eos)
    assert config_eos.optimization.algorithm == OptimizationAlgorithm.MILP
    assert "MILP" in config_eos.optimization.algorithms
    assert config_eos.optimization.milp.polish_generations == 30


def test_terminal_segments():
    class Curve:
        energy_wh = [0.0, 2000.0, 5000.0]
        value_euro = [0.0, 0.6, 0.9]

    assert terminal_segments(Curve(), 0.0, 10000) == [
        (2000.0, 0.0003),
        (3000.0, pytest.approx(0.0001)),
    ]
    assert terminal_segments(None, 0.0002, 10000) == [(10000.0, 0.0002)]
    assert terminal_segments(None, 0.0, 10000) == []


def test_battery_arbitrage_seed_is_optimal_without_polish(config_eos: ConfigEOS):
    """Cheap night, expensive evening: the MILP seed alone charges early and discharges late."""
    configure(config_eos, polish_generations=0)
    prices = [0.00010] * 4 + [0.00030] * 4 + [0.00060] * 4
    params = parameters(prices=prices)
    optimizer = MilpOptimization(fixed_seed=1)
    solution = optimizer.optimize_ems(params, ngen=0, individuals=20)
    assert optimizer.milp_summary["seeds"] >= 1
    ac = np.asarray(solution.ac_charge)
    discharge = np.asarray(solution.discharge_allowed)
    assert ac[:4].sum() > 0, ac
    assert ac[8:].sum() == 0
    assert discharge[8:].all()
    soc = np.asarray(solution.result.battery_soc_per_hour)
    assert soc[8] > soc[0]


def test_milp_not_worse_than_genetic_on_the_same_budget(config_eos: ConfigEOS):
    configure(config_eos, polish_generations=5, polish_individuals=20)
    prices = [
        0.00012,
        0.0001,
        0.0001,
        0.00011,
        0.0003,
        0.00035,
        0.0004,
        0.0005,
        0.00055,
        0.0006,
        0.0004,
        0.0003,
    ]
    pv = [0, 0, 0, 0, 500, 2000, 4000, 4000, 2000, 500, 0, 0]
    params = parameters(prices=prices, pv=[float(v) for v in pv])
    milp = MilpOptimization(fixed_seed=5)
    milp_solution = milp.optimize_ems(params, ngen=5, individuals=20)
    genetic = GeneticOptimization(fixed_seed=5)
    genetic_solution = genetic.optimize_ems(params, ngen=5, individuals=20)
    assert fitness(milp, milp_solution, params) <= fitness(genetic, genetic_solution, params) + 1e-9


def heat_pump(**settings: Any) -> dict[str, Any]:
    device = HeatPumpCommonSettings(device_id="wbb12", **settings)
    return {"device_id": "wbb12", "settings": device, "initial_room_c": 21.0, "initial_dhw_c": 45.0}


def test_heat_pump_search_shifts_heating_to_cheap_hours(config_eos: ConfigEOS):
    configure(config_eos, hours=8, polish_generations=0)
    prices = [0.00010] * 3 + [0.00030] * 2 + [0.00060] * 3
    params = parameters(hours=8, prices=prices, battery=False, heat_pumps=[heat_pump()])
    optimizer = MilpOptimization(fixed_seed=3)
    solution = optimizer.optimize_ems(params, ngen=0, individuals=20)
    assert optimizer.milp_summary["solves"] > 3
    hp = np.asarray(solution.result.heat_pump_wh_per_hour)
    assert hp[:3].mean() > hp[5:].mean()
    # Better than the status quo heat pump schedule.
    best = list(solution.start_solution)
    status_quo = list(best)
    optimizer._set_heat_pump_genes(status_quo, optimizer.heat_pump_layout.default_genes())
    assert (
        optimizer.evaluate(creator.Individual(best), params, 0, False)[0]
        < optimizer.evaluate(creator.Individual(status_quo), params, 0, False)[0]
    )


def test_heat_pump_and_battery_with_appliance(config_eos: ConfigEOS):
    configure(config_eos, hours=12, polish_generations=2, polish_individuals=16)
    prices = [0.0003] * 12
    pv = [0, 0, 0, 1000, 3000, 4000, 4000, 3000, 1000, 0, 0, 0]
    params = parameters(
        prices=prices,
        pv=[float(v) for v in pv],
        heat_pumps=[heat_pump()],
        home_appliances=[
            {
                "device_id": "washer",
                "consumption_wh": 1500,
                "duration_h": 2,
            }
        ],
    )
    optimizer = MilpOptimization(fixed_seed=2)
    solution = optimizer.optimize_ems(params, ngen=2, individuals=16)
    starts = solution.appliance_starts["washer"]
    assert starts, solution.appliance_starts
    start_hour = starts[0].hour
    assert 3 <= start_hour <= 7  # inside the PV window
    assert (
        len(solution.start_solution)
        == 12 + optimizer.heat_pump_layout.n_genes + optimizer.appliance_layout.n_genes
    )


def test_electric_vehicle_falls_back_to_genetic(config_eos: ConfigEOS, caplog):
    configure(config_eos)
    optimizer = MilpOptimization(fixed_seed=1)
    optimizer.optimize_ev = True
    assert optimizer._extra_seeds(parameters(), 0) == []


@pytest.mark.asyncio
async def test_ems_run_with_milp_publishes_solution_plan_and_history_hook(
    configured_request, monkeypatch
):
    from unittest.mock import AsyncMock, Mock

    from akkudoktoreos.core.ems import EnergyManagement
    from akkudoktoreos.core.emsettings import EnergyManagementMode
    from akkudoktoreos.core.historyrecorder import HistoryRecorder
    from akkudoktoreos.optimization.genetic import configrequest
    from akkudoktoreos.optimization.genetic.configrequest import (
        ConfigOptimizationRequest,
    )

    config, fake, _, data = configured_request
    config.merge_settings_from_dict(
        {"optimization": {"milp": {"polish_generations": 2, "polish_individuals": 10}}}
    )
    ems = EnergyManagement()
    monkeypatch.setattr(configrequest, "get_ems", lambda: ems)
    monkeypatch.setattr(EnergyManagement, "prediction", Mock(update_data=AsyncMock()))
    monkeypatch.setattr(EnergyManagement, "adapter", Mock(update_data=AsyncMock()))
    scheduled = []
    monkeypatch.setattr(HistoryRecorder, "schedule", lambda self, *args: scheduled.append(args))
    request = ConfigOptimizationRequest.model_validate(data)
    solution = await ems.run(
        start_datetime=fake.start_datetime.add(minutes=2),
        mode=EnergyManagementMode.OPTIMIZATION,
        algorithm=OptimizationAlgorithm.MILP,
        genetic_parameters=request,
        genetic_seed=42,
    )
    assert solution is not None
    assert solution is ems.genetic_solution()
    assert ems.optimization_solution() is not None and ems.plan() is not None
    assert len(scheduled) == 1 and scheduled[0][3] == "MILP"


def test_scenarios_and_import_limit_fallback(config_eos: ConfigEOS, tmp_path):
    configure(
        config_eos,
        polish_generations=0,
        scenarios=True,
        load_scenario_spread=None,
        max_grid_import_w=100,  # far below the 500 Wh load: infeasible
    )
    config_eos.merge_settings_from_dict({"general": {"data_folder_path": str(tmp_path)}})
    (tmp_path / "rl29").mkdir()
    (tmp_path / "rl29" / "load_ml.json").write_text(
        '{"used": true, "spread_low": 0.3, "spread_high": 0.4}'
    )
    prices = [0.00010] * 4 + [0.00030] * 4 + [0.00060] * 4
    optimizer = MilpOptimization(fixed_seed=1)
    assert optimizer._load_spread() == pytest.approx(0.4)
    solution = optimizer.optimize_ems(parameters(prices=prices), ngen=0, individuals=20)
    assert optimizer._ignore_import_limit is True
    assert optimizer.milp_summary["seeds"] >= 1
    assert np.asarray(solution.ac_charge)[:4].sum() > 0
