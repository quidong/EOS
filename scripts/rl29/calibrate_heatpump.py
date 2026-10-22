#!/usr/bin/env python3
r"""Calibrate the heat pump model from measured Home Assistant data (RL29 fork).

The same fits run automatically inside EOS with
``devices.heat_pumps.<id>.calibration.enabled``; this tool is for a first
calibration before the measurement history exists in EOS, and for checking
the fits by hand.

Two steps:

1. ``fetch``: read the history of the relevant entities from Home Assistant and
   write an hourly CSV (mean values per hour)::

       python3 scripts/rl29/calibrate_heatpump.py fetch \\
           --ha-url http://192.168.30.180:8123 --token "$HA_TOKEN" --days 30 \\
           --open-meteo 49.49,8.47 -o hp.csv

   Entity ids default to the RL29 installation (see ``DEFAULT_ENTITIES``) and can
   be overridden with ``--entity column=sensor.xyz``. ``--open-meteo lat,lon``
   adds the global irradiance (and the outdoor temperature where no entity is
   given) from the Open-Meteo archive, which the solar term of the building fit
   needs. Only the Python standard library is needed for ``fetch``.

2. ``fit``: estimate the parameters from the CSV and print a configuration
   snippet for ``devices.heat_pumps.<id>``::

       python3 scripts/rl29/calibrate_heatpump.py fit hp.csv --device-id wbb12 \\
           --prior-loss 180 --prior-cop-scale 1.0

   The fits are those of ``akkudoktoreos.devices.thermal.calibration``:
   building losses, solar aperture and internal gains from a daily regression
   with the irradiance, the COP scale net of standby power from hours above the
   lowest compressor modulation, the tank loss from the night decay. The
   ``--prior-*`` values are the configured ones; the fits are shrunk towards
   them (``--prior-weight-days``, ``--cop-prior-hours``) and clipped to a band
   around them. ``--prior-weight-days 0 --cop-prior-hours 0 --dhw-prior-nights 0``
   trusts the data alone.

Columns of the CSV: ``timestamp`` (ISO, hourly), ``outdoor_c``, ``room_c``,
``heat_w`` (thermal output), ``electric_w``, ``flow_c``, ``dhw_c`` and optionally
``dhw_heat_w`` (thermal output into the tank) and ``ghi_w_per_m2``. Missing
columns skip the corresponding fit.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

DEFAULT_ENTITIES = {
    "outdoor_c": "sensor.wh_system_aussentemperatur",
    "room_c": "sensor.raumtemperatur_mittel",
    "heat_w": "sensor.wh_warmepumpe_warmeleistung",
    "electric_w": "sensor.strom_warmepumpe",
    "flow_c": "sensor.wh_warmepumpe_vorlauftemperatur",
    "dhw_c": "sensor.wh_warmwasser_warmwassertemperatur",
}
COLUMNS = [
    "outdoor_c",
    "room_c",
    "heat_w",
    "electric_w",
    "flow_c",
    "dhw_c",
    "dhw_heat_w",
    "ghi_w_per_m2",
]
WATER_WH_PER_LITRE_K = 1.163


# ---------------------------------------------------------------------------
# Data handling (standard library only, so `fetch` runs anywhere)
# ---------------------------------------------------------------------------


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


def read_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            values: dict[str, Any] = {"timestamp": datetime.fromisoformat(row["timestamp"])}
            for key in COLUMNS:
                raw = row.get(key)
                values[key] = float(raw) if raw not in (None, "", "nan") else None
            rows.append(values)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", *COLUMNS])
        for row in rows:
            writer.writerow(
                [row["timestamp"].isoformat()]
                + ["" if row.get(c) is None else f"{row[c]:.4f}" for c in COLUMNS]
            )


def rows_from_columns(columns: dict[str, dict[datetime, float]]) -> list[dict[str, Any]]:
    stamps = sorted({t for values in columns.values() for t in values})
    return [{"timestamp": t, **{c: columns.get(c, {}).get(t) for c in COLUMNS}} for t in stamps]


# ---------------------------------------------------------------------------
# Fetch from Home Assistant and Open-Meteo
# ---------------------------------------------------------------------------


def fetch_history(
    base_url: str, token: str, entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
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
    if not url.startswith(("http://", "https://")):
        raise ValueError("HA URL must be http(s)")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
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


def fetch_open_meteo(
    latitude: float, longitude: float, start: datetime, end: datetime
) -> dict[str, dict[datetime, float]]:
    """Hourly irradiance and temperature of past days from the Open-Meteo archive.

    Open-Meteo stamps an hourly value at the start of the hour it averages,
    which is the convention of the CSV.
    """
    query = urllib.parse.urlencode(
        {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "hourly": "shortwave_radiation,temperature_2m",
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
            "timezone": "UTC",
        }
    )
    url = f"https://archive-api.open-meteo.com/v1/archive?{query}"
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - admin tool
        data = json.loads(resp.read().decode("utf-8"))
    hourly = data.get("hourly") or {}
    result: dict[str, dict[datetime, float]] = {"ghi_w_per_m2": {}, "outdoor_c": {}}
    for stamp, ghi, temp in zip(
        hourly.get("time") or [],
        hourly.get("shortwave_radiation") or [],
        hourly.get("temperature_2m") or [],
    ):
        when = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        if ghi is not None:
            result["ghi_w_per_m2"][when] = float(ghi)
        if temp is not None:
            result["outdoor_c"][when] = float(temp)
    return result


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
    if args.open_meteo:
        latitude, longitude = (float(v) for v in args.open_meteo.split(","))
        weather = fetch_open_meteo(latitude, longitude, start, end)
        for column, values in weather.items():
            if column == "outdoor_c" and columns.get(column):
                continue  # the plant's own sensor wins
            columns[column] = {t: v for t, v in values.items() if start <= t < end}
            print(f"{column:12s} {'open-meteo':50s} {len(columns[column]):5d} h", file=sys.stderr)
    rows = rows_from_columns(columns)
    write_csv(Path(args.output), rows)
    print(f"written {args.output}: {len(rows)} hours", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Fit (uses the EOS package)
# ---------------------------------------------------------------------------


def build_snippet(device_id: str, result: Any) -> dict[str, Any]:
    snippet: dict[str, Any] = {"device_id": device_id}
    if result.building:
        snippet["building"] = {
            "loss_w_per_k": result.building.loss_w_per_k,
            "solar_aperture_m2": result.building.solar_aperture_m2,
            "internal_gains_w": result.building.internal_gains_w,
        }
    if result.cop:
        snippet["unit"] = {"cop_scale": result.cop.cop_scale}
    if result.dhw:
        snippet["dhw"] = {"loss_w_per_k": result.dhw.loss_w_per_k}
    return {"devices": {"heat_pumps": {device_id: snippet}}}


def cmd_fit(args: argparse.Namespace) -> int:
    # Import here so `fetch` works without the EOS package installed.
    from akkudoktoreos.devices.thermal.calibration import (
        CalibrationOptions,
        CalibrationPriors,
        HourlySample,
        calibrate,
    )
    from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel

    rows = read_csv(Path(args.csv))
    samples = [HourlySample(row["timestamp"], **{c: row.get(c) for c in COLUMNS}) for row in rows]
    priors = CalibrationPriors(
        loss_w_per_k=args.prior_loss,
        internal_gains_w=args.prior_gains,
        solar_aperture_m2=args.prior_aperture,
        cop_scale=args.prior_cop_scale,
        dhw_loss_w_per_k=args.prior_dhw_loss,
        standby_w=args.standby_w,
        dhw_volume_l=args.dhw_volume_l,
        dhw_ambient_c=args.dhw_ambient_c,
    )
    options = CalibrationOptions(
        prior_weight_days=args.prior_weight_days,
        cop_prior_hours=args.cop_prior_hours,
        cop_min_heat_w=args.cop_min_heat_w,
        dhw_prior_nights=args.dhw_prior_nights,
    )
    tz = ZoneInfo(args.timezone) if args.timezone else None
    result = calibrate(
        samples,
        priors,
        CopModel(WBB12_RATING_POINTS, scale=1.0),
        options,
        tz,
        window_days=args.days or 0,
    )
    print(result.summary(priors), file=sys.stderr)
    if result.building:
        b = result.building
        print(
            f"building : {b.days} days, r2={b.r2:.3f}, excluded outage={b.excluded_outage_days} "
            f"drift={b.excluded_drift_days}, solar {'fitted' if b.solar_fitted else 'not fitted'}",
            file=sys.stderr,
        )
    if result.cop:
        print(f"cop      : raw scale {result.cop.raw_scale:.3f}", file=sys.stderr)
    if result.dhw:
        print(f"dhw      : raw loss {result.dhw.raw_loss_w_per_k:.2f} W/K", file=sys.stderr)
    print(json.dumps(build_snippet(args.device_id, result), indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="export hourly data from Home Assistant")
    p_fetch.add_argument("--ha-url", required=True)
    p_fetch.add_argument("--token", required=True)
    p_fetch.add_argument("--days", type=int, default=30)
    p_fetch.add_argument("--entity", action="append", help="override: column=entity_id")
    p_fetch.add_argument(
        "--open-meteo",
        metavar="LAT,LON",
        help="add irradiance (and outdoor temperature if no entity) from the Open-Meteo archive",
    )
    p_fetch.add_argument("-o", "--output", required=True)
    p_fetch.set_defaults(func=cmd_fetch)

    p_fit = sub.add_parser("fit", help="fit model parameters from a CSV")
    p_fit.add_argument("csv")
    p_fit.add_argument("--device-id", default="wbb12")
    p_fit.add_argument("--timezone", default="Europe/Berlin", help="local days and nights")
    p_fit.add_argument("--days", type=int, default=None, help="window length, for the report")
    p_fit.add_argument("--prior-loss", type=float, default=180.0, help="configured loss_w_per_k")
    p_fit.add_argument("--prior-gains", type=float, default=0.0, help="configured internal_gains_w")
    p_fit.add_argument(
        "--prior-aperture", type=float, default=0.0, help="configured solar_aperture_m2"
    )
    p_fit.add_argument("--prior-cop-scale", type=float, default=1.0, help="configured cop_scale")
    p_fit.add_argument(
        "--prior-dhw-loss", type=float, default=1.5, help="configured dhw.loss_w_per_k"
    )
    p_fit.add_argument("--prior-weight-days", type=float, default=7.0)
    p_fit.add_argument("--cop-prior-hours", type=float, default=24.0)
    p_fit.add_argument("--cop-min-heat-w", type=float, default=3000.0)
    p_fit.add_argument("--dhw-prior-nights", type=float, default=3.0)
    p_fit.add_argument("--standby-w", type=float, default=22.0)
    p_fit.add_argument("--dhw-volume-l", type=float, default=190.0)
    p_fit.add_argument("--dhw-ambient-c", type=float, default=18.0)
    p_fit.set_defaults(func=cmd_fit)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
