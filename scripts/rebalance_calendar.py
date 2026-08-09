"""Shared rebalance-date calendar for the strategy engines.

Single source of truth for "which trading days are rebalance days", so the
weekly cadence rule lives in ONE place instead of being copy-pasted across
run_portfolio.py, run_asset_class_rotation.py and run_thematic_rotation.py
(it was duplicated at five sites before the 2026-07-06 refactor).
"""

from __future__ import annotations

import pandas as pd


SCHEDULED = "scheduled"
LAST_SESSION = "last_session"
MODES = (SCHEDULED, LAST_SESSION)


def weekly_rebalance_dates(
    trading_index: pd.DatetimeIndex,
    eligible_start: pd.Timestamp,
    freq: str = "W-FRI",
    mode: str = SCHEDULED,
) -> pd.DatetimeIndex:
    """Return the rebalance dates for a strategy: the trading days that carry
    the scheduled cadence (calendar Fridays for the default ``W-FRI``), from
    ``eligible_start`` through the last available close.

    Two modes, differing ONLY on weeks whose scheduled day was not a trading
    day on this index's calendar:

    ``scheduled`` (default, deployed)
        INTERSECT the calendar cadence with actual trading days, so a
        market-holiday Friday (Good Friday, or a July-4 / Juneteenth that
        lands on a Friday) drops that whole week's rebalance — the book then
        holds the prior week's positions through the gap. 16 of the 449
        Fridays since 2018-01-05 are such weeks on the NYSE calendar.

    ``last_session``
        Fall back to the last trading session on or before the scheduled day,
        so a shut Friday still gets its decision on the Thursday close. No
        look-ahead is introduced: callers read the signal from the session
        BEFORE the rebalance date, so a Thursday rebalance reads Wednesday's
        breadth.

    Switching the default is a HELD, track-record-affecting change — see the
    rebalance-cadence-deferred note. The mode is threaded as a parameter so
    the two can be measured against each other before that call is made; when
    approved, the default changes HERE, once, and every engine inherits it.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    target = pd.date_range(eligible_start, trading_index[-1], freq=freq)

    if mode == SCHEDULED:
        return trading_index[trading_index.isin(target)]

    # last_session: for each scheduled day, the last session at or before it.
    # searchsorted(side="right") - 1 gives that position; -1 means the whole
    # index post-dates the scheduled day, which is dropped.
    pos = trading_index.searchsorted(target, side="right") - 1
    picked = trading_index[pos[pos >= 0]]
    # A fallback must not reach back before eligibility, and two scheduled
    # weeks must not collapse onto one session (only possible if an entire
    # week was shut).
    picked = picked[picked >= eligible_start]
    return pd.DatetimeIndex(picked).drop_duplicates()
