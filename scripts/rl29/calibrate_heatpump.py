#!/usr/bin/env python3
"""Calibrate the heat pump model from measured Home Assistant data (RL29 fork).

Two steps:

1. ``fetch``: read the history of the relevant entities from Home Assistant and
   write an hourly CSV (mean values per hour)::

       python3 scripts/rl29/calibrate_heatpump.py fetch \\
           --ha-url http://homeassistant.local:8123 --token "$HA_TOKEN" --days 10 -o hp.csv

   Entity ids default to the RL29 installation (see ``DEFAULT_ENTITIES``) and can
   be overridden with ``--entity column=sensor.xyz``.

2. ``fit``: estimate model parameters from the CSV and print a configuration
   snippet for ``devices.heat_pumps.<id>``::

       python3 scripts/rl29/calibrate_heatpump.py fit hp.csv --device-id wbb12

Fitted parameters:

- ``building.loss_w_per_k`` and ``building.internal_gains_w``: linear
  regression of the daily space heating energy on the daily mean temperature
  difference room - outside.
- ``unit.cop_scale``: median ratio of measured to rated-model COP in hours the
  heat pump runs in space heating.
- ``dhw.loss_w_per_k``: exponential decay of the tank temperature in night hours
  without heating and tapping.

Columns of the CSV: ``timestamp`` (ISO, hourly), ``outdoor_c``, ``room_c``,
``heat_w`` (thermal output), ``electric_w``, ``flow_c``, ``dhw_c`` and optionally
``dhw_heat_w`` (thermal output into the tank). Missing columns skip the
corresponding fit. Only the Python standard library and numpy are needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_ENTITIES = {
    "outdoor_c": "sensor.wh_system_aussentemperatur",
    "room_c": "sensor.raumtemperatur_mittel",
    "heat_w": "sensor.wh_warmepumpe_warmeleistung",
    "electric_w": "sensor.strom_warmepumpe",
    "flow_c": "sensor.wh_warmepumpe_vorlauftemperatur",
    "dhw_c": "sensor.wh_warmwasser_warmwassertemperatur",
}
COLUMNS = ["outdoor_c", "room_c", "heat_w", "electric_w", "flow_c", "dhw_c", "dhw_heat_w"]
WATER_WH_PER_LITRE_K = 1.163


# ---------------------------------------------------------------------------
# Data handling
# ---------------------------------------------------------------------------


@dataclass
class Hour:
    timestamp: datetime
    values: dict[str, Optional[float]] = field(default_factory=dict)

    def get(self, key: str) -> Optional[float]:
        value = self.values.get(key)
        return value if value is not None and math.isfinite(value) else None


def read_csv(path: Path) -> list[Hour]:
    hours: list[Hour] = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            values: dict[str, Optional[float]] = {}
            for key in COLUMNS:
                raw = row.get(key)
                values[key] = float(raw) if raw not in (None, "", "nan") else None
            hours.append(Hour(datetime.fromisoformat(row["timestamp"]), values))
    hours.sort(key=lambda h: h.timestamp)
    return hours


def write_csv(path: Path, hours: list[Hour]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", *COLUMNS])
        for hour in hours:
            writer.writerow(
                [hour.timestamp.isoformat()]
                + ["" if hour.values.get(c) is None else f"{hour.values[c]:.4f}" for c in COLUMNS]
            )


def hourly_means(
    samples: list[tuple[datetime, float]], start: datetime, end: datetime
) -> dict[datetime, float]:
    """Time-weighted hourly means of a step signal (value holds until the next sample)."""
    samples = sorted(s for s in samples if math.isfinite(s[1]))
    result: dict[datetime, float] = {}
    if not samples:
        return result
    hour = start.replace(minute=0, second=0, microsecond=0)
    index = 0
    while hour < end:
        hour_end = hour + timedelta(hours=1)
        while index + 1 < len(samples) and samples[index + 1][0] <= hour:
            index += 1
        if samples[index][0] > hour_end:
            hour = hour_end
            continue
        weighted = 0.0
        covered = 0.0
        j = index
        while j < len(samples) and samples[j][0] < hour_end:
            seg_start = max(hour, samples[j][0])
            seg_end = min(hour_end, samples[j + 1][0] if j + 1 < len(samples) else hour_end)
            if seg_end > seg_start:
                seconds = (seg_end - seg_start).total_seconds()
                weighted += samples[j][1] * seconds
                covered += seconds
            j += 1
        if covered >= 1800:  # at least half the hour known
            result[hour] = weighted / covered
        hour = hour_end
    return result


# ---------------------------------------------------------------------------
# Fetch from Home Assistant
# ---------------------------------------------------------------------------


def fetch_history(base_url: str, token: str, entity_id: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    api = base_url.rstrip("/")
    if not api.endswith("/api"):
        api += "/api"
    query = urllib.parse.urlencode(
        {
            "filter_entity_id": entity_id,
            "end_time": end.isoformat(),
            "minimal_response": "",
            "no_attributes": "",
            "significant_changes_only": "0",
        }
    )
    url = f"{api}/history/period/{urllib.parse.quote(start.isoformat())}?{query}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310 - admin tool
        data = json.loads(resp.read().decode("utf-8"))
    samples: list[tuple[datetime, float]] = []
    for series in data:
        for item in series:
            try:
                value = float(item["state"])
            except (KeyError, TypeError, ValueError):
                continue
            stamp = item.get("last_changed") or item.get("last_updated")
            samples.append((datetime.fromisoformat(stamp.replace("Z", "+00:00")), value))
    return samples


def cmd_fetch(args: argparse.Namespace) -> int:
    entities = dict(DEFAULT_ENTITIES)
    for item in args.entity or []:
        column, entity_id = item.split("=", 1)
        if column not in COLUMNS:
            raise SystemExit(f"Unknown column {column!r}; expected one of {COLUMNS}")
        entities[column] = entity_id
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    columns: dict[str, dict[datetime, float]] = {}
    for column, entity_id in entities.items():
        samples = fetch_history(args.ha_url, args.token, entity_id, start, end)
        columns[column] = hourly_means(samples, start, end)
        print(f"{column:12s} {entity_id:50s} {len(columns[column]):5d} h", file=sys.stderr)
    stamps = sorted({t for values in columns.values() for t in values})
    hours = [Hour(t, {c: columns.get(c, {}).get(t) for c in COLUMNS}) for t in stamps]
    write_csv(Path(args.output), hours)
    print(f"written {args.output}: {len(hours)} hours", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Fits
# ---------------------------------------------------------------------------


def fit_building_loss(hours: list[Hour], min_hours: int = 22) -> Optional[dict[str, float]]:
    """Regress daily space heat on the daily temperature difference.

    heat_space_w = H * (T_room - T_out) - gains   (daily means)
    """
    days: dict[Any, list[Hour]] = defaultdict(list)
    for hour in hours:
        days[hour.timestamp.date()].append(hour)
    xs: list[float] = []
    ys: list[float] = []
    for day_hours in days.values():
        usable = [
            h for h in day_hours if None not in (h.get("outdoor_c"), h.get("room_c"), h.get("heat_w"))
        ]
        if len(usable) < min_hours:
            continue
        delta = statistics.fmean(h.get("room_c") - h.get("outdoor_c") for h in usable)  # type: ignore[operator]
        heat = statistics.fmean(
            (h.get("heat_w") or 0.0) - (h.get("dhw_heat_w") or 0.0) for h in usable
        )
        xs.append(delta)
        ys.append(heat)
    if len(xs) < 3 or max(xs) - min(xs) < 2.0:
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
    intercept = mean_y - slope * mean_x
    predicted = [slope * x + intercept for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, predicted))
    ss_tot = sum((y - mean_y) ** 2 for y in ys) or 1e-12
    return {
        "loss_w_per_k": slope,
        "internal_gains_w": max(0.0, -intercept),
        "r2": 1.0 - ss_res / ss_tot,
        "days": float(len(xs)),
    }


def fit_cop_scale(hours: list[Hour], cop_model: Any, min_heat_w: float = 500.0) -> Optional[dict[str, float]]:
    """Median of measured / model COP in hours with space heating operation."""
    ratios: list[float] = []
    for hour in hours:
        heat, electric = hour.get("heat_w"), hour.get("electric_w")
        outdoor, flow = hour.get("outdoor_c"), hour.get("flow_c")
        if None in (heat, electric, outdoor, flow):
            continue
        if heat < min_heat_w or electric < 100.0:  # type: ignore[operator]
            continue
        if (hour.get("dhw_heat_w") or 0.0) > 0.1 * heat:  # type: ignore[operator]
            continue
        measured = heat / electric  # type: ignore[operator]
        model = cop_model.cop(outdoor, flow) / cop_model.scale
        ratios.append(measured / model)
    if len(ratios) < 5:
        return None
    return {"cop_scale": statistics.median(ratios), "hours": float(len(ratios))}


def fit_dhw_loss(
    hours: list[Hour], volume_l: float, ambient_c: float, night=(0, 5), min_drop_k: float = 0.2
) -> Optional[dict[str, float]]:
    """Tank loss from the night decay: dT/dt = -UA/C (T - T_amb)."""
    capacity = volume_l * WATER_WH_PER_LITRE_K
    rates: list[float] = []
    by_day: dict[Any, list[Hour]] = defaultdict(list)
    for hour in hours:
        if night[0] <= hour.timestamp.hour <= night[1]:
            by_day[hour.timestamp.date()].append(hour)
    for day_hours in by_day.values():
        series = [(h.timestamp, h.get("dhw_c")) for h in day_hours if h.get("dhw_c") is not None]
        if len(series) < 4:
            continue
        if any((h.get("dhw_heat_w") or 0.0) > 50 for h in day_hours):
            continue
        temps = [t for _, t in series]
        if any(b > a + 0.05 for a, b in zip(temps, temps[1:])):
            continue  # reheated
        if temps[0] - temps[-1] < min_drop_k:
            continue
        hours_span = (series[-1][0] - series[0][0]).total_seconds() / 3600
        if temps[-1] - ambient_c <= 0 or temps[0] - ambient_c <= 0:
            continue
        k = math.log((temps[0] - ambient_c) / (temps[-1] - ambient_c)) / hours_span
        rates.append(k)
    if not rates:
        return None
    k = statistics.median(rates)
    return {"loss_w_per_k": k * capacity, "nights": float(len(rates))}


def build_snippet(device_id: str, results: dict[str, Optional[dict[str, float]]]) -> dict[str, Any]:
    snippet: dict[str, Any] = {"device_id": device_id}
    building = results.get("building")
    if building:
        snippet["building"] = {
            "loss_w_per_k": round(building["loss_w_per_k"], 1),
            "internal_gains_w": round(building["internal_gains_w"], 0),
        }
    cop = results.get("cop")
    if cop:
        snippet["unit"] = {"cop_scale": round(cop["cop_scale"], 3)}
    dhw = results.get("dhw")
    if dhw:
        snippet["dhw"] = {"loss_w_per_k": round(dhw["loss_w_per_k"], 2)}
    return {"devices": {"heat_pumps": {device_id: snippet}}}


def cmd_fit(args: argparse.Namespace) -> int:
    # Import here so `fetch` works without the EOS package installed.
    from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel

    hours = read_csv(Path(args.csv))
    results = {
        "building": fit_building_loss(hours),
        "cop": fit_cop_scale(hours, CopModel(WBB12_RATING_POINTS)),
        "dhw": fit_dhw_loss(hours, args.dhw_volume_l, args.dhw_ambient_c),
    }
    for name, result in results.items():
        if result is None:
            print(f"{name:9s}: not enough suitable data", file=sys.stderr)
        else:
            text = ", ".join(f"{k}={v:.3f}" for k, v in result.items())
            print(f"{name:9s}: {text}", file=sys.stderr)
    print(json.dumps(build_snippet(args.device_id, results), indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="export hourly data from Home Assistant")
    p_fetch.add_argument("--ha-url", required=True)
    p_fetch.add_argument("--token", required=True)
    p_fetch.add_argument("--days", type=int, default=10)
    p_fetch.add_argument("--entity", action="append", help="override: column=entity_id")
    p_fetch.add_argument("-o", "--output", required=True)
    p_fetch.set_defaults(func=cmd_fetch)

    p_fit = sub.add_parser("fit", help="fit model parameters from a CSV")
    p_fit.add_argument("csv")
    p_fit.add_argument("--device-id", default="wbb12")
    p_fit.add_argument("--dhw-volume-l", type=float, default=190.0)
    p_fit.add_argument("--dhw-ambient-c", type=float, default=18.0)
    p_fit.set_defaults(func=cmd_fit)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
