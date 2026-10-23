"""Measurement key resolution in the Home Assistant adapter (issue #1302, RL29 fork)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from akkudoktoreos.adapter.homeassistant import (
    HomeAssistantAdapter,
    resolve_measurement_key,
)
from akkudoktoreos.core.ems import EnergyManagementStage

KNOWN = ["battery1-soc-factor", "battery1-power-l1-w", "washer.cycles_completed", "ev11-soc-factor"]


class TestResolveMeasurementKey:
    def test_exact_match(self):
        assert resolve_measurement_key("battery1-soc-factor", KNOWN) == ("battery1-soc-factor", "")

    @pytest.mark.parametrize(
        "configured",
        [
            "battery1_soc_factor",
            "Battery1-SoC-Factor",
            "battery1.soc_factor",
            " battery1_soc_factor ",
        ],
    )
    def test_separator_and_case_variants_are_corrected(self, configured):
        key, message = resolve_measurement_key(configured, KNOWN)
        assert key == "battery1-soc-factor"
        assert "Please correct" in message

    def test_dotted_device_key_variant(self):
        key, _ = resolve_measurement_key("washer_cycles_completed", KNOWN)
        assert key == "washer.cycles_completed"

    def test_unknown_key_with_suggestion(self):
        key, message = resolve_measurement_key("battery2-soc-factor", KNOWN)
        assert key is None
        assert "not registered" in message
        assert "battery1-soc-factor" in message

    def test_unknown_key_without_suggestion(self):
        key, message = resolve_measurement_key("heatpump-flow", KNOWN)
        assert key is None
        assert "Did you mean" not in message

    def test_ambiguous_normalisation(self):
        key, message = resolve_measurement_key("a_b", ["a-b", "a.b"])
        assert key is None
        assert "ambiguous" in message


@pytest.fixture
def ha_adapter(config_eos, monkeypatch):
    config_eos.merge_settings_from_dict(
        {
            "devices": {
                "max_batteries": 1,
                "batteries": {"battery1": {"capacity_wh": 11000}},
            },
            "adapter": {
                "provider": ["HomeAssistant"],
                "homeassistant": {
                    "device_measurement_entity_ids": {
                        "battery1_soc_factor": "sensor.byd_soc_factor",
                        "battery9-soc-factor": "sensor.unknown",
                    }
                },
            },
        }
    )
    adapter = HomeAssistantAdapter()
    ems = MagicMock()
    ems.stage.return_value = EnergyManagementStage.DATA_ACQUISITION
    object.__setattr__(adapter, "ems", ems)
    monkeypatch.setattr(
        HomeAssistantAdapter, "ems_start_datetime", "2026-10-21T00:00:00+02:00", raising=False
    )
    measurement = MagicMock()
    measurement.record_keys_writable = ["battery1-soc-factor", "battery1-power-l1-w"]
    measurement.update_value = AsyncMock()
    monkeypatch.setattr(HomeAssistantAdapter, "measurement", measurement, raising=False)
    states = {"sensor.byd_soc_factor": "0.42", "sensor.unknown": "0.9"}
    monkeypatch.setattr(
        HomeAssistantAdapter, "get_entity_state", lambda self, entity_id: states[entity_id]
    )
    return adapter, measurement


@pytest.mark.asyncio
async def test_adapter_writes_corrected_key(ha_adapter):
    adapter, measurement = ha_adapter
    await adapter._update_data()
    calls = [c.args for c in measurement.update_value.await_args_list]
    written = {key: value for (_, key, value) in calls}
    assert written["battery1-soc-factor"] == pytest.approx(0.42)
    assert "battery9-soc-factor" not in written
    assert "battery1_soc_factor" not in written


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "unavailable", "Unknown "])
async def test_missing_state_is_skipped_and_names_the_entity(ha_adapter, monkeypatch, state):
    """After a Home Assistant restart template sensors are 'unknown' for a moment."""
    from loguru import logger

    adapter, measurement = ha_adapter
    states = {"sensor.byd_soc_factor": state, "sensor.unknown": "0.9"}
    monkeypatch.setattr(
        HomeAssistantAdapter, "get_entity_state", lambda self, entity_id: states[entity_id]
    )
    messages: list[tuple[str, str]] = []
    sink = logger.add(lambda m: messages.append((m.record["level"].name, m.record["message"])))
    try:
        await adapter._update_data()
    finally:
        logger.remove(sink)
    written = {key for (_, key, _v) in (c.args for c in measurement.update_value.await_args_list)}
    assert "battery1-soc-factor" not in written
    skipped = [m for level, m in messages if level == "WARNING" and "skipped" in m]
    assert skipped and "sensor.byd_soc_factor" in skipped[0]
    assert "battery1-soc-factor" in skipped[0]
    assert not [m for level, m in messages if level == "ERROR" and "convert" in m]


@pytest.mark.asyncio
async def test_unparsable_state_error_names_the_entity(ha_adapter, monkeypatch):
    from loguru import logger

    adapter, measurement = ha_adapter
    states = {"sensor.byd_soc_factor": "kaputt", "sensor.unknown": "0.9"}
    monkeypatch.setattr(
        HomeAssistantAdapter, "get_entity_state", lambda self, entity_id: states[entity_id]
    )
    messages: list[tuple[str, str]] = []
    sink = logger.add(lambda m: messages.append((m.record["level"].name, m.record["message"])))
    try:
        await adapter._update_data()
    finally:
        logger.remove(sink)
    errors = [m for level, m in messages if level == "ERROR" and "convert" in m]
    assert errors and "sensor.byd_soc_factor" in errors[0]
