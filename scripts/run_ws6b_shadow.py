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
from datetime import date, datetime, timedelta, timezone
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
    rulings,
    rulings_line,
    shadow_status,
    strict_snapshot_fallbacks,
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


# Arming facts, for the log header's week arithmetic. Both dates are asserted
# against a date library below rather than trusted — a mis-stated weekday here
# would mis-date the whole 8-week count and nothing downstream would catch it.
MANUAL_FIRE_DATE = date(2026, 9, 9)          # Wednesday, the first-run-clean fire
FIRST_SCHEDULED_FIRE = date(2026, 9, 12)     # Saturday 17:30 SGT, armed 2026-09-09
NORGATE_SOAK_CLOSE = date(2026, 8, 7)        # registration bar (d)


def load_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    return json.loads(LOG_PATH.read_text(encoding="utf-8"))["weeks"]


def schedule_header(weeks: list[dict]) -> dict:
    """Week numbering against bar (b), computed rather than asserted.

    Bar (b) wants 8 CONSECUTIVE publishable weeks, so the count is anchored on
    the first week actually in the log; a run that later breaks and restarts
    moves the finish line, which ``--status`` reports and this header does not
    pretend to predict. Weekdays are asserted, not remembered: Python's
    ``weekday()`` is Monday=0, so Friday is 4 and Saturday is 5.
    """
    assert MANUAL_FIRE_DATE.weekday() == 2, "manual fire is a Wednesday"
    assert FIRST_SCHEDULED_FIRE.weekday() == 5, "scheduled fire is a Saturday"
    hdr = {
        "manual_first_fire": (f"{MANUAL_FIRE_DATE.isoformat()} "
                              f"({MANUAL_FIRE_DATE:%A}) — first-run-clean"),
        "first_scheduled_fire": (f"{FIRST_SCHEDULED_FIRE.isoformat()} "
                                 f"({FIRST_SCHEDULED_FIRE:%A}) 17:30 SGT"),
        "required_consecutive_weeks": 8,
        "bar_d_earliest": (f"{NORGATE_SOAK_CLOSE.isoformat()} Norgate soak "
                           "close — passed"),
    }
    if not weeks:
        hdr["week_1_ending"] = "not yet published"
        return hdr
    w1 = date.fromisoformat(weeks[0]["week_ending"])
    w8 = w1 + timedelta(weeks=7)
    # Week 8's Friday is published by the Saturday fire the day after it.
    verdict = w8 + timedelta(days=1)
    assert w1.weekday() == 4, "shadow weeks end on a Friday (W-FRI, frozen)"
    hdr["week_1_ending"] = f"{w1.isoformat()} ({w1:%A})"
    hdr["week_8_ending"] = f"{w8.isoformat()} ({w8:%A})"
    hdr["earliest_verdict_date"] = (
        f"{max(verdict, NORGATE_SOAK_CLOSE).isoformat()} ({verdict:%A}) — the "
        "fire that publishes week 8, assuming no week is refused; every "
        "refused week resets the run and moves this out")
    return hdr


def save_log(weeks: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(
        {"_README": ("WS6b shadow log. Append-only and hash-chained: each "
                     "record seals its predecessor, so an altered or reordered "
                     "week is detected rather than silently counted toward "
                     "bar (b). Never hand-edit."),
         "_rulings_in_force": rulings(),
         "_schedule": schedule_header(weeks),
         "updated_utc": datetime.now(timezone.utc).isoformat(),
         "weeks": weeks}, indent=2), encoding="utf-8")


def _snapshot_at(snapshots: dict, t_minus_1) -> str:
    """The newest entry a t-1 read may consume, as an ISO date, or ``"none"``.

    Serves both halves of the SS6.3 check, whose keys are NOT the same type:
    ``load_constituents`` is keyed by ISO date STRING and ``load_member_weights``
    by ``pd.Timestamp``. Normalising to the first ten characters covers both —
    a bare ``str()`` on a Timestamp yields "2026-07-10 00:00:00", which sorts
    correctly but is not an ISO date and raises straight out of
    ``date.fromisoformat`` in the guard downstream.
    """
    cut = t_minus_1.isoformat()
    usable = [str(d)[:10] for d in snapshots if str(d)[:10] <= cut]
    return max(usable) if usable else "none"


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

    week_ending = rebal[-1]
    # SS6.3 STRICT (ZH, 2026-09-09). The snapshot a t-1 read actually consumes
    # is the newest one dated on or before the session BEFORE the rebalance —
    # not the newest one in the series. By the Saturday this week is computed
    # the current Friday's snapshot usually exists, so reporting max(snapshots)
    # would overstate freshness by exactly the week being measured, and under
    # an iShares outage it would hide the carry-forward entirely.
    prior_sessions = closes.index[closes.index < week_ending]
    t_minus_1 = (prior_sessions[-1] if len(prior_sessions) else week_ending).date()
    snapshot_used = {L: _snapshot_at(membership[L], t_minus_1) for L in PARTIAL_5}
    # Both halves, per the registration's "missing snapshot OR WEIGHTS" clause.
    # They come from different fetch routes and fail independently: membership
    # through the JSON product-data API, weights through the CSV holdings
    # endpoint. As at 2026-09-09 the second is walled and the first is not.
    weights_used = {L: _snapshot_at(weights[L], t_minus_1) for L in PARTIAL_5}
    stale_lines = strict_snapshot_fallbacks(snapshot_used, weights_used,
                                            t_minus_1)
    # A stale line is dropped from the week's adopted set, which is how the
    # registered fallback is actually EFFECTED rather than merely labelled: a
    # line outside the restriction stays as its ETF, so the logged reversion and
    # the computed return say the same thing.
    adopted_this_week = tuple(L for L in PARTIAL_5 if L not in stale_lines)

    e0, i0 = _build(None), _build(adopted_this_week)
    e0_daily = simulate_arm(e0.name_weights, returns, 0.0)["daily"]
    i0_daily = simulate_arm(i0.name_weights, returns, 0.0)["daily"]

    i0_r, e0_r, gap = weekly_gap_from_daily(i0_daily, e0_daily, week_ending)

    led = trade_ledger(i0.name_weights, rebal)
    turnover = float(led[led["date"] == week_ending]["abs_delta"].sum())

    row = sector["weights"].loc[week_ending]
    held = [L for L in row.index if float(row.get(L, 0)) > 0]
    line_w = {L: float(row[L]) for L in held}
    basketed_eligible = [L for L in adopted_this_week if L in held]

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
    # Two ways a held line can end the week on the ETF: the builder reverted it
    # (empty reconstructed basket), or the SS6.3 STRICT ruling withheld it for a
    # stale snapshot. Both are the same registered valve and are logged as one
    # list, because the verdict cares how many line-weeks ran on the valve, not
    # which of the two reasons put them there.
    fallbacks = sorted({L for L in basketed_eligible if not baskets[L]}
                       | {L for L in stale_lines if L in held})
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
            # Every adopted line, held or not, and the snapshot the t-1 read
            # would consume — not the newest in the series. Recorded for all
            # five because basketed_eligible now EXCLUDES the stale lines, and
            # those are precisely the rows a later reader needs to see.
            snapshot_dates={L: snapshot_used[L] for L in PARTIAL_5},
            weights_dates={L: weights_used[L] for L in PARTIAL_5},
            data_asof=str(closes.index.max().date()),
            engine_commit=_engine_commit(), params_sha=_params_sha(),
            rulings=rulings()),
        "line_weights": line_w,
        "basket_weights": baskets,
        "e0_total_weight": float(row.sum()),
        "t_minus_1": str(t_minus_1),
        "stale_snapshot_lines": stale_lines,
        "adopted_this_week": list(adopted_this_week),
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

    # The rulings print on every run, above the numbers they govern. SS6 left
    # three readings open; a log line that does not name the one applied cannot
    # be audited at T4 by anyone who was not in the room when it was ruled.
    print(f"\n{rulings_line()}")
    print(f"week ending {week.week_ending} (t-1 read {built['t_minus_1']}) | "
          f"I0 {week.i0_return:+.4%} E0 {week.e0_return:+.4%} | "
          f"gap {week.gap*1e4:+.1f}bp | turnover {week.turnover_i0:.4f}")
    stale = built["stale_snapshot_lines"]
    print(f"  SS6.3 STRICT: adopted this week {built['adopted_this_week']}"
          f" | withheld as stale: {stale or 'none'}")
    # Membership and weights printed SEPARATELY. They fail independently, and a
    # single as-of line would have concealed the state this shadow was armed in.
    for L in sorted(week.snapshot_dates):
        mark = " <-- WITHHELD" if L in stale else ""
        print(f"    {L:6} membership {week.snapshot_dates[L]:>10} | "
              f"weights {week.weights_dates.get(L, 'none'):>10}{mark}")
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
