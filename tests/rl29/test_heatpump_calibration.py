"""Automatic heat pump calibration inside EOS (RL29 fork).

Covers the settings (measurement keys, applying a result, the configuration
staying untouched), the gathering of hourly samples from the measurement
store with the weather fill, the refit cadence with the JSON store, and the
hook in the optimization request: a heat pump with ``calibration.enabled``
is planned with the fitted parameters.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.heatpumpcalibration import (
    HeatPumpCalibrationStore,
    calibrate_device,
    gather_samples,
)
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.calibration import (
    WATER_WH_PER_LITRE_K,
    BuildingFit,
    CalibrationResult,
    CopFit,
    DhwFit,
)
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest

H_TRUE = 200.0
GAINS_TRUE = 250.0
SCALE_TRUE = 0.8
UA_TRUE = 1.8
STANDBY = 22.0
VOLUME = 190.0
AMBIENT = 18.0
NOW = pendulum.datetime(2026, 11, 20, 9, 7, tz="Europe/Berlin")


def plant_history(days: int = 14, *, with_outdoor: bool = True) -> dict[str, list[tuple]]:
    """Measurement samples (time, value) per key of a plant with known parameters."""
    cop = CopModel(WBB12_RATING_POINTS, scale=SCALE_TRUE)
    capacity = VOLUME * WATER_WH_PER_LITRE_K
    start = NOW.subtract(days=days).start_of("hour")
    keys: dict[str, list[tuple]] = {
        "wbb12-outdoor-temperature": [],
        "wbb12-room-temperature": [],
        "wbb12-heat-power": [],
        "wbb12-electric-power": [],
        "wbb12-flow-temperature": [],
        "wbb12-dhw-temperature": [],
    }
    dhw = 46.0
    for i in range(days * 24):
        t = start.add(hours=i)
        local = t.in_timezone("Europe/Berlin")
        outdoor = -1.0 + (i // 24) * 0.5 + 3.0 * math.sin((local.hour - 9) / 24 * 2 * math.pi)
        heat = max(0.0, H_TRUE * (21.0 - outdoor) - GAINS_TRUE)
        flow = 26.0 + heat / 1000.0
        electric = (heat / cop.cop(outdoor, flow) if heat > 0 else 0.0) + STANDBY
        dhw_heat = 0.0
        if 0 <= local.hour <= 5:
            dhw = AMBIENT + (dhw - AMBIENT) * math.exp(-UA_TRUE / capacity)
        elif local.hour == 6:
            dhw_heat = (46.0 - dhw) * capacity
            dhw = 46.0
        # the signal holds for the hour: one sample per hour is a step signal
        if with_outdoor:
            keys["wbb12-outdoor-temperature"].append((t, outdoor))
        keys["wbb12-room-temperature"].append((t, 21.0))
        keys["wbb12-heat-power"].append((t, heat + dhw_heat))
        keys["wbb12-electric-power"].append((t, electric + (dhw_heat / 2.5 if dhw_heat else 0.0)))
        keys["wbb12-flow-temperature"].append((t, flow))
        keys["wbb12-dhw-temperature"].append((t, dhw))
    return keys


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


def _configure(config: ConfigEOS, data_folder: Path, **calibration) -> None:
    config.merge_settings_from_dict(
        {
            "general": {"timezone": "Europe/Berlin", "data_folder_path": str(data_folder)},
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
                    "measurement_max_age_seconds": 300,
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
                        "building": {"loss_w_per_k": 150.0},
                        "unit": {"cop_scale": 1.0, "standby_w": STANDBY},
                        "dhw": {"volume_l": VOLUME, "ambient_c": AMBIENT, "loss_w_per_k": 1.5},
                        "calibration": {
                            "enabled": True,
                            "days": 14,
                            "prior_weight_days": 0.05,
                            "cop_prior_hours": 0,
                            **calibration,
                        },
                    }
                },
            },
        }
    )


async def _feed(measurement: Measurement, history: dict[str, list[tuple]]) -> None:
    for key, samples in history.items():
        for when, value in samples:
            await measurement.update_value(when, key, value)


def _request() -> ConfigOptimizationRequest:
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


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def test_calibration_keys_are_registered_only_when_enabled():
    off = HeatPumpCommonSettings(device_id="wbb12")
    assert "wbb12-heat-power" not in off.measurement_keys
    on = HeatPumpCommonSettings(device_id="wbb12", calibration={"enabled": True})
    for key in (
        "wbb12-outdoor-temperature",
        "wbb12-heat-power",
        "wbb12-electric-power",
        "wbb12-flow-temperature",
        "wbb12-dhw-heat-power",
        "wbb12-ghi",
    ):
        assert key in on.measurement_keys
    assert len(on.measurement_keys) == len(set(on.measurement_keys))
    assert on.calibration_measurement_keys["room_c"] == "wbb12-room-temperature"


def test_calibrated_applies_fits_and_leaves_the_configuration_alone():
    device = HeatPumpCommonSettings(device_id="wbb12", building={"loss_w_per_k": 150.0})
    result = CalibrationResult(
        fitted_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
        building=BuildingFit(
            loss_w_per_k=190.0, solar_aperture_m2=3.5, internal_gains_w=220.0, days=10
        ),
        cop=CopFit(cop_scale=0.82, hours=50, measured_cop=3.4, model_cop=4.1, raw_scale=0.83),
        dhw=DhwFit(loss_w_per_k=1.9, nights=8, raw_loss_w_per_k=2.0),
    )
    fitted = device.calibrated(result)
    assert fitted.building.loss_w_per_k == 190.0
    assert fitted.building.solar_aperture_m2 == 3.5
    assert fitted.building.internal_gains_w == 220.0
    assert fitted.unit.cop_scale == 0.82
    assert fitted.dhw.loss_w_per_k == 1.9
    assert fitted.device_id == "wbb12"
    assert fitted.to_system(0.25).cop.scale == 0.82
    # the configuration is the prior and stays as it was
    assert device.building.loss_w_per_k == 150.0 and device.unit.cop_scale == 1.0
    assert device.calibration_priors().loss_w_per_k == 150.0
    # partial results keep the configured values; nothing fitted returns the device
    partial = CalibrationResult(fitted_at=result.fitted_at, cop=result.cop)
    assert device.calibrated(partial).building.loss_w_per_k == 150.0
    assert device.calibrated(None) is device
    assert device.calibrated(CalibrationResult(fitted_at=result.fitted_at)) is device


def test_options_follow_the_settings():
    device = HeatPumpCommonSettings(
        device_id="wbb12",
        calibration={"enabled": True, "outage_threshold": 0, "cop_min_heat_w": 2500},
    )
    options = device.calibration.to_options()
    assert options.min_healthy_days > 1000  # outage filter off
    assert options.cop_min_heat_w == 2500
    with pytest.raises(ValueError):
        HeatPumpCommonSettings(
            device_id="wbb12", calibration={"cop_min_scale": 1.2, "cop_max_scale": 1.0}
        )


# ----------------------------------------------------------------------
# Samples from the measurement store
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_samples_builds_hourly_means_and_fills_weather(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    _configure(config_eos, tmp_path)
    device = config_eos.devices.heat_pumps["wbb12"]
    history = plant_history(days=3, with_outdoor=False)
    await _feed(isolated_measurement, history)
    # the weather provider knows the last two days and the forecast
    hours = pd.date_range(NOW.subtract(hours=48).start_of("hour"), periods=96, freq="h")
    weather = Mock()

    async def raw_series(key: str, dropna: bool = True):
        if key == "weather_temp_air":
            return pd.Series([5.0] * len(hours), index=hours)
        if key == "weather_ghi":
            return pd.Series([120.0] * len(hours), index=hours)
        raise KeyError(key)

    weather.key_to_raw_series = raw_series
    samples, filled = await gather_samples(device, isolated_measurement, weather, NOW)
    assert filled == 2 * 49  # 48 h back from 09:07 touches 49 hourly buckets
    assert len(samples) >= 3 * 24
    recent = [s for s in samples if s.timestamp >= NOW.subtract(hours=47)]
    assert all(s.outdoor_c == pytest.approx(5.0) for s in recent)
    assert all(s.ghi_w_per_m2 == pytest.approx(120.0) for s in recent)
    older = [s for s in samples if s.timestamp < NOW.subtract(hours=49)]
    assert older and all(s.outdoor_c is None and s.ghi_w_per_m2 is None for s in older)
    assert all(s.room_c == pytest.approx(21.0) for s in samples)
    # the filled hours were written to the measurement store
    dates, values = await isolated_measurement.key_to_lists(
        key="wbb12-ghi", start_datetime=NOW.subtract(days=3), end_datetime=NOW
    )
    assert len(values) == 49 and values[0] == pytest.approx(120.0)
    # a second pass finds them and fills nothing
    _, filled_again = await gather_samples(device, isolated_measurement, weather, NOW)
    assert filled_again == 0
    # a measured outdoor temperature is never overwritten by the forecast
    await isolated_measurement.update_value(
        NOW.subtract(hours=3).start_of("hour"), "wbb12-outdoor-temperature", -7.0
    )
    samples, _ = await gather_samples(device, isolated_measurement, weather, NOW)
    measured = [s for s in samples if s.timestamp == NOW.subtract(hours=3).start_of("hour")]
    assert measured[0].outdoor_c == pytest.approx(-7.0)


@pytest.mark.asyncio
async def test_gather_samples_without_weather_provider(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    _configure(config_eos, tmp_path)
    device = config_eos.devices.heat_pumps["wbb12"]
    await _feed(isolated_measurement, plant_history(days=2))
    samples, filled = await gather_samples(device, isolated_measurement, None, NOW)
    assert filled == 0
    assert len(samples) == 49  # the window edges are partial hours
    assert samples[0].outdoor_c is not None and samples[0].ghi_w_per_m2 is None


# ----------------------------------------------------------------------
# calibrate_device: fit, store, cadence
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibrate_device_fits_stores_and_reuses(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    _configure(config_eos, tmp_path)
    device = config_eos.devices.heat_pumps["wbb12"]
    await _feed(isolated_measurement, plant_history(days=14))
    result = await calibrate_device(
        device, isolated_measurement, None, NOW, tmp_path, "Europe/Berlin"
    )
    assert result is not None and result.building and result.cop and result.dhw
    assert result.building.loss_w_per_k == pytest.approx(H_TRUE, rel=0.05)
    assert result.building.internal_gains_w == pytest.approx(GAINS_TRUE, rel=0.2)
    assert not result.building.solar_fitted
    assert result.cop.cop_scale == pytest.approx(SCALE_TRUE, rel=0.03)
    assert result.dhw.loss_w_per_k == pytest.approx(UA_TRUE, rel=0.15)
    store_path = tmp_path / "rl29" / "heatpump_calibration.json"
    stored = json.loads(store_path.read_text())
    assert stored["devices"]["wbb12"]["result"]["cop"]["cop_scale"] == result.cop.cop_scale
    assert "COP scale 1.000 ->" in stored["devices"]["wbb12"]["summary"]
    assert HeatPumpCalibrationStore(store_path).result("wbb12") == result

    # within refit_hours the stored result is used, even when the data changed
    await isolated_measurement.delete_by_datetime(None, None)
    again = await calibrate_device(
        device, isolated_measurement, None, NOW.add(hours=3), tmp_path, "Europe/Berlin"
    )
    assert again == result
    # after refit_hours a new fit runs; without data it fits nothing
    later = await calibrate_device(
        device, isolated_measurement, None, NOW.add(hours=30), tmp_path, "Europe/Berlin"
    )
    assert later is not None and later.building is None and later.cop is None
    assert later.fitted_at != result.fitted_at
    assert device.calibrated(later) is device


@pytest.mark.asyncio
async def test_calibrate_device_respects_the_fit_switches(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    _configure(config_eos, tmp_path, fit_building=False, fit_dhw=False)
    device = config_eos.devices.heat_pumps["wbb12"]
    await _feed(isolated_measurement, plant_history(days=7))
    result = await calibrate_device(
        device, isolated_measurement, None, NOW, tmp_path, "Europe/Berlin"
    )
    assert result is not None
    assert result.building is None and result.dhw is None and result.cop is not None
    assert device.calibrated(result).building.loss_w_per_k == 150.0


@pytest.mark.asyncio
async def test_disabled_calibration_does_nothing(config_eos: ConfigEOS, tmp_path: Path):
    _configure(config_eos, tmp_path, enabled=False)
    device = config_eos.devices.heat_pumps["wbb12"]
    measurement = Mock(key_to_lists=AsyncMock(side_effect=AssertionError("not read")))
    assert await calibrate_device(device, measurement, None, NOW, tmp_path) is None
    assert not (tmp_path / "rl29").exists()


# ----------------------------------------------------------------------
# Hook in the optimization request
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_plans_with_the_fitted_parameters(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    _configure(config_eos, tmp_path)
    await _feed(isolated_measurement, plant_history(days=14))
    await isolated_measurement.update_value(NOW, "wbb12-room-temperature", 20.8)
    await isolated_measurement.update_value(NOW, "wbb12-dhw-temperature", 44.0)
    get_ems(init=True).set_start_datetime(NOW, interval_seconds=3600)

    prepared = await _request().resolve()
    assert prepared.heat_pumps is not None
    state = prepared.heat_pumps[0]
    assert state.initial_room_c == pytest.approx(20.8)
    assert state.settings.unit.cop_scale == pytest.approx(SCALE_TRUE, rel=0.03)
    assert state.settings.building.loss_w_per_k == pytest.approx(H_TRUE, rel=0.05)
    # the configuration keeps the prior
    assert config_eos.devices.heat_pumps["wbb12"].unit.cop_scale == 1.0
    assert config_eos.devices.heat_pumps["wbb12"].building.loss_w_per_k == 150.0
    assert (tmp_path / "rl29" / "heatpump_calibration.json").exists()


@pytest.mark.asyncio
async def test_calibration_errors_never_cancel_the_run(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path, monkeypatch
):
    _configure(config_eos, tmp_path)
    await isolated_measurement.update_value(NOW, "wbb12-room-temperature", 20.8)
    get_ems(init=True).set_start_datetime(NOW, interval_seconds=3600)

    async def boom(*args, **kwargs):
        raise RuntimeError("database gone")

    import akkudoktoreos.devices.heatpumpcalibration as module

    monkeypatch.setattr(module, "calibrate_device", boom)
    prepared = await _request().resolve()
    state = prepared.heat_pumps[0]
    assert state.settings.unit.cop_scale == 1.0
    assert state.initial_room_c == pytest.approx(20.8)
