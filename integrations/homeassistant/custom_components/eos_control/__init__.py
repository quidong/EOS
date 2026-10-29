"""EOS Control: executes EOS heat pump plans in Home Assistant.

Version 0.1 runs in shadow mode only: it evaluates the plan and the safety rules,
publishes the decision and compares it with the existing controller, but writes
nothing to the heat pump.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import EosControlCoordinator

type EosControlConfigEntry = ConfigEntry[EosControlCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EosControlConfigEntry) -> bool:
    coordinator = EosControlCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [*PLATFORMS, "button"])
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EosControlConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, [*PLATFORMS, "button"])


async def _async_reload(hass: HomeAssistant, entry: EosControlConfigEntry) -> None:
    """Options or rooms changed."""
    await hass.config_entries.async_reload(entry.entry_id)
