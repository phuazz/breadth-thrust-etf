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


def yf_fetch_end(now_utc: datetime | None = None) -> str:
    """ISO ``end`` argument for a yfinance download that must include the
    latest completed close. yfinance's ``end`` is EXCLUSIVE — rows come
    back strictly before it, so ``end=today`` silently drops today's
    completed session. That is how the Friday 2026-07-17 22:00 UTC weekly
    run captured Strategy B/C only through Thursday and the factsheet
    shipped without the Friday rebalance. Pad two calendar days; pair
    with cap_to_last_completed_session so a mid-session run cannot
    ingest a partial bar instead.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware (UTC)")
    return ((now_utc.astimezone(timezone.utc) + timedelta(days=2)).date()
            .isoformat())


def cap_to_last_completed_session(frame, now_utc: datetime | None = None):
    """Drop rows dated after the last completed NYSE session from a
    DatetimeIndex-ed pandas object (DataFrame or Series).

    The partial-bar guard that makes the padded ``yf_fetch_end`` window
    safe: a run during US market hours would otherwise ingest today's
    in-progress bar as if it were a close, and a weekly engine could
    stamp a rebalance on it.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    cutoff = last_completed_session(now_utc)
    if len(frame) == 0:
        return frame
    return frame[frame.index.date <= cutoff]
