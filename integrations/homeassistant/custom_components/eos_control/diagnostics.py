"""Diagnostics of EOS Control (EOS URL redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EosControlConfigEntry
from .const import CONF_URL

TO_REDACT = {CONF_URL}


def _measurements(entry: EosControlConfigEntry) -> dict[str, Any] | None:
    """Values the integration would send to EOS now (without the URL)."""
    coordinator = entry.runtime_data
    data = coordinator.data
    if data is None or coordinator.last_input is None:
        return None
    return coordinator.measurement_values(
        coordinator.last_input.now, coordinator.last_input, data.decision
    )


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EosControlConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "rooms": [dict(s.data) for s in entry.runtime_data.room_subentries()],
        "eos_devices_added": [dict(s.data) for s in entry.runtime_data.device_subentries()],
        "decision": data.decision.as_attributes() if data else None,
        "shadow": data.shadow.as_dict() if data else None,
        "eos_error": data.eos_error if data else None,
        "eos_version": entry.runtime_data.eos_version,
        "measurements": _measurements(entry),
        "eos_devices": [d.__dict__ for d in data.eos_devices] if data else None,
        "devices": (
            {
                key: (
                    {k: v for k, v in state.__dict__.items() if k != "rows"}
                    if state is not None
                    else None
                )
                for key, state in data.devices.items()
            }
            if data
            else None
        ),
    }
