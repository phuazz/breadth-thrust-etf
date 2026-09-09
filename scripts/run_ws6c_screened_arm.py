"""WS6c — weekly screened-arm publisher (zero-touch, observational).

Registration: ``C:\\dev\\KICKOFF_ws6c-screened-arm.md``, FROZEN at vault-docs
``cc84122`` (2026-09-09). Reads the latest WS6b shadow week, recomputes E0, I0
and the screened arm I1X for that week from the SAME inputs, runs the §6 guards
and appends to its own hash-chained log. It writes nothing else: no dashboard,
no email, no factsheet, no ``docs/`` output. The record is private to the
register (§7).

Chained after ``run_ws6b_shadow.py`` in ``scripts/run_ws6b_shadow.bat``, same
environment (``BTE_PRICE_SOURCE=norgate``, main tree). Adding that step is not a
change to the WS6b registration (§7).

WHAT THIS PUBLISHER MAY NOT DO, restated where it would be edited:
  * It never touches the WS6b log, ``ws6b_shadow.py``, ``run_ws6b_shadow.py`` or
    ``data/ws6b_params.json``. It reads the first and the last of those.
  * It never re-decides the SS6.3 withholding. The basketed set is exactly the
    WS6b record's ``lines_basketed``, imposed through ``restricted_to``, so
    whatever missing-snapshot semantics WS6b applied that week are inherited.
  * It never backfills. §7: if the publisher was not committed before a fire,
    the record starts at the first fire after it was, and earlier WS6b weeks are
    not backfilled — a backfilled week is not a live week and the caches it
    would read have moved. ``--week-ending`` exists only to be refused.

Run:
  python scripts/run_ws6c_screened_arm.py --dry-run   # compute and guard, write nothing
  python scripts/run_ws6c_screened_arm.py             # publish the week
  python scripts/run_ws6c_screened_arm.py --status    # the §4 measures so far
  python scripts/run_ws6c_screened_arm.py --minutes 12  # §4.2 operator minutes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from fetch_ws6_weights import build_line  # noqa: E402
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
from ws6b_shadow import ShadowWeek, weekly_gap_from_daily  # noqa: E402
from ws6b_shadow import verify_log_chain as verify_ws6b_chain  # noqa: E402
from ws6c_screened import (  # noqa: E402
    BASE_ARM,
    UNSCREENED_ARM,
    ScreenedWeek,
    append_week,
    constants,
    constants_line,
    evaluate_week,
    line_identity,
    name_week_returns,
    screened_basket_fn,
    selection_record,
    status,
    verify_log_chain,
    within_line_weights,
)

LOG_PATH = PROJECT_ROOT / "data_local" / "ws6c" / "screened_log.json"
MINUTES_PATH = PROJECT_ROOT / "data_local" / "ws6c" / "operator_minutes.json"
WS6B_LOG_PATH = PROJECT_ROOT / "data_local" / "ws6b" / "shadow_log.json"
PARAMS_PATH = PROJECT_ROOT / "data" / "ws6b_params.json"

# §5 schedule facts, asserted against a date library below rather than
# remembered. Python's weekday() is Monday=0, so Friday is 4 and Saturday is 5;
# datetime months are 1-INDEXED.
FIRST_SCHEDULED_FIRE = date(2026, 9, 12)     # Saturday 17:30 SGT
INTENDED_WEEK_1 = date(2026, 9, 11)          # Friday


def _engine_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=PROJECT_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001 — a missing git is not a reason to fail
        return "unknown"


def _params_sha() -> str:
    return hashlib.sha256(PARAMS_PATH.read_bytes()).hexdigest()[:16]


# --- log I/O ---------------------------------------------------------------

def load_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    return json.loads(LOG_PATH.read_text(encoding="utf-8"))["weeks"]


def load_ws6b_log() -> list[dict]:
    if not WS6B_LOG_PATH.exists():
        return []
    return json.loads(WS6B_LOG_PATH.read_text(encoding="utf-8"))["weeks"]


def schedule_header(weeks: list[dict]) -> dict:
    """The §7 log header. The START WEEK is recorded because it is not knowable
    in advance: the arm's first counted week is the first WS6b published week for
    which this publisher, committed with tests, also ran, and if that is not the
    intended 2026-09-11 the difference has to be visible in the record rather
    than reconstructed later."""
    assert FIRST_SCHEDULED_FIRE.weekday() == 5, "the fire is a Saturday"
    assert INTENDED_WEEK_1.weekday() == 4, "screened weeks end on a Friday"
    hdr = {
        "registration": ("KICKOFF_ws6c-screened-arm.md, frozen at vault-docs "
                         "cc84122 (2026-09-09)"),
        "arm": "I1X — screened PARTIAL-5 basket, observational",
        "intended_week_1_ending": (f"{INTENDED_WEEK_1.isoformat()} "
                                   f"({INTENDED_WEEK_1:%A})"),
        "first_scheduled_fire": (f"{FIRST_SCHEDULED_FIRE.isoformat()} "
                                 f"({FIRST_SCHEDULED_FIRE:%A}) 17:30 SGT"),
        "backfill": "none, ever (§7)",
    }
    if not weeks:
        hdr["start_week_ending"] = "not yet published"
        return hdr
    w1 = date.fromisoformat(weeks[0]["week_ending"])
    assert w1.weekday() == 4, "screened weeks end on a Friday (W-FRI, frozen)"
    hdr["start_week_ending"] = f"{w1.isoformat()} ({w1:%A})"
    if w1 != INTENDED_WEEK_1:
        hdr["start_week_note"] = (
            f"started at {w1.isoformat()}, not the intended "
            f"{INTENDED_WEEK_1.isoformat()}: the publisher was not committed "
            "before that fire, so per §7 the record starts at the first fire "
            "after it was and the earlier WS6b weeks are NOT backfilled")
    return hdr


def save_log(weeks: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(
        {"_README": ("WS6c screened-arm log (I1X). Observational: descriptive "
                     "figures only, no bar and no verdict at any n, and no "
                     "adoption path (§1). Append-only and hash-chained, with "
                     "the frozen §3 construction sealed inside every record so "
                     "a changed parameter breaks the chain rather than passing "
                     "quietly. Never hand-edit."),
         "_constants_in_force": constants(),
         "_schedule": schedule_header(weeks),
         "updated_utc": datetime.now(timezone.utc).isoformat(),
         "weeks": weeks}, indent=2), encoding="utf-8")


def load_minutes() -> dict:
    if not MINUTES_PATH.exists():
        return {}
    return json.loads(MINUTES_PATH.read_text(encoding="utf-8")).get("minutes", {})


def save_minutes(minutes: dict) -> None:
    """§4.2 operator minutes: separate from the record and UN-HASHED.

    Deliberately outside the chain. Minutes are a measured human quantity filled
    in after the fact, sometimes days later; putting them inside the hashed
    payload would mean either sealing the week before they exist or rewriting a
    sealed record to add them, and the second is exactly what the chain is for
    preventing.
    """
    MINUTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    MINUTES_PATH.write_text(json.dumps(
        {"_README": ("WS6c §4.2 operator minutes, keyed by week_ending. "
                     "Un-hashed and separate from screened_log.json by design. "
                     "A week absent here is 'not logged', NEVER zero — an "
                     "unmeasured week recorded as 0 would understate the mean."),
         "minutes": minutes}, indent=2), encoding="utf-8")


def minutes_for(week_ending: str, minutes: dict) -> str:
    """§4.2: an unfilled week reads 'not logged', never 0."""
    return (f"{minutes[week_ending]}" if week_ending in minutes else "not logged")


# --- the week ---------------------------------------------------------------

def compute_week(window_end: pd.Timestamp, ws6b_rec: dict) -> dict:
    """Recompute E0, I0 and I1X for the WS6b record's week, same inputs.

    The same calls the WS6b publisher makes, in the same order, so the two arms
    see one set of inputs and the pairing guard has something real to test:
    ``build_line`` per PARTIAL-5 line with the live ``window_end``,
    ``load_or_fetch_member_prices`` to that window, ``load_constituents``,
    ``precompute_member_signals``, ``load_member_weights`` and
    ``deployed_sector_layer``. ``window_end`` must NOT be the frozen WS6
    ``WINDOW_END``, for the reason recorded in the WS6b publisher: clipping a
    live week to the study end reverts every basketed line and the arm measures
    nothing.
    """
    sector = deployed_sector_layer(window_end=window_end)
    closes, rebal = sector["closes"], sector["rebal_dates"]
    # Repeating the WS6b publisher's own call, minutes after it, is nearly free:
    # build_line's resume path returns immediately once every in-window snapshot
    # is either in the table or already recorded under source.fetch_failed, so
    # the chained run does not pay the throttled iShares fetch a second time. It
    # is repeated rather than skipped because "same inputs" has to be a property
    # of this publisher, not an assumption about what ran before it.
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

    week_ending = rebal[-1]
    if str(week_ending.date()) != ws6b_rec["week_ending"]:
        raise SystemExit(
            f"REFUSED: the deployed calendar's newest rebalance is "
            f"{week_ending.date()} but the WS6b record being paired to is "
            f"{ws6b_rec['week_ending']}. The screened arm computes only the "
            "week WS6b published, from the same inputs; a mismatch means the "
            "two are not looking at the same book.")

    # The basketed set is the WS6b record's, never re-decided (§3.6). Every
    # other line is its ETF, which is what restricted_to expresses.
    basketed = tuple(ws6b_rec.get("lines_basketed", []))
    sink: dict = {}

    def _build(arm_id, adopted, basket_fn=None):
        kw = dict(member_resolution=resolution, member_weights=weights)
        if basket_fn is not None:
            kw["basket_fn"] = basket_fn
        args = (ARM_BY_ID[arm_id], sector["weights"], closes, rebal,
                sector["eligible"], membership, signals, prices_by_line)
        if adopted is None:
            return build_arm_name_weights(*args, **kw)
        with restricted_to(adopted):
            return build_arm_name_weights(*args, **kw)

    e0 = _build("E0", None)
    i0 = _build(UNSCREENED_ARM, basketed)
    i1x = _build(BASE_ARM, basketed, basket_fn=screened_basket_fn(sink))

    e0_daily = simulate_arm(e0.name_weights, returns, 0.0)["daily"]
    i0_daily = simulate_arm(i0.name_weights, returns, 0.0)["daily"]
    i1x_daily = simulate_arm(i1x.name_weights, returns, 0.0)["daily"]

    i1x_r, e0_r, gap_i1x_e0 = weekly_gap_from_daily(i1x_daily, e0_daily,
                                                    week_ending)
    i0_r, _e0_again, _ = weekly_gap_from_daily(i0_daily, e0_daily, week_ending)

    def _turnover(build):
        led = trade_ledger(build.name_weights, rebal)
        return float(led[led["date"] == week_ending]["abs_delta"].sum())

    row = sector["weights"].loc[week_ending]
    held = [L for L in row.index if float(row.get(L, 0)) > 0]
    line_w = {L: float(row[L]) for L in held}

    # --- the book being formed this week (selection, keyed to week_ending) ---
    selections = {L: sink[(L, week_ending)] for L in basketed
                  if (L, week_ending) in sink}
    selection = {L: selection_record(s) for L, s in selections.items()}
    screen_fallback = sorted(L for L, s in selections.items()
                             if s.fallback and s.i0_weights)
    # §6.3(iii): a line the WS6b week carries on its ETF is on its ETF here too.
    inert = sorted(L for L in PARTIAL_5 if L in held and L not in basketed)

    # --- the book that EARNED this week (attribution, §3.5) -----------------
    # simulate_arm has yesterday's weights earn today's return, so the whole
    # measured week ran on the last rebalance STRICTLY BEFORE week_ending.
    # Decomposing the week against the basket selected at week_ending would
    # explain a week that has not traded yet.
    prior = [d for d in rebal if d < week_ending]
    in_force = prior[-1] if prior else None
    name_week = name_week_returns(returns, week_ending)

    attribution: dict = {}
    attribution_skipped: dict = {}
    if in_force is not None:
        for L in basketed:
            sel = sink.get((L, in_force))
            if sel is None:
                attribution_skipped[L] = "line not held at the in-force rebalance"
            elif not sel.i0_weights:
                attribution_skipped[L] = "inert in force — both arms on the ETF"
            elif sel.fallback:
                attribution_skipped[L] = (
                    f"screen valve in force — I1X on the ETF, I0 basketed "
                    f"({sel.reason})")
            else:
                attribution[L] = line_identity(sel, name_week)

    # Reconstruct each basketed line's I1X within-line weights from an ISOLATED
    # book, as the WS6b publisher does: exact, and it handles a name held by two
    # lines without double counting it into either basket. Reconstructing from
    # the panel rather than from the selection is deliberate — a guard fed by
    # the function it is guarding tests nothing.
    line_codes = set(sector["weights"].columns)
    baskets: dict[str, dict[str, float]] = {}
    for L in basketed:
        if line_w.get(L, 0.0) <= 0:
            continue
        b = _build(BASE_ARM, (L,), basket_fn=screened_basket_fn({}))
        bw = within_line_weights(b.name_weights.loc[week_ending], line_codes,
                                 line_w[L])
        if bw:
            baskets[L] = bw

    unresolved_shared = sorted(ws6b_rec.get("unresolved_gaps", []))
    unresolved_screen = sorted({n for s in selections.values()
                                for n in s.undefined_strength})

    week = ScreenedWeek(
        week_ending=str(week_ending.date()),
        i1x_return=i1x_r, i0_return=i0_r, e0_return=e0_r,
        gap_i1x_e0=gap_i1x_e0, gap_i1x_i0=i1x_r - i0_r,
        turnover_i1x=_turnover(i1x), turnover_i0=_turnover(i0),
        lines_held=held, lines_basketed=list(basketed),
        lines_inert=inert, lines_screen_fallback=screen_fallback,
        all_inert=not basketed,
        ws6b_i0_return=float(ws6b_rec["i0_return"]),
        ws6b_record_hash=str(ws6b_rec.get("record_hash", "")),
        paired=False, pairing_error=abs(i0_r - float(ws6b_rec["i0_return"])),
        selection=selection, attribution=attribution,
        weights_in_force_from=(str(in_force.date()) if in_force is not None
                               else "none"),
        unresolved_shared=unresolved_shared,
        unresolved_screen_specific=unresolved_screen,
        data_asof=str(closes.index.max().date()),
        engine_commit=_engine_commit(), params_sha=_params_sha(),
        constants=constants())

    return {"week": week, "selections": selections, "line_weights": line_w,
            "basket_weights": baskets, "e0_total_weight": float(row.sum()),
            "attribution_skipped": attribution_skipped}


# --- CLI --------------------------------------------------------------------

def _refuse(msg: str) -> int:
    print(f"REFUSED: {msg}")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and guard the week, write nothing")
    ap.add_argument("--status", action="store_true",
                    help="the §4 measures over the record so far (descriptive)")
    ap.add_argument("--minutes", type=float, default=None,
                    help="§4.2 operator minutes for the latest week; written to "
                         "a separate un-hashed annotation file")
    ap.add_argument("--week-ending", default=None,
                    help="accepted only if it IS the latest WS6b record's week. "
                         "There is no backfill (§7).")
    args = ap.parse_args()

    weeks = load_log()

    if args.status:
        st = status(weeks)
        mins = load_minutes()
        st["operator_minutes"] = {r["week_ending"]: minutes_for(r["week_ending"],
                                                                mins)
                                  for r in weeks}
        print(json.dumps(st, indent=2))
        return 0

    ws6b_weeks = load_ws6b_log()
    if not ws6b_weeks:
        return _refuse(
            f"the WS6b shadow log has no weeks yet "
            f"({WS6B_LOG_PATH.relative_to(PROJECT_ROOT)}"
            f"{' does not exist' if not WS6B_LOG_PATH.exists() else ' is empty'})."
            " The screened arm computes only a week WS6b has published (§6.3(i)),"
            " so there is nothing to pair to. Nothing written.")

    ok, detail = verify_ws6b_chain(
        [{k: v for k, v in r.items() if k in ShadowWeek.__annotations__}
         for r in ws6b_weeks])
    if not ok:
        return _refuse(f"WS6b shadow log integrity FAILED — {detail}. The "
                       "screened week pairs to that record and seals its hash; "
                       "a log that may have been altered cannot be paired to.")

    latest = ws6b_weeks[-1]
    target = latest["week_ending"]

    if args.minutes is not None:
        key = weeks[-1]["week_ending"] if weeks else target
        mins = load_minutes()
        mins[key] = args.minutes
        save_minutes(mins)
        print(f"operator minutes for week {key}: {args.minutes}")
        print(f"wrote {MINUTES_PATH.relative_to(PROJECT_ROOT)} (un-hashed, "
              "separate from the record)")
        return 0

    if args.week_ending and args.week_ending != target:
        return _refuse(
            f"--week-ending {args.week_ending} is not the latest WS6b record "
            f"({target}). There is no backfill (§7): a backfilled week is not a "
            "live week and the caches it would read have moved. Nothing written.")

    if not latest.get("publishable", False):
        return _refuse(
            f"the latest WS6b week {target} is not publishable "
            f"(guard failures: {latest.get('guard_failures', [])}). A screened "
            "week exists only for a WS6b week that passed its guards (§6.3(i)). "
            "Nothing written.")

    ok, detail = verify_log_chain(weeks)
    if not ok:
        return _refuse(f"screened log integrity FAILED — {detail}. No week "
                       "published; the record cannot be extended on a log that "
                       "may have been altered.")
    print(f"log chain: {detail}")

    if any(r["week_ending"] == target for r in weeks):
        return _refuse(f"a screened record already exists for week {target}. "
                       "The log is append-only and a week is computed once. "
                       "Nothing written.")

    window_end = pd.Timestamp(datetime.now(timezone.utc).date())
    built = compute_week(window_end, latest)
    week = built["week"]

    guard = evaluate_week(week, built["selections"], built["line_weights"],
                          built["basket_weights"], built["e0_total_weight"])
    week.paired = guard.checks.get("paired_to_ws6b", {}).get("ok", False)

    # --- the weekly log line ------------------------------------------------
    print(f"\n{constants_line()}")
    print(f"week ending {week.week_ending} "
          f"(weights in force from {week.weights_in_force_from}) | "
          f"I1X {week.i1x_return:+.4%} I0 {week.i0_return:+.4%} "
          f"E0 {week.e0_return:+.4%}")
    print(f"  gaps: I1X-I0 {week.gap_i1x_i0*1e4:+.1f}bp | "
          f"I1X-E0 {week.gap_i1x_e0*1e4:+.1f}bp "
          f"(reference lines only: 66.0bp registered, 42.9bp adopted-set — "
          f"neither is a bar for this arm)")
    print(f"  turnover: I1X {week.turnover_i1x:.4f} | I0 {week.turnover_i0:.4f}")
    print(f"  basketed {week.lines_basketed or 'none'} | "
          f"inert {week.lines_inert or 'none'} | "
          f"screen valve {week.lines_screen_fallback or 'none'}")
    for L in sorted(week.selection):
        s = week.selection[L]
        print(f"    {L:6} k {s['k']:>2} of n {s['n_eligible']:>3} eligible | "
              f"w_x {s['w_x']:.4f} (overbought {s['w_overbought']:.4f}) | "
              f"excluded {s['overbought_excluded'] or 'none'} | "
              f"survivors {len(s['survivors'])}")
    for L in sorted(week.attribution):
        a = week.attribution[L]
        print(f"    {L:6} §3.5 w_x {a['w_x']:.4f} r_S {a['r_s']:+.4%} "
              f"r_x {a['r_x']:+.4%} spread {a['spread']:+.4%} "
              f"residual {a['residual']:.2e}")
    for L, why in sorted(built["attribution_skipped"].items()):
        print(f"    {L:6} §3.5 not applicable: {why}")
    # A non-fatal check that did not pass is a NOTE, not a FAIL. "screen inert"
    # is the largest such case and it is information, not a fault; printing it as
    # FAIL beside a green PUBLISHABLE line would train the reader to skip the
    # word, which is the last thing this log needs.
    blocked = {f.split(":", 1)[0] for f in guard.failures}
    for name, c in guard.checks.items():
        tag = "ok  " if c["ok"] else ("FAIL" if name in blocked else "note")
        print(f"  [{tag}] {name}: {c['detail']}")
    for w in guard.warnings:
        print(f"  [warn] {w}")
    print(f"\n{'PAIRED' if week.paired else 'UNPAIRED — week does not count'}")
    print(f"PUBLISHABLE: {guard.publishable}")

    if args.dry_run:
        print("(dry run — nothing written)")
        return 0 if guard.publishable else 1

    rec = append_week(weeks, week)
    rec[-1]["publishable"] = guard.publishable
    rec[-1]["guard"] = guard.checks
    rec[-1]["guard_failures"] = guard.failures
    rec[-1]["guard_warnings"] = guard.warnings
    save_log(rec)
    st = status(rec)
    print(f"\nwrote {LOG_PATH.relative_to(PROJECT_ROOT)}")
    print(f"published {st['n_published']} week(s), {st['n_in_statistics']} in "
          f"the §4.1 statistics ({st['n_inert']} inert, {st['n_unpaired']} "
          f"unpaired) | citable at {st['citable_at_published_weeks']}")
    print(f"operator minutes this week: "
          f"{minutes_for(week.week_ending, load_minutes())}")
    return 0 if guard.publishable else 1


if __name__ == "__main__":
    raise SystemExit(main())
