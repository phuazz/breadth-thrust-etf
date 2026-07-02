"""WS1 Experiments 2+3 — vol-normalised signals, horizon ensembles, overlays.

Decomposes the prompt's proposals into attributable single steps, each run on
the FIXED common window with deployed K/cadence/costs and a 2x-cost stress:

  Breadth sleeves (A, D):   deployed = binary share-above-MA @200
    V1a graded raw @200     — grading alone (item 3a without vol)
    V1  graded vol-z @200   — grading + vol normalisation (3a)
    V3  binary ensemble     — multi-horizon alone (2a on the deployed signal)
    V2  graded vol-z ensemble {50,100,150,200} — 2a + 3a jointly (preferred
        formulation in the prompt)
  Momentum sleeves (B, C):  deployed = graded raw distance @200
    V1  vol-z @200          — vol normalisation alone (3a)
    V3  raw ensemble        — multi-horizon alone (2a)
    V2  vol-z ensemble      — 2a + 3a jointly
  Overlays (each SEPARATELY, on V0 baseline sleeves):
    S1  vol-targeted sleeve sizing (3b): weekly blend weights proportional to
        base_weight / trailing 63d sleeve vol, renormalised (no leverage)
    S2  slope gate (3c) on B and C: candidate must also have a RISING 200d MA
        (ma.diff(21) > 0). Sign-based, so no new threshold knob.

Zero-knob discipline: vol window 63d and sqrt(W) horizon scaling fixed
ex-ante (ws1_common.py); C's +5% floor and 30% sleeve gate stay on the
DEPLOYED raw-200d panel in all C variants — only the ranking signal changes
(c_rank_decoupled_weighter). A stays cross-sectionally relative; D absolute.
Graded signals can turn negative, so D variants gain an implicit cash floor
the deployed binary version does not have — invested share is reported so the
effect is attributable.

Silent-failure defences: see ws1_common.py docstring (deployed engines for
look-ahead; fixed window; deployed costs + 2x stress; C loader keeps FX and
expense drags).

Output: data/ws1_vol_variants.json
Run:    python scripts/run_ws1_vol_variants.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

OUT = W.DATA / "ws1_vol_variants.json"
EW = W.ENSEMBLE_WINDOWS  # [50, 100, 150, 200]


def report_run(run1, run2, common_start, common_end) -> dict:
    eq1 = run1["equity"].loc[:common_end]
    rep = W.full_report(eq1, run1["weights"].loc[:common_end],
                        common_start, common_end)
    rep["sharpe_2x_cost"] = W.window_stats(
        run2["equity"].loc[:common_end], common_start, common_end)["sharpe"]
    wts = run1["weights"].loc[common_start:common_end]
    rep["avg_invested_share"] = W._safe(
        float(wts.sum(axis=1).clip(upper=1.0).mean()))
    return rep, eq1


def verdict(rep: dict, base: dict) -> dict:
    """Cheap-reflex verdict vs baseline: OOS test delta, >=4/6 sub-period
    consistency, 2x-cost delta. All three must hold to KEEP."""
    d_full = rep["full"]["sharpe"] - base["full"]["sharpe"]
    d_test = rep["test"]["sharpe"] - base["test"]["sharpe"]
    d_2x = rep["sharpe_2x_cost"] - base["sharpe_2x_cost"]
    n_cons = W.consistency_count(rep["sub_period_sharpe"],
                                 base["sub_period_sharpe"])
    keep = (d_test >= 0.0) and (n_cons >= 4) and (d_2x >= -0.02)
    return {
        "delta_full_sharpe": W._safe(d_full),
        "delta_train_sharpe": W._safe(
            rep["train"]["sharpe"] - base["train"]["sharpe"]),
        "delta_test_sharpe": W._safe(d_test),
        "delta_2x_cost_sharpe": W._safe(d_2x),
        "subperiods_geq_baseline": n_cons,
        "verdict": "KEEP" if keep else "KILL",
    }


def main() -> int:
    print("Loading sleeve panels ...", flush=True)
    closes_a, cons_a = W.load_sleeve_a()
    closes_d, cons_d = W.load_sleeve_d()
    closes_b = W.load_sleeve_b()
    closes_c = W.load_sleeve_c()

    d_cons_end = min(cp.index.max() for cp in cons_d.values())
    a_cons_end = min(cp.index.max() for cp in cons_a.values())
    common_end = min(closes_b.index.max(), closes_c.index.max(),
                     a_cons_end, d_cons_end)
    cs = W.COMMON_START
    print(f"Window {cs.date()} -> {common_end.date()}")

    # ---------------- signal panels ----------------
    print("Building signal panels ...", flush=True)
    # A / D breadth families
    panels_ad = {}
    for name, closes, cons in (("A", closes_a, cons_a), ("D", closes_d, cons_d)):
        idx = closes.index
        binary200 = W.breadth_panel(cons, idx, 200)
        graded_raw200 = W.graded_breadth_panel(cons, idx, 200, False)
        graded_z200 = W.graded_breadth_panel(cons, idx, 200, True)
        binary_ens = W.ensemble_mean([W.breadth_panel(cons, idx, w) for w in EW])
        graded_z_ens = W.ensemble_mean(
            [W.graded_breadth_panel(cons, idx, w, True) for w in EW])
        panels_ad[name] = {
            "V0_deployed_binary200": binary200,
            "V1a_graded_raw200": graded_raw200,
            "V1_graded_z200": graded_z200,
            "V3_binary_ensemble": binary_ens,
            "V2_graded_z_ensemble": graded_z_ens,
        }
    # B / C momentum families
    panels_bc = {}
    for name, closes in (("B", closes_b), ("C", closes_c)):
        panels_bc[name] = {
            "V0_deployed_raw200": W.distance_signal(closes, 200),
            "V1_volz200": W.vol_norm_signal(closes, 200),
            "V3_raw_ensemble": W.ensemble_mean(
                [W.distance_signal(closes, w) for w in EW]),
            "V2_volz_ensemble": W.ensemble_mean(
                [W.vol_norm_signal(closes, w) for w in EW]),
        }

    results: dict = {"A": {}, "B": {}, "C": {}, "D": {}}
    eq_store: dict = {"A": {}, "B": {}, "C": {}, "D": {}}

    # ---------------- Sleeve A (relative) and D (absolute) ----------------
    for sleeve, closes, K, cost in (("A", closes_a, W.K_A, W.COST_A),
                                    ("D", closes_d, W.K_D, W.COST_D)):
        print(f"\n=== Sleeve {sleeve} breadth-family variants ===")
        for vname, panel in panels_ad[sleeve].items():
            sig = W.relative(panel) if sleeve == "A" else panel
            runs = [run_portfolio(closes, sig, top_k_breadth_weight(K), cs,
                                  cost=cost * m, rebalance_freq=W.REBAL)
                    for m in (1, 2)]
            rep, eq = report_run(runs[0], runs[1], cs, common_end)
            results[sleeve][vname] = rep
            eq_store[sleeve][vname] = eq
            print(f"  {vname:26s} Sharpe {rep['full']['sharpe']:+.2f} "
                  f"(tr {rep['train']['sharpe']:+.2f}/te {rep['test']['sharpe']:+.2f}) "
                  f"DD {rep['full']['max_dd']*100:.0f}% "
                  f"turn {rep['annual_turnover']:.1f}x "
                  f"inv {rep['avg_invested_share']*100:.0f}%")

    # ---------------- Sleeve B ----------------
    print("\n=== Sleeve B momentum-family variants ===")
    for vname, sig in panels_bc["B"].items():
        runs = [B_engine.run_rotation(closes_b, sig,
                                      B_engine.top_k_by_signal(W.K_B), cs,
                                      rebalance_freq=W.REBAL,
                                      cost=W.COST_B * m) for m in (1, 2)]
        rep, eq = report_run(runs[0], runs[1], cs, common_end)
        results["B"][vname] = rep
        eq_store["B"][vname] = eq
        print(f"  {vname:26s} Sharpe {rep['full']['sharpe']:+.2f} "
              f"(tr {rep['train']['sharpe']:+.2f}/te {rep['test']['sharpe']:+.2f}) "
              f"DD {rep['full']['max_dd']*100:.0f}% "
              f"turn {rep['annual_turnover']:.1f}x")

    # ---------------- Sleeve C (rank decoupled from floor/gate) -----------
    print("\n=== Sleeve C momentum-family variants (floor/gate deployed) ===")
    raw200_c = panels_bc["C"]["V0_deployed_raw200"]
    for vname, sig in panels_bc["C"].items():
        if vname.startswith("V0"):
            wf = C_engine.top_k_equal_weight(W.K_C)  # exact deployed path
        else:
            wf = W.c_rank_decoupled_weighter(
                W.K_C, raw200_c, C_engine.SIGNAL_FLOOR,
                C_engine.SLEEVE_GATE_THRESHOLD)
        runs = [C_engine.run_rotation(closes_c, sig, wf, cs,
                                      rebalance_freq=W.REBAL,
                                      cost=W.COST_C * m) for m in (1, 2)]
        rep, eq = report_run(runs[0], runs[1], cs, common_end)
        results["C"][vname] = rep
        eq_store["C"][vname] = eq
        print(f"  {vname:26s} Sharpe {rep['full']['sharpe']:+.2f} "
              f"(tr {rep['train']['sharpe']:+.2f}/te {rep['test']['sharpe']:+.2f}) "
              f"DD {rep['full']['max_dd']*100:.0f}% "
              f"turn {rep['annual_turnover']:.1f}x")

    # ---------------- S2 slope gate on B and C (separate overlay) ---------
    print("\n=== S2 slope gate (rising 200d MA, diff 21d > 0) ===")
    for sleeve, closes in (("B", closes_b), ("C", closes_c)):
        ma = closes.rolling(200, min_periods=200).mean()
        slope_ok = ma.diff(21) > 0
        if sleeve == "B":
            sig = panels_bc["B"]["V0_deployed_raw200"].where(slope_ok)
            runs = [B_engine.run_rotation(closes, sig,
                                          B_engine.top_k_by_signal(W.K_B), cs,
                                          rebalance_freq=W.REBAL,
                                          cost=W.COST_B * m) for m in (1, 2)]
        else:
            rank_masked = raw200_c.where(slope_ok)
            wf = W.c_rank_decoupled_weighter(
                W.K_C, raw200_c, C_engine.SIGNAL_FLOOR,
                C_engine.SLEEVE_GATE_THRESHOLD)
            runs = [C_engine.run_rotation(closes, rank_masked, wf, cs,
                                          rebalance_freq=W.REBAL,
                                          cost=W.COST_C * m) for m in (1, 2)]
        rep, eq = report_run(runs[0], runs[1], cs, common_end)
        results[sleeve]["S2_slope_gate"] = rep
        eq_store[sleeve]["S2_slope_gate"] = eq
        print(f"  {sleeve}+slope_gate            Sharpe {rep['full']['sharpe']:+.2f} "
              f"(tr {rep['train']['sharpe']:+.2f}/te {rep['test']['sharpe']:+.2f}) "
              f"DD {rep['full']['max_dd']*100:.0f}%")

    # ---------------- verdicts vs sleeve baselines ----------------
    base_key = {"A": "V0_deployed_binary200", "D": "V0_deployed_binary200",
                "B": "V0_deployed_raw200", "C": "V0_deployed_raw200"}
    for sleeve in ("A", "B", "C", "D"):
        base = results[sleeve][base_key[sleeve]]
        for vname, rep in results[sleeve].items():
            if vname == base_key[sleeve]:
                continue
            rep["vs_baseline"] = verdict(rep, base)

    # ---------------- blend-level: single-sleeve swaps + all-V2 -----------
    print("\n=== Blend-level impact (swap one sleeve, others deployed) ===")
    blend_reports = {}
    v0 = {s: eq_store[s][base_key[s]] for s in ("A", "B", "C", "D")}
    beq0 = W.blend_equity(v0["A"], v0["B"], v0["C"], v0["D"], cs, common_end)
    blend_reports["V0_all_deployed"] = W.full_report(beq0, None,
                                                     beq0.index[0], common_end)
    print(f"  V0 blend Sharpe {blend_reports['V0_all_deployed']['full']['sharpe']:+.3f}")
    for sleeve in ("A", "B", "C", "D"):
        for vname, eq in eq_store[sleeve].items():
            if vname == base_key[sleeve]:
                continue
            curves = dict(v0)
            curves[sleeve] = eq
            beq = W.blend_equity(curves["A"], curves["B"], curves["C"],
                                 curves["D"], cs, common_end)
            rep = W.full_report(beq, None, beq.index[0], common_end)
            rep["vs_baseline"] = verdict(
                {**rep, "sharpe_2x_cost": rep["full"]["sharpe"]},
                {**blend_reports["V0_all_deployed"],
                 "sharpe_2x_cost":
                     blend_reports["V0_all_deployed"]["full"]["sharpe"]})
            blend_reports[f"{sleeve}:{vname}"] = rep
            print(f"  swap {sleeve}:{vname:26s} blend Sharpe "
                  f"{rep['full']['sharpe']:+.3f} "
                  f"(d {rep['vs_baseline']['delta_full_sharpe']:+.3f}, "
                  f"test d {rep['vs_baseline']['delta_test_sharpe']:+.3f}, "
                  f"cons {rep['vs_baseline']['subperiods_geq_baseline']}/6)")
    # all four sleeves on the preferred V2 formulation
    v2 = {s: eq_store[s].get("V2_graded_z_ensemble",
                             eq_store[s].get("V2_volz_ensemble"))
          for s in ("A", "B", "C", "D")}
    beq_v2 = W.blend_equity(v2["A"], v2["B"], v2["C"], v2["D"], cs, common_end)
    rep_v2 = W.full_report(beq_v2, None, beq_v2.index[0], common_end)
    rep_v2["vs_baseline"] = verdict(
        {**rep_v2, "sharpe_2x_cost": rep_v2["full"]["sharpe"]},
        {**blend_reports["V0_all_deployed"],
         "sharpe_2x_cost": blend_reports["V0_all_deployed"]["full"]["sharpe"]})
    blend_reports["ALL_V2_volz_ensemble"] = rep_v2
    print(f"  ALL-V2 blend Sharpe {rep_v2['full']['sharpe']:+.3f} "
          f"(d {rep_v2['vs_baseline']['delta_full_sharpe']:+.3f})")

    # ---------------- S1 vol-targeted sleeve sizing on V0 curves ----------
    print("\n=== S1 vol-targeted sleeve sizing (risk-parity-lite) ===")
    idx = beq0.index
    rets = {s: v0[s].reindex(idx).pct_change().fillna(0)
            for s in ("A", "B", "C", "D")}
    base_w = dict(zip(("A", "B", "C", "D"), (*W.BLEND_W, 0.20)))
    vols = pd.DataFrame({s: rets[s].rolling(W.VOL_WIN).std()
                         for s in ("A", "B", "C", "D")})
    raw_w = pd.DataFrame({s: base_w[s] / vols[s] for s in ("A", "B", "C", "D")})
    raw_w = raw_w.div(raw_w.sum(axis=1), axis=0)
    fridays = idx[idx.dayofweek == 4]
    w_panel = raw_w.reindex(fridays).reindex(idx, method="ffill")
    w_panel = w_panel.fillna(pd.Series(base_w))  # warm-up: deployed weights
    ret_df = pd.DataFrame(rets)
    s1_ret = (w_panel.shift(1) * ret_df).sum(axis=1)
    s1_ret -= w_panel.diff().abs().sum(axis=1).fillna(0) * (5 / 10_000)
    s1_eq = (1 + s1_ret).cumprod()
    rep_s1 = W.full_report(s1_eq, None, s1_eq.index[0], common_end)
    rep_s1["avg_weights"] = {s: W._safe(float(w_panel[s].mean()))
                             for s in ("A", "B", "C", "D")}
    rep_s1["vs_baseline"] = verdict(
        {**rep_s1, "sharpe_2x_cost": rep_s1["full"]["sharpe"]},
        {**blend_reports["V0_all_deployed"],
         "sharpe_2x_cost": blend_reports["V0_all_deployed"]["full"]["sharpe"]})
    blend_reports["S1_vol_targeted_sizing"] = rep_s1
    print(f"  S1 blend Sharpe {rep_s1['full']['sharpe']:+.3f} "
          f"(d {rep_s1['vs_baseline']['delta_full_sharpe']:+.3f}, "
          f"test d {rep_s1['vs_baseline']['delta_test_sharpe']:+.3f}, "
          f"cons {rep_s1['vs_baseline']['subperiods_geq_baseline']}/6)  "
          f"avg w {rep_s1['avg_weights']}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Vol-normalised / ensemble signal variants per sleeve"
                        " + slope gate + vol-targeted sizing, vs deployed"
                        " baselines on a fixed window with deployed costs."),
        "common_start": str(cs.date()),
        "common_end": str(common_end.date()),
        "split_date": str(W.SPLIT_DATE.date()),
        "ensemble_windows": EW,
        "vol_window_days": W.VOL_WIN,
        "sleeves": results,
        "blend": blend_reports,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
