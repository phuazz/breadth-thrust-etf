"""Idea 2 — Sector-RELATIVE breadth in Strategy A (Phase 20 candidate).

Strategy A currently ranks sectors by ABSOLUTE breadth (% of constituents
above 200d MA). In a market-wide rally where everything is bullish, all
sectors have high absolute breadth and the top-K picker effectively
becomes a market-beta strategy.

The fix: rank by RELATIVE breadth = sector_breadth - cross_sectional_mean.
This neutralises market-wide moves and gives a cleaner cross-sectional
"which sector is genuinely leading on a breadth basis" signal.

Pros:
  - Cleaner cross-sectional signal
  - Should reduce correlation between A and the market beta
  - Improves A's diversification contribution to the blend

Cons:
  - Mean-zero signal — half the universe is always positive, half negative.
    The top-K-by-signal logic still picks the top K but the magnitudes
    are smaller, so breadth-weighted weighting may shift.
  - When ALL sectors crash together, relative breadth has no defensive
    properties — the top-K picker still picks the "least bad" sectors,
    which still go down with the market.

Test: A/B compare K=7 weekly Friday absolute-breadth vs relative-breadth
on full backtest + 2022 / 2022-2024 windows. Report Sharpe, CAGR, DD,
turnover, and correlation with SPY.

Usage: python scripts/test_idea2_sector_relative_breadth.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from run_portfolio import (  # noqa: E402
    build_panels, run_portfolio, top_k_breadth_weight, top_k_eq_weight,
    COST_BPS,
)
from run_ma200_sweep import MA_PERIOD  # noqa: E402
from backtest import download_spy_close  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

HEADLINE_K = 7
REBAL_FREQ = "W-FRI"
COST = COST_BPS / 10_000

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-2024",    "2022-01-01", "2024-12-31"),
    ("2022-onwards", "2022-01-01", None),
]


def _stats(eq: pd.Series) -> dict:
    if len(eq) < 2:
        return {"sharpe": None, "cagr": None, "total": None, "dd": None}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else 0
    sh = daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else 0
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return {"sharpe": sh, "cagr": cagr, "total": eq.iloc[-1] - 1, "dd": dd}


def _window_stats(eq: pd.Series, start, end) -> dict:
    w = eq.loc[start:end].dropna() if (start or end) else eq.dropna()
    return _stats(w) if len(w) >= 5 else {"sharpe": None, "cagr": None,
                                            "total": None, "dd": None}


def _correlation_with_spy(eq: pd.Series, spy_close: pd.Series) -> float:
    common = eq.index.intersection(spy_close.index)
    if len(common) < 30:
        return float("nan")
    a = eq.loc[common].pct_change().fillna(0)
    b = spy_close.loc[common].pct_change().fillna(0)
    return float(a.corr(b))


def make_relative_breadth(breadths: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional adjustment: subtract the row-mean from each entry.

    For each date, mean across all NON-NaN sectors is the "market"
    baseline. Each sector's relative breadth = (sector - mean). Half the
    sectors will have positive relative breadth on any given date.
    """
    row_mean = breadths.mean(axis=1, skipna=True)
    return breadths.sub(row_mean, axis=0)


def main() -> int:
    print("Loading per-sector breadth panel ...")
    closes, breadths, etfs_used = build_panels()
    print(f"  {len(etfs_used)} ETFs: {etfs_used}")
    print(f"  Date range: {closes.index[0].date()} -> {closes.index[-1].date()}")

    # Eligible start: 200d after the latest first-valid breadth date in the panel
    first_valids = {c: breadths[c].first_valid_index() for c in breadths.columns}
    latest_first = max(d for d in first_valids.values() if d is not None)
    eligible_idx = closes.index.searchsorted(latest_first) + MA_PERIOD
    eligible = closes.index[eligible_idx]
    print(f"  Eligible start: {eligible.date()}")

    # Load SPY for correlation
    spy = download_spy_close(start="2015-01-01",
                              end=closes.index[-1].strftime("%Y-%m-%d"))
    spy.index = pd.to_datetime(spy.index).tz_localize(None)

    print("\n=== A/B: absolute vs relative breadth (K=7 weekly Fri) ===\n")

    # Baseline — absolute breadth
    print("Running BASELINE (absolute breadth) ...")
    base = run_portfolio(closes, breadths, top_k_breadth_weight(HEADLINE_K),
                          eligible, cost=COST, rebalance_freq=REBAL_FREQ)
    base_eq = base["equity"].loc[base["equity"].index >= eligible]
    base_corr = _correlation_with_spy(base_eq, spy)

    # Variant — relative breadth (subtract row mean)
    print("Running VARIANT (relative breadth = sector - mean-of-sectors) ...")
    rel_breadths = make_relative_breadth(breadths)
    var = run_portfolio(closes, rel_breadths, top_k_breadth_weight(HEADLINE_K),
                         eligible, cost=COST, rebalance_freq=REBAL_FREQ)
    var_eq = var["equity"].loc[var["equity"].index >= eligible]
    var_corr = _correlation_with_spy(var_eq, spy)

    # Also test EQUAL-weight top-K under both signals (removes weight-fn confound)
    print("Running BASELINE eq-weight top-K (no breadth weighting) ...")
    base_eq_w = run_portfolio(closes, breadths, top_k_eq_weight(HEADLINE_K),
                               eligible, cost=COST, rebalance_freq=REBAL_FREQ)
    base_eq_w_eq = base_eq_w["equity"].loc[base_eq_w["equity"].index >= eligible]

    print("Running VARIANT eq-weight top-K (relative breadth, no signal weighting) ...")
    var_eq_w = run_portfolio(closes, rel_breadths, top_k_eq_weight(HEADLINE_K),
                              eligible, cost=COST, rebalance_freq=REBAL_FREQ)
    var_eq_w_eq = var_eq_w["equity"].loc[var_eq_w["equity"].index >= eligible]

    # Stats per window
    def print_block(label, eq, corr=None):
        print(f"\n  {label}")
        for win, start, end in WINDOWS:
            s = _window_stats(eq, start, end)
            if s["sharpe"] is None:
                print(f"    {win:<14s}  insufficient data")
                continue
            print(f"    {win:<14s}  Sharpe {s['sharpe']:+.3f}  "
                  f"CAGR {s['cagr']*100:+5.1f}%  "
                  f"Total {s['total']*100:+6.1f}%  "
                  f"DD {s['dd']*100:.1f}%")
        if corr is not None:
            print(f"    corr w/ SPY: {corr:+.3f}")

    print("\n" + "=" * 90)
    print("RESULTS — breadth-weighted top-K (current deployed weight fn)")
    print("=" * 90)
    print_block("BASELINE (absolute breadth)", base_eq, base_corr)
    print_block("VARIANT  (relative breadth)",  var_eq,  var_corr)

    # Compute deltas table
    print("\n" + "-" * 90)
    print("DELTAS (variant - baseline)")
    print("-" * 90)
    for win, start, end in WINDOWS:
        b = _window_stats(base_eq, start, end)
        v = _window_stats(var_eq, start, end)
        if b["sharpe"] is None or v["sharpe"] is None:
            continue
        d_sh = v["sharpe"] - b["sharpe"]
        d_total = (v["total"] - b["total"]) * 100
        d_dd = (v["dd"] - b["dd"]) * 100
        d_cagr = (v["cagr"] - b["cagr"]) * 100
        print(f"  {win:<14s}  dSharpe {d_sh:+.3f}  dCAGR {d_cagr:+.2f}pp  "
              f"dTotal {d_total:+.2f}pp  dDD {d_dd:+.2f}pp")
    print(f"  corr w/ SPY: baseline {base_corr:+.3f}  variant {var_corr:+.3f}  "
          f"d {(var_corr - base_corr):+.3f}")

    print("\n" + "=" * 90)
    print("RESULTS — equal-weight top-K (removes weight-fn confound)")
    print("=" * 90)
    print_block("BASELINE eq-weight (absolute)", base_eq_w_eq)
    print_block("VARIANT  eq-weight (relative)", var_eq_w_eq)

    # Turnover comparison
    base_to = base["weights"].diff().abs().sum(axis=1).fillna(0)
    var_to = var["weights"].diff().abs().sum(axis=1).fillna(0)
    n_years = (closes.index[-1] - eligible).days / 365.25
    base_annual_to = base_to.sum() / n_years
    var_annual_to = var_to.sum() / n_years
    print(f"\n  Annual turnover:  baseline {base_annual_to:.2f}  "
          f"variant {var_annual_to:.2f}  d {var_annual_to - base_annual_to:+.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
