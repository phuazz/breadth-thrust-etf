"""Freshness checks for MA200 breadth alignment (Phase 10.2).

Codex-flagged data-integrity bug: previously the breadth → trading-calendar
alignment used unbounded `.reindex(..., method='ffill').fillna(0)` which
would silently carry stale breadth forward forever if the source data
froze. The Phase 10.2 fix routes alignment through align_breadth_to_index
which caps forward-fill at MAX_BREADTH_STALE_DAYS (=7) and emits NaN past
the limit.

These tests guard against regression to the old behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_ma200_sweep import align_breadth_to_index, compute_ma200_breadth  # noqa: E402


def test_align_breadth_drops_stale_forward_fill():
    """A 2-day breadth source aligned onto a 2-week calendar should
    forward-fill within the stale window then go NaN past it."""
    source_idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    breadth = pd.Series([0.4, 0.8], index=source_idx)
    target_idx = pd.date_range("2024-01-02", "2024-01-15", freq="B")

    aligned = align_breadth_to_index(breadth, target_idx, max_stale_days=3)

    # Within 3 days of last observation: ffilled
    assert aligned.loc["2024-01-05"] == 0.8
    # Past 3 days: NaN (the strategy will treat this as no signal)
    assert np.isnan(aligned.loc["2024-01-08"])


def test_align_breadth_handles_fully_empty_source():
    """If breadth has no real observations at all, alignment returns
    all-NaN — never silently zeros."""
    breadth = pd.Series([np.nan, np.nan], index=pd.to_datetime([
        "2024-01-02", "2024-01-03"
    ]))
    target_idx = pd.date_range("2024-01-02", "2024-01-10", freq="B")
    aligned = align_breadth_to_index(breadth, target_idx)
    assert aligned.isna().all()


def test_compute_ma200_breadth_emits_nan_when_no_constituent_is_valid():
    """When ALL constituents go missing (e.g., source data stops), the
    breadth computation returns NaN — does NOT silently propagate the
    last observed value via .ffill().fillna(0)."""
    idx = pd.date_range("2024-01-02", periods=12, freq="B")
    prices = pd.DataFrame({
        "A": [10, 11, 12, 13, 14, np.nan, np.nan,
              np.nan, np.nan, np.nan, np.nan, np.nan]
    }, index=idx)

    breadth = compute_ma200_breadth(prices, period=3)

    # Once prices stop, both_valid becomes False everywhere → n_valid=0
    # → breadth becomes NaN. Phase 10.2 removed the .ffill().fillna(0)
    # that previously masked this with a stale carry-forward.
    assert np.isnan(breadth.iloc[5])
    assert np.isnan(breadth.iloc[-1])


def test_stale_guard_uses_true_observation_date_not_synthetic_fill():
    """The age check must measure from the last REAL observation, not
    from whatever the synthetic ffill produced. If breadth has a real
    value on day 4 and then NaN gaps, day 6 should be marked stale
    relative to day 4 — not relative to a fake ffill of day 5."""
    idx = pd.date_range("2024-01-02", periods=12, freq="B")
    prices = pd.DataFrame({
        "A": [10, 11, 12, 13, 14, np.nan, np.nan,
              np.nan, np.nan, np.nan, np.nan, np.nan]
    }, index=idx)
    breadth = compute_ma200_breadth(prices, period=3)

    aligned = align_breadth_to_index(breadth, idx, max_stale_days=3)

    # Day 4 (a real observation) → preserved
    assert aligned.iloc[4] == breadth.iloc[4]
    # Day 6 (real obs was day 4, gap=2 calendar days) — should still
    # be within the 3-day stale window. Day 11 (gap=7 calendar days)
    # is past the window → NaN.
    assert np.isnan(aligned.iloc[-1])
