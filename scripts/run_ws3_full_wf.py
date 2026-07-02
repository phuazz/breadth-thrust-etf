"""WS3 Item 2 — FULL-SYSTEM walk-forward: annual expanding-window re-fit of
the WHOLE configuration vs the frozen deployed one.

WS1 already showed the single-parameter version (re-fitting the lookback)
LOSES to fixed 200d out of sample. This is the full-configuration version:
every knob the deployed system has is re-fittable each year-end, chosen by
FULL-SYSTEM (gated + tilted) train Sharpe on data available at the refit.

SEARCH SPACE (pre-registered; nothing added after results were seen):
  common horizon W        {200, 250, 275}          (WS1 verdict candidates)
  sleeve weights A/B/C/D  {(35,35,10,20) deployed, (40,30,10,20),
                           (30,40,10,20), (35,35,15,15), (35,35,5,25),
                           (25,25,25,25)}
  K_A, K_B                {5, 7, 9}                (deployed 7)
  K_C                     {3, 5, 7}                (deployed 5)
  K_D                     {2, 3, 4}                (deployed 3)
  C floor/gate            {(5%, 30%) deployed, (0%, 30%) = shortlist S1}
  Phase 19 gate pair      {(15,45), (20,50) deployed, (25,55), OFF}
  Phase 22 tilt windows   {(50,200) deployed, (100,200), (50,150), OFF}
  -> 46,656 candidate configurations per refit; refits at each year-end
  2021-2025; OOS window 2022-01 -> common_end (identical for every
  protocol; run_ws1_wf_horizon.py protocol inherited).

PROTOCOLS
  frozen_deployed  — deployed config, never re-fit
  frozen_S1        — deployed except C floor 0% (shortlist S1)
  frozen_S2        — deployed except B slope gate (shortlist S2; own curve)
  wf_full          — re-fit ALL of the above annually by train Sharpe
  wf_weights_only  — re-fit sleeve weights only (rest deployed)
  oracle_full      — best single config judged ON the OOS window
                     (hindsight upper bound, never deployable)

Switch costs at refits (conservative, ws1_wf precedent): a sleeve whose
(W, K, floor) changed is charged 100% one-way turnover (weight x per-sleeve
cost); weight-menu shifts are charged |dW| x per-sleeve cost; gate/tilt
THRESHOLD changes are free (they trade nothing on the refit day itself).

Three ways this could be silently wrong, and the defences:
  1. SELECTION LEAKAGE — each refit scores candidates on daily returns
     strictly up to the refit date (expanding slice); the OOS segment
     starts the next trading day; gate/tilt state arrays are lagged one
     day before composition, so no same-day information crosses.
  2. SPLICE BIAS — every protocol is evaluated on the IDENTICAL
     concatenated OOS calendar; segments are rebased at their own start
     and chained multiplicatively; refit switch costs are deducted at the
     segment start (run_ws1_wf_horizon.py:120-140 protocol).
  3. COMPOSITION APPROXIMATION — blends are composed daily-fixed-weight
     from sleeve curves (no weekly snap-back, no 5 bps blend-rebal cost),
     IDENTICALLY for every protocol including the oracle, so protocol
     deltas are unaffected. Regression: the frozen_deployed composition
     must reproduce the ws3_common final track exactly (same construction)
     — asserted at 1e-6 on full-window Sharpe (float op-ordering bound).

Output: data/ws3_full_wf.json, chart data/ws3_full_wf.png
Run:    python scripts/run_ws3_full_wf.py
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import ws3_common as W3  # noqa: E402

OUT = W.DATA / "ws3_full_wf.json"
PNG = W.DATA / "ws3_full_wf.png"

W_GRID = [200, 250, 275]
WEIGHTS_MENU = [(0.35, 0.35, 0.10, 0.20), (0.40, 0.30, 0.10, 0.20),
                (0.30, 0.40, 0.10, 0.20), (0.35, 0.35, 0.15, 0.15),
                (0.35, 0.35, 0.05, 0.25), (0.25, 0.25, 0.25, 0.25)]
KA_GRID, KB_GRID, KC_GRID, KD_GRID = [5, 7, 9], [5, 7, 9], [3, 5, 7], [2, 3, 4]
FG_GRID = [0.05, 0.0]                      # C floor (gate fixed 30%)
GATE_MENU = [(0.15, 0.45), (0.20, 0.50), (0.25, 0.55), None]
TILT_MENU = [(50, 200), (100, 200), (50, 150), None]
REFIT_YEARS = [2021, 2022, 2023, 2024, 2025]
SLEEVE_COST = {"A": W.COST_A, "B": W.COST_B, "C": W.COST_C, "D": W.COST_D}
DEPLOYED = {"w": 200, "weights": (0.35, 0.35, 0.10, 0.20), "ka": 7, "kb": 7,
            "kc": 5, "kd": 3, "floor": 0.05, "gate": (0.20, 0.50),
            "tilt": (50, 200)}


def ann_sharpe(x: np.ndarray) -> float:
    sd = x.std(ddof=1)
    return float(x.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0


def main() -> int:
    base = W3.build_ws3_baselines()
    idx, end = base["idx"], base["common_end"]
    T = len(idx)

    # ---- sleeve return arrays from the precomputed grid -----------------
    grids = {s: pd.read_parquet(W.DATA / f"ws3_grid_{s}.parquet")
             for s in "ABCD"}

    def ret_arr(series: pd.Series) -> np.ndarray:
        return series.reindex(idx).pct_change().fillna(0).values

    rA = {(w, k): ret_arr(grids["A"][f"W{w}_K{k}"])
          for w in W_GRID for k in KA_GRID}
    rB = {(w, k): ret_arr(grids["B"][f"W{w}_K{k}"])
          for w in W_GRID for k in KB_GRID}
    rC = {(w, k, f): ret_arr(grids["C"][f"W{w}_K{k}_F{f}"])
          for w in W_GRID for k in KC_GRID for f in FG_GRID}
    rD = {(w, k): ret_arr(grids["D"][f"W{w}_K{k}"])
          for w in W_GRID for k in KD_GRID}
    r_s2 = base["rets"]["B_S2"].values           # S2 B curve (W200 K7)
    shy = base["shy_ret"].values
    eem = base["eem_ret"].values

    # ---- overlay state arrays (lagged one day) ---------------------------
    breadth = W3.load_gate_breadth()
    gate_states = {}
    for pair in GATE_MENU:
        if pair is None:
            gate_states[pair] = np.ones(T)
        else:
            st = W3.gate_states(breadth, pair[0], pair[1])
            gate_states[pair] = (st.reindex(idx, method="ffill").fillna(1.0)
                                 .shift(1).fillna(1.0).values)
    _, ratio = W3.load_eem_spy()
    tilt_sigs = {}
    for opt in TILT_MENU:
        if opt is None:
            tilt_sigs[opt] = np.zeros(T)
        else:
            sg = W3.tilt_signal(ratio, opt[0], opt[1])
            tilt_sigs[opt] = (sg.reindex(idx, method="ffill").fillna(0)
                              .shift(1).fillna(0).values)
    gate_sw = {p: np.abs(np.diff(s, prepend=s[0])) * W3.SWITCH_COST
               for p, s in gate_states.items()}
    tilt_sw = {p: np.abs(np.diff(s, prepend=s[0])) * W3.SWITCH_COST
               for p, s in tilt_sigs.items()}

    # ---- candidate enumeration ------------------------------------------
    sleeve_combos = list(itertools.product(W_GRID, KA_GRID, KB_GRID,
                                           KC_GRID, KD_GRID, FG_GRID))
    print(f"{len(sleeve_combos)} sleeve combos x {len(WEIGHTS_MENU)} weight "
          f"sets x {len(GATE_MENU)}x{len(TILT_MENU)} overlays = "
          f"{len(sleeve_combos) * len(WEIGHTS_MENU) * 16} candidates/refit")

    # Blend + B-used matrices per weight set (composition before overlays)
    blend_mats, bused_mats = {}, {}
    for wt in WEIGHTS_MENU:
        wa, wb, wc, wd = wt
        M = np.empty((len(sleeve_combos), T))
        BU = np.empty((len(sleeve_combos), T))
        for i, (w, ka, kb, kc, kd, f) in enumerate(sleeve_combos):
            b = rB[(w, kb)]
            M[i] = (wa * rA[(w, ka)] + wb * b + wc * rC[(w, kc, f)]
                    + wd * rD[(w, kd)])
            BU[i] = b
        blend_mats[wt] = M
        bused_mats[wt] = BU

    def full_system_matrix(wt, gate_pair, tilt_opt) -> np.ndarray:
        """(n_combos, T) matrix of final-track daily returns."""
        M, BU = blend_mats[wt], bused_mats[wt]
        sig, ssw = tilt_sigs[tilt_opt], tilt_sw[tilt_opt]
        r_t = M + sig * W3.TILT_W * (eem - BU) - ssw
        st, gsw = gate_states[gate_pair], gate_sw[gate_pair]
        return r_t + (1 - st) * W3.DERISK * (shy - r_t) - gsw

    def candidate_returns(cfg: dict) -> np.ndarray:
        combo = (cfg["w"], cfg["ka"], cfg["kb"], cfg["kc"], cfg["kd"],
                 cfg["floor"])
        i = sleeve_combos.index(combo)
        return full_system_matrix(cfg["weights"], cfg["gate"],
                                  cfg["tilt"])[i]

    # regression: frozen deployed == ws3_common final track
    dep_r = candidate_returns(DEPLOYED)
    ref = ann_sharpe(base["final_track_returns"].values)
    got = ann_sharpe(dep_r)
    print(f"regression: composed frozen_deployed {got:+.6f} vs ws3_common "
          f"{ref:+.6f}")
    assert abs(got - ref) < 1e-6, "frozen-deployed composition drift"

    # ---- refit calendar ---------------------------------------------------
    refit_ends = [idx[idx.searchsorted(pd.Timestamp(f"{y}-12-31"),
                                       side="right") - 1]
                  for y in REFIT_YEARS]
    refit_pos = [int(idx.get_loc(t)) for t in refit_ends]
    seg_bounds = [(refit_pos[i] + 1,
                   refit_pos[i + 1] if i + 1 < len(refit_pos) else T - 1)
                  for i in range(len(refit_pos))]
    oos_start_pos = seg_bounds[0][0]
    print(f"OOS: {idx[oos_start_pos].date()} -> {idx[-1].date()} "
          f"({len(seg_bounds)} segments)")

    # ---- the search: best candidate per refit by train Sharpe -----------
    def search_best(train_end_pos: int, weight_sets, gate_menu, tilt_menu,
                    sleeve_subset=None) -> dict:
        best, best_sh = None, -1e18
        for wt in weight_sets:
            for gp in gate_menu:
                for tl in tilt_menu:
                    R = full_system_matrix(wt, gp, tl)[:, :train_end_pos + 1]
                    means = R.mean(axis=1)
                    stds = R.std(axis=1, ddof=1)
                    sh = np.where(stds > 0, means / stds * math.sqrt(252),
                                  0.0)
                    if sleeve_subset is not None:
                        mask = np.full(len(sleeve_combos), -np.inf)
                        mask[sleeve_subset] = 0.0
                        sh = sh + mask
                    j = int(np.argmax(sh))
                    if sh[j] > best_sh:
                        best_sh = float(sh[j])
                        w, ka, kb, kc, kd, f = sleeve_combos[j]
                        best = {"w": w, "ka": ka, "kb": kb, "kc": kc,
                                "kd": kd, "floor": f, "weights": wt,
                                "gate": gp, "tilt": tl,
                                "train_sharpe": best_sh}
        return best

    dep_combo_idx = [sleeve_combos.index((DEPLOYED["w"], DEPLOYED["ka"],
                                          DEPLOYED["kb"], DEPLOYED["kc"],
                                          DEPLOYED["kd"], DEPLOYED["floor"]))]

    def stitched(pick_fn) -> tuple[pd.Series, list[dict]]:
        pieces, log, prev = [], [], None
        level = 1.0
        for (s_pos, e_pos), t_pos in zip(seg_bounds, refit_pos):
            cfg = pick_fn(t_pos)
            r = candidate_returns(cfg)[s_pos:e_pos + 1]
            seg = pd.Series(r, index=idx[s_pos:e_pos + 1])
            if prev is not None:
                switch = 0.0
                for s, kk, cost in (("A", "ka", W.COST_A), ("B", "kb", W.COST_B),
                                    ("C", "kc", W.COST_C), ("D", "kd", W.COST_D)):
                    si = "ABCD".index(s)
                    changed = (cfg["w"] != prev["w"] or cfg[kk] != prev[kk]
                               or (s == "C" and cfg["floor"] != prev["floor"]))
                    if changed:
                        switch += cfg["weights"][si] * cost
                    switch += abs(cfg["weights"][si]
                                  - prev["weights"][si]) * cost
                seg.iloc[0] -= switch
            eq_seg = (1 + seg).cumprod() * level
            level = float(eq_seg.iloc[-1])
            pieces.append(eq_seg)
            log.append({"train_end": str(idx[t_pos].date()),
                        "picked": {k: (list(v) if isinstance(v, tuple)
                                       else v)
                                   for k, v in cfg.items()
                                   if k != "train_sharpe"},
                        "train_sharpe": round(cfg.get("train_sharpe", 0), 4)
                        if "train_sharpe" in cfg else None,
                        "test_sharpe": round(ann_sharpe(seg.values), 4)})
            prev = cfg
        return pd.concat(pieces), log

    protocols = {}
    frozen_cfgs = {
        "frozen_deployed": DEPLOYED,
        "frozen_S1": {**DEPLOYED, "floor": 0.0},
    }
    for name, cfg in frozen_cfgs.items():
        curve, log = stitched(lambda t_pos, c=cfg: dict(c))
        protocols[name] = {"curve": curve, "log": log}

    # frozen_S2: deployed blend with the slope-gated B curve
    wa, wb, wc, wd = DEPLOYED["weights"]
    r_blend_s2 = (wa * rA[(200, 7)] + wb * r_s2 + wc * rC[(200, 5, 0.05)]
                  + wd * rD[(200, 3)])
    sig, ssw = tilt_sigs[(50, 200)], tilt_sw[(50, 200)]
    r_t = r_blend_s2 + sig * W3.TILT_W * (eem - r_s2) - ssw
    st, gsw = gate_states[(0.20, 0.50)], gate_sw[(0.20, 0.50)]
    r_f_s2 = r_t + (1 - st) * W3.DERISK * (shy - r_t) - gsw
    s2_oos = pd.Series(r_f_s2[oos_start_pos:], index=idx[oos_start_pos:])
    protocols["frozen_S2"] = {
        "curve": (1 + s2_oos).cumprod(),
        "log": [{"note": "deployed config with slope-gated B (W200 K7)"}]}

    print("searching wf_full ...", flush=True)
    protocols["wf_full"] = dict(zip(
        ("curve", "log"),
        stitched(lambda t_pos: search_best(t_pos, WEIGHTS_MENU, GATE_MENU,
                                           TILT_MENU))))
    print("searching wf_weights_only ...", flush=True)
    protocols["wf_weights_only"] = dict(zip(
        ("curve", "log"),
        stitched(lambda t_pos: search_best(
            t_pos, WEIGHTS_MENU, [DEPLOYED["gate"]], [DEPLOYED["tilt"]],
            sleeve_subset=dep_combo_idx))))

    # oracle: best single config judged on the OOS window (hindsight)
    print("oracle ...", flush=True)
    best_o, best_sh = None, -1e18
    for wt in WEIGHTS_MENU:
        for gp in GATE_MENU:
            for tl in TILT_MENU:
                R = full_system_matrix(wt, gp, tl)[:, oos_start_pos:]
                means, stds = R.mean(axis=1), R.std(axis=1, ddof=1)
                sh = np.where(stds > 0, means / stds * math.sqrt(252), 0.0)
                j = int(np.argmax(sh))
                if sh[j] > best_sh:
                    best_sh = float(sh[j])
                    w, ka, kb, kc, kd, f = sleeve_combos[j]
                    best_o = {"w": w, "ka": ka, "kb": kb, "kc": kc, "kd": kd,
                              "floor": f, "weights": wt, "gate": gp,
                              "tilt": tl}
    oracle_r = candidate_returns(best_o)[oos_start_pos:]
    protocols["oracle_full"] = {
        "curve": pd.Series((1 + oracle_r).cumprod(),
                           index=idx[oos_start_pos:]),
        "log": [{"picked": {k: (list(v) if isinstance(v, tuple) else v)
                            for k, v in best_o.items()},
                 "note": "hindsight upper bound, not deployable"}]}

    # ---- report -----------------------------------------------------------
    results = {}
    for name, p in protocols.items():
        eq = p["curve"]
        stats = W.window_stats(eq, eq.index[0], eq.index[-1])
        results[name] = {"oos_sharpe": stats["sharpe"], "oos_stats": stats,
                         "segments": p["log"]}
        print(f"{name:18s} OOS Sharpe {stats['sharpe']:+.3f}  "
              f"maxDD {stats['max_dd'] * 100:.1f}%")

    # ---- chart ------------------------------------------------------------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11, 6))
    styles = {"frozen_deployed": ("#1351b4", 2.2, "-"),
              "frozen_S1": ("#0d9488", 1.4, "-"),
              "frozen_S2": ("#7c3aed", 1.2, "-"),
              "wf_full": ("#dc2626", 1.8, "-"),
              "wf_weights_only": ("#ca8a04", 1.2, "--"),
              "oracle_full": ("#6b7280", 1.2, ":")}
    for name, p in protocols.items():
        c, lw, ls = styles[name]
        sh = results[name]["oos_sharpe"]
        ax.plot(p["curve"].index, p["curve"].values, color=c, lw=lw, ls=ls,
                label=f"{name} ({sh:+.3f})")
    ax.set_title("WS3 full-system walk-forward — OOS 2022-01 onward "
                 "(annual expanding re-fit of every knob vs frozen deployed)")
    ax.set_ylabel("Growth of 1 (OOS)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(PNG, dpi=130)
    print(f"wrote {PNG.relative_to(ROOT)}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Full-system annual expanding walk-forward (weights,"
                        " per-sleeve K, common horizon, C floor, gate pair,"
                        " tilt windows) vs frozen deployed config; identical"
                        " OOS calendar; ws1_wf switch-cost protocol."),
        "search_space": {
            "w_grid": W_GRID, "weights_menu": [list(w) for w in WEIGHTS_MENU],
            "ka": KA_GRID, "kb": KB_GRID, "kc": KC_GRID, "kd": KD_GRID,
            "c_floor": FG_GRID, "gate_menu": [list(g) if g else None
                                              for g in GATE_MENU],
            "tilt_menu": [list(t) if t else None for t in TILT_MENU],
            "candidates_per_refit": (len(sleeve_combos) * len(WEIGHTS_MENU)
                                     * len(GATE_MENU) * len(TILT_MENU)),
        },
        "oos_start": str(idx[oos_start_pos].date()),
        "oos_end": str(idx[-1].date()),
        "protocols": {k: {kk: vv for kk, vv in v.items() if kk != "curve"}
                      | {"oos_sharpe": results[k]["oos_sharpe"],
                         "oos_stats": results[k]["oos_stats"]}
                      for k, v in protocols.items()},
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
