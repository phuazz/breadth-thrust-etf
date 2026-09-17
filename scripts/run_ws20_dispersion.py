"""WS20 Layer A — the registered run, once.

Registration: `C:\\dev\\KICKOFF_ws20-trend-dispersion.md` (freeze `1cd0626`,
§10 signed 2026-09-18). This script computes the ONE estimate §4.5 permits,
plus the reported-only legs §5.5 names, and records every cell it computed so
a later reader can count them (§6.6).

    python scripts/run_ws20_dispersion.py

Requires BTE_PRICE_SOURCE=norgate (§4.4). The script fails closed without it —
a run on the yfinance caches would be a different price basis from the one
registered, and the difference would be invisible in the output.

Output: data_local/ws20/results.json (gitignored; no deployed surface, no
docs/, no dashboard, no scanner column).

Reading order, mirroring the deployed engine exactly:
  engine_rebalance_dates(...)          -> the W-FRI grid with the holiday skip
  prev_idx = index.get_loc(rd) - 1     -> the deployed t-1 read
  forward return  close[t] -> close[t+4 grid weeks]
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "data_local" / "ws20"
OUT_PATH = OUT_DIR / "results.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from run_portfolio import build_panels  # noqa: E402
from run_ma200_sweep import (  # noqa: E402
    align_breadth_to_index,
    load_constituent_prices,
    MA_PERIOD,
)
from rebalance_calendar import engine_rebalance_dates  # noqa: E402
import ws20_dispersion as ws20  # noqa: E402

WINDOW_END = pd.Timestamp("2026-06-30")   # §4.4, the WS6 register window end


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def demean(panel: pd.DataFrame) -> pd.DataFrame:
    """Phase 20 cross-sectional demeaning — the deployed sleeve A object."""
    return panel.sub(panel.mean(axis=1, skipna=True), axis=0)


def build_dispersion(used, trade_index, measure="iqr"):
    """Per-panel dispersion percentile, aligned to the trade calendar.

    Dispersion and its percentile are computed on each panel's OWN constituent
    calendar — the percentile's reference history is that panel's sessions, not
    a foreign grid — and only then projected onto the trade calendar through
    the deployed freshness-aware helper, exactly as breadth is.
    """
    z_cols, raw_cols, n_cols, meta = {}, {}, {}, {}
    for etf in used:
        cp = load_constituent_prices(etf)
        disp = ws20.panel_dispersion(cp, MA_PERIOD, ws20.MIN_VALID_NAMES)
        pct = ws20.prior_percentile(disp[measure], ws20.STD_MIN_PERIODS)
        z_cols[etf] = align_breadth_to_index(pct, trade_index)
        raw_cols[etf] = align_breadth_to_index(disp[measure], trade_index)
        n_cols[etf] = align_breadth_to_index(disp["n_valid"].astype(float),
                                             trade_index)
        first = pct.dropna()
        meta[etf] = {
            "n_constituents_last": _safe(disp["n_valid"].iloc[-1]),
            "n_valid_min": _safe(disp["n_valid"].min()),
            "n_valid_max": _safe(disp["n_valid"].max()),
            "first_percentile_date": (first.index.min().strftime("%Y-%m-%d")
                                      if len(first) else None),
            "panel_first_session": cp.index.min().strftime("%Y-%m-%d"),
        }
    cols = list(used)
    return (pd.DataFrame(z_cols).reindex(columns=cols),
            pd.DataFrame(raw_cols).reindex(columns=cols),
            pd.DataFrame(n_cols).reindex(columns=cols),
            meta)


def decision_frames(closes, breadth_d, z_panel, n_panel, grid, horizon_weeks):
    """Read every predictor at t-1 and the forward return from t.

    Returns (forward, breadth_at_t1, z_at_t1, ln_n_at_t1) indexed by decision
    date. A decision date survives only if its forward window completes on or
    before the registered window end — the period after 2026-06-30 is a
    holdout and is not touched, not even to finish a return.
    """
    fwd, b_rows, z_rows, n_rows = {}, {}, {}, {}
    for i, rd in enumerate(grid):
        j = i + horizon_weeks
        if j >= len(grid):
            break
        end = grid[j]
        if end > WINDOW_END:
            break
        prev = closes.index.get_loc(rd) - 1
        if prev < 0:
            continue
        fwd[rd] = closes.loc[end] / closes.loc[rd] - 1.0
        b_rows[rd] = breadth_d.iloc[prev]
        z_rows[rd] = z_panel.iloc[prev]
        n_rows[rd] = np.log(n_panel.iloc[prev].where(n_panel.iloc[prev] > 0))
    idx = sorted(fwd)
    return (pd.DataFrame(fwd).T.loc[idx], pd.DataFrame(b_rows).T.loc[idx],
            pd.DataFrame(z_rows).T.loc[idx], pd.DataFrame(n_rows).T.loc[idx])


def estimate(fwd, b, z, label, extra=None, with_inference=True):
    """One cell: the Fama-MacBeth mean and, for the primary, its inference."""
    coef, contrib, counts = ws20.cross_section_coefficients(fwd, b, z, extra=extra)
    coef = coef.dropna()
    out = {
        "label": label,
        "n_weeks": int(len(coef)),
        "mean_coefficient": _safe(coef.mean()),
        "panels_per_week_min": _safe(counts.min()),
        "panels_per_week_median": _safe(counts.median()),
    }
    if len(coef) == 0:
        return out, coef, contrib
    mid = len(coef) // 2
    h1, h2 = coef.iloc[:mid], coef.iloc[mid:]
    out["half1"] = {"mean": _safe(h1.mean()), "n": int(len(h1)),
                    "start": h1.index.min().strftime("%Y-%m-%d"),
                    "end": h1.index.max().strftime("%Y-%m-%d")}
    out["half2"] = {"mean": _safe(h2.mean()), "n": int(len(h2)),
                    "start": h2.index.min().strftime("%Y-%m-%d"),
                    "end": h2.index.max().strftime("%Y-%m-%d")}
    if with_inference:
        boot = ws20.circular_block_bootstrap_mean(
            coef, ws20.BLOCK_WEEKS, ws20.N_BOOT, ws20.SEED)
        out["ci95"] = [_safe(np.percentile(boot, 2.5)),
                       _safe(np.percentile(boot, 97.5))]
        out["bootstrap"] = {"block_weeks": ws20.BLOCK_WEEKS,
                            "n_boot": ws20.N_BOOT, "seed": ws20.SEED,
                            "se": _safe(boot.std(ddof=1))}
    return out, coef, contrib


def main() -> int:
    source = os.environ.get("BTE_PRICE_SOURCE", "")
    if source.lower() != "norgate":
        print("REFUSED: §4.4 registers the Norgate per-column price basis. "
              "Set BTE_PRICE_SOURCE=norgate and re-run.")
        return 2

    print("WS20 Layer A — registered run. Building deployed panels ...", flush=True)
    closes, breadth, used = build_panels()
    closes = closes.loc[:WINDOW_END]
    breadth = breadth.loc[:WINDOW_END]
    print(f"  panels: {len(used)} | closes {closes.index.min().date()} .. "
          f"{closes.index.max().date()}")

    z_panel, raw_disp, n_panel, panel_meta = build_dispersion(used, closes.index)
    breadth_d = demean(breadth[used])

    # The realised window start: the first session on which EVERY panel carries
    # a defined percentile, i.e. 252 sessions of its own dispersion history.
    ready = z_panel.notna().all(axis=1)
    if not ready.any():
        print("REFUSED: no session on which all panels carry a percentile.")
        return 3
    start = ready.idxmax()
    grid = engine_rebalance_dates(closes.index, start, "W-FRI", "NYSE")
    grid = [d for d in grid if d >= start]
    print(f"  realised start {start.date()} | grid {len(grid)} rebalances "
          f"to {grid[-1].date()}")

    fwd, b, z, ln_n = decision_frames(closes, breadth_d, z_panel, n_panel,
                                      grid, ws20.FORWARD_WEEKS)
    print(f"  decision weeks (4w forward, completing by {WINDOW_END.date()}): "
          f"{len(fwd)}")

    cells = []

    # --- PRIMARY (§5.1) ----------------------------------------------------
    primary, coef, contrib = estimate(fwd, b, z, "primary_iqr_4w")
    cells.append("primary_iqr_4w")
    null = ws20.shuffle_null_means(fwd, b, z, ws20.N_NULL, ws20.SEED)
    primary["null"] = {
        "n_paths": int(ws20.N_NULL), "seed": int(ws20.SEED),
        "median": _safe(np.nanmedian(null)), "sd": _safe(np.nanstd(null, ddof=1)),
        "p2_5": _safe(np.nanpercentile(null, 2.5)),
        "p97_5": _safe(np.nanpercentile(null, 97.5)),
    }
    pctile = ws20.percentile_of(float(coef.mean()), null)
    primary["null_percentile"] = _safe(pctile)
    # Persisted so the record's charts are built from the run's own output
    # rather than a second computation of the same quantity.
    primary["null_draws"] = [_safe(v) for v in null]
    primary["weekly_coefficients"] = {
        d.strftime("%Y-%m-%d"): _safe(v) for d, v in coef.items()
    }

    dec = ws20.decompose(coef, contrib)
    thin = ws20.thinness_flag(dec)
    primary["decomposition"] = {
        "by_panel": {k: _safe(v) for k, v in dec["by_panel"].items()},
        "by_year": {str(k): _safe(v) for k, v in dec["by_year"].items()},
        "panel_shares": {k: _safe(v) for k, v in dec["panel_shares"].items()},
        "year_shares": {str(k): _safe(v) for k, v in dec["year_shares"].items()},
    }
    primary["thinness"] = {k: (_safe(v) if isinstance(v, float) else v)
                           for k, v in thin.items()}

    gates = ws20.evaluate_gates(
        mean_coef=float(coef.mean()),
        ci_low=primary["ci95"][0], ci_high=primary["ci95"][1],
        half1_mean=primary["half1"]["mean"], half2_mean=primary["half2"]["mean"],
        null_percentile=pctile, thin=bool(thin["thin"]),
    )

    # --- REPORTED ONLY (§5.5) — no bar attached ----------------------------
    reported = {}

    uni = ws20.univariate_coefficients(fwd, z).dropna()
    reported["univariate_no_breadth_control"] = {
        "mean_coefficient": _safe(uni.mean()), "n_weeks": int(len(uni)),
        "caveat": "CONFOUNDED — dispersion and breadth are mechanically "
                  "related (§3). Not an input to any gate and not an argument "
                  "of evaluate_gates.",
    }
    cells.append("univariate_iqr_4w")

    ctl, _, _ = estimate(fwd, b, z, "with_ln_n_control",
                         extra={"ln_n": ln_n}, with_inference=False)
    reported["with_ln_n_control"] = ctl
    cells.append("ln_n_control_iqr_4w")

    for h in ws20.REPORTED_HORIZONS_WEEKS:
        f2, b2, z2, _ = decision_frames(closes, breadth_d, z_panel, n_panel,
                                        grid, h)
        cell, _, _ = estimate(f2, b2, z2, f"horizon_{h}w", with_inference=False)
        reported[f"horizon_{h}w"] = cell
        cells.append(f"iqr_{h}w")

    z_sd, raw_sd, _, _ = build_dispersion(used, closes.index, measure="sd")
    f3, b3, z3, _ = decision_frames(closes, breadth_d, z_sd, n_panel, grid,
                                    ws20.FORWARD_WEEKS)
    cell_sd, _, _ = estimate(f3, b3, z3, "sd_variant", with_inference=False)
    reported["sd_variant_4w"] = cell_sd
    cells.append("sd_4w")

    # The confound, as a number rather than an assertion (§5.5).
    pooled = pd.DataFrame({"z": z.stack(), "b": b.stack(),
                           "raw": raw_disp.reindex(z.index).stack()}).dropna()
    reported["confound"] = {
        "corr_z_breadth": _safe(pooled["z"].corr(pooled["b"])),
        "corr_raw_dispersion_breadth": _safe(pooled["raw"].corr(pooled["b"])),
        "n_panel_weeks": int(len(pooled)),
    }

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "registration": {
            "document": "KICKOFF_ws20-trend-dispersion.md",
            "freeze": "1cd0626", "signed_off": "2026-09-18",
        },
        "config": {
            "ma_period": MA_PERIOD, "min_periods": int(MA_PERIOD * 0.9),
            "min_valid_names": ws20.MIN_VALID_NAMES,
            "std_min_periods": ws20.STD_MIN_PERIODS,
            "forward_weeks": ws20.FORWARD_WEEKS,
            "block_weeks": ws20.BLOCK_WEEKS, "n_boot": ws20.N_BOOT,
            "n_null": ws20.N_NULL, "seed": ws20.SEED,
            "price_source": source, "window_end": WINDOW_END.strftime("%Y-%m-%d"),
            "rebalance": "W-FRI (engine_rebalance_dates, NYSE)",
        },
        "window": {
            "realised_start": start.strftime("%Y-%m-%d"),
            "first_decision": fwd.index.min().strftime("%Y-%m-%d"),
            "last_decision": fwd.index.max().strftime("%Y-%m-%d"),
            "n_decision_weeks": int(len(fwd)), "n_panels": len(used),
        },
        "panels": panel_meta,
        "primary": primary,
        "gates": gates,
        "reported_only": reported,
        "cells_computed": cells,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"WS20 LAYER A VERDICT: {gates['verdict']}")
    print("=" * 78)
    print(f"  mean coefficient {primary['mean_coefficient']:+.5f}  "
          f"CI95 [{primary['ci95'][0]:+.5f}, {primary['ci95'][1]:+.5f}]")
    print(f"  halves {primary['half1']['mean']:+.5f} / "
          f"{primary['half2']['mean']:+.5f}   "
          f"null percentile {primary['null_percentile']:.3f}")
    print(f"  G-A1 {gates['G_A1_ci_excludes_zero']}  "
          f"G-A2 {gates['G_A2_sign_holds_in_halves']}  "
          f"G-A3 {gates['G_A3_outside_null']}  G-A4 thin={gates['G_A4_thin']}")
    print(f"  univariate (confounded, no gate) "
          f"{reported['univariate_no_breadth_control']['mean_coefficient']:+.5f}")
    print(f"  cells computed: {len(cells)} -> {cells}")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
