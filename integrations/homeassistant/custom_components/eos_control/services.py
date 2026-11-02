"""Actions of EOS Control (instead of helper entities)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EosControlCoordinator

ATTR_ENTRY = "config_entry_id"
ATTR_UNTIL = "until"
ATTR_DURATION = "duration"
ATTR_START = "start"
ATTR_RETURN = "return_at"
ATTR_DEVICE = "device"
ATTR_FINISH_BY = "finish_by"

_ENTRY: dict[Any, Any] = {vol.Optional(ATTR_ENTRY): cv.string}

LOCK_SCHEMA = vol.All(
    vol.Schema(
        {
            **_ENTRY,
            vol.Exclusive(ATTR_UNTIL, "lock"): cv.datetime,
            vol.Exclusive(ATTR_DURATION, "lock"): cv.positive_time_period,
        }
    ),
    cv.has_at_least_one_key(ATTR_UNTIL, ATTR_DURATION),
)
ENTRY_SCHEMA = vol.Schema(_ENTRY)
HOLIDAY_SCHEMA = vol.Schema(
    {**_ENTRY, vol.Required(ATTR_RETURN): cv.datetime, vol.Optional(ATTR_START): cv.datetime}
)
RUN_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Required(ATTR_FINISH_BY): cv.datetime,
        vol.Optional(ATTR_DURATION): cv.positive_time_period,
    }
)
CANCEL_SCHEMA = vol.Schema({**_ENTRY, vol.Required(ATTR_DEVICE): cv.string})


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> EosControlCoordinator:
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_ENTRY)
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
    if len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_ambiguous" if entries else "entry_not_found",
        )
    return entries[0].runtime_data


def _local(value: datetime) -> datetime:
    return dt_util.as_local(
        value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())
    )


def async_setup_services(hass: HomeAssistant) -> None:
    async def lock_reduction(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        until = call.data.get(ATTR_UNTIL)
        coordinator.manual.lock_until = (
            _local(until) if until is not None else dt_util.now() + call.data[ATTR_DURATION]
        )
        await coordinator.async_manual_changed()

    async def clear_reduction_lock(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        coordinator.manual.lock_until = None
        await coordinator.async_manual_changed()

    async def set_holiday(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        end = _local(call.data[ATTR_RETURN])
        start = call.data.get(ATTR_START)
        start = _local(start) if start is not None else None
        if end <= (start or dt_util.now()):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="return_before_start"
            )
        coordinator.manual.holiday_start = start
        coordinator.manual.holiday_end = end
        await coordinator.async_manual_changed()

    async def end_holiday(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        coordinator.manual.holiday_start = coordinator.manual.holiday_end = None
        await coordinator.async_manual_changed()

    async def request_run(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        device = call.data[ATTR_DEVICE]
        appliances = {
            str(s.data.get("eos_device_id"))
            for s in coordinator.device_subentries("home_appliance")
        }
        known = {d.device_id for d in coordinator.eos_devices if d.kind == "home_appliance"}
        if device not in appliances | known:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_appliance",
                translation_placeholders={"device": device},
            )
        finish_by = _local(call.data[ATTR_FINISH_BY])
        if finish_by <= dt_util.now():
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="finish_in_the_past"
            )
        duration: timedelta | None = call.data.get(ATTR_DURATION)
        coordinator.manual.request(
            device, finish_by, duration.total_seconds() / 3600 if duration else None
        )
        await coordinator.async_manual_changed()

    async def cancel_run(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        coordinator.manual.cancel(call.data[ATTR_DEVICE])
        await coordinator.async_manual_changed()

    services: dict[str, tuple[Any, Any]] = {
        "lock_reduction": (lock_reduction, LOCK_SCHEMA),
        "clear_reduction_lock": (clear_reduction_lock, ENTRY_SCHEMA),
        "set_holiday": (set_holiday, HOLIDAY_SCHEMA),
        "end_holiday": (end_holiday, ENTRY_SCHEMA),
        "request_run": (request_run, RUN_SCHEMA),
        "cancel_run": (cancel_run, CANCEL_SCHEMA),
    }
    for name, (handler, schema) in services.items():
        hass.services.async_register(DOMAIN, name, handler, schema=schema)
