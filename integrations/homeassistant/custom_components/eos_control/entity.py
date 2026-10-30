"""Base entity of EOS Control."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, DOMAIN
from .coordinator import EosControlCoordinator
from .devices import DeviceState


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
            model=(
                f"Heat pump {entry.data[CONF_DEVICE_ID]}"
                if entry.data.get(CONF_DEVICE_ID)
                else "EOS"
            ),
            entry_type=DeviceEntryType.SERVICE,
        )


KIND_MODELS = {
    "battery": "Battery",
    "electric_vehicle": "Electric vehicle / wallbox",
    "home_appliance": "Home appliance",
}


class EosDeviceEntity(CoordinatorEntity[EosControlCoordinator]):
    """Entity of one EOS device (battery, electric vehicle, home appliance)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EosControlCoordinator, device_id: str, kind: str, key: str
    ) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self.device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{device_id}")},
            name=f"EOS {device_id}",
            manufacturer="EOS Control",
            model=KIND_MODELS.get(kind, kind),
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def state_data(self) -> DeviceState | None:
        data = self.coordinator.data
        return data.devices.get(self.device_id) if data is not None else None

    @property
    def available(self) -> bool:
        return super().available and self.state_data is not None
