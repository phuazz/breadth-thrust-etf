"""WS18 Amendment 2 — does the verdict survive the gate and the tilt?

A CONDITION ON ADOPTION, not an optional extra. The WS18 comparison ran on the
UNGATED 35/35/10/20 blend, because the WS13 prior its bar was calibrated
against is on that basis and judging across bases is the like-for-like
violation the study forbids.

WHY IT STILL HAS TO BE CHECKED. The Phase 19 gate and the Phase 22 tilt are
driven by cadence-independent daily series, so they apply identically to every
arm. It is tempting to conclude the ordering must therefore carry. IT DOES NOT
FOLLOW: a common multiplicative exposure overlay is not a monotone transform of
a Sharpe DIFFERENCE. Two curves whose difference is +0.02 ungated can order
differently once both are scaled by the same time-varying series, because the
scaling reweights which periods dominate the variance. So it is measured.

NOTHING IS REIMPLEMENTED. The tilt comes from run_risk_overlay's own
_build_eem_tilted_blend and the gate from that module's own formula and
constants, so this check cannot quietly diverge from the deployed overlay — the
failure mode that produced two miscalibrated caps in this repo inside a
fortnight.

Usage:
    python scripts/run_ws18_gated_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rebalance_calendar  # noqa: E402
import run_risk_overlay as ro  # noqa: E402
from run_ws10_holiday_cadence import SLEEVES  # noqa: E402
from run_ws18_monday_cadence import (ARMS, BLEND, TRADING_DAYS,  # noqa: E402
                                     _maxdd, _sharpe, run_arm)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data_local" / "ws18_gated_check.json"
SLEEVE_KEYS = {"a": "strategy_a", "b": "strategy_b", "c": "strategy_c", "d": "strategy_d"}


def _synthetic_multi(curves_for_arm: dict[str, pd.Series]) -> dict:
    """Shape one arm's sleeve curves like multi_strategy.json.

    _build_eem_tilted_blend reads `strategies.<key>.dates/equity`, so handing
    it this is the same code path the deployed overlay takes.
    """
    return {"strategies": {
        SLEEVE_KEYS[k]: {"dates": [d.strftime("%Y-%m-%d") for d in s.index],
                         "equity": [float(x) for x in s.values]}
        for k, s in curves_for_arm.items()}}


def _gate(blend_eq: pd.Series, breadth: pd.Series,
          fallback: pd.Series) -> pd.Series:
    """Phase 19 gated returns — run_risk_overlay's formula, its constants."""
    common = blend_eq.index
    b = ro.align_series_to_index(breadth, common,
                                 max_stale_days=ro.GATE_MAX_STALE_DAYS)
    fb = fallback.reindex(common, method="ffill")
    blend_ret = blend_eq.pct_change().fillna(0)
    fallback_ret = fb.pct_change().fillna(0)

    states = ro._compute_states(b, ro.OFF_THRESHOLD, ro.ON_THRESHOLD)
    states_lagged = states.shift(1).fillna(1.0)
    changes = states_lagged.diff().fillna(0).abs()
    switch_cost = changes * (ro.SWITCH_COST_BPS / 10_000.0)

    blend_w = states_lagged + (1.0 - states_lagged) * (1.0 - ro.DERISK_FRACTION)
    fallback_w = (1.0 - states_lagged) * ro.DERISK_FRACTION
    return blend_w * blend_ret + fallback_w * fallback_ret - switch_cost


def main() -> int:
    print("WS18 Amendment 2 — gated + tilted comparison\n")

    # ---- rebuild each arm's sleeve curves on one pinned frame -------------
    curves: dict[str, dict[str, pd.Series]] = {}
    for key in ("a", "b", "c", "d"):
        module, patch_module, closes, eligible, run, label, cal = SLEEVES[key]()
        curves[key] = {}
        for arm, freq, mode, _ in ARMS:
            r = run_arm(patch_module, cal, freq, mode, run)
            curves[key][arm] = r["equity"].dropna()
        print(f"  sleeve {key.upper()} rebuilt for {len(ARMS)} arms")

    # ---- overlay inputs, identical across arms by construction -----------
    eem_prices, eem_ratio = ro._load_eem_data()
    if eem_prices is None:
        print("ERROR: EEM data unavailable — cannot run the tilt leg.", file=sys.stderr)
        return 2
    eem_signal = ro._compute_eem_tilt_signal(eem_ratio)
    breadth = ro.load_gate_breadth() if hasattr(ro, "load_gate_breadth") else None
    if breadth is None:
        blob = json.loads((ROOT / "data" / "breadth_csp1.json").read_text(encoding="utf-8"))
        s = blob["series"]
        breadth = pd.Series(s["ma_breadth"], index=pd.to_datetime(s["dates"])).dropna()
    fb_cache = ROOT / "data" / f"risk_overlay_{ro.FALLBACK_TICKER.lower()}_cache.parquet"
    fallback = pd.read_parquet(fb_cache)[ro.FALLBACK_TICKER].dropna()
    print(f"  gate breadth to {breadth.index.max().date()}, "
          f"{ro.FALLBACK_TICKER} to {fallback.index.max().date()}, "
          f"EEM to {eem_prices.index.max().date()}\n")

    # ---- per arm: tilt, then gate ----------------------------------------
    results, series = {}, {}
    for arm, _, _, note in ARMS:
        per_arm = {k: curves[k][arm] for k in BLEND}
        common = None
        for s in per_arm.values():
            common = s.index if common is None else common.intersection(s.index)
        tilted = ro._build_eem_tilted_blend(
            _synthetic_multi(per_arm), eem_prices, eem_signal, common)
        if tilted is None:
            print(f"ERROR: tilt returned None for {arm}", file=sys.stderr)
            return 2
        gated_ret = _gate(tilted, breadth, fallback).dropna()
        series[arm] = gated_ret
        eq = (1.0 + gated_ret).cumprod()
        results[arm] = {"note": note, "sharpe": round(_sharpe(gated_ret), 4),
                        "max_dd": round(_maxdd(eq), 4)}

    # Align so the bootstrap is genuinely paired.
    common = None
    for s in series.values():
        common = s.index if common is None else common.intersection(s.index)
    for arm in series:
        series[arm] = series[arm].loc[common]
        eq = (1.0 + series[arm]).cumprod()
        results[arm]["sharpe"] = round(_sharpe(series[arm]), 4)
        results[arm]["max_dd"] = round(_maxdd(eq), 4)
        results[arm]["cagr"] = round(
            float(eq.iloc[-1] ** (TRADING_DAYS / len(series[arm])) - 1.0), 4)

    print(f"gated + tilted, common window {common.min().date()} to "
          f"{common.max().date()} ({len(common)} sessions)\n")
    for arm, _, _, note in ARMS:
        r = results[arm]
        print(f"  {arm:15s} ({note:9s}) Sharpe {r['sharpe']:+.4f}  "
              f"CAGR {r['cagr']:+.2%}  DD {r['max_dd']:+.2%}")

    from run_phase7_bootstrap import (BLOCK_SIZE, N_SAMPLES, RNG_SEED,
                                      paired_bootstrap_diff)
    rng = np.random.default_rng(RNG_SEED)
    tests = {}
    for a, b in (("arm3_mon_next", "arm1_fri_aware"),
                 ("arm2_fri_next", "arm1_fri_aware")):
        tests[f"{a} minus {b}"] = paired_bootstrap_diff(
            series[a].to_numpy(), series[b].to_numpy(), BLOCK_SIZE, N_SAMPLES, rng)
    print(f"\npaired block bootstrap — block {BLOCK_SIZE}, {N_SAMPLES} samples, "
          f"seed {RNG_SEED}")
    for name, t in tests.items():
        lo, hi = t["delta_p5"], t["delta_p95"]
        print(f"  {name:34s} point {t['delta_point']:+.4f}  "
              f"90% CI [{lo:+.4f}, {hi:+.4f}] "
              f"{'clear of zero' if (lo > 0 or hi < 0) else 'straddles zero'}")

    a1 = results["arm3_mon_next"]["sharpe"] - results["arm1_fri_aware"]["sharpe"]
    a2 = results["arm3_mon_next"]["max_dd"] - results["arm1_fri_aware"]["max_dd"]
    print("\nFROZEN BAR on the DEPLOYED variant, arm 3 against arm 1:")
    print(f"  A1 Sharpe delta {a1:+.4f}  bar -0.05  {'PASS' if a1 >= -0.05 else 'FAIL'}")
    print(f"  A2 MaxDD delta  {a2:+.4f}  bar -0.02  {'PASS' if a2 >= -0.02 else 'FAIL'}")
    verdict = ("HOLDS — the ungated verdict survives the gate and the tilt"
               if a1 >= -0.05 and a2 >= -0.02 else
               "REVERSES — adoption condition FAILS, do not proceed")
    print(f"\nVERDICT: {verdict}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"basis": "gated + EEM-tilted (deployed variant)",
                               "arms": results, "tests": tests,
                               "bar": {"a1": a1, "a2": a2}, "verdict": verdict,
                               "window": {"start": str(common.min().date()),
                                          "end": str(common.max().date()),
                                          "sessions": int(len(common))}},
                              indent=2, default=str), encoding="utf-8")
    print(f"Wrote {OUT}")
    return 0 if a1 >= -0.05 and a2 >= -0.02 else 1


if __name__ == "__main__":
    raise SystemExit(main())
