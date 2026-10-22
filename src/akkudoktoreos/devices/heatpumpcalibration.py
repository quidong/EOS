"""Automatic heat pump calibration inside EOS (RL29 fork).

Glue between the measurement store and ``devices.thermal.calibration``, the
heat pump counterpart of the PV calibration in ``PVForecastAkkudoktorLocal``:

1. Hourly means of the calibration measurements of the last
   ``calibration.days`` are read from the measurement store.
2. Hours without a measured outdoor temperature or irradiance are filled from
   the weather provider's stored forecast (the last two days) and written back
   as measurements, so the history builds up while EOS runs and the solar term
   needs no extra sensor.
3. The fits run at most every ``calibration.refit_hours``; the result is kept
   in ``<data folder>/rl29/heatpump_calibration.json`` and applied to a copy of
   the device settings for the optimization run. The configuration itself is
   never written: it stays the prior of the next fit.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from akkudoktoreos.devices.thermal.calibration import (
    CalibrationResult,
    HourlySample,
    build_samples,
    calibrate,
    hourly_means,
)
from akkudoktoreos.utils.datetimeutil import to_datetime

WEATHER_FILL_HOURS = 48
"""How far back weather forecast values are mirrored into the measurements."""

WEATHER_KEYS = {"outdoor_c": "weather_temp_air", "ghi_w_per_m2": "weather_ghi"}
"""Sample fields the weather provider can supply, with their prediction keys."""


class HeatPumpCalibrationStore:
    """Last calibration result per device, persisted as JSON."""

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

    def result(
        self, device_id: str, priors_key: Optional[str] = None
    ) -> Optional[CalibrationResult]:
        """Stored result; ``None`` when it was fitted against other priors."""
        entry = self.data["devices"].get(device_id)
        if not entry or "result" not in entry:
            return None
        if priors_key is not None and entry.get("priors") != priors_key:
            return None
        try:
            return CalibrationResult.from_dict(entry["result"])
        except (KeyError, TypeError, ValueError):
            return None

    def set_result(
        self, device_id: str, result: CalibrationResult, summary: str, priors_key: str
    ) -> None:
        self.data["devices"][device_id] = {
            "result": result.to_dict(),
            "summary": summary,
            "priors": priors_key,
        }


def _as_datetime(value: Any) -> datetime:
    """Plain timezone-aware UTC datetime from pendulum, pandas or datetime values.

    Plain ``datetime`` on purpose: pendulum arithmetic can hand back naive
    values, and the hourly bucketing compares a lot of timestamps.
    """
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)


async def _measurement_series(
    measurement: Any, key: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    try:
        dates, values = await measurement.key_to_lists(
            key=key,
            start_datetime=to_datetime(start),
            end_datetime=to_datetime(end),
            dropna=True,
        )
    except (KeyError, ValueError):
        return []
    return [
        (_as_datetime(date), float(value))
        for date, value in zip(dates, values)
        if value is not None and math.isfinite(float(value))
    ]


async def _weather_series(prediction: Any, key: str) -> list[tuple[datetime, float]]:
    """Stored weather values (past and future) as (time, value)."""
    try:
        series = await prediction.key_to_raw_series(key=key, dropna=True)
    except (KeyError, ValueError, AttributeError):
        return []
    result = []
    for stamp, value in series.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append((_as_datetime(stamp), number))
    return result


async def gather_samples(
    device: Any,
    measurement: Any,
    prediction: Optional[Any],
    observation_time: datetime,
    *,
    fill_weather: bool = True,
) -> tuple[list[HourlySample], int]:
    """Hourly samples of the calibration window; returns them and the filled hours.

    Hours of the last ``WEATHER_FILL_HOURS`` without a measured outdoor
    temperature or irradiance are filled from the weather provider and written
    to the measurement store.
    """
    settings = device.calibration
    keys: dict[str, str] = device.calibration_measurement_keys
    end = _as_datetime(observation_time)
    start = end - timedelta(days=settings.days)
    columns: dict[str, dict[datetime, float]] = {}
    for field, key in keys.items():
        columns[field] = hourly_means(
            await _measurement_series(measurement, key, start, end + timedelta(seconds=1)),
            start,
            end,
        )
    filled = 0
    if fill_weather and prediction is not None:
        fill_start = max(start, end - timedelta(hours=WEATHER_FILL_HOURS))
        for field, weather_key in WEATHER_KEYS.items():
            weather = await _weather_series(prediction, weather_key)
            if not weather:
                continue
            means = hourly_means(weather, fill_start, end)
            for hour, value in means.items():
                if hour in columns[field]:
                    continue
                columns[field][hour] = value
                try:
                    await measurement.update_value(to_datetime(hour), keys[field], value)
                    filled += 1
                except Exception as exc:  # the store is best effort here
                    logger.debug(
                        "Heat pump {}: could not store {}: {}", device.device_id, keys[field], exc
                    )
    return build_samples(columns), filled


async def calibrate_device(
    device: Any,
    measurement: Any,
    prediction: Optional[Any],
    observation_time: datetime,
    data_folder: Path,
    timezone_name: Optional[str] = None,
) -> Optional[CalibrationResult]:
    """Calibration result for ``device``: the stored one if fresh, else a new fit."""
    settings = device.calibration
    if not settings.enabled:
        return None
    now = _as_datetime(observation_time)
    store = HeatPumpCalibrationStore(Path(data_folder) / "rl29" / "heatpump_calibration.json")
    priors = device.calibration_priors()
    # A changed configuration (new prior, other window or options) refits at once.
    priors_key = json.dumps(
        {**asdict(priors), **settings.model_dump()}, sort_keys=True, default=str
    )
    previous = store.result(device.device_id, priors_key)
    if previous is not None:
        age_h = (now - _as_datetime(previous.fitted_at)).total_seconds() / 3600.0
        if 0 <= age_h < settings.refit_hours:
            return previous
    tz = ZoneInfo(timezone_name) if timezone_name else None
    try:
        samples, filled = await gather_samples(
            device,
            measurement,
            prediction,
            now,
            fill_weather=settings.weather_from_prediction,
        )
        keys = device.calibration_measurement_keys
        series_end = now + timedelta(seconds=1)
        series_start = now - timedelta(days=settings.days)
        dhw_series = (
            await _measurement_series(measurement, keys["dhw_c"], series_start, series_end)
            if settings.fit_draw_profile
            else []
        )
        dhw_heat_series = (
            await _measurement_series(measurement, keys["dhw_heat_w"], series_start, series_end)
            if dhw_series
            else []
        )
        result = calibrate(
            samples,
            priors,
            device.cop_model(),
            settings.to_options(),
            tz,
            window_days=settings.days,
            now=now,
            fit_building_enabled=settings.fit_building,
            fit_cop_enabled=settings.fit_cop,
            fit_dhw_enabled=settings.fit_dhw,
            fit_emitter_enabled=settings.fit_emitter,
            fit_draw_enabled=settings.fit_draw_profile,
            dhw_series=dhw_series,
            dhw_heat_series=dhw_heat_series,
        )
        summary = result.summary(priors)
        store.set_result(device.device_id, result, summary, priors_key)
        store.save()
    except (OSError, ValueError, ArithmeticError) as exc:
        logger.warning("Heat pump {}: calibration failed: {}", device.device_id, exc)
        return previous
    if result.building or result.cop or result.dhw or result.emitter or result.draw:
        logger.info(
            "Heat pump {} calibration over {} days ({} hourly samples, {} weather hours filled): {}",
            device.device_id,
            settings.days,
            result.samples,
            filled,
            summary,
        )
    else:
        logger.info(
            "Heat pump {} calibration: nothing fitted yet ({} hourly samples): {}",
            device.device_id,
            result.samples,
            summary,
        )
    return result
