"""Base entities of EOS Control.

Devices in Home Assistant:

- "EOS" (the entry): plan, forecast and history of the house;
- the heat pump and every other EOS device (battery, electric vehicle, home
  appliance), each added as an EOS device subentry, below "EOS";
- every room (room subentry), below "EOS".

The heat pump keeps the unique ids ``<entry>_<key>`` of the versions before 0.5, so its
entity ids stay the same after the migration (one heat pump per EOS entry).
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME, DOMAIN
from .coordinator import EosControlCoordinator
from .devices import DeviceState
from .logic import RoomState

KIND_MODELS = {
    "heat_pump": "Heat pump",
    "battery": "Battery",
    "electric_vehicle": "Electric vehicle / wallbox",
    "home_appliance": "Home appliance",
}


def hub_device(coordinator: EosControlCoordinator) -> DeviceInfo:
    entry = coordinator.entry
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="EOS",
        manufacturer="EOS Control",
        model="EOS",
        entry_type=DeviceEntryType.SERVICE,
    )


def eos_device(coordinator: EosControlCoordinator, device_id: str, kind: str) -> DeviceInfo:
    entry = coordinator.entry
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{device_id}")},
        name=f"EOS {device_id}",
        manufacturer="EOS Control",
        model=KIND_MODELS.get(kind, kind),
        entry_type=DeviceEntryType.SERVICE,
        via_device=(DOMAIN, entry.entry_id),
    )


class EosControlEntity(CoordinatorEntity[EosControlCoordinator]):
    """Entity of the house ("EOS") or, with ``heat_pump``, of the heat pump."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EosControlCoordinator, key: str, heat_pump: bool = True
    ) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = (
            eos_device(coordinator, coordinator.heat_pump_id, "heat_pump")
            if heat_pump
            else hub_device(coordinator)
        )


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
        self._attr_device_info = eos_device(coordinator, device_id, kind)

    @property
    def state_data(self) -> DeviceState | None:
        data = self.coordinator.data
        return data.devices.get(self.device_id) if data is not None else None

    @property
    def available(self) -> bool:
        return super().available and self.state_data is not None


class EosRoomEntity(CoordinatorEntity[EosControlCoordinator]):
    """Entity of one room (room subentry)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EosControlCoordinator, subentry: Any, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self.room_key = subentry.subentry_id
        self.room_name = str(subentry.data.get(CONF_NAME, subentry.title))
        self._attr_unique_id = f"{entry.entry_id}_{subentry.subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_room_{subentry.subentry_id}")},
            name=f"EOS Raum {self.room_name}",
            manufacturer="EOS Control",
            model="Room",
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def room(self) -> RoomState | None:
        data = self.coordinator.data
        return data.rooms.get(self.room_key) if data is not None else None
