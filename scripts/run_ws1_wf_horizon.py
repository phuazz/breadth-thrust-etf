"""WS1 follow-up — WALK-FORWARD lookback selection vs fixed 200d.

The dense-grid surface shows the blend plateau at 250-325 with deployed 200
just below its edge. Reading that chart and recalibrating would be in-sample
selection. This script runs the decision the DISCIPLINED way: an annual
re-fit that may only use data available at each refit date, then pays for
its choices out of sample.

Protocols (identical refit calendar, identical OOS window):
  fixed_200 / fixed_250 / fixed_275 — no re-fitting (comparators; 250/275
      are the "chart-suggested" candidates, included to answer whether the
      apparent plateau edge would actually have paid OOS post-2022).
  wf_common     — one common lookback for all four sleeves, chosen each
      year-end by BLEND Sharpe on the expanding train window.
  wf_per_sleeve — each sleeve picks its own lookback by its own train
      Sharpe (more knobs; must beat wf_common to justify itself).
  oracle_fixed  — best single W chosen with hindsight ON the OOS window
      (upper bound / reference only, never deployable).

Three ways this could be silently wrong, and the defences:
  1. SELECTION LEAKAGE — W* per refit uses train Sharpe on data strictly up
     to the refit date; test segments start the next trading day.
  2. SPLICE BIAS — each test segment is rebased at its own start and chained
     multiplicatively; every protocol is evaluated on the IDENTICAL
     concatenated window (first test day -> common end), so no protocol
     gets a friendlier calendar.
  3. UN-MODELLED SWITCH COST — splicing equity curves hides the real
     turnover of moving between W-portfolios at a refit. A conservative
     100% one-way turnover is charged per sleeve whose W changed
     (weight_i x cost_i), deducted at the segment start.

Output: data/ws1_wf_horizon.json
Run:    python scripts/run_ws1_wf_horizon.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

GRID = [25, 50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300, 325]
REFIT_YEARS = [2021, 2022, 2023, 2024, 2025]
SLEEVE_COST = {"A": W.COST_A, "B": W.COST_B, "C": W.COST_C, "D": W.COST_D}
SLEEVE_WEIGHT = {"A": 0.35, "B": 0.35, "C": 0.10, "D": 0.20}
OUT = W.DATA / "ws1_wf_horizon.json"


def sharpe(eq: pd.Series) -> float:
    if len(eq) < 10:
        return float("nan")
    d = (eq / eq.iloc[0]).pct_change().fillna(0)
    return float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0.0


def main() -> int:
    print("Loading panels ...", flush=True)
    closes_a, cons_a = W.load_sleeve_a()
    closes_d, cons_d = W.load_sleeve_d()
    closes_b = W.load_sleeve_b()
    closes_c = W.load_sleeve_c()
    d_end = min(cp.index.max() for cp in cons_d.values())
    a_end = min(cp.index.max() for cp in cons_a.values())
    common_end = min(closes_b.index.max(), closes_c.index.max(), a_end, d_end)
    cs = W.COMMON_START

    print("Computing per-sleeve equity for every lookback ...", flush=True)
    eq: dict[str, dict[int, pd.Series]] = {s: {} for s in "ABCD"}
    for w in GRID:
        sig_a = W.relative(W.breadth_panel(cons_a, closes_a.index, w))
        eq["A"][w] = run_portfolio(closes_a, sig_a, top_k_breadth_weight(W.K_A),
                                   cs, cost=W.COST_A, rebalance_freq=W.REBAL
                                   )["equity"].loc[:common_end]
        bp_d = W.breadth_panel(cons_d, closes_d.index, w)
        eq["D"][w] = run_portfolio(closes_d, bp_d, top_k_breadth_weight(W.K_D),
                                   cs, cost=W.COST_D, rebalance_freq=W.REBAL
                                   )["equity"].loc[:common_end]
        eq["B"][w] = B_engine.run_rotation(
            closes_b, W.distance_signal(closes_b, w),
            B_engine.top_k_by_signal(W.K_B), cs, rebalance_freq=W.REBAL,
            cost=W.COST_B)["equity"].loc[:common_end]
        eq["C"][w] = C_engine.run_rotation(
            closes_c, W.distance_signal(closes_c, w),
            C_engine.top_k_equal_weight(W.K_C), cs, rebalance_freq=W.REBAL,
            cost=W.COST_C)["equity"].loc[:common_end]
        print(f"  W={w} done", flush=True)

    blend_cache: dict[tuple, pd.Series] = {}

    def blend_for(combo: dict[str, int]) -> pd.Series:
        key = tuple(combo[s] for s in "ABCD")
        if key not in blend_cache:
            blend_cache[key] = W.blend_equity(
                eq["A"][combo["A"]], eq["B"][combo["B"]],
                eq["C"][combo["C"]], eq["D"][combo["D"]], cs, common_end)
        return blend_cache[key]

    # Refit calendar on the blend index (last trading day <= Dec 31).
    idx = blend_for({s: 200 for s in "ABCD"}).index
    refit_ends = [idx[idx.searchsorted(pd.Timestamp(f"{y}-12-31"),
                                       side="right") - 1] for y in REFIT_YEARS]
    segments = [(refit_ends[i],
                 refit_ends[i + 1] if i + 1 < len(refit_ends) else idx[-1])
                for i in range(len(refit_ends))]
    oos_start = idx[idx.get_loc(refit_ends[0]) + 1]
    print(f"OOS window: {oos_start.date()} -> {idx[-1].date()} "
          f"({len(segments)} segments)")

    def stitched(pick_fn) -> tuple[pd.Series, list[dict]]:
        """pick_fn(train_end) -> {'A': W, 'B': W, 'C': W, 'D': W}."""
        pieces, log, prev = [], [], None
        for t_end, s_end in segments:
            combo = pick_fn(t_end)
            curve = blend_for(combo)
            seg = curve.loc[(curve.index > t_end) & (curve.index <= s_end)]
            base = float(curve.loc[:t_end].iloc[-1])
            seg = seg / base
            if prev is not None:
                switch = sum(SLEEVE_WEIGHT[s] * SLEEVE_COST[s]
                             for s in "ABCD" if combo[s] != prev[s])
                seg = seg * (1.0 - switch)
            last = pieces[-1].iloc[-1] if pieces else 1.0
            pieces.append(seg * last)
            log.append({"train_end": str(t_end.date()),
                        "test_end": str(s_end.date()),
                        "picked": dict(combo),
                        "test_sharpe": W._safe(sharpe(seg))})
            prev = combo
        return pd.concat(pieces), log

    def pick_fixed(w):
        return lambda t_end: {s: w for s in "ABCD"}

    def pick_common(t_end):
        best_w, best = None, -1e9
        for w in GRID:
            sh = sharpe(blend_for({s: w for s in "ABCD"}).loc[:t_end])
            if sh > best:
                best, best_w = sh, w
        return {s: best_w for s in "ABCD"}

    def pick_per_sleeve(t_end):
        combo = {}
        for s in "ABCD":
            best_w, best = None, -1e9
            for w in GRID:
                sh = sharpe(eq[s][w].loc[cs:t_end])
                if sh > best:
                    best, best_w = sh, w
            combo[s] = best_w
        return combo

    protocols = {
        "fixed_200": pick_fixed(200),
        "fixed_250": pick_fixed(250),
        "fixed_275": pick_fixed(275),
        "wf_common": pick_common,
        "wf_per_sleeve": pick_per_sleeve,
    }
    results = {}
    for name, fn in protocols.items():
        curve, log = stitched(fn)
        results[name] = {
            "oos_sharpe": W._safe(sharpe(curve)),
            "oos_stats": W.window_stats(curve, curve.index[0], curve.index[-1]),
            "segments": log,
        }
        seq = [seg["picked"] for seg in log]
        uniq = (sorted({v for c in seq for v in c.values()})
                if "wf" in name else [seq[0]["A"]])
        print(f"{name:14s} OOS Sharpe {results[name]['oos_sharpe']:+.3f}  "
              f"Ws used {uniq}")

    # Oracle: best single fixed W judged ON the OOS window (hindsight bound).
    oracle = max(GRID, key=lambda w: sharpe(
        blend_for({s: w for s in "ABCD"}).loc[oos_start:]))
    results["oracle_fixed"] = {
        "w": oracle,
        "oos_sharpe": W._safe(sharpe(
            blend_for({s: oracle for s in "ABCD"}).loc[oos_start:])),
        "note": "hindsight upper bound, not deployable",
    }
    print(f"oracle_fixed   OOS Sharpe {results['oracle_fixed']['oos_sharpe']:+.3f} "
          f"(W={oracle}, hindsight)")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Walk-forward lookback selection (annual expanding"
                        " re-fit) vs fixed lookbacks, blend level, identical"
                        " OOS window; 100% turnover charged per sleeve on"
                        " each W change."),
        "grid": GRID,
        "oos_start": str(oos_start.date()),
        "oos_end": str(idx[-1].date()),
        "protocols": results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
