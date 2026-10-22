#!/usr/bin/env python3
"""Plausibility check of the heat pump part of the current EOS plan (RL29 fork).

Used during commissioning: after the HA main switch is on, the plan is
checked every hour. Exit code 0 = plausible, 1 = warnings, 2 = implausible
(switch the HA control off).

Rules (thresholds from the heat pump configuration, overridable):

- FAIL  plan older than ``--max-age-min`` (default 45 min)
- FAIL  planned room temperature above comfort max + forced overshoot + 0.5 K
- FAIL  planned room temperature below comfort minimum - 1 K
- FAIL  active heating (RAISED/PREHEAT/FORCED) for more than ``--max-active-h``
  consecutive hours (default 6)
- WARN  active heating in an hour without PV surplus and with a price above
  the 75th percentile of the horizon
- WARN  REDUCED for more than 4 consecutive hours
- WARN  hot water HEAT_PUMP in more than 4 hours of the horizon

Input: ``--url http://192.168.30.85:8503`` (reads the solution and the
configuration) or ``--solution file.json --config file.json``.

Usage::

    python3 scripts/rl29/check_heatpump_plan.py --url http://192.168.30.85:8503 --device wbb12
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

ACTIVE = {2, 3, 4}
NAMES = {0: "Reduziert", 1: "Normal", 2: "Angehoben", 3: "Vorheizen", 4: "Forciert"}


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.failures:
            return 2
        return 1 if self.warnings else 0

    def lines(self) -> list[str]:
        status = {0: "PLAUSIBEL", 1: "WARNUNG", 2: "UNPLAUSIBEL"}[self.exit_code]
        out = [f"Status: {status}"]
        out += [f"FAIL  {m}" for m in self.failures]
        out += [f"WARN  {m}" for m in self.warnings]
        out += [f"INFO  {m}" for m in self.info]
        return out


def _get(base: str, path: str) -> Any:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=60) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _runs(flags: list[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def comfort_limits(config: dict[str, Any], device: str) -> tuple[float, float]:
    """(lowest comfort minimum, comfort max + forced overshoot) of the device."""
    hp = config.get("devices", {}).get("heat_pumps", {}).get(device, {})
    comfort = hp.get("comfort", {})
    max_c = float(comfort.get("max_c", 22.0))
    overshoot = float(hp.get("unit", {}).get("forced_overshoot_k", 1.0))
    windows = (comfort.get("min_c") or {}).get("windows") or []
    minima = [float(w["value"]) for w in windows if w.get("value") is not None]
    return (min(minima) if minima else 19.5), max_c + overshoot


def check(
    solution: dict[str, Any],
    config: dict[str, Any],
    device: str = "wbb12",
    now: Optional[datetime] = None,
    max_age_min: float = 45.0,
    max_active_h: int = 6,
    max_reduced_h: int = 6,
) -> Report:
    report = Report()
    now = now or datetime.now(timezone.utc)
    generated = datetime.fromisoformat(solution["generated_at"])
    age_min = (now - generated).total_seconds() / 60.0
    report.info.append(
        f"Plan von {generated.isoformat(timespec='minutes')} ({age_min:.0f} min alt)"
    )
    if age_min > max_age_min:
        report.failures.append(f"Plan ist {age_min:.0f} min alt (> {max_age_min:.0f} min)")

    rows = solution["solution"]["data"]
    prediction = (solution.get("prediction") or {}).get("data", {})
    times = sorted(rows)
    upcoming = [
        t
        for t in times
        if datetime.fromisoformat(t) >= now.replace(minute=0, second=0, microsecond=0)
    ]
    if not upcoming:
        report.failures.append("Plan enthält keine kommenden Stunden")
        return report

    key = f"{device}_heating_mode"
    if key not in rows[upcoming[0]]:
        report.failures.append(f"Plan enthält keine Wärmepumpe '{device}'")
        return report

    modes = [int(round(float(rows[t][key]))) for t in upcoming]
    dhw = [int(round(float(rows[t].get(f"{device}_dhw_mode", 0)))) for t in upcoming]
    rooms = [float(rows[t][f"{device}_room_temperature_c"]) for t in upcoming]
    prices = [float(prediction.get(t, {}).get("elec_price_amt_kwh", 0.0)) for t in upcoming]
    pv = [float(prediction.get(t, {}).get("pvforecast_ac_energy_wh", 0.0)) for t in upcoming]
    load = [float(rows[t].get("load_energy_wh", 0.0)) for t in upcoming]
    feedin = [float(rows[t].get("grid_feedin_energy_wh", 0.0)) for t in upcoming]

    low, high = comfort_limits(config, device)
    report.info.append(
        "Modi: "
        + " ".join(f"{datetime.fromisoformat(t).hour:02d}:{m}" for t, m in zip(upcoming, modes))
    )
    report.info.append(
        f"Raum geplant {min(rooms):.1f}–{max(rooms):.1f} °C (Band {low:.1f}–{high:.1f})"
    )

    if max(rooms) > high + 0.5:
        report.failures.append(f"Geplante Raumtemperatur {max(rooms):.1f} °C > {high + 0.5:.1f} °C")
    if min(rooms) < low - 1.0:
        report.failures.append(f"Geplante Raumtemperatur {min(rooms):.1f} °C < {low - 1.0:.1f} °C")

    active_run = _runs([m in ACTIVE for m in modes])
    if active_run > max_active_h:
        report.failures.append(f"{active_run} h am Stück aktiv geheizt (> {max_active_h} h)")

    expensive = _percentile(prices, 0.75)
    for t, m, p, gen, ld, fi in zip(upcoming, modes, prices, pv, load, feedin):
        surplus = gen > ld or fi > 100.0
        if m in ACTIVE and not surplus and p > expensive:
            report.warnings.append(
                f"{datetime.fromisoformat(t).strftime('%H:%M')} {NAMES[m]} ohne PV-Überschuss "
                f"bei {p * 100:.1f} ct/kWh"
            )

    reduced_run = _runs([m == 0 for m in modes])
    if reduced_run > max_reduced_h:
        report.warnings.append(
            f"{reduced_run} h am Stück Reduziert (> {max_reduced_h} h, HA begrenzt die Absenkung)"
        )
    if sum(1 for d in dhw if d == 1) > 4:
        report.warnings.append(
            f"Warmwasser-Ladung in {sum(1 for d in dhw if d == 1)} Stunden geplant"
        )
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="EOS base URL")
    parser.add_argument("--solution", help="Solution JSON file")
    parser.add_argument("--config", help="Configuration JSON file")
    parser.add_argument("--device", default="wbb12")
    parser.add_argument("--max-age-min", type=float, default=45.0)
    parser.add_argument("--max-active-h", type=int, default=6)
    parser.add_argument(
        "--max-reduced-h", type=int, default=6, help="Warn above this many reduced hours in a row"
    )
    parser.add_argument("--now", help="ISO time instead of the current time (tests)")
    args = parser.parse_args(argv)

    if args.url:
        try:
            solution = _get(args.url, "/v1/energy-management/optimization/solution")
            config = _get(args.url, "/v1/config")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            report = Report(failures=[f"Kein Plan abrufbar: {exc}"])
            print("\n".join(report.lines()))
            return report.exit_code
    elif args.solution and args.config:
        with open(args.solution, encoding="utf-8") as f:
            solution = json.load(f)
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
    else:
        parser.error("--url or --solution and --config required")

    now = datetime.fromisoformat(args.now) if args.now else None
    report = check(
        solution,
        config,
        args.device,
        now,
        args.max_age_min,
        args.max_active_h,
        args.max_reduced_h,
    )
    print("\n".join(report.lines()))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
