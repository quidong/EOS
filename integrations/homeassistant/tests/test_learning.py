"""Comfort learning from manual interventions (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "custom_components" / "eos_control" / "learning.py"
spec = importlib.util.spec_from_file_location("eos_control_learning", MODULE)
assert spec and spec.loader
learning = importlib.util.module_from_spec(spec)
sys.modules["eos_control_learning"] = learning
spec.loader.exec_module(learning)

TZ = timezone(timedelta(hours=1))


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, 10, tzinfo=TZ)


def event(day, hour, temp, room="Office", kind=learning.KIND_SETPOINT, lower=19.5):
    return learning.ComfortEvent(at(day, hour), room, kind, temp, lower)


def test_periods_of_the_day():
    assert [learning.period_of(at(1, h)) for h in (23, 3, 7, 13, 19)] == [
        "night",
        "night",
        "morning",
        "afternoon",
        "evening",
    ]


def test_suggestion_after_enough_events_in_one_period():
    model = learning.ComfortLearning()
    for day, temp in ((1, 19.6), (2, 19.9), (3, 19.7)):
        model.record(event(day, 8, temp))
    model.record(event(4, 20, 19.0))  # other period, alone
    (suggestion,) = model.suggestions({"Office": 19.5})
    assert suggestion.period == "morning" and suggestion.events == 3
    assert suggestion.suggested_c == 20.0  # median 19.7 + 0.3, 0.5 K steps


def test_no_suggestion_below_the_current_limit_or_for_unknown_rooms():
    model = learning.ComfortLearning()
    for day in (1, 2, 3):
        model.record(event(day, 8, 18.5))
    assert model.suggestions({"Office": 19.5}) == []
    assert model.suggestions({"Kitchen": 19.5}) == []


def test_suggestion_is_capped():
    model = learning.ComfortLearning()
    for day in (1, 2, 3):
        model.record(event(day, 8, 23.0))
    (suggestion,) = model.suggestions({"Office": 19.5})
    assert suggestion.suggested_c == 21.5  # lower + max_raise_k 2 K


def test_old_events_are_dropped_and_storage_round_trips():
    model = learning.ComfortLearning(keep_days=30)
    model.record(event(1, 8, 20.0))
    model.record(learning.ComfortEvent(at(1, 8) + timedelta(days=40), "Office", "lock", 19.8, 19.5))
    assert len(model.events) == 1
    copy = learning.ComfortLearning.from_list(model.as_list() + [{"bad": 1}])
    assert [e.as_dict() for e in copy.events] == model.as_list()


def test_detection_helpers():
    assert learning.setpoint_raised(21.0, 21.5)
    assert not learning.setpoint_raised(21.0, 21.2)
    assert not learning.setpoint_raised(None, 22.0)
    now = at(5, 12)
    assert learning.lock_set(None, now + timedelta(hours=2), now)
    assert not learning.lock_set(now + timedelta(hours=2), now + timedelta(hours=2), now)
    assert learning.lock_set(now + timedelta(hours=1), now + timedelta(hours=3), now)
    assert not learning.lock_set(None, now - timedelta(hours=1), now)
    room = learning.lock_room([("A", 20.5, 19.5), ("B", 19.8, 19.5), ("C", None, 19.5)])
    assert room == ("B", 19.8, 19.5)
    assert learning.lock_room([("C", None, 19.5)]) is None


@pytest.mark.parametrize("hour", [22, 5])
def test_night_spans_midnight(hour):
    assert learning.period_of(at(1, hour)) == "night"
