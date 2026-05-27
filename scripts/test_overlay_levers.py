"""Test 4 levers for addressing 2022-2024 underperformance.

The Phase 19.1 IEF->SHY cash-floor swap captured the high-leverage win.
This script tests four further levers to see if any move the needle:

  Lever 1: Tighter overlay de-risk fraction
    Current 50%; sweep 50% / 75% / 100% (full move to cash).

  Lever 2: Faster overlay trigger
    Current off-threshold 20%; sweep 20% / 25% / 30% / 35%.
    Combined with Lever 1 in a 4×3 = 12-variant grid.

  Lever 3: Alt regime signal (VIX overlay stacked on breadth)
    Add a VIX-based trigger; require 2-of-2 (AND) or 1-of-2 (OR) to fire.
    Test VIX absolute thresholds and VIX-vs-20d-MA thresholds.

  Lever 4: Smaller K under stress
    Currently B uses K=7 and C uses K=4 always. Test running B with K=3
    and C with K=2 specifically in RISK_OFF state — concentrates into
    the strongest signals when most needed.

For each variant, report Sharpe / WF Sharpe / 2022 / 2022-2024 / DD
deltas vs the current deployed (gated, IEF->SHY, derisk=50%, off=20%,
on=50%) baseline.

Usage:
    python scripts/test_overlay_levers.py [--lever {1,2,3,4,all}]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Current deployed parameters
BASE_OFF = 0.20
BASE_ON = 0.50
BASE_DERISK = 0.50
SWITCH_COST_BPS = 5

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-2024",    "2022-01-01", "2024-12-31"),
    ("2022-onwards", "2022-01-01", None),
]


def _stats(eq: pd.Series) -> dict:
    if len(eq) < 2:
        return {"sharpe": None, "cagr": None, "total": None, "dd": None}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else 0
    sh = daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else 0
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return {"sharpe": sh, "cagr": cagr, "total": eq.iloc[-1] - 1, "dd": dd}


def _window_stats(eq: pd.Series, start, end) -> dict:
    w = eq.loc[start:end].dropna() if (start or end) else eq.dropna()
    return _stats(w) if len(w) >= 5 else {"sharpe": None, "cagr": None,
                                            "total": None, "dd": None}


def _regime_states(breadth: pd.Series, off: float, on: float) -> pd.Series:
    states = []
    state = 1.0
    for v in breadth.values:
        if pd.isna(v):
            states.append(state); continue
        if state == 1.0 and v < off:
            state = 0.0
        elif state == 0.0 and v > on:
            state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index)


def _composite_regime_states(breadth: pd.Series, vix: pd.Series,
                                breadth_off: float, breadth_on: float,
                                vix_off: float, vix_on: float,
                                logic: str = "OR") -> pd.Series:
    """Two-signal gate. logic='OR' fires when EITHER signal flags risk-off;
    logic='AND' requires BOTH. Each signal has independent hysteresis."""
    breadth_state = _regime_states(breadth, breadth_off, breadth_on)
    # VIX: HIGH = risk-off. Flip sign so we use the same hysteresis pattern.
    # Convert to "breadth-like" by negating: high vix -> low pseudo-breadth.
    vix_aligned = vix.reindex(breadth.index, method="ffill")
    # VIX state: 1.0 when calm (vix < vix_off), 0.0 when stressed (vix > vix_on).
    # Note: vix_off is the threshold ABOVE which we go off; vix_on is BELOW which we resume.
    vix_states = []
    state = 1.0
    for v in vix_aligned.values:
        if pd.isna(v):
            vix_states.append(state); continue
        if state == 1.0 and v > vix_off:
            state = 0.0
        elif state == 0.0 and v < vix_on:
            state = 1.0
        vix_states.append(state)
    vix_state = pd.Series(vix_states, index=breadth.index)
    if logic == "OR":
        # Risk-off if EITHER says off -> state = min
        combined = pd.concat([breadth_state, vix_state], axis=1).min(axis=1)
    else:  # AND
        # Risk-off only if BOTH say off -> state = max(1.0,1.0)=1.0, etc.
        # When both 0 -> 0; when one 0 one 1 -> still 1 (ON). Use max:
        combined = pd.concat([breadth_state, vix_state], axis=1).max(axis=1)
    return combined


def _run_gate(blend_eq: pd.Series, fallback_eq: pd.Series,
                states: pd.Series, derisk: float,
                switch_cost_bps: float = SWITCH_COST_BPS) -> pd.Series:
    common = blend_eq.index
    fallback = fallback_eq.reindex(common, method="ffill")
    blend_ret = blend_eq.pct_change().fillna(0)
    fallback_ret = fallback.pct_change().fillna(0)
    states_lagged = states.reindex(common, method="ffill").shift(1).fillna(1.0)
    state_changes = states_lagged.diff().fillna(0).abs()
    switch_cost = state_changes * (switch_cost_bps / 10_000.0)
    blend_w = states_lagged + (1.0 - states_lagged) * (1.0 - derisk)
    fallback_w = (1.0 - states_lagged) * derisk
    gated_ret = blend_w * blend_ret + fallback_w * fallback_ret - switch_cost
    return (1.0 + gated_ret).cumprod()


def _format_row(label, stats_per_window, n_switches=None,
                 pct_off=None, baseline=None):
    """Print a one-line row with all windows + optional baseline deltas."""
    parts = [f"  {label:<38s}"]
    for win in WINDOWS:
        s = stats_per_window[win[0]]
        if s["sharpe"] is None:
            parts.append("n/a".ljust(38))
            continue
        cell = f"Sh{s['sharpe']:+.2f} Tot{s['total']*100:+.1f}% DD{s['dd']*100:.1f}%"
        if baseline:
            b = baseline[win[0]]
            d_sh = s["sharpe"] - b["sharpe"]
            d_tot = (s["total"] - b["total"]) * 100
            cell += f" [d{d_sh:+.2f}/{d_tot:+.1f}pp]"
        parts.append(cell.ljust(38))
    if n_switches is not None:
        parts.append(f"  sw{n_switches} off{pct_off:.0f}%")
    print("  ".join(parts))


def load_data():
    """Load deployed UNGATED blend equity, breadth, SHY, VIX."""
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    blend = multi["strategies"]["blend_35_35_10_20"]
    blend_eq = pd.Series(blend["equity"], index=pd.to_datetime(blend["dates"]))

    csp1 = json.loads((DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"])).dropna()

    ac = pd.read_parquet(DATA_DIR / "asset_class_prices_cache.parquet")
    shy = ac["SHY"].dropna()

    vix_cache = DATA_DIR / "vix_cache.parquet"
    if vix_cache.exists():
        vix = pd.read_parquet(vix_cache)
        vix = vix["VIX"] if "VIX" in vix.columns else vix.iloc[:, 0]
    else:
        print("  Fetching ^VIX from yfinance ...")
        raw = yf.download("^VIX", start="2007-01-01", auto_adjust=True,
                           progress=False, threads=False)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        vix = raw["Close"]
        if isinstance(vix, pd.DataFrame):
            vix = vix.iloc[:, 0]
        vix.index = pd.to_datetime(vix.index).tz_localize(None)
        vix.name = "VIX"
        vix.to_frame().to_parquet(vix_cache)
    return blend_eq, breadth, shy, vix.dropna()


def compute_baseline(blend_eq, breadth, shy):
    """Reproduce the currently-deployed gated variant."""
    breadth_aligned = breadth.reindex(blend_eq.index, method="ffill")
    states = _regime_states(breadth_aligned, BASE_OFF, BASE_ON)
    gated_eq = _run_gate(blend_eq, shy, states, BASE_DERISK)
    states_lagged = states.shift(1).fillna(1.0)
    return {
        "eq": gated_eq,
        "states": states,
        "n_switches": int(states_lagged.diff().fillna(0).abs().sum()),
        "pct_off": (states_lagged == 0).sum() / len(states_lagged) * 100,
    }


def windows_stats(eq: pd.Series) -> dict:
    return {w[0]: _window_stats(eq, w[1], w[2]) for w in WINDOWS}


def lever_1_2(blend_eq, breadth, shy, baseline):
    """Sweep off ∈ {20%, 25%, 30%, 35%} × derisk ∈ {50%, 75%, 100%}."""
    print("\n" + "=" * 200)
    print("LEVER 1 + 2 — off-threshold × de-risk fraction sweep "
          f"(on-threshold fixed at {int(BASE_ON*100)}%)")
    print("=" * 200)
    print(f"  {'Variant':<38s}  " + "  ".join(
        f"{w[0]:<38s}" for w in WINDOWS) + "  switches/off%")
    base_stats = windows_stats(baseline["eq"])
    _format_row(f"BASELINE off=20 on=50 derisk=50", base_stats,
                  baseline["n_switches"], baseline["pct_off"])
    rows = []
    for off in [0.20, 0.25, 0.30, 0.35]:
        for derisk in [0.50, 0.75, 1.00]:
            if off == BASE_OFF and derisk == BASE_DERISK:
                continue
            states = _regime_states(
                breadth.reindex(blend_eq.index, method="ffill"), off, BASE_ON)
            gated = _run_gate(blend_eq, shy, states, derisk)
            wstats = windows_stats(gated)
            states_lagged = states.shift(1).fillna(1.0)
            n_sw = int(states_lagged.diff().fillna(0).abs().sum())
            pct_off = (states_lagged == 0).sum() / len(states_lagged) * 100
            _format_row(
                f"off={int(off*100)}% on={int(BASE_ON*100)}% derisk={int(derisk*100)}%",
                wstats, n_sw, pct_off, base_stats)
            rows.append({"off": off, "derisk": derisk,
                          "stats": wstats, "n_sw": n_sw, "pct_off": pct_off})
    return rows


def lever_3(blend_eq, breadth, shy, vix, baseline):
    """VIX overlay stacked on breadth — OR / AND combinations."""
    print("\n" + "=" * 200)
    print("LEVER 3 — VIX overlay stacked on breadth (CSP1 breadth + VIX, "
          f"breadth held at off={int(BASE_OFF*100)}%/on={int(BASE_ON*100)}%, "
          f"derisk fixed at {int(BASE_DERISK*100)}%)")
    print("=" * 200)
    print(f"  {'Variant':<38s}  " + "  ".join(
        f"{w[0]:<38s}" for w in WINDOWS) + "  switches/off%")
    base_stats = windows_stats(baseline["eq"])
    _format_row(f"BASELINE (breadth only)", base_stats,
                  baseline["n_switches"], baseline["pct_off"])

    # VIX threshold pairs: (vix_off, vix_on) — vix_off = trigger above, vix_on = release below
    vix_thresh_grid = [
        (25, 20), (30, 22), (35, 25), (40, 28),
    ]
    rows = []
    for logic in ["OR", "AND"]:
        for vix_off, vix_on in vix_thresh_grid:
            states = _composite_regime_states(
                breadth.reindex(blend_eq.index, method="ffill"), vix,
                BASE_OFF, BASE_ON, vix_off, vix_on, logic=logic)
            gated = _run_gate(blend_eq, shy, states, BASE_DERISK)
            wstats = windows_stats(gated)
            states_lagged = states.shift(1).fillna(1.0)
            n_sw = int(states_lagged.diff().fillna(0).abs().sum())
            pct_off = (states_lagged == 0).sum() / len(states_lagged) * 100
            _format_row(
                f"{logic}  VIX off>{vix_off} on<{vix_on}",
                wstats, n_sw, pct_off, base_stats)
            rows.append({"logic": logic, "vix_off": vix_off,
                          "vix_on": vix_on, "stats": wstats,
                          "n_sw": n_sw, "pct_off": pct_off})
    return rows


def _compute_concentrated_blend(blend_eq, breadth) -> pd.DataFrame:
    """Compute a 4-way blend where B uses K=3 and C uses K=2 (concentrated)
    instead of B's K=7 / C's K=4 default. Used for Lever 4: splice
    concentrated blend over normal blend in RISK_OFF days.

    Returns: dict with 'concentrated_eq' = pd.Series indexed like blend_eq.
    """
    import importlib
    B_mod = importlib.import_module("run_asset_class_rotation")
    C_mod = importlib.import_module("run_thematic_rotation")

    print("  Computing B with K=3 weekly Fri ...")
    b_panel = B_mod.download_prices()
    b_panel = b_panel.dropna().sort_index()
    b_eligible = b_panel.index[B_mod.MA_PERIOD]
    b_signal = B_mod.compute_signal(b_panel)
    b_r = B_mod.run_rotation(b_panel, b_signal, B_mod.top_k_by_signal(3),
                                b_eligible, rebalance_freq="W-FRI")
    b_eq_k3 = b_r["equity"].loc[b_r["equity"].index >= b_eligible]
    b_eq_k3 = b_eq_k3 / b_eq_k3.iloc[0]

    print("  Computing C with K=2 weekly Fri ...")
    c_panel = C_mod.download_prices()
    c_panel = c_panel.dropna(axis=1, how="all")
    # Determine eligible start the same way C's main() does — exclude
    # late-inception tickers from the constraint
    late = {t for t, m in C_mod.UNIVERSE.items()
            if m.get("late_inception") and t in c_panel.columns}
    core_first = {col: c_panel[col].first_valid_index()
                   for col in c_panel.columns if col not in late}
    latest = max(d for d in core_first.values() if d is not None)
    eligible_idx = c_panel.index.searchsorted(latest) + C_mod.MA_PERIOD
    c_eligible = c_panel.index[eligible_idx]
    c_signal = C_mod.compute_signal(c_panel)
    c_r = C_mod.run_rotation(c_panel, c_signal,
                                C_mod.WEIGHTER_FACTORY(2), c_eligible,
                                rebalance_freq="W-FRI")
    c_eq_k2 = c_r["equity"].loc[c_r["equity"].index >= c_eligible]
    c_eq_k2 = c_eq_k2 / c_eq_k2.iloc[0]

    # Load A, D from multi_strategy.json (unchanged from baseline)
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    a_eq = pd.Series(multi["strategies"]["strategy_a"]["equity"],
                      index=pd.to_datetime(multi["strategies"]["strategy_a"]["dates"]))
    d_eq = pd.Series(multi["strategies"]["strategy_d"]["equity"],
                      index=pd.to_datetime(multi["strategies"]["strategy_d"]["dates"]))

    # Re-blend on common window using 35/35/10/20 weights
    common = blend_eq.index
    a_r = a_eq.reindex(common, method="ffill").pct_change().fillna(0)
    b_r_ret = b_eq_k3.reindex(common, method="ffill").pct_change().fillna(0)
    c_r_ret = c_eq_k2.reindex(common, method="ffill").pct_change().fillna(0)
    d_r = d_eq.reindex(common, method="ffill").pct_change().fillna(0)
    concentrated_ret = (0.35 * a_r + 0.35 * b_r_ret + 0.10 * c_r_ret + 0.20 * d_r)
    concentrated_eq = (1.0 + concentrated_ret).cumprod()
    concentrated_eq.index = common
    return concentrated_eq


def lever_4(blend_eq, breadth, shy, baseline):
    """State-dependent K — splice concentrated B/C blend over normal in RISK_OFF."""
    print("\n" + "=" * 200)
    print("LEVER 4 — state-dependent K (B K=7->3, C K=4->2 in RISK_OFF state)")
    print("=" * 200)
    print(f"  {'Variant':<38s}  " + "  ".join(
        f"{w[0]:<38s}" for w in WINDOWS) + "  switches/off%")
    base_stats = windows_stats(baseline["eq"])
    _format_row(f"BASELINE (no K switching)", base_stats,
                  baseline["n_switches"], baseline["pct_off"])

    print("  Computing concentrated (B K=3, C K=2) blend ...")
    concentrated_eq = _compute_concentrated_blend(blend_eq, breadth)

    rows = []
    # Test L4 alone (no overlay) + L4 stacked with overlay variants
    common = blend_eq.index
    breadth_aligned = breadth.reindex(common, method="ffill")
    # L4 alone — splice concentrated over normal blend on RISK_OFF days, no SHY overlay
    # The "spliced blend" is the underlying that the overlay then de-risks (or doesn't).
    # Variant 4a: L4 alone (no overlay at all)
    states_a = _regime_states(breadth_aligned, BASE_OFF, BASE_ON)
    states_a_lagged = states_a.shift(1).fillna(1.0)
    normal_ret = blend_eq.pct_change().fillna(0)
    conc_ret = concentrated_eq.pct_change().fillna(0)
    # When state == 1.0 (RISK_ON) use normal_ret; when 0.0 (RISK_OFF) use conc_ret
    spliced_ret_a = states_a_lagged * normal_ret + (1.0 - states_a_lagged) * conc_ret
    spliced_eq_a = (1.0 + spliced_ret_a).cumprod()
    spliced_eq_a.index = common
    wstats = windows_stats(spliced_eq_a)
    n_sw = int(states_a_lagged.diff().fillna(0).abs().sum())
    pct_off = (states_a_lagged == 0).sum() / len(states_a_lagged) * 100
    _format_row("L4 alone (K shift only, no SHY)", wstats, n_sw, pct_off, base_stats)
    rows.append({"variant": "L4 alone", "stats": wstats, "n_sw": n_sw, "pct_off": pct_off})

    # Variant 4b: L4 + standard overlay (concentrated blend then 50% SHY)
    gated_b = _run_gate(spliced_eq_a, shy, states_a, BASE_DERISK)
    wstats = windows_stats(gated_b)
    n_sw = int(states_a_lagged.diff().fillna(0).abs().sum())
    pct_off = (states_a_lagged == 0).sum() / len(states_a_lagged) * 100
    _format_row("L4 + overlay (K shift + 50% SHY)", wstats, n_sw, pct_off, base_stats)
    rows.append({"variant": "L4 + overlay", "stats": wstats, "n_sw": n_sw, "pct_off": pct_off})

    # Variant 4c: L4 + tighter overlay (concentrated blend then 75% SHY)
    gated_c = _run_gate(spliced_eq_a, shy, states_a, 0.75)
    wstats = windows_stats(gated_c)
    _format_row("L4 + overlay 75% SHY", wstats, n_sw, pct_off, base_stats)
    rows.append({"variant": "L4 + overlay 75% SHY", "stats": wstats, "n_sw": n_sw, "pct_off": pct_off})

    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lever", default="all",
                          choices=["1", "2", "12", "3", "4", "all"])
    args = parser.parse_args()

    print("Loading data ...")
    blend_eq, breadth, shy, vix = load_data()
    print(f"  Blend: {blend_eq.index[0].date()} -> {blend_eq.index[-1].date()}  "
          f"({len(blend_eq)} days)")
    print(f"  VIX:   {vix.index[0].date()} -> {vix.index[-1].date()}  "
          f"({len(vix)} days)")

    print("\nComputing current deployed baseline (off=20%/on=50%/derisk=50%, fallback=SHY) ...")
    baseline = compute_baseline(blend_eq, breadth, shy)
    base_stats = windows_stats(baseline["eq"])
    print(f"  Full  Sharpe {base_stats['Full']['sharpe']:+.3f}  "
          f"CAGR {base_stats['Full']['cagr']*100:+.1f}%  "
          f"DD {base_stats['Full']['dd']*100:.1f}%")
    for win in WINDOWS[1:]:
        s = base_stats[win[0]]
        if s["sharpe"] is not None:
            print(f"  {win[0]:<14s} Sharpe {s['sharpe']:+.3f}  "
                  f"Total {s['total']*100:+.1f}%  DD {s['dd']*100:.1f}%")

    out = {"baseline": {"stats": base_stats,
                          "n_switches": baseline["n_switches"],
                          "pct_off": baseline["pct_off"]}}

    if args.lever in ("1", "2", "12", "all"):
        rows12 = lever_1_2(blend_eq, breadth, shy, baseline)
        out["lever12"] = rows12
    if args.lever in ("3", "all"):
        rows3 = lever_3(blend_eq, breadth, shy, vix, baseline)
        out["lever3"] = rows3
    if args.lever in ("4", "all"):
        rows4 = lever_4(blend_eq, breadth, shy, baseline)
        out["lever4"] = rows4

    # Find best by 2022-onwards Sharpe lift while preserving full Sharpe
    print("\n" + "=" * 110)
    print("RANKED — best variants by (2022-onwards total return d + Full Sharpe d × 5)")
    print("=" * 110)
    candidates = []
    for r in out.get("lever12", []):
        label = f"L1+2: off={int(r['off']*100)}% derisk={int(r['derisk']*100)}%"
        candidates.append((label, r["stats"]))
    for r in out.get("lever3", []):
        label = f"L3: {r['logic']} VIX>{r['vix_off']}/{r['vix_on']}"
        candidates.append((label, r["stats"]))
    for r in out.get("lever4", []):
        label = f"L4: {r['variant']}"
        candidates.append((label, r["stats"]))
    scored = []
    for label, s in candidates:
        if s["Full"]["sharpe"] is None or s["2022-onwards"]["total"] is None:
            continue
        d_full_sh = s["Full"]["sharpe"] - base_stats["Full"]["sharpe"]
        d_22on_tot = (s["2022-onwards"]["total"] - base_stats["2022-onwards"]["total"]) * 100
        d_22on_sh = s["2022-onwards"]["sharpe"] - base_stats["2022-onwards"]["sharpe"]
        d_22on_dd = (s["2022-onwards"]["dd"] - base_stats["2022-onwards"]["dd"]) * 100
        d_full_dd = (s["Full"]["dd"] - base_stats["Full"]["dd"]) * 100
        # Score: 22-onwards total + 5x full-sharpe (proxy for OOS preservation)
        score = d_22on_tot + 5 * d_full_sh
        scored.append((score, label, d_full_sh, d_22on_sh, d_22on_tot, d_22on_dd, d_full_dd))
    scored.sort(reverse=True)
    print(f"  {'Variant':<40s}  {'score':>6s}   "
          f"{'Full dSh':>9s}   {'22on dSh':>9s}  {'22on dTot':>10s}  "
          f"{'22on dDD':>9s}  {'Full dDD':>9s}")
    for s, lbl, d_fsh, d_2sh, d_2tot, d_2dd, d_fdd in scored[:10]:
        print(f"  {lbl:<40s}  {s:+6.2f}   {d_fsh:>+8.3f}   "
              f"{d_2sh:>+8.3f}  {d_2tot:>+8.2f}pp  "
              f"{d_2dd:>+7.2f}pp  {d_fdd:>+7.2f}pp")

    return 0


if __name__ == "__main__":
    sys.exit(main())
