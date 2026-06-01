"""Strategy C exit-rule validation — V6 sensitivity + V6+V3a stack + walk-forward.

Follow-up to scripts/run_thematic_exit_bakeoff.py. The bake-off
identified V6 (sleeve-breadth gate at 30%) as the clear winner with
12.4pp drawdown reduction for a 0.05 Sharpe cost. This script puts
that result through three additional tests before deployment:

1. THRESHOLD SENSITIVITY SWEEP — V6 at 20%, 30%, 40%, 50%, and 60%
   sleeve-breadth thresholds. Confirms the 30% pick is not a knife-
   edge in-sample optimum.

2. V6 + V3a STACKED — combines the sleeve breadth gate (regime-change
   catcher) with a 10% per-ETF trailing stop (single-name blow-up
   catcher). Tests whether the two mechanisms compound.

3. WALK-FORWARD VALIDATION OF V6 (fixed 30%) — annual K refit on
   expanding train window with V6 eligibility rule throughout.
   Confirms the variant survives realistic OOS testing.

4. WALK-FORWARD VALIDATION OF V6 (joint K + threshold refit) —
   annual joint refit of K and threshold. Strictest test: does the
   30% threshold survive being picked OOS each year?

Run:
    python scripts/run_thematic_exit_validation.py

Output: data/thematic_exit_validation.json + comparison table.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from run_thematic_rotation import (  # noqa: E402
    UNIVERSE, TICKERS, CASH_PROXY, SIGNAL_FLOOR, COST_FRAC,
    download_prices, compute_signal, _safe,
)
from run_thematic_exit_bakeoff import (  # noqa: E402
    HEADLINE_K, EPISODE_2021_PEAK, EPISODE_2021_TROUGH,
    compute_metrics, _initial_state,
    _eligible_baseline, _eligible_v3_trailing_stop, _eligible_v6_sleeve_breadth,
    compute_ema, compute_signal_slope, compute_rsi, compute_realised_vol,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


# ---------------------------------------------------------------------------
# Shared rotation engine (lifted from bakeoff for direct K parametrisation)
# ---------------------------------------------------------------------------


def run_rotation_with_eligibility(
    closes: pd.DataFrame,
    signal: pd.DataFrame,
    K: int,
    eligible_start: pd.Timestamp,
    eligible_fn,
    features: dict,
) -> dict:
    """Mirrors run_thematic_exit_bakeoff._run_variant but exposed here
    so callers can vary K (for walk-forward) without re-importing."""
    rebalance_target = pd.date_range(eligible_start, closes.index[-1],
                                      freq="W-FRI")
    rebalance_dates = closes.index[closes.index.isin(rebalance_target)]
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)
    state = _initial_state(list(closes.columns))

    for rd in rebalance_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0:
            continue
        s_row = signal.iloc[prev_idx]
        prev_close = closes.iloc[prev_idx]

        for t in closes.columns:
            px = prev_close.get(t)
            if px is not None and px == px:
                if state[t]["held"]:
                    pk = state[t]["peak_price"]
                    state[t]["peak_price"] = max(pk, px) if pk is not None else px

        eligible = eligible_fn(s_row, prev_close, prev_idx, state, features)

        w = pd.Series(0.0, index=closes.columns)
        if CASH_PROXY in eligible.index:
            eligible = eligible.drop(CASH_PROXY)
        if len(eligible) == 0:
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
        else:
            top = eligible.nlargest(min(K, len(eligible)))
            invested_frac = len(top) / K
            per_etf = invested_frac / len(top)
            w.loc[top.index] = per_etf
            cash = 1.0 - invested_frac
            if cash > 0 and CASH_PROXY in w.index:
                w[CASH_PROXY] = cash

        new_held = set(w[w > 1e-6].index) - {CASH_PROXY}
        old_held = {t for t, st in state.items() if st["held"]}
        for t in new_held - old_held:
            px = prev_close.get(t)
            state[t]["held"] = True
            state[t]["peak_price"] = float(px) if px is not None and px == px else None
            state[t]["was_overbought"] = False
        for t in old_held - new_held:
            state[t]["held"] = False
            state[t]["peak_price"] = None
            state[t]["was_overbought"] = False

        rb_weights.loc[rd] = w

    weight_panel = rb_weights.reindex(closes.index).ffill().fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel, "turnover": turnover,
             "daily_ret": port_ret}


# ---------------------------------------------------------------------------
# V6 + V3a stacked eligibility rule
# ---------------------------------------------------------------------------


def _eligible_v6_plus_v3a(min_breadth: float = 0.30, stop_frac: float = 0.10):
    """Combined: sleeve breadth gate + per-ETF trailing stop.

    1. Sleeve-level: if < min_breadth of universe is above +5%, exit ALL.
    2. Per-ETF: among the eligible names that pass sleeve gate, drop
       any held name whose price < (1 - stop_frac) * peak-while-held.
    """
    sleeve_fn = _eligible_v6_sleeve_breadth(min_breadth)
    stop_fn = _eligible_v3_trailing_stop(stop_frac)

    def f(s_row, prev_close, idx, state, feat):
        sleeve_eligible = sleeve_fn(s_row, prev_close, idx, state, feat)
        if len(sleeve_eligible) == 0:
            return sleeve_eligible
        # Apply trailing stop within the sleeve-gate-survivors. Trailing
        # stop's eligibility logic uses the standard floor filter
        # internally, but here we want it to filter sleeve_eligible
        # instead — so we wrap.
        keepers = []
        for t in sleeve_eligible.index:
            if state[t]["held"]:
                pk = state[t]["peak_price"]
                px = prev_close.get(t)
                if pk is not None and px is not None and px == px:
                    if px < (1 - stop_frac) * pk:
                        continue
            keepers.append(t)
        return sleeve_eligible.loc[keepers]
    return f


# ---------------------------------------------------------------------------
# Walk-forward with variant eligibility
# ---------------------------------------------------------------------------


def walk_forward_with_variant(
    closes: pd.DataFrame,
    signal: pd.DataFrame,
    features: dict,
    eligible_fn_factory,  # callable: (params) -> eligible_fn  OR just an eligible_fn
    param_grid: list[dict] | None,
    eligible_start: pd.Timestamp,
    initial_train_end: pd.Timestamp,
    K_grid: list[int] = (3, 4, 5),
    refit_freq: str = "YE",
) -> dict:
    """Walk-forward refit of K (and optional variant parameters) on
    expanding train windows. Each refit picks the (K, params) combo
    that maximises Sharpe over the train window, then applies it to
    the next 12 months. Concatenate test periods → final WF series.

    If ``param_grid`` is None, ``eligible_fn_factory`` is treated as
    a fixed eligibility function and only K is refit.
    """
    last_date = closes.index[-1]
    refit_ends = pd.date_range(initial_train_end, last_date, freq=refit_freq)
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_ends]
    refit_ends = [r for r in refit_ends if r >= eligible_start]
    if not refit_ends:
        return {}

    def _make_eligible_fn(params):
        if param_grid is None:
            return eligible_fn_factory
        return eligible_fn_factory(**params)

    def _sharpe(equity, win_start, win_end):
        eq = equity.loc[(equity.index >= win_start) & (equity.index <= win_end)]
        if len(eq) < 5:
            return float("nan")
        eq = eq / float(eq.iloc[0])
        daily = eq.pct_change().fillna(0)
        if daily.std() == 0:
            return 0.0
        return float(daily.mean() / daily.std() * math.sqrt(252))

    segments = []
    test_eq_pieces = []
    params_iter = param_grid if param_grid is not None else [{}]

    for i, train_end in enumerate(refit_ends):
        train_end_idx = closes.index.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(closes):
            break
        test_start = closes.index[test_start_idx]
        if test_start > test_end:
            continue

        # Grid-search over (K, params) on the train window
        best_K, best_params, best_sh = None, None, -1e9
        for params in params_iter:
            elig_fn = _make_eligible_fn(params)
            for K in K_grid:
                r = run_rotation_with_eligibility(
                    closes, signal, K, eligible_start, elig_fn, features,
                )
                sh = _sharpe(r["equity"], eligible_start, train_end)
                if not np.isnan(sh) and sh > best_sh:
                    best_sh, best_K, best_params = sh, K, params

        if best_K is None:
            continue
        # Apply the winning (K, params) combo to the test window
        elig_fn = _make_eligible_fn(best_params)
        full_eq = run_rotation_with_eligibility(
            closes, signal, best_K, eligible_start, elig_fn, features,
        )["equity"]
        test_eq = full_eq.loc[test_start:test_end]
        base_val = (float(full_eq.iloc[test_start_idx - 1])
                     if test_start_idx > 0 else 1.0)
        test_eq = test_eq / base_val
        test_sh = _sharpe(test_eq, test_start, test_end)
        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_K": best_K,
            "best_params": best_params,
            "train_sharpe": _safe(best_sh),
            "test_sharpe": _safe(test_sh),
            "n_test_days": int(len(test_eq)),
        })
        last_val = test_eq_pieces[-1].iloc[-1] if test_eq_pieces else 1.0
        test_eq_pieces.append(test_eq * last_val / test_eq.iloc[0])

    if not test_eq_pieces:
        return {}
    wf_equity = pd.concat(test_eq_pieces)
    wf_daily = wf_equity.pct_change().fillna(0)
    wf_sh = (wf_daily.mean() / wf_daily.std() * math.sqrt(252)
              if wf_daily.std() > 0 else 0.0)
    return {
        "segments": segments,
        "walk_forward_sharpe": _safe(wf_sh),
        "wf_dates": [d.strftime("%Y-%m-%d") for d in wf_equity.index],
        "wf_equity_first": _safe(float(wf_equity.iloc[0])),
        "wf_equity_last": _safe(float(wf_equity.iloc[-1])),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("STRATEGY C EXIT-RULE VALIDATION — sensitivity + stack + walk-forward")
    print("=" * 78)

    print("Loading prices ...", flush=True)
    closes = download_prices()
    signal = compute_signal(closes)
    features = {
        "ema_fast": compute_ema(closes, 50),
        "ema_slow": compute_ema(closes, 100),
        "signal_slope": compute_signal_slope(signal, 20),
        "rsi": compute_rsi(closes, 14),
        "realised_vol": compute_realised_vol(closes, 20),
    }
    eligible_start = pd.Timestamp("2018-11-08")

    # ==================================================================
    # PHASE A — Threshold sensitivity sweep on V6
    # ==================================================================
    print()
    print("=" * 78)
    print("PHASE A: V6 threshold sensitivity sweep (in-sample)")
    print("=" * 78)
    sensitivity = []
    # Add baseline first for reference
    for label, elig_fn in [
        ("Baseline (no V6)", _eligible_baseline),
        ("V6 @ 20%", _eligible_v6_sleeve_breadth(0.20)),
        ("V6 @ 30% (winner)", _eligible_v6_sleeve_breadth(0.30)),
        ("V6 @ 40%", _eligible_v6_sleeve_breadth(0.40)),
        ("V6 @ 50%", _eligible_v6_sleeve_breadth(0.50)),
        ("V6 @ 60%", _eligible_v6_sleeve_breadth(0.60)),
    ]:
        print(f"  {label} ...", flush=True)
        r = run_rotation_with_eligibility(
            closes, signal, HEADLINE_K, eligible_start, elig_fn, features,
        )
        m = compute_metrics(r["equity"], eligible_start, r["turnover"],
                              r["weights"])
        sensitivity.append({"name": label, "metrics": m})

    print(f"\n{'Variant':<30} {'Sharpe':>7} {'CAGR':>7} {'MaxDD':>8} "
          f"{'2021 DD':>8} {'Turn':>6}")
    print("-" * 70)
    for r in sensitivity:
        m = r["metrics"]
        print(f"  {r['name']:<28} {m.get('sharpe', 0):+.2f}  "
              f"{m.get('cagr', 0)*100:+.1f}%  "
              f"{m.get('max_dd', 0)*100:.1f}%  "
              f"{m.get('episode_2021_dd', 0)*100:.1f}%  "
              f"{m.get('annual_turnover', 0):.1f}x")

    # ==================================================================
    # PHASE B — V6 30% + V3a 10% stacked
    # ==================================================================
    print()
    print("=" * 78)
    print("PHASE B: V6 + V3a stacked variants (in-sample)")
    print("=" * 78)
    stacked = []
    for label, elig_fn in [
        ("V6 30% (alone)", _eligible_v6_sleeve_breadth(0.30)),
        ("V3a 10% trailing stop (alone)", _eligible_v3_trailing_stop(0.10)),
        ("V6 30% + V3a 10% stacked", _eligible_v6_plus_v3a(0.30, 0.10)),
        ("V6 30% + V3a 15% stacked", _eligible_v6_plus_v3a(0.30, 0.15)),
    ]:
        print(f"  {label} ...", flush=True)
        r = run_rotation_with_eligibility(
            closes, signal, HEADLINE_K, eligible_start, elig_fn, features,
        )
        m = compute_metrics(r["equity"], eligible_start, r["turnover"],
                              r["weights"])
        stacked.append({"name": label, "metrics": m})

    print(f"\n{'Variant':<30} {'Sharpe':>7} {'CAGR':>7} {'MaxDD':>8} "
          f"{'2021 DD':>8} {'Turn':>6}")
    print("-" * 70)
    for r in stacked:
        m = r["metrics"]
        print(f"  {r['name']:<28} {m.get('sharpe', 0):+.2f}  "
              f"{m.get('cagr', 0)*100:+.1f}%  "
              f"{m.get('max_dd', 0)*100:.1f}%  "
              f"{m.get('episode_2021_dd', 0)*100:.1f}%  "
              f"{m.get('annual_turnover', 0):.1f}x")

    # ==================================================================
    # PHASE C — Walk-forward validation of V6
    # ==================================================================
    print()
    print("=" * 78)
    print("PHASE C: Walk-forward validation (annual K refit, expanding train)")
    print("=" * 78)
    initial_train_end = pd.Timestamp("2023-11-08")  # 5-year initial train
    walk_forward = []

    # WF.1 — baseline (no V6), refit K only
    print("  WF.1 Baseline (no variant), refit K ...", flush=True)
    wf_base = walk_forward_with_variant(
        closes, signal, features,
        eligible_fn_factory=_eligible_baseline,
        param_grid=None,
        eligible_start=eligible_start,
        initial_train_end=initial_train_end,
    )
    walk_forward.append({"name": "Baseline (no V6)", "wf": wf_base})

    # WF.2 — V6 fixed at 30%, refit K only
    print("  WF.2 V6 @ 30% fixed, refit K only ...", flush=True)
    wf_v6 = walk_forward_with_variant(
        closes, signal, features,
        eligible_fn_factory=_eligible_v6_sleeve_breadth(0.30),
        param_grid=None,
        eligible_start=eligible_start,
        initial_train_end=initial_train_end,
    )
    walk_forward.append({"name": "V6 @ 30% fixed (refit K only)", "wf": wf_v6})

    # WF.3 — V6 joint refit K and breadth threshold each year
    print("  WF.3 V6 joint K + threshold refit ...", flush=True)
    wf_v6_joint = walk_forward_with_variant(
        closes, signal, features,
        eligible_fn_factory=_eligible_v6_sleeve_breadth,
        param_grid=[
            {"min_breadth": 0.20},
            {"min_breadth": 0.30},
            {"min_breadth": 0.40},
            {"min_breadth": 0.50},
        ],
        eligible_start=eligible_start,
        initial_train_end=initial_train_end,
    )
    walk_forward.append({"name": "V6 joint K + threshold refit", "wf": wf_v6_joint})

    print()
    print(f"{'Walk-forward variant':<36} {'WF Sharpe':>10} {'Segments':>10}")
    print("-" * 60)
    for r in walk_forward:
        wf = r["wf"]
        wf_sh = wf.get("walk_forward_sharpe")
        n_seg = len(wf.get("segments") or [])
        wf_sh_str = f"{wf_sh:+.2f}" if wf_sh is not None else "—"
        print(f"  {r['name']:<34} {wf_sh_str:>10} {n_seg:>10}")
    # Per-segment dump for V6 fixed (the deployment candidate)
    if wf_v6.get("segments"):
        print()
        print(f"  WF.2 V6 @ 30% per-segment K choices:")
        for seg in wf_v6["segments"]:
            print(f"    {seg['train_end']} -> {seg['test_end']}: "
                  f"K={seg['best_K']} train_sh={seg['train_sharpe']:+.2f} "
                  f"test_sh={seg['test_sharpe']:+.2f}")
    if wf_v6_joint.get("segments"):
        print()
        print(f"  WF.3 V6 joint refit picks per segment:")
        for seg in wf_v6_joint["segments"]:
            p = seg.get('best_params', {})
            print(f"    {seg['train_end']} -> {seg['test_end']}: "
                  f"K={seg['best_K']} thr={p.get('min_breadth', 0)*100:.0f}% "
                  f"train_sh={seg['train_sharpe']:+.2f} "
                  f"test_sh={seg['test_sharpe']:+.2f}")

    # ---- Save JSON ------------------------------------------
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "K_default": HEADLINE_K,
        "signal_floor": SIGNAL_FLOOR,
        "eligible_start": str(eligible_start.date()),
        "phase_a_sensitivity": sensitivity,
        "phase_b_stacked": stacked,
        "phase_c_walk_forward": walk_forward,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "thematic_exit_validation.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
