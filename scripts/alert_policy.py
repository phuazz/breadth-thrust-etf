"""Which operational observation earns an email, and which is recorded and kept quiet.

WHY THIS EXISTS (2026-09-20).

Two channels mail the operator on vendor and readiness conditions, and both
mail on the OBSERVATION rather than on its consequence:

  * the retraction tripwire in ``check_vendor_probe`` raises a ``[WARN]`` for
    every anomalous withdrawal the four-times-daily probe sees, and it repeats
    the mail on every later probe that still sees it, because each run's only
    memory of itself is the probe log it never reads for that purpose;
  * ``check_pretrade_ready`` raises a ``[HOLD]`` or ``[PRE-TRADE]`` at the
    Monday deadline whenever ANY sleeve is unverified, including the ordinary
    case where only Strategy D is short a European session and the weekend
    factsheet is about to say exactly that to the same reader.

Neither is wrong about the facts. Both answer the wrong question. What an
operator needs at 03:08 on a Sunday is not "did a vendor withdraw a bar" but
"is a distributed instruction now in doubt, and must I act before the fill".

WHAT THIS MODULE DECIDES, AND WHAT IT REFUSES TO DECIDE.

It decides ROUTING only: whether an observation is recorded alone, or recorded
and mailed. It changes no data-integrity check, no freshness gate and no HOLD
safeguard. Every observation reaches the operations log whatever the routing,
so a quiet decision is auditable after the fact and a suppressed repeat is
still a recorded repeat.

TWO ROUTING MODES (vendor-resilience Phase 1, 2026-10-15).

``tripwire`` (the default) keeps the deployed mailing set exactly: every
withdrawal ``check_vendor_probe`` classifies as anomalous mails, and the
routine European cycle and lines under measurement stay quiet, as they did
before this module existed. What it adds is incident memory: one mail per
incident episode once delivery is CONFIRMED, a retry while it is not, and a
recovery notice under the agreed recovery rule. The consequence below is still
computed and recorded on every event (``consequence``, ``consequence_severity``)
so the owner can judge the consequence table against real incidents.

``consequence`` routes on the consequence table: lines that feed nothing,
pre-release, historical and archived withdrawals stay quiet. That is a broader
suppression than the deployed tripwire and is NOT the default; it needs the
owner's explicit approval before any workflow passes ``--routing consequence``.

SEVERITY UNDER ``consequence`` IS A FUNCTION OF FOUR THINGS, AND THE AGE OF
THE VENDOR RESPONSE IS NOT ONE OF THEM:

  1. AFFECTED DEPENDENCIES - the live instruction's own signal, weight and
     missing-signal-input keys, then the CONFIGURED sleeve universes and their
     price lines, then the committed constituent rosters. Configured, not only
     currently ranked: a sleeve on HOLD carries empty signals and weights, and
     "not ranked this week" must never read as "feeds nothing".
  2. RELEASE PROVENANCE - whether a factsheet carrying that instruction has
     actually been distributed, read from the delivery ledger, which is
     written only after a confirmed send.
  3. READINESS AT THE CHECKPOINT - and WHICH checkpoint. The early review
     checkpoint is informational; the final deadline is the one that escalates.
  4. THE EXECUTION DEADLINE - whether the fill the instruction names is still
     ahead.

TWO DISTINCTIONS THAT THE OBVIOUS IMPLEMENTATION GETS WRONG.

A vendor serving older data does not by itself invalidate a release that was
verified and archived at the time. Conversely, a cache that still holds the
bar does not prove the released instruction remains valid - the cache holds it
because the preservation merge kept it. Nothing here consults a cache to
excuse an alert, and nothing here reads a cache to declare a recovery: a
recovery needs the newest probe row's EXPLICIT evidence for the withdrawn
session (``probe_vendor_availability.session_evidence``), never a later bar.

WHAT THIS MODULE NEVER ASSERTS. That an instruction is SUPERSEDED. Nothing
here recomputes a ranking. Supersession is an operator act - a revision, sent
through the existing sender - and every message this module writes says so.

INCIDENT STATE. One incident is one (ticker, withdrawn session) pair, folded
from the operations log into EPISODES: a withdrawal opens one, explicit
evidence that the session is served again closes it, and a later withdrawal
of the same session opens a fresh episode with fresh notification state. Data
state and delivery state are separate questions and are kept apart:

  * alert:    none | pending | delivered | withdrawn | abandoned
  * recovery: none | pending | delivered | not_required | superseded

Only a CONFIRMED delivery (``delivery: confirmed``) marks a mail delivered. A
failed, skipped or unknown send leaves it pending, and a pending alert or
recovery is routed again on the next probe even after the detector's
lookback has forgotten the withdrawal. An incident with a pending delivery is
never closed as stale.

Episodes have stable identities chained from their predecessor, every event
is bound to one, and each transition is revalidated when the log is folded;
compaction keeps the identity in a tombstone. The full episode, transition,
retention and migration contract is in ``fold_log`` and ``_trim`` (reviews
F2 and R1-R6, 2026-10-15).

ACKNOWLEDGEMENT IS TIED TO A FROZEN BATCH. The route step writes the exact
event set it routed (each event with an ``event_id``, the set with a
``batch_id``) and the subject and body built from it. The record step reads
that batch and stamps the mail step's outcome onto it; it never re-evaluates,
so a newly recomputed set can never inherit an earlier send's outcome.

WHAT REMAINS AMBIGUOUS, STATED PLAINLY. SMTP acceptance is the strongest
evidence available here; it is not inbox delivery. A send that succeeds and is
followed by a failed record or a rejected commit leaves no acknowledgement, so
the next probe mails again: a duplicate, never a silence. A send reported as
failed after the server accepted it duplicates the same way. Exactly-once
delivery is not claimed.

CONCURRENCY. Other writers are supported: every recorded event is folded
against the log as it exists at persistence time, including events routed
from older snapshots. Within one checkout ``record`` takes an exclusive lock
file, re-reads the log under it, appends by ``event_id`` (idempotent) and
replaces the file atomically. The workflow also serialises its own runs. The
lock does not coordinate separate clones; their commits still need git
conflict resolution, and a failed persistence leaves events unacknowledged.

Python datetime months are 1-indexed (January = 1). All date arithmetic goes
through ``datetime.date`` and ``timedelta``; exchange calendars answer every
session question. All strings are plain ASCII.

Exit 0 from the route step whenever it could read its state: routing is never
a gate. Exit 1 when the operations log or probe log exists but cannot be read;
an unreadable memory is never routed as an empty one (review F1).

Usage:
    python scripts/alert_policy.py --batch-out PATH          # route (CI)
    python scripts/alert_policy.py --record-batch PATH --mail-outcome success
    python scripts/alert_policy.py --now 2026-09-20T03:08:16Z    # rehearsal
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR_LOG = PROJECT_ROOT / "data" / "vendor_availability_log.jsonl"
# The operations log is COMMITTED, not written under logs/. Two reasons, and
# both are load-bearing: the probe runs in CI on a fresh checkout, so an
# ignored file would give every run an empty memory and every repeat would
# mail; and a test may read a committed artefact, never an ignored one.
OPS_LOG = PROJECT_ROOT / "data" / "ops_alert_log.jsonl"
BOOK = PROJECT_ROOT / "data" / "live_targets.json"
DELIVERY = PROJECT_ROOT / "docs" / "component_delivery.json"

# Schema 2 (2026-10-15): event_id, batch_id, delivery, routing, episode-aware
# kinds. Schema-1 rows stay readable; see incidents_from for how they fold.
SCHEMA = 2

# Rows retained in the operations log, EXCLUDING every row belonging to an
# incident that is still open or still owes a delivery. Trimming those would
# delete the record that keeps the incident quiet or the one that says a mail
# is still owed.
MAX_OPS_ROWS = 2000

# How long an unrecovered incident stays open, from the start of its
# episode. Past this it is closed as stale and recorded as such, unless a
# delivery is still pending. Calendar days, because the exposure being
# bounded is wall-clock. A first calibration from 2026-09-20; unchanged.
INCIDENT_MAX_OPEN_DAYS = 14

# How long ``record`` waits for the lock before failing. Failing is the safe
# direction: the run's events go unrecorded and are routed again.
LOCK_TIMEOUT_SECONDS = 30.0

# Tolerate small clock skew between a router and the recording writer. A
# rehearsal or faulty clock further ahead cannot poison the retention horizon.
MAX_FUTURE_SKEW = timedelta(minutes=5)

# --- Routing modes ------------------------------------------------------------
ROUTING_TRIPWIRE = "tripwire"
ROUTING_CONSEQUENCE = "consequence"
DEFAULT_ROUTING = ROUTING_TRIPWIRE
ROUTING_MODES = (ROUTING_TRIPWIRE, ROUTING_CONSEQUENCE)

# --- Severity ---------------------------------------------------------------
LOG_ONLY = "log_only"
ACTION_REQUIRED = "action_required"

# --- Consequence, most severe first ----------------------------------------
RANKED_INPUT_WITHDRAWN = "ranked_input_withdrawn"
PANEL_INPUT_WITHDRAWN = "panel_input_withdrawn"
CORE_READINESS_FAILURE = "core_readiness_failure"
D_UNVERIFIED = "d_unverified"
AUTHORISED_HOLD = "authorised_hold"
ARCHIVED_RELEASE = "archived_release"
PRE_RELEASE_INPUT = "pre_release_input"
NO_DEPENDENCY = "no_dependency"
ROUTINE_CYCLE = "routine_cycle"
UNDER_MEASUREMENT = "under_measurement"
HISTORICAL_INPUT = "historical_input"
READY = "ready"
REVIEW_CHECKPOINT = "review_checkpoint"

# --- Disposition ------------------------------------------------------------
NOTIFY = "notify"                 # mail it
QUIET = "quiet"                   # record it and say nothing
REPEAT = "repeat"                 # same episode, alert already delivered
RECOVERY_NOTIFY = "recovery_notify"
RECOVERY_QUIET = "recovery_quiet"
# Schema 1 only: a routed notification recorded as not delivered. Read as a
# NOTIFY whose delivery is unknown; never written by schema 2.
NOTIFY_UNCONFIRMED = "notify_unconfirmed"
# Operator-only correction of a RESOLVED notice this system confirmed
# delivered, when newer exact-session evidence lists the session as missing.
# Owner approval 2026-10-16: one per contradicted episode, confirmed-delivery
# and retry rules as for alerts, no wider historical-hole detection.
CORRECTION_NOTIFY = "correction_notify"
MAIL_DISPOSITIONS = frozenset({NOTIFY, RECOVERY_NOTIFY, CORRECTION_NOTIFY})

# --- Delivery outcomes ------------------------------------------------------
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_FAILED = "failed"
DELIVERY_SKIPPED = "skipped"
DELIVERY_UNKNOWN = "unknown"
DELIVERY_NOT_REQUIRED = "not_required"

# --- Event kinds ------------------------------------------------------------
RETRACTION = "retraction"
RECOVERY = "recovery"
STALE = "stale"
RETRY = "retry"
# A closed episode whose pending recovery notice is contradicted by fresh,
# explicit evidence that the session is missing again (review F3). Opens the
# next episode, carries the alert state the reader already has, supersedes
# the recovery notice. Never mails.
REOPEN = "reopen"
# Written only by compaction: one row standing for a settled incident's
# trimmed history, carrying its episode identity and terminal state (R1).
TOMBSTONE = "tombstone"
# The correction notice for a delivered RESOLVED contradicted by newer
# exact-session absence; routed with CORRECTION_NOTIFY until confirmed.
CONTRADICTION = "resolution_contradicted"
READINESS = "readiness"

# How long a tombstone outlives its incident's last activity before it may
# be dropped, and therefore how late a frozen batch may be persisted: record
# refuses a bound event older than this relative to the newest row in the
# log, so an expired identity can never be matched again (review R1). Batches
# are recorded minutes after routing in the same workflow run; the value is
# a deliberately wide first calibration, unmeasured, for owner review.
TOMBSTONE_RETENTION_DAYS = 30

# --- Notification states ----------------------------------------------------
ALERT_NONE, ALERT_PENDING, ALERT_DELIVERED = "none", "pending", "delivered"
ALERT_WITHDRAWN, ALERT_ABANDONED = "withdrawn", "abandoned"
RECOVERY_NONE, RECOVERY_PENDING, RECOVERY_DELIVERED = "none", "pending", "delivered"
RECOVERY_NOT_REQUIRED, RECOVERY_SUPERSEDED = "not_required", "superseded"

CORE_SCOPES = frozenset({"A", "B", "C", "core", "overlay", "book"})


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Dependency:
    """What a probed line actually feeds.

    ``ranked`` means the line is one the live instruction itself ranks, holds
    or is missing: its ticker is in a sleeve's signals, weights or
    ``missing_signal_inputs``. ``roles`` names every way the line was found
    (live_signal, missing_signal_input, universe, trading_proxy, roster,
    overlay_input). ``known`` is False only when nothing configured or live
    names it, which is the honest answer for a genuinely unrelated proxy.
    """
    ticker: str
    venue: str | None
    sleeves: tuple = ()
    panels: tuple = ()
    ranked: bool = False
    overlay: bool = False
    hold_input: bool = False
    roles: tuple = ()
    resolution_errors: tuple = ()

    @property
    def known(self) -> bool:
        return bool(self.sleeves) or self.overlay

    @property
    def core(self) -> bool:
        return self.overlay or any(s in ("A", "B", "C") for s in self.sleeves)

    def describe(self) -> str:
        return describe_dependency(self.as_dict())

    def as_dict(self) -> dict:
        return {"ticker": self.ticker, "venue": self.venue,
                "sleeves": list(self.sleeves), "panels": list(self.panels),
                "ranked": self.ranked, "overlay": self.overlay,
                "hold_input": self.hold_input, "roles": list(self.roles),
                "resolution_errors": list(self.resolution_errors),
                "known": self.known}


def describe_dependency(dep: dict) -> str:
    """One sentence naming what the line feeds. Reads the recorded dict, not a
    live object, so an operations-log row written weeks ago describes itself."""
    ticker = dep.get("ticker")
    sleeves = list(dep.get("sleeves") or [])
    roles = list(dep.get("roles") or [])
    if not sleeves and not dep.get("overlay"):
        text = (f"{ticker} feeds no sleeve universe, no breadth panel and no "
                f"overlay input")
        if dep.get("resolution_errors"):
            text += " that could be read (dependency lookup incomplete)"
        return text
    if dep.get("hold_input"):
        what = "missing signal input of a sleeve on HOLD"
    elif dep.get("ranked"):
        what = "ranked line"
    elif "universe" in roles:
        what = "configured universe line, not currently ranked"
    elif "trading_proxy" in roles:
        what = "price line"
    elif "overlay_input" in roles and not sleeves:
        what = "overlay input"
    else:
        what = "panel constituent"
    where = ", ".join(f"panel {p}" for p in dep.get("panels") or []) or "no panel"
    owner = "/".join("Strategy " + s for s in sleeves) or "the portfolio overlay"
    return (f"{ticker} is a {what} for {owner} ({where})"
            + ("; also a broad-market overlay input" if dep.get("overlay") and sleeves else ""))


def _base_symbols(ticker: str) -> tuple:
    """The forms a probed ticker may take in a universe or a roster.

    Probe lines carry the vendor's venue suffix (EXV1.DE), universes carry
    the bare panel id (EXV1), and rosters carry whichever form the fund
    publishes (SAP.DE). Both are tried; nothing is stripped from a symbol
    whose suffix is not a venue code, so 159801.SZ and BTC-USD survive.
    """
    forms = [ticker]
    head, sep, tail = ticker.partition(".")
    if sep and head and tail.isalpha() and len(tail) <= 3:
        forms.append(head)
    return tuple(dict.fromkeys(forms))


# Panels whose freshness the portfolio risk overlay depends on, not only the
# sleeve that ranks them. Named here rather than inferred.
OVERLAY_PANELS = frozenset({"CSP1"})


def configured_dependencies() -> tuple[dict, tuple]:
    """symbol -> [(sleeve, panel, role)] from the sleeves' CONFIGURED
    universes, plus any errors met while reading them.

    The registries are the deployed configuration: etf_registry for sleeves
    A and D (panel ids, and the yfinance price line each panel trades on),
    run_asset_class_rotation.UNIVERSE for B, run_thematic_rotation.UNIVERSE
    for C, and run_risk_overlay.EEM_TICKER for the overlay tilt. Errors are
    returned, never swallowed silently: an incomplete lookup is recorded on
    the event and, under consequence routing, cannot buy silence."""
    out: dict = {}
    errors: list = []

    def add(symbol, sleeve, panel, role):
        if symbol:
            out.setdefault(str(symbol), []).append((sleeve, panel, role))

    try:
        from etf_registry import ETF_REGISTRY, UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
        for sleeve, keys in (("A", UNIVERSE_ETFS), ("D", UNIVERSE_EUROPE_SECTORS)):
            for key in keys:
                add(key, sleeve, key, "universe")
                add((ETF_REGISTRY.get(key) or {}).get("yfinance_trading_proxy"),
                    sleeve, key, "trading_proxy")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"etf_registry: {exc!r}")
    for sleeve, module in (("B", "run_asset_class_rotation"),
                           ("C", "run_thematic_rotation")):
        try:
            mod = __import__(module)
            for key in getattr(mod, "UNIVERSE"):
                add(key, sleeve, key, "universe")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{module}: {exc!r}")
    try:
        from run_risk_overlay import EEM_TICKER
        add(EEM_TICKER, None, None, "overlay_input")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"run_risk_overlay: {exc!r}")
    return out, tuple(errors)


def resolve_dependency(ticker: str, venue: str | None, book: dict | None,
                       data_dir: Path = PROJECT_ROOT / "data",
                       configured: tuple | None = None) -> Dependency:
    """What does this line feed? Answered from the repository's own records.

    The live instruction is asked first: its signal, weight and
    missing-signal-input keys ARE the dependency list of the thing at risk.
    The configured universes answer next, so a sleeve on HOLD (empty signals
    and weights) still owns its lines. Constituent rosters answer for the
    signal side. A line none of them knows is reported unknown rather than
    guessed at from its venue.
    """
    forms = set(_base_symbols(ticker))
    sleeves: set = set()
    panels: set = set()
    roles: set = set()
    ranked = hold_input = overlay = False

    for sleeve in (book or {}).get("sleeves") or []:
        name = sleeve.get("sleeve")
        keys = set(sleeve.get("signals") or {}) | set(sleeve.get("weights") or {})
        if forms & keys and name:
            sleeves.add(name)
            ranked = True
            panels |= forms & keys
            roles.add("live_signal")
        missing = set(sleeve.get("missing_signal_inputs") or [])
        if forms & missing and name:
            sleeves.add(name)
            ranked = hold_input = True
            panels |= forms & missing
            roles.add("missing_signal_input")

    table, errors = configured if configured is not None else configured_dependencies()
    for form in forms:
        for sleeve, panel, role in table.get(form, ()):
            roles.add(role)
            if role == "overlay_input":
                overlay = True
                continue
            sleeves.add(sleeve)
            panels.add(panel)

    if not sleeves:
        # The signal side. Only the rosters of the venue's own universe are
        # opened: a probed Xetra line cannot be in a US panel.
        try:
            from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
            if venue == "XETR":
                candidates = list(UNIVERSE_EUROPE_SECTORS)
            elif venue:
                candidates = list(UNIVERSE_ETFS)
            else:
                candidates = list(UNIVERSE_ETFS) + list(UNIVERSE_EUROPE_SECTORS)
            for panel in candidates:
                roster = _read_json(Path(data_dir) / f"constituents_{panel.lower()}.json")
                snapshots = (roster or {}).get("snapshots") or {}
                if not snapshots:
                    continue
                latest = snapshots[max(snapshots)]
                if forms & set(latest.get("tickers") or []):
                    panels.add(panel)
                    roles.add("roster")
                    sleeves.add("D" if panel in UNIVERSE_EUROPE_SECTORS else "A")
        except Exception as exc:  # noqa: BLE001
            errors = tuple(errors) + (f"rosters: {exc!r}",)

    overlay = overlay or bool(panels & OVERLAY_PANELS)
    return Dependency(ticker=ticker, venue=venue,
                      sleeves=tuple(sorted(s for s in sleeves if s)),
                      panels=tuple(sorted(p for p in panels if p)),
                      ranked=ranked, overlay=overlay, hold_input=hold_input,
                      roles=tuple(sorted(roles)),
                      resolution_errors=tuple(errors))


# ---------------------------------------------------------------------------
# Release provenance
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Provenance:
    """What the delivery ledger and the live book say about the instruction.

    ``distributed`` is true only when a factsheet for the current anchor was
    CONFIRMED sent - the ledger records that after SMTP accepted it, never
    before.
    """
    anchor: str | None = None
    distributed: bool = False
    distributed_at: str | None = None
    d_hold_communicated: bool = False
    decision_session: date | None = None
    fill_date: date | None = None

    def fill_passed(self, today: date) -> bool:
        return self.fill_date is not None and self.fill_date < today

    def as_dict(self) -> dict:
        return {"anchor": self.anchor, "distributed": self.distributed,
                "distributed_at": self.distributed_at,
                "d_hold_communicated": self.d_hold_communicated,
                "decision_session": (self.decision_session.isoformat()
                                     if self.decision_session else None),
                "fill_date": self.fill_date.isoformat() if self.fill_date else None}


def read_provenance(book: dict | None, delivery: dict | None,
                    sleeves: tuple = ()) -> Provenance:
    """The provenance of the instruction the named sleeves belong to.

    Decision session and fill date are taken PER SLEEVE, because the venues
    genuinely differ, and the earliest fill among the affected sleeves is the
    deadline that binds.
    """
    entries = [s for s in (book or {}).get("sleeves") or []
               if not sleeves or s.get("sleeve") in sleeves]
    decisions = [d for d in (_as_date(s.get("decision_session")) for s in entries) if d]
    fills = [f for f in (_as_date(s.get("fill_date")) for s in entries) if f]

    # THE LEDGER IS ASKED ABOUT THIS BOOK'S OWN ANCHOR, never about its newest
    # entry; see the 2026-09-20 handoff for the Saturday trap this avoids.
    anchors = (delivery or {}).get("anchors") or {}
    anchor = (book or {}).get("as_of") or (max(anchors) if anchors else None)
    state = anchors.get(anchor) or {}
    regular = state.get("regular")
    distributed = bool(regular)
    at = state.get("last_confirmed_at")
    d_hold = regular == "d_hold" and not state.get("d_update")

    return Provenance(anchor=anchor, distributed=distributed, distributed_at=at,
                      d_hold_communicated=d_hold,
                      decision_session=max(decisions) if decisions else None,
                      fill_date=min(fills) if fills else None)


# ---------------------------------------------------------------------------
# Consequence
# ---------------------------------------------------------------------------
def assess_retraction(retraction: dict, dependency: Dependency,
                      provenance: Provenance, today: date) -> tuple:
    """(consequence, severity, reason) for one withdrawal, on the consequence
    table. Under tripwire routing this is recorded but does not route."""
    was = _as_date(retraction.get("was"))
    if retraction.get("routine"):
        return (ROUTINE_CYCLE, LOG_ONLY,
                "the vendor's measured overnight cycle; the bar is due back "
                "within two calendar days, before any fill it could reach")
    if retraction.get("measuring"):
        return (UNDER_MEASUREMENT, LOG_ONLY,
                "the withdrawal cycle of this line is still being measured, "
                "and the exemption expires on its own date")
    if not dependency.known:
        if dependency.resolution_errors:
            return (PANEL_INPUT_WITHDRAWN, ACTION_REQUIRED,
                    "the dependency lookup could not be completed, and an "
                    "unreadable configuration may not buy silence")
        return (NO_DEPENDENCY, LOG_ONLY,
                dependency.describe() + ", so no instruction rests on it")
    if provenance.fill_passed(today):
        return (ARCHIVED_RELEASE, LOG_ONLY,
                f"the fill this instruction named ({provenance.fill_date}) has "
                f"passed, so the release is archived; a later withdrawal does "
                f"not reopen it")
    if not provenance.distributed:
        return (PRE_RELEASE_INPUT, LOG_ONLY,
                "no factsheet carrying this instruction has been distributed; "
                "the readiness checkpoints decide this week")
    if was is None or provenance.decision_session is None:
        return (PANEL_INPUT_WITHDRAWN, ACTION_REQUIRED,
                "the withdrawn session or the instruction's decision session "
                "could not be read, and an unreadable date may not buy silence")
    if was < provenance.decision_session:
        return (HISTORICAL_INPUT, LOG_ONLY,
                f"the withdrawn session {was} precedes the session the "
                f"distributed instruction ranked on "
                f"({provenance.decision_session}); the cache guards hold the "
                f"published series")
    if was > provenance.decision_session:
        return (PRE_RELEASE_INPUT, LOG_ONLY,
                f"the withdrawn session {was} is later than the session the "
                f"distributed instruction ranked on "
                f"({provenance.decision_session}), so it was not an input to it")
    if dependency.ranked:
        return (RANKED_INPUT_WITHDRAWN, ACTION_REQUIRED,
                f"a line the distributed instruction itself ranks was withdrawn "
                f"for {was}, the session it ranked on, and the fill "
                f"({provenance.fill_date}) is still ahead")
    return (PANEL_INPUT_WITHDRAWN, ACTION_REQUIRED,
            f"a line feeding a panel the distributed instruction ranked on "
            f"was withdrawn for {was}, and the fill ({provenance.fill_date}) "
            f"is still ahead")


def route_retraction(retraction: dict, dependency: Dependency,
                     provenance: Provenance, today: date,
                     routing: str = DEFAULT_ROUTING) -> tuple:
    """(consequence, severity, reason, consequence_severity).

    Under ``tripwire`` every anomalous withdrawal is ACTION_REQUIRED, exactly
    the set the deployed tripwire mails; routine and under-measurement
    withdrawals stay LOG_ONLY, as they did. Under ``consequence`` the
    consequence table routes."""
    consequence, c_sev, reason = assess_retraction(retraction, dependency,
                                                   provenance, today)
    if routing == ROUTING_CONSEQUENCE:
        return consequence, c_sev, reason, c_sev
    if routing != ROUTING_TRIPWIRE:
        raise ValueError(f"unknown routing mode {routing!r}")
    if retraction.get("routine") or retraction.get("measuring"):
        return consequence, LOG_ONLY, reason, c_sev
    return (consequence, ACTION_REQUIRED,
            "an anomalous withdrawal of a settled bar; tripwire routing mails "
            "every anomalous withdrawal once per episode. Consequence "
            f"assessment, recorded and not used for routing: {reason}", c_sev)


def assess_readiness(status: str, scopes, phase: str) -> tuple:
    """(consequence, severity, reason) for a pre-trade readiness verdict.

    Not wired into any workflow by Phase 1: check_pretrade_ready keeps its
    deployed routing until the owner decides the readiness policy.
    """
    scopes = frozenset(scopes or ())
    if status == "error":
        return (CORE_READINESS_FAILURE, ACTION_REQUIRED,
                "the pre-trade inputs are invalid, which no later run repairs "
                "by itself")
    if status == "ready":
        return (READY, LOG_ONLY, "every sleeve is verified for the next fill")
    if status == "hold":
        return (AUTHORISED_HOLD, LOG_ONLY,
                "an authorised HOLD is a built, current instruction to retain "
                "the existing holdings, and the factsheet names every held "
                "sleeve")
    if phase == "review":
        return (REVIEW_CHECKPOINT, LOG_ONLY,
                "the early review checkpoint is a progress report, not a "
                "deadline; the deadline check escalates what is still "
                "outstanding then")
    if scopes and not (scopes & CORE_SCOPES):
        return (D_UNVERIFIED, LOG_ONLY,
                "Strategy D alone is unverified at the final checkpoint, and "
                "the scheduled factsheet states that to the same reader in the "
                "same hour")
    return (CORE_READINESS_FAILURE, ACTION_REQUIRED,
            "a core sleeve or the overlay input is unverified at the final "
            "deadline, and only the operator's machine can rebuild it")


# ---------------------------------------------------------------------------
# The operations log and incident state
# ---------------------------------------------------------------------------
def incident_key(ticker: str, session) -> str:
    return f"{ticker}@{session}"


@dataclass
class Incident:
    key: str
    open: bool = False
    # ``episode`` is an ordinal for people; ``episode_id`` is the identity.
    episode: int = 0
    episode_id: str | None = None
    opened_at: str | None = None
    # The newest OBSERVATION (probe time, not persistence time) in this
    # episode that saw the session withdrawn. Recovery proof must be later.
    withdrawn_observed_at: str | None = None
    # Probe-record identity of the latest withdrawal whose own observation
    # time was unusable (T4); "" when its router saw no record at all.
    withdrawn_record: str | None = None
    # The ops-log watermark the episode's first opener was routed from (T1).
    opener_snapshot: str | None = None
    # Event ID of the opener that a router must have seen before it can bind
    # a non-opening event. It distinguishes genesis episodes after expiry.
    opener_event_id: str | None = None
    opener_event_ids: list[str] = field(default_factory=list)
    # Absolute probe-log line for an unusably timed withdrawal, when known.
    withdrawn_record_position: int | None = None
    alert: str = ALERT_NONE
    recovery: str = RECOVERY_NONE
    consequence: str = NO_DEPENDENCY
    retraction: dict = field(default_factory=dict)
    first_seen: str | None = None
    last_seen: str | None = None
    session: str | None = None
    ticker: str | None = None
    venue: str | None = None
    closed_by: str | None = None
    # Routing time of the transition that closed the current episode; an
    # opener must be routed at or after it (review S3).
    closed_at: str | None = None
    recovery_evidence_at: str | None = None
    recovery_evidence_record: str | None = None
    recovery_evidence_record_position: int | None = None
    # Kept across a successor so a delayed withdrawal from its predecessor
    # can be placed against the source proof, in either persistence order.
    predecessor_episode_id: str | None = None
    predecessor_closed_by: str | None = None
    predecessor_recovery_evidence_at: str | None = None
    predecessor_recovery_evidence_record: str | None = None
    predecessor_recovery_evidence_record_position: int | None = None
    # An anomalous successor has contradicted a confirmed RESOLVED and still
    # needs to put a warning before the reader (review S1, R3, G1).
    contradicted: bool = False
    # Delivery of the correction notice: none | pending | delivered | withdrawn
    # (withdrawn: the session recovered again before it was confirmed).
    correction: str = "none"
    # Migration boundary (review R6): once a bound (schema-2) event has been
    # folded for this incident, unbound rows are history only.
    bound_seen: bool = False
    # Known only from a compaction tombstone (review R1); not reported by
    # incidents_from, but its identity seeds the next episode.
    retired_only: bool = False
    # Rows kept as history but not applied: events bound to another episode,
    # transitions whose precondition no longer held when folded, schema-1
    # recovery claims, and unbound rows past the migration boundary.
    unapplied: int = 0
    rows: list = field(default_factory=list)

    @property
    def notified(self) -> bool:
        """The current episode's alert was CONFIRMED delivered."""
        return self.alert == ALERT_DELIVERED

    @property
    def alert_pending(self) -> bool:
        return self.open and self.alert == ALERT_PENDING

    @property
    def recovery_pending(self) -> bool:
        return self.recovery == RECOVERY_PENDING

    @property
    def correction_owed(self) -> bool:
        return self.open and self.contradicted and self.correction != "delivered"

    @property
    def owes_delivery(self) -> bool:
        return self.alert_pending or self.recovery_pending or self.correction_owed


def episode_identity(key: str, prev_episode_id: str | None) -> str:
    """The stable identity of the episode that succeeds ``prev_episode_id``.

    Derived from the incident and its predecessor only, so two routers that
    open the next episode from the same predecessor agree on it (they are
    the same episode), while an episode opened from a different predecessor
    can never share an identity with it. Compaction keeps the chain tip in a
    tombstone, so trimming cannot restart the chain and reuse an identity
    (review R1)."""
    seed = f"{key}|{prev_episode_id or 'genesis'}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _instant(value) -> datetime | None:
    """A timezone-aware ISO-8601 instant, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _read_raw_lines(path: Path) -> list:
    """Lines of a log, [] only when the file does not exist yet.

    Any other failure (a denied or locked read, an undecodable file)
    propagates. Reading an existing log as empty would let ``record`` replace
    it with only the incoming events and erase every open and delivery-owed
    incident (independent review F1, 2026-10-15); routing on an empty memory
    would re-alert everything. Failing is the safe direction for both."""
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _parse(raw: str):
    if not raw.strip():
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def read_ops_log(path: Path) -> list:
    """Every readable row, in file order. One unreadable or truncated row
    never costs the rest of the history; an unreadable FILE raises."""
    return [r for r in (_parse(x) for x in _read_raw_lines(path)) if r is not None]


def _delivery_of(row: dict) -> str:
    """The delivery outcome a row records, reading schema 1 as written then.

    Schema 1 recorded ``notify`` only after confirm_delivery saw a delivered
    mail, and ``notify_unconfirmed`` otherwise; a schema-1 recovery_notify
    carried no outcome at all, so it is unknown (retryable)."""
    delivery = row.get("delivery")
    if delivery:
        return str(delivery)
    disposition = row.get("disposition")
    legacy = (row.get("schema") or 1) < 2
    if disposition == NOTIFY and legacy:
        return DELIVERY_CONFIRMED
    if disposition in MAIL_DISPOSITIONS or disposition == NOTIFY_UNCONFIRMED:
        return DELIVERY_UNKNOWN
    return DELIVERY_NOT_REQUIRED


def incidents_from(rows: list) -> dict:
    """The incidents the log still holds rows for. See ``fold_log``."""
    return {k: v for k, v in fold_log(rows).items() if not v.retired_only}


def _transition_state(inc: Incident) -> tuple:
    """State that an event can change, excluding log metadata and history."""
    return (inc.open, inc.episode_id, inc.episode, inc.opened_at,
            inc.withdrawn_observed_at, inc.withdrawn_record,
            inc.withdrawn_record_position, inc.alert, inc.recovery,
            inc.consequence, inc.retraction, inc.closed_by, inc.closed_at,
            inc.recovery_evidence_at, inc.recovery_evidence_record,
            inc.recovery_evidence_record_position, inc.contradicted,
            inc.correction, inc.opener_snapshot, inc.opener_event_id,
            tuple(inc.opener_event_ids), inc.predecessor_episode_id,
            inc.predecessor_closed_by,
            inc.predecessor_recovery_evidence_at,
            inc.predecessor_recovery_evidence_record,
            inc.predecessor_recovery_evidence_record_position)


def fold_log(rows: list) -> dict:
    """Fold the log into one state per incident, by episode, tombstones
    included.

    The log is the state. Rows are folded in file order, which ``record``
    keeps equal to persistence order, and the same rows always fold to the
    same state.

    THE EPISODE CONTRACT (reviews F2, R1-R4, R6).

    Identity. Every schema-2 event carries ``episode_id``. An event that
    OPENS an episode (a retraction routed against a closed or unknown
    incident, or a REOPEN) also carries ``prev_episode_id``, and its identity
    is ``episode_identity(key, prev_episode_id)``.

    Transitions. A bound event applies only while its precondition holds
    against the state folded so far, which is the state ``record`` re-reads
    under its lock:
      * retraction opener: the incident is closed or unknown, its current
        identity equals ``prev_episode_id``, and the opener was routed at or
        after the transition that closed the predecessor (S3); it opens the
        episode. A second opener of the same identity is the same episode
        (concurrent routers) and advances its withdrawal observation.
      * every withdrawal (retraction, reopen) records its observation time;
        an absent or malformed one needs an ordered probe-log record (T4).
      * retraction, alert retry: bound to the current episode. While it is
        open the alert state advances. Once it is closed, a late CONFIRMED
        alert is still recorded against it, and when that episode was closed
        by a recovery that owed no notice, a recovery notice becomes owed
        (review R4). A failed late send changes nothing.
      * recovery: the current episode, open, AND its proof observed strictly
        after the newest withdrawal folded so far (S1); it closes it. An
        older proof is history; if its RESOLVED was delivered, the episode is
        marked ``contradicted``. A duplicate from a second router on the same
        recovered episode may only discharge or create a notice; a quiet
        duplicate never cancels one owed (S2).
      * recovery retry: the current episode, closed by a recovery; a quiet
        retry discharges only a pending notice.
      * stale closure: the current episode, open, and NO alert delivery owed
        at fold time (review R2). Episode equality alone is not enough.
      * a delayed withdrawal naming an episode closed as STALE cannot be
        placed after that closure by source evidence: STALE has no probe
        proof. It remains history; only a fresh opener routed from the
        closed state starts a successor. No router clock is compared with an
        observation time.
      * reopen: the current episode, closed by a recovery, identity equal to
        ``prev_episode_id``, routed after that close, and observed strictly
        after the recovery proof it contradicts. The new episode inherits the
        delivered alert only while that recovery's notice is still pending.
        If the notice was delivered meanwhile, the episode starts unannounced
        and ``contradicted`` (R3, S1); one operator-only correction notice is
        then owed (owner approval 2026-10-16). A duplicate reopen of the open
        successor advances its withdrawal observation (S1).
    Last activity (``last_seen``) is monotonic; it is the retention clock
    compaction writes into a tombstone (S3).
    Anything else is kept as history and counted in ``unapplied``: it can
    neither confirm, close nor reopen another episode.

    Migration boundary (review R6). Rows without ``episode_id`` (schema 1)
    fold under the old rules only until the first bound row of that
    incident; after it they are history. Unbound episodes get a synthetic
    identity from the same chain, so a schema-2 router continuing a legacy
    episode binds to it. Schema-1 recovery rows never close an incident
    (review F5). The boundary is held in the incident's rows or tombstone;
    once a tombstone expires it is gone, which is why ``record`` refuses any
    schema-1 row older than the retention horizon (``_admit``, review S5).

    Retention (review R1). Compaction replaces a settled incident's rows
    with one tombstone carrying its identity and terminal state; the fold
    seeds the incident from it, so the next episode's identity continues the
    chain and a pending episode routed before the compaction still opens.
    """
    state: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == TOMBSTONE:
            key = row.get("tombstone_of")
            if not key:
                continue
            if key in state and not state[key].retired_only:
                state[key].unapplied += 1
                continue
            state[key] = _from_tombstone(key, row)
            continue
        key = row.get("incident")
        if not key:
            continue
        inc = state.setdefault(key, Incident(key=key))
        inc.retired_only = False
        inc.rows.append(row)
        inc.ticker = row.get("ticker") or inc.ticker
        inc.session = row.get("session") or inc.session
        inc.venue = row.get("venue") or inc.venue
        stamp = row.get("recorded_at_utc")
        inc.first_seen = inc.first_seen or stamp
        # Monotonic (review S3): a delayed row appended after newer activity
        # never moves last activity, and so the retention clock, backwards.
        if _instant(stamp) is not None and (
                _instant(inc.last_seen) is None
                or _instant(stamp) > _instant(inc.last_seen)):
            inc.last_seen = stamp
        kind = row.get("kind")
        disposition = row.get("disposition")
        if disposition == NOTIFY_UNCONFIRMED:
            disposition = NOTIFY
        delivery = _delivery_of(row)
        eid = row.get("episode_id")

        if not (isinstance(eid, str) and eid):
            if inc.bound_seen:
                inc.unapplied += 1
            else:
                _fold_unbound(inc, row, kind, disposition, delivery, stamp)
            continue
        inc.bound_seen = True
        opener = "prev_episode_id" in row
        prev = row.get("prev_episode_id")
        before_state = _transition_state(inc)
        before_unapplied = inc.unapplied

        # A router may have observed a new withdrawal from an old ops
        # snapshot. The source proof, not its router clock or its stale send,
        # reopens a recovery-closed episode. This precedes lineage checks:
        # the stale row names the predecessor, not the successor it proves.
        if (kind in (RETRACTION, REOPEN) and not inc.open
                and inc.closed_by == RECOVERY and eid == inc.episode_id
                and _withdrawal_after_recovery(
                    row, inc.recovery_evidence_at,
                    inc.recovery_evidence_record,
                    inc.recovery_evidence_record_position)):
            successor = episode_identity(key, inc.episode_id)
            _open_after_recovery(inc, successor, None, stamp, row)
            if kind == RETRACTION:
                _note_retraction(inc, row)
                _apply_alert(inc, disposition, DELIVERY_UNKNOWN)
            else:
                _note_withdrawal(inc, row)
            continue

        # The fresh router may have opened that same successor first. A
        # delayed predecessor-bound withdrawal can advance its source floor,
        # but its earlier send cannot confirm the successor's alert.
        if (kind == RETRACTION and inc.open
                and eid == inc.predecessor_episode_id
                and inc.predecessor_closed_by == RECOVERY
                and _withdrawal_after_recovery(
                    row, inc.predecessor_recovery_evidence_at,
                    inc.predecessor_recovery_evidence_record,
                    inc.predecessor_recovery_evidence_record_position)):
            _note_retraction(inc, row)
            _apply_alert(inc, disposition, DELIVERY_UNKNOWN)
            if _transition_state(inc) == before_state:
                inc.unapplied += 1
            continue

        # LINEAGE CHECKS on events bound to the CURRENT identity (T1). An
        # identity can recur after tombstone expiry (genesis reuse), so the
        # identity alone does not prove an event belongs to this episode:
        #  * a non-opening event must name the opener its router saw and have
        #    been routed at or after that episode's earliest opener;
        #  * a duplicate opener from an older ops snapshot cannot confirm
        #    delivery, although newer source evidence may still advance the
        #    withdrawal floor.
        if eid == inc.episode_id and inc.episode_id is not None:
            if opener:
                if not _snapshot_at_least(row, inc):
                    # An older ops snapshot cannot confirm this opener's mail,
                    # but a later source observation still advances the data
                    # floor. This matters when unrelated incident writes skew
                    # two legitimate routers' global snapshot watermarks.
                    if inc.open and kind in (RETRACTION, REOPEN):
                        if _note_withdrawal(inc, row) and kind == RETRACTION:
                            # Preserve quiet-to-action escalation, but never
                            # credit a send from the stale ops snapshot.
                            _note_retraction(inc, row)
                            _apply_alert(inc, disposition, DELIVERY_UNKNOWN)
                    inc.unapplied += 1
                    continue
                if inc.open:
                    _note_opener(inc, row)
            elif ((inc.opener_event_id is not None
                   and row.get("opener_event_id") not in inc.opener_event_ids)
                  or not _routed_after_open(inc, row)):
                inc.unapplied += 1
                continue

        if kind == RETRACTION:
            if eid == inc.episode_id:
                if inc.open:
                    _note_retraction(inc, row)
                    _apply_alert(inc, disposition, delivery)
                else:
                    _late_alert(inc, disposition, delivery)
            elif (opener and not inc.open and prev == inc.episode_id
                  and eid == episode_identity(key, prev)
                  and _routed_after_close(inc, row)):
                if (inc.closed_by == RECOVERY
                        and _withdrawal_after_recovery(
                            row, inc.recovery_evidence_at,
                            inc.recovery_evidence_record,
                            inc.recovery_evidence_record_position)):
                    _open_after_recovery(inc, eid, row.get("episode"), stamp, row)
                else:
                    _open_episode(inc, eid, row.get("episode"), stamp, row)
                _note_retraction(inc, row)
                _apply_alert(inc, disposition, delivery)
            else:
                inc.unapplied += 1
        elif kind == REOPEN:
            if eid == inc.episode_id and inc.open and opener:
                # A second router reopening the same episode: its absence is
                # evidence too, and the newest one must survive (review S1).
                if not _note_withdrawal(inc, row):
                    inc.unapplied += 1
            elif (opener and not inc.open and prev == inc.episode_id
                    and inc.closed_by == RECOVERY
                    and eid == episode_identity(key, prev)
                    and _routed_after_close(inc, row)
                    and _withdrawal_after_recovery(
                        row, inc.recovery_evidence_at,
                        inc.recovery_evidence_record,
                        inc.recovery_evidence_record_position)):
                _open_after_recovery(inc, eid, row.get("episode"), stamp, row)
                _note_withdrawal(inc, row)
            else:
                inc.unapplied += 1
        elif kind == RECOVERY:
            proof_at = (row.get("evidence_probed_at")
                        or (row.get("evidence") or {}).get("probed_at_utc"))
            if eid != inc.episode_id:
                inc.unapplied += 1
            elif inc.open:
                if _proof_ordered(inc, proof_at, row.get("evidence_record"),
                                  row.get("evidence_record_position"),
                                  row.get("evidence_after_record")):
                    inc.open = False
                    inc.closed_by = RECOVERY
                    inc.closed_at = stamp
                    if inc.contradicted and inc.correction != "delivered":
                        inc.correction = "withdrawn"
                    inc.recovery_evidence_at = proof_at
                    inc.recovery_evidence_record = row.get("evidence_record")
                    inc.recovery_evidence_record_position = row.get("evidence_record_position")
                    if inc.alert == ALERT_PENDING:
                        inc.alert = ALERT_WITHDRAWN
                    _apply_recovery(inc, disposition, delivery)
                    # T2: the router chose quiet from a snapshot in which the
                    # alert was not yet confirmed; if it is confirmed by now,
                    # the RESOLVED notice is owed and retried (the retry path
                    # applies the fill rule), whichever was persisted first.
                    if (disposition == RECOVERY_QUIET
                            and inc.alert == ALERT_DELIVERED
                            and inc.recovery != RECOVERY_DELIVERED
                            and not (inc.contradicted and inc.correction != "delivered")):
                        inc.recovery = RECOVERY_PENDING
                else:
                    # Proof not ordered after a withdrawal folded since it was
                    # routed (review S1): history, not recovery. If its
                    # RESOLVED notice was delivered anyway, that stays visible.
                    inc.unapplied += 1
                    if disposition == RECOVERY_NOTIFY and delivery == DELIVERY_CONFIRMED:
                        inc.contradicted = True
            elif inc.closed_by == RECOVERY:
                # A duplicate router on the same recovered episode may only
                # discharge or create a notice; its own claim that none was
                # owed can never cancel one owed now (review S2).
                if disposition == RECOVERY_NOTIFY:
                    _apply_recovery(inc, disposition, delivery)
                else:
                    inc.unapplied += 1
            else:
                inc.unapplied += 1
        elif kind == STALE:
            if (eid == inc.episode_id and inc.open and not inc.alert_pending
                    and not inc.correction_owed):
                inc.open = False
                inc.closed_by = STALE
                inc.closed_at = stamp
            else:
                inc.unapplied += 1
        elif kind == RETRY:
            if eid != inc.episode_id:
                inc.unapplied += 1
            elif row.get("retry_of") == "recovery":
                if not inc.open and inc.closed_by == RECOVERY and (
                        disposition == RECOVERY_NOTIFY
                        or inc.recovery == RECOVERY_PENDING):
                    _apply_recovery(inc, disposition, delivery)
                else:
                    inc.unapplied += 1
            elif inc.open:
                if disposition == QUIET:
                    inc.alert = ALERT_ABANDONED
                else:
                    _apply_alert(inc, disposition, delivery)
            else:
                _late_alert(inc, disposition, delivery)
        elif kind == CONTRADICTION:
            if (eid == inc.episode_id and inc.open and inc.contradicted
                    and disposition == CORRECTION_NOTIFY):
                if delivery == DELIVERY_CONFIRMED:
                    inc.correction = "delivered"
                    # The reader now holds a warning for this episode.
                    inc.alert = ALERT_DELIVERED
                elif inc.correction != "delivered":
                    inc.correction = "pending"
            else:
                inc.unapplied += 1
        if inc.unapplied == before_unapplied and _transition_state(inc) == before_state:
            inc.unapplied += 1
    return state


def record_identity(record: dict | None) -> str:
    """A content identity for one probe record, excluding local line metadata.
    Returns "" when there is no record."""
    if not record:
        return ""
    source = {k: v for k, v in record.items() if k != "_log_line"}
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _proof_ordered(inc: Incident, proof_at, proof_record,
                   proof_position=None, after_record=None) -> bool:
    """Is a recovery proof provably observed AFTER this episode's withdrawal?

    ORDERING CONTRACT (S1, S4 as revised by T4). Two floors, both binding
    when present, neither substituted by the router's clock:
      * a TIME floor, the newest valid ``observed_at`` of the episode's
        withdrawals; the proof's own probe time must be strictly later. Both
        stamps are probe-record times, written by the probe itself.
      * a RECORD floor, used when a withdrawal's source time is absent or
        unreadable: the probe record the router saw (or "" when it saw none).
        Distinct content hashes do not establish order. Absolute source-line
        positions, or both records in the same ordered history snapshot, do.
        The same record never proves anything. No clock is compared.
    With neither floor the episode cannot be ordered and nothing is proven.
    The proof itself must carry a valid probe time (R5)."""
    if inc.withdrawn_observed_at is None and inc.withdrawn_record is None:
        return False
    if _instant(proof_at) is None:
        return False
    if (inc.withdrawn_observed_at is not None
            and not _strictly_after(proof_at, inc.withdrawn_observed_at)):
        return False
    if inc.withdrawn_record is not None:
        if not proof_record or proof_record == inc.withdrawn_record:
            return False
        if inc.withdrawn_record != "":
            floor = inc.withdrawn_record_position
            # A hash names content, not its position. Prefer absolute source
            # lines; for injected histories without them, accept only a proof
            # whose own snapshot contains the withdrawn record before it.
            if (isinstance(floor, int) and isinstance(proof_position, int)):
                if proof_position <= floor:
                    return False
            elif after_record != inc.withdrawn_record:
                return False
    return True


def _strictly_after(later, earlier) -> bool:
    a, b = _instant(later), _instant(earlier)
    return a is not None and b is not None and a > b


def _withdrawal_after_recovery(row: dict, proof_at, proof_record,
                               proof_position) -> bool:
    """Order a withdrawal after a recovery using source evidence only.

    A probe time is sufficient unless both records also carry absolute line
    positions, in which case those must agree. An untimed withdrawal needs
    distinct records ordered by absolute positions or by one shared history
    snapshot. The router's recorded_at_utc is never an observation clock.
    """
    observed_at = row.get("observed_at")
    observed_record = row.get("observed_record")
    observed_position = row.get("observed_record_position")
    position_ordered = (isinstance(observed_position, int)
                        and not isinstance(observed_position, bool)
                        and isinstance(proof_position, int)
                        and not isinstance(proof_position, bool))
    if _instant(observed_at) is not None:
        if not _strictly_after(observed_at, proof_at):
            return False
        return not position_ordered or observed_position > proof_position
    if (not isinstance(observed_record, str) or not observed_record
            or not isinstance(proof_record, str) or not proof_record
            or observed_record == proof_record):
        return False
    if position_ordered:
        return observed_position > proof_position
    return row.get("observed_after_record") == proof_record


def _routed_after_close(inc: Incident, row: dict) -> bool:
    """An opener must have been routed at or after the transition that
    closed its predecessor: a router only opens from a snapshot in which the
    predecessor was already closed. An opener routed earlier is from a
    different lineage (for example a chain restarted after expiry) and is
    history (review S3)."""
    if inc.episode_id is None or inc.closed_at is None:
        return True
    routed, closed = _instant(row.get("recorded_at_utc")), _instant(inc.closed_at)
    return routed is not None and closed is not None and routed >= closed


def _routed_after_open(inc: Incident, row: dict) -> bool:
    """A non-opening event bound to the current episode must have been
    routed at or after that episode's earliest opener (T1)."""
    if inc.opened_at is None:
        return True
    routed, opened = _instant(row.get("recorded_at_utc")), _instant(inc.opened_at)
    return routed is not None and opened is not None and routed >= opened


def _snapshot_at_least(row: dict, inc: Incident) -> bool:
    """A duplicate opener's log snapshot must be at least as new as the
    episode's first opener's (T1). An empty snapshot is the oldest."""
    first = _instant(inc.opener_snapshot)
    if first is None:
        return True
    mine = _instant(row.get("snapshot_through"))
    return mine is not None and mine >= first


def _note_opener(inc: Incident, row: dict) -> None:
    """A duplicate opener admitted by the lineage checks: the episode's
    earliest routing time is the earliest of its openers."""
    routed, opened = _instant(row.get("recorded_at_utc")), _instant(inc.opened_at)
    if routed is not None and (opened is None or routed < opened):
        inc.opened_at = row.get("recorded_at_utc")
    event_id = row.get("event_id")
    if isinstance(event_id, str) and event_id not in inc.opener_event_ids:
        inc.opener_event_ids.append(event_id)


def _fold_unbound(inc: Incident, row: dict, kind, disposition, delivery, stamp) -> None:
    """Schema-1 rules, before the incident's migration boundary."""
    if kind == RETRACTION:
        if not inc.open:
            _open_episode(inc, episode_identity(inc.key, inc.episode_id),
                          None, stamp)
        # Schema 1 recorded neither an observation time nor a probe-record
        # identity; the row's own stamp is the only placement it has. This
        # is the one place a router-written time stands in for an
        # observation, and it is confined to pre-migration rows.
        _note_retraction(inc, row, legacy_time=row.get("observed_at") or stamp)
        _apply_alert(inc, disposition, delivery)
    elif kind == STALE and inc.open:
        inc.open = False
        inc.closed_by = STALE
        inc.closed_at = stamp
    else:
        inc.unapplied += 1      # schema-1 recovery claims are history (F5)


def _note_retraction(inc: Incident, row: dict, legacy_time=None) -> None:
    inc.consequence = row.get("consequence") or inc.consequence
    inc.retraction = {
        "ticker": inc.ticker, "venue": inc.venue,
        "was": row.get("session"), "now": row.get("now_bar"),
        "routine": row.get("class") == "routine",
        "measuring": row.get("class") == "measuring"}
    if legacy_time is not None:
        _advance_time_floor(inc, legacy_time)
    else:
        _note_withdrawal(inc, row)


def _advance_time_floor(inc: Incident, observed) -> bool:
    new = _instant(observed)
    if new is None:
        return False
    old = _instant(inc.withdrawn_observed_at)
    if old is None or new > old:
        inc.withdrawn_observed_at = observed
        return True
    return False


def _note_withdrawal(inc: Incident, row: dict) -> bool:
    """Record a withdrawal observation; True when a floor advanced. A valid
    ``observed_at`` advances the time floor. An absent or unreadable one sets
    the record floor only when its place after any prior record is established;
    a router clock never stands in for it."""
    if _advance_time_floor(inc, row.get("observed_at")):
        return True
    if _instant(row.get("observed_at")) is not None:
        return False
    record = row.get("observed_record")
    position = row.get("observed_record_position")
    if not isinstance(position, int) or isinstance(position, bool):
        position = None
    previous = inc.withdrawn_record_position
    if previous is not None and (position is None or position <= previous):
        return False
    if (inc.withdrawn_record not in (None, "") and previous is None
            and position is None
            and row.get("observed_after_record") != inc.withdrawn_record):
        return False
    if (isinstance(record, str)
            and (record != inc.withdrawn_record or position != previous)):
        inc.withdrawn_record = record
        inc.withdrawn_record_position = position
        return True
    return False


def _open_episode(inc: Incident, episode_id: str, ordinal, stamp, row=None) -> None:
    """A fresh episode: data state and both delivery states reset."""
    inc.predecessor_episode_id = inc.episode_id
    inc.predecessor_closed_by = inc.closed_by
    inc.predecessor_recovery_evidence_at = inc.recovery_evidence_at
    inc.predecessor_recovery_evidence_record = inc.recovery_evidence_record
    inc.predecessor_recovery_evidence_record_position = inc.recovery_evidence_record_position
    inc.open = True
    inc.episode_id = episode_id
    inc.episode = (ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool)
                   else inc.episode + 1)
    inc.opened_at = stamp
    inc.opener_snapshot = (row or {}).get("snapshot_through")
    inc.opener_event_id = (row or {}).get("event_id")
    inc.opener_event_ids = ([inc.opener_event_id]
                            if isinstance(inc.opener_event_id, str) else [])
    inc.withdrawn_observed_at = None
    inc.withdrawn_record = None
    inc.withdrawn_record_position = None
    inc.closed_by = None
    inc.closed_at = None
    inc.recovery_evidence_at = None
    inc.recovery_evidence_record = None
    inc.recovery_evidence_record_position = None
    inc.contradicted = False
    inc.correction = "none"
    inc.alert = ALERT_NONE
    inc.recovery = (RECOVERY_SUPERSEDED if inc.recovery == RECOVERY_PENDING
                    else RECOVERY_NONE)


def _open_after_recovery(inc: Incident, episode_id: str, ordinal, stamp,
                         row: dict) -> None:
    """Open a successor from source-ordered withdrawal evidence.

    A pending RESOLVED never told the reader the session served again, so its
    delivered warning carries. A delivered RESOLVED creates a correction
    obligation only for an anomalous successor. A fresh WARN can fulfil it;
    routine and measuring openers never create it.
    """
    told_resolved = inc.recovery == RECOVERY_DELIVERED
    carried = inc.alert if inc.recovery == RECOVERY_PENDING else ALERT_NONE
    _open_episode(inc, episode_id, ordinal, stamp, row)
    inc.alert = carried
    inc.contradicted = told_resolved and row.get("class") not in ("routine", "measuring")


def _apply_alert(inc: Incident, disposition, delivery: str) -> None:
    if disposition != NOTIFY or inc.alert == ALERT_DELIVERED:
        return
    inc.alert = ALERT_DELIVERED if delivery == DELIVERY_CONFIRMED else ALERT_PENDING
    if delivery == DELIVERY_CONFIRMED and inc.contradicted:
        # The successor's confirmed WARN itself corrects the earlier RESOLVED.
        inc.correction = "delivered"


def _late_alert(inc: Incident, disposition, delivery: str) -> None:
    """A CONFIRMED alert for the current episode that arrives after it
    closed (review R4). The reader has the warning, so it is recorded; if a
    recovery closed the episode without owing a notice, a notice is now owed
    under the ordinary recovery rule. Anything unconfirmed changes nothing."""
    if disposition != NOTIFY or delivery != DELIVERY_CONFIRMED:
        return
    if inc.alert != ALERT_DELIVERED:
        inc.alert = ALERT_DELIVERED
        if inc.closed_by == RECOVERY and inc.recovery in (RECOVERY_NONE,
                                                          RECOVERY_NOT_REQUIRED):
            inc.recovery = RECOVERY_PENDING


def _apply_recovery(inc: Incident, disposition, delivery: str) -> None:
    """Recovery-notice state. A delivered notice is final. A quiet decision
    sets ``not_required`` only where nothing is owed yet, or discharges a
    pending notice through the recovery-retry rule (fill passed); it never
    cancels an obligation another event created (review S2)."""
    if inc.recovery == RECOVERY_DELIVERED:
        return
    if disposition == RECOVERY_NOTIFY:
        inc.recovery = (RECOVERY_DELIVERED if delivery == DELIVERY_CONFIRMED
                        else RECOVERY_PENDING)
    elif disposition == RECOVERY_QUIET:
        inc.recovery = RECOVERY_NOT_REQUIRED


_TOMBSTONE_FIELDS = ("ticker", "venue", "session", "episode", "episode_id",
                     "opened_at", "withdrawn_observed_at", "alert", "recovery",
                     "consequence", "closed_by", "closed_at",
                     "recovery_evidence_at", "recovery_evidence_record",
                     "recovery_evidence_record_position", "bound_seen",
                      "withdrawn_record", "withdrawn_record_position",
                       "opener_snapshot", "opener_event_id", "opener_event_ids",
                       "predecessor_episode_id", "predecessor_closed_by",
                       "predecessor_recovery_evidence_at",
                       "predecessor_recovery_evidence_record",
                       "predecessor_recovery_evidence_record_position")


def _tombstone(inc: Incident, compacted: int) -> dict:
    row = {"schema": SCHEMA, "kind": TOMBSTONE, "tombstone_of": inc.key,
           "recorded_at_utc": inc.last_seen, "compacted_rows": compacted,
           **{f: getattr(inc, f) for f in _TOMBSTONE_FIELDS}}
    row["event_id"] = _event_id(row)
    return row


def _from_tombstone(key: str, row: dict) -> Incident:
    inc = Incident(key=key, retired_only=True)
    for f in _TOMBSTONE_FIELDS:
        if f in row:
            setattr(inc, f, row[f])
    if inc.opener_event_id and not inc.opener_event_ids:
        inc.opener_event_ids = [inc.opener_event_id]
    inc.open = False
    inc.first_seen = inc.last_seen = row.get("recorded_at_utc")
    return inc


def _disposition(severity: str, incident: Incident | None) -> str:
    if severity != ACTION_REQUIRED:
        return QUIET
    if incident is not None and incident.open and incident.notified:
        return REPEAT
    return NOTIFY


def _event_id(event: dict) -> str:
    body = {k: v for k, v in event.items() if k not in ("event_id", "batch_id")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def batch_id_of(events: list) -> str | None:
    ids = sorted(e["event_id"] for e in events)
    if not ids:
        return None
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:20]


def _evidence_summary(row: dict | None, stamp) -> dict | None:
    if not row:
        return None
    keep = ("last_bar", "last_completed_session", "required_day_bar_present",
            "missing_completed_days", "checked_days", "evidence")
    return {"probed_at_utc": stamp, **{k: row.get(k) for k in keep}}


def evaluate(*, vendor_rows: list, retractions: list, ops_rows: list,
             book: dict | None, delivery: dict | None, now: datetime,
             data_dir: Path = PROJECT_ROOT / "data",
             routing: str = DEFAULT_ROUTING,
             configured: tuple | None = None) -> dict:
    """Route this probe's observations. Returns events, not side effects.

    ``vendor_rows`` is the probe history (newest last); ``retractions`` is
    what ``check_vendor_probe.detect_retractions`` made of it.
    """
    from probe_vendor_availability import PROBE_SCHEMA, session_evidence
    if routing not in ROUTING_MODES:
        raise ValueError(f"unknown routing mode {routing!r}")
    if configured is None:
        configured = configured_dependencies()
    today = now.astimezone(timezone.utc).date()
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    # Tombstoned incidents included: their identity seeds the next episode.
    incidents = fold_log(ops_rows)
    newest = vendor_rows[-1] if vendor_rows else {}
    newest_rows = {r.get("ticker"): r for r in (newest.get("rows") or [])
                   if isinstance(r, dict)}
    newest_stamp = newest.get("probed_at_utc")
    newest_at = _instant(newest_stamp)
    # The probe record this routing reads, by content identity, and the ops
    # log watermark it was routed from (T1, T4).
    newest_record = record_identity(newest) if vendor_rows else ""
    newest_position = newest.get("_log_line")
    if not isinstance(newest_position, int) or isinstance(newest_position, bool):
        newest_position = None

    def after_record(floor: str | None) -> str | None:
        """Prove a source-order relation within this one history snapshot."""
        if not floor or len(vendor_rows) < 2:
            return None
        if any(record_identity(r) == floor for r in vendor_rows[:-1]):
            return floor
        return None
    snapshot_rows = [r for r in ops_rows if isinstance(r, dict)
                     and _instant(r.get("recorded_at_utc")) is not None]
    snapshot_through = (max(snapshot_rows, key=lambda r: _instant(r["recorded_at_utc"]))
                        ["recorded_at_utc"] if snapshot_rows else None)
    events: list = []
    seen_keys = set()

    def proof(ticker, session, inc, also_after=None):
        """Exact-session evidence from the NEWEST probe record only: that
        record must declare the schema whose proof fields this reads (F4),
        carry a valid observation time, and be ordered after the episode's
        withdrawal under ``_proof_ordered`` (R5, S4/T4) - and, for a reopen,
        strictly after the recovery proof it contradicts. Anything unordered
        is unknown, never proof."""
        if newest.get("schema") != PROBE_SCHEMA or newest_at is None:
            return None
        if not _proof_ordered(inc, newest_stamp, newest_record,
                              newest_position, after_record(inc.withdrawn_record)):
            return None
        if also_after is not None and not _strictly_after(newest_stamp, also_after):
            return None
        return session_evidence(newest_rows.get(ticker), session)

    def base(kind, key, ticker, venue, session, episode_id, ordinal, prev=None,
             opens=False):
        # ``episode_id`` binds the event to the episode it was routed for;
        # ``prev_episode_id`` is present only on an event that opens one. The
        # fold applies either only while its precondition holds (F2, R1-R6).
        out = {"schema": SCHEMA, "recorded_at_utc": stamp,
               "source": "vendor_probe", "routing": routing, "kind": kind,
               "incident": key, "ticker": ticker, "venue": venue,
               "session": session, "episode_id": episode_id,
               "episode": ordinal}
        if opens:
            out["prev_episode_id"] = prev
            out["snapshot_through"] = snapshot_through
        elif key in incidents and incidents[key].opener_event_id is not None:
            out["opener_event_id"] = incidents[key].opener_event_id
        return out

    def deps(ticker, venue):
        dependency = resolve_dependency(ticker, venue, book, data_dir, configured)
        return dependency, read_provenance(book, delivery, dependency.sleeves)

    for retraction in retractions:
        ticker = retraction.get("ticker")
        key = incident_key(ticker, retraction.get("was"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        dependency, provenance = deps(ticker, retraction.get("venue"))
        consequence, severity, reason, c_sev = route_retraction(
            retraction, dependency, provenance, today, routing)
        prior = incidents.get(key)
        if prior is not None and prior.open:
            head = base(RETRACTION, key, ticker, retraction.get("venue"),
                        retraction.get("was"), prior.episode_id, prior.episode)
        else:
            prev = prior.episode_id if prior is not None else None
            head = base(RETRACTION, key, ticker, retraction.get("venue"),
                        retraction.get("was"), episode_identity(key, prev),
                        (prior.episode if prior is not None else 0) + 1,
                        prev=prev, opens=True)
            if prior is not None and not prior.open:
                prior = None        # a fresh episode: never a repeat
        events.append({
            **head,
            # The observation that saw the withdrawal, by probe time and by
            # probe-record identity; recovery proof must be ordered after it
            # (R5, T4).
            "observed_at": newest_stamp,
            "observed_record": newest_record,
            "observed_record_position": newest_position,
            "observed_after_record": after_record(
                prior.withdrawn_record if prior is not None else None),
            "now_bar": retraction.get("now"),
            "class": ("routine" if retraction.get("routine") else
                      "measuring" if retraction.get("measuring") else "anomalous"),
            "dependency": dependency.as_dict(),
            "provenance": provenance.as_dict(),
            "consequence": consequence, "severity": severity,
            "consequence_severity": c_sev,
            "disposition": _disposition(severity, prior), "reason": reason,
        })

    for key, inc in sorted(incidents.items()):
        if inc.correction_owed and not inc.alert_pending and not (
                key not in seen_keys
                and proof(inc.ticker, inc.session, inc) is True):
            # Owner-approved correction (2026-10-16): the reader holds a
            # RESOLVED this system delivered, and newer exact-session evidence
            # lists the session as missing. Routed until confirmed delivered,
            # once per contradicted episode; not if this probe proves it served.
            again = inc.correction == "pending"
            events.append({
                **base(CONTRADICTION, key, inc.ticker, inc.venue, inc.session,
                       inc.episode_id, inc.episode),
                "evidence_probed_at": inc.withdrawn_observed_at,
                "consequence": inc.consequence, "severity": ACTION_REQUIRED,
                "disposition": CORRECTION_NOTIFY,
                "reason": ("a RESOLVED notice for this session was confirmed "
                           "delivered, and newer exact-session evidence lists "
                           "it as missing; the RESOLVED no longer holds"
                           + ("; sent again, earlier delivery not confirmed"
                              if again else "")),
            })
        if key in seen_keys:
            continue
        row = newest_rows.get(inc.ticker)
        if inc.open:
            if proof(inc.ticker, inc.session, inc) is True:
                dependency, provenance = deps(inc.ticker, inc.venue)
                # A contradicted episode whose correction never arrived: the
                # reader already believes the session served, so a second
                # RESOLVED would repeat it (owner approval 2026-10-16).
                notify = (inc.notified and not provenance.fill_passed(today)
                          and not (inc.contradicted and inc.correction != "delivered"))
                if notify:
                    reason = ("the withdrawn session is served again, which "
                              "closes an alert the reader was told about")
                elif inc.alert == ALERT_PENDING:
                    reason = ("the withdrawn session is served again before the "
                              "alert was confirmed delivered; the pending alert "
                              "is withdrawn, nothing remains to act on")
                elif inc.notified:
                    reason = ("the withdrawn session is served again after the "
                              "fill the instruction named; it changes nothing "
                              "anybody can still act on")
                else:
                    reason = ("the withdrawn session is served again; nothing "
                              "was communicated that this changes")
                events.append({
                    **base(RECOVERY, key, inc.ticker, inc.venue, inc.session,
                           inc.episode_id, inc.episode),
                    "now_bar": row.get("last_bar"),
                    "evidence": _evidence_summary(row, newest_stamp),
                    "evidence_basis": "newest_probe",
                    "evidence_probed_at": newest_stamp,
                    "evidence_record": newest_record,
                    "evidence_record_position": newest_position,
                    "evidence_after_record": after_record(inc.withdrawn_record),
                    "dependency": dependency.as_dict(),
                    "provenance": provenance.as_dict(),
                    "consequence": inc.consequence, "severity": LOG_ONLY,
                    "disposition": RECOVERY_NOTIFY if notify else RECOVERY_QUIET,
                    "reason": reason,
                })
                continue
            if inc.alert_pending:
                dependency, provenance = deps(inc.ticker, inc.venue)
                consequence, severity, reason, c_sev = route_retraction(
                    inc.retraction, dependency, provenance, today, routing)
                notify = severity == ACTION_REQUIRED
                events.append({
                    **base(RETRY, key, inc.ticker, inc.venue, inc.session,
                           inc.episode_id, inc.episode),
                    "retry_of": "alert",
                    "now_bar": inc.retraction.get("now"),
                    "class": ("routine" if inc.retraction.get("routine") else
                              "measuring" if inc.retraction.get("measuring")
                              else "anomalous"),
                    "dependency": dependency.as_dict(),
                    "provenance": provenance.as_dict(),
                    "consequence": consequence, "severity": severity,
                    "consequence_severity": c_sev,
                    "disposition": NOTIFY if notify else QUIET,
                    "reason": (("an alert routed earlier was not confirmed "
                                "delivered and the session is still not proven "
                                "served; routed again. ") + reason) if notify else
                              ("an alert routed earlier was never confirmed "
                               "delivered and no longer requires action: "
                               + reason),
                })
                continue
            opened = _as_date((inc.opened_at or "")[:10])
            if opened is not None and today - opened > timedelta(days=INCIDENT_MAX_OPEN_DAYS):
                events.append({
                    **base(STALE, key, inc.ticker, inc.venue, inc.session,
                           inc.episode_id, inc.episode),
                    "consequence": inc.consequence, "severity": LOG_ONLY,
                    "disposition": QUIET,
                    "reason": (f"open for more than {INCIDENT_MAX_OPEN_DAYS} "
                               f"days with no proof either way and no delivery "
                               f"owed; closed so a later withdrawal of the same "
                               f"session is not suppressed"),
                })
        elif inc.recovery_pending:
            # RECONCILE BEFORE RETRYING (review F3). The detector compares
            # tails, so a session withdrawn again under a newer completed bar
            # raises no retraction; the newest probe's exact-session evidence
            # is asked directly. Explicitly missing: the recovery notice is
            # obsolete, and the incident reopens unresolved with the alert the
            # reader already holds. Unknown: the notice is sent on the
            # earlier proof and says so, never as the newest probe's.
            current = proof(inc.ticker, inc.session, inc)
            if current is False and proof(
                    inc.ticker, inc.session, inc,
                    also_after=inc.recovery_evidence_at) is not False:
                # An absence not observed after the recovery proof it would
                # contradict cannot reopen anything (R5): unknown.
                current = None
            if current is False:
                events.append({
                    **base(REOPEN, key, inc.ticker, inc.venue, inc.session,
                           episode_identity(key, inc.episode_id), inc.episode + 1,
                           prev=inc.episode_id, opens=True),
                    "observed_at": newest_stamp,
                    "observed_record": newest_record,
                    "observed_record_position": newest_position,
                    "observed_after_record": after_record(inc.withdrawn_record),
                    "now_bar": (row or {}).get("last_bar"),
                    "evidence": _evidence_summary(row, newest_stamp),
                    "consequence": inc.consequence, "severity": LOG_ONLY,
                    "disposition": QUIET,
                    "reason": ("the newest probe lists the session as missing "
                               "again; the recovery notice that was never "
                               "confirmed delivered is withdrawn and the "
                               "incident is unresolved. The alert delivered "
                               "earlier still stands unless that recovery "
                               "notice is confirmed before this is recorded, "
                               "in which case the reopened incident starts "
                               "unannounced and one correction notice is owed"),
                })
                continue
            dependency, provenance = deps(inc.ticker, inc.venue)
            notify = not provenance.fill_passed(today)
            basis = "newest_probe" if current is True else "earlier_probe"
            events.append({
                **base(RETRY, key, inc.ticker, inc.venue, inc.session,
                       inc.episode_id, inc.episode),
                "retry_of": "recovery",
                "evidence_basis": basis,
                "evidence_probed_at": (newest_stamp if current is True
                                       else inc.recovery_evidence_at),
                "dependency": dependency.as_dict(),
                "provenance": provenance.as_dict(),
                "consequence": inc.consequence, "severity": LOG_ONLY,
                "disposition": RECOVERY_NOTIFY if notify else RECOVERY_QUIET,
                "reason": ("a recovery notice routed earlier was not confirmed "
                           "delivered; routed again" if notify else
                           "a recovery notice routed earlier was not confirmed "
                           "delivered, and the fill has since passed"),
            })

    for event in events:
        event["event_id"] = _event_id(event)
    batch = batch_id_of(events)
    for event in events:
        event["batch_id"] = batch
    notifying = [e for e in events if e["disposition"] in MAIL_DISPOSITIONS]
    return {
        "routing": routing,
        "batch_id": batch,
        "events": events,
        "notifying": notifying,
        "action_required": any(e["disposition"] == NOTIFY for e in events),
        "recovery": any(e["disposition"] == RECOVERY_NOTIFY for e in events),
        "correction": any(e["disposition"] == CORRECTION_NOTIFY for e in events),
        "recorded": len(events),
        "suppressed": [e for e in events if e["disposition"] in (QUIET, REPEAT,
                                                                 RECOVERY_QUIET)],
    }


# ---------------------------------------------------------------------------
# Delivery acknowledgement against a frozen batch
# ---------------------------------------------------------------------------
_OUTCOMES = {"success": DELIVERY_CONFIRMED, "failure": DELIVERY_FAILED,
             "skipped": DELIVERY_SKIPPED}


def stamp_delivery(events: list, outcome: str | None) -> list:
    """Stamp the mail step's own outcome onto exactly these events.

    Only ``success`` confirms. ``failure``, ``skipped``, ``cancelled``, an
    empty value or anything unrecognised leaves every mail-bearing event
    unconfirmed, so the next probe routes it again. Events that needed no
    mail are ``not_required`` whatever the outcome."""
    mapped = _OUTCOMES.get(str(outcome or "").strip().lower(), DELIVERY_UNKNOWN)
    out = []
    for event in events:
        needs = event.get("disposition") in MAIL_DISPOSITIONS
        out.append({**event, "delivery": mapped if needs else DELIVERY_NOT_REQUIRED})
    return out


def confirm_delivery(events: list, delivered: bool) -> list:
    """Compatibility shim: True is a confirmed send, False is unknown."""
    return stamp_delivery(events, "success" if delivered else None)


def freeze_batch(verdict: dict) -> dict:
    events = verdict["events"]
    return {"schema": SCHEMA, "batch_id": verdict["batch_id"],
            "routing": verdict["routing"], "events": events,
            "mail": bool(verdict["notifying"]),
            "subject": subject(events), "body": body(events)}


class BatchIntegrityError(ValueError):
    """The batch file does not match the events it carries."""


def read_batch(path: Path) -> dict:
    batch = json.loads(Path(path).read_text(encoding="utf-8"))
    events = batch.get("events") or []
    for event in events:
        if event.get("event_id") != _event_id(event):
            raise BatchIntegrityError(f"event {event.get('event_id')} was altered")
        if event.get("batch_id") != batch.get("batch_id"):
            raise BatchIntegrityError("an event belongs to a different batch")
    if batch_id_of(events) != batch.get("batch_id"):
        raise BatchIntegrityError("batch_id does not match its events")
    return batch


class LockTimeout(RuntimeError):
    """Another writer holds the operations-log lock."""


@contextlib.contextmanager
def _locked(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Exclusive lock file beside the log. Same-machine only: it does not
    coordinate separate clones, which is the workflow's concurrency group's
    and git's job. A stale lock is never broken automatically; the writer
    fails, its events stay unrecorded and are routed again."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            # PermissionError: on Windows a lock file another writer has just
            # unlinked stays "delete pending" for a moment, and creating it
            # again is refused rather than reported as existing. Same wait.
            if time.monotonic() >= deadline:
                raise LockTimeout(f"{lock} held for more than {timeout}s")
            time.sleep(0.02)
    # The lock is ours from here. The descriptor is closed in its own finally
    # BEFORE any unlink: on Windows an open handle makes the unlink fail, and
    # a metadata-write failure used to leave both the handle and the lock
    # behind, timing out every later writer (independent review F6).
    try:
        try:
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode("ascii"))
        finally:
            os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def _atomic_write(path: Path, lines: list) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(line + "\n" for line in lines))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _group_key(row: dict | None, i: int) -> str:
    r = row or {}
    key = r.get("tombstone_of") if r.get("kind") == TOMBSTONE else r.get("incident")
    return key or f"__line{i}"


def _newest_instant(rows) -> datetime | None:
    stamps = [t for t in (_instant((r or {}).get("recorded_at_utc")) for r in rows) if t]
    return max(stamps) if stamps else None


def _trim(entries: list, now: datetime | None = None) -> list:
    """Fit the log under MAX_OPS_ROWS without losing what the fold needs.

    RETENTION CONTRACT (review R1). An incident that is open or owes a
    delivery is never touched. A settled incident with an episode identity
    is COMPACTED, oldest last activity first, to one tombstone carrying its
    identity and terminal state; one with no identity (never opened) and
    unreadable lines are dropped. Only if the log still does not fit are
    tombstones dropped, oldest first, and only those that stand alone for
    their incident and whose last activity is more than
    TOMBSTONE_RETENTION_DAYS before the earlier of the newest row and the
    recording clock - the same horizon past
    which ``record`` refuses a frozen batch, so an expired identity can never
    be matched again. A protected set larger than the cap is kept whole."""
    if len(entries) <= MAX_OPS_ROWS:
        return entries
    parsed = [row for _raw, row in entries if row is not None]
    state = fold_log(parsed)
    groups: dict = {}
    for i, (_raw, row) in enumerate(entries):
        groups.setdefault(_group_key(row, i), []).append(i)
    protected = {k for k, inc in state.items() if inc.open or inc.owes_delivery}
    order = sorted((max(ix), key) for key, ix in groups.items() if key not in protected)

    drop: set = set()
    replace: dict = {}
    excess = len(entries) - MAX_OPS_ROWS
    for _last, key in order:
        if excess <= 0:
            break
        ix, inc = groups[key], state.get(key)
        if inc is not None and inc.episode_id:
            if len(ix) == 1 and (entries[ix[0]][1] or {}).get("kind") == TOMBSTONE:
                continue
            tomb = _tombstone(inc, len(ix))
            drop.update(ix[:-1])
            replace[ix[-1]] = (json.dumps(tomb, sort_keys=True), tomb)
            excess -= len(ix) - 1
        else:
            drop.update(ix)
            excess -= len(ix)
    out = [replace.get(i, e) for i, e in enumerate(entries) if i not in drop]
    if len(out) <= MAX_OPS_ROWS:
        return out

    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("recording clock must be timezone-aware")
    newest = _newest_instant(r for _raw, r in out)
    if newest is None:
        return out
    horizon = min(newest, clock) - timedelta(days=TOMBSTONE_RETENTION_DAYS)
    counts: dict = {}
    for i, (_raw, row) in enumerate(out):
        counts[_group_key(row, i)] = counts.get(_group_key(row, i), 0) + 1
    excess = len(out) - MAX_OPS_ROWS
    expired: set = set()
    for i, (_raw, row) in enumerate(out):
        if excess <= 0:
            break
        if (row or {}).get("kind") != TOMBSTONE or counts[_group_key(row, i)] != 1:
            continue
        seen = _instant(row.get("recorded_at_utc"))
        if seen is not None and seen < horizon:
            expired.add(i)
            excess -= 1
    return [e for i, e in enumerate(out) if i not in expired]


class StaleBatch(ValueError):
    """A batch the log can no longer interpret safely, refused whole and
    atomically (nothing written): an event routed before the retention
    horizon, whose identity may have expired and been reused; a schema-1 row
    older than that horizon, past the legacy-ingestion boundary (review S5);
    or a bound event whose episode's incident has expired (review S3)."""


class PredecessorExpired(StaleBatch):
    """A bound event refers to an episode the log no longer holds (its
    tombstone expired between routing and recording). Admitting it would
    record it as history and drop its obligation, so the batch is refused
    and the next probe routes the withdrawal again from current state."""


def _admit(entries: list, events: list, now: datetime | None = None) -> None:
    """The ADMISSION CONTRACT, checked under the lock against the log as it
    is now; raises StaleBatch, so either the whole batch is admitted or the
    file is untouched.

      * Any incident event (bound or not) must carry a parseable routing
        time no older than TOMBSTONE_RETENTION_DAYS before the earlier of
        the newest row and the recording clock, and no later than the clock
        plus MAX_FUTURE_SKEW. For bound events this guarantees an expired
        identity is never matched; for schema-1 rows it is the
        legacy-ingestion boundary: a legacy row old enough to predate an
        expired migration boundary is refused (S5).
      * A bound event must refer to an incident the log still holds (live
        rows or a tombstone), unless it is a genesis opener
        (``prev_episode_id`` None) (S3).
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("recording clock must be timezone-aware")
    newest = _newest_instant(row for _raw, row in entries)
    anchor = min(newest, clock) if newest is not None else None
    floor = (anchor - timedelta(days=TOMBSTONE_RETENTION_DAYS)
             if newest is not None else None)
    # Evaluate each prefix under the same fold that will replay the committed
    # log. A row merely mentioning an incident does not establish an episode.
    prefix = [row for _raw, row in entries if row is not None]
    for event in events:
        at = _instant(event.get("recorded_at_utc"))
        if at is not None and at > clock + MAX_FUTURE_SKEW:
            raise StaleBatch(
                f"event {event.get('event_id')} is stamped beyond the "
                f"recording clock plus {MAX_FUTURE_SKEW}; nothing written")
        if not event.get("incident"):
            continue
        # Undated or unreadable rows are refused whatever the log holds,
        # including an empty log (T3): an undated row cannot be placed against
        # any boundary, and an undated schema-1 NOTIFY would otherwise fold as
        # a confirmed delivery and suppress fresh mail.
        if at is None or (floor is not None and at < floor):
            raise StaleBatch(
                f"event {event.get('event_id')} for {event.get('incident')} "
                f"routed at {event.get('recorded_at_utc')} is older than the "
                f"{TOMBSTONE_RETENTION_DAYS}-day horizon before {anchor}; "
                f"nothing written")
        genesis_opener = (event.get("kind") == RETRACTION
                          and "prev_episode_id" in event
                          and event.get("prev_episode_id") is None)
        if event.get("episode_id") and not genesis_opener:
            predecessor = fold_log(prefix).get(event["incident"])
            if predecessor is None or predecessor.episode_id is None:
                raise PredecessorExpired(
                    f"event {event.get('event_id')} is bound to an episode of "
                    f"{event['incident']}, which the log no longer holds; "
                    f"nothing written, the next probe routes it again")
        prefix.append(event)


def record(path: Path, events: list, *, lock_timeout: float = LOCK_TIMEOUT_SECONDS,
           now: datetime | None = None) -> int:
    """Persist events: under the lock, re-read the log, skip any event_id
    already present (a re-run of the same batch is a no-op), append, trim,
    and replace the file atomically. Unreadable lines are kept verbatim.
    Returns the number of rows added."""
    if not events:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path, lock_timeout):
        clock = now or datetime.now(timezone.utc)
        entries = [(raw, _parse(raw)) for raw in _read_raw_lines(path) if raw.strip()]
        _admit(entries, events, clock)
        have = {row.get("event_id") for _raw, row in entries
                if row is not None and row.get("event_id")}
        added = 0
        for event in events:
            eid = event.get("event_id")
            if eid and eid in have:
                continue
            entries.append((json.dumps(event, sort_keys=True), event))
            if eid:
                have.add(eid)
            added += 1
        entries = _trim(entries, clock)
        _atomic_write(path, [raw for raw, _row in entries])
    return added


def record_batch(path: Path, batch: dict, outcome: str | None, **kw) -> list:
    events = stamp_delivery(batch.get("events") or [], outcome)
    record(path, events, **kw)
    return events


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------
def _routing_of(events: list) -> str:
    for e in events:
        if e.get("routing"):
            return e["routing"]
    return DEFAULT_ROUTING


def subject(events: list) -> str:
    notify = [e for e in events if e["disposition"] == NOTIFY]
    if notify:
        lines = ", ".join(f"{e['ticker']} {e['session']}" for e in notify)
        if _routing_of(events) == ROUTING_TRIPWIRE:
            return f"[WARN] Vendor RETRACTED a settled bar - {lines}"
        return f"[ACTION REQUIRED] Vendor withdrew a released instruction input - {lines}"
    correction = [e for e in events if e["disposition"] == CORRECTION_NOTIFY]
    if correction:
        lines = ", ".join(f"{e['ticker']} {e['session']}" for e in correction)
        return f"[CORRECTION] Withdrawn bar is missing again - {lines}"
    recovery = [e for e in events if e["disposition"] == RECOVERY_NOTIFY]
    if recovery:
        lines = ", ".join(f"{e['ticker']} {e['session']}" for e in recovery)
        return f"[RESOLVED] Withdrawn instruction input is served again - {lines}"
    return "[NO ACTION] Vendor observations recorded"


def body(events: list) -> str:
    """What is affected, whether anything is superseded, and what to do."""
    notify = [e for e in events if e["disposition"] == NOTIFY]
    recovery = [e for e in events if e["disposition"] == RECOVERY_NOTIFY]
    correction = [e for e in events if e["disposition"] == CORRECTION_NOTIFY]
    quiet = [e for e in events if e["disposition"] in (QUIET, REPEAT, RECOVERY_QUIET)]
    tripwire = _routing_of(events) == ROUTING_TRIPWIRE
    out: list = []

    if correction:
        out += ["CORRECTION", ""]
        for e in correction:
            again = (" (sent again: earlier delivery not confirmed)"
                     if "sent again" in e.get("reason", "") else "")
            out.append(f"  {e['ticker']}: a RESOLVED notice said the {e['session']} "
                       f"session was served again. The probe at "
                       f"{e.get('evidence_probed_at') or 'a later time'} lists it "
                       f"as missing.{again}")
        out += ["",
                "  That RESOLVED no longer holds: treat the withdrawal as",
                "  unresolved. Nothing here recomputes a ranking; re-verify on",
                "  values before relying on the affected line. A further notice",
                "  follows only when a probe proves the session served again."]

    if notify:
        provenance = notify[0].get("provenance") or {}
        if out:
            out.append("")
        out += ["WHAT IS AFFECTED", ""]
        for e in notify:
            again = " (sent again: earlier delivery not confirmed)" if e["kind"] == RETRY else ""
            out.append(f"  {e['ticker']}: last bar was "
                       f"{e['session']}, now {e.get('now_bar') or 'no bar at all'}.{again}")
            out.append(f"    {describe_dependency(e.get('dependency') or {'ticker': e['ticker']})}.")
            reason = e["reason"]
            out.append(f"    {reason[:1].upper()}{reason[1:]}.")
        out += ["", "IS A PREVIOUS INSTRUCTION SUPERSEDED", "",
                "  No. Nothing here recomputes a ranking, so nothing here can",
                "  say whether the withdrawn input would change one."]
        if provenance.get("distributed"):
            out += [f"  The factsheet distributed for anchor {provenance.get('anchor')} "
                    f"stands as sent until you",
                    "  supersede it deliberately, through the existing sender."]
        else:
            out += [f"  No factsheet for anchor {provenance.get('anchor')} is recorded "
                    f"as distributed."]
        fill = provenance.get("fill_date")
        out += [
            "", "WHAT TO DO", "",
            f"  1. Re-verify the affected strategy against the "
            f"{provenance.get('decision_session')} session",
            "     before the fill. Check on VALUES, never on the index - a",
            "     placeholder row carries the right date and a NaN close:",
            f"       python -c \"import yfinance as yf; "
            f"s=yf.Ticker('{notify[0]['ticker']}').history(period='5d', "
            f"auto_adjust=True)['Close'].dropna(); "
            f"print(s.index[-1].date(), float(s.iloc[-1]))\"",
            "  2. If the ranking is unchanged, no message is needed: the "
            "instruction",
            "     already sent remains correct and this alert closes when the "
            "probe proves the bar served again.",
            "  3. If the ranking changes, issue a revision through",
            "     scripts/send_component_factsheet.py revise --revision <id>. Do "
            "not",
            "     edit or re-send the delivered factsheet by any other route.",
            f"  4. The fill this instruction names is {fill}. That is the "
            f"deadline this",
            "     alert is measured against, not the age of the vendor's answer.",
        ]

    if recovery:
        if out:
            out.append("")
        out += ["RESOLVED", ""]
        for e in recovery:
            again = " (sent again: earlier delivery not confirmed)" if e["kind"] == RETRY else ""
            at = e.get("evidence_probed_at") or "an earlier probe"
            if e.get("evidence_basis") == "newest_probe":
                basis = f"proven by the newest probe, {at}"
            else:
                basis = (f"proven by the probe at {at}; the newest probe "
                         f"neither confirms nor contradicts it")
            out.append(f"  {e['ticker']}: the {e['session']} session is served "
                       f"again ({basis}).{again}")
        label = "WARN" if tripwire else "ACTION REQUIRED"
        out += ["",
                f"  This closes the {label} alert raised earlier for the same",
                "  session. No instruction changed in the meantime; if you "
                "re-verified",
                "  and the ranking held, nothing further is required."]

    if quiet:
        if out:
            out.append("")
        out += ["RECORDED, NO ACTION", ""]
        for e in quiet:
            out.append(f"  {e['ticker']} {e['session']}: {e['consequence']} "
                       f"({e['disposition']}) - {e['reason']}.")

    batch = next((e.get("batch_id") for e in events if e.get("batch_id")), None)
    out += ["",
            "Every observation above is recorded in data/ops_alert_log.jsonl, "
            "whether",
            "or not it was mailed. Routing changes nothing about the data-"
            "integrity",
            "checks, the freshness gates or the HOLD safeguards, all of which "
            "still",
            "refuse exactly what they refused before.",
            f"Routing: {_routing_of(events)}. Batch: {batch}."]
    return "\n".join(out)


def _emit_outputs(batch: dict) -> None:
    """Step outputs for the workflow's conditional email step. No-op locally."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    events = batch["events"]
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"action_required={'true' if any(e['disposition'] == NOTIFY for e in events) else 'false'}\n")
        fh.write(f"recovery={'true' if any(e['disposition'] == RECOVERY_NOTIFY for e in events) else 'false'}\n")
        fh.write(f"correction={'true' if any(e['disposition'] == CORRECTION_NOTIFY for e in events) else 'false'}\n")
        fh.write(f"mail={'true' if batch['mail'] else 'false'}\n")
        fh.write(f"recorded={len(events)}\n")
        fh.write(f"batch_id={batch['batch_id'] or ''}\n")
        fh.write(f"subject={batch['subject']}\n")
        fh.write("detail<<ALERT_POLICY_EOF\n")
        fh.write(batch["body"].rstrip("\n") + "\n")
        fh.write("ALERT_POLICY_EOF\n")


def load_history(path: Path, lookback: int) -> list:
    history = []
    lines = _read_raw_lines(path)
    start = max(0, len(lines) - (lookback + 1))
    for position, line in enumerate(lines[start:], start=start):
        row = _parse(line)
        if row is not None:
            row["_log_line"] = position
            history.append(row)
    return history


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vendor-log", default=str(VENDOR_LOG))
    ap.add_argument("--ops-log", default=str(OPS_LOG))
    ap.add_argument("--book", default=str(BOOK))
    ap.add_argument("--delivery", default=str(DELIVERY))
    ap.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    ap.add_argument("--routing", default=DEFAULT_ROUTING, choices=ROUTING_MODES,
                    help="tripwire (default, the deployed mailing set) or "
                         "consequence (needs owner approval)")
    ap.add_argument("--now", default=None,
                    help="ISO-8601 UTC instant to evaluate at, for rehearsal.")
    ap.add_argument("--batch-out", default=None,
                    help="write the routed batch here (route step); the log "
                         "is not touched")
    ap.add_argument("--record-batch", default=None,
                    help="record this frozen batch with --mail-outcome; no "
                         "re-evaluation")
    ap.add_argument("--mail-outcome", default="",
                    help="the mail step's outcome: success confirms delivery; "
                         "anything else leaves it pending and retried")
    args = ap.parse_args(argv)

    if args.now and (args.batch_out or args.record_batch):
        print("routing FAILED: --now is rehearsal-only and cannot write or record a batch")
        return 1

    if args.record_batch:
        try:
            batch = read_batch(Path(args.record_batch))
            events = record_batch(Path(args.ops_log), batch, args.mail_outcome)
        except (OSError, ValueError, LockTimeout) as exc:
            print(f"record FAILED, nothing written: {exc!r}. The events stay "
                  f"unrecorded and are routed again on the next probe.")
            return 1
        pending = sum(1 for e in events if e["disposition"] in MAIL_DISPOSITIONS
                      and e["delivery"] != DELIVERY_CONFIRMED)
        print(f"recorded batch {batch['batch_id']}: {len(events)} event(s)"
              + (f"; {pending} mail(s) NOT confirmed and will be routed again"
                 if pending else ""))
        return 0

    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

    import check_vendor_probe as cvp
    try:
        history = load_history(Path(args.vendor_log), cvp.RETRACTION_LOOKBACK_ROWS)
        ops_rows = read_ops_log(Path(args.ops_log))
    except (OSError, ValueError) as exc:
        # Unreadable state is not empty state (review F1). No batch is
        # written, so nothing is recorded; the workflow falls back to the
        # tripwire's own mail.
        print(f"routing FAILED, unreadable state: {exc!r}. No batch written.")
        return 1
    verdict = evaluate(
        vendor_rows=history,
        retractions=cvp.detect_retractions(history),
        ops_rows=ops_rows,
        book=_read_json(Path(args.book)),
        delivery=_read_json(Path(args.delivery)),
        now=now, data_dir=Path(args.data_dir), routing=args.routing)
    batch = freeze_batch(verdict)

    print(f"Alert policy ({verdict['routing']}) - {verdict['recorded']} event(s), "
          f"{len(verdict['notifying'])} to mail, "
          f"{len(verdict['suppressed'])} recorded quietly; batch {verdict['batch_id']}")
    for event in verdict["events"]:
        print(f"  {event['disposition']:15s} {event['kind']:10s} "
              f"{event['incident']:24s} {event['consequence']}")
    if verdict["notifying"]:
        print("")
        print(batch["subject"])
        print(batch["body"])
    if args.batch_out:
        out = Path(args.batch_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(out, [json.dumps(batch, sort_keys=True)])
        print(f"\n  batch written to {out}; the operations log is untouched")
    else:
        print("\n  rehearsal: nothing written, nothing sent")
    _emit_outputs(batch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
