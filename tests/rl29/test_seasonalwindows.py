"""Seasonal time windows with the upstream `month` restriction (RL29 fork).

Needed for time-variable grid fees under §14a EnWG Modul 3: MVV Netze applies the
time-variable fee only in Q1 and Q4; in Q2/Q3 the standard grid fee applies. Upstream
TimeWindow restricts a window to one month, so a seasonal band needs one window per month.
"""

from __future__ import annotations

import numpy as np
import pendulum
import pytest

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


def per_month(window: dict, months: list[int]) -> list[dict]:
    """One copy of ``window`` per month."""
    return [{**window, "month": month} for month in months]


# ---------------------------------------------------------------------------
# Behaviour of a seasonal window
# ---------------------------------------------------------------------------


class TestSeasonalWindow:
    sequence = TimeWindowSequence.model_validate(
        {"windows": per_month({"start_time": "17:00", "duration": "3 hours"}, Q1_Q4)}
    )

    @pytest.mark.parametrize("month", Q1_Q4)
    def test_contains_in_listed_months(self, month):
        assert self.sequence.contains(dt(2026, month, 10, 18))

    @pytest.mark.parametrize("month", Q2_Q3)
    def test_not_contained_in_other_months(self, month):
        assert not self.sequence.contains(dt(2026, month, 10, 18))

    def test_month_boundary_follows_local_time(self):
        # 2026-09-30 22:30 UTC is already 2026-10-01 00:30 in Berlin.
        night = TimeWindow(start_time="00:00", duration="2 hours", month=10)
        local = pendulum.datetime(2026, 9, 30, 22, 30, tz="UTC").in_timezone(TZ)
        assert night.contains(local)

    def test_combined_with_day_of_week(self):
        window = TimeWindow(start_time="08:00", duration="1 hour", day_of_week=0, month=11)
        assert window.contains(dt(2026, 11, 2, 8, 30))  # Monday in November
        assert not window.contains(dt(2026, 11, 3, 8, 30))  # Tuesday
        assert not window.contains(dt(2026, 6, 1, 8, 30))  # Monday in June

    def test_matches_day_uses_month(self):
        window = TimeWindow(start_time="08:00", duration="1 hour", month=11)
        assert window.matches_day(dt(2026, 11, 2))
        assert not window.matches_day(dt(2026, 12, 2))


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
    """Consecutive windows with explicit end times.

    Upstream EOS counts ``duration`` in elapsed time, so windows chained by
    duration overlap or leave gaps on DST change days. Explicit ``end_time``
    bounds follow the wall clock and keep the chain gap-free.
    """
    return ValueTimeWindowSequence.model_validate(
        {
            "windows": [
                *per_month({"start_time": "00:00", "end_time": "02:00", "value": STANDARD}, Q1_Q4),
                *per_month({"start_time": "02:00", "end_time": "06:00", "value": NIEDRIG}, Q1_Q4),
                *per_month({"start_time": "06:00", "end_time": "17:00", "value": STANDARD}, Q1_Q4),
                *per_month({"start_time": "17:00", "end_time": "20:00", "value": HOCH}, Q1_Q4),
                *per_month({"start_time": "20:00", "end_time": "00:00", "value": STANDARD}, Q1_Q4),
                *per_month({"start_time": "00:00", "end_time": "00:00", "value": REGULAR}, Q2_Q3),
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
# DST handling of window bounds (explicit end times, upstream semantics)
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

    def test_end_time_bounds_on_autumn_change_day(self):
        # 2026-10-25: 02:00-03:00 occurs twice; ambiguous bounds use the second
        # occurrence (upstream rule), so 00:00-02:00 has 3 real hours and
        # 02:00-06:00 keeps 4.
        night = TimeWindow(start_time="00:00", end_time="02:00")
        low = TimeWindow(start_time="02:00", end_time="06:00")
        _, night_end = night._window_start_end(dt(2026, 10, 25))
        low_start, low_end = low._window_start_end(dt(2026, 10, 25))
        assert night_end == low_start
        assert (low_end - low_start).in_hours() == 4

    def test_end_time_bounds_on_spring_change_day(self):
        # 2026-03-29: 02:00-03:00 does not exist; the chain stays closed.
        night = TimeWindow(start_time="00:00", end_time="02:00")
        low = TimeWindow(start_time="02:00", end_time="06:00")
        _, night_end = night._window_start_end(dt(2026, 3, 29))
        low_start, _ = low._window_start_end(dt(2026, 3, 29))
        assert night_end == low_start

    def test_duration_counts_elapsed_time(self):
        # Upstream semantics: duration is elapsed time, also across DST changes.
        window = TimeWindow(start_time="02:00", duration="4 hours")
        start, end = window._window_start_end(dt(2026, 10, 25))
        assert (end - start).in_hours() == 4

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


# ---------------------------------------------------------------------------
# Compatibility with configurations of earlier fork releases (`months`)
# ---------------------------------------------------------------------------


class TestLegacyMonths:
    def test_months_expand_to_one_window_per_month(self):
        seq = ValueTimeWindowSequence.model_validate(
            {"windows": [{"start_time": "17:00", "end_time": "20:00", "months": [11, 1], "value": 1.0}]}
        )
        assert [w.month for w in seq.windows] == [1, 11]
        assert all(w.value == 1.0 for w in seq.windows)
        assert seq.contains(dt(2026, 11, 3, 18))
        assert not seq.contains(dt(2026, 6, 3, 18))

    def test_legacy_mvv_config_keeps_seasonal_fees(self):
        legacy = ValueTimeWindowSequence.model_validate(
            {
                "windows": [
                    {"start_time": "17:00", "end_time": "20:00", "months": Q1_Q4, "value": HOCH},
                    {"start_time": "00:00", "end_time": "00:00", "months": Q2_Q3, "value": REGULAR},
                ]
            }
        )
        start = dt(2026, 7, 14)
        summer = legacy.to_array(start, start.add(days=1), to_duration("1 hour"))
        np.testing.assert_allclose(summer, [REGULAR] * 24)

    def test_months_combined_with_month_keeps_intersection(self):
        seq = TimeWindowSequence.model_validate(
            {"windows": [{"start_time": "08:00", "duration": "1 hour", "month": 2, "months": [1, 2]}]}
        )
        assert [w.month for w in seq.windows] == [2]

    @pytest.mark.parametrize("months", [[], [0], [13]])
    def test_invalid_legacy_months_rejected(self, months):
        with pytest.raises(ValueError):
            TimeWindowSequence.model_validate(
                {"windows": [{"start_time": "08:00", "duration": "1 hour", "months": months}]}
            )

    def test_windows_without_months_unchanged(self):
        seq = TimeWindowSequence.model_validate(
            {"windows": [{"start_time": "08:00", "duration": "1 hour", "month": 3}]}
        )
        assert [w.month for w in seq.windows] == [3]
