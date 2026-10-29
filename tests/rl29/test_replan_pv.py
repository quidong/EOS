"""Re-planning when PV deviates from the forecast (fork).

``ems.replan_pv_deviation_w`` compares the mean PV power of the last complete slot,
taken from the PV production meters, with the forecast the current plan used.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core import coreabc
from akkudoktoreos.core.coreabc import get_measurement
from akkudoktoreos.core.replan import _meter_energy_kwh, deviation_reasons
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.utils.datetimeutil import to_datetime


class _Meters:
    """Cumulative meters [kWh], read at 10:00 and 10:15 (last complete slot)."""

    def __init__(self, readings: dict[str, list[float | None]]):
        self.readings = readings

    async def key_to_lists(self, key, start_datetime, end_datetime):
        if key not in self.readings:
            return [], []
        times = [to_datetime("2026-10-26T10:00:00Z"), to_datetime("2026-10-26T10:15:00Z")]
        return times, self.readings[key]


def _ems(pv_plan_wh: float, limit_w=1500.0, keys=("pv-meter",)):
    generated = to_datetime("2026-10-26T10:00:00Z")
    index = pd.date_range(generated.isoformat(), periods=4, freq="15min")
    prediction = pd.DataFrame({"pvforecast_ac_energy_wh": [pv_plan_wh] * 4}, index=index)
    solution = SimpleNamespace(
        generated_at=generated,
        solution=SimpleNamespace(to_dataframe=lambda: pd.DataFrame(index=index)),
        prediction=SimpleNamespace(to_dataframe=lambda: prediction),
    )
    config = SimpleNamespace(
        ems=SimpleNamespace(
            replan_soc_deviation_pct=10.0,
            replan_temperature_deviation_k=0.8,
            replan_pv_deviation_w=limit_w,
        ),
        devices=SimpleNamespace(batteries={}, heat_pumps={}),
        measurement=SimpleNamespace(pv_production_emr_keys=list(keys)),
    )
    return SimpleNamespace(config=config, optimization_solution=lambda: solution)


NOW = to_datetime("2026-10-26T10:20:00Z")  # last complete slot 10:00-10:15


@pytest.mark.asyncio
async def test_clouds_trigger_a_run(monkeypatch):
    # plan 1250 Wh per 15 min = 5000 W, measured 0.3 kWh = 1200 W
    monkeypatch.setattr(coreabc, "get_measurement", lambda: _Meters({"pv-meter": [100.0, 100.3]}))
    reasons = await deviation_reasons(_ems(1250.0), NOW)
    assert reasons == ["PV W: measured 1200.0, planned 5000.0"]


@pytest.mark.asyncio
async def test_small_deviation_and_missing_readings_do_not(monkeypatch):
    monkeypatch.setattr(coreabc, "get_measurement", lambda: _Meters({"pv-meter": [100.0, 101.0]}))
    assert await deviation_reasons(_ems(1250.0), NOW) == []  # 4000 vs 5000 W, limit 1500
    monkeypatch.setattr(coreabc, "get_measurement", lambda: _Meters({"pv-meter": [100.0, None]}))
    assert await deviation_reasons(_ems(1250.0), NOW) == []
    # meter reset (second < first) is no measurement either
    monkeypatch.setattr(coreabc, "get_measurement", lambda: _Meters({"pv-meter": [100.0, 0.5]}))
    assert await deviation_reasons(_ems(1250.0), NOW) == []


@pytest.mark.asyncio
async def test_off_without_limit_or_meters(monkeypatch):
    monkeypatch.setattr(coreabc, "get_measurement", lambda: _Meters({"pv-meter": [100.0, 100.0]}))
    assert await deviation_reasons(_ems(1250.0, limit_w=None), NOW) == []
    assert await deviation_reasons(_ems(1250.0, keys=()), NOW) == []


@pytest.mark.asyncio
async def test_several_meters_are_summed(monkeypatch):
    meters = _Meters({"a": [10.0, 10.5], "b": [20.0, 20.75]})  # 1.25 kWh = 5000 W
    monkeypatch.setattr(coreabc, "get_measurement", lambda: meters)
    assert await deviation_reasons(_ems(1250.0, keys=("a", "b")), NOW) == []


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_meter_energy_from_the_measurement_store(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    config_eos.merge_settings_from_dict({"measurement": {"pv_production_emr_keys": ["pv-meter"]}})
    start = to_datetime("2026-10-26T10:00:00Z")
    # readings every 4 min, not on the slot boundaries: interpolated
    for minutes in (-2, 2, 6, 10, 14, 18):
        await isolated_measurement.update_value(
            start.add(minutes=minutes), "pv-meter", 100.0 + 0.02 * minutes
        )
    energy = await _meter_energy_kwh(isolated_measurement, "pv-meter", start, 900)
    assert energy == pytest.approx(0.3)
    assert (
        await _meter_energy_kwh(isolated_measurement, "pv-meter", start.add(hours=2), 900) is None
    )
