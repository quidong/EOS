#!/usr/bin/env python3
"""Snapshot and compare the observable state of a running EOS server.

Used as the acceptance check when the RL29 fork image replaces another image:
take a snapshot before the switch, one after, and diff them.

    python3 scripts/rl29/eos_snapshot.py snapshot http://eos.local:8503 -o before.json
    python3 scripts/rl29/eos_snapshot.py snapshot http://eos.local:8503 -o after.json
    python3 scripts/rl29/eos_snapshot.py diff before.json after.json

The snapshot holds the configuration (volatile keys removed), the health
state, the available prediction and measurement keys and a few statistics of
every prediction series over the next 24 h. Only the standard library is used,
so the script runs on any machine with Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

# Config paths that legitimately differ between two runs or two images.
VOLATILE_CONFIG_PATHS = {
    ("general", "version"),
    ("general", "data_folder_path"),
    ("general", "data_output_path"),
    ("general", "config_folder_path"),
    ("general", "config_file_path"),
    ("server", "eosdash_sesskey"),
}

# Relative tolerance for series statistics (forecast providers refresh).
DEFAULT_REL_TOL = 0.02


def _get(base: str, path: str, params: dict[str, Any] | None = None, timeout: float = 30) -> Any:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local admin tool
        return json.loads(resp.read().decode("utf-8"))


def _strip_volatile(cfg: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(cfg, dict):
        return {
            k: _strip_volatile(v, path + (k,))
            for k, v in cfg.items()
            if path + (k,) not in VOLATILE_CONFIG_PATHS
        }
    return cfg


def _series_stats(values: list[Any]) -> dict[str, Any]:
    nums = [float(v) for v in values if isinstance(v, (int, float)) and not math.isnan(float(v))]
    if not nums:
        return {"count": len(values), "numeric": 0}
    return {
        "count": len(values),
        "numeric": len(nums),
        "min": min(nums),
        "max": max(nums),
        "mean": sum(nums) / len(nums),
    }


def snapshot(base: str, hours: int = 24) -> dict[str, Any]:
    health = _get(base, "/v1/health")
    config = _strip_volatile(_get(base, "/v1/config"))
    pred_keys = sorted(_get(base, "/v1/prediction/keys"))
    meas_keys = sorted(_get(base, "/v1/measurement/keys"))

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now.isoformat()
    end = (now + timedelta(hours=hours)).isoformat()
    series: dict[str, Any] = {}
    for key in pred_keys:
        try:
            data = _get(
                base,
                "/v1/prediction/series",
                {"key": key, "start_datetime": start, "end_datetime": end},
            )
        except urllib.error.HTTPError as exc:
            series[key] = {"error": exc.code}
            continue
        values = list(data.get("data", {}).values()) if isinstance(data, dict) else []
        series[key] = _series_stats(values)

    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "base": base,
        "health": {
            "status": health.get("status"),
            "version": health.get("version"),
            "energy-management": health.get("energy-management"),
        },
        "config": config,
        "prediction_keys": pred_keys,
        "measurement_keys": meas_keys,
        "series_window": {"start": start, "end": end},
        "series": series,
    }


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = obj
    return out


def diff(a: dict[str, Any], b: dict[str, Any], rel_tol: float = DEFAULT_REL_TOL) -> list[str]:
    """Return human readable differences; empty list means equivalent."""
    problems: list[str] = []

    if b["health"]["status"] != "alive":
        problems.append(f"health status after: {b['health']['status']!r}")

    fa, fb = _flatten(a["config"]), _flatten(b["config"])
    for key in sorted(set(fa) | set(fb)):
        if key not in fb:
            problems.append(f"config key removed: {key}")
        elif key not in fa:
            # New defaults from a newer EOS version are reported but are not fatal.
            problems.append(f"config key added (check): {key} = {fb[key]!r}")
        elif fa[key] != fb[key]:
            problems.append(f"config changed: {key}: {fa[key]!r} -> {fb[key]!r}")

    for kind in ("prediction_keys", "measurement_keys"):
        missing = sorted(set(a[kind]) - set(b[kind]))
        if missing:
            problems.append(f"{kind} missing after switch: {missing}")

    for key, sa in a["series"].items():
        sb = b["series"].get(key)
        if sb is None:
            continue
        if sa.get("numeric", 0) > 0 and sb.get("numeric", 0) == 0:
            problems.append(f"series {key}: had data before, none after")
            continue
        for stat in ("min", "max", "mean"):
            if stat in sa and stat in sb:
                va, vb = sa[stat], sb[stat]
                scale = max(abs(va), abs(vb), 1e-9)
                if abs(va - vb) / scale > rel_tol:
                    problems.append(f"series {key}.{stat}: {va:.4g} -> {vb:.4g}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="take a snapshot of a running EOS")
    p_snap.add_argument("base", help="EOS base URL, e.g. http://eos.local:8503")
    p_snap.add_argument("-o", "--output", required=True)
    p_snap.add_argument("--hours", type=int, default=24)

    p_diff = sub.add_parser("diff", help="compare two snapshots")
    p_diff.add_argument("before")
    p_diff.add_argument("after")
    p_diff.add_argument("--rel-tol", type=float, default=DEFAULT_REL_TOL)

    args = parser.parse_args(argv)
    if args.cmd == "snapshot":
        snap = snapshot(args.base, args.hours)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
        print(f"snapshot written: {args.output} ({len(snap['prediction_keys'])} prediction keys)")
        return 0

    with open(args.before, encoding="utf-8") as fh:
        before = json.load(fh)
    with open(args.after, encoding="utf-8") as fh:
        after = json.load(fh)
    problems = diff(before, after, args.rel_tol)
    fatal = [p for p in problems if "(check)" not in p]
    for p in problems:
        print(("WARN  " if "(check)" in p else "FAIL  ") + p)
    print("EQUIVALENT" if not fatal else f"{len(fatal)} difference(s)")
    return 0 if not fatal else 1


if __name__ == "__main__":
    sys.exit(main())
