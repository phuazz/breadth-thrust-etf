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
  refresh     scripts/refresh_all.py --price-source <source> (default
              norgate since 2026-09-03), plus --deployed-only for the
              post-fill cadence. Exit 0 there already requires every step
              green INCLUDING pytest.
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
import publication_debt  # noqa: E402  (what this cadence still owes)
import run_status  # noqa: E402  (durable, machine-readable run + alert state)


def _safe(log, what: str, fn, *args, **kwargs):
    """Run a DIAGNOSTIC and swallow anything it does.

    The diagnostics added on 2026-09-16 are called from inside ``fail()``
    and from ``_email``, which is itself called from ``fail()``. A
    diagnostic that raises there does not merely lose its own record: it
    unwinds out of the failure handler and replaces the refresh's actual
    failure with a traceback about the instrument. That is not
    hypothetical - a corrupt ``alert_delivery.json`` holding a JSON array
    raised AttributeError on exactly that path. The writers are themselves
    written not to raise; this is the belt to that pair of braces, and it
    records the fact in the log rather than hiding it.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a diagnostic never breaks a run
        try:
            log.write(f"\n[diagnostic '{what}' failed, ignored: "
                      f"{type(exc).__name__}: {exc}]\n")
        except Exception:  # noqa: BLE001
            pass
        return None


def _durable_state_enabled() -> bool:
    """Whether this process may write the operational state under logs/.

    False under pytest, and the reason is not squeamishness about test
    pollution in general. ``tests/test_europe_capture_recovery.py`` calls
    ``main()`` IN-PROCESS against the real REPO_ROOT, so without this a
    full suite run rewrites logs/alert_delivery.json with an UNCONFIGURED
    verdict produced by a process that was never going to send anything.
    The health readout would then be corrupted by the act of testing it —
    the same shape of defect as the incident that prompted the file.

    run_status itself stays pure and is tested directly against tmp_path;
    the awareness lives here, in the module the tests drive.
    """
    return not os.environ.get("PYTEST_CURRENT_TEST")

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


def _git(args: list[str], log, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    log.write(f"\n$ git {' '.join(args)}\n{cp.stdout}{cp.stderr}")
    log.flush()
    return cp


def _email(subject: str, body: str, log) -> str:
    """Best-effort operator email; never raises. Uses the same variable
    names as the CI secrets so one convention covers both sides.

    RETURNS THE OUTCOME (2026-09-17, eighth review): ``run_status.SENT``,
    ``UNCONFIGURED`` or ``FAILED``. It returned nothing before, so a caller
    that needed to know whether the message had actually gone anywhere could
    not ask - and the notice dedupe, which is the one caller that does,
    treated an unconfigured mailer as a delivered alert. SENT means the SMTP
    server accepted the message; it is not evidence that anybody read it.

    EVERY outcome is also written to logs/alert_delivery.json (2026-09-16).
    Until then the only record was the prose line below, and on 15 and 16
    September five consecutive failures each wrote "[email skipped]" into a
    text file nothing reads. The machine-readable record exists so an
    INDEPENDENT process can see that the alerting channel is dead; the log
    line is kept because a human reading the log should see it too.
    """
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        missing = ", ".join(run_status.credential_state()["missing"])
        log.write(f"\n[email skipped: {missing} not set]\n")
        if _durable_state_enabled():
            _safe(log, "alert record (unconfigured)", run_status.record_alert,
                  run_status.alert_state_path(REPO_ROOT),
                  subject=subject, status=run_status.UNCONFIGURED,
                  detail=f"missing: {missing}")
        return run_status.UNCONFIGURED
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"Scheduled Refresh <{user}>"
        msg["To"] = user
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(user, pw)
            s.send_message(msg)
        log.write(f"\n[email sent: {subject}]\n")
        if _durable_state_enabled():
            _safe(log, "alert record (sent)", run_status.record_alert,
                  run_status.alert_state_path(REPO_ROOT),
                  subject=subject, status=run_status.SENT)
        return run_status.SENT
    except Exception as exc:
        # REDACTED (2026-09-16). SMTPRecipientsRefused stringifies to a dict
        # keyed by the recipient address, which IS GMAIL_USER, and an
        # authentication failure can echo the password back from the server.
        # Both used to be written verbatim here and into the durable record.
        detail = run_status.redact(f"{type(exc).__name__}: {exc}")
        log.write(f"\n[email FAILED: {detail}]\n")
        if _durable_state_enabled():
            _safe(log, "alert record (failed)", run_status.record_alert,
                  run_status.alert_state_path(REPO_ROOT),
                  subject=subject, status=run_status.FAILED,
                  detail=detail)
        return run_status.FAILED


# RESTORE THE CLONE AFTER A FAILED REFRESH (2026-09-03).
#
# A run that fails after refresh_all has started writing leaves tracked
# outputs modified, and the clean-tree preflight then refuses EVERY later
# firing until a person resets the clone: sixteen firings were consumed that
# way between 2026-08-29 and 2026-09-01, and the hourly repetition the
# schedule carries was worth nothing. Every fail-closed guard added since
# (decision session, coverage depth, strict Norgate) is one more way to fail
# mid-run, so without this the weekend has ONE real attempt, not twelve.
# Restoring the committed state after a failure is what makes the next firing
# a retry instead of a refusal.
#
# Only for failures INSIDE the refresh (exit 3 and 4). A preflight refusal
# (exit 2) found the tree dirty before this run touched it, and that is a
# person's work to keep; a push failure (exit 5) has already committed, so
# there is nothing to restore. The porcelain listing is logged before anything
# is discarded, so nothing disappears without a record.
RESTORE_ON_EXIT_CODES = (3, 4)
RESTORE_PATHS = ("data/", "docs/", "build/portfolio.html", "template.html")


def refusal_report(repo_root: Path = REPO_ROOT) -> str:
    """Every roster refusal currently on disk, as text for the run log.

    WHY THIS RUNS BEFORE THE ROLLBACK. ``data/constituents_*.json`` is
    TRACKED, so restore_tracked_outputs checks it back out to HEAD and the
    ``roster_refusals`` array the failing run wrote is gone. The run log is
    under logs/, which is gitignored and therefore survives — so the refusal
    detail has to be copied into it while it still exists. Without this, the
    one artefact naming the venue to map is destroyed by the cleanup that
    makes the next firing a retry.

    The retained vendor responses under data/raw_ishares/*.refused.json also
    survive: that path is gitignored, and the rollback's `git clean -fd`
    deliberately omits -x. This function names them so the operator knows the
    dates can be rebuilt from disk.

    Returns "" when nothing is refused, so the caller can skip the section.
    """
    lines: list[str] = []
    for path in sorted((repo_root / "data").glob("constituents_*.json")):
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        recs = blob.get("roster_refusals")
        if not isinstance(recs, list) or not recs:
            continue
        etf = blob.get("etf") or path.stem.replace("constituents_", "").upper()
        lines.append(f"  {etf}: {len(recs)} refused target Friday(s)")
        for r in recs:
            lines.append(
                f"    target {r.get('target_friday')} "
                f"(source {r.get('source_date')}): "
                f"{r.get('n_affected')} of {r.get('n_equity_rows')} equity "
                f"rows")
            lines.append(f"      venues:  "
                         f"{', '.join(r.get('exchanges') or []) or '<none>'}")
            syms = r.get("affected_symbols") or []
            shown = ", ".join(syms[:12])
            more = f" (+{len(syms) - 12} more)" if len(syms) > 12 else ""
            lines.append(f"      symbols: {shown}{more}")
    if not lines:
        return ""
    return (
        "\nROSTER REFUSALS recorded by this run (copied here BEFORE the "
        "rollback, which restores data/ from HEAD and would otherwise "
        "destroy them):\n"
        + "\n".join(lines)
        + "\n  The row counts above are ROW shares, not portfolio weights.\n"
        "  Remedy: map the venue in "
        "fetch_constituents._EXCHANGE_TO_YF_SUFFIX, then re-run. The refused\n"
        "  vendor responses are retained under "
        "data/raw_ishares/*.refused.json, which the rollback does not touch\n"
        "  (gitignored; `git clean -fd` omits -x), so the dates rebuild from "
        "disk rather than from the vendor.\n"
    )


def restore_tracked_outputs(log, repo_root: Path = REPO_ROOT) -> bool:
    """Discard what a failed refresh wrote: tracked files under the output
    paths back to HEAD, untracked (never ignored) files under data/ and docs/
    removed. Returns True when the tree is clean afterwards."""
    before = _git(["status", "--porcelain"], log, cwd=repo_root)
    if before.returncode != 0:
        return False
    if not before.stdout.strip():
        log.write("\nrestore: tree already clean\n")
        return True
    log.write("\nrestore: discarding the failed run's outputs:\n" + before.stdout)
    # One path per call: a pathspec that matches nothing aborts the whole
    # checkout, and build/portfolio.html need not exist in every tree.
    for path in RESTORE_PATHS:
        _git(["checkout", "--", path], log, cwd=repo_root)
    # -fd without -x: ignored files (the price caches, logs/) are untouched.
    _git(["clean", "-fd", "--", "data/", "docs/"], log, cwd=repo_root)
    after = _git(["status", "--porcelain"], log, cwd=repo_root)
    clean = after.returncode == 0 and not after.stdout.strip()
    log.write("\nrestore: tree clean\n" if clean else
              "\nrestore: tree STILL dirty — the next firing will refuse:\n"
              + after.stdout)
    return clean


# ONE GREEN RUN PER LOCAL DAY PER CADENCE (2026-09-03).
#
# The hourly repeats need a way to tell "the day's work is done" from "this
# is the first firing". Until 2026-09-03 that test was "the S&P panel already
# reaches the last completed session", and the two-run weekend fell into its
# hole: after a green Saturday the panel reaches Friday, so SUNDAY also read
# as done and exited at once — the run that exists because sleeve D's
# European close settles only on Sunday never happened. Nobody had seen it,
# because the scheduled run had never yet succeeded on its own.
#
# The run now records its own green completion, keyed on the cadence and the
# LOCAL date the firing started (the schedule is local; see log_path_for).
# Saturday's marker does not satisfy Sunday; Tuesday's does not satisfy
# Wednesday; the 10:00 retry after a green 09:00 run exits as before.
GREEN_MARKER = LOG_DIR / "last_green_run.json"


def _local_date(when_utc: datetime, tz=None) -> str:
    return when_utc.astimezone(tz).date().isoformat()


def already_ran_today(marker: Path, cadence: str, now_utc: datetime,
                      tz=None) -> bool:
    """True when a green run of ``cadence`` already completed on the local
    date of ``now_utc``. A missing or unreadable marker means run."""
    try:
        rec = json.loads(Path(marker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(rec, dict) and rec.get("cadence") == cadence
            and rec.get("local_date") == _local_date(now_utc, tz))


def _read_marker(marker: Path) -> dict:
    try:
        rec = json.loads(Path(marker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def record_green_run(marker: Path, cadence: str, started_utc: datetime,
                     tz=None) -> Path:
    """Write the marker for a run that STARTED at ``started_utc`` — the
    firing's own date, so a run that crosses midnight is filed under the
    day it was scheduled for.

    THE DAILY ATTEMPT BUDGET IS PRESERVED THROUGH THIS WRITE (2026-09-16).
    It used to replace the record wholesale, which zeroed the counter: a
    green run that left its debt standing therefore restored the budget it
    had just spent, and the "bounded" daily override was not bounded at all.
    Reproduced by looping green-run / debt-rerun / green-run three times and
    reading the counter back at zero each cycle. The budget belongs to the
    DAY, not to the run, so a completion never refunds it.
    """
    marker = Path(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    prior = _read_marker(marker)
    rec = {
        "cadence": cadence,
        "local_date": _local_date(started_utc, tz),
        "started_utc": started_utc.isoformat(timespec="seconds"),
        "recorded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if (prior.get("cadence") == cadence
            and prior.get("debt_attempt_date") == _local_date(started_utc, tz)):
        rec["debt_attempt_date"] = prior["debt_attempt_date"]
        rec["debt_attempts"] = _marker_int(prior.get("debt_attempts"))
    marker.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return marker


# A green run that leaves the fill still unpublished — 2026-09-13, exactly —
# used to be indistinguishable from a healthy one, because the marker only
# ever answered "did a green run happen today". Outstanding or unreadable
# debt now makes a run proceed, but BOUNDEDLY: a vendor mid-retraction is
# not cleared by retrying, and an unbounded rule would re-run a one-to-four
# hour refresh on every firing for the rest of the window. Two attempts is a
# recovery plus one retry; six is a loop.
#
# The budget covers EVERY debt-driven proceed decision, not only the ones
# that override a green marker. A persistent UNKNOWN verdict with no marker
# present — an unreadable ledger, an unresolvable remote — would otherwise
# authorise a full refresh on all twelve catch-up firings of a Thursday and
# Friday, which is the same unbounded-work failure wearing the fail-safe
# direction as a disguise.
#
# ITS SCOPE, STATED EXACTLY (2026-09-16, third review, correcting an earlier
# description of it as global):
#
#   * PER MARKER, and each component keeps its own. The weekend pair writes
#     last_green_core.json and last_green_europe.json, so a catch-up day in
#     which the diagnostics keep failing allows two CORE attempts and two
#     EUROPE attempts, not two in total. That is the intended shape - the
#     two halves publish independently - and it is four full refreshes, not
#     two, on the worst day.
#   * PER LOCAL DATE and per cadence. Deleting the marker resets it, which
#     is the escape hatch the marker has always had.
#   * CAPTURE-ONLY WORK IS OUTSIDE IT. A --capture-only child returns before
#     the debt is read: it publishes nothing, so it owes nothing, and six
#     recover-europe-first firings legitimately produce six captures. That
#     is deliberate. Bringing collection under a publication budget would
#     stop the one activity that clears a vendor retraction.
#   * The re-exec after a pull rewrites this script does NOT double-spend:
#     it happens in the preflight, before any attempt is counted, and the
#     waited child spends exactly one.
MAX_DEBT_ATTEMPTS_PER_DAY = 2


def _marker_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def debt_attempts_today(marker: Path, cadence: str, now_utc: datetime,
                        tz=None) -> int:
    """How many debt-driven attempts have already fired on this local date."""
    rec = _read_marker(marker)
    if rec.get("cadence") != cadence:
        return 0
    if rec.get("debt_attempt_date") != _local_date(now_utc, tz):
        return 0
    return _marker_int(rec.get("debt_attempts"))


def record_debt_attempt(marker: Path, cadence: str, now_utc: datetime,
                        tz=None) -> int:
    """Count one debt-driven attempt against today's budget. Never raises.

    Written BEFORE the work, so a run that dies mid-refresh still spends its
    attempt. A budget that only counted completions would be refilled by
    every crash, which is the failure mode it exists to bound.
    """
    rec = _read_marker(marker)
    today = _local_date(now_utc, tz)
    n = (_marker_int(rec.get("debt_attempts")) + 1
         if rec.get("debt_attempt_date") == today
         and rec.get("cadence") == cadence else 1)
    rec.update(cadence=cadence, debt_attempt_date=today, debt_attempts=n)
    try:
        Path(marker).parent.mkdir(parents=True, exist_ok=True)
        Path(marker).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    except OSError:
        pass
    return n


def component_sequence(run_child, *, armed: bool, recover_first: bool = False) -> int:
    """One bounded recovery, with no publication from collection-only work."""
    if recover_first:
        rc = run_child("europe", capture_only=True)
        if rc not in (0, 3):
            return rc
    rc = run_child("core", capture_only=False)
    if rc and armed and not recover_first and rc in (3, 4):
        capture_rc = run_child("europe", capture_only=True)
        # A source failure (3) may still have refreshed the traded fund.
        # Core must judge its own inputs. A dirty/preflight failure must stop.
        if capture_rc not in (0, 3):
            return capture_rc
        rc = run_child("core", capture_only=False)
    if rc or not armed:
        return rc
    return run_child("europe", capture_only=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("auto", "all", "core", "europe"), default="auto",
                        help="Weekend default: publish verified core first, then attempt Europe independently.")
    parser.add_argument("--capture-only", action="store_true",
                        help="Collect Europe data without a core release; retain only ignored caches.")
    parser.add_argument("--recover-europe-first", action="store_true",
                        help="One recovery run: collect Europe first, then retry the normal core/Europe sequence.")
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
    parser.add_argument("--catch-up", action="store_true",
                        help="Proceed ONLY when this cadence still owes a "
                             "publication (publication_debt). A firing with "
                             "nothing owed exits 0 in seconds without "
                             "touching the refresh. This is what makes a "
                             "trigger outside the original Tue/Wed window "
                             "safe to add: the extra firings are free on a "
                             "healthy week and do the work on a broken one.")
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
    if args.capture_only and (args.component != "europe" or args.push or args.commit
                              or args.preflight_only):
        parser.error("--capture-only requires Europe and forbids commit, push and preflight-only")
    if args.recover_europe_first and (args.component != "auto" or args.cadence != "weekend"
                                     or args.preflight_only or not (args.push or args.commit)):
        parser.error("--recover-europe-first requires the armed weekend auto sequence")

    LOG_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    run_started = now          # the firing's own date, for the green-run marker
    log_path = log_path_for(now)
    log = open(log_path, "a", encoding="utf-8")
    # Both stamps on the header line: the local one is how the schedule and the
    # operator think, the UTC one is unambiguous across any future change of
    # machine or zone. Reading only one of them is what went wrong before.
    log.write(f"\n{'='*72}\nscheduled_refresh start "
              f"{now.astimezone():%Y-%m-%d %H:%M %Z} (= {now.isoformat()}) "
              f"(push={args.push}, preflight_only={args.preflight_only})\n{'='*72}\n")

    # ----- Alerting configuration, probed at START (2026-09-16) -----
    # Recorded before anything can fail, so an unconfigured channel is
    # visible to the independent watcher on the FIRST firing rather than
    # only once something has already gone wrong and failed to alert.
    # Names and presence only — never a value, never a length.
    creds = _safe(log, "credential probe", run_status.credential_state) or {
        "configured": False, "missing": list(run_status.CREDENTIAL_VARS)}
    log.write(f"\nalert configuration: {json.dumps(creds)}\n")
    if not creds["configured"] and _durable_state_enabled():
        _safe(log, "startup alert record", run_status.record_alert,
              run_status.alert_state_path(REPO_ROOT),
              subject="(startup probe)", status=run_status.UNCONFIGURED,
              detail=f"missing at start: {', '.join(creds['missing'])}")

    def _record_run(*a, **kw):
        if _durable_state_enabled():
            _safe(log, "run outcome record", run_status.record_run, *a, **kw)

    # Set once the debt is known; ``fail`` closes over the holder so a
    # failure recorded before that point simply carries no debt field.
    debt_seen: dict = {}

    def fail(code: int, subject: str, body: str) -> int:
        print(f"FAILED ({subject}) - see {log_path}")
        log.write(f"\nFAILED exit {code}: {subject}\n{body}\n")
        # BEFORE the rollback: the refusal detail lives in tracked files that
        # restore_tracked_outputs is about to check back out. See
        # refusal_report. Best-effort — a failure to read a roster must never
        # stop the restore that makes the next firing a retry.
        refusals = _safe(log, "roster refusal report", refusal_report) or ""
        if refusals:
            log.write(refusals)
            body = body + "\n" + refusals
        # A failure inside the refresh must not poison every later firing;
        # see restore_tracked_outputs.
        if code in RESTORE_ON_EXIT_CODES:
            restore_tracked_outputs(log)
        _email(f"[FAIL] Scheduled refresh - {subject}", body + f"\n\nLog: {log_path}", log)
        # Durable and machine-readable, in logs/ which the restore above
        # deliberately does not touch. Until this existed, a failed run
        # restored the tree and left no trace a program could read.
        _record_run(run_status.run_ledger_path(REPO_ROOT),
                              cadence=args.cadence, exit_code=code,
                              subject=subject, debt=debt_seen or None)
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
            log.write("\nthe pull rewrote this script; re-running it as a "
                      "waited child so both halves come from the same commit\n")
            log.flush()
            log.close()
            # WAITED, NOT EXEC'D (2026-09-03). This was os.execv, which does
            # not replace the process on Windows: it spawns the new
            # interpreter and exits the current one at once. Probed on
            # 2026-09-03 — the caller saw exit 0 after one second while the
            # child was still running and later exited 7. Under Task
            # Scheduler that recorded the firing as a success within seconds,
            # left the real run outside ExecutionTimeLimit and the
            # single-instance guard, and let every hourly repeat start a
            # fresh instance against the tree the detached run was writing.
            # A child this process WAITS for keeps it the scheduled instance
            # and returns the outcome that actually happened.
            env = dict(os.environ, BTE_SCHED_REEXEC="1")
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                cwd=REPO_ROOT, env=env)
            return child.returncode

    # Waited children preserve Task Scheduler single-instance protection.
    # Core is committed first; a Europe failure cannot roll it back.
    if args.capture_only:
        cmd = [sys.executable, "scripts/refresh_all.py", "--component", "europe",
               "--capture-only", "--price-source", args.price_source]
        log.write("\nIndependent Europe collection; no core release required, no publication.\n")
        log.flush()
        try:
            rc = subprocess.run(cmd, cwd=REPO_ROOT, stdout=log,
                                stderr=subprocess.STDOUT).returncode
        finally:
            # Preflight proved this clone clean. Keep raw ignored price caches,
            # but discard this collection's unsealed JSON and page outputs.
            clean = restore_tracked_outputs(log)
            log.write("\nEurope collection cleanup complete; no commit, push or email.\n")
            log.close()
        return (0 if rc == 0 else 3) if clean else 2

    if args.component == "auto" and args.cadence == "weekend" and not args.preflight_only:
        log.write("\ncomponent sequence: core then Europe; one independent Europe capture on core validation failure\n")
        log.close()

        def run_child(component, *, capture_only):
            child_args = [sys.executable, str(Path(__file__).resolve()), "--component", component,
                          "--cadence", args.cadence, "--price-source", args.price_source]
            # The flag travels. A parent that proceeded because something was
            # owed must not hand its children a plain run that then suppresses
            # itself on the green marker: the debt gate has to hold end to
            # end or it holds nowhere.
            if args.catch_up:
                child_args.append("--catch-up")
            if capture_only:
                child_args.append("--capture-only")
            elif args.push:
                child_args.append("--push")
            elif args.commit:
                child_args.append("--commit")
            else:
                print("Soak mode: validating core only; no commit, push or email.")
            return subprocess.run(child_args, cwd=REPO_ROOT).returncode

        return component_sequence(run_child, armed=bool(args.push or args.commit),
                                  recover_first=args.recover_europe_first)
    component = "all" if args.component == "auto" else args.component
    marker = GREEN_MARKER if component == "all" else LOG_DIR / f"last_green_{component}.json"
    if component == "core" and not args.preflight_only:
        release_path = REPO_ROOT / "data/component_release.json"
        if release_path.exists():
            try:
                from component_release import verify
                released = verify(REPO_ROOT, now, committed=True)
                if released["anchor"] == week_final_anchor(now).isoformat():
                    print("CORE ALREADY VERIFIED for this weekly decision; proceeding to Europe.")
                    log.close()
                    return 0
            except Exception:
                pass  # Changed sources require a new guarded refresh.

    # ----- Publication debt (2026-09-16) -----
    #
    # Read AFTER the pull, so the question is asked of what ORIGIN actually
    # carries rather than of whatever this clone was left holding, and read
    # BEFORE the green-run marker is consulted.
    #
    # The order is the whole point. The marker answers "did a green run
    # happen TODAY", which is the right gate for an hourly retry and says
    # nothing at all about whether the fill the pair exists to record ever
    # reached main. On 13 September a green run left Monday's fill
    # unpublished and the marker suppressed everything behind it; on 15 and
    # 16 September both windows closed on failures and the schedule moved on
    # to the following Tuesday with the hole still open. Nothing was owed,
    # because nothing tracked owing. Evaluating the debt after the marker —
    # which is where this check first went — reproduces that exactly.
    #
    # UNKNOWN IS NOT NOTHING. A debt verdict that could not read its
    # evidence makes the run PROCEED. The defect being repaired here is an
    # unreadable state reading green.
    debt = _safe(log, "publication debt", publication_debt.current_debt,
                 REPO_ROOT, now.astimezone().date(), cadence=args.cadence,
                 persist=_durable_state_enabled())
    if debt is None:
        log.write("\npublication debt: UNREADABLE; proceeding as if owed\n")
    else:
        debt_seen.update(debt.as_dict())
        log.write(f"\npublication debt: {json.dumps(debt.as_dict())}\n")
    debt_requires_run = (debt is None) or debt.should_run

    # NOT ON A PREFLIGHT-ONLY RUN, FOR EITHER ALERT TYPE (2026-09-17, ninth
    # review). The eighth pass suppressed NOTICES under --preflight-only and
    # left escalations mailing and marking, so the documented smoke test still
    # raised an overdue-publication alert and spent that obligation's
    # escalation budget for the day. A rehearsal must not consume the state the
    # performance depends on, and there is no reading under which one alert
    # type is a side effect and the other is not. The verdict is still written
    # to the log and still printed, so the smoke test still SHOWS what it
    # found.
    if debt is not None and debt.escalate and args.preflight_only:
        log.write("\n[preflight-only: escalation suppressed; the verdict "
                  "above is the whole output of this run]\n")
    if debt is not None and debt.escalate and not args.preflight_only:
        # Escalate through the alert channel, whose own health is now
        # independently observed. A missed publication deadline is not a
        # retry, and the run continues: the escalation is a notification,
        # not a refusal. Deduplicated per obligation per day and capped, so
        # an obligation that cannot be discharged does not train the
        # operator to ignore the channel.
        due = _safe(log, "escalation dedupe", publication_debt.escalation_due,
                    debt, now.astimezone().date()) or []
        if due:
            _email(f"[ESCALATION] {args.cadence} publication overdue - "
                   f"fill {debt.oldest_owed_fill} unrecorded for "
                   f"{debt.age_days} day(s)",
                   f"{debt.reason}\n\nGrace is {debt.grace_days} day(s). The "
                   f"published book and the traded book disagree until a "
                   f"{args.cadence} refresh publishes the sleeve rebalance "
                   f"records for fill {debt.oldest_owed_fill}.\n\n"
                   f"This measures PUBLICATION only. It says nothing about "
                   f"whether the broker filled anything.\n\n"
                   f"Obligations: {json.dumps(list(debt.obligations), indent=2)}"
                   f"\n\nLog: {log_path}", log)
            if _durable_state_enabled():
                _safe(log, "escalation record",
                      publication_debt.mark_escalated,
                      publication_debt.ledger_path(REPO_ROOT), due,
                      now.astimezone().date())

    # ----- Notices (2026-09-17) -----
    #
    # A condition an operator must hear about that is NOT an overdue
    # publication, and therefore can never escalate. A venue whose book has
    # stopped advancing owes nothing by construction: it produced a
    # catch-up run and a line in a log file nobody reads, which is the
    # shape of silence this whole workstream exists to remove. Bounded and
    # deduplicated exactly as an escalation is.
    # NOT ON A PREFLIGHT-ONLY RUN (2026-09-17, eighth review). --preflight-only
    # is the documented smoke test, invoked by hand to check that the clone is
    # clean and the gate preview works. It ran the notice block, so a smoke
    # test mailed an operational alert and spent a day of that notice's
    # dedupe - after which the real firing an hour later said nothing. A
    # rehearsal must not consume the state the performance depends on. The
    # condition is still printed and still in the verdict the run prints.
    if debt is not None and debt.notices and not args.preflight_only:
        ledger = publication_debt.ledger_path(REPO_ROOT)
        today = now.astimezone().date()
        # Stamp every LIVE condition first, whether or not it is due. A
        # condition that has spent its budget is never attempted again, so
        # without this its record would stop being refreshed and the retention
        # rule would eventually evict the very count that is keeping it quiet.
        observed = True
        if _durable_state_enabled():
            observed = bool(_safe(log, "notice conditions",
                                  publication_debt.record_notice_conditions,
                                  ledger, [n["key"] for n in debt.notices],
                                  today))
            if not observed:
                # A LEDGER THAT CANNOT BE WRITTEN AUTHORISES NOTHING
                # (2026-09-17, tenth review). The claim file bounds
                # repetition, but the attempt and delivery counts - the
                # budget that stops an unfixable condition training the
                # operator to ignore the channel - live in the ledger. A
                # successful claim over a dead ledger would mail on a budget
                # nothing is keeping.
                log.write("\n[notices withheld: the obligation ledger could "
                          "not record this run's observation, so the notice "
                          "budget cannot be maintained]\n")
        pending = ([] if not observed else
                   _safe(log, "notice dedupe", publication_debt.notices_due,
                         debt, ledger, today) or [])
        for notice in pending:
            # CLAIM, THEN SEND. ``notices_due`` is a read, so two overlapping
            # firings - which an hourly schedule over a one-to-four-hour
            # refresh produces routinely - can both select the same notice.
            # The claim is an O_EXCL file create: exactly one process wins it,
            # and only the winner mails. A claim that cannot be created at all
            # means nothing would bound the repetition, so nothing is sent.
            key = notice["key"]
            if _durable_state_enabled():
                claim = _safe(log, "notice claim", publication_debt.claim_notice,
                              ledger, key, today)
                if claim != publication_debt.NOTICE_CLAIMED:
                    log.write(f"\n[notice not sent: {key} - "
                              f"{claim or publication_debt.NOTICE_UNCLAIMABLE}"
                              f"]\n")
                    continue
                # Written before the send so the budget the next firing reads
                # is never behind the mail already gone out - and if it does
                # not persist, the claim alone does not authorise the send.
                # The claim is handed back so a later firing the same day can
                # take it once writes recover.
                if not _safe(log, "notice attempt",
                             publication_debt.record_notice_attempt, ledger,
                             [key], today):
                    log.write(f"\n[notice not sent: {key} - the attempt could "
                              f"not be recorded, so the send budget cannot be "
                              f"maintained; claim released]\n")
                    _safe(log, "notice claim release",
                          publication_debt.release_notice_claim, ledger, key,
                          today)
                    continue
            outcome = _email(
                f"[NOTICE] {args.cadence} - {notice['venue']} book has "
                f"stopped advancing",
                f"{notice['detail']}\n\nThe schedule cannot tell which "
                f"{notice['venue']} fills have occurred since, so the "
                f"publication debt reads UNKNOWN and every firing does the "
                f"work rather than trusting the book.\n\nThis measures the "
                f"BOOK, not the broker: it says live_targets.json stopped "
                f"moving, nothing about what traded.\n\nLog: {log_path}",
                log)
            # Only a message the server accepted spends the delivered budget.
            # An unconfigured or refused channel has told nobody anything, and
            # must not exhaust the budget that exists to stop the channel
            # being muted.
            #
            # If the server accepted it and THIS write fails, the mail is gone
            # and the ledger does not know: the notice goes out again on a
            # later day and the operator may receive it twice. The guarantee
            # is AT MOST ONCE WITHIN A DAY - the claim is exclusive - with a
            # cross-day retry only while the condition is still reported. It
            # is the direction to fail in; the alternative loses alerts.
            if _durable_state_enabled():
                _safe(log, "notice outcome",
                      publication_debt.record_notice_delivery, ledger, [key],
                      today,
                      publication_debt.NOTICE_DELIVERED
                      if outcome == run_status.SENT
                      else publication_debt.NOTICE_UNCONFIRMED)

    # ----- Already done today? -----
    # The scheduled task retries hourly and starts as soon as the machine is
    # available, so that a Saturday with the laptop shut still gets its
    # refresh when it opens. Without this exit every one of those retries
    # would re-run the whole ~1-4 hour refresh, and a long run could still be
    # going when the next hour fired. The test used to be "the S&P panel is
    # already current", which silently swallowed the SUNDAY run once Saturday
    # had succeeded — see the note above GREEN_MARKER. It is now "a green run
    # of this cadence already completed on this local date". Checked AFTER
    # the pull, so a rewritten wrapper is the one making the decision, and
    # after the debt, which can override it a bounded number of times a day.
    ran_today = (not args.preflight_only
                 and already_ran_today(marker, args.cadence, now))

    # --catch-up inverts the default: a firing outside the original window
    # runs ONLY if something is still owed or unreadable, and exits in
    # seconds otherwise. That is what makes extra triggers safe to add —
    # without it, widening the schedule means re-running a 1-4 hour refresh
    # against a book that is already published, which is how a duplicate
    # publication happens.
    if args.catch_up and not debt_requires_run and not args.preflight_only:
        msg = (f"NOTHING OWED - {debt.reason}. Catch-up firing exits without "
               f"running the refresh.")
        print(msg)
        log.write(f"\n{msg}\n")
        _record_run(run_status.run_ledger_path(REPO_ROOT),
                    cadence=args.cadence, exit_code=0,
                    subject="catch-up: nothing owed",
                    debt=debt.as_dict())
        log.close()
        return 0

    if ran_today and not debt_requires_run:
        msg = (f"ALREADY RAN TODAY - a green {args.cadence} run completed "
               f"on {now.astimezone().date().isoformat()} (local); this "
               f"firing is an hourly retry. Nothing to do.")
        print(msg)
        log.write(f"\n{msg}\n")
        _record_run(run_status.run_ledger_path(REPO_ROOT),
                    cadence=args.cadence, exit_code=0,
                    subject="already ran today",
                    debt=debt.as_dict() if debt else None)
        log.close()
        return 0

    # ----- The daily budget on debt-driven work (2026-09-16) -----
    #
    # Applies to EVERY firing that proceeds because of debt: a catch-up
    # firing, and a firing that overrides its own green marker. It used to
    # cover only the second, and only until the next green run rewrote the
    # marker and refunded the counter. Both holes are closed: the counter
    # survives ``record_green_run``, and an UNKNOWN verdict with no marker
    # at all is bounded by the same budget rather than authorising a full
    # refresh on every catch-up firing of the day.
    debt_driven = debt_requires_run and (args.catch_up or ran_today)
    if debt_driven and not args.preflight_only:
        spent = debt_attempts_today(marker, args.cadence, now)
        if spent >= MAX_DEBT_ATTEMPTS_PER_DAY:
            outstanding = (debt.oldest_owed_fill if debt and debt.oldest_owed_fill
                           else "unknown")
            msg = (f"DEBT ATTEMPT BUDGET SPENT "
                   f"({spent}/{MAX_DEBT_ATTEMPTS_PER_DAY}) for "
                   f"{now.astimezone().date().isoformat()}. STILL OWED: fill "
                   f"{outstanding} - "
                   f"{debt.reason if debt else 'debt unreadable'}")
            print(msg)
            log.write(f"\n{msg}\n")
            _record_run(run_status.run_ledger_path(REPO_ROOT),
                        cadence=args.cadence, exit_code=0,
                        subject="debt attempt budget spent",
                        debt=debt.as_dict() if debt else None)
            log.close()
            return 0
        n = record_debt_attempt(marker, args.cadence, now)
        log.write(f"\nproceeding on publication debt "
                  f"({'green marker overridden' if ran_today else 'catch-up'}); "
                  f"attempt {n}/{MAX_DEBT_ATTEMPTS_PER_DAY} today\n")

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
               "--price-source", args.price_source, "--component", component]
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
        # ----- AUTOMATIC RELEASE (2026-09-06, owner decision) -----
        # The release marker used to be written by a person after reading
        # the week. It is now a mechanical verdict taken here, by the run
        # that produced the book, on the conditions auto_release.py lists —
        # every sleeve final on its fill's close, data current, basis as
        # requested, no hold, readiness clean. On RELEASE the marker joins
        # this commit, the push triggers the gated workflow, and the
        # workflow mails the operator a notice with the items no script
        # judges. On HOLD nothing is written and the reasons are logged; the
        # Sunday check then names them. Never on a post-fill run.
        if not args.preflight_only and args.push and args.cadence == "weekend" and component == "all":
            import auto_release
            verdict = auto_release.release_if_ready(
                week_final_anchor(now), cadence=args.cadence,
                price_source=args.price_source)
            log.write("\n" + auto_release.format_report(verdict) + "\n")
            if verdict.get("marker"):
                log.write(f"release marker written: {verdict['marker']}\n")
        gate = build_gate_report("publish", now, PANEL, MARKER,
                                 release_path=RELEASE)
        if component != "all" and not args.preflight_only:
            from component_release import verify
            sealed = verify(REPO_ROOT, now)
            gate = {"detail": f"Sealed {component} release {sealed['anchor']}; D ready={sealed['d_ready']}. "
                              "The component sender checks the delivery ledger before emailing."}
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
            # The debt is deliberately NOT discharged by this state. The
            # commit exists locally and nothing was published, and the
            # obligation reads origin/main precisely so the next firing
            # still sees the work as owed and retries the push rather than
            # exiting on evidence that never left this machine.
            unpushed = publication_debt.unpushed_commits(REPO_ROOT,
                                                         "origin/main")
            return fail(5, "git push failed after 3 attempts",
                        f"Refresh is committed locally in the automation "
                        f"clone but not pushed after three rebase-and-retry "
                        f"attempts; push manually. {unpushed} local commit(s) "
                        f"are not on origin/main, and the publication debt "
                        f"stays outstanding until they are. " + cp.stderr)
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
    # One green run per local day per cadence — see GREEN_MARKER. Written on
    # every green outcome (pushed, committed locally, or READY), never on a
    # preflight-only run.
    if component != "europe" or sealed["d_ready"]:
        record_green_run(marker, args.cadence, run_started)
        log.write(f"\ngreen-run marker written: {marker.name} "
                  f"({args.cadence}, {run_started.astimezone().date().isoformat()})\n")
    else:
        log.write("\nD remains HOLD; do not suppress later Europe retries today.\n")
    # The debt AFTER the publication, so the ledger records whether this
    # green run actually discharged what it owed. A green run that leaves
    # the debt standing is the case the old green-run marker could not
    # express, and it is exactly what happened on 2026-09-13.
    after = _safe(log, "publication debt (after run)",
                  publication_debt.current_debt, REPO_ROOT,
                  now.astimezone().date(), cadence=args.cadence,
                  persist=_durable_state_enabled())
    if after is not None:
        log.write(f"\npublication debt after run: {json.dumps(after.as_dict())}\n")
        if after.should_run:
            log.write("\nNOTE: this run was green and the publication it owed "
                      "is STILL outstanding. That is the 2026-09-13 shape, and "
                      "it is now recorded rather than inferred.\n")
    _record_run(run_status.run_ledger_path(REPO_ROOT),
                cadence=args.cadence, exit_code=0,
                subject="green", debt=after.as_dict() if after else None)
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
