"""WS6d — run the frozen exclusion study once and write its results.

Registration: ``C:\\dev\\KICKOFF_ws6d-overbought-exclusion.md``, FROZEN at
vault-docs ``37022b7`` on 2026-09-10 before this file existed.

One cell per arm, run once (§4.4). Every cell computed is written to the results
file, so a later reader can count them and see that no menu was swept.

Writes ONLY ``data_local/ws6d/results.json``. No deployed surface, no ``docs/``,
no dashboard, and nothing under ``data/``.

Run:
  python scripts/run_ws6d_study.py --smoke      # 25 paths, labelled NOT registered
  python scripts/run_ws6d_study.py              # the registered 1,000-path run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from single_name_impl import (  # noqa: E402
    ARM_BY_ID,
    SINGLE_NAMED_LINES,
    build_arm_name_weights,
    build_name_return_panel,
    deployed_sector_layer,
    load_constituents,
    load_member_weights,
    precompute_member_signals,
)
from run_ws6_single_name import load_or_fetch_member_prices  # noqa: E402
from ws6b_friction import FULL_11, PARTIAL_5, restricted_to  # noqa: E402
from ws6c_screened import screened_basket_fn  # noqa: E402
from ws6d_exclusion_study import (  # noqa: E402
    BINDING_COST_BPS,
    COST_SWEEP_BPS,
    PLACEBO_PATHS,
    PLACEBO_SEED,
    WINDOW_END,
    LineWeekPlan,
    concentration,
    constants,
    episode_attribution,
    gate,
    gross_and_turnover,
    net_daily,
    path_seeds,
    placebo_rebalance_rows,
    recording_basket_fn,
    rows_to_panel,
    seen_data_caveat,
    summarise_arm,
    thinness,
    verdict,
    weekly_returns_from_daily,
)

OUT_PATH = PROJECT_ROOT / "data_local" / "ws6d" / "results.json"

UNIVERSES = {"PARTIAL-5": PARTIAL_5, "FULL-11": FULL_11}
PRIMARY_UNIVERSE = "PARTIAL-5"


def _engine_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=PROJECT_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def load_inputs() -> dict:
    """The WS6 register inputs at the frozen window. No live end date anywhere.

    ``deployed_sector_layer`` and ``load_or_fetch_member_prices`` both default to
    the frozen ``WINDOW_END``; that default is the point. The A3 weight table has
    since been extended past the window by the WS6b and WS6c publishers, which is
    a superset and never a rewrite of the in-window entries, so the register-window
    baskets are unaffected — ``snapshot_asof`` only ever reads entries at or
    before the effective date.
    """
    sector = deployed_sector_layer(window_end=WINDOW_END)
    prices_by_line, _m, _f, resolution = load_or_fetch_member_prices(
        SINGLE_NAMED_LINES)
    membership = {L: load_constituents(L)["snapshots"] for L in SINGLE_NAMED_LINES}
    signals = {L: precompute_member_signals(prices_by_line[L])
               for L in SINGLE_NAMED_LINES}
    weights = {L: load_member_weights(L) for L in SINGLE_NAMED_LINES}
    combined = pd.concat([prices_by_line[L] for L in SINGLE_NAMED_LINES], axis=1)
    combined = combined.loc[:, ~combined.columns.duplicated(keep="first")]
    returns = build_name_return_panel(sector["closes"], combined)
    return {"sector": sector, "prices": prices_by_line, "membership": membership,
            "signals": signals, "weights": weights, "resolution": resolution,
            "returns": returns}


def build_arm(inp: dict, arm_id: str, adopted, basket_fn=None):
    sector = inp["sector"]
    args = (ARM_BY_ID[arm_id], sector["weights"], sector["closes"],
            sector["rebal_dates"], sector["eligible"], inp["membership"],
            inp["signals"], inp["prices"])
    kw = dict(member_resolution=inp["resolution"], member_weights=inp["weights"])
    if basket_fn is not None:
        kw["basket_fn"] = basket_fn
    if adopted is None:
        return build_arm_name_weights(*args, **kw)
    with restricted_to(adopted):
        return build_arm_name_weights(*args, **kw)


def make_plans(i1_sink: dict, i1x_sink: dict, sector: dict,
               adopted: tuple[str, ...]) -> list[LineWeekPlan]:
    """One plan per basketed line-week, from the single I1 and I1X passes.

    ``k_excluded`` is the count §3.3 actually removed. Note the symmetry it
    produces and which the design depends on: the placebo keeps
    ``len(i1_basket) - k`` names, exactly as I1X does, so MIN_PASS fires on the
    same line-weeks in both arms and the two never disagree about WHETHER a line
    is basketed — only about which names it holds.
    """
    plans: list[LineWeekPlan] = []
    for (line, rd), sel in i1x_sink.items():
        if line not in adopted:
            continue
        i1 = i1_sink.get((line, rd))
        if i1 is None:
            continue
        lw = float(sector["weights"].loc[rd].get(line, 0.0))
        if lw <= 0:
            continue
        # When I1 itself reverted — fewer than MIN_PASS passed the state screen,
        # as on IUUS in the March 2020 crash — there is no basket to exclude
        # from, and I1, I1X and the placebo are all on the ETF. The exclusion is
        # still RECORDED for that line-week, because §3.3 computes it before the
        # valve, so k must be zeroed here rather than carried into a plan whose
        # basket is empty. I1X cannot avoid the same revert: its survivors are a
        # subset of I1's passing set, so a set too small for I1 is too small for
        # it.
        k = 0 if i1.fallback else len(sel.overbought_excluded)
        plans.append(LineWeekPlan(
            line=line, rebal_date=rd, line_weight=lw,
            i1_weights=i1.weights, i1x_weights=sel.basket.weights,
            k_excluded=k,
            i1_fallback=i1.fallback, i1x_fallback=sel.basket.fallback))
    return plans


def run_universe(inp: dict, name: str, n_paths: int) -> dict:
    """Every cell for one universe. Costs are derived from one simulation per
    arm and per path, so the sweep adds no re-simulation."""
    t0 = time.time()
    sector = inp["sector"]
    adopted = UNIVERSES[name]
    i1_sink: dict = {}
    i1x_sink: dict = {}

    e0 = build_arm(inp, "E0", None)
    i0 = build_arm(inp, "I0", adopted)
    i1 = build_arm(inp, "I1", adopted, basket_fn=recording_basket_fn(i1_sink))
    i1x = build_arm(inp, "I1", adopted, basket_fn=screened_basket_fn(i1x_sink))
    print(f"  [{name}] four register arms built ({time.time() - t0:.0f}s)",
          flush=True)

    plans = make_plans(i1_sink, i1x_sink, sector, adopted)
    fired = sum(1 for p in plans if p.k_excluded > 0)
    dropped = sum(p.k_excluded for p in plans)
    print(f"  [{name}] {len(plans)} basketed line-weeks; the exclusion fired on "
          f"{fired}, dropping {dropped} name-weeks", flush=True)

    arm_panels = {"E0": e0.name_weights, "I0": i0.name_weights,
                  "I1": i1.name_weights, "I1X": i1x.name_weights}
    arm_gt = {a: gross_and_turnover(p, inp["returns"])
              for a, p in arm_panels.items()}

    # --- the placebo: 1,000 paths, one simulation each ----------------------
    t1 = time.time()
    placebo_gt = []
    for i, rng in enumerate(path_seeds(n_paths, PLACEBO_SEED)):
        rows = placebo_rebalance_rows(plans, sector["weights"],
                                      sector["rebal_dates"], adopted, rng)
        panel = rows_to_panel(rows, sector["closes"].index,
                              sector["rebal_dates"], sector["eligible"])
        placebo_gt.append(gross_and_turnover(panel, inp["returns"]))
        if (i + 1) % 100 == 0:
            print(f"  [{name}] placebo {i + 1}/{n_paths} "
                  f"({time.time() - t1:.0f}s)", flush=True)

    out: dict = {"universe": name, "n_basketed_line_weeks": len(plans),
                 "exclusion_fired_line_weeks": fired,
                 "name_weeks_dropped": dropped, "cells": {}}

    for bps in COST_SWEEP_BPS:
        arms = {a: weekly_for_gt(gt, inp, bps) for a, gt in arm_gt.items()}
        pl = [weekly_for_gt(gt, inp, bps) for gt in placebo_gt]
        pl_df = pd.DataFrame({i: s for i, s in enumerate(pl)})
        pl_median = pl_df.median(axis=1)

        null_mdd = [summarise_arm(s)["max_drawdown"] for s in pl]
        null_dsd = [summarise_arm(s)["downside_deviation"] for s in pl]
        i1x_sum = summarise_arm(arms["I1X"])

        g1 = gate("G1_max_drawdown", i1x_sum["max_drawdown"], null_mdd)
        g2 = gate("G2_downside_deviation", i1x_sum["downside_deviation"],
                  null_dsd)
        attrib = episode_attribution(list(arms["I0"]), list(arms["I1X"]),
                                     list(pl_median))
        thin = thinness(attrib)

        out["cells"][f"{bps}bps"] = {
            "cost_bps": bps,
            "arms": {a: summarise_arm(s) for a, s in arms.items()},
            "placebo": {
                "n_paths": len(pl),
                "max_drawdown": {"p10": float(pd.Series(null_mdd).quantile(0.10)),
                                 "p50": float(pd.Series(null_mdd).quantile(0.50)),
                                 "p90": float(pd.Series(null_mdd).quantile(0.90))},
                "downside_deviation": {
                    "p10": float(pd.Series(null_dsd).quantile(0.10)),
                    "p50": float(pd.Series(null_dsd).quantile(0.50)),
                    "p90": float(pd.Series(null_dsd).quantile(0.90))},
            },
            "G1": g1, "G2": g2,
            "G3_episodes": attrib, "G3_thinness": thin,
            "verdict": verdict(g1, g2, thin),
            # §5.5 descriptive, carrying its caveat wherever it is read.
            "descriptive_i1x_vs_i0": {
                "max_drawdown_diff_pp": (
                    (i1x_sum["max_drawdown"]
                     - summarise_arm(arms["I0"])["max_drawdown"]) * 100.0),
                "caveat": seen_data_caveat(),
            },
        }
        print(f"  [{name}] {bps}bps -> {out['cells'][f'{bps}bps']['verdict']['outcome']}"
              f" (G1 {g1['percentile_rank']:.3f}, G2 {g2['percentile_rank']:.3f},"
              f" thin {thin['thin']})", flush=True)

    out["concentration"] = {
        "I1_mean_effective_n": _mean_eff_n(plans, "i1_weights"),
        "I1X_mean_effective_n": _mean_eff_n(plans, "i1x_weights"),
    }
    out["seconds"] = round(time.time() - t0, 1)
    return out


def weekly_for_gt(gt, inp: dict, cost_bps: float) -> pd.Series:
    gross, turn = gt
    return weekly_returns_from_daily(net_daily(gross, turn, cost_bps),
                                     inp["sector"]["rebal_dates"])


def _mean_eff_n(plans: list[LineWeekPlan], attr: str) -> float | None:
    vals = [concentration(getattr(p, attr))["effective_n"] for p in plans
            if getattr(p, attr)]
    vals = [v for v in vals if v is not None]
    return float(sum(vals) / len(vals)) if vals else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="25 placebo paths; the output is labelled NOT the "
                         "registered run and must never be reported as it")
    ap.add_argument("--paths", type=int, default=None,
                    help="override the placebo path count (see --smoke)")
    args = ap.parse_args()

    n_paths = 25 if args.smoke else (args.paths or PLACEBO_PATHS)
    registered = (n_paths == PLACEBO_PATHS)
    print(f"WS6d — {constants()['registration']} @ "
          f"{constants()['registration_frozen_at']}")
    print(f"placebo paths {n_paths} (seed {PLACEBO_SEED}) | "
          f"{'REGISTERED RUN' if registered else 'NOT the registered run'}")
    print(seen_data_caveat())

    inp = load_inputs()
    print(f"inputs loaded: {len(inp['sector']['rebal_dates'])} rebalances, "
          f"window to {inp['sector']['closes'].index.max().date()}", flush=True)

    results = {
        "_README": ("WS6d results. One cell per arm, run once; every cell "
                    "computed is listed here so a later reader can count them "
                    "and confirm no menu was swept. The verdict reads the "
                    "placebo gates and the thinness gate only — the "
                    "I1X-versus-I0 figure is descriptive and carries its "
                    "seen-data caveat inline."),
        "constants": constants(),
        "registered_run": registered,
        "placebo_paths_used": n_paths,
        "primary_universe": PRIMARY_UNIVERSE,
        "binding_cost_bps": BINDING_COST_BPS,
        "engine_commit": _engine_commit(),
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "universes": {},
    }
    for name in UNIVERSES:
        results["universes"][name] = run_universe(inp, name, n_paths)

    headline = (results["universes"][PRIMARY_UNIVERSE]["cells"]
                [f"{BINDING_COST_BPS}bps"]["verdict"])
    results["headline_verdict"] = headline
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str),
                        encoding="utf-8")
    print(f"\nwrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"HEADLINE ({PRIMARY_UNIVERSE}, {BINDING_COST_BPS}bps): "
          f"{headline['outcome']} — {headline['reason']}")
    if not registered:
        print("NOT THE REGISTERED RUN — do not report this as the study result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
