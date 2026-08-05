"""WS6b T2 — weekly shadow publisher (zero-touch).

Computes the most recently completed W-FRI week for the signed PARTIAL-5
adoption set alongside the live E0 book, runs the guard layer, and appends the
week to the tamper-evident shadow log. Bar (b) of the registration counts a
week only if the guard passes.

Deployed-pipeline discipline: this reads the deployed sector layer and writes
ONLY to its own log under ``data_local/ws6b/``. It mutates nothing the deployed
pipeline owns and publishes nothing to ``docs/``.

Run:
  python scripts/run_ws6b_shadow.py --dry-run    # compute and guard, write nothing
  python scripts/run_ws6b_shadow.py              # publish the week
  python scripts/run_ws6b_shadow.py --status     # where the shadow stands
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from fetch_ws6_weights import build_line  # noqa: E402
from nyse_sessions import last_completed_session  # noqa: E402
from single_name_impl import (  # noqa: E402
    ARM_BY_ID,
    SINGLE_NAMED_LINES,
    build_arm_name_weights,
    build_name_return_panel,
    deployed_sector_layer,
    load_constituents,
    load_member_weights,
    precompute_member_signals,
    simulate_arm,
)
from run_ws6_single_name import load_or_fetch_member_prices  # noqa: E402
from ws6b_friction import PARTIAL_5, restricted_to, trade_ledger  # noqa: E402
from ws6b_shadow import (  # noqa: E402
    ShadowWeek,
    append_week,
    evaluate_week,
    shadow_status,
    verify_log_chain,
    weekly_gap_from_daily,
)

LOG_PATH = PROJECT_ROOT / "data_local" / "ws6b" / "shadow_log.json"
PARAMS_PATH = PROJECT_ROOT / "data" / "ws6b_params.json"


def _engine_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=PROJECT_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001 — a missing git is not a reason to fail
        return "unknown"


def _params_sha() -> str:
    return hashlib.sha256(PARAMS_PATH.read_bytes()).hexdigest()[:16]


def load_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    return json.loads(LOG_PATH.read_text(encoding="utf-8"))["weeks"]


def save_log(weeks: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(
        {"_README": ("WS6b shadow log. Append-only and hash-chained: each "
                     "record seals its predecessor, so an altered or reordered "
                     "week is detected rather than silently counted toward "
                     "bar (b). Never hand-edit."),
         "updated_utc": datetime.now(timezone.utc).isoformat(),
         "weeks": weeks}, indent=2), encoding="utf-8")


def compute_week(window_end: pd.Timestamp) -> dict:
    """Build the most recently completed W-FRI shadow week from live data.

    ``window_end`` must NOT default to ``single_name_impl.WINDOW_END``. That
    constant is the frozen WS6 study window (2026-06-30) and clipping the
    shadow to it would make every forward week look three weeks stale to the
    capture-integrity guard — which is exactly what happened on the first run,
    and correctly refused to publish.
    """
    sector = deployed_sector_layer(window_end=window_end)
    closes, rebal = sector["closes"], sector["rebal_dates"]
    # The A3 weights and the member universe are frozen at the study window
    # by default. A live shadow week needs both extended to ITS window end —
    # otherwise every basketed line silently reverts to its ETF and the
    # shadow measures nothing (caught by the 2026-08-05 pre-arm dry run:
    # empty baskets, 60 unresolved names, gap exactly 0.0 bp). Weights come
    # from the same raw snapshot cache the deployed pipeline maintains
    # (cache-first, throttled network fallback); member prices from Norgate.
    for L in PARTIAL_5:
        build_line(L, force=False, window_end=window_end)
    (prices_by_line, _m, _f, resolution) = load_or_fetch_member_prices(
        SINGLE_NAMED_LINES, end=window_end)
    membership = {L: load_constituents(L)["snapshots"] for L in SINGLE_NAMED_LINES}
    signals = {L: precompute_member_signals(prices_by_line[L])
               for L in SINGLE_NAMED_LINES}
    weights = {L: load_member_weights(L) for L in SINGLE_NAMED_LINES}

    combined = pd.concat([prices_by_line[L] for L in SINGLE_NAMED_LINES], axis=1)
    combined = combined.loc[:, ~combined.columns.duplicated(keep="first")]
    returns = build_name_return_panel(closes, combined)

    def _build(adopted):
        ctx = restricted_to(adopted) if adopted else None
        if ctx is None:
            return build_arm_name_weights(
                ARM_BY_ID["E0"], sector["weights"], closes, rebal,
                sector["eligible"], membership, signals, prices_by_line,
                member_resolution=resolution, member_weights=weights)
        with ctx:
            return build_arm_name_weights(
                ARM_BY_ID["I0"], sector["weights"], closes, rebal,
                sector["eligible"], membership, signals, prices_by_line,
                member_resolution=resolution, member_weights=weights)

    e0, i0 = _build(None), _build(PARTIAL_5)
    e0_daily = simulate_arm(e0.name_weights, returns, 0.0)["daily"]
    i0_daily = simulate_arm(i0.name_weights, returns, 0.0)["daily"]

    week_ending = rebal[-1]
    i0_r, e0_r, gap = weekly_gap_from_daily(i0_daily, e0_daily, week_ending)

    led = trade_ledger(i0.name_weights, rebal)
    turnover = float(led[led["date"] == week_ending]["abs_delta"].sum())

    row = sector["weights"].loc[week_ending]
    held = [L for L in row.index if float(row.get(L, 0)) > 0]
    line_w = {L: float(row[L]) for L in held}
    basketed_eligible = [L for L in PARTIAL_5 if L in held]

    # Reconstruct each basketed line's within-line weights for the guard, via
    # that line's ISOLATED book. Exact, and it handles a name held by two lines
    # without double counting it into either basket.
    baskets: dict[str, dict[str, float]] = {}
    for L in basketed_eligible:
        with restricted_to((L,)):
            b = build_arm_name_weights(
                ARM_BY_ID["I0"], sector["weights"], closes, rebal,
                sector["eligible"], membership, signals, prices_by_line,
                member_resolution=resolution, member_weights=weights)
        cols = [c for c in b.name_weights.columns if c not in SINGLE_NAMED_LINES]
        w = b.name_weights.loc[week_ending, cols]
        w = w[w > 0]
        baskets[L] = ({n: float(v) / line_w[L] for n, v in w.items()}
                      if line_w[L] > 0 else {})

    # A held, adopted line whose reconstructed basket is EMPTY did not trade
    # as a basket this week — the builder reverted it to its ETF (the
    # registered fallback). Classify from the baskets themselves: the old
    # cumulative-counter test missed a week where EVERY line reverted, so the
    # weight-integrity guard fired on empty baskets instead of the fallback
    # being reported as the resolved, logged outcome it is registered to be.
    fallbacks = [L for L in basketed_eligible if not baskets[L]]
    baskets = {L: w for L, w in baskets.items() if w}
    basketed = [L for L in basketed_eligible if L in baskets]

    unresolved = sorted({n for L in basketed
                         for n in i0.uncovered_seen.get(L, set())
                         | i0.missing_seen.get(L, set())})

    return {
        "week": ShadowWeek(
            week_ending=str(week_ending.date()),
            i0_return=i0_r, e0_return=e0_r, gap=gap, turnover_i0=turnover,
            lines_held=held, lines_basketed=basketed,
            fallback_lines=fallbacks, unresolved_gaps=unresolved,
            corporate_actions=[],
            snapshot_dates={L: (str(max(membership[L])) if membership[L]
                                else "none") for L in basketed_eligible},
            data_asof=str(closes.index.max().date()),
            engine_commit=_engine_commit(), params_sha=_params_sha()),
        "line_weights": line_w,
        "basket_weights": baskets,
        "e0_total_weight": float(row.sum()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and guard the week, write nothing")
    ap.add_argument("--status", action="store_true",
                    help="report where the shadow stands against bar (b)")
    ap.add_argument("--window-end", default=None,
                    help="ISO date; defaults to today. Never the frozen WS6 "
                         "WINDOW_END — see compute_week.")
    args = ap.parse_args()

    weeks = load_log()

    if args.status:
        print(json.dumps(shadow_status(weeks), indent=2))
        return 0

    ok, detail = verify_log_chain(
        [{k: v for k, v in r.items() if k in ShadowWeek.__annotations__}
         for r in weeks])
    if not ok:
        print(f"STOP: shadow log integrity FAILED — {detail}")
        print("No week published. The 8-consecutive-week bar cannot be counted "
              "on a log that may have been altered.")
        return 2
    print(f"log chain: {detail}")

    window_end = (pd.Timestamp(args.window_end) if args.window_end
                  else pd.Timestamp(datetime.now(timezone.utc).date()))
    built = compute_week(window_end)
    week = built["week"]
    # Anchor to the TRUE NYSE calendar, tz-aware: an implicit local clock here
    # would corrupt the capture-integrity verdict the whole guard rests on.
    expected = last_completed_session(datetime.now(timezone.utc))
    guard = evaluate_week(
        week, expected, built["line_weights"], built["basket_weights"],
        built["e0_total_weight"],
        [r["turnover_i0"] for r in weeks if r.get("publishable")])

    print(f"\nweek ending {week.week_ending} | I0 {week.i0_return:+.4%} "
          f"E0 {week.e0_return:+.4%} | gap {week.gap*1e4:+.1f}bp | "
          f"turnover {week.turnover_i0:.4f}")
    for name, c in guard.checks.items():
        if name == "divergence_detail":
            continue
        print(f"  [{'ok ' if c['ok'] else 'FAIL'}] {name}: {c['detail']}")
    for w in guard.warnings:
        print(f"  [warn] {w}")
    print(f"\nPUBLISHABLE: {guard.publishable}")

    if args.dry_run:
        print("(dry run — nothing written)")
        return 0 if guard.publishable else 1

    rec = append_week(weeks, week)
    rec[-1]["publishable"] = guard.publishable
    rec[-1]["guard"] = guard.checks
    rec[-1]["guard_failures"] = guard.failures
    rec[-1]["guard_warnings"] = guard.warnings
    save_log(rec)
    st = shadow_status(rec)
    print(f"\nwrote {LOG_PATH.relative_to(PROJECT_ROOT)}")
    print(f"consecutive publishable weeks: {st['consecutive_publishable']}"
          f"/{st['required']}  | bar (b) met: {st['bar_b_met']}")
    return 0 if guard.publishable else 1


if __name__ == "__main__":
    raise SystemExit(main())
