"""Phase 24 #5 — Drop bond duplication from B's universe.

B has 4 bond ETFs: TLT (20+y Treasury), IEF (7-10y), TIP (TIPS),
HYG (HY credit). TLT and IEF are both duration plays (correlation
typically 0.85+). Dropping one might reduce signal noise.

Test variants:
  Baseline: B's current 14 ETFs
  -TLT:     drop TLT, keep IEF as the duration play
  -IEF:     drop IEF, keep TLT as the duration play
  -HYG:     drop HYG (since HYG also correlates with equities)
  Bond-lean: keep only IEF + TIP (no TLT, no HYG)

If signal noise is the issue, dropping correlated bonds should help.
If diversification matters more, dropping any bond should hurt.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import run_asset_class_rotation as B  # noqa: E402

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


def run_with_universe(closes_full, drop_tickers, eligible):
    """Run B's K=7 weekly rotation with selected tickers dropped from
    the universe (signal still computed on remaining columns)."""
    keep_cols = [c for c in closes_full.columns if c not in set(drop_tickers)]
    closes = closes_full[keep_cols].dropna()
    if len(closes) < B.MA_PERIOD + 100:
        return None
    signal = B.compute_signal(closes)
    r = B.run_rotation(closes, signal, B.top_k_by_signal(7),
                        eligible, rebalance_freq="W-FRI")
    return r["equity"].loc[r["equity"].index >= eligible]


def main():
    closes = B.download_prices().dropna()
    eligible = closes.index[B.MA_PERIOD]
    print(f"  Eligible from {eligible.date()}, {closes.shape[1]} ETFs")

    print("\nBASELINE (all 14 + SHY):")
    base_eq = run_with_universe(closes, [], eligible)
    base_stats = {w[0]: _ws(base_eq, w[1], w[2]) for w in WINDOWS}
    for w in WINDOWS:
        s = base_stats[w[0]]
        if s["sharpe"] is None: continue
        print(f"  {w[0]:<14s}  Sharpe {s['sharpe']:+.3f}  CAGR {s['cagr']*100:+5.1f}%  "
              f"Total {s['total']*100:+6.1f}%  DD {s['dd']*100:.1f}%")

    print("\n" + "=" * 110)
    print("Drop-tests")
    print("=" * 110)
    print(f"  {'Variant':<26s}  " + "  ".join(f"{w[0]:<32s}" for w in WINDOWS))
    variants = [
        ("-TLT only",         ["TLT"]),
        ("-IEF only",         ["IEF"]),
        ("-HYG only",         ["HYG"]),
        ("-TLT -HYG",         ["TLT", "HYG"]),
        ("bond-lean (IEF+TIP)", ["TLT", "HYG"]),  # same as above; alt label
        ("-TLT -IEF -HYG",    ["TLT", "IEF", "HYG"]),  # only TIP
    ]
    for name, drop in variants:
        eq = run_with_universe(closes, drop, eligible)
        if eq is None:
            print(f"  {name:<26s}  insufficient data"); continue
        cells = []
        for w in WINDOWS:
            s = _ws(eq, w[1], w[2]); b = base_stats[w[0]]
            if s["sharpe"] is None or b["sharpe"] is None:
                cells.append("n/a"); continue
            d_sh = s["sharpe"] - b["sharpe"]
            d_tot = (s["total"] - b["total"]) * 100
            cells.append(f"Sh{s['sharpe']:+.3f}({d_sh:+.3f}) "
                          f"Tot{s['total']*100:+5.1f}%({d_tot:+.1f}pp)")
        print(f"  {name:<26s}  " + "  ".join(c.ljust(32) for c in cells))


if __name__ == "__main__":
    sys.exit(main())
