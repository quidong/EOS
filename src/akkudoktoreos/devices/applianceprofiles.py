"""Learned load profiles of home appliances per program (eos-plus fork).

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
while single outliers have limited influence.

Profiles are aligned at the start of the run, because the heating phase of a
washing machine always sits at the beginning while the total duration varies
with the load (automatic load sensing). Three details keep the planning
profile honest:

- When two profiles of different length are blended, the shorter one is
  continued with the mean power of its own tail, not with zeros: zero padding
  would drain the energy of the rinse and spin phases with every run.
- The planning length is an upper quantile of the recent run durations, not
  their mean. A plan built on the mean duration would finish late every
  second run and miss the deadline given by the controller.
- Once a program has a few runs, a run whose energy or duration deviates far
  from the profile is rejected as an outlier (an EOS restart in the middle of
  a run, a wrong program code). After several rejections in a row the program
  is taken to have changed and its profile starts over with the next run.

The store is a small JSON file in the EOS data folder.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

PROFILE_INTERVAL_SECONDS = 300

HISTORY_RUNS = 20
"""Number of recent run durations and energies kept per program."""

OUTLIER_MIN_RUNS = 3
"""Outlier rejection starts once a program has this many learned runs."""

OUTLIER_ACCEPT_AFTER = 3
"""After this many rejected runs in a row the next run is learned anyway."""

TAIL_FRACTION = 0.2
"""Share of a profile (from its end) whose mean power continues a shorter profile."""


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

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


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
    window_start: Optional[datetime] = None,
) -> list[DetectedRun]:
    """Finished runs in a power signal; runs still in progress are left out.

    Args:
        window_start: Start of the measurement window the samples were read
            from. A run that starts within ``end_gap_seconds`` of it may have
            begun before the window (nothing is known about its beginning) and
            is left out.
    """
    samples = _clean(power)
    programs = _clean(program)
    now = _as_utc(now)
    edge = _as_utc(window_start) + timedelta(seconds=end_gap_seconds) if window_start else None
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
        if edge is not None and start < edge:
            continue  # may have started before the window
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


def tail_mean(profile: Sequence[float], fraction: float = TAIL_FRACTION) -> float:
    """Mean power of the last ``fraction`` of a profile (at least one step)."""
    if not profile:
        return 0.0
    steps = max(1, int(round(len(profile) * fraction)))
    tail = profile[-steps:]
    return sum(tail) / len(tail)


def extend_profile(profile: Sequence[float], length: int) -> list[float]:
    """Cut or continue a profile to ``length`` steps; continued with its tail mean."""
    length = max(1, int(length))
    result = [float(v) for v in profile[:length]]
    if len(result) < length:
        result += [round(tail_mean(profile), 1)] * (length - len(result))
    return result


def blend_profiles(
    old: Sequence[float],
    new: Sequence[float],
    weight_new: float,
    length: Optional[int] = None,
) -> list[float]:
    """Moving average of two start-aligned profiles.

    The shorter profile is continued with the mean power of its own tail, so
    that the rinse and spin phases of a longer run are averaged with realistic
    power instead of zeros. ``length`` is the length of the result; by default
    the weighted mean of both lengths.
    """
    weight_new = min(1.0, max(0.0, weight_new))
    if length is None:
        length = round((1.0 - weight_new) * len(old) + weight_new * len(new))
    length = max(1, int(length))
    span = max(len(old), len(new), length)
    a = extend_profile(old, span)
    b = extend_profile(new, span)
    result = [round((1.0 - weight_new) * x + weight_new * y, 1) for x, y in zip(a, b)]
    return result[:length]


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of ``values`` (0 <= q <= 1)."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        raise ValueError("quantile of empty sequence")
    q = min(1.0, max(0.0, q))
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def planning_length(durations_s: Sequence[float], duration_quantile: float) -> int:
    """Profile length [steps] for planning: an upper quantile of the run durations."""
    seconds = quantile(durations_s, duration_quantile)
    return max(1, math.ceil(seconds / PROFILE_INTERVAL_SECONDS - 1e-9))


@dataclass
class LearningResult:
    """Outcome of ``ApplianceProfileStore.learn``."""

    learned: int = 0
    rejected: int = 0
    rejected_runs: list[str] = field(default_factory=list)
    """Description of every rejected run, for the log."""
    restarted: list[str] = field(default_factory=list)
    """Programs whose profile was started over after repeated deviations."""

    def __bool__(self) -> bool:
        return self.learned > 0 or self.rejected > 0


class ApplianceProfileStore:
    """Learned profiles per device and program, persisted as JSON.

    A program entry holds:

    - ``power_w``: the planning profile (start aligned, planning length),
    - ``shape_w``: the blended profile at the blended run length,
    - ``durations_s`` / ``energies_wh``: the recent runs (``HISTORY_RUNS``),
    - ``runs``: learned runs, ``rejected``: rejected outliers,
    - ``energy_wh``, ``duration_s``: of the planning profile,
    - ``last_run``: start of the last learned run.
    """

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

    @staticmethod
    def is_outlier(entry: dict[str, Any], run: DetectedRun, tolerance: float) -> bool:
        """A run deviating more than ``tolerance`` (relative) in energy or duration."""
        if tolerance <= 0 or int(entry.get("runs", 0)) < OUTLIER_MIN_RUNS:
            return False
        if int(entry.get("rejected_in_row", 0)) >= OUTLIER_ACCEPT_AFTER:
            return False
        energies = entry.get("energies_wh") or []
        durations = entry.get("durations_s") or []
        if not energies or not durations:
            return False
        energy_ref = quantile(energies, 0.5)
        duration_ref = quantile(durations, 0.5)
        if energy_ref > 0 and abs(run.energy_wh - energy_ref) > tolerance * energy_ref:
            return True
        if duration_ref > 0 and abs(run.duration_seconds - duration_ref) > tolerance * duration_ref:
            return True
        return False

    def learn(
        self,
        device_id: str,
        runs: Sequence[DetectedRun],
        learning_rate: float = 0.3,
        processed_until: Optional[datetime] = None,
        duration_quantile: float = 0.8,
        outlier_tolerance: float = 0.4,
    ) -> LearningResult:
        """Add finished runs.

        Returns the number of learned and rejected runs (truthy when anything
        changed, so ``if store.learn(...): store.save()`` keeps working).
        """
        device = self._device(device_id)
        last = self.processed_until(device_id)
        result = LearningResult()
        for run in sorted(runs, key=lambda r: r.start):
            if last is not None and run.start <= last:
                continue  # already learned
            if run.program is None:
                continue
            entry = device["programs"].get(run.program)
            if entry is not None and self.is_outlier(entry, run, outlier_tolerance):
                entry["rejected"] = int(entry.get("rejected", 0)) + 1
                entry["rejected_in_row"] = int(entry.get("rejected_in_row", 0)) + 1
                entry["last_rejected"] = run.start.isoformat()
                result.rejected += 1
                result.rejected_runs.append(
                    f"{run.program}@{run.start.isoformat()} "
                    f"({run.energy_wh:.0f} Wh, {run.duration_seconds / 60:.0f} min)"
                )
                last = run.start
                continue
            if entry is not None and int(entry.get("rejected_in_row", 0)) >= OUTLIER_ACCEPT_AFTER:
                # Several deviating runs in a row: the program has changed (new
                # settings, other machine). Start the profile over with this run.
                entry = {"rejected": int(entry.get("rejected", 0))}
                result.restarted.append(run.program)
            if entry is None or "shape_w" not in entry:
                entry = {
                    **(entry or {}),
                    "shape_w": list(run.power_w),
                    "runs": 1,
                    "durations_s": [run.duration_seconds],
                    "energies_wh": [round(run.energy_wh)],
                }
            else:
                n = int(entry.get("runs", 1))
                weight = max(1.0 / (n + 1), learning_rate)
                shape = entry.get("shape_w") or entry.get("power_w") or []
                entry = {
                    **entry,
                    "shape_w": blend_profiles(shape, run.power_w, weight),
                    "runs": n + 1,
                    "durations_s": (entry.get("durations_s") or [])[-(HISTORY_RUNS - 1) :]
                    + [run.duration_seconds],
                    "energies_wh": (entry.get("energies_wh") or [])[-(HISTORY_RUNS - 1) :]
                    + [round(run.energy_wh)],
                }
            entry["rejected_in_row"] = 0
            entry["interval_seconds"] = PROFILE_INTERVAL_SECONDS
            length = planning_length(entry["durations_s"], duration_quantile)
            entry["power_w"] = extend_profile(entry["shape_w"], length)
            entry["duration_s"] = length * PROFILE_INTERVAL_SECONDS
            entry["energy_wh"] = round(sum(entry["power_w"]) * PROFILE_INTERVAL_SECONDS / 3600.0)
            entry["last_run"] = run.start.isoformat()
            device["programs"][run.program] = entry
            last = run.start
            result.learned += 1
        marker = processed_until or last
        if marker is not None:
            device["processed_until"] = _as_utc(marker).isoformat()
        return result
