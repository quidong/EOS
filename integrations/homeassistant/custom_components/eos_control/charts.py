"""Plan and forecast series for charts (free of Home Assistant imports).

EOS publishes its plan and the forecasts it used per slot (usually 15 min). This module
turns them into compact rows for dashboards (e.g. apexcharts-card ``data_generator``):

- slot rows ``{"t": ISO, ...}`` with short, device-independent keys;
- hourly rows with the energy per hour in kWh (summed over the slots of the hour), so a
  plan can be compared with hourly meter statistics;
- a plan memory: for every slot the values of the last plan that contained it. A slot
  that has passed keeps what was planned for it, which gives "plan vs. actual" for the
  past without the recorder.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any, Optional

# EOS solution columns -> row keys
PLAN_COLUMNS: dict[str, str] = {
    "load_energy_wh": "load_wh",
    "grid_consumption_energy_wh": "import_wh",
    "grid_feedin_energy_wh": "export_wh",
    "costs_amt": "costs_amt",
    "revenue_amt": "revenue_amt",
}
# Heat pump columns, prefixed with the device id in EOS ("<id>_energy_wh")
DEVICE_COLUMNS: dict[str, str] = {
    "energy_wh": "heat_pump_wh",
    "rod_energy_wh": "rod_wh",
    "heating_mode": "heating_mode",
    "dhw_mode": "dhw_mode",
    "standby": "standby",
    "room_temperature_c": "room_c",
    "slab_temperature_c": "slab_c",
    "dhw_temperature_c": "dhw_c",
}
# State of charge of batteries / electric vehicles ("<id>_soc_factor" -> "<id>_soc_pct")
SOC_SUFFIX = "_soc_factor"
# EOS prediction columns -> row keys
FORECAST_COLUMNS: dict[str, str] = {
    "pvforecast_ac_energy_wh": "pv_wh",
    "loadforecast_energy_wh": "load_wh",
    "elec_price_amt_kwh": "price_amt_kwh",
    "feed_in_tariff_amt_kwh": "feed_in_amt_kwh",
    "weather_air_temp_celcius": "outdoor_c",
}

# Decimals per key ending; energies in Wh need no more than 0.1 Wh.
_DECIMALS = (("_amt_kwh", 4), ("_kwh", 3), ("_wh", 1), ("_amt", 4), ("_c", 2), ("_pct", 1))

DEFAULT_SLOT_MINUTES = 15
PAST_HOURS = 24
"""Hours of passed slots kept in the plan rows (plan vs. actual of the last day)."""
MEMORY_HOURS = 48


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN, inf
        return None
    return float(value)


def _round(key: str, value: float) -> float:
    for ending, decimals in _DECIMALS:
        if key.endswith(ending):
            return round(value, decimals)
    return round(value, 3)


def _frame(solution: Mapping[str, Any], part: str) -> Mapping[str, Any]:
    frame = solution.get(part)
    data = frame.get("data") if isinstance(frame, Mapping) else None
    return data if isinstance(data, Mapping) else {}


def _rows(frame: Mapping[str, Any], columns: Mapping[str, str], soc: bool = False) -> list[dict]:
    rows = []
    for stamp, values in frame.items():
        start = parse_time(stamp)
        if start is None or not isinstance(values, Mapping):
            continue
        row: dict[str, Any] = {"t": start.isoformat()}
        for column, key in columns.items():
            value = _number(values.get(column))
            if value is not None:
                row[key] = _round(key, value)
        if soc:
            for column, raw in values.items():
                value = _number(raw)
                if column.endswith(SOC_SUFFIX) and value is not None:
                    key = f"{column[: -len(SOC_SUFFIX)]}_soc_pct"
                    row[key] = _round(key, value * 100.0)
        rows.append(row)
    rows.sort(key=lambda r: parse_time(r["t"]) or datetime.min)
    return rows


def plan_rows(solution: Mapping[str, Any], device_id: str) -> list[dict]:
    """Slot rows of the plan: house values plus the heat pump ``device_id``."""
    columns = dict(PLAN_COLUMNS)
    columns.update({f"{device_id}_{column}": key for column, key in DEVICE_COLUMNS.items()})
    return _rows(_frame(solution, "solution"), columns, soc=True)


def forecast_rows(solution: Mapping[str, Any]) -> list[dict]:
    """Slot rows of the forecasts EOS planned with (PV, load, prices, outdoor temperature)."""
    return _rows(_frame(solution, "prediction"), FORECAST_COLUMNS)


def slot_minutes(rows: list[dict]) -> int:
    """Slot length from the first two rows."""
    if len(rows) >= 2:
        first, second = parse_time(rows[0]["t"]), parse_time(rows[1]["t"])
        if first is not None and second is not None and second > first:
            return int((second - first).total_seconds() // 60)
    return DEFAULT_SLOT_MINUTES


def _hour(start: datetime, tz: tzinfo) -> datetime:
    return start.astimezone(tz).replace(minute=0, second=0, microsecond=0)


def hourly(rows: Iterable[dict], tz: tzinfo, mean_keys: Iterable[str] = ()) -> list[dict]:
    """Hourly rows: energies (``*_wh``) summed as ``*_kwh``, ``mean_keys`` averaged.

    ``slots`` counts the slots of the hour; an hour at the start or end of the horizon
    may be incomplete.
    """
    means = set(mean_keys)
    groups: dict[datetime, list[dict]] = {}
    for row in rows:
        start = parse_time(row.get("t"))
        if start is not None:
            groups.setdefault(_hour(start, tz), []).append(row)
    result = []
    for hour in sorted(groups):
        members = groups[hour]
        out: dict[str, Any] = {"t": hour.isoformat(), "slots": len(members)}
        keys = {k for row in members for k in row if k.endswith("_wh") or k in means}
        for key in sorted(keys):
            values = [row[key] for row in members if isinstance(row.get(key), (int, float))]
            if not values:
                continue
            if key.endswith("_wh"):
                name = f"{key[:-3]}_kwh"
                out[name] = _round(name, sum(values) / 1000.0)
            else:
                out[key] = _round(key, sum(values) / len(values))
        result.append(out)
    return result


def current_row(rows: Iterable[dict], now: datetime) -> Optional[dict]:
    """Row of the slot that contains ``now`` (last slot starting at or before now)."""
    current = None
    for row in rows:
        start = parse_time(row.get("t"))
        if start is None:
            continue
        if start <= now:
            current = row
        else:
            break
    return current


def hour_energy_kwh(rows: Iterable[dict], now: datetime, tz: tzinfo, key: str) -> Optional[float]:
    """Planned energy of ``key`` (Wh per slot) in the hour that contains ``now``, in kWh."""
    hour = _hour(now, tz)
    values = []
    for row in rows:
        start = parse_time(row.get("t"))
        if (
            start is not None
            and _hour(start, tz) == hour
            and isinstance(row.get(key), (int, float))
        ):
            values.append(row[key])
    if not values:
        return None
    return round(sum(values) / 1000.0, 3)


@dataclass
class PlanMemory:
    """Slot rows of the latest plan that contained each slot."""

    rows: dict[str, dict] = field(default_factory=dict)
    keep_hours: float = MEMORY_HOURS
    generated_at: Optional[datetime] = None
    """Creation time of the latest plan taken (kept while EOS is unreachable)."""

    def update(self, new_rows: list[dict], now: datetime) -> None:
        """Take a new plan: its slots replace stored ones; later stored slots are dropped.

        Slots before the first slot of the new plan keep the values planned for them.
        """
        if new_rows:
            first = parse_time(new_rows[0]["t"])
            if first is not None:
                self.rows = {
                    t: r
                    for t, r in self.rows.items()
                    if (start := parse_time(t)) is not None and start < first
                }
            for row in new_rows:
                start = parse_time(row["t"])
                if start is not None:
                    self.rows[start.isoformat()] = row
        cutoff = now - timedelta(hours=self.keep_hours)
        self.rows = {
            t: r for t, r in self.rows.items() if (start := parse_time(t)) and start >= cutoff
        }

    def series(self, since: Optional[datetime] = None) -> list[dict]:
        """Rows sorted by time, from ``since`` on."""
        items: list[tuple[datetime, dict]] = []
        for stamp, row in self.rows.items():
            start = parse_time(stamp)
            if start is not None and (since is None or start >= since):
                items.append((start, row))
        return [row for _, row in sorted(items, key=lambda item: item[0])]

    def as_list(self) -> list[dict]:
        return self.series()

    @classmethod
    def from_list(cls, data: Optional[list[Any]]) -> "PlanMemory":
        memory = cls()
        for row in data or []:
            if isinstance(row, dict) and (start := parse_time(row.get("t"))) is not None:
                memory.rows[start.isoformat()] = row
        return memory


@dataclass
class Charts:
    """Everything the chart sensors publish, computed once per poll."""

    generated_at: Optional[datetime] = None
    slot_minutes: int = DEFAULT_SLOT_MINUTES
    plan: list[dict] = field(default_factory=list)
    """Slot rows: passed slots (last ``PAST_HOURS``) from the memory, then the plan."""
    plan_hourly: list[dict] = field(default_factory=list)
    forecast: list[dict] = field(default_factory=list)
    forecast_hourly: list[dict] = field(default_factory=list)
    load_hour_kwh: Optional[float] = None
    heat_pump_hour_kwh: Optional[float] = None
    room_c: Optional[float] = None
    dhw_c: Optional[float] = None


def build_charts(
    solution: Optional[Mapping[str, Any]],
    device_id: str,
    memory: PlanMemory,
    now: datetime,
    tz: tzinfo,
) -> Charts:
    """Update ``memory`` with the plan in ``solution`` and compute the chart series.

    Without a solution (EOS unreachable) the memory alone still gives the past and
    the rest of the last plan.
    """
    charts = Charts()
    new_rows: list[dict] = []
    if solution:
        generated = parse_time(solution.get("generated_at"))
        if generated is not None:
            memory.generated_at = generated
        new_rows = plan_rows(solution, device_id)
        charts.forecast = forecast_rows(solution)
        charts.forecast_hourly = hourly(
            charts.forecast, tz, mean_keys=("price_amt_kwh", "feed_in_amt_kwh", "outdoor_c")
        )
    memory.update(new_rows, now)
    # A short outage (EOS busy optimising) keeps the time of the last plan: consumers
    # judge the plan's age by it and must not see "no plan" for a minute.
    charts.generated_at = memory.generated_at
    rows = memory.series()
    if not rows:
        return charts
    charts.slot_minutes = slot_minutes(new_rows or rows)
    # The window starts relative to the first slot of the plan, not to now: the rows
    # only change with a new plan (no state write per poll).
    anchor = parse_time(new_rows[0]["t"]) if new_rows else parse_time(rows[0]["t"])
    since = anchor - timedelta(hours=PAST_HOURS) if anchor is not None else None
    charts.plan = memory.series(since)
    charts.plan_hourly = hourly(charts.plan, tz)
    charts.load_hour_kwh = hour_energy_kwh(rows, now, tz, "load_wh")
    charts.heat_pump_hour_kwh = hour_energy_kwh(rows, now, tz, "heat_pump_wh")
    row = current_row(rows, now)
    if row is not None:
        charts.room_c = row.get("room_c")
        charts.dhw_c = row.get("dhw_c")
    return charts
