"""Calendar events from the EOS plan (no Home Assistant imports).

Consecutive slots with the same notable state become one event:

- heat pump: raised heating, preheating, forced heating, standby, hot water charges
  (heat pump or heating rod);
- home appliances: planned runs;
- batteries and electric vehicles: charging from the grid, battery export.

Plain NORMAL and REDUCED slots are no events: they are the everyday case.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

TEXTS = {
    "de": {
        "raised": "Wärmepumpe: Heizen angehoben",
        "preheat": "Wärmepumpe: Vorheizen",
        "forced": "Wärmepumpe: Heizen forciert",
        "cool": "Wärmepumpe: Kühlen",
        "standby": "Wärmepumpe: Standby",
        "dhw": "Warmwasser laden (Wärmepumpe)",
        "rod": "Warmwasser laden (Heizstab)",
        "run": "{device} läuft",
        "grid_charge": "{device} lädt aus dem Netz",
        "export": "{device} speist ein",
    },
    "en": {
        "raised": "Heat pump: raised heating",
        "preheat": "Heat pump: preheating",
        "forced": "Heat pump: forced heating",
        "cool": "Heat pump: cooling",
        "standby": "Heat pump: standby",
        "dhw": "Hot water charge (heat pump)",
        "rod": "Hot water charge (heating rod)",
        "run": "{device} runs",
        "grid_charge": "{device} charges from the grid",
        "export": "{device} exports",
    },
}

HEATING = {2: "raised", 3: "preheat", 4: "forced", 5: "cool"}
GRID_CHARGE_MODES = {"GRID_SUPPORT_IMPORT", "FORCED_CHARGE"}
EXPORT_MODES = {"GRID_SUPPORT_EXPORT", "FORCED_DISCHARGE"}


@dataclass(frozen=True)
class PlanEvent:
    start: datetime
    end: datetime
    summary: str
    uid: str


def _time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _runs(
    rows: list[dict[str, Any]],
    slot: timedelta,
    label: Callable[[dict[str, Any]], Optional[str]],
) -> list[tuple[datetime, datetime, str]]:
    """Consecutive slots with the same label (None = no event)."""
    events: list[tuple[datetime, datetime, str]] = []
    current: Optional[tuple[datetime, datetime, str]] = None
    for row in rows:
        start = _time(row.get("t"))
        if start is None:
            continue
        name = label(row)
        if current is not None and (name != current[2] or start != current[1]):
            events.append(current)
            current = None
        if name is None:
            continue
        current = (current[0] if current else start, start + slot, name)
    if current is not None:
        events.append(current)
    return events


def _heating(row: dict[str, Any]) -> Optional[str]:
    if (row.get("standby") or 0) >= 0.5:
        return "standby"
    mode = row.get("heating_mode")
    return HEATING.get(int(round(mode))) if isinstance(mode, (int, float)) else None


def _dhw(row: dict[str, Any]) -> Optional[str]:
    mode = row.get("dhw_mode")
    if not isinstance(mode, (int, float)) or mode < 0.5:
        return None
    return "rod" if mode >= 1.5 else "dhw"


def plan_events(
    plan_rows: list[dict[str, Any]],
    device_rows: Iterable[tuple[str, str, list[dict[str, Any]]]],
    slot_minutes: int,
    language: str = "en",
) -> list[PlanEvent]:
    """Events of the heat pump plan and of the devices ``(device_id, kind, rows)``."""
    texts = TEXTS.get(language[:2], TEXTS["en"])
    slot = timedelta(minutes=slot_minutes)
    events: list[PlanEvent] = []

    def add(found: list[tuple[datetime, datetime, str]], prefix: str, device: str = "") -> None:
        for start, end, key in found:
            events.append(
                PlanEvent(
                    start=start,
                    end=end,
                    summary=texts[key].format(device=device),
                    uid=f"{prefix}-{key}-{start.isoformat()}",
                )
            )

    add(_runs(plan_rows, slot, _heating), "hp")
    add(_runs(plan_rows, slot, _dhw), "hp")
    for device_id, kind, rows in device_rows:
        if kind == "home_appliance":
            add(
                _runs(rows, slot, lambda r: "run" if r.get("running") else None),
                device_id,
                device_id,
            )
        else:

            def battery(row: dict[str, Any]) -> Optional[str]:
                mode = row.get("mode")
                if mode in GRID_CHARGE_MODES:
                    return "grid_charge"
                if mode in EXPORT_MODES:
                    return "export"
                return None

            add(_runs(rows, slot, battery), device_id, device_id)
    events.sort(key=lambda e: (e.start, e.summary))
    return events
