"""Sensors of EOS Control."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EosControlConfigEntry
from .coordinator import ControlData
from .entity import EosControlEntity
from .logic import MODE_NAMES


@dataclass(frozen=True, kw_only=True)
class EosSensorDescription(SensorEntityDescription):
    value: Callable[[ControlData], Any]
    attributes: Callable[[ControlData], dict[str, Any]] | None = None


def _agreement(data: ControlData) -> float | None:
    stats = data.shadow
    if stats.compared == 0:
        return None
    return round(100.0 * (stats.compared - stats.mismatches) / stats.compared, 1)


SENSORS: tuple[EosSensorDescription, ...] = (
    EosSensorDescription(
        key="sg_target",
        value=lambda d: d.decision.sg_target,
        attributes=lambda d: d.decision.as_attributes(),
    ),
    EosSensorDescription(key="reason", value=lambda d: d.decision.reason[:255]),
    EosSensorDescription(
        key="eos_mode",
        value=lambda d: (
            MODE_NAMES.get(d.decision.eos_mode) if d.decision.eos_mode is not None else None
        ),
        attributes=lambda d: {
            "plan_generated_at": d.plan.generated_at.isoformat()
            if d.plan is not None and d.plan.generated_at is not None
            else None,
            "eos_error": d.eos_error,
        },
    ),
    EosSensorDescription(
        key="room_mean",
        value=lambda d: d.decision.room_mean_c,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EosSensorDescription(
        key="room_min_margin",
        value=lambda d: d.decision.room_min_margin_k,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        attributes=lambda d: {"room": d.decision.coldest_room},
    ),
    EosSensorDescription(
        key="room_max_margin",
        value=lambda d: d.decision.room_max_margin_k,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        attributes=lambda d: {"room": d.decision.warmest_room},
    ),
    EosSensorDescription(
        key="window_open_share",
        value=lambda d: d.decision.window_open_share,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EosSensorDescription(
        key="humidity_max",
        value=lambda d: d.decision.humidity_max_pct,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EosSensorDescription(
        key="shadow_agreement",
        value=_agreement,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        attributes=lambda d: {
            "compared": d.shadow.compared,
            "mismatches": d.shadow.mismatches,
            "reference_sg": d.reference_sg,
            "reference_standby": d.reference_standby,
            "last_mismatches": d.shadow.log[-10:],
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EosControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(EosSensor(coordinator, description) for description in SENSORS)


class EosSensor(EosControlEntity, SensorEntity):
    entity_description: EosSensorDescription

    def __init__(self, coordinator, description: EosSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator.data)
