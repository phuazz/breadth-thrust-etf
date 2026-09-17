"""Durable, machine-readable status for the scheduled refresh.

WHY THIS EXISTS (2026-09-16). Five consecutive post-fill failures across
15 and 16 September each ended with one line in a text log:

    [email skipped: GMAIL_USER / GMAIL_APP_PASSWORD not set]

GMAIL_APP_PASSWORD was set on the machine; GMAIL_USER was not; ``_email``
requires both, so the alert path degraded to a log entry and the only
thing that noticed was a human reading a date on a dashboard three days
later. Two separate defects sit in that line and this module addresses
both:

  1. The outcome was PROSE. Nothing could read it, so nothing could watch
     it. Every status this module writes is JSON with a fixed schema.
  2. The outcome was written by the process that was failing, to a file
     only that process touches. That is necessary but never sufficient -
     a run that dies before it writes, or a task that never fires at all,
     leaves the file looking exactly as it did on the last healthy run.
     The artefacts here are therefore designed to be read by an
     INDEPENDENT observer on its own schedule, and they carry an explicit
     ``asof`` so a reader can tell a stale file from a current one without
     trusting mtime.

WHAT THE FIRST VERSION GOT WRONG (adjudicated 2026-09-16, external
review). It trusted the records it read:

  * a prior record carrying ``"consecutive_failures": "bad"`` raised
    ValueError inside ``record_alert``, and a prior record that was a JSON
    ARRAY raised AttributeError. Both propagated into ``_email``, which the
    refresh calls from inside ``fail()`` - so a corrupt diagnostic file
    could replace the refresh's own failure with a traceback about the
    diagnostic. The instrument taking out the patient.
  * ``alert_health`` returned OK for a record whose ``status`` was a word
    it had never heard of. An unrecognised health value is not health.

Everything read from disk is now validated, every unknown is visibly an
error rather than silently a pass, and every write is atomic so an
interrupted run leaves the previous record rather than half of the new one.

WHERE IT WRITES, AND WHY THERE. ``logs/`` is gitignored and sits outside
``scheduled_refresh.RESTORE_PATHS``, so these records survive the tracked
output restore that follows every exit 3 or 4. That property is the whole
point: the evidence that matters most is produced by the runs that fail,
and until now those runs cleaned up after themselves and left the tree
looking untouched.

Nothing in this module may raise into a caller. A status writer that can
break a refresh is a worse defect than the one it was added to fix.

Python datetime months are 1-indexed (January = 1). Strings are ASCII.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1

# Alert delivery outcomes. A closed vocabulary, because the point of the
# file is that something else can branch on it.
SENT = "sent"                    # handed to the SMTP server without error
UNCONFIGURED = "unconfigured"    # credentials absent: nothing was attempted
FAILED = "failed"                # attempted and the server or socket refused
DELIVERY_STATUSES = (SENT, UNCONFIGURED, FAILED)

# The environment variables _email reads. Named here so the configuration
# probe and the sender cannot disagree about what "configured" means.
CREDENTIAL_VARS = ("GMAIL_USER", "GMAIL_APP_PASSWORD")

# Shortest credential value worth scrubbing. Below this a "value" is more
# likely to be a substring of ordinary prose than a secret, and redacting it
# would corrupt the diagnostic it is meant to protect.
_MIN_REDACTABLE = 4

# Health thresholds. The channel writes a record on EVERY alert attempt,
# including the green "pushed" notice, and the armed pairs run four times a
# week, so eight days without any attempt is already abnormal.
MAX_ATTEMPT_AGE_HOURS = 192.0
# A delivery that has not SUCCEEDED in this long is a breach even if
# attempts keep being recorded - the failure mode where the channel is
# busy failing is exactly the one the age-only watcher could not see.
MAX_SUCCESS_AGE_HOURS = 192.0
MAX_CONSECUTIVE_FAILURES = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def alert_state_path(repo_root: Path) -> Path:
    return Path(repo_root) / "logs" / "alert_delivery.json"


def run_ledger_path(repo_root: Path) -> Path:
    return Path(repo_root) / "logs" / "run_outcomes.jsonl"


def alert_ok_path(repo_root: Path) -> Path:
    """Refreshed ONLY when an alert was actually delivered.

    Watching ``alert_delivery.json`` by age would not do: that file is
    rewritten on every attempt including an UNCONFIGURED one, so a dead
    channel keeps its mtime fresh and an age-based row goes green on
    exactly the state it exists to catch. This one moves only on a real
    delivery, so its age is a last-SUCCESS measure.

    It is still only half the question - see ``observe``, which reads the
    latest delivery STATUS as well, because a success this morning followed
    by a failure this afternoon leaves this file young and the channel
    broken.
    """
    return Path(repo_root) / "logs" / "alert_ok.json"


def watch_ok_path(repo_root: Path) -> Path:
    """Written by the INDEPENDENT observer, only on an OK verdict.

    The meta-guard's target: fleet_watch ages this file, so the row breaches
    both when the alert channel is unhealthy and when the observer itself
    has stopped running. Neither the refresh nor ``_email`` ever writes it.
    """
    return Path(repo_root) / "logs" / "alert_watch_ok.json"


def redact(text, env: dict | None = None) -> str:
    """Remove credential VALUES from any text about to be stored or logged.

    ADDED 2026-09-16 after an adversarial review. The credential-state record
    had always carried names and presence only, and that was mistaken for the
    whole problem. The values reached the durable record by another door: an
    EXCEPTION. ``smtplib.SMTPRecipientsRefused`` stringifies to a dict keyed
    by the recipient - which is GMAIL_USER - and an authentication failure can
    echo the password back in the server's own reply. Both were written
    verbatim into ``logs/alert_delivery.json`` and into the text log, which is
    what an operator pastes into a ticket.

    Scrubbing the value rather than the exception type keeps the diagnostic:
    the operator still learns that the recipient was refused, and reads
    ``<GMAIL_USER>`` where the address was. The password's spaced and unspaced
    forms are both removed, because the stored value is the four-block form
    and SMTP may echo either.
    """
    if text is None:
        return ""
    out = str(text)
    src = env if env is not None else os.environ
    for var in CREDENTIAL_VARS:
        try:
            value = src.get(var)
        except Exception:  # noqa: BLE001
            value = None
        if not value:
            continue
        for form in {str(value), str(value).replace(" ", "")}:
            if len(form) >= _MIN_REDACTABLE and form in out:
                out = out.replace(form, f"<{var}>")
    return out


def redact_obj(obj, env: dict | None = None, _depth: int = 0):
    """``redact`` over every string in a nested structure.

    The observer echoes the whole delivery record, so scrubbing two named
    fields was never enough: a value can reach any key, including one
    generated by a validator. Bounded in depth so a cyclic or pathological
    record cannot spin.

    THE LIMIT, STATED. This removes VALUES it can see - the two credentials
    in this process's environment, spaced and unspaced. A secret that
    arrives truncated, re-encoded or split across fields is not recognised,
    and neither is one belonging to an environment this process does not
    have: an observer running without GMAIL_APP_PASSWORD set cannot scrub a
    password a recorder wrote. Redaction is a second line; not echoing
    unknown values in the first place is the one that holds.
    """
    if _depth > 6:
        return obj
    if isinstance(obj, str):
        return redact(obj, env)
    if isinstance(obj, dict):
        return {k: redact_obj(v, env, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, env, _depth + 1) for v in obj]
    return obj


def credential_state() -> dict:
    """Which alert credentials this PROCESS can see - names and presence
    only, never values, never lengths that could narrow a guess.

    Read from ``os.environ`` rather than the user registry on purpose: a
    scheduled task inherits its environment at launch, so the only
    question worth answering is what the running process actually has.
    """
    try:
        present = {v: bool(os.environ.get(v)) for v in CREDENTIAL_VARS}
    except Exception:  # noqa: BLE001 - never raise into a caller
        present = {v: False for v in CREDENTIAL_VARS}
    return {"vars": present,
            "configured": all(present.values()),
            "missing": sorted(k for k, v in present.items() if not v)}


# ---------------------------------------------------------------------------
# Reading, defensively
# ---------------------------------------------------------------------------
def read_json(path: Path) -> dict | None:
    """A JSON OBJECT from ``path``, or None.

    A file holding a list, a string or a number is not a record. Returning
    it as one is how ``[1]`` reached ``prior.get`` and raised
    AttributeError inside the failure path of the refresh.
    """
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


def _coerce_int(value, default: int = 0) -> int:
    """An int from anything, or ``default``. Never raises."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def validate_alert_record(rec) -> tuple[bool, list[str]]:
    """(usable, problems) for an alert-delivery record.

    Usable means the fields a verdict depends on are present and of the
    right type. Anything else is reported rather than repaired: a record
    that has to be guessed at is evidence of nothing.
    """
    problems: list[str] = []
    if not isinstance(rec, dict):
        return False, ["record is not a JSON object"]
    status = rec.get("status")
    if status not in DELIVERY_STATUSES:
        # THE OFFENDING VALUE IS NOT ECHOED (2026-09-16, third review). It
        # used to be interpolated into this message, which then travelled
        # into prior_record_problems on the next record and out through the
        # observer - so a corrupted file whose "status" happened to hold a
        # credential published it. A diagnostic must describe the fault, not
        # repeat the payload: the type and length locate the corruption
        # without carrying it.
        problems.append(f"status is not one of {', '.join(DELIVERY_STATUSES)} "
                        f"(a {type(status).__name__} of length "
                        f"{len(status) if isinstance(status, (str, bytes)) else 0})")
    asof = rec.get("asof")
    if not isinstance(asof, str) or _parse_iso(asof) is None:
        problems.append("asof is missing or not an ISO timestamp")
    if not isinstance(rec.get("consecutive_failures"), int):
        problems.append("consecutive_failures is missing or not an integer")
    return (not problems), problems


def _parse_iso(raw) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Writing, atomically
# ---------------------------------------------------------------------------
def _write_json(path: Path, rec: dict) -> dict | None:
    """Replace ``path`` atomically. None when the write failed.

    Atomic because these files are read by an independent process that may
    look at any instant, and because a run killed mid-write would otherwise
    leave a truncated record - which the reader above would then have to
    treat as unreadable, losing the history the file exists to keep.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, indent=2))
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return rec
    except Exception:  # noqa: BLE001 - never raise into a caller
        return None


def record_alert(path: Path, *, subject: str, status: str, detail: str = "",
                 now: datetime | None = None) -> dict | None:
    """Write the latest alert-delivery outcome. Returns the record, or None
    when the write itself failed (which the caller logs, never raises).

    A malformed PRIOR record is tolerated: the streak restarts and the
    record says so. The alternative - raising - put a diagnostic's own
    corruption in front of the refresh failure it was trying to report.
    """
    try:
        if status not in DELIVERY_STATUSES:
            detail = f"unknown status {status!r} supplied; recorded as failed. {detail}"
            status = FAILED
        now = now or _now()
        prior = read_json(path)
        if prior is None and Path(path).exists():
            # The file is there and is not a record. Say so on the new one:
            # losing the streak silently is how a corrupt diagnostic looks
            # exactly like a healthy first run.
            prior, prior_ok = {}, False
            prior_problems = ["prior record was unreadable or not a JSON "
                              "object; streak restarted"]
        else:
            prior = prior or {}
            prior_ok, prior_problems = (validate_alert_record(prior)
                                        if prior else (True, []))
        streak_raw = prior.get("consecutive_failures")
        streak = _coerce_int(streak_raw, default=0)
        streak = 0 if status == SENT else streak + 1
        last_sent = prior.get("last_sent_utc")
        if not isinstance(last_sent, str) or _parse_iso(last_sent) is None:
            last_sent = None
        rec = {
            "schema": SCHEMA,
            "asof": now.isoformat(timespec="seconds"),
            "status": status,
            # Redacted BEFORE truncation: a value split by the cut would
            # otherwise survive in pieces.
            "subject": redact(subject)[:200],
            "detail": redact(detail)[:500],
            "consecutive_failures": streak,
            "credentials": credential_state(),
            # Carried so an independent reader can answer "has an alert EVER
            # been delivered from this clone" without walking the ledger.
            "last_sent_utc": (now.isoformat(timespec="seconds") if status == SENT
                              else last_sent),
        }
        if prior_problems:
            rec["prior_record_problems"] = prior_problems[:5]
        written = _write_json(path, rec)
        if status == SENT:
            # Only a real delivery moves this file - see alert_ok_path.
            _write_json(Path(path).parent / "alert_ok.json",
                        {"schema": SCHEMA, "asof": rec["asof"],
                         "subject": rec["subject"]})
        return written
    except Exception:  # noqa: BLE001 - never raise into a caller
        return None


def record_run(path: Path, *, cadence: str, exit_code: int, subject: str,
               debt: dict | None = None, now: datetime | None = None
               ) -> dict | None:
    """Append one run outcome to the durable ledger. Never raises.

    Append-only and bounded by trimming on write: the file is a forensic
    record for the last few weeks, not an archive. A refresh runs at most
    a few times a day, so a 500-record tail covers months.
    """
    try:
        now = now or _now()
        rec = {"schema": SCHEMA, "asof": now.isoformat(timespec="seconds"),
               "cadence": str(cadence), "exit_code": _coerce_int(exit_code, -1),
               "outcome": redact(subject)[:200],
               "debt": debt if isinstance(debt, dict) else None}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if p.exists():
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                     if ln.strip()][-499:]
        lines.append(json.dumps(rec))
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return rec
    except Exception:  # noqa: BLE001 - never raise into a caller
        return None


# ---------------------------------------------------------------------------
# The independent-observer side. The verdict logic is pure so it can be
# tested without a machine state.
# ---------------------------------------------------------------------------
def alert_health(rec: dict | None, now: datetime,
                 *, max_age_hours: float = MAX_ATTEMPT_AGE_HOURS,
                 max_success_age_hours: float = MAX_SUCCESS_AGE_HOURS,
                 max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
                 ) -> tuple[str, str]:
    """(status, detail) for the alerting channel itself.

    Reads the latest delivery STATUS and the last SUCCESS age together. A
    send that succeeded this morning and failed this afternoon must breach
    now, not in eight days when a file-age row finally notices.

    A missing record is ERROR, and so is a record whose ``status`` is a
    value this module does not define. Absence of evidence about whether
    alerts work is the exact state this incident was in for three days, and
    an unrecognised health value is not health.

    NEVER DELIVERED IS A BREACH, not a grace period. A clone that has not
    demonstrated a delivery has an uncommissioned alert channel, which is
    actionable now rather than after something has failed silently.
    """
    usable, problems = validate_alert_record(rec)
    if not isinstance(rec, dict):
        return "ERROR", "no alert-delivery record: alerting is unproven"
    if not usable:
        return "ERROR", ("alert-delivery record unusable: "
                         + "; ".join(problems)[:200])
    status = rec.get("status")
    streak = _coerce_int(rec.get("consecutive_failures"), 0)
    asof = _parse_iso(rec.get("asof"))
    age_h = (now - asof).total_seconds() / 3600.0
    if status == UNCONFIGURED:
        missing = ",".join((rec.get("credentials") or {}).get("missing") or [])
        return "BREACH", (f"alerting UNCONFIGURED (missing: {missing or '?'}); "
                          f"failures are invisible")
    if status == FAILED and streak >= max_consecutive_failures:
        # Redacted again on the way OUT: the observer's verdict is printed to
        # a console and toasted, and a record written before redact() existed
        # may still carry a value.
        return "BREACH", (f"alert delivery failing ({streak} consecutive); "
                          f"{redact(rec.get('detail'))[:80]}")
    sent = _parse_iso(rec.get("last_sent_utc"))
    if sent is None:
        return "BREACH", ("no alert has ever been delivered from this clone; "
                          "the channel is not commissioned")
    sent_age_h = (now - sent).total_seconds() / 3600.0
    if sent_age_h > max_success_age_hours:
        return "BREACH", (f"no successful delivery for {sent_age_h:.0f}h "
                          f"(limit {max_success_age_hours:.0f}h)")
    if age_h > max_age_hours:
        return "BREACH", (f"no alert attempt for {age_h:.0f}h "
                          f"(limit {max_age_hours:.0f}h)")
    return "OK", (f"last attempt {status} {age_h:.0f}h ago, last delivery "
                  f"{sent_age_h:.0f}h ago, streak {streak}")


def observe(repo_root: Path, now: datetime | None = None, *,
            max_age_hours: float = MAX_ATTEMPT_AGE_HOURS,
            max_success_age_hours: float = MAX_SUCCESS_AGE_HOURS,
            write_marker: bool = True) -> dict:
    """The independent observer's verdict, and its own liveness marker.

    Run by a task of its OWN, not by the refresh: a channel watched only by
    the process that uses it is watched by the thing most likely to be dead.
    On OK it refreshes ``alert_watch_ok.json``; on anything else it leaves
    that file alone, so a fleet-level age row breaches both when the channel
    is unhealthy and when this observer has itself stopped running.
    """
    now = now or _now()
    rec = read_json(alert_state_path(repo_root))
    status, detail = alert_health(rec, now, max_age_hours=max_age_hours,
                                  max_success_age_hours=max_success_age_hours)
    if isinstance(rec, dict):
        # The whole record is echoed to a console and may predate redact();
        # scrub EVERY string in it, not the two fields that were expected to
        # carry text.
        rec = redact_obj(rec)
    verdict = {"schema": SCHEMA, "asof": now.isoformat(timespec="seconds"),
               "repo": str(repo_root), "status": status,
               "detail": redact(detail), "record": rec}
    if status == "OK" and write_marker:
        _write_json(watch_ok_path(repo_root),
                    {"schema": SCHEMA, "asof": verdict["asof"],
                     "detail": detail})
    return verdict


def notify(message: str, *, script: Path | None = None) -> bool:
    """Best-effort desktop toast through the vault's notifier. Never raises."""
    path = Path(script) if script else Path("C:/dev/scripts/notify.ps1")
    if not path.exists():
        return False
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(path), "-Message", str(message)[:300]],
            capture_output=True, text=True, timeout=60)
        return cp.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    """CLI for an independent watcher. Exit 0 OK, 1 BREACH, 2 ERROR."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--max-age-hours", type=float, default=MAX_ATTEMPT_AGE_HOURS)
    ap.add_argument("--max-success-age-hours", type=float,
                    default=MAX_SUCCESS_AGE_HOURS)
    ap.add_argument("--observe", action="store_true",
                    help="Independent-observer mode: judge the channel and, "
                         "on OK only, refresh the observer's own liveness "
                         "marker for the fleet-level age row.")
    ap.add_argument("--notify", action="store_true",
                    help="With --observe, toast a non-OK verdict.")
    ap.add_argument("--probe-credentials", action="store_true",
                    help="Print which alert credentials THIS process can "
                         "see (names and presence only) and exit.")
    args = ap.parse_args(argv)
    if args.probe_credentials:
        state = credential_state()
        print(json.dumps(state, indent=2))
        return 0 if state["configured"] else 1
    verdict = observe(Path(args.repo), max_age_hours=args.max_age_hours,
                      max_success_age_hours=args.max_success_age_hours,
                      write_marker=args.observe)
    print(json.dumps(verdict, indent=2))
    if args.observe and args.notify and verdict["status"] != "OK":
        notify(f"breadth-etf alerting {verdict['status']}: {verdict['detail']}")
    return {"OK": 0, "BREACH": 1}.get(verdict["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
