"""Optimization input snapshots and their replay (RL29 fork).

A snapshot holds everything an automatic GENETIC or MILP run optimized: the fully
prepared ``GeneticOptimizationParameters`` (forecasts, prices, device states, heat
pump zones and hold times, warm start), the configuration at that moment (secrets
masked), the slot the run started in and the score of the plan that was executed.

Replaying snapshots with another algorithm, another budget or other settings answers
"would this have planned better?" on exactly the inputs EOS had, any number of times
and long after the fact - without waiting for new days with different weather.

Files: ``<data_folder>/rl29/snapshots/<YYYY-MM-DD>/<issued UTC>_<algorithm>.json.gz``.
"""

from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from loguru import logger

SNAPSHOT_VERSION = 1


def snapshot_dir(config: Any) -> Path:
    """Directory of the snapshots below the EOS data folder."""
    base = getattr(config.general, "data_folder_path", None) or "."
    return Path(base) / "rl29" / "snapshots"


def config_payload(config: Any) -> dict[str, Any]:
    """Configuration as stored in the EOS file (no defaults, no computed fields), redacted."""
    from akkudoktoreos.server.rest.secrets import redact

    return redact(json.loads(config.to_config_json()))


def build_snapshot(
    issued_at: datetime,
    algorithm: str,
    start_hour: int,
    seed: Optional[int],
    parameters: dict[str, Any],
    config: dict[str, Any],
    start_datetime: Optional[str] = None,
    start_timezone: Optional[str] = None,
) -> dict[str, Any]:
    """Snapshot document before the run; ``record_result`` adds the outcome."""
    return {
        "version": SNAPSHOT_VERSION,
        "issued_at": _utc(issued_at).isoformat(),
        "start_datetime": start_datetime,
        "start_timezone": start_timezone,
        "algorithm": str(algorithm),
        "start_hour": int(start_hour),
        "seed": seed,
        "parameters": parameters,
        "config": config,
        "result": None,
    }


def record_result(
    snapshot: dict[str, Any], solution: Any, duration_s: Optional[float]
) -> dict[str, Any]:
    """Add the executed plan's objective and net amount to the snapshot."""
    from akkudoktoreos.core.shadow import solution_objective

    objective, net = solution_objective(solution)
    snapshot["result"] = {
        "objective": objective,
        "net_amt": net,
        "duration_s": duration_s,
    }
    return snapshot


def snapshot_path(directory: Path, snapshot: dict[str, Any]) -> Path:
    issued = datetime.fromisoformat(snapshot["issued_at"]).astimezone(timezone.utc)
    name = f"{issued.strftime('%Y%m%dT%H%M%SZ')}_{snapshot['algorithm']}.json.gz"
    return directory / issued.strftime("%Y-%m-%d") / name


def write_snapshot(directory: Path, snapshot: dict[str, Any]) -> Path:
    """Write one snapshot (gzip JSON) atomically; returns its path."""
    path = snapshot_path(directory, snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(snapshot, handle, default=str, separators=(",", ":"))
    tmp.replace(path)
    return path


def prune_snapshots(directory: Path, retention_days: int, now: Optional[datetime] = None) -> int:
    """Delete day folders older than ``retention_days``; returns the number of files removed."""
    if not directory.is_dir():
        return 0
    cutoff = (_utc(now or datetime.now(timezone.utc)) - timedelta(days=retention_days)).date()
    removed = 0
    for day in sorted(directory.iterdir()):
        if not day.is_dir():
            continue
        try:
            day_date = datetime.strptime(day.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day_date >= cutoff:
            continue
        for file in day.glob("*.json.gz"):
            file.unlink()
            removed += 1
        try:
            day.rmdir()
        except OSError:
            pass
    return removed


def load_snapshot(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def iter_snapshots(
    directory: Path, start: Optional[datetime] = None, end: Optional[datetime] = None
) -> Iterator[Path]:
    """Snapshot files in time order, optionally within ``[start, end)``."""
    for path in sorted(directory.glob("*/*.json.gz")):
        try:
            issued = datetime.strptime(path.name.split("_", 1)[0], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if start is not None and issued < _utc(start):
            continue
        if end is not None and issued >= _utc(end):
            continue
        yield path


async def store_snapshot(config: Any, snapshot: dict[str, Any]) -> None:
    """Write and prune in a worker thread (background task of the EMS run)."""
    import asyncio

    settings = config.optimization.snapshots
    directory = snapshot_dir(config)
    try:
        await asyncio.to_thread(write_snapshot, directory, snapshot)
        await asyncio.to_thread(prune_snapshots, directory, settings.retention_days)
    except Exception as exc:
        logger.warning("Optimization snapshot not stored: {}", exc)


# --------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------


def apply_snapshot_config(
    config: Any,
    snapshot: dict[str, Any],
    overrides: Iterable[tuple[str, Any]] = (),
    data_folder_path: Optional[str] = None,
) -> None:
    """Load the snapshot's configuration (secrets dropped) plus ``path=value`` overrides."""
    from akkudoktoreos.server.rest.secrets import strip_masked

    data = strip_masked(snapshot.get("config") or {})
    # Never touch the history database or the adapters of the live system from a replay.
    data.get("database", {}).pop("history", None)
    data.pop("adapter", None)
    if data_folder_path is not None:
        data.setdefault("general", {})["data_folder_path"] = data_folder_path
    config.merge_settings_from_dict(data)
    for path, value in overrides:
        config.set_nested_value(path, value)


def replay_snapshot(
    snapshot: dict[str, Any],
    algorithm: Any,
    seed: Optional[int] = None,
    generations: Optional[int] = None,
    individuals: Optional[int] = None,
) -> dict[str, Any]:
    """Optimize the snapshot's inputs with ``algorithm`` under the current configuration.

    Call ``apply_snapshot_config`` first. Returns the objective (GENETIC fitness, lower
    is better), the net grid amount and the runtime, next to the recorded result.
    """
    from akkudoktoreos.core.coreabc import get_config, get_ems
    from akkudoktoreos.core.ems import _genetic_family_budget, _genetic_family_optimizer
    from akkudoktoreos.core.shadow import solution_objective
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
    from akkudoktoreos.optimization.optimization import OptimizationAlgorithm
    import pendulum

    config = get_config()
    algorithm = OptimizationAlgorithm(str(algorithm))
    start = snapshot.get("start_datetime") or snapshot["issued_at"]
    # Keep the stored offset: the start hour of the run is relative to it.
    parsed = pendulum.parse(start)
    if not isinstance(parsed, pendulum.DateTime):
        raise ValueError(f"Snapshot start '{start}' is not a datetime.")
    zone = snapshot.get("start_timezone")
    start_dt = parsed.in_timezone(zone) if zone else parsed
    get_ems().set_start_datetime(
        start_dt, interval_seconds=config.optimization.genetic.interval_sec
    )
    parameters = GeneticOptimizationParameters.model_validate(snapshot["parameters"])
    default_generations, default_individuals = _genetic_family_budget(config, algorithm)
    optimizer = _genetic_family_optimizer(algorithm, False, seed)
    started = time.monotonic()
    solution = optimizer.optimize_ems(
        start_hour=int(snapshot["start_hour"]),
        parameters=parameters,
        ngen=generations if generations is not None else default_generations,
        individuals=individuals if individuals is not None else default_individuals,
    )
    duration = time.monotonic() - started
    objective, net = solution_objective(solution)
    recorded = snapshot.get("result") or {}
    delta = None
    if objective is not None and recorded.get("objective") is not None:
        delta = objective - float(recorded["objective"])
    return {
        "issued_at": snapshot["issued_at"],
        "recorded_algorithm": snapshot["algorithm"],
        "recorded_objective": recorded.get("objective"),
        "algorithm": str(algorithm),
        "objective": objective,
        "net_amt": net,
        "duration_s": round(duration, 2),
        "delta_vs_recorded": delta,
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
