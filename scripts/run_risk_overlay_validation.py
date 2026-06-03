"""Risk Overlay (breadth regime gate) — walk-forward validation.

The deployed regime gate uses three parameters chosen from a 12-
variant in-sample sweep:
  - off_threshold = 0.20   (de-risk when S&P 500 breadth falls below)
  - on_threshold  = 0.50   (re-engage when breadth crosses back above)
  - derisk_fraction = 0.50 (move 50% of NAV to SHY when OFF)

The Caveats accordion already acknowledges these thresholds were
in-sample-tuned. This script walk-forward refits them annually on
an expanding train window and compares:

  - Baseline (no gate)              -- the un-gated deployed blend
  - Deployed fixed thresholds (20/50/50)  -- what we ship today
  - Walk-forward refit (annual)     -- pick best (off, on) by train Sharpe
                                       each year, apply to next 12 months

Question: does the in-sample-tuned (20/50/50) Sharpe contribution
of ~+0.10 survive a realistic out-of-sample test? If yes, the gate
is more rigorous than the caveat suggests. If no, the deployed
contribution is partially in-sample noise — important to know
before any audit / CMS review.

Parameter grid (deliberately small + defensible):
  off thresholds: 0.15, 0.20, 0.25, 0.30
  on thresholds:  0.45, 0.50, 0.55
  derisk fraction: 0.50 (kept fixed; that one is a structural
                          choice, not a tuneable hyperparameter)

Run:
    python scripts/run_risk_overlay_validation.py

Output: data/risk_overlay_validation.json + printed table.
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
    _compute_states, OFF_THRESHOLD, ON_THRESHOLD, DERISK_FRACTION,
    SWITCH_COST_BPS, FALLBACK_TICKER, UNDERLYING_BLEND_KEY,
)

# Match run_risk_overlay.py's hardcoded source path for the SPY/CSP1
# constituent-breadth time series.
BREADTH_SOURCE = "breadth_csp1.json"


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


# ---------------------------------------------------------------------------
# Parameter grid + refit cadence
# ---------------------------------------------------------------------------


OFF_GRID = [0.15, 0.20, 0.25, 0.30]
ON_GRID = [0.45, 0.50, 0.55]
DERISK_FRACTION_FIXED = 0.50

# Match other walk-forward harnesses in this repo
INITIAL_TRAIN_YEARS = 5
REFIT_FREQ = "YE"  # annual at year-end


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


def _load_blend_equity() -> tuple[pd.Series, str]:
    """Load the ungated deployed blend (35/35/10/20) daily equity series.
    Returns (equity, label) — the series we apply the overlay to."""
    with open(DATA_DIR / "multi_strategy.json", encoding="utf-8") as fh:
        d = json.load(fh)
    strats = d.get("strategies") or {}
    v = strats.get(UNDERLYING_BLEND_KEY)
    if not v or not v.get("equity"):
        raise SystemExit(f"Missing {UNDERLYING_BLEND_KEY} equity in "
                          "multi_strategy.json — rerun run_multi_strategy.py")
    eq = pd.Series(v["equity"],
                    index=pd.to_datetime(v["dates"]),
                    name="blend").sort_index()
    return eq, v.get("label", UNDERLYING_BLEND_KEY)


def _load_breadth() -> pd.Series:
    """Load the S&P 500 (CSP1) constituent breadth series — daily
    fraction of S&P 500 constituents above their own 200d MA."""
    src_path = DATA_DIR / BREADTH_SOURCE
    with open(src_path, encoding="utf-8") as fh:
        d = json.load(fh)
    s = d.get("series") or {}
    dates = s.get("dates")
    vals = s.get("ma_breadth")
    if not dates or not vals:
        raise SystemExit(f"Missing series in {src_path}")
    out = pd.Series(vals, index=pd.to_datetime(dates),
                     name="csp1_breadth")
    return out


def _load_fallback() -> pd.Series:
    """Load the cash-fallback (SHY) price series."""
    cache = DATA_DIR / f"risk_overlay_{FALLBACK_TICKER.lower()}_cache.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if FALLBACK_TICKER in df.columns:
            return df[FALLBACK_TICKER].dropna()
    # Try the holdings-prices cache as a fallback
    holdings = DATA_DIR / "holdings_prices_1y.json"
    if holdings.exists():
        with open(holdings, encoding="utf-8") as fh:
            hp = json.load(fh)
        t = (hp.get("tickers") or {}).get(FALLBACK_TICKER)
        if t and t.get("dates"):
            return pd.Series(t["closes"], index=pd.to_datetime(t["dates"]),
                              name=FALLBACK_TICKER).sort_index()
    raise SystemExit(f"No {FALLBACK_TICKER} cache — run "
                     "scripts/run_risk_overlay.py once to populate it.")


# ---------------------------------------------------------------------------
# Gate equity calculation given parameters
# ---------------------------------------------------------------------------


def _gated_equity(blend_ret: pd.Series, fallback_ret: pd.Series,
                    breadth: pd.Series, off: float, on: float,
                    derisk_fraction: float) -> pd.Series:
    """Apply the breadth gate to the blend's daily returns.

    Matches the deployed logic in run_risk_overlay.py — hysteresis
    state machine, lag by 1 day to avoid look-ahead, blend weight =
    state + (1-state) * (1-derisk_fraction), fallback weight =
    (1-state) * derisk_fraction, with a switch cost on every state
    change. Returns the cumulative gated equity series."""
    states = _compute_states(breadth, off, on)
    states_lagged = states.shift(1).fillna(1.0)
    state_changes = states_lagged.diff().fillna(0).abs()
    switch_cost = state_changes * (SWITCH_COST_BPS / 10_000.0)
    blend_w = states_lagged + (1.0 - states_lagged) * (1.0 - derisk_fraction)
    fallback_w = (1.0 - states_lagged) * derisk_fraction
    gated_ret = blend_w * blend_ret + fallback_w * fallback_ret - switch_cost
    return (1.0 + gated_ret).cumprod()


def _sharpe(ret: pd.Series) -> float:
    if len(ret) < 5 or ret.std() == 0 or pd.isna(ret.std()):
        return float("nan")
    return float(ret.mean() / ret.std() * math.sqrt(252))


def _max_dd(equity: pd.Series) -> float:
    if len(equity) == 0:
        return float("nan")
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min())


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


def walk_forward(blend_eq: pd.Series, breadth: pd.Series,
                  fallback_eq: pd.Series) -> dict:
    """Annual refit of (off, on) thresholds on expanding train window.

    Each refit:
      1. Train window: from common_start to train_end (expanding).
      2. Grid-search every (off, on) combo, computing the gate's
         train-window Sharpe.
      3. Pick the best (off, on). Apply to the next 12-month test
         window. Record test-window Sharpe.
    Concatenate all test pieces to get WF equity + WF Sharpe.

    Also computes:
      - Baseline (no gate) on the same test pieces
      - Deployed-fixed (20/50/50) gate on the same test pieces
    so the WF result can be compared apples-to-apples.
    """
    common = blend_eq.index.intersection(breadth.index).intersection(
        fallback_eq.index,
    )
    blend = blend_eq.reindex(common)
    breadth_a = breadth.reindex(common, method="ffill")
    fallback = fallback_eq.reindex(common, method="ffill")

    blend_ret = blend.pct_change().fillna(0)
    fallback_ret = fallback.pct_change().fillna(0)

    if len(common) < 252 * (INITIAL_TRAIN_YEARS + 1):
        raise SystemExit("Insufficient common window for walk-forward "
                          "(need ≥ 6 years).")

    initial_train_end = common[0] + pd.DateOffset(years=INITIAL_TRAIN_YEARS)
    last_date = common[-1]
    refit_targets = pd.date_range(initial_train_end, last_date,
                                    freq=REFIT_FREQ)
    refit_ends = []
    for r in refit_targets:
        idx = common.searchsorted(r, side="right") - 1
        if 0 <= idx < len(common):
            refit_ends.append(common[idx])

    segments = []
    wf_pieces = []
    baseline_pieces = []
    fixed_pieces = []
    wf_eq_prev = 1.0
    baseline_prev = 1.0
    fixed_prev = 1.0

    for i, train_end in enumerate(refit_ends):
        train_end_idx = common.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(common):
            break
        test_start = common[test_start_idx]
        if test_start > test_end:
            continue

        # ---- Grid search on training window
        best_combo, best_sh = None, -1e9
        train_window_mask = (common >= common[0]) & (common <= train_end)
        for off, on in product(OFF_GRID, ON_GRID):
            if off >= on:  # off must be strictly below on (hysteresis)
                continue
            eq = _gated_equity(blend_ret, fallback_ret, breadth_a,
                                 off, on, DERISK_FRACTION_FIXED)
            train_eq = eq.loc[train_window_mask]
            if len(train_eq) < 5:
                continue
            train_eq = train_eq / train_eq.iloc[0]
            train_ret = train_eq.pct_change().fillna(0)
            sh = _sharpe(train_ret)
            if not np.isnan(sh) and sh > best_sh:
                best_sh, best_combo = sh, (off, on)

        if best_combo is None:
            continue
        off, on = best_combo

        # ---- Apply winning combo to the test window
        full_eq = _gated_equity(blend_ret, fallback_ret, breadth_a,
                                   off, on, DERISK_FRACTION_FIXED)
        test_eq_raw = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq_raw / base_val
        test_ret = test_eq.pct_change().fillna(0)
        test_sh = _sharpe(test_ret)

        # Baseline (un-gated) on the same test slice
        baseline_test_raw = blend.loc[test_start:test_end]
        base_val_b = float(blend.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        baseline_test_eq = baseline_test_raw / base_val_b
        baseline_ret = baseline_test_eq.pct_change().fillna(0)

        # Deployed-fixed (20/50/50) on the same test slice
        fixed_full = _gated_equity(blend_ret, fallback_ret, breadth_a,
                                       OFF_THRESHOLD, ON_THRESHOLD,
                                       DERISK_FRACTION)
        fixed_test_raw = fixed_full.loc[test_start:test_end]
        base_val_f = float(fixed_full.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        fixed_test_eq = fixed_test_raw / base_val_f
        fixed_ret = fixed_test_eq.pct_change().fillna(0)

        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_off": off,
            "best_on": on,
            "train_sharpe": round(best_sh, 4),
            "test_sharpe_wf": round(test_sh, 4),
            "test_sharpe_baseline": round(_sharpe(baseline_ret), 4),
            "test_sharpe_fixed": round(_sharpe(fixed_ret), 4),
            "n_test_days": int(len(test_eq_raw)),
        })

        wf_pieces.append(test_eq * wf_eq_prev / test_eq.iloc[0])
        wf_eq_prev = wf_pieces[-1].iloc[-1]
        baseline_pieces.append(baseline_test_eq * baseline_prev / baseline_test_eq.iloc[0])
        baseline_prev = baseline_pieces[-1].iloc[-1]
        fixed_pieces.append(fixed_test_eq * fixed_prev / fixed_test_eq.iloc[0])
        fixed_prev = fixed_pieces[-1].iloc[-1]

    if not wf_pieces:
        return {}

    wf_eq = pd.concat(wf_pieces)
    baseline_eq = pd.concat(baseline_pieces)
    fixed_eq = pd.concat(fixed_pieces)
    wf_ret = wf_eq.pct_change().fillna(0)
    baseline_ret = baseline_eq.pct_change().fillna(0)
    fixed_ret = fixed_eq.pct_change().fillna(0)

    return {
        "segments": segments,
        "wf_sharpe":       round(_sharpe(wf_ret), 4),
        "baseline_sharpe": round(_sharpe(baseline_ret), 4),
        "fixed_sharpe":    round(_sharpe(fixed_ret), 4),
        "wf_max_dd":       round(_max_dd(wf_eq), 4),
        "baseline_max_dd": round(_max_dd(baseline_eq), 4),
        "fixed_max_dd":    round(_max_dd(fixed_eq), 4),
        "wf_total_return":       round(float(wf_eq.iloc[-1] - 1), 4),
        "baseline_total_return": round(float(baseline_eq.iloc[-1] - 1), 4),
        "fixed_total_return":    round(float(fixed_eq.iloc[-1] - 1), 4),
        "common_start": common[0].strftime("%Y-%m-%d"),
        "common_end":   common[-1].strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("RISK OVERLAY (regime gate) — walk-forward validation")
    print("=" * 78)
    print(f"Underlying blend: {UNDERLYING_BLEND_KEY}")
    print(f"Off threshold grid: {OFF_GRID}")
    print(f"On  threshold grid: {ON_GRID}")
    print(f"Derisk fraction fixed: {DERISK_FRACTION_FIXED}")
    print(f"Initial train: {INITIAL_TRAIN_YEARS}y, then annual refit")
    print(f"Deployed fixed thresholds: off={OFF_THRESHOLD}, "
          f"on={ON_THRESHOLD}, derisk={DERISK_FRACTION}")
    print()

    print("Loading blend equity ...", flush=True)
    blend_eq, blend_label = _load_blend_equity()
    print(f"  {blend_label}: {blend_eq.index[0].date()} -> "
          f"{blend_eq.index[-1].date()}, {len(blend_eq)} days")

    print("Loading SPY breadth ...", flush=True)
    breadth = _load_breadth()
    print(f"  {breadth.index[0].date()} -> {breadth.index[-1].date()}, "
          f"{len(breadth)} days")

    print("Loading fallback prices ...", flush=True)
    fallback_eq = _load_fallback()
    print(f"  {fallback_eq.index[0].date()} -> "
          f"{fallback_eq.index[-1].date()}, {len(fallback_eq)} days")

    print()
    print("Running walk-forward ...", flush=True)
    result = walk_forward(blend_eq, breadth, fallback_eq)
    if not result:
        print("ERROR: no walk-forward result")
        return 1

    # ---- Summary table ----
    print()
    print("=" * 78)
    print(f"RESULTS  (common window {result['common_start']} -> "
          f"{result['common_end']})")
    print("=" * 78)
    print(f"{'Variant':<40} {'Sharpe':>9} {'Max DD':>9} {'Total ret':>11}")
    print("-" * 78)
    print(f"  {'Baseline (no gate)':<38} "
          f"{result['baseline_sharpe']:>+9.4f} "
          f"{result['baseline_max_dd']*100:>+8.2f}% "
          f"{result['baseline_total_return']*100:>+10.2f}%")
    print(f"  {'Fixed thresholds (20/50/50, deployed)':<38} "
          f"{result['fixed_sharpe']:>+9.4f} "
          f"{result['fixed_max_dd']*100:>+8.2f}% "
          f"{result['fixed_total_return']*100:>+10.2f}%")
    print(f"  {'Walk-forward refit (annual)':<38} "
          f"{result['wf_sharpe']:>+9.4f} "
          f"{result['wf_max_dd']*100:>+8.2f}% "
          f"{result['wf_total_return']*100:>+10.2f}%")
    print()

    delta_fixed_baseline = result['fixed_sharpe'] - result['baseline_sharpe']
    delta_wf_baseline = result['wf_sharpe'] - result['baseline_sharpe']
    delta_wf_fixed = result['wf_sharpe'] - result['fixed_sharpe']
    print("Headline:")
    print(f"  Fixed (deployed) vs baseline:  {delta_fixed_baseline:+.4f} "
          f"Sharpe lift on TEST periods only")
    print(f"  Walk-forward vs baseline:      {delta_wf_baseline:+.4f} "
          f"Sharpe lift")
    print(f"  Walk-forward vs fixed:         {delta_wf_fixed:+.4f} "
          f"(positive = refit beats fixed; "
          f"negative = fixed is good enough)")

    # ---- Per-segment table ----
    print()
    print("Per-segment detail (training-window grid pick + OOS test results):")
    print(f"{'Train end':<12} {'Test':<22} {'Best (off,on)':<16} "
          f"{'Train Sh':>9} {'Test WF':>9} {'Test fix':>9} {'Test base':>10}")
    print("-" * 78)
    for seg in result["segments"]:
        test_range = f"{seg['test_start']}→{seg['test_end']}"
        combo = f"({seg['best_off']:.2f},{seg['best_on']:.2f})"
        print(f"  {seg['train_end']:<10} {test_range:<22} {combo:<16} "
              f"{seg['train_sharpe']:>+9.4f} "
              f"{seg['test_sharpe_wf']:>+9.4f} "
              f"{seg['test_sharpe_fixed']:>+9.4f} "
              f"{seg['test_sharpe_baseline']:>+10.4f}")

    # ---- Interpretation ----
    print()
    print("=" * 78)
    print("INTERPRETATION")
    print("=" * 78)
    if delta_fixed_baseline > 0.05:
        print(f"  Deployed fixed thresholds GENUINELY BEAT baseline by ")
        print(f"  {delta_fixed_baseline:+.4f} Sharpe on out-of-sample test")
        print(f"  segments — this is OOS evidence that the 20/50/50 thresholds")
        print(f"  do something real, not just in-sample fit.")
    elif delta_fixed_baseline < -0.05:
        print(f"  Deployed fixed thresholds UNDERPERFORM baseline by ")
        print(f"  {abs(delta_fixed_baseline):.4f} Sharpe on OOS test segments —")
        print(f"  the in-sample contribution does NOT survive walk-forward.")
        print(f"  Recommend reconsidering deployment of the gate.")
    else:
        print(f"  Deployed fixed thresholds are within noise vs baseline on")
        print(f"  OOS test segments ({delta_fixed_baseline:+.4f} Sharpe).")
        print(f"  The gate's contribution is ambiguous out-of-sample.")
    print()
    if delta_wf_fixed > 0.02:
        print(f"  Annual refit BEATS fixed thresholds OOS by "
              f"{delta_wf_fixed:+.4f} Sharpe — the parameter surface is")
        print(f"  stable enough that walk-forward refit captures it.")
    else:
        print(f"  Annual refit is NOT MEANINGFULLY BETTER than fixed "
              f"(Δ {delta_wf_fixed:+.4f}). The deployed fixed")
        print(f"  thresholds are about as good as we can do with this signal.")

    # ---- Save ----
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "blend_key": UNDERLYING_BLEND_KEY,
        "blend_label": blend_label,
        "deployed_fixed_thresholds": {
            "off": OFF_THRESHOLD, "on": ON_THRESHOLD,
            "derisk_fraction": DERISK_FRACTION,
        },
        "grid": {"off": OFF_GRID, "on": ON_GRID,
                  "derisk_fraction_fixed": DERISK_FRACTION_FIXED},
        "initial_train_years": INITIAL_TRAIN_YEARS,
        "refit_freq": REFIT_FREQ,
        **result,
        "delta_fixed_vs_baseline": delta_fixed_baseline,
        "delta_wf_vs_baseline": delta_wf_baseline,
        "delta_wf_vs_fixed": delta_wf_fixed,
    }
    out_path = DATA_DIR / "risk_overlay_validation.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print()
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
