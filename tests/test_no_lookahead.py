"""No-look-ahead invariance test for the full breadth pipeline.

The idea: build a synthetic price panel, compute breadth components and
their z-scores via the same helper functions used in compute_breadth.py,
record the values for date T_mid, then mutate prices on dates AFTER T_mid,
recompute, and assert that the values for date T_mid did not change.

If the recomputed series differ at T_mid, some look-ahead has crept in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute_breadth import (  # noqa: E402
    compute_rsi,
    expanding_percentile,
    expanding_zscore,
)


def _synthetic_panel(n_days: int = 400, n_tickers: int = 10, seed: int = 7) -> pd.DataFrame:
    """Generate a deterministic synthetic adjusted-close panel."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-02", periods=n_days, freq="B")
    # Random walk with drift, distinct per ticker
    rets = rng.normal(loc=0.0004, scale=0.018, size=(n_days, n_tickers))
    prices = 100.0 * np.exp(np.cumsum(rets, axis=0))
    cols = [f"T{i:02d}" for i in range(n_tickers)]
    return pd.DataFrame(prices, index=idx, columns=cols)


def _breadth_components(prices: pd.DataFrame):
    """Replicate compute_breadth's per-day component computation on a small
    synthetic panel. Returns rsi_breadth, ma_breadth, highs_breadth as Series."""
    rsi = compute_rsi(prices, period=14)
    ma50 = prices.rolling(50, min_periods=50).mean()
    high63 = prices.rolling(63, min_periods=63).max()
    rsi_ob = (rsi > 70.0) & rsi.notna()
    above_ma = (prices > ma50) & ma50.notna()
    at_high = (prices >= high63) & high63.notna()

    # All tickers in the synthetic universe are "active" on every date,
    # so we can collapse to simple per-row means of the boolean mask
    # divided by the count of non-NaN entries.
    def fraction(mask: pd.DataFrame, valid: pd.DataFrame) -> pd.Series:
        v = valid.sum(axis=1).replace(0, np.nan)
        return (mask.sum(axis=1) / v).astype(float)

    rsi_b = fraction(rsi_ob, rsi.notna())
    ma_b = fraction(above_ma, ma50.notna())
    high_b = fraction(at_high, high63.notna())
    return rsi_b, ma_b, high_b


def test_breadth_at_t_invariant_to_future_prices():
    """The full breadth + z-score pipeline at date T must not change when
    prices AFTER T are mutated."""
    prices = _synthetic_panel(n_days=400, n_tickers=10, seed=42)
    t_mid = prices.index[200]

    rsi_b1, ma_b1, high_b1 = _breadth_components(prices)
    z_rsi1 = expanding_zscore(rsi_b1)
    z_ma1 = expanding_zscore(ma_b1)
    z_high1 = expanding_zscore(high_b1)
    comp1 = pd.concat([z_rsi1, z_ma1, z_high1], axis=1).mean(axis=1)
    p90_1 = expanding_percentile(comp1, q=0.90)

    # Snapshot the values at T_mid.
    before = {
        "rsi": rsi_b1.loc[t_mid],
        "ma": ma_b1.loc[t_mid],
        "high": high_b1.loc[t_mid],
        "z_rsi": z_rsi1.loc[t_mid],
        "z_ma": z_ma1.loc[t_mid],
        "z_high": z_high1.loc[t_mid],
        "composite": comp1.loc[t_mid],
        "p90": p90_1.loc[t_mid],
    }

    # Mutate prices strictly AFTER T_mid.
    prices2 = prices.copy()
    prices2.iloc[201:, :] *= np.random.default_rng(99).uniform(0.5, 1.5, size=(199, 10))

    rsi_b2, ma_b2, high_b2 = _breadth_components(prices2)
    z_rsi2 = expanding_zscore(rsi_b2)
    z_ma2 = expanding_zscore(ma_b2)
    z_high2 = expanding_zscore(high_b2)
    comp2 = pd.concat([z_rsi2, z_ma2, z_high2], axis=1).mean(axis=1)
    p90_2 = expanding_percentile(comp2, q=0.90)

    after = {
        "rsi": rsi_b2.loc[t_mid],
        "ma": ma_b2.loc[t_mid],
        "high": high_b2.loc[t_mid],
        "z_rsi": z_rsi2.loc[t_mid],
        "z_ma": z_ma2.loc[t_mid],
        "z_high": z_high2.loc[t_mid],
        "composite": comp2.loc[t_mid],
        "p90": p90_2.loc[t_mid],
    }

    for k, v in before.items():
        if pd.isna(v) and pd.isna(after[k]):
            continue
        assert np.isclose(v, after[k], equal_nan=True), (
            f"No-look-ahead violation: {k} at T_mid changed when future prices "
            f"were mutated (before={v}, after={after[k]})"
        )


def test_rsi_first_value_uses_only_prior_history():
    """RSI at day 14 (the first non-NaN day with period=14) must not depend
    on prices after day 14."""
    prices = _synthetic_panel(n_days=100, n_tickers=3, seed=1)
    rsi1 = compute_rsi(prices, period=14)
    target_day = rsi1.index[14]  # first index where RSI should be finite
    rsi_first_value = rsi1.loc[target_day].copy()

    # Mutate all prices after day 14 wildly.
    prices2 = prices.copy()
    prices2.iloc[15:, :] *= 2.0
    rsi2 = compute_rsi(prices2, period=14)

    pd.testing.assert_series_equal(rsi_first_value, rsi2.loc[target_day])
