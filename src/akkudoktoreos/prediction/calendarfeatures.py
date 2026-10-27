"""Calendar features for forecast models (eos-plus fork).

Public holidays come from the ``holidays`` package for any supported country and
subdivision. A *holiday region* is written as an ISO 3166 code: ``"DE"`` (national
holidays only), ``"DE-BW"`` (country and subdivision), ``"AT-9"``, ``"CH-ZH"``,
``"NL"``. ``None`` or an empty string means no public holidays (weekends only).

Christmas Eve and New Year's Eve are no public holidays in most countries but behave like
them in a household, so they are reported as ``special`` days.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Optional

import holidays as _holidays

# Codes of the German federal states; the former ``state`` settings held one of them.
GERMAN_STATES = (
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


def split_region(region: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``"DE-BW"`` -> ``("DE", "BW")``, ``"NL"`` -> ``("NL", None)``, empty -> ``(None, None)``."""
    if region is None:
        return None, None
    text = str(region).strip()
    if not text or text.lower() == "none":
        return None, None
    country, _, subdivision = text.partition("-")
    return country.upper(), (subdivision.upper() or None)


def validate_region(region: Optional[str]) -> Optional[str]:
    """Normalised holiday region, or ``ValueError`` for an unknown country or subdivision."""
    country, subdivision = split_region(region)
    if country is None:
        return None
    supported = _holidays.list_supported_countries()
    if country not in supported:
        raise ValueError(
            f"Unknown holiday country '{country}' (ISO 3166-1 alpha-2, "
            "supported by the 'holidays' package)"
        )
    if subdivision is not None:
        subdivisions = {str(code).upper() for code in supported[country]}
        if subdivision not in subdivisions:
            raise ValueError(
                f"Unknown subdivision '{subdivision}' of {country}; one of "
                f"{', '.join(sorted(subdivisions))}"
            )
        return f"{country}-{subdivision}"
    return country


def resolve_region(region: Optional[str], legacy_state: Optional[str] = None) -> Optional[str]:
    """Holiday region from the current setting, else from a former German ``state`` setting."""
    if region:
        return validate_region(region)
    if legacy_state:
        return validate_region(f"DE-{legacy_state}")
    return None


@lru_cache(maxsize=128)
def public_holidays(year: int, region: Optional[str] = None) -> frozenset[date]:
    """Public holidays of ``year`` in the holiday ``region`` (empty set without region)."""
    country, subdivision = split_region(validate_region(region))
    if country is None:
        return frozenset()
    calendar = _holidays.country_holidays(country, subdiv=subdivision, years=year)
    return frozenset(day for day in calendar if day.year == year)


def is_public_holiday(day: date, region: Optional[str] = None) -> bool:
    """True when ``day`` is a public holiday in ``region``."""
    return day in public_holidays(day.year, region)


def is_special_day(day: date) -> bool:
    """Christmas Eve and New Year's Eve."""
    return (day.month, day.day) in ((12, 24), (12, 31))


def day_type(day: date, region: Optional[str] = None) -> int:
    """0 = working day, 1 = Saturday, 2 = Sunday / public holiday / special day."""
    if day.weekday() == 6 or is_public_holiday(day, region) or is_special_day(day):
        return 2
    if day.weekday() == 5:
        return 1
    return 0


def is_bridge_day(day: date, region: Optional[str] = None) -> bool:
    """A working day between a public holiday and the weekend (often taken off)."""
    if day_type(day, region) != 0:
        return False
    before = day - timedelta(days=1)
    after = day + timedelta(days=1)
    return (day.weekday() == 4 and is_public_holiday(before, region)) or (
        day.weekday() == 0 and is_public_holiday(after, region)
    )
