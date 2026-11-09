#!/usr/bin/env python3
r"""Replay stored optimization snapshots with other algorithms or settings (RL29 fork).

Every automatic run with ``optimization.snapshots.enabled`` leaves its fully prepared
inputs and the score of the executed plan below ``<data_folder>/rl29/snapshots``. This
script optimizes them again and compares the scores (GENETIC fitness, lower is better)
with what was executed. Variants differ in algorithm and configuration overrides:

    # MILP and GENETIC on the last 14 days, every 4th run
    uv run python scripts/rl29/replay_snapshots.py --dir /data/rl29/snapshots \\
        --days 14 --every 4 --variant MILP --variant GENETIC

    # MILP with more time against the recorded plans
    uv run python scripts/rl29/replay_snapshots.py --dir ./snapshots --days 14 \\
        --variant "MILP:optimization/milp/time_limit_s=60,optimization/milp/hp_search_seconds=120"

Results go to stdout (summary) and optionally to a CSV (``--csv``). Runs are CPU bound:
on the EOS host run it outside the busy hours or copy the snapshots elsewhere.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def parse_variant(text: str) -> tuple[str, list[tuple[str, Any]]]:
    """``ALGORITHM[:path=value,path=value]``; values are parsed as JSON when possible."""
    algorithm, _, rest = text.partition(":")
    overrides: list[tuple[str, Any]] = []
    for item in filter(None, (part.strip() for part in rest.split(","))):
        path, sep, raw = item.partition("=")
        if not sep or not path.strip():
            raise argparse.ArgumentTypeError(f"Override '{item}' is not path=value.")
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        overrides.append((path.strip().strip("/"), value))
    return algorithm.strip().upper(), overrides


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per variant: runs, mean objective, mean delta vs. the executed plan, share better."""
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = result.setdefault(
            row["variant"],
            {
                "runs": 0,
                "objective_sum": 0.0,
                "delta_sum": 0.0,
                "deltas": 0,
                "better": 0,
                "duration_sum": 0.0,
            },
        )
        if row.get("objective") is None:
            continue
        entry["runs"] += 1
        entry["objective_sum"] += row["objective"]
        entry["duration_sum"] += row["duration_s"]
        if row.get("delta_vs_recorded") is not None:
            entry["deltas"] += 1
            entry["delta_sum"] += row["delta_vs_recorded"]
            if row["delta_vs_recorded"] < -1e-6:
                entry["better"] += 1
    summary: dict[str, dict[str, Any]] = {}
    for name, entry in result.items():
        runs = entry["runs"]
        deltas = entry["deltas"]
        summary[name] = {
            "runs": runs,
            "mean_objective": round(entry["objective_sum"] / runs, 4) if runs else None,
            "mean_delta_vs_executed": round(entry["delta_sum"] / deltas, 4) if deltas else None,
            "better_than_executed_share": round(entry["better"] / deltas, 3) if deltas else None,
            "mean_duration_s": round(entry["duration_sum"] / runs, 1) if runs else None,
        }
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, required=True, help="Snapshot directory.")
    parser.add_argument("--days", type=float, default=14.0, help="Look back this many days.")
    parser.add_argument("--every", type=int, default=1, help="Use every n-th snapshot.")
    parser.add_argument("--limit", type=int, default=None, help="At most this many snapshots.")
    parser.add_argument(
        "--variant",
        action="append",
        type=parse_variant,
        required=True,
        help="ALGORITHM[:config/path=value,...] (repeatable).",
    )
    parser.add_argument("--seed", type=int, default=1, help="Fixed GENETIC seed (reproducible).")
    parser.add_argument("--csv", type=Path, default=None, help="Write all rows to this CSV.")
    args = parser.parse_args(argv)

    from akkudoktoreos.core.coreabc import get_config
    from akkudoktoreos.core.snapshot import (
        apply_snapshot_config,
        iter_snapshots,
        load_snapshot,
        replay_snapshot,
    )

    end = datetime.now(timezone.utc)
    paths = list(iter_snapshots(args.dir, end - timedelta(days=args.days), end))
    paths = paths[:: max(1, args.every)]
    if args.limit is not None:
        paths = paths[: args.limit]
    if not paths:
        print(f"No snapshots in {args.dir} for the last {args.days:g} days.", file=sys.stderr)
        return 1

    config = get_config()
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as data_folder:
        for index, path in enumerate(paths, start=1):
            snapshot = load_snapshot(path)
            for algorithm, overrides in args.variant:
                name = algorithm + (
                    ":" + ",".join(f"{p}={v}" for p, v in overrides) if overrides else ""
                )
                apply_snapshot_config(config, snapshot, overrides, data_folder_path=data_folder)
                row = replay_snapshot(snapshot, algorithm, seed=args.seed)
                row["variant"] = name
                rows.append(row)
                print(
                    f"[{index}/{len(paths)}] {row['issued_at']} {name}: objective "
                    f"{row['objective']} (executed {row['recorded_algorithm']} "
                    f"{row['recorded_objective']}) in {row['duration_s']} s",
                    flush=True,
                )

    if args.csv is not None:
        with args.csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps(summarize(rows), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
