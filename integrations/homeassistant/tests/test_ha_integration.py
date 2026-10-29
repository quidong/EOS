"""EOS Control in Home Assistant: config flow, rooms, coordinator, shadow comparison."""

from __future__ import annotations

from datetime import timedelta

import pytest
from custom_components.eos_control.const import (
    CONF_DEVICE_ID,
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


async def setup(hass, aioclient_mock, sol, options=None, rooms=None) -> MockConfigEntry:
    aioclient_mock.get(SOLUTION_URL, json=sol)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EOS hp1",
        data={CONF_URL: URL, CONF_DEVICE_ID: "hp1", CONF_SCAN_INTERVAL: 60},
        options={**DEFAULTS, **(options or {})},
        subentries_data=rooms or [],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


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
    aioclient_mock.get(f"{URL}/v1/health", json={"status": "alive"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    aioclient_mock.get(SOLUTION_URL, json=solution(1))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL + "/", CONF_DEVICE_ID: "hp1", CONF_SCAN_INTERVAL: 60}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == URL
    assert result["options"]["sg_reduced"] == 1


async def test_user_flow_cannot_connect(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{URL}/v1/health", status=500)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_DEVICE_ID: "hp1", CONF_SCAN_INTERVAL: 60}
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
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HOLIDAY_ENTITY] == "input_boolean.away"
    assert entry.options["max_room_c"] == 24.0


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
    sub = next(iter(entry.subentries.values()))
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
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_URL: URL, CONF_DEVICE_ID: "hp1", CONF_SCAN_INTERVAL: 60},
        options=dict(DEFAULTS),
        subentries_data=ROOMS,
        title="EOS hp1",
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
    # raised by an automation: ignored
    hass.states.async_set(
        "climate.living_controller",
        "heat",
        {"temperature": 23.0},
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
