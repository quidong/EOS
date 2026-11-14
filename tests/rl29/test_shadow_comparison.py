"""Shadow comparison of GENETIC and MILP on identical inputs (RL29 fork)."""

import asyncio
import inspect
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from akkudoktoreos.core.history import summary_from
from akkudoktoreos.core.shadow import (
    ShadowResult,
    ShadowRunner,
    shadow_algorithms_for,
    solution_objective,
    summarize,
)
from akkudoktoreos.optimization.optimization import (
    OptimizationAlgorithm,
    OptimizationCommonSettings,
)


def fake_solution(objective: float, costs: float = 5.0, revenue: float = 1.0) -> Any:
    return SimpleNamespace(
        fitness_history={"min": [objective + 2.0, objective + 1.0, objective]},
        result=SimpleNamespace(total_costs=costs, total_revenue=revenue),
    )


def test_setting_defaults_to_no_shadow():
    settings = OptimizationCommonSettings()
    assert settings.shadow_algorithms == []
    assert shadow_algorithms_for(settings, OptimizationAlgorithm.GENETIC) == []


def test_shadow_algorithms_skip_active_unsupported_and_duplicates():
    settings = OptimizationCommonSettings(shadow_algorithms=["GENETIC", "MILP", "GENETIC0", "MILP"])
    assert shadow_algorithms_for(settings, "MILP") == [OptimizationAlgorithm.GENETIC]
    assert shadow_algorithms_for(settings, "GENETIC") == [OptimizationAlgorithm.MILP]
    # GENETIC0 runs on other parameters and cannot shadow or be shadowed.
    assert shadow_algorithms_for(settings, "GENETIC0") == []


def test_solution_objective_uses_final_best_fitness_and_net():
    objective, net = solution_objective(fake_solution(9.5, costs=10.0, revenue=0.8))
    assert objective == pytest.approx(9.5)
    assert net == pytest.approx(9.2)
    assert solution_objective(SimpleNamespace()) == (None, None)


@pytest.mark.asyncio
async def test_runner_compares_on_a_parameter_copy_and_reports():
    runner = ShadowRunner()
    calls: list[tuple[OptimizationAlgorithm, Any]] = []
    stored: list[ShadowResult] = []

    def optimize(algorithm: OptimizationAlgorithm, parameters: Any) -> Any:
        calls.append((algorithm, parameters))
        return fake_solution(9.0)

    parameters = object()
    started = runner.schedule(
        [OptimizationAlgorithm.GENETIC],
        parameters,
        OptimizationAlgorithm.MILP,
        fake_solution(9.5),
        30.0,
        optimize,
        on_result=stored.append,
    )
    assert started
    await runner.wait_idle()

    assert calls == [(OptimizationAlgorithm.GENETIC, parameters)]
    assert len(runner.results) == 1 and stored == list(runner.results)
    result = runner.results[0]
    assert result.active_algorithm == "MILP" and result.shadow_algorithm == "GENETIC"
    assert result.delta == pytest.approx(-0.5)
    summary = runner.summary()
    assert summary["GENETIC_vs_MILP"] == {
        "runs": 1,
        "shadow_better": 1,
        "mean_delta_amt": -0.5,
        "shadow_better_share": 1.0,
    }


@pytest.mark.asyncio
async def test_runner_skips_while_busy_and_survives_failures():
    runner = ShadowRunner()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow(algorithm: OptimizationAlgorithm, parameters: Any) -> Any:
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        raise RuntimeError("solver crashed")

    assert runner.schedule(
        [OptimizationAlgorithm.MILP], object(), "GENETIC", fake_solution(1.0), 1.0, slow
    )
    assert runner.busy
    assert not runner.schedule(
        [OptimizationAlgorithm.MILP], object(), "GENETIC", fake_solution(1.0), 1.0, slow
    )
    release.set()
    await runner.wait_idle()
    assert not runner.busy
    assert len(runner.results) == 0
    assert "solver crashed" in (runner.last_error or "")


def test_runner_does_nothing_without_algorithms_or_parameters():
    runner = ShadowRunner()
    assert not runner.schedule([], object(), "GENETIC", fake_solution(1.0), 1.0, lambda a, p: None)
    assert not runner.schedule(
        [OptimizationAlgorithm.MILP], None, "GENETIC", fake_solution(1.0), 1.0, lambda a, p: None
    )


def test_summary_counts_pairs_and_ignores_missing_objectives():
    rows = [
        {"active_algorithm": "MILP", "shadow_algorithm": "GENETIC", "delta": -0.2},
        {"active_algorithm": "MILP", "shadow_algorithm": "GENETIC", "delta": 0.4},
        {"active_algorithm": "GENETIC", "shadow_algorithm": "MILP", "delta": 0.1},
        {"active_algorithm": "GENETIC", "shadow_algorithm": "MILP", "delta": None},
    ]
    summary = summarize(rows)
    assert summary["GENETIC_vs_MILP"]["runs"] == 2
    assert summary["GENETIC_vs_MILP"]["mean_delta_amt"] == pytest.approx(0.1)
    assert summary["GENETIC_vs_MILP"]["shadow_better_share"] == pytest.approx(0.5)
    assert summary["MILP_vs_GENETIC"]["runs"] == 1
    assert summary_from([], [], [], rows)["shadow"] == summary
    assert summary_from([], [], [])["shadow"] == {}


def test_result_dict_is_serialisable():
    result = ShadowResult(
        issued_at=datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc),
        active_algorithm="MILP",
        shadow_algorithm="GENETIC",
        active_objective=10.0,
        shadow_objective=None,
        active_net_amt=9.0,
        shadow_net_amt=None,
        active_duration_s=30.0,
        shadow_duration_s=180.0,
    )
    data = result.as_dict()
    assert data["issued_at"] == "2026-11-09T20:00:00+00:00"
    assert data["delta"] is None


@pytest.mark.asyncio
async def test_ems_schedules_shadow_only_for_automatic_runs(config_eos, monkeypatch):
    """The EMS hands a parameter copy, the active solution and its budget to the runner."""
    from akkudoktoreos.core import ems as ems_module
    from akkudoktoreos.core.coreabc import get_ems

    config_eos.merge_settings_from_dict(
        {"optimization": {"shadow_algorithms": ["GENETIC", "MILP"]}}
    )
    captured: dict[str, Any] = {}

    def fake_schedule(
        self, algorithms, parameters, active, solution, duration, optimize, on_result=None
    ):
        captured.update(
            algorithms=algorithms, parameters=parameters, active=active, optimize=optimize
        )
        return True

    monkeypatch.setattr(ShadowRunner, "schedule", fake_schedule)
    ems = get_ems()
    ems._schedule_shadow(
        [OptimizationAlgorithm.GENETIC],
        "params-copy",
        OptimizationAlgorithm.MILP,
        fake_solution(1.0),
        12.0,
        start_hour=7,
        seed=None,
    )
    assert captured["algorithms"] == [OptimizationAlgorithm.GENETIC]
    assert captured["parameters"] == "params-copy"
    assert captured["active"] == OptimizationAlgorithm.MILP

    # The shadow GENETIC run uses the configured GENETIC budget, MILP its polish budget.
    budget = ems_module._genetic_family_budget(config_eos, OptimizationAlgorithm.GENETIC)
    assert budget == (config_eos.optimization.genetic.generations, None)
    budget = ems_module._genetic_family_budget(config_eos, OptimizationAlgorithm.MILP)
    assert budget == (
        config_eos.optimization.milp.polish_generations,
        config_eos.optimization.milp.polish_individuals,
    )


@pytest.mark.asyncio
async def test_automatic_ems_run_shadows_the_other_algorithm(configured_request, monkeypatch):
    """End to end: an automatic MILP run is shadowed by GENETIC on the same inputs.

    The shadow plan is never published: the EMS keeps the MILP solution.
    """
    from unittest.mock import AsyncMock, Mock

    from akkudoktoreos.core.ems import EnergyManagement
    from akkudoktoreos.core.emsettings import EnergyManagementMode
    from akkudoktoreos.core.historyrecorder import HistoryRecorder
    from akkudoktoreos.optimization.genetic import configrequest
    from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters

    config, fake, _, data = configured_request
    config.merge_settings_from_dict(
        {
            "optimization": {
                "algorithm": "MILP",
                "shadow_algorithms": ["GENETIC", "MILP"],
                "milp": {"polish_generations": 2, "polish_individuals": 10},
                "genetic": {"generations": 10, "individuals": 10},
            }
        }
    )
    ems = EnergyManagement()
    for name in ("_genetic_solution", "_optimization_solution", "_plan"):
        monkeypatch.setattr(EnergyManagement, name, None)
    monkeypatch.setattr(configrequest, "get_ems", lambda: ems)
    monkeypatch.setattr(EnergyManagement, "prediction", Mock(update_data=AsyncMock()))
    monkeypatch.setattr(EnergyManagement, "adapter", Mock(update_data=AsyncMock()))
    monkeypatch.setattr(HistoryRecorder, "schedule", lambda self, *args: None)
    stored: list[Any] = []

    async def record_shadow(self, config, result):
        stored.append(result)

    monkeypatch.setattr(HistoryRecorder, "record_shadow", record_shadow)
    request = ConfigOptimizationRequest.model_validate(data)

    async def prepare(cls):
        return await request.resolve()

    monkeypatch.setattr(GeneticOptimizationParameters, "prepare", classmethod(prepare))
    runner = ShadowRunner()
    monkeypatch.setattr(ShadowRunner, "_instance", runner)

    solution = await ems.run(
        start_datetime=fake.start_datetime.add(minutes=2),
        mode=EnergyManagementMode.OPTIMIZATION,
    )
    assert solution is not None
    await runner.wait_idle()

    assert ems.genetic_solution() is solution  # the active MILP plan stays published
    assert len(runner.results) == 1
    result = runner.results[0]
    assert (result.active_algorithm, result.shadow_algorithm) == ("MILP", "GENETIC")
    assert result.active_objective is not None and result.shadow_objective is not None
    assert stored == [result]

    # An explicitly requested run (e.g. via REST) is not shadowed.
    await ems.run(
        start_datetime=fake.start_datetime.add(minutes=2),
        mode=EnergyManagementMode.OPTIMIZATION,
        algorithm=OptimizationAlgorithm.MILP,
        genetic_parameters=ConfigOptimizationRequest.model_validate(data),
    )
    await runner.wait_idle()
    assert len(runner.results) == 1


# --------------------------------------------------------------------------------------
# Own warm start per shadow algorithm (RL29 fork, measure 9)
# --------------------------------------------------------------------------------------


class FakeParameters:
    """Stands in for GeneticOptimizationParameters (model_copy, warm start fields)."""

    def __init__(self, start: Any = None, start_time: Any = None) -> None:
        self.start_solution = start
        self.start_solution_datetime = start_time

    def model_copy(self, deep: bool = False) -> "FakeParameters":
        return FakeParameters(
            list(self.start_solution) if self.start_solution is not None else None,
            self.start_solution_datetime,
        )


def test_shadow_parameters_drop_the_active_warm_start_without_own_plan():
    from akkudoktoreos.core.shadow import shadow_parameters

    active = FakeParameters([1, 2, 3], "t0")
    params = shadow_parameters(active, None)
    assert params is not active
    assert params.start_solution is None and params.start_solution_datetime is None
    # The active run's parameters stay untouched.
    assert active.start_solution == [1, 2, 3]


def test_shadow_parameters_use_the_shadow_algorithms_own_last_plan():
    from akkudoktoreos.core.shadow import shadow_parameters

    params = shadow_parameters(FakeParameters([1, 2, 3], "t0"), ([7, 8], "t1"))
    assert params.start_solution == [7, 8]
    assert params.start_solution_datetime == "t1"


@pytest.mark.asyncio
async def test_runner_warm_starts_each_shadow_from_its_own_previous_solution():
    runner = ShadowRunner()
    seen: list[Any] = []

    def optimize(algorithm: OptimizationAlgorithm, parameters: Any) -> Any:
        seen.append(parameters.start_solution)
        solution = fake_solution(9.0)
        solution.start_solution = [len(seen)] * 3
        solution.start_solution_datetime = f"run{len(seen)}"
        return solution

    for _ in range(2):
        assert runner.schedule(
            [OptimizationAlgorithm.MILP],
            FakeParameters([0, 0, 0], "active"),
            OptimizationAlgorithm.GENETIC,
            fake_solution(9.5),
            30.0,
            optimize,
        )
        await runner.wait_idle()
    # First comparison cold, second from the shadow's own first solution.
    assert seen == [None, [1, 1, 1]]


@pytest.mark.asyncio
async def test_a_live_run_stops_the_comparison_and_drops_its_result():
    """The live run asks the shadow to stop; it waits at most the timeout."""
    runner = ShadowRunner()
    started = threading.Event()
    seen: list[bool] = []

    def optimize(algorithm: OptimizationAlgorithm, parameters: Any) -> Any:
        started.set()
        # the optimizer polls the stop event between generations
        seen.append(runner.stop_event.wait(timeout=5))
        return fake_solution(1.0)

    assert runner.schedule(
        [OptimizationAlgorithm.MILP, OptimizationAlgorithm.GENETIC],
        object(),
        "GENETIC",
        fake_solution(2.0),
        1.0,
        optimize,
    )
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    await runner.yield_to_live_run(timeout_s=5)
    assert not runner.busy
    assert seen == [True]  # stopped, the second algorithm is skipped
    assert len(runner.results) == 0
    # the next comparison starts with a fresh stop flag
    assert runner.schedule(
        [OptimizationAlgorithm.MILP], object(), "GENETIC", fake_solution(2.0), 1.0,
        lambda a, p: fake_solution(1.0),
    )
    await runner.wait_idle()
    assert len(runner.results) == 1


@pytest.mark.asyncio
async def test_a_live_run_does_not_wait_longer_than_the_timeout():
    runner = ShadowRunner()
    release = threading.Event()
    started = threading.Event()

    def stubborn(algorithm: OptimizationAlgorithm, parameters: Any) -> Any:
        started.set()
        release.wait(timeout=5)  # ignores the stop request
        return fake_solution(1.0)

    assert runner.schedule(
        [OptimizationAlgorithm.MILP], object(), "GENETIC", fake_solution(2.0), 1.0, stubborn
    )
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    began = time.monotonic()
    await runner.yield_to_live_run(timeout_s=0.2)
    assert time.monotonic() - began < 2.0
    assert runner.busy
    release.set()
    await runner.wait_idle()
    assert len(runner.results) == 0  # stopped meanwhile: result dropped


def test_genetic_loop_stops_on_request():
    from akkudoktoreos.optimization.genetic import genetic

    source = inspect.getsource(genetic.GeneticOptimization._evolve_population_adaptive)
    assert "_stop_event" in source


def test_frozen_ems_view_keeps_the_run_state():
    from akkudoktoreos.core.ems import FrozenEmsView

    live = SimpleNamespace(
        start_datetime="t0",
        observation_datetime="t0+2",
        genetic_solution=lambda: "previous",
        config="config",
    )
    view = FrozenEmsView(live)
    live.start_datetime = "t1"
    live.genetic_solution = lambda: "newer"
    assert view.start_datetime == "t0"
    assert view.observation_datetime == "t0+2"
    assert view.genetic_solution() == "previous"
    assert view.config == "config"


def test_predictions_are_updated_once_per_run(monkeypatch):
    from akkudoktoreos.core.ems import EnergyManagement, EnergyManagementStage

    monkeypatch.setattr(EnergyManagement, "_predictions_updated_in_run", True)
    monkeypatch.setattr(EnergyManagement, "_stage", EnergyManagementStage.OPTIMIZATION)
    assert EnergyManagement.predictions_updated_in_run()
    # outside a run (REST request) the preparation updates them itself
    monkeypatch.setattr(EnergyManagement, "_stage", EnergyManagementStage.IDLE)
    assert not EnergyManagement.predictions_updated_in_run()


@pytest.mark.asyncio
async def test_a_stopped_optimization_still_returns_a_plan(config_eos):
    from akkudoktoreos.core.coreabc import get_ems
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
    from akkudoktoreos.utils.datetimeutil import to_datetime

    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": 4,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                }
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-10T00:00:00"))
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * 4,
                "gesamtlast": [300.0] * 4,
                "strompreis_euro_pro_wh": [0.0003] * 4,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * 4,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "pv_battery": {"device_id": "bat", "capacity_wh": 5000, "initial_soc_percentage": 50},
            "inverter": {"device_id": "inv", "battery_id": "bat", "max_power_wh": 5000},
            "forecast_interval_seconds": 3600,
            "ev": None,
        }
    )
    optimizer = GeneticOptimization(fixed_seed=1)
    stop = threading.Event()
    stop.set()
    optimizer._stop_event = stop
    solution = optimizer.optimize_ems(params, ngen=50, individuals=10)
    assert solution is not None
    assert len(solution.discharge_allowed) >= 4
