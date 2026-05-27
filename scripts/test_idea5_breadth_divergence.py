"""Idea 5 — Breadth divergence as risk overlay.

Classic Lowry's-style technical signal: when the market index makes a
new high but breadth (% of stocks above 200d MA) is FALLING, that's a
bearish divergence — fewer and fewer stocks are participating in the
rally, suggesting the trend is being carried by a narrowing leadership
group and is vulnerable to a top.

Operationalisation:
  - Track S&P 500 (SPY) rolling N-day high
  - Track CSP1 (S&P 500 constituent) breadth rolling N-day high
  - DIVERGENCE = SPY at a new N-day high AND breadth NOT at a new high
                  (breadth's high was earlier than SPY's high — fewer
                  participants confirming the price high)
  - Or quantitatively: SPY_high_date - breadth_high_date > LAG_DAYS

Two deployment modes to test:
  (A) Standalone overlay — de-risk when in divergence state, similar to
      Phase 19's breadth-level overlay but triggered by divergence
      instead of absolute low breadth.
  (B) Stacked with Phase 19 — divergence as an EARLY-WARNING that
      triggers a smaller de-risk fraction (say 25%) BEFORE breadth
      collapses to absolute-low territory (which would then trigger the
      full Phase 19 50% de-risk).

The "risk" with divergence signals is they can persist for many months
before resolving. 2021 had near-continuous breadth divergence for
~9 months before the actual top. So a binary "de-risk on divergence"
might give back a lot of upside while waiting for the resolution.

Test: parameter sweep over (lookback_days, lag_days, derisk_fraction)
and compare to the deployed Phase 19 baseline.

Usage:
    python scripts/test_idea5_breadth_divergence.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from backtest import download_spy_close  # noqa: E402

# Phase 19 baseline parameters (the deployed overlay)
PHASE19_OFF = 0.20
PHASE19_ON = 0.50
PHASE19_DERISK = 0.50
SWITCH_COST_BPS = 5

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-onwards", "2022-01-01", None),
]


def _stats(eq: pd.Series) -> dict:
    if len(eq) < 5:
        return {"sharpe": None, "cagr": None, "total": None, "dd": None}
    e = eq.dropna() / eq.dropna().iloc[0]
    d = e.pct_change().fillna(0)
    n = (e.index[-1] - e.index[0]).days / 365.25
    return {
        "sharpe": d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0,
        "cagr": e.iloc[-1] ** (1 / n) - 1 if n > 0 else 0,
        "total": e.iloc[-1] - 1,
        "dd": ((e - e.cummax()) / e.cummax()).min(),
    }


def _ws(eq, start, end):
    w = eq.loc[start:end] if (start or end) else eq
    return _stats(w)


def _phase19_states(breadth: pd.Series) -> pd.Series:
    """Reproduce Phase 19 deployed overlay states."""
    states = []
    s = 1.0
    for v in breadth.values:
        if pd.isna(v): states.append(s); continue
        if s == 1.0 and v < PHASE19_OFF: s = 0.0
        elif s == 0.0 and v > PHASE19_ON: s = 1.0
        states.append(s)
    return pd.Series(states, index=breadth.index)


def _divergence_states(spy_close: pd.Series, breadth: pd.Series,
                          lookback: int, lag_days: int) -> pd.Series:
    """Detect bearish divergence.

    State = 0 (RISK_OFF) when SPY is at/near a `lookback`-day high but
    breadth's rolling max occurred more than `lag_days` ago — i.e.
    breadth has NOT confirmed the price high within lag_days.
    State = 1 (RISK_ON) otherwise.

    Note: this is a CONTINUOUS detector, not a stateful one with
    hysteresis. The state flips back to RISK_ON as soon as breadth
    catches up (its rolling max date moves to within lag_days).
    """
    common = spy_close.index.intersection(breadth.index)
    spy = spy_close.reindex(common).ffill()
    br  = breadth.reindex(common).ffill()
    # Rolling N-day argmax (date) for both series
    spy_argmax = spy.rolling(lookback, min_periods=lookback // 2).apply(
        lambda x: float(np.argmax(x)), raw=True)
    br_argmax = br.rolling(lookback, min_periods=lookback // 2).apply(
        lambda x: float(np.argmax(x)), raw=True)
    # The argmax values are positions WITHIN the window. We want how
    # many bars BACK from the current bar each high occurred.
    # If current position is t (window end), and argmax returns idx i
    # within the window of size lookback, the high is at (t - (lookback-1) + i)
    # so "days ago" = lookback - 1 - i
    spy_days_ago = lookback - 1 - spy_argmax
    br_days_ago = lookback - 1 - br_argmax
    # Divergence: SPY's high is recent (<= lag_days ago) AND breadth's
    # high is OLDER than SPY's by more than lag_days. Equivalently:
    # br_days_ago - spy_days_ago > lag_days
    divergence = (spy_days_ago <= lag_days) & (br_days_ago - spy_days_ago > lag_days)
    states = pd.Series(1.0, index=common)
    states[divergence] = 0.0
    return states


def _run_gate(blend_eq, fallback_eq, states, derisk):
    common = blend_eq.index
    fb = fallback_eq.reindex(common, method="ffill")
    br = blend_eq.pct_change().fillna(0)
    fr = fb.pct_change().fillna(0)
    sl = states.reindex(common, method="ffill").shift(1).fillna(1.0)
    sw = sl.diff().fillna(0).abs() * (SWITCH_COST_BPS / 10_000)
    bw = sl + (1.0 - sl) * (1.0 - derisk)
    fw = (1.0 - sl) * derisk
    return (1.0 + (bw * br + fw * fr - sw)).cumprod()


def main():
    print("Loading deployed blend equity + breadth + SHY + SPY ...")
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    blend = multi["strategies"]["blend_35_35_10_20"]
    blend_eq = pd.Series(blend["equity"], index=pd.to_datetime(blend["dates"]))

    csp1 = json.loads((DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"])).dropna()

    ac = pd.read_parquet(DATA_DIR / "asset_class_prices_cache.parquet")
    shy = ac["SHY"].dropna()

    spy_close = download_spy_close(start="2017-01-01",
                                    end=blend_eq.index[-1].strftime("%Y-%m-%d"))
    spy_close.index = pd.to_datetime(spy_close.index).tz_localize(None)

    common = blend_eq.index
    breadth_a = breadth.reindex(common, method="ffill")

    # Phase 19 baseline
    phase19_states = _phase19_states(breadth_a)
    phase19_gated = _run_gate(blend_eq, shy, phase19_states, PHASE19_DERISK)

    def print_block(label, eq):
        print(f"\n  {label}")
        for w, start, end in WINDOWS:
            s = _ws(eq, start, end)
            if s["sharpe"] is None: continue
            print(f"    {w:<14s}  Sharpe {s['sharpe']:+.3f}  "
                  f"Total {s['total']*100:+6.1f}%  DD {s['dd']*100:.1f}%")

    print("\n" + "=" * 100)
    print("BASELINES")
    print("=" * 100)
    print_block("ungated blend", blend_eq)
    print_block("Phase 19 deployed gate (breadth-level)", phase19_gated)

    # Divergence sweep
    print("\n" + "=" * 100)
    print("DIVERGENCE OVERLAY SWEEP (divergence alone, no breadth-level stacking)")
    print("=" * 100)

    sweeps = []
    # Test: lookback ∈ {60, 90, 120, 180}, lag_days ∈ {10, 20, 30}, derisk ∈ {50%, 75%}
    for lookback in [60, 90, 120, 180]:
        for lag in [10, 20, 30]:
            for derisk in [0.50, 0.75]:
                states = _divergence_states(spy_close, breadth, lookback, lag)
                gated = _run_gate(blend_eq, shy, states, derisk)
                wstats = {w[0]: _ws(gated, w[1], w[2]) for w in WINDOWS}
                # Days RISK_OFF
                sl = states.reindex(common, method="ffill").shift(1).fillna(1.0)
                pct_off = (sl == 0).sum() / len(sl) * 100
                n_sw = int(sl.diff().fillna(0).abs().sum())
                sweeps.append({
                    "lookback": lookback, "lag": lag, "derisk": derisk,
                    "stats": wstats, "pct_off": pct_off, "n_sw": n_sw,
                })

    # Print top 10 by 2022-onwards return
    sweeps.sort(key=lambda r: -(r["stats"]["2022-onwards"]["total"] or -1))
    print(f"\n  Top 10 by 2022-onwards total return:")
    print(f"  {'LB':<3s} {'lag':<3s} {'der':<3s}  {'Full Sh':>7s} {'Full DD':>7s}  "
          f"{'22 Tot':>7s}  {'22on Tot':>8s}  {'%off':>5s} {'sw':>4s}")
    base22on = _ws(phase19_gated, "2022-01-01", None)
    print(f"  Phase19 baseline:                Sharpe {_ws(phase19_gated,None,None)['sharpe']:+.2f}  "
          f"DD {_ws(phase19_gated,None,None)['dd']*100:.1f}%  "
          f"22-on Tot {base22on['total']*100:+.1f}%")
    for r in sweeps[:10]:
        s_full = r["stats"]["Full"]; s_22 = r["stats"]["2022 only"]; s_22on = r["stats"]["2022-onwards"]
        if s_full["sharpe"] is None: continue
        print(f"  {r['lookback']:<3d} {r['lag']:<3d} {int(r['derisk']*100):<3d}  "
              f"{s_full['sharpe']:>+6.2f}  {s_full['dd']*100:>+6.1f}%  "
              f"{s_22['total']*100:>+6.1f}%  {s_22on['total']*100:>+7.1f}%  "
              f"{r['pct_off']:>4.1f}% {r['n_sw']:>4d}")

    # Also test STACKED variant: divergence + Phase 19 (logical OR)
    print(f"\n=== STACKED: divergence OR Phase 19 (either triggers de-risk) ===")
    print(f"  {'LB':<3s} {'lag':<3s} {'der':<3s}  {'Full Sh':>7s} {'Full DD':>7s}  "
          f"{'22 Tot':>7s}  {'22on Tot':>8s}  {'%off':>5s}")
    stacked_rows = []
    for lookback in [60, 90, 120]:
        for lag in [10, 20]:
            for derisk in [0.50, 0.75]:
                div_states = _divergence_states(spy_close, breadth, lookback, lag)
                # OR: state = min of the two states (0 if either is off)
                combined = pd.concat([
                    div_states.reindex(common, method="ffill"),
                    phase19_states.reindex(common, method="ffill"),
                ], axis=1).min(axis=1)
                gated = _run_gate(blend_eq, shy, combined, derisk)
                wstats = {w[0]: _ws(gated, w[1], w[2]) for w in WINDOWS}
                sl = combined.shift(1).fillna(1.0)
                pct_off = (sl == 0).sum() / len(sl) * 100
                stacked_rows.append({
                    "lookback": lookback, "lag": lag, "derisk": derisk,
                    "stats": wstats, "pct_off": pct_off
                })
    stacked_rows.sort(key=lambda r: -(r["stats"]["2022-onwards"]["total"] or -1))
    for r in stacked_rows[:8]:
        s_full = r["stats"]["Full"]; s_22 = r["stats"]["2022 only"]; s_22on = r["stats"]["2022-onwards"]
        print(f"  {r['lookback']:<3d} {r['lag']:<3d} {int(r['derisk']*100):<3d}  "
              f"{s_full['sharpe']:>+6.2f}  {s_full['dd']*100:>+6.1f}%  "
              f"{s_22['total']*100:>+6.1f}%  {s_22on['total']*100:>+7.1f}%  "
              f"{r['pct_off']:>4.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
