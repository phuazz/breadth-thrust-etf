"""Phase 4 experiment — test merge vs separate-sleeve architectures.

Builds the constituent-breadth top-K rotation engine on three new universes
in addition to the existing US sector universe (Strategy A), and compares
multiple combination architectures.

Inputs (must have been fetched + breadth-computed):
  - 14 US sector ETFs (current Strategy A universe)
  - 5 Stoxx Europe 600 sector ETFs (EXV1 Banks, EXH1 Oil&Gas, EXV3 Tech,
    EXH3 Industrials, EXH9 Utilities)
  - 4 country UCITS (IJPN Japan, NDIA India, ICHN China, ITWN Taiwan)

Variants tested:
  (1) Baseline: Strategy A (14 US ETFs, K=7 Weekly Fri)
  (2) Merge variant: Strategy A-Global (14 US + 5 Europe + 4 Countries
      = 23 ETFs, K=7 Weekly Fri)
  (3) Separate sleeve: Strategy D-Europe (5 EU sectors only, K=3)
  (4) Separate sleeve: Strategy E-Countries (4 country ETFs, K=2 or 3)
  (5) Multi-strategy combinations:
       - Current baseline:  45/45/10 A:B:C        (no Phase 4)
       - Add D as 4th:      35/35/10/20 A:B:C:D
       - Add E as 4th:      35/35/10/20 A:B:C:E
       - Add both:          30/30/10/15/15 A:B:C:D:E
       - Merged in A:       45/45/10 A-Global:B:C  (A replaced by merged)

Output: prints comparison table to stdout. Does NOT modify dashboard data.
Used as a research / decision tool before any dashboard publish.
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

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import (  # noqa: E402
    UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS, UNIVERSE_COUNTRIES, UNIVERSE_GLOBAL,
)
from run_portfolio import _build_panels_for, run_portfolio, top_k_breadth_weight  # noqa: E402
from run_improvements import compute_stats as _stats_full  # noqa: E402
from run_ma200_sweep import MA_PERIOD  # noqa: E402

# Phase 4 experiment will use the same headline conventions as Strategy A.
HEADLINE_FREQ = "W-FRI"


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def compute_stats_eq(equity: pd.Series, eligible: pd.Timestamp) -> dict:
    eq = equity.loc[equity.index >= eligible]
    if len(eq) < 5:
        return {}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = float(eq.iloc[-1] - 1.0)
    cagr = (eq.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else 0.0
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    return {
        "sharpe": float(sharpe),
        "cagr": float(cagr),
        "total_return": total,
        "max_dd": float(dd.min()),
    }


def _eligible_start(closes: pd.DataFrame, breadths: pd.DataFrame) -> pd.Timestamp:
    """Earliest date all ETFs in the panel have valid breadth."""
    first_valid = breadths.apply(lambda s: s.first_valid_index())
    latest = max(d for d in first_valid if d is not None)
    eligible_idx = closes.index.searchsorted(latest) + 1  # day after warmup
    return closes.index[min(eligible_idx, len(closes) - 1)]


def run_top_k(universe: list[str], K: int, label: str) -> dict:
    """Top-K-by-breadth weekly Friday on a given universe; returns equity + stats."""
    closes, breadths, used = _build_panels_for(universe)
    print(f"  [{label}]  universe={len(used)} ETFs, "
          f"closes shape {closes.shape}")
    eligible = _eligible_start(closes, breadths)
    print(f"            eligible from {eligible.date()}")
    r = run_portfolio(closes, breadths, top_k_breadth_weight(K), eligible,
                      rebalance_freq=HEADLINE_FREQ)
    eq = r["equity"]
    eq_window = eq.loc[eq.index >= eligible]
    eq_window = eq_window / eq_window.iloc[0]
    stats = compute_stats_eq(eq, eligible)
    print(f"            K={K}  Sharpe {stats['sharpe']:+.3f}  "
          f"CAGR {stats['cagr']*100:+5.1f}%  DD {stats['max_dd']*100:+5.1f}%")
    return {
        "equity": eq_window,
        "stats": stats,
        "eligible_start": eligible,
        "n_etfs": len(used),
        "etfs": used,
        "K": K,
    }


def blend_nway(equities: list[pd.Series], weights: list[float],
               rebal_freq: str = "W-FRI",
               cost: float = 5 / 10_000) -> pd.Series:
    """Generic n-way fixed-weight blend with weekly rebalance.

    Each sleeve gets weight w_i; weights sum to 1.0. Between rebalances each
    sleeve drifts with its own return; at rebal dates we snap back to target
    and pay 5 bps on absolute weight change.
    """
    assert abs(sum(weights) - 1.0) < 1e-6, f"weights must sum to 1.0 (got {sum(weights)})"
    # Intersect all sleeves to a common date index
    common = equities[0].index
    for eq in equities[1:]:
        common = common.intersection(eq.index)
    rets = [eq.loc[common].pct_change().fillna(0) for eq in equities]
    eqs_norm = [eq.loc[common] / eq.loc[common].iloc[0] for eq in equities]
    rebal_dates_target = pd.date_range(common[0], common[-1], freq=rebal_freq)
    rebal_dates = common[common.isin(rebal_dates_target)]
    blend_ret = pd.Series(0.0, index=common)
    ws = list(weights)
    for i, dt in enumerate(common):
        if i == 0:
            continue
        # Today's return = yesterday's weights × today's per-sleeve returns
        blend_ret.iloc[i] = sum(w * r.iloc[i] for w, r in zip(ws, rets))
        # Drift weights
        ws = [w * (1.0 + r.iloc[i]) for w, r in zip(ws, rets)]
        tot = sum(ws)
        if tot > 0:
            ws = [w / tot for w in ws]
        if dt in rebal_dates:
            turnover = sum(abs(w - tgt) for w, tgt in zip(ws, weights))
            blend_ret.iloc[i] -= turnover * cost
            ws = list(weights)
    return (1.0 + blend_ret).cumprod()


def load_strategy_eq(path: Path, dates_key="headline_equity_dates",
                       eq_key="headline_equity") -> pd.Series:
    blob = json.loads(path.read_text(encoding="utf-8"))
    h = blob["headline"]
    return pd.Series(h[eq_key], index=pd.to_datetime(h[dates_key]), dtype=float)


def main() -> int:
    print("=" * 78)
    print("PHASE 4 EXPERIMENT — Europe sector + Country breadth: merge vs sleeve")
    print("=" * 78)

    # --- Step 1: Run top-K on each universe ---
    print("\n[Step 1] Run top-K rotation on each universe individually")
    print()
    # Baseline (current Strategy A)
    a_current = run_top_k(UNIVERSE_ETFS, K=7, label="A current (14 US)")
    # Europe sectors only
    d_europe = run_top_k(UNIVERSE_EUROPE_SECTORS, K=3, label="D Europe sectors (5)")
    # Countries only
    e_countries = run_top_k(UNIVERSE_COUNTRIES, K=2, label="E Countries (4)")
    # Merged global (US + Europe + Countries)
    a_global = run_top_k(UNIVERSE_GLOBAL, K=7, label="A-Global merged (23)")

    # --- Step 2: Load existing Strategy B and C for combinations ---
    print("\n[Step 2] Load existing Strategy B (asset class) and C (thematic)")
    eq_b = load_strategy_eq(DATA_DIR / "asset_class_rotation.json")
    eq_c = load_strategy_eq(DATA_DIR / "thematic_rotation.json")
    print(f"  Strategy B: {eq_b.index[0].date()} -> {eq_b.index[-1].date()}, "
          f"{len(eq_b)} days")
    print(f"  Strategy C: {eq_c.index[0].date()} -> {eq_c.index[-1].date()}, "
          f"{len(eq_c)} days")

    # --- Step 3: Build combinations ---
    print("\n[Step 3] Build combinations and compare")
    print()
    eq_a_current = a_current["equity"]
    eq_a_global  = a_global["equity"]
    eq_d         = d_europe["equity"]
    eq_e         = e_countries["equity"]

    variants = []

    # Baseline: A only
    s = compute_stats_eq(eq_a_current, a_current["eligible_start"])
    variants.append({"label": "(0) Baseline: A-current alone (14 US, K=7)", **s})

    # Baseline blend: 45/45/10 A:B:C
    blend_baseline = blend_nway([eq_a_current, eq_b, eq_c], [0.45, 0.45, 0.10])
    s = compute_stats_eq(blend_baseline, blend_baseline.index[1])
    variants.append({"label": "(1) Baseline blend: 45/45/10 A:B:C (no Phase 4)", **s})

    # Europe sleeve added: 35/35/10/20 A:B:C:D
    blend_with_d = blend_nway([eq_a_current, eq_b, eq_c, eq_d],
                                [0.35, 0.35, 0.10, 0.20])
    s = compute_stats_eq(blend_with_d, blend_with_d.index[1])
    variants.append({"label": "(2) +Europe: 35/35/10/20 A:B:C:D", **s})

    # Lighter Europe sleeve: 40/35/10/15
    blend_with_d_light = blend_nway([eq_a_current, eq_b, eq_c, eq_d],
                                      [0.40, 0.35, 0.10, 0.15])
    s = compute_stats_eq(blend_with_d_light, blend_with_d_light.index[1])
    variants.append({"label": "(3) +Europe light: 40/35/10/15 A:B:C:D", **s})

    # Countries sleeve added: 35/35/10/20 A:B:C:E
    blend_with_e = blend_nway([eq_a_current, eq_b, eq_c, eq_e],
                                [0.35, 0.35, 0.10, 0.20])
    s = compute_stats_eq(blend_with_e, blend_with_e.index[1])
    variants.append({"label": "(4) +Countries: 35/35/10/20 A:B:C:E", **s})

    # Countries lighter
    blend_with_e_light = blend_nway([eq_a_current, eq_b, eq_c, eq_e],
                                      [0.40, 0.35, 0.10, 0.15])
    s = compute_stats_eq(blend_with_e_light, blend_with_e_light.index[1])
    variants.append({"label": "(5) +Countries light: 40/35/10/15 A:B:C:E", **s})

    # Both sleeves: 30/30/10/15/15 A:B:C:D:E
    blend_full = blend_nway([eq_a_current, eq_b, eq_c, eq_d, eq_e],
                              [0.30, 0.30, 0.10, 0.15, 0.15])
    s = compute_stats_eq(blend_full, blend_full.index[1])
    variants.append({"label": "(6) +Both: 30/30/10/15/15 A:B:C:D:E", **s})

    # Merge variant: replace A with A-Global in 45/45/10
    blend_global = blend_nway([eq_a_global, eq_b, eq_c], [0.45, 0.45, 0.10])
    s = compute_stats_eq(blend_global, blend_global.index[1])
    variants.append({"label": "(7) MERGE: 45/45/10 A-Global:B:C "
                              "(23 ETFs in A)", **s})

    # Print comparison
    print("\n" + "=" * 90)
    print(f"{'Variant':<60} {'Sharpe':>8} {'CAGR':>8} {'Tot Ret':>8} {'Max DD':>8}")
    print("=" * 90)
    baseline_sh = variants[1].get("sharpe") or 0.0
    for v in variants:
        sh = v.get("sharpe", float("nan"))
        cagr = v.get("cagr", float("nan"))
        tot = v.get("total_return", float("nan"))
        dd = v.get("max_dd", float("nan"))
        d = sh - baseline_sh
        flag = ""
        if v["label"].startswith("(1)"):
            flag = " <- baseline"
        elif d > 0.01:
            flag = f"  WINS ({d:+.3f} vs baseline)"
        elif d < -0.01:
            flag = f"  loses ({d:+.3f} vs baseline)"
        else:
            flag = f"  flat ({d:+.3f})"
        print(f"{v['label']:<60} {sh:+7.3f}  {cagr*100:+6.1f}% "
              f"{tot*100:+7.0f}% {dd*100:+6.1f}%{flag}")

    print("\n=== Decision summary ===")
    # Find the best Sharpe variant (excluding baseline A-only)
    best = max(variants[1:], key=lambda v: v.get("sharpe", -1e9))
    print(f"Best Sharpe: {best['label']}  ({best['sharpe']:+.3f})")
    print(f"vs baseline 45/45/10:  {best['sharpe'] - baseline_sh:+.3f} Sharpe delta")
    return 0


if __name__ == "__main__":
    sys.exit(main())
