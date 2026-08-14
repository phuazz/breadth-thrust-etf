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
  push        ONLY with --push (armed mode). Soak mode (no flag — the
              initial state) stops here and reports READY so the
              operator reviews and pushes manually. Arm the scheduled
              task by adding --push after two clean soak runs.

Failure alerting is best-effort local email (GMAIL_USER +
GMAIL_APP_PASSWORD environment variables, same names as the CI
secrets; silently skipped when unset) plus the dated log file under
logs/. The CI backstop needs nothing from this machine: the Sunday
09:00 UTC check emails [WARN] whenever the week's factsheet has not gone
out, whatever the reason this wrapper failed to run.

CADENCE, changed 2026-08-12. The task now runs FRIDAY 08:00 SGT, not
Saturday, because the instruction has to exist before the fill rather
than after it. The book executes in the Friday CLOSING auctions - Xetra
23:30 SGT that evening, the US 04:00 SGT on the Saturday - via
market-on-close orders submitted Friday evening. (An earlier revision the
same day used the Friday OPEN and was reversed; ignore any lingering
reference to open fills.) 08:00 SGT sits after Thursday's US close
(04:00 SGT summer, 05:00 winter) with hours of vendor-settle margin, and
roughly fifteen hours before the earliest auction.

Note what the Sunday CI backstop can and cannot do under that cadence:
it still catches a week where nothing was published, but it fires AFTER
the Friday fill, so it is a reconciliation check rather than a pre-trade
one. A miss is detected, not prevented.

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


def scheduled_commit_message(today: date, panel_end: date) -> str:
    """House-style local-refresh commit message, marked as scheduled."""
    return (
        f"Local weekly refresh {today.isoformat()} (scheduled): "
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
    parser.add_argument("--push", action="store_true",
                        help="Armed mode: commit and push on full green. "
                             "Without it (soak mode) the run stops after "
                             "validation and reports READY.")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run the git preflight and the gate preview "
                             "only — no refresh. Smoke test for the "
                             "scheduled task setup.")
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

    # ----- Preflight: clean tree, then sync to origin -----
    cp = _git(["status", "--porcelain"], log)
    if cp.returncode != 0:
        return fail(2, "git status failed", cp.stderr)
    if cp.stdout.strip():
        return fail(2, "working tree not clean",
                    "The automation clone has local changes; a human or "
                    "another process interfered. Not touching anything.\n"
                    + cp.stdout)
    cp = _git(["pull", "--rebase", "origin", "main"], log)
    if cp.returncode != 0:
        return fail(2, "git pull --rebase failed", cp.stderr)

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
        log.write("\nrunning refresh_all.py (output follows)\n")
        log.flush()
        rc = subprocess.run(
            [sys.executable, "scripts/refresh_all.py"],
            cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
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
        gate = build_gate_report("publish", now, PANEL, MARKER)
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
        msg = scheduled_commit_message(now.date(), panel_end)
        for step in (["add", "data/", "docs/"], ["commit", "-m", msg]):
            cp = _git(step, log)
            if cp.returncode != 0:
                return fail(5, f"git {step[0]} failed", cp.stderr or cp.stdout)
        cp = _git(["push", "origin", "main"], log)
        if cp.returncode != 0:
            return fail(5, "git push failed",
                        "Refresh is committed locally in the automation "
                        "clone but not pushed; push manually. " + cp.stderr)
        print(f"PUSHED - {msg}")
        log.write(f"\npushed: {msg}\n")
        _email("[OK] Scheduled refresh pushed - factsheet publishing",
               f"{msg}\n\nThe push triggers the gated factsheet "
               f"workflow.\n\nGate preview:\n{gate['detail']}", log)
    else:
        print(f"READY TO PUSH (soak mode) - review the clone, then: "
              f"git add data/ docs/ && git commit && git push")
        log.write("\nsoak mode: validated, NOT pushed\n")
        _email("[READY] Scheduled refresh validated - review and push (soak mode)",
               f"refresh_all.py green; panel current to {panel_end}.\n"
               f"Review {REPO_ROOT}, then commit and push to publish "
               f"the factsheet.\n\nGate preview:\n{gate['detail']}", log)
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
