"""Record that the weekly factsheet email for a given anchor went out.

Written by weekly_factsheet.yml immediately after a successful non-trial
email and committed back to main. check_factsheet_gate.py reads this
marker to decide "already published this week" — factsheet_meta.json
cannot serve because the daily mark-to-market runs re-stamp its as-of
every weekday.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MARKER = ROOT / "docs" / "factsheet_published.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--anchor", required=True,
        help="ISO date of the week-final session the emailed factsheet covers",
    )
    parser.add_argument("--marker", default=str(DEFAULT_MARKER))
    args = parser.parse_args(argv)
    anchor = date.fromisoformat(args.anchor)  # validates the format
    payload = {
        "anchor": anchor.isoformat(),
        "published_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    Path(args.marker).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(f"factsheet publish marker written: {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
