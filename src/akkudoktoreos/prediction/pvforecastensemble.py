"""PV forecast ensemble with self-calibration (RL29 fork).

Combines the physical PVLib forecast (weather → irradiance → module → inverter) with a
forecast read from a Home Assistant entity (Helios Forecast, Solcast, ...):

1. every update computes both members and keeps them in the provider's own records
   (``pvforecast_member_pvlib_power_w``, ``pvforecast_member_ha_power_w``),
2. the members are weighted by the inverse squared mean absolute error of their hourly
   energy against the measured PV production of the last ``days`` days (equal weights
   until ``min_days`` days with measurements exist; a member that failed gets weight 0),
3. the blend is scaled by a calibration factor: the ``scale_quantile`` of the daily
   ratio measured / blended over the same days, bounded by ``scale_min``/``scale_max``.
   A quantile below 0.5 plans with less PV than expected (reserve for cloudy surprises).

The result is ``pvforecast_ac_power`` as for every PV provider. If no measurement or no
second member is configured the provider is a calibrated PVLib forecast; if PVLib fails
(no weather) the Home Assistant member alone is used.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
from loguru import logger
from pydantic import Field, model_validator

from akkudoktoreos.config.configabc import SettingsBaseModel
from akkudoktoreos.prediction.pvforecastabc import PVForecastDataRecord
from akkudoktoreos.prediction.pvforecastpvlib import PVForecastPVLib
from akkudoktoreos.utils.datetimeutil import to_datetime, to_duration

MEMBERS = ("pvlib", "homeassistant")
MEMBER_KEYS = {
    "pvlib": "pvforecast_member_pvlib_power_w",
    "homeassistant": "pvforecast_member_ha_power_w",
}


class PVForecastEnsembleCommonSettings(SettingsBaseModel):
    """Settings of the PV ensemble (RL29 fork)."""

    members: list[str] = Field(
        default_factory=lambda: ["pvlib"],
        json_schema_extra={
            "description": (
                "Members of the ensemble [pvlib | homeassistant]. 'homeassistant' reads the "
                "entity configured in pvforecast.homeassistant."
            ),
            "examples": [["pvlib", "homeassistant"]],
        },
    )
    days: int = Field(
        default=14,
        ge=3,
        le=60,
        json_schema_extra={
            "description": "Days used for weights and calibration.",
            "examples": [14],
        },
    )
    min_days: int = Field(
        default=4,
        ge=1,
        json_schema_extra={
            "description": "Days with measurements needed before weights/calibration apply.",
            "examples": [4],
        },
    )
    scale_quantile: float = Field(
        default=0.5,
        gt=0,
        lt=1,
        json_schema_extra={
            "description": (
                "Quantile of the daily measured/forecast ratio used as calibration factor; "
                "< 0.5 is conservative."
            ),
            "examples": [0.5, 0.4],
        },
    )
    scale_min: float = Field(
        default=0.6, gt=0, json_schema_extra={"description": "Lower bound of the factor."}
    )
    scale_max: float = Field(
        default=1.3, gt=0, json_schema_extra={"description": "Upper bound of the factor."}
    )
    min_day_fraction: float = Field(
        default=0.15,
        ge=0,
        lt=1,
        json_schema_extra={
            "description": (
                "Days whose measured energy is below this share of the forecast are not "
                "used for the factor (outage, snow) - only for the weights."
            ),
            "examples": [0.15],
        },
    )

    @model_validator(mode="after")
    def _check(self) -> "PVForecastEnsembleCommonSettings":
        unknown = set(self.members) - set(MEMBERS)
        if not self.members or unknown:
            raise ValueError(f"members must be a non-empty subset of {MEMBERS}")
        if self.scale_min > self.scale_max:
            raise ValueError("scale_min must not exceed scale_max")
        return self


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def member_weights(
    errors: dict[str, Optional[float]], available: Sequence[str]
) -> dict[str, float]:
    """Inverse squared MAE weights of the available members (equal without errors)."""
    members = [m for m in available]
    if not members:
        return {}
    known = {m: errors.get(m) for m in members}
    if any(v is None for v in known.values()):
        return {m: 1.0 / len(members) for m in members}
    inverse = {m: 1.0 / max(float(v), 1.0) ** 2 for m, v in known.items()}  # type: ignore[arg-type]
    total = sum(inverse.values())
    return {m: v / total for m, v in inverse.items()}


def calibration_factor(
    daily_actual: Sequence[float],
    daily_forecast: Sequence[float],
    quantile: float,
    low: float,
    high: float,
    min_fraction: float,
    min_days: int,
) -> Optional[float]:
    """Quantile of the daily measured/forecast ratios; None with too few usable days."""
    ratios = []
    for actual, forecast in zip(daily_actual, daily_forecast):
        if forecast <= 500.0:  # < 0.5 kWh forecast: ratio meaningless
            continue
        ratio = actual / forecast
        if ratio < min_fraction:
            continue
        ratios.append(ratio)
    if len(ratios) < min_days:
        return None
    return float(min(max(np.quantile(ratios, quantile), low), high))


def hourly_mae(
    forecast: Sequence[Optional[float]], actual: Sequence[Optional[float]]
) -> Optional[float]:
    pairs = [
        (f, a)
        for f, a in zip(forecast, actual)
        if f is not None and a is not None and math.isfinite(f) and math.isfinite(a)
    ]
    if not pairs:
        return None
    return float(np.mean([abs(f - a) for f, a in pairs]))


def parse_ha_forecast(
    entries: Any, datetime_key: str, value_key: str, unit: str, timezone: str
) -> list[tuple[Any, float]]:
    """(datetime, W) pairs from a Home Assistant forecast attribute."""
    factor = 1000.0 if unit == "kW" else 1.0
    parsed = []
    for entry in entries or []:
        try:
            parsed.append(
                (
                    to_datetime(entry[datetime_key], in_timezone=timezone),
                    float(entry[value_key]) * factor,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return parsed


# --------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------


class PVForecastEnsembleDataRecord(PVForecastDataRecord):
    """PV record with the ensemble members."""

    pvforecast_member_pvlib_power_w: Optional[float] = Field(
        default=None, json_schema_extra={"description": "Ensemble member PVLib, AC power (W)."}
    )
    pvforecast_member_ha_power_w: Optional[float] = Field(
        default=None,
        json_schema_extra={"description": "Ensemble member Home Assistant, AC power (W)."},
    )


class PVForecastEnsemble(PVForecastPVLib):
    """Weighted, self-calibrated blend of PVLib and a Home Assistant PV forecast."""

    records: list[PVForecastEnsembleDataRecord] = Field(
        default_factory=list,
        json_schema_extra={"description": "List of PVForecastEnsembleDataRecord records"},
    )

    @classmethod
    def provider_id(cls) -> str:
        return "PVForecastEnsemble"

    @property
    def _ens(self) -> PVForecastEnsembleCommonSettings:
        return self.config.pvforecast.ensemble

    async def _member_pvlib(self) -> bool:
        """Run PVLib into ``pvforecast_ac_power`` and copy it into the member key."""
        try:
            await super()._update_data()
        except Exception as exc:
            logger.warning("PVForecastEnsemble: PVLib member failed: {}", exc)
            return False
        start = self.ems_start_datetime.start_of("day")
        end = self.ems_start_datetime.add(hours=self.config.prediction.hours)
        dates, values = await self.key_to_lists("pvforecast_ac_power", start, end, dropna=True)
        for date, value in zip(dates, values):
            if value is not None:
                await self._update_value(date, MEMBER_KEYS["pvlib"], float(value))
        return bool(dates)

    async def _member_homeassistant(self) -> bool:
        from akkudoktoreos.prediction.pvforecasthomeassistant import (
            PVForecastHomeAssistant,
        )

        settings = self.config.pvforecast.homeassistant
        try:
            state = PVForecastHomeAssistant()._request_entity_state()
            parsed = parse_ha_forecast(
                state.get("attributes", {}).get(settings.attribute),
                settings.datetime_key,
                settings.value_key,
                settings.value_unit,
                self.config.general.timezone,
            )
        except Exception as exc:
            logger.warning("PVForecastEnsemble: Home Assistant member failed: {}", exc)
            return False
        if not parsed:
            return False
        start = self.ems_start_datetime
        end = self.end_datetime
        if start is not None and end is not None:
            await self.key_delete_by_datetime(
                MEMBER_KEYS["homeassistant"], start_datetime=start, end_datetime=end
            )
        for date, watts in parsed:
            await self._update_value(date, MEMBER_KEYS["homeassistant"], watts)
        return True

    async def _hourly(self, key: str, start: Any, end: Any) -> list[Optional[float]]:
        try:
            values = await self.key_to_array(
                key=key,
                start_datetime=start,
                end_datetime=end,
                interval=to_duration("1 hour"),
                fill_method="linear",
                boundary="context",
            )
        except Exception:
            return []
        return [None if v is None or not math.isfinite(float(v)) else float(v) for v in values]

    async def _measured_hourly(self, start: Any, end: Any) -> list[Optional[float]]:
        keys = self.config.measurement.pv_production_emr_keys or []
        if not keys:
            return []
        total: Optional[np.ndarray] = None
        try:
            for key in keys:
                kwh = await self.measurement._energy_from_meter_readings(
                    key, start, end, to_duration("1 hour")
                )
                total = kwh if total is None else total + kwh
        except Exception as exc:
            logger.debug("PVForecastEnsemble: no PV measurements: {}", exc)
            return []
        if total is None:
            return []
        return [None if not math.isfinite(float(v)) or v < 0 else float(v) * 1000.0 for v in total]

    async def _update_data(self, force_update: Optional[bool] = False) -> None:
        settings = self._ens
        ok: dict[str, bool] = {}
        if "pvlib" in settings.members:
            ok["pvlib"] = await self._member_pvlib()
        if "homeassistant" in settings.members:
            ok["homeassistant"] = await self._member_homeassistant()
        available = [m for m in settings.members if ok.get(m)]
        if not available:
            raise ValueError("PVForecastEnsemble: no member produced a forecast")

        # Errors, weights and calibration over the last days (complete days only).
        today = self.ems_start_datetime.start_of("day")
        past_start = today.subtract(days=settings.days)
        actual = await self._measured_hourly(past_start, today)
        member_hourly = {
            m: await self._hourly(MEMBER_KEYS[m], past_start, today) for m in available
        }
        days_with_data = sum(
            1 for i in range(0, len(actual), 24) if all(a is not None for a in actual[i : i + 24])
        )
        errors: dict[str, Optional[float]] = {}
        if days_with_data >= settings.min_days:
            for m in available:
                errors[m] = hourly_mae(member_hourly[m], actual)
        weights = member_weights(errors, available)

        factor = None
        if actual and days_with_data >= settings.min_days:
            n = min(len(actual), *(len(v) for v in member_hourly.values()))
            blended_past = [
                sum(weights[m] * (member_hourly[m][i] or 0.0) for m in available) for i in range(n)
            ]
            daily_actual, daily_forecast = [], []
            for d in range(0, n - 23, 24):
                day_actual = actual[d : d + 24]
                if any(a is None for a in day_actual):
                    continue
                daily_actual.append(sum(day_actual))  # type: ignore[arg-type]
                daily_forecast.append(sum(blended_past[d : d + 24]))
            factor = calibration_factor(
                daily_actual,
                daily_forecast,
                settings.scale_quantile,
                settings.scale_min,
                settings.scale_max,
                settings.min_day_fraction,
                settings.min_days,
            )
        scale = factor if factor is not None else 1.0

        # Blend the future (15 min grid like PVLib; members are interpolated onto it).
        start = self.ems_start_datetime.start_of("day")
        end = self.ems_start_datetime.add(hours=self.config.prediction.hours)
        grid = to_duration("15 minutes")
        series: dict[str, Any] = {}
        for m in available:
            try:
                series[m] = await self.key_to_array(
                    key=MEMBER_KEYS[m],
                    start_datetime=start,
                    end_datetime=end,
                    interval=grid,
                    fill_method="linear",
                    boundary="context",
                )
            except Exception:
                series[m] = None
        n_slots = max((len(v) for v in series.values() if v is not None), default=0)
        limit = self.config.pvforecast.pvlib.max_ac_power_w
        stamp = start
        for i in range(n_slots):
            total = weight_sum = 0.0
            for m in available:
                values = series[m]
                if (
                    values is None
                    or i >= len(values)
                    or values[i] is None
                    or not math.isfinite(float(values[i]))
                ):
                    continue
                total += weights[m] * max(float(values[i]), 0.0)
                weight_sum += weights[m]
            if weight_sum > 0:
                power = total / weight_sum * scale
                if limit:
                    power = min(power, limit)
                await self._update_value(stamp, "pvforecast_ac_power", power)
            stamp = stamp.add(minutes=15)

        summary = {
            "members": available,
            "weights": {m: round(w, 3) for m, w in weights.items()},
            "mae_wh": {m: (round(v, 1) if v is not None else None) for m, v in errors.items()},
            "scale": round(scale, 3),
            "calibrated": factor is not None,
            "days_with_measurements": days_with_data,
        }
        logger.info("PVForecastEnsemble: {}", summary)
        try:
            path = Path(self.config.general.data_folder_path) / "rl29" / "pv_ensemble.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.debug("PVForecastEnsemble: summary not written: {}", exc)
        self.update_datetime = to_datetime(in_timezone=self.config.general.timezone)
