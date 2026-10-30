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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EosControlConfigEntry
from .charts import parse_time
from .const import CONF_DEVICE_ID, CONF_EOS_DEVICE, CONF_KIND
from .coordinator import ControlData
from .devices import KIND_APPLIANCE, KIND_BATTERY, KIND_EV, OPERATION_MODES, DeviceState
from .entity import EosControlEntity, EosDeviceEntity
from .logic import MODE_NAMES


@dataclass(frozen=True, kw_only=True)
class EosSensorDescription(SensorEntityDescription):
    value: Callable[[ControlData], Any]
    attributes: Callable[[ControlData], dict[str, Any]] | None = None
    available: Callable[[ControlData], bool] | None = None
    heat_pump: bool = True
    """Only with a heat pump (entry without heat pump: charts and devices only)."""


@dataclass(frozen=True, kw_only=True)
class EosDeviceSensorDescription(SensorEntityDescription):
    kinds: tuple[str, ...]
    value: Callable[[DeviceState], Any]
    attributes: Callable[[DeviceState], dict[str, Any]] | None = None


# Series for charts: large, kept out of the recorder (the state machine has them).
CHART_ATTRIBUTES = frozenset({"data", "hourly", "kpi", "plan", "days", "ab"})


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


CHART_SENSORS: tuple[EosSensorDescription, ...] = (
    EosSensorDescription(
        key="plan",
        heat_pump=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: d.charts.generated_at,
        attributes=lambda d: {
            "slot_minutes": d.charts.slot_minutes,
            "data": d.charts.plan,
            "hourly": d.charts.plan_hourly,
        },
    ),
    EosSensorDescription(
        key="forecast",
        heat_pump=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: d.charts.generated_at if d.charts.forecast else None,
        attributes=lambda d: {"data": d.charts.forecast, "hourly": d.charts.forecast_hourly},
    ),
    EosSensorDescription(
        key="planned_load_hour",
        heat_pump=False,
        value=lambda d: d.charts.load_hour_kwh,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EosSensorDescription(
        key="planned_heat_pump_hour",
        value=lambda d: d.charts.heat_pump_hour_kwh,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EosSensorDescription(
        key="planned_room_temperature",
        value=lambda d: d.charts.room_c,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EosSensorDescription(
        key="planned_dhw_temperature",
        value=lambda d: d.charts.dhw_c,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EosSensorDescription(
        key="history",
        heat_pump=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        available=lambda d: d.history is not None,
        value=lambda d: parse_time((d.history or {}).get("state")),
        attributes=lambda d: {
            key: (d.history or {}).get(key)
            for key in ("enabled", "last_error", "kpi", "plan", "days", "ab")
        },
    ),
    EosSensorDescription(
        key="history_hours",
        heat_pump=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        available=lambda d: d.history_hours is not None,
        value=lambda d: parse_time((d.history_hours or {}).get("state")),
        attributes=lambda d: {
            "data": (d.history_hours or {}).get("data") or [],
            "last_error": (d.history_hours or {}).get("last_error"),
        },
    ),
)


def _plan_attributes(state: DeviceState) -> dict[str, Any]:
    return {
        "factor": state.factor,
        "next_change": state.next_change.isoformat() if state.next_change else None,
        "next_mode": state.next_mode,
        "pending": state.pending,
    }


DEVICE_SENSORS: tuple[EosDeviceSensorDescription, ...] = (
    EosDeviceSensorDescription(
        key="operation_mode",
        kinds=(KIND_BATTERY, KIND_EV),
        device_class=SensorDeviceClass.ENUM,
        options=[m.lower() for m in OPERATION_MODES],
        value=lambda s: s.mode.lower() if s.mode else None,
        attributes=_plan_attributes,
    ),
    EosDeviceSensorDescription(
        key="mode_factor",
        kinds=(KIND_BATTERY, KIND_EV),
        native_unit_of_measurement=PERCENTAGE,
        value=lambda s: round(s.factor * 100.0, 1) if s.factor is not None else None,
    ),
    EosDeviceSensorDescription(
        key="planned_soc",
        kinds=(KIND_BATTERY, KIND_EV),
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: s.soc_pct,
    ),
    EosDeviceSensorDescription(
        key="charge_power_target",
        kinds=(KIND_BATTERY, KIND_EV),
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: s.power_w,
    ),
    EosDeviceSensorDescription(
        key="next_start",
        kinds=(KIND_APPLIANCE,),
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda s: s.next_start,
        attributes=lambda s: {
            "next_change": s.next_change.isoformat() if s.next_change else None,
            "pending": s.pending,
        },
    ),
    EosDeviceSensorDescription(
        key="device_plan",
        kinds=(KIND_BATTERY, KIND_EV, KIND_APPLIANCE),
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda s: parse_time(s.rows[0]["t"]) if s.rows else None,
        attributes=lambda s: {"data": s.rows},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EosControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    heat_pump = bool(entry.data.get(CONF_DEVICE_ID))
    async_add_entities(
        [
            *(EosSensor(coordinator, d) for d in SENSORS if heat_pump or not d.heat_pump),
            *(
                EosChartSensor(coordinator, d)
                for d in CHART_SENSORS
                if heat_pump or not d.heat_pump
            ),
        ]
    )
    for subentry in coordinator.device_subentries():
        device_id = str(subentry.data[CONF_EOS_DEVICE])
        kind = str(subentry.data[CONF_KIND])
        async_add_entities(
            [
                EosDeviceSensor(coordinator, device_id, kind, d)
                for d in DEVICE_SENSORS
                if kind in d.kinds
            ],
            config_subentry_id=subentry.subentry_id,
        )


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

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        check = self.entity_description.available
        return check is None or check(self.coordinator.data)


class EosChartSensor(EosSensor):
    """Plan, forecast and history series for dashboards (series not recorded)."""

    _unrecorded_attributes = CHART_ATTRIBUTES


class EosDeviceSensor(EosDeviceEntity, SensorEntity):
    """Planned state of an EOS device (battery, electric vehicle, home appliance)."""

    entity_description: EosDeviceSensorDescription
    _unrecorded_attributes = frozenset({"data"})

    def __init__(
        self, coordinator, device_id: str, kind: str, description: EosDeviceSensorDescription
    ) -> None:
        super().__init__(coordinator, device_id, kind, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        state = self.state_data
        return self.entity_description.value(state) if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.state_data
        if state is None or self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(state)
