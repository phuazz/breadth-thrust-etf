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

from scripts.nyse_sessions import (
    cap_to_last_completed_session,
    last_completed_session,
    sessions_behind,
    yf_fetch_end,
)


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


# ---------------------------------------------------------------------------
# yf_fetch_end / cap_to_last_completed_session (2026-07-18 fencepost fix)
# ---------------------------------------------------------------------------

def test_yf_fetch_end_pads_past_today():
    # Fri 17 Jul 2026 22:10 UTC — the minute-class of the weekly CI run
    # that shipped the factsheet without the Friday rebalance. yfinance's
    # `end` is exclusive, so end=today would EXCLUDE 17 Jul; the padded
    # window ends 19 Jul, putting 17 Jul strictly inside it.
    assert yf_fetch_end(
        datetime(2026, 7, 17, 22, 10, tzinfo=timezone.utc)
    ) == "2026-07-19"


def test_yf_fetch_end_month_and_year_boundary():
    # Month boundary: Fri 31 Jul 2026 pads to 2 Aug. Year boundary:
    # Thu 31 Dec 2026 pads to 2 Jan 2027.
    assert yf_fetch_end(
        datetime(2026, 7, 31, 22, 0, tzinfo=timezone.utc)
    ) == "2026-08-02"
    assert yf_fetch_end(
        datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc)
    ) == "2027-01-02"


def test_yf_fetch_end_naive_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        yf_fetch_end(datetime(2026, 7, 17, 22, 10))


def test_cap_drops_rows_after_last_completed_session():
    import pandas as pd

    df = pd.DataFrame(
        {"SPY": [1.0, 2.0, 3.0]},
        index=pd.to_datetime(["2026-07-16", "2026-07-17", "2026-07-18"]),
    )
    # After the Friday 20:00 UTC close: the synthetic Saturday row goes
    # (e.g. a 24/7-traded component's weekend print), Friday stays.
    after_close = datetime(2026, 7, 17, 22, 10, tzinfo=timezone.utc)
    assert list(cap_to_last_completed_session(df, after_close).index) == [
        pd.Timestamp("2026-07-16"), pd.Timestamp("2026-07-17")]
    # Mid-session Friday (before the close): Friday's partial bar must
    # go too — a weekly engine must never stamp a rebalance on it.
    mid_session = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)
    assert list(cap_to_last_completed_session(df, mid_session).index) == [
        pd.Timestamp("2026-07-16")]


def test_sessions_between_excludes_juneteenth_and_weekend():
    """2026-06-19 (Friday) is Juneteenth — an NYSE holiday with Xetra
    open, exactly the phantom-bar case the live-track filter exists for."""
    from datetime import date

    from scripts.nyse_sessions import sessions_between

    got = sessions_between(date(2026, 6, 15), date(2026, 6, 22))
    assert date(2026, 6, 18) in got
    assert date(2026, 6, 19) not in got     # Juneteenth
    assert date(2026, 6, 20) not in got     # Saturday
    assert date(2026, 6, 22) in got         # Monday


def test_sessions_between_year_boundary_excludes_new_years_day():
    from datetime import date

    from scripts.nyse_sessions import sessions_between

    got = sessions_between(date(2025, 12, 30), date(2026, 1, 2))
    assert date(2025, 12, 31) in got        # NYSE trades 31 Dec
    assert date(2026, 1, 1) not in got      # New Year's Day
    assert date(2026, 1, 2) in got


def test_sessions_between_inverted_range_is_empty():
    from datetime import date

    from scripts.nyse_sessions import sessions_between

    assert sessions_between(date(2026, 7, 10), date(2026, 7, 1)) == set()
