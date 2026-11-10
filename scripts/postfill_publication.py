"""Positive confirmation that a weekly fill reached the published record.

WHY THIS EXISTS (2026-11-10). The post-fill refresh runs only on the owner's
PC (BreadthThrust-PostFillRefresh Tue + Wed 09:00 SGT, PostFillCatchup Thu +
Fri hourly; StartWhenAvailable on, WakeToRun off). Until now a missed week
and a healthy week looked the same until the Saturday fleet_watch: a machine
that was off sent nothing, and a healthy run sent an undeduplicated "[OK]"
notice on every push, including a Wednesday push that carried nothing new.
This module is shared by both halves of the fix:

  * scheduled_refresh.py sends ONE confirmation per fill, built from what the
    pushed commit actually records, and only for a commit this run created
    that is on origin/main;
  * check_postfill_published.py (cloud, Thursday 08:00 UTC) mails when the
    record on origin/main has not reached the latest fill.

THE EVIDENCE IS THE ENGINE RECORD, NOT ``deployed_series_end`` ALONE.
daily_live_track.yml (CI, every weekday) rewrites data/live_track.json and
moves ``deployed_series_end`` forward on its own: commits 9c09e188, afa90953
and 6e0f2923 (3-7 Nov 2026) did so with no local run behind them. With the PC
off for a whole week, that field would therefore pass the Thursday check.
What CI does not move is (a) each sleeve's ``headline.latest_rebalance.date``
in its engine file - the record publication_debt already treats as the
discharge of a fill - and (b) live_track.json's ``anchor_date``, which only
an engine re-run re-anchors. Measured on origin/main: the post-fill commit
b764d01f moved both to the 2 Nov fill; the CI commits either side left them
alone. A fill counts as published when every obliged sleeve's record reaches
it AND live_track's anchor_date and deployed_series_end both reach it.

THE FILL DATE comes from the engine's own calendar (rebalance_calendar.
engine_rebalance_dates, holiday_aware_next, per venue), the route live_targets.
next_fill_date uses. A holiday Monday rolls forward, and the venues can split:
2026-09-07 was a NYSE holiday (US sleeves filled the 8th) while Xetra traded
the 7th. "Last Monday" would be wrong on both counts.

Python datetime months are 1-indexed (January = 1). No weekday is computed by
hand; every session comes from the venue calendar.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The published record per sleeve, and the venue each sleeve trades on. The
# record map is publication_debt's own (imported so the two cannot drift);
# the venue map is the FALLBACK for when no live_targets.json naming the fill
# can be found - the book itself is preferred, so a sleeve that changes venue
# moves with it.
from publication_debt import SLEEVE_FILES  # noqa: E402

DEFAULT_SLEEVE_VENUE = {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"}
VENUES = ("NYSE", "XETR")
ENGINE_FREQ = "W-MON"           # run_topk_robustness.HEADLINE_FREQ, pinned by test
CALENDAR_LOOKBACK_DAYS = 28

# Verdicts. A closed vocabulary: the workflow branches on it.
OK = "ok"                       # every obliged sleeve and the live track reach the fill
HELD = "held"                   # only authorised-HOLD sleeves are behind: reported, not mailed
MISSED = "missed"               # the record has not reached the fill: mail
UNKNOWN = "unknown"             # the record could not be read: mail (never read as green)

Reader = Callable[[str, str], "str | None"]   # (ref, repo-relative path) -> text


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
def latest_fill(venue: str, asof: date) -> date | None:
    """The latest scheduled fill for ``venue`` strictly BEFORE ``asof``.

    Strictly before, because a fill on ``asof`` itself has not closed when
    either caller asks (the Thursday check runs at 08:00 UTC; the post-fill
    refresh runs the morning after). Sessions come from the venue calendar
    through ``asof``, so a holiday Monday rolls to the Tuesday when the
    Tuesday is inside the window.
    """
    import pandas as pd
    import rebalance_calendar as rc
    from venue_calendars import get_calendar
    start = (asof - timedelta(days=CALENDAR_LOOKBACK_DAYS)).isoformat()
    sched = get_calendar(venue).schedule(start_date=start, end_date=asof.isoformat())
    if not len(sched):
        return None
    idx = pd.DatetimeIndex(sched.index)
    dates = rc.engine_rebalance_dates(idx, idx[0], freq=ENGINE_FREQ, calendar=venue)
    prior = [d.date() for d in dates if d.date() < asof]
    return prior[-1] if prior else None


def latest_fills(asof: date, venues=VENUES) -> dict[str, date | None]:
    return {v: latest_fill(v, asof) for v in venues}


def fill_key(fills: dict[str, date | None]) -> str:
    """Dedupe key for one fill: 'NYSE:2026-11-09|XETR:2026-11-09'."""
    return "|".join(f"{v}:{d.isoformat() if d else '-'}" for v, d in sorted(fills.items()))


# ---------------------------------------------------------------------------
# Reading the published record
# ---------------------------------------------------------------------------
def git_reader(repo_root: Path) -> Reader:
    def show(ref: str, path: str) -> str | None:
        try:
            cp = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(repo_root),
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return cp.stdout if cp.returncode == 0 else None
    return show


def _json(reader: Reader, ref: str, path: str):
    raw = reader(ref, path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _as_date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def sleeve_records(reader: Reader, ref: str) -> dict[str, date | None]:
    """Each sleeve's published headline.latest_rebalance.date at ``ref``."""
    out: dict[str, date | None] = {}
    for sleeve, name in SLEEVE_FILES.items():
        doc = _json(reader, ref, f"data/{name}")
        rec = (doc.get("headline") or {}).get("latest_rebalance") if isinstance(doc, dict) else None
        out[sleeve] = _as_date(rec.get("date")) if isinstance(rec, dict) else None
    return out


def live_track_state(reader: Reader, ref: str) -> dict:
    doc = _json(reader, ref, "data/live_track.json")
    if not isinstance(doc, dict):
        return {"readable": False, "anchor_date": None, "deployed_series_end": None}
    return {"readable": True,
            "anchor_date": _as_date(doc.get("anchor_date")),
            "deployed_series_end": _as_date(doc.get("deployed_series_end"))}


def book_for_fill(targets_history: list[dict], fills: dict[str, date | None]) -> dict | None:
    """The newest live_targets.json (newest first in ``targets_history``) whose
    sleeves name one of these fills - the book the fill was RANKED on, which
    carries each sleeve's status. Read from history because the daily CI run
    may already have rolled the current file to next week's fill."""
    wanted = {d.isoformat() for d in fills.values() if d}
    for doc in targets_history:
        sleeves = doc.get("sleeves") if isinstance(doc, dict) else None
        if isinstance(sleeves, list) and any(
                isinstance(s, dict) and str(s.get("fill_date")) in wanted for s in sleeves):
            return doc
    return None


def targets_history(repo_root: Path, ref: str, limit: int = 40) -> list[dict]:
    """live_targets.json at the last ``limit`` commits on ``ref`` that touched it."""
    try:
        cp = subprocess.run(["git", "log", f"-{limit}", "--format=%H", ref, "--",
                             "data/live_targets.json"], cwd=str(repo_root),
                            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0:
        return []
    reader = git_reader(repo_root)
    out = []
    for sha in cp.stdout.split():
        doc = _json(reader, sha, "data/live_targets.json")
        if isinstance(doc, dict):
            out.append(doc)
    return out


def postfill_commit(repo_root: Path, ref: str, since: date) -> str | None:
    """Short hash of the newest 'Local post-fill refresh' commit on ``ref``
    committed on or after ``since`` (informational: a manual recovery may
    carry another subject, so this is never the verdict on its own)."""
    try:
        cp = subprocess.run(["git", "log", "-1", "--format=%h", f"--since={since.isoformat()}",
                             "--grep=^Local post-fill refresh", ref], cwd=str(repo_root),
                            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (cp.stdout.strip() or None) if cp.returncode == 0 else None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
@dataclass
class Assessment:
    verdict: str
    fills: dict[str, date | None]
    records: dict[str, date | None]
    live_track: dict
    sleeve_venue: dict[str, str]
    updated: list[str] = field(default_factory=list)        # record reaches the fill
    behind: list[str] = field(default_factory=list)         # obliged, record short of the fill
    held: list[str] = field(default_factory=list)           # status HOLD in the book (any authorisation)
    held_authorised: list[str] = field(default_factory=list)
    hold_reasons: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def deployed_series_end(self) -> date | None:
        return self.live_track.get("deployed_series_end")


def assess(fills: dict[str, date | None], records: dict[str, date | None],
           live_track: dict, book: dict | None,
           hold_ok: Callable[[dict], bool] = lambda s: False) -> Assessment:
    """Pure verdict. ``hold_ok(sleeve_dict)`` says whether a HOLD is authorised
    (publication_debt.hold_is_authorised in production); the default refuses
    every HOLD, which is the conservative direction - an unverified hold is
    mailed, never silently excused."""
    sleeve_venue = dict(DEFAULT_SLEEVE_VENUE)
    status: dict[str, dict] = {}
    problems: list[str] = []
    if isinstance(book, dict) and isinstance(book.get("sleeves"), list):
        for s in book["sleeves"]:
            if isinstance(s, dict) and s.get("sleeve") and s.get("venue"):
                sleeve_venue[str(s["sleeve"])] = str(s["venue"])
                status[str(s["sleeve"])] = s
    else:
        problems.append("no live_targets.json naming this fill was found; sleeve "
                        "statuses are unknown, so no HOLD can be excused")
    a = Assessment(UNKNOWN, fills, records, live_track, sleeve_venue, problems=problems)

    if any(d is None for d in fills.values()):
        a.problems.append("the fill date could not be derived from the venue calendar")
        return a
    if not live_track.get("readable"):
        a.problems.append("data/live_track.json is not readable at the ref")
        return a
    unreadable = [s for s in sleeve_venue if records.get(s) is None]
    if unreadable:
        a.problems.append(f"published rebalance record unreadable for sleeve(s) "
                          f"{','.join(sorted(unreadable))}")
        return a

    for s in sorted(sleeve_venue):
        fill = fills.get(sleeve_venue[s])
        if fill is None:
            a.problems.append(f"sleeve {s} trades on {sleeve_venue[s]}, a venue with no derived fill")
            return a
        st = status.get(s) or {}
        if str(st.get("status") or "").upper() == "HOLD":
            a.held.append(s)
            a.hold_reasons[s] = str(st.get("reason") or "no reason given")
            if hold_ok(st):
                a.held_authorised.append(s)
        if records[s] >= fill:
            a.updated.append(s)
        else:
            a.behind.append(s)

    newest = max(fills.values())
    anchor, end = live_track.get("anchor_date"), live_track.get("deployed_series_end")
    track_ok = bool(anchor and end and anchor >= newest and end >= newest)
    if not track_ok:
        a.problems.append(f"live_track.json anchor_date {anchor} / deployed_series_end {end} "
                          f"do not both reach the fill {newest}")
    if track_ok and not a.behind:
        a.verdict = OK
    elif track_ok and all(s in a.held_authorised for s in a.behind):
        a.verdict = HELD
    else:
        a.verdict = MISSED
    return a


def fills_text(fills: dict[str, date | None]) -> str:
    vals = {d for d in fills.values()}
    if len(vals) == 1:
        d = next(iter(vals))
        return d.isoformat() if d else "unknown"
    return ", ".join(f"{v} {d.isoformat() if d else 'unknown'}" for v, d in sorted(fills.items()))


def describe(a: Assessment) -> str:
    lines = [f"Fill: {fills_text(a.fills)}",
             f"Book deployed_series_end: {a.deployed_series_end or 'unreadable'} "
             f"(anchor_date {a.live_track.get('anchor_date') or 'unreadable'})"]
    if a.records:
        lines.append("Sleeve records (latest_rebalance): " + ", ".join(
            f"{s} {d.isoformat() if d else 'unreadable'}" for s, d in sorted(a.records.items())))
    lines.append(f"Sleeves updated to the fill: {', '.join(a.updated) or 'none'}")
    if a.behind:
        lines.append(f"Sleeves NOT at the fill: {', '.join(a.behind)}")
    if a.held:
        for s in a.held:
            tag = "authorised" if s in a.held_authorised else "NOT authorised"
            lines.append(f"HOLD sleeve {s} ({tag}): {a.hold_reasons.get(s)}")
    else:
        lines.append("HOLD: none")
    for p in a.problems:
        lines.append(f"Note: {p}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Success notice (local), with once-per-fill dedupe
# ---------------------------------------------------------------------------
NOTICE_STATE = Path("logs") / "postfill_notice.json"
NOTICE_RETENTION_DAYS = 120


def load_notice_state(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sent": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("sent"), dict):
        return {"sent": {}}
    return doc


def already_notified(path: Path, key: str) -> bool:
    return key in load_notice_state(path)["sent"]


def record_notified(path: Path, key: str, sha: str, now_utc: datetime) -> None:
    """Written ONLY after the SMTP server accepted the message. Prunes old keys."""
    doc = load_notice_state(path)
    cutoff = (now_utc - timedelta(days=NOTICE_RETENTION_DAYS)).isoformat()
    doc["sent"] = {k: v for k, v in doc["sent"].items()
                   if isinstance(v, dict) and str(v.get("sent_utc", "")) >= cutoff}
    doc["sent"][key] = {"sha": sha, "sent_utc": now_utc.isoformat()}
    doc["schema"] = 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def build_notice(a: Assessment, sha: str) -> tuple[str, str]:
    """(subject, body) for the post-fill confirmation."""
    if a.verdict == OK:
        subject = f"[OK] Post-fill published - fill {fills_text(a.fills)}"
    elif a.verdict == HELD:
        subject = (f"[OK] Post-fill published - fill {fills_text(a.fills)}, "
                   f"HOLD {','.join(a.behind)}")
    else:
        subject = (f"[CHECK] Post-fill pushed but fill {fills_text(a.fills)} is "
                   f"not fully recorded")
    body = f"Commit {sha} is on origin/main.\n\n{describe(a)}"
    return subject, body


def notice_decision(a: Assessment, key: str, state_path: Path) -> str:
    """'send' | 'duplicate' | 'send-unconfirmed'. A notice that does not
    confirm the fill (MISSED/UNKNOWN) is sent but never consumes the key, so
    the run that does record the fill still confirms it."""
    if a.verdict not in (OK, HELD):
        return "send-unconfirmed"
    return "duplicate" if already_notified(state_path, key) else "send"


def git_out(repo_root: Path, *args: str) -> str | None:
    try:
        cp = subprocess.run(["git", *args], cwd=str(repo_root), capture_output=True,
                            text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout.strip() if cp.returncode == 0 else None


def is_on_origin(repo_root: Path, sha: str, ref: str = "origin/main") -> bool:
    try:
        cp = subprocess.run(["git", "merge-base", "--is-ancestor", sha, ref],
                            cwd=str(repo_root), capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def postfill_success_notice(repo_root: Path, *, created_commit: bool, now_utc: datetime,
                            send: Callable[[str, str], str], sent_status: str,
                            state_path: Path, log=None, extra: str = "",
                            hold_ok: Callable[[dict], bool] | None = None) -> str:
    """Send the one confirmation for this fill, or explain why not. Returns
    the action taken: 'no-commit', 'not-on-origin', 'duplicate', 'sent',
    'sent-unconfirmed', or 'send-failed:<status>'. Never raises."""
    def note(msg: str) -> None:
        if log is not None:
            log.write(f"\n[post-fill notice] {msg}\n")
    try:
        if not created_commit:
            note("this run created no commit; nothing new was published, so no notice")
            return "no-commit"
        sha = git_out(repo_root, "rev-parse", "--short", "HEAD") or "unknown"
        if not is_on_origin(repo_root, "HEAD"):
            note(f"HEAD {sha} is not an ancestor of origin/main; no confirmation sent")
            return "not-on-origin"
        fills = latest_fills(now_utc.date())
        reader = git_reader(repo_root)
        book = book_for_fill(targets_history(repo_root, "HEAD"), fills)
        a = assess(fills, sleeve_records(reader, "HEAD"), live_track_state(reader, "HEAD"),
                   book, hold_ok or _production_hold_ok(repo_root, book, now_utc))
        key = fill_key(fills)
        decision = notice_decision(a, key, state_path)
        if decision == "duplicate":
            note(f"fill {key} was already confirmed; no second notice ({a.verdict})")
            return "duplicate"
        subject, body = build_notice(a, sha)
        outcome = send(subject, body + (f"\n\n{extra}" if extra else ""))
        if outcome != sent_status:
            note(f"send returned {outcome}; key {key} NOT recorded, a later run retries")
            return f"send-failed:{outcome}"
        if decision == "send":
            record_notified(state_path, key, sha, now_utc)
            note(f"confirmation sent and recorded for {key}")
            return "sent"
        note(f"unconfirmed notice sent ({a.verdict}); key {key} left open")
        return "sent-unconfirmed"
    except Exception as exc:  # noqa: BLE001 - a notice never breaks a green run
        note(f"failed, ignored: {type(exc).__name__}: {exc}")
        return f"error:{type(exc).__name__}"


def _production_hold_ok(repo_root: Path, book: dict | None, now_utc: datetime):
    """publication_debt.hold_is_authorised against the verified release, lazily."""
    cache: list = []

    def ok(sleeve: dict) -> bool:
        try:
            import publication_debt as pd_
            if not cache:
                cache.append(pd_.release_authorisation(repo_root, now_utc))
            authorised, _ = pd_.hold_is_authorised(
                sleeve, (book or {}).get("as_of"), cache[0], now_utc.date())
            return bool(authorised)
        except Exception:  # noqa: BLE001 - unverifiable means not authorised
            return False
    return ok
