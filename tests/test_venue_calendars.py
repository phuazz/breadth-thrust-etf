"""The shared calendar cache, and the guard that keeps it shared.

2026-09-15. ``mcal.get_calendar`` builds a new calendar object per call and the
holiday rules are then resolved from scratch, because pandas caches them on the
instance. Measured at 141ms against 15ms for a reused handle. Nothing here
mutates a calendar, so the two answers are the same answer.

That cost blocked the daily publish for three days: the regression suite went
from ~1m30s to past the 8-minute cap on daily_live_track.yml, the job was
cancelled before it could commit, and a cancel is not a failure so the alert
stayed silent. The suite was not slow for any reason of its own — the
component-factsheet fixtures build a four-sleeve book, that is ten schedule()
calls, and every one rebuilt the holiday rules. 99.7% of fixture time.

Two of these tests prove the cache is sound. The third is the one that matters
in a year: it fails the moment a new call site reintroduces the uncached form.

Python datetime months are 1-indexed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas_market_calendars as mcal
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from venue_calendars import get_calendar  # noqa: E402

# venue_calendars is the one place allowed to call the uncached constructor.
EXEMPT = {"venue_calendars.py"}


def test_one_handle_per_venue_and_venues_stay_distinct():
    assert get_calendar("NYSE") is get_calendar("NYSE")
    assert get_calendar("XETR") is get_calendar("XETR")
    assert get_calendar("NYSE") is not get_calendar("XETR")


@pytest.mark.parametrize("venue,start,end", [
    # An ordinary fortnight, then the two boundaries the house rules require:
    # a month boundary (31 Aug -> 1 Sep) and a year boundary carrying New
    # Year's Day, which is a holiday on both calendars and so is exactly the
    # kind of date a wrong cache would smear.
    ("NYSE", "2026-08-28", "2026-09-12"),
    ("XETR", "2026-08-28", "2026-09-12"),
    ("NYSE", "2026-08-25", "2026-09-04"),
    ("XETR", "2026-08-25", "2026-09-04"),
    ("NYSE", "2026-12-24", "2027-01-08"),
    ("XETR", "2026-12-24", "2027-01-08"),
])
def test_cached_calendar_answers_exactly_what_the_uncached_one_answers(venue, start, end):
    """Equality of the schedule itself, not merely of its length.

    The sessions AND their market_open/market_close timestamps, because a
    calendar that returned the right days at the wrong closes would pass a
    count check and still redate every decision that reads a close.
    """
    fresh = mcal.get_calendar(venue).schedule(start_date=start, end_date=end)
    cached = get_calendar(venue).schedule(start_date=start, end_date=end)
    assert cached.equals(fresh)
    assert list(cached.index) == list(fresh.index)


def test_no_module_builds_its_own_calendar():
    """The guard. Without it the cost returns one call site at a time.

    It reads as a style rule and is not one: every direct call rebuilds the
    holiday set, and the last time that went unnoticed it cost three days of
    publishing. If a new call site genuinely needs an unshared instance, add
    it to EXEMPT with the reason — do not delete the test.
    """
    pattern = re.compile(r"\bmcal\.get_calendar\(|pandas_market_calendars\.get_calendar\(")
    offenders = []
    for path in sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "tests").glob("*.py")):
        if path.name in EXEMPT or path.name == Path(__file__).name:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, (
        "Build calendars through venue_calendars.get_calendar, not directly:\n  "
        + "\n  ".join(offenders))
