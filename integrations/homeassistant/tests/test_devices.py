"""EOS devices and their planned state (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DEVICES = Path(__file__).resolve().parents[1] / "custom_components" / "eos_control" / "devices.py"
spec = importlib.util.spec_from_file_location("eos_control_devices", DEVICES)
assert spec and spec.loader
devices = importlib.util.module_from_spec(spec)
sys.modules["eos_control_devices"] = devices
spec.loader.exec_module(devices)

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 2, 10, 7, tzinfo=TZ)
START = NOW.replace(minute=0)

CONFIG = {
    "batteries": {"bat1": {"device_id": "bat1", "capacity_wh": 10000, "max_charge_power_w": 5000}},
    "electric_vehicles": [{"device_id": "ev1", "max_charge_power_w": 11000}],
    "home_appliances": {"dishwasher": {"device_id": "dishwasher"}},
    "heat_pumps": {"wbb12": {"device_id": "wbb12"}},
    "inverters": {"fronius": {}},
    "max_batteries": 1,
}


def battery_row(mode: str, factor: float = 1.0, soc: float = 0.5, did: str = "bat1") -> dict:
    row = {}
    for m in devices.OPERATION_MODES:
        row[f"{did}_{m.lower()}_op_mode"] = 1.0 if m == mode else 0.0
        row[f"{did}_{m.lower()}_op_factor"] = factor if m == mode else 0.0
    row[f"{did}_soc_factor"] = soc
    return row


def solution(rows: list[dict]) -> dict:
    data = {}
    for i, row in enumerate(rows):
        t = (START + timedelta(minutes=15 * i)).isoformat()
        data[t] = {"load_energy_wh": 100.0, **row}
    return {"generated_at": START.isoformat(), "solution": {"data": data}}


def test_parse_devices_from_mappings_and_lists():
    parsed = devices.parse_devices(CONFIG)
    by_id = {d.device_id: d for d in parsed}
    assert by_id["bat1"].kind == devices.KIND_BATTERY
    assert by_id["bat1"].max_charge_power_w == 5000
    assert by_id["ev1"].kind == devices.KIND_EV
    assert by_id["dishwasher"].kind == devices.KIND_APPLIANCE
    assert by_id["wbb12"].kind == devices.KIND_HEAT_PUMP
    assert by_id["fronius"].kind == devices.KIND_INVERTER
    assert devices.parse_devices(None) == []
    assert devices.parse_devices({"batteries": None, "heat_pumps": {}}) == []


def test_battery_state_now_and_next_change():
    bat = devices.EosDevice("bat1", devices.KIND_BATTERY, max_charge_power_w=5000)
    sol = solution(
        [
            battery_row("SELF_CONSUMPTION", 0.0, 0.40),
            battery_row("GRID_SUPPORT_IMPORT", 0.6, 0.45),  # 10:15 grid charging
            battery_row("GRID_SUPPORT_IMPORT", 0.6, 0.55),
            battery_row("PEAK_SHAVING", 1.0, 0.6),
        ]
    )
    state = devices.device_state(sol, bat, NOW)
    assert state.mode == "SELF_CONSUMPTION" and state.soc_pct == 40.0
    assert state.discharge_allowed is True and state.grid_charge is False
    assert state.power_w == 0.0
    assert state.next_change == START + timedelta(minutes=15)
    assert state.next_mode == "GRID_SUPPORT_IMPORT"
    later = devices.device_state(sol, bat, NOW + timedelta(minutes=10))
    assert later.mode == "GRID_SUPPORT_IMPORT" and later.grid_charge is True
    assert later.power_w == 3000  # 0.6 x 5000 W
    assert later.discharge_allowed is False
    assert later.next_mode == "PEAK_SHAVING"
    assert len(state.rows) == 4 and state.rows[1] == {
        "t": (START + timedelta(minutes=15)).isoformat(),
        "mode": "GRID_SUPPORT_IMPORT",
        "factor": 0.6,
        "soc_pct": 45.0,
    }


def test_battery_export_and_unknown_power():
    bat = devices.EosDevice("bat1", devices.KIND_BATTERY)  # no max charge power known
    state = devices.device_state(
        solution([battery_row("GRID_SUPPORT_EXPORT", 0.5), battery_row("FORCED_CHARGE", 1.0)]),
        bat,
        NOW + timedelta(minutes=10),
    )
    assert state.mode == "FORCED_CHARGE" and state.grid_charge is True
    assert state.power_w is None and state.next_change is None
    export = devices.device_state(solution([battery_row("GRID_SUPPORT_EXPORT", 0.5)]), bat, NOW)
    assert export.grid_export is True and export.discharge_allowed is True


def test_ev_charging():
    ev = devices.EosDevice("ev1", devices.KIND_EV, max_charge_power_w=11000)
    state = devices.device_state(
        solution([battery_row("GRID_SUPPORT_IMPORT", 0.5, 0.3, did="ev1")]), ev, NOW
    )
    assert state.grid_charge is True and state.power_w == 5500 and state.soc_pct == 30.0


def test_home_appliance_running_and_next_start():
    dish = devices.EosDevice("dishwasher", devices.KIND_APPLIANCE)
    start_ts = (START + timedelta(minutes=30)).timestamp()
    rows = [
        {
            "dishwasher_run_op_mode": 0.0,
            "dishwasher_energy_wh": 0.0,
            "dishwasher_next_start_ts": start_ts,
        },
        {
            "dishwasher_run_op_mode": 0.0,
            "dishwasher_energy_wh": 0.0,
            "dishwasher_next_start_ts": start_ts,
        },
        {
            "dishwasher_run_op_mode": 1.0,
            "dishwasher_energy_wh": 300.0,
            "dishwasher_next_start_ts": start_ts,
        },
    ]
    state = devices.device_state(solution(rows), dish, NOW)
    assert state.running is False
    assert state.next_start == START + timedelta(minutes=30)
    assert state.next_change == START + timedelta(minutes=30)
    running = devices.device_state(solution(rows), dish, NOW + timedelta(minutes=25))
    assert running.running is True and running.energy_wh == 300.0


def test_device_not_in_plan():
    bat = devices.EosDevice("bat2", devices.KIND_BATTERY)
    assert devices.device_state(solution([battery_row("IDLE")]), bat, NOW) is None
    assert devices.device_state(None, bat, NOW) is None
    assert devices.device_state({"solution": {"data": {}}}, bat, NOW) is None
    assert devices.device_state(solution([{"x": 1}]), bat, NOW) is None


def test_hold_settles_a_mode_change():
    bat = devices.EosDevice("bat1", devices.KIND_BATTERY, max_charge_power_w=5000)
    hold = devices.Hold(seconds=10)
    first = devices.device_state(solution([battery_row("SELF_CONSUMPTION", 0.0)]), bat, NOW)
    state, wait = hold.apply(first, NOW)
    assert state.mode == "SELF_CONSUMPTION" and wait is None
    change = devices.device_state(solution([battery_row("GRID_SUPPORT_IMPORT", 1.0)]), bat, NOW)
    held, wait = hold.apply(change, NOW)
    assert held.mode == "SELF_CONSUMPTION" and held.power_w == 0.0
    assert held.pending == "GRID_SUPPORT_IMPORT (in 10 s)" and wait == pytest.approx(10)
    accepted, wait = hold.apply(change, NOW + timedelta(seconds=10))
    assert accepted.mode == "GRID_SUPPORT_IMPORT" and accepted.power_w == 5000 and wait is None
    # a change back within the time is dropped
    hold.apply(first, NOW + timedelta(seconds=11))
    back, wait = hold.apply(change, NOW + timedelta(seconds=13))
    assert back.mode == "GRID_SUPPORT_IMPORT" and wait is None


def test_hold_zero_and_forget():
    bat = devices.EosDevice("bat1", devices.KIND_BATTERY)
    hold = devices.Hold(seconds=0)
    hold.apply(devices.device_state(solution([battery_row("IDLE")]), bat, NOW), NOW)
    state, wait = hold.apply(
        devices.device_state(solution([battery_row("FORCED_CHARGE")]), bat, NOW), NOW
    )
    assert state.mode == "FORCED_CHARGE" and wait is None
    hold.forget("bat1")
    assert "bat1" not in hold.accepted
