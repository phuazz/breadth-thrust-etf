"""Post-refresh guard: is the state refresh_all.py just produced coherent?

The vault rule (CLAUDE.md, session discipline): any run whose output is
trusted without a human re-deriving it needs a verification layer that
can catch a silently-wrong step. refresh_all.py is that run — 40+ steps,
~50 minutes, and every panel it writes is trusted downstream by the
dashboard, the factsheet email and the strategy engines. Each step
already fails loudly on its OWN errors; what none of them can see is
cross-panel incoherence: one ETF's fetch quietly serving an older week,
a panel whose endpoint died mid-run, a breadth series that stopped a
session short, or a rewrite that silently DROPPED history that the
previous commit still had (the 2026-08-04 SPY case: a later refresh lost
two sessions and nothing noticed).

This script runs after the refresh, before the operator commits, and
asserts over the whole 24-panel deployed set:

  G1  every constituents panel shares ONE end_friday, equal to the
      expected target Friday (latest completed Friday);
  G2  no panel reports endpoint_health.status != "ok";
  G3  no panel is critically stale (staleness.status == "critical" or
      "no_real_fetches" fails; "warning" warns);
  G4  every breadth panel's end_date is a real session on that ETF's OWN
      trading calendar, and lands in the band the writer is allowed to
      produce: at least the last session on or before the shared
      end_friday, and at most that calendar's last COMPLETED session
      (XETR panels legitimately end on a US-holiday Friday that NYSE
      panels do not, and vice versa — see the 2026-07-03 boundary).

      A BAND, NOT AN EQUALITY, SINCE 2026-08-21. This was an equality
      against the end_friday bound until the 2026-08-15 tail-extension
      landed (register 2026-08-15-breadth-thrust-etf-1, CONFIRMED):
      compute_breadth now takes schedule_end = max(end_friday,
      last_completed_session), optionally capped back down by price
      coverage but never below end_friday. The guard was not updated with
      the writer, so from 2026-08-15 it failed EVERY panel on EVERY run —
      24 of 24 on 2026-08-21, all in the same direction, which is the
      signature of a rule change rather than corruption. A guard that
      fires on every run is a guard nobody reads, which is worse than no
      guard at all.

      The band keeps both failures the equality caught. Below the lower
      bound is a TRUNCATED panel — the silent-data-loss case, and still
      the one that matters most. Above the upper bound is a bar whose
      close has not happened — the partial-bar / look-ahead case. And the
      session-membership test keeps the phantom-bar case (a NYSE-calendar
      panel dated a US holiday) that the old equality caught only as a
      side effect of comparing against a session-derived date;
  G5  no panel lost state versus the previous COMMITTED version (HEAD):
      no constituents snapshot key disappears, breadth n_trading_days
      never decreases, breadth end_date never moves backwards;
  G6  no panel's breadth was computed on a thin vendor download — roster
      coverage (share of CURRENT constituents carrying a 50-day average)
      must clear compute_breadth.MIN_ROSTER_COVERAGE_WARN. That writer
      refuses to write below its own hard floor but only WARNS in the
      band between, and the warn band is not safe to commit: on
      2026-08-08 IDP6 was published at 61.5% and changed Strategy A's
      holdings, keeping IDP6 at 6.3% and ejecting IUMS. Writing a thin
      panel beats writing a stale one; committing one does not, so the
      block belongs here rather than as a stricter floor in the writer;
  W1  (warn only) if EVERY deployed panel's newest snapshot walked back
      from the target Friday, the refresh almost certainly ran before
      iShares published Friday's holdings (the 2026-08-08 lesson).
      Prices/breadth are still Friday-close; the roster is one day
      stale, which is defensible — hence warn, not fail. Re-run
      `fetch_constituents --etf <sym>` later to close the gap, and see
      scripts/measure_publication_lag.py for the lag measurement.

Verdicts: FAIL -> exit 1 (the run must not be trusted or committed as-is),
WARN -> exit 0 with a printed notice, OK -> silence beyond the summary.

Scope note: the 14 EUROPE_SUPERSECTORS_CANDIDATE panels are data-only
captures, so they are deliberately NOT held to the shared end_friday; the
deployed set is refresh_all.ETFS_ALL. Since 2026-08-08 they ARE refreshed
by refresh_all (which walks ETFS_REFRESH = deployed + candidates), so the
reason they sit outside this guard is no longer "nothing refreshes them"
— it is that a screening capture carries no deployment obligation. Keep
this reading ETFS_ALL: widening it to ETFS_REFRESH would put candidates
under the cross-panel alignment checks and fail runs for panels no
strategy trades.

Pure verdict logic lives in the check_* functions and is unit-tested in
tests/test_check_refresh_guard.py; only main() touches disk/git.

Python datetime months are 1-indexed (January = 1). All calendar
arithmetic uses pandas_market_calendars / datetime — never manual
weekday computation.

Usage:
    python scripts/check_refresh_guard.py
    python scripts/check_refresh_guard.py --end-friday 2026-08-07
    python scripts/check_refresh_guard.py --baseline-ref HEAD~1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etf_registry import get_etf  # noqa: E402
from fetch_constituents import latest_completed_friday  # noqa: E402
from refresh_all import ETFS_ALL  # noqa: E402  (single source of truth)
from session_bounds import last_completed_session_on  # noqa: E402
from compute_breadth import (  # noqa: E402  (one floor, one definition)
    MIN_ROSTER_COVERAGE_WARN,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


def verdict(check: str, status: str, evidence: str) -> dict:
    return {"check": check, "status": status, "evidence": evidence}


# ---------------------------------------------------------------------------
# Pure verdict logic — unit-tested offline
# ---------------------------------------------------------------------------
def check_shared_end_friday(end_fridays: dict[str, str],
                            expected: date) -> list[dict]:
    """G1: one end_friday across all panels, equal to ``expected``."""
    distinct = sorted(set(end_fridays.values()))
    out = []
    if len(distinct) != 1:
        by_value = {v: sorted(k for k, vv in end_fridays.items() if vv == v)
                    for v in distinct}
        out.append(verdict(
            "G1 shared end_friday", FAIL,
            f"panels disagree on end_friday: {by_value}"))
    else:
        got = distinct[0]
        if got != expected.isoformat():
            out.append(verdict(
                "G1 shared end_friday", FAIL,
                f"all panels end {got} but the expected target Friday is "
                f"{expected.isoformat()} — the refresh captured the wrong "
                f"week"))
        else:
            out.append(verdict(
                "G1 shared end_friday", OK,
                f"{len(end_fridays)} panels all end {got}"))
    return out


def check_endpoint_health(health: dict[str, str]) -> list[dict]:
    """G2: every panel's endpoint_health.status must be "ok"."""
    bad = {k: v for k, v in health.items() if v != "ok"}
    if bad:
        return [verdict("G2 endpoint health", FAIL,
                        f"panels with unhealthy transport: {bad}")]
    return [verdict("G2 endpoint health", OK,
                    f"{len(health)} panels report ok")]


def check_staleness(staleness: dict[str, str]) -> list[dict]:
    """G3: critical / no-real-fetch staleness fails; warning warns."""
    out = []
    critical = sorted(k for k, v in staleness.items()
                      if v in ("critical", "no_real_fetches"))
    warning = sorted(k for k, v in staleness.items() if v == "warning")
    if critical:
        out.append(verdict("G3 roster staleness", FAIL,
                           f"critically stale panels: {critical}"))
    elif warning:
        out.append(verdict("G3 roster staleness", WARN,
                           f"panels in the warning band: {warning}"))
    else:
        out.append(verdict("G3 roster staleness", OK,
                           f"{len(staleness)} panels fresh"))
    return out


def expected_panel_end(cal_name: str, end_friday: date) -> date:
    """Last session of ``cal_name`` on or before ``end_friday``.

    This is what a breadth panel's end_date must equal after a refresh:
    the target Friday itself on a normal week, or the prior session when
    that Friday is a holiday ON THAT ETF'S OWN CALENDAR (a Friday-holiday
    panel dated Thursday is correct, not stale — cadence rule).
    """
    cal = mcal.get_calendar(cal_name)
    # 10 calendar days comfortably spans any run of weekend + holidays
    # around a single Friday.
    sched = cal.schedule(
        start_date=(end_friday - timedelta(days=10)).isoformat(),
        end_date=end_friday.isoformat(),
    )
    if sched.empty:
        raise RuntimeError(
            f"no {cal_name} session in the 10 days up to {end_friday} — "
            f"calendar data is broken")
    return sched.index[-1].date()


def latest_admissible_panel_end(cal_name: str, now_utc: datetime) -> date | None:
    """Upper G4 bound: the last session on ``cal_name`` that has CLOSED.

    Shares compute_breadth's own definition rather than re-deriving one, so
    the guard and the writer cannot drift apart again — which is exactly how
    G4 came to fail every panel between 2026-08-15 and 2026-08-21.

    None when the calendar yields nothing in the horizon; the caller then
    drops the upper bound rather than failing, matching every other
    last_completed_session_on caller.
    """
    ts = last_completed_session_on(mcal.get_calendar(cal_name), now_utc)
    return None if ts is None else ts.date()


def is_session(cal_name: str, day: date) -> bool:
    """Is ``day`` an actual trading session on ``cal_name``?"""
    sched = mcal.get_calendar(cal_name).schedule(
        start_date=day.isoformat(), end_date=day.isoformat())
    return not sched.empty


def check_breadth_ends(breadth_ends: dict[str, str],
                       calendars: dict[str, str],
                       end_friday: date,
                       now_utc: datetime | None = None) -> list[dict]:
    """G4: each breadth panel ends inside the band the writer may produce.

    ``now_utc`` is injectable so the band is testable; it defaults to the
    wall clock. A guard running after midnight UTC sees a later upper bound
    than the writer did, which can only LOOSEN the check — it cannot
    manufacture a false failure.
    """
    now = now_utc or datetime.now(timezone.utc)
    out = []
    bad = {}
    for etf, got in breadth_ends.items():
        cal_name = calendars.get(etf, "NYSE")
        floor = expected_panel_end(cal_name, end_friday)
        ceiling = latest_admissible_panel_end(cal_name, now)
        try:
            got_date = date.fromisoformat(got)
        except (TypeError, ValueError):
            bad[etf] = f"end_date {got!r} is not an ISO date"
            continue
        if not is_session(cal_name, got_date):
            bad[etf] = f"ends {got}, which is not a {cal_name} session"
        elif got_date < floor:
            bad[etf] = (f"ends {got}, TRUNCATED — must reach at least "
                        f"{floor.isoformat()}")
        elif ceiling is not None and got_date > ceiling:
            bad[etf] = (f"ends {got}, past the last completed {cal_name} "
                        f"session {ceiling.isoformat()} — partial bar")
    if bad:
        out.append(verdict("G4 breadth end dates", FAIL,
                           f"panels outside the admissible band "
                           f"[{end_friday.isoformat()} session .. last "
                           f"completed session]: {bad}"))
    else:
        out.append(verdict("G4 breadth end dates", OK,
                           f"{len(breadth_ends)} panels end on a real session "
                           f"at or after their {end_friday.isoformat()} bound "
                           f"and no later than their last completed session"))
    return out


def panel_roster_coverage(breadth: dict) -> float | None:
    """Share of CURRENT constituents carrying a 50-day average, or None.

    Prefers ``data_quality.roster_coverage_latest``, which compute_breadth
    records from 2026-08-09. Falls back to deriving it from the series,
    because every panel written before that date lacks the field and a
    check that silently skipped 23 of 24 panels would be worse than none.
    """
    dq = breadth.get("data_quality") or {}
    recorded = dq.get("roster_coverage_latest")
    if isinstance(recorded, (int, float)):
        return float(recorded)
    ser = breadth.get("series") or {}
    ma, const = ser.get("n_with_ma50") or [], ser.get("n_constituents") or []
    if not ma or not const or not const[-1]:
        return None
    return ma[-1] / const[-1]


def check_roster_coverage(coverages: dict[str, float | None],
                          floor: float) -> list[dict]:
    """G6: no DEPLOYED panel may be committed on a thin vendor download.

    compute_breadth already refuses to WRITE below its own hard floor, but
    it only warns in the band between. That band is not safe to commit: on
    2026-08-08 IDP6 was published at 61.5% coverage, which was inside the
    warn band, and it changed Strategy A's holdings — the sleeve kept IDP6
    at 6.3% and ejected IUMS entirely. A warning did not stop that reaching
    main. Writing a thin panel is tolerable because the alternative is a
    stale one; COMMITTING one is not, which is why this lives here rather
    than as a stricter floor in the writer.
    """
    out = []
    thin = {e: f"{c:.1%}" for e, c in coverages.items()
            if c is not None and c < floor}
    unknown = sorted(e for e, c in coverages.items() if c is None)
    if thin:
        out.append(verdict("G6 roster coverage", FAIL,
                           f"deployed panels below the {floor:.0%} floor — "
                           f"breadth computed on a thin sample can move the "
                           f"deployed book: {thin}"))
    else:
        out.append(verdict("G6 roster coverage", OK,
                           f"{len(coverages) - len(unknown)} panels at or "
                           f"above the {floor:.0%} floor"))
    if unknown:
        out.append(verdict("G6 roster coverage readable", WARN,
                           f"coverage indeterminable for {unknown} — "
                           f"neither the recorded field nor n_with_ma50 / "
                           f"n_constituents was usable"))
    return out


def check_no_lost_state(etf: str,
                        old_snapshot_keys: set[str],
                        new_snapshot_keys: set[str],
                        old_breadth: dict | None,
                        new_breadth: dict) -> list[dict]:
    """G5 for one panel: nothing the previous commit had may vanish."""
    out = []
    lost = sorted(old_snapshot_keys - new_snapshot_keys)
    if lost:
        out.append(verdict(
            f"G5 {etf} snapshots", FAIL,
            f"{len(lost)} snapshot(s) in HEAD are gone from the working "
            f"tree: {lost[:5]}{' ...' if len(lost) > 5 else ''}"))
    if old_breadth is not None:
        old_n = old_breadth.get("n_trading_days")
        new_n = new_breadth.get("n_trading_days")
        if isinstance(old_n, int) and isinstance(new_n, int) and new_n < old_n:
            out.append(verdict(
                f"G5 {etf} breadth length", FAIL,
                f"n_trading_days shrank {old_n} -> {new_n}: the refreshed "
                f"series silently lost sessions (2026-08-04 SPY class)"))
        old_end = old_breadth.get("end_date")
        new_end = new_breadth.get("end_date")
        if old_end and new_end and new_end < old_end:
            out.append(verdict(
                f"G5 {etf} breadth end", FAIL,
                f"end_date moved backwards {old_end} -> {new_end}"))
    return out


def check_universal_walkback(latest_actuals: dict[str, str],
                             end_friday: date) -> list[dict]:
    """W1: every panel walking back from the target Friday means the
    refresh predated iShares' publication of Friday's holdings."""
    n = len(latest_actuals)
    walked = sorted(k for k, v in latest_actuals.items()
                    if v != end_friday.isoformat())
    if n and len(walked) == n:
        sample = latest_actuals[walked[0]]
        return [verdict(
            "W1 universal walkback", WARN,
            f"all {n} panels' newest snapshot fell back from "
            f"{end_friday.isoformat()} (e.g. to {sample}) — the refresh "
            f"likely ran before iShares published Friday. Roster is one "
            f"day stale (defensible; prices/breadth are Friday-close). "
            f"Consider re-running fetch_constituents later; see "
            f"measure_publication_lag.py.")]
    if walked:
        return [verdict(
            "W1 universal walkback", OK,
            f"{len(walked)}/{n} panels walked back ({walked}) — normal "
            f"holiday/data-gap territory, recorded in walkbacks arrays")]
    return [verdict("W1 universal walkback", OK,
                    f"all {n} panels captured the target Friday exactly")]


# ---------------------------------------------------------------------------
# Disk / git access
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_committed_json(rel_path: str, ref: str = "HEAD") -> dict | None:
    """The committed version of ``rel_path`` at ``ref``, or None when the
    file is not in that ref (new panel) or git is unavailable (in which
    case the caller downgrades G5 to a WARN — a guard that cannot see its
    baseline must say so rather than silently pass)."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{rel_path}"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def latest_snapshot_actual(consts: dict) -> str | None:
    """actual_date of the newest snapshot in a constituents payload."""
    snaps = consts.get("snapshots") or {}
    if not snaps:
        return None
    newest_key = max(snaps)
    return (snaps[newest_key] or {}).get("actual_date")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert cross-panel coherence of the state a "
        "refresh_all.py run just produced; exit 1 when it must not be "
        "trusted.",
    )
    parser.add_argument("--end-friday", default=None,
                        help="expected target Friday YYYY-MM-DD (default: "
                        "the latest completed Friday per the clock)")
    parser.add_argument("--baseline-ref", default="HEAD",
                        help="git ref holding the previous committed state "
                        "for the loss check (default: HEAD)")
    args = parser.parse_args(argv)

    if args.end_friday:
        expected_friday = date.fromisoformat(args.end_friday)
    else:
        # Clock-derived, never a literal. date.today() matches the
        # convention fetch_constituents.main uses to pick its end_friday,
        # so guard and fetcher cannot disagree across a midnight boundary
        # any more than the fetcher disagrees with itself.
        expected_friday = latest_completed_friday(date.today())

    results: list[dict] = []
    end_fridays: dict[str, str] = {}
    health: dict[str, str] = {}
    staleness: dict[str, str] = {}
    breadth_ends: dict[str, str] = {}
    calendars: dict[str, str] = {}
    coverages: dict[str, float | None] = {}
    latest_actuals: dict[str, str] = {}

    baseline_missing: list[str] = []
    n_baseline_checked = 0
    n_loss_failures = 0
    for etf in ETFS_ALL:
        key = etf.lower()
        consts_rel = f"data/constituents_{key}.json"
        breadth_rel = f"data/breadth_{key}.json"
        try:
            consts = load_json(DATA_DIR / f"constituents_{key}.json")
            breadth = load_json(DATA_DIR / f"breadth_{key}.json")
        except (OSError, json.JSONDecodeError) as exc:
            results.append(verdict(f"G0 {etf} readable", FAIL,
                                   f"panel unreadable: {exc}"))
            continue
        end_fridays[etf] = consts.get("end_friday", "<absent>")
        health[etf] = (consts.get("endpoint_health") or {}).get(
            "status", "<absent>")
        staleness[etf] = (consts.get("staleness") or {}).get(
            "status", "<absent>")
        breadth_ends[etf] = breadth.get("end_date", "<absent>")
        coverages[etf] = panel_roster_coverage(breadth)
        try:
            calendars[etf] = get_etf(etf).get("trading_calendar", "NYSE")
        except KeyError:
            calendars[etf] = "NYSE"
        actual = latest_snapshot_actual(consts)
        if actual:
            latest_actuals[etf] = actual

        # G5 versus the committed baseline.
        old_consts = load_committed_json(consts_rel, args.baseline_ref)
        old_breadth = load_committed_json(breadth_rel, args.baseline_ref)
        if old_consts is None:
            baseline_missing.append(etf)
        else:
            n_baseline_checked += 1
            loss = check_no_lost_state(
                etf,
                set((old_consts.get("snapshots") or {}).keys()),
                set((consts.get("snapshots") or {}).keys()),
                old_breadth, breadth,
            )
            n_loss_failures += len(loss)
            results.extend(loss)

    results.extend(check_shared_end_friday(end_fridays, expected_friday))
    results.extend(check_endpoint_health(health))
    results.extend(check_staleness(staleness))
    results.extend(check_roster_coverage(coverages,
                                         MIN_ROSTER_COVERAGE_WARN))
    results.extend(check_breadth_ends(breadth_ends, calendars,
                                      expected_friday))
    results.extend(check_universal_walkback(latest_actuals, expected_friday))
    if n_loss_failures == 0 and n_baseline_checked:
        results.append(verdict(
            "G5 no lost state", OK,
            f"{n_baseline_checked} panels checked against "
            f"{args.baseline_ref}; nothing lost"))
    if baseline_missing:
        results.append(verdict(
            "G5 baseline", WARN,
            f"no committed baseline at {args.baseline_ref} for "
            f"{baseline_missing} — loss check skipped for these panels "
            f"(new panel or git unavailable)"))

    n_fail = sum(1 for r in results if r["status"] == FAIL)
    n_warn = sum(1 for r in results if r["status"] == WARN)
    print(f"refresh guard @ {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
          f"— expected target Friday {expected_friday.isoformat()}, "
          f"{len(ETFS_ALL)} deployed panels")
    for r in results:
        print(f"  {r['status']:<4} {r['check']}: {r['evidence']}")
    print(f"\n{n_fail} FAIL, {n_warn} WARN, "
          f"{len(results) - n_fail - n_warn} ok")
    if n_fail:
        print("\n[REFRESH-GUARD] The refreshed state is incoherent. Do not "
              "commit or publish it; re-run the failing step or "
              "investigate before trusting any output of this refresh.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
