"""WS2 Experiment 4 — pre-registered within-sleeve prune tests.

The correlation analysis (run_ws2_correlation.py) found within-sleeve
pairs above the 0.9 flag threshold — the IUIT/CNDX 0.97 prune precedent
territory. EXACTLY TWO drop bundles are tested, pre-registered from the
correlation evidence alone (no performance peeking, no other combinations):

  P1  Sleeve B drop VGK   — VGK/EFA weekly corr 0.984 (both B members;
      EFA is the more liquid, broader line). K stays 7 of now-12.
  P2  Sleeve C drop {TAN, SKYY, PAVE} — TAN/ICLN 0.930, SKYY/CIBR 0.901
      (within-C duplicates; keep the more liquid of each pair) and
      PAVE/XLI 0.954 (a "thematic" that is A's industrials beta in
      disguise). K stays 5 of now-22; the Phase 27 +5%-floor and
      30%-gate rule is UNCHANGED (its denominator becomes 22 names —
      the mechanical consequence of the drop, not a re-tune).

Keep bar (cheap reflex, kill-on-contact): OOS test-half Sharpe not worse
than the deployed sleeve, >=4/6 sub-periods, survives 2x cost — judged at
BLEND level with the sleeve spliced into the 35/35/10/20 mix.

Three ways this backtest could be silently wrong, and the defences:
  1. LOOK-AHEAD — deployed engines only (prior-day signal row,
     weights.shift(1) * returns), signals recomputed on the reduced
     panels with the same trailing 200d code path (ws1_common).
  2. WINDOW INCONSISTENCY — pruned variants run on the identical fixed
     window and calendar as the cached baselines (column drops cannot
     change the calendar; asserted by full_report's start check).
  3. COST REALISM — deployed per-sleeve costs (B 2 / C 5 bps) plus 2x
     stress; pruning REDUCES turnover mechanically, so costs cannot
     flatter the variant (reported anyway).

Output: data/ws2_prune_tests.json
Run:    python scripts/run_ws2_prune_tests.py
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
import ws2_common as W2  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

OUT = W2.DATA / "ws2_prune_tests.json"

P1_DROP_B = ["VGK"]
P2_DROP_C = ["TAN", "SKYY", "PAVE"]


def main() -> int:
    base = W2.build_baselines()
    start, end = base["common_start"], base["common_end"]
    eqs = base["equities"]

    closes_b = B_engine.download_prices().loc[:end]
    closes_c = C_engine.download_prices().loc[:end]

    idx = eqs.dropna().index
    idx = idx[(idx >= start) & (idx <= end)]
    rets = {s: eqs[s].reindex(idx).pct_change().fillna(0) for s in "ABCD"}
    base_ret = (0.35 * rets["A"] + 0.35 * rets["B"]
                + 0.10 * rets["C"] + 0.20 * rets["D"])
    rep_blend0 = W.full_report((1 + base_ret).cumprod(), None, idx[0], end)
    sleeve_base = {s: W.full_report(eqs[s].dropna(), None, start, end)
                   for s in ("B", "C")}

    results = {"blend_baseline": rep_blend0, "sleeve_baselines": sleeve_base}

    specs = {
        "P1_B_drop_VGK": ("B", closes_b.drop(columns=P1_DROP_B),
                          W.COST_B, W.K_B,
                          lambda K: B_engine.top_k_by_signal(K),
                          B_engine.run_rotation),
        "P2_C_drop_TAN_SKYY_PAVE": ("C", closes_c.drop(columns=P2_DROP_C),
                                    W.COST_C, W.K_C,
                                    lambda K: C_engine.top_k_equal_weight(K),
                                    C_engine.run_rotation),
    }
    for name, (sleeve, closes, cost, K, wf, engine) in specs.items():
        sig = W.distance_signal(closes, 200)
        r1 = engine(closes, sig, wf(K), start, rebalance_freq=W.REBAL,
                    cost=cost)
        r2 = engine(closes, sig, wf(K), start, rebalance_freq=W.REBAL,
                    cost=cost * 2)
        rep = W.full_report(r1["equity"].loc[:end], r1["weights"].loc[:end],
                            start, end)
        rep["sharpe_2x_cost"] = W.window_stats(r2["equity"].loc[:end],
                                               start, end)["sharpe"]
        b = sleeve_base[sleeve]
        rep["delta_vs_deployed_sleeve"] = {
            "full": round(rep["full"]["sharpe"] - b["full"]["sharpe"], 4),
            "test": round(rep["test"]["sharpe"] - b["test"]["sharpe"], 4),
            "consistency": W.consistency_count(rep["sub_period_sharpe"],
                                               b["sub_period_sharpe"]),
        }
        # blend splice
        r = dict(rets)
        r[sleeve] = r1["equity"].reindex(idx).pct_change().fillna(0)
        v_ret = (0.35 * r["A"] + 0.35 * r["B"]
                 + 0.10 * r["C"] + 0.20 * r["D"])
        rep_bl = W.full_report((1 + v_ret).cumprod(), None, idx[0], end)
        rep["blend_spliced"] = rep_bl
        rep["blend_delta"] = {
            "full": round(rep_bl["full"]["sharpe"]
                          - rep_blend0["full"]["sharpe"], 4),
            "test": round(rep_bl["test"]["sharpe"]
                          - rep_blend0["test"]["sharpe"], 4),
            "consistency": W.consistency_count(rep_bl["sub_period_sharpe"],
                                               rep_blend0["sub_period_sharpe"]),
        }
        results[name] = rep
        d, db = rep["delta_vs_deployed_sleeve"], rep["blend_delta"]
        print(f"{name}: sleeve Sharpe {rep['full']['sharpe']:+.2f} "
              f"(2x {rep['sharpe_2x_cost']:+.2f}) "
              f"dFull {d['full']:+.3f} dTest {d['test']:+.3f} "
              f"cons {d['consistency']}/6 | blend dFull {db['full']:+.3f} "
              f"dTest {db['test']:+.3f} cons {db['consistency']}/6 "
              f"DD {rep['full']['max_dd']*100:.1f}%")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "split": str(W.SPLIT_DATE.date())},
        "preregistered": ("two bundles only, from correlation evidence: "
                          "B-VGK (0.984 vs EFA); C-{TAN,SKYY,PAVE} "
                          "(0.930/0.901 within-C, PAVE 0.954 vs XLI). "
                          "Phase 27 floor/gate untouched (denominator "
                          "mechanically 22)."),
        **results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
