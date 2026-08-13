"""WS15 adoption — the pipeline must HOLD the residual-fixed panel.

Three properties keep the adopted fills alive and honest across refreshes:

1. Cell-level cache preservation: the download merge fills only the dates
   the vendor left NaN, so a reuse-era column (FB serving a 2025 ETF's bars)
   no longer deletes the recovered roster-era history — the exact wipe that
   undid the first Norgate backfill, one granularity down.
2. Era barriers in per_ticker_apply: indicator windows never span two
   securities sharing a column, and a column with no pre-barrier bars is
   bit-identical to the unbarriered computation.
3. Held-window-aware _unpriced in the backfill: a column whose bars all sit
   outside the held window counts as unpriced, and fills are NaN-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compute_breadth as cb  # noqa: E402
from backfill_delisted_prices import _unpriced  # noqa: E402


def _idx(start, n):
    return pd.bdate_range(start, periods=n)


# ---- 1. cell-level preservation (unit-tests the merge shape) --------------

def test_cell_merge_semantics_fresh_wins_prior_fills():
    idx_old = _idx("2018-01-02", 6)
    idx_new = _idx("2018-01-08", 4)          # overlaps the tail
    prior = pd.DataFrame({"FB": pd.Series([10.0] * 6, index=idx_old)})
    close = pd.DataFrame({"FB": pd.Series([99.0] * 4, index=idx_new)})
    # replicate download_prices' merge block
    close = close.reindex(close.index.union(prior.index))
    pcol = prior["FB"].reindex(close.index)
    mask = close["FB"].isna() & pcol.notna()
    close.loc[mask, "FB"] = pcol[mask]
    got = close["FB"].sort_index()
    # prior-only dates preserved, overlap dates taken from fresh
    assert (got.loc[idx_old[:4]] == 10.0).all()
    assert (got.loc[idx_new] == 99.0).all()


# ---- 2. era barriers ------------------------------------------------------

def test_barrier_noop_when_column_has_no_pre_barrier_bars():
    idx = _idx("2019-03-12", 80)
    prices = pd.DataFrame({"FOXA": pd.Series(np.linspace(35, 40, 80), index=idx)})
    fn = lambda s: s.rolling(5, min_periods=5).mean()
    plain = cb.per_ticker_apply(prices, fn)
    barred = cb.per_ticker_apply(prices, fn, {"FOXA": "2019-03-12"})
    pd.testing.assert_frame_equal(plain, barred)


def test_barrier_isolates_eras_across_a_gap():
    era1 = pd.Series([100.0] * 10, index=_idx("2018-01-02", 10))
    era2 = pd.Series([7.0] * 10, index=_idx("2025-06-26", 10))
    prices = pd.DataFrame({"FB": pd.concat([era1, era2])}).reindex(
        era1.index.union(era2.index))
    fn = lambda s: s.rolling(5, min_periods=5).mean()
    barred = cb.per_ticker_apply(prices, fn, {"FB": "2025-06-26"})
    # Without the barrier, the 5-bar window at the start of era 2 mixes
    # 100.0 bars into a 7.0 era; with it, era 2 warms up on its own bars.
    first_era2_ma = barred["FB"].loc[era2.index[4]]
    assert first_era2_ma == 7.0
    plain = cb.per_ticker_apply(prices, fn)
    assert plain["FB"].loc[era2.index[0]] > 7.0   # the pollution the barrier removes
    # era 1 values identical either way
    pd.testing.assert_series_equal(plain["FB"].loc[era1.index],
                                   barred["FB"].loc[era1.index])


def test_no_barriers_is_bit_identical_to_previous_behaviour():
    rng = np.random.default_rng(7)
    idx = _idx("2020-01-02", 120)
    prices = pd.DataFrame(
        {c: pd.Series(rng.uniform(10, 20, 120), index=idx) for c in "ABC"})
    prices.loc[idx[30:40], "B"] = np.nan
    fn = lambda s: s.rolling(20, min_periods=20).mean()
    pd.testing.assert_frame_equal(cb.per_ticker_apply(prices, fn),
                                  cb.per_ticker_apply(prices, fn, {}))


# ---- 3. held-window-aware unpriced detection ------------------------------

def test_reuse_masked_column_counts_as_unpriced():
    held_dates = [d.strftime("%Y-%m-%d") for d in _idx("2018-01-05", 8)]
    snaps = {d: {"tickers": ["FB", "AAPL"]} for d in held_dates}
    idx = pd.DatetimeIndex(
        list(pd.to_datetime(held_dates)) + list(_idx("2025-06-26", 5)))
    px = pd.DataFrame(index=idx)
    px["FB"] = np.nan
    px.loc[_idx("2025-06-26", 5), "FB"] = 99.0     # reuse era only
    px["AAPL"] = 50.0                              # priced throughout
    got = _unpriced(px, {"FB", "AAPL"}, snaps)
    assert "FB" in got and "AAPL" not in got


def test_all_nan_and_absent_still_count():
    snaps = {"2018-01-05": {"tickers": ["X", "Y", "Z"]}}
    idx = _idx("2018-01-05", 3)
    px = pd.DataFrame({"X": [np.nan] * 3, "Y": [1.0] * 3}, index=idx)
    got = _unpriced(px, {"X", "Y", "Z"}, snaps)
    assert got == {"X", "Z"}
