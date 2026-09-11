"""Pre-trade readiness check — is the instruction built before the next fill?

WHY THIS EXISTS, and why it is not check_factsheet_gate.py.

Since WS18 (2026-08-22), the book ranks on Friday's close and normally fills
in the Monday CLOSING auctions. The instruction is produced by the local weekend
refresh pair; Sunday 09:00 SGT is the full-book run after sleeve D's Friday
Xetra close has settled. It cannot run in CI because the per-constituent
parquet caches are gitignored. If the weekend pair does not land, nothing is
built, and this check reports it before Monday's trade. The existing Sunday
09:00 UTC factsheet check is a reconciliation question, not a pre-trade one.

This asks the pre-trade question instead: does the committed panel reach the
session the decision reads? It is deliberately a different question from
publishability, because week_final_anchor is unanswerable mid-week — it points
at the previous week by design, so a check built on it would pass on a Friday
morning while the panel sat six days stale.

ONE DEFINITION OF READY. The test itself is imported from
scheduled_refresh.panel_is_current rather than restated here, so the local
guard that decides whether to push and the CI guard that decides whether to
alarm cannot drift apart. That drift is exactly how a re-timed guard goes
quietly blind, which is the defect this whole change set began with.

WHAT IT DOES NOT DO. It cannot rebuild the panel — only the operator's machine
can. It reports, and it never blocks: exit is always 0, and an internal error
sets warn=true, so the failure mode is a spurious email rather than silence.

Outputs (stdout always; appended to $GITHUB_OUTPUT when set, which is what the
conditional email step in the workflow reads):
  warn    'true' | 'false' — email trigger
  status  'ready' | 'not_ready' | 'error'
  tag     'OK' | 'PRE-TRADE' | 'WARN' — email severity for the subject
  summary one line, used as the email subject tail
  detail  multi-line block, used as the email body

Python datetime months are 1-indexed (January = 1).

Usage:
    python scripts/check_pretrade_ready.py
    python scripts/check_pretrade_ready.py --now 2026-09-13T06:00:00Z   # test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nyse_sessions import last_completed_session  # noqa: E402
from scheduled_refresh import panel_is_current  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PANEL = REPO_ROOT / "data" / "breadth_csp1.json"


def build_report(panel_path: Path, now_utc: datetime) -> dict:
    """Decide readiness and compose the operator-facing text."""
    try:
        blob = json.loads(panel_path.read_text(encoding="utf-8"))
        panel_end = date.fromisoformat(blob["end_date"])
    except Exception as exc:  # noqa: BLE001
        # Fail toward alerting: an unreadable panel is not evidence of health.
        return {
            "warn": "true", "status": "error", "tag": "WARN",
            "summary": f"panel unreadable ({type(exc).__name__})",
            "detail": (
                f"Could not read {panel_path.name} to decide pre-trade "
                f"readiness: {exc!r}\n\n"
                "Treating this as NOT ready. Check the panel file and the "
                "last local refresh before trading."),
        }

    needed = last_completed_session(now_utc)
    ready = panel_is_current(panel_end, now_utc)
    stale_days = (needed - panel_end).days

    if ready:
        return {
            "warn": "false", "status": "ready", "tag": "OK",
            "summary": f"panel current to {panel_end.isoformat()}",
            "detail": (
                f"Pre-trade check PASSED at {now_utc.isoformat()}.\n"
                f"  panel end_date          : {panel_end.isoformat()}\n"
                f"  last completed session  : {needed.isoformat()}\n\n"
            "The panel reaches Friday's decision session, so the instruction "
            "is built for the next scheduled fill."),
        }

    return {
        "warn": "true", "status": "not_ready", "tag": "PRE-TRADE",
        "summary": (f"panel at {panel_end.isoformat()}, needs "
                    f"{needed.isoformat()} ({stale_days}d behind)"),
        "detail": (
            f"Pre-trade check FAILED at {now_utc.isoformat()}.\n"
            f"  panel end_date          : {panel_end.isoformat()}\n"
            f"  last completed session  : {needed.isoformat()}\n"
            f"  behind by               : {stale_days} calendar days\n\n"
            "The committed panel does NOT reach Friday's decision session, "
            "so no current instruction has been built for the next scheduled "
            "fill.\n\n"
            "Most likely cause: the Saturday/Sunday local refresh pair did "
            "not complete or did not push. The scheduled task starts at 09:00 "
            "SGT, retries hourly until early afternoon SGT, and starts when "
            "the machine becomes available.\n\n"
            "To act manually:\n"
            "  use the dedicated automation clone, not an interactive tree\n"
            "  python scripts/scheduled_refresh.py     (soak: validates, no push)\n"
            "  then review and push, which triggers the rest of the chain.\n\n"
            "The next fill is normally in the Monday CLOSING auctions: Xetra "
            "23:30 SGT on Monday evening (sleeve D), US 04:00 SGT on Tuesday "
            "(sleeves A/B/C), one hour later in winter. Check the exchange "
            "calendar for a holiday displacement. Market-on-close orders must "
            "be in before 15:50 New York time. Do not trade on the stale card."),
    }


def write_github_output(report: dict) -> None:
    """Append step outputs for the workflow's conditional email step.
    No-op outside GitHub Actions."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        for key in ("warn", "status", "tag", "summary"):
            fh.write(f"{key}={report[key]}\n")
        fh.write("detail<<PRETRADE_DETAIL_EOF\n")
        fh.write(report["detail"].rstrip("\n") + "\n")
        fh.write("PRETRADE_DETAIL_EOF\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", default=str(DEFAULT_PANEL))
    ap.add_argument("--now", default=None,
                    help="ISO-8601 UTC instant to evaluate at, for testing.")
    args = ap.parse_args(argv)

    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    try:
        report = build_report(Path(args.panel), now)
    except Exception as exc:  # noqa: BLE001
        report = {
            "warn": "true", "status": "error", "tag": "WARN",
            "summary": f"checker error ({type(exc).__name__})",
            "detail": f"check_pretrade_ready itself failed: {exc!r}",
        }

    print(f"[{report['tag']}] {report['summary']}")
    print(report["detail"])
    write_github_output(report)
    # Never blocks: the workflow decides what to do with `warn`.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
