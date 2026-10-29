"""Diagnostics of EOS Control (EOS URL redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EosControlConfigEntry
from .const import CONF_URL

TO_REDACT = {CONF_URL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EosControlConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "rooms": [dict(s.data) for s in entry.subentries.values()],
        "decision": data.decision.as_attributes() if data else None,
        "shadow": data.shadow.as_dict() if data else None,
        "eos_error": data.eos_error if data else None,
    }
