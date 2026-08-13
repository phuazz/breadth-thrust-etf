"""WS15 regression — the Monte Carlo non-overlap sampler must actually fit.

The Phase 10.2 sampler (2026-05-25) placed each random entry uniformly over
all remaining feasible positions while reserving room for later trades at
only the MINIMUM holding. On the CNDX OOS re-run (13 trades, 596 holding
sessions, ~1,750-session window) all 1,000 paths came back partial, every
one was discarded, and the null distribution was empty — every MC field
None. These tests pin the WS15 replacement: a gap-transform placement that
cannot dead-end a feasible configuration.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtest import _sample_non_overlapping_random_trades, monte_carlo_null  # noqa: E402

# The exact shape that produced 0 valid paths in 1,000: the CNDX
# regime_time_only_delay5_trend leg on the May survivor breadth.
CNDX_HOLDINGS = np.array([1, 9, 19, 23, 25, 54, 55, 56, 61, 61, 67, 69, 96])


def _check_path(path, holdings_pool, e0, e1, end_idx, n_trades):
    assert len(path) == n_trades
    last_exit = None
    for entry, exit_ in path:
        assert e0 <= entry <= e1, "entry outside the eligible window"
        assert exit_ <= end_idx, "exit beyond the data"
        assert (exit_ - entry) in holdings_pool, "holding not from the pool"
        if last_exit is not None:
            assert entry > last_exit, "overlapping trades"
        last_exit = exit_


def test_cndx_shape_always_fits():
    rng = np.random.default_rng(20260516)
    elig = np.arange(292, 292 + 1756)
    end_idx = 2144
    for _ in range(200):
        path = _sample_non_overlapping_random_trades(
            rng, CNDX_HOLDINGS, elig, end_idx, len(CNDX_HOLDINGS))
        _check_path(path, set(CNDX_HOLDINGS.tolist()), elig[0], elig[-1],
                    end_idx, len(CNDX_HOLDINGS))


def test_single_trade_and_tight_window():
    rng = np.random.default_rng(7)
    # Window exactly big enough for one 10-session trade.
    elig = np.arange(100, 101)
    path = _sample_non_overlapping_random_trades(
        rng, np.array([10]), elig, 110, 1)
    assert path == [(100, 110)]


def test_infeasible_returns_empty():
    rng = np.random.default_rng(7)
    # 5 trades of 50 sessions cannot fit in a 60-session window.
    elig = np.arange(0, 60)
    path = _sample_non_overlapping_random_trades(
        rng, np.array([50]), elig, 59, 5)
    assert path == []


def test_gapped_window_is_refused():
    rng = np.random.default_rng(7)
    with pytest.raises(ValueError):
        _sample_non_overlapping_random_trades(
            rng, np.array([5]), np.array([1, 2, 3, 10, 11]), 20, 1)


def test_monte_carlo_null_produces_valid_paths():
    # End-to-end: a synthetic upward-drifting OHLC frame and a handful of
    # strategy-like trades must yield an essentially complete null.
    import backtest as bt

    idx = pd.bdate_range("2019-01-02", periods=1800)
    close = pd.Series(np.linspace(100.0, 300.0, len(idx)), index=idx)
    ohlc = pd.DataFrame({"Open": close.values, "High": close.values,
                         "Low": close.values, "Close": close.values},
                        index=idx)
    trades = []
    pos = 10
    for h in CNDX_HOLDINGS.tolist():
        t = bt.Trade(
            signal_date=str(idx[pos].date()),
            entry_date=str(idx[pos].date()),
            entry_open=float(close.iloc[pos]),
            entry_price=float(close.iloc[pos]),
            exit_date=str(idx[pos + h].date()),
            exit_close=float(close.iloc[pos + h]),
            exit_price=float(close.iloc[pos + h]),
            exit_reason="time_stop",
            holding_days=h,
            trade_return=float(close.iloc[pos + h] / close.iloc[pos] - 1.0),
            max_drawdown=0.0,
        )
        trades.append(t)
        pos += h + 5
    mc = monte_carlo_null(trades, ohlc, idx[0], n_paths=200)
    assert mc["n_valid_paths"] >= 199, mc
    assert mc["strategy_total_return_percentile"] is not None
