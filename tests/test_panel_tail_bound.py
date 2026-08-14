"""The panel's tail bound: last COMPLETED session, per venue.

Python months are 1-indexed (January = 1), unlike JavaScript's 0-indexed Date.
Every literal below is 1-indexed.

What this protects. compute_breadth used to end its daily loop at the last
published roster Friday, so a Friday-morning run produced a panel ending the
PREVIOUS Friday while the decision that morning reads Thursday's close. The
bound now follows the venue calendar instead. The risk it introduces is the
opposite one — running past the close of a session that has not finished, and
computing breadth on a partial bar the vendor will revise. These tests exist
for that risk.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute_breadth import last_completed_session_on  # noqa: E402

NYSE = mcal.get_calendar("NYSE")
XETR = mcal.get_calendar("XETR")


def _utc(y, m, d, hh=0, mm=0):
    """1-indexed month, as Python uses."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_before_the_close_returns_the_previous_session():
    """The live case. Fri 14 Aug 2026, 12:05 UTC — NYSE opens at 13:30.

    This is the exact moment the refresh asked the question. Answering "today"
    would have put an unfinished session into a panel feeding that evening's
    fill.
    """
    got = last_completed_session_on(NYSE, _utc(2026, 8, 14, 12, 5))
    assert got == pd.Timestamp("2026-08-13")


def test_after_the_close_includes_that_session():
    got = last_completed_session_on(NYSE, _utc(2026, 8, 13, 21, 0))
    assert got == pd.Timestamp("2026-08-13")


def test_the_minute_before_the_close_still_excludes_it():
    """NYSE closes 20:00 UTC. 19:59 must not count the session as complete."""
    assert last_completed_session_on(NYSE, _utc(2026, 8, 13, 19, 59)) \
        == pd.Timestamp("2026-08-12")
    assert last_completed_session_on(NYSE, _utc(2026, 8, 13, 20, 1)) \
        == pd.Timestamp("2026-08-13")


def test_weekend_falls_back_to_friday():
    assert last_completed_session_on(NYSE, _utc(2026, 8, 16, 6, 0)) \
        == pd.Timestamp("2026-08-14")


def test_month_boundary():
    """1 June 2026 is a Monday; the last completed session is Fri 29 May."""
    got = last_completed_session_on(NYSE, _utc(2026, 6, 1, 6, 0))
    assert got == pd.Timestamp("2026-05-29")
    assert got.month == 5, "must not roll forward into the new month"


def test_year_boundary():
    """1 January is a holiday on both venues; roll back into the prior year."""
    got = last_completed_session_on(NYSE, _utc(2026, 1, 1, 12, 0))
    assert got == pd.Timestamp("2025-12-31")
    assert got.year == 2025


def test_venues_disagree_and_that_is_the_point():
    """US Independence Day, Fri 3 Jul 2026 — NYSE shut, Xetra open.

    A single NYSE-derived cap would truncate the European funds by a session.
    """
    when = _utc(2026, 7, 3, 20, 0)          # after Xetra's close
    assert last_completed_session_on(XETR, when) == pd.Timestamp("2026-07-03")
    assert last_completed_session_on(NYSE, when) == pd.Timestamp("2026-07-02")


def test_naive_datetimes_are_treated_as_utc_not_rejected():
    aware = last_completed_session_on(NYSE, _utc(2026, 8, 14, 12, 5))
    naive = last_completed_session_on(NYSE, datetime(2026, 8, 14, 12, 5))
    assert aware == naive


def test_returns_none_when_the_horizon_holds_no_session():
    """Caller treats None as "keep the previous bound", so it must not raise."""
    assert last_completed_session_on(NYSE, _utc(2026, 8, 14, 12, 5),
                                     horizon_days=0) is None


def test_result_is_tz_naive_and_normalised():
    """It is compared against tz-naive roster Timestamps; a tz would raise."""
    got = last_completed_session_on(XETR, _utc(2026, 8, 14, 12, 5))
    assert got.tz is None
    assert (got.hour, got.minute, got.second) == (0, 0, 0)
    # and it must be comparable with the roster's end_friday without error
    assert max(pd.Timestamp("2026-08-07"), got) == got


@pytest.mark.parametrize("cal", [NYSE, XETR])
def test_never_returns_a_future_session(cal):
    now = _utc(2026, 8, 14, 12, 5)
    got = last_completed_session_on(cal, now)
    assert got <= pd.Timestamp("2026-08-14")
