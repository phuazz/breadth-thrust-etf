"""Edge-case tests for scripts/nyse_sessions.py.

Month boundary, year boundary, a full-day US holiday and an early-close
day, per CLAUDE.md date rules. Python date months are 1-indexed
(January = 1). Expected values are stated with their reasoning so review
does not rely on mental calendar arithmetic; the library supplies the
holiday facts.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from scripts.nyse_sessions import last_completed_session, sessions_behind


# ---------------------------------------------------------------------------
# last_completed_session
# ---------------------------------------------------------------------------

def test_holiday_friday_returns_thursday():
    # Fri 3 Jul 2026 is the Independence Day observance (4 Jul falls on a
    # Saturday) — no session completes that day, so the answer is
    # Thursday 2 Jul at any time of that Friday.
    assert last_completed_session(
        datetime(2026, 7, 3, 22, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 2)


def test_intraday_vs_after_close_month_boundary():
    # Thu 2 Jul 2026: the NYSE close is 16:00 ET = 20:00 UTC (EDT).
    # Before the close the last completed session is Wed 1 Jul (month
    # boundary crossed); after the close it is 2 Jul itself.
    assert last_completed_session(
        datetime(2026, 7, 2, 19, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 1)
    assert last_completed_session(
        datetime(2026, 7, 2, 21, 30, tzinfo=timezone.utc)
    ) == date(2026, 7, 2)


def test_year_boundary_new_years_day():
    # Thu 1 Jan 2026 is a full holiday: the last completed session is
    # Wed 31 Dec 2025 — the answer crosses the year boundary.
    assert last_completed_session(
        datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    ) == date(2025, 12, 31)


def test_early_close_counts_once_closed():
    # Thu 24 Dec 2026 is a half-day (13:00 ET = 18:00 UTC close, ahead
    # of Christmas Day Friday). By 19:00 UTC the session has completed —
    # a naive fixed-20:00-UTC assumption would get this wrong.
    assert last_completed_session(
        datetime(2026, 12, 24, 19, 0, tzinfo=timezone.utc)
    ) == date(2026, 12, 24)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        last_completed_session(datetime(2026, 7, 2, 21, 30))


# ---------------------------------------------------------------------------
# sessions_behind
# ---------------------------------------------------------------------------

def test_sessions_behind_zero_when_current_or_ahead():
    assert sessions_behind(date(2026, 7, 2), date(2026, 7, 2)) == 0
    # A 24/7-traded component can supply a bar dated ahead of the NYSE
    # session — that is "current", not an error.
    assert sessions_behind(date(2026, 7, 4), date(2026, 7, 2)) == 0


def test_sessions_behind_across_holiday_weekend():
    # Fri 26 Jun -> Mon 6 Jul 2026 is 5 true NYSE sessions (Jun 29, 30,
    # Jul 1, 2, 6 — Fri 3 Jul is a holiday). Contrast with the pipeline
    # guard's plain-weekday count of 6 for the same span: these two
    # arithmetics differ by design (see nyse_sessions module docstring).
    assert sessions_behind(date(2026, 6, 26), date(2026, 7, 6)) == 5


def test_sessions_behind_one_and_two():
    assert sessions_behind(date(2026, 7, 1), date(2026, 7, 2)) == 1
    assert sessions_behind(date(2026, 6, 30), date(2026, 7, 2)) == 2


def test_sessions_behind_year_boundary():
    # Wed 31 Dec 2025 -> Fri 2 Jan 2026: only 2 Jan is a session in
    # between (1 Jan is a holiday) = 1 session behind.
    assert sessions_behind(date(2025, 12, 31), date(2026, 1, 2)) == 1
