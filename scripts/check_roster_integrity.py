"""Did this refresh punch holes in any constituent roster?

WHY THIS EXISTS.

fetch_constituents records an upstream outage honestly: on a transport
failure the EndpointCircuit trips, no snapshot is written, no carry-forward is
fabricated, and the affected Fridays land in the roster file's
``endpoint_unavailable`` array. The comment in that module is exact — "the
honest record of an outage is absence, not a fabricated roster".

Nothing consumed it. Grepping refresh_all, check_capture_integrity, pipeline
and scheduled_refresh for ``endpoint_unavailable`` returned one hit, in
build_data_audit, which counts it for display. So the array was written and
never acted on, and this failure was available:

    the network drops during ETF 13 of 24. Sleeves A and D lose roster history
    for the remaining ETFs. breadth_csp1 was written earlier and still reaches
    Thursday, so the wrapper's anchor guard PASSES, the run reports READY, and
    breadth for the later ETFs is computed on rosters with holes.

The anchor guard cannot catch it. It asks "does the panel reach the session
the decision reads", not "was every roster complete" — different questions,
which is the same distinction that let a re-timed week_final_anchor go blind.

WHAT IT CHECKS, and why not simply "zero".

Some gaps are legitimate and already committed: an endpoint that was down
during a past refresh leaves a permanent, honest hole. Demanding zero would
fail forever on history nobody intends to repair. So this compares against the
COMMITTED state and fails only on entries this run introduced.

Exit 0 = no new holes. Exit 1 = new holes, do not push. Exit 2 = cannot tell.

Usage:
    python scripts/check_roster_integrity.py
    python scripts/check_roster_integrity.py --repo C:/dev/breadth-thrust-etf-sched
    python scripts/check_roster_integrity.py --baseline HEAD
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _counts_from_text(text: str) -> tuple[int, int]:
    """(n_endpoint_unavailable, n_snapshots) for one roster blob."""
    blob = json.loads(text)
    return (len(blob.get("endpoint_unavailable") or []),
            len(blob.get("snapshots") or {}))


def _committed_text(repo: Path, rel: str, baseline: str) -> str | None:
    """The file as of `baseline`, or None when it is not tracked there."""
    cp = subprocess.run(["git", "show", f"{baseline}:{rel}"],
                        cwd=repo, capture_output=True, text=True)
    return cp.stdout if cp.returncode == 0 else None


def evaluate(repo: Path, baseline: str = "HEAD") -> dict:
    rosters = sorted((repo / "data").glob("constituents_*.json"))
    if not rosters:
        return {"ok": False, "undetermined": True, "rows": [],
                "summary": f"no constituent rosters found under {repo / 'data'}"}

    rows, new_holes, unreadable = [], 0, 0
    for path in rosters:
        rel = f"data/{path.name}"
        try:
            now_holes, now_snaps = _counts_from_text(
                path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            unreadable += 1
            rows.append({"etf": path.stem.replace("constituents_", "").upper(),
                         "error": repr(exc)})
            continue
        was = _committed_text(repo, rel, baseline)
        if was is None:
            was_holes = 0          # untracked: everything in it is new
        else:
            try:
                was_holes, _ = _counts_from_text(was)
            except Exception:  # noqa: BLE001
                was_holes = 0
        delta = now_holes - was_holes
        if delta > 0:
            new_holes += delta
        rows.append({
            "etf": path.stem.replace("constituents_", "").upper(),
            "holes_now": now_holes, "holes_before": was_holes,
            "new": delta, "snapshots": now_snaps,
        })

    if unreadable:
        return {"ok": False, "undetermined": True, "rows": rows,
                "summary": f"{unreadable} roster file(s) unreadable — cannot "
                           "certify roster integrity"}
    return {
        "ok": new_holes == 0, "undetermined": False, "rows": rows,
        "new_holes": new_holes,
        "summary": ("no new roster holes" if new_holes == 0 else
                    f"{new_holes} NEW endpoint_unavailable entr"
                    f"{'y' if new_holes == 1 else 'ies'} introduced by this run"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--baseline", default="HEAD",
                    help="git rev to compare against (default HEAD)")
    args = ap.parse_args(argv)

    r = evaluate(Path(args.repo), args.baseline)
    flagged = [x for x in r["rows"] if x.get("new", 0) > 0 or "error" in x]
    print(f"Roster integrity — {len(r['rows'])} rosters, baseline {args.baseline}")
    if flagged:
        for x in flagged:
            if "error" in x:
                print(f"  [UNREADABLE] {x['etf']}: {x['error']}")
            else:
                print(f"  [NEW HOLES]  {x['etf']}: {x['holes_before']} -> "
                      f"{x['holes_now']} ({x['new']} new)")
    else:
        print("  no roster gained an endpoint_unavailable entry")
    print(f"\nVERDICT: {r['summary']}")
    if r["undetermined"]:
        return 2
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
