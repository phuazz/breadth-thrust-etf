"""WS1 follow-up — 2-D THRESHOLD surfaces for the deployed system.

Two threshold families have never had a surface built on the deployed
strategies (the README concedes the Phase 19 gate parameters were tuned
in-sample from a 12-variant sweep):

  1. Sleeve C: signal floor x sleeve-gate threshold
     floor in {0, 2.5%, 5%, 7.5%, 10%} x gate in {off, 20%, 30%, 40%, 50%}.
     Deployed = (5%, 30%). Engine, universe, K=5 equal-weight, costs all
     deployed; only the two thresholds move. Weighter is a parameterised
     replica of run_thematic_rotation.top_k_equal_weight (deployed logic
     verified line-for-line; the deployed one reads module globals).
  2. Phase 19 regime gate: off-threshold x on-threshold hysteresis
     off in {10..30%} x on in {40..60%} on the committed ungated blend
     (multi_strategy.json blend_35_35_10_20) with CSP1 50d ma_breadth,
     SHY fallback, 50% derisk, 5 bps per flip — exactly the deployed
     mechanics from run_risk_overlay.py, parameterised.

Purpose: flat-vs-peak diagnosis (parameter-robustness invariant), NOT a
re-tune. Deployed cells are reported in the context of their neighbourhood.

Three ways this could be silently wrong, and the defences:
  1. LOOK-AHEAD in the gate state machine — states applied with shift(1)
     (deployed convention); breadth ffilled onto the blend calendar only.
  2. DEGENERATE CELLS — a high floor/gate parks C in SHY: fine Sharpe, no
     strategy. avg_invested_share is reported per cell; cells below 60%
     invested are flagged degenerate.
  3. HYSTERESIS VALIDITY + COSTS — off >= on pairs skipped as invalid;
     every flip charged 5 bps and stressed at 2x (10 bps); C cells run at
     deployed 5 bps and 2x.

Output: data/ws1_threshold_surface.json
Run:    python scripts/run_ws1_threshold_surface.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

OUT = W.DATA / "ws1_threshold_surface.json"

C_FLOORS = [0.0, 0.025, 0.05, 0.075, 0.10]
C_GATES = [0.0, 0.20, 0.30, 0.40, 0.50]        # 0 = gate off
G_OFF = [0.10, 0.15, 0.20, 0.25, 0.30]
G_ON = [0.40, 0.45, 0.50, 0.55, 0.60]
DERISK = 0.50
SWITCH_BPS = 5


def c_weighter(K: int, floor: float, gate: float):
    """Parameterised replica of run_thematic_rotation.top_k_equal_weight
    (deployed logic at run_thematic_rotation.py:529-599; globals replaced
    by the floor/gate arguments, gate<=0 disables the sleeve gate)."""
    cash = C_engine.CASH_PROXY

    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        w = pd.Series(0.0, index=s_row.index)
        if gate > 0:
            univ = valid.drop(cash, errors="ignore")
            if len(univ) > 0 and float((univ > floor).mean()) < gate:
                if cash in w.index:
                    w[cash] = 1.0
                return w
        eligible = valid[valid > floor]
        if cash in eligible.index:
            eligible = eligible.drop(cash)
        if len(eligible) == 0:
            if cash in w.index:
                w[cash] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested = len(top) / K
        w.loc[top.index] = invested / len(top)
        if invested < 1.0 and cash in w.index:
            w[cash] = w.get(cash, 0.0) + (1.0 - invested)
        return w

    return f


def hysteresis_states(breadth: pd.Series, off: float, on: float) -> pd.Series:
    """Replica of run_risk_overlay._compute_states (walk-forward, starts
    RISK_ON, NaN holds state)."""
    states, state = [], 1.0
    for v in breadth.values:
        if pd.isna(v):
            states.append(state)
            continue
        if state == 1.0 and v < off:
            state = 0.0
        elif state == 0.0 and v > on:
            state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index, dtype=float)


def main() -> int:
    # ---------------- Part 1: Sleeve C floor x gate ----------------
    print("=== Sleeve C: floor x gate surface ===", flush=True)
    closes_c = W.load_sleeve_c()
    sig = W.distance_signal(closes_c, 200)
    cs = W.COMMON_START
    common_end = closes_c.index.max()
    c_cells = {}
    for fl in C_FLOORS:
        for gt in C_GATES:
            runs = [C_engine.run_rotation(closes_c, sig,
                                          c_weighter(W.K_C, fl, gt), cs,
                                          rebalance_freq=W.REBAL,
                                          cost=W.COST_C * m) for m in (1, 2)]
            eq1 = runs[0]["equity"].loc[:common_end]
            rep = W.full_report(eq1, runs[0]["weights"].loc[:common_end],
                                cs, common_end)
            rep["sharpe_2x_cost"] = W.window_stats(
                runs[1]["equity"].loc[:common_end], cs, common_end)["sharpe"]
            wts = runs[0]["weights"].loc[cs:common_end]
            cash_col = (wts[C_engine.CASH_PROXY]
                        if C_engine.CASH_PROXY in wts.columns else 0.0)
            invested = float((wts.sum(axis=1) - cash_col).clip(lower=0).mean())
            rep["avg_invested_share"] = W._safe(invested)
            rep["degenerate"] = bool(invested < 0.60)
            c_cells[f"floor={fl}|gate={gt}"] = rep
            print(f"  floor {fl * 100:4.1f}% gate {gt * 100:2.0f}%  "
                  f"Sharpe {rep['full']['sharpe']:+.2f} "
                  f"(tr {rep['train']['sharpe']:+.2f}/"
                  f"te {rep['test']['sharpe']:+.2f})  "
                  f"DD {rep['full']['max_dd'] * 100:.0f}%  "
                  f"inv {invested * 100:.0f}%"
                  f"{'  DEGENERATE' if rep['degenerate'] else ''}", flush=True)

    # ---------------- Part 2: Phase 19 gate off x on ----------------
    print("\n=== Phase 19 gate: off x on hysteresis surface ===", flush=True)
    ms = json.loads((W.DATA / "multi_strategy.json").read_text(
        encoding="utf-8"))
    blend = ms["strategies"]["blend_35_35_10_20"]
    blend_eq = pd.Series(blend["equity"],
                         index=pd.to_datetime(blend["dates"])).sort_index()
    csp1 = json.loads((W.DATA / "breadth_csp1.json").read_text(
        encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                        index=pd.to_datetime(csp1["series"]["dates"])).dropna()
    shy = pd.read_parquet(W.DATA / "asset_class_prices_cache.parquet")["SHY"]
    common = blend_eq.index
    breadth = breadth.reindex(common, method="ffill")
    shy_ret = shy.reindex(common, method="ffill").pct_change().fillna(0)
    blend_ret = blend_eq.pct_change().fillna(0)

    def gated(off, on, switch_bps):
        states = hysteresis_states(breadth, off, on)
        lag = states.shift(1).fillna(1.0)
        flips = lag.diff().fillna(0).abs()
        w_blend = lag + (1.0 - lag) * (1.0 - DERISK)
        w_shy = (1.0 - lag) * DERISK
        ret = (w_blend * blend_ret + w_shy * shy_ret
               - flips * (switch_bps / 10_000.0))
        return (1.0 + ret).cumprod(), int(flips.sum()), float((lag == 0).mean())

    g_cells = {}
    for off in G_OFF:
        for on in G_ON:
            if off >= on:
                continue
            eq1, n_sw, pct_off = gated(off, on, SWITCH_BPS)
            eq2, _, _ = gated(off, on, SWITCH_BPS * 2)
            rep = W.full_report(eq1, None, common[0], common[-1])
            rep["sharpe_2x_cost"] = W.window_stats(eq2, common[0],
                                                   common[-1])["sharpe"]
            rep["n_switches"] = n_sw
            rep["pct_days_risk_off"] = W._safe(pct_off * 100)
            g_cells[f"off={off}|on={on}"] = rep
            print(f"  off {off * 100:2.0f}% on {on * 100:2.0f}%  "
                  f"Sharpe {rep['full']['sharpe']:+.3f} "
                  f"(tr {rep['train']['sharpe']:+.2f}/"
                  f"te {rep['test']['sharpe']:+.2f})  "
                  f"DD {rep['full']['max_dd'] * 100:.1f}%  "
                  f"switches {n_sw}  off-days {pct_off * 100:.1f}%", flush=True)
    ungated_rep = W.full_report(blend_eq, None, common[0], common[-1])
    ungated_rep["sharpe_2x_cost"] = ungated_rep["full"]["sharpe"]
    g_cells["no_gate"] = ungated_rep
    print(f"  no gate          Sharpe {ungated_rep['full']['sharpe']:+.3f}  "
          f"DD {ungated_rep['full']['max_dd'] * 100:.1f}%")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Threshold surfaces on deployed mechanics: Sleeve C"
                        " floor x sleeve-gate, and Phase 19 off x on"
                        " hysteresis on the committed ungated blend."),
        "deployed": {"c_floor": 0.05, "c_gate": 0.30,
                     "gate_off": 0.20, "gate_on": 0.50},
        "c_floors": C_FLOORS, "c_gates": C_GATES,
        "gate_offs": G_OFF, "gate_ons": G_ON,
        "c_surface": c_cells,
        "phase19_surface": g_cells,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
