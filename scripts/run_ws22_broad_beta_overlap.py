"""WS22 — cross-sleeve broad US beta: does the second line earn its seat?

Registration: KICKOFF_ws22-cross-sleeve-broad-beta.md, countersigned
2026-09-19, frozen at commit 5fc2b4b. This engine implements §4 and nothing
else. No K re-tuning, no budget change, no arm outside the ten registered.

Question
--------
Sleeve A holds CSP1, CNDX and IDP6; sleeve B holds SPY, QQQ and IJR. Each
sleeve A line is PRICED THROUGH its sleeve B twin (etf_registry
`yfinance_trading_proxy`), so the book carries one return series twice per
pair. WS2 quantified the look-through and accepted the duals; WS8 ran the
analogous ablation on IUSP/VNQ and returned KEEP BOTH. Neither ever ablated
these three. That is what this does.

Arms (§4) — ten, and no eleventh
--------------------------------
  Pairs   P1 = (CSP1, SPY)   P2 = (CNDX, QQQ)   P3 = (IDP6, IJR)

  V1(P)   sleeve B drops its line.   K_B fixed 7 of now-11.
  V2(P)   sleeve A drops its line.   K_A fixed 7 of now-13.
  V3(P)   both drop.                 K_A 7 of 13, K_B 7 of 11.
  V4      sleeve A drops all three.  K_A fixed 7 of now-11.   (H2)

  N1      exhaustive C(11,3) = 165 three-name drops from sleeve A's OTHER
          eleven members, same K_A = 7 of 11 — the count-matched null that
          separates "broad beta was redundant" from "sleeve A does better
          holding 7 of 11 than 7 of 14". A reference distribution, not
          candidate configurations.

Keep bar — inherited VERBATIM from WS2 P1/P2 as WS8 implemented it
(run_ws8_reit_overlap.py:279-288). Conjunctive, kill-on-contact, judged at
BLEND level with the varied sleeve(s) spliced into the 35/35/10/20 mix:
  - blend test-half Sharpe delta >= 0, AND
  - blend consistency >= 4 of the 6 full sub-periods, AND
  - the varied sleeve survives 2x cost.
The incumbent wins ties. For V3, which varies two sleeves, leg 3 is read
CONJUNCTIVELY — both must clear their own 2x leg. That reading was fixed in
§4 before any result was computed, not at the point of reading one.

Two further gates, both registered, both making adoption harder:
  - COHERENCE. The three pairs are one mechanism repeated. A per-pair arm
    type is adoption-eligible only if it passes on at least 2 of the 3 pairs.
    A lone pass is recorded NOT ADOPTED and read as noise.
  - H2 NULL GATE. V4's blend test-half delta must sit at or above p90 of N1,
    whatever the keep bar says, or the result is not separable from the
    selection-ratio change.

Three ways this could be silently wrong, and the defences (§5)
--------------------------------------------------------------
1. BASELINE TRAP. The cached ws2_baseline_*.parquet are unusable (WS8: sleeve
   B still holds EEM pre-Phase-29; sleeve D predates Phase 30 and the
   EXH3->EXH4 fix) and the book has moved again since (WS15/WS16 restatements,
   the per-column Norgate cutover, the staged sleeve D roster). Baselines are
   rebuilt from the deployed configuration on one fixed window, and the drift
   against the cached WS2 meta is printed so the shift is visible rather than
   absorbed. SECOND HALF: BTE_APPLY_STAGED_ROSTER and BTE_C_BTC_BASIS are
   asserted unset before anything loads — either one set would price a staged
   change as part of a drop.
2. DEMEAN AND SELECTION RATIO. Dropping a column changes sleeve A's
   cross-sectional demean for every remaining member, and moves the selection
   ratio from 7-of-14 to 7-of-13 or 7-of-11. Both are mechanical consequences
   of the drop, reported and not corrected away. For V4 the ratio change is a
   rival explanation in its own right, which is what N1 settles.
3. COST REALISM. Deployed one-way costs (A 2 bps, B 2 bps) charged inside the
   engines on absolute weight change, plus the 2x stress leg. Pruning
   mechanically reduces turnover, so costs cannot flatter a variant; turnover
   is reported either way. Standing caveat: sleeve A's 2 bps is the US proxy's
   liquidity, not the LSE UCITS line's — WS14 left the realised spread on the
   thinner lines open, and nothing here is evidence about UCITS execution.

A fourth, carried from WS8 and easy to misread: V1 removes sleeve B's line
while sleeve A keeps its twin, so the result measures the marginal value of
the SECOND line, not of the index exposure. No V1 or V2 number may be quoted
as "the NASDAQ-100 adds nothing". Only V3 speaks to the exposure.

A fifth, specific to a proxy-identity pair: the two lines share a price series
but not a selection — A ranks on constituent breadth, B on the ETF's own 200d
distance. A finding that the second line adds nothing is a finding about the
second SIGNAL's marginal value at this budget, not that the two signals are
the same.

Offline by construction: B and C price panels are read from their committed
parquet caches rather than through download_prices(), which would refetch and
rewrite files shared with concurrent sessions.

Output: data/ws22_broad_beta_overlap.json
Run:    python scripts/run_ws22_broad_beta_overlap.py [--smoke]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "ws22_broad_beta_overlap.json"
WS2_META = DATA / "ws2_baselines_meta.json"

MA = 200
FREEZE_COMMIT = "5fc2b4b"

# §4 pairs, in registration order. `a` is the sleeve A line, `b` its sleeve B
# twin, `index_name` the index both track.
PAIRS = [
    {"key": "P1", "a": "CSP1", "b": "SPY", "index_name": "S&P 500"},
    {"key": "P2", "a": "CNDX", "b": "QQQ", "index_name": "NASDAQ-100"},
    {"key": "P3", "a": "IDP6", "b": "IJR", "index_name": "S&P SmallCap 600"},
]
BROAD_A = [p["a"] for p in PAIRS]          # the three sleeve A broad lines

# WS2's filed look-through figures, for the §8 comparison. mean / max share of
# NAV and the share of weeks BOTH sleeves hold. Source: the filed WS8 engine
# docstring, run_ws8_reit_overlap.py:19-21. IJR has no filed figure.
WS2_FILED_LOOKTHROUGH = {
    "P1": {"mean": 0.0398, "max": 0.1036, "share_weeks_both": 0.432},
    "P2": {"mean": 0.0679, "max": 0.2408, "share_weeks_both": 0.427},
    "P3": None,
}

# §5 defence 1, second half. Either flag set would price a staged change as
# part of a drop.
STAGING_FLAGS = ("BTE_APPLY_STAGED_ROSTER", "BTE_C_BTC_BASIS")


def assert_staging_flags_unset() -> dict:
    """Refuse to run under a staging flag. Fails closed."""
    seen = {f: os.environ.get(f) for f in STAGING_FLAGS}
    set_flags = {f: v for f, v in seen.items() if v not in (None, "", "0")}
    if set_flags:
        raise RuntimeError(
            f"staging flag(s) set: {set_flags}. WS22 arms must run on the "
            "deployed configuration only — see KICKOFF §5 defence 1.")
    return {f: (v if v is not None else "unset") for f, v in seen.items()}


def load_cached_panel(path: Path, needed: list[str]) -> pd.DataFrame:
    """Deployed price panel straight from its committed parquet cache.

    Deliberately NOT download_prices(): see the module docstring. Raises if
    the cache does not carry exactly the deployed universe, so a universe
    change cannot be silently evaluated against a stale panel.
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

    # Sleeve A breadth is computed PER ETF from that ETF's own constituents
    # (ws1_common.breadth_panel), so the full panel's column subset is
    # identical to a panel rebuilt on the reduced cons dict. The only
    # cross-sectional step is relative(), which is recomputed per arm. That
    # equivalence is asserted below before any arm uses the fast path.
    breadth_a_full = W.breadth_panel(cons_a, closes_a.index, MA)

    sig_a = W.relative(breadth_a_full)
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
    return {"equities": eqs, "weights": weights,
            "closes_a": closes_a, "cons_a": cons_a,
            "breadth_a_full": breadth_a_full, "closes_b": closes_b,
            "common_start": common_start, "common_end": common_end}


def assert_breadth_subset_equivalence(cons_a: dict, closes_a: pd.DataFrame,
                                      breadth_full: pd.DataFrame) -> None:
    """The fast path must reproduce the deployed code path exactly.

    N1 runs 165 sleeve A variants; rebuilding breadth from the constituent
    frames each time is wasted work ONLY IF the subset is identical. That is
    checked here on a real three-name drop, not assumed, because the whole
    null rests on it.
    """
    drop = BROAD_A
    cons = {k: v for k, v in cons_a.items() if k not in drop}
    slow = W.breadth_panel(cons, closes_a.drop(columns=drop).index, MA)
    fast = breadth_full.drop(columns=drop)
    pd.testing.assert_frame_equal(slow[fast.columns], fast,
                                  check_exact=True, check_names=True)
    print(f"breadth subset equivalence: exact on a {len(drop)}-name drop "
          f"({fast.shape[1]} columns x {fast.shape[0]} rows)")


def lookthrough(w_a: pd.DataFrame, w_b: pd.DataFrame,
                a_line: str, b_line: str) -> dict:
    """WS8's three statistics (run_ws8_reit_overlap.py:170-196), per pair.

    Effective NAV weight of each line is its within-sleeve weight times the
    sleeve's blend share (A 35%, B 35%), sampled on the weekly rebalance grid
    so "share of weeks" means what it says.
    """
    a = w_a[a_line].resample(W.REBAL).last().dropna()
    b = w_b[b_line].resample(W.REBAL).last().dropna()
    idx = a.index.intersection(b.index)
    a, b = a.loc[idx], b.loc[idx]
    eff_a, eff_b = 0.35 * a, 0.35 * b
    combined = eff_a + eff_b
    held_both = (a > 1e-9) & (b > 1e-9)
    return {
        "n_weeks": int(len(idx)),
        f"A_{a_line}": {"mean_lookthrough_w": round(float(eff_a.mean()), 4),
                        "max_lookthrough_w": round(float(eff_a.max()), 4),
                        "share_weeks_held": round(float((a > 1e-9).mean()), 3)},
        f"B_{b_line}": {"mean_lookthrough_w": round(float(eff_b.mean()), 4),
                        "max_lookthrough_w": round(float(eff_b.max()), 4),
                        "share_weeks_held": round(float((b > 1e-9).mean()), 3)},
        "combined": {"mean_lookthrough_w": round(float(combined.mean()), 4),
                     "max_lookthrough_w": round(float(combined.max()), 4),
                     "share_weeks_held_by_both_A_and_B":
                         round(float(held_both.mean()), 3)},
    }


class Ablation:
    """Runs registered arms against one rebuilt baseline."""

    def __init__(self, base: dict):
        self.base = base
        self.start = base["common_start"]
        self.end = base["common_end"]
        eqs, wts = base["equities"], base["weights"]
        idx = eqs.dropna().index
        self.idx = idx[(idx >= self.start) & (idx <= self.end)]
        self.rets = {s: eqs[s].reindex(self.idx).pct_change().fillna(0)
                     for s in "ABCD"}
        base_ret = self.blend_returns({})
        self.blend0 = W.full_report((1 + base_ret).cumprod(), None,
                                    self.idx[0], self.end)
        self.sleeve0 = {s: W.full_report(eqs[s].dropna(), wts[s],
                                         self.start, self.end) for s in "ABCD"}

    def blend_returns(self, overrides: dict[str, pd.Series]) -> pd.Series:
        r = dict(self.rets)
        r.update(overrides)
        return (0.35 * r["A"] + 0.35 * r["B"]
                + 0.10 * r["C"] + 0.20 * r["D"])

    # -- sleeve variant runners ------------------------------------------
    def sleeve_a(self, drop: list[str], cost_mult: int = 1) -> dict:
        cols = [c for c in self.base["closes_a"].columns if c not in drop]
        closes = self.base["closes_a"][cols]
        sig = W.relative(self.base["breadth_a_full"][cols])
        return run_portfolio(closes, sig, top_k_breadth_weight(W.K_A),
                             self.start, cost=W.COST_A * cost_mult,
                             rebalance_freq=W.REBAL)

    def sleeve_b(self, drop: list[str], cost_mult: int = 1) -> dict:
        closes = self.base["closes_b"].drop(columns=drop)
        sig = W.distance_signal(closes, MA)
        return B_engine.run_rotation(closes, sig,
                                     B_engine.top_k_by_signal(W.K_B),
                                     self.start, rebalance_freq=W.REBAL,
                                     cost=W.COST_B * cost_mult)

    # -- registered arm ---------------------------------------------------
    def arm(self, name: str, drop_a: list[str], drop_b: list[str]) -> dict:
        """One registered arm, judged mechanically on the §4 keep bar."""
        varied, rep_sleeves, overrides = [], {}, {}
        for sleeve, drop, runner in (("A", drop_a, self.sleeve_a),
                                     ("B", drop_b, self.sleeve_b)):
            if not drop:
                continue
            varied.append(sleeve)
            r1 = runner(drop, 1)
            r2 = runner(drop, 2)
            rep = W.full_report(r1["equity"].loc[:self.end],
                                r1["weights"].loc[:self.end],
                                self.start, self.end)
            rep["sharpe_2x_cost"] = W.window_stats(
                r2["equity"].loc[:self.end], self.start, self.end)["sharpe"]
            b0 = self.sleeve0[sleeve]
            rep["dropped"] = list(drop)
            rep["n_universe_after"] = int(
                r1["weights"].shape[1]) if r1["weights"] is not None else None
            rep["delta_vs_deployed_sleeve"] = {
                "full": round(rep["full"]["sharpe"] - b0["full"]["sharpe"], 4),
                "test": round(rep["test"]["sharpe"] - b0["test"]["sharpe"], 4),
                "consistency": W.consistency_count(rep["sub_period_sharpe"],
                                                   b0["sub_period_sharpe"]),
            }
            rep["survives_2x_cost"] = bool(
                rep["sharpe_2x_cost"] >= b0["full"]["sharpe"])
            rep_sleeves[sleeve] = rep
            overrides[sleeve] = (r1["equity"].reindex(self.idx)
                                 .pct_change().fillna(0))

        v_ret = self.blend_returns(overrides)
        rep_bl = W.full_report((1 + v_ret).cumprod(), None, self.idx[0],
                               self.end)
        blend_delta = {
            "full": round(rep_bl["full"]["sharpe"]
                          - self.blend0["full"]["sharpe"], 4),
            "test": round(rep_bl["test"]["sharpe"]
                          - self.blend0["test"]["sharpe"], 4),
            "consistency": W.consistency_count(rep_bl["sub_period_sharpe"],
                                               self.blend0["sub_period_sharpe"]),
        }
        # §4 keep bar, evaluated mechanically. Leg 3 is conjunctive across
        # every varied sleeve (clarification fixed in §4 before any result).
        keep_bar = {
            "blend_test_not_worse": bool(blend_delta["test"] >= 0),
            "blend_consistency_at_least_4_of_6":
                bool(blend_delta["consistency"] >= 4),
            "all_varied_sleeves_survive_2x_cost":
                bool(all(rep_sleeves[s]["survives_2x_cost"] for s in varied)),
        }
        out = {"varied_sleeves": varied, "sleeves": rep_sleeves,
               "blend_spliced": rep_bl, "blend_delta": blend_delta,
               "keep_bar": keep_bar,
               "keep_bar_passed": bool(all(keep_bar.values())),
               "verdict": ("DROP CLEARS BAR" if all(keep_bar.values())
                           else "KEEP INCUMBENT")}
        legs = "".join("Y" if v else "n" for v in keep_bar.values())
        print(f"  {name:22s} blend dFull {blend_delta['full']:+.4f} "
              f"dTest {blend_delta['test']:+.4f} "
              f"cons {blend_delta['consistency']}/6 legs {legs} "
              f"-> {out['verdict']}")
        return out

    # -- H2 null ----------------------------------------------------------
    def null_n1(self) -> dict:
        """Exhaustive C(11,3) three-name drops from sleeve A's OTHER eleven.

        Blend test-half delta only: that is the statistic V4 is gated on.
        """
        others = [c for c in self.base["closes_a"].columns
                  if c not in BROAD_A]
        combos = list(itertools.combinations(others, 3))
        print(f"  N1: {len(others)} other lines -> {len(combos)} exhaustive "
              "three-name drops")
        t0, deltas = time.time(), []
        for i, combo in enumerate(combos, 1):
            r1 = self.sleeve_a(list(combo), 1)
            ov = {"A": r1["equity"].reindex(self.idx).pct_change().fillna(0)}
            rep = W.full_report((1 + self.blend_returns(ov)).cumprod(), None,
                                self.idx[0], self.end)
            deltas.append({"drop": list(combo),
                           "blend_test_delta": round(
                               rep["test"]["sharpe"]
                               - self.blend0["test"]["sharpe"], 4)})
            if i % 25 == 0 or i == len(combos):
                print(f"    {i}/{len(combos)} ({time.time() - t0:.0f}s)")
        vals = np.array([d["blend_test_delta"] for d in deltas])
        return {
            "n_draws": len(combos), "others": others,
            "blend_test_delta": {
                "p10": round(float(np.percentile(vals, 10)), 4),
                "p50": round(float(np.percentile(vals, 50)), 4),
                "p90": round(float(np.percentile(vals, 90)), 4),
                "min": round(float(vals.min()), 4),
                "max": round(float(vals.max()), 4),
                "mean": round(float(vals.mean()), 4),
            },
            "draws": deltas,
        }


def coherence(arms: dict) -> dict:
    """§4 coherence requirement: an arm TYPE needs 2 of 3 pairs to pass.

    The three pairs are one mechanism repeated, so a lone pass is noise.
    Applied to V1/V2/V3 only; V4 has its own gate.
    """
    out = {}
    for vtype in ("V1", "V2", "V3"):
        passed = [p["key"] for p in PAIRS
                  if arms[f"{vtype}_{p['key']}"]["keep_bar_passed"]]
        out[vtype] = {
            "pairs_passing_keep_bar": passed,
            "n_passing": len(passed),
            "adoption_eligible": len(passed) >= 2,
            "reading": ("eligible — passes on at least 2 of 3 pairs"
                        if len(passed) >= 2 else
                        ("lone pass recorded NOT ADOPTED and read as noise"
                         if len(passed) == 1 else "no pair passes")),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="baseline + one arm + look-through only; skips N1")
    args = ap.parse_args()

    flags = assert_staging_flags_unset()
    print(f"staging flags: {flags}")

    base = build_today_baselines()
    assert_breadth_subset_equivalence(base["cons_a"], base["closes_a"],
                                      base["breadth_a_full"])
    ab = Ablation(base)

    drift = {}
    if WS2_META.exists():
        meta = json.loads(WS2_META.read_text(encoding="utf-8"))
        drift = {s: round(ab.sleeve0[s]["full"]["sharpe"]
                          - meta["sleeve_sharpe"][s], 4) for s in "ABCD"}
        drift["blend"] = round(ab.blend0["full"]["sharpe"]
                               - meta["blend_sharpe_w200"], 4)
    print(f"baseline blend Sharpe {ab.blend0['full']['sharpe']:+.4f} "
          f"(test {ab.blend0['test']['sharpe']:+.4f}); "
          f"drift vs cached WS2 meta: {drift}")

    # §8 look-through, disclosure only.
    lt = {}
    for p in PAIRS:
        lt[p["key"]] = {
            "index": p["index_name"], "a_line": p["a"], "b_line": p["b"],
            "measured": lookthrough(base["weights"]["A"],
                                    base["weights"]["B"], p["a"], p["b"]),
            "ws2_filed": WS2_FILED_LOOKTHROUGH[p["key"]],
            "note": ("first measurement — no filed WS2 figure exists for this "
                     "pair" if WS2_FILED_LOOKTHROUGH[p["key"]] is None
                     else "filed WS2 figures alongside, for drift"),
        }
        c = lt[p["key"]]["measured"]["combined"]
        print(f"  look-through {p['key']} {p['a']}/{p['b']:4s} "
              f"mean {c['mean_lookthrough_w']:.4f} "
              f"max {c['max_lookthrough_w']:.4f} "
              f"both {c['share_weeks_held_by_both_A_and_B']:.3f}")

    print("arms (§4):")
    arms = {}
    if args.smoke:
        arms["V2_P2"] = ab.arm("V2_P2 (A drops CNDX)", ["CNDX"], [])
    else:
        for p in PAIRS:
            arms[f"V1_{p['key']}"] = ab.arm(
                f"V1_{p['key']} (B drops {p['b']})", [], [p["b"]])
            arms[f"V2_{p['key']}"] = ab.arm(
                f"V2_{p['key']} (A drops {p['a']})", [p["a"]], [])
            arms[f"V3_{p['key']}"] = ab.arm(
                f"V3_{p['key']} (both drop)", [p["a"]], [p["b"]])
        arms["V4"] = ab.arm("V4 (A drops all three)", BROAD_A, [])

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "registration": {
            "document": "KICKOFF_ws22-cross-sleeve-broad-beta.md",
            "countersigned": "2026-09-19",
            "freeze_commit": FREEZE_COMMIT,
            "arms_registered": 10,
            "keep_bar": ("WS2 P1/P2 as WS8 implemented it: blend test-half "
                         "delta >= 0 AND blend consistency >= 4 of 6 AND "
                         "every varied sleeve survives 2x cost. Conjunctive, "
                         "kill-on-contact, incumbent wins ties."),
            "coherence_requirement": ("a per-pair arm type needs 2 of 3 pairs "
                                      "to pass; a lone pass is noise"),
            "h2_null_gate": ("V4 blend test-half delta must be >= p90 of the "
                             "exhaustive 165-draw count-matched null N1"),
        },
        "staging_flags_asserted_unset": flags,
        "window": {"start": str(ab.start.date()), "end": str(ab.end.date()),
                   "split": str(W.SPLIT_DATE.date()),
                   "rebalance": W.REBAL},
        "deployed_config": {"K_A": W.K_A, "K_B": W.K_B,
                            "cost_a_bps": W.COST_A * 10_000,
                            "cost_b_bps": W.COST_B * 10_000,
                            "blend": "A .35 / B .35 / C .10 / D .20"},
        "baseline_note": (
            "baselines rebuilt on TODAY's deployed configuration; the cached "
            "ws2_baseline_*.parquet are unusable here (sleeve B holds EEM "
            "pre-Phase-29; sleeve D predates Phase 30 and the EXH3->EXH4 "
            "correction) and the book has moved again since WS8"),
        "blend_baseline": ab.blend0,
        "sleeve_baselines": ab.sleeve0,
        "baseline_drift_vs_ws2_meta": drift,
        "lookthrough": lt,
        "arms": arms,
    }

    if not args.smoke:
        payload["coherence"] = coherence(arms)
        n1 = ab.null_n1()
        v4_delta = arms["V4"]["blend_delta"]["test"]
        p90 = n1["blend_test_delta"]["p90"]
        vals = np.array([d["blend_test_delta"] for d in n1["draws"]])
        payload["null_n1"] = n1
        payload["h2_gate"] = {
            "v4_blend_test_delta": v4_delta,
            "n1_p90": p90,
            "v4_percentile_in_n1": round(
                float((vals <= v4_delta).mean() * 100), 1),
            "passes_null_gate": bool(v4_delta >= p90),
            "keep_bar_passed": arms["V4"]["keep_bar_passed"],
            "adoption_eligible": bool(arms["V4"]["keep_bar_passed"]
                                      and v4_delta >= p90),
        }
        g = payload["h2_gate"]
        print(f"  H2 gate: V4 dTest {v4_delta:+.4f} vs N1 p90 {p90:+.4f} "
              f"(percentile {g['v4_percentile_in_n1']}) "
              f"-> {'ELIGIBLE' if g['adoption_eligible'] else 'NOT ADOPTED'}")

        eligible = [k for k, v in payload["coherence"].items()
                    if v["adoption_eligible"]]
        if g["adoption_eligible"]:
            eligible.append("V4")
        payload["verdict"] = ("KEEP BOTH — no arm is adoption-eligible"
                              if not eligible else
                              f"ADOPTION-ELIGIBLE: {', '.join(eligible)}")
        print(f"\nVERDICT: {payload['verdict']}")

    W.write_json(OUT, payload)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
