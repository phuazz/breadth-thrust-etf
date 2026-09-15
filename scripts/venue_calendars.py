"""One cached handle per trading venue.

WHY THIS EXISTS — 2026-09-15, found while unblocking the daily publish.

``mcal.get_calendar(name)`` builds a NEW calendar object on every call. The
holiday rules are then recomputed from scratch, because pandas caches resolved
holiday dates on the calendar INSTANCE and a fresh instance has an empty cache.
Measured on this machine, NYSE over a 15-day window:

    fresh instance each call    141.4 ms
    reused instance              15.5 ms

Nine times the cost, for an object that is a definition rather than a state:
nothing in this repo mutates one (no change_time, no add_time, no assignment to
regular_market_times, holidays or adhoc_holidays — checked across scripts/ and
tests/), so one shared handle per venue is the same answer, faster.

This is not a new idea here. ``export_holdings_prices._venue_calendar`` had
exactly this cache, with the note that "building one per ticker costs more than
the whole export", and ``nyse_sessions`` holds a module-level ``_NYSE`` for the
same reason. Both were local fixes to a repo-wide cost. This is that fix, in
one place, for every venue.

WHAT IT COST TO LEAVE UNCACHED. The regression suite ran in ~1m30s on 10
September and past 7m15s by 14 September, which took it over the 8-minute cap
on daily_live_track.yml and blocked the publish for three days. The tests that
grew were the component-factsheet ones, and they were not slow for any reason
of their own: they build a four-sleeve book, that costs ten schedule() calls,
and every one of them rebuilt the holiday rules. Profiled at 99.7% of fixture
time in MarketCalendar.schedule.

CACHE SAFETY. Unbounded by venue count, which is small and fixed (NYSE, XETR
and a handful more). Keyed on the name only, because that is the whole input —
schedule() takes its date range as arguments and caches nothing date-specific
that a later caller could read back. There is no time-varying state here: a
calendar's holiday RULES do not change within a process, and a package upgrade
that changed them would be picked up on the next process start like any other
code change.

Date rules unchanged. This module answers exactly what mcal.get_calendar
answers; it only stops asking twice.
"""

from __future__ import annotations

from functools import lru_cache

import pandas_market_calendars as mcal


@lru_cache(maxsize=None)
def get_calendar(name: str):
    """The shared handle for venue ``name``.

    Drop-in for ``mcal.get_calendar``. Callers must treat the result as
    read-only — it is shared across every caller in the process.
    """
    return mcal.get_calendar(name)
