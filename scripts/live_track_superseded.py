"""Is this run's live-track publish already superseded on origin?

Used by daily_live_track.yml when its push is rejected (2026-10-01). The
rejection usually comes from a probe or scanner commit touching other files,
and the step then rebases and retries as it always has. The case this script
exists for is a LOCAL refresh landing while the run was in flight: the
scheduled refresh in the automation clone rewrites the same generated files
this job rewrites, with a re-anchored and fully guarded book, and rebasing
this job's forward-only extension over that commit either conflicts (the job
fails loudly, over a dashboard that is in fact current) or auto-merges two
regenerated versions of one artefact. Neither is wanted. If origin's
data/live_track.json already reaches the last completed NYSE session, this
run has nothing to add and the step exits 0 without publishing.

The verdict is check_capture_integrity's own, applied to the file as origin
holds it, so "reaches the session" means exactly what the capture check and
the repair slot mean by it.

Exit codes: 0 superseded (origin already current) / 1 not superseded (publish
by rebase-and-retry, as before) / 2 origin's file could not be read or judged
(the workflow treats that as not superseded).

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_capture_integrity as cci  # noqa: E402
from nyse_sessions import last_completed_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def ref_verdict(ref: str, expected: date, repo_root: Path = ROOT) -> dict | None:
    """check_capture_integrity's verdict on ``<ref>:data/live_track.json``,
    or None when the ref or the file cannot be read."""
    label, fname, dpath, epath, apath = cci.TARGETS["live"]
    cp = subprocess.run(["git", "show", f"{ref}:data/{fname}"], cwd=repo_root,
                        capture_output=True, text=True, encoding="utf-8")
    if cp.returncode != 0:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / fname
        path.write_text(cp.stdout, encoding="utf-8")
        return cci.evaluate_target(label, path, dpath, epath, expected,
                                   anchor_path=apath)


def main(argv: list[str] | None = None, *, repo_root: Path = ROOT,
         now_utc: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="origin/main",
                        help="the ref that rejected this run's push "
                             "(the caller fetches it first)")
    args = parser.parse_args(argv)
    expected = last_completed_session(now_utc or datetime.now(timezone.utc))
    verdict = ref_verdict(args.ref, expected, repo_root)
    if verdict is None:
        print(f"could not read {args.ref}:data/live_track.json; not superseded")
        return 2
    print(f"expected last completed NYSE session: {expected.isoformat()}")
    print(f"{verdict['status'].upper():5s} {args.ref} live track: "
          f"{verdict['evidence']}")
    if verdict["status"] == "ok":
        print(f"SUPERSEDED - {args.ref} already reaches the expected session; "
              f"nothing to publish")
        return 0
    print(f"NOT SUPERSEDED - {args.ref} is behind; publish by rebase-and-retry")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
