"""WS8 — REIT dual-coverage ablation (the prune test WS2 did not run).

Question
--------
US REITs are reached twice: sleeve A holds IUSP (traded via XLRE) and
sleeve B holds VNQ. Their weekly signal correlation is 0.990 — the
highest pair in the book among instruments actually held, and second
only to EEM/IEMG (0.998, and IEMG is not held) across the whole WS2
correlation panel.

WS2 (2026-07-02) adopted the overlap rule "reject CANDIDATES above 0.9
versus an incumbent unless distinct exposure is argued in writing" and
recorded the REIT pair as "deliberate dual-signal coverage, US-only"
(run_ws2_trend_map.py:53). The rule is prospective by construction, so
neither REIT line was ever subjected to it, and the pair was not among
the two pre-registered prune bundles (B-VGK; C-{TAN,SKYY,PAVE}). The
written argument on file is one line, and no ablation stands behind it.

WS2 DID quantify look-through for the other deliberate duals — SPY mean
3.98% / max 10.36% / both-sleeves 43.2% of weeks; QQQ 6.79% / 24.08% /
42.7% — but not for the REIT pair. That gap is closed here too.

Pre-registration (fixed BEFORE any result was inspected)
--------------------------------------------------------
Two variants, both from correlation evidence alone. No other
combinations, no K re-tuning, no floor/gate changes.

  V1  Sleeve B drops VNQ, sleeve A unchanged.  K_B stays 7 of now-11.
  V2  Sleeve A drops IUSP, sleeve B unchanged. K_A stays 7 of now-13.

Both directions are run deliberately. Testing only V1 would presuppose
that B is the line to cut; the pair is symmetric until the evidence says
otherwise, and the two sleeves reach REITs by different signals (A on
constituent breadth, B on price momentum) so there is no a-priori reason
the momentum line is the redundant one.

KEEP BAR (WS2 P1/P2 convention, kill-on-contact, judged at BLEND level
with the varied sleeve spliced into the 35/35/10/20 mix):
  - test-half Sharpe not worse than the deployed blend, AND
  - >= 4 of 6 full sub-periods at or above the deployed blend, AND
  - survives 2x cost at sleeve level.
The bar is deliberately conservative — the incumbent wins ties, so a
"no change" verdict is a legitimate and likely outcome. The value of
running it is that a live 13.1%-of-NAV position stops being asserted and
starts being evidenced, in whichever direction it lands.

Three ways this backtest could be silently wrong, and the defences
------------------------------------------------------------------
1. LOOK-AHEAD — deployed engines only (run_portfolio.run_portfolio for
   A, run_asset_class_rotation.run_rotation for B), which rebalance on
   the PRIOR trading day's signal row and apply weights.shift(1) *
   returns. Variant signals are recomputed on the reduced panels through
   the same ws1_common code path as the baseline, never patched.

2. STALE-COMPARATOR / DEMEAN INCONSISTENCY — the cached WS2 baselines
   (ws2_baseline_*.parquet) are NOT usable here: their sleeve B still
   holds EEM (pre-Phase-29) and their sleeve D predates both the Phase 30
   European rebuild and the 2026-08-03 EXH3->EXH4 instrument correction.
   Comparing a VNQ-drop against them would price three changes as one.
   Baselines are therefore rebuilt from today's deployed configuration on
   the same fixed window, and the deltas versus the cached WS2 meta are
   printed so the shift is visible rather than absorbed. Separately, for
   V2, dropping a column changes sleeve A's cross-sectional demean for
   every remaining member — that is the mechanical consequence of the
   drop, reported as such and not corrected away.

3. COST REALISM — deployed per-sleeve one-way costs (A 2 bps, B 2 bps)
   charged on absolute weight change inside the engines, plus a 2x
   stress run. Pruning mechanically reduces turnover, so costs cannot
   flatter a variant; turnover is reported either way.

And a fourth, specific to a cross-sleeve overlap test and easy to
misread: V1 removes B's REIT line while A keeps IUSP, so the blend
result measures the marginal value of the SECOND REIT line, not the
value of REIT exposure. That is precisely the question asked, but the
numbers must not be quoted as "REITs add nothing".

Offline by construction: the B and C price panels are read from their
committed parquet caches rather than through download_prices(), which
would refetch (caches end after the evaluation window but before the
last completed session) and rewrite files shared with concurrent
sessions. Both caches are written post-adjustment — C's after FX and
expense-ratio drag (run_thematic_rotation.py:541) — so they are exactly
what the loaders would return, minus sessions the window does not use.

Output: data/ws8_reit_overlap.json
Run:    python scripts/run_ws8_reit_overlap.py
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
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "ws8_reit_overlap.json"
WS2_META = DATA / "ws2_baselines_meta.json"

MA = 200
A_REIT = "IUSP"          # traded via XLRE (etf_registry.py:367)
B_REIT = "VNQ"
PAIR_CORR = 0.990        # XLRE/VNQ, ws2_correlation.json pairs_gt_090_full


def load_cached_panel(path: Path, needed: list[str]) -> pd.DataFrame:
    """Deployed price panel straight from its committed parquet cache.

    Deliberately NOT download_prices(): see the module docstring. Raises
    if the cache does not carry exactly the deployed universe, so a
    universe change cannot be silently evaluated against a stale panel.
    """
    df = pd.read_parquet(path)
    missing = sorted(set(needed) - set(df.columns))
    if missing:
        raise RuntimeError(f"{path.name} missing deployed tickers: {missing}")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[needed].sort_index()


def build_today_baselines() -> dict:
    """A/B/C/D on TODAY's deployed configuration, one fixed window."""
    closes_a, cons_a = W.load_sleeve_a()
    closes_d, cons_d = W.load_sleeve_d()
    closes_b = load_cached_panel(B_engine.PRICE_CACHE,
                                 B_engine.TICKERS + B_engine.CASH_ONLY_TICKERS)
    closes_c = load_cached_panel(C_engine.PRICE_CACHE,
                                 C_engine.TICKERS + [C_engine.CASH_PROXY])

    common_start = W.COMMON_START
    common_end = min(closes_b.index.max(), closes_c.index.max(),
                     min(cp.index.max() for cp in cons_a.values()),
                     min(cp.index.max() for cp in cons_d.values()))
    print(f"fixed window {common_start.date()} -> {common_end.date()}")

    sig_a = W.relative(W.breadth_panel(cons_a, closes_a.index, MA))
    run_a = run_portfolio(closes_a, sig_a, top_k_breadth_weight(W.K_A),
                          common_start, cost=W.COST_A, rebalance_freq=W.REBAL)
    run_d = run_portfolio(closes_d, W.breadth_panel(cons_d, closes_d.index, MA),
                          top_k_breadth_weight(W.K_D), common_start,
                          cost=W.COST_D, rebalance_freq=W.REBAL)
    run_b = B_engine.run_rotation(closes_b, W.distance_signal(closes_b, MA),
                                  B_engine.top_k_by_signal(W.K_B),
                                  common_start, rebalance_freq=W.REBAL,
                                  cost=W.COST_B)
    run_c = C_engine.run_rotation(closes_c, W.distance_signal(closes_c, MA),
                                  C_engine.top_k_equal_weight(W.K_C),
                                  common_start, rebalance_freq=W.REBAL,
                                  cost=W.COST_C)

    runs = {"A": run_a, "B": run_b, "C": run_c, "D": run_d}
    eqs = pd.DataFrame({s: r["equity"].loc[:common_end] for s, r in runs.items()})
    weights = {s: r["weights"].loc[:common_end] for s, r in runs.items()}
    return {"equities": eqs, "weights": weights, "panels":
            {"A": (closes_a, cons_a), "B": closes_b},
            "common_start": common_start, "common_end": common_end}


def reit_lookthrough(w_a: pd.DataFrame, w_b: pd.DataFrame) -> dict:
    """The three statistics WS2 computed for SPY/QQQ/IJR but not REITs.

    Effective NAV weight of each REIT line is its within-sleeve weight
    times the sleeve's blend share (A 35%, B 35%), sampled on the weekly
    rebalance grid so 'share of weeks' means what it says.
    """
    a = w_a[A_REIT].resample(W.REBAL).last().dropna()
    b = w_b[B_REIT].resample(W.REBAL).last().dropna()
    idx = a.index.intersection(b.index)
    a, b = a.loc[idx], b.loc[idx]
    eff_a, eff_b = 0.35 * a, 0.35 * b
    combined = eff_a + eff_b
    held_both = ((a > 1e-9) & (b > 1e-9))
    return {
        "n_weeks": int(len(idx)),
        "A_IUSP": {"mean_lookthrough_w": round(float(eff_a.mean()), 4),
                   "max_lookthrough_w": round(float(eff_a.max()), 4),
                   "share_weeks_held": round(float((a > 1e-9).mean()), 3)},
        "B_VNQ": {"mean_lookthrough_w": round(float(eff_b.mean()), 4),
                  "max_lookthrough_w": round(float(eff_b.max()), 4),
                  "share_weeks_held": round(float((b > 1e-9).mean()), 3)},
        "combined": {"mean_lookthrough_w": round(float(combined.mean()), 4),
                     "max_lookthrough_w": round(float(combined.max()), 4),
                     "share_weeks_held_by_both_A_and_B":
                         round(float(held_both.mean()), 3)},
    }


def main() -> int:
    base = build_today_baselines()
    start, end = base["common_start"], base["common_end"]
    eqs, wts = base["equities"], base["weights"]
    closes_a, cons_a = base["panels"]["A"]
    closes_b = base["panels"]["B"]

    idx = eqs.dropna().index
    idx = idx[(idx >= start) & (idx <= end)]
    rets = {s: eqs[s].reindex(idx).pct_change().fillna(0) for s in "ABCD"}
    base_ret = (0.35 * rets["A"] + 0.35 * rets["B"]
                + 0.10 * rets["C"] + 0.20 * rets["D"])
    rep_blend0 = W.full_report((1 + base_ret).cumprod(), None, idx[0], end)
    sleeve_base = {s: W.full_report(eqs[s].dropna(), wts[s], start, end)
                   for s in "ABCD"}

    # Transparency on the rebuilt baseline: how far today's configuration
    # has moved from the cached WS2 reference, per sleeve.
    ws2_meta = json.loads(WS2_META.read_text(encoding="utf-8"))
    drift = {s: round(sleeve_base[s]["full"]["sharpe"]
                      - ws2_meta["sleeve_sharpe"][s], 4) for s in "ABCD"}
    drift["blend"] = round(rep_blend0["full"]["sharpe"]
                           - ws2_meta["blend_sharpe_w200"], 4)
    print("baseline drift vs cached WS2 meta (EEM removal + Phase 30 + "
          f"EXH3->EXH4): {drift}")

    results = {"blend_baseline": rep_blend0, "sleeve_baselines": sleeve_base,
               "baseline_drift_vs_ws2_meta": drift,
               "reit_lookthrough": reit_lookthrough(wts["A"], wts["B"])}

    # --- variants -------------------------------------------------------
    def run_v1():
        c = closes_b.drop(columns=[B_REIT])
        sig = W.distance_signal(c, MA)
        wf = B_engine.top_k_by_signal(W.K_B)
        r1 = B_engine.run_rotation(c, sig, wf, start, rebalance_freq=W.REBAL,
                                   cost=W.COST_B)
        r2 = B_engine.run_rotation(c, sig, wf, start, rebalance_freq=W.REBAL,
                                   cost=W.COST_B * 2)
        return "B", r1, r2

    def run_v2():
        c = closes_a.drop(columns=[A_REIT])
        cons = {k: v for k, v in cons_a.items() if k != A_REIT}
        sig = W.relative(W.breadth_panel(cons, c.index, MA))
        wf = top_k_breadth_weight(W.K_A)
        r1 = run_portfolio(c, sig, wf, start, cost=W.COST_A,
                           rebalance_freq=W.REBAL)
        r2 = run_portfolio(c, sig, wf, start, cost=W.COST_A * 2,
                           rebalance_freq=W.REBAL)
        return "A", r1, r2

    for name, runner in (("V1_B_drop_VNQ", run_v1), ("V2_A_drop_IUSP", run_v2)):
        sleeve, r1, r2 = runner()
        rep = W.full_report(r1["equity"].loc[:end], r1["weights"].loc[:end],
                            start, end)
        rep["sharpe_2x_cost"] = W.window_stats(r2["equity"].loc[:end],
                                               start, end)["sharpe"]
        b = sleeve_base[sleeve]
        rep["sleeve"] = sleeve
        rep["delta_vs_deployed_sleeve"] = {
            "full": round(rep["full"]["sharpe"] - b["full"]["sharpe"], 4),
            "test": round(rep["test"]["sharpe"] - b["test"]["sharpe"], 4),
            "consistency": W.consistency_count(rep["sub_period_sharpe"],
                                               b["sub_period_sharpe"]),
        }
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
            "consistency": W.consistency_count(
                rep_bl["sub_period_sharpe"], rep_blend0["sub_period_sharpe"]),
        }
        # Pre-registered keep bar, evaluated mechanically.
        db = rep["blend_delta"]
        rep["keep_bar"] = {
            "blend_test_not_worse": bool(db["test"] >= 0),
            "blend_consistency_at_least_4_of_6": bool(db["consistency"] >= 4),
            "sleeve_survives_2x_cost": bool(
                rep["sharpe_2x_cost"] >= b["full"]["sharpe"]),
        }
        rep["verdict"] = ("ADOPT DROP" if all(rep["keep_bar"].values())
                          else "KEEP INCUMBENT")
        results[name] = rep
        d = rep["delta_vs_deployed_sleeve"]
        print(f"{name}: sleeve {sleeve} Sharpe {rep['full']['sharpe']:+.3f} "
              f"(2x {rep['sharpe_2x_cost']:+.3f}) dFull {d['full']:+.3f} "
              f"dTest {d['test']:+.3f} cons {d['consistency']}/6 | blend "
              f"dFull {db['full']:+.3f} dTest {db['test']:+.3f} "
              f"cons {db['consistency']}/6 -> {rep['verdict']}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "split": str(W.SPLIT_DATE.date())},
        "pair_corr_xlre_vnq": PAIR_CORR,
        "preregistered": (
            "two variants only, from correlation evidence (XLRE/VNQ 0.990, "
            "ws2_correlation.json): V1 B drops VNQ (K_B 7 of 11); V2 A drops "
            "IUSP (K_A 7 of 13, cross-sectional demean recomputed on 13 — "
            "mechanical consequence, not a re-tune). Keep bar judged at "
            "blend level: test-half not worse AND >=4/6 sub-periods AND "
            "sleeve survives 2x cost. Incumbent wins ties."),
        "baseline_note": (
            "baselines rebuilt on TODAY's deployed configuration; the cached "
            "ws2_baseline_*.parquet are unusable here (sleeve B still holds "
            "EEM pre-Phase-29; sleeve D predates Phase 30 and the 2026-08-03 "
            "EXH3->EXH4 correction)"),
        **results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
