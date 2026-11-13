#!/usr/bin/env python3
"""Plausibility check of the battery part of the current EOS plan (RL29 fork).

Companion of ``check_heatpump_plan.py`` for a stationary battery
(``devices.batteries.<id>``). Exit code 0 = plausible, 1 = warnings,
2 = implausible (switch the HA battery control off).

Rules (limits from the battery configuration):

- FAIL  plan older than ``--max-age-min`` (default 45 min)
- FAIL  the battery is configured but has no columns in the plan
- FAIL  a slot without exactly one active operation mode
  (``<id>_<mode>_op_mode`` = 1)
- FAIL  planned state of charge below ``min_soc_percentage`` - 1 or above
  max(``max_soc_percentage``, ``full_charge_soc_percentage`` when a periodic
  full charge is configured) + 1 percentage points
- FAIL  grid charging (GRID_SUPPORT_IMPORT / FORCED_CHARGE) although the inverter
  of the battery has ``max_ac_charge_power_w`` = 0 (grid charging disabled).
  Exception: with ``full_charge_allow_grid`` and ``full_charge_grid_power_w`` a due
  full charge may charge from the grid up to that power (INFO); more is a FAIL
- WARN  grid charging (GRID_SUPPORT_IMPORT / FORCED_CHARGE) in a slot whose
  price, after the round-trip losses and the storage cost, is not below the
  highest price of the following ``--arbitrage-h`` hours (default 24): the
  stored energy cannot be used at a profit
- WARN  battery export to the grid (GRID_SUPPORT_EXPORT / FORCED_DISCHARGE)
- WARN  PV charging blocked (IDLE / PEAK_SHAVING) while more than
  ``--surplus-w`` (default 1000 W) are fed in and the battery is not full
- WARN  first planned state of charge differs from the measured one
  (``<id>-soc-factor``) by more than ``--soc-tolerance-pct`` (default 10)

Input: ``--url http://eos.local:8503`` (reads the solution, the configuration
and the measured state of charge) or ``--solution file.json --config file.json``.

Usage::

    python3 scripts/rl29/check_battery_plan.py --url http://eos.local:8503 --device byd
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

MODES = (
    "IDLE",
    "SELF_CONSUMPTION",
    "NON_EXPORT",
    "PEAK_SHAVING",
    "GRID_SUPPORT_EXPORT",
    "GRID_SUPPORT_IMPORT",
    "FREQUENCY_REGULATION",
    "RAMP_RATE_CONTROL",
    "RESERVE_BACKUP",
    "OUTAGE_SUPPLY",
    "FORCED_CHARGE",
    "FORCED_DISCHARGE",
    "FAULT",
)
GRID_CHARGE = {"GRID_SUPPORT_IMPORT", "FORCED_CHARGE"}
EXPORT = {"GRID_SUPPORT_EXPORT", "FORCED_DISCHARGE"}
PV_BLOCKED = {"IDLE", "PEAK_SHAVING"}
NAMES = {
    "IDLE": "Sperren",
    "SELF_CONSUMPTION": "Eigenverbrauch",
    "NON_EXPORT": "Halten",
    "PEAK_SHAVING": "Nur entladen",
    "GRID_SUPPORT_EXPORT": "Einspeisen",
    "GRID_SUPPORT_IMPORT": "Netzladen",
    "FORCED_CHARGE": "Netzladen",
    "FORCED_DISCHARGE": "Einspeisen",
}


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


def _latest(base: str, key: str, hours: float = 2.0) -> Optional[float]:
    """Latest value of an EOS measurement within ``hours`` (None if unavailable)."""
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
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


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def slot_hours(times: list[str]) -> float:
    """Length of one plan slot in hours."""
    stamps = [datetime.fromisoformat(t) for t in times]
    diffs = sorted((b - a).total_seconds() / 3600.0 for a, b in zip(stamps, stamps[1:]) if b > a)
    return diffs[len(diffs) // 2] if diffs else 1.0


def battery_config(config: dict[str, Any], device: str) -> Optional[dict[str, Any]]:
    batteries = config.get("devices", {}).get("batteries") or {}
    if isinstance(batteries, list):
        return next((b for b in batteries if b.get("device_id") == device), None)
    return batteries.get(device)


def grid_charge_disabled(config: dict[str, Any], device: str) -> bool:
    """True if the inverter of the battery may not charge from the grid."""
    inverters = config.get("devices", {}).get("inverters") or {}
    items = inverters if isinstance(inverters, list) else list(inverters.values())
    for inverter in items:
        if inverter.get("battery_id") == device:
            limit = inverter.get("max_ac_charge_power_w")
            return limit is not None and _float(limit, 1.0) <= 0.0
    return False


def full_charge_grid_power_w(battery: dict[str, Any]) -> Optional[float]:
    """Grid charging power allowed for a due full charge [W], None = no exception."""
    if not battery.get("full_charge_allow_grid") or not battery.get("full_charge_interval_days"):
        return None
    power = battery.get("full_charge_grid_power_w")
    if power is None:
        return None
    value = _float(power, 0.0)
    return value if value > 0.0 else None


def soc_limits(battery: dict[str, Any]) -> tuple[float, float]:
    """(minimum, maximum) planned state of charge in percent."""
    low = _float(battery.get("min_soc_percentage"), 0.0)
    high = _float(battery.get("max_soc_percentage"), 100.0)
    if battery.get("full_charge_interval_days"):
        high = max(high, _float(battery.get("full_charge_soc_percentage"), 100.0))
    return low, high


def active_modes(values: dict[str, Any], device: str) -> list[str]:
    return [m for m in MODES if _float(values.get(f"{device}_{m.lower()}_op_mode")) >= 0.5]


def _mode_changes(times: list[str], modes: list[str]) -> str:
    parts, previous = [], None
    for t, m in zip(times, modes):
        if m != previous:
            parts.append(f"{datetime.fromisoformat(t).strftime('%H:%M')} {NAMES.get(m, m)}")
            previous = m
    return ", ".join(parts)


def check(
    solution: dict[str, Any],
    config: dict[str, Any],
    device: str = "byd",
    now: Optional[datetime] = None,
    *,
    max_age_min: float = 45.0,
    arbitrage_h: float = 24.0,
    surplus_w: float = 1000.0,
    soc_tolerance_pct: float = 10.0,
    measured_soc_factor: Optional[float] = None,
) -> Report:
    report = Report()
    battery = battery_config(config, device)
    if battery is None:
        report.failures.append(f"Batterie {device} ist in EOS nicht konfiguriert")
        return report

    generated = solution.get("generated_at")
    now = now or datetime.now(timezone.utc)
    if generated:
        age_min = (now - datetime.fromisoformat(generated)).total_seconds() / 60.0
        if age_min > max_age_min:
            report.failures.append(f"Plan ist {age_min:.0f} min alt (> {max_age_min:.0f} min)")

    rows: dict[str, Any] = (solution.get("solution") or {}).get("data") or {}
    prediction: dict[str, Any] = (solution.get("prediction") or {}).get("data") or {}
    times = sorted(rows)
    if not times:
        report.failures.append("Plan enthält keine Zeitschritte")
        return report
    # The current slot and everything after it.
    upcoming = [t for t in times if datetime.fromisoformat(t) + timedelta(minutes=15) > now]
    upcoming = upcoming or times
    if not any(k.startswith(f"{device}_") for k in rows[upcoming[0]]):
        report.failures.append(f"Plan enthält keine Spalten für die Batterie {device}")
        return report

    slot_h = slot_hours(times)
    low, high = soc_limits(battery)
    eta = _float(battery.get("charging_efficiency"), 1.0) * _float(
        battery.get("discharging_efficiency"), 1.0
    )
    lcos = _float(battery.get("levelized_cost_of_storage_amt_kwh"), 0.0)

    modes: list[str] = []
    socs: list[Optional[float]] = []
    for t in upcoming:
        active = active_modes(rows[t], device)
        stamp = datetime.fromisoformat(t).strftime("%H:%M")
        if len(active) != 1:
            report.failures.append(
                f"{stamp} {len(active)} Betriebsarten aktiv ({', '.join(active) or 'keine'})"
            )
        modes.append(active[0] if active else "?")
        soc = rows[t].get(f"{device}_soc_factor")
        socs.append(_float(soc) * 100.0 if soc is not None else None)

    known = [s for s in socs if s is not None]
    if known:
        if min(known) < low - 1.0:
            report.failures.append(f"Ladestand {min(known):.1f} % < Minimum {low:.0f} %")
        if max(known) > high + 1.0:
            report.failures.append(f"Ladestand {max(known):.1f} % > Maximum {high:.0f} %")
        report.info.append(f"Ladestand geplant {min(known):.0f}–{max(known):.0f} %")
        if measured_soc_factor is not None and socs[0] is not None:
            measured = measured_soc_factor * 100.0
            if abs(socs[0] - measured) > soc_tolerance_pct:
                report.warnings.append(
                    f"Plan beginnt bei {socs[0]:.0f} %, gemessen {measured:.0f} % "
                    f"(> {soc_tolerance_pct:.0f} Prozentpunkte)"
                )

    prices = [
        _float((prediction.get(t) or {}).get("elec_price_amt_kwh"), float("nan")) for t in upcoming
    ]
    window = max(1, round(arbitrage_h / slot_h))
    grid_wh = 0.0
    warned_hours: set[str] = set()
    max_charge_w = _float(battery.get("max_charge_power_w"), 0.0)
    no_grid_charge = grid_charge_disabled(config, device)
    full_charge_w = full_charge_grid_power_w(battery) if no_grid_charge else None
    full_charge_wh = 0.0
    for i, (t, mode) in enumerate(zip(upcoming, modes)):
        stamp = datetime.fromisoformat(t).strftime("%H:%M")
        hour = datetime.fromisoformat(t).strftime("%H:00")
        values = rows[t]
        if mode in GRID_CHARGE:
            factor = _float(values.get(f"{device}_{mode.lower()}_op_factor"))
            grid_wh += factor * max_charge_w * slot_h
            if full_charge_w is not None and factor > 0.0:
                # Due full charge: grid charging up to the configured power is allowed.
                power_w = factor * max_charge_w
                if power_w <= full_charge_w * 1.1 + 50.0:
                    full_charge_wh += power_w * slot_h
                    continue
                if hour not in warned_hours:
                    warned_hours.add(hour)
                    report.failures.append(
                        f"{stamp} Netzladen mit {power_w:.0f} W über der Freigabe für die "
                        f"Vollladung ({full_charge_w:.0f} W)"
                    )
                continue
            if no_grid_charge and factor > 0.0 and hour not in warned_hours:
                warned_hours.add(hour)
                report.failures.append(
                    f"{stamp} Netzladen geplant, obwohl Laden aus dem Netz gesperrt ist "
                    "(max_ac_charge_power_w = 0)"
                )
            price = prices[i]
            later = [p for p in prices[i + 1 : i + 1 + window] if p == p]
            if price == price and later:
                cost = price / eta + lcos if eta > 0 else float("inf")
                if cost >= max(later) and hour not in warned_hours:
                    warned_hours.add(hour)
                    report.warnings.append(
                        f"{stamp} Netzladen bei {price * 100:.1f} ct/kWh lohnt nicht "
                        f"(mit Verlusten {cost * 100:.1f} ct, später höchstens "
                        f"{max(later) * 100:.1f} ct)"
                    )
        elif mode in EXPORT:
            if hour not in warned_hours:
                warned_hours.add(hour)
                report.warnings.append(f"{stamp} Einspeisen aus dem Akku geplant")
        elif mode in PV_BLOCKED:
            feedin_w = _float(values.get("grid_feedin_energy_wh")) / slot_h
            soc = socs[i]
            if (
                feedin_w > surplus_w
                and (soc is None or soc < high - 5.0)
                and hour not in warned_hours
            ):
                warned_hours.add(hour)
                report.warnings.append(
                    f"{stamp} PV-Laden gesperrt ({NAMES.get(mode, mode)}) bei "
                    f"{feedin_w:.0f} W Einspeisung"
                )

    report.info.append(f"Betriebsarten: {_mode_changes(upcoming, modes)}")
    if grid_wh > 0:
        report.info.append(f"Netzladen geplant: {grid_wh / 1000.0:.1f} kWh")
    if full_charge_wh > 0:
        report.info.append(
            f"davon für die Vollladung freigegeben: {full_charge_wh / 1000.0:.1f} kWh"
        )
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="EOS base URL")
    parser.add_argument("--solution", help="Solution JSON file")
    parser.add_argument("--config", help="Configuration JSON file")
    parser.add_argument("--device", default="byd")
    parser.add_argument("--max-age-min", type=float, default=45.0)
    parser.add_argument("--arbitrage-h", type=float, default=24.0)
    parser.add_argument("--surplus-w", type=float, default=1000.0)
    parser.add_argument("--soc-tolerance-pct", type=float, default=10.0)
    parser.add_argument("--measured-soc", type=float, help="Measured SoC factor 0..1 (override)")
    parser.add_argument("--now", help="ISO time instead of the current time (tests)")
    args = parser.parse_args(argv)

    measured = args.measured_soc
    if args.url:
        try:
            solution = _get(args.url, "/v1/energy-management/optimization/solution")
            config = _get(args.url, "/v1/config")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            report = Report(failures=[f"Kein Plan abrufbar: {exc}"])
            print("\n".join(report.lines()))
            return report.exit_code
        if measured is None:
            measured = _latest(args.url, f"{args.device}-soc-factor")
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
        arbitrage_h=args.arbitrage_h,
        surplus_w=args.surplus_w,
        soc_tolerance_pct=args.soc_tolerance_pct,
        measured_soc_factor=measured,
    )
    print("\n".join(report.lines()))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
