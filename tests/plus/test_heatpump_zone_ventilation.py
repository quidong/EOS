"""Zone setback and window ventilation of the heat pump model (eos-plus fork).

Zone setback = upper floor cooler at night and on working-day mornings.

Zone setback: the measured room temperature is the mean of all rooms; while the
zone (e.g. the 6 upstairs rooms of 15) is set back to the heating circuit's reduced
setpoint, the targets of the mean drop by the zone's share of the difference.

Ventilation: airing is an extra loss of the room node; the expected airing per hour
is a profile learned from the window contacts, the building fit takes the measured
airing out of the balance, plan-versus-actual skips aired slots, and one airing slot
per day around midday can be recommended.
"""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.heatpumpkpis import device_kpis
from akkudoktoreos.devices.heatpumpplanactual import PlanActualStore, evaluate_plans
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.calibration import (
    CalibrationOptions,
    CalibrationPriors,
    CalibrationResult,
    HourlySample,
    VentilationFit,
    calibrate,
    fit_building,
    fit_ventilation_profile,
)
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel
from akkudoktoreos.devices.thermal.heatpump import (
    DhwMode,
    HeatingMode,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
    plan_result,
)

TZ = "Europe/Berlin"
FRIDAY = pendulum.datetime(2026, 11, 6, 0, 0, tz=TZ)
SATURDAY = pendulum.datetime(2026, 11, 7, 0, 0, tz=TZ)


def device(**settings) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings(device_id="wbb12", **settings)


def run_plan(start=FRIDAY, hours=24, outdoor=None, surplus=None, **kwargs):
    settings = kwargs.pop("settings", device())
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=settings,
        initial_room_c=21.0,
        initial_dhw_c=45.0,
        **kwargs,
    )
    starts = [start.add(hours=i) for i in range(hours)]
    return build_run_plan(
        params,
        starts,
        1.0,
        outdoor if outdoor is not None else [5.0] * hours,
        [0.0] * hours,
        [0.0003] * hours,
        surplus,
    )


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def test_default_setback_windows_nights_and_working_day_mornings():
    zone = device(zone_setback={"enabled": True}).zone_setback
    assert zone.active_at(FRIDAY.add(hours=3)) and zone.active_at(FRIDAY.add(hours=11))
    assert not zone.active_at(FRIDAY.add(hours=12)) and not zone.active_at(FRIDAY.add(hours=21))
    assert zone.active_at(FRIDAY.add(hours=22, minutes=30))
    assert zone.active_at(SATURDAY.add(hours=7)) and not zone.active_at(SATURDAY.add(hours=8))
    assert not device().zone_setback.active_at(FRIDAY.add(hours=3))  # disabled by default


def test_measurement_keys_follow_the_features():
    plain = device()
    assert "wbb12-window-open" not in plain.measurement_keys
    both = device(zone_setback={"enabled": True}, ventilation={"enabled": True})
    assert {"wbb12-setback-room-temperature", "wbb12-window-open"} <= set(both.measurement_keys)
    assert both.calibration_measurement_keys["window_open"] == "wbb12-window-open"


def test_ventilation_profile_is_validated():
    with pytest.raises(ValueError):
        device(ventilation={"profile": [0.1] * 23})
    with pytest.raises(ValueError):
        device(ventilation={"profile": [1.5] + [0.0] * 23})
    with pytest.raises(ValueError):
        device(ventilation={"recommend_start_hour": 14, "recommend_end_hour": 12})
    vent = device(ventilation={"enabled": True, "profile_weekend": [0.0] * 24}).ventilation
    assert vent.slot_loss_w_per_k(FRIDAY.add(hours=7)) == pytest.approx(100.0)
    assert vent.slot_loss_w_per_k(SATURDAY.add(hours=7)) == 0.0
    assert device().ventilation.slot_loss_w_per_k(FRIDAY.add(hours=7)) == 0.0


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------


def test_airing_cools_the_room_and_costs_heat():
    run = run_plan()
    state = ThermalState(slab_c=24.0, room_c=21.0, dhw_c=45.0)
    closed = SlotInputs(outdoor_c=0.0, comfort_min_c=20.0, setpoint_c=21.0, comfort_max_c=22.0)
    aired = SlotInputs(
        outdoor_c=0.0,
        comfort_min_c=20.0,
        setpoint_c=21.0,
        comfort_max_c=22.0,
        ventilation_w_per_k=400.0,
    )
    a = run.system.step(state, closed, HeatingMode.REDUCED, DhwMode.HOLD)
    b = run.system.step(state, aired, HeatingMode.REDUCED, DhwMode.HOLD)
    assert b.state.room_c < a.state.room_c - 0.1
    hold_a = run.system.step(state, closed, HeatingMode.NORMAL, DhwMode.HOLD)
    hold_b = run.system.step(state, aired, HeatingMode.NORMAL, DhwMode.HOLD)
    assert hold_b.heat_space_wh >= hold_a.heat_space_wh


def test_expected_airing_comes_from_the_profile():
    run = run_plan(settings=device(ventilation={"enabled": True}))
    assert run.inputs[7].ventilation_w_per_k == pytest.approx(100.0)
    assert run.inputs[21].ventilation_w_per_k == pytest.approx(100.0)
    assert run.inputs[3].ventilation_w_per_k == 0.0
    # windows open now count in the first slot
    now = run_plan(settings=device(ventilation={"enabled": True}), window_open_share=0.6)
    assert now.inputs[0].ventilation_w_per_k == pytest.approx(1000.0 * 0.5 * 0.6)


# ----------------------------------------------------------------------
# Zone setback in the run plan
# ----------------------------------------------------------------------


def test_zone_setback_lowers_the_targets_of_the_mean_by_the_share():
    settings = device(zone_setback={"enabled": True, "share": 0.4})
    run = run_plan(settings=settings, setback_room_c=20.0)
    night, noon = run.inputs[3], run.inputs[13]
    # setpoint 21 -> 21 - 0.4 * (21 - 20)
    assert night.setpoint_c == pytest.approx(20.6)
    assert noon.setpoint_c == pytest.approx(21.0)
    # comfort minimum 19.5 at night: zone may go to 19.5 -> unchanged; 20.5 by day -> lower
    assert night.comfort_min_c == pytest.approx(19.5)
    morning = run.inputs[8]
    assert morning.comfort_min_c == pytest.approx(20.5 - 0.4 * (20.5 - 19.5))
    # RAISED and the maximum keep the zone at its setback temperature
    assert morning.raised_c == pytest.approx(22.0 - 0.4 * 2.0)
    assert morning.comfort_max_c == pytest.approx(22.0 - 0.4 * 2.0)
    assert run.system._room_target(HeatingMode.RAISED, morning) == pytest.approx(21.2)
    detail = plan_result(run, run.default_genes())
    assert detail["zone_setback"][:12] == [1.0] * 12
    assert detail["zone_setback"][12:22] == [0.0] * 10
    assert detail["zone_setback"][22:] == [1.0] * 2


def test_zone_setback_follows_the_measured_setback_temperature():
    settings = device(zone_setback={"enabled": True, "share": 0.4, "room_c": 20.0})
    assert run_plan(settings=settings, setback_room_c=19.0).inputs[3].setpoint_c == pytest.approx(
        21.0 - 0.4 * 2.0
    )
    assert run_plan(settings=settings).inputs[3].setpoint_c == pytest.approx(20.6)  # fallback


def test_zone_setback_pauses_while_away_and_without_the_feature():
    settings = device(zone_setback={"enabled": True})
    away = run_plan(settings=settings, away=True, away_until=FRIDAY.add(hours=6), away_room_c=18.0)
    assert away.zone_setback_slots[:6] == (False,) * 6
    assert away.zone_setback_slots[6] is True
    assert not any(run_plan().zone_setback_slots)


def test_without_zone_setback_the_cooler_zone_reads_as_a_comfort_violation():
    """The mean drops to ~20.6 while upstairs is at 20; with the setback that is fine."""
    with_zone = run_plan(
        settings=device(zone_setback={"enabled": True, "share": 0.4}), setback_room_c=20.0
    )
    without = run_plan()
    genes_low = [HeatingMode.REDUCED] * 12 + [HeatingMode.NORMAL] * 12
    a = with_zone.evaluate(with_zone.genes_for_modes(genes_low, [DhwMode.HOLD] * 24))
    b = without.evaluate(without.genes_for_modes(genes_low, [DhwMode.HOLD] * 24))
    assert sum(a.electric_wh[:12]) <= sum(b.electric_wh[:12])
    normal_zone = with_zone.evaluate(with_zone.default_genes())
    normal = without.evaluate(without.default_genes())
    assert sum(normal_zone.electric_wh) < sum(normal.electric_wh)  # less heat for the zone


# ----------------------------------------------------------------------
# Recommendation
# ----------------------------------------------------------------------


def test_one_airing_slot_per_day_at_the_warmest_midday_hour():
    outdoor = [2.0] * 48
    outdoor[13] = 6.0  # Friday 13:00
    outdoor[24 + 12] = 4.0  # Saturday 12:00
    outdoor[24 + 14] = 4.0  # Saturday 14:00, more PV
    surplus = [0.0] * 48
    surplus[24 + 14] = 2000.0
    run = run_plan(
        settings=device(ventilation={"enabled": True, "recommend": True}),
        hours=48,
        outdoor=outdoor,
        surplus=surplus,
    )
    slots = [i for i, v in enumerate(run.ventilate_slots) if v]
    assert slots == [13, 24 + 14]
    assert run.inputs[13].ventilation_w_per_k == pytest.approx(1000.0 * 0.15)
    detail = plan_result(run, run.default_genes())
    assert detail["ventilate"][13] == 1.0 and sum(detail["ventilate"]) == 2.0
    # profile already expects airing in the window: only the rest is added
    settings = device(
        ventilation={
            "enabled": True,
            "recommend": True,
            "profile": [0.0] * 12 + [0.1] + [0.0] * 11,
        }
    )
    run = run_plan(settings=settings, hours=24, outdoor=outdoor[:24])
    assert run.inputs[13].ventilation_w_per_k == pytest.approx(1000.0 * 0.05)
    assert not any(run_plan(hours=24).ventilate_slots)


# ----------------------------------------------------------------------
# Calibration
# ----------------------------------------------------------------------

UTC = timezone.utc
BERLIN = ZoneInfo(TZ)
H_TRUE = 180.0
VENT_TRUE = 800.0


def synthetic(days: int = 21, airing: bool = True) -> list[HourlySample]:
    """Daily balance with airing 7-8 and 21-22 local (40 % open for the hour)."""
    samples = []
    start = datetime(2026, 11, 2, tzinfo=BERLIN)  # Monday
    for d in range(days):
        outdoor = -2.0 + (d % 7) * 1.5
        for h in range(24):
            stamp = (start + timedelta(days=d, hours=h)).astimezone(UTC)
            local = stamp.astimezone(BERLIN)
            open_share = 0.4 if airing and local.hour in (7, 21) else 0.0
            if local.weekday() >= 5 and local.hour == 7:
                open_share = 0.0  # weekends: no morning airing
            room = 21.0
            heat = H_TRUE * (room - outdoor) + VENT_TRUE * open_share * (room - outdoor)
            samples.append(
                HourlySample(
                    stamp,
                    outdoor_c=outdoor,
                    room_c=room,
                    heat_w=heat,
                    electric_w=heat / 3.5,
                    window_open=open_share,
                )
            )
    return samples


def priors(vent: float) -> CalibrationPriors:
    profile = [0.0] * 24
    profile[7] = profile[21] = 0.1
    return CalibrationPriors(
        loss_w_per_k=150.0,
        ventilation_w_per_k=vent,
        ventilation_profile=tuple(profile),
    )


def test_building_fit_takes_the_measured_airing_out():
    options = CalibrationOptions(prior_weight_days=0.0)
    with_vent, _ = fit_building(synthetic(), priors(VENT_TRUE), options, BERLIN)
    without, _ = fit_building(synthetic(), priors(0.0), options, BERLIN)
    assert with_vent is not None and without is not None
    assert with_vent.loss_w_per_k == pytest.approx(H_TRUE, rel=0.03)
    assert without.loss_w_per_k > H_TRUE * 1.1  # airing counted as transmission


def test_airing_profile_is_learned_with_weekends():
    fit, note = fit_ventilation_profile(synthetic(21), priors(VENT_TRUE), tz=BERLIN)
    assert fit is not None, note
    assert fit.weekend is not None and fit.weekend_days == 6
    n = fit.days - fit.weekend_days
    assert fit.profile[7] == pytest.approx((n * 0.4 + 7 * 0.1) / (n + 7), abs=0.01)
    assert fit.weekend[7] == pytest.approx((0 + 7 * 0.1) / (6 + 7), abs=0.01)
    assert fit.profile[12] == 0.0
    short, note = fit_ventilation_profile(synthetic(10), priors(VENT_TRUE), tz=BERLIN)
    assert short is None and "need 14" in note
    assert fit_ventilation_profile(synthetic(21), CalibrationPriors(loss_w_per_k=150.0))[0] is None


def test_calibrate_learns_airing_only_with_ventilation_modelled():
    cop = CopModel(WBB12_RATING_POINTS)
    on = calibrate(synthetic(), priors(VENT_TRUE), cop, tz=BERLIN)
    assert on.ventilation is not None and "airing" in on.summary(priors(VENT_TRUE))
    assert CalibrationResult.from_dict(on.to_dict()).ventilation == on.ventilation
    off = calibrate(synthetic(), priors(0.0), cop, tz=BERLIN)
    assert off.ventilation is None
    settings = device(ventilation={"enabled": True, "loss_w_per_k": VENT_TRUE})
    assert settings.calibration_priors().ventilation_w_per_k == VENT_TRUE
    assert device().calibration_priors().ventilation_w_per_k == 0.0
    applied = settings.calibrated(on)
    assert applied.ventilation.profile == on.ventilation.profile
    assert applied.ventilation.profile_weekend == on.ventilation.weekend
    assert settings.ventilation.profile[7] == 0.1  # configuration untouched


def test_airing_figure_reaches_the_kpis(tmp_path: Path):
    from akkudoktoreos.devices.heatpumpcalibration import HeatPumpCalibrationStore

    result = CalibrationResult(
        fitted_at=datetime(2026, 11, 20, tzinfo=UTC),
        ventilation=VentilationFit(
            profile=[0.0] * 24, weekend=None, days=15, daily_open_hours=0.25
        ),
    )
    store = HeatPumpCalibrationStore(tmp_path / "plus" / "heatpump_calibration.json")
    store.set_result("wbb12", result, "", "")
    store.save()
    kpis = device_kpis("wbb12", tmp_path)
    assert kpis["kpi_airing_open_h_per_day"] == 0.25 and kpis["kpi_calibrated"] == 1.0


# ----------------------------------------------------------------------
# Plan versus actual and the request
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_plan_actual_skips_aired_slots(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    config_eos.merge_settings_from_dict(
        {
            "devices": {
                "max_heat_pumps": 1,
                "heat_pumps": {
                    "wbb12": {"calibration": {"enabled": True}, "ventilation": {"enabled": True}}
                },
            }
        }
    )
    now = pendulum.datetime(2026, 11, 20, 12, 7, tz=TZ)
    store = PlanActualStore(tmp_path / "plans.json")
    rows = {
        "room_c": [21.0] * 24,
        "slab_c": [24.0] * 24,
        "dhw_c": [45.0] * 24,
        "electric_wh": [400.0] * 24,
        "heating_mode": ["NORMAL"] * 24,
        "dhw_mode": ["HOLD"] * 24,
    }
    for h in range(26, 0, -1):
        issued = now.subtract(hours=h).start_of("hour").add(minutes=5)
        store.record(
            "wbb12", issued, [issued.start_of("hour").add(hours=i) for i in range(24)], 1.0, rows
        )
    t = now.subtract(hours=26)
    while t <= now:
        aired = t.in_timezone(TZ).hour == 7
        await isolated_measurement.update_value(
            t, "wbb12-room-temperature", 18.0 if aired else 21.0
        )
        await isolated_measurement.update_value(t, "wbb12-dhw-temperature", 45.0)
        await isolated_measurement.update_value(t, "wbb12-electric-power", 400.0)
        await isolated_measurement.update_value(t, "wbb12-window-open", 0.5 if aired else 0.0)
        t = t.add(minutes=15)
    settings = config_eos.devices.heat_pumps["wbb12"]
    evaluation = await evaluate_plans(settings, store, isolated_measurement, now)
    assert evaluation is not None
    room = evaluation["leads"]["1h"]["room"]
    assert room["slots"] >= 18
    assert abs(room["bias_k"]) < 0.05  # the 18 degC of the airing hour is not compared


@pytest.mark.asyncio
async def test_request_reads_window_share_and_setback_temperature(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    from akkudoktoreos.optimization.genetic.configrequest import (
        ConfigOptimizationRequest,
    )

    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 8,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                },
            },
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "max_home_appliances": 0,
                "home_appliances": {},
                "max_heat_pumps": 1,
                "heat_pumps": {
                    "wbb12": {
                        "zone_setback": {"enabled": True},
                        "ventilation": {"enabled": True},
                    }
                },
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "wbb12-room-temperature", 20.5)
    await isolated_measurement.update_value(now, "wbb12-dhw-temperature", 45.0)
    await isolated_measurement.update_value(
        now.subtract(days=2), "wbb12-setback-room-temperature", 19.5
    )
    await isolated_measurement.update_value(now, "wbb12-window-open", 0.25)

    def request() -> ConfigOptimizationRequest:
        return ConfigOptimizationRequest.model_validate(
            {
                "forecasts": {
                    "pv_forecast_wh": [0.0] * 48,
                    "total_load": [300.0] * 48,
                    "electricity_price_per_wh": [0.0003] * 48,
                    "feed_in_tariff_per_wh": [0.00007] * 48,
                    "temperature_forecast": [4.0] * 48,
                    "ghi_forecast": [0.0] * 48,
                }
            }
        )

    state = (await request().resolve()).heat_pumps[0]
    assert state.setback_room_c == 19.5
    assert state.window_open_share == 0.25
    # implausible values are ignored
    await isolated_measurement.update_value(now, "wbb12-setback-room-temperature", 5.0)
    await isolated_measurement.update_value(now, "wbb12-window-open", 3.0)
    state = (await request().resolve()).heat_pumps[0]
    assert state.setback_room_c is None and state.window_open_share == 1.0
    assert math.isfinite(state.initial_room_c or 0.0)


@pytest.mark.asyncio
async def test_solution_exports_zone_setback_and_ventilate(config_eos: ConfigEOS):
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import (
        GeneticOptimizationParameters,
    )
    from akkudoktoreos.utils.datetimeutil import to_datetime

    hours = 24
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 12,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                }
            },
        }
    )
    start = to_datetime("2026-11-06T00:00:00", in_timezone=TZ)  # Friday
    get_ems(init=True).set_start_datetime(start)
    settings = device(
        zone_setback={"enabled": True},
        ventilation={"enabled": True, "recommend": True},
    )
    outdoor = [2.0] * hours
    outdoor[13] = 7.0
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * hours,
                "gesamtlast": [300.0] * hours,
                "strompreis_euro_pro_wh": [0.0003] * hours,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": outdoor,
            "pv_battery": None,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "wbb12",
                    "settings": settings,
                    "initial_room_c": 20.8,
                    "initial_dhw_c": 45.0,
                    "setback_room_c": 20.0,
                }
            ],
        }
    )
    solution = GeneticOptimization(fixed_seed=3).optimize_ems(params, ngen=5, individuals=12)
    schedule = solution.heat_pumps["wbb12"]
    assert schedule["zone_setback"][:12] == [1.0] * 12 and schedule["zone_setback"][12] == 0.0
    assert schedule["ventilate"][13] == 1.0 and sum(schedule["ventilate"]) == 1.0
    table = (await solution.optimization_solution()).solution.to_dataframe()
    assert {"wbb12_zone_setback", "wbb12_ventilate"} <= set(table.columns)
