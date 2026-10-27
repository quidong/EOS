"""Event-driven re-planning (eos-plus fork).

With ``ems.run_on_slot_start`` EOS plans at every slot start. In between, the plan can
become wrong long before the next slot: the battery is emptier than planned (an
unforeseen load), a room cools faster than the model expected, or the day-ahead
prices for tomorrow have just been published. ``ems.replan_on_deviation`` starts an
extra run in these cases, at most every ``replan_min_interval_sec`` seconds.

Checked on every poll (``ems.interval``):

- battery SoC: measured vs. planned for this moment, more than
  ``replan_soc_deviation_pct`` percentage points apart,
- heat pump room and hot water temperature: more than ``replan_temperature_deviation_k``
  apart from the planned value of the current slot,

New day-ahead prices need no trigger: prices are fetched in every run, and with
``run_on_slot_start`` that is at most one slot after their publication.
"""

from __future__ import annotations

import math
from typing import Any, Optional


def compare(
    planned: dict[str, Optional[float]],
    measured: dict[str, Optional[float]],
    thresholds: dict[str, float],
) -> list[str]:
    """Names (with figures) of the quantities whose deviation exceeds its threshold."""
    reasons = []
    for name, limit in thresholds.items():
        plan = planned.get(name)
        actual = measured.get(name)
        if plan is None or actual is None or not math.isfinite(plan) or not math.isfinite(actual):
            continue
        if abs(actual - plan) > limit:
            reasons.append(f"{name}: measured {actual:.1f}, planned {plan:.1f}")
    return reasons


def _plan_value(frame: Any, column: str, moment_ts: float) -> Optional[float]:
    """Value of ``column`` in the slot that contains ``moment_ts``."""
    if frame is None or column not in frame.columns or len(frame) == 0:
        return None
    position = None
    for i, stamp in enumerate(frame.index):
        if stamp.timestamp() <= moment_ts:
            position = i
    if position is None:
        return None
    try:
        value = float(frame[column].iloc[position])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


async def _latest(measurement: Any, key: str, now: Any, max_age_s: float) -> Optional[float]:
    try:
        dates, values = await measurement.key_to_lists(
            key=key, start_datetime=now.subtract(seconds=max_age_s), end_datetime=now.add(seconds=1)
        )
    except Exception:
        return None
    if not values or values[-1] is None:
        return None
    value = float(values[-1])
    return value if math.isfinite(value) else None


async def deviation_reasons(ems: Any, now: Any) -> list[str]:
    """Why the current plan should be replaced now; empty list = keep it."""
    from akkudoktoreos.core.coreabc import get_measurement

    config = ems.config
    settings = config.ems
    solution = ems.optimization_solution()
    reasons: list[str] = []

    if solution is None:
        return reasons
    frame = solution.solution.to_dataframe()
    measurement = get_measurement()
    moment = now.timestamp()
    planned: dict[str, Optional[float]] = {}
    measured: dict[str, Optional[float]] = {}
    thresholds: dict[str, float] = {}
    for battery in (config.devices.batteries or {}).values():
        name = f"{battery.device_id} SoC %"
        value = _plan_value(frame, f"{battery.device_id}_soc_factor", moment)
        planned[name] = value * 100 if value is not None else None
        actual = await _latest(measurement, battery.measurement_key_soc_factor, now, 600)
        measured[name] = actual * 100 if actual is not None else None
        thresholds[name] = float(settings.replan_soc_deviation_pct)
    for heat_pump in (config.devices.heat_pumps or {}).values():
        hp = heat_pump.device_id
        for label, column, key in (
            ("room", f"{hp}_room_temperature_c", heat_pump.measurement_key_room_temperature),
            ("hot water", f"{hp}_dhw_temperature_c", heat_pump.measurement_key_dhw_temperature),
        ):
            name = f"{hp} {label} °C"
            planned[name] = _plan_value(frame, column, moment)
            measured[name] = await _latest(measurement, key, now, 1800)
            limit = float(settings.replan_temperature_deviation_k)
            # Hot water swings with every draw; only a larger drop matters.
            thresholds[name] = limit if label == "room" else 4 * limit
    reasons.extend(compare(planned, measured, thresholds))
    return reasons
