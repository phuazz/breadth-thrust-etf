"""WS3 Item 1 — deflated Sharpe (Bailey-Lopez de Prado DSR) and
Harvey-Liu-Zhu-style multiple-testing haircut for the deployed track and the
frozen shortlist variants (S1: drop Sleeve C floor; S2: slope gate on B).

DECISION BAR (pre-registered before any number below was computed):
a track/variant "survives the deflated haircut" if
  (i)  DSR >= 0.95 at N = 171 trials (the review's own register) with the
       MEASURED cross-trial Sharpe variance, AND
  (ii) the expected-maximum Sharpe under selection-only (annualised SR0)
       stays below the track's observed Sharpe at the LIBERAL nominal trial
       count (the high end of the pre-review phase estimate + register).
The break-even trial count N* (where DSR crosses 0.95) is reported so the
verdict is transparent to the N assumption.

Trial accounting:
- Register lower bound: ~171 configurations logged across WS1+WS2 (memo
  convention: each evaluated configuration once; stress reports of the same
  configuration do not count).
- Pre-review phases: the ~28 phases of sequential iteration predate the
  register. Per-phase configuration counts are ESTIMATES from the phase
  history, script grids on disk and documented sweeps (e.g. Phase 19's
  12-variant sweep, Phase 27's six-variant bake-off, the Phase 22 4x3 tilt
  grid). Conservative (low) and liberal (high) sums are both reported and
  both are flagged as estimates, not counts.
- Effective independent trials: register trials are variants of ONE
  strategy family on ONE window; their return streams are highly
  correlated. Measured mean pairwise correlation of representative variant
  tracks is reported, plus the Satterthwaite-style approximation
  N_eff = 1 + (N-1)(1-rho_bar) as corroborating context for the structural
  cluster count. DSR is reported across the FULL N grid so no single
  N assumption carries the verdict.

Three ways this analysis could be silently wrong, and the defences:
  1. TRIAL-SET BIAS — if only plausible candidates were harvested, the
     cross-trial variance V would be understated and the haircut too kind.
     Defence: the harvest includes the deliberately-bad configurations
     (fast lookbacks, the degenerate W=25 point, failed ensembles); V is
     also reported excluding the degenerate point and at a 2x stress; the
     register's sleeve-level-only trials enter N (raising the haircut) even
     though their blend-level Sharpes cannot be reconstructed for V.
  2. UNIT MIXING — annualised vs per-day Sharpe confusion silently breaks
     DSR. Defence: all DSR internals run in per-day units with one
     conversion point; an inline assert reconstructs the annualised Sharpe
     from the daily inputs and requires agreement to 1e-9.
  3. WINDOW MISMATCH — mixing full-window and OOS-window trial Sharpes
     would corrupt V. Defence: only fixed-window FULL Sharpes are
     harvested; ws1_wf_horizon protocol Sharpes (OOS window) and the
     country-sleeve long-window numbers are excluded and listed as such.

Output: data/ws3_deflated.json
Run:    python scripts/run_ws3_deflated.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sstats

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import ws3_common as W3  # noqa: E402

OUT = W.DATA / "ws3_deflated.json"
EULER_GAMMA = 0.5772156649015329

# Pre-review per-phase configuration estimates (low, high). Sources: README
# phase history, documented sweeps, and the experiment scripts on disk
# (run_phase4-8, run_tuning, run_variants, run_ma200_sweep, run_improvements,
# test_idea2-5, test_overlay_levers, test_phase22-24, run_subindustry_bakeoff,
# run_thematic_exit_*, run_topk_robustness, run_regime_gate). ESTIMATES.
PRE_REVIEW_PHASES = [
    ("Phases 1-3: composite signal + MA/L sweeps (legacy single-ETF)", 40, 80),
    ("Phase 4: 4-way blend introduction + weight variants", 8, 20),
    ("Phases 5-8: weighting, correlation, bootstrap, right-tail", 15, 30),
    ("Phases 9-17.1: thematic expansion, exits, FX, ideas 2-5", 40, 90),
    ("Phases 18-19.1: regime gate (12-variant sweep + fallback test)", 15, 25),
    ("Phases 20-21: relative breadth + long-only fix", 6, 15),
    ("Phase 22: EEM tilt (4x3 window grid + funding source + validation)", 15, 25),
    ("Phase 23: regional rotation test", 8, 15),
    ("Phase 24: B universe pruning (4 scripts)", 10, 20),
    ("Phases 25-26.3: universe adds + data integrity", 5, 10),
    ("Phase 27: thematic exit bake-off (6 exits x thresholds x K, WF)", 25, 60),
    ("Phase 28.x: misc pre-review iteration", 5, 15),
]
REGISTER_N = 171   # WS1 ~139 + WS2 32 (memo trial register)

# N sensitivity grid: structural-cluster low, corr-adjusted mid, register,
# conservative nominal, liberal nominal, stress ceiling.
N_GRID_LABELS = ["n_eff_low_30", "n_eff_mid_60", "register_171",
                 "nominal_low", "nominal_high", "ceiling_1000"]


def harvest_trials() -> tuple[list[dict], list[str]]:
    """Blend-level full-window trial Sharpes from the on-file WS1/WS2
    artefacts. Returns (trials, excluded_notes)."""
    trials, excluded = [], []

    d = json.loads((W.DATA / "ws1_ma_surface.json").read_text(encoding="utf-8"))
    for w_str, rep in d["surface"]["blend"].items():
        trials.append({"family": "ws1_lookback_blend", "label": f"W={w_str}",
                       "sharpe": rep["full"]["sharpe"],
                       "degenerate": w_str == "25"})

    d = json.loads((W.DATA / "ws1_vol_variants.json").read_text(encoding="utf-8"))
    for k, rep in d["blend"].items():
        trials.append({"family": "ws1_vol_variants_blend", "label": k,
                       "sharpe": rep["full"]["sharpe"], "degenerate": False})

    d = json.loads((W.DATA / "ws1_threshold_surface.json").read_text(encoding="utf-8"))
    for k, rep in d["phase19_surface"].items():
        trials.append({"family": "ws1_gate_threshold", "label": k,
                       "sharpe": rep["full"]["sharpe"], "degenerate": False})
    excluded.append("ws1_threshold_surface.c_surface (25 cells): sleeve-level"
                    " only — counted in N, no blend Sharpe to harvest")
    excluded.append("ws1_wf_horizon protocols (5): OOS-window Sharpes, not"
                    " full-window — excluded from V, counted in N")

    d = json.loads((W.DATA / "ws2_prune_tests.json").read_text(encoding="utf-8"))
    for k in ("P1_B_drop_VGK", "P2_C_drop_TAN_SKYY_PAVE"):
        blend = d[k].get("blend")
        if blend:
            trials.append({"family": "ws2_prunes_blend", "label": k,
                           "sharpe": blend["full"]["sharpe"],
                           "degenerate": False})

    d = json.loads((W.DATA / "ws2_commodity_fixed.json").read_text(encoding="utf-8"))
    for k in ("blend_B_widened", "blend_C_widened", "blend_both_widened"):
        trials.append({"family": "ws2_commodity_blend", "label": k,
                       "sharpe": d[k]["full"]["sharpe"], "degenerate": False})
    excluded.append("ws2_country_sleeve (7): sleeve/benchmark level, never"
                    " blended — counted in N")

    d = json.loads((W.DATA / "ws2_eem_coherence.json").read_text(encoding="utf-8"))
    for k in d:
        if k.startswith("V") and isinstance(d[k], dict) and "full" in d[k]:
            trials.append({"family": "ws2_eem_blend", "label": k,
                           "sharpe": d[k]["full"]["sharpe"],
                           "degenerate": False})
    return trials, excluded


def harvest_constructions() -> list[dict]:
    """Committed multi_strategy.json construction tracks — the DIVERSE
    family this project actually explored across its history (single
    sleeves, 2/3/4-way blends, meta-rotation). Used for the liberal V
    stress, NOT the primary V: some entries are benchmarks rather than
    candidates, and their inclusion widens V (more punitive)."""
    d = json.loads((W.DATA / "multi_strategy.json").read_text(encoding="utf-8"))
    out = []
    for k, v in d.get("strategies", {}).items():
        sh = v.get("sharpe") if isinstance(v, dict) else None
        if sh is not None:
            out.append({"family": "committed_constructions", "label": k,
                        "sharpe": float(sh), "degenerate": False})
    return out


def dsr(daily: pd.Series, n_trials: float, var_trials_daily: float) -> dict:
    """Bailey & Lopez de Prado deflated Sharpe ratio, per-day units."""
    r = daily.dropna().values
    T = len(r)
    sr_d = r.mean() / r.std(ddof=1)
    # unit-mixing defence: round-trip the annualised Sharpe
    assert abs(sr_d * math.sqrt(252)
               - (daily.mean() / daily.std() * math.sqrt(252))) < 1e-9
    g3 = float(sstats.skew(r))
    g4 = float(sstats.kurtosis(r, fisher=False))
    sr0_d = math.sqrt(max(var_trials_daily, 0.0)) * (
        (1 - EULER_GAMMA) * sstats.norm.ppf(1 - 1 / n_trials)
        + EULER_GAMMA * sstats.norm.ppf(1 - 1 / (n_trials * math.e)))
    denom = math.sqrt(max(1 - g3 * sr_d + (g4 - 1) / 4 * sr_d ** 2, 1e-12))
    z = (sr_d - sr0_d) * math.sqrt(T - 1) / denom
    return {"T": T, "sr_annual": sr_d * math.sqrt(252),
            "skew": g3, "kurtosis_raw": g4,
            "sr0_annual_expected_max": sr0_d * math.sqrt(252),
            "dsr": float(sstats.norm.cdf(z)), "z": z}


def dsr_breakeven_n(daily: pd.Series, var_trials_daily: float,
                    target: float = 0.95) -> float | None:
    """Largest N at which DSR still >= target (bisection; None if even N=2
    fails, inf-like 1e9 if the ceiling never brings DSR below target)."""
    lo, hi = 2.0, 1e9
    if dsr(daily, lo, var_trials_daily)["dsr"] < target:
        return None
    if dsr(daily, hi, var_trials_daily)["dsr"] >= target:
        return float("inf")
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        if dsr(daily, mid, var_trials_daily)["dsr"] >= target:
            lo = mid
        else:
            hi = mid
    return lo


def hlz_haircut(daily: pd.Series, n_trials: float) -> dict:
    """Multiple-testing haircut in the Harvey-Liu-Zhu style: convert the
    track's t-ratio to a p-value, adjust for N tests (Bonferroni and Sidak
    — the punitive end of the HLZ family; BHY would land between Sidak and
    unadjusted), convert back to a haircut Sharpe."""
    r = daily.dropna().values
    T = len(r)
    sr_d = r.mean() / r.std(ddof=1)
    t = sr_d * math.sqrt(T)
    p = 2 * (1 - sstats.norm.cdf(t))
    out = {"t_stat": t, "p_single": p}
    for name, p_adj in (("bonferroni", min(1.0, p * n_trials)),
                        ("sidak", 1 - (1 - p) ** n_trials)):
        if p_adj >= 1.0:
            hc_annual = 0.0
        else:
            hc_annual = (sstats.norm.ppf(1 - p_adj / 2) / math.sqrt(T)
                         * math.sqrt(252))
        out[name] = {"p_adj": p_adj, "haircut_sharpe_annual": hc_annual,
                     "haircut_pct": (1 - hc_annual / (sr_d * math.sqrt(252)))
                     * 100}
    return out


def main() -> int:
    base = W3.build_ws3_baselines()
    idx, end = base["idx"], base["common_end"]
    rets = base["rets"]

    # ---- tracks under audit -------------------------------------------
    def final_track(b_key: str, c_key: str) -> pd.Series:
        r = {"A": rets["A"], "B": rets[b_key], "C": rets[c_key],
             "D": rets["D"]}
        tilted = W3.tilted_blend_returns(r, base["eem_ret"],
                                         base["tilt_sig_lagged"])
        return W3.gated_returns(tilted, base["shy_ret"],
                                base["gate_state_lagged"])

    tracks = {
        "deployed_final_gated_tilted": base["final_track_returns"],
        "deployed_ungated_blend": base["ungated_returns"],
        "S1_final_drop_C_floor": final_track("B", "C_S1"),
        "S2_final_B_slope_gate": final_track("B_S2", "C"),
    }

    # ---- trial harvest + V --------------------------------------------
    trials, excluded = harvest_trials()
    sh_all = np.array([t["sharpe"] for t in trials], dtype=float)
    sh_ex = np.array([t["sharpe"] for t in trials if not t["degenerate"]],
                     dtype=float)
    v_ann_all = float(np.var(sh_all, ddof=1))
    v_ann_ex = float(np.var(sh_ex, ddof=1))
    constructions = harvest_constructions()
    sh_div = np.concatenate([sh_all,
                             [t["sharpe"] for t in constructions]])
    v_ann_div = float(np.var(sh_div, ddof=1))
    print(f"harvested {len(sh_all)} blend-level trials; "
          f"sd(Sharpe) {math.sqrt(v_ann_all):.3f} "
          f"(ex-degenerate {math.sqrt(v_ann_ex):.3f}; incl. "
          f"{len(constructions)} committed constructions "
          f"{math.sqrt(v_ann_div):.3f})")

    # ---- measured trial correlation (representative variant subset) ----
    grid_b = pd.read_parquet(W.DATA / "ws3_grid_B.parquet")
    grid_a = pd.read_parquet(W.DATA / "ws3_grid_A.parquet")
    grid_c = pd.read_parquet(W.DATA / "ws3_grid_C.parquet")
    grid_d = pd.read_parquet(W.DATA / "ws3_grid_D.parquet")

    def grid_blend(w: int) -> pd.Series:
        r = {"A": grid_a[f"W{w}_K7"].reindex(idx).pct_change().fillna(0),
             "B": grid_b[f"W{w}_K7"].reindex(idx).pct_change().fillna(0),
             "C": grid_c[f"W{w}_K5_F0.05"].reindex(idx).pct_change().fillna(0),
             "D": grid_d[f"W{w}_K3"].reindex(idx).pct_change().fillna(0)}
        return (0.35 * r["A"] + 0.35 * r["B"] + 0.10 * r["C"]
                + 0.20 * r["D"])

    variant_curves = {
        "blend_W200": grid_blend(200), "blend_W250": grid_blend(250),
        "blend_W275": grid_blend(275),
        "blend_S1": (0.35 * rets["A"] + 0.35 * rets["B"]
                     + 0.10 * rets["C_S1"] + 0.20 * rets["D"]),
        "blend_S2": (0.35 * rets["A"] + 0.35 * rets["B_S2"]
                     + 0.10 * rets["C"] + 0.20 * rets["D"]),
        "blend_eqw": (0.25 * rets["A"] + 0.25 * rets["B"]
                      + 0.25 * rets["C"] + 0.25 * rets["D"]),
    }
    cm = pd.DataFrame(variant_curves).corr()
    rho_bar = float(cm.values[np.triu_indices_from(cm.values, k=1)].mean())
    print(f"measured mean pairwise trial correlation (6 representative "
          f"variants): {rho_bar:.4f}")

    # ---- N accounting ---------------------------------------------------
    pre_low = sum(lo for _, lo, _ in PRE_REVIEW_PHASES)
    pre_high = sum(hi for _, _, hi in PRE_REVIEW_PHASES)
    nominal_low = pre_low + REGISTER_N
    nominal_high = pre_high + REGISTER_N
    n_eff_satterthwaite = {
        "at_register": 1 + (REGISTER_N - 1) * (1 - rho_bar),
        "at_nominal_high": 1 + (nominal_high - 1) * (1 - rho_bar),
    }
    n_grid = {"n_eff_low_30": 30, "n_eff_mid_60": 60,
              "register_171": REGISTER_N, "nominal_low": nominal_low,
              "nominal_high": nominal_high, "ceiling_1000": 1000}
    print(f"N grid: {n_grid}  (Satterthwaite N_eff at rho_bar: "
          f"{n_eff_satterthwaite['at_register']:.1f} / "
          f"{n_eff_satterthwaite['at_nominal_high']:.1f})")

    # ---- DSR + HLZ per track -------------------------------------------
    results = {}
    for name, ret in tracks.items():
        daily = ret.loc[(ret.index >= idx[0]) & (ret.index <= end)]
        entry = {"n_days": int(len(daily))}
        for v_name, v_ann in (("v_measured", v_ann_all),
                              ("v_ex_degenerate", v_ann_ex),
                              ("v_2x_stress", 2 * v_ann_all),
                              ("v_diverse_incl_constructions", v_ann_div),
                              ("v_sd_0.30_hypothetical", 0.30 ** 2),
                              ("v_sd_0.50_hypothetical", 0.50 ** 2)):
            v_d = v_ann / 252.0
            per_n = {}
            for label, n in n_grid.items():
                d = dsr(daily, n, v_d)
                per_n[label] = {"n": n, "dsr": round(d["dsr"], 4),
                                "sr0_annual": round(
                                    d["sr0_annual_expected_max"], 3)}
            be = dsr_breakeven_n(daily, v_d)
            entry[v_name] = {
                "sd_trials_annual": math.sqrt(v_ann),
                "per_n": per_n,
                "breakeven_n_dsr95": (None if be is None else
                                      (be if be != float("inf") else 1e18)),
            }
        d0 = dsr(daily, 2, v_ann_all / 252.0)
        entry["sr_annual"] = round(d0["sr_annual"], 4)
        entry["skew"] = round(d0["skew"], 3)
        entry["kurtosis_raw"] = round(d0["kurtosis_raw"], 2)
        entry["psr_vs_zero"] = round(float(sstats.norm.cdf(
            (daily.mean() / daily.std(ddof=1)) * math.sqrt(len(daily) - 1)
            / math.sqrt(max(1 - d0["skew"] * (daily.mean() / daily.std(ddof=1))
                            + (d0["kurtosis_raw"] - 1) / 4
                            * (daily.mean() / daily.std(ddof=1)) ** 2, 1e-12))
        )), 6)
        entry["hlz"] = {label: hlz_haircut(daily, n)
                        for label, n in n_grid.items()}
        results[name] = entry
        pn = entry["v_measured"]["per_n"]
        pd_ = entry["v_diverse_incl_constructions"]["per_n"]
        ph = entry["v_sd_0.30_hypothetical"]["per_n"]
        print(f"{name}: SR {entry['sr_annual']:+.3f} skew {entry['skew']:+.2f}"
              f" kurt {entry['kurtosis_raw']:.1f} | DSR@171 "
              f"{pn['register_171']['dsr']:.3f} (E[maxSR] "
              f"{pn['register_171']['sr0_annual']:+.2f}) | DSR@nominal_high "
              f"{pn['nominal_high']['dsr']:.3f} | diverse-V@high "
              f"{pd_['nominal_high']['dsr']:.3f} (E[maxSR] "
              f"{pd_['nominal_high']['sr0_annual']:+.2f}) | sd0.30@high "
              f"{ph['nominal_high']['dsr']:.3f}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Bailey-Lopez de Prado deflated Sharpe + "
                        "Bonferroni/Sidak haircuts across an N-assumption "
                        "grid, for the deployed post-Phase-29 track and the "
                        "frozen shortlist variants."),
        "decision_bar": ("survive = DSR>=0.95 at N=171 (measured V) AND "
                         "E[maxSR under selection] < observed SR at "
                         "nominal_high"),
        "window": {"start": str(idx[0].date()), "end": str(end.date())},
        "trial_register": {
            "register_n": REGISTER_N,
            "pre_review_phase_estimates": [
                {"phase": p, "low": lo, "high": hi}
                for p, lo, hi in PRE_REVIEW_PHASES],
            "pre_review_total_low": pre_low,
            "pre_review_total_high": pre_high,
            "nominal_low": nominal_low, "nominal_high": nominal_high,
            "note": "pre-review counts are estimates, not logs",
        },
        "harvest": {
            "n_trials_harvested": len(sh_all),
            "sd_sharpe_annual": math.sqrt(v_ann_all),
            "sd_sharpe_annual_ex_degenerate": math.sqrt(v_ann_ex),
            "sd_sharpe_annual_diverse_incl_constructions": math.sqrt(v_ann_div),
            "families": sorted({t["family"] for t in trials}),
            "excluded": excluded,
            "trials": trials,
            "committed_constructions": constructions,
        },
        "trial_correlation": {
            "rho_bar_measured": rho_bar,
            "subset": list(variant_curves.keys()),
            "satterthwaite_n_eff": n_eff_satterthwaite,
            "note": ("N_eff = 1+(N-1)(1-rho_bar) is an approximation used "
                     "as corroborating context only; the DSR verdict is "
                     "reported across the full N grid"),
        },
        "n_grid": n_grid,
        "tracks": results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
