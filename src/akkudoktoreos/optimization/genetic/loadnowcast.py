"""Base load nowcast: anchor the first slots of the load forecast to the measurement (RL29 fork).

The load forecast is a statistical profile; the house right now may run well above or
below it (cooking, a guest, nobody home). The measured base load of the last completed
slot replaces the forecast of the current slot and decays linearly into the forecast
over ``decay_slots`` slots, as evcc does for its home profile::

    w_i      = (decay_slots - i) / decay_slots          i = 0 .. decay_slots-1
    load_i'  = w_i * measured + (1 - w_i) * load_i

The base load is the house meter (``measurement.load_emr_keys``) minus the consumers EOS
plans itself (heat pump, heating rod, washing machine, ...), the same definition LoadML
trains on; otherwise their energy would be counted twice.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from loguru import logger

from akkudoktoreos.prediction.loadml import hold_hourly_means
from akkudoktoreos.utils.datetimeutil import DateTime, to_duration


def blend_measured(
    values: Sequence[float], first: int, measured_wh: float, decay_slots: int
) -> list[float]:
    """Blend a measured slot energy into the forecast from slot ``first`` on.

    Args:
        values: Forecast energy per slot [Wh].
        first: Index of the current slot.
        measured_wh: Measured energy of the last completed slot [Wh].
        decay_slots: Slots over which the measurement decays into the forecast; 0 = off.

    Returns:
        A new list; slots before ``first`` and from ``first + decay_slots`` on are unchanged.
    """
    result = [float(v) for v in values]
    if decay_slots <= 0 or not math.isfinite(measured_wh) or measured_wh < 0:
        return result
    for i in range(decay_slots):
        index = first + i
        if index < 0 or index >= len(result):
            continue
        weight = (decay_slots - i) / decay_slots
        result[index] = weight * measured_wh + (1.0 - weight) * result[index]
    return result


async def measured_base_load_wh(
    measurement: Any,
    slot_start: DateTime,
    interval_s: int,
    subtract_keys: Sequence[str],
    max_hold_s: float,
) -> Optional[float]:
    """Base load energy of the slot that ended at ``slot_start`` [Wh].

    Args:
        measurement: The measurement store (``load_total_kwh``, ``key_to_lists``).
        slot_start: Start of the current slot (end of the measured slot).
        interval_s: Slot length [s].
        subtract_keys: Power measurements [W] of consumers EOS plans itself.
        max_hold_s: A power value holds until the next sample, at most this long.

    Returns:
        The energy, or None when the meter or a subtracted consumer has no usable value
        for the whole slot (then the forecast stays as it is).
    """
    start = slot_start.subtract(seconds=interval_s)
    try:
        kwh = await measurement.load_total_kwh(
            start_datetime=start, end_datetime=slot_start, interval=to_duration(interval_s)
        )
    except Exception as exc:  # no meter configured or no readings
        logger.debug("Load nowcast: no meter energy ({}).", exc)
        return None
    if len(kwh) < 1 or not math.isfinite(float(kwh[0])) or float(kwh[0]) <= 0.0:
        return None
    energy_wh = float(kwh[0]) * 1000.0
    for key in subtract_keys:
        try:
            dates, values = await measurement.key_to_lists(
                key=key,
                start_datetime=start.subtract(seconds=max_hold_s),
                end_datetime=slot_start,
                dropna=True,
            )
        except (KeyError, ValueError):
            return None
        samples = [
            (d.timestamp(), None if v is None else float(v)) for d, v in zip(dates, values)
        ]
        means = hold_hourly_means(samples, [start.timestamp()], max_hold_s, float(interval_s))
        if not means or means[0] is None:
            return None
        energy_wh -= float(means[0]) * interval_s / 3600.0
    if energy_wh < 0.0:
        # A consumer meter ahead of the house meter: no usable base load.
        return None
    return energy_wh
