"""Phase 7 — block bootstrap confidence intervals on Sharpe.

Answers the question every institutional AI / quant peer reviewer asks:
"Is the +1.16 deployed blend Sharpe statistically distinguishable from
the +1.09 baseline?"

Methodology:
  - Moving block bootstrap (MBB) with block size 60 trading days (~3 months,
    matches the existing Robustness Test 4 methodology).
  - 2000 bootstrap samples per strategy.
  - For each sample: draw blocks with replacement from the daily return
    series, concatenate to a sample of the same length, compute Sharpe.
  - Report point estimate + (p5, p50, p95) of the bootstrap distribution.

  For the deployed-vs-baseline comparison:
  - PAIRED bootstrap: sample the same block indices for BOTH equity
    curves jointly, so the bootstrap preserves the realised correlation
    between the two strategies. Without pairing, the differential CI
    would be too wide.
  - For each paired sample: compute (Sharpe_deployed - Sharpe_baseline).
  - Report p5, p50, p95 of the differential distribution + the fraction
    of bootstrap samples where deployed > baseline (p_better).

Strategies bootstrapped:
  - Strategy A (US sector breadth rotation) — from topk_robustness.json
  - Strategy B (asset-class momentum) — from asset_class_rotation.json
  - Strategy C (thematic momentum) — from thematic_rotation.json
  - Strategy D (Europe sector breadth) — from europe_rotation.json
  - 4-way blend 35/35/10/20 (deployed) — from multi_strategy.json
  - 3-way blend 45/45/10 (pre-Phase-4 baseline) — from multi_strategy.json

Differentials:
  - 4-way vs 3-way (Phase 4 + 6 cumulative improvement)
  - 4-way vs Strategy A alone (deployed vs simplest baseline)

Output: data/phase7_bootstrap.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "phase7_bootstrap.json"

sys.stdout.reconfigure(encoding="utf-8")

# Bootstrap parameters
BLOCK_SIZE = 60     # trading days per block (~3 months)
N_SAMPLES = 2000    # number of bootstrap samples
RNG_SEED = 42       # reproducibility


def _sharpe_from_returns(daily_ret: np.ndarray) -> float:
    """Annualised Sharpe from a daily return array."""
    if len(daily_ret) < 2:
        return float("nan")
    mu = daily_ret.mean()
    sigma = daily_ret.std()
    if sigma == 0:
        return 0.0
    return float(mu / sigma * math.sqrt(252))


def _max_drawdown_from_returns(daily_ret: np.ndarray) -> float:
    """Max drawdown from a daily return array (negative number)."""
    if len(daily_ret) < 2:
        return 0.0
    equity = (1.0 + daily_ret).cumprod()
    rolling_max = np.maximum.accumulate(equity)
    dd = (equity - rolling_max) / rolling_max
    return float(dd.min())


def moving_block_bootstrap_indices(n: int, block_size: int, n_samples: int,
                                     rng: np.random.Generator) -> np.ndarray:
    """Generate bootstrap indices via moving block bootstrap.

    Returns an array of shape (n_samples, n) where each row is a vector
    of indices into the original return array. Blocks are sampled with
    replacement from the (n - block_size + 1) possible starting positions.
    """
    n_blocks = int(np.ceil(n / block_size))
    n_starts = max(1, n - block_size + 1)
    out = np.empty((n_samples, n), dtype=np.int64)
    for s in range(n_samples):
        starts = rng.integers(0, n_starts, size=n_blocks)
        idx = np.concatenate([np.arange(start, start + block_size)
                              for start in starts])[:n]
        out[s] = idx
    return out


def bootstrap_sharpe(daily_ret: np.ndarray, block_size: int,
                      n_samples: int, rng: np.random.Generator) -> dict:
    """Bootstrap the Sharpe ratio of a single return series."""
    n = len(daily_ret)
    indices = moving_block_bootstrap_indices(n, block_size, n_samples, rng)
    sharpes = np.array([_sharpe_from_returns(daily_ret[idx])
                          for idx in indices])
    sharpes = sharpes[~np.isnan(sharpes)]
    if len(sharpes) == 0:
        return {"point": None, "p5": None, "p50": None, "p95": None,
                "n_valid": 0}
    return {
        "point": _sharpe_from_returns(daily_ret),
        "p5": float(np.percentile(sharpes, 5)),
        "p50": float(np.percentile(sharpes, 50)),
        "p95": float(np.percentile(sharpes, 95)),
        "mean": float(sharpes.mean()),
        "std": float(sharpes.std()),
        "n_valid": int(len(sharpes)),
    }


def paired_bootstrap_diff(daily_a: np.ndarray, daily_b: np.ndarray,
                            block_size: int, n_samples: int,
                            rng: np.random.Generator) -> dict:
    """Paired bootstrap on (Sharpe_a - Sharpe_b). Both series must be
    same length and aligned in time. Returns the differential
    distribution + p_better (fraction where a > b)."""
    assert len(daily_a) == len(daily_b), "series must be aligned"
    n = len(daily_a)
    indices = moving_block_bootstrap_indices(n, block_size, n_samples, rng)
    diffs = []
    for idx in indices:
        sh_a = _sharpe_from_returns(daily_a[idx])
        sh_b = _sharpe_from_returns(daily_b[idx])
        if not (np.isnan(sh_a) or np.isnan(sh_b)):
            diffs.append(sh_a - sh_b)
    diffs = np.array(diffs)
    if len(diffs) == 0:
        return {"delta_point": None, "delta_p5": None, "delta_p50": None,
                "delta_p95": None, "p_better": None, "n_valid": 0}
    return {
        "delta_point": (_sharpe_from_returns(daily_a)
                         - _sharpe_from_returns(daily_b)),
        "delta_p5": float(np.percentile(diffs, 5)),
        "delta_p50": float(np.percentile(diffs, 50)),
        "delta_p95": float(np.percentile(diffs, 95)),
        "p_better": float((diffs > 0).mean()),
        "n_valid": int(len(diffs)),
    }


def equity_to_daily_ret(equity_dates: list[str],
                          equity_values: list[float]) -> tuple[pd.DatetimeIndex,
                                                                 np.ndarray]:
    """Convert dashboard equity JSON arrays to a daily return numpy array.
    Drops the leading NaN from pct_change."""
    idx = pd.to_datetime(equity_dates)
    eq = pd.Series(equity_values, index=idx, dtype=float)
    rets = eq.pct_change().dropna().values
    return idx[1:], rets


def main() -> int:
    print(f"Block bootstrap on strategy Sharpe ratios")
    print(f"  block_size={BLOCK_SIZE} trading days (~3 months)")
    print(f"  n_samples={N_SAMPLES} bootstrap draws")
    print(f"  seed={RNG_SEED}")
    rng = np.random.default_rng(RNG_SEED)

    # ---------- Load each strategy's daily return series ----------
    series = {}

    print("\nLoading strategies ...")
    # Strategy A (from topk_robustness.json headline)
    topk = json.loads((DATA_DIR / "topk_robustness.json").read_text(encoding="utf-8"))
    h = topk["headline"]
    dates_a, rets_a = equity_to_daily_ret(h["headline_equity_dates"],
                                            h["headline_equity"])
    series["strategy_a"] = {
        "label": f"Strategy A — US sector breadth (K={h['K']} {h['rebal_freq']})",
        "dates": dates_a, "rets": rets_a,
    }
    print(f"  A: {len(rets_a)} days, {dates_a[0].date()} -> {dates_a[-1].date()}")

    # Strategy B
    ac = json.loads((DATA_DIR / "asset_class_rotation.json").read_text(encoding="utf-8"))
    h = ac["headline"]
    dates_b, rets_b = equity_to_daily_ret(h["headline_equity_dates"],
                                            h["headline_equity"])
    series["strategy_b"] = {
        "label": f"Strategy B — asset-class momentum (K={h['K']} {h['rebal_freq']})",
        "dates": dates_b, "rets": rets_b,
    }
    print(f"  B: {len(rets_b)} days, {dates_b[0].date()} -> {dates_b[-1].date()}")

    # Strategy C
    tc = json.loads((DATA_DIR / "thematic_rotation.json").read_text(encoding="utf-8"))
    h = tc["headline"]
    dates_c, rets_c = equity_to_daily_ret(h["headline_equity_dates"],
                                            h["headline_equity"])
    series["strategy_c"] = {
        "label": f"Strategy C — thematic momentum (K={h['K']} {h['rebal_freq']}, equal-wt)",
        "dates": dates_c, "rets": rets_c,
    }
    print(f"  C: {len(rets_c)} days, {dates_c[0].date()} -> {dates_c[-1].date()}")

    # Strategy D
    eu = json.loads((DATA_DIR / "europe_rotation.json").read_text(encoding="utf-8"))
    h = eu["headline"]
    dates_d, rets_d = equity_to_daily_ret(h["headline_equity_dates"],
                                            h["headline_equity"])
    series["strategy_d"] = {
        "label": f"Strategy D — Europe sector breadth (K={h['K']} {h['rebal_freq']})",
        "dates": dates_d, "rets": rets_d,
    }
    print(f"  D: {len(rets_d)} days, {dates_d[0].date()} -> {dates_d[-1].date()}")

    # Multi-strategy blends — use the common-window normalised equity from
    # multi_strategy.json (these are all on the same common window so paired
    # comparisons work).
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    for k in ["blend_35_35_10_20", "blend_45_45_10", "blend_50_50",
               "strategy_a", "strategy_b", "strategy_c", "strategy_d"]:
        if k not in multi["strategies"]:
            continue
        v = multi["strategies"][k]
        dates, rets = equity_to_daily_ret(v["dates"], v["equity"])
        series[f"multi_{k}"] = {
            "label": v["label"], "dates": dates, "rets": rets,
        }
    print(f"  Common window for blends: "
          f"{series['multi_blend_35_35_10_20']['dates'][0].date()} -> "
          f"{series['multi_blend_35_35_10_20']['dates'][-1].date()} "
          f"({len(series['multi_blend_35_35_10_20']['rets'])} days)")

    # ---------- Bootstrap each strategy's Sharpe ----------
    print(f"\nBootstrapping per-strategy Sharpe CIs ({N_SAMPLES} samples each) ...")
    boot_results = {}
    for key, s in series.items():
        r = bootstrap_sharpe(s["rets"], BLOCK_SIZE, N_SAMPLES, rng)
        boot_results[key] = {
            "label": s["label"],
            "n_days": int(len(s["rets"])),
            "date_range": [s["dates"][0].strftime("%Y-%m-%d"),
                            s["dates"][-1].strftime("%Y-%m-%d")],
            **r,
        }
        print(f"  {key:<30}  point {r['point']:+.3f}   "
              f"p5/p50/p95: {r['p5']:+.3f} / {r['p50']:+.3f} / {r['p95']:+.3f}")

    # ---------- Paired bootstrap on key differentials ----------
    print(f"\nPaired bootstrap on Sharpe differentials ...")
    diff_pairs = [
        ("blend_35_35_10_20_vs_blend_45_45_10",
         "4-way deployed vs 3-way baseline (Phase 4+6 cumulative)",
         "multi_blend_35_35_10_20", "multi_blend_45_45_10"),
        ("blend_35_35_10_20_vs_strategy_a",
         "4-way deployed vs Strategy A alone",
         "multi_blend_35_35_10_20", "multi_strategy_a"),
        ("blend_35_35_10_20_vs_blend_50_50",
         "4-way deployed vs 50/50 A:B (simplest blend)",
         "multi_blend_35_35_10_20", "multi_blend_50_50"),
        ("blend_45_45_10_vs_blend_50_50",
         "3-way baseline vs 50/50 A:B (does adding C help?)",
         "multi_blend_45_45_10", "multi_blend_50_50"),
    ]
    diff_results = {}
    for key, label, key_a, key_b in diff_pairs:
        if key_a not in series or key_b not in series:
            print(f"  SKIP {key}: missing inputs")
            continue
        sa = series[key_a]
        sb = series[key_b]
        # Align on common date range (intersection)
        common_idx = sa["dates"].intersection(sb["dates"])
        if len(common_idx) < BLOCK_SIZE * 4:
            print(f"  SKIP {key}: only {len(common_idx)} aligned days")
            continue
        # Map back to aligned return arrays
        df_a = pd.Series(sa["rets"], index=sa["dates"]).reindex(common_idx)
        df_b = pd.Series(sb["rets"], index=sb["dates"]).reindex(common_idx)
        d = paired_bootstrap_diff(df_a.values, df_b.values,
                                    BLOCK_SIZE, N_SAMPLES, rng)
        diff_results[key] = {
            "label": label,
            "vs_a": key_a, "vs_b": key_b,
            "n_aligned_days": int(len(common_idx)),
            "date_range": [common_idx[0].strftime("%Y-%m-%d"),
                            common_idx[-1].strftime("%Y-%m-%d")],
            **d,
        }
        sig = ("** statistically significant @ 5%"
                if (d["delta_p5"] is not None and d["delta_p5"] > 0)
                else "")
        print(f"  {label}")
        print(f"    delta point {d['delta_point']:+.3f}, "
              f"95% CI [{d['delta_p5']:+.3f}, {d['delta_p95']:+.3f}], "
              f"p(better)={d['p_better']:.1%}  {sig}")

    payload = {
        "computed_at_utc": pd.Timestamp.utcnow().isoformat(),
        "block_size_days": BLOCK_SIZE,
        "n_bootstrap_samples": N_SAMPLES,
        "rng_seed": RNG_SEED,
        "per_strategy": boot_results,
        "differentials": diff_results,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
