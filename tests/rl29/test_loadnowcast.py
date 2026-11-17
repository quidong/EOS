"""Base load nowcast: measured base load blended into the first slots (RL29 fork)."""

from __future__ import annotations

from typing import AsyncGenerator

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_measurement
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.loadnowcast import (
    blend_measured,
    measured_base_load_wh,
)
from akkudoktoreos.prediction.loadml import hold_hourly_means

TZ = "Europe/Berlin"


class TestBlendMeasured:
    def test_linear_decay_from_first_slot(self):
        forecast = [100.0] * 8
        blended = blend_measured(forecast, 2, 500.0, 4)
        assert blended[:2] == [100.0, 100.0]
        assert blended[2:6] == pytest.approx([500.0, 400.0, 300.0, 200.0])
        assert blended[6:] == [100.0, 100.0]
        assert forecast == [100.0] * 8  # input untouched

    def test_off_and_invalid_measurement_keep_forecast(self):
        forecast = [100.0, 200.0]
        assert blend_measured(forecast, 0, 500.0, 0) == forecast
        assert blend_measured(forecast, 0, float("nan"), 4) == forecast
        assert blend_measured(forecast, 0, -1.0, 4) == forecast

    def test_decay_beyond_horizon_end_is_clipped(self):
        assert blend_measured([100.0, 100.0], 1, 300.0, 4) == pytest.approx([100.0, 300.0])


def test_hold_means_for_quarter_hours():
    start = 1_000_000.0
    samples = [(start - 60.0, 400.0), (start + 450.0, 800.0)]
    means = hold_hourly_means(samples, [start], 3600.0, 900.0)
    assert means == [pytest.approx(600.0)]
    # Default period stays one hour (backward compatible).
    assert hold_hourly_means(samples, [start], 7200.0) == [pytest.approx(750.0)]


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


def configure(config_eos: ConfigEOS) -> None:
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "measurement": {
                "load_emr_keys": ["load_mr"],
                "channels": {
                    "rod_power": {
                        "quantity": "power",
                        "unit": "W",
                        "integration_method": "hold",
                        "max_gap_seconds": 7200,
                    },
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_measured_base_load_subtracts_planned_consumers(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    configure(config_eos)
    slot_start = pendulum.datetime(2026, 11, 17, 20, 0, tz=TZ)
    begin = slot_start.subtract(minutes=15)
    # House meter: 1.5 kWh in the last quarter hour (6 kW average).
    await isolated_measurement.update_value(begin.subtract(minutes=15), "load_mr", 99.0)
    await isolated_measurement.update_value(begin, "load_mr", 100.0)
    await isolated_measurement.update_value(slot_start, "load_mr", 101.5)
    # Heating rod 3 kW over the whole slot -> 750 Wh of it is planned load.
    await isolated_measurement.update_value(begin.subtract(minutes=5), "rod_power", 3000.0)

    energy = await measured_base_load_wh(
        isolated_measurement, slot_start, 900, ["rod_power"], 3600.0
    )
    assert energy == pytest.approx(750.0, abs=1.0)

    without = await measured_base_load_wh(isolated_measurement, slot_start, 900, [], 3600.0)
    assert without == pytest.approx(1500.0, abs=1.0)


@pytest.mark.asyncio
async def test_measured_base_load_missing_consumer_value_gives_none(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    configure(config_eos)
    slot_start = pendulum.datetime(2026, 11, 17, 20, 0, tz=TZ)
    await isolated_measurement.update_value(slot_start.subtract(minutes=15), "load_mr", 100.0)
    await isolated_measurement.update_value(slot_start, "load_mr", 101.0)
    assert (
        await measured_base_load_wh(isolated_measurement, slot_start, 900, ["rod_power"], 3600.0)
        is None
    )


@pytest.mark.asyncio
async def test_measured_base_load_without_readings_gives_none(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    configure(config_eos)
    slot_start = pendulum.datetime(2026, 11, 17, 20, 0, tz=TZ)
    assert await measured_base_load_wh(isolated_measurement, slot_start, 900, [], 3600.0) is None
