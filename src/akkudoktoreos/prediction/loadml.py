"""Base load forecast with gradient boosting and quantiles (RL29 fork).

``LoadMeasured`` forecasts a robust statistic per weekday and hour. ``LoadML`` learns
the household's **base load** (the house meter minus the consumers EOS plans itself:
heat pump, heating rod, washing machine, ...) with LightGBM quantile regression:

- target: base load per hour [Wh] = load meters (``measurement.load_emr_keys``)
  minus the hourly mean power of ``subtract_power_keys`` (hold-last-value, like the
  Home Assistant history),
- features: hour, weekday, day type (working day / Saturday / Sunday or public holiday
  of ``state``), bridge day, season (day of year), outdoor temperature, away mode,
  the load of the same hour one week before and the level of the week before last,
- one model per quantile (``quantiles``); the forecast EOS plans with is
  ``plan_quantile`` (0.5 = median, higher = more conservative = more reserve), the
  others are published as ``loadforecast_p<q>_power_w`` for the optimizer reports.

Safety gates: the models are only used when there are ``min_days`` healthy days and
their error on the last ``holdout_days`` beats the ``LoadMeasured`` profile on the same
days. Otherwise this provider behaves exactly like ``LoadMeasured`` (which itself falls
back to the adjusted standard profile). The forecast is clipped to ``[0, max]`` of the
history.

Because the target is the base load only, the heat pump must be configured with
``load_forecast_includes_heat_pump: false`` and appliances EOS schedules must be in
``subtract_power_keys``; otherwise their energy is counted twice.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
from loguru import logger
from pydantic import Field, field_validator

from akkudoktoreos.config.configabc import SettingsBaseModel
from akkudoktoreos.core.coreabc import PredictionMixin
from akkudoktoreos.prediction.calendarfeatures import STATES, day_type, is_bridge_day
from akkudoktoreos.prediction.loadakkudoktor import LoadAkkudoktorDataRecord
from akkudoktoreos.prediction.loadmeasured import (
    LoadMeasured,
    LoadMeasuredCommonSettings,
    build_profile,
    profile_value,
)
from akkudoktoreos.utils.datetimeutil import to_datetime, to_duration

HOLDOUT_TOLERANCE_WH = 2.0
"""The model may be this much worse than the profile on the holdout and still be used."""

FEATURES = (
    "hour",
    "weekday",
    "day_type",
    "bridge",
    "doy_sin",
    "doy_cos",
    "temp_c",
    "away",
    "lag_week",
    "level_week",
)


class LoadMLCommonSettings(SettingsBaseModel):
    """Settings of the gradient boosting base load provider (RL29 fork)."""

    history_weeks: int = Field(
        default=52,
        ge=2,
        le=156,
        json_schema_extra={"description": "Training window [weeks].", "examples": [52]},
    )
    min_days: int = Field(
        default=21,
        ge=7,
        json_schema_extra={
            "description": "Healthy days needed before the model is used.",
            "examples": [21],
        },
    )
    holdout_days: int = Field(
        default=7,
        ge=1,
        le=28,
        json_schema_extra={
            "description": (
                "The model must beat the LoadMeasured profile on these last days "
                "(trained without them) to be used."
            ),
            "examples": [7],
        },
    )
    quantiles: list[float] = Field(
        default_factory=lambda: [0.1, 0.5, 0.9],
        json_schema_extra={
            "description": "Quantiles that are modelled.",
            "examples": [[0.1, 0.5, 0.9]],
        },
    )
    plan_quantile: float = Field(
        default=0.5,
        gt=0,
        lt=1,
        json_schema_extra={
            "description": (
                "Quantile EOS plans with (must be in `quantiles`). Above 0.5 plans with more "
                "load than expected, i.e. keeps reserve for uncertain hours."
            ),
            "examples": [0.5, 0.6],
        },
    )
    subtract_power_keys: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "description": (
                "Power measurements [W] of consumers EOS plans itself; their energy is "
                "removed from the load meter to get the base load."
            ),
            "examples": [["wbb12-electric-power", "waschmaschine-power"]],
        },
    )
    subtract_max_hold_hours: float = Field(
        default=24.0,
        gt=0,
        json_schema_extra={
            "description": (
                "A power value is held until the next sample, at most this long; hours "
                "without a held value are not used for training."
            ),
            "examples": [24.0],
        },
    )
    temperature_key: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Measured outdoor temperature for the history (the forecast uses "
                "`weather_temp_air`). None: feature from the weather prediction store only."
            ),
            "examples": ["wbb12-outdoor-temperature"],
        },
    )
    away_key: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "description": "Measurement 1 = away / holiday mode.",
            "examples": ["wbb12-away"],
        },
    )
    away_return_key: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "description": "Measurement with the return time (Unix seconds) of the away mode.",
            "examples": ["wbb12-return-ts"],
        },
    )
    state: str = Field(
        default="BW",
        json_schema_extra={
            "description": "German federal state for public holidays.",
            "examples": ["BW"],
        },
    )
    refit_hours: float = Field(
        default=24.0,
        ge=1,
        json_schema_extra={
            "description": "Models are retrained after this many hours.",
            "examples": [24],
        },
    )
    gap_threshold: float = Field(
        default=0.3,
        ge=0,
        lt=1,
        json_schema_extra={
            "description": "Days below this share of the median daily base load are left out.",
            "examples": [0.3],
        },
    )
    num_leaves: int = Field(
        default=15, ge=4, le=127, json_schema_extra={"description": "LightGBM num_leaves."}
    )
    n_estimators: int = Field(
        default=300, ge=20, le=3000, json_schema_extra={"description": "LightGBM trees."}
    )
    learning_rate: float = Field(
        default=0.05, gt=0, le=1, json_schema_extra={"description": "LightGBM learning rate."}
    )
    min_data_in_leaf: int = Field(
        default=20, ge=1, json_schema_extra={"description": "LightGBM min_data_in_leaf."}
    )

    @field_validator("state")
    @classmethod
    def _state(cls, value: str) -> str:
        value = value.upper()
        if value not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        return value

    @field_validator("quantiles")
    @classmethod
    def _quantiles(cls, value: list[float]) -> list[float]:
        if not value or any(not 0 < q < 1 for q in value):
            raise ValueError("quantiles must be within (0, 1)")
        return sorted(set(round(q, 4) for q in value))


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def hold_hourly_means(
    samples: Sequence[tuple[float, Optional[float]]],
    hour_starts: Sequence[float],
    max_hold_s: float,
) -> list[Optional[float]]:
    """Time-weighted hourly means of a step signal (value holds until the next sample).

    ``samples`` are (unix seconds, value) sorted by time; ``None`` ends the hold. An hour
    is ``None`` unless it is fully covered by held values.
    """
    result: list[Optional[float]] = []
    clean = [(t, v) for t, v in samples if t is not None]
    clean.sort(key=lambda s: s[0])
    idx = 0
    n = len(clean)
    for start in hour_starts:
        end = start + 3600.0
        # Last sample at or before the hour start.
        while idx + 1 < n and clean[idx + 1][0] <= start:
            idx += 1
        if n == 0 or clean[0][0] > start:
            result.append(None)
            continue
        covered = 0.0
        integral = 0.0
        ok = True
        j = idx
        t = start
        while t < end:
            sample_t, value = clean[j]
            next_t = clean[j + 1][0] if j + 1 < n else math.inf
            seg_end = min(end, next_t, sample_t + max_hold_s)
            if value is None or not math.isfinite(value) or seg_end <= t:
                ok = False
                break
            integral += value * (seg_end - t)
            covered += seg_end - t
            t = seg_end
            if t >= end:
                break
            if next_t <= t:
                j += 1
            else:
                ok = False  # hold expired before the next sample
                break
        result.append(integral / covered if ok and covered >= 3600.0 - 1e-6 else None)
    return result


def healthy_mask(
    hours: Sequence[datetime], values: Sequence[Optional[float]], threshold: float
) -> list[bool]:
    """False for hours of incomplete days and of days far below the median day."""
    days: dict[Any, list[float]] = {}
    for stamp, value in zip(hours, values):
        if value is not None and math.isfinite(value):
            days.setdefault(stamp.date(), []).append(value)
    complete = {d: sum(v) for d, v in days.items() if len(v) >= 22}
    if not complete:
        return [False] * len(hours)
    median = float(np.median(list(complete.values())))
    good = {d for d, total in complete.items() if total >= threshold * median}
    return [
        (stamp.date() in good) and value is not None and math.isfinite(value)
        for stamp, value in zip(hours, values)
    ]


def feature_rows(
    hours: Sequence[datetime],
    history: dict[datetime, float],
    temperature: dict[datetime, float],
    away: dict[datetime, float],
    state: str,
) -> np.ndarray:
    """Feature matrix (``FEATURES`` order) for local hour starts.

    ``history`` holds the known base load per local hour; lags that point to unknown
    hours become NaN (LightGBM handles missing values).
    """
    rows = np.full((len(hours), len(FEATURES)), np.nan, dtype=float)
    for i, stamp in enumerate(hours):
        day = stamp.date()
        doy = stamp.timetuple().tm_yday
        rows[i, 0] = stamp.hour
        rows[i, 1] = stamp.weekday()
        rows[i, 2] = day_type(day, state)
        rows[i, 3] = 1.0 if is_bridge_day(day, state) else 0.0
        rows[i, 4] = math.sin(2 * math.pi * doy / 365.25)
        rows[i, 5] = math.cos(2 * math.pi * doy / 365.25)
        rows[i, 6] = temperature.get(stamp, np.nan)
        rows[i, 7] = away.get(stamp, 0.0)
        rows[i, 8] = history.get(stamp - timedelta(days=7), np.nan)
        window = [
            history[stamp - timedelta(hours=h)]
            for h in range(48, 48 + 7 * 24)
            if (stamp - timedelta(hours=h)) in history
        ]
        rows[i, 9] = float(np.mean(window)) if len(window) >= 24 else np.nan
    return rows


@dataclass
class TrainedLoadModel:
    """Models per quantile plus the figures that decided whether they are used."""

    models: dict[float, Any]
    trained_at: float
    days: int
    holdout_mae_ml: Optional[float]
    holdout_mae_profile: Optional[float]
    max_wh: float
    use: bool
    importance: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "trained_at": datetime.fromtimestamp(self.trained_at, tz=timezone.utc).isoformat(),
            "days": self.days,
            "holdout_mae_ml_wh": self.holdout_mae_ml,
            "holdout_mae_profile_wh": self.holdout_mae_profile,
            "used": self.use,
            "max_wh": self.max_wh,
            "quantiles": sorted(self.models),
            "importance": self.importance,
        }


class _QuantileModel:
    """LightGBM booster with a ``predict`` and ``feature_importances_`` like sklearn."""

    def __init__(self, booster: Any):
        self.booster = booster
        self.feature_importances_ = booster.feature_importance(importance_type="gain")

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self.booster.predict(X), dtype=float)


def _fit_quantile(
    X: np.ndarray, y: np.ndarray, quantile: float, settings: LoadMLCommonSettings
) -> Any:
    """Train one quantile model with the native LightGBM API (no scikit-learn needed)."""
    import lightgbm as lgb

    params = {
        "objective": "quantile",
        "alpha": quantile,
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
    booster = lgb.train(params, dataset, num_boost_round=settings.n_estimators)
    return _QuantileModel(booster)


def train_load_model(
    hours: Sequence[datetime],
    base_wh: Sequence[Optional[float]],
    temperature: dict[datetime, float],
    away: dict[datetime, float],
    settings: LoadMLCommonSettings,
    now: Optional[float] = None,
) -> Optional[TrainedLoadModel]:
    """Train the quantile models; None when there are too few healthy days.

    The holdout comparison trains a median model without the last ``holdout_days`` and
    compares it with the ``LoadMeasured`` profile built from the same training days.
    """
    mask = healthy_mask(hours, base_wh, settings.gap_threshold)
    good_hours = [h for h, ok in zip(hours, mask) if ok]
    good_days = {h.date() for h in good_hours}
    if len(good_days) < settings.min_days:
        return None
    history = {h: float(v) for h, v, ok in zip(hours, base_wh, mask) if ok and v is not None}
    X_all = feature_rows(good_hours, history, temperature, away, settings.state)
    y_all = np.array([history[h] for h in good_hours], dtype=float)

    # Holdout: the last days, models trained only on the days before.
    last_day = max(good_days)
    cut = last_day - timedelta(days=settings.holdout_days - 1)
    train_idx = [i for i, h in enumerate(good_hours) if h.date() < cut]
    test_idx = [i for i, h in enumerate(good_hours) if h.date() >= cut]
    mae_ml = mae_profile = None
    if (
        len({good_hours[i].date() for i in train_idx})
        >= max(7, settings.min_days - settings.holdout_days)
        and test_idx
    ):
        median_model = _fit_quantile(X_all[train_idx], y_all[train_idx], 0.5, settings)
        predicted = np.clip(median_model.predict(X_all[test_idx]), 0, None)
        mae_ml = float(np.mean(np.abs(predicted - y_all[test_idx])))
        profile_settings = LoadMeasuredCommonSettings(
            min_days=min(7, settings.min_days), gap_threshold=settings.gap_threshold
        )
        profile = build_profile([(good_hours[i], y_all[i]) for i in train_idx], profile_settings)
        if profile is not None:
            reference = np.array(
                [
                    profile_value(profile, good_hours[i].weekday(), good_hours[i].hour)
                    for i in test_idx
                ]
            )
            mae_profile = float(np.mean(np.abs(reference - y_all[test_idx])))

    models = {q: _fit_quantile(X_all, y_all, q, settings) for q in settings.quantiles}
    median = models.get(0.5) or models[min(models, key=lambda q: abs(q - 0.5))]
    gains = getattr(median, "feature_importances_", None)
    importance = {}
    if gains is not None and float(np.sum(gains)) > 0:
        total = float(np.sum(gains))
        importance = {name: round(float(g) / total, 3) for name, g in zip(FEATURES, gains)}
    # Not worse than the profile (a few Wh tolerance for an exactly repeating profile).
    use = mae_ml is not None and (
        mae_profile is None or mae_ml <= mae_profile * 1.02 + HOLDOUT_TOLERANCE_WH
    )
    return TrainedLoadModel(
        models=models,
        trained_at=now if now is not None else time.time(),
        days=len(good_days),
        holdout_mae_ml=round(mae_ml, 1) if mae_ml is not None else None,
        holdout_mae_profile=round(mae_profile, 1) if mae_profile is not None else None,
        max_wh=float(np.max(y_all)),
        use=use,
        importance=importance,
    )


def predict_load(
    model: TrainedLoadModel,
    hours: Sequence[datetime],
    history: dict[datetime, float],
    temperature: dict[datetime, float],
    away: dict[datetime, float],
    state: str,
) -> dict[float, list[float]]:
    """Forecast [Wh] per quantile for local hour starts; monotone across quantiles."""
    X = feature_rows(hours, history, temperature, away, state)
    quantiles = sorted(model.models)
    raw = np.vstack([np.clip(model.models[q].predict(X), 0, model.max_wh * 1.5) for q in quantiles])
    raw = np.maximum.accumulate(raw, axis=0)  # no quantile crossing
    return {q: raw[i].tolist() for i, q in enumerate(quantiles)}


def quantile_key(quantile: float) -> str:
    return f"loadforecast_p{int(round(quantile * 100)):02d}_power_w"


# --------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------


class LoadMLDataRecord(LoadAkkudoktorDataRecord):
    """Load record with the base load quantiles of ``LoadML``."""

    loadforecast_p10_power_w: Optional[float] = Field(
        default=None, json_schema_extra={"description": "Base load 10 % quantile (W)."}
    )
    loadforecast_p50_power_w: Optional[float] = Field(
        default=None, json_schema_extra={"description": "Base load median (W)."}
    )
    loadforecast_p90_power_w: Optional[float] = Field(
        default=None, json_schema_extra={"description": "Base load 90 % quantile (W)."}
    )


class LoadML(PredictionMixin, LoadMeasured):
    """Gradient boosting base load forecast; LoadMeasured until it is good enough."""

    records: list[LoadMLDataRecord] = Field(  # type: ignore[assignment]
        default_factory=list,
        json_schema_extra={"description": "List of LoadMLDataRecord records"},
    )

    _model: Optional[TrainedLoadModel] = None

    @classmethod
    def provider_id(cls) -> str:
        return "LoadML"

    @property
    def _ml(self) -> LoadMLCommonSettings:
        return self.config.load.loadml

    def _summary_path(self) -> Path:
        return Path(self.config.general.data_folder_path) / "rl29" / "load_ml.json"

    async def _hourly_history(self) -> tuple[list[datetime], list[Optional[float]], dict, dict]:
        """Local hour starts, base load Wh, temperature and away per hour."""
        settings = self._ml
        tz = self.config.general.timezone
        end = self.ems_start_datetime.start_of("hour")
        start = end.subtract(weeks=settings.history_weeks)
        min_dt = await self.measurement.min_datetime()
        if min_dt is None:
            return [], [], {}, {}
        min_local = to_datetime(min_dt, in_timezone=tz).start_of("day").add(days=1)
        if min_local > start:
            start = min_local
        if not start < end:
            return [], [], {}, {}
        kwh = await self.measurement.load_total_kwh(
            start_datetime=start, end_datetime=end, interval=to_duration("1 hour")
        )
        hours_local = [start.in_timezone(tz).add(hours=i) for i in range(len(kwh))]
        hour_ts = [h.timestamp() for h in hours_local]
        base: list[Optional[float]] = [float(v) * 1000.0 for v in kwh]
        max_hold = settings.subtract_max_hold_hours * 3600.0

        async def samples(key: str) -> list[tuple[float, Optional[float]]]:
            dates, values = await self.measurement.key_to_lists(
                key=key,
                start_datetime=start.subtract(seconds=max_hold),
                end_datetime=end,
                dropna=False,
            )
            return [(d.timestamp(), None if v is None else float(v)) for d, v in zip(dates, values)]

        for key in settings.subtract_power_keys:
            means = hold_hourly_means(await samples(key), hour_ts, max_hold)
            for i, mean in enumerate(means):
                current = base[i]
                if current is None or mean is None:
                    base[i] = None
                else:
                    value = current - mean  # W over one hour = Wh
                    base[i] = value if value >= 0 else None
        naive = [_naive(h) for h in hours_local]
        temperature: dict[datetime, float] = {}
        if settings.temperature_key:
            for h, v in zip(
                naive,
                hold_hourly_means(await samples(settings.temperature_key), hour_ts, 6 * 3600.0),
            ):
                if v is not None:
                    temperature[h] = v
        away: dict[datetime, float] = {}
        if settings.away_key:
            for h, v in zip(
                naive, hold_hourly_means(await samples(settings.away_key), hour_ts, 30 * 86400.0)
            ):
                if v is not None:
                    away[h] = 1.0 if v >= 0.5 else 0.0
        return naive, base, temperature, away

    async def _future_inputs(self, hours_local: list[Any]) -> tuple[dict, dict]:
        """Forecast temperature and the away flag for the forecast hours."""
        settings = self._ml
        temperature: dict[datetime, float] = {}
        try:
            start, end = hours_local[0], hours_local[-1].add(hours=1)
            values = await self.prediction.key_to_array(
                key="weather_temp_air",
                start_datetime=start,
                end_datetime=end,
                interval=to_duration("1 hour"),
            )
            for h, v in zip(hours_local, values):
                if v is not None and math.isfinite(float(v)):
                    temperature[_naive(h)] = float(v)
        except Exception as exc:
            logger.debug("LoadML: no temperature forecast: {}", exc)
        away: dict[datetime, float] = {}
        if settings.away_key:
            now = self.ems_start_datetime
            current = await self._latest(settings.away_key, now)
            returns = (
                await self._latest(settings.away_return_key, now)
                if settings.away_return_key
                else None
            )
            if current is not None and current >= 0.5:
                for h in hours_local:
                    if returns is None or returns <= 0 or h.timestamp() < returns:
                        away[_naive(h)] = 1.0
        return temperature, away

    async def _latest(self, key: Optional[str], now: Any) -> Optional[float]:
        if not key:
            return None
        try:
            dates, values = await self.measurement.key_to_lists(
                key=key,
                start_datetime=now.subtract(days=60),
                end_datetime=now.add(seconds=1),
                dropna=True,
            )
        except Exception:
            return None
        if not values:
            return None
        value = values[-1]
        return float(value) if value is not None and math.isfinite(float(value)) else None

    async def _update_data(self, force_update: Optional[bool] = False) -> None:
        settings = self._ml
        model = self._model
        hours: list[datetime] = []
        base: list[Optional[float]] = []
        temperature: dict[datetime, float] = {}
        away: dict[datetime, float] = {}
        try:
            hours, base, temperature, away = await self._hourly_history()
            stale = model is None or (time.time() - model.trained_at) > settings.refit_hours * 3600
            if stale or force_update:
                model = train_load_model(hours, base, temperature, away, settings)
                self._model = model
                self._write_summary(model)
        except ImportError as exc:
            logger.warning("LoadML: LightGBM unavailable ({}); using LoadMeasured.", exc)
            model = None
        except Exception as exc:  # never lose the forecast because of the model
            logger.warning("LoadML: training failed ({}); using LoadMeasured.", exc)
            model = None
        if model is None or not model.use:
            if model is not None:
                logger.info(
                    "LoadML: model (holdout MAE {} Wh) does not beat the profile ({} Wh); using LoadMeasured.",
                    model.holdout_mae_ml,
                    model.holdout_mae_profile,
                )
            await super()._update_data(force_update)
            return

        tz = self.config.general.timezone
        data_year_energy = self.load_data()
        first = self.ems_start_datetime.start_of("day")
        count = int(
            math.ceil(
                (
                    self.ems_start_datetime.add(hours=self.config.prediction.hours) - first
                ).total_seconds()
                / 3600
            )
        )
        future = [first.add(hours=i) for i in range(count)]
        future_local = [h.in_timezone(tz) for h in future]
        f_temp, f_away = await self._future_inputs(future_local)
        temperature.update(f_temp)
        if temperature and settings.temperature_key:
            # No weather forecast for an hour: persistence of the last measured value.
            last_known = temperature[max(temperature)]
            for h in future_local:
                temperature.setdefault(_naive(h), last_known)
        away.update(f_away)
        history = {h: v for h, v in zip(hours, base) if v is not None}
        forecast = predict_load(
            model, [_naive(h) for h in future_local], history, temperature, away, settings.state
        )
        plan_q = min(forecast, key=lambda q: abs(q - settings.plan_quantile))
        for i, date in enumerate(future):
            hourly_stats = data_year_energy[date.day_of_year - 1, :, date.hour]
            values: dict[str, Any] = {
                "loadakkudoktor_mean_power_w": hourly_stats[0],
                "loadakkudoktor_std_power_w": hourly_stats[1],
                "loadforecast_power_w": forecast[plan_q][i],
            }
            for q, series in forecast.items():
                key = quantile_key(q)
                if key in LoadMLDataRecord.model_fields:
                    values[key] = series[i]
            await self.update_value(date, values)
        logger.info(
            "LoadML: base load forecast from {} days (holdout MAE {} Wh vs. profile {} Wh), "
            "planning with the {:.0%} quantile, {:.1f} kWh/day on average.",
            model.days,
            model.holdout_mae_ml,
            model.holdout_mae_profile,
            plan_q,
            float(np.sum(forecast[plan_q])) / max(len(future) / 24.0, 1e-9) / 1000.0,
        )
        self.update_datetime = to_datetime(in_timezone=tz)

    def _write_summary(self, model: Optional[TrainedLoadModel]) -> None:
        try:
            path = self._summary_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = model.summary() if model else {"used": False, "reason": "too few healthy days"}
            path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.debug("LoadML: summary not written: {}", exc)


def _naive(stamp: Any) -> datetime:
    """Local wall-clock hour as naive datetime (dict key, independent of tz objects)."""
    return datetime(stamp.year, stamp.month, stamp.day, stamp.hour)
