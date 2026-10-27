"""Electricity price forecast beyond the published day-ahead prices (eos-plus fork).

Day-ahead prices are known until midnight today, from about 13:00 until midnight
tomorrow. Everything after that - up to 60 hours with a 72 hour prediction - used to come
from an exponential smoothing (ETS) of the price history alone. Those hours decide how
much charge the battery keeps and how much heat is stored "for later", so their quality
matters.

This module forecasts them with gradient boosting (LightGBM, already used for the base
load):

- calendar: time of day, weekday, working day / Saturday / Sunday or public holiday,
  season;
- price level: the same time on the last published day, the same time a week before,
  mean / min / max / spread of the last published day, mean of the last week;
- weather at the target time and on the last published day at the same time: global
  irradiance, wind speed, temperature, cloud cover from the EOS weather forecast. The
  site weather is a proxy for German solar and wind generation, the main drivers of the
  price shape;
- lead time after the last known price.

The model learns the price *relative to the level of the last published day*. Training
samples are built like the real situation: for every past midnight the following hours
are "forecast" from what was known at that midnight.

Safety gate (as for the base load): the model is used only when it beats the ETS
forecast on the last ``holdout_days`` days (both trained without them); otherwise ETS
stays in charge. Report: ``<data>/plus/price_ml.json``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
from pydantic import Field, field_validator

from akkudoktoreos.config.configabc import SettingsBaseModel
from akkudoktoreos.prediction.calendarfeatures import (
    GERMAN_STATES,
    day_type,
    resolve_region,
    validate_region,
)

WEATHER_KEYS = ("weather_ghi", "weather_wind_speed", "weather_temp_air", "weather_total_clouds")

FEATURES = (
    "tod_h",
    "weekday",
    "day_type",
    "doy_sin",
    "doy_cos",
    "lead_h",
    "p_last_day",
    "p_week",
    "day_mean",
    "day_min",
    "day_max",
    "day_std",
    "week_mean",
    "ghi",
    "wind",
    "temp",
    "clouds",
    "ghi_last_day",
    "wind_last_day",
)


class ElecPriceMLSettings(SettingsBaseModel):
    """Price forecast after the published day-ahead with LightGBM (eos-plus fork)."""

    enabled: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "Forecast the hours after the last published price with LightGBM (used "
                "only when it beats the ETS forecast on the holdout days)."
            ),
            "examples": [True],
        },
    )
    history_days: int = Field(
        default=180,
        ge=35,
        le=730,
        json_schema_extra={
            "description": "Price history fetched and used for training [days].",
            "examples": [180],
        },
    )
    min_days: int = Field(
        default=28,
        ge=14,
        json_schema_extra={
            "description": "Days of history needed before training.",
            "examples": [28],
        },
    )
    holdout_days: int = Field(
        default=7,
        ge=2,
        le=28,
        json_schema_extra={
            "description": "The model must beat ETS on these last days to be used.",
            "examples": [7],
        },
    )
    horizon_hours: int = Field(
        default=72,
        ge=24,
        le=168,
        json_schema_extra={
            "description": "Longest lead time after the last known price that is trained [h].",
            "examples": [72],
        },
    )
    refit_hours: float = Field(
        default=6.0,
        ge=1,
        json_schema_extra={"description": "Retrain after this many hours.", "examples": [6]},
    )
    holiday_region: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Public holidays as ISO 3166 code: country ('NL') or country-subdivision "
                "('DE-BW', 'AT-9', 'CH-ZH'). Empty: no public holidays, weekends only."
            ),
            "examples": ["DE-BW", "AT", "NL"],
        },
    )
    state: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "description": "Deprecated: German federal state, use holiday_region, e.g. 'DE-BW' for 'BW'.",
            "examples": [None],
        },
    )
    num_leaves: int = Field(default=15, ge=4, le=127)
    n_estimators: int = Field(default=300, ge=20, le=3000)
    learning_rate: float = Field(default=0.05, gt=0, le=1)
    min_data_in_leaf: int = Field(default=40, ge=1)

    @field_validator("holiday_region")
    @classmethod
    def _holiday_region(cls, value: Optional[str]) -> Optional[str]:
        return validate_region(value)

    @field_validator("state")
    @classmethod
    def _state(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        value = value.upper()
        if value not in GERMAN_STATES:
            raise ValueError(f"state must be one of {', '.join(GERMAN_STATES)}")
        return value

    @property
    def region(self) -> Optional[str]:
        """Effective holiday region (``holiday_region``, else the deprecated ``state``)."""
        return resolve_region(self.holiday_region, self.state)


# --------------------------------------------------------------------------------------
# Pure model code (no EOS objects): arrays of slot starts (Unix seconds) and prices
# --------------------------------------------------------------------------------------


@dataclass
class PriceSeries:
    """Equidistant price history; ``price[i]`` belongs to ``start + i * step``."""

    start: float
    step: int
    price: np.ndarray
    weather: dict[str, Callable[[float], float]] = field(default_factory=dict)
    """Weather value at a time (NaN when unknown), per key of ``WEATHER_KEYS``."""

    @property
    def end(self) -> float:
        """Start of the last known slot."""
        return self.start + (len(self.price) - 1) * self.step

    def at(self, ts: float) -> float:
        i = int(round((ts - self.start) / self.step))
        if 0 <= i < len(self.price):
            value = float(self.price[i])
            return value if math.isfinite(value) else math.nan
        return math.nan

    def window(self, end: float, hours: float) -> np.ndarray:
        """Prices of the slots in (end - hours, end]."""
        i_end = int(round((end - self.start) / self.step))
        i_start = i_end - int(hours * 3600 / self.step) + 1
        i_start = max(i_start, 0)
        if i_end < 0:
            return np.array([])
        values = self.price[i_start : i_end + 1]
        return values[np.isfinite(values)]


def _weather(series: PriceSeries, key: str, ts: float) -> float:
    getter = series.weather.get(key)
    if getter is None:
        return math.nan
    try:
        value = float(getter(ts))
    except Exception:
        return math.nan
    return value if math.isfinite(value) else math.nan


def level_features(series: PriceSeries, cutoff: float) -> tuple[float, float, float, float, float]:
    """Mean, min, max, spread of the last known day and mean of the last week."""
    day = series.window(cutoff, 24)
    week_values = series.window(cutoff, 24 * 7)
    nan = math.nan
    return (
        float(day.mean()) if day.size else nan,
        float(day.min()) if day.size else nan,
        float(day.max()) if day.size else nan,
        float(day.std()) if day.size else nan,
        float(week_values.mean()) if week_values.size else nan,
    )


def feature_row(
    series: PriceSeries,
    cutoff: float,
    target: float,
    tz: ZoneInfo,
    region: Optional[str],
    level: Optional[tuple[float, float, float, float, float]] = None,
) -> list[float]:
    """Features of ``target`` forecast with prices known up to the slot ``cutoff``."""
    local = datetime.fromtimestamp(target, tz)
    doy = local.timetuple().tm_yday
    days_back = max(1, math.ceil((target - cutoff) / 86400.0 - 1e-9))
    same_last_day = target - days_back * 86400.0
    week = target - 7 * 86400.0
    if week > cutoff:
        week -= 7 * 86400.0
    day_mean, day_min, day_max, day_std, week_mean = level or level_features(series, cutoff)
    return [
        local.hour + local.minute / 60.0,
        float(local.weekday()),
        float(day_type(local.date(), region)),
        math.sin(2 * math.pi * doy / 366.0),
        math.cos(2 * math.pi * doy / 366.0),
        (target - cutoff) / 3600.0,
        series.at(same_last_day),
        series.at(week),
        day_mean,
        day_min,
        day_max,
        day_std,
        week_mean,
        _weather(series, "weather_ghi", target),
        _weather(series, "weather_wind_speed", target),
        _weather(series, "weather_temp_air", target),
        _weather(series, "weather_total_clouds", target),
        _weather(series, "weather_ghi", same_last_day),
        _weather(series, "weather_wind_speed", same_last_day),
    ]


DAY_MEAN = FEATURES.index("day_mean")


def _midnight_cutoffs(series: PriceSeries, tz: ZoneInfo, first: float, last: float) -> list[float]:
    """Start of the last slot before every local midnight in [first, last]."""
    cutoffs = []
    day = datetime.fromtimestamp(first, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        midnight = day.timestamp()
        if midnight > last:
            break
        if midnight >= first:
            cutoffs.append(midnight - series.step)
        day = datetime.fromtimestamp(midnight + 26 * 3600, tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return cutoffs


def training_rows(
    series: PriceSeries,
    cutoffs: Sequence[float],
    horizon_hours: float,
    tz: ZoneInfo,
    region: Optional[str],
    target_until: Optional[float] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(X, y relative to the day mean, cutoff per row) for the given cutoffs."""
    rows: list[list[float]] = []
    ys: list[float] = []
    groups: list[float] = []
    limit = series.end if target_until is None else min(series.end, target_until)
    for cutoff in cutoffs:
        steps = int(horizon_hours * 3600 / series.step)
        level = level_features(series, cutoff)
        for k in range(1, steps + 1):
            target = cutoff + k * series.step
            if target > limit:
                break
            value = series.at(target)
            if not math.isfinite(value):
                continue
            row = feature_row(series, cutoff, target, tz, region, level)
            if not math.isfinite(row[DAY_MEAN]):
                continue
            rows.append(row)
            ys.append(value - row[DAY_MEAN])
            groups.append(cutoff)
    return np.array(rows, dtype=float), np.array(ys, dtype=float), np.array(groups, dtype=float)


def _fit(X: np.ndarray, y: np.ndarray, settings: ElecPriceMLSettings) -> Any:
    import lightgbm as lgb

    params = {
        "objective": "l1",
        "learning_rate": settings.learning_rate,
        "num_leaves": settings.num_leaves,
        "min_data_in_leaf": settings.min_data_in_leaf,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "feature_fraction": 0.9,
        "seed": 42,
        "deterministic": True,
        "num_threads": 1,
        "verbosity": -1,
    }
    dataset = lgb.Dataset(X, label=y, feature_name=list(FEATURES), free_raw_data=True)
    return lgb.train(params, dataset, num_boost_round=settings.n_estimators)


@dataclass
class PriceModel:
    booster: Any
    trained_at: float
    days: int
    holdout_mae_ml: Optional[float]
    holdout_mae_ets: Optional[float]
    use: bool
    rows: int

    def summary(self) -> dict[str, Any]:
        return {
            "used": self.use,
            "days": self.days,
            "rows": self.rows,
            "holdout_mae_ml_ct_kwh": _ct(self.holdout_mae_ml),
            "holdout_mae_ets_ct_kwh": _ct(self.holdout_mae_ets),
            "trained_at": datetime.fromtimestamp(self.trained_at, timezone.utc).isoformat(),
        }


def _ct(value_per_wh: Optional[float]) -> Optional[float]:
    return None if value_per_wh is None else round(value_per_wh * 1e5, 3)


def predict_prices(
    model: PriceModel, series: PriceSeries, count: int, tz: ZoneInfo, region: Optional[str]
) -> np.ndarray:
    """``count`` prices after the last known slot."""
    cutoff = series.end
    level = level_features(series, cutoff)
    rows = [
        feature_row(series, cutoff, cutoff + (k + 1) * series.step, tz, region, level)
        for k in range(count)
    ]
    X = np.array(rows, dtype=float)
    return np.asarray(model.booster.predict(X), dtype=float) + X[:, DAY_MEAN]


def train_price_model(
    series: PriceSeries,
    settings: ElecPriceMLSettings,
    tz: ZoneInfo,
    ets: Callable[[np.ndarray, int], np.ndarray],
    now: Optional[float] = None,
) -> Optional[PriceModel]:
    """Train and validate; None with too little history.

    ``ets(history, count)`` is the existing forecast (the reference to beat).
    """
    days = int((series.end - series.start) / 86400)
    if days < settings.min_days:
        return None
    first = series.start + 8 * 86400  # one week of lags plus a day of level
    holdout_start = series.end - settings.holdout_days * 86400
    cutoffs = _midnight_cutoffs(series, tz, first, series.end)
    train_cutoffs = [c for c in cutoffs if c < holdout_start]
    holdout_cutoffs = [c for c in cutoffs if c >= holdout_start]
    X, y, _ = training_rows(
        series,
        train_cutoffs,
        settings.horizon_hours,
        tz,
        settings.region,
        target_until=holdout_start,
    )
    if len(y) < 500 or not holdout_cutoffs:
        return None
    booster = _fit(X, y, settings)
    errors_ml: list[float] = []
    errors_ets: list[float] = []
    steps_per_day = int(86400 / series.step)
    ets_window = 35 * steps_per_day
    for cutoff in holdout_cutoffs:
        Xh, yh, _ = training_rows(series, [cutoff], settings.horizon_hours, tz, settings.region)
        if len(yh) == 0:
            continue
        predicted = np.asarray(booster.predict(Xh), dtype=float) + Xh[:, DAY_MEAN]
        actual = yh + Xh[:, DAY_MEAN]
        i_cut = int(round((cutoff - series.start) / series.step))
        history = series.price[max(0, i_cut + 1 - ets_window) : i_cut + 1]
        history = history[np.isfinite(history)]
        try:
            reference = np.asarray(ets(history, len(actual)), dtype=float)[: len(actual)]
        except Exception:  # noqa: S112 - no reference for this day: leave it out
            continue
        errors_ml.extend(np.abs(predicted - actual).tolist())
        errors_ets.extend(np.abs(reference - actual).tolist())
    mae_ml = float(np.mean(errors_ml)) if errors_ml else None
    mae_ets = float(np.mean(errors_ets)) if errors_ets else None
    use = mae_ml is not None and mae_ets is not None and mae_ml < mae_ets
    # Final model with all days.
    X_all, y_all, _ = training_rows(series, cutoffs, settings.horizon_hours, tz, settings.region)
    final = _fit(X_all, y_all, settings) if use else booster
    return PriceModel(
        booster=final,
        trained_at=now if now is not None else time.time(),
        days=days,
        holdout_mae_ml=mae_ml,
        holdout_mae_ets=mae_ets,
        use=use,
        rows=int(len(y_all)),
    )
