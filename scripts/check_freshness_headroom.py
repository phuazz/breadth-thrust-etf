"""Freshness headroom early warning for the CSP1 gating panel.

pipeline.py::assert_source_panel_fresh_vs_today hard-aborts every build
once data/breadth_csp1.json lags the run date by more than
DEFAULT_BUDGET_TRADING_DAYS weekdays — by which point the dashboard is
already frozen (26 Jun - 1 Jul 2026: four failed daily runs with no
notice). This script is the tripwire BEFORE the hard stop: both CI
workflows run it early and email a warning from weekday-lag 4, while
the pipeline still publishes.

Design rules:
  - Never blocks a build. Always exits 0, even on internal errors (an
    error sets warn=true — fail-safe towards alerting, never silence).
  - Reuses regime_publish's arithmetic so this checker cannot disagree
    with the guard it forecasts. Lag counts plain weekdays
    (numpy.busday_count); US market holidays consume budget by design —
    see the regime_publish module docstring.
  - Python datetime months are 1-indexed throughout (January = 1).

Outputs (stdout always; appended to $GITHUB_OUTPUT when set, which is
what the conditional email step in the workflows reads):
  warn    'true' | 'false' — email trigger, true from the warn-at lag
  status  'ok' | 'warn' | 'fail'
  tag     'OK' | 'REMINDER' | 'WARN' — email severity for the subject.
          Under the normal weekly cadence (local refresh_all.py each
          weekend) the panel ends every week at lag 4-5, so the Thu/Fri
          alerts describe a healthy steady state. Those are REMINDER
          tier: the warn band with at least one weekend day still ahead
          of the first failing run, i.e. the routine weekend refresh
          window can still clear it. WARN is reserved for states where
          that window is gone (mid-week staleness), the hard stop, or a
          checker error (fail-safe).
  lag     integer weekday lag of the panel behind the runner clock
  summary one line, used as the email subject tail
  detail  multi-line block, used as the email body
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# Allow importing sibling scripts/ modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from regime_publish import (  # noqa: E402
    DEFAULT_BUDGET_TRADING_DAYS,
    _trading_days_between,  # single source of lag arithmetic — do not reimplement
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PANEL = ROOT / "data" / "breadth_csp1.json"

# Warn threshold chosen 2026-07-03: lag 4 gives one to two scheduled
# weekday runs of notice before the hard stop first fires at lag 6.
WARN_AT_LAG = 4

# The daily cron fires 21:30 UTC Mon-Fri — the earlier of the two
# schedules, so it defines the remediation deadline.
RUN_TIME_UTC = time(21, 30)

# Singapore is fixed UTC+8 with no DST — a constant offset is exact and
# avoids a tzdata dependency on Windows dev machines.
SGT = timezone(timedelta(hours=8), name="SGT")


def panel_lag(panel_end: date, today: date) -> int:
    """Weekday lag exactly as the pipeline guard computes it.

    Returns 0 when the panel is current (or dated after ``today``, e.g.
    clock skew) — mirrors the guard's early return.
    """
    return _trading_days_between(panel_end, today)


def classify(
    lag: int,
    warn_at: int = WARN_AT_LAG,
    budget: int = DEFAULT_BUDGET_TRADING_DAYS,
) -> str:
    """'fail' = the hard guard aborts a build run at this lag;
    'warn' = within warn_at..budget (still publishes, remediation due);
    'ok' otherwise."""
    if lag > budget:
        return "fail"
    if lag >= warn_at:
        return "warn"
    return "ok"


def first_failing_run_date(
    panel_end: date,
    start: date,
    budget: int = DEFAULT_BUDGET_TRADING_DAYS,
) -> date:
    """First weekday on or after ``start`` whose scheduled run would trip
    the hard guard (lag > budget), assuming no local refresh lands first.

    Iterates calendar days keeping weekdays only — the crons fire Mon-Fri
    regardless of market holidays, so the run-date grid is weekdays, not
    NYSE sessions.
    """
    d = start
    for _ in range(budget * 7 + 21):  # generous bound; answer is near start
        if d.weekday() < 5 and panel_lag(panel_end, d) > budget:
            return d
        d += timedelta(days=1)
    raise RuntimeError("no failing run date found within bound — logic error")


def weekend_between(start: date, end: date) -> bool:
    """True when at least one Saturday or Sunday lies strictly between
    ``start`` and ``end`` (calendar days; both endpoints excluded).

    Used as the tier test: a weekend day before the first failing run
    means the operator's routine weekend refresh window is still ahead.
    """
    d = start + timedelta(days=1)
    while d < end:
        if d.weekday() >= 5:
            return True
        d += timedelta(days=1)
    return False


def email_tag(status: str, today: date, fail_day: date) -> str:
    """Email severity tier for the subject line.

    'REMINDER' = warn band with a weekend day still ahead of the first
    failing run — the structural end-of-week state under the weekly
    local-refresh cadence, a nudge rather than an alarm. 'WARN' = the
    warn band with no weekend left (mid-week staleness) or the hard
    stop itself. 'OK' = below the warn band (no email is sent).
    """
    if status == "ok":
        return "OK"
    if status == "warn" and weekend_between(today, fail_day):
        return "REMINDER"
    return "WARN"


def deadline_strings(run_day: date) -> tuple[str, str]:
    """The failing run's start moment as ('%a YYYY-MM-DD HH:MM UTC',
    '%a YYYY-MM-DD HH:MM SGT'). The SGT stamp lands the next calendar
    morning (21:30 UTC + 8h crosses midnight)."""
    utc_dt = datetime.combine(run_day, RUN_TIME_UTC, tzinfo=timezone.utc)
    sgt_dt = utc_dt.astimezone(SGT)
    return (
        utc_dt.strftime("%a %Y-%m-%d %H:%M UTC"),
        sgt_dt.strftime("%a %Y-%m-%d %H:%M SGT"),
    )


def build_report(panel_path: Path, today: date, warn_at: int) -> dict:
    """Compute lag, status, email tier, deadline and the messages."""
    blob = json.loads(panel_path.read_text(encoding="utf-8"))
    end_iso = blob.get("end_date")
    if not end_iso:
        raise RuntimeError(f"{panel_path.name} has no end_date field")
    panel_end = date.fromisoformat(end_iso)
    budget = DEFAULT_BUDGET_TRADING_DAYS
    lag = panel_lag(panel_end, today)
    status = classify(lag, warn_at=warn_at, budget=budget)
    fail_day = first_failing_run_date(panel_end, today, budget=budget)
    utc_s, sgt_s = deadline_strings(fail_day)
    tag = email_tag(status, today, fail_day)

    # Printed strings use plain ASCII only: the local dev console may not
    # be UTF-8 (Windows cp1252) and the alert path must never depend on
    # terminal encoding.
    if status == "fail":
        summary = (
            f"breadth_csp1 lag {lag}/{budget} weekdays - the hard guard "
            f"aborts builds NOW; run refresh_all.py and commit"
        )
    elif tag == "REMINDER":
        summary = (
            f"weekend panel refresh due - breadth_csp1 lag {lag}/{budget} "
            f"weekdays; run refresh_all.py before {utc_s} ({sgt_s})"
        )
    else:
        summary = (
            f"breadth_csp1 lag {lag}/{budget} weekdays - run refresh_all.py "
            f"before {utc_s} ({sgt_s})"
        )

    if tag == "REMINDER":
        tier_note = "routine end-of-week; the weekend refresh window is still ahead"
    elif status == "fail":
        tier_note = "hard stop reached - builds abort at this lag"
    else:
        tier_note = "no weekend left before the first failing run"

    detail = "\n".join([
        f"breadth_csp1.json end_date : {end_iso}",
        f"today (runner clock)       : {today.isoformat()}",
        f"weekday lag                : {lag} (warn at {warn_at}; hard stop when lag exceeds {budget})",
        f"status                     : {status.upper()}",
        f"email tier                 : {tag} ({tier_note})",
        f"first failing run          : {utc_s} = {sgt_s}",
        "action                     : run `python scripts/refresh_all.py` locally, commit and push before that run.",
        "note                       : lag counts plain weekdays; US market holidays consume budget"
        " (deliberate fail-early - see scripts/regime_publish.py).",
    ])
    return {
        "lag": lag,
        "status": status,
        "tag": tag,
        "summary": summary,
        "detail": detail,
    }


def write_github_output(values: dict[str, str], detail: str) -> None:
    """Append step outputs for the workflow's conditional email step.
    No-op outside GitHub Actions."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        for key, val in values.items():
            fh.write(f"{key}={val}\n")
        # Multi-line output uses the heredoc form. The delimiter cannot
        # occur in the detail text (fixed format, fully under our control).
        fh.write("detail<<HEADROOM_DETAIL_EOF\n")
        fh.write(detail.rstrip("\n") + "\n")
        fh.write("HEADROOM_DETAIL_EOF\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Early warning for breadth_csp1 freshness — warns "
        "before pipeline.py's hard guard starts aborting builds.",
    )
    parser.add_argument("--panel", default=str(DEFAULT_PANEL))
    parser.add_argument("--warn-at", type=int, default=WARN_AT_LAG)
    args = parser.parse_args(argv)
    try:
        # date.today() mirrors the hard guard's reference clock in
        # pipeline.py main(); the CI runner clock is UTC.
        report = build_report(Path(args.panel), date.today(), args.warn_at)
    except Exception as exc:  # fail-safe: alert rather than stay silent
        summary = f"headroom check could not run: {exc}"
        print(f"WARN {summary}")
        # status 'fail' + tag 'WARN': an unreadable panel is closer to the
        # hard stop than to a routine reminder, and the weekly workflow
        # (which only emails at status 'fail') must not swallow it.
        write_github_output(
            {
                "warn": "true",
                "status": "fail",
                "tag": "WARN",
                "lag": "-1",
                "summary": summary,
            },
            summary,
        )
        return 0

    warn = report["status"] in ("warn", "fail")
    print(report["detail"])
    write_github_output(
        {
            "warn": "true" if warn else "false",
            "status": report["status"],
            "tag": report["tag"],
            "lag": str(report["lag"]),
            "summary": report["summary"],
        },
        report["detail"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
