"""Phase 24 #1 — Relative momentum in Strategy B (Idea 2 analog).

Strategy A benefited from sector-relative breadth (subtract cross-
sectional mean) — Phase 20 deployed it for clean Pareto gain.

For B, the cross-sectional adjustment is more nuanced because B's
universe is HETEROGENEOUS (equities, bonds, commodities, REITs have
very different signal distributions). A pure row-mean subtraction
systematically favours high-vol asset classes whose signals are
larger.

Test three variants to find what works:

  V1 = pure row-mean subtraction
       signal_rel_i = signal_i - mean(all signals)
       Risk: bias toward high-vol asset classes.

  V2 = z-score normalisation per ETF
       z_i = (signal_i - rolling_mean_i_past) / rolling_std_i_past
       Each ETF's signal expressed against its OWN history. Should
       neutralise the heterogeneous-vol issue. Picks ETFs whose
       current momentum is unusually strong vs their own track record.

  V3 = within-bucket relative
       Split universe into equity / bond / real-asset buckets. Within
       each bucket, subtract the bucket's row-mean. Then rank globally
       by the bucket-relative signal. Equity-vs-equity, bond-vs-bond,
       etc — cleaner cross-sectional adjustment.

Baseline: B's current top-K signal-weighted (K=7 weekly Fri).
All variants also exclude CASH_PROXY (SHY) from candidates as the
deployed B does.

Usage: python scripts/test_phase24_b_relative_momentum.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import run_asset_class_rotation as B  # noqa: E402

# Bucket map for V3
BUCKETS = {
    "equity":     ["SPY", "IJR", "QQQ", "EFA", "VGK", "EWJ", "EEM"],
    "real_estate":["VNQ"],
    "commodity":  ["GLD", "DBC"],
    "bond":       ["TLT", "IEF", "TIP", "HYG"],
}

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-onwards", "2022-01-01", None),
]


def _stats(eq):
    if len(eq) < 5: return {"sharpe": None}
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


def make_v1_signal(signal: pd.DataFrame) -> pd.DataFrame:
    """V1: cross-sectional row-mean subtraction. Pure Idea 2 analog."""
    # Exclude CASH_PROXY from the mean computation
    cols_for_mean = [c for c in signal.columns if c != B.CASH_PROXY]
    row_mean = signal[cols_for_mean].mean(axis=1, skipna=True)
    return signal.sub(row_mean, axis=0)


def make_v2_signal(signal: pd.DataFrame, window: int = 504) -> pd.DataFrame:
    """V2: z-score per ETF vs its own 2-year (504 trading day) history.
    Uses .shift(1).rolling(...) so the z-score at date t only uses data
    available through t-1 (no look-ahead).
    """
    past = signal.shift(1)
    mean = past.rolling(window, min_periods=window // 2).mean()
    std  = past.rolling(window, min_periods=window // 2).std()
    return (signal - mean) / std.replace(0, np.nan)


def make_v3_signal(signal: pd.DataFrame) -> pd.DataFrame:
    """V3: within-bucket relative. For each bucket, subtract the
    bucket's row-mean from each member's signal. Real-estate bucket has
    only VNQ — bucket-relative is always zero (no neighbours), so VNQ
    competes on absolute signal. Treat as own micro-bucket.
    """
    out = signal.copy()
    for bucket, members in BUCKETS.items():
        cols = [c for c in members if c in signal.columns]
        if len(cols) <= 1:
            # Single-member bucket — leave signal as-is
            continue
        bucket_mean = signal[cols].mean(axis=1, skipna=True)
        for c in cols:
            out[c] = signal[c] - bucket_mean
    return out


def run_b_with_signal(closes, signal_panel, eligible):
    r = B.run_rotation(closes, signal_panel, B.top_k_by_signal(7),
                        eligible, rebalance_freq="W-FRI")
    eq = r["equity"].loc[r["equity"].index >= eligible]
    return eq, r["weights"]


def main():
    print("Loading B panel ...")
    closes = B.download_prices()
    closes = closes.dropna().sort_index()
    eligible = closes.index[B.MA_PERIOD]
    print(f"  Eligible from {eligible.date()}  "
          f"({len(closes)} dates x {closes.shape[1]} ETFs)")
    signal = B.compute_signal(closes)

    # Baseline (current deployed B)
    print("\nBASELINE (current B — raw absolute momentum, K=7 weekly Fri):")
    base_eq, base_w = run_b_with_signal(closes, signal, eligible)
    base_stats = {w[0]: _ws(base_eq, w[1], w[2]) for w in WINDOWS}
    for w in WINDOWS:
        s = base_stats[w[0]]
        if s["sharpe"] is None: continue
        print(f"  {w[0]:<14s}  Sharpe {s['sharpe']:+.3f}  CAGR {s['cagr']*100:+5.1f}%  "
              f"Total {s['total']*100:+6.1f}%  DD {s['dd']*100:.1f}%")

    # Run variants
    variants = {
        "V1_pure_relative":      make_v1_signal(signal),
        "V2_zscore_504d":        make_v2_signal(signal, 504),
        "V2_zscore_252d":        make_v2_signal(signal, 252),
        "V3_within_bucket":      make_v3_signal(signal),
    }
    print("\n" + "=" * 120)
    print("Variants: top-K signal-weighted with K=7 weekly Fri")
    print("=" * 120)
    print(f"  {'Variant':<24s}  " + "  ".join(f"{w[0]:<36s}" for w in WINDOWS))
    for name, sig_var in variants.items():
        eq, _ = run_b_with_signal(closes, sig_var, eligible)
        cells = []
        for w in WINDOWS:
            s = _ws(eq, w[1], w[2])
            b = base_stats[w[0]]
            if s["sharpe"] is None or b["sharpe"] is None:
                cells.append("n/a".ljust(36))
                continue
            d_sh = s["sharpe"] - b["sharpe"]
            d_tot = (s["total"] - b["total"]) * 100
            cells.append(f"Sh{s['sharpe']:+.3f}({d_sh:+.3f}) "
                          f"Tot{s['total']*100:+5.1f}%({d_tot:+.1f}pp)")
        print(f"  {name:<24s}  " + "  ".join(c.ljust(36) for c in cells))

    print("\nDONE — interpret 22-onwards dTotal as headline; check Full dSh stays >= 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
