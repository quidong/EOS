"""Comfort learning from manual interventions (free of Home Assistant imports).

Every manual intervention says "too cold here, now": raising a room's setpoint by hand,
or setting the lock of the reduction. The integration records the room temperature at
that moment, per room and period of the day. After a few such events it suggests a
lower limit for that room and period. Nothing is changed automatically: the suggestion
is published, the user decides.

A lock of the reduction concerns the whole house; it is attributed to the room that was
closest to (or furthest below) its lower limit at that moment.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

# Periods of the day (local time): name -> (start hour, end hour), end exclusive.
PERIODS: dict[str, tuple[int, int]] = {
    "night": (22, 6),
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
}

KIND_SETPOINT = "setpoint"
KIND_LOCK = "lock"


def period_of(moment: datetime) -> str:
    hour = moment.hour
    for name, (start, end) in PERIODS.items():
        if start < end and start <= hour < end:
            return name
        if start > end and (hour >= start or hour < end):
            return name
    return "night"


@dataclass
class ComfortEvent:
    time: datetime
    room: str
    kind: str
    temperature_c: float
    lower_c: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat(timespec="minutes"),
            "room": self.room,
            "kind": self.kind,
            "temperature_c": round(self.temperature_c, 2),
            "lower_c": round(self.lower_c, 2),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Optional["ComfortEvent"]:
        try:
            return cls(
                time=datetime.fromisoformat(str(data["time"])),
                room=str(data["room"]),
                kind=str(data.get("kind", KIND_SETPOINT)),
                temperature_c=float(data["temperature_c"]),
                lower_c=float(data["lower_c"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class Suggestion:
    room: str
    period: str
    lower_c: float
    suggested_c: float
    events: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "room": self.room,
            "period": self.period,
            "lower_c": self.lower_c,
            "suggested_c": self.suggested_c,
            "events": self.events,
        }


@dataclass
class ComfortLearning:
    """Recorded interventions and the suggestions derived from them."""

    events: list[ComfortEvent] = field(default_factory=list)
    keep_days: int = 90
    min_events: int = 3
    margin_k: float = 0.3
    """Suggested limit = median room temperature at the interventions + margin."""
    max_raise_k: float = 2.0

    # -- recording ---------------------------------------------------------

    def record(self, event: ComfortEvent) -> None:
        self.events.append(event)
        self.prune(event.time)

    def prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.keep_days)
        self.events = [e for e in self.events if e.time >= cutoff]

    # -- suggestions -------------------------------------------------------

    def suggestions(self, current_lower: dict[str, float]) -> list[Suggestion]:
        """Suggested lower limits per room and period, only where they are higher."""
        groups: dict[tuple[str, str], list[float]] = {}
        for event in self.events:
            groups.setdefault((event.room, period_of(event.time)), []).append(event.temperature_c)
        result = []
        for (room, period), temps in sorted(groups.items()):
            if len(temps) < self.min_events or room not in current_lower:
                continue
            lower = current_lower[room]
            wanted = statistics.median(temps) + self.margin_k
            suggested = round(min(wanted, lower + self.max_raise_k) * 2) / 2  # 0.5 K steps
            if suggested > lower:
                result.append(Suggestion(room, period, lower, suggested, len(temps)))
        return result

    # -- storage -----------------------------------------------------------

    def as_list(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.events]

    @classmethod
    def from_list(cls, data: Optional[list[dict[str, Any]]]) -> "ComfortLearning":
        learning = cls()
        for item in data or []:
            event = ComfortEvent.from_dict(item) if isinstance(item, dict) else None
            if event is not None:
                learning.events.append(event)
        return learning


def setpoint_raised(
    previous: Optional[float], current: Optional[float], min_step_k: float = 0.5
) -> bool:
    """A setpoint went up by at least ``min_step_k``."""
    return previous is not None and current is not None and current - previous >= min_step_k - 1e-9


def lock_set(previous: Optional[datetime], current: Optional[datetime], now: datetime) -> bool:
    """The reduction lock was set (or extended) to a time in the future."""
    if current is None or current <= now:
        return False
    return previous is None or current > previous + timedelta(minutes=1)


def lock_room(
    rooms: list[tuple[str, Optional[float], float]],
) -> Optional[tuple[str, float, float]]:
    """Room a house-wide lock is attributed to: smallest margin above its lower limit.

    ``rooms`` holds (name, temperature, lower limit); rooms without a value are skipped.
    """
    valid = [(n, t, low) for n, t, low in rooms if t is not None]
    if not valid:
        return None
    name, temp, lower = min(valid, key=lambda r: r[1] - r[2])
    return name, temp, lower
