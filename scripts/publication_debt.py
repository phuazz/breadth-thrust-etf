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

THE CONTRACT, STATED (2026-09-16). An obligation is owed by the sleeves
that were going to TRADE the fill, and discharged by the record those
sleeves publish. Three conditions sit outside that and are handled
explicitly rather than by silence:

  * AN AUTHORISED HOLD IS NOT A MISSED FILL. ``component_release.verify``
    admits a HOLD only for sleeve D, only with a reason, and only when it
    is risk-only. Such a sleeve is not going to trade, so it is not obliged
    to advance its record, and a venue whose every sleeve is on HOLD
    carries no debt at all. A sleeve ever observed READY for a fill stays
    obliged: an obligation that was once real is not retired by a later
    change of state.
  * A REBALANCE CAN BE SKIPPED. ``run_portfolio`` drops a rebalance date
    whose decision session sits beyond the panel's ``validated_through``,
    so a refresh can publish while the record stays behind the fill. The
    obligation is still owed - the fill really is unrecorded - but retrying
    will not move it, and the escalation says so rather than repeating
    "the refresh did not run".
  * A MISS MUST BE EVIDENCED. See the history walk below.

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
DISCHARGED = "discharged"      # a publication carried this fill
SUPERSEDED = "superseded"      # the published record is past it and no
                               # publication carrying it was found. Whether
                               # that is a MISS or merely unobserved is a
                               # separate field - see ``missed``.
NOT_OBLIGED = "not_obliged"    # every sleeve of this venue was on an
                               # authorised HOLD for this fill
REVISED = "revised"            # the venue's calendar moved this fill; the
                               # obligation was replaced, not missed

# Why a revision check answered as it did. A STRUCTURED verdict, because two
# escalation paths branch on it and both used to do so by searching the
# reason PROSE for "could not be read" (2026-09-17, sixth review). A reworded
# message would have silently flipped a suppression into an assertion, or the
# reverse, with no test failing.
REVISION_CONFIRMED = "confirmed"          # the calendar shows the fill moved
REVISION_CALENDAR_UNREADABLE = "calendar_unreadable"   # no evidence either way
REVISION_OLD_FILL_STILL_TRADES = "old_fill_still_trades"
REVISION_NEW_FILL_NOT_A_SESSION = "new_fill_not_a_session"
REVISION_WRONG_DECISION_SESSION = "wrong_decision_session"
REVISION_UNREADABLE_DATES = "unreadable_dates"
# The one status that means "we cannot tell", and therefore the only one that
# may excuse an obligation. Every other status is the calendar ANSWERING.
REVISION_INCONCLUSIVE = (REVISION_CALENDAR_UNREADABLE, REVISION_UNREADABLE_DATES)
UNKNOWN = "unknown"            # evidence unreadable; never silently green
STATES = (OWED, PENDING, DISCHARGED, SUPERSEDED, NOT_OBLIGED, REVISED, UNKNOWN)
TERMINAL = (DISCHARGED, SUPERSEDED, NOT_OBLIGED, REVISED)

# How old a release's anchor may be and still exempt a sleeve. One weekly
# decision cycle plus three days of slack: a release sealed for the week
# ending Friday covers the Monday fill and the Tuesday and Wednesday
# post-fill pair, and stops covering anything once the next decision week
# has been and gone. Calendar days on purpose - the point is wall-clock
# staleness of the pair of files, not sessions.
HOLD_AUTHORISATION_MAX_AGE_DAYS = 10

# How far behind the clock the BOOK's own newest fill may fall before the
# module stops treating what it reads as a current picture. Same cadence
# reasoning as the authorisation bound above: past one decision cycle plus
# slack, a book that has not moved is not telling us which fills have since
# occurred, and agreement between it and an equally old release proves only
# that both stopped.
BOOK_STALE_DAYS = 10

# The cadences a publishing run carries. A file that names one of these, and
# parses, is evidence the clone has published before; a blank, malformed or
# collection-only artefact is not (2026-09-16, fifth review).
CADENCE_NAMES = ("weekend", "post-fill")

# How long a lost-write gap in the obligation ledger keeps making the verdict
# UNKNOWN. Thirty days comfortably outlives any obligation that could still
# be recovered by publishing; past it the gap stays in the continuity file as
# a permanent record but stops colouring every verdict.
GAP_RELEVANCE_DAYS = 30

# How far back the publication history is walked when deciding whether a fill
# was ever recorded. Only superseded CANDIDATES trigger the walk, so this is
# a few dozen `git show` calls in a rare branch, not a per-run cost.
HISTORY_LIMIT = 40

# Ledger bounds. A diagnostic that grows without limit is one an operator
# eventually deletes.
MAX_OBLIGATIONS = 60
# Escalation is a notification, not a dead man's handle. An obligation that
# cannot be discharged - a sleeve permanently on HOLD, say - would otherwise
# email every day until somebody muted the channel, which is how a notifier
# gets ignored. After this many the obligation stays visible in every
# verdict and stops mailing.
MAX_ESCALATIONS = 5
# The same bound for a NOTICE - a condition an operator must hear about that
# is not an overdue publication. A frozen venue that nobody fixes would
# otherwise mail every day until the channel was muted.
#
# COUNTED ON DELIVERY, NOT ON INTENT (2026-09-17, eighth review). The first
# version incremented this whenever the notice block ran, because the mailer
# returned nothing at all: five firings against an unconfigured SMTP - which
# is EXACTLY the state of the incident this workstream exists for, GMAIL_USER
# unset - exhausted the budget without a word reaching anybody. What is
# capped here is the number of times an operator was actually told.
MAX_NOTICES = 5
# Attempts are capped separately and more loosely. A channel that refuses
# every time must stop being retried eventually, but it must survive far more
# firings than the delivered budget, or a transient outage spends it.
MAX_NOTICE_ATTEMPTS = 20
# Retention for the notices map itself. MAX_NOTICES bounds sends per key; this
# bounds the number of keys, which grow as venues and fills accumulate.
#
# EXHAUSTION IS NOT A LICENCE TO FORGET (2026-09-17, ninth review). The eighth
# pass evicted exhausted records first, on the reasoning that a spent record
# can no longer alert. It can: evicting it deletes the very count that stopped
# it, so the next firing saw no record, treated a condition that had already
# mailed five times as new, and re-armed the whole budget. A spent record is a
# TOMBSTONE and it is what keeps the condition quiet, so eviction now turns on
# whether the CONDITION is still being reported, not on whether the budget is
# spent.
MAX_NOTICE_KEYS = 40
# How long a record survives after its condition stops being reported. Every
# firing stamps ``last_seen`` on every live notice, so a record that falls this
# far behind the freshest one describes something that resolved.
NOTICE_RETENTION_DAYS = 120

# What a delivery attempt is known to have achieved. "delivered" means the
# SMTP server accepted the message - it does not mean a person read it, and
# nothing in this module may claim that it does.
NOTICE_DELIVERED = "delivered"
NOTICE_UNCONFIRMED = "unconfirmed"   # unconfigured, refused, or never tried

# The result of asking to send one notice. THESE ARE THREE ANSWERS, NOT TWO
# (2026-09-17, ninth review). The eighth pass returned a bare bool, and its
# "already recorded today" branch returned True - the same value as "you may
# send". Two firings that overlapped, which an hourly schedule over a one-to-
# four-hour refresh produces routinely, therefore both read authorisation and
# both mailed.
NOTICE_CLAIMED = "claimed"                   # this process may send
NOTICE_ALREADY_CLAIMED = "already_claimed"   # somebody else holds it
NOTICE_UNCLAIMABLE = "unclaimable"           # no durable claim: do NOT send
# Claim files are per (notice, day) and are swept once they are this old.
NOTICE_CLAIM_TTL_DAYS = 30
# Retained evidence of a notice record that could not be read.
MAX_UNREADABLE_NOTICES = 20
NOTICE_EVIDENCE_CHARS = 120


@dataclass(frozen=True)
class Obligation:
    """One (venue, fill date) publication obligation.

    ``on_hold`` names the sleeves observed on an AUTHORISED HOLD for this
    fill and never observed READY for it. Those sleeves are not obliged to
    advance their rebalance record, because they are not going to trade:
    ``component_release.verify`` admits a HOLD only for sleeve D, only with
    a reason, and only when it is risk-only. Counting one as an unpublished
    fill would redefine a sanctioned decision as an operational miss.

    The direction is deliberately one-way. A sleeve ever observed READY for
    this fill stays obliged even if a later observation shows it on HOLD:
    an obligation that was once real is not retired by a subsequent change
    of state.
    """
    venue: str
    fill_date: str
    sleeves: tuple[str, ...]
    decision_session: str | None = None
    on_hold: tuple[str, ...] = ()
    ready_final: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.venue}|{self.fill_date}"

    @property
    def obliged(self) -> tuple[str, ...]:
        return tuple(s for s in self.sleeves if s not in set(self.on_hold))


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
    # Conditions an operator must be TOLD about that are not an overdue
    # publication. A frozen venue is the first: it owes nothing, so it can
    # never escalate, and before this it produced daily catch-up work and a
    # log line that nothing read (2026-09-17, seventh review).
    notices: tuple[dict, ...] = ()

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


def venue_sessions(venue: str, start: date, end: date) -> set[str] | None:
    """The venue's trading sessions in ``[start, end]``, or None.

    The SAME calendar the engines and ``next_fill_date`` use, imported
    lazily so this module stays importable where the market-calendar
    package is not installed. None means "could not be established", which
    the caller must treat as no evidence either way.
    """
    try:
        from venue_calendars import get_calendar
        sched = get_calendar(venue).schedule(start_date=start.isoformat(),
                                             end_date=end.isoformat())
        return {str(d.date()) for d in sched.index}
    except Exception:  # noqa: BLE001 - an absent calendar is not a verdict
        return None


def fill_was_revised(venue: str, old_fill: str, new_fill: str,
                     decision_session: str | None) -> tuple[bool, str, str]:
    """(verified, status, why) that ``old_fill`` was REPLACED by ``new_fill``.

    ``status`` is one of the REVISION_* constants. Callers branch on it and
    never on ``why``, which is prose for a human.

    A schedule revision and a missed publication look identical in the
    ledger: an older obligation nobody discharged, and a newer one that was.
    They are told apart on the venue's own calendar, and only on evidence:

      * the old fill is no longer a session on that venue - the session the
        obligation named does not exist, so nothing could have filled on it;
      * the new fill IS a session; and
      * the new fill is ranked on the SAME decision session the old
        obligation recorded, so it is the same scheduled rebalance moved,
        not the following week's.

    ``fill + 1`` on its own proves nothing and is not accepted. Ordinary
    holidays never reach here: they produce one fill date, not two
    obligations sharing a decision session.
    """
    old_d, new_d = _as_date(old_fill), _as_date(new_fill)
    if old_d is None or new_d is None:
        return False, REVISION_UNREADABLE_DATES, "unreadable fill dates"
    lo = min(old_d, new_d) - timedelta(days=20)
    hi = max(old_d, new_d) + timedelta(days=5)
    sessions = venue_sessions(venue, lo, hi)
    if sessions is None:
        return (False, REVISION_CALENDAR_UNREADABLE,
                f"the {venue} calendar could not be read")
    if old_fill in sessions:
        return (False, REVISION_OLD_FILL_STILL_TRADES,
                f"{old_fill} is still a {venue} session, so the fill was not "
                f"moved off it")
    if new_fill not in sessions:
        return (False, REVISION_NEW_FILL_NOT_A_SESSION,
                f"{new_fill} is not a {venue} session")
    if decision_session:
        prior = sorted(d for d in sessions if d < new_fill)
        if not prior or prior[-1] != decision_session:
            return (False, REVISION_WRONG_DECISION_SESSION,
                    f"{new_fill} ranks on "
                    f"{prior[-1] if prior else 'nothing'}, not on the recorded "
                    f"decision session {decision_session}")
    return (True, REVISION_CONFIRMED,
            f"{old_fill} is no longer a {venue} session and {new_fill} ranks "
            f"on the same decision session {decision_session or '(unrecorded)'}")


def frozen_venues(targets: dict | None, asof: date) -> list[dict]:
    """Which VENUES have stopped advancing in ``live_targets.json``.

    ADDED 2026-09-17 (sixth review), PER VENUE 2026-09-17 (seventh review).
    The HOLD authorisation compares the release's anchor with the book's,
    which proves the two files agree - and two files that have both stopped
    moving agree perfectly. The freshness bound in ``hold_is_authorised``
    refuses to grant a NEW exemption from an old release; this says the
    broader thing, which is that a frozen book cannot tell us which fills
    have happened since.

    THE FIRST VERSION TOOK THE MAXIMUM ACROSS VENUES, which is exactly the
    wrong direction: the venues are independent, and a NYSE fill that keeps
    advancing hid an XETR fill that had stopped a month earlier. That is
    the case the repair exists for - sleeve D is the venue that holds, its
    exemption is retained for its original fill, and with NYSE still moving
    nothing reported UNKNOWN and no later XETR obligation was ever
    observed. Each venue is now aged on its own.

    Returns one record per stale venue: ``{"venue", "fill", "age_days"}``.
    """
    if not isinstance(targets, dict):
        return []
    nxt = targets.get("next_fill")
    if not isinstance(nxt, dict):
        return []
    by_venue = nxt.get("by_venue")
    if isinstance(by_venue, dict) and by_venue:
        pairs = [(str(v), f) for v, f in by_venue.items()]
    elif nxt.get("date"):
        pairs = [("ALL", nxt["date"])]
    else:
        return []
    out = []
    for venue, raw in sorted(pairs):
        fill = _as_date(raw)
        if fill is None:
            continue
        age = (asof - fill).days
        if age > BOOK_STALE_DAYS:
            out.append({"venue": venue, "fill": fill.isoformat(),
                        "age_days": age})
    return out


def book_looks_frozen(targets: dict | None, asof: date) -> tuple[bool, str]:
    """(frozen, why) over every venue. See ``frozen_venues``."""
    stale = frozen_venues(targets, asof)
    if not stale:
        return False, ""
    detail = "; ".join(f"{s['venue']} still names {s['fill']}, "
                       f"{s['age_days']} days before {asof.isoformat()}"
                       for s in stale)
    return True, (f"live_targets.json has not advanced for "
                  f"{', '.join(s['venue'] for s in stale)}: {detail}. Which "
                  f"fills have occurred since cannot be read from a book that "
                  f"stopped moving")


def release_authorisation(repo_root: Path, now: datetime | None = None) -> dict:
    """The release, AS VERIFIED BY ITS OWN CONTRACT.

    THE MARKER IS NOT THE EVIDENCE (2026-09-16, fourth review). The previous
    version read ``data/component_release.json`` and accepted it on two
    fields, ``anchor`` and ``d_ready``. Two lines of hand-written JSON
    therefore exempted sleeve D from its publication obligation - a forged
    exemption, and the file sits in the working tree where anything can
    write it.

    ``component_release.verify`` is the contract, and it is a great deal
    more than two fields: an identity hash over the sealed body, the seal
    read back from the COMMIT that last changed it, a digest of every
    declared source, the sealed book and held basis re-checked against the
    live files, the book re-validated for the current week, the scoped
    guard receipt, the price evidence and the performance labels. Running
    it is the only way to know a release is real, so it is run.

    Returns ``{"verified": bool, ...}``. Anything short of a clean verify -
    a missing file, an uncommitted seal, a changed source, a book from
    another week - is ``verified: False`` with the reason, and the sleeve
    stays OBLIGED. Never raises.

    VERIFIED AT THE MOMENT THE SEAL CLAIMS (2026-09-16, fifth review).
    ``verify`` validates the book against the clock it is handed: the last
    completed session, the venue's next fill and the week anchor are all
    read from ``now``. Handing it the CURRENT time therefore refused every
    release as soon as one more session completed - measured on this
    repository, the live release verified through Monday 14 September and
    was refused from Tuesday the 15th with "book predates the required
    close", which is exactly when the post-fill pair runs. Sleeve D would
    have acquired a false debt on every post-fill firing, undischargeable
    by any publication.

    The release is therefore re-verified at its own ``sealed_at``. That is
    not a way of trusting the file: ``sealed_at`` is inside the body the
    identity hash covers, so a forged or edited timestamp fails the very
    check it was trying to pass, and the committed-seal comparison still
    reads the payload out of the commit that last changed it. A seal
    claiming the future is refused outright.
    """
    out: dict = {"verified": False, "error": ""}
    real_now = now or datetime.now(timezone.utc)
    try:
        import component_release
        root = Path(repo_root)
        raw = json.loads((root / "data" /
                          "component_release.json").read_text(encoding="utf-8"))
        sealed = datetime.fromisoformat(str(raw.get("sealed_at")))
        if sealed.tzinfo is None:
            sealed = sealed.replace(tzinfo=timezone.utc)
        if sealed > real_now + timedelta(minutes=5):
            out["error"] = (f"the release claims to have been sealed at "
                            f"{sealed.isoformat()}, which is in the future")
            return out
        payload = component_release.verify(root, sealed, committed=True)
        out.update(verified=True, anchor=payload.get("anchor"),
                   d_ready=payload.get("d_ready"),
                   identity=payload.get("identity"),
                   verified_at=sealed.isoformat())
    except Exception as exc:  # noqa: BLE001 - an unverifiable release is a no
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def hold_is_authorised(sleeve: dict, book_anchor, release: dict | None,
                       asof: date | None = None) -> tuple[bool, str]:
    """(authorised, why-not) for one sleeve's HOLD.

    Weaker than ``component_release.verify`` by construction - this module
    does not re-run the release - but it refuses everything that contract
    refuses: a HOLD on any sleeve but D, a HOLD with no reason, and a HOLD
    with no sealed release for THIS anchor saying D was not ready. An
    unauthorised HOLD leaves the sleeve OBLIGED, which is the conservative
    direction: an incomplete or invalid book is not an exemption.
    """
    name = str(sleeve.get("sleeve") or "")
    if name != "D":
        return False, f"sleeve {name} may not HOLD under the release contract"
    if not str(sleeve.get("reason") or "").strip():
        return False, "the HOLD carries no reason"
    if not isinstance(release, dict):
        return False, "no component release to authorise it"
    if release.get("verified") is not True:
        return False, (f"the component release does not verify against its "
                       f"own contract ({release.get('error') or 'unverified'})")
    if release.get("d_ready") is not False:
        return False, "the verified release does not record D as not ready"
    if book_anchor and release.get("anchor") != book_anchor:
        return False, (f"the verified release is for anchor "
                       f"{release.get('anchor')!r}, not {book_anchor!r}")
    # AGREEMENT IS NOT FRESHNESS (2026-09-17, sixth review). The anchor
    # comparison above proves only that the release and the book name the
    # same week - and if BOTH are stale, they agree perfectly. A release
    # sealed on 13 September exempted sleeve D six months later, because
    # live_targets.json had frozen on the same anchor and nothing in the
    # comparison could tell a current pair from a pair that had stopped
    # moving. An exemption is granted for ONE decision week; past that it
    # has to be granted again.
    anchor_date = _as_date(release.get("anchor"))
    if asof is not None:
        if anchor_date is None:
            return False, (f"the verified release carries no readable anchor "
                           f"({release.get('anchor')!r}), so its age cannot "
                           f"be established")
        age = (asof - anchor_date).days
        if age > HOLD_AUTHORISATION_MAX_AGE_DAYS:
            return False, (f"the verified release is for the week of "
                           f"{anchor_date.isoformat()}, {age} days before "
                           f"{asof.isoformat()}: an exemption older than "
                           f"{HOLD_AUTHORISATION_MAX_AGE_DAYS} days is a "
                           f"frozen book, not a current decision")
    return True, ""


def ready_is_final(sleeve: dict, book_final: bool) -> bool:
    """Whether a READY observation is evidence of a real obligation.

    A midweek book is PROVISIONAL: it is ranked on a session that is not
    the one the fill will be decided on, and every later session re-ranks
    it. Treating such a READY as binding made a sleeve permanently obliged
    for a fill whose final release might legitimately hold it - which is
    why ``_targets_final`` exists in live_targets.py in the first place.
    Either the book declares itself final, or this sleeve was ranked on the
    very session its fill is decided by.
    """
    if str(sleeve.get("status") or "").upper() != "READY":
        return False
    if book_final:
        return True
    ds, dsf = sleeve.get("decision_session"), sleeve.get("decision_session_for_fill")
    return bool(ds and dsf and ds == dsf)


def observe_obligations(targets: dict | None, release=None,
                        asof: date | None = None
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
    # LAZY. Verifying the release is a real piece of work - an identity
    # hash, a git read of the sealed commit, a digest of every source - and
    # it is needed only if a sleeve actually claims a HOLD. ``release`` may
    # be a dict or a zero-argument callable; a callable is invoked at most
    # once, on the first HOLD seen.
    _release_cache: list = []

    def _release():
        if not _release_cache:
            _release_cache.append(release() if callable(release) else release)
        return _release_cache[0]

    book_final = targets.get("targets_final") is True
    anchor = targets.get("as_of")

    # sleeve -> venue, from the book itself rather than a hardcoded map, so
    # a sleeve that changes venue moves its obligation with it. ``status``
    # is READY or HOLD; a sleeve carrying neither is treated as obliged,
    # which is the conservative direction.
    by_sleeve: dict[str, str] = {}
    held: set[str] = set()
    final_ready: set[str] = set()
    unstated: set[str] = set()
    sleeves = targets.get("sleeves")
    if isinstance(sleeves, list):
        for sl in sleeves:
            if isinstance(sl, dict) and sl.get("sleeve") and sl.get("venue"):
                name = str(sl["sleeve"])
                by_sleeve[name] = str(sl["venue"])
                status = str(sl.get("status") or "").upper()
                if status == "HOLD":
                    ok, why = hold_is_authorised(sl, anchor, _release(), asof)
                    if ok:
                        held.add(name)
                    else:
                        notes.append(f"sleeve {name} is on HOLD but that hold "
                                     f"is NOT authorised ({why}); the sleeve "
                                     f"stays obliged")
                elif status == "READY":
                    if ready_is_final(sl, book_final):
                        final_ready.add(name)
                    else:
                        notes.append(f"sleeve {name} is READY on a provisional "
                                     f"book; the observation is recorded but "
                                     f"does not bind the obligation")
                else:
                    unstated.add(name)
    if unstated:
        notes.append(f"sleeve(s) {','.join(sorted(unstated))} carry no "
                     f"READY/HOLD status; treated as obliged")
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
            out.append(Obligation(
                str(venue), fd.isoformat(), venue_sleeves,
                _iso_or_none(decisions.get(venue)),
                tuple(sorted(s for s in venue_sleeves if s in held)),
                tuple(sorted(s for s in venue_sleeves if s in final_ready))))
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
                        _iso_or_none(nxt.get("decision_session")),
                        tuple(sorted(held)), tuple(sorted(final_ready)))],
            blocking, notes)


def _iso_or_none(raw) -> str | None:
    d = _as_date(raw)
    return d.isoformat() if d else None


def classify(ob: Obligation, published: dict[str, date | None], asof: date,
             *, history: dict | None = None, observed_owed: bool = False
             ) -> tuple[str, str, bool]:
    """(state, reason, missed) for one obligation against the published records.

    ``published`` maps sleeve -> the published ``latest_rebalance`` date at
    the remote ref, or None when that sleeve's record could not be read. An
    unreadable record is UNKNOWN: it is the state the incident was actually
    in, and it must not be spelled "discharged".

    ``history`` maps sleeve -> {"dates": set of ISO dates that record has
    ever CARRIED on the remote ref since the fill, "conclusive": bool}. It
    is the difference between evidence of a miss and absence of an
    observation, and it exists because ``latest_rebalance`` names only the
    LAST rebalance. Two benign paths produced a false "missed publication"
    without it (both reproduced 2026-09-16):

      * an engine rerun through 22 September reconstructs 8, 14 and 21
        September and publishes a record naming the 21st. The 14th WAS
        recorded, at the time, and the history shows it.
      * the required publication succeeded while nothing was observing, and
        a later publication is now the latest.

    ``missed`` is therefore asserted only when the history walk is
    conclusive AND this obligation was seen OWED after its own fill date —
    that is, we were watching, we saw it unpublished, and no publication in
    the covered window ever carried it. Anything weaker is SUPERSEDED with
    ``missed`` false: the book moved past, and we cannot say whether it was
    recorded on the way.
    """
    fill = _as_date(ob.fill_date)
    if fill is None:                        # pragma: no cover - constructor pins
        return UNKNOWN, f"fill date {ob.fill_date!r} is not a calendar date", False
    if not ob.sleeves:
        return UNKNOWN, (f"no sleeve is known to trade on {ob.venue}; the "
                         f"obligation cannot be checked"), False
    obliged = ob.obliged
    if not obliged:
        return NOT_OBLIGED, (
            f"every sleeve on {ob.venue} ({','.join(ob.sleeves)}) was on an "
            f"authorised HOLD for fill {ob.fill_date}; a sanctioned HOLD is "
            f"not an unpublished fill"), False
    missing = [s for s in obliged if published.get(s) is None]
    if missing:
        return UNKNOWN, (f"published rebalance record unreadable for sleeve(s) "
                         f"{','.join(missing)}"), False
    held_note = (f" (sleeve(s) {','.join(ob.on_hold)} on an authorised HOLD, "
                 f"not obliged)" if ob.on_hold else "")
    dates = {s: published[s] for s in obliged}
    behind = {s: d for s, d in dates.items() if d < fill}
    if behind:
        detail = ", ".join(f"{s} at {d.isoformat()}"
                           for s, d in sorted(behind.items()))
        if asof < fill:
            return PENDING, (f"fill {ob.fill_date} ({ob.venue}) is in the "
                             f"future as at {asof.isoformat()}"), False
        age = (asof - fill).days
        return OWED, (f"fill {ob.fill_date} ({ob.venue}) unrecorded for "
                      f"{age} day(s); published: {detail}{held_note}"), False
    if any(d == fill for d in dates.values()):
        return DISCHARGED, (
            f"fill {ob.fill_date} ({ob.venue}) recorded by sleeve(s) "
            f"{','.join(s for s, d in sorted(dates.items()) if d == fill)}"
            f"{held_note}"), False

    # Every obliged sleeve is PAST the fill and none is at it. The current
    # record cannot distinguish the three ways that happens; the history can.
    ahead = ", ".join(f"{s} at {d.isoformat()}" for s, d in sorted(dates.items()))
    hist = history or {}
    carried = {s: (ob.fill_date in set((hist.get(s) or {}).get("dates") or ()))
               for s in obliged}
    if carried and all(carried.values()):
        return DISCHARGED, (
            f"fill {ob.fill_date} ({ob.venue}) was carried by a publication on "
            f"the remote ref at the time; the book has since moved on "
            f"({ahead}){held_note}"), False
    conclusive = all((hist.get(s) or {}).get("conclusive") for s in obliged)
    if conclusive and observed_owed:
        return SUPERSEDED, (
            f"fill {ob.fill_date} ({ob.venue}) was observed unpublished after "
            f"its own fill date, and no publication in the covered history "
            f"carried it; the book has moved past it ({ahead}){held_note}"), True
    why = ("the publication history could not be read back far enough"
           if not conclusive else
           "this obligation was never observed unpublished after its fill")
    return SUPERSEDED, (
        f"fill {ob.fill_date} ({ob.venue}) is not the published record and no "
        f"publication carrying it was found, but {why}, so a missed "
        f"publication is NOT established ({ahead}){held_note}"), False


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


def is_shallow(repo_root: Path) -> bool:
    cp = _git(repo_root, ["rev-parse", "--is-shallow-repository"])
    return bool(cp and cp.returncode == 0
                and cp.stdout.strip().lower() == "true")


def is_ancestor(repo_root: Path, older: str, newer: str) -> bool:
    cp = _git(repo_root, ["merge-base", "--is-ancestor", older, newer])
    return bool(cp and cp.returncode == 0)


def _commits_since(repo_root: Path, sha: str | None, ref: str) -> int:
    """How many commits ``ref`` has gained since ``sha``. 0 when unknown."""
    if not sha:
        return 0
    cp = _git(repo_root, ["rev-list", "--count", f"{sha}..{ref}"])
    if cp is None or cp.returncode != 0:
        return 0
    try:
        return int(cp.stdout.strip())
    except ValueError:                      # pragma: no cover
        return 0


def ref_was_rewritten(repo_root: Path, ref: str, *,
                      limit: int = 50) -> tuple[bool, str]:
    """(rewritten, detail) for a non-fast-forward move of ``ref``.

    Every sha this clone has previously seen ``ref`` point at is in the
    ref's reflog. If any of them is NOT an ancestor of where ``ref`` points
    now, the ref moved non-fast-forward at some point: a force-push or a
    rebase, which may have dropped a publication that carried the fill.

TWO LIMITS, BOTH STATED (the second added 2026-09-16, fourth review).

    First, this can only see what this clone FETCHED. A rewrite that removed
    commits between two of our own fetches - commits we never held - leaves
    no trace here or anywhere else in the object store.

    Second, REFLOGS EXPIRE. This repository sets no ``gc.reflogExpire``, so
    git's 90-day default applies and entries older than that are pruned:
    evidence of a rewrite this clone DID fetch is not permanent. The walk is
    also capped at ``limit`` entries.

    That is why a rewrite makes the verdict inconclusive rather than
    adjusting it: where the evidence is gone, the honest answer is that it
    is gone.
    """
    cp = _git(repo_root, ["reflog", "show", f"--format=%H", f"-{int(limit)}",
                          ref])
    if cp is None or cp.returncode != 0:
        return False, ""                  # no reflog is not evidence of a rewrite
    for sha in dict.fromkeys(s for s in cp.stdout.split() if s):
        if not is_ancestor(repo_root, sha, ref):
            return True, (f"{ref} previously pointed at {sha[:8]}, which is no "
                          f"longer an ancestor: the ref was rewritten")
    return False, ""


def _read_blobs(repo_root: Path, specs: list[str]) -> dict[str, str | None]:
    """``{spec: contents}`` for many blobs in ONE git process.

    ``git cat-file --batch`` instead of one ``git show`` per commit: the
    walk is 27-32 commits per sleeve on a real window, and four sleeves of
    per-commit process spawns is where the seconds went.
    """
    out: dict[str, str | None] = {s: None for s in specs}
    if not specs:
        return out
    try:
        cp = subprocess.run(["git", "cat-file", "--batch"], cwd=str(repo_root),
                            input="\n".join(specs) + "\n",
                            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return out
    if cp.returncode != 0:
        return out
    # Each answer is "<sha> <type> <size>\n<payload>\n", or
    # "<spec> missing\n". Walked in request order.
    data, pos = cp.stdout, 0
    for spec in specs:
        nl = data.find("\n", pos)
        if nl < 0:
            break
        header = data[pos:nl]
        pos = nl + 1
        parts = header.split()
        if len(parts) < 3 or parts[1] not in ("blob", "commit", "tag", "tree"):
            continue                      # "missing" / "ambiguous": leave None
        try:
            size = int(parts[2])
        except ValueError:
            continue
        out[spec] = data[pos:pos + size]
        pos += size + 1
    return out


def publication_history(repo_root: Path, ref: str, sleeve: str,
                        since: date | None = None, *,
                        since_sha: str | None = None,
                        confirm_absent: str | None = None,
                        limit: int = HISTORY_LIMIT) -> dict:
    """Every rebalance date that ``sleeve``'s published record has CARRIED
    on ``ref``, over a range whose COMPLETENESS can be established.

    Returns {"dates", "conclusive", "commits", "unreadable", "note",
    "anchor", "shallow", "rewritten"}.

    WHAT MAKES A NEGATIVE CONCLUSIVE (rewritten 2026-09-16 after a third
    review). The previous version walked ``--since=<fill - 1 day>`` and
    declared the result conclusive whenever it neither hit the limit nor
    errored. Four ways that asserted a MISS from evidence it did not have,
    each reproduced:

      * a publication that DID carry the fill, committed with a timestamp
        before the boundary - clock skew, or a rebase rewriting committer
        dates - is simply outside the walk;
      * a SHALLOW clone answers with the commits it happens to hold;
      * every ``git show`` failing produced an empty date set and a
        confident verdict;
      * a force-push that dropped the carrying publication was invisible.

    Conclusiveness is now POSITIVELY established, and all four of those
    return ``conclusive: false``:

      1. the range is an ANCESTRY range ``since_sha..ref``, not a date
         range, so commit timestamps cannot exclude anything. ``since_sha``
         is the remote tip recorded when this obligation was first seen
         OWED - every publication that could have carried the fill is after
         it, because at that moment the record was behind the fill;
      2. ``since_sha`` must still be an ancestor of ``ref``. A force-push or
         rebase that removed it is detected here and reported as
         ``rewritten``;
      3. the repository must not be shallow;
      4. every blob in the range must have been read and parsed; and
      5. the range must not exceed ``limit``.

    Without ``since_sha`` the walk falls back to a date bound and is NEVER
    conclusive: a date-bounded walk can confirm a publication, but it
    cannot establish that none exists.

    Each commit's OWN blob is read, which is what was published at the
    time. The current blob is a recomputation and cannot answer whether an
    earlier fill was recorded when it was due.
    """
    name = SLEEVE_FILES.get(sleeve)
    out = {"dates": set(), "conclusive": False, "commits": 0, "unreadable": 0,
           "note": "", "anchor": since_sha, "shallow": False,
           "rewritten": False}
    if not name:
        out["note"] = f"sleeve {sleeve} has no published record file"
        return out

    reasons: list[str] = []
    if is_shallow(repo_root):
        out["shallow"] = True
        reasons.append("the repository is shallow, so earlier publications "
                       "are not present")
    if since_sha:
        if not is_ancestor(repo_root, since_sha, ref):
            out["rewritten"] = True
            reasons.append(f"the recorded anchor {since_sha[:8]} is no longer "
                           f"an ancestor of {ref}: history was rewritten")
        rev = f"{since_sha}..{ref}"
    else:
        reasons.append("no anchor commit was recorded for this obligation, so "
                       "a date-bounded walk cannot establish a negative")
        rev = ref

    args = ["log", rev, f"-{int(limit) + 1}", "--format=%H"]
    if not since_sha:
        # One day of slack: a publication made in the hours after the fill
        # can carry a commit date on the fill day itself.
        floor = ((since or date(1970, 1, 1)) - timedelta(days=1)).isoformat()
        args.append(f"--since={floor}")
    args += ["--", f"data/{name}"]
    cp = _git(repo_root, args)
    if cp is None or cp.returncode != 0:
        reasons.append(f"could not read the history of data/{name} on {rev}")
        out["note"] = "; ".join(reasons)
        return out
    shas = [s for s in cp.stdout.split() if s]
    out["commits"] = len(shas)
    if len(shas) > limit:
        reasons.append(f"more than {limit} publications touched data/{name}; "
                       f"the walk was truncated")
        shas = shas[:limit]

    blobs = _read_blobs(repo_root, [f"{s}:data/{name}" for s in shas])
    for spec, body in blobs.items():
        if body is None:
            out["unreadable"] += 1
            continue
        try:
            doc = json.loads(body)
        except ValueError:
            out["unreadable"] += 1
            continue
        rec = ((doc.get("headline") or {}) if isinstance(doc, dict) else {})
        rec = rec.get("latest_rebalance") if isinstance(rec, dict) else None
        d = _as_date((rec or {}).get("date")) if isinstance(rec, dict) else None
        if d is not None:
            out["dates"].add(d.isoformat())
    if out["unreadable"]:
        reasons.append(f"{out['unreadable']} publication blob(s) could not be "
                       f"read or parsed")

    # THE REWRITE CHECK IS LAST, AND CONDITIONAL (2026-09-16, fourth review).
    # The anchor surviving is not enough - a force-push can drop a LATER
    # publication and leave the anchor in place - but the check walks the
    # reflog with a merge-base per entry, measured at 0.95s here and 1.49s on
    # the reviewer's machine. It matters only when we are about to assert
    # that ``confirm_absent`` is NOT in the history: if the date was found,
    # the positive evidence stands whatever else the ref has done.
    if (since_sha and not out["rewritten"] and confirm_absent
            and confirm_absent not in out["dates"]):
        rewritten, detail = ref_was_rewritten(repo_root, ref)
        if rewritten:
            out["rewritten"] = True
            reasons.append(detail)
    out["note"] = "; ".join(reasons)
    out["conclusive"] = not reasons
    return out


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

    The notice half is read the same way, with one difference: its problems
    come back on ``book["notice_problems"]`` rather than in the returned
    list. A dedupe record for the ALERT channel says nothing about whether a
    publication is owed, so it must not turn the publication verdict UNKNOWN
    and put every hourly firing through a four-hour refresh. It is reported,
    not escalated.
    """
    empty = {"schema": LEDGER_SCHEMA, "obligations": {}, "notices": {},
             "notices_observed_on": "", "notices_unreadable": {},
             "notice_problems": []}
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
    raw_notices = doc.get("notices") if isinstance(doc.get("notices"), dict) else {}
    notices = {k: v for k, v in raw_notices.items() if isinstance(v, dict)}
    # NAMED AND RETAINED, NEVER SILENTLY RESET (2026-09-17, eighth review).
    # A notice record that cannot be read is a SEND HISTORY that cannot be
    # read. Dropping it quietly - which is what happened before - hands the
    # key a fresh budget of MAX_NOTICES, so a malformed record was a way to
    # re-arm the mailer. The evidence is carried in a map of its own instead:
    # ``notices_due`` suppresses the key and every run says so.
    #
    # AND IT MUST BE CLEARABLE (2026-09-17, ninth review). The eighth pass
    # unioned the persisted keys back in unconditionally and persisted them
    # again on every save, so the suppression could not be lifted by any
    # action at all - not by repairing the record, not by deleting it. The
    # handoff and the comment here both said it cleared when a person removed
    # the bad record. It did not. Two explicit repairs clear it now, and only
    # an explicit repair does:
    #
    #   * write a WELL-FORMED record for the key - replacing garbage with a
    #     real record is unambiguous, and the new record's own counts govern
    #     from then on;
    #   * call ``clear_notice_suppression`` (``--clear-notice KEY`` on the
    #     CLI), which is the documented route when the record was deleted
    #     outright rather than replaced.
    #
    # Absence alone does NOT clear it, because ``save_ledger`` writes the
    # cleaned map and the malformed value is absent from the very next read.
    bad = _unreadable_notices(doc, raw_notices, notices)
    notice_problems: list[str] = []
    if bad:
        notice_problems.append(
            f"{len(bad)} notice record(s) in {Path(path).name} cannot be "
            f"read ({', '.join(sorted(bad))}); how often each has already "
            f"alerted is unknown, so they are suppressed rather than "
            f"restarted. Repair one by writing a well-formed record for it, "
            f"or clear it with --clear-notice KEY")
    if len(notices) > MAX_NOTICE_KEYS:
        notice_problems.append(
            f"{len(notices)} notice record(s) exceed the retention bound of "
            f"{MAX_NOTICE_KEYS}; none was evicted because each still "
            f"describes a condition seen recently, and a spent record is what "
            f"keeps its condition quiet")
    if len(bad) >= MAX_UNREADABLE_NOTICES:
        notice_problems.append(
            f"the unreadable-notice evidence is at its bound of "
            f"{MAX_UNREADABLE_NOTICES}; the oldest entries are being dropped "
            f"and the keys they suppressed can alert again")
    return ({"schema": LEDGER_SCHEMA, "obligations": clean,
             "notices": notices,
             "notices_observed_on": str(doc.get("notices_observed_on") or ""),
             "notices_unreadable": bad,
             "notice_problems": notice_problems}, problems)


def _unreadable_notices(doc: dict, raw_notices: dict, notices: dict) -> dict:
    """The retained evidence for notice records that could not be read.

    Returns {key: {"evidence", "first_seen"}}, bounded to
    MAX_UNREADABLE_NOTICES with the oldest ``first_seen`` dropped first. The
    evidence is truncated: it exists so a person can recognise what they are
    being asked to repair, not to reconstruct the value.
    """
    out: dict[str, dict] = {}
    carried = doc.get("notices_unreadable")
    if isinstance(carried, dict):
        for key, rec in carried.items():
            if not isinstance(rec, dict):
                continue
            # A well-formed record for this key IS the repair.
            if isinstance(raw_notices.get(str(key)), dict):
                continue
            out[str(key)] = {
                "evidence": str(rec.get("evidence") or "")[:NOTICE_EVIDENCE_CHARS],
                "first_seen": str(rec.get("first_seen") or "")}
    elif isinstance(carried, list):
        # The eighth-pass shape, carried forward so an existing ledger does
        # not read as repaired the moment this version first runs.
        for key in carried:
            if isinstance(key, (str, int)) and not isinstance(
                    raw_notices.get(str(key)), dict):
                out[str(key)] = {"evidence": "", "first_seen": ""}
    for key in set(raw_notices) - set(notices):
        out.setdefault(str(key), {})
        out[str(key)].setdefault("first_seen", "")
        out[str(key)]["evidence"] = repr(raw_notices[key])[:NOTICE_EVIDENCE_CHARS]
    if len(out) > MAX_UNREADABLE_NOTICES:
        keep = sorted(out, key=lambda k: (str(out[k].get("first_seen") or ""), k),
                      reverse=True)[:MAX_UNREADABLE_NOTICES]
        out = {k: out[k] for k in keep}
    return out


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


def history_looks_lost(repo_root: Path, book: dict) -> tuple[bool, str]:
    """Has this clone run before while holding no obligation history?

    THE CONTINUITY SIDECAR IS NOT ENOUGH ON ITS OWN (2026-09-16, fourth
    review). It is written by the same ``_atomic_write``, into the same
    directory, as the ledger: a disk that is full, a directory that is not
    writable, a scanner holding a lock on the folder takes BOTH. Once
    writes recovered, the ledger was empty, the sidecar was empty, and the
    verdict read perfectly clean with a publication still behind.

    The signal that survives that is written by DIFFERENT code, to
    DIFFERENT files: the run ledger, the green-run marker and the alert
    record. If any of those exists, this clone has run before - and a run
    that has happened before, holding no obligations at all, has lost its
    history rather than never having had one.

    THE LIMIT, STATED. If nothing at all can be written and no other
    artefact exists - a first run on a broken disk - the next process has
    no memory of anything, and no check here can give it one.
    """
    if (book.get("obligations") or {}):
        return False, ""
    logs = Path(repo_root) / "logs"

    def _publishing_run_ledger(p: Path) -> bool:
        """At least one PARSEABLE record of a run that could publish."""
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (isinstance(rec, dict) and rec.get("cadence") in CADENCE_NAMES
                    and rec.get("asof")):
                return True
        return False

    def _publishing_marker(p: Path) -> bool:
        """A green marker a PUBLISHING run wrote. The component markers are
        excluded: a Europe collection firing writes one and publishes
        nothing, so its presence says only that data was collected."""
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (isinstance(rec, dict) and rec.get("cadence") in CADENCE_NAMES
                and bool(rec.get("local_date")))

    checks = ((logs / "run_outcomes.jsonl", _publishing_run_ledger),
              (logs / "last_green_run.json", _publishing_marker))
    for path, is_evidence in checks:
        if path.exists() and is_evidence(path):
            return True, (f"the obligation ledger holds nothing, but logs/"
                          f"{path.name} records a completed publishing run: "
                          f"the obligation history was lost, not never "
                          f"written")
    return False, ""


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
           "obligations": obs,
           "notices": _prune_notices(ledger.get("notices") or {},
                                     ledger.get("notices_observed_on")),
           "notices_observed_on": str(ledger.get("notices_observed_on") or ""),
           "notices_unreadable": _stamp_unreadable(
               ledger.get("notices_unreadable") or {})}
    if not _atomic_write(path, json.dumps(doc, indent=2, sort_keys=True)):
        return False
    # VERIFIED BY READ-BACK, ON CONTENT (2026-09-16, fifth review). A writer
    # that returns True has reported on its own behaviour, so the file is
    # read again - but comparing KEY SETS alone passed a stale file that
    # happened to carry the same keys. A partial write, or a write that
    # silently did nothing over an older ledger, leaves every key in place
    # and every VALUE wrong: the states, the anchors, the observed-owed
    # flags. Those are what the next run reasons from, so those are what is
    # compared.
    #
    # EVERY SEMANTIC MAP, NOT ONLY THE OBLIGATIONS (2026-09-17, eighth
    # review). The fifth review's fix compared the obligations; the seventh
    # pass then added a second map to the same file, carrying the delivered
    # count and last-alerted date that bound the mailer, and the read-back
    # did not look at it. A write that silently did nothing over an older
    # ledger returned True with the alert budget unchanged. Only
    # ``updated_utc`` is excluded, and only because it is a stamp rather than
    # something the next run reasons from.
    back, _ = load_ledger(path)
    written = {k: v for k, v in doc.items() if k != "updated_utc"}
    read = {k: back.get(k) for k in written}
    return json.dumps(read, sort_keys=True) == json.dumps(written, sort_keys=True)


def _stamp_unreadable(evidence) -> dict:
    """Normalise the unreadable-notice evidence and date what is new."""
    today = datetime.now(timezone.utc).date().isoformat()
    out: dict[str, dict] = {}
    if isinstance(evidence, dict):
        items = evidence.items()
    else:                               # the eighth-pass list shape
        items = ((k, {}) for k in (evidence or []))
    for key, rec in items:
        rec = rec if isinstance(rec, dict) else {}
        out[str(key)] = {
            "evidence": str(rec.get("evidence") or "")[:NOTICE_EVIDENCE_CHARS],
            "first_seen": str(rec.get("first_seen") or "") or today}
    return out


def _prune_notices(notices: dict, observed_on=None) -> dict:
    """Bound the notices map, anchored on an OBSERVATION this run trusts.

    EVICTION TURNS ON THE CONDITION, NOT ON THE BUDGET (2026-09-17, ninth
    review). The eighth pass evicted EXHAUSTED records first, reasoning that
    a record which can no longer alert is the safe one to drop. It is the
    opposite: the spent count IS what keeps the condition quiet, so dropping
    it hands the same still-frozen venue a fresh budget of MAX_NOTICES on the
    next firing. A spent record is a tombstone and it has to outlive the
    condition it silenced.

    So what may be dropped is a record whose condition has STOPPED being
    reported. Every firing stamps ``last_seen`` on every live notice through
    ``record_notice_conditions``, so a record that has fallen
    NOTICE_RETENTION_DAYS behind the anchor describes something that
    resolved. Nothing else is evictable: if every record is recent the map
    stays oversized and ``load_ledger`` reports it, because a silent eviction
    is the defect this whole module exists to remove.

    THE ANCHOR IS AN OBSERVATION, NOT THE CONTENTS OF THE FILE (2026-09-17,
    tenth review). The ninth pass took the MAXIMUM ``last_seen`` across the
    map, so that a ledger nobody had written for a year would not empty
    itself when it was opened. That made the floor a function of untrusted
    data: one dict-shaped record carrying ``last_seen: "2099-01-01"`` - which
    passes every validity check this module has - moved the floor to 2098 and
    made every genuine record look resolved. Measured: 57 records, 56 of them
    stamped today, one future-dated, and 17 live tombstones were evicted,
    each of which then re-armed with a fresh budget. That is the very defect
    the ninth pass was repairing, re-entered through a different door.

    The anchor is now ``observed_on``: the date of a run that successfully
    recorded its observation of the live conditions, written by
    ``record_notice_conditions`` and carried on the ledger. WITHOUT ONE,
    NOTHING IS PRUNED - a stale ledger is not pruned merely because it was
    opened, which is the case the relative floor existed to protect, and it
    is protected here without trusting the records themselves.
    """
    out = {k: v for k, v in notices.items() if isinstance(v, dict)}
    if len(out) <= MAX_NOTICE_KEYS:
        return out
    anchor = _as_date(observed_on)
    if anchor is None:
        return out          # no trusted observation: evict nothing
    floor = anchor - timedelta(days=NOTICE_RETENTION_DAYS)

    def _resolved(rec: dict) -> bool:
        seen = _as_date(rec.get("last_seen"))
        # A record dated AFTER the anchor is not evidence that it resolved;
        # it is evidence that its date cannot be trusted. Either way it is
        # not evictable.
        return seen is not None and seen < floor and seen <= anchor

    stale = sorted((k for k, v in out.items() if _resolved(v)),
                   key=lambda k: (str(out[k].get("last_seen") or ""), k))
    for k in stale[:len(out) - MAX_NOTICE_KEYS]:
        out.pop(k, None)
    return out


def continuity_path(ledger: Path) -> Path:
    """Beside the ledger, and written on EVERY save attempt."""
    p = Path(ledger)
    return p.with_name(p.stem + ".continuity.json")


def record_write_outcome(ledger: Path, ok: bool,
                         now_utc: datetime | None = None) -> dict:
    """Record that a ledger save succeeded or failed, durably.

    WHY A SECOND FILE (2026-09-16, third review). The previous fix detected
    a write that was failing RIGHT NOW, and nothing else. Once writes
    recovered, the ledger was a fresh file with no trace of the obligations
    lost while it could not be written: the verdict went back to
    ``owed:false, unknown:false, problems:[]`` with a publication still
    behind. The loss has to be recorded somewhere the loss itself does not
    erase, and a gap has to outlive the failure that opened it.

    An open gap is closed by the next successful save, but the RECORD of it
    remains. What cannot be repaired is repaired nowhere: an obligation that
    was never written is gone, and the gap says so rather than pretending
    the history is clean.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    stamp = now_utc.isoformat(timespec="seconds")
    p = continuity_path(ledger)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        state = doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        state = {}
    gaps = [g for g in (state.get("gaps") or []) if isinstance(g, dict)][-20:]
    state["writes"] = int(state.get("writes") or 0) + (1 if ok else 0)
    state["failures"] = int(state.get("failures") or 0) + (0 if ok else 1)
    if ok:
        state["last_ok_utc"] = stamp
        for g in gaps:
            if not g.get("recovered_utc"):
                g["recovered_utc"] = stamp
    else:
        state["last_fail_utc"] = stamp
        if not any(not g.get("recovered_utc") for g in gaps):
            gaps.append({"failed_utc": stamp, "recovered_utc": None})
    state["gaps"] = gaps
    # Written by a DIFFERENT path from the ledger, and to a fallback
    # location when the ledger's own directory refuses. It is still the
    # same disk - see history_looks_lost for the signal that survives when
    # nothing here can be written at all.
    body = json.dumps(state, indent=2, sort_keys=True)
    if not _atomic_write(p, body):
        state["degraded"] = True
        for alt in (Path(ledger).parent.parent / "logs" /
                    (Path(ledger).stem + ".continuity.json"),):
            try:
                alt.parent.mkdir(parents=True, exist_ok=True)
                alt.write_text(body, encoding="utf-8")
                state["written_to"] = str(alt)
                break
            except OSError:
                continue
    return state


def continuity_state(ledger: Path) -> dict:
    try:
        doc = json.loads(continuity_path(ledger).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"gaps": []}
    return doc if isinstance(doc, dict) else {"gaps": []}


def relevant_gaps(state: dict, asof: date) -> list[dict]:
    """Gaps recent enough that an obligation lost in one could still matter."""
    out = []
    for g in (state.get("gaps") or []):
        if not isinstance(g, dict):
            continue
        failed = str(g.get("failed_utc") or "")[:10]
        d = _as_date(failed)
        if d is None:
            continue
        if not g.get("recovered_utc") or (asof - d).days <= GAP_RELEVANCE_DAYS:
            out.append(g)
    return out


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
                "on_hold": list(ob.on_hold),
                "ever_ready_final": list(ob.ready_final),
                "decision_session": ob.decision_session,
                "first_seen_utc": now_utc.isoformat(timespec="seconds"),
                "state": PENDING, "reason": "recorded",
                "ever_discharged": False, "observed_owed_after_fill": False,
                "first_owed_sha": None,
                "escalations": 0, "escalated_on": None,
            }
        else:
            # The sleeve set can legitimately grow (a sleeve added, a venue
            # reassigned). Never shrink it away: fewer sleeves is a weaker
            # discharge test, and that direction must be a deliberate act.
            cur["sleeves"] = sorted(set(cur.get("sleeves") or []) | set(ob.sleeves))
            # A FINAL READY binds for good; a provisional one does not bind
            # at all, which is what let a midweek book make a sleeve
            # permanently obliged for a fill the release later held.
            ready = set(cur.get("ever_ready_final") or []) | set(ob.ready_final)
            cur["ever_ready_final"] = sorted(ready)
            # An AUTHORISED hold is a decision about this fill and persists,
            # but never over a sleeve already bound by a final READY.
            held = (set(cur.get("on_hold") or []) | set(ob.on_hold)) - ready
            cur["on_hold"] = sorted(held)
            if ob.decision_session and not cur.get("decision_session"):
                cur["decision_session"] = ob.decision_session
    return {"schema": LEDGER_SCHEMA, "obligations": obs}


def _revision_candidates(siblings: dict, ob: "Obligation"
                         ) -> tuple[list[str], list[str]]:
    """(candidates, unreadable) among the other fills of this venue that
    share this obligation's decision session, nearest first, in EITHER
    direction.

    THIS PARSES NOTHING IT HAS NOT CHECKED (2026-09-17, seventh review).
    Sorting by distance called ``date.fromisoformat`` on every sibling
    before ``fill_was_revised`` could classify it, so ONE malformed
    ``fill_date`` in the ledger raised ValueError straight out of
    ``current_debt`` - a function whose docstring promises it never does,
    and which the refresh calls from inside its own failure handler. A
    record that cannot be read is now separated out and REPORTED rather
    than parsed, dropped or guessed at.
    """
    if not ob.decision_session:
        return [], []
    mine = _as_date(ob.fill_date)
    if mine is None:                        # pragma: no cover - ctor pins it
        return [], []
    key = (ob.venue, str(ob.decision_session))
    readable: list[tuple[int, str]] = []
    unreadable: list[str] = []
    for f in siblings.get(key, []):
        if f == ob.fill_date:
            continue
        other = _as_date(f)
        if other is None:
            unreadable.append(str(f))
            continue
        readable.append((abs((other - mine).days), f))
    return [f for _, f in sorted(readable)], sorted(set(unreadable))


def _obligation_from_record(key: str, rec: dict) -> Obligation | None:
    venue = rec.get("venue") or (key.split("|")[0] if "|" in key else None)
    fill = rec.get("fill_date") or (key.split("|")[1] if "|" in key else None)
    if not venue or not _as_date(fill):
        return None
    return Obligation(
        str(venue), str(fill),
        tuple(sorted(str(s) for s in (rec.get("sleeves") or []))),
        rec.get("decision_session"),
        tuple(sorted(str(s) for s in (rec.get("on_hold") or []))),
        tuple(sorted(str(s) for s in (rec.get("ever_ready_final") or []))))


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
    # A damaged ALERT-dedupe record is reported, not escalated: it says
    # nothing about whether a publication is owed, and blocking on it would
    # put every hourly firing through a full refresh.
    notes: list[str] = list(book.get("notice_problems") or [])
    lost, why_lost = history_looks_lost(repo_root, book)
    if lost:
        blocking.append(why_lost)
    gaps = relevant_gaps(continuity_state(lp), asof)
    gap_floor = max((str(g.get("failed_utc") or "")[:10] for g in gaps),
                    default=None)
    if lost:
        # Nothing about any earlier fill can be asserted from a history that
        # is not there.
        gap_floor = asof.isoformat()
    for g in gaps:
        blocking.append(
            f"the obligation ledger lost writes at "
            f"{g.get('failed_utc')} (recovered "
            f"{g.get('recovered_utc') or 'not yet'}); obligations observed "
            f"before that are not in this history and a miss cannot be "
            f"established for any fill at or before it")
    targets = load_targets(repo_root)
    observed, obs_blocking, obs_notes = observe_obligations(
        targets, lambda: release_authorisation(repo_root, now_utc), asof)
    blocking.extend(obs_blocking)
    notes.extend(obs_notes)
    stale_venues = frozen_venues(targets, asof)
    notices = [{"kind": "frozen_book",
                "key": f"frozen_book|{s['venue']}|{s['fill']}",
                "venue": s["venue"], "fill": s["fill"],
                "age_days": s["age_days"],
                "detail": (f"{s['venue']} has stopped advancing: "
                           f"live_targets.json still names fill {s['fill']}, "
                           f"{s['age_days']} days before {asof.isoformat()}")}
               for s in stale_venues]
    for stale in stale_venues:
        blocking.append(
            f"live_targets.json has not advanced for {stale['venue']}: it "
            f"still names fill {stale['fill']}, {stale['age_days']} days "
            f"before {asof.isoformat()}. Which {stale['venue']} fills have "
            f"occurred since cannot be read from a book that stopped moving")
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
    # The history walk is memoised per (sleeve, fill) because several
    # obligations of the same venue share it, and it is only reached by a
    # superseded candidate.
    records: list[dict] = []
    newly_missed: list[str] = []
    hist_cache: dict[tuple[str, str], dict] = {}

    def _history_for(ob: Obligation, rec: dict) -> dict | None:
        """The walk, MEMOISED against the evidence it rests on.

        A retained obligation repeats this on every firing, before the
        retry budget is even consulted - measured at 1.8s here and 7.1s on
        the reviewer's machine for one four-sleeve window. The cached
        result is keyed on the pair that determines it: the anchor commit
        and the current remote tip. Any change to either - a new
        publication, a fetch, a force-push - invalidates it, so a reused
        result can never outlive the history that produced it.
        """
        if ref is None:
            return None
        fill = _as_date(ob.fill_date)
        if fill is None:                    # pragma: no cover
            return None
        anchor = rec.get("first_owed_sha")
        obliged = sorted(ob.obliged)
        cached = rec.get("history_cache")
        # The SLEEVE SET is part of what determines the result: a sleeve
        # added to the obligation without the tip moving used to be served a
        # cached history that simply did not contain it (2026-09-16, fourth
        # review). Anchor, tip AND sleeves.
        if (isinstance(cached, dict) and cached.get("tip") == sha
                and cached.get("anchor") == anchor
                and cached.get("sleeves") == obliged
                and isinstance(cached.get("result"), dict)):
            return {k: {**v, "dates": set(v.get("dates") or ())}
                    for k, v in cached["result"].items()}
        out = {}
        for s in ob.obliged:
            ck = (s, ob.fill_date, anchor or "")
            if ck not in hist_cache:
                hist_cache[ck] = publication_history(
                    repo_root, ref, s, fill, since_sha=anchor,
                    confirm_absent=ob.fill_date)
            out[s] = hist_cache[ck]
        rec["history_cache"] = {
            "anchor": anchor, "tip": sha, "sleeves": obliged,
            "result": {k: {**v, "dates": sorted(v["dates"])}
                       for k, v in out.items()}}
        return out

    # Sibling fills of the same venue and decision session, in EITHER
    # direction. A calendar revision can move a fill earlier - a session
    # withdrawn from the front of the week - and searching only for a later
    # fill left the original obligation owed for ever (2026-09-16, fourth
    # review). fill_was_revised already accepts either ordering; its caller
    # did not.
    siblings: dict[tuple[str, str], list[str]] = {}
    # (venue, unreadable fill date) -> the obligation keys it blocks.
    bad_siblings: dict[tuple[str, str], set] = {}
    for key, rec in book["obligations"].items():
        ds = rec.get("decision_session")
        if ds and rec.get("venue") and rec.get("fill_date"):
            siblings.setdefault((str(rec["venue"]), str(ds)), []).append(
                str(rec["fill_date"]))

    for key, rec in sorted(book["obligations"].items()):
        ob = _obligation_from_record(key, rec)
        if ob is None:
            rec["state"], rec["reason"] = UNKNOWN, "record unreadable"
            records.append({"key": key, **rec})
            blocking.append(f"obligation {key} is unreadable")
            continue
        if ref is None:
            state, reason, missed = (UNKNOWN,
                                     "no remote publication ref to check against",
                                     False)
        else:
            # First pass without history: only a superseded candidate needs
            # the walk, and it is the only branch that can assert a miss.
            state, reason, missed = classify(
                ob, published, asof,
                observed_owed=bool(rec.get("observed_owed_after_fill")))
            # A schedule revision looks exactly like a miss in the ledger,
            # and it can leave the original obligation in EITHER state: a
            # fill moved later leaves it superseded, a fill moved EARLIER
            # leaves it simply owed for ever, because the published record
            # never reaches a session the calendar has withdrawn. Both are
            # checked, against the venue calendar and only on evidence.
            if state in (SUPERSEDED, OWED):
                # Recomputed every evaluation: a sibling recorded last week
                # must not decide this week's verdict.
                rec.pop("revision_sibling", None)
                rec.pop("revision_checked", None)
                rec.pop("revision_status", None)
                candidates, unreadable_siblings = _revision_candidates(
                    siblings, ob)
                if unreadable_siblings:
                    # Not parsed, not deleted, not guessed at. A ledger that
                    # carries a fill date nobody can read cannot be reasoned
                    # over, so it is named and the verdict goes UNKNOWN.
                    #
                    # ONE PROBLEM PER BAD RECORD, NOT ONE PER NEIGHBOUR
                    # (2026-09-17, eighth review). Every obligation sharing
                    # the decision session sees the same bad sibling, so a
                    # single malformed record produced a line per obligation
                    # and the operator read four copies of one fault. The
                    # evidence stays on each record, where the affected
                    # obligation is identified; the problem is raised once,
                    # below, naming the record and everything it blocks.
                    rec["unreadable_siblings"] = unreadable_siblings
                    for sib in unreadable_siblings:
                        bad_siblings.setdefault((ob.venue, sib), set()).add(key)
                for candidate in candidates:
                    ok, status, why = fill_was_revised(
                        ob.venue, ob.fill_date, candidate,
                        ob.decision_session)
                    if ok:
                        rec["revised_to"] = candidate
                        rec["state"] = REVISED
                        rec["reason"] = (f"fill {ob.fill_date} ({ob.venue}) "
                                         f"was replaced by {candidate}: {why}")
                        rec.pop("missed", None)
                        rec["age_days"] = None
                        records.append({"key": key, **rec})
                        break
                    # NOT verified. The move is still evidence of SOMETHING:
                    # two obligations of the same venue sharing one decision
                    # session is the signature of a schedule that moved, not
                    # of a publication that was skipped. It does not
                    # discharge anything - the fill really was never
                    # recorded - but a MISS cannot be asserted over it, and
                    # it is not escalated as a missed deadline.
                    rec["revision_checked"] = why
                    rec["revision_status"] = status
                    rec["revision_sibling"] = candidate
                else:
                    candidate = None
                if rec.get("state") == REVISED:
                    continue
            if state == SUPERSEDED:
                hist = _history_for(ob, rec)
                state, reason, missed = classify(
                    ob, published, asof, history=hist,
                    observed_owed=bool(rec.get("observed_owed_after_fill")))
                rec["history"] = {
                    s: {"dates": sorted(h.get("dates") or ()),
                        "conclusive": bool(h.get("conclusive")),
                        "commits": int(h.get("commits") or 0),
                        "unreadable": int(h.get("unreadable") or 0),
                        "shallow": bool(h.get("shallow")),
                        "rewritten": bool(h.get("rewritten")),
                        "anchor": h.get("anchor"),
                        "note": h.get("note") or ""}
                    for s, h in (hist or {}).items()}
        # ADVANCEMENT IS NOT UNCONDITIONAL (2026-09-16). ``run_portfolio``
        # SKIPS a rebalance date whose decision session sits beyond the
        # panel's ``validated_through`` - an unverified tail may not choose a
        # new basket - and the skipped date never enters
        # ``latest_rebalance``. A refresh can therefore publish while the
        # record stays behind the fill, and retrying will not move it. The
        # obligation stays OWED, which is correct - the fill really is
        # unrecorded - but the operator action is different, so the escalation
        # says which of the two it is looking at. Walked only once the debt is
        # past grace, so the common path pays nothing.
        if (state == OWED and ref is not None
                and (asof - date.fromisoformat(ob.fill_date)).days > grace_days):
            hist = _history_for(ob, rec) or {}
            # "Has anything published since?" is a question about the REF,
            # not about the sleeve file: the case being diagnosed is exactly
            # one where the sleeve record does NOT change, so counting
            # commits that touched it would always answer zero.
            published_since = bool(_commits_since(repo_root,
                                                  rec.get("first_owed_sha"),
                                                  ref))
            carried = any(ob.fill_date in set((h or {}).get("dates") or ())
                          for h in hist.values())
            if published_since and not carried:
                reason += (" - NOTE: the record has been published since the "
                           "fill without carrying it, so this is a sleeve that "
                           "did not rebalance (see validated_through in "
                           "run_portfolio), not a refresh that did not run; "
                           "retrying will not move it")
                rec["published_without_recording"] = True
        was = rec.get("state")
        rec["state"], rec["reason"] = state, reason
        if state == OWED:
            # Recorded so a later SUPERSEDED verdict can tell "we watched it
            # go unpublished" from "we were not looking". The remote tip at
            # the FIRST such observation is the anchor the history walk needs:
            # the record was behind the fill at that moment, so every
            # publication that could have carried it is after this commit.
            rec["observed_owed_after_fill"] = True
            if not rec.get("first_owed_sha") and sha:
                rec["first_owed_sha"] = sha
        if state == DISCHARGED:
            rec["ever_discharged"] = True
            rec.setdefault("discharged_utc",
                           now_utc.isoformat(timespec="seconds"))
            rec["evidence_sha"] = sha
        if state == SUPERSEDED:
            lost = bool(gap_floor and ob.fill_date <= gap_floor)
            if lost and missed:
                missed = False
                rec["reason"] += (" - a miss cannot be established: the "
                                  "obligation ledger lost writes covering "
                                  "this fill")
            # A SIBLING SUPPRESSES A MISS ONLY WHILE THE CALENDAR IS SILENT
            # (2026-09-16, fifth review). The suppression used to apply
            # whenever a sibling shared the decision session, so a
            # conclusively evidenced miss - observed owed, complete history,
            # no publication carrying it - was held down on every future
            # firing by a fill the calendar had never confirmed as its
            # replacement. Absence of evidence and evidence of absence are
            # different: a calendar that could not be READ is the first, and
            # a calendar that says the old fill is still a session is the
            # second, and the second does not excuse anything.
            if missed and rec.get("revision_sibling"):
                unreadable = rec.get("revision_status") in REVISION_INCONCLUSIVE
                if unreadable:
                    missed = False
                    rec["reason"] += (
                        f" - a miss cannot be established: fill "
                        f"{rec['revision_sibling']} shares this obligation's "
                        f"decision session and the venue calendar could not "
                        f"be read, so a schedule revision cannot be ruled "
                        f"out ({rec.get('revision_checked')})")
                else:
                    rec["reason"] += (
                        f" - fill {rec['revision_sibling']} shares this "
                        f"obligation's decision session, but the calendar "
                        f"REFUSES the move ({rec.get('revision_checked')}), "
                        f"so this is a missed publication and not a schedule "
                        f"that shifted")
            rec["missed"] = bool(missed) and not rec.get("ever_discharged")
            if rec["missed"] and was != SUPERSEDED:
                newly_missed.append(key)
        rec["age_days"] = ((asof - date.fromisoformat(ob.fill_date)).days
                           if asof >= date.fromisoformat(ob.fill_date) else None)
        records.append({"key": key, **rec})

    for (venue, sib), blocked in sorted(bad_siblings.items()):
        blocking.append(
            f"the obligation ledger holds a {venue} record whose fill date is "
            f"unreadable ({sib}); a schedule revision cannot be ruled in or "
            f"out for {len(blocked)} obligation(s) sharing its decision "
            f"session ({', '.join(sorted(blocked))}) until the record is "
            f"removed")

    # A superseded obligation whose miss could NOT be established is an
    # unresolved observation, not a clean outcome. It is reported as such and
    # never escalated.
    unresolved = [r for r in records
                  if r["state"] == SUPERSEDED and not r.get("missed")]
    for r in unresolved:
        notes.append(f"obligation {r['key']}: {r['reason']}")

    owed_recs = [r for r in records if r["state"] == OWED]
    # An obligation whose fill shares a decision session with another is a
    # schedule that moved. It stays owed - the fill really is unrecorded -
    # but escalating it as a missed deadline would be wrong, and it can
    # never be discharged once the calendar has withdrawn its session.
    escalatable = [r for r in owed_recs
                   if not (r.get("revision_sibling")
                           and r.get("revision_status")
                           in REVISION_INCONCLUSIVE)]
    unknown_recs = [r for r in records if r["state"] == UNKNOWN]
    owed = bool(owed_recs) and cadence != "weekend"
    unknown = bool(unknown_recs) or bool(blocking)
    problems = [*blocking, *notes]
    oldest = min((r["fill_date"] for r in escalatable), default=None)
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
        # A FAILED WRITE IS NOT A HEALTHY EMPTY HISTORY (2026-09-16).
        # Reproduced: force save_ledger to False while the 14 September
        # obligation is owed, advance next_fill to the 21st, and the verdict
        # returns owed:false, unknown:false, problems:[] with the publication
        # still behind. The obligation had only ever lived in a file that was
        # never written. A persistence failure now makes the verdict UNKNOWN,
        # which makes the run proceed and do the work - the fail-safe
        # direction - and says plainly that history may be lost. It cannot
        # recover the record; it can refuse to report its absence as health.
        saved = save_ledger(lp, book)
        record_write_outcome(lp, saved, now_utc)
        if not saved:
            lost = (f"the obligation ledger {lp.name} could not be written; "
                    f"outstanding obligations may be lost on the next run")
            blocking.append(lost)
            unknown = True
            problems = [*blocking, *notes]
            reason = f"{reason}; {lost}" if reason else lost
            evidence["ledger_written"] = False
        else:
            evidence["ledger_written"] = True

    return DebtReport(
        owed=owed, unknown=unknown and cadence != "weekend", escalate=escalate,
        cadence=cadence, asof=asof.isoformat(), oldest_owed_fill=oldest,
        age_days=age, grace_days=grace_days,
        obligations=tuple(records), problems=tuple(problems),
        evidence=evidence, reason=reason, notices=tuple(notices))


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


def notices_due(report: DebtReport, ledger: Path, on: date) -> list[dict]:
    """Which notices should be ATTEMPTED today.

    A frozen venue owes nothing, so it can never escalate; without this it
    produced catch-up work and a log line and reached no one - the same
    shape of silence the whole workstream exists to remove. Bounded the
    same way an escalation is: once per notice per day, capped, and the
    condition stays in every verdict either way.

    Three suppressions, and they are not the same bound:

    ``attempted_on``  once a day, whatever the attempt achieved. This is
                      what makes an hourly schedule send one notice rather
                      than twelve, and it holds when delivery FAILS.
    ``count``         the delivered budget, MAX_NOTICES. Spent only by an
                      attempt the mailer confirmed.
    ``attempts``      the attempt budget, MAX_NOTICE_ATTEMPTS. A channel
                      that never works stops being retried, far later.

    A key whose record could not be read is suppressed outright: its send
    history is unknown, and an unknown history must not be read as a fresh
    budget.
    """
    book, _ = load_ledger(ledger)
    seen = book.get("notices") or {}
    unreadable = set(book.get("notices_unreadable") or {})
    out = []
    for n in report.notices:
        if n["key"] in unreadable:
            continue
        rec = seen.get(n["key"]) or {}
        if rec.get("attempted_on") == on.isoformat():
            continue
        if int(rec.get("count") or 0) >= MAX_NOTICES:
            continue
        if int(rec.get("attempts") or 0) >= MAX_NOTICE_ATTEMPTS:
            continue
        out.append(n)
    return out


def notice_claim_dir(ledger: Path) -> Path:
    """Where the per-(notice, day) claim files live. Beside the ledger."""
    p = Path(ledger)
    return p.with_name(p.stem + ".claims")


def _claim_file(ledger: Path, key: str, on: date) -> Path:
    # The key carries '|', which is not a legal Windows filename character,
    # so the file is named by digest and carries the key in its body.
    import hashlib
    digest = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:16]
    return notice_claim_dir(ledger) / f"{digest}-{on.isoformat()}.claim"


def claim_notice(ledger: Path, key: str, on: date) -> str:
    """Ask for the exclusive right to send ``key`` today. Never raises.

    Returns NOTICE_CLAIMED, NOTICE_ALREADY_CLAIMED or NOTICE_UNCLAIMABLE.
    ONLY NOTICE_CLAIMED authorises a send.

    WHY A FILE AND NOT THE LEDGER (2026-09-17, ninth review). The scheduled
    task retries HOURLY and a real refresh takes one to four hours, so two
    firings overlap routinely. Both can pass ``notices_due`` - it is a read -
    and the eighth pass then had both call ``record_notice_attempt``, whose
    "already recorded today" branch returned True, the same value as "you may
    send". Two processes read authorisation and both mailed. Nothing in a
    read-modify-write over a JSON file can fix that, because the race is
    between the read and the write.

    ``os.open`` with O_CREAT|O_EXCL is atomic on Windows and on POSIX: exactly
    one caller creates the file, everybody else gets FileExistsError. That is
    the claim. It is also DURABLE, which is what lets a send proceed when the
    ledger write later fails - see ``record_notice_delivery`` for what that
    costs.

    A claim that cannot be created at all - a dead disk - is UNCLAIMABLE and
    must not be sent, because nothing would then bound the repetition.
    """
    path = _claim_file(ledger, key, on)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return NOTICE_ALREADY_CLAIMED
    except (OSError, ValueError):
        return NOTICE_UNCLAIMABLE
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": str(key), "on": on.isoformat(),
                                 "pid": os.getpid()}))
    except OSError:
        # The claim exists, which is what bounds the day; a body we could not
        # finish writing costs diagnostics, not correctness.
        pass
    _sweep_claims(ledger, on)
    return NOTICE_CLAIMED


def release_notice_claim(ledger: Path, key: str, on: date) -> bool:
    """Give a claim back, for a caller that claimed and then did NOT send.

    A claim held by a process that sent nothing burns the whole day for that
    notice. The caller withholds when the ledger cannot be written - see the
    scheduler - and that is usually transient, so the claim is released and
    a later firing the same day can take it once writes recover.
    """
    try:
        _claim_file(ledger, key, on).unlink()
        return True
    except OSError:
        return False


def _sweep_claims(ledger: Path, on: date) -> None:
    """Drop claim files older than NOTICE_CLAIM_TTL_DAYS. Never raises."""
    floor = (on - timedelta(days=NOTICE_CLAIM_TTL_DAYS)).isoformat()
    try:
        entries = list(notice_claim_dir(ledger).glob("*.claim"))
    except OSError:
        return
    for entry in entries:
        stamp = entry.stem[-10:]
        if len(stamp) == 10 and stamp < floor:
            try:
                entry.unlink()
            except OSError:
                pass


def record_notice_conditions(ledger: Path, keys, on: date) -> bool:
    """Stamp ``last_seen`` on every notice condition reported today.

    This is what makes retention safe. ``_prune_notices`` may only drop a
    record whose condition has stopped being reported, and it decides that
    from ``last_seen`` - so something has to keep ``last_seen`` truthful for a
    condition that is still live but has spent its budget and will therefore
    never be attempted again. This does, on every firing, whether or not
    anything is due.

    It also records ``notices_observed_on``, which is the ONLY anchor
    ``_prune_notices`` trusts. Retention must not be a function of the
    records being retained - see that function for what taking the maximum
    ``last_seen`` cost - so the date comes from a run that got this far,
    never from the file.

    Writes at most once a day per key: if the stamp is already today's and
    the observation date is already recorded, nothing changes and nothing is
    written.
    """
    keys = [str(k) for k in keys]
    if not keys:
        return True
    book, _ = load_ledger(ledger)
    notices = dict(book.get("notices") or {})
    # A SUPPRESSED KEY IS LEFT ALONE. Writing a well-formed record for it is
    # the explicit operator repair, so stamping one here would clear a
    # suppression that no person had lifted.
    suppressed = set(book.get("notices_unreadable") or {})
    stamp = on.isoformat()
    changed = False
    for key in keys:
        if key in suppressed:
            continue
        rec = dict(notices.get(key) or {})
        if rec.get("last_seen") == stamp:
            continue
        rec["last_seen"] = stamp
        rec.setdefault("count", 0)
        rec.setdefault("attempts", 0)
        notices[key] = rec
        changed = True
    if book.get("notices_observed_on") != stamp:
        changed = True
    if not changed:
        return True
    book["notices"] = notices
    book["notices_observed_on"] = stamp
    return save_ledger(ledger, book)


def clear_notice_suppression(ledger: Path, key: str) -> bool:
    """Lift the suppression on a notice whose record could not be read.

    The documented operator repair for a record that was DELETED rather than
    replaced. Writing a well-formed record for the key clears it too; this
    exists because deleting the garbage outright is the more obvious move and
    left the key suppressed for ever until the ninth pass.
    """
    book, _ = load_ledger(ledger)
    bad = dict(book.get("notices_unreadable") or {})
    if str(key) not in bad:
        return False
    bad.pop(str(key), None)
    book["notices_unreadable"] = bad
    return save_ledger(ledger, book)


def record_notice_attempt(ledger: Path, keys, on: date) -> bool:
    """Record that ``keys`` are ABOUT to be attempted. False if not persisted.

    WRITTEN BEFORE THE SEND (2026-09-17, eighth review), so the budget the
    next firing reads is never behind the mail that has already gone out.

    THIS IS BOOKKEEPING, NOT AUTHORISATION (2026-09-17, ninth review). The
    eighth pass treated its return value as permission to send, and its
    "already recorded today" branch returned True - so a second, overlapping
    process read permission and mailed a duplicate. ``claim_notice`` is the
    authorisation now, it is atomic across processes, and it has three
    answers rather than two. This records what the claim holder is doing.
    """
    book, _ = load_ledger(ledger)
    notices = dict(book.get("notices") or {})
    stamp = on.isoformat()
    changed = False
    for key in keys:
        rec = dict(notices.get(key) or {})
        if rec.get("attempted_on") == stamp:
            continue
        rec["attempted_on"] = stamp
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["last_seen"] = stamp
        rec.setdefault("count", int(rec.get("count") or 0))
        rec["last_outcome"] = NOTICE_UNCONFIRMED
        notices[key] = rec
        changed = True
    if not changed:
        return True                      # already recorded today: nothing owed
    book["notices"] = notices
    return save_ledger(ledger, book)


def record_notice_delivery(ledger: Path, keys, on: date, outcome: str) -> bool:
    """Record what an attempt on ``keys`` achieved.

    Only ``NOTICE_DELIVERED`` spends the delivered budget. Anything else
    leaves ``count`` alone, so an unconfigured or refused channel cannot
    exhaust the budget without an operator having been told anything - which
    is exactly what the unset GMAIL_USER did to every other alert in this
    repository.

    "Delivered" means the SMTP server accepted the message. It does not mean
    a person read it, and no surface may say that it does.

    THE DELIVERY GUARANTEE, STATED PRECISELY. Earlier wording said simply
    "at-least-once", which is not true of any interval. What holds is:

      WITHIN A DAY, AT MOST ONCE. The claim is exclusive, so one attempt is
      made. A process that dies after claiming and before sending loses that
      day's notice outright - every later firing sees ALREADY_CLAIMED.

      ACROSS DAYS, RETRY ONLY WHILE THE CONDITION IS STILL REPORTED. If the
      SMTP server accepts the message and THIS write then fails, the mail has
      gone and the ledger does not know: ``count`` is not incremented, so the
      notice goes out again on a later day and the operator may receive it
      twice. It recurs only while ``current_debt`` still reports the
      condition; a venue fixed in the meantime is simply never mentioned
      again.

    The two acts are not one transaction and cannot be made one - the mail is
    gone the moment the server takes it, and no write here can recall it. The
    alternative, marking delivery before sending, would lose alerts silently,
    which is the failure this workstream exists to remove.
    """
    book, _ = load_ledger(ledger)
    notices = dict(book.get("notices") or {})
    stamp = on.isoformat()
    changed = False
    for key in keys:
        rec = dict(notices.get(key) or {})
        if not rec:
            continue                     # never attempted: nothing to record
        rec["last_outcome"] = outcome
        if outcome == NOTICE_DELIVERED and rec.get("notified_on") != stamp:
            rec["notified_on"] = stamp
            rec["count"] = int(rec.get("count") or 0) + 1
        notices[key] = rec
        changed = True
    if not changed:
        return False
    book["notices"] = notices
    return save_ledger(ledger, book)


def mark_notified(ledger: Path, keys, on: date) -> bool:
    """Attempt and delivery in one call, for a caller that knows both.

    Kept because the attempt/delivery split is the important distinction and
    a single-step caller should not have to reimplement it. Returns True only
    when BOTH halves persisted.
    """
    if not record_notice_attempt(ledger, keys, on):
        return False
    return record_notice_delivery(ledger, keys, on, NOTICE_DELIVERED)


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
    ap.add_argument("--clear-notice", metavar="KEY", default=None,
                    help="Lift the suppression on a notice whose record "
                         "could not be read, after removing the bad record. "
                         "Writing a well-formed record for the key clears it "
                         "too; this is for the case where it was deleted.")
    args = ap.parse_args(argv)
    if args.clear_notice:
        lp = ledger_path(Path(args.repo))
        if clear_notice_suppression(lp, args.clear_notice):
            print(f"cleared: {args.clear_notice}")
            return 0
        print(f"not suppressed, or the ledger could not be written: "
              f"{args.clear_notice}")
        return 1
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
