"""Decision logic of EOS Control (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

LOGIC = Path(__file__).resolve().parents[1] / "custom_components" / "eos_control" / "logic.py"
spec = importlib.util.spec_from_file_location("eos_control_logic", LOGIC)
assert spec and spec.loader
logic = importlib.util.module_from_spec(spec)
sys.modules["eos_control_logic"] = logic
spec.loader.exec_module(logic)

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 2, 10, 7, tzinfo=TZ)


def solution(
    modes, dhw=None, away=None, zone=None, standby=None, generated=NOW - timedelta(minutes=5)
):
    start = NOW.replace(minute=0)
    data = {}
    for i, mode in enumerate(modes):
        t = (start + timedelta(minutes=15 * i)).isoformat()
        row = {"hp1_heating_mode": float(mode), "hp1_dhw_mode": float((dhw or [0] * len(modes))[i])}
        if away is not None:
            row["hp1_away"] = float(away[i])
        if zone is not None:
            row["hp1_zone_setback"] = float(zone[i])
        if standby is not None:
            row["hp1_standby"] = float(standby[i])
        data[t] = row
    return {"generated_at": generated.isoformat(), "solution": {"data": data}}


def rooms(*temps, lower=19.5, upper=23.5, **kw):
    return [
        logic.RoomState(name=f"r{i}", temperature=t, lower_c=lower, upper_c=upper, **kw)
        for i, t in enumerate(temps)
    ]


def run(modes, room_list=None, settings=None, **kw):
    plan_kw = {k: kw.pop(k) for k in ("dhw", "away", "zone", "standby", "generated") if k in kw}
    data = logic.ControlInput(
        now=kw.pop("now", NOW),
        plan=logic.parse_plan(solution(modes, **plan_kw), "hp1"),
        rooms=room_list if room_list is not None else rooms(21.0, 20.8, 21.2),
        **kw,
    )
    return logic.decide(data, settings or logic.Settings())


# ----------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------


def test_parse_plan_and_current_slot():
    plan = logic.parse_plan(solution([0, 1, 2, 2], dhw=[0, 0, 1, 0]), "hp1")
    assert len(plan.slots) == 4
    assert plan.current(NOW).heating_mode == 0  # 10:00 slot
    assert plan.current(NOW + timedelta(minutes=10)).heating_mode == 1
    assert plan.dhw_charge_within(NOW, 1.0)
    assert not logic.parse_plan(solution([0, 0]), "hp1").dhw_charge_within(NOW, 3.0)


def test_parse_plan_rejects_other_device():
    with pytest.raises(ValueError):
        logic.parse_plan(solution([1, 1]), "other")


# ----------------------------------------------------------------------
# SG Ready target
# ----------------------------------------------------------------------


def test_mapping_of_the_modes():
    assert run([1] * 4).sg_target == 2
    assert run([2] * 4).sg_target == 3
    assert run([4] * 4).sg_target == 4
    assert run([0] * 4).sg_target == 1
    custom = logic.Settings(sg_by_mode={0: 1, 1: 2, 2: 2, 3: 3, 4: 4})
    assert run([2] * 4, settings=custom).sg_target == 2


def test_stale_or_missing_plan_falls_back_to_normal():
    d = run([0] * 4, generated=NOW - timedelta(minutes=50))
    assert d.sg_target == 2 and "50 min old" in d.reason and not d.eos_fresh
    empty = logic.decide(
        logic.ControlInput(now=NOW, plan=None, rooms=rooms(21.0)), logic.Settings()
    )
    assert empty.sg_target == 2 and empty.reason == "NORMAL: no EOS plan"


def test_not_heating_season_is_normal():
    assert run([0] * 4, heating_season=False).sg_target == 2


def test_reduction_needs_every_room_above_its_limit_plus_hysteresis():
    d = run([0] * 4, rooms(21.0, 19.8, 21.2))
    assert d.sg_target == 2 and "r1 19.8 < 20.0" in d.reason
    # running reduction ends only below the limit itself
    assert run([0] * 4, rooms(21.0, 19.8, 21.2), sg_actual=1).sg_target == 1
    assert run([0] * 4, rooms(21.0, 19.4, 21.2), sg_actual=1).sg_target == 2


def test_per_room_limits():
    office = logic.RoomState("office", 17.0, lower_c=16.5, upper_c=22.0)
    assert run([0] * 4, rooms(21.0, 21.0) + [office]).sg_target == 1
    office.lower_c = 19.5
    assert run([0] * 4, rooms(21.0, 21.0) + [office]).sg_target == 2


def test_airing_rooms_are_not_too_cold():
    aired = rooms(21.0, 18.0, airing=False)
    aired[1].airing = True
    d = run([0] * 4, aired)
    assert d.sg_target == 1
    assert d.room_min_margin_k == pytest.approx(1.5)  # aired room left out of the margin


def test_humidity_warning_and_too_few_rooms_block_reduction():
    humid = rooms(21.0, 21.0)
    humid[0].humidity_high = True
    assert run([0] * 4, humid).sg_target == 2
    settings = logic.Settings(min_valid_rooms=3)
    assert "only 2 room values" in run([0] * 4, rooms(21.0, 21.0), settings=settings).reason


def test_reduction_lock():
    locked = run([0] * 4, reduction_locked_until=NOW + timedelta(minutes=30))
    assert locked.sg_target == 2 and "locked" in locked.reason
    assert run([0] * 4, reduction_locked_until=NOW - timedelta(minutes=1)).sg_target == 1


def test_raising_stops_at_the_warmest_room():
    d = run([2] * 4, rooms(21.0, 23.6))
    assert d.sg_target == 2 and "r1 23.6" in d.reason
    assert run([2] * 4, rooms(21.0, 23.4)).sg_target == 3


def test_zone_rooms_use_the_reduced_setpoint():
    upstairs = rooms(19.6, in_zone=True)
    downstairs = rooms(21.0)
    d = run([0] * 4, upstairs + downstairs, zone=[1] * 4, reduced_setpoint_c=19.0)
    assert d.sg_target == 1  # zone limit 18.0
    assert run([0] * 4, upstairs + downstairs, zone=[0] * 4, reduced_setpoint_c=19.0).sg_target == 2


# ----------------------------------------------------------------------
# Holiday
# ----------------------------------------------------------------------


def holiday(modes, temps=(17.5, 18.0), **kw):
    kw.setdefault("away", [1] * len(modes))
    return run(
        modes,
        rooms(*temps),
        holiday=True,
        holiday_return=kw.pop("ret", NOW + timedelta(days=2)),
        reduced_setpoint_c=18.0,
        **kw,
    )


def test_holiday_normal_maps_to_reduced_and_standby_follows_eos():
    d = holiday([0] * 4)
    assert d.standby and d.sg_target == 1 and not d.dhw_charge
    heat = holiday([1] * 4)
    assert not heat.standby and heat.sg_target == 1  # holiday temperature via reduced setpoint
    assert "EOS plans NORMAL" in heat.standby_reason


def test_holiday_ignores_a_plan_without_holiday():
    d = holiday([1] * 4, away=[0] * 4)
    assert d.standby and "without holiday" in d.standby_reason


def test_holiday_floor_and_restart():
    cold = holiday([0] * 4, temps=(15.8, 18.0))
    assert not cold.standby and "15.8" in cold.standby_reason
    missing = holiday([0] * 4, generated=NOW - timedelta(hours=2))
    assert missing.standby  # EOS missing: standby as reserve
    restart = holiday([0] * 4, generated=NOW - timedelta(hours=2), ret=NOW + timedelta(minutes=30))
    assert not restart.standby and restart.dhw_setpoints_restore


def test_holiday_hot_water_charge_restores_setpoints_first():
    soon = holiday([0] * 12, dhw=[0] * 8 + [1] * 4)
    assert soon.standby and soon.dhw_setpoints_restore and not soon.dhw_charge
    now = holiday([0] * 4, dhw=[1] * 4)
    assert not now.standby and now.dhw_charge


def test_holiday_standby_from_the_eos_column():
    """EOS states the standby itself: REDUCED near the floor is no standby."""
    d = holiday([0] * 4, standby=[1] * 4)
    assert d.standby and d.standby_reason == "standby: EOS plans standby"
    near_floor = holiday([0] * 4, standby=[0] * 4)
    assert not near_floor.standby and "EOS plans REDUCED" in near_floor.standby_reason
    # the room floor of the controller still wins over the plan
    cold = holiday([0] * 4, standby=[1] * 4, temps=(15.8, 18.0))
    assert not cold.standby
    # without the column (older EOS): derived from REDUCED without hot water
    assert holiday([0] * 4).standby_reason == "standby: EOS plans REDUCED without hot water"


def test_parse_plan_standby_column():
    plan = logic.parse_plan(solution([0, 0], standby=[1, 0]), "hp1")
    assert [s.standby for s in plan.slots] == [True, False]
    assert logic.parse_plan(solution([0]), "hp1").slots[0].standby is None


def test_hot_water_outside_holiday():
    assert run([1] * 4, dhw=[1] * 4).dhw_charge
    assert not run([1] * 4, dhw=[1] * 4, generated=NOW - timedelta(hours=1)).dhw_charge


# ----------------------------------------------------------------------
# Figures for EOS
# ----------------------------------------------------------------------


def test_room_figures():
    room_list = rooms(20.0, 21.0, 22.0)
    room_list[2].humidity_pct = 66.0
    room_list[0].has_windows = room_list[1].has_windows = True
    room_list[1].window_open = room_list[1].airing = True
    d = run([1] * 4, room_list)
    assert d.room_mean_c == pytest.approx(21.0)
    assert d.room_min_margin_k == pytest.approx(0.5)
    assert d.room_max_margin_k == pytest.approx(1.5)
    assert d.coldest_room == "r0" and d.warmest_room == "r2"
    assert d.window_open_share == pytest.approx(0.5)
    assert d.humidity_max_pct == 66.0
    attrs = d.as_attributes()
    assert attrs["eos_mode"] == "NORMAL" and attrs["room_mean_c"] == 21.0


def test_margins_use_the_limits_valid_now():
    """Holiday: margin against the holiday floor (standby down to it), not 19.5."""
    d = holiday([0] * 4, temps=(17.5, 18.0))
    assert d.room_min_margin_k == pytest.approx(1.5)  # 17.5 - 16.0
    # set-back zone: rooms in the zone against the reduced setpoint - 1
    zone_rooms = rooms(18.5, lower=19.5, in_zone=True) + rooms(21.0)
    z = run([1] * 4, zone_rooms, zone=[1] * 4, reduced_setpoint_c=19.0)
    assert z.room_min_margin_k == pytest.approx(0.5)  # 18.5 - 18.0
    no_zone = run([1] * 4, zone_rooms, zone=[0] * 4, reduced_setpoint_c=19.0)
    assert no_zone.room_min_margin_k == pytest.approx(-1.0)  # 18.5 - 19.5


def test_holiday_without_reduced_setpoint_uses_the_holiday_floor():
    d = run([0] * 4, rooms(16.5, 17.0), holiday=True, away=[1] * 4)
    assert d.sg_target == 1  # floor 16 instead of the room limit 19.5
    assert run([0] * 4, rooms(15.5, 17.0), holiday=True, away=[1] * 4).sg_target == 2


# ----------------------------------------------------------------------
# Settle: a changed decision is accepted only after it stayed the same
# ----------------------------------------------------------------------


def _decision(sg, standby=False, reason=None, standby_reason=""):
    return logic.Decision(
        sg_target=sg,
        reason=reason or f"SG {sg}",
        standby=standby,
        standby_reason=standby_reason,
    )


def test_settle_accepts_the_first_decision_at_once():
    settle = logic.Settle(seconds=10)
    decision, wait = settle.apply(_decision(1), NOW)
    assert decision.sg_target == 1 and wait is None and decision.pending is None


def test_settle_holds_a_change_for_the_settle_time():
    settle = logic.Settle(seconds=10)
    settle.apply(_decision(2), NOW)
    held, wait = settle.apply(_decision(1, reason="REDUCED -> SG 1"), NOW)
    assert held.sg_target == 2 and held.reason == "SG 2"
    assert held.pending == "REDUCED -> SG 1 (in 10 s)"
    assert wait == pytest.approx(10)
    held, wait = settle.apply(_decision(1), NOW + timedelta(seconds=6))
    assert held.sg_target == 2 and wait == pytest.approx(4)
    accepted, wait = settle.apply(_decision(1), NOW + timedelta(seconds=10))
    assert accepted.sg_target == 1 and accepted.pending is None and wait is None


def test_settle_restarts_when_the_change_flips_back_or_changes():
    settle = logic.Settle(seconds=10)
    settle.apply(_decision(2), NOW)
    settle.apply(_decision(1), NOW)
    # back to the accepted value: nothing pending any more
    back, wait = settle.apply(_decision(2), NOW + timedelta(seconds=5))
    assert back.sg_target == 2 and back.pending is None and wait is None
    settle.apply(_decision(1), NOW + timedelta(seconds=8))
    # another change restarts the time
    held, wait = settle.apply(_decision(3), NOW + timedelta(seconds=15))
    assert held.sg_target == 2 and wait == pytest.approx(10)
    accepted, _ = settle.apply(_decision(3), NOW + timedelta(seconds=25))
    assert accepted.sg_target == 3


def test_settle_holds_standby_and_names_its_reason():
    settle = logic.Settle(seconds=10)
    settle.apply(_decision(2, standby=True, standby_reason="standby: EOS plans standby"), NOW)
    held, _ = settle.apply(
        _decision(2, standby=False, standby_reason="no standby: EOS plans NORMAL"), NOW
    )
    assert held.standby is True
    assert held.standby_reason == "standby: EOS plans standby"
    assert held.pending == "no standby: EOS plans NORMAL (in 10 s)"


def test_settle_keeps_current_measurements_while_holding():
    settle = logic.Settle(seconds=10)
    settle.apply(_decision(2), NOW)
    new = _decision(1)
    new.room_mean_c = 20.4
    held, _ = settle.apply(new, NOW)
    assert held.sg_target == 2 and held.room_mean_c == 20.4


def test_settle_zero_accepts_every_change():
    settle = logic.Settle(seconds=0)
    settle.apply(_decision(2), NOW)
    decision, wait = settle.apply(_decision(1), NOW)
    assert decision.sg_target == 1 and wait is None
