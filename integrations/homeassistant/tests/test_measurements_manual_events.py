"""Measurements for EOS, state set by actions and calendar events (no Home Assistant)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "eos_control"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"eos_control_{name}", PACKAGE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"eos_control_{name}"] = module
    spec.loader.exec_module(module)
    return module


measurements = _load("measurements")
manual = _load("manual")
plan_events = _load("plan_events")

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 2, 10, 7, tzinfo=TZ)


# ----------------------------------------------------------------------
# measurements
# ----------------------------------------------------------------------


def test_state_values():
    sv = measurements.state_value
    assert sv("k", "21.5") == 21.5
    assert sv("k", "on") == 1.0 and sv("k", "off") == 0.0
    assert sv("k", "unavailable") is None and sv("k", None) is None
    assert sv("k", "heat") is None
    assert (
        sv("hp-return-ts", "2026-11-01T01:00:00+01:00")
        == datetime(2026, 11, 1, 1, tzinfo=TZ).timestamp()
    )
    assert sv("hp-other", "2026-11-01T01:00:00+01:00") is None  # times only for *-ts


def test_keys():
    keys = ["wbb12-room-temperature", "wbb12.cycles", "waschmaschine-power", "sensor.meter"]
    assert measurements.device_keys(keys, "wbb12") == ["wbb12-room-temperature", "wbb12.cycles"]
    assert "wbb12-reduce-lock-until-ts" in measurements.computed_keys("wbb12")
    assert measurements.emr_keys(
        {
            "load_emr_keys": ["a", "b"],
            "grid_import_emr_keys": ["b", "c"],
            "pv_production_emr_keys": None,
        }
    ) == ["a", "b", "c"]
    assert measurements.emr_keys(None) == []


def _hp(**kwargs):
    base = dict(
        room_mean_c=20.456,
        min_margin_k=0.5,
        max_margin_k=2.0,
        humidity_warning=False,
        window_open_share=0.1,
        holiday=False,
        holiday_return=None,
        reduced_setpoint_c=18.0,
        holiday_floor_c=16.0,
        reduction_locked_until=None,
        now=NOW,
    )
    base.update(kwargs)
    return measurements.heat_pump_values("hp1", **base)


def test_heat_pump_values():
    values = _hp()
    assert values["hp1-room-temperature"] == 20.46
    assert values["hp1-humidity"] == 0.0 and values["hp1-away"] == 0.0
    assert values["hp1-return-ts"] == 0.0 and values["hp1-reduce-lock-until-ts"] == 0.0
    assert values["hp1-away-min-temperature"] == 16.0
    assert values["hp1-setback-room-temperature"] == 18.0
    lock = NOW + timedelta(hours=2)
    assert _hp(reduction_locked_until=lock)["hp1-reduce-lock-until-ts"] == lock.timestamp()
    ret = NOW + timedelta(days=2)
    away = _hp(holiday=True, holiday_return=ret, reduction_locked_until=lock, humidity_warning=True)
    assert away["hp1-away"] == 1.0 and away["hp1-return-ts"] == ret.timestamp()
    assert away["hp1-reduce-lock-until-ts"] == 0.0  # no lock while away
    assert away["hp1-humidity"] == 100.0
    assert _hp(reduction_locked_until=NOW - timedelta(minutes=1))["hp1-reduce-lock-until-ts"] == 0.0


def test_payload():
    body = measurements.payload({"b": 2.0, "a": None, "c": 1.5}, NOW)
    assert body == {"start_datetime": NOW.isoformat(), "interval": "1 hour", "b": [2.0], "c": [1.5]}
    assert measurements.payload({"a": None}, NOW) is None


# ----------------------------------------------------------------------
# manual state
# ----------------------------------------------------------------------


def test_holiday_and_lock():
    state = manual.ManualState()
    assert not state.holiday_active(NOW)
    state.holiday_start, state.holiday_end = NOW + timedelta(hours=1), NOW + timedelta(days=2)
    assert not state.holiday_active(NOW)
    assert state.holiday_active(NOW + timedelta(hours=2))
    state.lock_until = NOW + timedelta(hours=1)
    assert state.lock_active(NOW) == NOW + timedelta(hours=1)
    assert state.lock_active(NOW + timedelta(hours=2)) is None
    assert state.expire(NOW + timedelta(days=3)) is True
    assert state.holiday_end is None and state.lock_until is None
    assert state.expire(NOW + timedelta(days=3)) is False


def test_runs_and_storage():
    state = manual.ManualState()
    state.request("washer", NOW + timedelta(hours=5), 1.5)
    assert state.run_measurements() == {
        "washer-requested": 1.0,
        "washer-deadline": (NOW + timedelta(hours=5)).timestamp(),
        "washer-duration": 1.5,
    }
    restored = manual.ManualState.from_dict(state.as_dict())
    assert restored.runs["washer"].finish_by == NOW + timedelta(hours=5)
    state.cancel("washer")
    assert state.run_measurements() == {"washer-requested": 0.0}
    state.sent()
    assert state.run_measurements() == {}
    state.request("washer", NOW + timedelta(minutes=10), None)
    assert state.expire(NOW + timedelta(minutes=11)) is True
    assert state.run_measurements() == {"washer-requested": 0.0}
    assert manual.ManualState.from_dict(None).runs == {}
    assert manual.ManualState.from_dict({"runs": {"x": {"finish_by": "bad"}}}).runs == {}


# ----------------------------------------------------------------------
# calendar events
# ----------------------------------------------------------------------


def _rows(values, **const):
    start = NOW.replace(minute=0)
    return [
        {"t": (start + timedelta(minutes=15 * i)).isoformat(), **const, **v}
        for i, v in enumerate(values)
    ]


def test_heat_pump_events():
    rows = _rows(
        [
            {"heating_mode": 0, "standby": 1},
            {"heating_mode": 0, "standby": 1},
            {"heating_mode": 1, "dhw_mode": 1},
            {"heating_mode": 2, "dhw_mode": 1},
            {"heating_mode": 2},
            {"heating_mode": 3, "dhw_mode": 2},
            {"heating_mode": 1},
        ]
    )
    events = plan_events.plan_events(rows, [], 15, "de")
    got = [(e.start.strftime("%H:%M"), e.end.strftime("%H:%M"), e.summary) for e in events]
    assert got == [
        ("10:00", "10:30", "Wärmepumpe: Standby"),
        ("10:30", "11:00", "Warmwasser laden (Wärmepumpe)"),
        ("10:45", "11:15", "Wärmepumpe: Heizen angehoben"),
        ("11:15", "11:30", "Warmwasser laden (Heizstab)"),
        ("11:15", "11:30", "Wärmepumpe: Vorheizen"),
    ]
    assert len({e.uid for e in events}) == len(events)


def test_device_events_and_gaps():
    washer = _rows([{"running": False}, {"running": True}, {"running": True}, {"running": False}])
    battery = _rows(
        [
            {"mode": "GRID_SUPPORT_IMPORT"},
            {"mode": "SELF_CONSUMPTION"},
            {"mode": "GRID_SUPPORT_EXPORT"},
        ]
    )
    events = plan_events.plan_events(
        [], [("washer", "home_appliance", washer), ("bat1", "battery", battery)], 15, "en"
    )
    assert [(e.start.strftime("%H:%M"), e.summary) for e in events] == [
        ("10:00", "bat1 charges from the grid"),
        ("10:15", "washer runs"),
        ("10:30", "bat1 exports"),
    ]
    # a gap in the slots ends an event
    gap = [
        {"t": NOW.replace(minute=0).isoformat(), "standby": 1},
        {"t": NOW.replace(minute=30).isoformat(), "standby": 1},
    ]
    assert len(plan_events.plan_events(gap, [], 15, "xx")) == 2  # unknown language: English
