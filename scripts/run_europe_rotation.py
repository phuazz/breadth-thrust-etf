"""Strategy D — Europe sector top-K-by-breadth rotation.

Same constituent-breadth engine as Strategy A, but on 5 Stoxx Europe 600
sector UCITS funds (Banks, Oil & Gas, Technology, Industrials, Utilities).

After the 2026-05-24 compute_ma200_breadth fix (allowing 10% sparse
missingness in the rolling window, required because non-US constituents
have ~1-2% missing days that would otherwise nuke the MA200), the Phase 4
experiment showed adding this as a separate 20% sleeve to the 45/45/10
A:B:C baseline improves Sharpe from +1.082 to +1.152 (CAGR +14.9% to
+15.1%) at the cost of ~2.3pp higher max drawdown (-21.5% to -23.8%).
The Sharpe improvement is meaningful at the 20% sleeve weight; the DD
cost is the honest price of adding equity beta.

Standalone Strategy D (common window 2018-11 → 2026-05): Sharpe +0.93,
CAGR +14.9%, DD -32.0% — better Sharpe than SPY's +0.77 on the same
window. Europe's distinct macro cycle (ECB rates, EUR/USD, China
trade exposure) provides genuine orthogonality at the blend level.

Output: data/europe_rotation.json (mirrors topk_robustness.json structure
so the dashboard renderer can reuse Strategy A's code paths).
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
OUT_PATH = DATA_DIR / "europe_rotation.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import UNIVERSE_EUROPE_SECTORS  # noqa: E402
from run_portfolio import _build_panels_for, run_portfolio, top_k_breadth_weight  # noqa: E402
from run_improvements import compute_stats  # noqa: E402
from run_ma200_sweep import MA_PERIOD  # noqa: E402
from backtest import download_spy_close  # noqa: E402

K_GRID = [2, 3, 4]
REBAL_FREQS = [
    ("Daily",         "D"),
    ("Weekly Fri",    "W-FRI"),
    ("Bi-weekly Fri", "2W-FRI"),
    ("Month-end",     "BME"),
]
HEADLINE_K = 3
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


def turnover_stats(weight_panel: pd.DataFrame, eligible_start: pd.Timestamp) -> dict:
    wp = weight_panel.loc[weight_panel.index >= eligible_start].copy()
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    return {
        "annual_turnover": float(diff.sum() / n_years) if n_years > 0 else 0.0,
        "n_flips": int((diff > 1e-6).sum()),
    }


def build_trade_history(weight_panel: pd.DataFrame, breadth_panel: pd.DataFrame,
                          eligible: pd.Timestamp) -> list[dict]:
    """Per-rebalance holdings, records prior-trading-day breadth (decision-time
    value, so share-math reproduces weights exactly)."""
    wp = weight_panel.loc[weight_panel.index >= eligible].copy()
    bp = breadth_panel.reindex(wp.index, method="ffill")
    full_idx = list(wp.index)
    out: list[dict] = []
    prev: pd.Series | None = None
    for i, (dt, row) in enumerate(wp.iterrows()):
        if prev is None or not np.allclose(row.values, prev.values, atol=1e-6):
            non_zero = row[row > 1e-6].sort_values(ascending=False)
            if len(non_zero) == 0:
                prev = row
                continue
            decision_date = full_idx[i - 1] if i > 0 else full_idx[i]
            holdings = []
            for etf, w in non_zero.items():
                b_val = bp.loc[decision_date, etf] if etf in bp.columns else None
                holdings.append({
                    "etf": etf,
                    "weight": round(float(w), 4),
                    "breadth_pct": round(float(b_val) * 100, 1) if b_val == b_val else None,
                })
            out.append({"date": dt.strftime("%Y-%m-%d"), "holdings": holdings})
            prev = row
    return out


def main() -> int:
    print(f"Building Europe sector breadth panels for {len(UNIVERSE_EUROPE_SECTORS)} ETFs ...",
          flush=True)
    closes, breadths, etfs_used = _build_panels_for(UNIVERSE_EUROPE_SECTORS)
    print(f"  {len(etfs_used)} ETFs used: {etfs_used}")
    if not etfs_used:
        print("ERROR: no usable ETFs in Europe universe")
        return 1

    # Eligible start = latest first-valid date + MA period
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

    # K x cadence grid
    print(f"\n=== K × cadence sensitivity (Strategy D: Europe sectors) ===")
    grid: dict[str, dict[str, dict]] = {}
    headline_payload: dict | None = None
    for K in K_GRID:
        grid[f"K={K}"] = {}
        print(f"\n  --- K = {K} ---")
        for freq_name, freq_code in REBAL_FREQS:
            r = run_portfolio(closes, breadths, top_k_breadth_weight(K),
                              eligible, rebalance_freq=freq_code)
            eq_window = r["equity"].loc[r["equity"].index >= eligible]
            if len(eq_window) > 0:
                eq_window = eq_window / eq_window.iloc[0]
            st = compute_stats(r["equity"], eligible)
            to = turnover_stats(r["weights"], eligible)
            grid[f"K={K}"][freq_name] = {
                "sharpe": _safe(st["sharpe"]),
                "cagr": _safe(st.get("cagr")),
                "total_return": _safe(st["total_return"]),
                "max_dd": _safe(st["max_dd"]),
                "annual_turnover": _safe(to["annual_turnover"]),
                "n_flips": int(to["n_flips"]),
            }
            print(f"    {freq_name:<14}  Sharpe {st['sharpe']:+.2f}   "
                  f"totRet {st['total_return']*100:+5.0f}%   "
                  f"DD {st['max_dd']*100:>4.1f}%   "
                  f"turnover/yr {to['annual_turnover']:>4.2f}")
            if K == HEADLINE_K and freq_name == HEADLINE_FREQ_NAME:
                trades = build_trade_history(r["weights"], breadths, eligible)

                rets = closes.pct_change().fillna(0).loc[r["weights"].index]
                rets = rets.loc[rets.index >= eligible]
                used_w = r["weights"].loc[rets.index].shift(1).fillna(0)
                daily_contrib = used_w * rets
                total_contrib = daily_contrib.sum()
                total_all = float(total_contrib.sum())
                attribution = {}
                for etf in closes.columns:
                    if etf not in daily_contrib.columns:
                        continue
                    held_mask = used_w[etf] > 1e-6
                    n_held = int(held_mask.sum())
                    total_days = len(used_w)
                    if n_held == 0:
                        ann_ret, avg_w = None, 0.0
                    else:
                        mean_daily = float(rets.loc[held_mask, etf].mean())
                        ann_ret = (1.0 + mean_daily) ** 252 - 1.0
                        avg_w = float(used_w[etf][held_mask].mean())
                    pnl = float(total_contrib.get(etf, 0.0))
                    attribution[etf] = {
                        "days_held": n_held,
                        "pct_of_days": round(n_held / total_days * 100, 1)
                                          if total_days else 0.0,
                        "avg_weight_when_held": round(avg_w, 4),
                        "ann_return_when_held": _safe(ann_ret),
                        "contribution_to_total_return": _safe(pnl),
                        "pct_of_total_contribution": (
                            round(pnl / total_all * 100, 1)
                            if total_all != 0 else 0.0
                        ),
                    }

                weekly_idx = r["weights"].index[r["weights"].index.dayofweek == 4]
                weekly_w = r["weights"].loc[weekly_idx]
                weekly_w = weekly_w.loc[(weekly_w.sum(axis=1) > 0.5)]

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
                    "attribution": attribution,
                    "weekly_allocation_dates": [d.strftime("%Y-%m-%d")
                                                  for d in weekly_w.index],
                    "weekly_allocation": {
                        etf: round_series(weekly_w[etf].values)
                        for etf in weekly_w.columns
                    },
                }

    print("\n=== Benchmarks (Europe sleeve vs SPY) ===")
    spy_close = download_spy_close(closes.index.min().strftime("%Y-%m-%d"),
                                    (closes.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d"))
    spy_close = spy_close.reindex(closes.index).ffill()
    spy_window = spy_close.loc[spy_close.index >= eligible]
    spy_eq = (spy_window / spy_window.iloc[0])
    spy_stats = compute_stats(spy_close, eligible)
    print(f"  SPY                Sharpe {spy_stats['sharpe']:+.2f}   "
          f"totRet {spy_stats['total_return']*100:+.0f}%   DD {spy_stats['max_dd']*100:.1f}%")

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
    }

    # Per-ETF colour palette (consistent with Strategy A)
    europe_colours = {
        "EXV1": "#1351b4",  # blue (Banks)
        "EXH1": "#b76e00",  # amber (Oil & Gas)
        "EXV3": "#7c3aed",  # purple (Technology)
        "EXH3": "#a16207",  # bronze (Industrials)
        "EXH9": "#0e7490",  # teal (Utilities)
    }

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": [
            {"etf": t, "label": _etf_label(t), "sector": _etf_sector(t)}
            for t in etfs_used
        ],
        "ma_period": MA_PERIOD,
        "rebalance_freq_grid": grid,
        "headline": headline_payload,
        "benchmarks": benchmarks,
        "europe_colours": europe_colours,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    print()
    print("=" * 90)
    print(f"STRATEGY D EUROPE HEADLINE — K={HEADLINE_K}, {HEADLINE_FREQ_NAME}")
    print("=" * 90)
    s = headline_payload["headline_stats"]
    print(f"  Sharpe          : {s['sharpe']:+.2f}")
    print(f"  CAGR            : {s['cagr']*100:+.1f}%")
    print(f"  Total return    : {s['total_return']*100:+.1f}%")
    print(f"  Max drawdown    : {s['max_dd']*100:.1f}%")
    print(f"  Annual turnover : {s['annual_turnover']:.2f}")
    return 0


def _etf_label(t):
    return {
        "EXV1": "STOXX Europe 600 Banks",
        "EXH1": "STOXX Europe 600 Oil & Gas",
        "EXV3": "STOXX Europe 600 Technology",
        "EXH3": "STOXX Europe 600 Industrial Goods & Services",
        "EXH9": "STOXX Europe 600 Utilities",
    }.get(t, t)


def _etf_sector(t):
    return {
        "EXV1": "Banks", "EXH1": "Energy", "EXV3": "Technology",
        "EXH3": "Industrials", "EXH9": "Utilities",
    }.get(t, "")


if __name__ == "__main__":
    sys.exit(main())
