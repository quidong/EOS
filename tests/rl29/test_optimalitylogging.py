"""Optimality and convergence reporting of MILP and GENETIC (RL29 fork).

The logs tell whether a MILP solve proved its optimum or stopped at the time limit
(with the remaining gap), and how the genetic search converged over its generations.
They are the basis to choose the time limits of both optimizers.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

import numpy as np
import pytest
from deap import creator, tools
from loguru import logger

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.optimization.genetic.genetic import (
    GeneticOptimization,
    genetic_convergence,
)
from akkudoktoreos.optimization.milp.dispatch import (
    DispatchInput,
    DispatchResult,
    _outcome,
    describe_optimality,
    solve_dispatch,
)
from akkudoktoreos.optimization.milp.milpoptimization import (
    describe_report,
    optimality_report,
    solve_joint_modes,
)
from akkudoktoreos.optimization.milp.thermal import thermal_block
from akkudoktoreos.utils.datetimeutil import to_datetime

from .test_milp_thermal import _run_milp, make_plan, valley_prices

CENT = 0.01 / 1000.0


@pytest.fixture
def log_messages():
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    yield messages
    logger.remove(sink)


def _scipy_result(status: int, gap=None, x=(1.0,), fun=2.0):
    return SimpleNamespace(
        status=status,
        message=f"status {status}",
        x=None if x is None else np.array(x),
        fun=fun,
        mip_gap=gap,
    )


# --- solver outcome ---------------------------------------------------------------


def test_outcome_optimal():
    outcome = _outcome(_scipy_result(0, gap=1e-5))
    assert outcome.ok and outcome.optimal
    assert outcome.mip_gap == pytest.approx(1e-5)
    assert describe_optimality(outcome.optimal, outcome.mip_gap) == "optimal"


def test_outcome_optimal_without_gap_counts_as_zero_gap():
    outcome = _outcome(_scipy_result(0, gap=None))
    assert outcome.optimal and outcome.mip_gap == 0.0


def test_outcome_time_limit_keeps_solution_and_gap():
    outcome = _outcome(_scipy_result(1, gap=0.0321))
    assert outcome.ok and not outcome.optimal
    assert outcome.solution is not None and outcome.objective == 2.0
    assert outcome.mip_gap == pytest.approx(0.0321)
    assert describe_optimality(False, outcome.mip_gap) == "NOT optimal (gap 3.2 %)"


@pytest.mark.parametrize("gap", [None, math.inf, math.nan])
def test_outcome_time_limit_with_unknown_gap(gap):
    outcome = _outcome(_scipy_result(1, gap=gap))
    assert outcome.ok and not outcome.optimal and outcome.mip_gap is None
    assert describe_optimality(False, None) == "NOT optimal (gap unknown)"


def test_outcome_without_solution():
    outcome = _outcome(_scipy_result(2, x=None, fun=None))
    assert not outcome.ok and not outcome.optimal
    assert outcome.solution is None and outcome.objective is None


def test_dispatch_result_reports_the_optimum():
    result = solve_dispatch(
        DispatchInput(
            pv_wh=[1000, 0],
            load_wh=[400, 500],
            price_per_wh=[30 * CENT] * 2,
            feed_in_per_wh=[8 * CENT] * 2,
        )
    )
    assert result.success and result.optimal
    assert result.mip_gap == pytest.approx(0.0, abs=1e-9)


# --- reports over several solves ---------------------------------------------------


def _result(optimal: bool, gap, success: bool = True) -> DispatchResult:
    return DispatchResult(
        "s", success, 1.0, [], [], [], [], [], [], [], [], 0.1, optimal=optimal, mip_gap=gap
    )


def test_report_all_optimal():
    report = optimality_report([_result(True, 0.0)] * 4)
    assert report == {"solves": 4, "optimal": 4, "max_gap": 0.0}
    assert describe_report(report) == "all 4 optimal"
    assert describe_report(optimality_report([_result(True, 0.0)])) == "optimal"


def test_report_partly_at_the_time_limit():
    report = optimality_report(
        [_result(True, 0.0), _result(False, 0.012), _result(False, 0.034), _result(True, 0.0)]
    )
    assert report["optimal"] == 2 and report["max_gap"] == pytest.approx(0.034)
    assert describe_report(report) == "optimal 2/4, max gap 3.4 %"


def test_report_unknown_gap_and_failed_solve():
    report = optimality_report([_result(False, None), _result(False, None, success=False)])
    assert report == {"solves": 2, "optimal": 0, "max_gap": None}
    assert describe_report(report) == "optimal 0/2, gap unknown"


def test_joint_model_reports_every_window():
    plan, _ = make_plan(hours=6, settings={"optimization": {"heating_modes": [0, 1, 2]}})
    block = thermal_block(plan, joint_modes=True)
    n = plan.n_slots
    data = DispatchInput(
        pv_wh=[0.0] * n,
        load_wh=[100.0] * n,
        price_per_wh=valley_prices(n),
        feed_in_per_wh=[0.00007] * n,
        thermal=[block],
        mip_rel_gap=0.005,
    )
    windows: list[DispatchResult] = []
    result, solves = solve_joint_modes(data, 2, 30.0, windows)
    assert result is not None and result.success
    assert len(windows) == solves >= 2
    assert windows[-1] is result
    report = optimality_report(windows)
    assert report["solves"] == solves
    assert report["optimal"] == solves  # a small model is proven well within 30 s


def test_milp_run_logs_optimality(config_eos: ConfigEOS, log_messages):
    optimizer = _run_milp(config_eos, thermal=True, search=0.0)
    heat_pump = [m for m in log_messages if m.startswith("MILP: heat pump model solved")]
    dispatch = [m for m in log_messages if "dispatch solves in" in m]
    assert heat_pump and dispatch
    assert "optimal" in heat_pump[-1]
    assert "schedule dispatch solves" in dispatch[-1]
    summary = optimizer.milp_summary
    assert isinstance(summary["best_optimal"], bool)
    assert summary["dispatch"]["solves"] >= 1
    assert "optimal" in summary["thermal"]
    assert "mip_gap" in summary["thermal"]


# --- genetic convergence -----------------------------------------------------------


def test_convergence_settled_early():
    best = [10.0, 6.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0]  # 8 generations
    c = genetic_convergence(best, improvements=2, last_improvement_generation=2)
    assert c["generations"] == 8
    assert c["last_improvement_share"] == pytest.approx(0.25)
    assert c["last_quarter_gain"] == pytest.approx(0.0)
    assert c["last_quarter_share"] == pytest.approx(0.0)
    assert c["best_at_quarters"] == [5.0, 5.0, 5.0]


def test_convergence_still_improving():
    best = [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0]
    c = genetic_convergence(best, improvements=8, last_improvement_generation=8)
    assert c["last_improvement_share"] == pytest.approx(1.0)
    assert c["last_quarter_gain"] == pytest.approx(2.0)  # 4.0 at gen 6 -> 2.0
    assert c["last_quarter_share"] == pytest.approx(0.25)
    assert c["best_at_quarters"] == [8.0, 6.0, 4.0]


def test_convergence_without_generations_or_gain():
    c = genetic_convergence([3.0], improvements=0, last_improvement_generation=0)
    assert c["generations"] == 0 and c["last_improvement_share"] == 0.0
    assert c["last_quarter_share"] == 0.0


def test_adaptive_evolution_logs_convergence(config_eos: ConfigEOS, log_messages):
    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {"tail_horizon_hours": 0, "horizon_hours": 48, "interval_sec": 3600}
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime().set(hour=0, minute=0))
    opt = GeneticOptimization(fixed_seed=42)
    opt.optimize_ev = False
    opt.setup_deap_environment({"home_appliance": 0}, start_hour=0)
    opt.toolbox.register("evaluate", lambda individual: (float(sum(individual)),))
    rng = random.Random(1)
    population = [
        creator.Individual([rng.randint(0, 3) for _ in range(opt.control_slots)]) for _ in range(20)
    ]
    stats = tools.Statistics(lambda individual: individual.fitness.values)
    stats.register("min", np.min)
    halloffame = tools.HallOfFame(1)
    _, log = opt._evolve_population_adaptive(
        population, mu=20, lambda_=20, ngen=8, stats=stats, halloffame=halloffame
    )
    convergence = opt._adaptive_evolution_metrics["convergence"]
    assert convergence["generations"] == 8
    assert convergence["final_fitness"] == pytest.approx(halloffame[0].fitness.values[0])
    assert convergence["final_fitness"] <= convergence["initial_fitness"]
    assert 0 <= convergence["last_improvement_generation"] <= 8
    # The best fitness never gets worse over the generations.
    quarters = convergence["best_at_quarters"]
    assert quarters == sorted(quarters, reverse=True)
    lines = [m for m in log_messages if m.startswith("Genetic convergence:")]
    assert len(lines) == 1 and "in 8 generations" in lines[0]
