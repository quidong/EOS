"""Heat pump calibration fits (eos-plus fork): parameters are recovered from synthetic data.

The synthetic plant has known building losses, solar aperture, internal gains,
COP scale and tank losses. The tests check that the fits recover them, that
the biases of the first calibration script are gone (standby power and cycling
hours in the COP fit, solar gains in the building fit), that outage days and
drifting days are excluded, and that thin data falls back to the prior.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from akkudoktoreos.devices.thermal.calibration import (
    WATER_WH_PER_LITRE_K,
    CalibrationOptions,
    CalibrationPriors,
    CalibrationResult,
    HourlySample,
    build_samples,
    calibrate,
    estimate_dhw_heat,
    fit_building,
    fit_cop_scale,
    fit_dhw_loss,
    fit_emitter,
    hourly_means,
)
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel

H_TRUE = 180.0
GAINS_TRUE = 300.0
APERTURE_TRUE = 4.0
SCALE_TRUE = 0.8
UA_TRUE = 1.6
STANDBY = 22.0
VOLUME = 190.0
AMBIENT = 18.0
TZ = ZoneInfo("Europe/Berlin")

PRIORS = CalibrationPriors(
    loss_w_per_k=150.0,
    internal_gains_w=0.0,
    solar_aperture_m2=0.0,
    cop_scale=1.0,
    dhw_loss_w_per_k=1.5,
    standby_w=STANDBY,
    dhw_volume_l=VOLUME,
    dhw_ambient_c=AMBIENT,
)


def synthetic(days: int = 14, *, solar: bool = True, outage_days=(), cycling: bool = False):
    """Hourly samples of a plant with the true parameters."""
    cop = CopModel(WBB12_RATING_POINTS, scale=SCALE_TRUE)
    capacity = VOLUME * WATER_WH_PER_LITRE_K
    start = datetime(2026, 11, 1, 23, tzinfo=timezone.utc)  # local midnight
    samples = []
    dhw = 46.0
    for i in range(days * 24):
        t = start + timedelta(hours=i)
        day = i // 24
        hour = (t.hour + 1) % 24  # local hour
        outdoor = -2.0 + day * 0.6 + 3.0 * math.sin((hour - 9) / 24 * 2 * math.pi)
        ghi = 0.0
        if solar and 8 <= hour <= 16:
            # cold, partly sunny winter days; never enough sun to stop the heating
            ghi = (25.0 + 12.5 * (day % 4)) * math.sin((hour - 8) / 8 * math.pi) * 4
        room = 21.0
        heat = max(0.0, H_TRUE * (room - outdoor) - APERTURE_TRUE * ghi - GAINS_TRUE)
        if day in outage_days:
            heat = 0.0
        flow = 26.0 + heat / 1000.0
        if cycling and heat < 2500.0:
            # The heat pump ran half of the hour at double load; the hourly mean
            # of the flow temperature is pulled down by the idle half.
            flow_on = 26.0 + 2.0 * heat / 1000.0
            electric = 0.5 * (2.0 * heat / cop.cop(outdoor, flow_on)) + STANDBY
            flow = 0.5 * flow_on + 0.5 * 24.0
        else:
            electric = (heat / cop.cop(outdoor, flow) if heat > 0 else 0.0) + STANDBY
        dhw_heat = 0.0
        if 0 <= hour <= 5:
            dhw = AMBIENT + (dhw - AMBIENT) * math.exp(-UA_TRUE / capacity)
        elif hour == 6:
            dhw_heat = (46.0 - dhw) * capacity
            dhw = 46.0
        samples.append(
            HourlySample(
                t,
                outdoor_c=outdoor,
                room_c=room,
                heat_w=heat + dhw_heat,
                electric_w=electric + (dhw_heat / 2.5 if dhw_heat else 0.0),
                flow_c=flow,
                dhw_c=dhw,
                dhw_heat_w=dhw_heat,
                ghi_w_per_m2=ghi if solar else None,
            )
        )
    return samples


# ----------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------


def test_building_fit_recovers_loss_aperture_and_gains():
    fit, note = fit_building(synthetic(), PRIORS, CalibrationOptions(prior_weight_days=0.05), TZ)
    assert fit is not None, note
    assert fit.loss_w_per_k == pytest.approx(H_TRUE, rel=0.03)
    assert fit.solar_aperture_m2 == pytest.approx(APERTURE_TRUE, rel=0.1)
    assert fit.internal_gains_w == pytest.approx(GAINS_TRUE, rel=0.15)
    assert fit.solar_fitted
    assert fit.days == 14
    assert fit.r2 > 0.99
    assert fit.mae_fit_w < 0.1 * fit.mae_prior_w


def test_default_shrinkage_keeps_the_daily_balance_right():
    """Loss and gains are collinear over a narrow temperature range; the prior
    decides between them, the fitted balance still matches the measurements."""
    fit, note = fit_building(synthetic(), PRIORS, CalibrationOptions(), TZ)
    assert fit is not None, note
    assert fit.mae_fit_w < 0.5 * fit.mae_prior_w
    assert fit.r2 > 0.95
    assert 150.0 <= fit.loss_w_per_k <= H_TRUE
    assert 0.0 <= fit.solar_aperture_m2 <= APERTURE_TRUE
    assert 0.0 <= fit.internal_gains_w <= GAINS_TRUE


def test_building_fit_without_irradiance_keeps_the_prior_aperture():
    fit, note = fit_building(
        synthetic(solar=False), PRIORS, CalibrationOptions(prior_weight_days=0.05), TZ
    )
    assert fit is not None, note
    assert not fit.solar_fitted
    assert fit.solar_aperture_m2 == PRIORS.solar_aperture_m2
    assert fit.loss_w_per_k == pytest.approx(H_TRUE, rel=0.03)


def test_solar_term_is_fitted_on_the_days_that_have_irradiance():
    """The weather fill builds the irradiance history up from the end of the
    window; as soon as enough days have it, the solar term is fitted on them."""
    samples = synthetic()
    cutoff = samples[0].timestamp + timedelta(days=8)
    for sample in samples:
        if sample.timestamp < cutoff:
            sample.ghi_w_per_m2 = None  # the first 8 days have no irradiance yet
    fit, note = fit_building(samples, PRIORS, CalibrationOptions(prior_weight_days=0.05), TZ)
    assert fit is not None, note
    assert fit.solar_fitted and fit.days == 6
    assert fit.solar_aperture_m2 == pytest.approx(APERTURE_TRUE, rel=0.15)
    # with fewer irradiance days than min_days all days are used without the term
    thin = CalibrationOptions(prior_weight_days=0.05, min_days=8)
    fit, note = fit_building(samples, PRIORS, thin, TZ)
    assert fit is not None, note
    assert not fit.solar_fitted and fit.days == 14


def test_building_fit_is_shrunk_towards_the_prior_with_few_days():
    few = synthetic(days=5)
    free, _ = fit_building(few, PRIORS, CalibrationOptions(prior_weight_days=0.01), TZ)
    shrunk, _ = fit_building(few, PRIORS, CalibrationOptions(prior_weight_days=30.0), TZ)
    assert free is not None and shrunk is not None
    assert abs(shrunk.loss_w_per_k - PRIORS.loss_w_per_k) < abs(
        free.loss_w_per_k - PRIORS.loss_w_per_k
    )
    # the prior wins completely with an overwhelming weight
    pinned, _ = fit_building(few, PRIORS, CalibrationOptions(prior_weight_days=1e6), TZ)
    assert pinned is not None
    assert pinned.loss_w_per_k == pytest.approx(PRIORS.loss_w_per_k, abs=1.0)


def test_outage_days_and_drifting_days_are_excluded():
    samples = synthetic(outage_days=(3, 9))
    # day 6: the rooms warm up by 2 K (visitors, open windows the day before)
    for sample in samples:
        if 6 * 24 <= (sample.timestamp - samples[0].timestamp).total_seconds() / 3600 < 7 * 24:
            hour = ((sample.timestamp - samples[0].timestamp).total_seconds() / 3600) % 24
            sample.room_c = 20.0 + 2.0 * hour / 23
    fit, note = fit_building(samples, PRIORS, CalibrationOptions(prior_weight_days=0.05), TZ)
    assert fit is not None, note
    assert fit.excluded_outage_days == 2
    assert fit.excluded_drift_days == 1
    assert fit.days == 11
    assert fit.loss_w_per_k == pytest.approx(H_TRUE, rel=0.03)


def test_building_fit_needs_enough_days_and_temperature_spread():
    fit, note = fit_building(synthetic(days=3), PRIORS, CalibrationOptions(), TZ)
    assert fit is None and "usable days" in note
    flat = synthetic(days=8)
    for sample in flat:
        sample.outdoor_c = 5.0
    fit, note = fit_building(flat, PRIORS, CalibrationOptions(), TZ)
    assert fit is None and "spans only" in note


def test_building_fit_stays_within_the_band_around_the_prior():
    samples = synthetic()
    for sample in samples:
        sample.heat_w = (sample.heat_w or 0.0) * 10  # nonsense meter
    fit, _ = fit_building(samples, PRIORS, CalibrationOptions(prior_weight_days=0.01), TZ)
    assert fit is not None
    assert fit.loss_w_per_k == pytest.approx(PRIORS.loss_w_per_k * 3.0)


# ----------------------------------------------------------------------
# COP
# ----------------------------------------------------------------------


def test_cop_scale_is_recovered_net_of_standby():
    model = CopModel(WBB12_RATING_POINTS)
    fit, note = fit_cop_scale(synthetic(), model, PRIORS, CalibrationOptions(cop_prior_hours=0))
    assert fit is not None, note
    assert fit.cop_scale == pytest.approx(SCALE_TRUE, rel=0.03)
    assert fit.hours > 100
    # the old script included the standby power and came out low
    biased = CalibrationPriors(**{**PRIORS.__dict__, "standby_w": 0.0})
    low, _ = fit_cop_scale(synthetic(), model, biased, CalibrationOptions(cop_prior_hours=0))
    assert low is not None and low.cop_scale < fit.cop_scale


def test_cop_fit_skips_cycling_hours():
    model = CopModel(WBB12_RATING_POINTS)
    samples = synthetic(cycling=True)
    fit, _ = fit_cop_scale(samples, model, PRIORS, CalibrationOptions(cop_prior_hours=0))
    assert fit is not None
    assert fit.cop_scale == pytest.approx(SCALE_TRUE, rel=0.03)
    # including the cycling hours (threshold 500 W as in the old script) biases it
    loose = CalibrationOptions(cop_prior_hours=0, cop_min_heat_w=500.0)
    biased, _ = fit_cop_scale(samples, model, PRIORS, loose)
    assert biased is not None
    assert abs(biased.cop_scale - SCALE_TRUE) > abs(fit.cop_scale - SCALE_TRUE)


def test_cop_fit_divides_out_the_models_own_scale():
    scaled = CopModel(WBB12_RATING_POINTS, scale=0.6)
    fit, _ = fit_cop_scale(synthetic(), scaled, PRIORS, CalibrationOptions(cop_prior_hours=0))
    assert fit is not None
    assert fit.cop_scale == pytest.approx(SCALE_TRUE, rel=0.03)


def test_cop_fit_shrinks_and_clips():
    model = CopModel(WBB12_RATING_POINTS)
    samples = synthetic(days=2)
    shrunk, _ = fit_cop_scale(samples, model, PRIORS, CalibrationOptions(cop_prior_hours=1e6))
    assert shrunk is not None
    assert shrunk.cop_scale == pytest.approx(PRIORS.cop_scale, abs=0.01)
    for sample in samples:
        sample.electric_w = (sample.electric_w or 0.0) * 10
    clipped, _ = fit_cop_scale(samples, model, PRIORS, CalibrationOptions(cop_prior_hours=0))
    assert clipped is not None and clipped.cop_scale == CalibrationOptions().cop_min_scale


# ----------------------------------------------------------------------
# Emitter (flow temperature)
# ----------------------------------------------------------------------


def model_flow_samples(emitter: float = 800.0, slab_room: float = 1500.0, spread: float = 3.0):
    """Samples whose flow temperature follows the steady-state model."""
    samples = synthetic(solar=False)
    for sample in samples:
        heat = (sample.heat_w or 0.0) - (sample.dhw_heat_w or 0.0)
        sample.flow_c = 21.0 + heat / slab_room + heat / emitter + spread / 2.0
    return samples


def test_emitter_is_recovered_from_the_flow_temperature():
    priors = CalibrationPriors(
        **{**PRIORS.__dict__, "emitter_w_per_k": 1000.0, "slab_room_w_per_k": 1500.0}
    )
    fit, note = fit_emitter(
        model_flow_samples(800.0), priors, CalibrationOptions(emitter_prior_hours=0)
    )
    assert fit is not None, note
    assert fit.emitter_w_per_k == pytest.approx(800.0, rel=0.02)
    assert fit.hours > 100
    assert fit.measured_lift_k > 3.0
    # shrinkage and band
    shrunk, _ = fit_emitter(
        model_flow_samples(800.0), priors, CalibrationOptions(emitter_prior_hours=1e6)
    )
    assert shrunk is not None and shrunk.emitter_w_per_k == pytest.approx(1000.0, abs=1)
    clipped, _ = fit_emitter(
        model_flow_samples(100.0), priors, CalibrationOptions(emitter_prior_hours=0)
    )
    assert clipped is not None and clipped.emitter_w_per_k == pytest.approx(300.0)
    # a flow the slab-room coupling alone explains leaves the emitter at the upper band
    perfect, _ = fit_emitter(
        model_flow_samples(1e9), priors, CalibrationOptions(emitter_prior_hours=0)
    )
    assert perfect is not None and perfect.emitter_w_per_k == pytest.approx(3000.0)
    thin, note = fit_emitter(model_flow_samples()[:4], priors)
    assert thin is None and "emitter:" in note


# ----------------------------------------------------------------------
# Tank
# ----------------------------------------------------------------------


def test_dhw_loss_is_recovered():
    fit, note = fit_dhw_loss(synthetic(), PRIORS, CalibrationOptions(dhw_prior_nights=0), TZ)
    assert fit is not None, note
    assert fit.loss_w_per_k == pytest.approx(UA_TRUE, rel=0.05)
    assert fit.nights == 14


def test_dhw_loss_needs_several_nights():
    fit, note = fit_dhw_loss(synthetic(days=2), PRIORS, CalibrationOptions(), TZ)
    assert fit is None and "need 3" in note
    fit, note = fit_dhw_loss(synthetic(days=2), PRIORS, CalibrationOptions(dhw_min_nights=1), TZ)
    assert fit is not None and fit.nights == 2


def test_dhw_heat_is_estimated_from_tank_rises_when_not_measured():
    samples = synthetic()
    charged = [s for s in samples if (s.dhw_heat_w or 0.0) > 0]
    assert charged
    expected = charged[0].dhw_heat_w
    for sample in samples:
        sample.dhw_heat_w = None
    filled = estimate_dhw_heat(samples, VOLUME)
    estimated = [s.dhw_heat_w for s in filled if (s.dhw_heat_w or 0.0) > 0]
    assert len(estimated) == len(charged)
    assert estimated[0] == pytest.approx(expected, rel=0.01)


# ----------------------------------------------------------------------
# All together, serialisation, hourly means
# ----------------------------------------------------------------------


def test_calibrate_runs_all_fits_and_roundtrips():
    result = calibrate(
        synthetic(),
        PRIORS,
        CopModel(WBB12_RATING_POINTS),
        CalibrationOptions(prior_weight_days=0.05, cop_prior_hours=0, dhw_prior_nights=0),
        TZ,
        window_days=14,
    )
    assert result.building and result.cop and result.dhw and result.emitter
    assert result.notes == []
    text = result.summary(PRIORS)
    assert "loss 150 -> 1" in text and "COP scale 1.000 -> 0." in text
    again = CalibrationResult.from_dict(result.to_dict())
    assert again.building == result.building
    assert again.cop == result.cop
    assert again.dhw == result.dhw
    assert again.emitter == result.emitter
    assert again.fitted_at == result.fitted_at


def test_calibrate_reports_why_fits_were_skipped():
    result = calibrate(synthetic(days=2), PRIORS, CopModel(WBB12_RATING_POINTS), tz=TZ)
    assert result.building is None
    assert any(note.startswith("building:") for note in result.notes)


def test_hourly_means_are_time_weighted_and_build_samples():
    t0 = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)
    power = [(t0 - timedelta(minutes=30), 1000.0), (t0 + timedelta(minutes=15), 3000.0)]
    means = hourly_means(power, t0 - timedelta(hours=1), t0 + timedelta(hours=2))
    assert means[t0 - timedelta(hours=1)] == pytest.approx(1000.0)  # second half only
    assert means[t0] == pytest.approx(0.25 * 1000 + 0.75 * 3000)
    assert means[t0 + timedelta(hours=1)] == pytest.approx(3000.0)
    samples = build_samples({"heat_w": means, "outdoor_c": {t0: 4.0}})
    assert [s.timestamp for s in samples] == sorted(means)
    assert samples[1].outdoor_c == 4.0 and samples[0].outdoor_c is None
