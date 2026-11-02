"""State set by actions (no Home Assistant imports).

``eos_control.lock_reduction``, ``set_holiday`` and ``request_run`` keep their state in
the integration (stored), so no helper entities are needed. They combine with the
context entities of the options: a lock counts until the later of both, holiday is on
when the entity or the action says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass
class RunRequest:
    """A home appliance run requested by action: done by ``finish_by``."""

    finish_by: datetime
    duration_h: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {"finish_by": self.finish_by.isoformat(), "duration_h": self.duration_h}


@dataclass
class ManualState:
    lock_until: Optional[datetime] = None
    holiday_start: Optional[datetime] = None
    holiday_end: Optional[datetime] = None
    runs: dict[str, RunRequest] = field(default_factory=dict)
    ended_runs: set[str] = field(default_factory=set)
    """Runs that ended or were cancelled: EOS gets ``requested = 0`` once more."""

    # -- holiday and lock --------------------------------------------------

    def holiday_active(self, now: datetime) -> bool:
        if self.holiday_end is None or now >= self.holiday_end:
            return False
        return self.holiday_start is None or self.holiday_start <= now

    def lock_active(self, now: datetime) -> Optional[datetime]:
        return self.lock_until if self.lock_until is not None and self.lock_until > now else None

    def expire(self, now: datetime) -> bool:
        """Drop what is over; True = something changed (store)."""
        changed = False
        if self.holiday_end is not None and now >= self.holiday_end:
            self.holiday_start = self.holiday_end = None
            changed = True
        if self.lock_until is not None and self.lock_until <= now:
            self.lock_until = None
            changed = True
        for device_id in [d for d, run in self.runs.items() if run.finish_by <= now]:
            del self.runs[device_id]
            self.ended_runs.add(device_id)
            changed = True
        return changed

    # -- home appliance runs -----------------------------------------------

    def request(self, device_id: str, finish_by: datetime, duration_h: Optional[float]) -> None:
        self.runs[device_id] = RunRequest(finish_by, duration_h)
        self.ended_runs.discard(device_id)

    def cancel(self, device_id: str) -> None:
        if self.runs.pop(device_id, None) is not None:
            self.ended_runs.add(device_id)

    def run_measurements(self) -> dict[str, Optional[float]]:
        """EOS keys ``<id>-requested``, ``<id>-deadline``, ``<id>-duration``."""
        values: dict[str, Optional[float]] = {}
        for device_id in self.ended_runs:
            values[f"{device_id}-requested"] = 0.0
        for device_id, run in self.runs.items():
            values[f"{device_id}-requested"] = 1.0
            values[f"{device_id}-deadline"] = run.finish_by.timestamp()
            if run.duration_h is not None:
                values[f"{device_id}-duration"] = run.duration_h
        return values

    def sent(self) -> None:
        """The ``requested = 0`` of ended runs reached EOS."""
        self.ended_runs.clear()

    # -- storage -------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "lock_until": self.lock_until.isoformat() if self.lock_until else None,
            "holiday_start": self.holiday_start.isoformat() if self.holiday_start else None,
            "holiday_end": self.holiday_end.isoformat() if self.holiday_end else None,
            "runs": {d: r.as_dict() for d, r in self.runs.items()},
            "ended_runs": sorted(self.ended_runs),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ManualState":
        state = cls()
        if not isinstance(data, dict):
            return state
        state.lock_until = _parse(data.get("lock_until"))
        state.holiday_start = _parse(data.get("holiday_start"))
        state.holiday_end = _parse(data.get("holiday_end"))
        for device_id, run in (data.get("runs") or {}).items():
            finish_by = _parse(run.get("finish_by")) if isinstance(run, dict) else None
            if finish_by is not None:
                duration = run.get("duration_h")
                state.runs[str(device_id)] = RunRequest(
                    finish_by, float(duration) if isinstance(duration, (int, float)) else None
                )
        state.ended_runs = {str(d) for d in data.get("ended_runs") or []}
        return state
