"""Load forecast from the household's own measured history (RL29 fork).

``LoadAkkudoktorAdjusted`` corrects a standard load profile with the deviation of
the last seven days. Once enough of the household's own measurements exist, this
provider forecasts directly from them instead:

- the hourly energy of the last ``weeks`` weeks is read from the load meters
  (``measurement.load_emr_keys``),
- days with a measurement gap (daily energy far below the median day) are left
  out, the same idea as the outage filter of the PV calibration,
- the forecast for a slot is a robust statistic (median by default) of the same
  hour on comparable days: the same weekday when every weekday has
  ``min_days_per_type`` healthy days, otherwise working days / weekend.

Until ``min_days`` healthy days exist the provider behaves exactly like
``LoadAkkudoktorAdjusted``, so it can be configured from day one and switches over
by itself. Public holidays are not recognised (they count as their weekday).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Optional

import numpy as np
from loguru import logger
from pydantic import Field

from akkudoktoreos.config.configabc import SettingsBaseModel
from akkudoktoreos.prediction.loadakkudoktor import LoadAkkudoktorAdjusted
from akkudoktoreos.utils.datetimeutil import compare_datetimes, to_datetime, to_duration


class LoadMeasuredCommonSettings(SettingsBaseModel):
    """Settings of the measured-history load provider (RL29 fork)."""

    weeks: int = Field(
        default=6,
        ge=1,
        le=52,
        json_schema_extra={"description": "History window [weeks].", "examples": [6, 4]},
    )
    min_days: int = Field(
        default=14,
        ge=3,
        json_schema_extra={
            "description": (
                "Healthy days needed before the own history replaces the adjusted standard profile."
            ),
            "examples": [14],
        },
    )
    min_days_per_type: int = Field(
        default=3,
        ge=1,
        json_schema_extra={
            "description": (
                "Each weekday needs this many healthy days for a per-weekday profile; "
                "otherwise working days / weekend are pooled."
            ),
            "examples": [3],
        },
    )
    quantile: float = Field(
        default=0.5,
        gt=0,
        lt=1,
        json_schema_extra={
            "description": "Statistic per hour: 0.5 = median; higher is more conservative.",
            "examples": [0.5, 0.6],
        },
    )
    gap_threshold: float = Field(
        default=0.3,
        ge=0,
        lt=1,
        json_schema_extra={
            "description": (
                "A day with less than this share of the median daily energy is treated as a "
                "measurement gap and left out."
            ),
            "examples": [0.3],
        },
    )


def build_profile(
    hourly: list[tuple[Any, float]],
    settings: LoadMeasuredCommonSettings,
) -> Optional[dict[str, Any]]:
    """Profile from (local hour start, Wh) pairs; None when there is not enough data.

    Returns ``{"mode": "weekday"|"workday_weekend", "days": n, "profile": {key: [24 Wh]}}``
    with keys ``0``..``6`` (weekday mode) or ``"workday"``/``"weekend"``.
    """
    days: dict[Any, dict[int, float]] = defaultdict(dict)
    for stamp, wh in hourly:
        if wh is None or not math.isfinite(wh) or wh < 0:
            continue
        days[stamp.date()][stamp.hour] = wh
    complete = {d: h for d, h in days.items() if len(h) >= 22}
    if not complete:
        return None
    totals = {d: sum(h.values()) for d, h in complete.items()}
    median_total = float(np.median(list(totals.values())))
    healthy = {
        d: h for d, h in complete.items() if totals[d] >= settings.gap_threshold * median_total
    }
    if len(healthy) < settings.min_days:
        return None
    by_weekday: dict[int, list[dict[int, float]]] = defaultdict(list)
    for d, h in healthy.items():
        by_weekday[d.weekday()].append(h)
    per_weekday = all(len(by_weekday[w]) >= settings.min_days_per_type for w in range(7))

    def stat(groups: list[dict[int, float]]) -> list[float]:
        result = []
        for hour in range(24):
            values = [g[hour] for g in groups if hour in g]
            result.append(float(np.quantile(values, settings.quantile)) if values else 0.0)
        return result

    if per_weekday:
        profile = {str(w): stat(by_weekday[w]) for w in range(7)}
        mode = "weekday"
    else:
        workday = [h for w in range(5) for h in by_weekday[w]]
        weekend = [h for w in (5, 6) for h in by_weekday[w]]
        if not workday or not weekend:
            return None
        profile = {"workday": stat(workday), "weekend": stat(weekend)}
        mode = "workday_weekend"
    return {"mode": mode, "days": len(healthy), "profile": profile}


def profile_value(profile: dict[str, Any], weekday: int, hour: int) -> float:
    """Forecast Wh for a weekday (0 = Monday) and hour."""
    if profile["mode"] == "weekday":
        return float(profile["profile"][str(weekday)][hour])
    key = "weekend" if weekday >= 5 else "workday"
    return float(profile["profile"][key][hour])


class LoadMeasured(LoadAkkudoktorAdjusted):
    """Load forecast from the own measured history; adjusted standard profile until then."""

    @classmethod
    def provider_id(cls) -> str:
        return "LoadMeasured"

    @property
    def _settings(self) -> LoadMeasuredCommonSettings:
        return self.config.load.loadmeasured

    async def _measured_hourly(self) -> list[tuple[Any, float]]:
        settings = self._settings
        end = self.ems_start_datetime.start_of("hour")
        start = end.subtract(weeks=settings.weeks)
        min_dt = await self.measurement.min_datetime()
        if min_dt is None:
            return []
        if compare_datetimes(start, min_dt).lt:
            start = to_datetime(min_dt, in_timezone=self.config.general.timezone).start_of("day")
        if not compare_datetimes(start, end).lt:
            return []
        kwh = await self.measurement.load_total_kwh(
            start_datetime=start, end_datetime=end, interval=to_duration("1 hour")
        )
        result = []
        stamp = start.in_timezone(self.config.general.timezone)
        for value in kwh:
            result.append((stamp, float(value) * 1000.0))
            stamp = stamp.add(hours=1)
        return result

    async def _update_data(self, force_update: Optional[bool] = False) -> None:
        try:
            profile = build_profile(await self._measured_hourly(), self._settings)
        except Exception as exc:  # never lose the forecast because of the history
            logger.warning("LoadMeasured: history unusable ({}); using the adjusted profile.", exc)
            profile = None
        if profile is None:
            logger.info(
                "LoadMeasured: fewer than {} healthy days of own measurements; using "
                "LoadAkkudoktorAdjusted.",
                self._settings.min_days,
            )
            await super()._update_data(force_update)
            return
        data_year_energy = self.load_data()
        tz = self.config.general.timezone
        date = self.ems_start_datetime.start_of("day")
        end_date = self.ems_start_datetime.add(hours=self.config.prediction.hours)
        while compare_datetimes(date, end_date).lt:
            local = date.in_timezone(tz)
            hourly_stats = data_year_energy[date.day_of_year - 1, :, date.hour]
            await self.update_value(
                date,
                {
                    "loadakkudoktor_mean_power_w": hourly_stats[0],
                    "loadakkudoktor_std_power_w": hourly_stats[1],
                    "loadforecast_power_w": max(
                        0.0, profile_value(profile, local.day_of_week, local.hour)
                    ),
                },
            )
            date += to_duration("1 hour")
        logger.info(
            "LoadMeasured: forecast from {} healthy days ({} profile), {:.1f} kWh/day on average.",
            profile["days"],
            profile["mode"],
            float(np.mean([sum(v) for v in profile["profile"].values()])) / 1000.0,
        )
        self.update_datetime = to_datetime(in_timezone=tz)
