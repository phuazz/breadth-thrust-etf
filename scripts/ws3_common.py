"""Workstream 3 shared helpers — heavy robustness gate (review session 3).

Baseline = the POST-PHASE-29 architecture (EEM overlay-only, approved and
landed 2026-07-02): Sleeve B rotates 12 lines + SHY; EM exposure lives only
in the Phase 22 tilt. The frozen shortlist under test is S1 (drop Sleeve C's
+5% floor, keep the 30% gate) and S2 (slope gate on B); S3 (EEM
overlay-only) landed before this session and is recorded as closed.

Reuses the WS1/WS2 harness unchanged (fixed window, split date, deployed
costs, sub-period grid, report helpers, cached A/C/D baselines). Rebuilds
only what Phase 29 changed (Sleeve B) plus the two shortlist variants.

Three ways these backtests could be silently wrong, and the defences:
  1. LOOK-AHEAD — all sleeve maths goes through the deployed engines
     (prior-day signal row; weights.shift(1) * returns; turnover charged on
     weight change). Overlay reimplementations (Phase 22 tilt, Phase 19
     gate) shift their state series by one day before applying, mirroring
     run_risk_overlay.py:270 and the deployed hysteresis walk. The S2
     slope mask uses trailing MA differences only.
  2. WINDOW / BASELINE DRIFT — every curve is evaluated on the ONE fixed
     window (WS1's COMMON_START -> the WS2 cached common_end). The rebuilt
     Sleeve B must reproduce data/ws2_eem_coherence.json
     b_without_eem_sleeve (+1.0217 full) within 0.02; the composed ungated
     blend must reproduce the V3 cell (+1.2070) within 0.012; the composed
     gated+tilted track must reproduce the V1 gated cell (+1.2891) within
     0.012. A failed reproduction raises rather than reporting.
  3. COST / COMPOSITION REALISM — deployed per-sleeve one-way costs
     (A 2 / B 2 / C 5 / D 9 bps) inside the engines; tilt and gate flips
     charged 5 bps exactly as deployed; every rebuilt sleeve also run at
     2x cost. Blend composition is daily-fixed-weight on sleeve returns
     (the WS2 decision-number convention). Known approximation vs the
     committed weekly-snap-back blend: ~0.001-0.009 Sharpe, identical
     across all variants compared, so deltas are unaffected; absolute
     levels are cross-checked against the committed track as a diagnostic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))

import ws1_common as W  # noqa: E402
import ws2_common as W2  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402
from run_ws1_threshold_surface import c_weighter  # noqa: E402  (S1 weighter)

# ---------------------------------------------------------------------------
# Deployed overlay parameters (run_risk_overlay.py:100-127)
# ---------------------------------------------------------------------------
TILT_FAST, TILT_SLOW, TILT_W = 50, 200, 0.10
SWITCH_COST = 5 / 10_000
GATE_OFF, GATE_ON, DERISK = 0.20, 0.50, 0.50

# Regression targets (data/ws2_eem_coherence.json, fixed window)
REF_B_NO_EEM_SHARPE = 1.0217     # b_without_eem_sleeve.full.sharpe
REF_UNGATED_BLEND = 1.2070       # V3_neither.full.sharpe (B_no, no tilt)
REF_GATED_TILTED = 1.2891        # V1 gated_context (B_no + tilt + gate)
COMMITTED_LIVE_TRACK = 1.2956    # risk_overlay.json gated_eem_tilted (own window)

META_PATH = DATA / "ws3_baselines_meta.json"
EQ_PATH = DATA / "ws3_baseline_equities.parquet"
WT_B_PATH = DATA / "ws3_baseline_weights_B.parquet"
WT_S1C_PATH = DATA / "ws3_s1_weights_C.parquet"
WT_S2B_PATH = DATA / "ws3_s2_weights_B.parquet"

S1_FLOOR, S1_GATE = 0.0, 0.30    # shortlist S1: drop +5% floor, keep gate


# ---------------------------------------------------------------------------
# Overlay state machines (replicated from run_ws2_eem_coherence.py, which
# validated the gate against the committed track at +1.286 vs +1.287)
# ---------------------------------------------------------------------------

def tilt_signal(ratio: pd.Series, fast: int = TILT_FAST,
                slow: int = TILT_SLOW) -> pd.Series:
    f = ratio.rolling(fast, min_periods=fast).mean()
    s = ratio.rolling(slow, min_periods=slow).mean()
    return (f > s).astype(float)


def gate_states(breadth: pd.Series, off: float = GATE_OFF,
                on: float = GATE_ON) -> pd.Series:
    states, state = [], 1.0
    for v in breadth.values:
        if pd.notna(v):
            if state == 1.0 and v < off:
                state = 0.0
            elif state == 0.0 and v > on:
                state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index, dtype=float)


def tilted_blend_returns(rets: dict[str, pd.Series], eem_ret: pd.Series,
                         sig_lagged: pd.Series,
                         w: tuple[float, float, float, float] = (0.35, 0.35, 0.10, 0.20),
                         tilt_w: float = TILT_W) -> pd.Series:
    """Daily blend returns with the Phase 22 tilt funded from B.
    `sig_lagged` must already be shifted one day (look-ahead defence)."""
    wa, wb, wc, wd = w
    off = wa * rets["A"] + wb * rets["B"] + wc * rets["C"] + wd * rets["D"]
    on = (wa * rets["A"] + (wb - tilt_w) * rets["B"] + wc * rets["C"]
          + wd * rets["D"] + tilt_w * eem_ret)
    sw = sig_lagged.diff().fillna(0).abs() * SWITCH_COST
    return sig_lagged * on + (1.0 - sig_lagged) * off - sw


def gated_returns(blend_ret: pd.Series, shy_ret: pd.Series,
                  state_lagged: pd.Series,
                  derisk: float = DERISK) -> pd.Series:
    """Phase 19 gate applied to composed blend returns. `state_lagged`
    must already be shifted one day."""
    derisked = (1 - derisk) * blend_ret + derisk * shy_ret
    sw = state_lagged.diff().fillna(0).abs() * SWITCH_COST
    return state_lagged * blend_ret + (1 - state_lagged) * derisked - sw


def load_gate_breadth() -> pd.Series:
    """CSP1 50d ma_breadth — the deployed Phase 19 input
    (run_risk_overlay.py:308-311 reads breadth_csp1.json series.ma_breadth)."""
    bjson = json.loads((DATA / "breadth_csp1.json").read_text(encoding="utf-8"))
    s = bjson["series"]
    return pd.Series(s["ma_breadth"], index=pd.to_datetime(s["dates"]),
                     dtype=float)


def load_eem_spy() -> tuple[pd.Series, pd.Series]:
    """(EEM close, EEM/SPY ratio) from the deployed Phase 22 cache
    (em_regime_context.parquet); falls back to the WS2 panel."""
    cache = DATA / "em_regime_context.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if "EEM" in df.columns and "SPY" in df.columns:
            eem = df["EEM"].dropna()
            ratio = (df["EEM"] / df["SPY"]).dropna()
            return eem, ratio
    ws2 = W2.load_ws2_prices()
    return ws2["EEM"].dropna(), (ws2["EEM"] / ws2["SPY"]).dropna()


# ---------------------------------------------------------------------------
# Baselines: post-Phase-29 sleeves + shortlist variants, cached
# ---------------------------------------------------------------------------

def build_ws3_baselines(force: bool = False) -> dict:
    """Returns dict with:
      equities: DataFrame[A, B, C, D, C_S1, B_S2]  (fixed window, new arch)
      weights:  {"A","B","C","D","C_S1","B_S2": DataFrame}
      sharpe_2x: {"B","C_S1","B_S2": float}  (2x-cost sleeve stress)
      common_start/common_end, and lagged overlay states on the blend calendar.
    """
    ws2_base = W2.build_baselines()          # cached A/B_old/C/D + weights
    start, end = ws2_base["common_start"], ws2_base["common_end"]

    if not force and META_PATH.exists() and EQ_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        eq = pd.read_parquet(EQ_PATH)
        weights = {"A": ws2_base["weights"]["A"], "C": ws2_base["weights"]["C"],
                   "D": ws2_base["weights"]["D"],
                   "B": pd.read_parquet(WT_B_PATH),
                   "C_S1": pd.read_parquet(WT_S1C_PATH),
                   "B_S2": pd.read_parquet(WT_S2B_PATH)}
        return _finalise(eq, weights, meta["sharpe_2x"], start, end)

    print("Building WS3 baselines (post-Phase-29 architecture) ...", flush=True)
    closes_b = B_engine.download_prices().loc[:end]
    assert "EEM" not in B_engine.UNIVERSE, (
        "Phase 29 not landed in run_asset_class_rotation.py — WS3 baseline "
        "assumption broken")
    sig_b = W.distance_signal(closes_b, 200)
    runs_b = [B_engine.run_rotation(closes_b, sig_b,
                                    B_engine.top_k_by_signal(W.K_B), start,
                                    rebalance_freq=W.REBAL, cost=W.COST_B * m)
              for m in (1, 2)]
    b_sharpe = W.window_stats(runs_b[0]["equity"].loc[:end], start, end)["sharpe"]
    print(f"  B (12 lines + SHY): {b_sharpe:+.4f} vs WS2 ref "
          f"{REF_B_NO_EEM_SHARPE:+.4f}")
    assert abs(b_sharpe - REF_B_NO_EEM_SHARPE) < 0.02, "B regression FAILED"

    # S1 — C with floor 0, gate 0.30 (WS1 threshold-surface weighter)
    closes_c = W.load_sleeve_c().loc[:end]
    sig_c = W.distance_signal(closes_c, 200)
    runs_s1 = [C_engine.run_rotation(closes_c, sig_c,
                                     c_weighter(W.K_C, S1_FLOOR, S1_GATE),
                                     start, rebalance_freq=W.REBAL,
                                     cost=W.COST_C * m) for m in (1, 2)]
    s1_sharpe = W.window_stats(runs_s1[0]["equity"].loc[:end], start, end)["sharpe"]
    print(f"  C_S1 (floor 0, gate 30%): {s1_sharpe:+.4f} "
          f"(WS1 cell reference +0.78)")

    # S2 — B masked by rising 200d MA (run_ws1_vol_variants.py:196-203)
    ma_b = closes_b.rolling(200, min_periods=200).mean()
    slope_ok = ma_b.diff(21) > 0
    sig_b_s2 = sig_b.where(slope_ok)
    runs_s2 = [B_engine.run_rotation(closes_b, sig_b_s2,
                                     B_engine.top_k_by_signal(W.K_B), start,
                                     rebalance_freq=W.REBAL, cost=W.COST_B * m)
               for m in (1, 2)]
    s2_sharpe = W.window_stats(runs_s2[0]["equity"].loc[:end], start, end)["sharpe"]
    print(f"  B_S2 (slope gate, new arch): {s2_sharpe:+.4f}")

    eq = pd.DataFrame({
        "A": ws2_base["equities"]["A"],
        "B": runs_b[0]["equity"].loc[:end],
        "C": ws2_base["equities"]["C"],
        "D": ws2_base["equities"]["D"],
        "C_S1": runs_s1[0]["equity"].loc[:end],
        "B_S2": runs_s2[0]["equity"].loc[:end],
    })
    weights = {"A": ws2_base["weights"]["A"], "C": ws2_base["weights"]["C"],
               "D": ws2_base["weights"]["D"],
               "B": runs_b[0]["weights"].loc[:end],
               "C_S1": runs_s1[0]["weights"].loc[:end],
               "B_S2": runs_s2[0]["weights"].loc[:end]}
    sharpe_2x = {
        "B": W.window_stats(runs_b[1]["equity"].loc[:end], start, end)["sharpe"],
        "C_S1": W.window_stats(runs_s1[1]["equity"].loc[:end], start, end)["sharpe"],
        "B_S2": W.window_stats(runs_s2[1]["equity"].loc[:end], start, end)["sharpe"],
    }
    eq.to_parquet(EQ_PATH)
    weights["B"].to_parquet(WT_B_PATH)
    weights["C_S1"].to_parquet(WT_S1C_PATH)
    weights["B_S2"].to_parquet(WT_S2B_PATH)
    META_PATH.write_text(json.dumps({
        "common_start": str(start.date()), "common_end": str(end.date()),
        "sleeve_sharpe": {c: W.window_stats(eq[c].dropna(), start, end)["sharpe"]
                          for c in eq.columns},
        "sharpe_2x": sharpe_2x,
    }, indent=1), encoding="utf-8")
    print(f"  cached -> {EQ_PATH.name}")
    return _finalise(eq, weights, sharpe_2x, start, end)


def _finalise(eq: pd.DataFrame, weights: dict, sharpe_2x: dict,
              start: pd.Timestamp, end: pd.Timestamp) -> dict:
    idx = eq[["A", "B", "C", "D"]].dropna().index
    idx = idx[(idx >= start) & (idx <= end)]
    rets = {c: eq[c].reindex(idx).pct_change().fillna(0) for c in eq.columns}

    eem_close, ratio = load_eem_spy()
    sig = (tilt_signal(ratio).reindex(idx, method="ffill").fillna(0)
           .shift(1).fillna(0))
    eem_ret = eem_close.reindex(idx, method="ffill").pct_change().fillna(0)

    closes_b = B_engine.download_prices()
    shy_ret = (closes_b["SHY"].reindex(idx, method="ffill")
               .pct_change().fillna(0))

    breadth = load_gate_breadth()
    state = (gate_states(breadth).reindex(idx, method="ffill").fillna(1.0)
             .shift(1).fillna(1.0))

    # --- regression checks on the composed tracks -------------------------
    base_rets = {s: rets[s] for s in "ABCD"}
    ungated = (0.35 * rets["A"] + 0.35 * rets["B"] + 0.10 * rets["C"]
               + 0.20 * rets["D"])
    sh_ungated = W.window_stats((1 + ungated).cumprod(), idx[0], end)["sharpe"]
    tilted = tilted_blend_returns(base_rets, eem_ret, sig)
    final = gated_returns(tilted, shy_ret, state)
    sh_final = W.window_stats((1 + final).cumprod(), idx[0], end)["sharpe"]
    print(f"  composed ungated blend {sh_ungated:+.4f} (ref {REF_UNGATED_BLEND:+.4f}); "
          f"gated+tilted {sh_final:+.4f} (ref {REF_GATED_TILTED:+.4f}; "
          f"committed live {COMMITTED_LIVE_TRACK:+.4f}, own window)")
    assert abs(sh_ungated - REF_UNGATED_BLEND) < 0.012, "ungated regression FAILED"
    assert abs(sh_final - REF_GATED_TILTED) < 0.012, "gated+tilted regression FAILED"

    return {"equities": eq, "weights": weights, "sharpe_2x": sharpe_2x,
            "idx": idx, "rets": rets, "eem_ret": eem_ret, "shy_ret": shy_ret,
            "tilt_sig_lagged": sig, "gate_state_lagged": state,
            "final_track_returns": final, "ungated_returns": ungated,
            "tilted_returns": tilted,
            "common_start": start, "common_end": end}


if __name__ == "__main__":
    build_ws3_baselines(force="--force" in sys.argv)
