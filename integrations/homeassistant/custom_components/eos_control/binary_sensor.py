"""Binary sensors of EOS Control."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EosControlConfigEntry
from .const import CONF_EOS_DEVICE, CONF_KIND
from .coordinator import ControlData
from .devices import KIND_APPLIANCE, KIND_BATTERY, KIND_EV, KIND_HEAT_PUMP, DeviceState
from .entity import EosControlEntity, EosDeviceEntity, EosRoomEntity


@dataclass(frozen=True, kw_only=True)
class EosBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[ControlData], bool]


BINARY_SENSORS: tuple[EosBinaryDescription, ...] = (
    EosBinaryDescription(key="standby", value=lambda d: d.decision.standby),
    EosBinaryDescription(key="dhw_charge", value=lambda d: d.decision.dhw_charge),
    EosBinaryDescription(
        key="dhw_setpoints_restore", value=lambda d: d.decision.dhw_setpoints_restore
    ),
    EosBinaryDescription(
        key="humidity_warning",
        value=lambda d: d.decision.humidity_warning,
        device_class=BinarySensorDeviceClass.MOISTURE,
    ),
    EosBinaryDescription(
        key="eos_plan_ok",
        value=lambda d: d.decision.eos_fresh,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
)


@dataclass(frozen=True, kw_only=True)
class EosDeviceBinaryDescription(BinarySensorEntityDescription):
    kinds: tuple[str, ...]
    value: Callable[[DeviceState], bool | None]


DEVICE_BINARY_SENSORS: tuple[EosDeviceBinaryDescription, ...] = (
    EosDeviceBinaryDescription(
        key="grid_charge",
        kinds=(KIND_BATTERY, KIND_EV),
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value=lambda s: s.grid_charge,
    ),
    EosDeviceBinaryDescription(
        key="discharge_allowed", kinds=(KIND_BATTERY,), value=lambda s: s.discharge_allowed
    ),
    EosDeviceBinaryDescription(
        key="grid_export", kinds=(KIND_BATTERY,), value=lambda s: s.grid_export
    ),
    EosDeviceBinaryDescription(
        key="running_planned",
        kinds=(KIND_APPLIANCE,),
        device_class=BinarySensorDeviceClass.RUNNING,
        value=lambda s: s.running,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EosControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for subentry in coordinator.device_subentries(KIND_HEAT_PUMP)[:1]:
        async_add_entities(
            [EosBinary(coordinator, d) for d in BINARY_SENSORS],
            config_subentry_id=subentry.subentry_id,
        )
    for subentry in coordinator.room_subentries():
        async_add_entities(
            [EosRoomBinary(coordinator, subentry, key) for key in ("airing", "humidity_high")],
            config_subentry_id=subentry.subentry_id,
        )
    for subentry in coordinator.device_subentries():
        if subentry.data.get(CONF_KIND) == KIND_HEAT_PUMP:
            continue
        device_id = str(subentry.data[CONF_EOS_DEVICE])
        kind = str(subentry.data[CONF_KIND])
        async_add_entities(
            [
                EosDeviceBinary(coordinator, device_id, kind, d)
                for d in DEVICE_BINARY_SENSORS
                if kind in d.kinds
            ],
            config_subentry_id=subentry.subentry_id,
        )


class EosBinary(EosControlEntity, BinarySensorEntity):
    entity_description: EosBinaryDescription

    def __init__(self, coordinator, description: EosBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.value(self.coordinator.data)


class EosDeviceBinary(EosDeviceEntity, BinarySensorEntity):
    entity_description: EosDeviceBinaryDescription

    def __init__(
        self, coordinator, device_id: str, kind: str, description: EosDeviceBinaryDescription
    ) -> None:
        super().__init__(coordinator, device_id, kind, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        state = self.state_data
        return self.entity_description.value(state) if state is not None else None


class EosRoomBinary(EosRoomEntity, BinarySensorEntity):
    """Airing (window open or run-on) and humidity of a room."""

    def __init__(self, coordinator, subentry, key: str) -> None:
        super().__init__(coordinator, subentry, key)
        self._key = key
        if key == "humidity_high":
            self._attr_device_class = BinarySensorDeviceClass.MOISTURE

    @property
    def is_on(self) -> bool | None:
        room = self.room
        if room is None:
            return None
        return room.airing if self._key == "airing" else room.humidity_high
