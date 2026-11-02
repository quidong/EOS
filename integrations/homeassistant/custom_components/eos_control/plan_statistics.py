"""Planned values per hour as long-term statistics (builder without HA imports).

For every completed hour the plan memory holds the values of the last plan that
contained each slot. These go into the recorder as external statistics, so a
statistics graph shows plan and meter side by side for weeks (no attributes, no
custom card). Energies are kWh per hour, temperatures the mean of the slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any, Optional

from .charts import hourly, parse_time


@dataclass(frozen=True)
class PlanStatistic:
    key: str
    """Part of the statistic id (``eos_control:plan_<key>``)."""
    column: str
    """Key of the hourly row."""
    unit: str
    unit_class: Optional[str]
    heat_pump: bool
    name: str


PLAN_STATISTICS = (
    PlanStatistic("house_load", "load_kwh", "kWh", "energy", False, "Plan house load"),
    PlanStatistic("grid_import", "import_kwh", "kWh", "energy", False, "Plan grid import"),
    PlanStatistic("grid_export", "export_kwh", "kWh", "energy", False, "Plan grid export"),
    PlanStatistic("heat_pump_energy", "heat_pump_kwh", "kWh", "energy", True, "Plan heat pump"),
    PlanStatistic("room_temperature", "room_c", "°C", "temperature", True, "Plan room"),
    PlanStatistic("dhw_temperature", "dhw_c", "°C", "temperature", True, "Plan hot water"),
)


def statistic_id(domain: str, stat: PlanStatistic, heat_pump: str) -> str:
    device = f"{heat_pump}_" if stat.heat_pump and heat_pump else ""
    raw = f"plan_{device}{stat.key}".lower()
    safe = "".join(c if c.isalnum() else "_" for c in raw)
    return f"{domain}:{safe}"


def completed_hours(
    rows: list[dict[str, Any]],
    tz: tzinfo,
    slot_minutes: int,
    now: datetime,
    after: Optional[datetime],
) -> list[dict[str, Any]]:
    """Complete hours that ended before ``now`` and start after ``after``."""
    per_hour = max(1, 60 // max(1, slot_minutes))
    result = []
    for row in hourly(rows, tz, mean_keys=("room_c", "dhw_c")):
        start = parse_time(row["t"])
        if start is None or start + timedelta(hours=1) > now:
            continue
        if after is not None and start <= after:
            continue
        if row.get("slots", 0) < per_hour:
            continue
        result.append({**row, "start": start})
    return result
