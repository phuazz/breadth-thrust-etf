"""Top-K rotation strategy: rebalance-frequency robustness + trade history.

The Test 10 result in run_robustness.py established that the cross-sectional
top-K-by-breadth rotation paradigm is the right deployment paradigm. This
script drills into that strategy specifically:

  1. Rebalance-frequency sensitivity grid: K ∈ {3, 5, 7} × cadence ∈
     {Daily, Weekly Fri, Bi-weekly Fri, Month-end}. Compares Sharpe, max DD,
     total return, annual turnover, n flips.

  2. Trade history: for each rebalance date of the headline variant
     (K=7 weighted-by-breadth, weekly Friday), list the ETFs held and their
     weights. This is the trade explorer table.

  3. Headline equity curve for K=5 and K=7 weekly Friday, including the
     turnover series for the trade-impact chart.

Output: data/topk_robustness.json

Run: python scripts/run_topk_robustness.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "topk_robustness.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from run_portfolio import (  # noqa: E402
    build_panels, run_portfolio, top_k_breadth_weight, equal_weight_all_fn,
)
from run_improvements import compute_stats  # noqa: E402
from run_ma200_sweep import MA_PERIOD, COST_BPS  # noqa: E402
from backtest import download_spy_close  # noqa: E402


REBAL_FREQS = [
    ("Daily",         "D"),
    ("Weekly Fri",    "W-FRI"),
    ("Bi-weekly Fri", "2W-FRI"),
    ("Month-end",     "BME"),
]
K_GRID = [3, 5, 7]
HEADLINE_K = 7
HEADLINE_FREQ_NAME = "Weekly Fri"
HEADLINE_FREQ = "W-FRI"


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def round_series(values, ndigits=4):
    out = []
    for v in values:
        try:
            f = float(v)
            out.append(round(f, ndigits) if not (math.isnan(f) or math.isinf(f)) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def turnover_stats(weight_panel: pd.DataFrame, eligible: pd.Timestamp) -> dict:
    """Annual turnover and number of allocation flips."""
    wp = weight_panel.loc[weight_panel.index >= eligible].copy()
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    annual_to = float(diff.sum() / n_years) if n_years > 0 else 0.0
    # Flip = any day where weights change materially
    flips = int((diff > 1e-6).sum())
    return {
        "annual_turnover": annual_to,
        "n_flips": flips,
        "n_years": float(n_years),
    }


def build_trade_history(weight_panel: pd.DataFrame,
                        breadth_panel: pd.DataFrame,
                        eligible: pd.Timestamp,
                        top_n: int | None = None) -> list[dict]:
    """One row per rebalance date with non-zero weight changes.

    Each row: { date, holdings: [(etf, weight, breadth_pct)] }
    Only emits rows where weights actually changed vs the previous row
    (collapses the daily ffill into actual rebalance events).
    """
    wp = weight_panel.loc[weight_panel.index >= eligible].copy()
    bp = breadth_panel.reindex(wp.index, method="ffill")
    out: list[dict] = []
    prev: pd.Series | None = None
    for dt, row in wp.iterrows():
        if prev is None or not np.allclose(row.values, prev.values, atol=1e-6):
            non_zero = row[row > 1e-6].sort_values(ascending=False)
            # Skip rows with no holdings (pre-signal warm-up period)
            if len(non_zero) == 0:
                prev = row
                continue
            holdings = []
            for etf, w in non_zero.items():
                b_val = bp.loc[dt, etf] if etf in bp.columns else None
                holdings.append({
                    "etf": etf,
                    "weight": round(float(w), 4),
                    "breadth_pct": round(float(b_val) * 100, 1) if b_val == b_val else None,
                })
            out.append({
                "date": dt.strftime("%Y-%m-%d"),
                "holdings": holdings,
            })
            prev = row
    if top_n is not None and len(out) > top_n:
        out = out[-top_n:]
    return out


def main() -> int:
    print("Building panels (closes + ma200 breadth) for all ETFs ...", flush=True)
    closes, breadths, etfs_used = build_panels()
    print(f"  {len(etfs_used)} ETFs used: {etfs_used}")

    # Eligible start: same logic as run_portfolio
    starts = []
    for etf in etfs_used:
        b = breadths[etf].dropna()
        if len(b):
            starts.append(b.index.min())
    eligible = max(starts)
    eligible = pd.Timestamp(eligible.date()) + pd.Timedelta(days=MA_PERIOD)
    eligible = (closes.index[closes.index >= eligible][0]
                if (closes.index >= eligible).any() else closes.index[MA_PERIOD])
    print(f"  Eligible start: {eligible.date()}")

    # =====================================================================
    # 1. Rebalance-frequency sensitivity grid
    # =====================================================================
    print("\n=== Rebalance-frequency sensitivity: K × cadence ===")
    grid: dict[str, dict[str, dict]] = {}
    headline_payload: dict | None = None

    for K in K_GRID:
        grid[f"K={K}"] = {}
        print(f"\n  --- K = {K} (top-{K}, weighted by breadth excess) ---")
        for freq_name, freq_code in REBAL_FREQS:
            r = run_portfolio(closes, breadths, top_k_breadth_weight(K),
                              eligible, rebalance_freq=freq_code)
            eq_window = r["equity"].loc[r["equity"].index >= eligible]
            eq_window = eq_window / eq_window.iloc[0]
            st = compute_stats(r["equity"], eligible)
            to = turnover_stats(r["weights"], eligible)
            grid[f"K={K}"][freq_name] = {
                "sharpe": _safe(st["sharpe"]),
                "total_return": _safe(st["total_return"]),
                "max_dd": _safe(st["max_dd"]),
                "annual_turnover": _safe(to["annual_turnover"]),
                "n_flips": int(to["n_flips"]),
            }
            print(f"    {freq_name:<14}  Sharpe {st['sharpe']:+.2f}   "
                  f"totRet {st['total_return']*100:+5.0f}%   "
                  f"DD {st['max_dd']*100:>4.1f}%   "
                  f"turnover/yr {to['annual_turnover']:>4.2f}   "
                  f"flips {to['n_flips']:>3d}")

            # Snapshot the headline (K=HEADLINE_K, HEADLINE_FREQ_NAME)
            if K == HEADLINE_K and freq_name == HEADLINE_FREQ_NAME:
                # Trade history
                trades = build_trade_history(r["weights"], breadths, eligible)
                headline_payload = {
                    "K": K,
                    "rebal_freq": freq_name,
                    "rebal_freq_code": freq_code,
                    "n_etfs": len(etfs_used),
                    "etfs_used": etfs_used,
                    "eligible_start": eligible.strftime("%Y-%m-%d"),
                    "headline_stats": grid[f"K={K}"][freq_name],
                    "headline_equity_dates": [d.strftime("%Y-%m-%d")
                                              for d in eq_window.index],
                    "headline_equity": round_series(eq_window.values),
                    "n_rebalances": len(trades),
                    "trade_history": trades,
                }

    # =====================================================================
    # 2. Side panel: K=5 weekly Friday equity (for chart overlay)
    # =====================================================================
    r5 = run_portfolio(closes, breadths, top_k_breadth_weight(5),
                       eligible, rebalance_freq="W-FRI")
    eq5 = r5["equity"].loc[r5["equity"].index >= eligible]
    eq5 = eq5 / eq5.iloc[0]

    # K=3 for visual comparison
    r3 = run_portfolio(closes, breadths, top_k_breadth_weight(3),
                       eligible, rebalance_freq="W-FRI")
    eq3 = r3["equity"].loc[r3["equity"].index >= eligible]
    eq3 = eq3 / eq3.iloc[0]

    # =====================================================================
    # 3. Headline portfolio: weight panel, attribution, weekly allocation
    # =====================================================================
    headline_run = run_portfolio(closes, breadths,
                                  top_k_breadth_weight(HEADLINE_K),
                                  eligible, rebalance_freq=HEADLINE_FREQ)
    headline_weights = headline_run["weights"].loc[headline_run["weights"].index >= eligible]

    # Per-ETF performance attribution
    # daily_ret_etf = closes.pct_change(); used_weight = yesterday's weight
    # daily contribution per ETF = used_weight * daily_ret_etf
    # sum over the in-window days → P&L contribution. Express also annualised.
    rets = closes.pct_change().fillna(0).loc[headline_weights.index]
    used_w = headline_weights.shift(1).fillna(0)
    daily_contrib = used_w * rets  # DataFrame: per-ETF daily contribution
    total_contrib_per_etf = daily_contrib.sum()  # additive contribution
    total_all = float(total_contrib_per_etf.sum())
    avg_ret_when_held = {}
    for etf in closes.columns:
        if etf not in daily_contrib.columns:
            continue
        # Average daily *ETF* return on days when we held this ETF
        held_mask = used_w[etf] > 1e-6
        n_held = int(held_mask.sum())
        if n_held == 0:
            avg_ret_when_held[etf] = {"days_held": 0, "ann_return_when_held": None}
        else:
            mean_daily = float(rets.loc[held_mask, etf].mean())
            ann_return = (1.0 + mean_daily) ** 252 - 1.0
            avg_ret_when_held[etf] = {
                "days_held": n_held,
                "ann_return_when_held": _safe(ann_return),
            }

    # Per-ETF time-in-portfolio + attribution
    attribution = {}
    for etf in closes.columns:
        if etf not in headline_weights.columns:
            continue
        held_days = int((headline_weights[etf] > 1e-6).sum())
        total_days = len(headline_weights)
        pnl = float(total_contrib_per_etf.get(etf, 0.0))
        attribution[etf] = {
            "days_held": held_days,
            "pct_of_days": round(held_days / total_days * 100, 1) if total_days else 0.0,
            "avg_weight_when_held": (
                round(float(headline_weights[etf][headline_weights[etf] > 1e-6].mean()), 4)
                if held_days else 0.0
            ),
            "ann_return_when_held": avg_ret_when_held[etf].get("ann_return_when_held"),
            "contribution_to_total_return": _safe(pnl),
            "pct_of_total_contribution": (
                round(pnl / total_all * 100, 1) if total_all != 0 else 0.0
            ),
        }

    # Weekly allocation snapshot (Fridays only) for stacked-area chart
    weekly_idx = headline_weights.index[headline_weights.index.dayofweek == 4]
    weekly_w = headline_weights.loc[weekly_idx]
    weekly_w = weekly_w.loc[(weekly_w.sum(axis=1) > 0.5)]  # skip warm-up

    # =====================================================================
    # 4. Benchmarks: SPY buy-and-hold and equal-weight all 11
    # =====================================================================
    print("\n=== Benchmarks ===")
    spy_close = download_spy_close(closes.index.min().strftime("%Y-%m-%d"),
                                    (closes.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d"))
    spy_close = spy_close.reindex(closes.index).ffill()
    spy_window = spy_close.loc[spy_close.index >= eligible]
    spy_eq = (spy_window / spy_window.iloc[0])
    spy_stats = compute_stats(spy_close, eligible)
    print(f"  SPY               Sharpe {spy_stats['sharpe']:+.2f}   "
          f"totRet {spy_stats['total_return']*100:+.0f}%   DD {spy_stats['max_dd']*100:.1f}%")

    ew_run = run_portfolio(closes, breadths, equal_weight_all_fn,
                            eligible, rebalance_freq=HEADLINE_FREQ)
    ew_eq = ew_run["equity"].loc[ew_run["equity"].index >= eligible]
    ew_eq = ew_eq / ew_eq.iloc[0]
    ew_stats = compute_stats(ew_run["equity"], eligible)
    print(f"  Equal-weight 11   Sharpe {ew_stats['sharpe']:+.2f}   "
          f"totRet {ew_stats['total_return']*100:+.0f}%   DD {ew_stats['max_dd']*100:.1f}%")

    benchmarks = {
        "spy_buy_hold": {
            "label": "SPY buy-and-hold",
            "dates": [d.strftime("%Y-%m-%d") for d in spy_eq.index],
            "equity": round_series(spy_eq.values),
            "sharpe": _safe(spy_stats["sharpe"]),
            "total_return": _safe(spy_stats["total_return"]),
            "max_dd": _safe(spy_stats["max_dd"]),
            "cagr": _safe(spy_stats.get("cagr")),
        },
        "equal_weight_11": {
            "label": "Equal-weight 11 ETFs (no signal)",
            "dates": [d.strftime("%Y-%m-%d") for d in ew_eq.index],
            "equity": round_series(ew_eq.values),
            "sharpe": _safe(ew_stats["sharpe"]),
            "total_return": _safe(ew_stats["total_return"]),
            "max_dd": _safe(ew_stats["max_dd"]),
            "cagr": _safe(ew_stats.get("cagr")),
        },
    }

    # Strategy stats — recompute with cagr included
    strat_stats_full = compute_stats(headline_run["equity"], eligible)
    headline_payload["headline_stats_full"] = {
        "sharpe": _safe(strat_stats_full["sharpe"]),
        "total_return": _safe(strat_stats_full["total_return"]),
        "max_dd": _safe(strat_stats_full["max_dd"]),
        "cagr": _safe(strat_stats_full.get("cagr")),
        "annual_turnover": headline_payload["headline_stats"]["annual_turnover"],
        "n_flips": headline_payload["headline_stats"]["n_flips"],
    }

    # =====================================================================
    # Output payload
    # =====================================================================
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "rebalance_freq_grid": grid,
        "headline": headline_payload,
        "k5_weekly_dates": [d.strftime("%Y-%m-%d") for d in eq5.index],
        "k5_weekly_equity": round_series(eq5.values),
        "k3_weekly_dates": [d.strftime("%Y-%m-%d") for d in eq3.index],
        "k3_weekly_equity": round_series(eq3.values),
        "headline_weekly_allocation_dates": [d.strftime("%Y-%m-%d")
                                               for d in weekly_w.index],
        "headline_weekly_allocation": {
            etf: round_series(weekly_w[etf].values, ndigits=4)
            for etf in weekly_w.columns
        },
        "headline_attribution": attribution,
        "benchmarks": benchmarks,
    }

    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    # Headline summary
    print()
    print("=" * 90)
    print(f"TOP-K ROTATION HEADLINE — K={HEADLINE_K}, {HEADLINE_FREQ_NAME} rebalance")
    print("=" * 90)
    h = headline_payload["headline_stats"]
    print(f"  Sharpe          : {h['sharpe']:+.2f}")
    print(f"  Total return    : {h['total_return']*100:+.1f}%")
    print(f"  Max drawdown    : {h['max_dd']*100:.1f}%")
    print(f"  Annual turnover : {h['annual_turnover']:.2f}")
    print(f"  Number of flips : {h['n_flips']}")
    print(f"  Number of rebals: {headline_payload['n_rebalances']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
