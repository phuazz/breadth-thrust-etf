"""WS2 Experiment 2 — country price-momentum sleeve ("Sleeve E" candidate).

Signal-by-structure: PRICE MOMENTUM (the deployed B formulation — graded
(close - MA200)/MA200, top-K by signal share among positive names, SHY
deficit floor) on single-country ETFs. The positive-signal eligibility plus
the SHY floor IS the "own-200d risk gate" the review prompt asks for — the
sleeve can never hold a country below its own 200d MA and parks deficit
slots in cash. No separate vol gate is added (zero new knobs; WS1 killed
vol-normalisation decisively).

PRE-REGISTERED before any result was seen:
  - Universe U10: EWZ EWW EWY INDA EWT EWA EWS EWG EWU EWJ (+SHY floor).
    Variant U11 adds EEM as an eleventh member. FM EXCLUDED — the fund was
    liquidated (last price 2025-01-08); a dead fund cannot be held through
    the window and its tail is wind-down mechanics.
  - HEADLINE: U10 at K=3 (the Idea-3 / Phase-23 precedent K), 5 bps.
    K in {2,3,4} and U11 are REPORTED for plateau evidence, not selected.
  - Decision bar (all three to ADD, kill on contact otherwise):
      (i) full-window AND test-half Sharpe >= the 50/50 EEM+EFA benchmark;
     (ii) >= 4 of 6 sub-periods not worse than the benchmark;
    (iii) still >= benchmark full-window at 2x cost.
    If passed: blend-level test at A35/B25/C10/D20/E10 (the Phase 22
    funding precedent) — the decision number is the blend delta.
  - Long-window context 2004 -> end on the same config, to reconcile with
    the Idea 3 rejection ("fails in every regime", 23y evidence).

Three ways this backtest could be silently wrong, and the defences:
  1. LOOK-AHEAD — the deployed engine (run_asset_class_rotation.run_rotation)
     rebalances on the PRIOR day's signal row and applies
     weights.shift(1) * returns; signals are trailing 200d MAs on a panel
     fetched once (no per-variant re-fetch).
  2. WINDOW INCONSISTENCY — one fixed window (COMMON_START -> common_end
     from the WS1 baseline meta) for every variant AND the benchmark;
     the panel starts 2003 so the 200d signal is fully warm at
     COMMON_START (asserted: >= 9 of 10 lines have valid signal on the
     first rebalance); benchmark evaluated on the identical calendar.
  3. COST / TR REALISM — uniform 5 bps one-way on ALL turnover including
     the SHY cash legs (conservative: overstates cost vs the deployed
     2 bps cash legs), 2x stress on every variant; USD adjusted closes
     carry dividends (total return); the benchmark pays its own weekly
     rebalancing cost at 2 bps so the comparison is net-of-cost on both
     sides. Survivorship: all ten lines are live funds today (live-fund
     bias inherited and recorded); the one dead candidate (FM) was
     caught by ticker verification and excluded.

Output: data/ws2_country_sleeve.json
Run:    python scripts/run_ws2_country_sleeve.py
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

OUT = W2.DATA / "ws2_country_sleeve.json"

U10 = ["EWZ", "EWW", "EWY", "INDA", "EWT", "EWA", "EWS", "EWG", "EWU", "EWJ"]
CASH = "SHY"
COST_1X = 5 / 10_000          # uniform one-way, incl. cash legs (conservative)
BENCH_COST = 2 / 10_000
HEADLINE_K = 3                # pre-registered (Idea 3 / Phase 23 precedent)
K_GRID = [2, 3, 4]
E_BLEND_W = 0.10              # Phase 22 funding precedent: 10pp out of B


def run_sleeve(closes: pd.DataFrame, K: int, eligible: pd.Timestamp,
               cost: float) -> dict:
    sig = W.distance_signal(closes, 200)
    return B_engine.run_rotation(closes, sig, B_engine.top_k_by_signal(K),
                                 eligible, rebalance_freq=W.REBAL, cost=cost)


def benchmark_5050(prices: pd.DataFrame, eligible: pd.Timestamp,
                   cost: float) -> pd.Series:
    """50/50 EEM+EFA, weekly W-FRI snap-back, cost on rebalancing turnover."""
    px = prices[["EEM", "EFA"]].dropna()
    rets = px.pct_change().fillna(0)
    rb_target = pd.date_range(eligible, px.index[-1], freq=W.REBAL)
    rb = px.index[px.index.isin(rb_target)]
    w = pd.DataFrame(index=px.index, columns=["EEM", "EFA"], dtype=float)
    w.loc[rb, :] = 0.5
    # drift between rebalances: propagate weights with asset returns
    w = w.astype(float)
    cur = None
    rows = []
    for dt in px.index:
        if dt < eligible:
            rows.append((0.0, 0.0))
            continue
        if cur is None:
            cur = pd.Series([0.5, 0.5], index=["EEM", "EFA"])
        grown = cur * (1 + rets.loc[dt])
        grown = grown / grown.sum() if grown.sum() > 0 else grown
        if dt in rb:
            rows.append(tuple(grown))       # pre-rebal weights for turnover
            cur = pd.Series([0.5, 0.5], index=["EEM", "EFA"])
        else:
            rows.append(tuple(grown))
            cur = grown
    drift = pd.DataFrame(rows, index=px.index, columns=["EEM", "EFA"])
    tgt = drift.copy()
    tgt.loc[tgt.index.isin(rb)] = 0.5
    turnover = (tgt - drift).abs().sum(axis=1)
    port = (drift.shift(1).fillna(0) * rets).sum(axis=1) - turnover * cost
    port.loc[port.index < eligible] = 0.0
    return (1 + port).cumprod()


def main() -> int:
    base = W2.build_baselines()
    start, end = base["common_start"], base["common_end"]

    ws2 = W2.load_ws2_prices()
    panel10 = ws2[U10 + [CASH]].loc[:end].dropna(how="all")
    panel11 = ws2[U10 + ["EEM", CASH]].loc[:end].dropna(how="all")

    # signal warm at the fixed start?
    sig_probe = W.distance_signal(panel10[U10], 200)
    first_row = sig_probe.loc[sig_probe.index >= start].iloc[0]
    assert first_row.notna().sum() >= 9, (
        f"signal not warm at {start.date()}: {first_row.notna().sum()}/10")

    results: dict = {"variants": {}, "benchmarks": {}}

    # ---- benchmarks on the identical window ----
    bench = benchmark_5050(ws2.loc[:end], start, BENCH_COST)
    rep_bench = W.full_report(bench.loc[start:end], None, start, end)
    results["benchmarks"]["EEM_EFA_5050"] = rep_bench
    for t in ("EEM", "EFA"):
        eq = ws2[t].loc[start:end].dropna()
        results["benchmarks"][f"{t}_buyhold"] = W.full_report(
            eq / eq.iloc[0], None, start, end)
    print(f"benchmark 50/50 EEM+EFA: Sharpe {rep_bench['full']['sharpe']:+.2f} "
          f"train {rep_bench['train']['sharpe']:+.2f} "
          f"test {rep_bench['test']['sharpe']:+.2f} "
          f"DD {rep_bench['full']['max_dd']*100:.1f}%")

    # ---- sleeve variants on the fixed window ----
    for label, panel in (("U10", panel10), ("U11_with_EEM", panel11)):
        for K in K_GRID:
            r1 = run_sleeve(panel, K, start, COST_1X)
            r2 = run_sleeve(panel, K, start, COST_1X * 2)
            rep = W.full_report(r1["equity"].loc[:end],
                                r1["weights"].loc[:end], start, end)
            rep["sharpe_2x_cost"] = W.window_stats(
                r2["equity"].loc[:end], start, end)["sharpe"]
            results["variants"][f"{label}_K{K}"] = rep
            print(f"{label} K={K}: Sharpe {rep['full']['sharpe']:+.2f} "
                  f"(2x {rep['sharpe_2x_cost']:+.2f}) "
                  f"train {rep['train']['sharpe']:+.2f} "
                  f"test {rep['test']['sharpe']:+.2f} "
                  f"DD {rep['full']['max_dd']*100:.1f}% "
                  f"turn {rep['annual_turnover']:.1f}x")

    # ---- decision bar on the pre-registered headline ----
    head = results["variants"][f"U10_K{HEADLINE_K}"]
    nb = W.consistency_count(head["sub_period_sharpe"],
                             rep_bench["sub_period_sharpe"])
    bar = {
        "full_ge_bench": head["full"]["sharpe"] >= rep_bench["full"]["sharpe"],
        "test_ge_bench": head["test"]["sharpe"] >= rep_bench["test"]["sharpe"],
        "subperiods_ge_4of6": nb >= 4,
        "subperiods_n": nb,
        "cost2x_ge_bench": head["sharpe_2x_cost"] >= rep_bench["full"]["sharpe"],
    }
    bar["PASS"] = all(v for k, v in bar.items()
                      if k in ("full_ge_bench", "test_ge_bench",
                               "subperiods_ge_4of6", "cost2x_ge_bench"))
    results["decision_bar_headline_U10_K3_vs_5050"] = bar
    print(f"\nDecision bar (U10 K=3 vs 50/50 EEM+EFA): {bar}")

    # ---- blend impact, only if the bar passed ----
    if bar["PASS"]:
        r1 = run_sleeve(panel10, HEADLINE_K, start, COST_1X)
        eq_e = r1["equity"].loc[:end]
        eqs = base["equities"]
        idx = (eqs.dropna().index.intersection(eq_e.index))
        idx = idx[(idx >= start) & (idx <= end)]
        rets = {s: eqs[s].reindex(idx).pct_change().fillna(0) for s in "ABCD"}
        ret_e = eq_e.reindex(idx).pct_change().fillna(0)
        base_ret = (0.35 * rets["A"] + 0.35 * rets["B"]
                    + 0.10 * rets["C"] + 0.20 * rets["D"])
        var_ret = (0.35 * rets["A"] + (0.35 - E_BLEND_W) * rets["B"]
                   + 0.10 * rets["C"] + 0.20 * rets["D"] + E_BLEND_W * ret_e)
        rep_b0 = W.full_report((1 + base_ret).cumprod(), None, idx[0], end)
        rep_b1 = W.full_report((1 + var_ret).cumprod(), None, idx[0], end)
        results["blend_impact"] = {
            "baseline_35_35_10_20": rep_b0,
            "with_E10_from_B": rep_b1,
            "delta_full_sharpe": round(rep_b1["full"]["sharpe"]
                                       - rep_b0["full"]["sharpe"], 4),
            "delta_test_sharpe": round(rep_b1["test"]["sharpe"]
                                       - rep_b0["test"]["sharpe"], 4),
        }
        print(f"blend +E10: dSharpe full "
              f"{results['blend_impact']['delta_full_sharpe']:+.3f} "
              f"test {results['blend_impact']['delta_test_sharpe']:+.3f}")

    # ---- long-window context (reconcile with the Idea 3 rejection) ----
    long_panel = ws2[U10 + [CASH]].dropna(how="all")
    elig_long = long_panel.index[210]
    r_long = run_sleeve(long_panel, HEADLINE_K, elig_long, COST_1X)
    eq_long = r_long["equity"]
    bench_long = benchmark_5050(ws2, elig_long, BENCH_COST)
    ctx = {}
    for name, s, e in [("2004_2010", "2004-01-01", "2010-12-31"),
                       ("2011_2013", "2011-01-01", "2013-12-31"),
                       ("2014_2021", "2014-01-01", "2021-12-31"),
                       ("2022_now", "2022-01-01", None)]:
        seg = eq_long.loc[s:e]
        segb = bench_long.loc[s:e]
        ctx[name] = {
            "sleeve_sharpe": W.window_stats(seg, seg.index[0]).get("sharpe")
            if len(seg) > 20 else None,
            "bench_sharpe": W.window_stats(segb, segb.index[0]).get("sharpe")
            if len(segb) > 20 else None,
        }
        print(f"long {name}: sleeve {ctx[name]['sleeve_sharpe']} "
              f"bench {ctx[name]['bench_sharpe']}")
    results["long_window_context_U10_K3"] = ctx

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "split": str(W.SPLIT_DATE.date())},
        "preregistered": ("headline U10 K=3 @5bps; K grid and U11 reported "
                          "not selected; bar = beat 50/50 EEM+EFA on full, "
                          "test, >=4/6 sub-periods, 2x cost"),
        "fm_excluded": "liquidated 2025-01 (see ws2_ticker_verification.json)",
        **results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
