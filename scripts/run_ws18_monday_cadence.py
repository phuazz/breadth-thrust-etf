"""WS18 step 2 — does a Monday rebalance cost anything?

Pre-registered in reviews/2026-08-22_prereg_ws18_monday-cadence.md. Run ONLY
after run_ws18_cadence_dates.py has reconciled (§8 gate). It has, and it
amended the design: see Amendment 1.

THREE ARMS, not two, because switching cadence and calendar mode together
would confound them:

    1  W-FRI  holiday_aware        the deployed incumbent
    2  W-FRI  holiday_aware_next   the MODE change alone
    3  W-MON  holiday_aware_next   the CADENCE change, given the mode

Amendment 1 forced arm 2 into existence. Under `holiday_aware` a holiday Monday
rolls BACK three days to the previous Friday (39 of 406 on NYSE), which the new
cadence cannot operate — the decision is made on Saturday from Friday's close,
so a fill rolled onto that same Friday precedes the decision producing it.
`holiday_aware_next` rolls forward instead.

ONE PINNED PRICE FRAME FOR ALL ARMS. §5.1 makes a diff computed across two
downloads inadmissible, and that is not pedantry: the tail-extension
verification failed its first control for exactly this, then had two further
controls pass it for the wrong reason. Each sleeve's panel is built ONCE here
and every arm ranks on that same object.

Usage:
    python scripts/run_ws18_monday_cadence.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rebalance_calendar  # noqa: E402
from run_ws10_holiday_cadence import SLEEVES  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data_local" / "ws18_monday_cadence.json"

# Deployed blend weights. Held fixed by §6 — this study varies the cadence only.
BLEND = {"a": 0.35, "b": 0.35, "c": 0.10, "d": 0.20}

ARMS = [
    ("arm1_fri_aware", "W-FRI", rebalance_calendar.HOLIDAY_AWARE, "incumbent"),
    ("arm2_fri_next", "W-FRI", rebalance_calendar.HOLIDAY_AWARE_NEXT, "mode only"),
    ("arm3_mon_next", "W-MON", rebalance_calendar.HOLIDAY_AWARE_NEXT, "cadence"),
]

TRADING_DAYS = 252


def arm_patch(calendar: str, freq: str, mode: str):
    """engine_rebalance_dates rebound to one arm's (freq, mode)."""
    def f(trading_index, eligible_start, _freq="W-FRI", _cal=None):
        return rebalance_calendar.weekly_rebalance_dates(
            trading_index, eligible_start, freq, mode=mode, calendar=calendar)
    return f


def run_arm(patch_module, calendar, freq, mode, run):
    original = patch_module.engine_rebalance_dates
    patch_module.engine_rebalance_dates = arm_patch(calendar, freq, mode)
    try:
        return run()
    finally:
        patch_module.engine_rebalance_dates = original


def _daily_from_equity(eq: pd.Series) -> pd.Series:
    return eq.pct_change().dropna()


def _sharpe(daily: pd.Series) -> float:
    sd = daily.std(ddof=1)
    return float("nan") if sd == 0 else float(daily.mean() / sd * np.sqrt(TRADING_DAYS))


def _maxdd(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1.0).min())


def _turnover(weights: pd.DataFrame) -> float:
    """One-way turnover per year, as a share of NAV."""
    d = weights.diff().abs().sum(axis=1).fillna(0.0)
    years = max((weights.index[-1] - weights.index[0]).days / 365.25, 1e-9)
    return float(d.sum() / 2.0 / years)


def main() -> int:
    print(__doc__.split("Usage:")[0].strip()[:0] or "", end="")
    print("WS18 — three-arm cadence comparison, one pinned frame per sleeve\n")

    curves: dict[str, dict[str, pd.Series]] = {}
    stats: dict[str, dict] = {}

    for key in ("a", "b", "c", "d"):
        module, patch_module, closes, eligible, run, label, cal = SLEEVES[key]()
        print(f"sleeve {key.upper()} ({label}) on {cal} — panel pinned "
              f"{closes.index.min().date()} to {closes.index.max().date()}")
        curves[key] = {}
        for arm, freq, mode, _ in ARMS:
            r = run_arm(patch_module, cal, freq, mode, run)
            eq = r["equity"].dropna()
            curves[key][arm] = eq
            stats.setdefault(arm, {})[key] = {
                "sharpe": round(_sharpe(_daily_from_equity(eq)), 4),
                "max_dd": round(_maxdd(eq), 4),
                "turnover_pa": round(_turnover(r["weights"]), 4),
                "rebalances": int(len(r["rebalance_dates"])),
            }
            s = stats[arm][key]
            print(f"    {arm:15s} Sharpe {s['sharpe']:+.4f}  DD {s['max_dd']:+.2%}"
                  f"  turnover {s['turnover_pa']:.2f}x  rebals {s['rebalances']}")
        print()

    # ---- blend each arm on the common index -------------------------------
    blends: dict[str, pd.Series] = {}
    for arm, _, _, _ in ARMS:
        common = None
        for key in BLEND:
            idx = curves[key][arm].index
            common = idx if common is None else common.intersection(idx)
        daily = None
        for key, w in BLEND.items():
            d = _daily_from_equity(curves[key][arm]).reindex(common).fillna(0.0)
            daily = w * d if daily is None else daily + w * d
        blends[arm] = daily.dropna()

    # Align all arms so the bootstrap is genuinely paired.
    common = None
    for arm in blends:
        common = blends[arm].index if common is None else common.intersection(blends[arm].index)
    for arm in blends:
        blends[arm] = blends[arm].loc[common]
    print(f"blend common window: {common.min().date()} to {common.max().date()} "
          f"({len(common)} sessions)\n")

    blend_stats = {}
    for arm, _, _, note in ARMS:
        d = blends[arm]
        eq = (1.0 + d).cumprod()
        blend_stats[arm] = {
            "note": note,
            "sharpe": round(_sharpe(d), 4),
            "cagr": round(float(eq.iloc[-1] ** (TRADING_DAYS / len(d)) - 1.0), 4),
            "max_dd": round(_maxdd(eq), 4),
            "turnover_pa": round(sum(stats[arm][k]["turnover_pa"] * w
                                     for k, w in BLEND.items()), 4),
        }
        b = blend_stats[arm]
        print(f"  {arm:15s} ({note:9s}) Sharpe {b['sharpe']:+.4f}  "
              f"CAGR {b['cagr']:+.2%}  DD {b['max_dd']:+.2%}  "
              f"turnover {b['turnover_pa']:.2f}x")

    # ---- paired block bootstrap, the frozen yardstick ----------------------
    from run_phase7_bootstrap import (BLOCK_SIZE, N_SAMPLES, RNG_SEED,
                                      paired_bootstrap_diff)
    rng = np.random.default_rng(RNG_SEED)
    tests = {}
    for a, b in (("arm3_mon_next", "arm1_fri_aware"),
                 ("arm2_fri_next", "arm1_fri_aware"),
                 ("arm3_mon_next", "arm2_fri_next")):
        tests[f"{a} minus {b}"] = paired_bootstrap_diff(
            blends[a].to_numpy(), blends[b].to_numpy(),
            BLOCK_SIZE, N_SAMPLES, rng)
    print(f"\npaired block bootstrap — block {BLOCK_SIZE}, {N_SAMPLES} samples, "
          f"seed {RNG_SEED}")
    for name, t in tests.items():
        # The CI is what decides, not the point estimate: a difference whose
        # 90% band straddles zero is not distinguishable from none, which is
        # exactly the verdict a non-inferiority bar wants to be able to reach.
        lo, hi = t["delta_p5"], t["delta_p95"]
        clears = "clear of zero" if (lo > 0 or hi < 0) else "straddles zero"
        print(f"  {name:34s} point {t['delta_point']:+.4f}  "
              f"90% CI [{lo:+.4f}, {hi:+.4f}] {clears}  "
              f"P(a>b) {t['p_better']:.2f}")

    # ---- the frozen bar ---------------------------------------------------
    a1 = blend_stats["arm3_mon_next"]["sharpe"] - blend_stats["arm1_fri_aware"]["sharpe"]
    a2 = blend_stats["arm3_mon_next"]["max_dd"] - blend_stats["arm1_fri_aware"]["max_dd"]
    t_rel = (blend_stats["arm3_mon_next"]["turnover_pa"]
             / max(blend_stats["arm1_fri_aware"]["turnover_pa"], 1e-9) - 1.0)
    print("\nFROZEN BAR (§4), arm 3 against arm 1:")
    print(f"  A1 Sharpe delta   {a1:+.4f}   bar: not worse than -0.05   "
          f"{'PASS' if a1 >= -0.05 else 'FAIL'}")
    print(f"  A2 MaxDD delta    {a2:+.4f}   bar: not worse than -0.02   "
          f"{'PASS' if a2 >= -0.02 else 'FAIL'}")
    print(f"  A3 turnover rel   {t_rel:+.1%}  trigger: >+25% -> retest at 2x cost   "
          f"{'not triggered' if t_rel <= 0.25 else 'TRIGGERED'}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "arms": [{"key": a, "freq": f, "mode": m, "note": n} for a, f, m, n in ARMS],
        "per_sleeve": stats, "blend": blend_stats, "tests": tests,
        "bar": {"a1_sharpe_delta": a1, "a2_maxdd_delta": a2,
                "a3_turnover_relative": t_rel},
        "window": {"start": str(common.min().date()), "end": str(common.max().date()),
                   "sessions": int(len(common))},
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
