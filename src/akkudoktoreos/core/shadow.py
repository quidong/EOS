"""Shadow comparison of optimization algorithms on identical inputs (RL29 fork).

The A/B operation (``optimization.alternate_algorithms``) compares algorithms on
different days, so weather and prices dominate the result. The shadow comparison
removes that noise: after an automatic GENETIC or MILP run, every algorithm in
``optimization.shadow_algorithms`` other than the active one optimizes a copy of the
same prepared parameters in the background. Its solution is never published or
executed; only its objective is compared with the active plan.

Both algorithms are scored with the GENETIC fitness (MILP polishes its result with
the GENETIC evaluator), which is the quantity the optimizers minimise: grid costs
minus revenues plus heat pump comfort, legionella and switching penalties minus the
value of the stored heat. Lower is better.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from loguru import logger

from akkudoktoreos.optimization.optimization import OptimizationAlgorithm

SHADOW_CAPABLE = (OptimizationAlgorithm.GENETIC, OptimizationAlgorithm.MILP)
"""Algorithms that share the GENETIC parameters and fitness and can shadow each other."""

MAX_RESULTS = 2000
"""In-memory results kept for the summary (about three weeks at 15-minute runs)."""
YIELD_TIMEOUT_S = 10.0
"""Longest wait of a live run for a shadow comparison to stop (RL29 fork)."""


@dataclass
class ShadowResult:
    """One comparison of the active plan with a shadow plan on the same inputs."""

    issued_at: datetime
    active_algorithm: str
    shadow_algorithm: str
    active_objective: Optional[float]
    shadow_objective: Optional[float]
    active_net_amt: Optional[float]
    shadow_net_amt: Optional[float]
    active_duration_s: Optional[float]
    shadow_duration_s: Optional[float]

    @property
    def delta(self) -> Optional[float]:
        """Shadow minus active objective; negative = the shadow would have been better."""
        if self.active_objective is None or self.shadow_objective is None:
            return None
        return self.shadow_objective - self.active_objective

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["issued_at"] = self.issued_at.isoformat()
        data["delta"] = None if self.delta is None else round(self.delta, 4)
        return data


def solution_objective(solution: Any) -> tuple[Optional[float], Optional[float]]:
    """Objective (best GENETIC fitness) and net grid amount of a GeneticSolution.

    The fitness is the last entry of ``fitness_history["min"]``; the net amount is
    costs minus revenues of the simulated plan.
    """
    objective: Optional[float] = None
    history = getattr(solution, "fitness_history", None) or {}
    minima = history.get("min") if isinstance(history, dict) else None
    if minima:
        try:
            objective = float(minima[-1])
        except (TypeError, ValueError):
            objective = None
    net: Optional[float] = None
    result = getattr(solution, "result", None)
    if result is not None:
        costs = getattr(result, "total_costs", None)
        revenue = getattr(result, "total_revenue", None)
        if costs is not None and revenue is not None:
            net = float(costs) - float(revenue)
    return objective, net


def shadow_algorithms_for(settings: Any, active: Any) -> list[OptimizationAlgorithm]:
    """Configured shadow algorithms that can run beside ``active`` (deduplicated)."""
    try:
        active_algorithm = OptimizationAlgorithm(str(active))
    except ValueError:
        return []
    if active_algorithm not in SHADOW_CAPABLE:
        return []
    result: list[OptimizationAlgorithm] = []
    for value in getattr(settings, "shadow_algorithms", None) or []:
        try:
            algorithm = OptimizationAlgorithm(str(value))
        except ValueError:
            continue
        if algorithm == active_algorithm or algorithm not in SHADOW_CAPABLE:
            continue
        if algorithm not in result:
            result.append(algorithm)
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per algorithm pair: runs, mean delta and how often the shadow was better.

    ``delta`` is shadow minus active objective in currency units; a negative mean
    means the shadow algorithm would have planned cheaper on average.
    """
    pairs: dict[str, dict[str, Any]] = {}
    for row in results:
        delta = row.get("delta")
        if delta is None:
            continue
        key = f"{row['shadow_algorithm']}_vs_{row['active_algorithm']}"
        entry = pairs.setdefault(key, {"runs": 0, "sum": 0.0, "shadow_better": 0})
        entry["runs"] += 1
        entry["sum"] += float(delta)
        if float(delta) < -1e-6:
            entry["shadow_better"] += 1
    for entry in pairs.values():
        entry["mean_delta_amt"] = round(entry.pop("sum") / entry["runs"], 4)
        entry["shadow_better_share"] = round(entry["shadow_better"] / entry["runs"], 3)
    return pairs


def shadow_parameters(parameters: Any, own_start: Optional[tuple[Any, Any]]) -> Any:
    """Parameter copy for a shadow run, warm-started from the shadow's own last plan.

    The prepared parameters carry the active algorithm's published plan as warm
    start. A shadow GENETIC run seeded with the active MILP plan can hardly end up
    worse than it, which biases the comparison towards a zero delta. Each shadow
    algorithm therefore continues from its own previous solution, or starts cold
    when it has none yet (RL29 fork).

    Args:
        parameters: Prepared GeneticOptimizationParameters of the active run.
        own_start: ``(start_solution, start_solution_datetime)`` of the shadow
            algorithm's last solution, or None.

    Returns:
        A copy of ``parameters`` with the warm start replaced.
    """
    try:
        params = parameters.model_copy(deep=True)
    except AttributeError:
        return parameters
    start, start_time = own_start if own_start is not None else (None, None)
    params.start_solution = list(start) if start is not None else None
    params.start_solution_datetime = start_time if start is not None else None
    return params


OptimizeFn = Callable[[OptimizationAlgorithm, Any], Any]
"""Runs one algorithm on a parameter copy and returns its GeneticSolution (blocking)."""


class ShadowRunner:
    """Process-wide runner; one shadow comparison at a time."""

    _instance: Optional["ShadowRunner"] = None

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self.results: deque[ShadowResult] = deque(maxlen=MAX_RESULTS)
        self.last_error: Optional[str] = None
        # Last solution of each shadow algorithm: its own warm start for the next
        # comparison (see ``shadow_parameters``).
        self._own_starts: dict[str, tuple[Any, Any]] = {}
        # Set by the next live run: the comparison stops and drops its result.
        self.stop_event = threading.Event()

    @classmethod
    def instance(cls) -> "ShadowRunner":
        if cls._instance is None:
            cls._instance = ShadowRunner()
        return cls._instance

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def wait_idle(self) -> None:
        """Wait until a running comparison has finished (tests, shutdown)."""
        task = self._task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    async def yield_to_live_run(self, timeout_s: float = YIELD_TIMEOUT_S) -> None:
        """Ask a running comparison to stop and give it ``timeout_s`` to do so (fork).

        The live run never waits longer: the comparison stops after its current
        generation, its result is dropped, and a comparison still busy after the
        timeout goes on with its own frozen EMS view.
        """
        task = self._task
        if task is None or task.done():
            return
        self.stop_event.set()
        logger.info("Shadow comparison still running; asked to stop for the live run.")
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout_s))
        if not done:
            logger.info(
                "Shadow comparison did not stop within {:.0f} s; the live run goes on.", timeout_s
            )

    def schedule(
        self,
        algorithms: list[OptimizationAlgorithm],
        parameters: Any,
        active_algorithm: Any,
        active_solution: Any,
        active_duration_s: float,
        optimize: OptimizeFn,
        on_result: Optional[Callable[[ShadowResult], Any]] = None,
    ) -> bool:
        """Start the comparison in the background; False when skipped."""
        if not algorithms or parameters is None or active_solution is None:
            return False
        if self.busy:
            logger.info("Shadow comparison skipped: the previous one is still running.")
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self.stop_event = threading.Event()  # a fresh one per comparison
        self._task = loop.create_task(
            self._run(
                list(algorithms),
                parameters,
                str(active_algorithm),
                active_solution,
                active_duration_s,
                optimize,
                on_result,
            )
        )
        return True

    async def _run(
        self,
        algorithms: list[OptimizationAlgorithm],
        parameters: Any,
        active_algorithm: str,
        active_solution: Any,
        active_duration_s: float,
        optimize: OptimizeFn,
        on_result: Optional[Callable[[ShadowResult], Any]],
    ) -> None:
        active_objective, active_net = solution_objective(active_solution)
        issued = datetime.now(timezone.utc)
        loop = asyncio.get_running_loop()
        stop = self.stop_event
        for algorithm in algorithms:
            if stop.is_set():
                logger.info("Shadow comparison {} skipped: a live run started.", algorithm)
                break
            started = time.monotonic()
            own = self._own_starts.get(str(algorithm))
            params = shadow_parameters(parameters, own)
            try:
                solution = await loop.run_in_executor(None, optimize, algorithm, params)
            except Exception as exc:
                self.last_error = f"{algorithm}: {exc}"
                logger.warning("Shadow comparison {} failed: {}", algorithm, exc)
                continue
            duration = time.monotonic() - started
            if stop.is_set():
                logger.info(
                    "Shadow comparison {} stopped after {:.0f} s for a live run; result dropped.",
                    algorithm,
                    duration,
                )
                break
            start = getattr(solution, "start_solution", None)
            if start is not None:
                self._own_starts[str(algorithm)] = (
                    list(start),
                    getattr(solution, "start_solution_datetime", None),
                )
            objective, net = solution_objective(solution)
            result = ShadowResult(
                issued_at=issued,
                active_algorithm=active_algorithm,
                shadow_algorithm=str(algorithm),
                active_objective=active_objective,
                shadow_objective=objective,
                active_net_amt=active_net,
                shadow_net_amt=net,
                active_duration_s=active_duration_s,
                shadow_duration_s=duration,
            )
            self.results.append(result)
            self.last_error = None
            delta = result.delta
            logger.info(
                "Shadow comparison: {} (active) objective {} net {} | {} objective {} net {} "
                "in {:.0f} s -> delta {} ({}).",
                active_algorithm,
                _fmt(active_objective),
                _fmt(active_net),
                algorithm,
                _fmt(objective),
                _fmt(net),
                duration,
                _fmt(delta),
                "shadow better"
                if delta is not None and delta < -1e-6
                else "active better or equal",
            )
            if on_result is not None:
                try:
                    outcome = on_result(result)
                    if asyncio.iscoroutine(outcome):
                        await outcome
                except Exception as exc:
                    logger.debug("Shadow comparison: storing the result failed: {}", exc)

    def summary(self) -> dict[str, Any]:
        return summarize([r.as_dict() for r in self.results])


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.3f}"
