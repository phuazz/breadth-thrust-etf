"""Repair slot for the daily live track: dispatch the mark-to-market run when,
and only when, the committed series is behind the NYSE calendar.

WHY THIS EXISTS (2026-10-01). daily_live_track.yml is scheduled at 21:30 UTC,
but GitHub fired it 1h54m to 3h35m late on every weekday from 15 to 30
September, and from 25 September the start sat past 00:00 UTC. At 00:00 UTC
(20:00 ET, the after-hours close) Yahoo withdraws the just-completed US daily
bar for its overnight rebuild and serves the row as NaN until about 03:45
UTC, so a run that fetches inside that window captures the PREVIOUS session:
the capture check reports "1 session(s) behind", the operator gets a [WARN]
that reads as a stuck feed, and the dashboard publishes a close a day old.
Five of the eight weekday runs for the sessions 21 to 30 September did
exactly that (runs 35670927270, 36203097906, 36506264012, 36651199976 and
36797213088); the three that fired before 00:00 UTC were OK. The vendor-
availability probe never shows the gap because its slots (about 21:xx and
03:45 UTC) straddle it.

Moving the 21:30 slot earlier would not help: from the first Sunday of
November the NYSE close is 21:00 UTC, and before the close there is nothing
new to mark. This slot runs instead in the UTC morning and asks one question
of the COMMITTED data/live_track.json, with check_capture_integrity's own
evaluator so the two cannot disagree about "behind": does the deployed series
already reach the last completed NYSE session? If yes, nothing is dispatched
and nothing downstream runs (no rebuild, no scanner, no state emission, no
commit churn). If no, it dispatches daily_live_track.yml by workflow_dispatch
with reason=repair-slot; that run re-marks from the anchor under the same
guards as the scheduled slot, and it is the run that emails the capture
warning if the bar is STILL missing, which after the vendor window is the
signal worth reading.

WHY 08:17 UTC, TUESDAY TO FRIDAY. The local scheduled refresh runs in its own
clone from 09:00 SGT (01:00 UTC) on Tuesday to Friday with hourly catch-up
firings to 14:00 SGT, and a run has taken 77 minutes; a CI commit that lands
between one of those runs' preflight pull and its push rejects the local
push, and the retry's rebase of two different versions of the same data
files is a by-hand failure (scheduled_refresh.py exit 5). 08:17 UTC (16:17
SGT) is clear of that window with room for GitHub's delay. Saturday is
excluded for the same reason: the weekend refresh pushes Friday's re-anchor
between 01:00 and 07:35 UTC, and Monday's 21:30 slot marks Friday regardless.
Xetra is open at 08:17 UTC. That is safe for THIS job, which never ranks:
mark_to_market_live caps the extension at the last completed NYSE session and
export_holdings_prices trims each line to its own venue's last completed
session (tests/test_session_bounds.py). The README's "never refresh after
15:00 SGT" rule is about the engines.

The exit code is always 0: the decision is the GITHUB_OUTPUT `dispatch`. A
committed file that cannot be evaluated dispatches, because the daily job
rewrites it from the anchor and judges the result itself.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_capture_integrity as cci  # noqa: E402
from nyse_sessions import last_completed_session  # noqa: E402

# Set by the workflow from its `force` input: dispatch regardless of the
# verdict, to prove the plumbing once. Costs one full publish.
FORCE_ENV = "LIVE_TRACK_REPAIR_FORCE"


def live_verdict(expected: date, data_dir: Path | None = None) -> dict:
    """check_capture_integrity's own verdict on the committed live track."""
    label, fname, dpath, epath, apath = cci.TARGETS["live"]
    return cci.evaluate_target(label, (data_dir or cci.DATA_DIR) / fname,
                               dpath, epath, expected, anchor_path=apath)


def decide(verdict: dict, force: bool = False) -> tuple[bool, str]:
    """(dispatch, reason). Any verdict other than ok dispatches: warn is the
    withheld-bar case this slot exists for; fail is a corrupt or unreadable
    committed file, which the daily job rewrites from the anchor and then
    judges under its own 2-session threshold, loudly."""
    if force:
        return True, "forced by the operator (plumbing test)"
    if verdict["status"] == "ok":
        return False, "committed series already reaches the last completed session"
    return True, f"committed series {verdict['status']}: {verdict['evidence']}"


def main(argv: list[str] | None = None, *, data_dir: Path | None = None,
         now_utc: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true",
                        help="dispatch even if the committed series is current")
    args = parser.parse_args(argv)
    force = (args.force
             or os.environ.get(FORCE_ENV, "").strip().lower() == "true")

    expected = last_completed_session(now_utc or datetime.now(timezone.utc))
    verdict = live_verdict(expected, data_dir)
    dispatch, reason = decide(verdict, force)
    detail = "\n".join([
        f"expected last completed NYSE session: {expected.isoformat()}",
        f"{verdict['status'].upper():5s} {verdict['label']}: {verdict['evidence']}",
        ("DISPATCH daily_live_track.yml - " if dispatch else "SKIP - ") + reason,
    ])
    print(detail)
    cci.write_github_output({"dispatch": "true" if dispatch else "false",
                             "summary": reason}, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
