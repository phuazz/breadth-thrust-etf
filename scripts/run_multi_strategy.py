"""Multi-strategy combination — Strategy A (US sector breadth rotation) +
Strategy B (asset-class momentum rotation).

Loads the headline equity curves from both strategies and constructs three
combination variants:

  1. Fixed 70/30 blend  — 70% Strategy A + 30% Strategy B, rebalanced weekly
  2. Fixed 50/50 blend  — equal weight, rebalanced weekly
  3. Meta-rotation      — at each Friday close, allocate 100% to whichever
                          of {A, B} has the higher trailing 6-month Sharpe.
                          Switches no more than once per refit date.

All blends are computed on the common date window (intersection of the
two strategies' equity curves).

Output: data/multi_strategy.json
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
TOPK_PATH = DATA_DIR / "topk_robustness.json"          # Strategy A
ASSET_PATH = DATA_DIR / "asset_class_rotation.json"    # Strategy B
OUT_PATH = DATA_DIR / "multi_strategy.json"

sys.stdout.reconfigure(encoding="utf-8")


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


def equity_from_blob(blob: dict, dates_key: str, equity_key: str) -> pd.Series:
    dates = pd.to_datetime(blob[dates_key])
    eq = pd.Series(blob[equity_key], index=dates, dtype=float)
    return eq.sort_index()


def compute_stats(equity: pd.Series) -> dict:
    eq = equity.copy()
    if len(eq) < 2:
        return {"sharpe": None, "cagr": None, "total_return": None, "max_dd": None}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    total_ret = float(eq.iloc[-1] - 1.0)
    cagr = (float(eq.iloc[-1]) ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(cagr),
        "total_return": _safe(total_ret),
        "max_dd": _safe(float(dd.min())),
    }


def fixed_blend(eq_a: pd.Series, eq_b: pd.Series, w_a: float,
                  rebal_freq: str = "W-FRI",
                  cost: float = 5 / 10_000) -> pd.Series:
    """Fixed-weight blend of two equity curves, rebalanced at rebal_freq.

    Mechanics: each Friday close, target weights are (w_a, 1-w_a). Between
    rebalances, the per-strategy capital evolves with the underlying
    strategy's daily return. At each rebal date we snap back to target.
    Cost = 5 bps on absolute weight change at each rebal.
    """
    common = eq_a.index.intersection(eq_b.index)
    eq_a = eq_a.loc[common]
    eq_b = eq_b.loc[common]
    ret_a = eq_a.pct_change().fillna(0)
    ret_b = eq_b.pct_change().fillna(0)
    rebal_dates_target = pd.date_range(common[0], common[-1], freq=rebal_freq)
    rebal_dates = common[common.isin(rebal_dates_target)]
    blend_ret = pd.Series(0.0, index=common)
    wa = w_a
    wb = 1.0 - w_a
    for i, dt in enumerate(common):
        if i == 0:
            blend_ret.iloc[0] = 0.0
            continue
        # Today's return: yesterday's weights x today's returns
        blend_ret.iloc[i] = wa * ret_a.iloc[i] + wb * ret_b.iloc[i]
        # Drift the weights with the returns
        wa = wa * (1.0 + ret_a.iloc[i])
        wb = wb * (1.0 + ret_b.iloc[i])
        tot = wa + wb
        if tot > 0:
            wa, wb = wa / tot, wb / tot
        # If this is a rebalance day, snap back to target and pay cost
        if dt in rebal_dates:
            turnover = abs(wa - w_a) + abs(wb - (1.0 - w_a))
            blend_ret.iloc[i] -= turnover * cost
            wa = w_a
            wb = 1.0 - w_a
    return (1.0 + blend_ret).cumprod()


def meta_rotation(eq_a: pd.Series, eq_b: pd.Series,
                    lookback_days: int = 126,
                    refit_freq: str = "W-FRI",
                    cost: float = 5 / 10_000) -> pd.Series:
    """Allocate 100% to whichever of {A, B} has the higher trailing
    `lookback_days` Sharpe. Re-decide each Friday."""
    common = eq_a.index.intersection(eq_b.index)
    eq_a = eq_a.loc[common]
    eq_b = eq_b.loc[common]
    ret_a = eq_a.pct_change().fillna(0)
    ret_b = eq_b.pct_change().fillna(0)
    refit_dates_target = pd.date_range(common[0], common[-1], freq=refit_freq)
    refit_dates = common[common.isin(refit_dates_target)]
    alloc = pd.Series("A", index=common)  # current choice
    current = "A"
    for dt in refit_dates:
        end_idx = common.get_loc(dt)
        start_idx = max(0, end_idx - lookback_days)
        win_a = ret_a.iloc[start_idx:end_idx + 1]
        win_b = ret_b.iloc[start_idx:end_idx + 1]
        sh_a = (win_a.mean() / win_a.std() * math.sqrt(252)
                if win_a.std() > 0 else 0.0)
        sh_b = (win_b.mean() / win_b.std() * math.sqrt(252)
                if win_b.std() > 0 else 0.0)
        if sh_b > sh_a:
            current = "B"
        else:
            current = "A"
        alloc.iloc[end_idx:] = current
    # Apply: use yesterday's allocation x today's return
    alloc_shift = alloc.shift(1).bfill()
    blend_ret = pd.Series(0.0, index=common)
    for i in range(1, len(common)):
        if alloc_shift.iloc[i] == "A":
            blend_ret.iloc[i] = ret_a.iloc[i]
        else:
            blend_ret.iloc[i] = ret_b.iloc[i]
        # Cost on switch
        if i > 0 and alloc_shift.iloc[i] != alloc_shift.iloc[i - 1]:
            blend_ret.iloc[i] -= 1.0 * cost  # 100% turnover on full switch
    return (1.0 + blend_ret).cumprod(), alloc


def main() -> int:
    if not TOPK_PATH.exists() or not ASSET_PATH.exists():
        print(f"ERROR: missing input. Run run_topk_robustness.py and "
              f"run_asset_class_rotation.py first.", file=sys.stderr)
        return 1

    print("Loading Strategy A (US sector breadth rotation) ...")
    a_blob = json.loads(TOPK_PATH.read_text(encoding="utf-8"))
    eq_a = equity_from_blob(a_blob["headline"], "headline_equity_dates",
                             "headline_equity")
    sa = compute_stats(eq_a)
    print(f"  K={a_blob['headline']['K']}, {a_blob['headline']['rebal_freq']} -> "
          f"Sharpe {sa['sharpe']:+.2f}, CAGR {sa['cagr']*100:+.1f}%, "
          f"DD {sa['max_dd']*100:.1f}%, "
          f"{eq_a.index[0].date()} -> {eq_a.index[-1].date()}")

    print("Loading Strategy B (asset-class momentum rotation) ...")
    b_blob = json.loads(ASSET_PATH.read_text(encoding="utf-8"))
    eq_b = equity_from_blob(b_blob["headline"], "headline_equity_dates",
                             "headline_equity")
    sb = compute_stats(eq_b)
    print(f"  K={b_blob['headline']['K']}, {b_blob['headline']['rebal_freq']} -> "
          f"Sharpe {sb['sharpe']:+.2f}, CAGR {sb['cagr']*100:+.1f}%, "
          f"DD {sb['max_dd']*100:.1f}%, "
          f"{eq_b.index[0].date()} -> {eq_b.index[-1].date()}")

    common = eq_a.index.intersection(eq_b.index)
    print(f"\nCommon window: {common[0].date()} -> {common[-1].date()} "
          f"({len(common)} trading days, "
          f"{(common[-1] - common[0]).days/365.25:.1f} years)")

    # Renormalise both to start = 1.0 in the common window
    eq_a_norm = (eq_a.loc[common] / eq_a.loc[common].iloc[0])
    eq_b_norm = (eq_b.loc[common] / eq_b.loc[common].iloc[0])

    print("\n=== Combinations ===")
    blend_70_30 = fixed_blend(eq_a_norm, eq_b_norm, 0.70)
    s7030 = compute_stats(blend_70_30)
    print(f"  70/30 blend  Sharpe {s7030['sharpe']:+.2f}  "
          f"CAGR {s7030['cagr']*100:+.1f}%  DD {s7030['max_dd']*100:.1f}%")

    blend_50_50 = fixed_blend(eq_a_norm, eq_b_norm, 0.50)
    s5050 = compute_stats(blend_50_50)
    print(f"  50/50 blend  Sharpe {s5050['sharpe']:+.2f}  "
          f"CAGR {s5050['cagr']*100:+.1f}%  DD {s5050['max_dd']*100:.1f}%")

    blend_30_70 = fixed_blend(eq_a_norm, eq_b_norm, 0.30)
    s3070 = compute_stats(blend_30_70)
    print(f"  30/70 blend  Sharpe {s3070['sharpe']:+.2f}  "
          f"CAGR {s3070['cagr']*100:+.1f}%  DD {s3070['max_dd']*100:.1f}%")

    meta_eq, alloc = meta_rotation(eq_a_norm, eq_b_norm, lookback_days=126)
    smeta = compute_stats(meta_eq)
    a_pct = float((alloc == "A").mean()) * 100
    print(f"  Meta-rotation (126d Sharpe lookback)  Sharpe {smeta['sharpe']:+.2f}  "
          f"CAGR {smeta['cagr']*100:+.1f}%  DD {smeta['max_dd']*100:.1f}%  "
          f"(A: {a_pct:.0f}% of days)")

    # Re-stat A and B on the common window for fair comparison
    sa_cw = compute_stats(eq_a_norm)
    sb_cw = compute_stats(eq_b_norm)

    print(f"\n  (Re-stat on common window {common[0].date()} -> {common[-1].date()}:)")
    print(f"  Strategy A   Sharpe {sa_cw['sharpe']:+.2f}  "
          f"CAGR {sa_cw['cagr']*100:+.1f}%  DD {sa_cw['max_dd']*100:.1f}%")
    print(f"  Strategy B   Sharpe {sb_cw['sharpe']:+.2f}  "
          f"CAGR {sb_cw['cagr']*100:+.1f}%  DD {sb_cw['max_dd']*100:.1f}%")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "common_start": common[0].strftime("%Y-%m-%d"),
        "common_end": common[-1].strftime("%Y-%m-%d"),
        "strategies": {
            "strategy_a": {
                "label": ("Strategy A: US sector top-K breadth rotation "
                          f"(K={a_blob['headline']['K']}, "
                          f"{a_blob['headline']['rebal_freq']})"),
                "dates": [d.strftime("%Y-%m-%d") for d in eq_a_norm.index],
                "equity": round_series(eq_a_norm.values),
                **sa_cw,
            },
            "strategy_b": {
                "label": ("Strategy B: Asset-class top-K momentum rotation "
                          f"(K={b_blob['headline']['K']}, "
                          f"{b_blob['headline']['rebal_freq']})"),
                "dates": [d.strftime("%Y-%m-%d") for d in eq_b_norm.index],
                "equity": round_series(eq_b_norm.values),
                **sb_cw,
            },
            "blend_70_30": {
                "label": "70% A + 30% B (fixed weekly rebalance)",
                "dates": [d.strftime("%Y-%m-%d") for d in blend_70_30.index],
                "equity": round_series(blend_70_30.values),
                **s7030,
            },
            "blend_50_50": {
                "label": "50% A + 50% B (fixed weekly rebalance)",
                "dates": [d.strftime("%Y-%m-%d") for d in blend_50_50.index],
                "equity": round_series(blend_50_50.values),
                **s5050,
            },
            "blend_30_70": {
                "label": "30% A + 70% B (fixed weekly rebalance)",
                "dates": [d.strftime("%Y-%m-%d") for d in blend_30_70.index],
                "equity": round_series(blend_30_70.values),
                **s3070,
            },
            "meta_rotation": {
                "label": "Meta-rotation: pick A or B each week by 126d Sharpe",
                "dates": [d.strftime("%Y-%m-%d") for d in meta_eq.index],
                "equity": round_series(meta_eq.values),
                "pct_in_A": round(a_pct, 1),
                **smeta,
            },
        },
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
