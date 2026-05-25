"""First-principles tests for the end-to-end strategy mechanics.

Phase 10.2 (Codex). These tests use tiny synthetic panels so the expected
values can be reasoned from definitions, not from cached research outputs.
They are the regression guards that would have caught Phase 4's stale-
breadth bug and Phase 10's structural inconsistency between sleeves.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute_breadth import active_roster_at, normalise_for_yfinance  # noqa: E402
from run_ma200_sweep import compute_ma200_breadth, run_strategy  # noqa: E402
from run_portfolio import run_portfolio, top_k_eq_weight  # noqa: E402


def test_active_roster_uses_latest_snapshot_not_future_snapshot():
    """The point-in-time roster lookup must never reach FORWARD into a
    snapshot that postdates the query date (look-ahead leak)."""
    snapshot_dates = ["2024-01-05", "2024-01-12"]
    snapshot_map = {
        "2024-01-05": {"tickers": ["A", "B"]},
        "2024-01-12": {"tickers": ["C", "D"]},
    }

    # Before any snapshot → empty (no leak from 2024-01-05)
    assert active_roster_at(snapshot_dates, snapshot_map, "2024-01-04") == []
    # Between snapshots → uses the older one
    assert active_roster_at(snapshot_dates, snapshot_map, "2024-01-10") == ["A", "B"]
    # On the snapshot date → that day's roster
    assert active_roster_at(snapshot_dates, snapshot_map, "2024-01-12") == ["C", "D"]


def test_yfinance_normalisation_distinguishes_share_classes_from_exchanges():
    """The dot in 'BRK.B' is a share class (→ BRK-B for yfinance), but the
    dot in '7203.T' is an exchange suffix (→ keep as-is). The normaliser
    must treat them differently or we get silent ticker corruption."""
    assert normalise_for_yfinance("BRK.B") == "BRK-B"
    assert normalise_for_yfinance("7203.T") == "7203.T"
    assert normalise_for_yfinance("HSBA.L") == "HSBA.L"


def test_ma_breadth_excludes_invalid_constituents_from_denominator():
    """A constituent with no price data should not contribute to either
    numerator OR denominator — otherwise the breadth percentage is biased."""
    idx = pd.date_range("2024-01-02", periods=5, freq="B")
    prices = pd.DataFrame({
        "UP": [10.0, 11.0, 12.0, 13.0, 14.0],
        "DOWN": [14.0, 13.0, 12.0, 11.0, 10.0],
        "MISSING": [np.nan, np.nan, np.nan, np.nan, np.nan],
    }, index=idx)

    breadth = compute_ma200_breadth(prices, period=3)

    # Day 0: not enough data for any MA → NaN
    assert np.isnan(breadth.iloc[0])
    # MA windows use 90% min_periods (Phase 4 fix), so period=3 becomes
    # valid after 2 bars. With UP above MA and DOWN below: 50% breadth.
    # MISSING is excluded from both numerator and denominator.
    assert breadth.iloc[1] == 0.5
    assert breadth.iloc[-1] == 0.5


def test_ma_strategy_uses_yesterdays_breadth_for_todays_allocation():
    """No look-ahead: today's allocation must be set using YESTERDAY's
    breadth signal. The win/lose on today's return uses today's allocation."""
    idx = pd.date_range("2024-01-02", periods=4, freq="B")
    close = pd.Series([100.0, 100.0, 110.0, 121.0], index=idx)
    # Breadth signal fires on day 1 (idx=1). The strategy should only
    # take that signal as input to day 2's allocation, and the day 2->3
    # return (+10%) is what gets earned.
    breadth = pd.Series([0.0, 1.0, 0.0, 0.0], index=idx)

    out = run_strategy(
        close, breadth, long_threshold=50, family="long_flat",
        base_alloc=0.0, cost=0.0,
    )

    # The high breadth on day 1 → allocation on day 2 = 1.0, captures
    # day 2->3 return. Then day 2 breadth=0 → day 3 allocation = 0.
    assert out["alloc"].tolist() == [0.0, 0.0, 1.0, 0.0]
    assert out["equity"].iloc[-1] == 1.1


def test_portfolio_rebalance_uses_prior_breadth_and_next_day_returns():
    """For the cross-sectional top-K rotation: the rebalance at end-of-day
    T uses breadth observed at T-1 (or earlier), and the position earns
    the T → T+1 return. End-to-end no-look-ahead invariant."""
    idx = pd.date_range("2024-01-02", periods=5, freq="B")
    closes = pd.DataFrame({
        "A": [100.0, 100.0, 100.0, 110.0, 110.0],
        "B": [100.0, 100.0, 100.0, 100.0, 100.0],
    }, index=idx)
    breadths = pd.DataFrame({
        "A": [0.1, 0.9, 0.1, 0.1, 0.1],
        "B": [0.8, 0.2, 0.2, 0.2, 0.2],
    }, index=idx)

    out = run_portfolio(
        closes, breadths, top_k_eq_weight(1),
        eligible_start=idx[0], cost=0.0, rebalance_freq="D",
    )

    # Day 1 weights use day 0 breadth (B wins 0.8 > 0.1). Day 2 weights
    # use day 1 breadth (A wins 0.9 > 0.2). A's day 3 rally (+10%) is
    # captured because A was the position from day 2 onwards.
    assert out["weights"].loc[idx[1]].to_dict() == {"A": 0.0, "B": 1.0}
    assert out["weights"].loc[idx[2]].to_dict() == {"A": 1.0, "B": 0.0}
    assert out["equity"].iloc[-1] == 1.1
