"""Calendar features for forecast models (RL29 fork).

German public holidays per federal state, computed (no external data or package):
fixed dates, the Easter based ones (Gauss/Meeus Gregorian algorithm) and the state
specific ones. Christmas Eve and New Year's Eve are no public holidays but behave like
them in a household, so they are reported as ``special`` days.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

STATES = (
    "BW",
    "BY",
    "BE",
    "BB",
    "HB",
    "HH",
    "HE",
    "MV",
    "NI",
    "NW",
    "RP",
    "SL",
    "SN",
    "ST",
    "SH",
    "TH",
)


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _repentance_day(year: int) -> date:
    """Buß- und Bettag: the Wednesday before 23 November (Saxony)."""
    day = date(year, 11, 22)
    while day.weekday() != 2:
        day -= timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def public_holidays(year: int, state: str = "BW") -> frozenset[date]:
    """Public holidays of ``year`` in the German federal ``state`` (ISO 3166-2:DE code)."""
    state = state.upper()
    if state not in STATES:
        raise ValueError(f"Unknown federal state '{state}'; one of {', '.join(STATES)}")
    easter = easter_sunday(year)
    days = {
        date(year, 1, 1),
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=1),  # Easter Monday
        date(year, 5, 1),
        easter + timedelta(days=39),  # Ascension
        easter + timedelta(days=50),  # Whit Monday
        date(year, 10, 3),
        date(year, 12, 25),
        date(year, 12, 26),
    }
    if state in ("BW", "BY", "ST"):
        days.add(date(year, 1, 6))
    if state in ("BW", "BY", "HE", "NW", "RP", "SL"):
        days.add(easter + timedelta(days=60))  # Corpus Christi
    if state in ("BW", "BY", "NW", "RP", "SL"):
        days.add(date(year, 11, 1))
    if state == "SL":
        days.add(date(year, 8, 15))
    if state in ("BB", "HB", "HH", "MV", "NI", "SN", "ST", "SH", "TH"):
        days.add(date(year, 10, 31))
    if state in ("BE", "MV") and year >= 2019:
        days.add(date(year, 3, 8))
    if state == "TH" and year >= 2019:
        days.add(date(year, 9, 20))
    if state == "SN":
        days.add(_repentance_day(year))
    if state == "BB":
        days.add(easter)
        days.add(easter + timedelta(days=49))
    return frozenset(days)


def is_public_holiday(day: date, state: str = "BW") -> bool:
    return day in public_holidays(day.year, state)


def is_special_day(day: date) -> bool:
    """Christmas Eve and New Year's Eve."""
    return (day.month, day.day) in ((12, 24), (12, 31))


def day_type(day: date, state: str = "BW") -> int:
    """0 = working day, 1 = Saturday, 2 = Sunday / public holiday / special day."""
    if day.weekday() == 6 or is_public_holiday(day, state) or is_special_day(day):
        return 2
    if day.weekday() == 5:
        return 1
    return 0


def is_bridge_day(day: date, state: str = "BW") -> bool:
    """A working day between a public holiday and the weekend (often taken off)."""
    if day_type(day, state) != 0:
        return False
    before = day - timedelta(days=1)
    after = day + timedelta(days=1)
    return (day.weekday() == 4 and is_public_holiday(before, state)) or (
        day.weekday() == 0 and is_public_holiday(after, state)
    )
