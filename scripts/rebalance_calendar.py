"""Shared rebalance-date calendar for the strategy engines.

Single source of truth for "which trading days are rebalance days", so the
weekly cadence rule lives in ONE place instead of being copy-pasted across
run_portfolio.py, run_asset_class_rotation.py and run_thematic_rotation.py
(it was duplicated at five sites before the 2026-07-06 refactor).
"""

from __future__ import annotations

from functools import lru_cache

import pandas as pd


SCHEDULED = "scheduled"
LAST_SESSION = "last_session"
HOLIDAY_AWARE = "holiday_aware"
MODES = (SCHEDULED, LAST_SESSION, HOLIDAY_AWARE)


@lru_cache(maxsize=32)
def _exchange_sessions(calendar: str, start: str, end: str) -> frozenset:
    """Dates the named exchange actually traded in [start, end].

    Cached because the engines call this once per K x cadence cell and the
    schedule build is the expensive part.
    """
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar(calendar).schedule(start_date=start,
                                                  end_date=end)
    return frozenset(d.date() for d in sched.index)


def scheduled_data_gaps(
    trading_index: pd.DatetimeIndex,
    eligible_start: pd.Timestamp,
    freq: str = "W-FRI",
    calendar: str = "NYSE",
) -> list[pd.Timestamp]:
    """Scheduled rebalance days the exchange TRADED but this price index
    lacks — vendor gaps masquerading as holidays.

    This is the distinction that makes ``holiday_aware`` safe. A bar that is
    missing because the market was shut is a fact about the world; a bar that
    is missing because the vendor dropped it is a fact about our data, and
    silently rebalancing a day early on the second is how a data defect turns
    into a trade. Confirmed live case: Fri 2025-10-24 is absent from the
    Europe sector panel although XETR traded it.
    """
    if len(trading_index) == 0:
        return []
    target = pd.date_range(eligible_start, trading_index[-1], freq=freq)
    if len(target) == 0:
        return []
    sessions = _exchange_sessions(
        calendar, pd.Timestamp(target[0]).date().isoformat(),
        pd.Timestamp(target[-1]).date().isoformat())
    have = set(trading_index)
    return [t for t in target if t not in have and t.date() in sessions]


def weekly_rebalance_dates(
    trading_index: pd.DatetimeIndex,
    eligible_start: pd.Timestamp,
    freq: str = "W-FRI",
    mode: str = SCHEDULED,
    calendar: str | None = None,
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
        breadth. UNSAFE on its own: it cannot tell a shut market from a
        missing bar, so a vendor gap becomes a silent early rebalance.
        Retained for the WS10 A/B only.

    ``holiday_aware`` (requires ``calendar``, e.g. "NYSE" / "XETR")
        As ``last_session``, but the fallback fires ONLY when the exchange
        genuinely did not trade that day. If the exchange traded and the bar
        is merely absent from our data, the week is skipped exactly as under
        ``scheduled`` — the conservative, visible outcome — and the date is
        reported by ``scheduled_data_gaps`` so it can be alarmed rather than
        traded through.

    Switching the default is a track-record-affecting change; the mode is
    threaded as a parameter so variants can be measured before that call is
    made. When approved, the default changes HERE, once, and every engine
    inherits it.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == HOLIDAY_AWARE and not calendar:
        raise ValueError("mode='holiday_aware' requires calendar=, e.g. 'NYSE'")
    if mode != HOLIDAY_AWARE and calendar:
        raise ValueError(f"calendar= is only meaningful for {HOLIDAY_AWARE!r}")

    target = pd.date_range(eligible_start, trading_index[-1], freq=freq)

    if mode == SCHEDULED:
        return trading_index[trading_index.isin(target)]

    if mode == HOLIDAY_AWARE:
        if len(target) == 0:
            return trading_index[:0]
        sessions = _exchange_sessions(
            calendar, pd.Timestamp(target[0]).date().isoformat(),
            pd.Timestamp(target[-1]).date().isoformat())
        have = set(trading_index)
        picked = []
        for t in target:
            if t in have:
                picked.append(t)                      # normal week
            elif t.date() in sessions:
                continue                              # data gap -> skip
            else:                                     # true holiday -> back up
                pos = trading_index.searchsorted(t, side="right") - 1
                if pos >= 0 and trading_index[pos] >= eligible_start:
                    picked.append(trading_index[pos])
        return pd.DatetimeIndex(picked).drop_duplicates()

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


# ---------------------------------------------------------------------------
# The engine entry point.
# ---------------------------------------------------------------------------
# THE FLIP POINT. Every deployed engine routes through engine_rebalance_dates,
# so adopting the holiday fallback across A/B/C/D and the blend is this one
# constant. It is track-record-affecting: WS10 measured the deployed
# 35/35/10/20 blend at Sharpe +1.1861 under `scheduled` and +1.1738 under
# `holiday_aware` (delta -0.0123, CAGR -0.17pp), so flipping it restates
# published history downward. Do not change it without the filed record.
DEFAULT_MODE = SCHEDULED


def engine_rebalance_dates(
    trading_index: pd.DatetimeIndex,
    eligible_start: pd.Timestamp,
    freq: str = "W-FRI",
    calendar: str = "NYSE",
) -> pd.DatetimeIndex:
    """Rebalance dates for a deployed engine, under the active DEFAULT_MODE.

    ``calendar`` is the venue the sleeve TRADES on - "NYSE" for the US-listed
    sleeves (A/B/C) and "XETR" for Europe (D). It is forwarded only when the
    active mode actually consumes it, so a mode that ignores the calendar can
    never be mistaken for one that honours it.
    """
    return weekly_rebalance_dates(
        trading_index, eligible_start, freq,
        mode=DEFAULT_MODE,
        calendar=calendar if DEFAULT_MODE == HOLIDAY_AWARE else None,
    )
