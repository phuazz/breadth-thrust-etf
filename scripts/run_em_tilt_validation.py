"""EM Tilt overlay — walk-forward validation.

The deployed EM tilt fires when the EEM/SPY price ratio's 50-day MA
crosses above its 200-day MA (golden cross). When ON: tilt 10% of
the blend's NAV into EEM, funded by reducing Strategy B from 35% ->
25%. The Caveats accordion has been honest that this signal is
"deployed on weak sample" (~11 distinct switches in 7 years).

This script walk-forward refits the (fast_ma, slow_ma) periods
annually on expanding train windows and reports the OOS contribution
vs (a) baseline (no tilt) and (b) the fixed deployed (50/200) signal.

Same shape as scripts/run_risk_overlay_validation.py — the matching
overlay validation that found "insurance economics" (signal works
in-sample because of rare events, costs Sharpe in OOS years that
happen not to contain those events).

Tilt weight (10%) and fund_from_sleeve (B) are NOT in the grid —
those are structural sizing/sourcing choices, not signal tuning.
Only the MA windows are walk-forward refit.

Grid:
  fast in [30, 50, 70, 100]
  slow in [150, 200, 250]
  (skip combos where fast >= slow; hysteresis requires fast < slow)

Run:
    python scripts/run_em_tilt_validation.py

Output: data/em_tilt_validation.json + printed table.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from run_risk_overlay import (  # noqa: E402
    EEM_TICKER, EEM_REFERENCE_TICKER, EEM_TILT_FAST_MA, EEM_TILT_SLOW_MA,
    EEM_TILT_WEIGHT, EEM_FUND_FROM_SLEEVE, SWITCH_COST_BPS,
    UNDERLYING_BLEND_KEY, _load_eem_data,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


# Grid for the MA windows. Anchored on the deployed (50, 200) — surround
# it with reasonable alternatives so we can see if the deployed choice
# is a knife-edge or a robust local optimum.
FAST_GRID = [30, 50, 70, 100]
SLOW_GRID = [150, 200, 250]

# Initial train window — match other walk-forward harnesses in the repo.
INITIAL_TRAIN_YEARS = 5
REFIT_FREQ = "YE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _golden_cross_signal(ratio: pd.Series, fast: int, slow: int) -> pd.Series:
    """1 when fast MA > slow MA, else 0. Same logic as the deployed
    _compute_eem_tilt_signal but with parametrised MA windows."""
    f = ratio.rolling(fast, min_periods=fast).mean()
    s = ratio.rolling(slow, min_periods=slow).mean()
    return (f > s).astype(float)


def _build_tilted_blend(
    sleeves: dict[str, pd.Series],
    eem_prices: pd.Series,
    signal: pd.Series,
    common: pd.DatetimeIndex,
    tilt_weight: float = EEM_TILT_WEIGHT,
    fund_from_sleeve: str = EEM_FUND_FROM_SLEEVE,
    switch_cost_bps: float = SWITCH_COST_BPS,
) -> pd.Series:
    """Same construction as run_risk_overlay._build_eem_tilted_blend but
    takes sleeves dict directly so we can call it inside the WF loop
    without re-loading multi_strategy.json on every iteration."""
    sleeve_weights = {
        "strategy_a": 0.35, "strategy_b": 0.35,
        "strategy_c": 0.10, "strategy_d": 0.20,
    }
    rets = {k: s.reindex(common).pct_change().fillna(0)
            for k, s in sleeves.items()}
    eem_ret = eem_prices.reindex(common, method="ffill").pct_change().fillna(0)
    sig = signal.reindex(common, method="ffill").fillna(0).shift(1).fillna(0)

    tilt_off_ret = sum(sleeve_weights[k] * rets[k] for k in sleeve_weights)
    base_w = sleeve_weights[fund_from_sleeve]
    tilt_on_w = {**sleeve_weights, fund_from_sleeve: base_w - tilt_weight}
    tilt_on_ret = sum(tilt_on_w[k] * rets[k] for k in tilt_on_w) + tilt_weight * eem_ret

    sw = sig.diff().fillna(0).abs() * (switch_cost_bps / 10_000.0)
    blended_ret = sig * tilt_on_ret + (1.0 - sig) * tilt_off_ret - sw
    return (1.0 + blended_ret).cumprod()


def _baseline_blend(sleeves: dict[str, pd.Series],
                      common: pd.DatetimeIndex) -> pd.Series:
    """Un-tilted 35/35/10/20 blend on the common index."""
    sleeve_weights = {
        "strategy_a": 0.35, "strategy_b": 0.35,
        "strategy_c": 0.10, "strategy_d": 0.20,
    }
    rets = {k: s.reindex(common).pct_change().fillna(0)
            for k, s in sleeves.items()}
    blended_ret = sum(sleeve_weights[k] * rets[k] for k in sleeve_weights)
    return (1.0 + blended_ret).cumprod()


def _sharpe(ret: pd.Series) -> float:
    if len(ret) < 5 or ret.std() == 0 or pd.isna(ret.std()):
        return float("nan")
    return float(ret.mean() / ret.std() * math.sqrt(252))


def _max_dd(equity: pd.Series) -> float:
    if len(equity) == 0:
        return float("nan")
    peak = equity.cummax()
    return float(((equity - peak) / peak).min())


def _load_sleeves() -> dict[str, pd.Series]:
    with open(DATA_DIR / "multi_strategy.json", encoding="utf-8") as fh:
        m = json.load(fh)
    out = {}
    for k in ("strategy_a", "strategy_b", "strategy_c", "strategy_d"):
        v = m.get("strategies", {}).get(k)
        if not v or not v.get("equity"):
            raise SystemExit(f"Missing {k} equity in multi_strategy.json — "
                              "rerun run_multi_strategy.py")
        out[k] = pd.Series(v["equity"],
                            index=pd.to_datetime(v["dates"])).sort_index()
    return out


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


def walk_forward(sleeves: dict[str, pd.Series],
                  eem_prices: pd.Series,
                  ratio: pd.Series) -> dict:
    """Annual refit of (fast, slow) MA on expanding train window."""
    common = sleeves["strategy_a"].index
    for k in ("strategy_b", "strategy_c", "strategy_d"):
        common = common.intersection(sleeves[k].index)
    common = common.intersection(eem_prices.index).intersection(ratio.index)
    if len(common) < 252 * (INITIAL_TRAIN_YEARS + 1):
        raise SystemExit("Insufficient common window for walk-forward.")

    initial_train_end = common[0] + pd.DateOffset(years=INITIAL_TRAIN_YEARS)
    last_date = common[-1]
    refit_targets = pd.date_range(initial_train_end, last_date, freq=REFIT_FREQ)
    refit_ends = []
    for r in refit_targets:
        idx = common.searchsorted(r, side="right") - 1
        if 0 <= idx < len(common):
            refit_ends.append(common[idx])

    # Pre-build the baseline once (it doesn't depend on grid params)
    baseline_full = _baseline_blend(sleeves, common)

    segments = []
    wf_pieces, fixed_pieces, baseline_pieces = [], [], []
    wf_prev = fixed_prev = baseline_prev = 1.0

    for i, train_end in enumerate(refit_ends):
        train_end_idx = common.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(common):
            break
        test_start = common[test_start_idx]
        if test_start > test_end:
            continue

        best_combo, best_sh = None, -1e9
        train_mask = (common >= common[0]) & (common <= train_end)
        for fast, slow in product(FAST_GRID, SLOW_GRID):
            if fast >= slow:
                continue
            sig = _golden_cross_signal(ratio, fast, slow)
            eq = _build_tilted_blend(sleeves, eem_prices, sig, common)
            train_eq = eq.loc[train_mask]
            if len(train_eq) < 5:
                continue
            train_eq = train_eq / train_eq.iloc[0]
            train_ret = train_eq.pct_change().fillna(0)
            sh = _sharpe(train_ret)
            if not np.isnan(sh) and sh > best_sh:
                best_sh, best_combo = sh, (fast, slow)

        if best_combo is None:
            continue
        fast, slow = best_combo

        # Apply winning combo to test window
        sig = _golden_cross_signal(ratio, fast, slow)
        full_eq = _build_tilted_blend(sleeves, eem_prices, sig, common)
        test_eq_raw = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq_raw / base_val
        test_sh = _sharpe(test_eq.pct_change().fillna(0))

        # Fixed deployed (50/200) on the same test slice
        fixed_sig = _golden_cross_signal(ratio, EEM_TILT_FAST_MA,
                                            EEM_TILT_SLOW_MA)
        fixed_full = _build_tilted_blend(sleeves, eem_prices, fixed_sig, common)
        fixed_raw = fixed_full.loc[test_start:test_end]
        fixed_base = float(fixed_full.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        fixed_test = fixed_raw / fixed_base
        fixed_sh = _sharpe(fixed_test.pct_change().fillna(0))

        # Baseline (no tilt)
        baseline_raw = baseline_full.loc[test_start:test_end]
        baseline_base = float(baseline_full.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        baseline_test = baseline_raw / baseline_base
        baseline_sh = _sharpe(baseline_test.pct_change().fillna(0))

        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_fast": fast,
            "best_slow": slow,
            "train_sharpe": round(best_sh, 4),
            "test_sharpe_wf": round(test_sh, 4),
            "test_sharpe_fixed": round(fixed_sh, 4),
            "test_sharpe_baseline": round(baseline_sh, 4),
            "n_test_days": int(len(test_eq_raw)),
        })

        wf_pieces.append(test_eq * wf_prev / test_eq.iloc[0])
        wf_prev = wf_pieces[-1].iloc[-1]
        fixed_pieces.append(fixed_test * fixed_prev / fixed_test.iloc[0])
        fixed_prev = fixed_pieces[-1].iloc[-1]
        baseline_pieces.append(baseline_test * baseline_prev / baseline_test.iloc[0])
        baseline_prev = baseline_pieces[-1].iloc[-1]

    if not wf_pieces:
        return {}

    wf_eq = pd.concat(wf_pieces)
    fixed_eq = pd.concat(fixed_pieces)
    baseline_eq = pd.concat(baseline_pieces)

    return {
        "segments": segments,
        "wf_sharpe":       round(_sharpe(wf_eq.pct_change().fillna(0)), 4),
        "fixed_sharpe":    round(_sharpe(fixed_eq.pct_change().fillna(0)), 4),
        "baseline_sharpe": round(_sharpe(baseline_eq.pct_change().fillna(0)), 4),
        "wf_max_dd":       round(_max_dd(wf_eq), 4),
        "fixed_max_dd":    round(_max_dd(fixed_eq), 4),
        "baseline_max_dd": round(_max_dd(baseline_eq), 4),
        "wf_total_return":       round(float(wf_eq.iloc[-1] - 1), 4),
        "fixed_total_return":    round(float(fixed_eq.iloc[-1] - 1), 4),
        "baseline_total_return": round(float(baseline_eq.iloc[-1] - 1), 4),
        "common_start": common[0].strftime("%Y-%m-%d"),
        "common_end":   common[-1].strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("EM TILT — walk-forward validation")
    print("=" * 78)
    print(f"Underlying blend: {UNDERLYING_BLEND_KEY}")
    print(f"Fast MA grid: {FAST_GRID}")
    print(f"Slow MA grid: {SLOW_GRID}")
    print(f"Tilt weight (fixed): {EEM_TILT_WEIGHT}")
    print(f"Fund from sleeve (fixed): {EEM_FUND_FROM_SLEEVE}")
    print(f"Initial train: {INITIAL_TRAIN_YEARS}y, then annual refit")
    print(f"Deployed fixed: fast={EEM_TILT_FAST_MA}, slow={EEM_TILT_SLOW_MA}")
    print()

    print("Loading sleeves ...", flush=True)
    sleeves = _load_sleeves()

    print("Loading EEM + SPY prices ...", flush=True)
    eem_prices, ratio = _load_eem_data()
    if eem_prices is None or ratio is None:
        print("ERROR: cannot load EEM/SPY data", file=sys.stderr)
        return 1
    print(f"  EEM: {eem_prices.index[0].date()} -> {eem_prices.index[-1].date()}")
    print(f"  Ratio: {ratio.index[0].date()} -> {ratio.index[-1].date()}")

    print()
    print("Running walk-forward ...", flush=True)
    result = walk_forward(sleeves, eem_prices, ratio)
    if not result:
        print("ERROR: no walk-forward result")
        return 1

    print()
    print("=" * 78)
    print(f"RESULTS  (common window {result['common_start']} -> "
          f"{result['common_end']}, OOS test segments only)")
    print("=" * 78)
    print(f"{'Variant':<40} {'Sharpe':>9} {'Max DD':>9} {'Total ret':>11}")
    print("-" * 78)
    print(f"  {'Baseline (no tilt)':<38} "
          f"{result['baseline_sharpe']:>+9.4f} "
          f"{result['baseline_max_dd']*100:>+8.2f}% "
          f"{result['baseline_total_return']*100:>+10.2f}%")
    print(f"  {'Fixed (50/200 deployed)':<38} "
          f"{result['fixed_sharpe']:>+9.4f} "
          f"{result['fixed_max_dd']*100:>+8.2f}% "
          f"{result['fixed_total_return']*100:>+10.2f}%")
    print(f"  {'Walk-forward refit (annual)':<38} "
          f"{result['wf_sharpe']:>+9.4f} "
          f"{result['wf_max_dd']*100:>+8.2f}% "
          f"{result['wf_total_return']*100:>+10.2f}%")
    print()

    d_fixed = result["fixed_sharpe"] - result["baseline_sharpe"]
    d_wf = result["wf_sharpe"] - result["baseline_sharpe"]
    d_wf_fixed = result["wf_sharpe"] - result["fixed_sharpe"]
    print("Headline:")
    print(f"  Fixed (deployed) vs baseline:  {d_fixed:+.4f} Sharpe on OOS")
    print(f"  Walk-forward vs baseline:      {d_wf:+.4f} Sharpe on OOS")
    print(f"  Walk-forward vs fixed:         {d_wf_fixed:+.4f} "
          f"(positive = refit beats fixed)")

    print()
    print("Per-segment detail:")
    print(f"{'Train end':<12} {'Test':<22} {'Best (fast,slow)':<18} "
          f"{'Train Sh':>9} {'Test WF':>9} {'Test fix':>9} {'Test base':>10}")
    print("-" * 78)
    for seg in result["segments"]:
        test_range = f"{seg['test_start']}→{seg['test_end']}"
        combo = f"({seg['best_fast']:3d}, {seg['best_slow']:3d})"
        print(f"  {seg['train_end']:<10} {test_range:<22} {combo:<18} "
              f"{seg['train_sharpe']:>+9.4f} "
              f"{seg['test_sharpe_wf']:>+9.4f} "
              f"{seg['test_sharpe_fixed']:>+9.4f} "
              f"{seg['test_sharpe_baseline']:>+10.4f}")

    # ---- Interpretation
    print()
    print("=" * 78)
    print("INTERPRETATION")
    print("=" * 78)
    if abs(d_fixed) < 0.03:
        print(f"  Deployed fixed (50/200) vs baseline: within noise "
              f"(Δ {d_fixed:+.4f} Sharpe).")
        print(f"  The tilt's OOS contribution is essentially flat.")
    elif d_fixed > 0:
        print(f"  Deployed fixed (50/200) BEATS baseline OOS by "
              f"{d_fixed:+.4f} Sharpe.")
    else:
        print(f"  Deployed fixed (50/200) UNDERPERFORMS baseline OOS by "
              f"{abs(d_fixed):.4f} Sharpe.")
    if d_wf_fixed > 0.02:
        print(f"  Annual refit beats fixed by {d_wf_fixed:+.4f} Sharpe — ")
        print(f"  parameter surface has exploitable instability.")
    else:
        print(f"  Annual refit not meaningfully better than fixed "
              f"(Δ {d_wf_fixed:+.4f}).")
    print()
    # Episode-count check
    fixed_sig_all = _golden_cross_signal(ratio, EEM_TILT_FAST_MA, EEM_TILT_SLOW_MA)
    common_check = ratio.index.intersection(eem_prices.index)
    sig_test_total = fixed_sig_all.reindex(common_check, method="ffill").fillna(0)
    n_transitions = int(sig_test_total.diff().fillna(0).abs().sum())
    print(f"  Fired count (full window): {n_transitions} state transitions "
          f"({n_transitions // 2} distinct ON-events)")
    print(f"  This is a structurally rare-event signal — same caveat as the")
    print(f"  Risk Overlay: walk-forward windows likely under-sample the")
    print(f"  events the tilt is designed to catch.")

    # ---- Save
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "blend_key": UNDERLYING_BLEND_KEY,
        "deployed_fixed": {
            "fast_ma": EEM_TILT_FAST_MA,
            "slow_ma": EEM_TILT_SLOW_MA,
            "tilt_weight": EEM_TILT_WEIGHT,
            "fund_from_sleeve": EEM_FUND_FROM_SLEEVE,
        },
        "grid": {"fast": FAST_GRID, "slow": SLOW_GRID},
        "initial_train_years": INITIAL_TRAIN_YEARS,
        "refit_freq": REFIT_FREQ,
        **result,
        "delta_fixed_vs_baseline": d_fixed,
        "delta_wf_vs_baseline": d_wf,
        "delta_wf_vs_fixed": d_wf_fixed,
    }
    out_path = DATA_DIR / "em_tilt_validation.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
