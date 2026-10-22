"""Learned load profiles per program (RL29 fork).

Covers run detection from a measured power signal, the online learning of
profiles (averaging, moving average, length), persistence, the history import
of the Home Assistant adapter and the use of a learned profile for a
request-driven appliance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.adapter import homeassistant as ha_module
from akkudoktoreos.adapter.homeassistant import HomeAssistantAdapter
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.core.ems import EnergyManagementStage
from akkudoktoreos.devices.applianceprofiles import (
    PROFILE_INTERVAL_SECONDS,
    ApplianceProfileStore,
    blend_profiles,
    detect_runs,
)
from akkudoktoreos.devices.settings.homeappliancesettings import HomeApplianceCommonSettings
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.utils.datetimeutil import to_datetime

T0 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


def wash(start: datetime, heat_w: float = 4200.0, heat_min: int = 20, rest_min: int = 120):
    """Minute samples of a washing cycle: heating, then washing with short pauses."""
    samples = [(start - timedelta(minutes=5), 0.0)]
    for minute in range(heat_min):
        samples.append((start + timedelta(minutes=minute), heat_w))
    for minute in range(heat_min, heat_min + rest_min):
        # drum pauses of 4 min every 20 min stay within the run
        paused = (minute % 20) in (0, 1, 2, 3)
        samples.append((start + timedelta(minutes=minute), 0.0 if paused else 180.0))
    samples.append((start + timedelta(minutes=heat_min + rest_min), 0.0))
    return samples


# ----------------------------------------------------------------------
# Run detection
# ----------------------------------------------------------------------


def test_one_finished_run_is_detected_with_its_program():
    power = wash(T0)
    program = [(T0 - timedelta(minutes=30), 3.0)]
    runs = detect_runs(power, program, now=T0 + timedelta(hours=4))
    assert len(runs) == 1
    run = runs[0]
    assert run.program == "3"
    assert run.start == T0
    assert (run.end - run.start) == timedelta(minutes=140)
    assert run.power_w[0] == pytest.approx(4200.0)
    assert len(run.power_w) == 140 * 60 // PROFILE_INTERVAL_SECONDS
    assert run.energy_wh == pytest.approx(4200 * 20 / 60 + 180 * 120 * 0.8 / 60, rel=0.05)


def test_running_cycle_is_not_learned_yet():
    power = wash(T0)[:60]  # stopped in the middle, still at 180 W
    assert detect_runs(power, now=T0 + timedelta(minutes=62)) == []


def test_short_blips_are_ignored():
    power = [(T0, 0.0), (T0 + timedelta(minutes=1), 500.0), (T0 + timedelta(minutes=3), 0.0)]
    assert detect_runs(power, now=T0 + timedelta(hours=1)) == []


def test_two_runs_and_unknown_program():
    power = wash(T0) + wash(T0 + timedelta(hours=5))
    program = [(T0 + timedelta(hours=4, minutes=50), 7.0)]
    runs = detect_runs(power, program, now=T0 + timedelta(hours=10), program_lookback_seconds=3600)
    assert [r.program for r in runs] == [None, "7"]


# ----------------------------------------------------------------------
# Learning
# ----------------------------------------------------------------------


def test_blend_profiles_averages_values_and_length():
    assert blend_profiles([100.0, 100.0], [300.0, 300.0, 300.0, 300.0], 0.5) == [
        200.0,
        200.0,
        150.0,
    ]


def test_store_averages_first_runs_then_moving_average(tmp_path: Path):
    store = ApplianceProfileStore(tmp_path / "profiles.json")
    runs = [
        detect_runs(
            wash(T0 + timedelta(days=d), heat_w=w),
            [(T0 + timedelta(days=d), 3.0)],
            now=T0 + timedelta(days=d, hours=4),
        )[0]
        for d, w in ((0, 4000.0), (1, 4400.0), (2, 4200.0), (3, 5200.0))
    ]
    assert store.learn("waschmaschine", runs[:2]) == 2
    profile = store.profile("waschmaschine", "3")
    assert profile is not None
    assert profile["runs"] == 2
    assert profile["power_w"][0] == pytest.approx(4200.0)  # mean of 4000 and 4400
    store.learn("waschmaschine", runs[2:3])
    assert store.profile("waschmaschine", "3")["power_w"][0] == pytest.approx(4200.0)
    store.learn("waschmaschine", runs[3:], learning_rate=0.3)
    # fourth run: weight max(1/4, 0.3) = 0.3
    assert store.profile("waschmaschine", "3")["power_w"][0] == pytest.approx(
        0.7 * 4200.0 + 0.3 * 5200.0
    )
    assert store.profile("waschmaschine", "3")["runs"] == 4


def test_runs_are_learned_once_and_persisted(tmp_path: Path):
    path = tmp_path / "profiles.json"
    store = ApplianceProfileStore(path)
    runs = detect_runs(wash(T0), [(T0, 2.0)], now=T0 + timedelta(hours=4))
    assert store.learn("waschmaschine", runs) == 1
    assert store.learn("waschmaschine", runs) == 0
    store.save()
    reloaded = ApplianceProfileStore(path)
    assert reloaded.profile("waschmaschine", "2")["runs"] == 1
    assert reloaded.learn("waschmaschine", runs) == 0


def test_corrupt_store_starts_empty(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text("{not json", encoding="utf-8")
    assert ApplianceProfileStore(path).profile("waschmaschine", "1") is None


# ----------------------------------------------------------------------
# Settings and adapter
# ----------------------------------------------------------------------


def test_learning_keys_are_registered():
    device = HomeApplianceCommonSettings(
        device_id="waschmaschine",
        consumption_wh=1200,
        duration_h=3,
        num_cycles=1,
        learn_profiles=True,
    )
    assert "waschmaschine-power" in device.measurement_keys
    assert "waschmaschine-program" in device.measurement_keys


@pytest.mark.asyncio
async def test_adapter_imports_the_history_of_marked_keys(config_eos, monkeypatch):
    config_eos.merge_settings_from_dict(
        {
            "adapter": {
                "provider": ["HomeAssistant"],
                "homeassistant": {
                    "device_measurement_entity_ids": {
                        "waschmaschine-power": "sensor.strom_waschmaschine",
                        "waschmaschine-program": "sensor.programm_code",
                    },
                    "device_measurement_history_keys": ["waschmaschine-power"],
                },
            },
        }
    )
    adapter = HomeAssistantAdapter()
    ems = MagicMock()
    ems.stage.return_value = EnergyManagementStage.DATA_ACQUISITION
    object.__setattr__(adapter, "ems", ems)
    monkeypatch.setattr(
        HomeAssistantAdapter, "ems_start_datetime", "2026-10-22T10:00:00+02:00", raising=False
    )
    measurement = MagicMock()
    measurement.record_keys_writable = ["waschmaschine-power", "waschmaschine-program"]
    measurement.update_value = AsyncMock()
    monkeypatch.setattr(HomeAssistantAdapter, "measurement", measurement, raising=False)
    monkeypatch.setattr(HomeAssistantAdapter, "get_entity_state", lambda self, entity_id: "5")
    history = [
        (to_datetime("2026-10-22T09:00:00+02:00"), 0.0),
        (to_datetime("2026-10-22T09:01:00+02:00"), 4200.0),
        (to_datetime("2026-10-22T09:21:00+02:00"), 180.0),
    ]
    calls: list[Any] = []

    def fake_history(self, entity_id, start, end):
        calls.append((entity_id, start, end))
        return history

    monkeypatch.setattr(HomeAssistantAdapter, "get_entity_history", fake_history)
    ha_module._HISTORY_IMPORTED.clear()
    await adapter._update_data()
    written = [c.args for c in measurement.update_value.await_args_list]
    power = [(when, value) for (when, key, value) in written if key == "waschmaschine-power"]
    assert [value for _, value in power] == [0.0, 4200.0, 180.0]
    assert any(key == "waschmaschine-program" and value == 5.0 for (_, key, value) in written)
    assert calls[0][0] == "sensor.strom_waschmaschine"
    # the next import continues where the last one ended
    await adapter._update_data()
    assert calls[1][1] == calls[0][2]


# ----------------------------------------------------------------------
# Planning with a learned profile
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def clean_measurements():
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    yield
    await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_request_uses_the_learned_profile_of_the_program(
    config_eos, clean_measurements, tmp_path
):
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": "Europe/Berlin", "data_folder_path": str(tmp_path)},
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {"horizon_hours": 24, "interval_sec": 3600, "tail_horizon_hours": 0},
            },
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "max_home_appliances": 1,
                "home_appliances": {
                    "waschmaschine": {
                        "device_id": "waschmaschine",
                        "consumption_wh": 1200,
                        "duration_h": 3,
                        "num_cycles": 1,
                        "request_driven": True,
                        "learn_profiles": True,
                    }
                },
                "max_heat_pumps": 0,
                "heat_pumps": {},
            },
        }
    )
    start = pendulum.datetime(2026, 11, 3, 18, 0, tz="Europe/Berlin")
    get_ems(init=True).set_start_datetime(start)
    measurement = get_measurement()
    yesterday = start.subtract(hours=20)
    await measurement.update_value(yesterday.subtract(minutes=30), "waschmaschine-program", 3.0)
    for when, value in wash(yesterday):
        await measurement.update_value(when, "waschmaschine-power", value)
    now = start.subtract(minutes=2)
    await measurement.update_value(now, "waschmaschine-program", 3.0)
    await measurement.update_value(now, "waschmaschine-requested", 1.0)
    request = ConfigOptimizationRequest.model_validate(
        {
            "forecasts": {
                "pv_forecast_wh": [0.0] * 48,
                "total_load": [300.0] * 48,
                "electricity_price_per_wh": [0.0003] * 48,
                "feed_in_tariff_per_wh": [0.00007] * 48,
                "temperature_forecast": [5.0] * 48,
            }
        }
    )
    prepared = await request.resolve()
    (appliance,) = prepared.resolved_home_appliances()
    assert appliance.load_profile_interval_seconds == PROFILE_INTERVAL_SECONDS
    assert appliance.load_profile_power_w is not None
    assert appliance.load_profile_power_w[0] == pytest.approx(4200.0)
    assert (tmp_path / "rl29" / "appliance_profiles.json").exists()

    # a program without a learned profile keeps the configured load
    await measurement.update_value(start.subtract(minutes=1), "waschmaschine-program", 9.0)
    prepared = await request.resolve()
    (appliance,) = prepared.resolved_home_appliances()
    assert appliance.load_profile_power_w is None
    assert appliance.consumption_wh == 1200
