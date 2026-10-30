"""EOS Control: EOS plans for heat pump, batteries, EVs and appliances in Home Assistant.

The integration evaluates the EOS plan and its safety rules and publishes targets and
reasons; switching the devices stays with Home Assistant automations. One entry per
EOS instance; the devices of EOS (heat pump, batteries, electric vehicles, home
appliances) and the rooms of the heat pump are subentries.
"""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_ID,
    CONF_EOS_DEVICE,
    CONF_KIND,
    CONF_PLAN_ENTITIES,
    CONF_URL,
    DOMAIN,
    PLATFORMS,
    SUBENTRY_DEVICE,
)
from .coordinator import EosControlCoordinator
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type EosControlConfigEntry = ConfigEntry[EosControlCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EosControlConfigEntry) -> bool:
    coordinator = EosControlCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.cancel_settle)
    await hass.config_entries.async_forward_entry_setups(entry, [*PLATFORMS, "button"])
    _link_devices(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


def _link_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Heat pump, EOS devices and rooms below the "EOS" device."""
    registry = dr.async_get(hass)
    hub = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if hub is None:
        return
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if device.id != hub.id and device.via_device_id != hub.id:
            registry.async_update_device(device.id, via_device_id=hub.id)


async def async_unload_entry(hass: HomeAssistant, entry: EosControlConfigEntry) -> bool:
    await entry.runtime_data.async_save_shadow()
    return await hass.config_entries.async_unload_platforms(entry, [*PLATFORMS, "button"])


async def _async_reload(hass: HomeAssistant, entry: EosControlConfigEntry) -> None:
    """Options, rooms or devices changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Version 1 (entry = one heat pump) -> 2 (entry = EOS, heat pump = device subentry).

    The heat pump entities keep their unique ids, so their entity ids stay the same.
    """
    if entry.version > 2:
        return False
    if entry.version == 1:
        device_id = str(entry.data.get(CONF_DEVICE_ID) or "")
        data = {k: v for k, v in entry.data.items() if k != CONF_DEVICE_ID}
        # EOS's own adapter may still publish sensor.eos_<column>: the plan column
        # entities stay off until that is switched off (options).
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options={**entry.options, CONF_PLAN_ENTITIES: False},
            title="EOS",
            unique_id=entry.data.get(CONF_URL),
            version=2,
        )
        present = {
            s.data.get(CONF_EOS_DEVICE)
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_DEVICE
        }
        if device_id and device_id not in present:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({CONF_EOS_DEVICE: device_id, CONF_KIND: "heat_pump"}),
                    subentry_type=SUBENTRY_DEVICE,
                    title=device_id,
                    unique_id=device_id,
                ),
            )
    return True
