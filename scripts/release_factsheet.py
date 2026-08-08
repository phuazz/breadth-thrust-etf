"""Release the current week's factsheet for automatic publication.

Writes docs/factsheet_release.json, the marker check_factsheet_gate.py
requires before a refresh push may email the distribution list. Commit and
push it; the next refresh push (or a re-push of the current one) sends.

This is a countersignature, not a convenience. It refuses to release a week
whose readiness checks fail, so "release" cannot become a reflex that
outruns the checking — pass --force only when you have a specific reason
and know what the failures are.

Run:
    python scripts/check_publish_readiness.py     # first
    python scripts/release_factsheet.py           # then
    python scripts/release_factsheet.py --clear   # rescind a release
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "docs" / "factsheet_release.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                     help="Release even though the readiness checks fail.")
    ap.add_argument("--note", default="",
                     help="Free text recorded alongside the release.")
    ap.add_argument("--clear", action="store_true",
                     help="Remove the marker, holding all automatic sends.")
    args = ap.parse_args()

    from nyse_sessions import week_final_anchor
    anchor = week_final_anchor(datetime.now(timezone.utc))

    if args.clear:
        if OUT.exists():
            OUT.unlink()
            print(f"Cleared {OUT.relative_to(ROOT)} — automatic sends are held.")
        else:
            print("No release marker present; automatic sends were already held.")
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

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "approved_anchor": anchor.isoformat(),
        "approved_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forced": bool(args.force),
        "note": args.note,
    }, indent=2) + "\n", encoding="utf-8")

    print(f"Released {anchor.isoformat()} for publication "
          f"-> {OUT.relative_to(ROOT)}"
          + ("  (FORCED past failing checks)" if args.force else ""))
    print("Commit and push it; the factsheet sends on the next refresh push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
