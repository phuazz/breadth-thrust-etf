"""Workstream 2 shared helpers — universe experiments (review session 2).

Reuses the WS1 harness (scripts/ws1_common.py) unchanged: fixed window,
split date, deployed per-sleeve costs, sub-period grid and report helpers.
Adds a cached builder for the deployed-formulation baseline sleeves so the
WS2 scripts do not recompute constituent breadth repeatedly, plus loaders
for the WS2 candidate price panel.

Three ways the WS2 backtests could be silently wrong, and the defences:
  1. LOOK-AHEAD — all portfolio maths goes through the deployed engines
     (run_portfolio.run_portfolio / run_*_rotation.run_rotation), which
     rebalance on the PRIOR trading day's signal row and apply
     weights.shift(1) * returns. Overlay reimplementations (Phase 22 tilt,
     Phase 19 gate) shift their state series by one day before applying,
     mirroring run_risk_overlay.py:270 and :279.
  2. WINDOW / UNIVERSE INCONSISTENCY — every variant is evaluated on the
     ONE fixed window (COMMON_START -> common_end, computed exactly as
     run_ws1_ma_surface.py does and asserted against the cached meta).
     Variant panels are REINDEXED to the baseline calendar, never
     inner-joined, so a late-inception name or a holiday NaN cannot
     silently shorten or reshape the evaluation window.
  3. COST / FX MIS-MODELLING — deployed per-sleeve one-way costs
     (A 2 / B 2 / C 5 / D 9 bps) plus per-ticker overrides for the less
     liquid additions (country lines 5 bps, FM 15 bps, commodity sector
     funds 10 bps), each variant also run at 2x cost; every series is USD
     total return (yfinance adjusted close carries dividends; Sleeve D is
     EUR->USD via the WS1 FX cache; Sleeve C uses the deployed loader
     with CNY->USD FX and expense drags).

Baseline regression check: the rebuilt ungated 35/35/10/20 blend at W=200
must land within 0.03 Sharpe of the WS1 harness result (+1.196) on the
identical window, or the loader raises.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))

import ws1_common as W  # noqa: E402
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

DEPLOYED_W = 200
WS1_BLEND_SHARPE = 1.196          # run_ws1_ma_surface.py result at W=200
REGRESSION_TOL = 0.03

META_PATH = DATA / "ws2_baselines_meta.json"
EQ_PATH = DATA / "ws2_baseline_equities.parquet"
WT_PATHS = {s: DATA / f"ws2_baseline_weights_{s}.parquet" for s in "ABCD"}

# WS2 candidate panel (run_ws2_fetch_panel.py writes this)
WS2_PRICES = DATA / "ws2_prices_cache.parquet"

# Per-ticker one-way cost assumptions for NEW lines (bps). Stated, not
# tuned: 5 bps for the large single-country iShares lines (typical quoted
# spread 2-5 bps plus slippage), 15 bps for FM (frontier basket, thin
# book), consistent with the deployed B=2 / C=5 / D=9 ladder.
COUNTRY_COST_BPS = 5.0
FM_COST_BPS = 15.0


def build_baselines(force: bool = False) -> dict:
    """Deployed-formulation sleeves at W=200 on the fixed window.

    Returns {"equities": DataFrame[A,B,C,D], "weights": {sleeve: DataFrame},
    "common_start", "common_end", "blend": Series}. Cached to parquet; the
    cache is invalidated only by force=True (single-session artefact).
    """
    if not force and META_PATH.exists() and EQ_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        eq = pd.read_parquet(EQ_PATH)
        weights = {s: pd.read_parquet(p) for s, p in WT_PATHS.items()}
        blend = W.blend_equity(eq["A"].dropna(), eq["B"].dropna(),
                               eq["C"].dropna(), eq["D"].dropna(),
                               pd.Timestamp(meta["common_start"]),
                               pd.Timestamp(meta["common_end"]))
        return {"equities": eq, "weights": weights, "blend": blend,
                "common_start": pd.Timestamp(meta["common_start"]),
                "common_end": pd.Timestamp(meta["common_end"])}

    print("Building deployed baselines at W=200 (A/B/C/D) ...", flush=True)
    closes_a, cons_a = W.load_sleeve_a()
    closes_d, cons_d = W.load_sleeve_d()
    closes_b = W.load_sleeve_b()
    closes_c = W.load_sleeve_c()

    d_cons_end = min(cp.index.max() for cp in cons_d.values())
    a_cons_end = min(cp.index.max() for cp in cons_a.values())
    common_end = min(closes_b.index.max(), closes_c.index.max(),
                     a_cons_end, d_cons_end)
    common_start = W.COMMON_START
    print(f"  fixed window {common_start.date()} -> {common_end.date()}")

    sig_a = W.relative(W.breadth_panel(cons_a, closes_a.index, DEPLOYED_W))
    run_a = run_portfolio(closes_a, sig_a, top_k_breadth_weight(W.K_A),
                          common_start, cost=W.COST_A, rebalance_freq=W.REBAL)
    bp_d = W.breadth_panel(cons_d, closes_d.index, DEPLOYED_W)
    run_d = run_portfolio(closes_d, bp_d, top_k_breadth_weight(W.K_D),
                          common_start, cost=W.COST_D, rebalance_freq=W.REBAL)
    sig_b = W.distance_signal(closes_b, DEPLOYED_W)
    run_b = B_engine.run_rotation(closes_b, sig_b,
                                  B_engine.top_k_by_signal(W.K_B),
                                  common_start, rebalance_freq=W.REBAL,
                                  cost=W.COST_B)
    sig_c = W.distance_signal(closes_c, DEPLOYED_W)
    run_c = C_engine.run_rotation(closes_c, sig_c,
                                  C_engine.top_k_equal_weight(W.K_C),
                                  common_start, rebalance_freq=W.REBAL,
                                  cost=W.COST_C)

    eqs = {"A": run_a["equity"].loc[:common_end],
           "B": run_b["equity"].loc[:common_end],
           "C": run_c["equity"].loc[:common_end],
           "D": run_d["equity"].loc[:common_end]}
    weights = {"A": run_a["weights"].loc[:common_end],
               "B": run_b["weights"].loc[:common_end],
               "C": run_c["weights"].loc[:common_end],
               "D": run_d["weights"].loc[:common_end]}
    blend = W.blend_equity(eqs["A"], eqs["B"], eqs["C"], eqs["D"],
                           common_start, common_end)
    blend_sharpe = W.window_stats(blend, common_start, common_end)["sharpe"]
    print(f"  blend Sharpe {blend_sharpe:+.3f} "
          f"(WS1 reference {WS1_BLEND_SHARPE:+.3f})")
    assert abs(blend_sharpe - WS1_BLEND_SHARPE) < REGRESSION_TOL, (
        f"baseline regression FAILED: {blend_sharpe:+.3f} vs "
        f"{WS1_BLEND_SHARPE:+.3f}")

    eq_df = pd.DataFrame(eqs)
    eq_df.to_parquet(EQ_PATH)
    for s, p in WT_PATHS.items():
        weights[s].to_parquet(p)
    META_PATH.write_text(json.dumps({
        "common_start": str(common_start.date()),
        "common_end": str(common_end.date()),
        "blend_sharpe_w200": round(float(blend_sharpe), 4),
        "sleeve_sharpe": {s: W.window_stats(eqs[s], common_start,
                                            common_end)["sharpe"]
                          for s in "ABCD"},
    }, indent=1), encoding="utf-8")
    print(f"  cached -> {EQ_PATH.name} / ws2_baseline_weights_*.parquet")
    return {"equities": eq_df, "weights": weights, "blend": blend,
            "common_start": common_start, "common_end": common_end}


def load_ws2_prices() -> pd.DataFrame:
    """Candidate panel written by run_ws2_fetch_panel.py."""
    if not WS2_PRICES.exists():
        raise FileNotFoundError(
            "run scripts/run_ws2_fetch_panel.py first (writes "
            "data/ws2_prices_cache.parquet)")
    df = pd.read_parquet(WS2_PRICES)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def country_cost_series(cols) -> pd.Series:
    """One-way cost per ticker for country-sleeve experiments."""
    cv = pd.Series(COUNTRY_COST_BPS / 1e4, index=list(cols), dtype=float)
    if "FM" in cv.index:
        cv["FM"] = FM_COST_BPS / 1e4
    if "SHY" in cv.index:
        cv["SHY"] = 2.0 / 1e4   # cash proxy, same as Sleeve B's base cost
    return cv


if __name__ == "__main__":
    build_baselines(force="--force" in sys.argv)
