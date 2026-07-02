"""WS3 precompute — sleeve equity-curve grid for the full-system walk-forward.

Builds every per-sleeve equity curve the WS3 full-system re-fit is allowed
to choose from, so the walk-forward search composes blends from cached
curves instead of re-running engines inside the refit loop.

Grid (pre-registered BEFORE any WS3 result was seen; nothing enters later):
  common horizon W in {200, 250, 275}      (WS1 verdict: plateau candidates)
  A: K in {5, 7, 9}                        (deployed 7)
  B: K in {5, 7, 9}                        (deployed 7; post-Phase-29 universe)
  C: K in {3, 5, 7} x floor/gate in {(5%, 30%), (0%, 30%)}   (deployed 5, (5%, 30%))
  D: K in {2, 3, 4}                        (deployed 3)
45 curves; weights panels are NOT stored (grid curves feed blend-level
composition only; per-line cost work uses the deployed-config weights
cached by ws3_common).

Three ways this could be silently wrong, and the defences:
  1. LOOK-AHEAD — deployed engines only (prior-day signal row,
     weights.shift(1) * returns); signal panels are trailing rolling
     windows through the deployed builders (ws1_common).
  2. WINDOW INCONSISTENCY — every curve trimmed to the ws3 baseline
     common_end; warm-up differences are irrelevant because all Ws are
     fully defined by COMMON_START (asserted in ws1_common.full_report
     when consumed); the grid parquet carries one shared calendar.
  3. REGRESSION DRIFT — the deployed cells of the grid must reproduce the
     cached WS3/WS2 baselines: (A,200,7) +0.9913, (B,200,7) +1.0217,
     (C,200,5,5%,30%) +0.7341, (D,200,3) +0.8665, all within 0.01
     (identical engines, identical data -> near-exact match expected).

Output: data/ws3_grid_{A,B,C,D}.parquet + data/ws3_grid_meta.json
Run:    python scripts/run_ws3_precompute.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import ws3_common as W3  # noqa: E402
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402
from run_ws1_threshold_surface import c_weighter  # noqa: E402

W_GRID = [200, 250, 275]
K_GRID = {"A": [5, 7, 9], "B": [5, 7, 9], "C": [3, 5, 7], "D": [2, 3, 4]}
C_FG = [(0.05, 0.30), (0.0, 0.30)]
DEPLOYED_REF = {"A": ("W200_K7", 0.9913), "B": ("W200_K7", 1.0217),
                "C": ("W200_K5_F0.05", 0.7341), "D": ("W200_K3", 0.8665)}


def main() -> int:
    base = W3.build_ws3_baselines()
    start, end = base["common_start"], base["common_end"]

    print("Loading panels ...", flush=True)
    closes_a, cons_a = W.load_sleeve_a()
    closes_d, cons_d = W.load_sleeve_d()
    closes_b = B_engine.download_prices().loc[:end]
    closes_c = W.load_sleeve_c().loc[:end]

    curves: dict[str, dict[str, pd.Series]] = {s: {} for s in "ABCD"}
    for w in W_GRID:
        print(f"W={w}: breadth panels ...", flush=True)
        sig_a = W.relative(W.breadth_panel(cons_a, closes_a.index, w))
        bp_d = W.breadth_panel(cons_d, closes_d.index, w)
        sig_b = W.distance_signal(closes_b, w)
        sig_c = W.distance_signal(closes_c, w)
        for k in K_GRID["A"]:
            curves["A"][f"W{w}_K{k}"] = run_portfolio(
                closes_a, sig_a, top_k_breadth_weight(k), start,
                cost=W.COST_A, rebalance_freq=W.REBAL)["equity"].loc[:end]
        for k in K_GRID["D"]:
            curves["D"][f"W{w}_K{k}"] = run_portfolio(
                closes_d, bp_d, top_k_breadth_weight(k), start,
                cost=W.COST_D, rebalance_freq=W.REBAL)["equity"].loc[:end]
        for k in K_GRID["B"]:
            curves["B"][f"W{w}_K{k}"] = B_engine.run_rotation(
                closes_b, sig_b, B_engine.top_k_by_signal(k), start,
                rebalance_freq=W.REBAL, cost=W.COST_B)["equity"].loc[:end]
        for k in K_GRID["C"]:
            for fl, gt in C_FG:
                curves["C"][f"W{w}_K{k}_F{fl}"] = C_engine.run_rotation(
                    closes_c, sig_c, c_weighter(k, fl, gt), start,
                    rebalance_freq=W.REBAL, cost=W.COST_C)["equity"].loc[:end]
        print(f"W={w}: done ({sum(len(v) for v in curves.values())} curves)",
              flush=True)

    meta = {"computed_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "w_grid": W_GRID, "k_grid": K_GRID,
            "c_floor_gate": C_FG, "c_gate_fixed": 0.30,
            "common_start": str(start.date()), "common_end": str(end.date()),
            "regression": {}}
    for s in "ABCD":
        df = pd.DataFrame(curves[s]).sort_index()
        df.to_parquet(W.DATA / f"ws3_grid_{s}.parquet")
        key, ref = DEPLOYED_REF[s]
        got = W.window_stats(df[key].dropna(), start, end)["sharpe"]
        meta["regression"][s] = {"cell": key, "sharpe": got, "ref": ref}
        print(f"  {s} deployed cell {key}: {got:+.4f} (ref {ref:+.4f})")
        assert abs(got - ref) < 0.01, f"grid regression FAILED for {s}"
    (W.DATA / "ws3_grid_meta.json").write_text(json.dumps(meta, indent=1),
                                               encoding="utf-8")
    print("wrote data/ws3_grid_meta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
