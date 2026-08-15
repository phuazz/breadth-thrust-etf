"""Did this vendor-availability probe record anything usable?

WHY (CLAUDE.md: no unattended run without a guard).

The failure mode is not a crash. It is a probe that exits 0 having recorded
nothing usable — yfinance answers with empty frames, every last_bar comes back
null, the log grows a row, the workflow stays green, and the cadence question
stays unanswered. A run of those silently costs the measurement window the
probe exists to fill, and a hole is indistinguishable from "nothing to report"
after the fact.

So this fails the job when:
  - the log gained no row from this run,
  - the newest row is not actually from this run (a stale file re-read), or
  - every probed line came back with no last_bar (the network answered nothing).

A PARTIAL result deliberately PASSES. One venue answering while the other does
not is itself an observation about that venue, and it is the observation the
probe is for — refusing it would discard the very asymmetry being measured.
The row records which lines were empty, so the analysis can see it.

The workflow runs this BEFORE committing, so the log never gains rows the
guard has not endorsed.

Exit 0 = usable. Exit 1 = nothing usable, do not commit. Exit 2 = cannot tell.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG = PROJECT_ROOT / "data" / "vendor_availability_log.jsonl"


def evaluate(log_path: Path, now_utc: datetime | None = None,
             max_age_minutes: int = 90) -> dict:
    now = now_utc or datetime.now(timezone.utc)
    if not log_path.exists():
        return {"ok": False, "undetermined": True,
                "summary": f"no log at {log_path}"}
    lines = [x for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not lines:
        return {"ok": False, "undetermined": True, "summary": "log is empty"}
    try:
        latest = json.loads(lines[-1])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "undetermined": True,
                "summary": f"newest row is not readable JSON: {exc!r}"}

    stamped = latest.get("probed_at_utc")
    try:
        when = datetime.fromisoformat(stamped)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return {"ok": False, "undetermined": True,
                "summary": f"newest row has an unreadable timestamp: {stamped!r}"}

    age = now - when
    if age > timedelta(minutes=max_age_minutes):
        return {"ok": False, "undetermined": False, "rows": len(lines),
                "summary": (f"newest row is {int(age.total_seconds()//60)} min "
                            f"old ({stamped}) — this run appended nothing")}

    rows = latest.get("rows") or []
    if not rows:
        return {"ok": False, "undetermined": False, "rows": len(lines),
                "summary": "this run's row carries no probed lines"}
    served = [r for r in rows if r.get("last_bar")]
    empty = [r["ticker"] for r in rows if not r.get("last_bar")]
    if not served:
        return {"ok": False, "undetermined": False, "rows": len(lines),
                "summary": (f"all {len(rows)} probed line(s) came back empty — "
                            f"the network answered nothing, so this run "
                            f"measured nothing")}
    return {
        "ok": True, "undetermined": False, "rows": len(lines),
        "served": len(served), "empty": empty,
        "summary": (f"{len(served)}/{len(rows)} lines served"
                    + (f"; empty: {', '.join(empty)}" if empty else "")),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=str(LOG))
    ap.add_argument("--max-age-minutes", type=int, default=90)
    args = ap.parse_args(argv)

    r = evaluate(Path(args.log), max_age_minutes=args.max_age_minutes)
    print(f"Vendor probe guard — {r.get('rows', '?')} row(s) in the log")
    print(f"VERDICT: {r['summary']}")
    if r.get("undetermined"):
        return 2
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
