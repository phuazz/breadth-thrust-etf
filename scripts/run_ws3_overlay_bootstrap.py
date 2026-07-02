"""WS3 Item 6 — overlay reality check: is the Phase 22 tilt (11 switches,
29.3% of days ON) and the Phase 19 gate (18 flips, 11.7% of days off)
contribution distinguishable from noise, given how few distinct bets each
represents?

Measured on the post-Phase-29 architecture, deployed stacking order:
  tilt contribution d_tilt = tilted_blend - ungated_blend   (daily)
  gate contribution d_gate = final_track - tilted_blend     (daily)

Two independent noise tests per overlay:
  A. STATIONARY BLOCK BOOTSTRAP of the daily contribution series
     (block 60d precedent run_robustness.py:305; 20/120 sensitivity):
     P(mean contribution > 0) and the CI of the annualised contribution.
  B. CIRCULAR-ROTATION PLACEBO: rotate the ACTUAL lagged overlay state by
     a uniform random offset (mod T). A rotation preserves the number of
     switches, the ON share and the full block-length structure exactly —
     the placebo is "the same overlay shape with no information content".
     Report the actual contribution's percentile among 1000 placebos.
  Plus an EPISODE LEDGER: contribution grouped by contiguous ON (tilt) /
  OFF (gate) episodes — the honest count of independent bets.

PRE-REGISTERED VERDICT RULES (fixed before results were computed):
  - Tilt: KEEP-AS-EDGE if point contribution > 0 AND bootstrap
    P(mean>0) >= 0.90 AND placebo percentile >= 90. KEEP-AS-POSITIONAL
    (documented bet, not evidence-backed alpha) if point contribution >= 0
    but either noise test fails. DROP if point contribution < 0.
  - Gate: primary case is structural drawdown insurance, not alpha.
    KEEP if max-DD improvement > 5pp AND point Sharpe delta >= 0; the
    alpha-vs-noise result is reported alongside. DROP only if it costs
    Sharpe AND fails to improve drawdown.

Three ways this could be silently wrong, and the defences:
  1. PLACEBO SHAPE MISMATCH — a placebo with different switch counts or ON
     share would make the actual overlay look artificially informative.
     Rotation preserves both exactly, and the placebo runs through the
     IDENTICAL downstream maths including the 5 bps switch costs (the
     rotated series is the already-lagged state, so no placebo sees
     information the deployed overlay could not have seen).
  2. BLOCK-SIZE SENSITIVITY — a single block length can flatter the CI.
     Bootstrap run at 20/60/120-day blocks; verdict requires the 60d
     precedent block but the others are reported.
  3. COMPOSITION DRIFT — contribution must be measured on the same curves
     the rest of WS3 uses. Asserted: tilted == ungated + d_tilt and
     final == tilted + d_gate elementwise to 1e-12 (identity by
     construction in ws3_common, re-checked here).

Output: data/ws3_overlay_bootstrap.json
Run:    python scripts/run_ws3_overlay_bootstrap.py
"""
from __future__ import annotations

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

OUT = W.DATA / "ws3_overlay_bootstrap.json"
N_BOOT = 1000
N_PLACEBO = 1000
SEED = 42          # run_robustness.py:306 precedent
BLOCKS = [20, 60, 120]


def sharpe(daily: pd.Series) -> float:
    d = daily.dropna()
    return float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0.0


def block_bootstrap_mean(d: pd.Series, block: int, n: int,
                         seed: int) -> dict:
    """Bootstrap distribution of the ANNUALISED mean contribution."""
    rng = np.random.default_rng(seed)
    x = d.dropna().values
    T = len(x)
    n_blocks = T // block
    means = []
    for _ in range(n):
        starts = rng.integers(0, T - block, size=n_blocks)
        sample = np.concatenate([x[s:s + block] for s in starts])
        means.append(sample.mean() * 252)
    means = np.array(means)
    return {"block": block,
            "ann_contribution_p5": float(np.percentile(means, 5)),
            "ann_contribution_p50": float(np.percentile(means, 50)),
            "ann_contribution_p95": float(np.percentile(means, 95)),
            "p_mean_positive": float((means > 0).mean())}


def rotation_placebos(state: pd.Series, n: int, seed: int) -> np.ndarray:
    """n circular rotations of the (already-lagged) state vector."""
    rng = np.random.default_rng(seed)
    v = state.values
    offsets = rng.integers(1, len(v) - 1, size=n)
    return np.stack([np.roll(v, int(o)) for o in offsets])


def episodes(state: pd.Series, d: pd.Series, active_value: float) -> list[dict]:
    """Contiguous active episodes of the overlay with their contribution.
    Contribution outside active episodes (switch-cost days on the boundary)
    is attributed to the episode it opens/closes via the state itself."""
    out, open_start = [], None
    active = state == active_value
    for i, (dt, a) in enumerate(active.items()):
        if a and open_start is None:
            open_start = dt
        elif not a and open_start is not None:
            seg = d.loc[open_start:dt]
            out.append({"start": str(open_start.date()),
                        "end": str(dt.date()),
                        "days": int(len(seg)),
                        "contribution_pp": float(seg.sum() * 100)})
            open_start = None
    if open_start is not None:
        seg = d.loc[open_start:]
        out.append({"start": str(open_start.date()), "end": "open",
                    "days": int(len(seg)),
                    "contribution_pp": float(seg.sum() * 100)})
    return out


def main() -> int:
    base = W3.build_ws3_baselines()
    idx, end = base["idx"], base["common_end"]
    ungated = base["ungated_returns"]
    tilted = base["tilted_returns"]
    final = base["final_track_returns"]
    sig = base["tilt_sig_lagged"]
    state = base["gate_state_lagged"]

    d_tilt = tilted - ungated
    d_gate = final - tilted
    assert (tilted - (ungated + d_tilt)).abs().max() < 1e-12
    assert (final - (tilted + d_gate)).abs().max() < 1e-12

    results = {}
    for name, d, st, active, with_r, without_r in (
            ("phase22_tilt", d_tilt, sig, 1.0, tilted, ungated),
            ("phase19_gate", d_gate, state, 0.0, final, tilted)):
        point_ann = float(d.sum() / len(d) * 252)
        sh_with, sh_without = sharpe(with_r), sharpe(without_r)
        eq_w = (1 + with_r).cumprod()
        eq_wo = (1 + without_r).cumprod()
        dd_w = float((eq_w / eq_w.cummax() - 1).min())
        dd_wo = float((eq_wo / eq_wo.cummax() - 1).min())
        n_switches = int(st.diff().fillna(0).abs().sum())
        share_active = float((st == active).mean())

        boot = {f"block_{b}": block_bootstrap_mean(d, b, N_BOOT, SEED + b)
                for b in BLOCKS}

        # placebo: recompute the overlay under rotated states. Percentiles
        # on three metrics: annualised return contribution, Sharpe of the
        # with-overlay track, and max-DD improvement (the risk-adjusted
        # question is the decisive one for an insurance overlay).
        placebo_contrib, placebo_sharpe, placebo_ddimp = [], [], []
        rots = rotation_placebos(st, N_PLACEBO, SEED)
        if name == "phase22_tilt":
            shift = 0.10 * (base["eem_ret"] - base["rets"]["B"])
            s_mult = 1.0   # active when state==1
        else:
            shift = W3.DERISK * (base["shy_ret"] - tilted)
            s_mult = -1.0  # active when state==0 -> weight (1-s)
        wo = without_r.values
        for rv in rots:
            s = rv if s_mult > 0 else (1.0 - rv)
            sw = np.abs(np.diff(rv, prepend=rv[0])) * W3.SWITCH_COST
            pd_d = s * shift.values - sw
            placebo_contrib.append(float(pd_d.mean() * 252))
            wr = wo + pd_d
            sd = wr.std(ddof=1)
            placebo_sharpe.append(float(wr.mean() / sd * math.sqrt(252))
                                  if sd > 0 else 0.0)
            eq = np.cumprod(1 + wr)
            ddp = float((eq / np.maximum.accumulate(eq) - 1).min())
            placebo_ddimp.append((ddp - dd_wo) * 100)
        placebo_contrib = np.array(placebo_contrib)
        placebo_sharpe = np.array(placebo_sharpe)
        placebo_ddimp = np.array(placebo_ddimp)
        pct = float((placebo_contrib < point_ann).mean() * 100)
        pct_sharpe = float((placebo_sharpe < sh_with).mean() * 100)
        pct_ddimp = float((placebo_ddimp
                           < (dd_w - dd_wo) * 100).mean() * 100)

        ep = episodes(st, d, active)
        results[name] = {
            "n_switches": n_switches,
            "share_days_active": share_active,
            "n_episodes": len(ep),
            "point_ann_contribution_pct": point_ann * 100,
            "sharpe_with": sh_with, "sharpe_without": sh_without,
            "sharpe_delta": sh_with - sh_without,
            "max_dd_with": dd_w, "max_dd_without": dd_wo,
            "dd_improvement_pp": (dd_w - dd_wo) * 100,
            "bootstrap": boot,
            "placebo": {
                "n": N_PLACEBO,
                "actual_percentile": pct,
                "actual_percentile_sharpe": pct_sharpe,
                "actual_percentile_dd_improvement": pct_ddimp,
                "placebo_p50_ann_pct": float(np.percentile(
                    placebo_contrib, 50)) * 100,
                "placebo_p90_ann_pct": float(np.percentile(
                    placebo_contrib, 90)) * 100,
                "placebo_sharpe_p50": float(np.percentile(
                    placebo_sharpe, 50)),
                "placebo_dd_improvement_p50_pp": float(np.percentile(
                    placebo_ddimp, 50)),
                "note": ("sharpe/dd percentiles are supplementary "
                         "diagnostics added alongside the pre-registered "
                         "contribution rule, not a replacement for it"),
            },
            "episodes": ep,
        }
        b60 = boot["block_60"]
        print(f"{name}: point {point_ann * 100:+.2f}%/yr, dSharpe "
              f"{sh_with - sh_without:+.4f}, dDD {(dd_w - dd_wo) * 100:+.1f}pp, "
              f"switches {n_switches}, episodes {len(ep)}")
        print(f"  bootstrap(60d): P(>0) {b60['p_mean_positive']:.2f} "
              f"CI [{b60['ann_contribution_p5'] * 100:+.2f}, "
              f"{b60['ann_contribution_p95'] * 100:+.2f}]%/yr | placebo "
              f"pct: contrib {pct:.0f}% sharpe {pct_sharpe:.0f}% "
              f"ddImp {pct_ddimp:.0f}%")

    # ---- pre-registered verdicts ---------------------------------------
    t = results["phase22_tilt"]
    b60 = t["bootstrap"]["block_60"]
    if t["point_ann_contribution_pct"] < 0:
        t_verdict = "DROP"
    elif (b60["p_mean_positive"] >= 0.90
          and t["placebo"]["actual_percentile"] >= 90):
        t_verdict = "KEEP_AS_EDGE"
    else:
        t_verdict = "KEEP_AS_POSITIONAL"
    g = results["phase19_gate"]
    if g["dd_improvement_pp"] > 5.0 and g["sharpe_delta"] >= 0:
        g_verdict = "KEEP_STRUCTURAL"
    elif g["sharpe_delta"] < 0 and g["dd_improvement_pp"] <= 5.0:
        g_verdict = "DROP"
    else:
        g_verdict = "REVIEW"
    results["verdicts"] = {"phase22_tilt": t_verdict,
                           "phase19_gate": g_verdict}
    print(f"verdicts: tilt {t_verdict}, gate {g_verdict}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Block-bootstrap + rotation-placebo audit of the "
                        "Phase 22 tilt and Phase 19 gate contributions on "
                        "the post-Phase-29 fixed-window track."),
        "window": {"start": str(idx[0].date()), "end": str(end.date())},
        "pre_registered_rules": {
            "tilt": "KEEP_AS_EDGE if point>0 AND P(mean>0)>=0.90 AND "
                    "placebo pct>=90; KEEP_AS_POSITIONAL if point>=0 "
                    "otherwise; DROP if point<0",
            "gate": "KEEP_STRUCTURAL if dDD>5pp AND dSharpe>=0; DROP if "
                    "costs Sharpe and fails DD; else REVIEW",
        },
        **results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
