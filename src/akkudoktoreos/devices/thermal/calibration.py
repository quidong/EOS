"""Calibration of the heat pump model from measured operation (RL29 fork).

Same idea as the PV calibration: the physical model keeps its structure, the
measurements correct its systematic errors, and the correction is refitted
over a rolling window so it follows the plant. Three parameter groups are
fitted from hourly means of the measurements:

1. **Building** (``loss_w_per_k``, ``solar_aperture_m2``, ``internal_gains_w``):
   a daily energy balance ``Q_space = H * (T_room - T_out) - A * G - g`` with
   the global irradiance ``G`` as second regressor. Without the solar term the
   gains of sunny days end up in the intercept and the loss slope comes out too
   small, and a solar aperture set by hand would then count the sun twice.
   The regression is shrunk towards the configured values (ridge with the
   configured parameters as prior), so a handful of mild autumn days cannot
   send the parameters anywhere. Days with little heating although the model
   expects some (heat pump off, manual operation) are excluded the same way the
   PV calibration excludes outage days; days with a drifting room temperature
   (storage effects) are excluded too.
2. **COP scale**: the energy-weighted ratio of measured heat to the heat the
   rated COP curve predicts from the electric energy, with the standby power
   taken out of the electric energy (the model adds standby separately) and
   only hours in which the heat pump ran most of the time (hourly means of the
   flow temperature are too low in hours with cycling).
3. **Tank loss** (``dhw.loss_w_per_k``): the exponential night decay of the tank
   temperature without charging and tapping.

Every fit reports what it used and how much it changed, and every result is
clipped to a plausible band around the configured value. The module has no
EOS dependencies; the EOS integration lives in
``akkudoktoreos.devices.heatpumpcalibration`` and the manual tool in
``scripts/rl29/calibrate_heatpump.py``.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Optional, Sequence

import numpy as np

WATER_WH_PER_LITRE_K = 1.163

SAMPLE_FIELDS = (
    "outdoor_c",
    "room_c",
    "heat_w",
    "electric_w",
    "flow_c",
    "dhw_c",
    "dhw_heat_w",
    "ghi_w_per_m2",
)


@dataclass
class HourlySample:
    """Hourly means of the measurements of one heat pump."""

    timestamp: datetime
    outdoor_c: Optional[float] = None
    room_c: Optional[float] = None
    heat_w: Optional[float] = None
    """Thermal output of the heat pump, space heating plus hot water [W]."""
    electric_w: Optional[float] = None
    """Electric power of the heat pump including standby [W]."""
    flow_c: Optional[float] = None
    dhw_c: Optional[float] = None
    dhw_heat_w: Optional[float] = None
    """Thermal output into the hot water tank [W], if measured."""
    ghi_w_per_m2: Optional[float] = None

    def get(self, key: str) -> Optional[float]:
        value = getattr(self, key, None)
        if value is None:
            return None
        value = float(value)
        return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# Sample preparation
# ---------------------------------------------------------------------------


def hourly_means(
    samples: Sequence[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    min_coverage_seconds: float = 1800.0,
) -> dict[datetime, float]:
    """Time-weighted hourly means of a step signal (a value holds until the next sample).

    Hours with less than ``min_coverage_seconds`` of known signal are left out.
    """
    ordered = sorted((t, float(v)) for t, v in samples if v is not None and math.isfinite(float(v)))
    result: dict[datetime, float] = {}
    if not ordered:
        return result
    hour = start.replace(minute=0, second=0, microsecond=0)
    index = 0
    while hour < end:
        hour_end = hour + timedelta(hours=1)
        while index + 1 < len(ordered) and ordered[index + 1][0] <= hour:
            index += 1
        if ordered[index][0] > hour_end:
            hour = hour_end
            continue
        weighted = 0.0
        covered = 0.0
        j = index
        while j < len(ordered) and ordered[j][0] < hour_end:
            seg_start = max(hour, ordered[j][0])
            seg_end = min(hour_end, ordered[j + 1][0] if j + 1 < len(ordered) else hour_end)
            if seg_end > seg_start:
                seconds = (seg_end - seg_start).total_seconds()
                weighted += ordered[j][1] * seconds
                covered += seconds
            j += 1
        if covered > 0 and covered >= min_coverage_seconds:
            result[hour] = weighted / covered
        hour = hour_end
    return result


def build_samples(columns: dict[str, dict[datetime, float]]) -> list[HourlySample]:
    """Hourly samples from per-column hourly means (``hourly_means`` output)."""
    stamps = sorted({t for values in columns.values() for t in values})
    samples = []
    for stamp in stamps:
        values = {key: columns.get(key, {}).get(stamp) for key in SAMPLE_FIELDS}
        samples.append(HourlySample(stamp, **values))
    return samples


def estimate_dhw_heat(
    samples: Sequence[HourlySample], volume_l: float, min_rise_k: float = 0.5
) -> list[HourlySample]:
    """Fill ``dhw_heat_w`` from tank temperature rises where it is not measured.

    A rise of the hourly mean tank temperature by more than ``min_rise_k`` is
    taken as a charge of ``capacity * rise`` within that hour. Tapping hides
    part of a charge, so this is a lower bound; it still keeps most of the hot
    water energy out of the space heating balance.
    """
    capacity = volume_l * WATER_WH_PER_LITRE_K
    result = [replace(sample) for sample in samples]
    previous: Optional[HourlySample] = None
    for sample in result:
        if sample.get("dhw_heat_w") is None and previous is not None:
            now_c, before_c = sample.get("dhw_c"), previous.get("dhw_c")
            if (
                now_c is not None
                and before_c is not None
                and (sample.timestamp - previous.timestamp) <= timedelta(hours=1, minutes=1)
            ):
                rise = now_c - before_c
                sample.dhw_heat_w = capacity * rise if rise > min_rise_k else 0.0
        previous = sample
    return result


# ---------------------------------------------------------------------------
# Priors and options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationPriors:
    """Configured values the fits are shrunk towards and bounded around."""

    loss_w_per_k: float
    internal_gains_w: float = 0.0
    solar_aperture_m2: float = 0.0
    cop_scale: float = 1.0
    dhw_loss_w_per_k: float = 1.5
    standby_w: float = 22.0
    dhw_volume_l: float = 190.0
    dhw_ambient_c: float = 18.0


@dataclass(frozen=True)
class CalibrationOptions:
    """Tuning of the fits; the defaults suit a dwelling with underfloor heating."""

    min_hours_per_day: int = 20
    """A day needs hourly means for this many hours to count."""
    min_days: int = 5
    """Minimum usable days for the building fit."""
    min_delta_range_k: float = 2.0
    """Minimum spread of the daily temperature difference across the days."""
    max_room_drift_k: float = 1.0
    """Days whose room temperature changes more than this are left out."""
    prior_weight_days: float = 7.0
    """Shrinkage of the building fit: the prior weighs like this many days."""
    outage_threshold: float = 0.4
    """A day delivering less than this share of the healthy heat per K is left out."""
    min_healthy_days: int = 3
    """Outage filtering needs at least this many candidate days."""
    loss_min_factor: float = 0.3
    loss_max_factor: float = 3.0
    """Band of the fitted loss coefficient around the configured value."""
    max_aperture_m2: float = 50.0
    max_internal_gains_w: float = 3000.0
    cop_min_heat_w: float = 3000.0
    """COP hours need at least this mean heat [W]: the lowest modulation of the
    compressor, so that the heat pump ran the whole hour and the hourly mean of
    the flow temperature is the operating temperature (WBB 12: 3 kW)."""
    cop_min_electric_w: float = 100.0
    cop_max_dhw_share: float = 0.1
    cop_prior_hours: float = 24.0
    """Shrinkage of the COP scale: the prior weighs like this many hours."""
    cop_min_scale: float = 0.5
    cop_max_scale: float = 1.5
    dhw_night_hours: tuple[int, int] = (0, 5)
    """Local hours (inclusive) in which the tank decay is evaluated."""
    dhw_min_drop_k: float = 0.2
    dhw_min_nights: int = 3
    """Minimum number of undisturbed nights for the tank loss fit."""
    dhw_prior_nights: float = 3.0
    dhw_min_loss_w_per_k: float = 0.1
    dhw_max_loss_w_per_k: float = 20.0


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class BuildingFit:
    loss_w_per_k: float
    solar_aperture_m2: float
    internal_gains_w: float
    days: int
    """Days used for the regression."""
    excluded_outage_days: int = 0
    excluded_drift_days: int = 0
    solar_fitted: bool = False
    """False when no irradiance data existed; the aperture is then the prior."""
    r2: float = 0.0
    mae_prior_w: float = 0.0
    """Mean absolute daily residual of the configured parameters."""
    mae_fit_w: float = 0.0
    """Mean absolute daily residual of the fitted parameters."""


@dataclass
class CopFit:
    cop_scale: float
    hours: int
    measured_cop: float
    """Energy-weighted measured COP (net of standby) in the used hours."""
    model_cop: float
    """Energy-weighted rated-curve COP in the same hours (scale 1)."""
    raw_scale: float
    """Ratio before shrinkage and clipping."""


@dataclass
class DhwFit:
    loss_w_per_k: float
    nights: int
    raw_loss_w_per_k: float


@dataclass
class CalibrationResult:
    fitted_at: datetime
    window_days: int = 0
    samples: int = 0
    building: Optional[BuildingFit] = None
    cop: Optional[CopFit] = None
    dhw: Optional[DhwFit] = None
    notes: list[str] = field(default_factory=list)
    """Why a fit was skipped, for the log."""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fitted_at"] = self.fitted_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationResult":
        return cls(
            fitted_at=datetime.fromisoformat(data["fitted_at"]),
            window_days=int(data.get("window_days", 0)),
            samples=int(data.get("samples", 0)),
            building=BuildingFit(**data["building"]) if data.get("building") else None,
            cop=CopFit(**data["cop"]) if data.get("cop") else None,
            dhw=DhwFit(**data["dhw"]) if data.get("dhw") else None,
            notes=list(data.get("notes") or []),
        )

    def summary(self, priors: CalibrationPriors) -> str:
        """One line for the log, prior -> fitted."""
        parts = []
        if self.building:
            b = self.building
            parts.append(
                f"loss {priors.loss_w_per_k:.0f} -> {b.loss_w_per_k:.0f} W/K, "
                f"aperture {priors.solar_aperture_m2:.1f} -> {b.solar_aperture_m2:.1f} m2, "
                f"gains {priors.internal_gains_w:.0f} -> {b.internal_gains_w:.0f} W "
                f"({b.days} days, MAE {b.mae_prior_w:.0f} -> {b.mae_fit_w:.0f} W)"
            )
        if self.cop:
            c = self.cop
            parts.append(
                f"COP scale {priors.cop_scale:.3f} -> {c.cop_scale:.3f} "
                f"({c.hours} h, measured {c.measured_cop:.2f} vs rated {c.model_cop:.2f})"
            )
        if self.dhw:
            d = self.dhw
            parts.append(
                f"tank loss {priors.dhw_loss_w_per_k:.2f} -> {d.loss_w_per_k:.2f} W/K "
                f"({d.nights} nights)"
            )
        parts.extend(self.notes)
        return "; ".join(parts) if parts else "nothing fitted"


# ---------------------------------------------------------------------------
# Building fit
# ---------------------------------------------------------------------------


@dataclass
class _Day:
    delta_k: float
    heat_w: float
    ghi: Optional[float]
    room_drift_k: float


def _local_day(stamp: datetime, tz: Optional[tzinfo]) -> date:
    local = stamp.astimezone(tz) if tz is not None else stamp
    return local.date()


def _daily_aggregates(
    samples: Sequence[HourlySample], options: CalibrationOptions, tz: Optional[tzinfo]
) -> list[_Day]:
    days: dict[Any, list[HourlySample]] = defaultdict(list)
    for sample in samples:
        days[_local_day(sample.timestamp, tz)].append(sample)
    result = []
    for day_samples in days.values():
        usable = [
            s
            for s in day_samples
            if None not in (s.get("outdoor_c"), s.get("room_c"), s.get("heat_w"))
        ]
        if len(usable) < options.min_hours_per_day:
            continue
        usable.sort(key=lambda s: s.timestamp)
        delta = statistics.fmean(s.get("room_c") - s.get("outdoor_c") for s in usable)  # type: ignore[operator]
        heat = statistics.fmean(
            max(0.0, (s.get("heat_w") or 0.0) - (s.get("dhw_heat_w") or 0.0)) for s in usable
        )
        ghis = [s.get("ghi_w_per_m2") for s in usable]
        ghi = (
            statistics.fmean(g for g in ghis if g is not None)
            if all(g is not None for g in ghis)
            else None
        )
        drift = (usable[-1].get("room_c") or 0.0) - (usable[0].get("room_c") or 0.0)
        result.append(_Day(delta, heat, ghi, drift))
    return result


def fit_building(
    samples: Sequence[HourlySample],
    priors: CalibrationPriors,
    options: CalibrationOptions = CalibrationOptions(),
    tz: Optional[tzinfo] = None,
) -> tuple[Optional[BuildingFit], str]:
    """Ridge regression of the daily space heat on temperature difference and irradiance.

    Returns the fit and a note; the fit is ``None`` when the data does not
    support one (the note says why).
    """
    days = _daily_aggregates(samples, options, tz)
    if not days:
        return None, "building: no complete days"
    kept = [d for d in days if abs(d.room_drift_k) <= options.max_room_drift_k]
    drift_excluded = len(days) - len(kept)

    # Outage filter: days that deliver far less heat per K than the healthy days
    # although the configured model expects heating (heat pump off, manual mode).
    outage_excluded = 0
    if len(kept) >= options.min_healthy_days:
        ratios = [(d.heat_w + priors.internal_gains_w) / max(d.delta_k, 1.0) for d in kept]
        reference = float(np.quantile(ratios, 0.75))
        limit = reference * options.outage_threshold
        healthy = []
        for day, ratio in zip(kept, ratios):
            expected = (
                priors.loss_w_per_k * day.delta_k
                - priors.solar_aperture_m2 * (day.ghi or 0.0)
                - priors.internal_gains_w
            )
            if ratio < limit and expected > 500.0:
                outage_excluded += 1
                continue
            healthy.append(day)
        kept = healthy

    if len(kept) < options.min_days:
        return None, (
            f"building: only {len(kept)} usable days (need {options.min_days}; "
            f"{outage_excluded} outage, {drift_excluded} drift excluded)"
        )

    def spread_ok(selection: list[_Day]) -> bool:
        deltas = [d.delta_k for d in selection]
        return max(deltas) - min(deltas) >= options.min_delta_range_k

    if not spread_ok(kept):
        deltas = [d.delta_k for d in kept]
        return None, (
            f"building: temperature difference spans only "
            f"{max(deltas) - min(deltas):.1f} K over {len(kept)} days"
        )

    # The solar term is fitted on the days that have irradiance data, as soon as
    # there are enough of them (the weather fill builds this history up over
    # time); until then all days are used without the solar term.
    with_ghi = [d for d in kept if d.ghi is not None]
    solar_fitted = (
        len(with_ghi) >= options.min_days
        and any(d.ghi > 5.0 for d in with_ghi)  # type: ignore[operator]
        and spread_ok(with_ghi)
    )
    if solar_fitted:
        kept = with_ghi
    # Model: heat = H * delta - A * ghi - g. Columns: delta, -ghi, -1.
    x_rows = []
    y_rows = []
    for d in kept:
        ghi = d.ghi or 0.0
        x_rows.append([d.delta_k, -ghi if solar_fitted else 0.0, -1.0])
        # Without irradiance data the prior aperture keeps acting as a fixed term.
        y_rows.append(d.heat_w + (0.0 if solar_fitted else priors.solar_aperture_m2 * ghi))
    x = np.asarray(x_rows, dtype=float)
    y = np.asarray(y_rows, dtype=float)
    prior = np.asarray(
        [priors.loss_w_per_k, priors.solar_aperture_m2, priors.internal_gains_w], dtype=float
    )

    # Ridge towards the prior as pseudo-observations. The weights express how
    # many days of evidence it takes to move a parameter by its prior spread.
    sigma_y = 200.0  # typical daily mean residual [W]
    spread = np.asarray(
        [
            max(0.5 * priors.loss_w_per_k, 10.0),
            max(priors.solar_aperture_m2, 5.0),
            max(0.5 * priors.internal_gains_w, 300.0),
        ]
    )
    weights = options.prior_weight_days * (sigma_y / spread) ** 2
    if not solar_fitted:
        weights[1] = 1e9  # pin the aperture to the prior
    x_aug = np.vstack([x, np.diag(np.sqrt(weights))])
    y_aug = np.concatenate([y, np.sqrt(weights) * prior])
    beta, *_ = np.linalg.lstsq(x_aug, y_aug, rcond=None)
    loss = float(
        np.clip(
            beta[0],
            priors.loss_w_per_k * options.loss_min_factor,
            priors.loss_w_per_k * options.loss_max_factor,
        )
    )
    aperture = (
        float(np.clip(beta[1], 0.0, options.max_aperture_m2))
        if solar_fitted
        else priors.solar_aperture_m2
    )
    gains = float(np.clip(beta[2], 0.0, options.max_internal_gains_w))

    fitted = x @ np.asarray([loss, aperture if solar_fitted else 0.0, gains])
    predicted_prior = x @ np.asarray([prior[0], prior[1] if solar_fitted else 0.0, prior[2]])
    residual = y - fitted
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1e-12
    return (
        BuildingFit(
            loss_w_per_k=round(loss, 1),
            solar_aperture_m2=round(aperture, 2),
            internal_gains_w=round(gains, 0),
            days=len(kept),
            excluded_outage_days=outage_excluded,
            excluded_drift_days=drift_excluded,
            solar_fitted=solar_fitted,
            r2=round(1.0 - float(np.sum(residual**2)) / ss_tot, 3),
            mae_prior_w=round(float(np.mean(np.abs(y - predicted_prior))), 1),
            mae_fit_w=round(float(np.mean(np.abs(residual))), 1),
        ),
        "",
    )


# ---------------------------------------------------------------------------
# COP fit
# ---------------------------------------------------------------------------


def fit_cop_scale(
    samples: Sequence[HourlySample],
    cop_model: Any,
    priors: CalibrationPriors,
    options: CalibrationOptions = CalibrationOptions(),
) -> tuple[Optional[CopFit], str]:
    """Energy-weighted ratio of measured to rated COP in space heating hours.

    ``cop_model`` is a ``CopModel``; its own scale is divided out so the result
    is the scale to configure, not a correction on top.
    """
    heat_sum = 0.0
    model_heat_sum = 0.0
    electric_sum = 0.0
    hours = 0
    for sample in samples:
        heat, electric = sample.get("heat_w"), sample.get("electric_w")
        outdoor, flow = sample.get("outdoor_c"), sample.get("flow_c")
        if heat is None or electric is None or outdoor is None or flow is None:
            continue
        if heat < options.cop_min_heat_w:
            continue
        dhw_heat = sample.get("dhw_heat_w") or 0.0
        if dhw_heat > options.cop_max_dhw_share * heat:
            continue
        electric_net = electric - priors.standby_w
        if electric_net < options.cop_min_electric_w:
            continue
        space_share = max(0.0, heat - dhw_heat) / heat
        heat_space = heat - dhw_heat
        electric_space = electric_net * space_share
        rated = cop_model.cop(outdoor, flow) / cop_model.scale
        heat_sum += heat_space
        electric_sum += electric_space
        model_heat_sum += rated * electric_space
        hours += 1
    if hours < 5 or model_heat_sum <= 0 or electric_sum <= 0:
        return None, f"cop: only {hours} space heating hours above {options.cop_min_heat_w:.0f} W"
    raw = heat_sum / model_heat_sum
    # Shrink towards the prior with the weight of ``cop_prior_hours`` average hours.
    prior_weight = options.cop_prior_hours * model_heat_sum / hours
    scale = (heat_sum + prior_weight * priors.cop_scale) / (model_heat_sum + prior_weight)
    scale = float(np.clip(scale, options.cop_min_scale, options.cop_max_scale))
    return (
        CopFit(
            cop_scale=round(scale, 3),
            hours=hours,
            measured_cop=round(heat_sum / electric_sum, 2),
            model_cop=round(model_heat_sum / electric_sum, 2),
            raw_scale=round(raw, 3),
        ),
        "",
    )


# ---------------------------------------------------------------------------
# Tank loss fit
# ---------------------------------------------------------------------------


def fit_dhw_loss(
    samples: Sequence[HourlySample],
    priors: CalibrationPriors,
    options: CalibrationOptions = CalibrationOptions(),
    tz: Optional[tzinfo] = None,
) -> tuple[Optional[DhwFit], str]:
    """Tank loss from the night decay: dT/dt = -UA/C (T - T_amb)."""
    capacity = priors.dhw_volume_l * WATER_WH_PER_LITRE_K
    first, last = options.dhw_night_hours
    by_night: dict[Any, list[HourlySample]] = defaultdict(list)
    for sample in samples:
        local = sample.timestamp.astimezone(tz) if tz is not None else sample.timestamp
        if first <= local.hour <= last:
            by_night[local.date()].append(sample)
    rates: list[float] = []
    for night in by_night.values():
        series = [(s.timestamp, s.get("dhw_c")) for s in night if s.get("dhw_c") is not None]
        if len(series) < 4:
            continue
        if any((s.get("dhw_heat_w") or 0.0) > 50 for s in night):
            continue
        temps = [t for _, t in series]
        if any(b > a + 0.05 for a, b in zip(temps, temps[1:])):  # type: ignore[operator]
            continue  # reheated
        if temps[0] - temps[-1] < options.dhw_min_drop_k:  # type: ignore[operator]
            continue
        span_h = (series[-1][0] - series[0][0]).total_seconds() / 3600
        if span_h <= 0:
            continue
        if temps[-1] - priors.dhw_ambient_c <= 0 or temps[0] - priors.dhw_ambient_c <= 0:  # type: ignore[operator]
            continue
        rates.append(
            math.log(
                (temps[0] - priors.dhw_ambient_c) / (temps[-1] - priors.dhw_ambient_c)  # type: ignore[operator]
            )
            / span_h
        )
    if len(rates) < options.dhw_min_nights:
        return None, (f"dhw: only {len(rates)} undisturbed night(s), need {options.dhw_min_nights}")
    raw = statistics.median(rates) * capacity
    n = len(rates)
    loss = (n * raw + options.dhw_prior_nights * priors.dhw_loss_w_per_k) / (
        n + options.dhw_prior_nights
    )
    loss = float(np.clip(loss, options.dhw_min_loss_w_per_k, options.dhw_max_loss_w_per_k))
    return DhwFit(loss_w_per_k=round(loss, 2), nights=n, raw_loss_w_per_k=round(raw, 2)), ""


# ---------------------------------------------------------------------------
# All together
# ---------------------------------------------------------------------------


def calibrate(
    samples: Sequence[HourlySample],
    priors: CalibrationPriors,
    cop_model: Any,
    options: CalibrationOptions = CalibrationOptions(),
    tz: Optional[tzinfo] = None,
    window_days: int = 0,
    now: Optional[datetime] = None,
    fit_building_enabled: bool = True,
    fit_cop_enabled: bool = True,
    fit_dhw_enabled: bool = True,
) -> CalibrationResult:
    """Run the enabled fits on hourly samples and collect the result."""
    samples = estimate_dhw_heat(samples, priors.dhw_volume_l)
    result = CalibrationResult(
        fitted_at=now or datetime.now(timezone.utc),
        window_days=window_days,
        samples=len(samples),
    )
    if fit_building_enabled:
        result.building, note = fit_building(samples, priors, options, tz)
        if note:
            result.notes.append(note)
    if fit_cop_enabled:
        result.cop, note = fit_cop_scale(samples, cop_model, priors, options)
        if note:
            result.notes.append(note)
    if fit_dhw_enabled:
        result.dhw, note = fit_dhw_loss(samples, priors, options, tz)
        if note:
            result.notes.append(note)
    return result
