"""Learned load profiles of home appliances per program (RL29 fork).

A washing machine draws very different power depending on the program
(60 degC cotton heats for 20 minutes at 4 kW, a wool program barely heats).
The controller reports two measurements:

- ``<id>-power``: the measured power of the appliance [W],
- ``<id>-program``: a numeric code of the selected program (any stable
  number per program, e.g. its index in the program list).

From these, finished runs are detected (power above a threshold, gaps
shorter than ``end_gap_seconds`` belong to the same run) and resampled to
``PROFILE_INTERVAL_SECONDS``. Each program keeps a profile that is updated
with every run (online learning): the first runs are averaged, later runs
are blended in with the learning rate (exponentially weighted moving
average), so the profile follows changes of the machine and the load habits
while single outliers have limited influence. Profiles are aligned at the
start of the run; the length follows the same moving average.

The store is a small JSON file in the EOS data folder.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

PROFILE_INTERVAL_SECONDS = 300


@dataclass
class DetectedRun:
    start: datetime
    end: datetime
    program: Optional[str]
    power_w: list[float]
    """Mean power per PROFILE_INTERVAL_SECONDS from the start of the run."""

    @property
    def energy_wh(self) -> float:
        return sum(self.power_w) * PROFILE_INTERVAL_SECONDS / 3600.0


def _as_utc(value: Any) -> datetime:
    """Timezone-aware UTC datetime from datetime, pandas Timestamp or ISO string."""
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError(f"Not a datetime: {value!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    result = value.astimezone(timezone.utc)
    return datetime(
        result.year,
        result.month,
        result.day,
        result.hour,
        result.minute,
        result.second,
        result.microsecond,
        tzinfo=timezone.utc,
    )


def _clean(samples: Sequence[tuple[datetime, Optional[float]]]) -> list[tuple[datetime, float]]:
    result = [
        (_as_utc(t), float(v))
        for t, v in samples
        if v is not None and isinstance(v, (int, float)) and math.isfinite(v)
    ]
    result.sort(key=lambda item: item[0])
    return result


def _mean_power(samples: list[tuple[datetime, float]], start: datetime, end: datetime) -> float:
    """Time-weighted mean of a step signal between start and end."""
    total = 0.0
    covered = 0.0
    for index, (t, value) in enumerate(samples):
        seg_start = max(t, start)
        seg_end = samples[index + 1][0] if index + 1 < len(samples) else end
        seg_end = min(seg_end, end)
        if seg_end > seg_start:
            seconds = (seg_end - seg_start).total_seconds()
            total += value * seconds
            covered += seconds
    return total / covered if covered > 0 else 0.0


def detect_runs(
    power: Sequence[tuple[datetime, Optional[float]]],
    program: Sequence[tuple[datetime, Optional[float]]] = (),
    *,
    now: datetime,
    threshold_w: float = 20.0,
    end_gap_seconds: float = 900.0,
    min_duration_seconds: float = 600.0,
    program_lookback_seconds: float = 6 * 3600.0,
) -> list[DetectedRun]:
    """Finished runs in a power signal; runs still in progress are left out."""
    samples = _clean(power)
    programs = _clean(program)
    now = _as_utc(now)
    if not samples:
        return []
    # Active segments of the step signal.
    segments: list[list[datetime]] = []
    for index, (t, value) in enumerate(samples):
        if value <= threshold_w:
            continue
        seg_end = samples[index + 1][0] if index + 1 < len(samples) else now
        if segments and (t - segments[-1][1]).total_seconds() < end_gap_seconds:
            segments[-1][1] = max(segments[-1][1], seg_end)
        else:
            segments.append([t, seg_end])
    runs = []
    for start, end in segments:
        if (end - start).total_seconds() < min_duration_seconds:
            continue
        if (now - end).total_seconds() < end_gap_seconds:
            continue  # may still be running
        steps = max(1, math.ceil((end - start).total_seconds() / PROFILE_INTERVAL_SECONDS))
        profile = [
            round(
                _mean_power(
                    samples,
                    start + timedelta(seconds=i * PROFILE_INTERVAL_SECONDS),
                    min(end, start + timedelta(seconds=(i + 1) * PROFILE_INTERVAL_SECONDS)),
                ),
                1,
            )
            for i in range(steps)
        ]
        code: Optional[str] = None
        candidates = [
            (t, value)
            for t, value in programs
            if t <= start + timedelta(seconds=60)
            and (start - t).total_seconds() <= program_lookback_seconds
        ]
        if candidates:
            value = candidates[-1][1]
            code = str(int(value)) if float(value).is_integer() else str(value)
        runs.append(DetectedRun(start=start, end=end, program=code, power_w=profile))
    return runs


def blend_profiles(old: list[float], new: list[float], weight_new: float) -> list[float]:
    """Moving average of two start-aligned profiles, length averaged as well."""
    weight_new = min(1.0, max(0.0, weight_new))
    length = round((1.0 - weight_new) * len(old) + weight_new * len(new))
    length = max(1, length)
    result = []
    for i in range(max(len(old), len(new))):
        a = old[i] if i < len(old) else 0.0
        b = new[i] if i < len(new) else 0.0
        result.append(round((1.0 - weight_new) * a + weight_new * b, 1))
    return result[:length]


class ApplianceProfileStore:
    """Learned profiles per device and program, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"devices": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {"devices": {}}
        self.data.setdefault("devices", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _device(self, device_id: str) -> dict[str, Any]:
        return self.data["devices"].setdefault(device_id, {"programs": {}, "processed_until": None})

    def processed_until(self, device_id: str) -> Optional[datetime]:
        value = self._device(device_id).get("processed_until")
        return datetime.fromisoformat(value) if value else None

    def profile(self, device_id: str, program: Optional[str]) -> Optional[dict[str, Any]]:
        if program is None:
            return None
        return self._device(device_id)["programs"].get(program)

    def learn(
        self,
        device_id: str,
        runs: Sequence[DetectedRun],
        learning_rate: float = 0.3,
        processed_until: Optional[datetime] = None,
    ) -> int:
        """Add finished runs; returns the number of runs learned."""
        device = self._device(device_id)
        last = self.processed_until(device_id)
        learned = 0
        for run in sorted(runs, key=lambda r: r.start):
            if last is not None and run.start <= last:
                continue  # already learned
            if run.program is None:
                continue
            entry = device["programs"].get(run.program)
            if entry is None:
                entry = {"power_w": run.power_w, "runs": 1}
            else:
                n = int(entry.get("runs", 1))
                weight = max(1.0 / (n + 1), learning_rate)
                entry = {
                    "power_w": blend_profiles(entry["power_w"], run.power_w, weight),
                    "runs": n + 1,
                }
            entry["interval_seconds"] = PROFILE_INTERVAL_SECONDS
            entry["energy_wh"] = round(sum(entry["power_w"]) * PROFILE_INTERVAL_SECONDS / 3600.0)
            entry["last_run"] = run.start.isoformat()
            device["programs"][run.program] = entry
            last = run.start
            learned += 1
        marker = processed_until or last
        if marker is not None:
            device["processed_until"] = _as_utc(marker).isoformat()
        return learned
