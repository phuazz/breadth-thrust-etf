"""Phase 24 #2 + #4 — Equal-weight top-K and hierarchical bucketed B.

#2 Equal-weight top-K (Phase 6 analog):
  B currently weights each top-K pick by signal share (high-signal
  picks get more weight). Test equal weighting (1/K per pick) — the
  change that lifted Strategy C from signal-weighted to equal-weight
  in Phase 6.

#4 Hierarchical bucketed B:
  Replace the flat top-K-of-14 with bucket allocations:
    Equity:    50% — top-K within {SPY, IJR, QQQ, EFA, VGK, EWJ, EEM}
    Bond:      30% — top-K within {TLT, IEF, TIP, HYG}
    Commodity: 15% — top-K within {GLD, DBC}
    REIT:       5% — VNQ if positive, else cash
  Within each bucket, if no member has positive momentum the bucket's
  allocation goes to SHY cash floor.

  Forces cross-asset diversification regardless of which bucket has
  the strongest momentum. Should reduce concentration risk (B's
  current K=7 can be 7-of-7 equities in a rally) but cost some upside.

Test against deployed B (raw absolute momentum, signal-weighted, K=7
weekly Fri) on Full / 2022 / 2022-onwards.
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

BUCKETS = {
    "equity":     (["SPY", "IJR", "QQQ", "EFA", "VGK", "EWJ", "EEM"], 0.50, 3),
    "bond":       (["TLT", "IEF", "TIP", "HYG"],                       0.30, 2),
    "commodity":  (["GLD", "DBC"],                                     0.15, 1),
    "reit":       (["VNQ"],                                            0.05, 1),
}
# bucket_meta[name] = (members, target_weight, top_K_within_bucket)

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


def top_k_equal_weight(K: int, exclude_negative: bool = True):
    """Equal-weight top-K-by-signal (Phase 6 analog for B)."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        if len(valid) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if B.CASH_PROXY in w.index:
                w[B.CASH_PROXY] = 1.0
            return w
        candidates = valid[valid > 0] if exclude_negative else valid
        if B.CASH_PROXY in candidates.index:
            candidates = candidates.drop(B.CASH_PROXY)
        if len(candidates) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if B.CASH_PROXY in w.index:
                w[B.CASH_PROXY] = 1.0
            return w
        top = candidates.nlargest(min(K, len(candidates)))
        invested = len(top) / K
        per_etf = invested / len(top)
        w = pd.Series(0.0, index=s_row.index)
        w.loc[top.index] = per_etf
        cash = 1.0 - invested
        if cash > 0 and B.CASH_PROXY in w.index:
            w[B.CASH_PROXY] = w.get(B.CASH_PROXY, 0.0) + cash
        return w
    return f


def hierarchical_bucketed(buckets: dict):
    """Per-bucket top-K with fixed bucket weights. Within each bucket,
    pick top-K by signal (equal-weight); fill un-invested bucket capacity
    with SHY cash floor.
    """
    def f(s_row: pd.Series) -> pd.Series:
        w = pd.Series(0.0, index=s_row.index)
        for bucket_name, (members, target_w, k_within) in buckets.items():
            cols = [m for m in members if m in s_row.index]
            sub = s_row.reindex(cols).dropna()
            positives = sub[sub > 0]
            if len(positives) == 0:
                # All bucket members negative — bucket goes to cash floor
                if B.CASH_PROXY in w.index:
                    w[B.CASH_PROXY] = w.get(B.CASH_PROXY, 0.0) + target_w
                continue
            top = positives.nlargest(min(k_within, len(positives)))
            # Equal weight WITHIN the picks; bucket may be under-filled
            invested_frac = len(top) / k_within
            per_etf_within_bucket = invested_frac / len(top)
            for etf in top.index:
                w[etf] += target_w * per_etf_within_bucket
            cash = target_w * (1.0 - invested_frac)
            if cash > 0 and B.CASH_PROXY in w.index:
                w[B.CASH_PROXY] = w.get(B.CASH_PROXY, 0.0) + cash
        return w
    return f


def main():
    print("Loading B panel ...")
    closes = B.download_prices()
    closes = closes.dropna().sort_index()
    eligible = closes.index[B.MA_PERIOD]
    print(f"  Eligible from {eligible.date()}  "
          f"({len(closes)} dates x {closes.shape[1]} ETFs)")
    signal = B.compute_signal(closes)

    # Baseline
    print("\nBASELINE (current B — raw signal, top-K signal-weighted, K=7):")
    base_r = B.run_rotation(closes, signal, B.top_k_by_signal(7), eligible,
                             rebalance_freq="W-FRI")
    base_eq = base_r["equity"].loc[base_r["equity"].index >= eligible]
    base_stats = {w[0]: _ws(base_eq, w[1], w[2]) for w in WINDOWS}
    for w in WINDOWS:
        s = base_stats[w[0]]
        if s["sharpe"] is None: continue
        print(f"  {w[0]:<14s}  Sharpe {s['sharpe']:+.3f}  CAGR {s['cagr']*100:+5.1f}%  "
              f"Total {s['total']*100:+6.1f}%  DD {s['dd']*100:.1f}%")

    # Test #2: equal-weight top-K at multiple K values
    print("\n" + "=" * 110)
    print("#2 EQUAL-WEIGHT top-K (vs current signal-weighted top-K)")
    print("=" * 110)
    print(f"  {'K':<3s}  " + "  ".join(f"{w[0]:<36s}" for w in WINDOWS))
    for K in [5, 6, 7, 8]:
        r = B.run_rotation(closes, signal, top_k_equal_weight(K), eligible,
                            rebalance_freq="W-FRI")
        eq = r["equity"].loc[r["equity"].index >= eligible]
        cells = []
        for w in WINDOWS:
            s = _ws(eq, w[1], w[2]); b = base_stats[w[0]]
            if s["sharpe"] is None or b["sharpe"] is None:
                cells.append("n/a"); continue
            d_sh = s["sharpe"] - b["sharpe"]
            d_tot = (s["total"] - b["total"]) * 100
            cells.append(f"Sh{s['sharpe']:+.3f}({d_sh:+.3f}) "
                          f"Tot{s['total']*100:+5.1f}%({d_tot:+.1f}pp)")
        print(f"  K={K:<2d} " + "  ".join(c.ljust(36) for c in cells))

    # Test #4: hierarchical bucketed
    print("\n" + "=" * 110)
    print("#4 HIERARCHICAL BUCKETED — fixed bucket weights, top-K within each")
    print(f"  Buckets: equity 50% (top-3 of 7), bond 30% (top-2 of 4), "
          f"commodity 15% (top-1 of 2), REIT 5% (VNQ if positive)")
    print("=" * 110)
    print(f"  {'Variant':<28s}  " + "  ".join(f"{w[0]:<36s}" for w in WINDOWS))

    # Test the default hierarchical
    r = B.run_rotation(closes, signal, hierarchical_bucketed(BUCKETS),
                        eligible, rebalance_freq="W-FRI")
    eq = r["equity"].loc[r["equity"].index >= eligible]
    cells = []
    for w in WINDOWS:
        s = _ws(eq, w[1], w[2]); b = base_stats[w[0]]
        d_sh = s["sharpe"] - b["sharpe"] if s["sharpe"] and b["sharpe"] else 0
        d_tot = (s["total"] - b["total"]) * 100 if s and b else 0
        cells.append(f"Sh{s['sharpe']:+.3f}({d_sh:+.3f}) "
                      f"Tot{s['total']*100:+5.1f}%({d_tot:+.1f}pp)")
    print(f"  {'default 50/30/15/5':<28s}  " + "  ".join(c.ljust(36) for c in cells))

    # Alt weights: equity-tilted (typical balanced)
    alt_buckets_a = {
        "equity":     (["SPY", "IJR", "QQQ", "EFA", "VGK", "EWJ", "EEM"], 0.60, 3),
        "bond":       (["TLT", "IEF", "TIP", "HYG"],                       0.25, 2),
        "commodity":  (["GLD", "DBC"],                                     0.10, 1),
        "reit":       (["VNQ"],                                            0.05, 1),
    }
    r = B.run_rotation(closes, signal, hierarchical_bucketed(alt_buckets_a),
                        eligible, rebalance_freq="W-FRI")
    eq = r["equity"].loc[r["equity"].index >= eligible]
    cells = []
    for w in WINDOWS:
        s = _ws(eq, w[1], w[2]); b = base_stats[w[0]]
        d_sh = s["sharpe"] - b["sharpe"] if s["sharpe"] and b["sharpe"] else 0
        d_tot = (s["total"] - b["total"]) * 100 if s and b else 0
        cells.append(f"Sh{s['sharpe']:+.3f}({d_sh:+.3f}) "
                      f"Tot{s['total']*100:+5.1f}%({d_tot:+.1f}pp)")
    print(f"  {'equity-tilted 60/25/10/5':<28s}  " + "  ".join(c.ljust(36) for c in cells))

    # Alt: bond-tilted (defensive)
    alt_buckets_b = {
        "equity":     (["SPY", "IJR", "QQQ", "EFA", "VGK", "EWJ", "EEM"], 0.40, 3),
        "bond":       (["TLT", "IEF", "TIP", "HYG"],                       0.40, 2),
        "commodity":  (["GLD", "DBC"],                                     0.15, 1),
        "reit":       (["VNQ"],                                            0.05, 1),
    }
    r = B.run_rotation(closes, signal, hierarchical_bucketed(alt_buckets_b),
                        eligible, rebalance_freq="W-FRI")
    eq = r["equity"].loc[r["equity"].index >= eligible]
    cells = []
    for w in WINDOWS:
        s = _ws(eq, w[1], w[2]); b = base_stats[w[0]]
        d_sh = s["sharpe"] - b["sharpe"] if s["sharpe"] and b["sharpe"] else 0
        d_tot = (s["total"] - b["total"]) * 100 if s and b else 0
        cells.append(f"Sh{s['sharpe']:+.3f}({d_sh:+.3f}) "
                      f"Tot{s['total']*100:+5.1f}%({d_tot:+.1f}pp)")
    print(f"  {'bond-tilted 40/40/15/5':<28s}  " + "  ".join(c.ljust(36) for c in cells))

    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
