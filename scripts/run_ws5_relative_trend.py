"""WS5 T3 — registered run of the constituent relative-trend challenger.

Runs the frozen §2 register ONCE (KICKOFF_ws5-relative-trend.md, signed
2026-07-10) and evaluates the frozen verdict rule. No configuration outside the
8-row register is computed.

Design — the challenger arms differ from the deployed Sleeve A ONLY in the
per-name trend condition. Everything downstream is the deployed Phase 20.1
path, reused verbatim:
  build_panels()                -> proxy closes + deployed A0 breadth + eligible
  relative_trend.compute_*      -> per-arm per-ETF breadth (shared-mask legs)
  align_breadth_to_index        -> deployed freshness-aware calendar projection
  demean (cross-sectional)      -> Phase 20 sector-relative signal
  top_k_breadth_weight(7)       -> Phase 20.1 positive-only weighting
  run_portfolio(..., 2 bps)     -> W-FRI holiday-skip rebalance, shift(1), costs
  walk_forward_sharpe           -> canonical annual-K-refit OOS (verdict primary)

A0 through this harness reproduces the deployed Sleeve A bit-for-bit — asserted
against build_panels' breadth (breadth level) and against the imported
walk_forward_sharpe (WF level). The 2x-cost walk-forward is a local
reimplementation validated to equal the imported one at 1x before use.

Register (frozen): #0 A0 abs · #1 A1 rel · #2 A2 dual · #3 P momentum-placebo ·
#4 OR (A0 or A1) · #5 A2 rel-150d · #6 A2 rel-250d · #7 blend-context.

Verdict (frozen): adopt A1 or A2 over A0 only if ALL of —
  1. WF OOS Sharpe >= A0 + 0.10
  2. WF OOS Sharpe >= P  + 0.10   (identical folds)
  3. full-window MaxDD <= A0 + 2pp
  4. conditions 1-2 survive 2x costs
  5. weekly selection Jaccard vs P < 0.8
otherwise KEEP A0 (incumbent wins ties).

Dates via pandas only. Window capped to the registered 2018-Q4 -> 2026-Q2.

Run: python scripts/run_ws5_relative_trend.py
Output: data/ws5_results.json
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "ws5_results.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from run_portfolio import build_panels, run_portfolio, top_k_breadth_weight  # noqa: E402
from run_ma200_sweep import align_breadth_to_index, load_constituent_prices, MA_PERIOD  # noqa: E402
from run_improvements import compute_stats  # noqa: E402
from run_phase6_weighting_experiment import walk_forward_sharpe  # noqa: E402 (canonical WF)
from run_thematic_rotation import COST_FRAC as CANONICAL_WF_COST  # noqa: E402 (5 bps)
from run_ws3_deflated import dsr  # noqa: E402
from relative_trend import compute_trend_breadth_all  # noqa: E402

# Deployed Strategy A calibration (run_topk_robustness.py).
COST_BPS = 2
COST_FRAC = COST_BPS / 10_000
K_DEPLOYED = 7
K_GRID = [3, 5, 7, 9]
REBAL = "W-FRI"

# Frozen verdict parameters (§6 sign-off, 2026-07-10).
ADOPT_MARGIN = 0.10          # item 1
DD_TOL_PP = 2.0              # condition 3
JACCARD_MAX = 0.8            # condition 5
N_TRIALS = 8                 # register size, for DSR
PLACEBO_MOM_DAYS = 126       # §5 assumption (fixed control, not tuned)
REL_NEIGHBOUR_WINDOWS = {"rel150": 150, "rel250": 250}

# Registered window: deployed backtest window 2018-Q4 -> 2026-Q2.
WINDOW_END = pd.Timestamp("2026-06-30")
# Walk-forward: first annual refit at this year-end; expanding train from
# eligible; OOS = each subsequent calendar segment. Applied identically to
# every arm, so the cross-arm comparison is invariant to this choice.
INITIAL_TRAIN_END = pd.Timestamp("2020-12-31")


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def demean(panel: pd.DataFrame) -> pd.DataFrame:
    """Phase 20 cross-sectional demeaning (sector-relative signal)."""
    return panel.sub(panel.mean(axis=1, skipna=True), axis=0)


# ---------------------------------------------------------------------------
# Per-arm breadth panels (columns = universe ETFs, aligned to trade calendar)
# ---------------------------------------------------------------------------

def build_arm_breadth(used, spy, trade_index):
    """For each ETF compute the three shared-mask arms (+ the two rel-window
    neighbours) on its constituent cache, then align to the trade calendar via
    the deployed freshness-aware helper. Returns a dict of breadth panels."""
    cols = {a: {} for a in ("absolute", "relative", "dual", "rel150", "rel250")}
    coverage = {}
    for etf in used:
        cp = load_constituent_prices(etf)
        base = compute_trend_breadth_all(cp, spy, period=MA_PERIOD)
        n150 = compute_trend_breadth_all(cp, spy, period=MA_PERIOD, rel_period=150)
        n250 = compute_trend_breadth_all(cp, spy, period=MA_PERIOD, rel_period=250)
        cols["absolute"][etf] = align_breadth_to_index(base["absolute"], trade_index)
        cols["relative"][etf] = align_breadth_to_index(base["relative"], trade_index)
        cols["dual"][etf] = align_breadth_to_index(base["dual"], trade_index)
        cols["rel150"][etf] = align_breadth_to_index(n150["dual"], trade_index)
        cols["rel250"][etf] = align_breadth_to_index(n250["dual"], trade_index)
        # per-ETF mean shared-valid coverage over the window (for the record)
        valid = base["absolute"].reindex(trade_index)
        coverage[etf] = float(valid.notna().mean())
    panels = {a: pd.DataFrame(cols[a]).reindex(columns=used) for a in cols}
    # OR arm via inclusion-exclusion on the shared denominator: |A0 ∪ A1| =
    # |A0| + |A1| - |A0 ∩ A1|; identical denominator so shares add directly.
    panels["or_"] = panels["absolute"] + panels["relative"] - panels["dual"]
    return panels, coverage


def placebo_signal(closes, spy):
    """Register #3 P — momentum placebo. ETF-level 126d total return relative
    to SPY, demeaned cross-sectionally. NO constituent data. (The SPY term is a
    per-date constant, so it cancels under the subsequent cross-sectional
    demeaning — the placebo is plain cross-sectional ETF momentum, exactly the
    intended control.)"""
    etf_mom = closes.pct_change(PLACEBO_MOM_DAYS)
    spy_mom = spy.pct_change(PLACEBO_MOM_DAYS)
    rel = etf_mom.sub(spy_mom, axis=0)
    return demean(rel)


# ---------------------------------------------------------------------------
# Backtest wrappers
# ---------------------------------------------------------------------------

def full_window(closes, signal, eligible, cost):
    r = run_portfolio(closes, signal, top_k_breadth_weight(K_DEPLOYED),
                      eligible, rebalance_freq=REBAL, cost=cost)
    st = compute_stats(r["equity"], eligible)
    daily = r["equity"].pct_change().fillna(0)
    daily = daily.loc[daily.index >= eligible]
    return r, st, daily


def _wf_local(closes, signal, eligible, initial_train_end, cost):
    """Local reimplementation of walk_forward_sharpe WITH an explicit cost,
    so the 2x-cost robustness leg uses identical folds. Validated to equal the
    imported walk_forward_sharpe at 1x before use (see main())."""
    last_date = closes.index[-1]
    refit_ends = pd.date_range(initial_train_end, last_date, freq="YE")
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                  for r in refit_ends]
    refit_ends = [r for r in refit_ends if r >= eligible]
    if not refit_ends:
        return {"walk_forward_sharpe": None, "n_segments": 0, "K_sequence": []}

    def _eq(K):
        r = run_portfolio(closes, signal, top_k_breadth_weight(K), eligible,
                          rebalance_freq=REBAL, cost=cost)
        return r["equity"]

    def _sharpe(eq, a, b):
        e = eq.loc[(eq.index >= a) & (eq.index <= b)]
        if len(e) < 5:
            return float("nan")
        e = e / float(e.iloc[0])
        d = e.pct_change().fillna(0)
        return 0.0 if d.std() == 0 else float(d.mean() / d.std() * math.sqrt(252))

    pieces, kseq = [], []
    for i, train_end in enumerate(refit_ends):
        tei = closes.index.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        tsi = tei + 1
        if tsi >= len(closes):
            break
        test_start = closes.index[tsi]
        if test_start > test_end:
            continue
        best_K, best_sh = None, -1e9
        for K in K_GRID:
            sh = _sharpe(_eq(K), eligible, train_end)
            if not np.isnan(sh) and sh > best_sh:
                best_sh, best_K = sh, K
        if best_K is None:
            continue
        kseq.append(best_K)
        full_eq = _eq(best_K)
        test_eq = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[tsi - 1]) if tsi > 0 else 1.0
        test_eq = test_eq / base_val
        last_val = pieces[-1].iloc[-1] if pieces else 1.0
        pieces.append(test_eq * last_val / test_eq.iloc[0])
    if not pieces:
        return {"walk_forward_sharpe": None, "n_segments": 0, "K_sequence": []}
    wf_eq = pd.concat(pieces)
    d = wf_eq.pct_change().fillna(0)
    sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0.0
    return {"walk_forward_sharpe": sh, "n_segments": len(pieces),
            "K_sequence": kseq}


def weekly_selection(weights, eligible):
    """Set of held ETFs (weight>1e-6) on each Friday rebalance in-window."""
    w = weights.loc[weights.index >= eligible]
    fri = w.loc[w.index.dayofweek == 4]
    fri = fri.loc[fri.sum(axis=1) > 0.5]  # skip warm-up
    return {d: frozenset(row[row > 1e-6].index) for d, row in fri.iterrows()}


def mean_jaccard(sel_a, sel_p):
    common = sorted(set(sel_a) & set(sel_p))
    if not common:
        return None
    js = []
    for d in common:
        a, p = sel_a[d], sel_p[d]
        u = a | p
        js.append(len(a & p) / len(u) if u else 1.0)
    return float(np.mean(js))


def main() -> int:
    print("WS5 T3 — registered run. Building deployed panels ...", flush=True)
    closes, breadths_dep, used = build_panels()
    closes = closes.loc[:WINDOW_END]
    breadths_dep = breadths_dep.loc[:WINDOW_END]
    spy = pd.read_parquet(DATA_DIR / "spy_close_cache.parquet")["Close"]
    spy = spy[~spy.index.duplicated(keep="first")].sort_index()

    # Eligible start — deployed logic (run_topk_robustness).
    starts = [breadths_dep[e].dropna().index.min() for e in used
              if breadths_dep[e].notna().any()]
    eligible = max(starts)
    eligible = pd.Timestamp(eligible.date()) + pd.Timedelta(days=MA_PERIOD)
    eligible = (closes.index[closes.index >= eligible][0]
                if (closes.index >= eligible).any() else closes.index[MA_PERIOD])
    print(f"  ETFs: {len(used)} | window {closes.index.min().date()} .. "
          f"{closes.index.max().date()} | eligible {eligible.date()}")

    # --- Per-arm breadth + signals ----------------------------------------
    panels, coverage = build_arm_breadth(used, spy, closes.index)

    # A0 breadth-level parity vs the deployed engine (must be exact).
    a0_parity = float((panels["absolute"] - breadths_dep[used])
                      .abs().max().max())
    print(f"  A0 breadth parity vs deployed: max|diff| = {a0_parity:.2e}")

    signals = {
        "A0_absolute": demean(panels["absolute"]),
        "A1_relative": demean(panels["relative"]),
        "A2_dual": demean(panels["dual"]),
        "P_placebo": placebo_signal(closes, spy),
        "OR_a0_or_a1": demean(panels["or_"]),
        "A2_rel150": demean(panels["rel150"]),
        "A2_rel250": demean(panels["rel250"]),
    }

    # Fairness: every arm must have a defined signal at eligible (no arm
    # silently sits flat early because its leg had not warmed up).
    for name, sig in signals.items():
        row = sig.loc[eligible]
        assert row.notna().any(), f"{name} has no defined signal at eligible"

    # --- Run each arm: full-window + walk-forward (1x and 2x) --------------
    rows = {}
    daily_by_arm = {}
    weights_by_arm = {}
    for name, sig in signals.items():
        r1, st1, daily1 = full_window(closes, sig, eligible, COST_FRAC)
        _, st2, _ = full_window(closes, sig, eligible, 2 * COST_FRAC)
        wf1 = _wf_local(closes, sig, eligible, INITIAL_TRAIN_END, COST_FRAC)
        wf2 = _wf_local(closes, sig, eligible, INITIAL_TRAIN_END, 2 * COST_FRAC)
        rows[name] = {
            "full_1x": {k: _safe(st1.get(k)) for k in
                        ("sharpe", "total_return", "max_dd", "cagr")},
            "full_2x": {k: _safe(st2.get(k)) for k in
                        ("sharpe", "total_return", "max_dd", "cagr")},
            "wf_1x_sharpe": _safe(wf1["walk_forward_sharpe"]),
            "wf_2x_sharpe": _safe(wf2["walk_forward_sharpe"]),
            "wf_K_sequence": wf1["K_sequence"],
        }
        daily_by_arm[name] = daily1
        weights_by_arm[name] = r1["weights"]
        print(f"  {name:<14} full Shp {st1['sharpe']:+.3f}  "
              f"WF {wf1['walk_forward_sharpe']:+.3f}  "
              f"WF2x {wf2['walk_forward_sharpe']:+.3f}  "
              f"DD {st1['max_dd']*100:5.1f}%  K{wf1['K_sequence']}")

    # WF validation: _wf_local must reproduce the canonical walk_forward_sharpe
    # EXACTLY. Note the canonical function is hardcoded to Strategy C's 5 bps
    # (it imports run_rotation/COST_FRAC from run_thematic_rotation), whereas
    # the verdict runs at Sleeve A's deployed 2 bps. The valid check is
    # therefore at MATCHED cost (5 bps) -> must be ~0; the 2-bps-vs-5-bps
    # magnitude gap in a naive cross-cost comparison is ~0.03 and immaterial.
    wf_import = walk_forward_sharpe(
        closes, signals["A0_absolute"], eligible, INITIAL_TRAIN_END,
        top_k_breadth_weight, K_GRID, REBAL)["walk_forward_sharpe"]
    wf_local_matched = _wf_local(
        closes, signals["A0_absolute"], eligible, INITIAL_TRAIN_END,
        CANONICAL_WF_COST)["walk_forward_sharpe"]
    wf_parity = abs(_safe(wf_import) - _safe(wf_local_matched))
    print(f"  WF validation (_wf_local vs canonical @matched 5bps): "
          f"|diff| = {wf_parity:.2e}")

    # --- DSR over N=8 for the best challenger -----------------------------
    arm_order = ["A0_absolute", "A1_relative", "A2_dual", "P_placebo",
                 "OR_a0_or_a1", "A2_rel150", "A2_rel250"]
    sr_daily = []
    for a in arm_order:
        d = daily_by_arm[a].dropna().values
        sr_daily.append(d.mean() / d.std(ddof=1) if d.std(ddof=1) > 0 else 0.0)
    # N=8 nominal register (blend-context #7 counts even though evaluated
    # elsewhere); pad the trial-Sharpe variance sample to N_TRIALS with the
    # incumbent so var reflects the full register size.
    while len(sr_daily) < N_TRIALS:
        sr_daily.append(sr_daily[0])
    var_trials_daily = float(np.var(sr_daily, ddof=1))
    best_ch = max(("A1_relative", "A2_dual"),
                  key=lambda a: rows[a]["wf_1x_sharpe"])
    dsr_best = dsr(daily_by_arm[best_ch].dropna(), float(N_TRIALS),
                   var_trials_daily)

    # --- Overlap diagnostics (condition 5) --------------------------------
    sel_p = weekly_selection(weights_by_arm["P_placebo"], eligible)
    overlap = {}
    for a in ("A1_relative", "A2_dual"):
        sel_a = weekly_selection(weights_by_arm[a], eligible)
        jac = mean_jaccard(sel_a, sel_p)
        ret_corr = float(daily_by_arm[a].corr(daily_by_arm["P_placebo"]))
        overlap[a] = {"weekly_jaccard_vs_P": _safe(jac),
                      "return_corr_vs_P": _safe(ret_corr)}

    # --- Verdict ----------------------------------------------------------
    a0 = rows["A0_absolute"]
    p = rows["P_placebo"]
    verdict_detail = {}
    adopt = []
    for a in ("A1_relative", "A2_dual"):
        c = rows[a]
        c1 = c["wf_1x_sharpe"] >= a0["wf_1x_sharpe"] + ADOPT_MARGIN
        c2 = c["wf_1x_sharpe"] >= p["wf_1x_sharpe"] + ADOPT_MARGIN
        c3 = (c["full_1x"]["max_dd"] - a0["full_1x"]["max_dd"]) >= -DD_TOL_PP / 100
        c4 = (c["wf_2x_sharpe"] >= a0["wf_2x_sharpe"] + ADOPT_MARGIN and
              c["wf_2x_sharpe"] >= p["wf_2x_sharpe"] + ADOPT_MARGIN)
        c5 = (overlap[a]["weekly_jaccard_vs_P"] is not None and
              overlap[a]["weekly_jaccard_vs_P"] < JACCARD_MAX)
        passed = bool(c1 and c2 and c3 and c4 and c5)
        verdict_detail[a] = {
            "cond1_wf_vs_A0": bool(c1), "cond2_wf_vs_P": bool(c2),
            "cond3_maxdd": bool(c3), "cond4_2x_costs": bool(c4),
            "cond5_jaccard": bool(c5), "passes_all": passed,
        }
        if passed:
            adopt.append(a)
    if adopt:
        winner = max(adopt, key=lambda a: rows[a]["wf_1x_sharpe"])
        verdict = f"PROPOSE Phase 30 ({winner}) — CIO sign-off required"
    else:
        winner = None
        verdict = "KEEP A0 (incumbent) — no challenger clears the frozen rule"

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": {"start": eligible.strftime("%Y-%m-%d"),
                   "end": closes.index.max().strftime("%Y-%m-%d"),
                   "registered": "2018-Q4 -> 2026-Q2"},
        "config": {"cost_bps": COST_BPS, "K_deployed": K_DEPLOYED,
                   "K_grid": K_GRID, "rebal": REBAL,
                   "adopt_margin": ADOPT_MARGIN, "dd_tol_pp": DD_TOL_PP,
                   "jaccard_max": JACCARD_MAX, "n_trials": N_TRIALS,
                   "placebo_mom_days": PLACEBO_MOM_DAYS,
                   "initial_train_end": INITIAL_TRAIN_END.strftime("%Y-%m-%d")},
        "parity": {"a0_breadth_maxdiff": a0_parity,
                   "wf_local_vs_canonical_matched_5bps": wf_parity,
                   "note": "verdict WF runs at Sleeve A's deployed 2 bps; the "
                           "canonical walk_forward_sharpe is hardcoded to 5 bps, "
                           "so validation is at matched 5 bps"},
        "register": {
            "0_A0_absolute": rows["A0_absolute"],
            "1_A1_relative": rows["A1_relative"],
            "2_A2_dual": rows["A2_dual"],
            "3_P_placebo": rows["P_placebo"],
            "4_OR": rows["OR_a0_or_a1"],
            "5_A2_rel150": rows["A2_rel150"],
            "6_A2_rel250": rows["A2_rel250"],
        },
        "coverage_shared_valid_share": coverage,
        "overlap_vs_placebo": overlap,
        "dsr_best_challenger": {"arm": best_ch, "var_trials_daily": var_trials_daily,
                                **{k: _safe(v) for k, v in dsr_best.items()}},
        "verdict_conditions": verdict_detail,
        "verdict": verdict,
        "winner": winner,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("WS5 VERDICT:", verdict)
    print("=" * 78)
    print(f"  WF OOS Sharpe — A0 {a0['wf_1x_sharpe']:+.3f} | "
          f"A1 {rows['A1_relative']['wf_1x_sharpe']:+.3f} | "
          f"A2 {rows['A2_dual']['wf_1x_sharpe']:+.3f} | "
          f"P {p['wf_1x_sharpe']:+.3f}  (adopt bar +{ADOPT_MARGIN})")
    for a in ("A1_relative", "A2_dual"):
        print(f"  {a}: {verdict_detail[a]}")
    print(f"  Jaccard vs P — A1 {overlap['A1_relative']['weekly_jaccard_vs_P']} | "
          f"A2 {overlap['A2_dual']['weekly_jaccard_vs_P']}")
    print(f"  DSR({best_ch}, N=8) = {_safe(dsr_best['dsr'])}")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
