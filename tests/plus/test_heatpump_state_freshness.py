"""Heat pump state from Home Assistant later in an hourly slot (eos-plus fork).

With 1 h GENETIC slots the EMS start is rounded down to the full hour, but the
freshness of device measurements is judged against the unrounded run time
(``observation_datetime``) with ``measurement_max_age_seconds`` (300 s). The
adapter must therefore stamp the values it reads with the run time, not with
the slot start - otherwise every run later than five minutes into the hour
falls back to the model defaults (room = setpoint 21.0 degC, hot water =
setpoint - hysteresis/2).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from akkudoktoreos.adapter.homeassistant import HomeAssistantAdapter
from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.core.ems import EnergyManagement, EnergyManagementStage
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.utils.datetimeutil import to_datetime

STATES = {
    "sensor.raumtemperatur_mittel": "20.57",
    "sensor.wh_warmwasser_warmwassertemperatur": "43.5",
    "sensor.wh_warmwasser_warmwassersolltemperatur": "45.0",
}


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


def _configure(config: ConfigEOS) -> None:
    config.merge_settings_from_dict(
        {
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
                "max_heat_pumps": 1,
                "heat_pumps": {"wbb12": {}},
            },
            "adapter": {
                "provider": ["HomeAssistant"],
                "homeassistant": {
                    "device_measurement_entity_ids": {
                        "wbb12-room-temperature": "sensor.raumtemperatur_mittel",
                        "wbb12-dhw-temperature": "sensor.wh_warmwasser_warmwassertemperatur",
                        "wbb12-dhw-setpoint": "sensor.wh_warmwasser_warmwassersolltemperatur",
                    }
                },
            },
        }
    )


def _adapter(monkeypatch: pytest.MonkeyPatch) -> HomeAssistantAdapter:
    """Singleton adapter without instance overrides leaked by other tests."""
    adapter = HomeAssistantAdapter()
    monkeypatch.delitem(adapter.__dict__, "ems", raising=False)
    monkeypatch.delitem(adapter.__dict__, "measurement", raising=False)
    return adapter


def _request() -> ConfigOptimizationRequest:
    return ConfigOptimizationRequest.model_validate(
        {
            "forecasts": {
                "pv_forecast_wh": [0.0] * 48,
                "total_load": [300.0] * 48,
                "electricity_price_per_wh": [0.0003] * 48,
                "feed_in_tariff_per_wh": [0.00007] * 48,
                "temperature_forecast": [8.0] * 48,
                "ghi_forecast": [0.0] * 48,
            }
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_time",
    [
        "2026-10-22T17:00:30+02:00",  # right after the hour change
        "2026-10-22T17:11:01+02:00",  # observed outlier run
        "2026-10-22T13:38:41+02:00",  # observed outlier run
        "2026-10-22T12:52:46+02:00",  # late in the hour
    ],
)
async def test_heat_pump_state_uses_measurements_read_in_the_same_run(
    config_eos: ConfigEOS,
    isolated_measurement: Measurement,
    monkeypatch: pytest.MonkeyPatch,
    run_time: str,
):
    _configure(config_eos)
    observation = to_datetime(run_time, in_timezone="Europe/Berlin")
    ems = get_ems(init=True)
    ems.set_start_datetime(observation, interval_seconds=3600)
    assert ems.start_datetime.minute == 0  # slot start is rounded down

    monkeypatch.setattr(EnergyManagement, "_stage", EnergyManagementStage.DATA_ACQUISITION)
    monkeypatch.setattr(
        HomeAssistantAdapter, "get_entity_state", lambda self, entity_id: STATES[entity_id]
    )
    await _adapter(monkeypatch)._update_data()

    prepared = await _request().resolve()
    assert prepared.heat_pumps is not None
    state = prepared.heat_pumps[0]
    assert state.initial_room_c == pytest.approx(20.57)
    assert state.initial_dhw_c == pytest.approx(43.5)
    assert state.dhw_setpoint_c == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_device_measurement_is_stamped_with_the_run_time(
    config_eos: ConfigEOS,
    isolated_measurement: Measurement,
    monkeypatch: pytest.MonkeyPatch,
):
    _configure(config_eos)
    observation = to_datetime("2026-10-22T17:11:01+02:00", in_timezone="Europe/Berlin")
    get_ems(init=True).set_start_datetime(observation, interval_seconds=3600)
    monkeypatch.setattr(EnergyManagement, "_stage", EnergyManagementStage.DATA_ACQUISITION)
    monkeypatch.setattr(
        HomeAssistantAdapter, "get_entity_state", lambda self, entity_id: STATES[entity_id]
    )
    await _adapter(monkeypatch)._update_data()

    dates, values = await isolated_measurement.key_to_lists(
        key="wbb12-room-temperature",
        start_datetime=observation.subtract(hours=2),
        end_datetime=observation.add(seconds=1),
    )
    assert values == [pytest.approx(20.57)]
    assert dates[0].timestamp() == pytest.approx(observation.timestamp(), abs=1.0)
