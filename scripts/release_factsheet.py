"""Release, hold, or rescind the weekly factsheet's publication.

Writes docs/factsheet_release.json, the marker check_factsheet_gate.py
requires before a refresh push may email the distribution list.

SINCE 2026-09-06 THE RELEASE IS AUTOMATIC. The weekend refresh takes the
verdict itself (scripts/auto_release.py) and writes this marker when every
condition is met — every sleeve final on its fill's close, data current,
price basis as requested, readiness clean, no hold. The commands here are
the operator's exceptions:

    python scripts/release_factsheet.py --hold "reason"   # veto: no automatic
                                                            send until --unhold
    python scripts/release_factsheet.py --unhold
    python scripts/release_factsheet.py                    # release by hand
                                                            (readiness must pass)
    python scripts/release_factsheet.py --force            # ...even if it does not
    python scripts/release_factsheet.py --clear            # rescind a release

A hold is a file (docs/factsheet_hold.json); commit and push it, since the
automatic verdict runs in the automation clone and reads main. A manual
release is still a countersignature: it refuses a week whose readiness
checks fail, so "release" cannot become a reflex that outruns the checking.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "docs" / "factsheet_release.json"
HOLD = ROOT / "docs" / "factsheet_hold.json"


def write_release_marker(anchor: date, note: str = "", forced: bool = False,
                         auto: bool = False, conditions: list | None = None,
                         out: Path = OUT) -> Path:
    """One writer for both paths, so the gate reads one shape. ``auto``
    marks a release taken by the weekend refresh; ``conditions`` is its
    evidence, kept beside the verdict rather than only in a log."""
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "approved_anchor": anchor.isoformat(),
        "approved_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forced": bool(forced),
        "note": note,
        "auto": bool(auto),
    }
    if conditions:
        payload["conditions"] = conditions
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def write_hold(note: str, out: Path = HOLD) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "held_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": note,
    }, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                     help="Release even though the readiness checks fail.")
    ap.add_argument("--note", default="",
                     help="Free text recorded alongside the release.")
    ap.add_argument("--clear", action="store_true",
                     help="Remove the release marker for this week.")
    ap.add_argument("--hold", metavar="REASON", default=None,
                     help="Veto automatic sends until --unhold; the reason is "
                          "recorded and reported by the Sunday check.")
    ap.add_argument("--unhold", action="store_true",
                     help="Lift the hold; automatic release resumes.")
    args = ap.parse_args()

    if args.hold is not None:
        p = write_hold(args.hold)
        print(f"HOLD written -> {p.relative_to(ROOT)}: {args.hold or 'no reason given'}")
        print("Commit and push it. No automatic release will fire until "
              "`release_factsheet.py --unhold`; a manual dispatch still sends.")
        return 0
    if args.unhold:
        if HOLD.exists():
            HOLD.unlink()
            print(f"Hold lifted ({HOLD.relative_to(ROOT)} removed) — automatic "
                  f"release resumes on the next weekend run. Commit and push.")
        else:
            print("No hold was in place.")
        return 0

    from nyse_sessions import week_final_anchor
    anchor = week_final_anchor(datetime.now(timezone.utc))

    if args.clear:
        if OUT.exists():
            OUT.unlink()
            print(f"Cleared {OUT.relative_to(ROOT)} — this week's release is rescinded.")
        else:
            print("No release marker present.")
        return 0

    if not args.force:
        rc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_publish_readiness.py")],
            cwd=ROOT).returncode
        if rc != 0:
            print("\nNot released: the readiness checks failed. Fix them, or "
                  "pass --force if you have a specific reason.", file=sys.stderr)
            return 1
        print()

    write_release_marker(anchor, note=args.note, forced=args.force, auto=False)
    print(f"Released {anchor.isoformat()} for publication "
          f"-> {OUT.relative_to(ROOT)}"
          + ("  (FORCED past failing checks)" if args.force else ""))
    print("Commit and push it; the factsheet sends on the next refresh push, "
          "or dispatch 'Weekly factsheet' to send now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
