"""WS5 (2026-07-10) — constituent relative-trend breadth engine.

Origin: the SentimenTrader relative-trend-score concept. Sleeve A already
rotates on ABSOLUTE per-constituent trend breadth (share of a sector's members
whose close is above their own 200d MA), demeaned cross-sectionally across the
14 universe ETFs. This module adds the missing leg: a per-constituent RELATIVE
trend, where the trend is measured on the stock's price RATIO to the benchmark
(SPY), not on its raw price. That distinguishes "names are up because the whole
market is up" from "names are out-trending the market".

Three per-name conditions are supported, each producing a per-ETF breadth
series that feeds the EXISTING Phase 20 demeaning + Phase 20.1 top-K positive-
only weighting path unchanged (that downstream path lives in
run_strategy_a_universe_gate.py / run_portfolio.py — this module deliberately
stops at the breadth series so the challenger arms differ ONLY in the per-name
condition):

  - "absolute"  A0  close > SMA200(close)                    [deployed leg]
  - "relative"  A1  ratio > SMA200(ratio),  ratio = close/SPY [new leg]
  - "dual"      A2  A0 AND A1                                  [SentimenTrader
                                                               highlighted col]

Denominator discipline (WS5 pre-registration failure mode 3): all three arms
use ONE shared validity mask — a constituent contributes to the numerator OR
the denominator of ANY arm on a given day only if BOTH legs are computable that
day (price and its MA valid AND ratio and its MA valid). This guarantees the
three arms share an identical denominator every day, so any difference between
them is purely the per-name condition, never a difference in which names were
eligible.

Consequence used as a correctness anchor: because ratio = close / SPY and SPY
is present on every US trading day the constituents trade, the ratio series
carries the SAME NaN mask as the close series, so the relative leg's validity
mask equals the absolute leg's. The shared mask therefore equals the deployed
absolute leg's own mask, and "absolute" breadth from this module reproduces
run_ma200_sweep.compute_ma200_breadth() to the float on a complete-SPY panel.
test_relative_trend.py asserts this equivalence.

No look-ahead: every quantity here is a trailing rolling window on prices/ratios
up to and including each date; the CALLER applies the deployed t-1 shift at
rebalance (signal.iloc[loc(rebal_date) - 1]). This module returns
same-date breadth; it does not shift. test_relative_trend.py verifies that
breadth at date T is invariant to any mutation of prices/SPY strictly after T.

Dates: pandas DatetimeIndex throughout; no manual date arithmetic. (Python
`datetime` months are 1-indexed, but no month indexing is done here.)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Deployed convention (run_ma200_sweep.compute_ma200_breadth): the MA needs at
# least 90% of the window populated. For period 200 that is 180 — tolerant of
# the 1-2% sparse missingness in non-US prints without letting a half-empty
# window define a trend.
MA_PERIOD = 200
MIN_PERIODS_FRACTION = 0.9

TREND_MODES = ("absolute", "relative", "dual")


def _min_periods(period: int) -> int:
    return max(1, int(period * MIN_PERIODS_FRACTION))


def _align_benchmark(prices: pd.DataFrame, benchmark: pd.Series) -> pd.Series:
    """Reindex the benchmark onto the constituent price calendar WITHOUT
    filling. A genuine calendar mismatch (benchmark missing on a day a name
    trades) then surfaces as NaN in the ratio and is dropped by the shared
    validity mask — never silently forward-filled, which would inject a stale
    benchmark level into the ratio. For an all-US-listed universe on SPY's
    calendar this reindex is a no-op."""
    if not isinstance(benchmark, pd.Series):
        raise TypeError("benchmark must be a pandas Series of benchmark closes")
    bench = benchmark.reindex(prices.index)
    # Guard against a zero/negative benchmark print corrupting the ratio.
    bench = bench.where(bench > 0)
    return bench


def _legs(prices: pd.DataFrame, benchmark: pd.Series, period: int):
    """Return (shared_mask, above_abs, above_rel) boolean frames aligned to
    `prices`.

    shared_mask[i, t] is True iff constituent t is computable on BOTH legs on
    day i (price & abs-MA valid AND ratio & rel-MA valid).
    above_abs[i, t]   is close > SMA(close).
    above_rel[i, t]   is ratio > SMA(ratio), ratio = close / benchmark.
    """
    min_p = _min_periods(period)
    bench = _align_benchmark(prices, benchmark)
    ratio = prices.div(bench, axis=0)

    sma_abs = prices.rolling(period, min_periods=min_p).mean()
    sma_rel = ratio.rolling(period, min_periods=min_p).mean()

    abs_ok = prices.notna() & sma_abs.notna()
    rel_ok = ratio.notna() & sma_rel.notna()
    shared = abs_ok & rel_ok

    above_abs = prices > sma_abs
    above_rel = ratio > sma_rel
    return shared, above_abs, above_rel


def compute_trend_breadth_all(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    period: int = MA_PERIOD,
) -> pd.DataFrame:
    """All three per-name trend-breadth arms for one ETF's constituent panel.

    Parameters
    ----------
    prices : DataFrame  (index = trading dates, columns = constituent tickers)
        Adjusted closes, same source/adjustment basis as `benchmark`.
    benchmark : Series  (index = trading dates)
        Benchmark adjusted close (SPY) for the relative leg's ratio.
    period : int
        MA window (default 200), 90%-populated minimum.

    Returns
    -------
    DataFrame with columns ("absolute", "relative", "dual"), indexed by date.
    Each value is the share of shared-valid constituents meeting that arm's
    condition. Days with zero shared-valid constituents are NaN (the caller
    routes these through the deployed freshness-aware alignment, exactly as
    for the deployed absolute breadth).
    """
    if period <= 1:
        raise ValueError("period must be > 1")
    shared, above_abs, above_rel = _legs(prices, benchmark, period)

    denom = shared.sum(axis=1)
    denom = denom.where(denom > 0, np.nan)

    n_abs = (above_abs & shared).sum(axis=1)
    n_rel = (above_rel & shared).sum(axis=1)
    n_dual = (above_abs & above_rel & shared).sum(axis=1)

    out = pd.DataFrame(
        {
            "absolute": n_abs / denom,
            "relative": n_rel / denom,
            "dual": n_dual / denom,
        },
        index=prices.index,
    )
    return out


def compute_trend_breadth(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    mode: str = "absolute",
    period: int = MA_PERIOD,
) -> pd.Series:
    """Single-arm convenience wrapper. `mode` in TREND_MODES."""
    if mode not in TREND_MODES:
        raise ValueError(f"mode must be one of {TREND_MODES}, got {mode!r}")
    return compute_trend_breadth_all(prices, benchmark, period=period)[mode]


def shared_valid_count(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    period: int = MA_PERIOD,
) -> pd.Series:
    """Per-day count of constituents valid on BOTH legs — the common
    denominator of all three arms. Exposed for the diagnostic panel and for
    per-ETF coverage reporting in the WS5 record."""
    shared, _, _ = _legs(prices, benchmark, period)
    return shared.sum(axis=1)
