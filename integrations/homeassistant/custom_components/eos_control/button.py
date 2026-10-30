"""Buttons to reset the shadow comparison and the comfort learning."""

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
    if not entry.data.get("device_id"):
        return  # shadow comparison and comfort learning need a heat pump
    async_add_entities(
        [
            ResetShadowButton(entry.runtime_data, "reset_shadow"),
            ResetComfortButton(entry.runtime_data, "reset_comfort"),
        ]
    )


class ResetShadowButton(EosControlEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.coordinator.async_reset_shadow()


class ResetComfortButton(EosControlEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.coordinator.async_reset_comfort()
