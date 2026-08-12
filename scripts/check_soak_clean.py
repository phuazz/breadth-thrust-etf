"""Was a scheduled_refresh soak run clean enough to arm on?

WHY THIS IS CODE AND NOT A JUDGEMENT CALL.

Arming the weekly refresh with --push lets an unattended process push to main,
which is the event trigger for the factsheet email — and that email reaches the
distribution list, not just the operator. The decision to arm therefore should
not rest on someone skim-reading a log and forming an impression. The criteria
are written down here, they are checked mechanically, and a run that fails any
one of them does not arm.

The substantive criterion is the LAST one. The cadence moved to Friday 08:00
SGT, which fetches roughly four hours after Thursday's US close where the old
Saturday run had twenty-six. Whether the vendor has settled in that window is
the one thing about the new cadence that has never been tested, and a run can
report "All steps OK" while quietly leaving the panel where it was — that
exact failure is in this repo's history ("panel NOT advanced, still
2026-07-31"). So the panel must be shown to have REACHED the session the
decision reads, not merely to have been written.

Usage:
    python scripts/check_soak_clean.py --repo C:/dev/breadth-thrust-etf-sched
    python scripts/check_soak_clean.py --repo <path> --date 2026-08-14

Exit 0 = clean, safe to arm. Exit 1 = not clean. Exit 2 = could not tell.
Prints a verdict block either way; never modifies anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nyse_sessions import last_completed_session  # noqa: E402

REQUIRED = "All steps OK"
SOAK_MARKER = "soak mode: validated, NOT pushed"
FAIL_RE = re.compile(r"FAILED exit (\d+)")
HEADER_RE = re.compile(r"scheduled_refresh start ([0-9]{4}-[0-9]{2}-[0-9]{2})")


def evaluate(repo: Path, run_date: date, now_utc: datetime | None = None) -> dict:
    """Decide whether the run on `run_date` was clean. Pure apart from reads."""
    checks: list[tuple[str, bool, str]] = []

    log = repo / "logs" / f"scheduled_refresh_{run_date.isoformat()}.log"
    if not log.exists():
        return {
            "ok": False, "undetermined": True,
            "checks": [("log file exists", False, f"{log} not found")],
            "summary": (f"No log for {run_date}. Either the run did not "
                        "happen, or it is filed under another date — check "
                        "the header timestamps inside the neighbouring logs "
                        "before concluding it is missing."),
        }
    text = log.read_text(encoding="utf-8", errors="replace")

    headers = HEADER_RE.findall(text)
    checks.append(("log contains a run header", bool(headers),
                   f"{len(headers)} run(s) in file"))
    checks.append((f"a run is dated {run_date}", run_date.isoformat() in headers,
                   f"headers: {headers or 'none'}"))

    failures = FAIL_RE.findall(text)
    checks.append(("no FAILED exit recorded", not failures,
                   f"exit codes seen: {failures or 'none'}"))
    checks.append(("refresh reported all steps OK", REQUIRED in text, REQUIRED))
    checks.append(("ran in soak mode (not already armed)",
                   SOAK_MARKER in text, SOAK_MARKER))

    # The one that matters: did the panel actually advance to the session the
    # decision reads? Evaluated at the run's own local morning, not "now".
    panel_file = repo / "data" / "breadth_csp1.json"
    try:
        panel_end = date.fromisoformat(
            json.loads(panel_file.read_text(encoding="utf-8"))["end_date"])
    except Exception as exc:  # noqa: BLE001
        checks.append(("panel readable", False, repr(exc)))
        return {"ok": False, "undetermined": True, "checks": checks,
                "summary": f"Could not read {panel_file.name}: {exc!r}"}

    at = now_utc or datetime.combine(run_date, time(0, 0), tzinfo=timezone.utc)
    needed = last_completed_session(at)
    advanced = panel_end >= needed
    checks.append((f"panel reached the decision session ({needed})", advanced,
                   f"panel end_date = {panel_end}"))

    ok = all(passed for _, passed, _ in checks)
    return {
        "ok": ok, "undetermined": False, "checks": checks,
        "panel_end": panel_end.isoformat(), "needed": needed.isoformat(),
        "summary": ("clean — every criterion passed" if ok else
                    "NOT clean — " + ", ".join(
                        name for name, passed, _ in checks if not passed)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True,
                    help="the automation clone the scheduled task runs from")
    ap.add_argument("--date", default=None,
                    help="local date of the run (default: today)")
    args = ap.parse_args(argv)

    run_date = (date.fromisoformat(args.date) if args.date
                else datetime.now().date())
    result = evaluate(Path(args.repo), run_date)

    print(f"Soak check for {run_date} in {args.repo}\n")
    for name, passed, detail in result["checks"]:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        print(f"         {detail}")
    print(f"\nVERDICT: {result['summary']}")

    if result["undetermined"]:
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
