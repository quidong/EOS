"""Button to reset the comfort learning."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EosControlConfigEntry
from .entity import EosControlEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EosControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for subentry in coordinator.device_subentries("heat_pump")[:1]:
        # the comfort learning belongs to the heat pump
        async_add_entities(
            [ResetComfortButton(coordinator, "reset_comfort")],
            config_subentry_id=subentry.subentry_id,
        )


class ResetComfortButton(EosControlEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.coordinator.async_reset_comfort()
