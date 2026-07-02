"""WS1 follow-up — paired test: is the best rival lookback a REAL improvement?

Question (2026-07-03): beyond robustness, can the MA parameter be IMPROVED —
is 275d (the hindsight-best on the surface) genuinely better than the
deployed 200d, or statistically indistinguishable?

Because the two variants hold nearly identical portfolios, the precise test
is on the DAILY RETURN DIFFERENCE (paired), not on the two headline Sharpes:
the paired standard error shrinks with the correlation between the variants.

Three ways this could be silently wrong, and the defences:
  1. WINDOW CHERRY-PICK — the comparison runs on the same fixed common
     window as every WS1 test (2018-11-08 -> common end), plus the split
     halves, so no sub-window can be quietly favoured.
  2. AUTOCORRELATION UNDERSTATING THE ERROR — the t-statistic is reported
     both plain and with a Newey-West (10-lag) variance.
  3. PRETENDING THE PICK WAS EX-ANTE — 275d was selected BY this surface,
     so even a significant paired t would overstate the case (selection of
     the max of 13 trials). The test is one-sided AGAINST the rival: if it
     cannot clear ~2 sigma even before a selection haircut, the improvement
     question is closed.

Output: data/ws1_paired_test.json
Run:    python scripts/run_ws1_paired_test.py
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
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

RIVALS = [250, 275]
BASE = 200
OUT = W.DATA / "ws1_paired_test.json"


def nw_tstat(d: np.ndarray, lags: int = 10) -> float:
    """t-stat of mean(d) with Newey-West variance (Bartlett weights)."""
    n = len(d)
    dc = d - d.mean()
    s = float(np.dot(dc, dc)) / n
    for k in range(1, lags + 1):
        wgt = 1.0 - k / (lags + 1.0)
        s += 2.0 * wgt * float(np.dot(dc[k:], dc[:-k])) / n
    se = math.sqrt(s / n)
    return float(d.mean() / se) if se > 0 else float("nan")


def main() -> int:
    print("Loading panels ...", flush=True)
    closes_a, cons_a = W.load_sleeve_a()
    closes_d, cons_d = W.load_sleeve_d()
    closes_b = W.load_sleeve_b()
    closes_c = W.load_sleeve_c()
    d_end = min(cp.index.max() for cp in cons_d.values())
    a_end = min(cp.index.max() for cp in cons_a.values())
    common_end = min(closes_b.index.max(), closes_c.index.max(), a_end, d_end)
    cs = W.COMMON_START

    def blend_at(w: int) -> pd.Series:
        sig_a = W.relative(W.breadth_panel(cons_a, closes_a.index, w))
        eq_a = run_portfolio(closes_a, sig_a, top_k_breadth_weight(W.K_A), cs,
                             cost=W.COST_A, rebalance_freq=W.REBAL)["equity"]
        bp_d = W.breadth_panel(cons_d, closes_d.index, w)
        eq_d = run_portfolio(closes_d, bp_d, top_k_breadth_weight(W.K_D), cs,
                             cost=W.COST_D, rebalance_freq=W.REBAL)["equity"]
        eq_b = B_engine.run_rotation(closes_b, W.distance_signal(closes_b, w),
                                     B_engine.top_k_by_signal(W.K_B), cs,
                                     rebalance_freq=W.REBAL,
                                     cost=W.COST_B)["equity"]
        eq_c = C_engine.run_rotation(closes_c, W.distance_signal(closes_c, w),
                                     C_engine.top_k_equal_weight(W.K_C), cs,
                                     rebalance_freq=W.REBAL,
                                     cost=W.COST_C)["equity"]
        return W.blend_equity(eq_a.loc[:common_end], eq_b.loc[:common_end],
                              eq_c.loc[:common_end], eq_d.loc[:common_end],
                              cs, common_end)

    print(f"Building blends at {BASE} and {RIVALS} ...", flush=True)
    base_eq = blend_at(BASE)
    r_base = base_eq.pct_change().dropna()

    results = {}
    for w in RIVALS:
        eq = blend_at(w)
        r = eq.pct_change().dropna()
        idx = r_base.index.intersection(r.index)
        a, b = r_base.loc[idx].values, r.loc[idx].values
        d = b - a
        n = len(d)
        n_years = (idx[-1] - idx[0]).days / 365.25
        corr = float(np.corrcoef(a, b)[0, 1])
        t_plain = float(d.mean() / d.std(ddof=1) * math.sqrt(n))
        t_nw = nw_tstat(d)
        ann_diff = float(d.mean() * 252)
        sh = {k: float(x.mean() / x.std(ddof=1) * math.sqrt(252))
              for k, x in (("base", a), ("rival", b))}
        years_to_2sig = (n_years * (2.0 / abs(t_nw)) ** 2
                         if t_nw not in (0.0,) and not math.isnan(t_nw)
                         else None)
        split = pd.Timestamp(W.SPLIT_DATE)
        d_ser = pd.Series(d, index=idx)
        halves = {
            "train_ann_diff": float(d_ser.loc[:split].mean() * 252),
            "test_ann_diff": float(d_ser.loc[split:].mean() * 252),
        }
        results[str(w)] = {
            "n_days": n,
            "n_years": round(n_years, 2),
            "daily_return_correlation": round(corr, 4),
            "sharpe_base_200": round(sh["base"], 3),
            "sharpe_rival": round(sh["rival"], 3),
            "annualised_return_difference": round(ann_diff, 5),
            "t_stat_plain": round(t_plain, 3),
            "t_stat_newey_west_10lag": round(t_nw, 3),
            "years_of_data_needed_for_2_sigma": (round(years_to_2sig)
                                                 if years_to_2sig else None),
            "halves_ann_diff": {k: round(v, 5) for k, v in halves.items()},
        }
        print(f"\n{w}d vs {BASE}d on {n} days ({n_years:.1f}y):")
        print(f"  daily-return correlation : {corr:.4f}")
        print(f"  Sharpe                   : {sh['rival']:+.3f} vs {sh['base']:+.3f}")
        print(f"  annualised return diff   : {ann_diff * 100:+.2f}%/yr "
              f"(train {halves['train_ann_diff'] * 100:+.2f}, "
              f"test {halves['test_ann_diff'] * 100:+.2f})")
        print(f"  t-stat plain / NW(10)    : {t_plain:+.2f} / {t_nw:+.2f}")
        if years_to_2sig:
            print(f"  data needed for 2 sigma  : ~{years_to_2sig:.0f} years "
                  f"(have {n_years:.1f}) — before any selection haircut")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Paired daily-return test of rival lookbacks vs the"
                        " deployed 200d blend on the fixed WS1 window. A"
                        " rival must clear ~2 sigma BEFORE any"
                        " multiple-testing haircut to count as a real"
                        " improvement; it was selected as the max of 13"
                        " trials, so even that would overstate it."),
        "base_w": BASE,
        "window": [str(cs.date()), str(common_end.date())],
        "rivals": results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
