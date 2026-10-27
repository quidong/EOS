"""Month restriction for time windows (eos-plus fork).

Needed for time-variable grid fees under §14a EnWG Modul 3: MVV Netze applies the
time-variable fee only in Q1 and Q4; in Q2/Q3 the standard grid fee applies.
"""

from __future__ import annotations

import numpy as np
import pendulum
import pytest
from pydantic import ValidationError

from akkudoktoreos.config.configabc import (
    TimeWindow,
    TimeWindowSequence,
    ValueTimeWindowSequence,
)
from akkudoktoreos.utils.datetimeutil import to_duration

TZ = "Europe/Berlin"
Q1_Q4 = [1, 2, 3, 10, 11, 12]
Q2_Q3 = [4, 5, 6, 7, 8, 9]


def dt(*args) -> pendulum.DateTime:
    return pendulum.datetime(*args, tz=TZ)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestMonthsValidation:
    def test_default_is_none(self):
        assert TimeWindow(start_time="00:00", duration="1 hour").months is None

    def test_sorted_and_deduplicated(self):
        window = TimeWindow(start_time="00:00", duration="1 hour", months=[12, 1, 1, 10])
        assert window.months == [1, 10, 12]

    @pytest.mark.parametrize("months", [[0], [13], [1, 14], [-1]])
    def test_out_of_range_rejected(self, months):
        with pytest.raises(ValidationError):
            TimeWindow(start_time="00:00", duration="1 hour", months=months)

    def test_empty_list_rejected(self):
        with pytest.raises(ValidationError):
            TimeWindow(start_time="00:00", duration="1 hour", months=[])

    def test_roundtrip_serialisation(self):
        window = TimeWindow(start_time="17:00", duration="3 hours", months=Q1_Q4)
        again = TimeWindow.model_validate(window.model_dump())
        assert again.months == Q1_Q4
        assert again.contains(dt(2026, 1, 15, 18))


# ---------------------------------------------------------------------------
# Behaviour of a single window
# ---------------------------------------------------------------------------


class TestMonthsBehaviour:
    window = TimeWindow(start_time="17:00", duration="3 hours", months=Q1_Q4)

    @pytest.mark.parametrize("month", Q1_Q4)
    def test_contains_in_listed_months(self, month):
        assert self.window.contains(dt(2026, month, 10, 18))

    @pytest.mark.parametrize("month", Q2_Q3)
    def test_not_contained_in_other_months(self, month):
        assert not self.window.contains(dt(2026, month, 10, 18))

    def test_month_boundary_follows_local_time(self):
        # 2026-09-30 22:30 UTC is already 2026-10-01 00:30 in Berlin.
        night = TimeWindow(start_time="00:00", duration="2 hours", months=[10])
        local = pendulum.datetime(2026, 9, 30, 22, 30, tz="UTC").in_timezone(TZ)
        assert night.contains(local)

    def test_combined_with_day_of_week(self):
        window = TimeWindow(start_time="08:00", duration="1 hour", day_of_week=0, months=[11])
        assert window.contains(dt(2026, 11, 2, 8, 30))  # Monday in November
        assert not window.contains(dt(2026, 11, 3, 8, 30))  # Tuesday
        assert not window.contains(dt(2026, 6, 1, 8, 30))  # Monday in June

    def test_start_time_helpers_respect_months(self):
        duration = to_duration("1 hour")
        assert self.window.earliest_start_time(duration, dt(2026, 11, 5)) is not None
        assert self.window.earliest_start_time(duration, dt(2026, 7, 5)) is None
        assert self.window.latest_start_time(duration, dt(2026, 7, 5)) is None
        assert self.window.available_duration(dt(2026, 7, 5)) is None
        assert self.window.available_duration(dt(2026, 12, 5)) == to_duration("3 hours")

    def test_sequence_contains(self):
        seq = TimeWindowSequence(windows=[self.window])
        assert seq.contains(dt(2026, 2, 1, 19))
        assert not seq.contains(dt(2026, 8, 1, 19))


# ---------------------------------------------------------------------------
# MVV Netze Modul 3 as value windows (grid fee per kWh)
# ---------------------------------------------------------------------------

# Netto values from the MVV price sheet 2026 (ct/kWh -> EUR/kWh).
NIEDRIG = 0.0173
STANDARD = 0.0432
HOCH = 0.0596
# Placeholder for the regular (non Modul 3) grid fee in Q2/Q3.
REGULAR = 0.0800


def mvv_modul3_sequence() -> ValueTimeWindowSequence:
    return ValueTimeWindowSequence.model_validate(
        {
            "windows": [
                {"start_time": "00:00", "duration": "2 hours", "months": Q1_Q4, "value": STANDARD},
                {"start_time": "02:00", "duration": "4 hours", "months": Q1_Q4, "value": NIEDRIG},
                {"start_time": "06:00", "duration": "11 hours", "months": Q1_Q4, "value": STANDARD},
                {"start_time": "17:00", "duration": "3 hours", "months": Q1_Q4, "value": HOCH},
                {"start_time": "20:00", "duration": "4 hours", "months": Q1_Q4, "value": STANDARD},
                {"start_time": "00:00", "duration": "24 hours", "months": Q2_Q3, "value": REGULAR},
            ]
        }
    )


class TestMvvModul3:
    def _day(self, year: int, month: int, day: int) -> np.ndarray:
        start = dt(year, month, day)
        return mvv_modul3_sequence().to_array(start, start.add(days=1), to_duration("1 hour"))

    def test_winter_day_profile(self):
        arr = self._day(2026, 1, 14)
        assert len(arr) == 24
        expected = [STANDARD] * 2 + [NIEDRIG] * 4 + [STANDARD] * 11 + [HOCH] * 3 + [STANDARD] * 4
        np.testing.assert_allclose(arr, expected)

    def test_summer_day_is_flat(self):
        arr = self._day(2026, 7, 14)
        np.testing.assert_allclose(arr, [REGULAR] * 24)

    def test_every_hour_of_the_year_has_a_fee(self):
        """No gaps: each hour of 2026 is covered by exactly one of the windows."""
        seq = mvv_modul3_sequence()
        start = dt(2026, 1, 1)
        arr = seq.to_array(start, dt(2027, 1, 1), to_duration("1 hour"))
        assert len(arr) == 8760
        assert np.all(arr > 0)

    def test_quarter_hour_grid(self):
        start = dt(2026, 11, 3)
        arr = mvv_modul3_sequence().to_array(start, start.add(days=1), to_duration("15 minutes"))
        assert len(arr) == 96
        assert arr[8] == pytest.approx(NIEDRIG)  # 02:00
        assert arr[7] == pytest.approx(STANDARD)  # 01:45
        assert arr[68] == pytest.approx(HOCH)  # 17:00
        assert arr[80] == pytest.approx(STANDARD)  # 20:00

    def test_dst_change_day(self):
        # 2026-10-25 has 25 hours in Berlin; the window logic must not drop or double fees.
        start = dt(2026, 10, 25)
        arr = mvv_modul3_sequence().to_array(start, dt(2026, 10, 26), to_duration("1 hour"))
        assert len(arr) == 25
        assert np.all(arr > 0)


# ---------------------------------------------------------------------------
# DST handling of window bounds (fix in TimeWindow._window_start_end)
# ---------------------------------------------------------------------------


class TestDstBounds:
    @pytest.mark.parametrize("day", [(2026, 3, 29), (2026, 10, 25)])
    def test_each_quarter_hour_in_exactly_one_window(self, day):
        seq = mvv_modul3_sequence()
        start = dt(*day)
        end = start.add(days=1)
        current = start
        while current < end:
            hits = [w for w in seq.windows if w.contains(current)]
            assert len(hits) == 1, f"{current}: {len(hits)} windows"
            current = current.add(minutes=15)

    def test_bounds_independent_of_fold_of_reference(self):
        window = TimeWindow(start_time="02:00", duration="4 hours")
        early = pendulum.datetime(2026, 10, 25, 0, 30, tz=TZ)  # CEST
        late = pendulum.datetime(2026, 10, 25, 5, 30, tz=TZ)  # CET
        assert window._window_start_end(early) == window._window_start_end(late)
        start, end = window._window_start_end(late)
        # 02:00 first occurrence (CEST) to 06:00 CET = 5 real hours on this day.
        assert (end - start).in_hours() == 5

    def test_nonexistent_start_moves_past_gap(self):
        # 02:00-03:00 does not exist on 2026-03-29; the window starts when the clocks jump.
        window = TimeWindow(start_time="02:00", duration="4 hours")
        start, end = window._window_start_end(dt(2026, 3, 29, 12))
        assert (start.hour, start.minute) == (3, 0)
        assert (end.hour, end.minute) == (6, 0)
        assert (end - start).in_hours() == 3

    def test_naive_reference_unchanged(self):
        window = TimeWindow(start_time="02:00", duration="4 hours")
        ref = pendulum.naive(2026, 10, 25, 5)
        start, end = window._window_start_end(ref)
        assert start == pendulum.naive(2026, 10, 25, 2)
        assert end == pendulum.naive(2026, 10, 25, 6)

    def test_ordinary_day_unchanged(self):
        window = TimeWindow(start_time="20:00", duration="4 hours")
        start, end = window._window_start_end(dt(2026, 11, 3, 21))
        assert start == dt(2026, 11, 3, 20)
        assert end == dt(2026, 11, 4, 0)
