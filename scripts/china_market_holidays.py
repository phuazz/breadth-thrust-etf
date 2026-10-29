"""Mainland exchange closures the calendar library does not yet know.

WHY THIS EXISTS — 2026-10-29.

native_sessions decides which Shenzhen close each NYSE row of sleeve C must
carry from the XSHG calendar in pandas_market_calendars. XSHG's holidays are
an ad-hoc list maintained from each year's announcement; in 5.4.0 it ends at
2026-10-07, and for 2027 it lists no closures at all, so every 2027 weekday
is a "session" (measured 2026-10-15, handoffs/2026-10-15_sse-calendar-2027-
coverage.md). On Lunar New Year 2027 the NYSE rows would then require Shenzhen
closes that were never printed, and sleeve C would HOLD until the calendar
learns the holiday. That error is on the safe side -- a calendar with too few
holidays can only demand MORE sessions, never excuse a missing one -- but it
is a false HOLD, and its cause would read as a vendor withdrawal.

So the closures are pinned here, per year, from the official notice, and
native_sessions removes them from the XSHG schedule. A year that is not
pinned is left to the library, and any required session past the holiday
data (``holiday_data_through``) is labelled as possibly an unannounced
holiday rather than as a withdrawn bar.

NEVER PIN A FORECAST. A pinned closure EXCUSES a missing bar: pin a day the
exchange actually traded and a withdrawn Shenzhen close is read as a
legitimate holiday fill -- a false READY. Only the State Council's annual
holiday notice, or the SSE/SZSE trading-calendar notice that follows it, is a
source. Third-party "2027 holiday" tables, and pandas_market_calendars' SSE
projection (its own docstring calls the rules guesses), are not.

HOW TO PIN A YEAR. When the notice is published (the 2026 notice appeared on
2025-11-04), put that year's WEEKDAY exchange closures in PINNED_CLOSURES with
the notice in PINNED_SOURCES. Weekend days are omitted: the exchanges never
trade weekends, including the make-up working Saturdays and Sundays the
notice lists. validate_year runs at import and refuses a malformed year.

Python datetime months are 1-indexed (January = 1). Weekdays are read with
datetime, never computed by hand.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

# Venues this pin applies to. Shenzhen shares Shanghai's date calendar.
PINNED_VENUES = frozenset({"XSHG", "SSE"})

# year -> weekday exchange closures, from the official notice only. 2027 is
# deliberately absent: as of 2026-10-29 the State Council's 2027 arrangement
# had not been published.
PINNED_CLOSURES: dict[int, tuple[str, ...]] = {}

# year -> where those dates came from (title, publisher, date, URL).
PINNED_SOURCES: dict[int, str] = {}

# Fixed Gregorian days that are statutory holidays under the State Council's
# national holiday regulations as revised for 2025 on: 1 January; 1-2 May;
# 1-3 October. When one falls on a weekday the exchanges are closed. The
# lunar-calendar holidays (Spring Festival, Qingming, Dragon Boat,
# Mid-Autumn) move each year and are not checked here.
_STATUTORY_FIXED = ((1, 1), (5, 1), (5, 2), (10, 1), (10, 2), (10, 3))


class HolidayPinError(ValueError):
    """A pinned year is malformed and must not be used."""


def validate_year(year: int, closures) -> list[date]:
    """The pinned closures for ``year`` as sorted dates, or HolidayPinError.

    Refuses: a date outside the year, a weekend date (never a session, so
    pinning it means the list was copied from the wrong column), a duplicate,
    and a list missing any statutory fixed-date holiday that falls on a
    weekday -- the cheapest proof the list is the year's whole notice and not
    a fragment of it.
    """
    days = []
    for raw in closures:
        d = pd.Timestamp(raw).date()
        if d.year != year:
            raise HolidayPinError(f"{year}: {d} is not in {year}")
        if d.weekday() >= 5:            # datetime: Monday = 0 ... Sunday = 6
            raise HolidayPinError(f"{year}: {d} is a weekend day; pin weekday "
                                  f"closures only")
        days.append(d)
    if len(set(days)) != len(days):
        raise HolidayPinError(f"{year}: duplicate dates")
    for month, day in _STATUTORY_FIXED:
        d = date(year, month, day)
        if d.weekday() < 5 and d not in days:
            raise HolidayPinError(f"{year}: statutory holiday {d} (a weekday) "
                                  f"is missing; is this the whole notice?")
    return sorted(days)


def _validated() -> dict[int, list[date]]:
    out = {}
    for year, closures in PINNED_CLOSURES.items():
        if not PINNED_SOURCES.get(year):
            raise HolidayPinError(f"{year}: pinned without a source")
        out[year] = validate_year(year, closures)
    return out


# Refuse at import: a malformed pin must stop the run, not excuse a bar.
_PINNED = _validated()


def pinned_closures(venue: str | None) -> pd.DatetimeIndex:
    """Every pinned closure for ``venue`` (empty for other venues)."""
    if venue not in PINNED_VENUES:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(sorted(pd.Timestamp(d) for ds in _PINNED.values()
                                   for d in ds))


def holiday_data_through(cal, venue: str | None) -> pd.Timestamp | None:
    """Last date the venue's holiday data is known to cover, or None when the
    venue's calendar is rule-based (NYSE, XETR) and has no such bound.

    31 December of the later of the library's last listed ad-hoc holiday and
    the last pinned year. Raises nothing: an unreadable list gives None for a
    venue that is not pinned, which callers treat as "no bound known".
    """
    if venue not in PINNED_VENUES:
        return None
    years = list(_PINNED)
    try:
        listed = getattr(cal, "adhoc_holidays", None)
        if listed is not None and len(listed):
            years.append(pd.Timestamp(max(listed)).year)
    except (TypeError, ValueError):
        pass
    if not years:
        return None
    return pd.Timestamp(year=max(years), month=12, day=31)


def library_conflicts(cal, venue: str | None) -> dict[int, dict[str, list[str]]]:
    """Where a pinned year disagrees with the library's own list for that
    year, if the library has since learnt it. Empty when they agree or the
    library has nothing for the year. A disagreement is for a person to
    resolve against the notice; it is reported, never merged silently."""
    if venue not in PINNED_VENUES:
        return {}
    # Not ``... or []``: a DatetimeIndex has no truth value (the probe's
    # latent crash, handoffs/2026-10-15_sse-calendar-2027-coverage.md §4).
    raw = getattr(cal, "adhoc_holidays", None)
    listed = pd.DatetimeIndex([] if raw is None else list(raw))
    out = {}
    for year, pinned in _PINNED.items():
        lib = {pd.Timestamp(d).date() for d in listed if pd.Timestamp(d).year == year}
        lib = {d for d in lib if d.weekday() < 5}
        if not lib:
            continue
        only_pin = sorted(set(pinned) - lib)
        only_lib = sorted(lib - set(pinned))
        if only_pin or only_lib:
            out[year] = {"pinned_only": [str(d) for d in only_pin],
                         "library_only": [str(d) for d in only_lib]}
    return out
