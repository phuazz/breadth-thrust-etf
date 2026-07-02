"""WS2 Experiment 5 — EEM coherence: one role for EEM, decided at blend level.

EEM currently appears TWICE in the architecture: as a Sleeve B rotation
member AND as the Phase 22 overlay (EEM/SPY 50/200 golden cross tilting
10pp of NAV from B into EEM). The 2x2 ablation isolates each role on the
fixed window, ungated (the WS1 comparison object; the Phase 19 gate is
applied afterwards as deployed-context, not as the decision number):

  V0  B with EEM,   tilt ON   — status quo
  V1  B without EEM, tilt ON  — overlay-only role
  V2  B with EEM,   tilt OFF  — B-member-only role
  V3  B without EEM, tilt OFF — EEM nowhere (clean ablation corner)

The third prompt option — decompose EM into a country sleeve — was killed
by run_ws2_country_sleeve.py (pre-registered bar failed: 3/6 sub-periods,
train half negative, edge entirely 2022+), so no V4 is run.

Also quantifies the double-count under V0: the blend's look-through EEM
weight (B's EEM weight x B's blend share, plus the 10pp tilt when ON) and
how often both roles hold EEM simultaneously.

Three ways this backtest could be silently wrong, and the defences:
  1. LOOK-AHEAD — the tilt state and the Phase 19 gate state are both
     shifted one day before applying (mirrors run_risk_overlay.py:270 and
     the deployed hysteresis walk); the B re-run uses the deployed engine.
  2. WINDOW / ALIGNMENT — all four variants are built from sleeve equity
     curves on the IDENTICAL fixed calendar; the EEM/SPY ratio and the
     CSP1 50d breadth are reindex-ffilled onto that calendar (deployed
     alignment), never the other way round. The gate reimplementation is
     VALIDATED by applying it to the ungated deployed blend and requiring
     the committed gated Sharpe (+1.287 at the WS1 window) within 0.05.
  3. COST REALISM — B re-run keeps the deployed 2 bps; tilt flips and
     gate flips are charged 5 bps each exactly as deployed; no layer is
     double-charged. 2x sleeve-cost stress reported for the B-without-EEM
     re-run (the only re-run sleeve).

Output: data/ws2_eem_coherence.json
Run:    python scripts/run_ws2_eem_coherence.py
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
import ws2_common as W2  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402

OUT = W2.DATA / "ws2_eem_coherence.json"

TILT_FAST, TILT_SLOW, TILT_W = 50, 200, 0.10   # run_risk_overlay.py:123-125
SWITCH_COST = 5 / 10_000                       # :103
GATE_OFF, GATE_ON, DERISK = 0.20, 0.50, 0.50   # :100-102
COMMITTED_GATED_SHARPE = 1.287                 # ws1_threshold_surface deployed cell


def tilt_signal(ratio: pd.Series) -> pd.Series:
    fast = ratio.rolling(TILT_FAST, min_periods=TILT_FAST).mean()
    slow = ratio.rolling(TILT_SLOW, min_periods=TILT_SLOW).mean()
    return (fast > slow).astype(float)


def gate_states(breadth: pd.Series) -> pd.Series:
    states, state = [], 1.0
    for v in breadth.values:
        if pd.notna(v):
            if state == 1.0 and v < GATE_OFF:
                state = 0.0
            elif state == 0.0 and v > GATE_ON:
                state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index, dtype=float)


def blend_returns(rets: dict[str, pd.Series], b_key: str) -> pd.Series:
    return (0.35 * rets["A"] + 0.35 * rets[b_key]
            + 0.10 * rets["C"] + 0.20 * rets["D"])


def tilted_returns(rets: dict[str, pd.Series], b_key: str,
                   eem_ret: pd.Series, sig: pd.Series) -> pd.Series:
    off = blend_returns(rets, b_key)
    on = (0.35 * rets["A"] + (0.35 - TILT_W) * rets[b_key]
          + 0.10 * rets["C"] + 0.20 * rets["D"] + TILT_W * eem_ret)
    sw = sig.diff().fillna(0).abs() * SWITCH_COST
    return sig * on + (1.0 - sig) * off - sw


def gated_returns(blend_ret: pd.Series, shy_ret: pd.Series,
                  state: pd.Series) -> pd.Series:
    derisked = (1 - DERISK) * blend_ret + DERISK * shy_ret
    sw = state.diff().fillna(0).abs() * SWITCH_COST
    return state * blend_ret + (1 - state) * derisked - sw


def main() -> int:
    base = W2.build_baselines()
    start, end = base["common_start"], base["common_end"]
    eqs = base["equities"]
    idx = eqs.dropna().index
    idx = idx[(idx >= start) & (idx <= end)]

    # --- B without EEM (deployed engine, deployed cost + 2x) ---
    closes_b = B_engine.download_prices().loc[:end]
    closes_b_no = closes_b.drop(columns=["EEM"])
    sig_no = W.distance_signal(closes_b_no, 200)
    r_no = B_engine.run_rotation(closes_b_no, sig_no,
                                 B_engine.top_k_by_signal(W.K_B), start,
                                 rebalance_freq=W.REBAL, cost=W.COST_B)
    r_no2 = B_engine.run_rotation(closes_b_no, sig_no,
                                  B_engine.top_k_by_signal(W.K_B), start,
                                  rebalance_freq=W.REBAL, cost=W.COST_B * 2)
    rep_b_no = W.full_report(r_no["equity"].loc[:end],
                             r_no["weights"].loc[:end], start, end)
    rep_b_no["sharpe_2x_cost"] = W.window_stats(r_no2["equity"].loc[:end],
                                                start, end)["sharpe"]
    print(f"B without EEM: Sharpe {rep_b_no['full']['sharpe']:+.2f} "
          f"(deployed B {json.loads(W2.META_PATH.read_text())['sleeve_sharpe']['B']:+.2f})")

    rets = {s: eqs[s].reindex(idx).pct_change().fillna(0) for s in "ABCD"}
    rets["B_no"] = r_no["equity"].reindex(idx).pct_change().fillna(0)

    # --- tilt inputs (fresh WS2 panel covers the window end) ---
    ws2 = W2.load_ws2_prices()
    ratio = (ws2["EEM"] / ws2["SPY"]).dropna()
    sig = tilt_signal(ratio).reindex(idx, method="ffill").fillna(0).shift(1).fillna(0)
    eem_ret = ws2["EEM"].reindex(idx, method="ffill").pct_change().fillna(0)
    shy_ret = closes_b["SHY"].reindex(idx, method="ffill").pct_change().fillna(0)

    variants = {
        "V0_status_quo_EEM_in_B_plus_tilt": tilted_returns(rets, "B", eem_ret, sig),
        "V1_overlay_only_B_without_EEM": tilted_returns(rets, "B_no", eem_ret, sig),
        "V2_B_member_only_no_tilt": blend_returns(rets, "B"),
        "V3_neither": blend_returns(rets, "B_no"),
    }
    results = {}
    for name, ret in variants.items():
        results[name] = W.full_report((1 + ret).cumprod(), None, idx[0], end)

    # --- Phase 19 gate context (validated against the committed track) ---
    bjson = json.loads((W2.DATA / "breadth_csp1.json").read_text(
        encoding="utf-8"))
    series = bjson["series"]
    breadth = pd.Series(series["ma_breadth"],
                        index=pd.to_datetime(series["dates"]), dtype=float)
    state = gate_states(breadth).reindex(idx, method="ffill").fillna(1.0)
    state = state.shift(1).fillna(1.0)
    gated_v2 = gated_returns(variants["V2_B_member_only_no_tilt"], shy_ret, state)
    val_sharpe = W.window_stats((1 + gated_v2).cumprod(), idx[0], end)["sharpe"]
    print(f"gate validation: reimplemented gated ungated-blend Sharpe "
          f"{val_sharpe:+.3f} vs committed {COMMITTED_GATED_SHARPE:+.3f}")
    assert abs(val_sharpe - COMMITTED_GATED_SHARPE) < 0.05, "gate mismatch"
    for name in list(variants):
        g = gated_returns(variants[name], shy_ret, state)
        results[name]["gated_context"] = W.window_stats(
            (1 + g).cumprod(), idx[0], end)

    # --- consistency vs V0 + double-count quantification ---
    v0_sub = results["V0_status_quo_EEM_in_B_plus_tilt"]["sub_period_sharpe"]
    for name in list(variants):
        results[name]["consistency_vs_V0"] = W.consistency_count(
            results[name]["sub_period_sharpe"], v0_sub)

    wB = base["weights"]["B"].reindex(idx).fillna(0.0)
    b_eem_w = wB["EEM"] if "EEM" in wB.columns else pd.Series(0.0, index=idx)
    b_share = 0.35 - TILT_W * sig            # B's blend share under the tilt
    lookthrough = b_eem_w * b_share + TILT_W * sig
    both = ((b_eem_w > 1e-6) & (sig > 0)).mean()
    dc = {
        "mean_lookthrough_eem_w": round(float(lookthrough.mean()), 4),
        "max_lookthrough_eem_w": round(float(lookthrough.max()), 4),
        "share_days_tilt_on": round(float(sig.mean()), 3),
        "share_days_b_holds_eem": round(float((b_eem_w > 1e-6).mean()), 3),
        "share_days_both_hold_eem": round(float(both), 3),
        "n_tilt_switches": int(sig.diff().fillna(0).abs().sum()),
    }
    results["double_count_quantification"] = dc
    print(f"double-count: mean look-through EEM {dc['mean_lookthrough_eem_w']*100:.1f}%, "
          f"max {dc['max_lookthrough_eem_w']*100:.1f}%, both-hold days "
          f"{dc['share_days_both_hold_eem']*100:.0f}%, switches {dc['n_tilt_switches']}")

    for name in variants:
        r = results[name]
        print(f"{name}: full {r['full']['sharpe']:+.3f} "
              f"train {r['train']['sharpe']:+.3f} "
              f"test {r['test']['sharpe']:+.3f} "
              f"DD {r['full']['max_dd']*100:.1f}% "
              f"cons_vs_V0 {r['consistency_vs_V0']}/6 "
              f"gated {r['gated_context']['sharpe']:+.3f}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "split": str(W.SPLIT_DATE.date())},
        "note": ("2x2 ablation of EEM roles, ungated decision numbers with "
                 "Phase 19 gate applied as context; V4 (country-sleeve "
                 "decomposition) not run — killed by "
                 "run_ws2_country_sleeve.py"),
        "b_without_eem_sleeve": rep_b_no,
        **results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
