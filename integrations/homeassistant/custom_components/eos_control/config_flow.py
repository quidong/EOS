"""Config flow of EOS Control: EOS connection, options and rooms (subentries)."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigSubentryData,
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
    CONF_HEATING_SEASON_ENTITY,
    CONF_HEATING_SEASON_STATES,
    CONF_HOLIDAY_DHW_LEAD,
    CONF_SETTLE,
    CONF_HOLIDAY_ENTITY,
    CONF_HOLIDAY_FLOOR,
    CONF_HOLIDAY_RESTART,
    CONF_HOLIDAY_RETURN_ENTITY,
    CONF_HUMIDITY_DELAY,
    CONF_HUMIDITY_ENTITY,
    CONF_SETPOINT_ENTITY,
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
    CONF_TEMPERATURE_ENTITY,
    CONF_UPPER,
    CONF_URL,
    CONF_WINDOWS,
    CONF_ZONE_ALLOWED_ENTITY,
    CONF_EOS_DEVICE,
    CONF_KIND,
    CONF_MEASUREMENTS,
    CONF_MEASUREMENT_INTERVAL,
    CONF_PLAN_ENTITIES,
    CONF_SEND_MEASUREMENTS,
    DEFAULTS,
    DOMAIN,
    SUBENTRY_DEVICE,
    SUBENTRY_ROOM,
)
from .devices import DEVICE_KINDS, KIND_HEAT_PUMP, EosDevice, parse_devices
from .measurements import computed_keys, device_keys, emr_keys

CONF_DEVICES = "devices"

ENTITY_OPTIONS = (
    CONF_HOLIDAY_ENTITY,
    CONF_HOLIDAY_RETURN_ENTITY,
    CONF_REDUCED_SETPOINT_ENTITY,
    CONF_HEATING_SEASON_ENTITY,
    CONF_SG_ACTUAL_ENTITY,
    CONF_ZONE_ALLOWED_ENTITY,
    CONF_REDUCTION_LOCK_ENTITY,
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


async def fetch_json(hass, url: str, path: str) -> Any:
    """GET ``path`` of EOS as JSON; None = no answer."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{url.rstrip('/')}{path}", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


ADAPTER = "/v1/config/adapter/homeassistant"
EMR_ADAPTER_SECTIONS = (
    "load_emr_entity_ids",
    "grid_import_emr_entity_ids",
    "grid_export_emr_entity_ids",
    "pv_production_emr_entity_ids",
)


async def measurement_catalog(hass, url: str) -> tuple[list[str], dict[str, str]]:
    """All EOS measurement keys and the mapping of EOS's own Home Assistant adapter.

    The adapter mapping only serves as suggestion (taking over from EOS); EOS's
    energy meters use the entity id as key.
    """
    devices = await fetch_json(hass, url, "/v1/config/devices") or {}
    measurement = await fetch_json(hass, url, "/v1/config/measurement") or {}
    keys = [k for k in devices.get("measurement_keys") or [] if isinstance(k, str)]
    keys += [k for k in emr_keys(measurement) if k not in keys]
    suggested: dict[str, str] = {}
    mapping = await fetch_json(hass, url, f"{ADAPTER}/device_measurement_entity_ids")
    if isinstance(mapping, dict):
        suggested.update({str(k): str(v) for k, v in mapping.items() if v})
    for section in EMR_ADAPTER_SECTIONS:
        for entity_id in await fetch_json(hass, url, f"{ADAPTER}/{section}") or []:
            if isinstance(entity_id, str):
                suggested.setdefault(entity_id, entity_id)
    return keys, suggested


def _mapping_schema(keys: list[str], current: dict[str, str]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    for key in keys:
        value = current.get(key)
        marker = (
            vol.Optional(key, description={"suggested_value": value})
            if value
            else vol.Optional(key)
        )
        fields[marker] = selector.EntitySelector(selector.EntitySelectorConfig())
    return vol.Schema(fields)


def _mapping(user_input: dict[str, Any], keys: list[str]) -> dict[str, str]:
    return {k: str(user_input[k]) for k in keys if user_input.get(k)}


async def fetch_devices(hass, url: str) -> list[EosDevice] | None:
    """Devices of the EOS configuration; None = EOS does not answer."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{url.rstrip('/')}/v1/config/devices", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            return parse_devices(await resp.json())
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


def _device_options(devices: list[EosDevice]) -> list[selector.SelectOptionDict]:
    return [
        selector.SelectOptionDict(value=d.device_id, label=f"{d.device_id} ({d.kind})")
        for d in devices
    ]


def _addable(
    devices: list[EosDevice], present: set[str], heat_pump_present: bool
) -> list[EosDevice]:
    """Devices that can be added: supported kinds, not added yet, one heat pump."""
    result = []
    for device in devices:
        if device.device_id in present:
            continue
        if device.kind == KIND_HEAT_PUMP:
            if heat_pump_present:
                continue
        elif device.kind not in DEVICE_KINDS:
            continue
        result.append(device)
    return result


def _device_subentry(device: EosDevice) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={CONF_EOS_DEVICE: device.device_id, CONF_KIND: device.kind},
        subentry_type=SUBENTRY_DEVICE,
        title=device.device_id,
        unique_id=device.device_id,
    )


class EosControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connection to EOS, then the EOS devices to add."""

    VERSION = 2

    def __init__(self) -> None:
        self._url = ""
        self._scan_interval = DEFAULTS[CONF_SCAN_INTERVAL]
        self._devices: list[EosDevice] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._url = user_input[CONF_URL].rstrip("/")
            self._scan_interval = int(user_input[CONF_SCAN_INTERVAL])
            await self.async_set_unique_id(self._url)
            self._abort_if_unique_id_configured()
            error = await _check_eos(self.hass, self._url)
            devices = None if error else await fetch_devices(self.hass, self._url)
            if error or devices is None:
                errors["base"] = error or "cannot_read_devices"
            else:
                self._devices = _addable(devices, set(), heat_pump_present=False)
                if not self._devices:
                    return self._create([])
                return await self.async_step_devices()
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default="http://eos:8503"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULTS[CONF_SCAN_INTERVAL]): _number(
                    15, 600, 5, "s"
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """EOS devices to add now (more can be added later)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = [d for d in self._devices if d.device_id in user_input[CONF_DEVICES]]
            if sum(d.kind == KIND_HEAT_PUMP for d in chosen) > 1:
                errors["base"] = "one_heat_pump"
            else:
                return self._create(chosen)
        first_heat_pump = next((d for d in self._devices if d.kind == KIND_HEAT_PUMP), None)
        default = [
            d.device_id for d in self._devices if d.kind != KIND_HEAT_PUMP or d is first_heat_pump
        ]
        schema = vol.Schema(
            {
                vol.Optional(CONF_DEVICES, default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_device_options(self._devices),
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="devices", data_schema=schema, errors=errors)

    def _create(self, devices: list[EosDevice]) -> ConfigFlowResult:
        return self.async_create_entry(
            title="EOS",
            data={CONF_URL: self._url, CONF_SCAN_INTERVAL: self._scan_interval},
            options={k: v for k, v in DEFAULTS.items() if k in RULE_KEYS},
            subentries=[_device_subentry(d) for d in devices],
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the EOS URL or the poll interval."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            error = await _check_eos(self.hass, url)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_URL: url,
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=entry.data[CONF_URL]): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=entry.data.get(CONF_SCAN_INTERVAL, DEFAULTS[CONF_SCAN_INTERVAL]),
                ): _number(15, 600, 5, "s"),
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EosControlOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ROOM: RoomSubentryFlow, SUBENTRY_DEVICE: DeviceSubentryFlow}


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
    CONF_SETTLE,
    CONF_HEATING_SEASON_STATES,
    CONF_SEND_MEASUREMENTS,
    CONF_MEASUREMENT_INTERVAL,
    CONF_PLAN_ENTITIES,
)


def _optional_entity(key: str, options: dict[str, Any]) -> vol.Optional:
    value = options.get(key)
    if value:
        return vol.Optional(key, description={"suggested_value": value})
    return vol.Optional(key)


class EosControlOptionsFlow(OptionsFlow):
    """Context entities and rules."""

    def __init__(self) -> None:
        self._entities: dict[str, Any] = {}
        self._rules: dict[str, Any] = {}
        self._keys: list[str] = []

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
                CONF_MEASUREMENT_INTERVAL,
            ):
                if key in rules:
                    rules[key] = int(rules[key])
            self._rules = rules
            return await self.async_step_measurements()
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
                vol.Required(CONF_SETTLE, default=options[CONF_SETTLE]): _number(0, 300, 1, "s"),
                vol.Required(
                    CONF_HEATING_SEASON_STATES, default=options[CONF_HEATING_SEASON_STATES]
                ): str,
                vol.Required(CONF_SEND_MEASUREMENTS, default=options[CONF_SEND_MEASUREMENTS]): bool,
                vol.Required(
                    CONF_MEASUREMENT_INTERVAL, default=options[CONF_MEASUREMENT_INTERVAL]
                ): _number(1, 60, 1, "min"),
                vol.Required(CONF_PLAN_ENTITIES, default=options[CONF_PLAN_ENTITIES]): bool,
            }
        )
        return self.async_show_form(step_id="rules", data_schema=schema)

    async def async_step_measurements(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entities for the EOS measurements that belong to no added device."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self._rules,
                    **self._entities,
                    CONF_MEASUREMENTS: _mapping(user_input, self._keys),
                }
            )
        url = self.config_entry.data[CONF_URL]
        keys, suggested = await measurement_catalog(self.hass, url)
        added = [
            str(s.data.get(CONF_EOS_DEVICE))
            for s in self.config_entry.subentries.values()
            if s.subentry_type == SUBENTRY_DEVICE
        ]
        own = {k for d in added for k in device_keys(keys, d)}
        self._keys = [k for k in keys if k not in own]
        current = self.config_entry.options.get(CONF_MEASUREMENTS)
        if current is None:
            current = {k: v for k, v in suggested.items() if k in self._keys}
        return self.async_show_form(
            step_id="measurements", data_schema=_mapping_schema(self._keys, current)
        )


class DeviceSubentryFlow(ConfigSubentryFlow):
    """A device from the EOS configuration: heat pump, battery, EV or home appliance."""

    def __init__(self) -> None:
        self._devices: list[EosDevice] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        if user_input is not None:
            device = next(
                (d for d in self._devices if d.device_id == user_input[CONF_EOS_DEVICE]), None
            )
            if device is None:
                return self.async_abort(reason="device_not_found")
            sub = _device_subentry(device)
            keys, suggested = await measurement_catalog(self.hass, entry.data[CONF_URL])
            data = dict(sub["data"])
            data[CONF_MEASUREMENTS] = _device_mapping(device, keys, suggested)
            return self.async_create_entry(
                title=sub["title"], data=data, unique_id=sub["unique_id"]
            )
        devices = await fetch_devices(self.hass, entry.data[CONF_URL])
        if devices is None:
            return self.async_abort(reason="cannot_read_devices")
        present = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_DEVICE]
        self._devices = _addable(
            devices,
            {str(s.data.get(CONF_EOS_DEVICE)) for s in present},
            heat_pump_present=any(s.data.get(CONF_KIND) == KIND_HEAT_PUMP for s in present),
        )
        if not self._devices:
            return self.async_abort(reason="no_devices")
        schema = vol.Schema(
            {
                vol.Required(CONF_EOS_DEVICE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_device_options(self._devices),
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Entities for the measurements of this device."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        device = EosDevice(str(subentry.data[CONF_EOS_DEVICE]), str(subentry.data[CONF_KIND]))
        keys, suggested = await measurement_catalog(self.hass, entry.data[CONF_URL])
        own = _own_keys(device, keys)
        if user_input is not None:
            return self.async_update_and_abort(
                entry,
                subentry,
                data={**subentry.data, CONF_MEASUREMENTS: _mapping(user_input, own)},
            )
        current = subentry.data.get(CONF_MEASUREMENTS)
        if current is None:
            current = _device_mapping(device, keys, suggested)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_mapping_schema(own, current)
        )


def _own_keys(device: EosDevice, keys: list[str]) -> list[str]:
    """Measurement keys of the device the user maps (not those computed for it)."""
    computed = computed_keys(device.device_id) if device.kind == KIND_HEAT_PUMP else set()
    return [k for k in device_keys(keys, device.device_id) if k not in computed]


def _device_mapping(
    device: EosDevice, keys: list[str], suggested: dict[str, str]
) -> dict[str, str]:
    own = set(_own_keys(device, keys))
    return {k: v for k, v in suggested.items() if k in own}


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
    if user_input.get(CONF_SETPOINT_ENTITY):
        data[CONF_SETPOINT_ENTITY] = user_input[CONF_SETPOINT_ENTITY]
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
        _optional_entity(CONF_SETPOINT_ENTITY, current): _entity(
            ["climate", "number", "input_number"]
        ),
    }
    return vol.Schema(fields)
