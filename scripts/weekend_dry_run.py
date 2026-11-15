"""Saturday-morning dry run of the core weekend refresh. Publishes nothing.

WHY (2026-11-15). The weekend book is built in the Sat/Sun 09:00-15:00 SGT
window, and every failure there costs an hour per attempt because it appears
35 minutes into a run. On 2026-11-14/15 two did: a rounding contract that
live_targets and the release audit implemented differently, and a pytest gate
killed at 1200 s because the tasks ran at low priority. Neither was a data
problem, and both would have shown on any Saturday-morning run of the same
code. The existing --preflight-only mode checks git and the gate preview but
never runs live_targets, the release preflight, pytest or the seal, so it
could not have caught either.

WHAT. In the automation clone, before the 09:00 firing:
  1. checks the refresh tasks' configured priority and this process's;
  2. checks the Norgate feed, and that the clone is clean (a killed run's
     debris is restored exactly as the real firing would);
  3. pulls origin/main, re-running itself if the pull rewrote this file;
  4. runs refresh_all.py --component core (live_targets, the release
     preflight, pytest, the seal) under a hard deadline before 09:00;
  5. ALWAYS restores the tracked outputs, so the 09:00 firing starts clean.

WHAT IT NEVER DOES. Commit, push, write a green-run marker, record a run in
the run ledger, record a debt attempt, write a release marker, or send a
factsheet. Its outputs are discarded whatever happens.

REPORTS. Email only when something needs attention: a failed step, a missed
deadline, a dirty clone, unpushed commits, or a task at low priority. A green
run is silent and writes logs/last_dry_run.json, which fleet_watch reads as
the heartbeat. A killed dry run leaves the in-flight marker, so the 09:00
firing restores its debris like any other killed run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scheduled_refresh as sr  # noqa: E402
import task_priority  # noqa: E402

REPO_ROOT = sr.REPO_ROOT
LOG_DIR = sr.LOG_DIR
HEARTBEAT = LOG_DIR / "last_dry_run.json"
DEFAULT_DEADLINE = "08:40"     # local; the weekend firing starts at 09:00
REEXEC_ENV = "BTE_DRYRUN_REEXEC"


def deadline_for(now_local: datetime, hhmm: str) -> datetime:
    """Today's deadline, local time. A deadline already past gives a negative
    budget, and the caller skips the run."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    return now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _kill_tree(proc: subprocess.Popen) -> None:
    """End the refresh and everything under it (pytest, engines). Popen.kill
    alone would leave the grandchildren writing the clone."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        pass


def run_refresh(log, budget_s: float, price_source: str) -> tuple[str, int | None, int]:
    """('ok' | 'failed' | 'timeout', returncode, log offset where it began)."""
    cmd = [sys.executable, "scripts/refresh_all.py",
           "--price-source", price_source, "--component", "core"]
    log.write(f"\nrunning {' '.join(cmd[1:])} (dry run; output follows; "
              f"budget {budget_s / 60:.0f} min)\n")
    log.flush()
    start = log.tell()
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        rc = proc.wait(timeout=budget_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return "timeout", None, start
    return ("ok" if rc == 0 else "failed"), rc, start


def last_lines_matching(log_path: Path, start: int, needles=("[FAILED]", "TIMEOUT",
                                                             "Error:", "FAILED STEP")) -> list[str]:
    out = []
    try:
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            fh.seek(start)
            prev = ""
            for line in fh:
                if any(n in line for n in needles):
                    out.append((prev.strip() + " | " if "[FAILED]" in line else "")
                               + line.strip())
                prev = line if line.strip() and not line.startswith("=") else prev
    except OSError:
        pass
    return out[-12:]


def panel_end_date() -> str | None:
    try:
        return json.loads(sr.PANEL.read_text(encoding="utf-8"))["end_date"]
    except (OSError, ValueError, KeyError):
        return None


def write_heartbeat(record: dict) -> None:
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deadline", default=DEFAULT_DEADLINE,
                   help="Local HH:MM by which the refresh must finish; it is killed "
                        "and its outputs discarded at this time (default %(default)s).")
    p.add_argument("--price-source", default=sr.DEFAULT_PRICE_SOURCE,
                   choices=sr.PRICE_SOURCES)
    p.add_argument("--no-email", action="store_true", help="Log only.")
    args = p.parse_args(argv)

    LOG_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    local = now.astimezone()
    log_path = LOG_DIR / f"dry_run_{local:%Y-%m-%d}.log"
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n{'=' * 72}\nweekend dry run start {local:%Y-%m-%d %H:%M %Z} "
              f"(= {now.isoformat()}); publishes nothing\n{'=' * 72}\n")
    findings: list[str] = []
    started = time.monotonic()

    def notify(subject: str, body: str) -> None:
        if args.no_email:
            log.write(f"\n[email suppressed: {subject}]\n")
            return
        sr._email(subject, body + f"\n\nLog: {log_path}", log)

    def finish(status: str, rc: int, extra: dict | None = None) -> int:
        record = {"schema": 1, "status": status, "recorded_at_utc":
                  datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "seconds": round(time.monotonic() - started),
                  "findings": findings, **(extra or {})}
        write_heartbeat(record)
        log.write(f"\ndry run {status}: {json.dumps(record)}\n")
        log.close()
        return rc

    # 1. Priority: the tasks as configured, and this process.
    prios = sr._safe(log, "task priorities", task_priority.configured_priorities) or {}
    log.write(f"\nrefresh task priorities: {json.dumps(prios)}\n")
    findings += task_priority.task_priority_findings(prios)
    own = sr._safe(log, "process priority warning", task_priority.process_priority_warning)
    if own:
        findings.append("dry run itself: " + own)

    # 2. Feed and clone.
    ok, msg = sr.price_source_preflight(args.price_source)
    log.write(f"\n{msg}\n")
    if not ok:
        findings.append(f"price source: {msg}")
        notify("[DRY-RUN FAIL] Saturday rehearsal - price source unavailable",
               "The 09:00 weekend firing will refuse on the same check.\n\n" + "\n".join(findings))
        return finish("refused", 2)
    cp = sr._git(["status", "--porcelain"], log)
    if cp.returncode != 0 or cp.stdout.strip():
        recovered, why = (False, "git status failed") if cp.returncode else \
            sr.recover_interrupted_run(log, cp.stdout)
        log.write(f"\ninterrupted-run check: {why}\n")
        if not recovered:
            findings.append(f"automation clone not clean: {why}")
            notify("[DRY-RUN FAIL] Saturday rehearsal - automation clone not clean",
                   "The 09:00 weekend firing will refuse a dirty clone it cannot "
                   "attribute to a killed run. Nothing was touched.\n\n"
                   + "\n".join(findings) + "\n\n" + cp.stdout[:3000])
            return finish("refused", 2)
    sr._git(["fetch", "origin", "main"], log)
    ahead = sr._git(["rev-list", "--count", "origin/main..HEAD"], log).stdout.strip()
    if ahead not in ("", "0"):
        findings.append(f"{ahead} local commit(s) not on origin; the 09:00 firing will "
                        "try to publish them first")
        notify("[DRY-RUN WARN] Saturday rehearsal - unpushed commits in the clone",
               "Dry run skipped so the unpushed commits are left for the real "
               "firing to reconcile.\n\n" + "\n".join(findings))
        return finish("skipped", 0)

    # 3. Sync, re-running if the pull rewrote this file.
    self_before = Path(__file__).read_bytes()
    cp = sr._git(["pull", "--rebase", "origin", "main"], log)
    if cp.returncode != 0:
        findings.append("git pull --rebase failed: " + (cp.stderr or cp.stdout)[:300])
        notify("[DRY-RUN FAIL] Saturday rehearsal - git pull failed", "\n".join(findings))
        return finish("refused", 2)
    if Path(__file__).read_bytes() != self_before and not os.environ.get(REEXEC_ENV):
        log.write("\nthe pull rewrote this script; re-running it as a waited child\n")
        log.close()
        return subprocess.run([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                              cwd=REPO_ROOT, env=dict(os.environ, **{REEXEC_ENV: "1"})).returncode
    head = sr._git(["rev-parse", "--short", "HEAD"], log).stdout.strip()

    # 4. The refresh, bounded so it cannot overlap the 09:00 firing.
    deadline = deadline_for(datetime.now().astimezone(), args.deadline)
    budget = (deadline - datetime.now().astimezone()).total_seconds()
    if budget < 30 * 60:
        findings.append(f"started too late: {budget / 60:.0f} min before the {args.deadline} deadline")
        notify("[DRY-RUN WARN] Saturday rehearsal - not run, too close to the window",
               "\n".join(findings))
        return finish("skipped", 0, {"head": head})
    sr._safe(log, "in-flight marker", sr.claim_in_flight, log, REPO_ROOT,
             cadence="dry-run", component="core")
    try:
        status, rc, start = run_refresh(log, budget, args.price_source)
    finally:
        log.flush()
        clean = sr.restore_tracked_outputs(log)
        if clean:
            sr.release_in_flight()
    panel_end = panel_end_date()
    extra = {"head": head, "panel_end": panel_end, "clone_clean": clean}

    if not clean:
        findings.append("the clone could NOT be restored after the dry run; the 09:00 "
                        "firing will refuse it unless the in-flight marker authorises a restore")
    if status == "ok":
        log.write(f"\nDRY RUN GREEN on {head}; panels end {panel_end}\n")
        if findings:
            notify("[DRY-RUN WARN] Saturday rehearsal green, with findings",
                   f"The core refresh passed on {head} (panels end {panel_end}); "
                   "nothing was published.\n\n" + "\n".join(findings))
        return finish("green", 0, extra)

    detail = "\n".join(last_lines_matching(log_path, start))
    if status == "timeout":
        subject = f"did not finish by {args.deadline}"
        body = (f"The core refresh on {head} was still running at {args.deadline} and was "
                "killed so it could not overlap the 09:00 firing. A run that slow is likely "
                "to fail the real window too: check machine load and task priority.")
    else:
        subject, body = sr.refresh_failure_details(log_path, start)
        body = (f"The core refresh on {head} failed in rehearsal; nothing was published "
                f"and the clone was restored. The 09:00 firing will most likely fail the "
                f"same way unless this is fixed first.\n\n{body}")
    findings.insert(0, subject)
    notify(f"[DRY-RUN FAIL] Saturday rehearsal - {subject}",
           body + (f"\n\nFailed lines:\n{detail}" if detail else "")
           + (f"\n\nOther findings:\n" + "\n".join(findings[1:]) if len(findings) > 1 else "")
           + f"\n\nPanels end {panel_end}.")
    return finish(status, 3, extra)


if __name__ == "__main__":
    raise SystemExit(main())
