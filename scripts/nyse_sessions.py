"""NYSE session arithmetic shared by the capture-integrity check and the
deployed-dashboard sentinel.

Uses pandas_market_calendars (pinned in requirements.txt) — the TRUE
exchange calendar, holidays and early closes included. This is
deliberately different from the pipeline hard guard's plain-weekday
counting (see scripts/regime_publish.py): the guard asks "how long since
the panel advanced", where fail-early around holidays is safe; these
checks ask "which session's data SHOULD exist", where holiday awareness
is required for correctness (a Friday-holiday factsheet dated Thursday
is correct, not stale — cadence rule, 2026-07-03).

Python datetime months are 1-indexed throughout (January = 1).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("NYSE")


def last_completed_session(now_utc: datetime) -> date:
    """The most recent NYSE session whose market close is at or before
    ``now_utc``. Early closes (e.g. Christmas Eve) are respected because
    the comparison uses the schedule's own market_close timestamps.

    Args:
        now_utc: timezone-aware UTC datetime (naive input is rejected —
            an implicit local clock here would corrupt every downstream
            freshness verdict).
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware (UTC)")
    now_utc = now_utc.astimezone(timezone.utc)
    # 15 calendar days comfortably spans any run of weekends + holidays.
    sched = _NYSE.schedule(
        start_date=(now_utc - timedelta(days=15)).date().isoformat(),
        end_date=now_utc.date().isoformat(),
    )
    completed = sched[sched["market_close"] <= now_utc]
    if completed.empty:
        raise RuntimeError(
            "no completed NYSE session in the last 15 days — clock or "
            "calendar data is broken"
        )
    return completed.index[-1].date()


def sessions_behind(series_end: date, expected: date) -> int:
    """Number of NYSE sessions strictly after ``series_end`` up to and
    including ``expected``. 0 means the series is current (or ahead,
    e.g. a 24/7-traded component supplying a weekend bar).
    """
    if series_end >= expected:
        return 0
    sched = _NYSE.schedule(
        start_date=series_end.isoformat(), end_date=expected.isoformat()
    )
    return int((sched.index.date > series_end).sum())
