"""Unattended weekly refresh wrapper — the scheduled counterpart of the
manual "run refresh_all.py, review, commit, push" Saturday ritual.

Commissioned 2026-07-25 alongside the event-driven factsheet publish:
the weekly email now fires on the push that lands the panel refresh, so
scheduling the refresh completes the chain close -> refresh -> push ->
gate -> email without operator involvement. Per the vault rule that no
unattended agent runs without a guard layer, this wrapper is nothing
BUT guard layers around refresh_all.py:

  preflight   clean working tree required (a dirty automation clone
              means a human or another process interfered — abort), then
              git pull --rebase so the run starts from origin HEAD.
  refresh     scripts/refresh_all.py, full run, no flags. Exit 0 there
              already requires every step green INCLUDING pytest.
  anchor      data/breadth_csp1.json end_date must reach
              nyse_sessions.last_completed_session(now) — catches the silent
              case where every step exits 0 on quietly-stale fetches
              (the pipeline hard guard catches a wholly-stalled panel,
              but a panel that advanced to Thursday when Friday exists
              would pass it and then be silently held by the CI gate).
  rosters     no constituent roster gained an endpoint_unavailable entry.
              A mid-run network drop leaves honest holes in the rosters
              fetched after it, and the anchor check cannot see them: it
              asks whether the PANEL reaches the decision session, not
              whether every ROSTER was complete. breadth_csp1 is written
              early, so it can pass while later sleeves are computed on
              holed rosters. Compares against the committed state rather
              than demanding zero, since past outages leave permanent,
              already-recorded gaps.
  gate view   scripts/check_factsheet_gate.py's own decision function,
              run locally, previews exactly what CI will do on push.
  commit      --commit commits data/ and docs/ LOCALLY and pushes nothing,
              so a second scheduled run that day starts on a clean tree.
  push        ONLY with --push (armed mode). Soak mode (no flag) stops
              here and reports READY so the operator reviews and pushes
              manually.

BOTH SCHEDULED TASKS ARE ARMED as of 2026-08-26 (owner decision):
BreadthThrust-WeeklyRefresh and BreadthThrust-PostFillRefresh both run
--push. What that does and does NOT do:

  DOES     push the refresh to main, so Pages rebuilds and every consumer
           — the dashboard, the reduced public page, the Navigo digest —
           reads the current book without an operator step.
  DOES NOT email the factsheet. check_factsheet_gate requires the anchor to
           be RELEASED (docs/factsheet_release.json, written only by
           scripts/release_factsheet.py) AND not yet published. An automatic
           push-triggered run gets no exemption; the release is a separate,
           deliberate operator act and remains the human gate on the one
           outward-facing send. Verified against the decision core for
           Saturday and Sunday clocks on 2026-08-26.

A NOTE ON THE EXECUTION LIMIT. Both tasks carry ExecutionTimeLimit PT8H,
raised from PT5H on 2026-08-26. A contended run that day took 4h55m and was
terminated at the limit having ALREADY completed every step including
pytest — killed at the finish line, reported as a failure, and leaving a
dirty tree that then blocked the next run's clean-tree preflight. A full
cold refresh is ~4h by itself, so PT5H had no real headroom.

A TRIGGER'S START BOUNDARY MUST BE IN THE FUTURE. Registering a weekly
trigger whose StartBoundary is earlier the same day makes Task Scheduler
treat it as a MISSED occurrence and, with StartWhenAvailable, fire it
immediately. That is how the post-fill task ran unintended at 15:00 on the
afternoon it was created, mid-market and against pre-fix code.

Failure alerting is best-effort local email (GMAIL_USER +
GMAIL_APP_PASSWORD environment variables, same names as the CI
secrets; silently skipped when unset) plus the dated log file under
logs/. The CI backstop needs nothing from this machine: the Sunday
09:00 UTC check emails [WARN] whenever the week's factsheet has not gone
out, whatever the reason this wrapper failed to run.

CADENCE, changed 2026-08-22 by WS18 (supersedes the 2026-08-12 Friday move,
whose description this replaces rather than annotates - a stale cadence
paragraph is the most quotable wrong line in an operations module).

The book rebalances MONDAY at the close, ranking on FRIDAY's close, so every
sleeve ranks at rd-1. Under the Friday cadence sleeve D could only reach rd-2:
the vendor-availability probe found the European data a session behind at every
hour of the Friday decision window.

TWO RUNS PER WEEKEND, and the second is not redundant. The probe showed the
European close is served about three hours after the bell, RETRACTED overnight,
and settled permanently only the following day - observed on four consecutive
day-boundaries. So on Saturday morning the US sleeves have Friday's close and
sleeve D does not; by Sunday morning all four do.

  Saturday 09:00 SGT  sleeves A/B/C ready to review and plan; D reports HOLD
  Sunday   09:00 SGT  D's European close has settled; the full book is ready

A SECOND PAIR AFTER THE FILL, added 2026-08-26 (BreadthThrust-PostFillRefresh,
--cadence post-fill). The refresh cadence did not move with the rebalance
cadence on 22 August, and that left a four-day hole nobody had looked for.
Under W-FRI the weekend refresh ran AFTER Friday's fill, so the published book
was current all the following week. Under W-MON it runs BEFORE Monday's fill,
and mark_to_market_live.py is a strictly forward-only extension that never
applies a rebalance — so without a second pair the dashboard, live_track.json
and every downstream consumer carry a book one fill stale from Tuesday to
Friday. Found on 2026-08-26: the dashboard was still advertising the 24 August
fill as PLANNED two days after it, and the Navigo digest quoted SOXX at 6.01%
of NAV against a post-fill target of 2.78%.

  Tuesday  09:00 SGT  A/B/C re-anchor onto Monday's fill; D still one behind
  Wednesday 09:00 SGT Xetra's Monday close has settled; D re-anchors too

THE SPLIT IS MEASURED, NOT ASSUMED. data/vendor_availability_log.jsonl (4x
daily since 2026-08-15) shows the Xetra bar for a session served about three
hours after that bell, retracted overnight, and settled permanently only the
following day. At 01:00 UTC (09:00 SGT) Xetra is reliably ONE session behind
on a weekday: on Tue 2026-08-25 it held Friday's bar while NYSE held Monday's;
by Wed 2026-08-26 it held Monday's. Tuesday therefore re-anchors 80% of NAV
and Wednesday completes it — the same shape as the weekend pair, for the same
reason. A mixed day is disclosed rather than hidden: strategy_freshness.py
reports per-sleeve reach and the dashboard prints it per sleeve.

All four fire hourly for six hours, with StartWhenAvailable, so a machine off at
09:00 catches up on power-on.

--commit EXISTS BECAUSE OF THAT SECOND RUN. Soak mode never commits, so
Saturday would leave a dirty tree and Sunday's clean-tree preflight would
refuse - the weekend silently collapsing to one run. That failure is not
hypothetical: six consecutive catch-up firings were consumed exactly that way
on 2026-08-14, each refusing in turn while the window closed.

Usage:
    python scripts/scheduled_refresh.py                  # soak: no push
    python scripts/scheduled_refresh.py --push           # armed
    python scripts/scheduled_refresh.py --preflight-only # smoke test

Exit codes: 0 ok (ready or pushed) / 2 preflight / 3 refresh failed /
4 anchor / roster / gate failed / 5 push failed.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import subprocess
import sys
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

# Allow importing sibling scripts/ modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_factsheet_gate import build_gate_report  # noqa: E402
from check_roster_integrity import evaluate as roster_integrity  # noqa: E402
from nyse_sessions import last_completed_session, week_final_anchor  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL = REPO_ROOT / "data" / "breadth_csp1.json"
MARKER = REPO_ROOT / "docs" / "factsheet_published.json"
RELEASE = REPO_ROOT / "docs" / "factsheet_release.json"
LOG_DIR = REPO_ROOT / "logs"


def panel_is_current(panel_end: date, now_utc: datetime) -> bool:
    """True when the panel reaches the last COMPLETED trading session.

    This is the session the decision reads: the engines rank on the close
    before the rebalance, so a Friday-morning refresh feeding a Friday fill
    must have Thursday's close in the panel.

    CHANGED 2026-08-12, with the move to a Friday-morning refresh. The guard
    previously anchored on ``week_final_anchor`` — the final session of the
    most recent COMPLETED week — which is correct only when the run happens
    after that week has closed, i.e. on a Saturday. Run on a Friday morning it
    goes blind: mid-week, week_final_anchor returns the PREVIOUS week's Friday
    by design, so on Fri 14 Aug 2026 it would demand only that the panel reach
    7 August while the decision that morning reads Thursday 13 August. A panel
    that had not refreshed at all since the previous week would pass, and then
    be handed to a live trade.

    ``last_completed_session`` is both correct for the new cadence and
    strictly tighter than the old anchor on the old one: on a Saturday the two
    agree exactly, because that week's final session IS the last completed
    session.
    """
    return panel_end >= last_completed_session(now_utc)


# Retained so the factsheet-publishability question can still be asked
# separately. It is a DIFFERENT question from "is the panel fresh enough to
# trade on", and conflating the two is what made the guard re-timable by
# accident.
def panel_is_week_current(panel_end: date, now_utc: datetime) -> bool:
    """True when the panel covers the most recent completed trading
    week's final session — the condition under which the CI publish gate
    will let the factsheet email out."""
    return panel_end >= week_final_anchor(now_utc)


def log_path_for(now_utc: datetime, tz=None) -> Path:
    """Log file for a run, named by LOCAL date.

    Named by UTC date until 2026-08-12, which answered a different question
    from the one anyone asks of it. The task is scheduled in local time and
    the operator asks "did Friday's run happen"; under the old Saturday 06:00
    SGT cadence, 06:00 SGT is 22:00 UTC on the FRIDAY, so every scheduled run
    was filed under the previous day's name and appended to that file. The
    8 August 2026 run consequently looked like it had never happened — it is
    in scheduled_refresh_2026-08-07.log, and reading the filename rather than
    the timestamps inside cost an investigation and produced a wrong soak
    count.

    ``tz`` exists so this is testable off a machine's own zone: the default
    None means the machine's local zone, which is what production uses.
    """
    return LOG_DIR / f"scheduled_refresh_{now_utc.astimezone(tz).date().isoformat()}.log"


CADENCES = ("weekend", "post-fill")

# PRICE SOURCE (2026-09-03, owner decision; WS19c found `auto` adopt-eligible).
#
# On Friday 2026-08-28 yfinance served no bar for ten of thirteen sleeve-B
# lines and for SHY, and the 2026-09-02 post-fill run from this clone
# published the 2026-08-31 rebalance decided on THURSDAY. Norgate carried the
# session throughout. The scheduled runs therefore source sleeves B and C and
# the A/D proxies from Norgate, on the same machine, and FAIL CLOSED when the
# feed is unreachable: a basis flip is a restatement and must be chosen, not
# suffered because a service was down at 09:00. `--price-source yfinance` is
# the explicit way to accept the yfinance basis for one run.
PRICE_SOURCES = ("norgate", "yfinance", "auto")
DEFAULT_PRICE_SOURCE = "norgate"


def price_source_preflight(requested: str, available=None) -> tuple[bool, str]:
    """(ok, message). A request for Norgate that the machine cannot honour is
    refused BEFORE the four-hour refresh starts, not discovered in an engine
    step at the end of it."""
    if requested not in PRICE_SOURCES:
        return False, f"unknown price source {requested!r}"
    if requested == "yfinance":
        return True, "price source yfinance (requested)"
    if available is None:
        import norgate_prices  # local: sibling module on sys.path
        available = norgate_prices.available
    reachable = bool(available())
    if requested == "norgate" and not reachable:
        return False, ("price source norgate requested but the Norgate feed is "
                       "unreachable on this machine. Start the Norgate Data "
                       "Updater, or re-run with --price-source yfinance to "
                       "accept the yfinance basis explicitly for this run.")
    if requested == "auto":
        return True, ("price source auto: " + ("norgate, feed reachable"
                                               if reachable else
                                               "yfinance, feed unreachable "
                                               "(fallback RECORDED)"))
    return True, "price source norgate (requested and reachable)"


def scheduled_commit_message(today: date, panel_end: date,
                             cadence: str = "weekend") -> str:
    """House-style local-refresh commit message, marked as scheduled.

    The cadence is IN the message because the two pairs make different
    promises and fleet_watch greps them apart. The weekend pair produces the
    book Monday's fill will be RANKED on; the post-fill pair records the fill
    itself. Under one shared prefix a post-fill week that never ran would be
    indistinguishable from a healthy one, because the weekend commit would
    keep the heartbeat fresh — which is the exact blind spot the row exists
    to close.
    """
    if cadence not in CADENCES:
        raise ValueError(f"unknown cadence {cadence!r}, expected one of {CADENCES}")
    kind = "weekly" if cadence == "weekend" else "post-fill"
    return (
        f"Local {kind} refresh {today.isoformat()} (scheduled): "
        f"panels current to {panel_end.isoformat()}, all steps OK"
    )


def _git(args: list[str], log) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    log.write(f"\n$ git {' '.join(args)}\n{cp.stdout}{cp.stderr}")
    log.flush()
    return cp


def _email(subject: str, body: str, log) -> None:
    """Best-effort operator email; never raises. Uses the same variable
    names as the CI secrets so one convention covers both sides."""
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        log.write("\n[email skipped: GMAIL_USER / GMAIL_APP_PASSWORD not set]\n")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"Scheduled Refresh <{user}>"
        msg["To"] = user
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(user, pw)
            s.send_message(msg)
        log.write(f"\n[email sent: {subject}]\n")
    except Exception as exc:
        log.write(f"\n[email FAILED: {exc}]\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="commit data/ and docs/ locally and push NOTHING. "
                             "Required for a multi-run weekend: soak mode never "
                             "commits, so the first run leaves a dirty tree and "
                             "the second refuses on the clean-tree preflight.")
    parser.add_argument("--push", action="store_true",
                        help="Armed mode: commit and push on full green. "
                             "Without it (soak mode) the run stops after "
                             "validation and reports READY.")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run the git preflight and the gate preview "
                             "only — no refresh. Smoke test for the "
                             "scheduled task setup.")
    parser.add_argument("--cadence", choices=CADENCES, default="weekend",
                        help="Which pair this run belongs to. Affects the "
                             "commit message only — every guard is identical. "
                             "'weekend' (Sat/Sun) produces the book Monday's "
                             "fill is ranked on; 'post-fill' (Tue/Wed) records "
                             "the fill itself.")
    parser.add_argument("--price-source", choices=PRICE_SOURCES,
                        default=DEFAULT_PRICE_SOURCE,
                        help="Price source for sleeves B and C and the A/D "
                             "proxies, passed to refresh_all.py. Default "
                             f"'{DEFAULT_PRICE_SOURCE}' (owner decision "
                             "2026-09-03, WS19c): the run FAILS at preflight "
                             "when the Norgate feed is unreachable rather "
                             "than publishing a yfinance-basis book under a "
                             "Norgate flag. Pass 'yfinance' to accept that "
                             "basis explicitly.")
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    log_path = log_path_for(now)
    log = open(log_path, "a", encoding="utf-8")
    # Both stamps on the header line: the local one is how the schedule and the
    # operator think, the UTC one is unambiguous across any future change of
    # machine or zone. Reading only one of them is what went wrong before.
    log.write(f"\n{'='*72}\nscheduled_refresh start "
              f"{now.astimezone():%Y-%m-%d %H:%M %Z} (= {now.isoformat()}) "
              f"(push={args.push}, preflight_only={args.preflight_only})\n{'='*72}\n")

    def fail(code: int, subject: str, body: str) -> int:
        print(f"FAILED ({subject}) - see {log_path}")
        log.write(f"\nFAILED exit {code}: {subject}\n{body}\n")
        _email(f"[FAIL] Scheduled refresh - {subject}", body + f"\n\nLog: {log_path}", log)
        log.close()
        return code

    # ----- Preflight: price source, clean tree, then sync to origin -----
    ok, msg = price_source_preflight(args.price_source)
    log.write(f"\n{msg}\n")
    if not ok:
        return fail(2, "price source unavailable", msg)
    cp = _git(["status", "--porcelain"], log)
    if cp.returncode != 0:
        return fail(2, "git status failed", cp.stderr)
    if cp.stdout.strip():
        return fail(2, "working tree not clean",
                    "The automation clone has local changes; a human or "
                    "another process interfered. Not touching anything.\n"
                    + cp.stdout)
    _self_before = Path(__file__).read_bytes()
    cp = _git(["pull", "--rebase", "origin", "main"], log)
    if cp.returncode != 0:
        return fail(2, "git pull --rebase failed", cp.stderr)

    # RE-EXEC IF THE PULL REWROTE THIS SCRIPT (2026-09-02).
    #
    # The preflight pull updates the clone this script is RUNNING FROM, so a
    # commit that touches both this file and something it calls leaves the
    # process holding the old half. On 2026-09-02 that ran the previous
    # scheduled_refresh against the new refresh_all and died on
    # "unrecognized arguments: --skip-panels" — the flag had been renamed in
    # the very commit the pull had just applied. Nothing was wrong with
    # either version; they were simply a commit apart inside one process.
    #
    # Re-exec rather than abort, so a run still happens on the schedule it
    # was given. Guarded by an environment marker so a pull that keeps
    # changing the file cannot spin: the second pass proceeds on whatever it
    # has, and the version skew is gone by then in every realistic case.
    if Path(__file__).read_bytes() != _self_before:
        if os.environ.get("BTE_SCHED_REEXEC") == "1":
            log.write("\nthis script changed again after re-exec — "
                      "continuing on the current version rather than "
                      "looping\n")
        else:
            log.write("\nthe pull rewrote this script; re-executing so both "
                      "halves come from the same commit\n")
            log.flush()
            log.close()
            os.environ["BTE_SCHED_REEXEC"] = "1"
            os.execv(sys.executable, [sys.executable, *sys.argv])

    # ----- Already done? -----
    # The scheduled task retries hourly and starts as soon as the machine is
    # available, so that a Friday with the laptop shut still gets its refresh
    # when it opens. Without this exit every one of those retries would re-run
    # the whole ~1-4 hour refresh over a panel that is already current, and a
    # long run could still be going when the next hour fired. Checked AFTER the
    # pull, so the answer reflects origin rather than a stale local clone.
    if not args.preflight_only:
        try:
            panel_end_now = date.fromisoformat(
                json.loads(PANEL.read_text(encoding="utf-8"))["end_date"])
        except Exception:  # noqa: BLE001
            panel_end_now = None          # unreadable -> fall through and run
        if panel_end_now and panel_is_current(panel_end_now, now):
            msg = (f"ALREADY CURRENT - panel ends {panel_end_now}, which "
                   f"reaches the last completed session "
                   f"{last_completed_session(now)}. Nothing to do.")
            print(msg)
            log.write(f"\n{msg}\n")
            log.close()
            return 0

    # ----- Refresh (the ~4.3 hour part) -----
    if not args.preflight_only:
        # CADENCE DECIDES THE SCOPE (2026-09-02). The weekend run walks all
        # 38 panels; the post-fill run walks only the 24 DEPLOYED ones.
        #
        # NOT a panel skip, which was tried first and was wrong. Skipping the
        # panels outright let the engines advance to 2026-09-01 while the
        # panels stayed at 2026-08-28, and build_simple_page refused the
        # result — "freshness says sleeve B reaches 2026-09-01, past the
        # newest data this refresh produced". Sleeve A ranks on those panels,
        # so they are part of a coherent re-anchor, not an optional extra.
        #
        # The 14 Europe supersector CANDIDATES are a different matter: they
        # are screened, never held, and cannot affect the book being
        # re-anchored. Dropping them cuts step 1 by roughly a third and costs
        # the post-fill run nothing it needs.
        #
        # The real protection against the 2026-09-01 stall is not scope but
        # the per-step timeout now in run_step: one compute_breadth consumed
        # 13.3 hours there once yfinance's limiter throttled it, and no
        # narrowing of scope would have bounded that.
        cmd = [sys.executable, "scripts/refresh_all.py",
               "--price-source", args.price_source]
        if args.cadence == "post-fill":
            cmd.append("--deployed-only")
        log.write(f"\nrunning {' '.join(cmd[1:])} (output follows)\n")
        log.flush()
        rc = subprocess.run(
            cmd, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
        ).returncode
        if rc != 0:
            return fail(3, "refresh_all.py reported failed steps",
                        "One or more refresh steps failed; nothing was "
                        "committed or pushed. Re-run the failed steps "
                        "manually (see the refresh summary in the log).")

    # ----- Anchor + gate verdict (the silent-wrong guard) -----
    now = datetime.now(timezone.utc)  # refresh took hours; re-read clock
    try:
        panel_end = date.fromisoformat(
            json.loads(PANEL.read_text(encoding="utf-8"))["end_date"])
        if not args.preflight_only and not panel_is_current(panel_end, now):
            return fail(4, "panel did not reach the last completed session",
                        f"All steps exited 0 but breadth_csp1 ends "
                        f"{panel_end} vs the last completed session "
                        f"{last_completed_session(now)} - quietly-stale "
                        f"fetches. This is the session the decision reads, "
                        f"so nothing was pushed; investigate the fetch "
                        f"steps in the log.")
        if not args.preflight_only:
            ri = roster_integrity(REPO_ROOT)
            if ri.get("undetermined"):
                return fail(4, "roster integrity undetermined", ri["summary"])
            if not ri["ok"]:
                holed = ", ".join(f"{x['etf']} +{x['new']}"
                                  for x in ri["rows"] if x.get("new", 0) > 0)
                return fail(4, "a roster gained outage holes this run",
                            f"{ri['summary']}: {holed}. An upstream drop "
                            f"mid-run leaves those Fridays absent, and breadth "
                            f"for those ETFs is computed on an incomplete "
                            f"roster. Nothing pushed; re-run once the endpoint "
                            f"is healthy - the raw responses are cached, so "
                            f"only the missing dates refetch.")
        # RELEASE must be passed explicitly. build_gate_report treats a missing
        # release_path as NOT RELEASED by construction (it is a pure core and
        # refuses to reach for repo state on its own), so omitting it made this
        # "preview" report HOLD unconditionally — including in the one case
        # that matters, where CI would actually send. A guard that returns the
        # same answer whatever the state is not a guard. Found 2026-08-26 while
        # arming the post-fill pair: the preview said "not released" against a
        # release marker on disk that plainly named the anchor.
        gate = build_gate_report("publish", now, PANEL, MARKER,
                                 release_path=RELEASE)
        log.write(f"\nCI gate preview on push:\n{gate['detail']}\n")
    except Exception as exc:
        return fail(4, "anchor/gate check errored", repr(exc))

    if args.preflight_only:
        print(f"PREFLIGHT OK - gate preview in {log_path}")
        log.write("\npreflight-only run complete\n")
        log.close()
        return 0

    # ----- Push (armed) or READY (soak) -----
    if args.push:
        msg = scheduled_commit_message(now.date(), panel_end, args.cadence)
        for step in (["add", "data/", "docs/", "build/portfolio.html", "template.html"], ["commit", "-m", msg]):
            cp = _git(step, log)
            if cp.returncode != 0:
                # A no-change run is a CLEAN run, not a failure — the same
                # tolerance the --commit branch below has always had. Without
                # it the armed post-fill pair alerts on a healthy outcome:
                # Tuesday commits the fill, and a Wednesday that finds nothing
                # further to record exits 5. The push still runs, because the
                # clone may carry earlier commits that never reached origin.
                if step[0] == "commit" and "nothing to commit" in (
                        cp.stdout + cp.stderr).lower():
                    log.write("\nnothing to commit — pushing any earlier "
                              "local commits\n")
                    break
                return fail(5, f"git {step[0]} failed", cp.stderr or cp.stdout)
        # RETRY, REBASING BETWEEN ATTEMPTS (2026-09-02).
        #
        # This run takes 40 minutes and the repo is written by several other
        # things — three probes a day, the scanner, the daily live track, and
        # whoever is at the keyboard. Origin therefore moves UNDER a healthy
        # run as a matter of course, and the first push comes back
        # "non-fast-forward" through no fault of the refresh. On 2026-09-02
        # that lost a complete, correct, fully-guarded post-fill run at the
        # final step; the commit sat in the clone until someone rebased it by
        # hand. A run that did everything right must not need a human for the
        # last thirty seconds.
        #
        # Same shape the workflows already use (daily_live_track, scanner,
        # universe_monitor): push, and on rejection rebase onto origin and try
        # again. --autostash because the build may have left tracked outputs
        # dirty. Three attempts, then fail loudly — a push that cannot land
        # after three rebases is not a race, it is something else.
        pushed = False
        for attempt in (1, 2, 3):
            cp = _git(["push", "origin", "main"], log)
            if cp.returncode == 0:
                pushed = True
                if attempt > 1:
                    log.write(f"\npushed on attempt {attempt} "
                              f"(origin moved during the run)\n")
                break
            log.write(f"\npush rejected on attempt {attempt}; rebasing onto "
                      f"origin/main and retrying\n")
            rb = _git(["pull", "--rebase", "--autostash", "origin", "main"], log)
            if rb.returncode != 0:
                return fail(5, "git push failed, and the rebase failed too",
                            "Refresh is committed locally in the automation "
                            "clone but not pushed, and it could not be "
                            "rebased onto origin. Resolve by hand.\n"
                            + rb.stderr)
        if not pushed:
            return fail(5, "git push failed after 3 attempts",
                        "Refresh is committed locally in the automation "
                        "clone but not pushed after three rebase-and-retry "
                        "attempts; push manually. " + cp.stderr)
        print(f"PUSHED - {msg}")
        log.write(f"\npushed: {msg}\n")
        _email("[OK] Scheduled refresh pushed - factsheet publishing",
               f"{msg}\n\nThe push triggers the gated factsheet "
               f"workflow.\n\nGate preview:\n{gate['detail']}", log)
    elif args.commit:
        # COMMIT LOCALLY, PUSH NOTHING. Added 2026-08-22 with the two-run
        # weekend (Saturday for sleeves A/B/C, Sunday once the European close
        # has settled).
        #
        # Without this the weekend silently collapses to ONE run. Soak mode
        # never commits, so Saturday leaves the tree dirty; the preflight above
        # refuses any dirty tree; Sunday therefore exits 2 having done nothing.
        # That is not hypothetical — it is exactly how six consecutive
        # catch-up firings were consumed on 2026-08-14, each refusing in turn
        # while the window closed.
        #
        # A local commit leaves the tree clean for the next run and publishes
        # nothing: the factsheet still waits for a human push and the CI gate.
        msg = scheduled_commit_message(now.date(), panel_end, args.cadence)
        for step in (["add", "data/", "docs/", "build/portfolio.html", "template.html"], ["commit", "-m", msg]):
            cp = _git(step, log)
            if cp.returncode != 0:
                if step[0] == "commit" and "nothing to commit" in (
                        cp.stdout + cp.stderr).lower():
                    break          # a no-change run is a clean run, not a fail
                return fail(5, f"git {step[0]} failed", cp.stderr or cp.stdout)
        print(f"COMMITTED LOCALLY (not pushed) - {msg}")
        log.write(f"\ncommitted locally, NOT pushed: {msg}\n")
        _email("[OK] Scheduled refresh committed locally - not published",
               f"refresh_all.py green; panel current to {panel_end}.\n"
               f"Committed in {REPO_ROOT} so the next scheduled run starts on a "
               f"clean tree. NOTHING PUBLISHED - push manually to release the "
               f"factsheet.\n\nGate preview:\n{gate['detail']}", log)
    else:
        print(f"READY TO PUSH (soak mode) - review the clone, then: "
              f"git add data/ docs/ build/portfolio.html template.html && git commit && git push")
        log.write("\nsoak mode: validated, NOT pushed\n")
        _email("[READY] Scheduled refresh validated - review and push (soak mode)",
               f"refresh_all.py green; panel current to {panel_end}.\n"
               f"Review {REPO_ROOT}, then commit and push to publish "
               f"the factsheet.\n\nGate preview:\n{gate['detail']}", log)
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
