"""Base entity of EOS Control."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, DOMAIN
from .coordinator import EosControlCoordinator


class EosControlEntity(CoordinatorEntity[EosControlCoordinator]):
    """Entity of one EOS Control entry (one heat pump)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EosControlCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="EOS Control",
            model=f"Heat pump {entry.data[CONF_DEVICE_ID]} (shadow mode)",
            entry_type=DeviceEntryType.SERVICE,
        )
