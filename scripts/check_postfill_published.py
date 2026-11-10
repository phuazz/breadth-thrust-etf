"""Thursday miss alert: has origin/main recorded the latest weekly fill?

Runs in CI (.github/workflows/postfill_miss_alert.yml, Thursday 08:00 UTC and
workflow_dispatch). The post-fill refresh runs only on the owner's PC, so a
machine that was off all week publishes nothing and says nothing; this is the
check that does not depend on that machine. 08:00 UTC is 16:00 SGT, after the
last Thursday PostFillCatchup slot (15:00 SGT), with Friday's catch-ups still
ahead of it - so a mail here is actionable the same day.

Verdict (see postfill_publication.assess for the rule and the evidence that
``deployed_series_end`` alone is not enough):

  ok       every obliged sleeve's latest_rebalance and the live track's
           anchor_date + deployed_series_end reach the fill. No mail.
  held     only sleeves on an AUTHORISED HOLD are behind. Reported in the job
           summary, not mailed (the brief: a sanctioned HOLD is not a miss).
  missed   the record has not reached the fill. Mailed.
  unknown  the record or the calendar could not be read. Mailed - an
           unreadable state is never spelled green.

Outputs (GITHUB_OUTPUT when set): verdict, mail (true/false), subject, and a
body file path. The exit code is 0 for every verdict, so the job stays green
and the mail step decides; a crash exits non-zero and the workflow's failure
mail fires instead. fleet_watch's run row keys on the last successful run, so
a check that stops running at all, or whose mail step fails, goes stale there.

The HOLD authorisation reads data/component_release.json from the WORKING
TREE (publication_debt.release_authorisation verifies it against its own
committed seal). In CI the checkout is origin/main, so tree and ref agree.
Run locally with --ref other than the checked-out commit, a HOLD may be
judged against the wrong release; the verdict says so in its notes.

    python scripts/check_postfill_published.py                    # asof = today (UTC)
    python scripts/check_postfill_published.py --asof 2026-11-12 --ref origin/main --fetch

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import postfill_publication as pp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_check(repo_root: Path, ref: str, asof: date, hold_ok=None) -> tuple[pp.Assessment, str | None]:
    fills = pp.latest_fills(asof)
    reader = pp.git_reader(repo_root)
    book = pp.book_for_fill(pp.targets_history(repo_root, ref), fills)
    now = datetime.combine(asof, datetime.min.time(), tzinfo=timezone.utc)
    a = pp.assess(fills, pp.sleeve_records(reader, ref), pp.live_track_state(reader, ref),
                  book, hold_ok or pp._production_hold_ok(repo_root, book, now))
    since = min((d for d in fills.values() if d), default=asof)
    return a, pp.postfill_commit(repo_root, ref, since)


def report(a: pp.Assessment, commit: str | None, ref: str, asof: date) -> tuple[bool, str, str]:
    """(mail, subject, body)."""
    fill = pp.fills_text(a.fills)
    head = {pp.OK: f"[OK] Post-fill record reaches fill {fill}",
            pp.HELD: f"[HELD] Fill {fill}: sleeve(s) {','.join(a.behind)} on an authorised HOLD",
            pp.MISSED: f"[MISS] Post-fill refresh has not published fill {fill}",
            pp.UNKNOWN: f"[UNKNOWN] Post-fill record for fill {fill} could not be read"}[a.verdict]
    body = [f"Checked {ref} as at {asof.isoformat()} (UTC).",
            f"Newest 'Local post-fill refresh' commit since the fill: {commit or 'none'}",
            "", pp.describe(a)]
    if a.verdict == pp.MISSED:
        body += ["", "The post-fill refresh runs only on the local PC (BreadthThrust-"
                 "PostFillRefresh Tue/Wed 09:00 SGT, PostFillCatchup Thu/Fri hourly). "
                 "Check that the machine is on and the Norgate feed is reachable; "
                 "Friday's catch-up slots are still ahead. The Monday fill is not "
                 "re-anchored on the published book until a post-fill run lands."]
    return a.verdict in (pp.MISSED, pp.UNKNOWN), head, "\n".join(body)


def _write_outputs(verdict: str, mail: bool, subject: str, body_path: Path) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"verdict={verdict}\nmail={'true' if mail else 'false'}\n"
                     f"subject={subject}\nbody_path={body_path}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## {subject}\n\n```\n{body_path.read_text(encoding='utf-8')}\n```\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--asof", type=date.fromisoformat, default=None,
                    help="check date (UTC calendar date); default today")
    ap.add_argument("--fetch", action="store_true", help="git fetch origin main first")
    ap.add_argument("--body-out", default="postfill_check_body.txt")
    args = ap.parse_args(argv)
    asof = args.asof or datetime.now(timezone.utc).date()
    if args.fetch:
        subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=REPO_ROOT, check=False)
    a, commit = run_check(REPO_ROOT, args.ref, asof)
    mail, subject, body = report(a, commit, args.ref, asof)
    body_path = Path(args.body_out).resolve()
    body_path.write_text(body + "\n", encoding="utf-8")
    print(subject)
    print(body)
    _write_outputs(a.verdict, mail, subject, body_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
