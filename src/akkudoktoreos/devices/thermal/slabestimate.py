"""Screed temperature carried from plan to plan (RL29 fork).

Most installations have no screed sensor. Without one every run used to start
from the steady state of the measured room, so heat stored by a PREHEAT or
FORCED block vanished from the model at the next run. The estimate here carries
the screed of the previous plan to the current time and corrects it with the
heat pump's return temperature while it heats the floor:

``mean water = (flow + return) / 2`` (or ``return + spread / 2``) and
``screed = mean water - space heat / emitter_w_per_k`` (``BuildingModel``).

The module has no EOS dependencies.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def planned_value_at(
    rows: Sequence[Any],
    start_ts: float,
    slot_seconds: float,
    observation_ts: float,
) -> Optional[float]:
    """State of a plan row series at ``observation_ts``, interpolated within the slot.

    ``rows[i]`` is the state at the end of slot ``i`` (``start_ts + (i + 1) *
    slot_seconds``); before the end of the first slot the first value is used.
    None when the plan does not cover the time.
    """
    if slot_seconds <= 0 or not rows:
        return None
    position = (observation_ts - start_ts) / slot_seconds  # slots since the plan start
    if position < 0 or position > len(rows):
        return None
    end = _finite(rows[min(int(position), len(rows) - 1)])
    if end is None:
        return None
    index = int(position)
    if index < 1:
        return end
    before = _finite(rows[index - 1])
    if before is None or index >= len(rows):
        return before if before is not None else end
    return before + (position - index) * (end - before)


def slab_from_return_c(
    return_c: Optional[float],
    space_heat_w: Optional[float],
    emitter_w_per_k: float,
    flow_spread_k: float,
    flow_c: Optional[float] = None,
    min_heat_w: float = 1000.0,
) -> Optional[float]:
    """Screed temperature the measured return implies; None while the floor is not heated.

    Args:
        return_c: Measured return temperature [degC].
        space_heat_w: Measured space heat (heat output minus hot water) [W].
        emitter_w_per_k: Emitter conductance water - screed [W/K].
        flow_spread_k: Flow - return spread of the model [K]; used without a flow value.
        flow_c: Measured flow temperature [degC], optional.
        min_heat_w: Below this space heat the return says little about the screed.
    """
    if return_c is None or space_heat_w is None or emitter_w_per_k <= 0:
        return None
    if space_heat_w < min_heat_w:
        return None
    if flow_c is not None and flow_c >= return_c:
        mean = (flow_c + return_c) / 2.0
    else:
        mean = return_c + flow_spread_k / 2.0
    return mean - space_heat_w / emitter_w_per_k


def estimate_slabs(
    carried_c: Optional[float],
    carried_zones_c: Optional[Mapping[str, float]],
    measured_c: Optional[float],
    gain: float,
    room_c: Optional[float] = None,
    max_c: Optional[float] = None,
) -> tuple[Optional[float], Optional[dict[str, float]]]:
    """Combine the carried screed and the return-based value; (house, per zone).

    The correction ``gain * (measured - carried)`` moves the house screed and every
    zone screed alike. Without a carried value the measured one is taken; the result
    stays between the room temperature and ``max_c``.
    """
    gain = min(max(gain, 0.0), 1.0)
    house = carried_c
    delta = 0.0
    if measured_c is not None:
        if house is None:
            house = measured_c
        else:
            delta = gain * (measured_c - house)
            house += delta

    def bounded(value: float) -> float:
        if room_c is not None:
            value = max(value, room_c - 0.5)
        if max_c is not None:
            value = min(value, max_c)
        return value

    zones = (
        {zone: bounded(value + delta) for zone, value in carried_zones_c.items()}
        if carried_zones_c
        else None
    )
    return (bounded(house) if house is not None else None), zones
