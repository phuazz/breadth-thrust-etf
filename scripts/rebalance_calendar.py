"""Shared rebalance-date calendar for the strategy engines.

Single source of truth for "which trading days are rebalance days", so the
weekly cadence rule lives in ONE place instead of being copy-pasted across
run_portfolio.py, run_asset_class_rotation.py and run_thematic_rotation.py
(it was duplicated at five sites before the 2026-07-06 refactor).
"""

from __future__ import annotations

import pandas as pd


def weekly_rebalance_dates(
    trading_index: pd.DatetimeIndex,
    eligible_start: pd.Timestamp,
    freq: str = "W-FRI",
) -> pd.DatetimeIndex:
    """Return the rebalance dates for a strategy: the trading days that fall
    on the scheduled cadence (calendar Fridays for the default ``W-FRI``),
    from ``eligible_start`` through the last available close.

    This INTERSECTS the calendar cadence with actual trading days, so a
    market-holiday Friday (e.g. Good Friday, or a July-4 / Juneteenth that
    lands on a Friday) drops that whole week's rebalance — the deployed book
    then holds the prior week's positions through the gap.

    That behaviour is preserved EXACTLY by this extraction; it is a pure
    dedup, not a behaviour change. Switching to "rebalance on each week's
    last completed session" (so a closed-Friday week still gets its decision
    on the Thursday close) is a HELD, track-record-affecting change — see the
    rebalance-cadence-deferred note. When approved, it is made HERE, once,
    and every engine inherits it.
    """
    target = pd.date_range(eligible_start, trading_index[-1], freq=freq)
    return trading_index[trading_index.isin(target)]
