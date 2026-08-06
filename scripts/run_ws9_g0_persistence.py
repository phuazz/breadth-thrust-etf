"""WS9 G0 — leadership persistence gate for Sleeve C.

Registered in KICKOFF_ws9-c-signal-shape.md sections 4 and 4a, frozen and
committed (bfab8ed) BEFORE this cell ran. Read-only: this script never
writes to data/ and never touches a deployed configuration or the WS7
universe freeze.

The question. Sleeve C ranks 25 thematic ETFs and holds the top K. That
only works if theme leadership persists from one rebalance to the next.
G0 measures the persistence directly and signal-agnostically: across the
cross-section, does a name's trailing-return rank predict its forward
return rank at the cadence the sleeve actually trades?

Signal-agnostic on purpose. G0 ranks on returns, not on the deployed
distance-above-200d-MA statistic, because a gate built on the incumbent
could not legitimately gate its own challenger (T1).

Primary (decides G0):     trailing 13w return rank -> forward 1w return rank
Report-only (cannot rescue a failing primary):
                          trailing 26w -> forward 1w
                          trailing 13w -> forward 4w

Bar: mean per-week Spearman rho > 0 with block-bootstrap P(rho > 0) >= 0.90,
blocks of 13 weeks to respect autocorrelation in the weekly rho series.

Costs are not modelled and should not be: G0 asks whether the phenomenon
exists, not whether it is tradable net of fees. That is T1's job.

Usage:
    python scripts/run_ws9_g0_persistence.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE = PROJECT_ROOT / "data" / "thematic_prices_cache.parquet"
OUT_PATH = PROJECT_ROOT / "reviews" / "ws9_g0_persistence.json"

# Frozen in section 4a. Do not add cells here.
PRIMARY = ("13w_to_1w", 13, 1)
REPORT_ONLY = [("26w_to_1w", 26, 1), ("13w_to_4w", 13, 4)]

CASH_PROXY = "SHY"          # excluded from the cross-section
MIN_NAMES = 8               # a rho on fewer names is not a cross-section
BLOCK_WEEKS = 13
N_BOOT = 10_000
SEED = 20260806             # registration date; fixed for reproducibility
BAR_PROB = 0.90

sys.stdout.reconfigure(encoding="utf-8")


def weekly_panel() -> pd.DataFrame:
    """Friday closes for the 25 risk names, cash proxy dropped.

    Prices come from the same cache the deployed sleeve reads, so the
    total-return convention (yfinance adjusted close) is whatever the
    sleeve itself uses — internally consistent by construction.
    """
    px = pd.read_parquet(CACHE)
    px = px.drop(columns=[CASH_PROXY], errors="ignore")
    px.index = pd.to_datetime(px.index)
    # W-FRI takes the last observation in each Mon-Fri week. A US holiday
    # Friday therefore carries Thursday's close, matching the engines'
    # existing cadence handling rather than inventing a second convention.
    return px.resample("W-FRI").last()


def weekly_rhos(px: pd.DataFrame, lookback: int, horizon: int) -> pd.Series:
    """One cross-sectional Spearman rho per rebalance week."""
    trailing = px / px.shift(lookback) - 1.0
    forward = px.shift(-horizon) / px - 1.0

    rhos = {}
    for dt in px.index:
        a, b = trailing.loc[dt], forward.loc[dt]
        both = a.notna() & b.notna()
        if int(both.sum()) < MIN_NAMES:
            continue
        rho, _ = stats.spearmanr(a[both].to_numpy(), b[both].to_numpy())
        if np.isfinite(rho):
            rhos[dt] = rho
    return pd.Series(rhos, dtype=float).sort_index()


def block_bootstrap_prob(rhos: np.ndarray, rng: np.random.Generator) -> float:
    """P(mean rho > 0) from a circular block bootstrap."""
    n = len(rhos)
    n_blocks = int(np.ceil(n / BLOCK_WEEKS))
    starts = rng.integers(0, n, size=(N_BOOT, n_blocks))
    offsets = np.arange(BLOCK_WEEKS)
    # circular wrap keeps every draw the same length as the sample
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    means = rhos[idx.reshape(N_BOOT, -1)[:, :n]].mean(axis=1)
    return float((means > 0).mean())


def evaluate(px: pd.DataFrame, label: str, lookback: int, horizon: int) -> dict:
    rhos = weekly_rhos(px, lookback, horizon)
    arr = rhos.to_numpy()
    rng = np.random.default_rng(SEED)
    prob = block_bootstrap_prob(arr, rng)
    mean = float(arr.mean())
    return {
        "cell": label,
        "lookback_weeks": lookback,
        "horizon_weeks": horizon,
        "n_weeks": int(len(arr)),
        "first_week": str(rhos.index[0].date()),
        "last_week": str(rhos.index[-1].date()),
        "mean_rho": mean,
        "median_rho": float(np.median(arr)),
        "share_weeks_positive": float((arr > 0).mean()),
        "p_mean_gt_zero": prob,
        "passes": bool(mean > 0 and prob >= BAR_PROB),
    }


def main() -> int:
    px = weekly_panel()

    primary = evaluate(px, *PRIMARY)
    reported = [evaluate(px, *cell) for cell in REPORT_ONLY]

    verdict = "G0_PASS" if primary["passes"] else "G0_FAIL_STOP"

    result = {
        "study": "WS9 G0 — Sleeve C leadership persistence",
        "registration": "KICKOFF_ws9-c-signal-shape.md sections 4, 4a (commit bfab8ed)",
        "sample": {
            "source": "data/thematic_prices_cache.parquet (read-only)",
            "names": int(px.shape[1]),
            "daily_range": "2018-01-02 to 2026-07-17",
            "note": "cache ends before the WS7 2026-07-18 freeze; the WS7 "
                    "out-of-sample window is untouched",
        },
        "bar": {
            "mean_rho": "> 0",
            "p_mean_gt_zero": f">= {BAR_PROB}",
            "bootstrap": f"circular block, {BLOCK_WEEKS}w blocks, "
                         f"{N_BOOT} draws, seed {SEED}",
        },
        "primary": primary,
        "report_only": reported,
        "verdict": verdict,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"WS9 G0 — Sleeve C leadership persistence\n{'=' * 56}")
    for row in [primary] + reported:
        tag = "PRIMARY    " if row is primary else "report-only"
        print(
            f"{tag} {row['cell']:>10}  n={row['n_weeks']:>3}w  "
            f"mean rho {row['mean_rho']:+.4f}  median {row['median_rho']:+.4f}  "
            f"weeks>0 {row['share_weeks_positive']:.1%}  "
            f"P(mean>0) {row['p_mean_gt_zero']:.3f}"
        )
    print(f"\nWindow: {primary['first_week']} to {primary['last_week']}")
    print(f"VERDICT: {verdict}")
    print(f"Written: {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
