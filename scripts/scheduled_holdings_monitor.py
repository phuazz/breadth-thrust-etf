"""Unattended daily wrapper for the theme-constituent monitor.

Per the vault rule that no unattended agent runs without a guard layer,
this wrapper is nothing but guard layers around the capture:

  preflight   git pull --rebase so the run starts from origin HEAD, and a
              cleanliness check scoped to THIS pipeline's own paths. It is
              scoped rather than repo-wide on purpose: several sessions
              share this working tree, so demanding a globally clean tree
              would abort on somebody else's unrelated edit, and an
              automation that cries wolf gets ignored.
  capture     run_holdings_monitor.py — fetches both rosters, writes the
              immutable daily snapshots, prices the union, computes flow.
  guard       check_holdings_monitor_guard.py. A FAIL here BLOCKS the page
              build and the push. This is the gate: the failure that
              matters is not a crash but a clean run against a changed or
              truncated upstream file, which would otherwise publish a
              confident, wrong table.
  build       build_holdings_monitor_page.py -> docs/holdings-monitor.html
  publish     ONLY with --push (armed mode). Soak mode (no flag — the
              initial state) stops here and reports READY so the operator
              reviews and pushes by hand. Arm the scheduled task by adding
              --push after two clean soak runs.

On success a sentinel is touched at logs/holdings_monitor_last_success.txt.
That exists because the git heartbeat in fleet_watch.json only moves when
the OUTPUT changes, so a run that fires and fails writes nothing and looks
identical to a quiet day. The sentinel is a liveness signal rather than a
change signal — the same blind spot that hid a failed Perp-Funding run for
a day in August 2026.

    python scripts/scheduled_holdings_monitor.py            # soak
    python scripts/scheduled_holdings_monitor.py --push     # armed
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
SENTINEL = LOG_DIR / "holdings_monitor_last_success.txt"

# Paths this pipeline owns. Cleanliness is asserted over these only.
OWNED = (
    "data/holdings_monitor/",
    "data/holdings_monitor_latest.json",
    "docs/holdings-monitor.html",
    "docs/holdings-monitor-series.json",
)

PY = sys.executable


class StepFailed(RuntimeError):
    pass


def run(cmd: list[str], label: str, log) -> str:
    log(f"$ {' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        log(f"  | {line}")
    if p.returncode != 0:
        raise StepFailed(f"{label} exited {p.returncode}")
    return out


def preflight(log) -> None:
    # Someone else's edit elsewhere in the tree is fine; an uncommitted
    # edit to THIS pipeline's own outputs means a manual run is mid-flight.
    p = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                       capture_output=True, text=True)
    dirty = [ln for ln in p.stdout.splitlines()
             if any(ln[3:].startswith(o) for o in OWNED)]
    if dirty:
        raise StepFailed(
            "monitor-owned paths are dirty before the run — a manual run may "
            "be in flight:\n  " + "\n  ".join(dirty))
    run(["git", "pull", "--rebase", "origin", "main"], "git pull", log)


def publish(log) -> None:
    # Stage ONLY this pipeline's paths. The tree is shared with other
    # sessions, so `git add -A` would sweep up unrelated work.
    run(["git", "add", *OWNED], "git add", log)
    p = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if p.returncode == 0:
        log("  nothing to commit — rosters and page unchanged since last run")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run(["git", "commit", "-m", f"monitor: holdings capture {stamp}"],
        "git commit", log)
    run(["git", "push", "origin", "main"], "git push", log)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--push", action="store_true",
                    help="armed mode: commit and push after the guard passes")
    a = ap.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    logfile = LOG_DIR / f"holdings_monitor_{started:%Y-%m-%d}.log"
    handle = logfile.open("a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc):%H:%M:%S} {msg}"
        print(line)
        handle.write(line + "\n")
        handle.flush()

    log(f"=== holdings monitor {'ARMED' if a.push else 'SOAK'} "
        f"{started.isoformat(timespec='seconds')} ===")
    try:
        preflight(log)
        run([PY, "scripts/run_holdings_monitor.py"], "capture", log)
        # The gate. Nothing downstream runs if this fails.
        run([PY, "scripts/check_holdings_monitor_guard.py"], "guard", log)
        run([PY, "scripts/build_holdings_monitor_page.py"], "build page", log)
        if a.push:
            publish(log)
        else:
            log("SOAK mode — page built, nothing pushed. Review "
                "docs/holdings-monitor.html, then arm with --push after two "
                "clean soak runs.")
        SENTINEL.write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n",
            encoding="utf-8")
        log("RESULT: OK")
        return 0
    except StepFailed as exc:
        log(f"RESULT: FAILED — {exc}")
        log("Nothing was published. The sentinel was not touched, so the "
            "fleet watch will breach if this persists.")
        return 1
    finally:
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
