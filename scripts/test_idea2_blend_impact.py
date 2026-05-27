"""Idea 2 blend-impact test — splice relative-breadth A into the deployed
35/35/10/20 blend + overlay and report deltas.

If sleeve-level A improvement of +15pp in 2022-onwards translates to a
material blend improvement after overlay, deploy. If it gets washed out
by B/C/D contribution + overlay smoothing, document and park.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from run_portfolio import (  # noqa: E402
    build_panels, run_portfolio, top_k_breadth_weight, COST_BPS,
)
from run_ma200_sweep import MA_PERIOD  # noqa: E402

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-2024",    "2022-01-01", "2024-12-31"),
    ("2022-onwards", "2022-01-01", None),
]

OFF = 0.20
ON  = 0.50
DERISK = 0.50
SWITCH_COST_BPS = 5


def _stats(eq: pd.Series) -> dict:
    if len(eq) < 5:
        return {"sharpe": None, "cagr": None, "total": None, "dd": None}
    e = eq.dropna() / eq.dropna().iloc[0]
    d = e.pct_change().fillna(0)
    n = (e.index[-1] - e.index[0]).days / 365.25
    sh = d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0
    cagr = e.iloc[-1] ** (1 / n) - 1 if n > 0 else 0
    dd = ((e - e.cummax()) / e.cummax()).min()
    return {"sharpe": sh, "cagr": cagr, "total": e.iloc[-1] - 1, "dd": dd}


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


def main():
    print("Loading sleeve A baseline + B/C/D from multi_strategy.json ...")
    ms = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    b = pd.Series(ms["strategies"]["strategy_b"]["equity"],
                   index=pd.to_datetime(ms["strategies"]["strategy_b"]["dates"]))
    c = pd.Series(ms["strategies"]["strategy_c"]["equity"],
                   index=pd.to_datetime(ms["strategies"]["strategy_c"]["dates"]))
    d = pd.Series(ms["strategies"]["strategy_d"]["equity"],
                   index=pd.to_datetime(ms["strategies"]["strategy_d"]["dates"]))
    a_base = pd.Series(ms["strategies"]["strategy_a"]["equity"],
                        index=pd.to_datetime(ms["strategies"]["strategy_a"]["dates"]))

    # Recompute Strategy A with RELATIVE breadth (variant)
    print("Recomputing A with relative breadth ...")
    closes, breadths, etfs_used = build_panels()
    first_valids = {col: breadths[col].first_valid_index() for col in breadths.columns}
    latest_first = max(d for d in first_valids.values() if d is not None)
    eligible_idx = closes.index.searchsorted(latest_first) + MA_PERIOD
    eligible = closes.index[eligible_idx]
    rel_breadths = breadths.sub(breadths.mean(axis=1, skipna=True), axis=0)
    a_var_run = run_portfolio(closes, rel_breadths, top_k_breadth_weight(7),
                                eligible, cost=COST_BPS / 10_000,
                                rebalance_freq="W-FRI")
    a_var = a_var_run["equity"].loc[a_var_run["equity"].index >= eligible]

    # SHY for overlay fallback
    ac = pd.read_parquet(DATA_DIR / "asset_class_prices_cache.parquet")
    shy = ac["SHY"].dropna()

    # CSP1 breadth for regime
    csp1 = json.loads((DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"])).dropna()

    # Common window for blend
    common = a_base.index.intersection(b.index).intersection(c.index).intersection(d.index)
    print(f"Common window: {common[0].date()} -> {common[-1].date()}")
    a_var_common = a_var.reindex(common).ffill()

    # Build both blends
    def blend(a_eq, b_eq, c_eq, d_eq):
        ar = a_eq.reindex(common).pct_change().fillna(0)
        br = b_eq.reindex(common).pct_change().fillna(0)
        cr = c_eq.reindex(common).pct_change().fillna(0)
        dr = d_eq.reindex(common).pct_change().fillna(0)
        return (1.0 + (0.35*ar + 0.35*br + 0.10*cr + 0.20*dr)).cumprod()

    base_blend = blend(a_base, b, c, d)
    var_blend = blend(a_var_common, b, c, d)
    # Apply overlay to both
    breadth_aligned = breadth.reindex(common, method="ffill")
    states = _regime(breadth_aligned, OFF, ON)
    base_gated = _gate(base_blend, shy, states, DERISK)
    var_gated = _gate(var_blend, shy, states, DERISK)

    # Stats per window
    def print_block(label, ungated, gated):
        print(f"\n  {label}")
        for w, start, end in WINDOWS:
            ug = _ws(ungated, start, end)
            g = _ws(gated, start, end)
            if ug["sharpe"] is None: continue
            print(f"    {w:<14s}  ungated Sh{ug['sharpe']:+.3f} Tot{ug['total']*100:+.1f}% DD{ug['dd']*100:.1f}%   "
                  f"gated Sh{g['sharpe']:+.3f} Tot{g['total']*100:+.1f}% DD{g['dd']*100:.1f}%")

    print("\n" + "=" * 110)
    print("BLEND IMPACT — variant A spliced into 4-way 35/35/10/20 blend")
    print("=" * 110)
    print_block("BASELINE (absolute breadth in A)", base_blend, base_gated)
    print_block("VARIANT  (relative breadth in A)", var_blend, var_gated)

    print("\n  DELTAS (variant - baseline):")
    for w, start, end in WINDOWS:
        ub = _ws(base_blend, start, end); uv = _ws(var_blend, start, end)
        gb = _ws(base_gated, start, end); gv = _ws(var_gated, start, end)
        if ub["sharpe"] is None: continue
        d_ung_sh = uv["sharpe"] - ub["sharpe"]; d_ung_t = (uv["total"] - ub["total"]) * 100
        d_g_sh = gv["sharpe"] - gb["sharpe"]; d_g_t = (gv["total"] - gb["total"]) * 100
        d_ung_dd = (uv["dd"] - ub["dd"]) * 100; d_g_dd = (gv["dd"] - gb["dd"]) * 100
        print(f"    {w:<14s}  ungated dSh{d_ung_sh:+.3f} dTot{d_ung_t:+.1f}pp dDD{d_ung_dd:+.1f}pp   "
              f"gated dSh{d_g_sh:+.3f} dTot{d_g_t:+.1f}pp dDD{d_g_dd:+.1f}pp")


if __name__ == "__main__":
    main()
