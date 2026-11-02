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
from .const import CONF_ZONE, SUBENTRY_ROOM
from .coordinator import ControlData
from .entity import EosControlEntity
from .logic import MODE_NAMES, ZoneFigures


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
    EosSensorDescription(
        key="comfort_suggestions",
        value=lambda d: len(d.suggestions),
        state_class=SensorStateClass.MEASUREMENT,
        attributes=lambda d: {
            "suggestions": [s.as_dict() for s in d.suggestions],
            "events": d.comfort_events,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EosControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [EosSensor(coordinator, description) for description in SENSORS]
    zones = sorted(
        {
            str(sub.data.get(CONF_ZONE))
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_ROOM and sub.data.get(CONF_ZONE)
        }
    )
    for zone in zones:
        entities.extend(EosZoneSensor(coordinator, zone, d) for d in ZONE_SENSORS)
    async_add_entities(entities)


@dataclass(frozen=True, kw_only=True)
class EosZoneSensorDescription(SensorEntityDescription):
    value: Callable[[ZoneFigures], Any]


ZONE_SENSORS: tuple[EosZoneSensorDescription, ...] = (
    EosZoneSensorDescription(
        key="zone_mode",
        value=lambda z: MODE_NAMES.get(z.mode) if z.mode is not None else None,
    ),
    EosZoneSensorDescription(
        key="zone_room_mean",
        value=lambda z: z.room_mean_c,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EosZoneSensorDescription(
        key="zone_room_min_margin",
        value=lambda z: z.room_min_margin_k,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EosZoneSensorDescription(
        key="zone_room_max_margin",
        value=lambda z: z.room_max_margin_k,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
)


class EosZoneSensor(EosControlEntity, SensorEntity):
    """One figure of a heating zone (multi-zone EOS)."""

    entity_description: EosZoneSensorDescription

    def __init__(self, coordinator, zone: str, description: EosZoneSensorDescription) -> None:
        super().__init__(coordinator, f"{description.key}_{zone}")
        self.entity_description = description
        self._zone = zone
        self._attr_translation_key = description.key
        self._attr_translation_placeholders = {"zone": zone}

    def _figures(self) -> ZoneFigures | None:
        data = self.coordinator.data
        return data.decision.zones.get(self._zone) if data is not None else None

    @property
    def native_value(self) -> Any:
        figures = self._figures()
        return self.entity_description.value(figures) if figures is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        figures = self._figures()
        if figures is None or self.entity_description.key != "zone_mode":
            return None
        return figures.as_attributes()


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
