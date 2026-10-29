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
from .coordinator import ControlData
from .entity import EosControlEntity


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EosControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(EosBinary(coordinator, d) for d in BINARY_SENSORS)


class EosBinary(EosControlEntity, BinarySensorEntity):
    entity_description: EosBinaryDescription

    def __init__(self, coordinator, description: EosBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.value(self.coordinator.data)
