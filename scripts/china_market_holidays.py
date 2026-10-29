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
2025-11-04), put that year's WEEKDAY exchange closures in PINNED_CLOSURES and
the review record in PINNED_SOURCES (see _check_source for its fields).
Weekend days are omitted: the exchanges never trade weekends, including the
make-up working Saturdays and Sundays the notice lists.

WHAT THE CODE CAN AND CANNOT ESTABLISH (2026-10-29 review). Two layers are
enforced. At import, validate_year and _check_source refuse a year whose
dates are malformed (outside the year, weekends, duplicates, a statutory
fixed-date holiday missing, more weekday closures than any year the library
lists) or whose source record is incomplete (no title, issuer, issue date, a
URL on gov.cn / sse.com.cn / szse.cn, reviewer, review date). At use,
checked_pins refuses every pin for a venue once any pinned year disagrees
with the library's own list for that year: adding one traded day to an
otherwise correct year is caught there, and the native check fails closed.
None of this establishes that the dates ARE the notice's. For a year the
library does not yet list -- the only year a pin changes anything -- the
dates rest on the named reviewer having read them against the official
notice; the record says who and when, so the review can be audited, not
assumed.

Python datetime months are 1-indexed (January = 1). Weekdays are read with
datetime, never computed by hand.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit

import pandas as pd

# Venues this pin applies to. Shenzhen shares Shanghai's date calendar.
PINNED_VENUES = frozenset({"XSHG", "SSE"})

# year -> weekday exchange closures, from the official notice only. 2027 is
# deliberately absent: as of 2026-10-29 the State Council's 2027 arrangement
# had not been published.
PINNED_CLOSURES: dict[int, tuple[str, ...]] = {}

# year -> the review record for those dates: {"title", "issuer",
# "issued_on" (YYYY-MM-DD), "url", "reviewed_by", "reviewed_on" (YYYY-MM-DD)}.
# A form check only (_check_source); see the module docstring.
PINNED_SOURCES: dict[int, dict] = {}

# Where an official notice can be published: the State Council's site and
# the two exchanges'. Hosts only; a URL elsewhere is refused.
_OFFICIAL_HOSTS = ("gov.cn", "sse.com.cn", "szse.cn")

# More weekday closures than this in one year is not a notice. The library's
# XSHG list (1991-2026) peaks at 24, in 2002, and stays within 15-20 since
# 2015 (measured 2026-10-29 on pandas_market_calendars 5.3.2 and 5.4.0), so a
# longer list is refused.
MAX_WEEKDAY_CLOSURES = 24

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
    if len(days) > MAX_WEEKDAY_CLOSURES:
        raise HolidayPinError(f"{year}: {len(days)} weekday closures exceeds "
                              f"{MAX_WEEKDAY_CLOSURES}; not a single notice")
    for month, day in _STATUTORY_FIXED:
        d = date(year, month, day)
        if d.weekday() < 5 and d not in days:
            raise HolidayPinError(f"{year}: statutory holiday {d} (a weekday) "
                                  f"is missing; is this the whole notice?")
    return sorted(days)


def _iso(value) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _check_source(year: int, src) -> None:
    """Refuse a review record that is not complete in form. Passing proves
    the record names a notice and a reviewer; it does not prove the dates
    are the notice's (module docstring)."""
    if not src:
        raise HolidayPinError(f"{year}: pinned without a source")
    if not isinstance(src, dict):
        raise HolidayPinError(f"{year}: the source must be a review record "
                              f"(title, issuer, issued_on, url, reviewed_by, "
                              f"reviewed_on), not free text")
    for field in ("title", "issuer", "reviewed_by"):
        if not isinstance(src.get(field), str) or not src[field].strip():
            raise HolidayPinError(f"{year}: source field {field!r} is missing")
    issued, reviewed = _iso(src.get("issued_on")), _iso(src.get("reviewed_on"))
    if issued is None or reviewed is None:
        raise HolidayPinError(f"{year}: issued_on and reviewed_on must be "
                              f"YYYY-MM-DD dates")
    if not (date(year - 1, 1, 1) <= issued <= date(year, 12, 31)):
        raise HolidayPinError(f"{year}: a notice issued on {issued} is not "
                              f"that year's notice")
    if reviewed < issued:
        raise HolidayPinError(f"{year}: reviewed on {reviewed}, before the "
                              f"notice was issued on {issued}")
    parts = urlsplit(src.get("url") or "")
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not any(
            host == h or host.endswith("." + h) for h in _OFFICIAL_HOSTS):
        raise HolidayPinError(f"{year}: source URL {src.get('url')!r} is not on "
                              f"{', '.join(_OFFICIAL_HOSTS)}")


def _validated() -> dict[int, list[date]]:
    out = {}
    for year, closures in PINNED_CLOSURES.items():
        _check_source(year, PINNED_SOURCES.get(year))
        out[year] = validate_year(year, closures)
    return out


# Refuse at import: a malformed pin must stop the run, not excuse a bar.
_PINNED = _validated()


def pinned_closures(venue: str | None) -> pd.DatetimeIndex:
    """Every pinned closure for ``venue`` (empty for other venues). Not
    checked against the library: callers that remove closures from a
    schedule use checked_pins."""
    if venue not in PINNED_VENUES:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(sorted(pd.Timestamp(d) for ds in _PINNED.values()
                                   for d in ds))


def checked_pins(cal, venue: str | None) -> pd.DatetimeIndex:
    """pinned_closures, or HolidayPinError if any pinned year disagrees with
    the library's own list for that year (library_conflicts).

    A pin can only EXCUSE a missing bar, so a disagreement is resolved by a
    person against the notice, never by preferring either list: every pin
    for the venue is refused until then, and native_sessions fails closed.
    """
    conflicts = library_conflicts(cal, venue)
    if conflicts:
        raise HolidayPinError(
            "pinned closures disagree with the calendar library "
            + "; ".join(f"{y}: pinned only {c['pinned_only']}, library only "
                        f"{c['library_only']}" for y, c in sorted(conflicts.items()))
            + " -- resolve against the official notice")
    return pinned_closures(venue)


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
    resolve against the notice; checked_pins refuses the pins until then,
    and nothing is merged silently."""
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
