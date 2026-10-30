"""Measurements sent from Home Assistant to EOS (no Home Assistant imports).

EOS keeps measurements under keys (``/v1/config/devices`` -> ``measurement_keys``,
energy meters under ``/v1/config/measurement`` -> ``*_emr_keys``). The integration
sends them itself (``PUT /v1/measurement/data``), so EOS needs no access to Home
Assistant and no template helpers are needed:

- values the integration computes for the heat pump (room mean and margins, humidity,
  share of open windows, holiday, return time, reduced setpoint, lock of the
  reduction) - see :data:`HEAT_PUMP_COMPUTED`;
- every other key from a Home Assistant entity chosen in the device's or EOS's
  settings.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Optional

# Heat pump keys ``<id>-<suffix>`` the integration computes itself
HEAT_PUMP_COMPUTED = (
    "room-temperature",
    "room-min-margin",
    "room-max-margin",
    "humidity",
    "window-open",
    "away",
    "return-ts",
    "away-room-temperature",
    "setback-room-temperature",
    "away-min-temperature",
    "reduce-lock-until-ts",
)
EMR_SECTIONS = (
    "load_emr_keys",
    "grid_import_emr_keys",
    "grid_export_emr_keys",
    "pv_production_emr_keys",
)

_TRUE = {"on", "true", "yes", "open", "home", "detected", "active"}
_FALSE = {"off", "false", "no", "closed", "not_home", "clear", "idle"}
_MISSING = {"unavailable", "unknown", "none", ""}


def computed_keys(device_id: str) -> set[str]:
    return {f"{device_id}-{suffix}" for suffix in HEAT_PUMP_COMPUTED}


def device_keys(all_keys: Iterable[str], device_id: str) -> list[str]:
    """Measurement keys of one device (``<id>-...`` or ``<id>.``...)."""
    return [k for k in all_keys if k.startswith(f"{device_id}-") or k.startswith(f"{device_id}.")]


def emr_keys(measurement_config: Any) -> list[str]:
    """Energy meter keys of ``/v1/config/measurement``."""
    keys: list[str] = []
    if isinstance(measurement_config, Mapping):
        for section in EMR_SECTIONS:
            for key in measurement_config.get(section) or []:
                if isinstance(key, str) and key not in keys:
                    keys.append(key)
    return keys


def state_value(key: str, state: Optional[str]) -> Optional[float]:
    """Number for EOS from an entity state; times (``*-ts`` keys) as Unix time."""
    if state is None:
        return None
    text = str(state).strip()
    lower = text.lower()
    if lower in _MISSING:
        return None
    if lower in _TRUE:
        return 1.0
    if lower in _FALSE:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    if key.endswith("-ts"):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.timestamp()
    return None


def heat_pump_values(
    device_id: str,
    *,
    room_mean_c: Optional[float],
    min_margin_k: Optional[float],
    max_margin_k: Optional[float],
    humidity_warning: bool,
    window_open_share: Optional[float],
    holiday: bool,
    holiday_return: Optional[datetime],
    reduced_setpoint_c: Optional[float],
    holiday_floor_c: float,
    reduction_locked_until: Optional[datetime],
    now: datetime,
) -> dict[str, Optional[float]]:
    """The computed heat pump measurements, by EOS key.

    The lock of the reduction is not sent while away (the holiday rules apply then);
    a lock in the past is 0.
    """
    lock = (
        reduction_locked_until.timestamp()
        if reduction_locked_until is not None and reduction_locked_until > now and not holiday
        else 0.0
    )

    def key(suffix: str) -> str:
        return f"{device_id}-{suffix}"

    return {
        key("room-temperature"): _round(room_mean_c),
        key("room-min-margin"): _round(min_margin_k),
        key("room-max-margin"): _round(max_margin_k),
        key("humidity"): 100.0 if humidity_warning else 0.0,
        key("window-open"): _round(window_open_share, 3),
        key("away"): 1.0 if holiday else 0.0,
        key("return-ts"): holiday_return.timestamp() if holiday and holiday_return else 0.0,
        key("away-room-temperature"): reduced_setpoint_c,
        key("setback-room-temperature"): reduced_setpoint_c,
        key("away-min-temperature"): holiday_floor_c,
        key("reduce-lock-until-ts"): lock,
    }


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None else None


def payload(values: Mapping[str, Optional[float]], now: datetime) -> Optional[dict[str, Any]]:
    """Body of ``PUT /v1/measurement/data``: one row at ``now``; None = nothing to send."""
    rows = {k: [v] for k, v in sorted(values.items()) if v is not None}
    if not rows:
        return None
    return {"start_datetime": now.isoformat(), "interval": "1 hour", **rows}
