#!/usr/bin/env python3
"""Compare the EOS heat pump plan with what actually happened (eos-plus fork).

Home Assistant records the plan values EOS publishes for the running hour
(``sensor.eos_<id>_heating_mode``, ``..._room_temperature_c``,
``..._energy_wh``). This script puts them next to the measurements per hour
and reports how well the model predicts:

- room temperature: planned value for the end of hour h against the measured
  hourly mean of hour h + 1 (bias and mean absolute error),
- electric energy: planned Wh of hour h against the measured mean power of
  hour h (= Wh), in total and per heating mode.

Large deviations are the input for the calibration
(``calibrate_heatpump.py``).

Usage::

    # fetch from Home Assistant into a CSV, then compare
    python3 scripts/plus/compare_heatpump_plan.py fetch \\
        --ha-url http://homeassistant.local:8123 --token "$HA_TOKEN" --days 3 -o plan_ist.csv
    python3 scripts/plus/compare_heatpump_plan.py compare plan_ist.csv

CSV columns: ``timestamp`` (hour start, ISO), ``mode``, ``plan_room_c``,
``plan_energy_wh``, ``room_c``, ``electric_w``.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

COLUMNS = ["mode", "plan_room_c", "plan_energy_wh", "room_c", "electric_w"]
MODE_NAMES = {0: "Reduziert", 1: "Normal", 2: "Angehoben", 3: "Vorheizen", 4: "Forciert"}


def default_entities(device_id: str) -> dict[str, str]:
    return {
        "mode": f"sensor.eos_{device_id}_heating_mode",
        "plan_room_c": f"sensor.eos_{device_id}_room_temperature_c",
        "plan_energy_wh": f"sensor.eos_{device_id}_energy_wh",
        "room_c": "sensor.raumtemperatur_mittel",
        "electric_w": "sensor.strom_warmepumpe",
    }


def _calibrate_module():
    path = Path(__file__).with_name("calibrate_heatpump.py")
    spec = importlib.util.spec_from_file_location("calibrate_heatpump", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("calibrate_heatpump", module)
    spec.loader.exec_module(module)
    return module


@dataclass
class Row:
    timestamp: datetime
    mode: Optional[float] = None
    plan_room_c: Optional[float] = None
    plan_energy_wh: Optional[float] = None
    room_c: Optional[float] = None
    electric_w: Optional[float] = None


def read_csv(path: Path) -> list[Row]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for item in csv.DictReader(fh):
            values = {
                key: (float(item[key]) if item.get(key) not in (None, "", "nan") else None)
                for key in COLUMNS
            }
            rows.append(Row(datetime.fromisoformat(item["timestamp"]), **values))
    rows.sort(key=lambda r: r.timestamp)
    return rows


def write_csv(path: Path, rows: list[Row]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", *COLUMNS])
        for row in rows:
            writer.writerow(
                [row.timestamp.isoformat()]
                + ["" if getattr(row, c) is None else f"{getattr(row, c):.4f}" for c in COLUMNS]
            )


@dataclass
class Report:
    hours: int
    room_pairs: int
    room_bias_k: Optional[float]
    room_mae_k: Optional[float]
    energy_hours: int
    plan_energy_kwh: float
    actual_energy_kwh: float
    per_mode: dict[int, tuple[int, float, float]]

    @property
    def energy_ratio(self) -> Optional[float]:
        if self.plan_energy_kwh <= 0:
            return None
        return self.actual_energy_kwh / self.plan_energy_kwh

    def lines(self) -> list[str]:
        out = [f"Stunden: {self.hours}"]
        if self.room_mae_k is None:
            out.append("Raumtemperatur: keine Paare")
        else:
            out.append(
                f"Raumtemperatur ({self.room_pairs} h): Abweichung Ist - Plan "
                f"{self.room_bias_k:+.2f} K, mittlerer Fehler {self.room_mae_k:.2f} K"
            )
        ratio = self.energy_ratio
        out.append(
            f"WP-Strom ({self.energy_hours} h): Plan {self.plan_energy_kwh:.2f} kWh, "
            f"Ist {self.actual_energy_kwh:.2f} kWh"
            + (f" (Ist/Plan {ratio:.2f})" if ratio is not None else "")
        )
        for mode, (count, plan, actual) in sorted(self.per_mode.items()):
            out.append(
                f"  {MODE_NAMES.get(mode, mode)}: {count} h, Plan {plan:.2f} kWh, Ist {actual:.2f} kWh"
            )
        return out


def compare(rows: list[Row]) -> Report:
    by_time = {r.timestamp: r for r in rows}
    diffs = []
    for row in rows:
        nxt = by_time.get(row.timestamp + timedelta(hours=1))
        if row.plan_room_c is not None and nxt is not None and nxt.room_c is not None:
            diffs.append(nxt.room_c - row.plan_room_c)
    per_mode: dict[int, list[float]] = {}
    plan_total = actual_total = 0.0
    energy_hours = 0
    for row in rows:
        if row.plan_energy_wh is None or row.electric_w is None:
            continue
        energy_hours += 1
        plan_total += row.plan_energy_wh / 1000.0
        actual_total += row.electric_w / 1000.0
        if row.mode is not None and math.isfinite(row.mode):
            entry = per_mode.setdefault(int(round(row.mode)), [0, 0.0, 0.0])
            entry[0] += 1
            entry[1] += row.plan_energy_wh / 1000.0
            entry[2] += row.electric_w / 1000.0
    return Report(
        hours=len(rows),
        room_pairs=len(diffs),
        room_bias_k=statistics.fmean(diffs) if diffs else None,
        room_mae_k=statistics.fmean(abs(d) for d in diffs) if diffs else None,
        energy_hours=energy_hours,
        plan_energy_kwh=plan_total,
        actual_energy_kwh=actual_total,
        per_mode={k: (int(v[0]), v[1], v[2]) for k, v in per_mode.items()},
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    cal = _calibrate_module()
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    entities = default_entities(args.device_id)
    columns: dict[str, dict[datetime, float]] = {}
    for column, entity_id in entities.items():
        samples = cal.fetch_history(args.ha_url, args.token, entity_id, start, end)
        columns[column] = cal.hourly_means(samples, start, end)
    hours = sorted({h for values in columns.values() for h in values})
    rows = [Row(h, **{c: columns[c].get(h) for c in COLUMNS}) for h in hours]
    write_csv(Path(args.output), rows)
    print(f"{len(rows)} Stunden nach {args.output} geschrieben")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    report = compare(read_csv(Path(args.csv)))
    print("\n".join(report.lines()))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_fetch = sub.add_parser("fetch", help="read plan and measurements from Home Assistant")
    p_fetch.add_argument("--ha-url", required=True)
    p_fetch.add_argument("--token", required=True)
    p_fetch.add_argument("--days", type=int, default=3)
    p_fetch.add_argument("--device-id", default="wbb12")
    p_fetch.add_argument("-o", "--output", required=True)
    p_fetch.set_defaults(func=cmd_fetch)
    p_cmp = sub.add_parser("compare", help="compare plan and measurements from a CSV")
    p_cmp.add_argument("csv")
    p_cmp.set_defaults(func=cmd_compare)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
