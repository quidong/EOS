"""Daily airing recommendation that stays put between runs (RL29 fork).

``ventilation.recommend`` marks one slot per day in the midday window
(``<device_id>_ventilate`` = 1) for a notification. Every run looks only at the
slots from now on, so without a memory each run would pick a new slot: in the
afternoon the warmest remaining slot is usually the current one, and the flag
reappears in every new plan - one notification per run.

This store remembers the slot of each day:

- a run records the slot it marked (``record``),
- once that slot has started, it is final for the day: later runs mark it
  while it is in the horizon and recommend nothing else that day,
- a slot that has not started yet may still move with a newer forecast,
- a day on which ``recommend_skip_aired_share`` of the windows (default 70 %)
  were already open in the window gets no recommendation any more.

The memory lives in ``<data folder>/rl29/heatpump_ventilation.json``; losing it
only means the next run picks the slot afresh.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from loguru import logger

KEEP_DAYS = 7
STORE_NAME = "heatpump_ventilation.json"


def store_path(data_folder: Path) -> Path:
    return Path(data_folder) / "rl29" / STORE_NAME


def _as_aware(value: Any) -> datetime:
    """Plain datetime in its own (local) timezone; days are local days."""
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime expected")
    return datetime.fromtimestamp(value.timestamp(), tz=value.tzinfo)


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class VentilationStore:
    """Recommended airing slot per device and local day."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"devices": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("devices"), dict):
                    self.data = loaded
            except (OSError, ValueError) as exc:
                logger.warning("Ventilation store {} unreadable, starting fresh: {}", path, exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def days(self, device_id: str) -> dict[str, str]:
        return self.data["devices"].setdefault(device_id, {})

    def slot(self, device_id: str, day: str) -> Optional[datetime]:
        return _parse(self.days(device_id).get(day))

    def record(
        self,
        device_id: str,
        issued_at: datetime,
        slot_starts: Sequence[datetime],
        ventilate: Sequence[float],
    ) -> None:
        """Remember the marked slot per day of the run.

        A slot that has started is final and never replaced. A day of the run
        without a marked slot drops its pending (not yet started) slot, so a slot
        that was never announced cannot turn final later.
        """
        days = self.days(device_id)
        marked: dict[str, datetime] = {}
        horizon: set[str] = set()
        for start, flag in zip(slot_starts, ventilate):
            day = start.date().isoformat()
            horizon.add(day)
            if flag is not None and float(flag) >= 0.5 and day not in marked:
                marked[day] = start
        for day in horizon:
            known = _parse(days.get(day))
            if known is not None and known.timestamp() <= issued_at.timestamp():
                continue  # final: already announced
            if day in marked:
                days[day] = marked[day].isoformat()
            else:
                days.pop(day, None)
        cutoff = (issued_at - timedelta(days=KEEP_DAYS)).date().isoformat()
        for day in [d for d in days if d < cutoff]:
            del days[day]


def fixed_days(
    store: Optional[VentilationStore],
    device_id: str,
    now: datetime,
    aired_today: bool,
) -> dict[str, Optional[str]]:
    """Days whose recommendation is decided: day -> slot start (ISO) or None (none).

    A recorded slot that has started is final. When the windows were already
    opened widely today, today gets no (further) recommendation unless its
    slot was already announced. Without a store (no data folder) only the
    airing counts.
    """
    fixed: dict[str, Optional[str]] = {}
    recorded = store.days(device_id) if store is not None else {}
    for day, value in recorded.items():
        start = _parse(value)
        if start is not None and start.timestamp() <= now.timestamp():
            fixed[day] = start.isoformat()
    today = now.date().isoformat()
    if aired_today and today not in fixed:
        fixed[today] = None
    return fixed


def aired_in_window(
    samples: Sequence[tuple[datetime, float]],
    day_start: datetime,
    window_start_hour: int,
    window_end_hour: int,
    now: datetime,
    min_share: float,
) -> bool:
    """True if at least ``min_share`` of the windows were open in today's window so far.

    ``samples`` are (time, open share) sorted or not; the value in force at the
    window start (last sample before it) counts too.
    """
    begin = day_start + timedelta(hours=window_start_hour)
    end = min(day_start + timedelta(hours=window_end_hour), now)
    if end <= begin:
        return False
    before = [(t, v) for t, v in samples if t.timestamp() <= begin.timestamp()]
    values = [v for t, v in samples if begin.timestamp() < t.timestamp() <= end.timestamp()]
    if before:
        values.append(max(before, key=lambda s: s[0].timestamp())[1])
    return any(v >= min_share - 1e-9 for v in values)


def record_ventilation(
    device_id: str,
    data_folder: Path,
    issued_at: datetime,
    slot_starts: Sequence[Any],
    ventilate: Sequence[float],
) -> None:
    """Record the marked slots of a run; never raises."""
    try:
        starts = [_as_aware(s) for s in slot_starts]
        store = VentilationStore(store_path(data_folder))
        store.record(device_id, _as_aware(issued_at), starts, list(ventilate))
        store.save()
    except Exception as exc:  # diagnostics-like memory, never part of the run
        logger.warning("Heat pump {}: could not record the airing slot: {}", device_id, exc)
