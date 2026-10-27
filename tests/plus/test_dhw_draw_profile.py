"""Learned hot water draw profile (eos-plus fork, B1).

A simulated tank (fully mixed, standby loss, hysteresis charging) is tapped on
known times. The tank temperature is sampled every 5 minutes like the EOS
measurement store sees it. The fit has to find the hours and the energy,
separate weekends, leave out days away and fall back to the configured profile
while data is thin. The calibrated settings carry the profile into the
optimizer, which uses the weekend profile on Saturdays and Sundays.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pendulum
import pytest

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.calibration import (
    WATER_WH_PER_LITRE_K,
    CalibrationOptions,
    CalibrationPriors,
    CalibrationResult,
    calibrate,
    daily_draws,
    fit_draw_profile,
)
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel

TZ = ZoneInfo("Europe/Berlin")
VOLUME = 190.0
CAPACITY = VOLUME * WATER_WH_PER_LITRE_K
LOSS = 1.5
AMBIENT = 18.0
FLAT_PRIOR = tuple([100.0] * 24)  # 2.4 kWh/day, no shape
PRIORS = CalibrationPriors(
    loss_w_per_k=150.0,
    dhw_loss_w_per_k=LOSS,
    dhw_volume_l=VOLUME,
    dhw_ambient_c=AMBIENT,
    draw_profile_wh=FLAT_PRIOR,
)
# local hour -> Wh tapped (in the first 10 minutes of the hour)
WORKDAY = {7: 1200.0, 19: 900.0}
WEEKEND = {10: 1500.0, 20: 900.0}


def simulate(
    days: int,
    start_local: datetime = datetime(2026, 10, 5, tzinfo=TZ),  # Monday
    away_days: tuple[int, ...] = (),
    heat_power: float = 2500.0,
    sample_minutes: int = 5,
):
    """Tank temperature samples (UTC) and the heat into the tank (UTC, W)."""
    temp = 45.0
    charging = False
    dhw: list[tuple[datetime, float]] = []
    heat: list[tuple[datetime, float]] = []
    t = start_local
    minutes = days * 24 * 60
    for minute in range(minutes):
        local = t
        day_index = minute // (24 * 60)
        pattern = WEEKEND if local.weekday() >= 5 else WORKDAY
        draw_w = 0.0
        if day_index not in away_days and local.hour in pattern and local.minute < 10:
            draw_w = pattern[local.hour] / (10 / 60)
        if temp < 40.0:
            charging = True
        if temp >= 45.0:
            charging = False
        heat_w = heat_power if charging else 0.0
        if minute % sample_minutes == 0:
            dhw.append((t.astimezone(timezone.utc), round(temp, 1)))
            heat.append((t.astimezone(timezone.utc), heat_w))
        loss_w = LOSS * (temp - AMBIENT)
        temp += (heat_w - draw_w - loss_w) / 60.0 / CAPACITY
        t = (t.astimezone(timezone.utc) + timedelta(minutes=1)).astimezone(TZ)
    return dhw, heat


# ----------------------------------------------------------------------
# Daily draws
# ----------------------------------------------------------------------


def test_draws_are_found_in_the_right_local_hours():
    dhw, heat = simulate(3)
    days = daily_draws(dhw, PRIORS, tz=TZ, heat_series=heat)
    assert len(days) == 1  # first and last day are cut by the window
    ((day, hours),) = days.items()
    assert day.weekday() == 1
    assert hours[7] == pytest.approx(1200.0, rel=0.1)
    assert hours[19] == pytest.approx(900.0, rel=0.1)
    others = sum(v for h, v in enumerate(hours) if h not in (7, 19))
    assert others < 150.0  # standby loss is not counted as tapping


def test_without_heat_measurement_draws_during_a_charge_are_a_lower_bound():
    dhw, heat = simulate(3)
    with_heat = daily_draws(dhw, PRIORS, tz=TZ, heat_series=heat)
    without = daily_draws(dhw, PRIORS, tz=TZ)
    day = next(iter(with_heat))
    assert sum(without[day]) <= sum(with_heat[day]) + 50.0
    assert sum(without[day]) >= 0.5 * sum(with_heat[day])


def test_days_with_a_reading_gap_are_left_out():
    dhw, heat = simulate(5)
    cut_from = dhw[0][0] + timedelta(days=2, hours=3)
    gappy = [(t, v) for t, v in dhw if not (cut_from <= t < cut_from + timedelta(hours=5))]
    days = daily_draws(gappy, PRIORS, tz=TZ, heat_series=heat)
    assert len(days) == 2  # 5 days - first - last - gap day


# ----------------------------------------------------------------------
# Profile fit
# ----------------------------------------------------------------------


def test_profile_needs_enough_days():
    dhw, heat = simulate(10)
    days = daily_draws(dhw, PRIORS, tz=TZ, heat_series=heat)
    fit, note = fit_draw_profile(days, PRIORS)
    assert fit is None and "need 14" in note


def test_weekday_and_weekend_profiles_are_learned():
    dhw, heat = simulate(30)
    days = daily_draws(dhw, PRIORS, tz=TZ, heat_series=heat)
    fit, note = fit_draw_profile(days, PRIORS)
    assert fit is not None, note
    assert fit.weekend_wh is not None and fit.weekend_days >= 4
    # the measured hours dominate, the prior pulls with 7 days
    n_work = fit.days - fit.weekend_days
    expected_7 = (n_work * 1200.0 + 7 * 100.0) / (n_work + 7)
    assert fit.profile_wh[7] == pytest.approx(expected_7, rel=0.1)
    assert fit.profile_wh[3] < 60.0
    assert fit.weekend_wh[10] > 4 * fit.weekend_wh[7]
    assert fit.profile_wh[7] > 4 * fit.profile_wh[10]
    true_daily = (5 * 2100.0 + 2 * 2400.0) / 7
    assert fit.daily_wh == pytest.approx(true_daily, rel=0.25)


def test_one_profile_when_weekends_are_too_few():
    dhw, heat = simulate(30)
    days = daily_draws(dhw, PRIORS, tz=TZ, heat_series=heat)
    fit, _ = fit_draw_profile(days, PRIORS, CalibrationOptions(draw_min_weekend_days=20))
    assert fit is not None and fit.weekend_wh is None and fit.weekend_days == 0


def test_days_away_are_left_out():
    dhw, heat = simulate(30, away_days=tuple(range(10, 17)))
    days = daily_draws(dhw, PRIORS, tz=TZ, heat_series=heat)
    fit, _ = fit_draw_profile(days, PRIORS)
    assert fit is not None and fit.excluded_days == 7
    n_work = fit.days - fit.weekend_days
    assert fit.profile_wh[7] == pytest.approx((n_work * 1200 + 700) / (n_work + 7), rel=0.1)


def test_implausible_profile_keeps_the_configured_one():
    dhw, heat = simulate(30)
    days = daily_draws(dhw, PRIORS, tz=TZ, heat_series=heat)
    tiny = CalibrationPriors(
        loss_w_per_k=150.0, dhw_volume_l=VOLUME, draw_profile_wh=tuple([10.0] * 24)
    )
    fit, note = fit_draw_profile(days, tiny)
    assert fit is None and "outside the band" in note
    no_prior = CalibrationPriors(loss_w_per_k=150.0)
    assert fit_draw_profile(days, no_prior)[0] is None


# ----------------------------------------------------------------------
# Calibration and settings
# ----------------------------------------------------------------------


def test_calibrate_learns_the_profile_and_applies_it_to_the_settings():
    dhw, heat = simulate(30)
    device = HeatPumpCommonSettings(device_id="wbb12", dhw={"draw_profile_wh": list(FLAT_PRIOR)})
    priors = device.calibration_priors()
    assert priors.draw_profile_wh == FLAT_PRIOR
    result = calibrate(
        [],
        priors,
        CopModel(WBB12_RATING_POINTS),
        tz=TZ,
        dhw_series=dhw,
        dhw_heat_series=heat,
    )
    assert result.draw is not None
    assert "hot water draw 2.4 ->" in result.summary(priors)
    again = CalibrationResult.from_dict(result.to_dict())
    assert again.draw == result.draw
    calibrated = device.calibrated(result)
    assert calibrated.dhw.draw_profile_wh == result.draw.profile_wh
    assert calibrated.dhw.draw_profile_weekend_wh == result.draw.weekend_wh
    assert device.dhw.draw_profile_wh == list(FLAT_PRIOR)  # configuration untouched
    tuesday = pendulum.datetime(2026, 11, 3, 7, tz="Europe/Berlin")
    saturday = pendulum.datetime(2026, 11, 7, 7, tz="Europe/Berlin")
    assert calibrated.dhw.draw_wh(tuesday, 1.0) == result.draw.profile_wh[7]
    assert calibrated.dhw.draw_wh(saturday, 0.25) == pytest.approx(result.draw.weekend_wh[7] * 0.25)


def test_calibrate_without_series_or_disabled_reports_and_skips():
    result = calibrate([], PRIORS, CopModel(WBB12_RATING_POINTS), tz=TZ)
    assert result.draw is None and not any(n.startswith("draw:") for n in result.notes)
    thin, _ = simulate(5)
    short = calibrate([], PRIORS, CopModel(WBB12_RATING_POINTS), tz=TZ, dhw_series=thin)
    assert short.draw is None and any("need 14" in n for n in short.notes)
    dhw, heat = simulate(30)
    off = calibrate(
        [], PRIORS, CopModel(WBB12_RATING_POINTS), tz=TZ, dhw_series=dhw, fit_draw_enabled=False
    )
    assert off.draw is None


def test_weekend_profile_setting_is_validated():
    with pytest.raises(ValueError):
        HeatPumpCommonSettings(device_id="x", dhw={"draw_profile_weekend_wh": [1.0] * 23})
    settings = HeatPumpCommonSettings(device_id="x")
    saturday = pendulum.datetime(2026, 11, 7, 7, tz="Europe/Berlin")
    assert settings.dhw.draw_wh(saturday, 1.0) == settings.dhw.draw_profile_wh[7]
