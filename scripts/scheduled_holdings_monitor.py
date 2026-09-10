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

A FAILED CAPTURE RESTORES THE OWNED PATHS (2026-09-10). The capture writes
its snapshots and rewrites both payloads BEFORE the guard reads them, so a
guard failure leaves the owned paths dirty and the preflight then refuses
every later firing until a person clears them. That is how the 2026-09-09
run stalled the monitor: yfinance returned nothing for 136 of 166 names, G5
failed on price coverage — the guard working — and the two unpriced
payloads plus two new snapshots sat in the tree, so the 2026-09-10 firing
refused at preflight. Same class as 2026-08-28 to 09-02 (cleared by hand at
0ea4208), different trigger. The remedy is the one scheduled_refresh.py
adopted at e3e992a: a run that fails inside the capture restores what it
wrote, so the next firing is a retry rather than a refusal. Two differences,
because this tree is SHARED with interactive sessions rather than a
dedicated clone: the restore is scoped to the owned paths, one path at a
time, never `git clean` over a directory; and it acts only on what THIS run
dirtied. The owned paths' state is recorded after the preflight and the
restore diffs against it, so a snapshot that existed before the run started
is never removed and a path that was already dirty is left for its owner.
The preflight refusal stays as the guard against a manual run already in
flight, and a publish failure is left as found — the commit may exist by
then, and a rejected commit is a person's to read. Everything restored is
named in the log.

    python scripts/scheduled_holdings_monitor.py            # soak
    python scripts/scheduled_holdings_monitor.py --push     # armed
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
SENTINEL = LOG_DIR / "holdings_monitor_last_success.txt"

# Paths this pipeline owns. Cleanliness is asserted over these only, and a
# failed capture restores these only.
OWNED = (
    "data/holdings_monitor/",
    "data/holdings_monitor_latest.json",
    "docs/holdings-monitor.html",
    "docs/holdings-monitor-series.json",
)

PY = sys.executable


class StepFailed(RuntimeError):
    pass


def run(cmd: list[str], label: str, log, cwd: Path | None = None) -> str:
    log(f"$ {' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        log(f"  | {line}")
    if p.returncode != 0:
        raise StepFailed(f"{label} exited {p.returncode}")
    return out


def owned_dirt(repo_root: Path | None = None) -> list[str]:
    """Porcelain status lines for the OWNED paths only.

    Untracked files are listed one per line rather than collapsed to their
    directory, so a snapshot is named individually both in the preflight
    refusal and in the restore.
    """
    p = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                       cwd=repo_root or ROOT, capture_output=True, text=True)
    # A status that cannot be read is not a clean tree. Until 2026-09-10 a
    # failing git status passed the preflight as if nothing were dirty.
    if p.returncode != 0:
        raise StepFailed(f"git status exited {p.returncode}: "
                         f"{(p.stderr or p.stdout).strip()}")
    return [ln for ln in p.stdout.splitlines()
            if any(ln[3:].startswith(o) for o in OWNED)]


def owned_files(repo_root: Path | None = None) -> frozenset[str]:
    """Every file present under the OWNED paths, repo-relative, tracked or
    not. The restore refuses to remove anything in this set."""
    root = repo_root or ROOT
    present: set[str] = set()
    for owned in OWNED:
        p = root / owned
        if p.is_dir():
            present.update(f.relative_to(root).as_posix()
                           for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            present.add(owned)
    return frozenset(present)


class OwnedState(NamedTuple):
    """The OWNED paths as they stood before the capture started."""
    dirt: tuple[str, ...]     # porcelain lines, normally empty after preflight
    files: frozenset[str]     # files present, so a pre-run snapshot is known


def record_owned_state(repo_root: Path | None = None) -> OwnedState:
    return OwnedState(tuple(owned_dirt(repo_root)), owned_files(repo_root))


def restore_owned_paths(before: OwnedState, log,
                        repo_root: Path | None = None) -> bool:
    """Undo what a failed capture wrote to the OWNED paths, and nothing else.

    Diffs the owned paths' porcelain status against ``before``: an entry
    that was not there when the run started is this run's, and is reverted
    (tracked, `git checkout HEAD -- path`) or removed (untracked, and only
    if the file was not present before the run either). An entry that was
    already there is left alone and named — it belongs to whoever put it
    there, and the preflight will keep refusing until they deal with it.
    One path per git call, never `git clean` over a directory: the tree is
    shared. Returns True when the owned paths are clean afterwards.
    """
    root = repo_root or ROOT
    after = owned_dirt(root)
    if not after:
        log("restore: owned paths already clean — nothing to restore")
        return True
    # By PATH, not by status line: a path that was dirty before this run
    # started belongs to whoever dirtied it, whatever its status reads now.
    before_paths = {ln[3:] for ln in before.dirt}
    ours = [ln for ln in after if ln[3:] not in before_paths]
    theirs = [ln for ln in after if ln[3:] in before_paths]
    if ours:
        log("restore: owned paths dirtied by this run:\n  " + "\n  ".join(ours))
    else:
        log("restore: nothing on the owned paths is this run's")
    if theirs:
        log("restore: left alone — dirty before this run started:\n  "
            + "\n  ".join(theirs))
    for ln in ours:
        code, path = ln[:2], ln[3:]
        if code == "??":
            if path in before.files:
                log(f"restore: LEFT {path} — it existed before this run started")
                continue
            try:
                (root / path).unlink()
                log(f"restore: removed {path} (written by this run)")
            except OSError as exc:
                log(f"restore: could not remove {path}: {exc}")
        else:
            p = subprocess.run(["git", "checkout", "HEAD", "--", path], cwd=root,
                               capture_output=True, text=True)
            if p.returncode == 0:
                log(f"restore: reverted {path} to HEAD")
            else:
                log(f"restore: could not revert {path}: "
                    f"{(p.stderr or p.stdout).strip()}")
    remaining = owned_dirt(root)
    if remaining:
        log("restore: owned paths STILL dirty — the next firing will refuse:\n  "
            + "\n  ".join(remaining))
        return False
    log("restore: owned paths clean — the next firing is a retry, not a refusal")
    return True


def preflight(log, repo_root: Path | None = None) -> None:
    # Someone else's edit elsewhere in the tree is fine; an uncommitted
    # edit to THIS pipeline's own outputs means a manual run is mid-flight.
    dirty = owned_dirt(repo_root)
    if dirty:
        raise StepFailed(
            "monitor-owned paths are dirty before the run — a manual run may "
            "be in flight:\n  " + "\n  ".join(dirty))

    # BEST EFFORT, deliberately. This working tree is shared with interactive
    # sessions, so `pull --rebase` routinely refuses on somebody else's
    # unstaged work. Aborting the capture for that would mean the monitor
    # goes dark whenever a human happens to be mid-edit — a daily job that
    # fails for reasons unrelated to its own health is a daily job that gets
    # ignored. Nothing in the capture depends on being at origin HEAD; only
    # the push does, and publish() rebases again with failure fatal there.
    try:
        run(["git", "pull", "--rebase", "origin", "main"], "git pull", log,
            cwd=repo_root)
    except StepFailed as exc:
        log(f"  WARN: {exc} — continuing. The capture does not depend on "
            f"origin HEAD; an armed push would still fail loudly.")


def publish(log, repo_root: Path | None = None) -> None:
    # Stage ONLY this pipeline's paths. The tree is shared with other
    # sessions, so `git add -A` would sweep up unrelated work.
    run(["git", "add", *OWNED], "git add", log, cwd=repo_root)
    p = subprocess.run(["git", "diff", "--cached", "--quiet"],
                       cwd=repo_root or ROOT)
    if p.returncode == 0:
        log("  nothing to commit — rosters and page unchanged since last run")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run(["git", "commit", "-m", f"monitor: holdings capture {stamp}"],
        "git commit", log, cwd=repo_root)
    # Fatal here, unlike in preflight: the commit exists now, so a diverged
    # branch must be reconciled before it can be published rather than
    # papered over. Autostash is safe at this point because the monitor's
    # own work is already committed.
    run(["git", "pull", "--rebase", "--autostash", "origin", "main"],
        "git pull before push", log, cwd=repo_root)
    run(["git", "push", "origin", "main"], "git push", log, cwd=repo_root)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
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
        # Recorded AFTER the preflight, so a snapshot the pull just brought
        # in counts as pre-existing. See restore_owned_paths.
        before = record_owned_state()
        try:
            run([PY, "scripts/run_holdings_monitor.py"], "capture", log)
            # The gate. Nothing downstream runs if this fails.
            run([PY, "scripts/check_holdings_monitor_guard.py"], "guard", log)
            run([PY, "scripts/build_holdings_monitor_page.py"], "build page", log)
        except Exception:
            # A guard failure is the guard working; the tree it leaves
            # behind must not stall tomorrow's firing. The tidy-up must
            # never hide the failure it is tidying up after.
            try:
                restore_owned_paths(before, log)
            except Exception as exc:
                log(f"restore: FAILED — {exc}; the owned paths may still be "
                    f"dirty and the next firing may refuse")
            raise
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
