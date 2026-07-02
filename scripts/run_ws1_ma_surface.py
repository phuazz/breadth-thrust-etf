"""WS1 Experiment 1 — MA-lookback parameter SURFACE for the deployed sleeves.

Question: is the inherited 200d horizon a plateau or a lucky peak? For each
sleeve independently, re-run the DEPLOYED formulation (same K, weighting,
cadence, floor/gate, costs) with the signal lookback swept over
{50, 75, 100, 125, 150, 200, 250, 300}; also a JOINT blend surface where all
four sleeves move to the same lookback. Deploy-from-the-flat-middle framing:
we report the surface, the peak-to-plateau gap, split-half rank stability and
sub-period consistency — NOT a new "best" lookback.

Silent-failure defences (see ws1_common.py docstring): deployed engines only
(look-ahead), one fixed evaluation window for every W (window inconsistency),
deployed per-sleeve costs + 2x stress (cost realism).

Output: data/ws1_ma_surface.json
Run:    python scripts/run_ws1_ma_surface.py
"""
from __future__ import annotations

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

# 25d-step grid, strict superset of the original 8-point grid. Grid SIZE is
# a trial count for WS3's deflated-Sharpe audit — log any change here.
GRID = [25, 50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300, 325]
DEPLOYED_W = 200
OUT = W.DATA / "ws1_ma_surface.json"


def main() -> int:
    print("Loading sleeve panels ...", flush=True)
    closes_a, cons_a = W.load_sleeve_a()
    closes_d, cons_d = W.load_sleeve_d()
    closes_b = W.load_sleeve_b()
    closes_c = W.load_sleeve_c()

    # Common end: last date where every sleeve still has live signal inputs.
    # D's constituent caches are the binding constraint (EU roster refresh).
    d_cons_end = min(cp.index.max() for cp in cons_d.values())
    a_cons_end = min(cp.index.max() for cp in cons_a.values())
    common_end = min(closes_b.index.max(), closes_c.index.max(),
                     a_cons_end, d_cons_end)
    common_start = W.COMMON_START
    print(f"Common evaluation window: {common_start.date()} -> "
          f"{common_end.date()} (split {W.SPLIT_DATE.date()})")

    # Pre-compute breadth panels per W (the expensive part) once.
    print("Computing breadth panels for sleeve A/D across the grid ...",
          flush=True)
    bp_a = {w: W.breadth_panel(cons_a, closes_a.index, w) for w in GRID}
    bp_d = {w: W.breadth_panel(cons_d, closes_d.index, w) for w in GRID}

    results: dict[str, dict] = {s: {} for s in ("A", "B", "C", "D", "blend")}
    eq_at_w: dict[int, dict[str, pd.Series]] = {w: {} for w in GRID}

    for w in GRID:
        print(f"\n=== lookback {w}d ===", flush=True)
        runs = {}
        # Sleeve A — relative breadth, K=7, 2 bps
        sig_a = W.relative(bp_a[w])
        runs["A"] = {
            c: run_portfolio(closes_a, sig_a, top_k_breadth_weight(W.K_A),
                             common_start, cost=W.COST_A * m,
                             rebalance_freq=W.REBAL)
            for c, m in (("1x", 1), ("2x", 2))
        }
        # Sleeve D — absolute breadth, K=3, 9 bps
        runs["D"] = {
            c: run_portfolio(closes_d, bp_d[w], top_k_breadth_weight(W.K_D),
                             common_start, cost=W.COST_D * m,
                             rebalance_freq=W.REBAL)
            for c, m in (("1x", 1), ("2x", 2))
        }
        # Sleeve B — graded distance, K=7 positive-only, SHY floor, 2 bps
        sig_b = W.distance_signal(closes_b, w)
        runs["B"] = {
            c: B_engine.run_rotation(closes_b, sig_b,
                                     B_engine.top_k_by_signal(W.K_B),
                                     common_start, rebalance_freq=W.REBAL,
                                     cost=W.COST_B * m)
            for c, m in (("1x", 1), ("2x", 2))
        }
        # Sleeve C — deployed weighter at W: +5% floor and 30% sleeve gate
        # apply to the W-panel itself (the honest "run C at W" formulation;
        # note the fixed floor bites harder at short W — reported, not tuned).
        sig_c = W.distance_signal(closes_c, w)
        runs["C"] = {
            c: C_engine.run_rotation(closes_c, sig_c,
                                     C_engine.top_k_equal_weight(W.K_C),
                                     common_start, rebalance_freq=W.REBAL,
                                     cost=W.COST_C * m)
            for c, m in (("1x", 1), ("2x", 2))
        }

        for sleeve in ("A", "B", "C", "D"):
            r1, r2 = runs[sleeve]["1x"], runs[sleeve]["2x"]
            eq1 = r1["equity"].loc[:common_end]
            rep = W.full_report(eq1, r1["weights"].loc[:common_end],
                                common_start, common_end)
            rep["sharpe_2x_cost"] = W.window_stats(
                r2["equity"].loc[:common_end], common_start, common_end,
            )["sharpe"]
            results[sleeve][str(w)] = rep
            eq_at_w[w][sleeve] = eq1
            print(f"  {sleeve}@{w:>3}: Sharpe {rep['full']['sharpe']:+.2f} "
                  f"(2x {rep['sharpe_2x_cost']:+.2f})  "
                  f"CAGR {rep['full']['cagr']*100:+.1f}%  "
                  f"DD {rep['full']['max_dd']*100:.1f}%  "
                  f"turn {rep['annual_turnover']:.1f}x  "
                  f"train {rep['train']['sharpe']:+.2f} / "
                  f"test {rep['test']['sharpe']:+.2f}")

        # Joint blend at this W (ungated 35/35/10/20; overlays out of scope)
        beq = W.blend_equity(eq_at_w[w]["A"], eq_at_w[w]["B"],
                             eq_at_w[w]["C"], eq_at_w[w]["D"],
                             common_start, common_end)
        rep_b = W.full_report(beq, None, beq.index[0], common_end)
        results["blend"][str(w)] = rep_b
        print(f"  blend@{w:>3}: Sharpe {rep_b['full']['sharpe']:+.2f}  "
              f"CAGR {rep_b['full']['cagr']*100:+.1f}%  "
              f"DD {rep_b['full']['max_dd']*100:.1f}%")

    # ---- Surface summary: peak vs plateau vs deployed, rank stability ----
    summary = {}
    for sleeve in ("A", "B", "C", "D", "blend"):
        sh = pd.Series({w: results[sleeve][str(w)]["full"]["sharpe"]
                        for w in GRID}, dtype=float)
        tr = pd.Series({w: results[sleeve][str(w)]["train"]["sharpe"]
                        for w in GRID}, dtype=float)
        te = pd.Series({w: results[sleeve][str(w)]["test"]["sharpe"]
                        for w in GRID}, dtype=float)
        rank_corr = float(tr.rank().corr(te.rank()))
        summary[sleeve] = {
            "sharpe_by_w": {str(w): W._safe(sh[w]) for w in GRID},
            "peak_w": int(sh.idxmax()),
            "peak_sharpe": W._safe(sh.max()),
            "plateau_median_sharpe": W._safe(sh.median()),
            "peak_minus_plateau": W._safe(sh.max() - sh.median()),
            "deployed_sharpe": W._safe(sh[DEPLOYED_W]),
            "deployed_minus_plateau": W._safe(sh[DEPLOYED_W] - sh.median()),
            "grid_min_sharpe": W._safe(sh.min()),
            "train_test_rank_corr": W._safe(rank_corr),
        }
        print(f"\n{sleeve}: peak W={sh.idxmax()} ({sh.max():+.2f}), "
              f"median {sh.median():+.2f}, deployed@200 {sh[200]:+.2f}, "
              f"train/test rank corr {rank_corr:+.2f}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("MA-lookback surface for deployed sleeve formulations"
                        " + joint 35/35/10/20 blend (ungated). Fixed window,"
                        " deployed costs, 2x stress."),
        "grid": GRID,
        "deployed_w": DEPLOYED_W,
        "common_start": str(common_start.date()),
        "common_end": str(common_end.date()),
        "split_date": str(W.SPLIT_DATE.date()),
        "costs_bps_one_way": {"A": 2, "B": 2, "C": 5, "D": 9},
        "summary": summary,
        "surface": results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
