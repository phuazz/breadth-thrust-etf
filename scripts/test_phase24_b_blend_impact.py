"""Phase 24 — measure blend-level impact of B universe pruning.

Standalone B with bond pruning showed clean Sharpe + Total lifts. Now
test the blend-level impact: does the improved B translate to deployed
gated 4-way + EEM-tilted blend gains, or does the improvement get
washed out at the blend level?
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import run_asset_class_rotation as B  # noqa: E402

DATA_DIR = ROOT / "data"
OFF = 0.20
ON = 0.50
DERISK = 0.50
SWITCH_COST_BPS = 5

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


def _regime(breadth, off, on):
    states = []
    s = 1.0
    for v in breadth.values:
        if pd.isna(v): states.append(s); continue
        if s == 1.0 and v < off: s = 0.0
        elif s == 0.0 and v > on: s = 1.0
        states.append(s)
    return pd.Series(states, index=breadth.index)


def _gate(blend_eq, fallback_eq, states, derisk):
    common = blend_eq.index
    fb = fallback_eq.reindex(common, method="ffill")
    br = blend_eq.pct_change().fillna(0)
    fr = fb.pct_change().fillna(0)
    sl = states.reindex(common, method="ffill").shift(1).fillna(1.0)
    sw = sl.diff().fillna(0).abs() * (SWITCH_COST_BPS / 10_000)
    bw = sl + (1.0 - sl) * (1.0 - derisk)
    fw = (1.0 - sl) * derisk
    return (1.0 + (bw * br + fw * fr - sw)).cumprod()


def run_b_with_universe(closes_full, drop_tickers, eligible):
    keep = [c for c in closes_full.columns if c not in set(drop_tickers)]
    closes = closes_full[keep].dropna()
    if len(closes) < B.MA_PERIOD + 100: return None
    signal = B.compute_signal(closes)
    r = B.run_rotation(closes, signal, B.top_k_by_signal(7),
                        eligible, rebalance_freq="W-FRI")
    return r["equity"].loc[r["equity"].index >= eligible]


def main():
    # Load existing sleeves
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    a = pd.Series(multi["strategies"]["strategy_a"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_a"]["dates"]))
    c = pd.Series(multi["strategies"]["strategy_c"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_c"]["dates"]))
    d = pd.Series(multi["strategies"]["strategy_d"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_d"]["dates"]))

    # CSP1 breadth + SHY for gate
    csp1 = json.loads((DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"])).dropna()
    ac = pd.read_parquet(DATA_DIR / "asset_class_prices_cache.parquet")
    shy = ac["SHY"].dropna()

    # Compute current deployed B (no pruning) — baseline
    closes_all = B.download_prices().dropna()
    b_eligible = closes_all.index[B.MA_PERIOD]
    b_base = run_b_with_universe(closes_all, [], b_eligible)
    b_drop_tlt_hyg = run_b_with_universe(closes_all, ["TLT", "HYG"], b_eligible)
    b_drop_three   = run_b_with_universe(closes_all, ["TLT", "IEF", "HYG"], b_eligible)

    def build_gated_blend(b_eq, label):
        common = a.index.intersection(b_eq.index).intersection(c.index).intersection(d.index)
        ar = a.reindex(common).pct_change().fillna(0)
        br = b_eq.reindex(common).pct_change().fillna(0)
        cr = c.reindex(common).pct_change().fillna(0)
        dr = d.reindex(common).pct_change().fillna(0)
        blend_ret = 0.35*ar + 0.35*br + 0.10*cr + 0.20*dr
        ungated = (1.0 + blend_ret).cumprod()
        ungated.index = common
        # Apply Phase 19 gate
        breadth_a = breadth.reindex(common, method="ffill")
        states = _regime(breadth_a, OFF, ON)
        gated = _gate(ungated, shy, states, DERISK)
        return ungated, gated

    print("\n" + "=" * 130)
    print("Phase 24 #5 — B universe pruning, BLEND-LEVEL impact (35/35/10/20 + Phase 19 gate)")
    print("=" * 130)
    print(f"  {'B universe':<28s}  " + "  ".join(f"{w[0]:<32s}" for w in WINDOWS))

    # Baseline blend
    base_ung, base_gated = build_gated_blend(b_base, "baseline")
    base_g_stats = {w[0]: _ws(base_gated, w[1], w[2]) for w in WINDOWS}
    base_u_stats = {w[0]: _ws(base_ung, w[1], w[2]) for w in WINDOWS}

    def print_row(name, b_eq):
        ung, gated = build_gated_blend(b_eq, name)
        ung_stats = {w[0]: _ws(ung, w[1], w[2]) for w in WINDOWS}
        g_stats = {w[0]: _ws(gated, w[1], w[2]) for w in WINDOWS}
        cells = []
        for w in WINDOWS:
            us = ung_stats[w[0]]; gs = g_stats[w[0]]
            bu = base_u_stats[w[0]]; bg = base_g_stats[w[0]]
            if any(x is None or x.get('sharpe') is None for x in (us, gs, bu, bg)):
                cells.append("n/a"); continue
            d_g_sh = gs["sharpe"] - bg["sharpe"]
            d_g_tot = (gs["total"] - bg["total"]) * 100
            d_g_dd = (gs["dd"] - bg["dd"]) * 100
            cells.append(f"GtSh{gs['sharpe']:+.3f}({d_g_sh:+.3f}) "
                          f"GtTot{gs['total']*100:+5.1f}%({d_g_tot:+.1f}) "
                          f"GtDD{gs['dd']*100:+5.1f}%({d_g_dd:+.1f})")
        # Manually adjust column width to fit the longer cells
        print(f"  {name:<28s}  " + "  ".join(c.ljust(50) for c in cells))

    # Print baseline first
    print(f"\n  BASELINE (B with all 14 ETFs):")
    for w in WINDOWS:
        gs = base_g_stats[w[0]]
        if gs["sharpe"] is None: continue
        print(f"    {w[0]:<14s}  Gated:  Sharpe {gs['sharpe']:+.3f}  "
              f"Total {gs['total']*100:+5.1f}%  DD {gs['dd']*100:+5.1f}%")
    print()

    print(f"  Variants — deltas vs baseline gated:")
    print(f"  {'B universe':<28s}  " + "  ".join(f"{w[0]:<50s}" for w in WINDOWS))
    b_drop_hyg_only = run_b_with_universe(closes_all, ["HYG"], b_eligible)
    print_row("-HYG only (keep all Tsy)", b_drop_hyg_only)
    print_row("-TLT -HYG (drop long-dur)", b_drop_tlt_hyg)
    print_row("-TLT -IEF -HYG (TIP only)", b_drop_three)

    return 0


if __name__ == "__main__":
    sys.exit(main())
