"""Idea 1 (Phase 19 candidate) — aggregate market-breadth regime filter.

Apply a CSP1 (S&P 500) constituent-breadth gate on top of the deployed
35/35/10/20 A:B:C:D blend. When market breadth (% of S&P 500
constituents above their own 200d MA) collapses below the RISK_OFF
threshold, switch the entire portfolio to IEF (7-10y Treasury). When
breadth recovers above the RISK_ON threshold, switch back to the blend.
Hysteresis (different thresholds for entering vs exiting RISK_OFF)
prevents whipsaw around the boundary.

This file is a one-off backtest / proposal. If the lift is material it
gets integrated into run_multi_strategy.py in a follow-up commit, with
the gated variant added as a new entry in data/multi_strategy.json.

Usage:
    python scripts/run_regime_gate.py

Outputs:
    Prints a comparison table (gated vs ungated): Sharpe, CAGR, max DD,
    annual turnover, n regime-switch events. Also dumps gate trajectory
    to data/regime_gate_diagnostic.json for inspection.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "regime_gate_diagnostic.json"

# Hysteresis thresholds. CSP1 ma_breadth empirical distribution over
# 2018-2026: median ~0.60, p10 ~0.30, p90 ~0.85.
RISK_OFF_THRESHOLD = 0.30
RISK_ON_THRESHOLD = 0.50

# Round-trip transaction cost when switching state (blend <-> IEF).
# Conservative: 5 bps per switch (one-way) × 2 switches per round trip.
SWITCH_COST_BPS = 5

# Parameter sweep — investigate Sharpe vs DD trade-off space.
# Each row: (off_threshold, on_threshold, derisk_fraction)
# derisk_fraction: how much of the portfolio moves to IEF when RISK_OFF.
#   1.0 = full move to IEF (current default)
#   0.5 = 50% IEF + 50% blend (partial de-risk, smaller Sharpe drag)
# Threshold pairs span tighter (whipsaw risk) to wider (slower to defend).
SWEEP_PARAMS = [
    # (off, on, derisk) — variants to compare
    (0.30, 0.50, 1.0),   # baseline
    (0.30, 0.50, 0.5),   # partial de-risk only
    (0.25, 0.55, 1.0),   # wider hysteresis
    (0.25, 0.55, 0.5),
    (0.20, 0.50, 1.0),   # only triggers in real crashes
    (0.20, 0.50, 0.5),
    (0.30, 0.60, 1.0),   # slower re-entry
    (0.30, 0.60, 0.5),
    (0.35, 0.55, 1.0),   # more sensitive risk-off
    (0.35, 0.55, 0.5),
    (0.25, 0.60, 1.0),   # widest hysteresis
    (0.25, 0.60, 0.5),
]


def _compute_regime_states(breadth: pd.Series,
                            off_thresh: float,
                            on_thresh: float) -> pd.Series:
    """Walk-forward regime detection with hysteresis. Returns 1.0
    when RISK_ON, 0.0 when RISK_OFF. Signal lagged 1 day downstream."""
    states = []
    state = 1.0  # start risk-on
    for v in breadth.values:
        if pd.isna(v):
            states.append(state)
            continue
        if state == 1.0 and v < off_thresh:
            state = 0.0
        elif state == 0.0 and v > on_thresh:
            state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index, dtype=float)


def _round(x: float, n: int = 4) -> float:
    return round(float(x), n) if x is not None and not pd.isna(x) else None


def _stats(daily_ret: pd.Series, eq: pd.Series, label: str) -> dict:
    mu = daily_ret.mean()
    sigma = daily_ret.std()
    ann_ret = (1 + mu) ** 252 - 1
    ann_vol = sigma * np.sqrt(252)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
    peak = eq.cummax()
    dd = eq / peak - 1
    maxdd = dd.min()
    total = eq.iloc[-1] / eq.iloc[0] - 1
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (1 + total) ** (1 / n_years) - 1 if n_years > 0 else 0
    return {
        "label": label, "sharpe": _round(sharpe),
        "cagr": _round(cagr), "total_return": _round(total),
        "max_dd": _round(maxdd), "n_years": _round(n_years, 2),
    }


def run_gate_variant(blend_ret: pd.Series, blend_eq: pd.Series,
                      ief_ret: pd.Series, breadth: pd.Series,
                      off: float, on: float, derisk: float) -> dict:
    """Run a single gate variant and return its stats + diagnostics."""
    states = _compute_regime_states(breadth, off, on)
    states_lagged = states.shift(1).fillna(1.0)
    state_changes = states_lagged.diff().fillna(0).abs()
    switch_cost = state_changes * (SWITCH_COST_BPS / 10000.0)
    # When RISK_ON: 100% blend, 0% IEF.
    # When RISK_OFF: (1-derisk) blend + derisk IEF.
    blend_weight = states_lagged + (1.0 - states_lagged) * (1.0 - derisk)
    ief_weight = (1.0 - states_lagged) * derisk
    gated_ret = blend_weight * blend_ret + ief_weight * ief_ret - switch_cost
    gated_eq = (1.0 + gated_ret).cumprod()
    n_switches = int(state_changes.sum())
    days_off = int((states_lagged == 0).sum())
    return {
        "off": off, "on": on, "derisk": derisk,
        "stats": _stats(gated_ret, gated_eq,
                          f"off={int(off*100)}% on={int(on*100)}% "
                          f"derisk={int(derisk*100)}%"),
        "n_switches": n_switches,
        "pct_days_off": _round(days_off / len(states_lagged) * 100, 1),
    }


def main() -> int:
    print("Loading deployed-blend equity + CSP1 breadth + IEF prices ...")
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    blend = multi["strategies"]["blend_35_35_10_20"]
    blend_dates = pd.to_datetime(blend["dates"])
    blend_eq = pd.Series(blend["equity"], index=blend_dates, name="blend")

    csp1 = json.loads((DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"]),
                         name="breadth").dropna()

    ac_cache = pd.read_parquet(DATA_DIR / "asset_class_prices_cache.parquet")
    ief = ac_cache["IEF"].dropna()

    common_idx = blend_eq.index
    breadth_aligned = breadth.reindex(common_idx, method="ffill")
    ief_aligned = ief.reindex(common_idx, method="ffill")
    blend_ret = blend_eq.pct_change().fillna(0)
    ief_ret = ief_aligned.pct_change().fillna(0)

    s_ungated = _stats(blend_ret, blend_eq, "blend 35/35/10/20 (ungated)")

    print()
    print(f"BASELINE  ungated blend:  Sharpe={s_ungated['sharpe']:+.4f}  "
          f"CAGR={s_ungated['cagr']*100:+.1f}%  "
          f"DD={s_ungated['max_dd']*100:.2f}%")

    print("\n" + "=" * 96)
    print(f"REGIME-GATE PARAMETER SWEEP — 12 variants on "
          f"{blend_eq.index[0].date()} -> {blend_eq.index[-1].date()}")
    print("=" * 96)
    print(f"  {'OFF':>4s} {'ON':>4s} {'derisk':>6s}   "
          f"{'Sharpe':>8s} {'vs ungated':>14s}   "
          f"{'CAGR':>7s}   {'Max DD':>8s} {'vs ungated':>14s}   "
          f"{'switches':>8s} {'%off':>6s}")
    print(f"  {'---':>4s} {'---':>4s} {'------':>6s}   "
          f"{'------':>8s} {'------------':>14s}   "
          f"{'------':>7s}   {'------':>8s} {'------------':>14s}   "
          f"{'-------':>8s} {'---':>6s}")

    variants = []
    for off, on, derisk in SWEEP_PARAMS:
        v = run_gate_variant(blend_ret, blend_eq, ief_ret,
                              breadth_aligned, off, on, derisk)
        s = v["stats"]
        d_sh = (s["sharpe"] or 0) - (s_ungated["sharpe"] or 0)
        d_dd = (s["max_dd"] or 0) - (s_ungated["max_dd"] or 0)
        # Mark variants that look attractive (preserve Sharpe within
        # 0.05 and improve DD by 3pp+) with an asterisk.
        attractive = (d_sh > -0.05) and (d_dd > 0.03)
        mark = " *" if attractive else "  "
        print(f"  {int(off*100):>3d}% {int(on*100):>3d}% {int(derisk*100):>5d}% "
              f"  {s['sharpe']:+.4f} {d_sh:+13.4f}   "
              f"{s['cagr']*100:+6.1f}%   "
              f"{s['max_dd']*100:+7.2f}% {d_dd*100:+13.2f}pp   "
              f"{v['n_switches']:>8d} {v['pct_days_off']:>5.1f}%{mark}")
        variants.append({**v, "delta_sharpe": _round(d_sh),
                          "delta_dd_pp": _round(d_dd * 100, 2)})

    # Sort by an attractiveness score (Sharpe preservation × DD reduction)
    print(f"\n{'=' * 96}")
    print(f"RANKED BY (Sharpe-preservation) × (DD-improvement-in-pp)")
    print(f"{'=' * 96}")
    scored = []
    for v in variants:
        d_sh = v["delta_sharpe"] or 0
        d_dd = v["delta_dd_pp"] or 0
        # Score: lift DD by 1pp = +1 unit; lose 1 unit of Sharpe = -1 unit
        # (with cap on lower bound — collapsing Sharpe is unacceptable)
        score = d_dd / 10 + d_sh  # 10pp DD improvement == 1.0 Sharpe lift
        if (v["stats"]["sharpe"] or 0) < (s_ungated["sharpe"] - 0.15):
            score -= 1  # penalty for Sharpe collapse
        scored.append({**v, "score": _round(score)})
    scored.sort(key=lambda x: -x["score"])
    for i, v in enumerate(scored[:6]):
        s = v["stats"]
        print(f"  #{i+1}  off={int(v['off']*100)}%  on={int(v['on']*100)}%  "
              f"derisk={int(v['derisk']*100)}%  "
              f"score={v['score']:+.3f}  Sharpe={s['sharpe']:+.4f} "
              f"(d{v['delta_sharpe']:+.3f})  "
              f"DD={s['max_dd']*100:.2f}% (d{v['delta_dd_pp']:+.1f}pp)")

    out = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ungated": s_ungated,
        "switch_cost_bps": SWITCH_COST_BPS,
        "variants": variants,
        "ranked_top": [v for v in scored[:6]],
    }
    OUT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
