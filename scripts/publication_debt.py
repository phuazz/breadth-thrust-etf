"""What publication does the schedule OWE, and has it actually happened?

WHY THIS EXISTS (2026-09-16). The post-fill pair fired six times across
15 and 16 September and published nothing. Every firing was a fresh
attempt that knew only one thing about its own history: the per-day
green-run marker, which answers "did a green run of this cadence already
happen TODAY". That is the right question for suppressing a 10:00 retry
after a green 09:00 run, and the wrong one for everything else. It cannot
say that Monday's fill is still unrecorded, so when the Tuesday and
Wednesday windows both closed on failures the schedule simply moved on to
the following Tuesday with a six-day hole behind it and nothing owed.

WHAT THE FIRST VERSION OF THIS MODULE GOT WRONG (adjudicated 2026-09-16,
external review). It derived the obligation solely from the CURRENT
``next_fill``, so the obligation was as transient as the file it was read
from:

  * ``next_fill`` advancing to the following week made the unpublished
    week disappear. ``evaluate(fill=2026-09-21, through=2026-09-08,
    asof=2026-09-16)`` returned ``owed: false`` - the 14 September
    obligation was gone, silently, exactly when it mattered.
  * A missing, malformed or venue-divergent ``next_fill`` also returned
    "nothing owed", so every unreadable state read as a healthy one.
  * Publication evidence was a commit SUBJECT read from ``HEAD``. A local
    commit that never pushed discharged the debt; so did an unrelated
    refresh whose panel happened to be fresh. Neither is publication, and
    the subject names the CSP1 panel date rather than confirming that the
    sleeve rebalance records the fill needs actually reached origin.

The obligation is therefore DURABLE and kept in a ledger of its own
(``logs/publication_obligations.json``), one entry per (venue, fill date),
carried until it is discharged, superseded or explicitly written off. An
obligation that can be erased by the thing it is meant to police is not an
obligation.

VENUE-AWARE. The fill date is per venue, and the venues genuinely differ:
on the Labor Day week of 7 September 2026 XETR filled Monday 7th while
NYSE filled Tuesday 8th, which is visible in the engines' own records
(sleeve D 2026-09-07, sleeves A/B/C 2026-09-08). One obligation per venue,
discharged by the sleeves that trade on that venue.

EVIDENCE IS REMOTE AND SPECIFIC. Discharge requires that, at the REMOTE
publication ref (``origin/main`` by default, never ``HEAD``), every sleeve
of that venue carries ``headline.latest_rebalance.date`` at or past the
fill date. That is the record the surfaces read. Absence of the file, an
unreadable ref, or a sleeve whose record cannot be resolved is UNKNOWN -
never "nothing owed".

WHAT THIS MODULE DOES NOT KNOW. Whether the broker filled anything. It
measures publication only: whether the recorded book caught up with the
fill the engine scheduled. ``executed`` in live_targets.json is a constant
by construction and is not read here.

Python datetime months are 1-indexed (January = 1). Every date is a
calendar date, never a datetime, and date arithmetic goes through
``datetime.date`` / ``timedelta`` rather than day-count arithmetic by
hand. All strings are plain ASCII.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

LEDGER_SCHEMA = 1

# The subject line scheduled_refresh.scheduled_commit_message writes. Kept
# and still pinned by a test so the two cannot drift, but DEMOTED: it is
# reported as context, never as discharge evidence. The panel date in that
# subject is the CSP1 panel, which says nothing about whether the sleeve
# rebalance records moved.
_SUBJECT = re.compile(
    r"^Local (?P<kind>weekly|post-fill) refresh (?P<ran>\d{4}-\d{2}-\d{2}) "
    r"\(scheduled\): panels current to (?P<through>\d{4}-\d{2}-\d{2})")

# How long a fill may go unrecorded before the debt is an escalation rather
# than a retry. Three calendar days from the fill: a Monday fill must be
# published by Thursday. Calibrated to the existing Tue/Wed pair plus one
# day of slack, so a single bad Tuesday does not escalate but a silently
# dead pair does. Deliberately NOT business days: the point is wall-clock
# exposure of a published book that disagrees with the traded one. A first
# calibration, not a measured one.
DEFAULT_GRACE_DAYS = 3

# Each sleeve's published record. The rebalance record - not the trade
# record - is the right evidence: it advances on a HELD week too, so a week
# that traded nothing still moves it, and a stalled publication does not
# hide behind "nothing changed". Verified against the live files on
# 2026-09-16: sleeve C last TRADED 2026-08-24 and its latest_rebalance
# reads 2026-09-08.
SLEEVE_FILES = {
    "A": "topk_robustness.json",
    "B": "asset_class_rotation.json",
    "C": "thematic_rotation.json",
    "D": "europe_rotation.json",
}

# Obligation states. A closed vocabulary, because callers branch on it and
# because "unknown" must never be spelled the same way as "nothing owed".
OWED = "owed"                  # fill reached, publication has not
PENDING = "pending"            # fill is in the future; recorded, not yet due
DISCHARGED = "discharged"      # the published record reaches the fill itself
SUPERSEDED = "superseded"      # the published record jumped PAST it: a
                               # permanent miss, not a success
UNKNOWN = "unknown"            # evidence unreadable; never silently green
STATES = (OWED, PENDING, DISCHARGED, SUPERSEDED, UNKNOWN)
TERMINAL = (DISCHARGED, SUPERSEDED)

# Ledger bounds. A diagnostic that grows without limit is one an operator
# eventually deletes.
MAX_OBLIGATIONS = 60
# Escalation is a notification, not a dead man's handle. An obligation that
# cannot be discharged - a sleeve permanently on HOLD, say - would otherwise
# email every day until somebody muted the channel, which is how a notifier
# gets ignored. After this many the obligation stays visible in every
# verdict and stops mailing.
MAX_ESCALATIONS = 5


@dataclass(frozen=True)
class Obligation:
    """One (venue, fill date) publication obligation."""
    venue: str
    fill_date: str
    sleeves: tuple[str, ...]
    decision_session: str | None = None

    @property
    def key(self) -> str:
        return f"{self.venue}|{self.fill_date}"


@dataclass(frozen=True)
class DebtReport:
    """The verdict for one cadence as at ``asof``.

    ``owed`` and ``unknown`` are separate on purpose. A caller that gates
    work on debt must treat UNKNOWN as "proceed", not as "nothing to do":
    the whole defect this replaces was an unreadable state reading green.
    """
    owed: bool
    unknown: bool
    escalate: bool
    cadence: str
    asof: str
    oldest_owed_fill: str | None
    age_days: int | None
    grace_days: int
    obligations: tuple[dict, ...] = ()
    problems: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def should_run(self) -> bool:
        """Whether a debt-gated firing must do the work."""
        return bool(self.owed or self.unknown)


# ---------------------------------------------------------------------------
# Pure readers of the engine's own state
# ---------------------------------------------------------------------------
def parse_publication(subject: str) -> tuple[str, date, date] | None:
    """(cadence, ran_on, panels_current_to) from a scheduled commit subject.

    Context only - see the module docstring. Returns None for any subject
    this module does not own, so an unrelated commit is never read as a
    publication.
    """
    m = _SUBJECT.match(str(subject).strip())
    if not m:
        return None
    cadence = "weekend" if m.group("kind") == "weekly" else "post-fill"
    try:
        return (cadence,
                date.fromisoformat(m.group("ran")),
                date.fromisoformat(m.group("through")))
    except ValueError:                      # pragma: no cover - regex pins it
        return None


def _as_date(raw) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def observe_obligations(targets: dict | None
                        ) -> tuple[list[Obligation], list[str], list[str]]:
    """The obligations ``live_targets.json`` currently implies, per VENUE.

    Returns (obligations, blocking, notes). BLOCKING means the caller cannot
    tell whether an obligation exists and must report UNKNOWN; a note is
    context that does not impair the verdict. The caller must never read an
    empty obligation list plus a blocking problem as "nothing owed".

    ``next_fill.by_venue`` is authoritative; ``next_fill.date`` is a
    convenience that is None whenever the venues disagree, and reading only
    it is how divergent venue fills became invisible.
    """
    blocking: list[str] = []
    notes: list[str] = []
    if not isinstance(targets, dict):
        return [], ["live_targets.json missing or not an object"], notes
    nxt = targets.get("next_fill")
    if not isinstance(nxt, dict):
        return [], ["live_targets.json carries no next_fill object"], notes

    # sleeve -> venue, from the book itself rather than a hardcoded map, so
    # a sleeve that changes venue moves its obligation with it.
    by_sleeve: dict[str, str] = {}
    sleeves = targets.get("sleeves")
    if isinstance(sleeves, list):
        for sl in sleeves:
            if isinstance(sl, dict) and sl.get("sleeve") and sl.get("venue"):
                by_sleeve[str(sl["sleeve"])] = str(sl["venue"])
    if not by_sleeve:
        blocking.append("live_targets.json carries no sleeve/venue map; "
                        "obligations cannot name the sleeves that discharge "
                        "them")

    fills = nxt.get("by_venue")
    decisions = nxt.get("decision_by_venue")
    decisions = decisions if isinstance(decisions, dict) else {}
    out: list[Obligation] = []
    if isinstance(fills, dict) and fills:
        for venue, raw in sorted(fills.items()):
            fd = _as_date(raw)
            if fd is None:
                blocking.append(f"venue {venue}: fill date {raw!r} is not a "
                                f"calendar date")
                continue
            venue_sleeves = tuple(sorted(s for s, v in by_sleeve.items()
                                         if v == str(venue)))
            out.append(Obligation(str(venue), fd.isoformat(), venue_sleeves,
                                  _iso_or_none(decisions.get(venue))))
        return out, blocking, notes

    # No by_venue map. Fall back to the scalar, which is the pre-venue shape
    # and still appears in fixtures; record that the venue is unresolved.
    fd = _as_date(nxt.get("date"))
    if fd is None:
        blocking.append("next_fill carries neither a by_venue map nor a "
                        "single fill date")
        return [], blocking, notes
    notes.append("next_fill has no by_venue map; obligation recorded "
                 "against all sleeves")
    return ([Obligation("ALL", fd.isoformat(), tuple(sorted(by_sleeve)),
                        _iso_or_none(nxt.get("decision_session")))],
            blocking, notes)


def _iso_or_none(raw) -> str | None:
    d = _as_date(raw)
    return d.isoformat() if d else None


def classify(ob: Obligation, published: dict[str, date | None],
             asof: date) -> tuple[str, str]:
    """(state, reason) for one obligation against the published records.

    ``published`` maps sleeve -> the published ``latest_rebalance`` date, or
    None when that sleeve's record could not be read. An unreadable record
    is UNKNOWN: it is the state the incident was actually in, and it must
    not be spelled "discharged".
    """
    fill = _as_date(ob.fill_date)
    if fill is None:                        # pragma: no cover - constructor pins
        return UNKNOWN, f"fill date {ob.fill_date!r} is not a calendar date"
    if not ob.sleeves:
        return UNKNOWN, (f"no sleeve is known to trade on {ob.venue}; the "
                         f"obligation cannot be checked")
    missing = [s for s in ob.sleeves if published.get(s) is None]
    if missing:
        return UNKNOWN, (f"published rebalance record unreadable for sleeve(s) "
                         f"{','.join(missing)}")
    dates = {s: published[s] for s in ob.sleeves}
    behind = {s: d for s, d in dates.items() if d < fill}
    if behind:
        detail = ", ".join(f"{s} at {d.isoformat()}"
                           for s, d in sorted(behind.items()))
        if asof < fill:
            return PENDING, (f"fill {ob.fill_date} ({ob.venue}) is in the "
                             f"future as at {asof.isoformat()}")
        age = (asof - fill).days
        return OWED, (f"fill {ob.fill_date} ({ob.venue}) unrecorded for "
                      f"{age} day(s); published: {detail}")
    if any(d == fill for d in dates.values()):
        return DISCHARGED, (f"fill {ob.fill_date} ({ob.venue}) recorded by "
                            f"sleeve(s) "
                            f"{','.join(s for s, d in sorted(dates.items()) if d == fill)}")
    ahead = ", ".join(f"{s} at {d.isoformat()}" for s, d in sorted(dates.items()))
    return SUPERSEDED, (f"fill {ob.fill_date} ({ob.venue}) was never recorded; "
                        f"the published book has moved past it ({ahead})")


# ---------------------------------------------------------------------------
# Repository readers - impure, thin, and kept out of the logic above so the
# whole verdict surface is testable without a git tree.
# ---------------------------------------------------------------------------
def _git(repo_root: Path, args: list[str], timeout: int = 60):
    try:
        return subprocess.run(["git", *args], cwd=str(repo_root),
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def publication_ref(repo_root: Path, *, remote: str = "origin",
                    branch: str = "main", fetch: bool = False
                    ) -> tuple[str | None, str | None, list[str]]:
    """(ref, sha, problems) for the REMOTE publication ref.

    ``origin/main``, not HEAD. A commit sitting unpushed in the automation
    clone is not a publication: on a push failure the refresh exits 5 with
    the commit local, and reading HEAD would have called that debt
    discharged and suppressed the very retry that recovers it.

    ``fetch`` is off by default because the caller that matters - the
    scheduled refresh - has just run ``git pull --rebase origin main`` and
    its remote ref is current. A standalone reader passes --fetch.
    """
    problems: list[str] = []
    ref = f"{remote}/{branch}"
    if fetch:
        cp = _git(repo_root, ["fetch", "--quiet", remote, branch], timeout=120)
        if cp is None or cp.returncode != 0:
            problems.append(f"git fetch {remote} {branch} failed; the remote "
                            f"ref may be stale")
    cp = _git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if cp is None or cp.returncode != 0:
        problems.append(f"remote ref {ref} does not resolve; publication "
                        f"cannot be verified")
        return None, None, problems
    return ref, cp.stdout.strip(), problems


def unpushed_commits(repo_root: Path, ref: str) -> int | None:
    """How many local commits are not on ``ref``. None when unreadable."""
    cp = _git(repo_root, ["rev-list", "--count", f"{ref}..HEAD"])
    if cp is None or cp.returncode != 0:
        return None
    try:
        return int(cp.stdout.strip())
    except ValueError:                      # pragma: no cover
        return None


def published_rebalances(repo_root: Path, ref: str,
                         sleeves=None) -> tuple[dict[str, date | None],
                                                list[str]]:
    """Each sleeve's published ``headline.latest_rebalance.date`` at ``ref``.

    Read out of the git object store, never the working tree: the working
    copy is mid-run scratch and can be in any state, whereas the ref is what
    was actually published.
    """
    wanted = list(sleeves) if sleeves else list(SLEEVE_FILES)
    out: dict[str, date | None] = {}
    problems: list[str] = []
    for sleeve in wanted:
        name = SLEEVE_FILES.get(sleeve)
        if not name:
            out[sleeve] = None
            problems.append(f"sleeve {sleeve} has no published record file")
            continue
        cp = _git(repo_root, ["show", f"{ref}:data/{name}"])
        if cp is None or cp.returncode != 0:
            out[sleeve] = None
            problems.append(f"data/{name} not readable at {ref}")
            continue
        try:
            doc = json.loads(cp.stdout)
        except ValueError:
            out[sleeve] = None
            problems.append(f"data/{name} at {ref} is not valid JSON")
            continue
        rec = ((doc.get("headline") or {}) if isinstance(doc, dict) else {})
        rec = rec.get("latest_rebalance") if isinstance(rec, dict) else None
        d = _as_date((rec or {}).get("date")) if isinstance(rec, dict) else None
        if d is None:
            problems.append(f"data/{name} at {ref} carries no "
                            f"headline.latest_rebalance.date")
        out[sleeve] = d
    return out, problems


def latest_publication(repo_root: Path, cadence: str = "post-fill",
                       *, ref: str = "origin/main", limit: int = 200
                       ) -> date | None:
    """Panel date of the newest ``cadence`` publication subject on ``ref``.

    CONTEXT ONLY. Reported beside the verdict because it is what an operator
    greps for, and defaulted to the remote ref rather than HEAD, but it does
    not discharge anything - see the module docstring.
    """
    kind = "weekly" if cadence == "weekend" else "post-fill"
    cp = _git(repo_root, ["log", ref, f"-{int(limit)}", "--format=%s",
                          f"--grep=^Local {kind} refresh "])
    if cp is None or cp.returncode != 0:
        return None
    best: date | None = None
    for line in cp.stdout.splitlines():
        parsed = parse_publication(line)
        if parsed is None or parsed[0] != cadence:
            continue
        if best is None or parsed[2] > best:
            best = parsed[2]
    return best


def load_targets(repo_root: Path) -> dict | None:
    """live_targets.json from the working tree, or None."""
    p = Path(repo_root) / "data" / "live_targets.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


# ---------------------------------------------------------------------------
# The durable ledger
# ---------------------------------------------------------------------------
def ledger_path(repo_root: Path) -> Path:
    return Path(repo_root) / "logs" / "publication_obligations.json"


def load_ledger(path: Path) -> tuple[dict, list[str]]:
    """The obligation ledger, plus any problem reading it.

    A corrupt ledger starts a fresh one rather than raising, but says so:
    silently forgetting every outstanding obligation is the defect this
    module exists to remove.
    """
    empty = {"schema": LEDGER_SCHEMA, "obligations": {}}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return empty, []
    try:
        doc = json.loads(raw)
    except ValueError:
        return empty, [f"{Path(path).name} is not valid JSON; obligation "
                       f"history was lost"]
    if not isinstance(doc, dict) or not isinstance(doc.get("obligations"), dict):
        return empty, [f"{Path(path).name} has an unrecognised shape; "
                       f"obligation history was lost"]
    clean = {k: v for k, v in doc["obligations"].items() if isinstance(v, dict)}
    dropped = len(doc["obligations"]) - len(clean)
    problems = ([f"{dropped} malformed obligation record(s) dropped"]
                if dropped else [])
    return {"schema": LEDGER_SCHEMA, "obligations": clean}, problems


def _atomic_write(path: Path, text: str) -> bool:
    """Replace ``path`` atomically. False when the write failed."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, ValueError):
        return False


def save_ledger(path: Path, ledger: dict) -> bool:
    """Write the ledger atomically, bounded to MAX_OBLIGATIONS.

    Eviction takes the oldest TERMINAL entries first, so an outstanding
    obligation is never dropped to make room for a discharged one.
    """
    obs = dict(ledger.get("obligations") or {})
    if len(obs) > MAX_OBLIGATIONS:
        terminal = sorted((k for k, v in obs.items()
                           if v.get("state") in TERMINAL),
                          key=lambda k: str(obs[k].get("fill_date") or ""))
        for k in terminal[:len(obs) - MAX_OBLIGATIONS]:
            obs.pop(k, None)
    if len(obs) > MAX_OBLIGATIONS:          # still over: drop oldest overall
        for k in sorted(obs, key=lambda k: str(obs[k].get("fill_date") or "")
                        )[:len(obs) - MAX_OBLIGATIONS]:
            obs.pop(k, None)
    doc = {"schema": LEDGER_SCHEMA,
           "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "obligations": obs}
    return _atomic_write(path, json.dumps(doc, indent=2, sort_keys=True))


def merge_obligations(ledger: dict, observed: list[Obligation],
                      now_utc: datetime) -> dict:
    """Record newly observed obligations WITHOUT touching existing ones.

    This is the property the first version lacked: advancing ``next_fill``
    ADDS an obligation, and cannot remove the one behind it.
    """
    obs = dict(ledger.get("obligations") or {})
    for ob in observed:
        cur = obs.get(ob.key)
        if cur is None:
            obs[ob.key] = {
                "venue": ob.venue, "fill_date": ob.fill_date,
                "sleeves": list(ob.sleeves),
                "decision_session": ob.decision_session,
                "first_seen_utc": now_utc.isoformat(timespec="seconds"),
                "state": PENDING, "reason": "recorded",
                "ever_discharged": False, "escalations": 0,
                "escalated_on": None,
            }
        else:
            # The sleeve set can legitimately grow (a sleeve added, a venue
            # reassigned). Never shrink it away: fewer sleeves is a weaker
            # discharge test, and that direction must be a deliberate act.
            cur["sleeves"] = sorted(set(cur.get("sleeves") or []) | set(ob.sleeves))
            if ob.decision_session and not cur.get("decision_session"):
                cur["decision_session"] = ob.decision_session
    return {"schema": LEDGER_SCHEMA, "obligations": obs}


def _obligation_from_record(key: str, rec: dict) -> Obligation | None:
    venue = rec.get("venue") or (key.split("|")[0] if "|" in key else None)
    fill = rec.get("fill_date") or (key.split("|")[1] if "|" in key else None)
    if not venue or not _as_date(fill):
        return None
    return Obligation(str(venue), str(fill),
                      tuple(sorted(str(s) for s in (rec.get("sleeves") or []))),
                      rec.get("decision_session"))


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
def current_debt(repo_root: Path, asof: date, *, cadence: str = "post-fill",
                 grace_days: int = DEFAULT_GRACE_DAYS,
                 ledger: Path | None = None,
                 remote: str = "origin", branch: str = "main",
                 fetch: bool = False, persist: bool = True,
                 now_utc: datetime | None = None) -> DebtReport:
    """The live verdict for ``repo_root``. Never raises.

    The weekend cadence produces the book the fill will be RANKED on and has
    no fill to record, so it carries no debt in these terms. It is stated
    rather than inferred, and the obligation ledger is still updated, so a
    weekend run contributes its observation of the upcoming fill.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    lp = Path(ledger) if ledger is not None else ledger_path(repo_root)
    book, blocking = load_ledger(lp)
    notes: list[str] = []
    targets = load_targets(repo_root)
    observed, obs_blocking, obs_notes = observe_obligations(targets)
    blocking.extend(obs_blocking)
    notes.extend(obs_notes)
    book = merge_obligations(book, observed, now_utc)

    ref, sha, ref_problems = publication_ref(repo_root, remote=remote,
                                             branch=branch, fetch=fetch)
    (blocking if ref is None else notes).extend(ref_problems)
    evidence: dict = {"ref": ref, "sha": sha, "fetched": bool(fetch)}
    published: dict[str, date | None] = {}
    if ref is not None:
        wanted = sorted({s for rec in book["obligations"].values()
                         for s in (rec.get("sleeves") or [])})
        published, pub_problems = published_rebalances(repo_root, ref, wanted)
        notes.extend(pub_problems)   # each also makes its obligation UNKNOWN
        evidence["published_rebalances"] = {
            s: (d.isoformat() if d else None) for s, d in published.items()}
        evidence["unpushed_commits"] = unpushed_commits(repo_root, ref)
        subj = latest_publication(repo_root, cadence, ref=ref)
        evidence["latest_publication_subject"] = subj.isoformat() if subj else None

    # Classify every obligation the ledger holds, not just the current one.
    records: list[dict] = []
    newly_missed: list[str] = []
    for key, rec in sorted(book["obligations"].items()):
        ob = _obligation_from_record(key, rec)
        if ob is None:
            rec["state"], rec["reason"] = UNKNOWN, "record unreadable"
            records.append({"key": key, **rec})
            blocking.append(f"obligation {key} is unreadable")
            continue
        if ref is None:
            state, reason = UNKNOWN, "no remote publication ref to check against"
        else:
            state, reason = classify(ob, published, asof)
        was = rec.get("state")
        rec["state"], rec["reason"] = state, reason
        if state == DISCHARGED:
            rec["ever_discharged"] = True
            rec.setdefault("discharged_utc",
                           now_utc.isoformat(timespec="seconds"))
            rec["evidence_sha"] = sha
        if state == SUPERSEDED and not rec.get("ever_discharged"):
            rec["missed"] = True
            if was != SUPERSEDED:
                newly_missed.append(key)
        rec["age_days"] = ((asof - date.fromisoformat(ob.fill_date)).days
                           if asof >= date.fromisoformat(ob.fill_date) else None)
        records.append({"key": key, **rec})

    owed_recs = [r for r in records if r["state"] == OWED]
    unknown_recs = [r for r in records if r["state"] == UNKNOWN]
    owed = bool(owed_recs) and cadence != "weekend"
    unknown = bool(unknown_recs) or bool(blocking)
    problems = [*blocking, *notes]
    oldest = min((r["fill_date"] for r in owed_recs), default=None)
    age = (asof - date.fromisoformat(oldest)).days if oldest else None
    escalate = bool(cadence != "weekend"
                    and ((age is not None and age > grace_days)
                         or newly_missed))

    if cadence == "weekend":
        reason = ("weekend cadence records no fill; obligations observed and "
                  "carried for the post-fill pair")
    elif owed:
        reason = "; ".join(r["reason"] for r in owed_recs)
    elif unknown:
        reason = "; ".join((*(r["reason"] for r in unknown_recs), *blocking))
    else:
        reason = "; ".join(r["reason"] for r in records) or "no obligation on record"

    if persist:
        save_ledger(lp, book)

    return DebtReport(
        owed=owed, unknown=unknown and cadence != "weekend", escalate=escalate,
        cadence=cadence, asof=asof.isoformat(), oldest_owed_fill=oldest,
        age_days=age, grace_days=grace_days,
        obligations=tuple(records), problems=tuple(problems),
        evidence=evidence, reason=reason)


def mark_escalated(path: Path, keys, on: date) -> bool:
    """Record that ``keys`` were escalated on ``on``. Bounded and idempotent."""
    book, _ = load_ledger(path)
    changed = False
    for k in keys:
        rec = book["obligations"].get(k)
        if rec is None or rec.get("escalated_on") == on.isoformat():
            continue
        rec["escalated_on"] = on.isoformat()
        rec["escalations"] = int(rec.get("escalations") or 0) + 1
        changed = True
    return save_ledger(path, book) if changed else False


def escalation_due(report: DebtReport, on: date) -> list[str]:
    """Which obligation keys should MAIL today.

    Suppressed once already escalated today, and capped at MAX_ESCALATIONS
    per obligation so an undischargeable one stops training the operator to
    ignore the channel. It stays in every verdict either way.
    """
    if not report.escalate:
        return []
    out = []
    for rec in report.obligations:
        if rec.get("state") not in (OWED,) and not rec.get("missed"):
            continue
        if rec.get("escalated_on") == on.isoformat():
            continue
        if int(rec.get("escalations") or 0) >= MAX_ESCALATIONS:
            continue
        out.append(rec["key"])
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI: print the verdict as JSON.

    Exit 0 nothing owed, 1 owed, 2 owed past grace (escalation),
    3 UNKNOWN - the evidence could not be read. Three is not a pass.
    """
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--cadence", default="post-fill",
                    choices=("post-fill", "weekend"))
    ap.add_argument("--asof", default=None,
                    help="YYYY-MM-DD; default the local date now")
    ap.add_argument("--grace-days", type=int, default=DEFAULT_GRACE_DAYS)
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--fetch", action="store_true",
                    help="Update the remote ref first. Off by default "
                         "because the refresh has just pulled.")
    ap.add_argument("--no-persist", action="store_true",
                    help="Do not write the obligation ledger.")
    args = ap.parse_args(argv)
    asof = (date.fromisoformat(args.asof) if args.asof
            else datetime.now(timezone.utc).astimezone().date())
    d = current_debt(Path(args.repo), asof, cadence=args.cadence,
                     grace_days=args.grace_days, remote=args.remote,
                     branch=args.branch, fetch=args.fetch,
                     persist=not args.no_persist)
    print(json.dumps(d.as_dict(), indent=2))
    if d.escalate:
        return 2
    if d.owed:
        return 1
    return 3 if d.unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
