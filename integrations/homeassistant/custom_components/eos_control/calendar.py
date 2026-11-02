"""Calendar of the EOS plan: heating phases, hot water, appliance runs, charging."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EosControlConfigEntry
from .entity import EosControlEntity
from .plan_events import PlanEvent, plan_events


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EosControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([EosPlanCalendar(entry.runtime_data, "plan_calendar", heat_pump=False)])


class EosPlanCalendar(EosControlEntity, CalendarEntity):
    """What EOS plans, as calendar events (see plan_events)."""

    def _events(self) -> list[PlanEvent]:
        data = self.coordinator.data
        if data is None:
            return []
        devices = [
            (device_id, state.kind, state.rows)
            for device_id, state in data.devices.items()
            if state is not None
        ]
        return plan_events(
            data.charts.plan,
            devices,
            data.charts.slot_minutes,
            self.hass.config.language,
        )

    @staticmethod
    def _event(event: PlanEvent) -> CalendarEvent:
        return CalendarEvent(start=event.start, end=event.end, summary=event.summary, uid=event.uid)

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        upcoming = [e for e in self._events() if e.end > now]
        return self._event(upcoming[0]) if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [self._event(e) for e in self._events() if e.end > start_date and e.start < end_date]
