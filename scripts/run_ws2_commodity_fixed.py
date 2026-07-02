"""WS2 Experiment 3 — commodity-spot expansion re-run on the FIXED window.

Folds the in-flight thread (scripts/run_commodity_expansion.py +
data/commodity_expansion.json, 2026-07-01) into the WS1 harness standard.
The original run was already uniformly negative (B MAR 0.54 -> 0.38,
C MAR 0.46 -> 0.28, negative dMAR at every start year 2008-2023, negative
even at 0 bps add-cost), but it used its own windows, MAR as headline, no
train/test split and no sub-period grid. Method deviations found in review:
  (a) common_window() inner-joins with dropna(), which truncated the C
      comparison to 2020-11 (when every thematic finally has data) — a
      different evaluation basis from the deployed C track;
  (b) sleeve-level verdicts only; the review standard is BLEND-level.
This script re-runs the SAME proposed additions (B + {DBA,DBB,DBE};
C + {DBC,DBA,DBB,DBE}) on the fixed window with the cheap reflex and
blend-level deltas. No new configurations are introduced.

Three ways this backtest could be silently wrong, and the defences:
  1. LOOK-AHEAD — reuses run_commodity_expansion.run_rotation, which was
     validated against the deployed engine to 1e-9 equity agreement; the
     self-check is RE-ASSERTED here on the fixed window.
  2. WINDOW INCONSISTENCY — commodity columns are REINDEXED onto the
     deployed sleeve calendars (never inner-joined), so late/absent rows
     cannot silently reshape the window. The commodity cache ends
     2026-06-12; the evaluation end is min(common_end, cache end) applied
     IDENTICALLY to baseline and variant, and the baseline blend is
     re-sliced to that same end for the deltas.
  3. COST / ROLL REALISM — per-ticker one-way costs (B 2 / C 5 bps base,
     10 bps on the DB adds) with a 2x stress; roll/contango cost is inside
     the ETF NAV (adjusted close), which is correct for an ETF backtest
     (the original's argument, kept).

Output: data/ws2_commodity_fixed.json
Run:    python scripts/run_ws2_commodity_fixed.py
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
from run_commodity_expansion import (  # noqa: E402
    run_rotation as pt_rotation, cost_series, B_ADD, C_ADD, COMMOD_COST_BPS,
)

OUT = W2.DATA / "ws2_commodity_fixed.json"


def main() -> int:
    base = W2.build_baselines()
    start = base["common_start"]
    cm = pd.read_parquet(W2.DATA / "commodity_expansion_prices.parquet")
    cm.index = pd.to_datetime(cm.index).tz_localize(None)
    end = min(base["common_end"], cm.index.max())
    print(f"fixed window {start.date()} -> {end.date()} "
          f"(commodity cache bound)")

    closes_b = B_engine.download_prices().loc[:end]
    closes_c = C_engine.download_prices().loc[:end]

    # --- engine self-check re-assert (B, fixed window, scalar costs) ---
    sig_b = W.distance_signal(closes_b, 200)
    cv0 = cost_series(closes_b.columns, 2, {})
    mine = pt_rotation(closes_b, sig_b, B_engine.top_k_by_signal(W.K_B),
                       start, cv0)
    theirs = B_engine.run_rotation(closes_b, sig_b,
                                   B_engine.top_k_by_signal(W.K_B),
                                   start, rebalance_freq=W.REBAL,
                                   cost=2 / 10_000)
    diff = (mine["equity"] - theirs["equity"]).abs().max()
    assert diff < 1e-9, f"engine replication mismatch: {diff}"
    print(f"engine self-check vs deployed: max diff {diff:.1e}")

    results: dict = {}
    variant_eq: dict[str, pd.Series] = {}

    specs = {
        "B_plus_DBA_DBB_DBE": (closes_b, sig_b, B_ADD, 2,
                               B_engine.top_k_by_signal(W.K_B)),
        "C_plus_DBC_DBA_DBB_DBE": (closes_c,
                                   W.distance_signal(closes_c, 200), C_ADD, 5,
                                   C_engine.top_k_equal_weight(W.K_C)),
    }
    for name, (closes, sig, adds, base_bps, weight_fn) in specs.items():
        widened = pd.concat(
            [closes, cm[adds].reindex(closes.index)], axis=1)
        sig_w = W.distance_signal(widened, 200)
        for mult, tag in ((1, "1x"), (2, "2x")):
            cv = cost_series(widened.columns, base_bps * mult,
                             {t: COMMOD_COST_BPS * mult for t in adds})
            r = pt_rotation(widened, sig_w, weight_fn, start, cv)
            if tag == "1x":
                rep = W.full_report(r["equity"].loc[:end],
                                    r["weights"].loc[:end], start, end)
                variant_eq[name] = r["equity"].loc[:end]
            else:
                rep["sharpe_2x_cost"] = W.window_stats(
                    r["equity"].loc[:end], start, end)["sharpe"]
        results[name] = rep
        print(f"{name}: Sharpe {rep['full']['sharpe']:+.2f} "
              f"(2x {rep['sharpe_2x_cost']:+.2f}) "
              f"train {rep['train']['sharpe']:+.2f} "
              f"test {rep['test']['sharpe']:+.2f} "
              f"DD {rep['full']['max_dd']*100:.1f}%")

    # --- sleeve baselines re-sliced to this end for honest deltas ---
    eqs = base["equities"].loc[:end]
    sleeve_base = {s: W.full_report(eqs[s].dropna(), None, start, end)
                   for s in "ABCD"}
    for s, key in (("B", "B_plus_DBA_DBB_DBE"),
                   ("C", "C_plus_DBC_DBA_DBB_DBE")):
        rep, b = results[key], sleeve_base[s]
        rep["delta_vs_deployed"] = {
            "full": round(rep["full"]["sharpe"] - b["full"]["sharpe"], 4),
            "test": round(rep["test"]["sharpe"] - b["test"]["sharpe"], 4),
            "consistency_vs_baseline": W.consistency_count(
                rep["sub_period_sharpe"], b["sub_period_sharpe"]),
        }
        print(f"  {key} vs deployed {s}: dFull "
              f"{rep['delta_vs_deployed']['full']:+.3f} dTest "
              f"{rep['delta_vs_deployed']['test']:+.3f} "
              f"consistency {rep['delta_vs_deployed']['consistency_vs_baseline']}/6")

    # --- blend-level deltas ---
    idx = eqs.dropna().index
    idx = idx[(idx >= start) & (idx <= end)]
    rets = {s: eqs[s].reindex(idx).pct_change().fillna(0) for s in "ABCD"}
    base_ret = (0.35 * rets["A"] + 0.35 * rets["B"]
                + 0.10 * rets["C"] + 0.20 * rets["D"])
    rep_base = W.full_report((1 + base_ret).cumprod(), None, idx[0], end)
    results["blend_baseline"] = rep_base

    blends = {
        "blend_B_widened": {"B": variant_eq["B_plus_DBA_DBB_DBE"]},
        "blend_C_widened": {"C": variant_eq["C_plus_DBC_DBA_DBB_DBE"]},
        "blend_both_widened": {"B": variant_eq["B_plus_DBA_DBB_DBE"],
                               "C": variant_eq["C_plus_DBC_DBA_DBB_DBE"]},
    }
    for name, subs in blends.items():
        r = dict(rets)
        for s, eq in subs.items():
            r[s] = eq.reindex(idx).pct_change().fillna(0)
        v_ret = (0.35 * r["A"] + 0.35 * r["B"]
                 + 0.10 * r["C"] + 0.20 * r["D"])
        rep = W.full_report((1 + v_ret).cumprod(), None, idx[0], end)
        rep["delta_vs_baseline"] = {
            "full": round(rep["full"]["sharpe"] - rep_base["full"]["sharpe"], 4),
            "test": round(rep["test"]["sharpe"] - rep_base["test"]["sharpe"], 4),
            "consistency": W.consistency_count(
                rep["sub_period_sharpe"], rep_base["sub_period_sharpe"]),
        }
        results[name] = rep
        d = rep["delta_vs_baseline"]
        print(f"{name}: dFull {d['full']:+.3f} dTest {d['test']:+.3f} "
              f"consistency {d['consistency']}/6")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "split": str(W.SPLIT_DATE.date()),
                   "note": "end bound by commodity cache 2026-06-12"},
        "method_review_of_original": (
            "original inner-join dropna() truncated C to 2020-11 and used "
            "MAR headline without split/sub-periods; re-run here on the "
            "fixed window, reindex alignment, cheap reflex, blend deltas"),
        "sleeve_baselines": sleeve_base,
        **results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
