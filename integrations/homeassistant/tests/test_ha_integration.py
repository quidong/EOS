"""EOS Control in Home Assistant: config flow, rooms, coordinator, shadow comparison."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from custom_components.eos_control.const import (
    CONF_HOLIDAY_ENTITY,
    CONF_SCAN_INTERVAL,
    CONF_SG_REFERENCE_ENTITY,
    CONF_STANDBY_REFERENCE_ENTITY,
    CONF_URL,
    DEFAULTS,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.exceptions import ServiceValidationError
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


URL = "http://eos.test:8503"
SOLUTION_URL = f"{URL}/v1/energy-management/optimization/solution"
SUMMARY_URL = f"{URL}/v1/energy-management/history/summary"
HOURS_URL = f"{URL}/v1/energy-management/history/hours"
DEVICES_URL = f"{URL}/v1/config/devices"
HEALTH_URL = f"{URL}/v1/health"
MEASURE_URL = f"{URL}/v1/measurement/data"
HP_DEVICES = {
    "heat_pumps": {"hp1": {"device_id": "hp1"}},
    "batteries": None,
    "electric_vehicles": None,
    "home_appliances": {},
}


def solution(mode: int, dhw: int = 0, away: int = 0, age_min: int = 5) -> dict:
    now = dt_util.now()
    start = now.replace(minute=0, second=0, microsecond=0)
    data = {}
    for i in range(8):
        data[(start + timedelta(minutes=15 * i)).isoformat()] = {
            "hp1_heating_mode": float(mode),
            "hp1_dhw_mode": float(dhw),
            "hp1_away": float(away),
        }
    return {
        "generated_at": (now - timedelta(minutes=age_min)).isoformat(),
        "solution": {"data": data},
    }


def room(name: str, entity: str, windows=None, **extra) -> ConfigSubentryData:
    data = {
        "name": name,
        "temperature_entity": entity,
        "lower_c": 19.5,
        "upper_c": 23.5,
        "window_entities": windows or [],
        "humidity_max_pct": 65.0,
        "humidity_delay_min": 0,
        "in_zone": False,
        **extra,
    }
    return ConfigSubentryData(data=data, subentry_type="room", title=name, unique_id=None)


async def setup(
    hass,
    aioclient_mock,
    sol,
    options=None,
    rooms=None,
    history=None,
    devices=None,
    heat_pump="hp1",
) -> MockConfigEntry:
    """``history`` = (summary, hours) of the EOS history; None = upstream EOS (404).

    ``devices`` = ``/v1/config/devices`` of EOS (default: heat pump hp1); ``rooms`` may
    also hold device subentries.
    """
    aioclient_mock.get(SOLUTION_URL, json=sol)
    aioclient_mock.get(DEVICES_URL, json=devices if devices is not None else HP_DEVICES)
    eos_extras(aioclient_mock)
    subentries = ([heat_pump_entry(heat_pump)] if heat_pump else []) + list(rooms or [])
    if history is None:
        aioclient_mock.get(SUMMARY_URL, status=404)
        aioclient_mock.get(HOURS_URL, status=404)
    else:
        aioclient_mock.get(SUMMARY_URL, json=history[0])
        aioclient_mock.get(HOURS_URL, json=history[1])
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EOS hp1",
        data={CONF_URL: URL, CONF_SCAN_INTERVAL: 60},
        options={**DEFAULTS, **(options or {})},
        subentries_data=subentries,
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


ADAPTER = f"{URL}/v1/config/adapter/homeassistant"


PLAN_URL = f"{URL}/v1/energy-management/plan"


def eos_extras(aioclient_mock, measurement=None, adapter=None, plan=None) -> None:
    """Health, measurement upload, the energy management plan and the catalogue."""
    aioclient_mock.get(HEALTH_URL, json={"status": "alive", "version": "0.3.0"})
    if plan is None:
        aioclient_mock.get(PLAN_URL, status=404)
    else:
        aioclient_mock.get(PLAN_URL, json=plan)
    aioclient_mock.put(MEASURE_URL, status=204)
    aioclient_mock.get(f"{URL}/v1/config/measurement", json=measurement or {})
    adapter = adapter or {}
    aioclient_mock.get(
        f"{ADAPTER}/device_measurement_entity_ids",
        json=adapter.get("device_measurement_entity_ids"),
    )
    for section in (
        "load_emr_entity_ids",
        "grid_import_emr_entity_ids",
        "grid_export_emr_entity_ids",
        "pv_production_emr_entity_ids",
    ):
        aioclient_mock.get(f"{ADAPTER}/{section}", json=adapter.get(section))


def heat_pump_entry(device_id: str = "hp1") -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"eos_device_id": device_id, "kind": "heat_pump"},
        subentry_type="device",
        title=device_id,
        unique_id=device_id,
    )


def set_rooms(hass, living=21.0, bath=20.8):
    hass.states.async_set("sensor.living", str(living))
    hass.states.async_set(
        "climate.bath", "heat", {"current_temperature": bath, "temperature": 21.0}
    )


ROOMS = [room("Living", "sensor.living"), room("Bath", "climate.bath")]


# ----------------------------------------------------------------------
# Config flow
# ----------------------------------------------------------------------


async def test_user_flow_creates_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    """EOS first, then its devices are offered (no typed ids); one heat pump."""
    eos_extras(aioclient_mock)
    aioclient_mock.get(
        DEVICES_URL,
        json={
            "heat_pumps": {"hp1": {}, "wp_b": {}},
            "batteries": {"bat1": {}},
            "inverters": {"inv": {}},
        },
    )
    aioclient_mock.get(SOLUTION_URL, json=solution(1))
    aioclient_mock.get(SUMMARY_URL, status=404)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL + "/", CONF_SCAN_INTERVAL: 60}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "devices"
    field = next(k for k in result["data_schema"].schema if k == "devices")
    options = result["data_schema"].schema[field].config["options"]
    assert [o["value"] for o in options] == ["hp1", "wp_b", "bat1"]  # no inverter
    assert field.default() == ["hp1", "bat1"]  # first heat pump and the battery
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"devices": ["hp1", "wp_b"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "one_heat_pump"}
    with pytest.raises(Exception):  # an id that EOS does not know is refused
        await hass.config_entries.flow.async_configure(result["flow_id"], {"devices": ["wbb13"]})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"devices": ["hp1", "bat1"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "EOS"
    assert result["data"] == {CONF_URL: URL, CONF_SCAN_INTERVAL: 60}
    assert result["options"]["sg_reduced"] == 1
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.version == 2
    assert sorted((s.data["eos_device_id"], s.data["kind"]) for s in entry.subentries.values()) == [
        ("bat1", "battery"),
        ("hp1", "heat_pump"),
    ]
    assert hass.states.get("sensor.eos_hp1_sg_ready_target") is not None
    assert hass.states.get("sensor.eos_plan") is not None
    assert hass.states.get("sensor.eos_bat1_operation_mode") is not None


async def test_user_flow_without_heat_pump(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    eos_extras(aioclient_mock)
    aioclient_mock.get(DEVICES_URL, json={"batteries": {"bat1": {}}})
    aioclient_mock.get(SOLUTION_URL, json=solution(1))
    aioclient_mock.get(SUMMARY_URL, status=404)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_SCAN_INTERVAL: 60}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"devices": []})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("sensor.eos_plan") is not None
    assert not [s for s in hass.states.async_entity_ids() if "sg_ready" in s]
    assert not [s for s in hass.states.async_entity_ids() if s.startswith("button.")]


async def test_user_flow_device_list_unreadable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(f"{URL}/v1/health", json={"status": "alive"})
    aioclient_mock.get(DEVICES_URL, status=404)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_SCAN_INTERVAL: 60}
    )
    assert result["errors"] == {"base": "cannot_read_devices"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{URL}/v1/health", status=500)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_SCAN_INTERVAL: 60}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    hass.states.async_set("input_boolean.away", "off")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HOLIDAY_ENTITY: "input_boolean.away"}
    )
    assert result["step_id"] == "rules"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **{k: v for k, v in DEFAULTS.items() if k in result["data_schema"].schema},
            "max_room_c": 24.0,
        },
    )
    assert result["step_id"] == "measurements"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HOLIDAY_ENTITY] == "input_boolean.away"
    assert entry.options["max_room_c"] == 24.0
    assert entry.options["measurements"] == {}


async def test_measurement_mapping_takes_over_from_the_eos_adapter(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """Keys of EOS and the mapping of EOS's own adapter become the suggestions."""
    set_rooms(hass)
    aioclient_mock.get(
        DEVICES_URL,
        json={
            **HP_DEVICES,
            "home_appliances": {"washer": {}},
            "measurement_keys": [
                "hp1-room-temperature",
                "hp1-dhw-temperature",
                "hp1-away",
                "washer-power",
                "washer-requested",
            ],
        },
    )
    eos_extras(
        aioclient_mock,
        measurement={"load_emr_keys": ["sensor.house_meter"], "pv_production_emr_keys": None},
        adapter={
            "device_measurement_entity_ids": {
                "hp1-dhw-temperature": "sensor.tank",
                "hp1-room-temperature": "sensor.room_mean",
                "washer-power": "sensor.washer_power",
            },
            "load_emr_entity_ids": ["sensor.house_meter"],
        },
    )
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    # EOS: keys of no added device (meter, washer); heat pump keys belong to the device
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {k: v for k, v in DEFAULTS.items() if k in result["data_schema"].schema}
    )
    assert result["step_id"] == "measurements"
    fields = {str(k): k for k in result["data_schema"].schema}
    assert sorted(fields) == ["sensor.house_meter", "washer-power", "washer-requested"]
    assert fields["washer-power"].description == {"suggested_value": "sensor.washer_power"}
    assert fields["sensor.house_meter"].description == {"suggested_value": "sensor.house_meter"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"sensor.house_meter": "sensor.house_meter", "washer-power": "sensor.washer_power"},
    )
    assert entry.options["measurements"] == {
        "sensor.house_meter": "sensor.house_meter",
        "washer-power": "sensor.washer_power",
    }
    # heat pump: the computed keys are not offered, the adapter mapping is suggested
    await hass.async_block_till_done()
    sub = next(s for s in entry.subentries.values() if s.data.get("kind") == "heat_pump")
    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    fields = {str(k): k for k in result["data_schema"].schema}
    assert sorted(fields) == ["hp1-dhw-temperature"]
    assert fields["hp1-dhw-temperature"].description == {"suggested_value": "sensor.tank"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"hp1-dhw-temperature": "sensor.tank"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[sub.subentry_id].data["measurements"] == {
        "hp1-dhw-temperature": "sensor.tank"
    }


async def test_room_subentry_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(1))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "room"), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Office",
            "temperature_entity": "sensor.living",
            "lower_c": 16.5,
            "upper_c": 22.0,
            "window_entities": [],
            "humidity_max_pct": 70,
            "humidity_delay_min": 60,
            "in_zone": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sub = next(s for s in entry.subentries.values() if s.subentry_type == "room")
    assert sub.title == "Office" and sub.data["lower_c"] == 16.5


# ----------------------------------------------------------------------
# Coordinator and entities
# ----------------------------------------------------------------------


async def test_entities_follow_the_plan(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    await setup(hass, aioclient_mock, solution(0), rooms=ROOMS)
    state = hass.states.get("sensor.eos_hp1_sg_ready_target")
    assert state is not None and state.state == "1"
    assert state.attributes["eos_mode"] == "REDUCED"
    assert float(hass.states.get("sensor.eos_hp1_room_temperature_mean").state) == pytest.approx(
        20.9
    )
    assert float(hass.states.get("sensor.eos_hp1_margin_to_lower_limit").state) == pytest.approx(
        1.3
    )
    assert hass.states.get("binary_sensor.eos_hp1_eos_plan_ok").state == "on"


async def test_cold_room_blocks_reduction(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass, bath=19.7)
    await setup(hass, aioclient_mock, solution(0), rooms=ROOMS)
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"
    assert "Bath 19.7" in hass.states.get("sensor.eos_hp1_reason").state


async def test_open_window_excludes_the_room(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass, bath=18.0)
    hass.states.async_set("binary_sensor.bath_window", "on")
    rooms = [
        room("Living", "sensor.living"),
        room("Bath", "climate.bath", ["binary_sensor.bath_window"]),
    ]
    await setup(hass, aioclient_mock, solution(0), rooms=rooms)
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "1"
    assert float(hass.states.get("sensor.eos_hp1_open_windows_share").state) == 1.0


async def test_restart_stamp_is_no_airing(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    """A closed window whose last_changed is the start time does not count as airing."""
    set_rooms(hass, bath=19.7)
    hass.states.async_set("binary_sensor.bath_window", "off")
    rooms = [
        room("Living", "sensor.living"),
        room("Bath", "climate.bath", ["binary_sensor.bath_window"]),
    ]
    await setup(hass, aioclient_mock, solution(0), rooms=rooms)
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"  # bath counts


async def test_stale_plan_and_unreachable_eos(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    await setup(hass, aioclient_mock, solution(0, age_min=90), rooms=ROOMS)
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"
    assert hass.states.get("binary_sensor.eos_hp1_eos_plan_ok").state == "off"


async def test_unreachable_eos(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    aioclient_mock.get(SOLUTION_URL, status=503)
    eos_extras(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_URL: URL, CONF_SCAN_INTERVAL: 60},
        options=dict(DEFAULTS),
        subentries_data=[heat_pump_entry(), *ROOMS],
        title="EOS",
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"
    assert hass.states.get("sensor.eos_hp1_eos_mode").attributes["eos_error"] == "EOS HTTP 503"


async def test_holiday_standby(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass, living=17.5, bath=17.2)
    hass.states.async_set("input_boolean.away", "on")
    await setup(
        hass,
        aioclient_mock,
        solution(0, away=1),
        options={CONF_HOLIDAY_ENTITY: "input_boolean.away"},
        rooms=ROOMS,
    )
    assert hass.states.get("binary_sensor.eos_hp1_standby_target").state == "on"
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "1"


async def test_humidity_warning(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    hass.states.async_set("sensor.bath_humidity", "72")
    rooms = [
        room("Living", "sensor.living"),
        room("Bath", "climate.bath", humidity_entity="sensor.bath_humidity"),
    ]
    await setup(hass, aioclient_mock, solution(0), rooms=rooms)
    assert hass.states.get("binary_sensor.eos_hp1_humidity_warning").state == "on"
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"
    assert float(hass.states.get("sensor.eos_hp1_humidity_maximum").state) == 72.0


async def test_shadow_comparison_counts_confirmed_mismatches(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    hass.states.async_set("sensor.reference_sg", "2")
    entry = await setup(
        hass,
        aioclient_mock,
        solution(0),
        options={CONF_SG_REFERENCE_ENTITY: "sensor.reference_sg"},
        rooms=ROOMS,
    )
    coordinator = entry.runtime_data
    assert coordinator.data.shadow.compared == 1 and coordinator.data.shadow.mismatches == 0
    await coordinator.async_refresh()
    assert coordinator.data.shadow.mismatches == 1  # confirmed on the second poll
    assert coordinator.data.shadow.log[-1]["integration"] == 1
    hass.states.async_set("sensor.reference_sg", "1")
    await coordinator.async_refresh()
    agreement = hass.states.get("sensor.eos_hp1_shadow_agreement")
    assert agreement.attributes["compared"] == 3 and agreement.attributes["mismatches"] == 1
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.eos_hp1_reset_shadow_comparison"}, blocking=True
    )
    assert coordinator.data.shadow.mismatches == 0


async def test_standby_reference(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass, living=17.5, bath=17.2)
    hass.states.async_set("input_boolean.away", "on")
    hass.states.async_set("select.heat_pump_mode", "automatic")
    entry = await setup(
        hass,
        aioclient_mock,
        solution(0, away=1),
        options={
            CONF_HOLIDAY_ENTITY: "input_boolean.away",
            CONF_STANDBY_REFERENCE_ENTITY: "select.heat_pump_mode",
            "standby_reference_state": "standby",
        },
        rooms=ROOMS,
    )
    coordinator = entry.runtime_data
    assert coordinator.data.reference_standby is False
    await coordinator.async_refresh()
    assert coordinator.data.shadow.log[-1]["check"] == "standby"


async def test_periodic_refresh_and_diagnostics(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    from custom_components.eos_control.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    calls = aioclient_mock.call_count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert aioclient_mock.call_count > calls
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"][CONF_URL] == "**REDACTED**"
    assert len(diag["rooms"]) == 2


async def test_shadow_counts_survive_a_reload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    hass.states.async_set("sensor.reference_sg", "1")
    entry = await setup(
        hass,
        aioclient_mock,
        solution(0),
        options={CONF_SG_REFERENCE_ENTITY: "sensor.reference_sg"},
        rooms=ROOMS,
    )
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.data.shadow.compared == 2
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.data.shadow.compared == 3  # 2 restored + first poll


async def test_comfort_learning_from_setpoints_and_lock(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    from homeassistant.core import Context

    set_rooms(hass, living=19.8, bath=20.8)
    hass.states.async_set("climate.living_controller", "heat", {"temperature": 21.0})
    hass.states.async_set("input_datetime.lock", "2026-01-01 00:00:00")
    rooms = [
        room("Living", "sensor.living", setpoint_entity="climate.living_controller"),
        room("Bath", "climate.bath"),
    ]
    entry = await setup(
        hass,
        aioclient_mock,
        solution(1),
        options={"reduction_lock_entity": "input_datetime.lock"},
        rooms=rooms,
    )
    coordinator = entry.runtime_data
    assert coordinator.data.comfort_events == 0
    # raised at the wall (no automation context): recorded
    hass.states.async_set("climate.living_controller", "heat", {"temperature": 22.0})
    await coordinator.async_refresh()
    assert coordinator.data.comfort_events == 1
    assert coordinator.learning.events[-1].room == "Living"
    assert coordinator.learning.events[-1].temperature_c == pytest.approx(19.8)
    # mode change of the controller (economy -> comfort) raises the setpoint: ignored
    hass.states.async_set(
        "climate.living_controller", "heat", {"temperature": 22.0, "preset_mode": "economy"}
    )
    await coordinator.async_refresh()
    hass.states.async_set(
        "climate.living_controller", "heat", {"temperature": 22.5, "preset_mode": "comfort"}
    )
    await coordinator.async_refresh()
    assert coordinator.data.comfort_events == 1
    # raised by an automation: ignored
    hass.states.async_set(
        "climate.living_controller",
        "heat",
        {"temperature": 23.5, "preset_mode": "comfort"},
        context=Context(parent_id="automation-run"),
    )
    await coordinator.async_refresh()
    assert coordinator.data.comfort_events == 1
    # lock set into the future: attributed to the room closest to its limit
    future = dt_util.now() + timedelta(hours=3)
    hass.states.async_set("input_datetime.lock", future.strftime("%Y-%m-%d %H:%M:%S"))
    await coordinator.async_refresh()
    assert coordinator.data.comfort_events == 2
    assert coordinator.learning.events[-1].kind == "lock"
    assert coordinator.learning.events[-1].room == "Living"
    # stored across a reload, reset by the button
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.data.comfort_events == 2
    state = hass.states.get("sensor.eos_hp1_comfort_suggestions")
    assert state.state == "0" and state.attributes["events"] == 2
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.eos_hp1_reset_comfort_learning"}, blocking=True
    )
    assert entry.runtime_data.data.comfort_events == 0


async def test_comfort_learning_ignores_holiday_and_the_return(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass, living=19.8, bath=20.8)
    hass.states.async_set("climate.living_controller", "heat", {"temperature": 18.0})
    hass.states.async_set("input_boolean.away", "on")
    rooms = [room("Living", "sensor.living", setpoint_entity="climate.living_controller")]
    entry = await setup(
        hass,
        aioclient_mock,
        solution(1, away=1),
        options={CONF_HOLIDAY_ENTITY: "input_boolean.away"},
        rooms=rooms,
    )
    coordinator = entry.runtime_data
    hass.states.async_set("climate.living_controller", "heat", {"temperature": 21.0})
    await coordinator.async_refresh()
    hass.states.async_set("input_boolean.away", "off")  # back home: setpoints go up
    hass.states.async_set("climate.living_controller", "heat", {"temperature": 22.0})
    await coordinator.async_refresh()
    assert coordinator.data.comfort_events == 0


# ----------------------------------------------------------------------
# Chart sensors (plan, forecast, history)
# ----------------------------------------------------------------------


def chart_solution(age_min: int = 5) -> dict:
    """Solution with house, heat pump and forecast columns, 8 slots from the current hour."""
    now = dt_util.now()
    start = now.replace(minute=0, second=0, microsecond=0)
    plan, prediction = {}, {}
    for i in range(8):
        t = (start + timedelta(minutes=15 * i)).isoformat()
        plan[t] = {
            "load_energy_wh": 100.0,
            "grid_consumption_energy_wh": 100.0,
            "hp1_heating_mode": 1.0,
            "hp1_dhw_mode": 0.0,
            "hp1_energy_wh": 250.0,
            "hp1_room_temperature_c": 21.25,
            "hp1_dhw_temperature_c": 48.0,
        }
        prediction[t] = {"pvforecast_ac_energy_wh": 300.0, "elec_price_amt_kwh": 0.3}
    return {
        "generated_at": (now - timedelta(minutes=age_min)).isoformat(),
        "solution": {"data": plan},
        "prediction": {"data": prediction},
    }


async def test_chart_sensors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    plan = hass.states.get("sensor.eos_plan")
    assert plan is not None and plan.state not in ("unknown", "unavailable")
    assert plan.attributes["slot_minutes"] == 15
    assert len(plan.attributes["data"]) == 8
    assert plan.attributes["data"][0]["heat_pump_wh"] == 250.0
    assert plan.attributes["hourly"][0]["heat_pump_kwh"] == pytest.approx(1.0)
    forecast = hass.states.get("sensor.eos_forecast")
    assert forecast.attributes["hourly"][0]["pv_kwh"] == pytest.approx(1.2)
    assert float(hass.states.get("sensor.eos_planned_house_load_hour").state) == 0.4
    assert float(hass.states.get("sensor.eos_hp1_planned_heat_pump_energy_hour").state) == 1.0
    room = hass.states.get("sensor.eos_hp1_planned_room_temperature")
    assert float(room.state) == 21.25
    assert room.attributes["state_class"] == "measurement"
    assert float(hass.states.get("sensor.eos_hp1_planned_hot_water_temperature").state) == 48.0
    # upstream EOS without history endpoints
    assert hass.states.get("sensor.eos_history").state == "unavailable"
    assert hass.states.get("sensor.eos_history_hours").state == "unavailable"


async def test_chart_series_are_not_recorded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    from custom_components.eos_control.sensor import CHART_ATTRIBUTES, EosChartSensor

    assert {"data", "hourly"} <= CHART_ATTRIBUTES
    assert EosChartSensor._unrecorded_attributes == CHART_ATTRIBUTES


async def test_history_sensors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    summary = {
        "state": "2026-10-29T20:25:04+00:00",
        "enabled": True,
        "last_error": None,
        "kpi": {"pv_mae_6h": 120.0},
        "plan": {"algorithm": "GENETIC", "saving_vs_baseline_amt": 0.1},
        "days": [{"day": "2026-10-28", "algorithm": "MILP", "net_amt": 1.2}],
        "ab": {"MILP": {"mean_net_amt": 1.2, "days": 1}},
    }
    hours = {
        "state": "2026-10-29T20:25:17+00:00",
        "data": [{"hour": "2026-10-29T19:00:00+00:00", "pv_actual": 0.0, "pv_predicted": 5.0}],
    }
    await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS, history=(summary, hours))
    state = hass.states.get("sensor.eos_history")
    assert state.state == "2026-10-29T20:25:04+00:00"
    assert state.attributes["kpi"] == {"pv_mae_6h": 120.0}
    assert state.attributes["ab"]["MILP"]["days"] == 1
    hourly = hass.states.get("sensor.eos_history_hours")
    assert hourly.attributes["data"][0]["pv_predicted"] == 5.0
    calls = [str(c[1]) for c in aioclient_mock.mock_calls]
    # fetched once, not on every poll
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    calls_after = [str(c[1]) for c in aioclient_mock.mock_calls]
    assert calls_after.count(SUMMARY_URL) == calls.count(SUMMARY_URL) == 1
    assert calls_after.count(SOLUTION_URL) > calls.count(SOLUTION_URL)


async def test_upstream_eos_is_asked_for_history_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    for minutes in (16, 32):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()
    calls = [str(c[1]) for c in aioclient_mock.mock_calls]
    assert calls.count(SUMMARY_URL) == 1
    assert HOURS_URL not in calls


async def test_plan_memory_survives_a_reload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert len(coordinator.memory.series()) == 8
    # EOS down after the reload: the remembered plan still feeds the charts
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, status=503)
    aioclient_mock.get(SUMMARY_URL, status=404)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    plan = hass.states.get("sensor.eos_plan")
    assert len(plan.attributes["data"]) == 8
    # the time of the last plan stays (plan age), also across the reload
    assert plan.state not in ("unknown", "unavailable")
    assert hass.states.get("sensor.eos_forecast").state == "unknown"
    assert float(hass.states.get("sensor.eos_hp1_planned_room_temperature").state) == 21.25


async def test_example_dashboard_uses_existing_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """dashboard/eos-charts.yaml only refers to entities the integration creates."""
    import re
    from pathlib import Path

    import yaml

    text = (Path(__file__).resolve().parents[1] / "dashboard" / "eos-charts.yaml").read_text()
    yaml.safe_load(text)
    referenced = set(re.findall(r"sensor\.eos_[a-z0-9_]+", text))
    assert "sensor.eos_plan" in referenced and "sensor.eos_history_hours" in referenced
    set_rooms(hass)
    await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    missing = {e for e in referenced if hass.states.get(e) is None}
    assert not missing


# ----------------------------------------------------------------------
# Settle time and short EOS outages
# ----------------------------------------------------------------------


async def test_changed_decision_is_accepted_after_the_settle_time(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, json=solution(0))
    aioclient_mock.get(SUMMARY_URL, status=404)
    aioclient_mock.get(PLAN_URL, status=404)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("sensor.eos_hp1_sg_ready_target")
    assert state.state == "2"  # held
    assert state.attributes["pending"].startswith("REDUCED")
    # re-evaluated by itself after the settle time, not only at the next poll
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.eos_hp1_sg_ready_target")
    assert state.state == "1"
    assert state.attributes["pending"] is None


async def test_settle_time_zero(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    entry = await setup(
        hass, aioclient_mock, solution(1), options={"settle_seconds": 0}, rooms=ROOMS
    )
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, json=solution(0))
    aioclient_mock.get(SUMMARY_URL, status=404)
    aioclient_mock.get(PLAN_URL, status=404)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "1"


async def test_short_eos_outage_keeps_the_last_plan(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(0), rooms=ROOMS)
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "1"
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, exc=TimeoutError())
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("sensor.eos_hp1_sg_ready_target")
    assert state.state == "1"  # plan of 5 min ago is still valid
    assert "EOS unreachable: TimeoutError" in state.attributes["reason"]
    assert hass.states.get("binary_sensor.eos_hp1_eos_plan_ok").state == "on"


# ----------------------------------------------------------------------
# EOS devices: heat pump check, batteries, electric vehicles, appliances
# ----------------------------------------------------------------------

DEVICE_CONFIG = {
    "heat_pumps": {"hp1": {"device_id": "hp1"}},
    "batteries": {"bat1": {"device_id": "bat1", "max_charge_power_w": 4000}},
    "electric_vehicles": {"ev1": {"device_id": "ev1", "max_charge_power_w": 11000}},
    "home_appliances": {"dishwasher": {"device_id": "dishwasher"}},
}
MODES = (
    "IDLE",
    "SELF_CONSUMPTION",
    "NON_EXPORT",
    "PEAK_SHAVING",
    "GRID_SUPPORT_EXPORT",
    "GRID_SUPPORT_IMPORT",
    "FORCED_CHARGE",
)


def device_solution(battery_modes, age_min: int = 5) -> dict:
    """Heat pump plan plus a battery (modes per slot), an EV and a dishwasher."""
    sol = solution(1, age_min=age_min)
    start_ts = None
    for i, (stamp, row) in enumerate(sorted(sol["solution"]["data"].items())):
        mode = battery_modes[min(i, len(battery_modes) - 1)]
        for m in MODES:
            row[f"bat1_{m.lower()}_op_mode"] = 1.0 if m == mode else 0.0
            row[f"bat1_{m.lower()}_op_factor"] = 0.5 if m == mode else 0.0
            row[f"ev1_{m.lower()}_op_mode"] = 1.0 if m == "IDLE" else 0.0
            row[f"ev1_{m.lower()}_op_factor"] = 1.0 if m == "IDLE" else 0.0
        row["bat1_soc_factor"] = 0.42
        row["ev1_soc_factor"] = 0.8
        if start_ts is None:
            start_ts = dt_util.parse_datetime(stamp).timestamp() + 3600
        row["dishwasher_run_op_mode"] = 0.0
        row["dishwasher_energy_wh"] = 0.0
        row["dishwasher_next_start_ts"] = start_ts
    return sol


def device_entry(device_id: str, kind: str) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"eos_device_id": device_id, "kind": kind},
        subentry_type="device",
        title=device_id,
        unique_id=device_id,
    )


DEVICE_SUBENTRIES = [
    device_entry("bat1", "battery"),
    device_entry("ev1", "electric_vehicle"),
    device_entry("dishwasher", "home_appliance"),
]


async def test_device_entities(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    await setup(
        hass,
        aioclient_mock,
        device_solution(["GRID_SUPPORT_IMPORT"]),
        rooms=ROOMS + DEVICE_SUBENTRIES,
        devices=DEVICE_CONFIG,
    )
    mode = hass.states.get("sensor.eos_bat1_operation_mode")
    assert mode.state == "grid_support_import"
    assert float(hass.states.get("sensor.eos_bat1_planned_state_of_charge").state) == 42.0
    assert float(hass.states.get("sensor.eos_bat1_charge_power_target").state) == 2000.0
    assert hass.states.get("sensor.eos_bat1_mode_factor") is None  # disabled by default
    assert mode.attributes["factor"] == 0.5
    assert hass.states.get("binary_sensor.eos_bat1_charging_from_the_grid").state == "on"
    assert hass.states.get("binary_sensor.eos_bat1_discharging_allowed").state == "off"
    assert hass.states.get("binary_sensor.eos_bat1_battery_export").state == "off"
    assert er.async_get(hass).async_get("sensor.eos_bat1_plan").disabled_by is not None
    # electric vehicle: idle, no discharge/export entities
    assert hass.states.get("sensor.eos_ev1_operation_mode").state == "idle"
    assert hass.states.get("binary_sensor.eos_ev1_charging_from_the_grid").state == "off"
    assert hass.states.get("binary_sensor.eos_ev1_discharging_allowed") is None
    # dishwasher: next start in an hour
    assert hass.states.get("binary_sensor.eos_dishwasher_running_as_planned").state == "off"
    assert hass.states.get("sensor.eos_dishwasher_next_start").state not in (
        "unknown",
        "unavailable",
    )
    # devices hang below the EOS device of the entry
    registry = dr.async_get(hass)
    bat = registry.async_get_device(identifiers={(DOMAIN, f"{_entry_id(hass)}_bat1")})
    assert bat is not None and bat.via_device_id is not None


def _entry_id(hass) -> str:
    return hass.config_entries.async_entries(DOMAIN)[0].entry_id


async def test_device_mode_change_settles(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    set_rooms(hass)
    entry = await setup(
        hass,
        aioclient_mock,
        device_solution(["SELF_CONSUMPTION"]),
        rooms=ROOMS + DEVICE_SUBENTRIES,
        devices=DEVICE_CONFIG,
    )
    assert hass.states.get("sensor.eos_bat1_operation_mode").state == "self_consumption"
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, json=device_solution(["FORCED_CHARGE"]))
    aioclient_mock.get(SUMMARY_URL, status=404)
    aioclient_mock.get(PLAN_URL, status=404)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("sensor.eos_bat1_operation_mode")
    assert state.state == "self_consumption"
    assert state.attributes["pending"].startswith("FORCED_CHARGE")
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.eos_bat1_operation_mode").state == "forced_charge"


async def test_devices_unavailable_without_a_valid_plan(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    await setup(
        hass,
        aioclient_mock,
        device_solution(["IDLE"], age_min=120),
        rooms=ROOMS + DEVICE_SUBENTRIES,
        devices=DEVICE_CONFIG,
    )
    assert hass.states.get("sensor.eos_bat1_operation_mode").state == "unavailable"
    assert hass.states.get("binary_sensor.eos_dishwasher_running_as_planned").state == (
        "unavailable"
    )


async def test_heat_pump_missing_in_eos(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    await setup(
        hass,
        aioclient_mock,
        solution(1),
        rooms=ROOMS,
        devices={"heat_pumps": {"wbb12": {}}},
    )
    reason = hass.states.get("sensor.eos_hp1_reason").state
    assert "heat pump 'hp1' not in the EOS configuration" in reason


async def test_device_subentry_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    entry = await setup(
        hass,
        aioclient_mock,
        device_solution(["IDLE"]),
        rooms=ROOMS + [device_entry("bat1", "battery")],
        devices=DEVICE_CONFIG,
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema["eos_device_id"].config["options"]
    # the heat pump and the battery already added are not offered
    assert [o["value"] for o in options] == ["ev1", "dishwasher"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"eos_device_id": "ev1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    sub = [s for s in entry.subentries.values() if s.subentry_type == "device"]
    assert {s.data["eos_device_id"]: s.data["kind"] for s in sub} == {
        "hp1": "heat_pump",
        "bat1": "battery",
        "ev1": "electric_vehicle",
    }
    assert hass.states.get("sensor.eos_ev1_operation_mode").state == "idle"


async def test_device_subentry_flow_nothing_to_add(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices"


async def test_reconfigure_connection(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    other = "http://eos2.test:8503"
    aioclient_mock.get(f"{other}/v1/health", json={"status": "alive"})
    aioclient_mock.get(f"{other}/v1/energy-management/optimization/solution", json=solution(1))
    aioclient_mock.get(f"{other}/v1/config/devices", json=HP_DEVICES)
    aioclient_mock.get(f"{other}/v1/energy-management/history/summary", status=404)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: other + "/", CONF_SCAN_INTERVAL: 30}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_URL: other, CONF_SCAN_INTERVAL: 30}


async def test_device_subentry_offers_the_heat_pump_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    entry = await setup(
        hass, aioclient_mock, solution(1), rooms=ROOMS, heat_pump="", devices=DEVICE_CONFIG
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"), context={"source": config_entries.SOURCE_USER}
    )
    options = result["data_schema"].schema["eos_device_id"].config["options"]
    assert [o["value"] for o in options] == ["hp1", "bat1", "ev1", "dishwasher"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"eos_device_id": "hp1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("sensor.eos_hp1_sg_ready_target") is not None
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"), context={"source": config_entries.SOURCE_USER}
    )
    options = result["data_schema"].schema["eos_device_id"].config["options"]
    assert "hp1" not in [o["value"] for o in options]


async def test_rooms_are_devices(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass, living=21.0, bath=20.8)
    hass.states.async_set("binary_sensor.bath_window", "on")
    hass.states.async_set("sensor.bath_humidity", "50")
    rooms = [
        room("Living", "sensor.living"),
        room(
            "Bath",
            "climate.bath",
            ["binary_sensor.bath_window"],
            humidity_entity="sensor.bath_humidity",
        ),
    ]
    entry = await setup(hass, aioclient_mock, solution(1), rooms=rooms)
    assert float(hass.states.get("sensor.eos_raum_living_room_temperature").state) == 21.0
    margin = hass.states.get("sensor.eos_raum_living_margin_to_the_lower_limit")
    assert float(margin.state) == pytest.approx(1.5)  # 21.0 - 19.5
    assert margin.attributes["lower_c"] == 19.5
    assert hass.states.get("binary_sensor.eos_raum_bath_airing").state == "on"
    assert hass.states.get("binary_sensor.eos_raum_bath_humidity_high").state == "off"
    assert hass.states.get("sensor.eos_raum_bath_comfort_suggestion").state == "unknown"
    registry = dr.async_get(hass)
    living = registry.async_get_device(
        identifiers={
            (
                DOMAIN,
                f"{entry.entry_id}_room_"
                + next(s.subentry_id for s in entry.subentries.values() if s.title == "Living"),
            )
        }
    )
    assert living is not None and living.name == "EOS Raum Living"
    hub = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert living.via_device_id == hub.id


async def test_room_margin_in_holiday(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass, living=17.0, bath=18.0)
    hass.states.async_set("input_boolean.away", "on")
    await setup(
        hass,
        aioclient_mock,
        solution(0, away=1),
        options={"holiday_entity": "input_boolean.away"},
        rooms=ROOMS,
    )
    margin = hass.states.get("sensor.eos_raum_living_margin_to_the_lower_limit")
    assert float(margin.state) == pytest.approx(1.0)  # 17.0 - holiday floor 16.0


async def test_migration_from_version_1_keeps_entity_ids(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """0.4 entries (entry = heat pump) become EOS entries with a heat pump subentry."""
    set_rooms(hass)
    aioclient_mock.get(SOLUTION_URL, json=chart_solution())
    aioclient_mock.get(DEVICES_URL, json=HP_DEVICES)
    aioclient_mock.get(SUMMARY_URL, status=404)
    eos_extras(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EOS hp1",
        unique_id=f"{URL}#hp1",
        data={CONF_URL: URL, "device_id": "hp1", CONF_SCAN_INTERVAL: 60},
        options=dict(DEFAULTS),
        subentries_data=ROOMS,
        version=1,
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, name="EOS hp1"
    )
    # entity ids as 0.4 created them (device "EOS hp1")
    old = {
        "sg_target": ("sensor", "eos_hp1_sg_ready_target"),
        "plan": ("sensor", "eos_hp1_plan"),
        "planned_room_temperature": ("sensor", "eos_hp1_planned_room_temperature"),
        "standby": ("binary_sensor", "eos_hp1_standby_target"),
        "reset_shadow": ("button", "eos_hp1_reset_shadow_comparison"),
    }
    for key, (domain, object_id) in old.items():
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            suggested_object_id=object_id,
            config_entry=entry,
            device_id=old_device.id,
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2
    assert entry.title == "EOS" and entry.unique_id == URL
    assert "device_id" not in entry.data
    assert entry.options["plan_entities"] is False  # EOS's adapter may still publish them
    heat_pumps = [s for s in entry.subentries.values() if s.subentry_type == "device"]
    assert [(s.data["eos_device_id"], s.data["kind"]) for s in heat_pumps] == [("hp1", "heat_pump")]
    assert len([s for s in entry.subentries.values() if s.subentry_type == "room"]) == 2
    for key, (domain, object_id) in old.items():
        entity_id = f"{domain}.{object_id}"
        assert registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}") == entity_id
        assert hass.states.get(entity_id) is not None, entity_id
    # heat pump entities moved to the heat pump device, the plan stays at "EOS"
    sg = registry.async_get("sensor.eos_hp1_sg_ready_target")
    plan = registry.async_get("sensor.eos_hp1_plan")
    assert sg.config_subentry_id == heat_pumps[0].subentry_id
    assert plan.device_id == old_device.id and sg.device_id != old_device.id
    assert hass.states.get("sensor.eos_hp1_sg_ready_target").state == "2"
    assert len(hass.states.get("sensor.eos_hp1_plan").attributes["data"]) == 8


# ----------------------------------------------------------------------
# Measurements to EOS, plan columns, repairs, statistics, actions, calendar
# ----------------------------------------------------------------------


def _puts(aioclient_mock) -> list[dict]:
    return [c[2] for c in aioclient_mock.mock_calls if c[0] == "PUT" and str(c[1]) == MEASURE_URL]


async def test_measurements_are_sent_to_eos(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    set_rooms(hass, living=21.0, bath=20.8)
    hass.states.async_set("sensor.tank", "47.5")
    hass.states.async_set("sensor.house_meter", "1234.5")
    hass.states.async_set("binary_sensor.dimmed", "off")
    hp = ConfigSubentryData(
        data={
            "eos_device_id": "hp1",
            "kind": "heat_pump",
            "measurements": {
                "hp1-dhw-temperature": "sensor.tank",
                "hp1-power-limited": "binary_sensor.dimmed",
                "hp1-room-temperature": "sensor.ignored",  # computed values win
            },
        },
        subentry_type="device",
        title="hp1",
        unique_id="hp1",
    )
    entry = await setup(
        hass,
        aioclient_mock,
        solution(1),
        options={"measurements": {"sensor.house_meter": "sensor.house_meter"}},
        rooms=[hp, *ROOMS],
        heat_pump="",
    )
    puts = _puts(aioclient_mock)
    assert len(puts) == 1
    body = puts[0]
    assert body["interval"] == "1 hour" and "start_datetime" in body
    assert body["hp1-dhw-temperature"] == [47.5]
    assert body["hp1-power-limited"] == [0.0]
    assert body["sensor.house_meter"] == [1234.5]
    assert body["hp1-room-temperature"] == [20.9]  # mean of the rooms
    assert body["hp1-room-min-margin"] == [pytest.approx(1.3)]  # bath 20.8 - 19.5
    assert body["hp1-away"] == [0.0] and body["hp1-reduce-lock-until-ts"] == [0.0]
    # not again before the interval
    await entry.runtime_data.async_refresh()
    assert len(_puts(aioclient_mock)) == 1
    freezer.tick(timedelta(minutes=5, seconds=1))
    await entry.runtime_data.async_refresh()
    assert len(_puts(aioclient_mock)) == 2


async def test_measurements_off(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    await setup(
        hass, aioclient_mock, solution(1), options={"send_measurements": False}, rooms=ROOMS
    )
    assert _puts(aioclient_mock) == []


async def test_plan_columns_as_entities(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    mode = hass.states.get("sensor.eos_hp1_heating_mode")
    assert mode is not None and float(mode.state) == 1.0
    assert float(hass.states.get("sensor.eos_hp1_room_temperature_c").state) == 21.25
    assert (
        hass.states.get("sensor.eos_hp1_room_temperature_c").attributes["unit_of_measurement"]
        == "°C"
    )
    assert float(hass.states.get("sensor.eos_load_energy_wh").state) == 100.0
    registry = er.async_get(hass)
    hp_sub = next(s for s in entry.subentries.values() if s.data.get("kind") == "heat_pump")
    assert registry.async_get("sensor.eos_hp1_heating_mode").config_subentry_id == (
        hp_sub.subentry_id
    )
    assert registry.async_get("sensor.eos_load_energy_wh").config_subentry_id is None
    # a column that appears in a later plan gets its entity then
    sol = chart_solution()
    for row in sol["solution"]["data"].values():
        row["hp1_standby"] = 1.0
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, json=sol)
    eos_extras(aioclient_mock)
    aioclient_mock.get(SUMMARY_URL, status=404)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.eos_hp1_standby").state) == 1.0


async def test_plan_columns_unavailable_with_old_plan_and_option_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    await setup(hass, aioclient_mock, chart_solution(age_min=120), rooms=ROOMS)
    assert hass.states.get("sensor.eos_hp1_heating_mode") is None  # never seen a valid plan


async def test_plan_columns_not_taken_from_another_source(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """A sensor.eos_<column> published by EOS's own adapter is not doubled as _2."""
    set_rooms(hass)
    hass.states.async_set("sensor.eos_hp1_heating_mode", "1.0")  # unregistered, like EOS
    await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    assert hass.states.get("sensor.eos_hp1_heating_mode_2") is None
    assert er.async_get(hass).async_get("sensor.eos_hp1_heating_mode") is None
    assert hass.states.get("sensor.eos_hp1_dhw_mode") is not None  # the others are added


async def test_plan_columns_option_off(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    await setup(
        hass, aioclient_mock, chart_solution(), options={"plan_entities": False}, rooms=ROOMS
    )
    assert hass.states.get("sensor.eos_hp1_heating_mode") is None


async def test_repairs(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer):
    set_rooms(hass)
    issues = ir.async_get(hass)
    entry = await setup(
        hass,
        aioclient_mock,
        solution(1, age_min=120),
        rooms=[
            *ROOMS,
            ConfigSubentryData(
                data={"eos_device_id": "bat9", "kind": "battery"},
                subentry_type="device",
                title="bat9",
                unique_id="bat9",
            ),
        ],
    )
    eid = entry.entry_id
    assert issues.async_get_issue(DOMAIN, f"{eid}_plan_stale") is not None
    assert issues.async_get_issue(DOMAIN, f"{eid}_device_missing_bat9") is not None
    assert issues.async_get_issue(DOMAIN, f"{eid}_eos_unreachable") is None
    # EOS gone for more than 30 minutes
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, status=503)
    freezer.tick(timedelta(minutes=31))
    await entry.runtime_data.async_refresh()
    assert issues.async_get_issue(DOMAIN, f"{eid}_eos_unreachable") is not None
    assert issues.async_get_issue(DOMAIN, f"{eid}_plan_stale") is None
    # back
    aioclient_mock.clear_requests()
    aioclient_mock.get(SOLUTION_URL, json=solution(1))
    eos_extras(aioclient_mock)
    aioclient_mock.get(SUMMARY_URL, status=404)
    aioclient_mock.get(DEVICES_URL, json=HP_DEVICES)
    await entry.runtime_data.async_refresh()
    assert issues.async_get_issue(DOMAIN, f"{eid}_eos_unreachable") is None


async def test_measurement_failure_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    set_rooms(hass)
    aioclient_mock.put(MEASURE_URL, status=500)  # registered before setup: wins
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"{entry.entry_id}_measurements_failed") is None
    for _ in range(7):
        freezer.tick(timedelta(minutes=5, seconds=1))
        await entry.runtime_data.async_refresh()
    issue = issues.async_get_issue(DOMAIN, f"{entry.entry_id}_measurements_failed")
    assert issue is not None and issue.translation_placeholders["error"] == "EOS HTTP 500"


async def test_plan_statistics(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    now = dt_util.now()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    data = {}
    for i in range(16):
        data[(start + timedelta(minutes=15 * i)).isoformat()] = {
            "load_energy_wh": 100.0,
            "hp1_heating_mode": 1.0,
            "hp1_energy_wh": 250.0,
            "hp1_room_temperature_c": 21.0 + i / 10,
        }
    sol = {"generated_at": (now - timedelta(minutes=5)).isoformat(), "solution": {"data": data}}
    hass.config.components.add("recorder")
    calls = []
    with patch(
        "homeassistant.components.recorder.statistics.async_add_external_statistics",
        side_effect=lambda h, meta, rows: calls.append((meta, list(rows))),
    ):
        entry = await setup(hass, aioclient_mock, sol, rooms=ROOMS)
        by_id = {meta["statistic_id"]: rows for meta, rows in calls}
        assert set(by_id) >= {
            "eos_control:plan_house_load",
            "eos_control:plan_hp1_heat_pump_energy",
            "eos_control:plan_hp1_room_temperature",
        }
        load = by_id["eos_control:plan_house_load"]
        assert [r["start"] for r in load] == [start, start + timedelta(hours=1)]
        assert load[0]["mean"] == pytest.approx(0.4)  # 4 x 100 Wh
        room = by_id["eos_control:plan_hp1_room_temperature"]
        assert room[1]["mean"] == pytest.approx((21.4 + 21.5 + 21.6 + 21.7) / 4)
        meta = next(m for m, _ in calls if m["statistic_id"] == "eos_control:plan_house_load")
        assert meta["unit_of_measurement"] == "kWh" and meta["source"] == DOMAIN
        # written once per hour
        calls.clear()
        await entry.runtime_data.async_refresh()
        assert calls == []


async def test_actions(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    devices = {**HP_DEVICES, "home_appliances": {"washer": {"device_id": "washer"}}}
    entry = await setup(hass, aioclient_mock, solution(0), rooms=ROOMS, devices=devices)
    coordinator = entry.runtime_data
    await hass.services.async_call(
        DOMAIN, "lock_reduction", {"duration": {"hours": 2}}, blocking=True
    )
    body = _puts(aioclient_mock)[-1]
    assert body["hp1-reduce-lock-until-ts"][0] == pytest.approx(
        (dt_util.now() + timedelta(hours=2)).timestamp(), abs=5
    )
    target = hass.states.get("sensor.eos_hp1_sg_ready_target")
    assert "reduction locked" in target.attributes["pending"]  # NORMAL after the settle time
    await hass.services.async_call(DOMAIN, "clear_reduction_lock", {}, blocking=True)
    assert coordinator.manual.lock_until is None
    # holiday by action
    ret = dt_util.now() + timedelta(days=3)
    await hass.services.async_call(DOMAIN, "set_holiday", {"return_at": ret}, blocking=True)
    body = _puts(aioclient_mock)[-1]
    assert body["hp1-away"] == [1.0]
    assert body["hp1-return-ts"][0] == pytest.approx(ret.timestamp(), abs=1)
    assert coordinator.data.holiday is True
    await hass.services.async_call(DOMAIN, "end_holiday", {}, blocking=True)
    assert coordinator.data.holiday is False
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_holiday",
            {"return_at": dt_util.now() - timedelta(hours=1)},
            blocking=True,
        )
    # appliance run
    finish = dt_util.now() + timedelta(hours=6)
    await hass.services.async_call(
        DOMAIN,
        "request_run",
        {"device": "washer", "finish_by": finish, "duration": {"minutes": 90}},
        blocking=True,
    )
    body = _puts(aioclient_mock)[-1]
    assert body["washer-requested"] == [1.0] and body["washer-duration"] == [1.5]
    assert body["washer-deadline"][0] == pytest.approx(finish.timestamp(), abs=1)
    await hass.services.async_call(DOMAIN, "cancel_run", {"device": "washer"}, blocking=True)
    assert _puts(aioclient_mock)[-1]["washer-requested"] == [0.0]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "request_run", {"device": "oven", "finish_by": finish}, blocking=True
        )
    # stored across a reload
    await hass.services.async_call(
        DOMAIN, "lock_reduction", {"duration": {"hours": 1}}, blocking=True
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.manual.lock_until is not None


async def test_plan_calendar(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    set_rooms(hass)
    sol = solution(1)
    rows = sorted(sol["solution"]["data"])
    for stamp in rows[2:4]:
        sol["solution"]["data"][stamp]["hp1_dhw_mode"] = 1.0
    await setup(hass, aioclient_mock, sol, rooms=ROOMS)
    state = hass.states.get("calendar.eos_plan")
    assert state is not None
    assert state.attributes["message"] == "Hot water charge (heat pump)"
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.eos_plan",
            "start_date_time": dt_util.now() - timedelta(hours=1),
            "end_date_time": dt_util.now() + timedelta(hours=4),
        },
        blocking=True,
        return_response=True,
    )
    events = result["calendar.eos_plan"]["events"]
    assert [e["summary"] for e in events] == ["Hot water charge (heat pump)"]


async def test_diagnostics_with_eos_version(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    from custom_components.eos_control.diagnostics import async_get_config_entry_diagnostics

    set_rooms(hass)
    entry = await setup(hass, aioclient_mock, solution(1), rooms=ROOMS)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["eos_version"] == "0.3.0"
    assert "measurements" in diag and "hp1-away" in diag["measurements"]


async def test_instruction_entities(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    """Active instructions of the EOS plan as sensor.eos_<resource> (mode, factor)."""
    set_rooms(hass)
    now = dt_util.now()
    plan = {
        "generated_at": (now - timedelta(minutes=5)).isoformat(),
        "instructions": [
            {
                "type": "FRBCInstruction",
                "resource_id": "hp1_space_heating",
                "execution_time": (now - timedelta(minutes=30)).isoformat(),
                "operation_mode_id": "REDUCED",
                "operation_mode_factor": 1.0,
            },
            {
                "type": "FRBCInstruction",
                "resource_id": "hp1_space_heating",
                "execution_time": (now - timedelta(minutes=10)).isoformat(),
                "operation_mode_id": "NORMAL",
                "operation_mode_factor": 1.0,
            },
            {
                "type": "FRBCInstruction",
                "resource_id": "hp1_space_heating",
                "execution_time": (now + timedelta(hours=2)).isoformat(),
                "operation_mode_id": "PREHEAT",
                "operation_mode_factor": 0.5,
            },
            {
                "type": "OMBCInstruction",
                "resource_id": "washer",
                "execution_time": (now - timedelta(hours=1)).isoformat(),
                "operation_mode_id": "OFF",
                "operation_mode_factor": 0.0,
            },
            {"type": "PEBCPowerConstraints", "resource_id": "x", "execution_time": now.isoformat()},
        ],
    }
    eos_extras(aioclient_mock, plan=plan)
    entry = await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    heating = hass.states.get("sensor.eos_hp1_space_heating")
    assert heating.state == "normal"
    assert heating.attributes["operation_mode_factor"] == 1.0
    assert heating.attributes["next_mode"] == "preheat"
    assert hass.states.get("sensor.eos_washer").state == "off"
    assert hass.states.get("sensor.eos_x") is None
    registry = er.async_get(hass)
    hp_sub = next(s for s in entry.subentries.values() if s.data.get("kind") == "heat_pump")
    assert registry.async_get("sensor.eos_hp1_space_heating").config_subentry_id == (
        hp_sub.subentry_id
    )
    assert registry.async_get("sensor.eos_washer").config_subentry_id is None


async def test_instructions_from_another_source_not_taken_over(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    set_rooms(hass)
    hass.states.async_set("sensor.eos_hp1_space_heating", "normal")  # EOS adapter
    now = dt_util.now()
    plan = {
        "generated_at": now.isoformat(),
        "instructions": [
            {
                "resource_id": "hp1_space_heating",
                "execution_time": (now - timedelta(minutes=1)).isoformat(),
                "operation_mode_id": "NORMAL",
                "operation_mode_factor": 1.0,
            }
        ],
    }
    eos_extras(aioclient_mock, plan=plan)
    await setup(hass, aioclient_mock, chart_solution(), rooms=ROOMS)
    assert er.async_get(hass).async_get("sensor.eos_hp1_space_heating") is None
    assert hass.states.get("sensor.eos_hp1_space_heating_2") is None
