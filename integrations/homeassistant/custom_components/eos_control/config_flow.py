"""Config flow of EOS Control: EOS connection, options and rooms (subentries)."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AIRING_RUN_ON,
    CONF_DEVICE_ID,
    CONF_HEATING_SEASON_ENTITY,
    CONF_HEATING_SEASON_STATES,
    CONF_HOLIDAY_DHW_LEAD,
    CONF_HOLIDAY_ENTITY,
    CONF_HOLIDAY_FLOOR,
    CONF_HOLIDAY_RESTART,
    CONF_HOLIDAY_RETURN_ENTITY,
    CONF_HUMIDITY_DELAY,
    CONF_HUMIDITY_ENTITY,
    CONF_HUMIDITY_MAX,
    CONF_HYSTERESIS,
    CONF_IN_ZONE,
    CONF_LOWER,
    CONF_MAX_ROOM,
    CONF_MIN_VALID_ROOMS,
    CONF_NAME,
    CONF_PLAN_MAX_AGE,
    CONF_REDUCED_SETPOINT_ENTITY,
    CONF_REDUCTION_LOCK_ENTITY,
    CONF_SCAN_INTERVAL,
    CONF_SG_ACTUAL_ENTITY,
    CONF_SG_FORCED,
    CONF_SG_NORMAL,
    CONF_SG_PREHEAT,
    CONF_SG_RAISED,
    CONF_SG_REDUCED,
    CONF_SG_REFERENCE_ENTITY,
    CONF_STANDBY_REFERENCE_ENTITY,
    CONF_STANDBY_REFERENCE_STATE,
    CONF_TEMPERATURE_ENTITY,
    CONF_UPPER,
    CONF_URL,
    CONF_WINDOWS,
    CONF_ZONE_ALLOWED_ENTITY,
    DEFAULTS,
    DOMAIN,
    SUBENTRY_ROOM,
)

ENTITY_OPTIONS = (
    CONF_HOLIDAY_ENTITY,
    CONF_HOLIDAY_RETURN_ENTITY,
    CONF_REDUCED_SETPOINT_ENTITY,
    CONF_HEATING_SEASON_ENTITY,
    CONF_SG_ACTUAL_ENTITY,
    CONF_ZONE_ALLOWED_ENTITY,
    CONF_REDUCTION_LOCK_ENTITY,
    CONF_SG_REFERENCE_ENTITY,
    CONF_STANDBY_REFERENCE_ENTITY,
)


def _number(minimum: float, maximum: float, step: float, unit: str | None = None):
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _entity(domains: list[str] | None = None, multiple: bool = False):
    config = selector.EntitySelectorConfig(multiple=multiple)
    if domains:
        config = selector.EntitySelectorConfig(domain=domains, multiple=multiple)
    return selector.EntitySelector(config)


async def _check_eos(hass, url: str) -> str | None:
    """Error key if EOS does not answer at ``url``."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{url.rstrip('/')}/v1/health", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return "cannot_connect"
    except (aiohttp.ClientError, TimeoutError):
        return "cannot_connect"
    return None


class EosControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connection to EOS."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            device_id = user_input[CONF_DEVICE_ID].strip()
            await self.async_set_unique_id(f"{url}#{device_id}")
            self._abort_if_unique_id_configured()
            error = await _check_eos(self.hass, url)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"EOS {device_id}",
                    data={
                        CONF_URL: url,
                        CONF_DEVICE_ID: device_id,
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    },
                    options={k: v for k, v in DEFAULTS.items() if k in RULE_KEYS},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default="http://eos:8503"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Required(CONF_DEVICE_ID, default="hp1"): str,
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULTS[CONF_SCAN_INTERVAL]): _number(
                    15, 600, 5, "s"
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EosControlOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ROOM: RoomSubentryFlow}


RULE_KEYS = (
    CONF_SG_REDUCED,
    CONF_SG_NORMAL,
    CONF_SG_RAISED,
    CONF_SG_PREHEAT,
    CONF_SG_FORCED,
    CONF_PLAN_MAX_AGE,
    CONF_HYSTERESIS,
    CONF_MIN_VALID_ROOMS,
    CONF_MAX_ROOM,
    CONF_AIRING_RUN_ON,
    CONF_HOLIDAY_FLOOR,
    CONF_HOLIDAY_RESTART,
    CONF_HOLIDAY_DHW_LEAD,
    CONF_HEATING_SEASON_STATES,
    CONF_STANDBY_REFERENCE_STATE,
)


def _optional_entity(key: str, options: dict[str, Any]) -> vol.Optional:
    value = options.get(key)
    if value:
        return vol.Optional(key, description={"suggested_value": value})
    return vol.Optional(key)


class EosControlOptionsFlow(OptionsFlow):
    """Context entities, shadow comparison and rules."""

    def __init__(self) -> None:
        self._entities: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        if user_input is not None:
            self._entities = {k: user_input.get(k) for k in ENTITY_OPTIONS if user_input.get(k)}
            return await self.async_step_rules()
        schema = vol.Schema(
            {
                _optional_entity(CONF_HOLIDAY_ENTITY, options): _entity(
                    ["input_boolean", "binary_sensor", "switch"]
                ),
                _optional_entity(CONF_HOLIDAY_RETURN_ENTITY, options): _entity(
                    ["input_datetime", "sensor"]
                ),
                _optional_entity(CONF_REDUCED_SETPOINT_ENTITY, options): _entity(
                    ["number", "input_number", "sensor"]
                ),
                _optional_entity(CONF_HEATING_SEASON_ENTITY, options): _entity(),
                _optional_entity(CONF_SG_ACTUAL_ENTITY, options): _entity(
                    ["input_number", "number", "sensor", "select"]
                ),
                _optional_entity(CONF_ZONE_ALLOWED_ENTITY, options): _entity(
                    ["input_boolean", "binary_sensor", "switch"]
                ),
                _optional_entity(CONF_REDUCTION_LOCK_ENTITY, options): _entity(
                    ["input_datetime", "sensor"]
                ),
                _optional_entity(CONF_SG_REFERENCE_ENTITY, options): _entity(
                    ["sensor", "input_number", "number", "select"]
                ),
                _optional_entity(CONF_STANDBY_REFERENCE_ENTITY, options): _entity(
                    ["select", "sensor", "input_select"]
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_rules(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = {**DEFAULTS, **self.config_entry.options}
        if user_input is not None:
            rules = {k: user_input[k] for k in RULE_KEYS if k in user_input}
            for key in (
                CONF_SG_REDUCED,
                CONF_SG_NORMAL,
                CONF_SG_RAISED,
                CONF_SG_PREHEAT,
                CONF_SG_FORCED,
                CONF_MIN_VALID_ROOMS,
                CONF_PLAN_MAX_AGE,
                CONF_AIRING_RUN_ON,
            ):
                if key in rules:
                    rules[key] = int(rules[key])
            return self.async_create_entry(data={**rules, **self._entities})
        sg = _number(1, 4, 1)
        schema = vol.Schema(
            {
                vol.Required(CONF_SG_REDUCED, default=options[CONF_SG_REDUCED]): sg,
                vol.Required(CONF_SG_NORMAL, default=options[CONF_SG_NORMAL]): sg,
                vol.Required(CONF_SG_RAISED, default=options[CONF_SG_RAISED]): sg,
                vol.Required(CONF_SG_PREHEAT, default=options[CONF_SG_PREHEAT]): sg,
                vol.Required(CONF_SG_FORCED, default=options[CONF_SG_FORCED]): sg,
                vol.Required(CONF_PLAN_MAX_AGE, default=options[CONF_PLAN_MAX_AGE]): _number(
                    5, 240, 5, "min"
                ),
                vol.Required(CONF_HYSTERESIS, default=options[CONF_HYSTERESIS]): _number(
                    0, 3, 0.1, "K"
                ),
                vol.Required(CONF_MIN_VALID_ROOMS, default=options[CONF_MIN_VALID_ROOMS]): _number(
                    0, 50, 1
                ),
                vol.Required(CONF_MAX_ROOM, default=options[CONF_MAX_ROOM]): _number(
                    18, 30, 0.5, "°C"
                ),
                vol.Required(CONF_AIRING_RUN_ON, default=options[CONF_AIRING_RUN_ON]): _number(
                    0, 240, 5, "min"
                ),
                vol.Required(CONF_HOLIDAY_FLOOR, default=options[CONF_HOLIDAY_FLOOR]): _number(
                    5, 22, 0.5, "°C"
                ),
                vol.Required(CONF_HOLIDAY_RESTART, default=options[CONF_HOLIDAY_RESTART]): _number(
                    0, 48, 0.5, "h"
                ),
                vol.Required(
                    CONF_HOLIDAY_DHW_LEAD, default=options[CONF_HOLIDAY_DHW_LEAD]
                ): _number(0, 12, 0.5, "h"),
                vol.Required(
                    CONF_HEATING_SEASON_STATES, default=options[CONF_HEATING_SEASON_STATES]
                ): str,
                vol.Required(
                    CONF_STANDBY_REFERENCE_STATE, default=options[CONF_STANDBY_REFERENCE_STATE]
                ): str,
            }
        )
        return self.async_show_form(step_id="rules", data_schema=schema)


class RoomSubentryFlow(ConfigSubentryFlow):
    """One room: temperature, own limits, optional windows and humidity."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            data = _room_data(user_input)
            return self.async_create_entry(title=data[CONF_NAME], data=data)
        return self.async_show_form(step_id="user", data_schema=_room_schema({}))

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            data = _room_data(user_input)
            return self.async_update_and_abort(
                self._get_entry(), subentry, title=data[CONF_NAME], data=data
            )
        return self.async_show_form(
            step_id="reconfigure", data_schema=_room_schema(dict(subentry.data))
        )


def _room_data(user_input: dict[str, Any]) -> dict[str, Any]:
    data = {
        CONF_NAME: user_input[CONF_NAME].strip(),
        CONF_TEMPERATURE_ENTITY: user_input[CONF_TEMPERATURE_ENTITY],
        CONF_LOWER: float(user_input[CONF_LOWER]),
        CONF_UPPER: float(user_input[CONF_UPPER]),
        CONF_WINDOWS: list(user_input.get(CONF_WINDOWS) or []),
        CONF_HUMIDITY_MAX: float(user_input.get(CONF_HUMIDITY_MAX, DEFAULTS[CONF_HUMIDITY_MAX])),
        CONF_HUMIDITY_DELAY: int(
            user_input.get(CONF_HUMIDITY_DELAY, DEFAULTS[CONF_HUMIDITY_DELAY])
        ),
        CONF_IN_ZONE: bool(user_input.get(CONF_IN_ZONE, False)),
    }
    if user_input.get(CONF_HUMIDITY_ENTITY):
        data[CONF_HUMIDITY_ENTITY] = user_input[CONF_HUMIDITY_ENTITY]
    return data


def _room_schema(current: dict[str, Any]) -> vol.Schema:
    def default(key: str) -> Any:
        return current.get(key, DEFAULTS.get(key))

    fields: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=current.get(CONF_NAME, "")): str,
        vol.Required(
            CONF_TEMPERATURE_ENTITY, default=current.get(CONF_TEMPERATURE_ENTITY, vol.UNDEFINED)
        ): _entity(["climate", "sensor"]),
        vol.Required(CONF_LOWER, default=default(CONF_LOWER)): _number(5, 25, 0.5, "°C"),
        vol.Required(CONF_UPPER, default=default(CONF_UPPER)): _number(15, 30, 0.5, "°C"),
        vol.Optional(CONF_WINDOWS, default=current.get(CONF_WINDOWS, [])): _entity(
            ["binary_sensor", "input_boolean"], multiple=True
        ),
        _optional_entity(CONF_HUMIDITY_ENTITY, current): _entity(["sensor"]),
        vol.Required(CONF_HUMIDITY_MAX, default=default(CONF_HUMIDITY_MAX)): _number(
            30, 100, 1, "%"
        ),
        vol.Required(CONF_HUMIDITY_DELAY, default=default(CONF_HUMIDITY_DELAY)): _number(
            0, 480, 5, "min"
        ),
        vol.Required(CONF_IN_ZONE, default=default(CONF_IN_ZONE)): bool,
    }
    return vol.Schema(fields)
