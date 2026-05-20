"""Robustness checks for the MA200 + 50/150 strategy.

Implements the five most important tests from the quant PM review:

  1. WALK-FORWARD L SELECTION
     Per ETF, refit L on expanding train window, apply to next 12 months.
     Concatenate the test segments to get a Sharpe with no in-sample
     optimisation bias. Compare to in-sample Sharpe.

  2. BORROW-COST ADJUSTMENT
     The 50/150 leveraged variant assumes free margin. Recompute Sharpe
     after subtracting a realistic 5%/yr broker call rate on the 50%
     extra notional during long-state. Quantifies the practical haircut.

  3. SUB-PERIOD DECOMPOSITION
     Slice the 2019-2026 window into regime sub-periods (pre-COVID, COVID,
     2021 rally, 2022 inflation shock, 2023 AI rally, 2024-26 recent)
     and compute Sharpe per sub-period for the headline strategies.
     Identifies whether performance is regime-driven.

  4. BOOTSTRAP SHARPE CONFIDENCE INTERVAL
     Block-bootstrap (60-day blocks) of the daily strategy return series
     to derive a Sharpe distribution. Reports mean, 5th, and 95th
     percentiles for each headline strategy.

  5. MA-PERIOD ROBUSTNESS
     Re-run the strategy with MA periods {100, 150, 200, 250, 300} on CSP1
     to check whether MA200 specifically is the "right" lookback or just
     a lucky single point.

Output: data/robustness.json

Run:
    python scripts/run_robustness.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import download_soxx_ohlc  # noqa: E402
from etf_registry import get_etf  # noqa: E402
from run_improvements import compute_stats  # noqa: E402
from run_ma200_sweep import (  # noqa: E402
    compute_ma200_breadth, load_constituent_prices, COST_BPS, LONG_THRESHOLDS,
)

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "robustness.json"

ETFS = ["SOXX", "CSP1", "CNDX", "IUES", "IUFS", "IUIT", "IUHC", "IUIS", "IUCS", "IUCD", "IUUS"]

# Borrow cost on extra notional above 100%
BORROW_RATE_ANNUAL = 0.05

# Sub-period boundaries (inclusive start, exclusive end)
SUB_PERIODS = [
    ("2019_pre_covid",       "2019-01-01", "2020-02-19"),
    ("2020_covid_recovery",  "2020-02-19", "2021-01-01"),
    ("2021_rally",           "2021-01-01", "2022-01-01"),
    ("2022_inflation_shock", "2022-01-01", "2023-01-01"),
    ("2023_ai_rally",        "2023-01-01", "2024-01-01"),
    ("2024_25_recent",       "2024-01-01", "2026-01-01"),
    ("2026_ytd",             "2026-01-01", "2026-12-31"),
]

# Walk-forward parameters
WF_INITIAL_TRAIN_END = "2021-12-31"  # First train ends here, then expand annually
WF_REFIT_FREQ = "Y"                    # Refit annually


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def family_d_alloc_series(breadth: pd.Series, dates: pd.DatetimeIndex,
                            L_pct: float, base: float = 0.5, on: float = 1.5,
                            window_start: pd.Timestamp | None = None) -> pd.Series:
    """Return daily allocation for the Family D (50/150) strategy."""
    aligned = breadth.reindex(dates).ffill().shift(1).fillna(0)
    alloc = pd.Series(base, index=dates, dtype=float)
    alloc.loc[aligned >= L_pct / 100.0] = on
    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0
    return alloc


def equity_from_alloc(alloc: pd.Series, close: pd.Series,
                       cost: float = COST_BPS / 10_000,
                       borrow_rate: float = 0.0) -> pd.Series:
    """Build equity curve from daily allocation + close prices.
    Optional borrow_rate (annual) on excess allocation above 100%."""
    daily = close.pct_change().fillna(0)
    strat = alloc * daily
    # Turnover cost
    turnover = alloc.diff().abs().fillna(0)
    strat = strat - turnover * cost
    # Borrow cost on excess leverage (annual rate / 252 per day)
    if borrow_rate > 0:
        excess = (alloc - 1.0).clip(lower=0)
        daily_borrow = excess * (borrow_rate / 252.0)
        strat = strat - daily_borrow
    return (1.0 + strat).cumprod()


def sharpe_of(equity: pd.Series, period_start=None, period_end=None) -> float:
    eq = equity
    if period_start is not None:
        eq = eq.loc[eq.index >= pd.Timestamp(period_start)]
    if period_end is not None:
        eq = eq.loc[eq.index <= pd.Timestamp(period_end)]
    if len(eq) < 2:
        return float("nan")
    daily = eq.pct_change().fillna(0)
    if daily.std() == 0:
        return 0.0
    return float(daily.mean() / daily.std() * math.sqrt(252))


def best_L_in_window(close: pd.Series, breadth: pd.Series,
                      window_start: pd.Timestamp, window_end: pd.Timestamp,
                      l_grid=LONG_THRESHOLDS, base: float = 0.5, on: float = 1.5
                      ) -> tuple[int, float]:
    """Find the L value with the highest Sharpe in the given window."""
    best_L = None
    best_sh = -1e9
    for L in l_grid:
        alloc = family_d_alloc_series(breadth, close.index, L, base, on,
                                        window_start=window_start)
        eq = equity_from_alloc(alloc, close)
        sh = sharpe_of(eq, window_start, window_end)
        if not np.isnan(sh) and sh > best_sh:
            best_sh = sh
            best_L = L
    return best_L, best_sh


# ----------------------------------------------------------------------
# TEST 1: WALK-FORWARD L SELECTION
# ----------------------------------------------------------------------


def walk_forward_l_per_etf(close: pd.Series, breadth: pd.Series,
                             eligible_start: pd.Timestamp,
                             initial_train_end: pd.Timestamp,
                             refit_freq: str = "Y") -> dict:
    """Refit L on expanding train window each refit period. Concatenate
    test segments into a full walk-forward equity curve.

    Returns dict with:
      - segments: list of {train_end, test_start, test_end, fitted_L, train_sh, test_sh}
      - walk_forward_equity: pd.Series of equity in the test-only periods
      - walk_forward_sharpe: Sharpe across the concatenated test segments
      - in_sample_L: the L picked using the full window (for comparison)
      - in_sample_sharpe: Sharpe with that in-sample L over full window
    """
    # Refit dates: end of each calendar year starting at initial_train_end
    last_date = close.index[-1]
    refit_ends = pd.date_range(initial_train_end, last_date, freq=refit_freq)
    # Ensure all refit_ends are on trading days
    refit_ends = [close.index[close.index.searchsorted(r, side="right") - 1] for r in refit_ends]
    refit_ends = [r for r in refit_ends if r >= eligible_start]
    if not refit_ends:
        return {}

    segments = []
    test_eq_pieces = []
    for i, train_end in enumerate(refit_ends):
        # Train: eligible_start to train_end (inclusive)
        train_end_idx = close.index.get_loc(train_end)
        # Test: train_end+1 to either next refit_end or end of data
        if i + 1 < len(refit_ends):
            test_end = refit_ends[i + 1]
        else:
            test_end = last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(close):
            break
        test_start = close.index[test_start_idx]
        if test_start > test_end:
            continue

        # Fit L on train window
        fitted_L, train_sh = best_L_in_window(close, breadth, eligible_start, train_end)
        if fitted_L is None:
            continue

        # Apply that L to the test segment
        alloc = family_d_alloc_series(breadth, close.index, fitted_L,
                                        window_start=eligible_start)
        full_eq = equity_from_alloc(alloc, close)
        test_eq = full_eq.loc[test_start:test_end]
        # Renormalise to 1.0 at test_start - 1 (start of test segment)
        # Use full_eq.loc[test_start_idx - 1] as the base if available
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq / base_val

        test_sh = sharpe_of(test_eq, test_start, test_end)
        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "fitted_L": fitted_L,
            "train_sharpe": _safe(train_sh),
            "test_sharpe": _safe(test_sh),
            "n_test_days": int(len(test_eq)),
        })
        # Build the walk-forward equity: each test segment is concatenated
        # We multiply through by the previous WF equity ending value.
        last_wf_val = test_eq_pieces[-1].iloc[-1] if test_eq_pieces else 1.0
        test_eq_pieces.append(test_eq * last_wf_val / test_eq.iloc[0])

    if not test_eq_pieces:
        return {}
    wf_equity = pd.concat(test_eq_pieces)
    wf_sharpe = sharpe_of(wf_equity)
    # In-sample equivalent: best L on full window
    in_L, in_sh = best_L_in_window(close, breadth, eligible_start, close.index[-1])
    return {
        "segments": segments,
        "in_sample_L": in_L,
        "in_sample_sharpe": _safe(in_sh),
        "walk_forward_sharpe": _safe(wf_sharpe),
        "wf_dates": [d.strftime("%Y-%m-%d") for d in wf_equity.index],
        "wf_equity": [round(float(x), 6) for x in wf_equity.values],
    }


# ----------------------------------------------------------------------
# TEST 2: BORROW COST ADJUSTMENT
# ----------------------------------------------------------------------


def borrow_adjusted(close: pd.Series, breadth: pd.Series, L_pct: float,
                     eligible_start: pd.Timestamp,
                     borrow_rate: float = BORROW_RATE_ANNUAL,
                     base: float = 0.5, on: float = 1.5) -> dict:
    alloc = family_d_alloc_series(breadth, close.index, L_pct, base, on,
                                    window_start=eligible_start)
    eq_free = equity_from_alloc(alloc, close, borrow_rate=0.0)
    eq_paid = equity_from_alloc(alloc, close, borrow_rate=borrow_rate)
    return {
        "L": L_pct,
        "sharpe_free_borrow": _safe(sharpe_of(eq_free, eligible_start)),
        "sharpe_with_borrow": _safe(sharpe_of(eq_paid, eligible_start)),
        "total_return_free": _safe(float(eq_free.loc[eligible_start:].iloc[-1] /
                                             eq_free.loc[eligible_start:].iloc[0] - 1)),
        "total_return_paid": _safe(float(eq_paid.loc[eligible_start:].iloc[-1] /
                                             eq_paid.loc[eligible_start:].iloc[0] - 1)),
        "borrow_rate_annual": borrow_rate,
        "avg_excess_leverage_when_long": float(((alloc - 1.0).clip(lower=0).loc[eligible_start:]).mean()),
    }


# ----------------------------------------------------------------------
# TEST 3: SUB-PERIOD DECOMPOSITION
# ----------------------------------------------------------------------


def sub_period_breakdown(equity: pd.Series, periods: list[tuple[str, str, str]]) -> list[dict]:
    out = []
    for label, start, end in periods:
        eq = equity.loc[(equity.index >= pd.Timestamp(start)) &
                        (equity.index < pd.Timestamp(end))].copy()
        if len(eq) < 5:
            out.append({"label": label, "start": start, "end": end,
                        "n_days": int(len(eq)), "sharpe": None,
                        "total_return": None, "max_dd": None})
            continue
        eq = eq / eq.iloc[0]
        daily = eq.pct_change().fillna(0)
        sh = _safe(daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else 0.0)
        peaks = eq.cummax()
        dd = float((1.0 - eq / peaks).max())
        out.append({
            "label": label, "start": start, "end": end,
            "n_days": int(len(eq)),
            "sharpe": sh,
            "total_return": _safe(float(eq.iloc[-1] - 1)),
            "max_dd": _safe(dd),
        })
    return out


# ----------------------------------------------------------------------
# TEST 4: BOOTSTRAP CONFIDENCE INTERVAL ON SHARPE
# ----------------------------------------------------------------------


def block_bootstrap_sharpe(daily_returns: pd.Series, n_samples: int = 1000,
                              block_size: int = 60, seed: int = 42) -> dict:
    """Stationary block bootstrap of daily returns to get Sharpe distribution."""
    rng = np.random.default_rng(seed)
    rets = daily_returns.dropna().values
    n = len(rets)
    if n < block_size * 2:
        return {"n_samples": 0, "note": "insufficient data"}
    sharpes = []
    for _ in range(n_samples):
        # Sample (n / block_size) blocks
        n_blocks = n // block_size
        starts = rng.integers(0, n - block_size, size=n_blocks)
        sample = np.concatenate([rets[s:s + block_size] for s in starts])
        if sample.std() > 0:
            sharpes.append(sample.mean() / sample.std() * math.sqrt(252))
    sharpes = np.array(sharpes)
    return {
        "n_samples": int(len(sharpes)),
        "block_size_days": block_size,
        "point_sharpe": _safe(sharpe_of_daily(daily_returns)),
        "bootstrap_mean": _safe(float(sharpes.mean())),
        "bootstrap_std": _safe(float(sharpes.std())),
        "bootstrap_p5": _safe(float(np.percentile(sharpes, 5))),
        "bootstrap_p50": _safe(float(np.percentile(sharpes, 50))),
        "bootstrap_p95": _safe(float(np.percentile(sharpes, 95))),
        "pct_positive": _safe(float((sharpes > 0).mean())),
    }


def sharpe_of_daily(daily: pd.Series) -> float:
    daily = daily.dropna()
    if len(daily) < 2 or daily.std() == 0:
        return float("nan")
    return float(daily.mean() / daily.std() * math.sqrt(252))


# ----------------------------------------------------------------------
# TEST 5: MA-PERIOD ROBUSTNESS
# ----------------------------------------------------------------------


def run_with_rebalance_freq(close: pd.Series, breadth: pd.Series, L_pct: float,
                              freq: str, base: float = 0.5, on: float = 1.5,
                              cost: float = COST_BPS / 10_000,
                              window_start: pd.Timestamp | None = None) -> dict:
    """Apply MA200 + 50/150 with a specific rebalance frequency.

    freq in {"D", "W-FRI", "W-MON", "2W-FRI", "BME"}.
    On non-rebalance days the allocation is held constant.
    """
    aligned = breadth.reindex(close.index, method="ffill").shift(1).fillna(0)
    raw_alloc = pd.Series(base, index=close.index, dtype=float)
    raw_alloc.loc[aligned >= L_pct / 100.0] = on

    if freq == "D":
        alloc = raw_alloc.copy()
    else:
        rebal_target = pd.date_range(close.index[0], close.index[-1], freq=freq)
        # Snap each rebalance target to the previous trading day
        rebal_dates = []
        for r in rebal_target:
            pos = close.index.searchsorted(r, side="right") - 1
            if pos >= 0:
                rebal_dates.append(close.index[pos])
        rebal_dates = pd.DatetimeIndex(rebal_dates).unique()
        # Hold alloc constant between rebalances
        alloc = pd.Series(np.nan, index=close.index, dtype=float)
        for rd in rebal_dates:
            alloc.loc[rd] = raw_alloc.loc[rd]
        alloc = alloc.ffill().fillna(base)

    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0

    daily = close.pct_change().fillna(0)
    strat_ret = alloc * daily
    turnover = alloc.diff().abs().fillna(0)
    strat_ret = strat_ret - turnover * cost
    equity = (1.0 + strat_ret).cumprod()
    return {"equity": equity, "alloc": alloc, "turnover": turnover}


def rebalance_freq_test(close: pd.Series, breadth: pd.Series,
                          L_pct: float, eligible_start: pd.Timestamp,
                          freqs: list[tuple[str, str]]) -> list[dict]:
    """Run the strategy at multiple rebalance frequencies; report stats."""
    rows = []
    for label, freq in freqs:
        r = run_with_rebalance_freq(close, breadth, L_pct, freq,
                                       window_start=eligible_start)
        eq_window = r["equity"].loc[r["equity"].index >= eligible_start]
        eq_window = eq_window / eq_window.iloc[0]
        sh = sharpe_of(eq_window)
        peaks = eq_window.cummax()
        max_dd = float((1.0 - eq_window / peaks).max())
        total = float(eq_window.iloc[-1] - 1)
        annual_turnover = float(r["turnover"].loc[r["turnover"].index >= eligible_start].sum()
                                  / (len(eq_window) / 252.0))
        n_changes = int((r["alloc"].diff().abs() > 1e-6).sum())
        rows.append({
            "rebalance_label": label,
            "rebalance_freq": freq,
            "sharpe": _safe(sh),
            "total_return": _safe(total),
            "max_dd": _safe(max_dd),
            "annual_turnover": _safe(annual_turnover),
            "n_allocation_changes": n_changes,
        })
    return rows


def ma_period_robustness(target_etf: str,
                            ma_periods: list[int] = [100, 150, 200, 250, 300],
                            eligible_start: pd.Timestamp | None = None) -> list[dict]:
    """For each MA period, recompute breadth, sweep L, find best Sharpe."""
    results = []
    cfg = get_etf(target_etf)
    proxy = cfg.get("yfinance_trading_proxy") or target_etf
    cprices = load_constituent_prices(target_etf)
    dl_start = (cprices.index.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    dl_end = (cprices.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    ohlc = download_soxx_ohlc(dl_start, dl_end, etf=proxy, yf_symbol=proxy)
    close = ohlc["Close"].astype(float)
    close = close[~close.index.duplicated(keep="first")]
    for ma in ma_periods:
        breadth = compute_ma200_breadth(cprices, period=ma)
        # Eligible: ma + 50 days buffer
        if eligible_start is None:
            n_with = cprices.rolling(ma, min_periods=ma).mean().notna().sum(axis=1)
            es = n_with[n_with >= 0.5 * cprices.shape[1]].index.min()
        else:
            es = eligible_start
        if pd.isna(es):
            continue
        best_L, best_sh = best_L_in_window(close, breadth, es, close.index[-1])
        alloc = family_d_alloc_series(breadth, close.index, best_L,
                                        window_start=es)
        eq = equity_from_alloc(alloc, close)
        eq_window = eq.loc[eq.index >= es]
        eq_window = eq_window / eq_window.iloc[0]
        total = float(eq_window.iloc[-1] - 1)
        peaks = eq_window.cummax()
        max_dd = float((1.0 - eq_window / peaks).max())
        results.append({
            "ma_period": ma,
            "best_L": best_L,
            "sharpe": _safe(best_sh),
            "total_return": _safe(total),
            "max_dd": _safe(max_dd),
            "eligible_start": es.strftime("%Y-%m-%d"),
        })
    return results


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------


def main() -> int:
    print("Loading data per ETF ...", flush=True)
    per_etf = {}
    eligible_starts = {}
    closes_panel = {}
    for etf in ETFS:
        cfg = get_etf(etf)
        proxy = cfg.get("yfinance_trading_proxy") or etf
        cprices = load_constituent_prices(etf)
        breadth = compute_ma200_breadth(cprices, period=200)
        n_with = cprices.rolling(200, min_periods=200).mean().notna().sum(axis=1)
        es = n_with[n_with >= 0.5 * cprices.shape[1]].index.min()
        if pd.isna(es):
            es = breadth.index[200]
        dl_start = (cprices.index.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end = (cprices.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        ohlc = download_soxx_ohlc(dl_start, dl_end, etf=proxy, yf_symbol=proxy)
        close = ohlc["Close"].astype(float)
        close = close[~close.index.duplicated(keep="first")]
        per_etf[etf] = {"breadth": breadth, "close": close, "eligible": es}
        eligible_starts[etf] = es
        closes_panel[etf] = close
        print(f"  {etf:5}  eligible from {es.date()}  {len(close)} price rows")

    # ===== TEST 1: WALK-FORWARD L PER ETF =====
    print("\n=== TEST 1: Walk-forward L per ETF (annual refit) ===")
    wf_per_etf = {}
    for etf in ETFS:
        if etf not in per_etf:
            continue
        d = per_etf[etf]
        wf = walk_forward_l_per_etf(d["close"], d["breadth"], d["eligible"],
                                      pd.Timestamp(WF_INITIAL_TRAIN_END),
                                      refit_freq="YE")
        if not wf:
            continue
        wf_per_etf[etf] = wf
        ls = [s["fitted_L"] for s in wf["segments"]]
        print(f"  {etf:5}  IS Sharpe {wf['in_sample_sharpe']:+.2f} (L={wf['in_sample_L']})  "
              f"WF Sharpe {wf['walk_forward_sharpe']:+.2f}  "
              f"refit L's: {ls}  Δ = {wf['walk_forward_sharpe'] - wf['in_sample_sharpe']:+.2f}")

    # ===== TEST 2: BORROW COST ADJUSTMENT =====
    print("\n=== TEST 2: Borrow cost (5%/yr on excess leverage) ===")
    borrow_results = {}
    for etf in ["CSP1", "SOXX", "IUIT", "CNDX"]:
        if etf not in per_etf:
            continue
        d = per_etf[etf]
        # Use in-sample best L per ETF
        in_L, _ = best_L_in_window(d["close"], d["breadth"], d["eligible"], d["close"].index[-1])
        if in_L is None:
            continue
        r = borrow_adjusted(d["close"], d["breadth"], in_L, d["eligible"])
        borrow_results[etf] = r
        print(f"  {etf:5}  L={in_L}  Sharpe free {r['sharpe_free_borrow']:+.2f}  "
              f"with borrow {r['sharpe_with_borrow']:+.2f}  "
              f"Δ = {r['sharpe_with_borrow'] - r['sharpe_free_borrow']:+.2f}  "
              f"totRet drag {r['total_return_paid'] - r['total_return_free']:+.1%}")

    # ===== TEST 3: SUB-PERIOD DECOMPOSITION =====
    print("\n=== TEST 3: Sub-period Sharpe decomposition ===")
    sub_period_results = {}
    target_strategies = ["CSP1", "SOXX", "IUIT", "CNDX", "IUUS"]
    for etf in target_strategies:
        if etf not in per_etf:
            continue
        d = per_etf[etf]
        in_L, _ = best_L_in_window(d["close"], d["breadth"], d["eligible"], d["close"].index[-1])
        alloc = family_d_alloc_series(d["breadth"], d["close"].index, in_L,
                                        window_start=d["eligible"])
        eq = equity_from_alloc(alloc, d["close"])
        # Also BH for comparison
        bh = d["close"] / d["close"].iloc[0]
        sub_period_results[etf] = {
            "strategy": sub_period_breakdown(eq, SUB_PERIODS),
            "buy_and_hold": sub_period_breakdown(bh, SUB_PERIODS),
        }
        print(f"\n  {etf:5} per-period Sharpe (strategy / BH):")
        for s, b in zip(sub_period_results[etf]["strategy"],
                         sub_period_results[etf]["buy_and_hold"]):
            sh_s = s.get("sharpe")
            sh_b = b.get("sharpe")
            if sh_s is None or sh_b is None:
                continue
            print(f"    {s['label']:<24}  strategy {sh_s:+.2f}  BH {sh_b:+.2f}  "
                  f"Δ {sh_s - sh_b:+.2f}")

    # ===== TEST 4: BOOTSTRAP SHARPE CI =====
    print("\n=== TEST 4: Block bootstrap (60-day blocks, 1000 samples) ===")
    bootstrap_results = {}
    for etf in target_strategies:
        if etf not in per_etf:
            continue
        d = per_etf[etf]
        in_L, _ = best_L_in_window(d["close"], d["breadth"], d["eligible"], d["close"].index[-1])
        alloc = family_d_alloc_series(d["breadth"], d["close"].index, in_L,
                                        window_start=d["eligible"])
        eq = equity_from_alloc(alloc, d["close"])
        eq_w = eq.loc[eq.index >= d["eligible"]]
        daily = eq_w.pct_change().fillna(0)
        boot = block_bootstrap_sharpe(daily, n_samples=1000, block_size=60)
        bootstrap_results[etf] = boot
        print(f"  {etf:5}  point Sharpe {boot['point_sharpe']:+.2f}  "
              f"bootstrap p5 {boot['bootstrap_p5']:+.2f}  "
              f"p50 {boot['bootstrap_p50']:+.2f}  "
              f"p95 {boot['bootstrap_p95']:+.2f}  "
              f"% positive {boot['pct_positive']*100:.0f}%")

    # ===== TEST 5: MA-PERIOD ROBUSTNESS =====
    print("\n=== TEST 5: MA-period robustness (CSP1) ===")
    ma_results_csp1 = ma_period_robustness("CSP1",
                                              ma_periods=[100, 150, 200, 250, 300])
    for r in ma_results_csp1:
        print(f"  MA{r['ma_period']:>3}  L={r['best_L']}  "
              f"Sharpe {r['sharpe']:+.2f}  totRet {r['total_return']*100:+.0f}%  "
              f"DD {r['max_dd']*100:.0f}%")

    # Also do SOXX since it's the second most-interesting case
    print("\n=== TEST 5b: MA-period robustness (SOXX) ===")
    ma_results_soxx = ma_period_robustness("SOXX",
                                              ma_periods=[100, 150, 200, 250, 300])
    for r in ma_results_soxx:
        print(f"  MA{r['ma_period']:>3}  L={r['best_L']}  "
              f"Sharpe {r['sharpe']:+.2f}  totRet {r['total_return']*100:+.0f}%  "
              f"DD {r['max_dd']*100:.0f}%")

    # ===== TEST 6: REBALANCE FREQUENCY =====
    print("\n=== TEST 6: Rebalance frequency sensitivity ===")
    rebal_freqs = [
        ("Daily",        "D"),
        ("Weekly Fri",   "W-FRI"),
        ("Weekly Mon",   "W-MON"),
        ("Bi-weekly Fri", "2W-FRI"),
        ("Month-end",    "BME"),
    ]
    rebal_etfs = ["CSP1", "SOXX", "IUIT", "CNDX", "IUES"]
    rebal_results: dict[str, list[dict]] = {}
    for etf in rebal_etfs:
        if etf not in per_etf:
            continue
        d = per_etf[etf]
        in_L, _ = best_L_in_window(d["close"], d["breadth"], d["eligible"], d["close"].index[-1])
        if in_L is None:
            continue
        rows = rebalance_freq_test(d["close"], d["breadth"], in_L, d["eligible"], rebal_freqs)
        rebal_results[etf] = rows
        print(f"\n  {etf:5}  (L={in_L} in-sample):")
        for r in rows:
            print(f"    {r['rebalance_label']:<14}  Sharpe {r['sharpe']:+.2f}  "
                  f"totRet {r['total_return']*100:+5.0f}%  DD {r['max_dd']*100:>4.1f}%  "
                  f"turnover/yr {r['annual_turnover']:.2f}  flips {r['n_allocation_changes']}")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "etfs": list(per_etf.keys()),
        "borrow_rate_annual": BORROW_RATE_ANNUAL,
        "sub_period_definitions": [
            {"label": l, "start": s, "end": e} for l, s, e in SUB_PERIODS
        ],
        "test_1_walk_forward_l": wf_per_etf,
        "test_2_borrow_cost": borrow_results,
        "test_3_sub_periods": sub_period_results,
        "test_4_bootstrap": bootstrap_results,
        "test_5_ma_period_csp1": ma_results_csp1,
        "test_5b_ma_period_soxx": ma_results_soxx,
        "test_6_rebalance_freq": rebal_results,
    }

    def clean(o):
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else o
        if isinstance(o, list):
            return [clean(x) for x in o]
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        return o
    payload = clean(payload)

    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    # Headline summary
    print()
    print("=" * 95)
    print("ROBUSTNESS HEADLINE — does the Sharpe 1.04 (CSP1 MA200 50/150) hold up?")
    print("=" * 95)
    csp1 = wf_per_etf.get("CSP1", {})
    csp1_borrow = borrow_results.get("CSP1", {})
    csp1_boot = bootstrap_results.get("CSP1", {})
    csp1_ma = next((r for r in ma_results_csp1 if r["ma_period"] == 200), {})
    print(f"  In-sample MA200 + best L Sharpe        : {csp1.get('in_sample_sharpe'):+.2f}")
    print(f"  Walk-forward (annual L refit) Sharpe   : {csp1.get('walk_forward_sharpe'):+.2f}")
    print(f"  Borrow-cost (5%) adjusted Sharpe       : {csp1_borrow.get('sharpe_with_borrow'):+.2f}")
    print(f"  Bootstrap p5 / p50 / p95 Sharpe        : "
          f"{csp1_boot.get('bootstrap_p5'):+.2f} / "
          f"{csp1_boot.get('bootstrap_p50'):+.2f} / "
          f"{csp1_boot.get('bootstrap_p95'):+.2f}")
    print(f"  MA-period range Sharpe (100 to 300)    : "
          f"{min(r['sharpe'] for r in ma_results_csp1):+.2f} to "
          f"{max(r['sharpe'] for r in ma_results_csp1):+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
