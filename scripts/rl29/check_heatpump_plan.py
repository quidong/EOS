#!/usr/bin/env python3
"""Plausibility check of the heat pump part of the current EOS plan (RL29 fork).

Used during commissioning: after the HA main switch is on, the plan is
checked every hour. Exit code 0 = plausible, 1 = warnings, 2 = implausible
(switch the HA control off).

Rules (thresholds from the heat pump configuration, overridable):

- FAIL  plan older than ``--max-age-min`` (default 45 min)
- FAIL  planned room temperature above comfort max + forced overshoot + 0.5 K
- FAIL  planned room temperature below comfort minimum - 1 K; in holiday slots
  (``<device>_away`` = 1) the lower bound is min(holiday floor, holiday room
  temperature) - 1 K instead (measurements ``<device>-away-min-temperature`` /
  ``<device>-away-room-temperature``, else ``away.standby_min_c`` / ``away.room_c``)
- FAIL  strong heating (PREHEAT/FORCED) for more than ``--max-active-h``
  consecutive hours (default 6)
- WARN  active heating incl. RAISED for more than ``--max-raised-h`` consecutive
  hours (default 8); RAISED alone (+1 K, no forced valves) is no emergency
- WARN  active heating in an hour without PV surplus and with a price above
  the 75th percentile of the horizon
- WARN  REDUCED for more than ``--max-reduced-h`` consecutive hours (default 6,
  matching the HA limit input_number.eos_wp_max_absenkdauer); holiday slots do
  not count (HA does not limit the reduction while away)
- WARN  hot water HEAT_PUMP in more than 4 hours of the horizon

All durations are in hours; the slot length is taken from the plan timestamps
(EOS plans hourly or, since the MILP update, in 15 min steps).

Input: ``--url http://eos.local:8503`` (reads the solution and the
configuration) or ``--solution file.json --config file.json``.

Usage::

    python3 scripts/rl29/check_heatpump_plan.py --url http://eos.local:8503 --device wbb12
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

ACTIVE = {2, 3, 4}
STRONG = {3, 4}
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


def _latest(base: str, key: str, days: int = 30) -> Optional[float]:
    """Latest value of an EOS measurement within ``days`` (None if unavailable)."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    query = urllib.parse.urlencode({"key": key, "start_datetime": start})
    try:
        series = _get(base, f"/v1/measurement/series?{query}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    data = series.get("data") if isinstance(series, dict) else None
    if not data:
        return None
    values = [v for _, v in sorted(data.items()) if v is not None]
    try:
        return float(values[-1]) if values else None
    except (TypeError, ValueError):
        return None


def _runs(flags: list[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def slot_hours(times: list[str]) -> float:
    """Length of one plan slot in hours (EOS plans hourly or in 15 min steps)."""
    stamps = [datetime.fromisoformat(t) for t in times]
    diffs = sorted((b - a).total_seconds() / 3600.0 for a, b in zip(stamps, stamps[1:]) if b > a)
    return diffs[len(diffs) // 2] if diffs else 1.0


def _h(value: float) -> str:
    """Hours for messages: 7 -> '7', 1.75 -> '1.75'."""
    return f"{value:g}"


def _mode_changes(times: list[str], modes: list[int]) -> str:
    """Compact mode overview: only the slots where the mode changes."""
    parts = []
    previous = None
    for t, m in zip(times, modes):
        if m != previous:
            parts.append(f"{datetime.fromisoformat(t).strftime('%H:%M')}:{m}")
            previous = m
    return " ".join(parts)


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


def away_floor(
    config: dict[str, Any],
    device: str,
    away_room_c: Optional[float] = None,
    away_min_c: Optional[float] = None,
) -> float:
    """Lowest comfort temperature EOS may plan in a holiday slot.

    Mirrors the optimizer: min(floor, holiday room temperature); the floor is
    optional (``away.standby_min_c`` or the measurement).
    """
    away = config.get("devices", {}).get("heat_pumps", {}).get(device, {}).get("away") or {}
    room = away_room_c if away_room_c is not None else away.get("room_c")
    room = float(room) if room is not None else 20.0
    floor = away_min_c if away_min_c is not None else away.get("standby_min_c")
    return min(float(floor), room) if floor is not None else room


def check(
    solution: dict[str, Any],
    config: dict[str, Any],
    device: str = "wbb12",
    now: Optional[datetime] = None,
    max_age_min: float = 45.0,
    max_active_h: int = 6,
    max_raised_h: int = 8,
    max_reduced_h: int = 6,
    away_room_c: Optional[float] = None,
    away_min_c: Optional[float] = None,
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
    away = [float(rows[t].get(f"{device}_away", 0.0) or 0.0) >= 0.5 for t in upcoming]
    feedin = [float(rows[t].get("grid_feedin_energy_wh", 0.0)) for t in upcoming]

    low, high = comfort_limits(config, device)
    slot_h = slot_hours(upcoming)
    report.info.append(
        f"Modi (Wechsel, Raster {slot_h * 60:.0f} min): {_mode_changes(upcoming, modes)}"
    )
    low_away = away_floor(config, device, away_room_c, away_min_c)
    report.info.append(
        f"Raum geplant {min(rooms):.1f}–{max(rooms):.1f} °C (Band {low:.1f}–{high:.1f})"
    )
    if any(away):
        report.info.append(
            f"Urlaub in {_h(sum(away) * slot_h)} von {_h(len(away) * slot_h)} Stunden, "
            f"Untergrenze {low_away:.1f} °C"
        )

    if max(rooms) > high + 0.5:
        report.failures.append(f"Geplante Raumtemperatur {max(rooms):.1f} °C > {high + 0.5:.1f} °C")
    lows = [(low_away if a else low) - 1.0 for a in away]
    cold = [(r, lim, a) for r, lim, a in zip(rooms, lows, away) if r < lim]
    if cold:
        r, lim, a = min(cold, key=lambda x: x[0] - x[1])
        report.failures.append(
            f"Geplante Raumtemperatur {r:.1f} °C < {lim:.1f} °C" + (" (Urlaub)" if a else "")
        )

    # Durations in hours, independent of the plan resolution (1 h or 15 min).
    strong_run = _runs([m in STRONG for m in modes]) * slot_h
    if strong_run > max_active_h:
        report.failures.append(
            f"{_h(strong_run)} h am Stück Vorheizen/Forciert (> {max_active_h} h)"
        )
    active_run = _runs([m in ACTIVE for m in modes]) * slot_h
    if active_run > max_raised_h:
        report.warnings.append(
            f"{_h(active_run)} h am Stück aktiv geheizt inkl. Angehoben (> {max_raised_h} h)"
        )

    # Surplus per slot (energies are per slot, so the comparison stays valid);
    # one warning per hour, also for 15 min plans.
    expensive = _percentile(prices, 0.75)
    feedin_min = 100.0 * slot_h
    warned_hours: set[str] = set()
    for t, m, p, gen, ld, fi in zip(upcoming, modes, prices, pv, load, feedin):
        surplus = gen > ld or fi > feedin_min
        hour = datetime.fromisoformat(t).strftime("%H:00")
        if m in ACTIVE and not surplus and p > expensive and hour not in warned_hours:
            warned_hours.add(hour)
            report.warnings.append(
                f"{datetime.fromisoformat(t).strftime('%H:%M')} {NAMES[m]} ohne PV-Überschuss "
                f"bei {p * 100:.1f} ct/kWh"
            )

    reduced_run = _runs([m == 0 and not a for m, a in zip(modes, away)]) * slot_h
    if reduced_run > max_reduced_h:
        report.warnings.append(
            f"{_h(reduced_run)} h am Stück Reduziert (> {max_reduced_h} h, "
            "HA begrenzt die Absenkung)"
        )
    dhw_h = sum(1 for d in dhw if d == 1) * slot_h
    if dhw_h > 4:
        report.warnings.append(f"Warmwasser-Ladung in {_h(dhw_h)} Stunden geplant")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="EOS base URL")
    parser.add_argument("--solution", help="Solution JSON file")
    parser.add_argument("--config", help="Configuration JSON file")
    parser.add_argument("--device", default="wbb12")
    parser.add_argument("--max-age-min", type=float, default=45.0)
    parser.add_argument(
        "--max-active-h", type=int, default=6, help="Fail above this many PREHEAT/FORCED hours"
    )
    parser.add_argument(
        "--max-raised-h", type=int, default=8, help="Warn above this many active hours incl. RAISED"
    )
    parser.add_argument(
        "--max-reduced-h", type=int, default=6, help="Warn above this many reduced hours in a row"
    )
    parser.add_argument("--away-room-c", type=float, help="Holiday room temperature (override)")
    parser.add_argument("--away-min-c", type=float, help="Holiday floor temperature (override)")
    parser.add_argument("--now", help="ISO time instead of the current time (tests)")
    args = parser.parse_args(argv)

    if args.url:
        try:
            solution = _get(args.url, "/v1/energy-management/optimization/solution")
            config = _get(args.url, "/v1/config")
            if args.away_room_c is None:
                args.away_room_c = _latest(args.url, f"{args.device}-away-room-temperature")
            if args.away_min_c is None:
                args.away_min_c = _latest(args.url, f"{args.device}-away-min-temperature")
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
        max_age_min=args.max_age_min,
        max_active_h=args.max_active_h,
        max_raised_h=args.max_raised_h,
        max_reduced_h=args.max_reduced_h,
        away_room_c=args.away_room_c,
        away_min_c=args.away_min_c,
    )
    print("\n".join(report.lines()))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
