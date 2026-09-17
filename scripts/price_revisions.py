"""Does the vendor REVISE prices it already served, or only append?

WHY THIS EXISTS (2026-09-16). The post-fill pair was blocked for days by
G1's hollow-tail check, and the argument for relaxing it rested on a claim
nobody could test: that when the market-data vendor withdraws a recent
European close and restores it later, it only APPENDS the missing row and
never revises rows it had already served. If that is true, a hollow row
dated after the book's required session is inert. If it is false, the
earlier closes were provisional too and the guard is right.

WHAT THE FIRST VERSION OF THIS MODULE MEASURED, AND WHY IT COULD NOT
ANSWER THAT (adjudicated 2026-09-16, external review). It diffed the
incumbent cache against the frame about to replace it, captured
immediately before ``close.to_parquet(cache_path)``. By that point
``download_prices`` has already run the cell-preservation merge, which
fills every cell the vendor did not serve from the cache's own previous
value. A RAW VENDOR WITHDRAWAL IS THEREFORE INVISIBLE AT THAT POINT: the
cell it withdrew still carries last run's price, the diff sees no change,
and the one category that bears on the question reads zero for the same
reason the cache looks healthy. Reproduced on synthetic frames: raw vs
prior gives one withdrawal, post-preservation gives none.

Three further defects in the same measurement:

  * ``diff_frames`` compared only intersecting rows and columns, so a
    populated row or column that vanished entirely counted as a shape
    change and not as a loss of served data.
  * ``price_source`` was recorded as the basis. ``auto`` is not a vendor,
    it is a selection policy, and a run that flipped yfinance -> auto
    reclassified every genuine change as a basis change.
  * A vendor re-adjusting a whole column (a split or a dividend applied
    retroactively) is not the same event as one close being restated, and
    both landed in ``revisions``.

WHAT IS MEASURED NOW. Two separate records, in that order of authority:

  1. VENDOR OBSERVATIONS (``logs/vendor_state/<panel>.json`` plus
     ``logs/vendor_events.jsonl``) taken from the RAW download, before
     preservation, before the Norgate overlay and before tail processing.
     Each admitted cell runs a small state machine - absent, served,
     withheld - so a served -> withheld -> restored CYCLE can be
     identified and the restored value compared against the value the
     vendor served before it withdrew it. That comparison is the only
     direct evidence on the question.
  2. CACHE CHANGES (``logs/cache_changes.jsonl``), the pre-write diff,
     kept because it is a useful diagnostic of what actually reached the
     cache - but labelled, and never read as vendor behaviour.

WHAT IS DELIBERATELY NOT INFERRED. Zero revisions is not append-only.
Zero complete cycles is not evidence of anything at all. The summary says
so in the verdict rather than leaving a reader to draw the reassuring
conclusion, and it reports how many cycles it has actually seen, what it
could not see, and the retention limits that bound both.

BOUNDED BY CONSTRUCTION. A fixed observation window, a cell budget per
panel, a retention limit on cells left mid-withdrawal and a capped event
ledger. Every bound is reported in the summary, because a diagnostic whose
silence could mean either "nothing happened" or "it was trimmed" is worse
than none.

Python datetime months are 1-indexed. Nothing here may raise into the
refresh: a diagnostic that can break a build is worse than no diagnostic.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

SCHEMA = 1

# --- Observation bounds (all reported in the summary) ----------------------
# Sessions of the raw frame's tail admitted per run. Eight covers a
# European retraction (withdrawn overnight, restored about T+2) several
# times over while keeping a 500-name panel's state file in the low
# hundreds of kilobytes.
WINDOW_SESSIONS = 8
# Cells admitted in ONE observation. A 750-name panel over an eight-session
# window is exactly 6000, and that working set is what the next run must
# compare against.
MAX_CELLS = 6000
# Out-of-window cells kept for open cycles, over and above the working set.
#
# THE WORKING SET IS NEVER EVICTED TO MAKE ROOM (2026-09-16, third review).
# A single accumulated ceiling of 6000 evicted 750 SERVED cells that were
# still inside the observation window, and a later withdrawal of those cells
# was then recorded as "absent" - the withdrawal count stayed at 750 where
# it should have reached 1500. Forgotten prior state is not evidence that a
# cell was never served, so the cap is now layered: the window always fits,
# retained cycles have their own budget, and only a pathological panel
# reaches the hard ceiling - where the loss of DETECTION, not merely of
# storage, is counted and constrains the verdict.
MAX_RETAINED_CELLS = 1500
MAX_CELLS_HARD = 24000
# A cell left WITHHELD is kept past the window for this long, so a cycle
# that spans the window boundary is not lost to eviction. One evicted
# while still withheld is counted: it is a cycle we will never close.
WITHHELD_RETENTION_DAYS = 30
# Event ledger tail.
EVENT_CAP = 2000
# Cache-diff per-cell detail. Counts are exact regardless.
SAMPLE_CAP = 40
# Rows of the CACHE diff actually walked cell by cell. The full CSP1 cache is
# 2309 x 727, and diffing it whole measured 16.4 seconds for ONE panel on
# 2026-09-16 - minutes across the book, on every refresh, for a SECONDARY
# diagnostic. Shape changes and lost populated cells are still counted over
# the whole frame, because those are set operations and a masked sum; only
# the per-cell walk is bounded, and the bound is reported on the record.
CACHE_DIFF_SESSIONS = 60

# Relative tolerance for "the same price". Parquet round-trips float64
# exactly, so an unchanged cell compares equal bit for bit and this never
# fires on one. It exists to stop a last-place difference from a vendor's
# own float formatting reading as a revision.
REL_TOL = 1e-12
# A uniform ratio is the SHAPE of a re-adjustment and not evidence of one.
# Three cells is the least that can even raise the question; below it a
# change is simply a revision. Whether a uniform ratio IS a corporate action
# can only be settled by an independent source, and none is wired - see
# _classify_changes.
ADJUSTMENT_MIN_CELLS = 3
ADJUSTMENT_REL_TOL = 1e-6

# Change classes. AMBIGUOUS is not a hedge: it is the honest answer when a
# single ratio runs across cells that do not form a prefix, which is neither
# a re-adjustment nor plainly a set of independent restatements. It never
# counts towards revision evidence AND never counts as clean - a summary
# carrying ambiguous changes cannot return a "no revision observed" verdict.
ADJUSTMENT = "adjustment"
REVISION = "revision"
AMBIGUOUS = "ambiguous"

# Basis vocabulary. "auto" is a SELECTION POLICY and never appears here.
YFINANCE = "yfinance"
NORGATE = "norgate"
UNKNOWN_BASIS = "unknown"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _same(a: float, b: float) -> bool:
    if a == b:
        return True
    a_na, b_na = math.isnan(a), math.isnan(b)
    if a_na and b_na:
        return True
    if a_na or b_na:
        return False
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=0.0)


def _atomic_write(path: Path, text: str) -> bool:
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


def _append_bounded(path: Path, records, cap: int) -> bool:
    """Append one record or a batch, trimming to ``cap`` lines. ONE rewrite.

    Takes a batch because the caller has a run's worth of events at once and
    appending them singly rewrote the whole ledger per event.
    """
    batch = [records] if isinstance(records, dict) else list(records or ())
    if not batch:
        return True
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if p.exists():
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
        lines.extend(json.dumps(r) for r in batch)
        return _atomic_write(p, "\n".join(lines[-cap:]) + "\n")
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def resolve_column_basis(sidecar: dict | None) -> dict[str, str]:
    """{column: basis} from a prices_cache_<panel>.source.json sidecar.

    Resolves the ACTUAL source of each column. ``source`` in the sidecar is
    the requested policy - ``yfinance``, ``norgate`` or ``auto`` - and
    ``auto`` is not a vendor: under it the columns named in
    ``columns_from_norgate`` came from Norgate and everything else carried
    the incumbent yfinance download. Recording "auto" as the basis made a
    yfinance-to-auto policy flip look like a basis change on every column
    and a genuine Norgate swap look like none.

    A strict ``norgate`` run blanks what it could not resolve, so a column
    it did not take is UNKNOWN rather than yfinance. Unknown never compares
    equal to anything, so such a cell is withheld from the revision
    evidence instead of admitted on an assumption.
    """
    if not isinstance(sidecar, dict):
        return {"__default__": UNKNOWN_BASIS}
    policy = str(sidecar.get("source") or "").strip().lower()
    out: dict[str, str] = {}
    for col in (sidecar.get("columns_from_norgate") or []):
        out[str(col)] = NORGATE
    for col in (sidecar.get("columns_kept_on_incumbent") or []):
        out.setdefault(str(col), YFINANCE)
    if policy in ("yfinance", "auto"):
        default = YFINANCE
    elif policy == "norgate":
        default = UNKNOWN_BASIS      # unresolved names are blanked, not served
    else:
        default = UNKNOWN_BASIS
    return {"__default__": default, **out}


def basis_of(basis_map: dict[str, str], col: str) -> str:
    if not basis_map:
        return UNKNOWN_BASIS
    return basis_map.get(str(col), basis_map.get("__default__", UNKNOWN_BASIS))


# ---------------------------------------------------------------------------
# 1. Vendor observations - the primary evidence
# ---------------------------------------------------------------------------
def admitted_dates(index, now_utc: datetime, *, required_through=None,
                   window: int = WINDOW_SESSIONS) -> list[str]:
    """The tail dates an observation may record.

    UNFINISHED SESSIONS ARE EXCLUDED. A date at or after the current UTC
    date can still be trading somewhere, and a partial intraday bar is not
    a missing one; admitting it would manufacture a withdrawal every time
    the run happened to be early. ``required_through``, when the caller has
    one, bounds it further.
    """
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(list(index)))
    except (TypeError, ValueError):
        return []
    cutoff = pd.Timestamp(now_utc.astimezone(timezone.utc).date())
    if required_through is not None:
        try:
            cutoff = min(cutoff, pd.Timestamp(required_through)
                         + pd.Timedelta(days=1))
        except (TypeError, ValueError):
            pass
    keep = [d for d in sorted(set(idx)) if d < cutoff]
    return [str(pd.Timestamp(d).date()) for d in keep[-int(window):]]


def observe_vendor_frame(raw, *, panel: str, source: str = YFINANCE,
                         now_utc: datetime | None = None,
                         required_through=None,
                         window: int = WINDOW_SESSIONS,
                         max_cells: int = MAX_CELLS,
                         also_admit=None) -> dict:
    """One bounded, provenanced observation of what the vendor SERVED.

    Takes the raw download frame, before any preservation, overlay or tail
    processing. ``answered`` separates a request that came back with
    nothing at all - a dead endpoint, a timeout swallowed upstream - from a
    vendor that answered and declared specific bars missing. The two cannot
    be conflated: the first is our failure and the second is evidence.

    ``also_admit`` carries dates OUTSIDE the rolling window that a retained
    open cycle still needs. Without it a withdrawal could be retained for
    thirty days and never close: once its date left the eight-session
    window it was never read again, so the vendor could serve the restored
    value in every subsequent frame while the cell stayed "withheld" for
    ever. Reproduced 2026-09-16. The set is bounded by the retained cells,
    which are themselves capped.
    """
    now_utc = now_utc or _now()
    rec = {"schema": SCHEMA, "panel": str(panel), "source": str(source),
           "asof": now_utc.isoformat(timespec="seconds"),
           "answered": False, "dates": [], "window_dates": [],
           "reopened_dates": [], "cells": {},
           "unanswered_columns": [], "columns_observed": 0,
           "truncated_dates": 0, "window": int(window),
           "max_cells": int(max_cells)}
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        rec["reason"] = "vendor returned no frame"
        return rec
    try:
        dates = admitted_dates(raw.index, now_utc,
                               required_through=required_through, window=window)
        if not dates:
            rec["reason"] = ("no finished session in the frame's window; "
                             "nothing admitted")
            return rec
        # A column with nothing anywhere in the frame is an UNANSWERED
        # request for that ticker, not a run of missing bars. Recording it
        # per date would fabricate withdrawals for every delisted name.
        served_any = raw.notna().any(axis=0)
        unanswered = [str(c) for c in raw.columns[~served_any.to_numpy()]]
        cols = [str(c) for c in raw.columns[served_any.to_numpy()]]
        rec["unanswered_columns"] = sorted(unanswered)[:500]
        rec["unanswered_column_count"] = len(unanswered)
        rec["columns_observed"] = len(cols)
        if cols and len(dates) * len(cols) > max_cells:
            keep = max(1, int(max_cells // max(1, len(cols))))
            rec["truncated_dates"] = len(dates) - keep
            dates = dates[-keep:]
        rec["window_dates"] = list(dates)
        # Dates the rolling window has passed but an open cycle still needs.
        if also_admit:
            have = {str(pd.Timestamp(d).date()) for d in raw.index}
            reopened = sorted({str(d) for d in also_admit}
                              & have - set(dates))
            if reopened:
                rec["reopened_dates"] = reopened
                dates = sorted(set(dates) | set(reopened))
        rec["dates"] = dates
        sub = raw.loc[[pd.Timestamp(d) for d in dates], cols] if cols else None
        cells: dict[str, dict[str, float | None]] = {}
        for d in dates:
            row = {}
            if sub is not None:
                series = sub.loc[pd.Timestamp(d)]
                if isinstance(series, pd.DataFrame):      # duplicate index date
                    series = series.iloc[0]
                for c in cols:
                    try:
                        v = float(series[c])
                    except (TypeError, ValueError, KeyError):
                        v = float("nan")
                    row[c] = None if math.isnan(v) else v
            cells[d] = row
        rec["cells"] = cells
        rec["answered"] = True
        return rec
    except Exception as exc:  # noqa: BLE001 - a diagnostic never breaks a build
        rec["reason"] = f"observation failed: {type(exc).__name__}: {exc}"
        return rec


def _empty_state(panel: str) -> dict:
    return {"schema": SCHEMA, "panel": str(panel), "runs": 0,
            "runs_unanswered": 0, "runs_cache_skipped": 0, "cells": {},
            "counters": {"first_served": 0, "withdrawals": 0,
                         "restorations": 0, "restorations_identical": 0,
                         "restorations_changed": 0,
                         "restorations_changed_by_adjustment": 0,
                         "restorations_changed_ambiguous": 0,
                         "revisions": 0, "adjustments": 0, "ambiguous": 0,
                         "adjustment_columns": 0,
                         "cells_evicted_while_withheld": 0,
                         "cells_evicted_at_cap": 0,
                         "working_set_evictions": 0,
                         "prior_state_forgotten": 0,
                         "comparison_opportunities": 0,
                         "withdrawals_undetectable": 0,
                         "unanswered_ticker_observations": 0},
            "retention": {"window_sessions": WINDOW_SESSIONS,
                          "max_cells": MAX_CELLS,
                          "withheld_retention_days": WITHHELD_RETENTION_DAYS}}


def _change_shape(dates: list[str], comparable: list[str] | None) -> str:
    """``whole`` | ``prefix`` | ``scatter`` for the cells that changed.

    Recorded on the event for an investigator. It is NOT a cause: a
    retroactive split does produce a prefix, but so can a coincidence, and
    a prefix is not evidence of a corporate action - see
    ``_classify_changes``.
    """
    seen = sorted(comparable or dates)
    if len(dates) == len(seen):
        return "whole"
    return "prefix" if seen[:len(dates)] == sorted(dates) else "scatter"


def _classify_changes(pending: dict[str, list[tuple]],
                      comparable: dict[str, list[str]] | None = None,
                      adjustment_evidence=None) -> dict[str, str]:
    """{column: ADJUSTMENT | REVISION | AMBIGUOUS} for this run's changes.

    ``pending`` maps column -> [(date, old, new), ...] for every cell whose
    populated value changed, INCLUDING cells restored after a withdrawal.
    Restorations used to bypass this entirely, so a column halved by a split
    while seven of its eight bars were withheld came back as seven changed
    restorations and read as REVISION OBSERVED - a corporate action
    presented as evidence that the vendor restates prices.

    ``comparable`` maps column -> every date compared for it this run, which
    is what makes "contiguous prefix" decidable. Without it the rule falls
    back to the changed cells alone.

    A CONSTANT RATIO IS NOT EVIDENCE OF A CORPORATE ACTION (2026-09-16,
    third review). The previous version called a uniform ratio over a
    contiguous prefix an ADJUSTMENT and excluded it from the revision
    evidence. That is the shape a split produces, but the shape does not
    establish the cause: eight cells uniformly restated by 1.01, seven of
    them restored after a withdrawal, were classified as eight adjustments,
    zero revisions and a clean BOUNDED, NOT PROVEN - a vendor restating a
    whole column disappearing into the one category that is excluded from
    the question this module exists to answer.

    ADJUSTMENT is therefore assigned only when ``adjustment_evidence``
    - an independent source, a corporate-actions feed - says so for that
    column and ratio. NOTHING IS WIRED TO IT, so in the deployed
    configuration no change is ever classified as an adjustment. A uniform
    ratio whose cause cannot be established is AMBIGUOUS, which blocks any
    clean verdict without asserting a revision either. The shape (whole,
    prefix or scatter) is recorded for an investigator and decides nothing.
    """
    out: dict[str, str] = {}
    for col, items in pending.items():
        ratios = [new / old for _, old, new in items if old]
        if len(items) < ADJUSTMENT_MIN_CELLS or len(ratios) != len(items):
            out[col] = REVISION
            continue
        lo, hi = min(ratios), max(ratios)
        uniform = lo > 0 and (hi / lo - 1.0) <= ADJUSTMENT_REL_TOL
        if not uniform:
            out[col] = REVISION
            continue
        explained = False
        if adjustment_evidence is not None:
            try:
                explained = bool(adjustment_evidence(col, ratios[0], items))
            except Exception:  # noqa: BLE001 - evidence never breaks the build
                explained = False
        out[col] = ADJUSTMENT if explained else AMBIGUOUS
    return out


def update_state(state: dict | None, obs: dict,
                 now_utc: datetime | None = None) -> tuple[dict, list[dict]]:
    """Advance the per-cell state machine with one observation.

    States: ABSENT (never served at this date), SERVED, WITHHELD. The value
    the vendor last served is retained on a withheld cell, which is what
    makes the restored value comparable to the withdrawn one - the
    comparison the append-versus-revise question turns on.
    """
    now_utc = now_utc or _now()
    st = dict(state) if isinstance(state, dict) and state.get("cells") is not None \
        else _empty_state(obs.get("panel", "?"))
    st.setdefault("cells", {})
    st.setdefault("counters", _empty_state("?")["counters"])
    for k, v in _empty_state("?")["counters"].items():
        st["counters"].setdefault(k, v)
    st["panel"] = obs.get("panel", st.get("panel", "?"))
    st["runs"] = int(st.get("runs") or 0) + 1
    st["updated_utc"] = now_utc.isoformat(timespec="seconds")
    st["retention"] = {"window_sessions": int(obs.get("window") or WINDOW_SESSIONS),
                       "max_cells_per_observation": int(obs.get("max_cells")
                                                        or MAX_CELLS),
                       "max_retained_cells": MAX_RETAINED_CELLS,
                       "max_cells_hard": MAX_CELLS_HARD,
                       "withheld_retention_days": WITHHELD_RETENTION_DAYS}
    events: list[dict] = []
    # An entirely unanswered TICKER used to leave no trace at all: the run
    # counted as answered, the column was skipped, and the fact that the
    # vendor gave nothing for it was discarded. It is missing evidence and
    # is now retained as such - never as a withdrawal.
    st["counters"]["unanswered_ticker_observations"] += int(
        obs.get("unanswered_column_count") or 0)
    st["last_unanswered_columns"] = list(obs.get("unanswered_columns") or [])[:50]
    if not obs.get("answered"):
        st["runs_unanswered"] = int(st.get("runs_unanswered") or 0) + 1
        st["last_unanswered_reason"] = str(obs.get("reason") or "")[:200]
        return st, events

    cells = st["cells"]
    # What the window held last run. A cell whose date was observed then but
    # is absent from the state now was EVICTED, not never-served, and its
    # prior value is gone: a withdrawal of it cannot be detected.
    prior_window = set(st.get("last_window_dates") or ())
    # A comparison OPPORTUNITY needs prior state for THAT ticker on THAT
    # date, not merely a date the window carried (2026-09-16, fifth review).
    # A ticker the vendor did not answer for on run one holds no prior
    # value, so its first answer on run two is a first sighting - counting
    # it as prior state lost reported four cells forgotten that were never
    # held, and dragged coverage to 0.5 on a panel that had lost nothing.
    prior_columns = set(st.get("last_observed_columns") or ())
    # Per-run figures as well as lifetime ones: a lifetime ratio dilutes a
    # loss that happened weeks ago, and an operator asking "is detection
    # sound NOW" needs the run in front of them. Neither mixes units.
    run_stats = {"opportunities": 0, "forgotten": 0}
    stamp = now_utc.isoformat(timespec="seconds")
    pending: dict[str, list[tuple]] = {}
    comparable: dict[str, list[str]] = {}
    for d, row in (obs.get("cells") or {}).items():
        for col, value in row.items():
            key = f"{d}|{col}"
            cur = cells.get(key)
            # Every cell whose date the window carried last run is a
            # COMPARISON OPPORTUNITY, whether or not the prior state
            # survived to be compared against. That is the denominator
            # detection coverage needs; see cycle_summary.
            if d in prior_window and col in prior_columns:
                st["counters"]["comparison_opportunities"] += 1
                run_stats["opportunities"] += 1
            if cur is None:
                forgotten = d in prior_window and col in prior_columns
                if forgotten:
                    st["counters"]["prior_state_forgotten"] += 1
                    run_stats["forgotten"] += 1
                    if value is None:
                        # We held this cell last run and no longer do, so
                        # "missing now" cannot be told from "never served".
                        st["counters"]["withdrawals_undetectable"] += 1
                cells[key] = ({"status": "served", "value": value,
                               "first_seen": stamp, "last_served": stamp,
                               "cycles": 0, **({"prior_forgotten": True}
                                               if forgotten else {})}
                              if value is not None else
                              {"status": "absent", "value": None,
                               "first_seen": stamp, "cycles": 0,
                               **({"prior_forgotten": True} if forgotten else {})})
                if value is not None and not forgotten:
                    st["counters"]["first_served"] += 1
                continue
            status = cur.get("status")
            if value is None:
                if status == "served":
                    cur["status"] = "withheld"
                    cur["withheld_since"] = stamp
                    st["counters"]["withdrawals"] += 1
                    events.append({"asof": stamp, "panel": st["panel"],
                                   "kind": "withdrawal", "date": d,
                                   "column": col, "old": cur.get("value"),
                                   "new": None})
                continue
            if status == "withheld":
                old = cur.get("value")
                changed = not (isinstance(old, (int, float))
                               and _same(float(old), float(value)))
                cur.update(status="served", value=value, last_served=stamp,
                           cycles=int(cur.get("cycles") or 0) + 1)
                cur.pop("withheld_since", None)
                st["counters"]["restorations"] += 1
                st["counters"]["restorations_changed" if changed
                               else "restorations_identical"] += 1
                if changed and isinstance(old, (int, float)):
                    # A restored value that CHANGED is a value change like
                    # any other and goes through the same classifier: a
                    # split does not stop being a split because the bars it
                    # rescaled were withheld in between.
                    pending.setdefault(col, []).append(
                        (d, float(old), float(value), "restored"))
                if isinstance(old, (int, float)):
                    comparable.setdefault(col, []).append(d)
                events.append({"asof": stamp, "panel": st["panel"],
                               "kind": "restoration", "date": d, "column": col,
                               "old": old, "new": value, "changed": changed})
            elif status == "absent":
                cur.update(status="served", value=value, last_served=stamp)
                st["counters"]["first_served"] += 1
            else:                                   # served -> served
                old = cur.get("value")
                if isinstance(old, (int, float)):
                    comparable.setdefault(col, []).append(d)
                    if not _same(float(old), float(value)):
                        pending.setdefault(col, []).append(
                            (d, float(old), float(value), "served"))
                cur["value"] = value
                cur["last_served"] = stamp

    verdicts = _classify_changes(
        {c: [(d, o, n) for d, o, n, _ in items] for c, items in pending.items()},
        comparable)
    for col, items in pending.items():
        verdict = verdicts.get(col, REVISION)
        if verdict == ADJUSTMENT:
            st["counters"]["adjustment_columns"] += 1
        for d, old, new, origin in items:
            if verdict == ADJUSTMENT:
                st["counters"]["adjustments"] += 1
                if origin == "restored":
                    st["counters"]["restorations_changed_by_adjustment"] += 1
            elif verdict == AMBIGUOUS:
                st["counters"]["ambiguous"] += 1
                if origin == "restored":
                    st["counters"]["restorations_changed_ambiguous"] += 1
            elif origin == "served":
                # A restored change is already counted as
                # restorations_changed; counting it here too would double it.
                st["counters"]["revisions"] += 1
            events.append({"asof": stamp, "panel": st["panel"],
                           "kind": verdict, "origin": origin, "date": d,
                           "column": col, "old": old, "new": new,
                           "shape": _change_shape(
                               [x[0] for x in items], comparable.get(col)),
                           "rel_change": (abs(new - old) / abs(old)) if old else None})

    # --- eviction, and the cycles it costs -------------------------------
    admitted = set(obs.get("window_dates") or obs.get("dates") or ())
    if admitted:
        floor = min(admitted)
        horizon = now_utc - timedelta(days=WITHHELD_RETENTION_DAYS)
        for key in [k for k in cells if k.split("|", 1)[0] < floor]:
            cur = cells[key]
            if cur.get("status") == "withheld":
                since = str(cur.get("withheld_since") or "")
                try:
                    keep = datetime.fromisoformat(since) > horizon
                except ValueError:
                    keep = False
                if keep:
                    continue
                st["counters"]["cells_evicted_while_withheld"] += 1
            cells.pop(key, None)

    # --- the LAYERED cap on accumulated state (2026-09-16) ----------------
    # The working set - every cell of the current observation window - is
    # never evicted, because the next run must compare against it and a
    # forgotten cell silently disables withdrawal detection for that cell.
    # Retained out-of-window cycles have their own budget below.
    window = set(obs.get("window_dates") or obs.get("dates") or ())

    def _rank(item):
        key, cur = item
        status = cur.get("status")
        tier = 0 if status == "absent" else (1 if status == "served" else 2)
        return (tier, key)

    retained = {k: v for k, v in cells.items()
                if k.split("|", 1)[0] not in window}
    if len(retained) > MAX_RETAINED_CELLS:
        for key, cur in sorted(retained.items(), key=_rank):
            if len(retained) <= MAX_RETAINED_CELLS:
                break
            if cur.get("status") == "withheld":
                st["counters"]["cells_evicted_while_withheld"] += 1
            st["counters"]["cells_evicted_at_cap"] += 1
            cells.pop(key, None)
            retained.pop(key, None)

    # The backstop. Reaching it means the working set itself is being cut,
    # so detection is incomplete from here on and the summary must say so.
    if len(cells) > MAX_CELLS_HARD:
        for key, cur in sorted(cells.items(), key=_rank):
            if len(cells) <= MAX_CELLS_HARD:
                break
            if cur.get("status") == "withheld":
                st["counters"]["cells_evicted_while_withheld"] += 1
            st["counters"]["cells_evicted_at_cap"] += 1
            st["counters"]["working_set_evictions"] += 1
            cells.pop(key, None)

    # What the NEXT run needs in order to tell "never served" from "we used
    # to know and forgot".
    st["last_window_dates"] = sorted(window)
    # The columns that ANSWERED this run. An unanswered ticker holds no
    # prior state, so it offers no comparison next run.
    st["last_observed_columns"] = sorted(
        {c for row in (obs.get("cells") or {}).values() for c in row})
    st["last_run"] = run_stats
    return st, events


def record_cache_skip(panel: str, root: Path,
                      now_utc: datetime | None = None) -> bool:
    """Record that this run reused the cache and made NO observation.

    ``download_prices`` returns early when the cache already covers the
    request, so no download happens and the vendor is never asked. The
    summary used to carry a generic footnote about this; it now carries a
    MEASURED count per panel, which is the difference between "we looked and
    saw nothing" and "we did not look". Never raises.
    """
    try:
        now_utc = now_utc or _now()
        sp = state_path(root, panel)
        try:
            doc = json.loads(sp.read_text(encoding="utf-8"))
            st = doc if isinstance(doc, dict) else _empty_state(panel)
        except (OSError, ValueError):
            st = _empty_state(panel)
        st.setdefault("cells", {})
        st.setdefault("counters", _empty_state(panel)["counters"])
        st["panel"] = panel
        st["runs_cache_skipped"] = int(st.get("runs_cache_skipped") or 0) + 1
        st["last_cache_skip_utc"] = now_utc.isoformat(timespec="seconds")
        return bool(_atomic_write(sp, json.dumps(st, sort_keys=True)))
    except Exception:  # noqa: BLE001
        return False


def state_path(root: Path, panel: str) -> Path:
    return Path(root) / "logs" / "vendor_state" / f"{panel}.json"


def events_path(root: Path) -> Path:
    return Path(root) / "logs" / "vendor_events.jsonl"


def record_vendor_observation(raw, *, panel: str, root: Path,
                              source: str = YFINANCE,
                              required_through=None,
                              now_utc: datetime | None = None,
                              window: int = WINDOW_SESSIONS,
                              max_cells: int = MAX_CELLS) -> dict | None:
    """Observe, advance the state machine, persist. Never raises."""
    try:
        now_utc = now_utc or _now()
        sp = state_path(root, panel)
        prior = None
        try:
            doc = json.loads(sp.read_text(encoding="utf-8"))
            prior = doc if isinstance(doc, dict) else None
        except (OSError, ValueError):
            prior = None
        # Read the state FIRST, so the dates of any still-open cycle can be
        # re-admitted even though the rolling window has passed them.
        open_dates = {k.split("|", 1)[0]
                      for k, v in ((prior or {}).get("cells") or {}).items()
                      if isinstance(v, dict) and v.get("status") == "withheld"}
        obs = observe_vendor_frame(raw, panel=panel, source=source,
                                   now_utc=now_utc,
                                   required_through=required_through,
                                   window=window, max_cells=max_cells,
                                   also_admit=open_dates)
        st, events = update_state(prior, obs, now_utc)
        _atomic_write(sp, json.dumps(st, sort_keys=True))
        # ONE rewrite per panel per run. Appending event by event rewrote
        # the whole ledger each time, so a busy run paid O(events x file).
        _append_bounded(events_path(root), events, EVENT_CAP)
        return {"panel": panel, "answered": obs.get("answered"),
                "dates": obs.get("dates"),
                "reopened": obs.get("reopened_dates"), "events": len(events),
                "counters": st.get("counters")}
    except Exception:  # noqa: BLE001 - a diagnostic never breaks a build
        return None


# ---------------------------------------------------------------------------
# 2. Cache changes - the secondary diagnostic
# ---------------------------------------------------------------------------
def diff_frames(old, new, *, old_sidecar: dict | None = None,
                new_sidecar: dict | None = None,
                sample_cap: int = SAMPLE_CAP,
                window_sessions: int | None = CACHE_DIFF_SESSIONS) -> dict:
    """Categorise every cell change between two versions of one CACHE.

    READ THIS AS CACHE BEHAVIOUR, NOT VENDOR BEHAVIOUR. It runs after the
    cell-preservation merge, so a cell the vendor withdrew still carries
    its preserved value here and reads as unchanged. The vendor question is
    answered by the observation records above.

    Categories:
      fills            missing -> priced
      withdrawals      priced -> missing, at a cell present on both sides
      removed_cells    priced -> GONE, because the row or column itself
                       left the frame. Counted separately and never
                       silently dropped, which is what comparing only the
                       intersection used to do.
      revisions        priced -> differently priced, SAME resolved basis
      adjustments      a whole column re-scaled by one ratio
      basis_changes    the column's resolved source moved, or cannot be
                       established; withheld from revisions on purpose
    """
    now = _now().isoformat(timespec="seconds")
    if old is None or not isinstance(new, pd.DataFrame):
        return {"kind": "cache_diff", "asof": now, "comparable": False,
                "reason": "no incumbent cache to compare against",
                "fills": 0, "withdrawals": 0, "removed_cells": 0,
                "revisions": 0, "adjustments": 0, "ambiguous": 0,
                "basis_changes": 0, "samples": {}}
    try:
        old_b = resolve_column_basis(old_sidecar)
        new_b = resolve_column_basis(new_sidecar)
        all_rows = old.index.intersection(new.index)
        # BOUNDED per-cell walk. Shape changes and lost populated cells below
        # are still measured over the WHOLE frame; only this loop is capped,
        # because 2309 x 727 took 16.4 seconds for one panel.
        rows = (all_rows.sort_values()[-int(window_sessions):]
                if window_sessions and len(all_rows) > window_sessions
                else all_rows)
        cols = old.columns.intersection(new.columns)
        counts = {"fills": 0, "withdrawals": 0, "removed_cells": 0,
                  "revisions": 0, "adjustments": 0, "ambiguous": 0,
                  "basis_changes": 0}
        samples: dict[str, list] = {k: [] for k in counts}

        # Populated cells lost with their row or column. A whole vanished
        # column is the loudest possible version of "served, then not",
        # and the intersection-only diff scored it zero.
        lost_rows = old.index.difference(new.index)
        lost_cols = old.columns.difference(new.columns)
        if len(lost_cols):
            counts["removed_cells"] += int(old[lost_cols].notna().sum().sum())
        if len(lost_rows):
            kept = old.columns.intersection(new.columns)
            if len(kept):
                counts["removed_cells"] += int(
                    old.loc[lost_rows, kept].notna().sum().sum())
        for c in list(lost_cols)[:sample_cap]:
            samples["removed_cells"].append({"column": str(c),
                                             "reason": "column removed"})
        for r in list(lost_rows)[:sample_cap]:
            samples["removed_cells"].append({"date": str(pd.Timestamp(r).date()),
                                             "reason": "row removed"})

        pending: dict[str, list[tuple]] = {}
        basis_pairs: dict[str, tuple[str, str]] = {}
        for col in cols:
            ob, nb = basis_of(old_b, str(col)), basis_of(new_b, str(col))
            basis_pairs[str(col)] = (ob, nb)
            o = old.loc[rows, col]
            n = new.loc[rows, col]
            if isinstance(o, pd.DataFrame):
                o = o.iloc[:, 0]
            if isinstance(n, pd.DataFrame):
                n = n.iloc[:, 0]
            for ts in rows:
                try:
                    a = float(o.loc[ts])
                    b = float(n.loc[ts])
                except (TypeError, ValueError, KeyError):
                    continue
                a_na, b_na = math.isnan(a), math.isnan(b)
                if a_na and b_na:
                    continue
                if a_na:
                    kind = "fills"
                elif b_na:
                    kind = "withdrawals"
                elif _same(a, b):
                    continue
                else:
                    if ob != nb or ob == UNKNOWN_BASIS:
                        kind = "basis_changes"
                    else:
                        pending.setdefault(str(col), []).append(
                            (str(pd.Timestamp(ts).date()), a, b))
                        continue
                counts[kind] += 1
                if len(samples[kind]) < sample_cap:
                    samples[kind].append({"date": str(pd.Timestamp(ts).date()),
                                          "column": str(col),
                                          "old": None if a_na else a,
                                          "new": None if b_na else b,
                                          "old_basis": ob, "new_basis": nb})
        comparable = {c: sorted(str(pd.Timestamp(t).date()) for t in rows)
                      for c in pending}
        verdicts = _classify_changes(pending, comparable)
        for col, items in pending.items():
            verdict = verdicts.get(col, REVISION)
            kind = {ADJUSTMENT: "adjustments", AMBIGUOUS: "ambiguous"}.get(
                verdict, "revisions")
            counts[kind] += len(items)
            ob, nb = basis_pairs.get(col, (UNKNOWN_BASIS, UNKNOWN_BASIS))
            for d, a, b in items:
                if len(samples[kind]) < sample_cap:
                    samples[kind].append({"date": d, "column": col,
                                          "old": a, "new": b,
                                          "old_basis": ob, "new_basis": nb,
                                          "rel_change": (abs(b - a) / abs(a))
                                          if a else None})
        return {
            "kind": "cache_diff", "asof": now, "comparable": True,
            "caveat": ("post-preservation: a raw vendor withdrawal may be "
                       "masked by the cell-preservation merge"),
            "rows_compared": int(len(rows)),
            "rows_in_common": int(len(all_rows)),
            "cell_walk_window_sessions": (int(window_sessions)
                                          if window_sessions else None),
            "columns_compared": int(len(cols)),
            "rows_added": int(len(new.index.difference(old.index))),
            "rows_removed": int(len(lost_rows)),
            "columns_added": int(len(new.columns.difference(old.columns))),
            "columns_removed": int(len(lost_cols)),
            "old_index_end": (str(pd.Timestamp(old.index.max()).date())
                              if len(old.index) else None),
            "new_index_end": (str(pd.Timestamp(new.index.max()).date())
                              if len(new.index) else None),
            "samples": samples, **counts}
    except Exception as exc:  # noqa: BLE001
        return {"kind": "cache_diff", "asof": now, "comparable": False,
                "reason": f"diff failed: {type(exc).__name__}: {exc}",
                "fills": 0, "withdrawals": 0, "removed_cells": 0,
                "revisions": 0, "adjustments": 0, "ambiguous": 0,
                "basis_changes": 0, "samples": {}}


def cache_ledger_path(root: Path) -> Path:
    return Path(root) / "logs" / "cache_changes.jsonl"


def capture_cache_change(cache_path: Path, new, new_sidecar: dict | None,
                         *, ledger: Path | None = None, panel: str = "",
                         max_records: int = 400) -> dict | None:
    """Diff the INCUMBENT cache at ``cache_path`` against ``new`` and append
    the verdict to the cache-change ledger. Never raises."""
    try:
        old = pd.read_parquet(cache_path) if Path(cache_path).exists() else None
    except Exception:  # noqa: BLE001
        old = None
    try:
        sc = Path(str(cache_path).replace(".parquet", ".source.json"))
        old_sidecar = json.loads(sc.read_text(encoding="utf-8")) if sc.exists() else None
    except (OSError, ValueError):
        old_sidecar = None
    rec = diff_frames(old, new, old_sidecar=old_sidecar, new_sidecar=new_sidecar)
    rec["panel"] = panel or Path(cache_path).stem
    if ledger is None:
        ledger = cache_ledger_path(Path(cache_path).resolve().parent.parent)
    _append_bounded(Path(ledger), rec, max_records)
    return rec


# ---------------------------------------------------------------------------
# 3. The read
# ---------------------------------------------------------------------------
def cycle_summary(root: Path) -> dict:
    """What the vendor observations support, and what they do not.

    Reports per panel because the claim under test is about European panels
    and a pooled count across nineteen would hide it, and states coverage,
    missing evidence and retention limits alongside every count. The verdict
    refuses to read zero cycles, or zero revisions within them, as evidence
    of append-only behaviour.
    """
    out: dict = {"panels": {}, "totals": {}, "as_at": _now().isoformat(timespec="seconds")}
    d = Path(root) / "logs" / "vendor_state"
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    totals = {"runs": 0, "runs_unanswered": 0, "runs_cache_skipped": 0,
              "withdrawals": 0, "restorations": 0, "restorations_identical": 0,
              "restorations_changed": 0,
              "restorations_changed_by_adjustment": 0,
              "restorations_changed_ambiguous": 0,
              "revisions": 0, "adjustments": 0, "ambiguous": 0,
              "cells_evicted_while_withheld": 0, "cells_evicted_at_cap": 0,
              "working_set_evictions": 0, "prior_state_forgotten": 0,
              "comparison_opportunities": 0, "withdrawals_undetectable": 0,
              "unanswered_ticker_observations": 0, "open_withdrawals": 0}
    for f in files:
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["panels"][f.stem] = {"error": "state file unreadable"}
            continue
        if not isinstance(st, dict):
            out["panels"][f.stem] = {"error": "state file unreadable"}
            continue
        c = dict(st.get("counters") or {})
        open_w = sum(1 for v in (st.get("cells") or {}).values()
                     if isinstance(v, dict) and v.get("status") == "withheld")
        row = {"runs": int(st.get("runs") or 0),
               "runs_unanswered": int(st.get("runs_unanswered") or 0),
               "runs_cache_skipped": int(st.get("runs_cache_skipped") or 0),
               "cells_tracked": len(st.get("cells") or {}),
               "open_withdrawals": open_w,
               "complete_cycles": int(c.get("restorations") or 0),
               "last_observation_utc": st.get("updated_utc"),
               "last_cache_skip_utc": st.get("last_cache_skip_utc"),
               "retention": st.get("retention"),
               **{k: int(c.get(k) or 0) for k in
                  ("withdrawals", "restorations", "restorations_identical",
                   "restorations_changed",
                   "restorations_changed_by_adjustment",
                   "restorations_changed_ambiguous",
                   "revisions", "adjustments", "ambiguous",
                   "cells_evicted_while_withheld", "cells_evicted_at_cap",
                   "working_set_evictions", "prior_state_forgotten",
                   "comparison_opportunities", "withdrawals_undetectable",
                   "unanswered_ticker_observations")}}
        # Measured, not asserted: of the runs this panel took part in, how
        # many actually asked the vendor anything.
        firings = row["runs"] + row["runs_cache_skipped"]
        # FIRING coverage: how often the vendor was asked at all.
        row["observation_coverage"] = (round(row["runs"] / firings, 4)
                                       if firings else None)
        # DETECTION coverage: of the cells re-observed, how many still had
        # the prior state a withdrawal must be measured against. A different
        # question, and an eviction count alone does not answer it.
        # DETECTION coverage: of the comparison OPPORTUNITIES this panel has
        # had - cells whose date the previous window carried - how many still
        # had the prior state a withdrawal must be measured against.
        #
        # Both terms are cumulative (2026-09-16, fourth review). The previous
        # denominator added a lifetime counter to the CURRENT cell count, so
        # observing four new cells moved coverage from 0.50 to 0.60 without
        # restoring anything that had been lost. A ratio whose denominator
        # grows for an unrelated reason is not a coverage measure.
        opportunities = row["comparison_opportunities"]
        row["detection_coverage"] = (
            round(1 - row["prior_state_forgotten"] / opportunities, 4)
            if opportunities else None)
        last = st.get("last_run") or {}
        last_opps = int(last.get("opportunities") or 0)
        row["detection_coverage_last_run"] = (
            round(1 - int(last.get("forgotten") or 0) / last_opps, 4)
            if last_opps else None)
        out["panels"][f.stem] = row
        for k in totals:
            totals[k] += int(row.get(k) or 0)
    out["totals"] = totals
    out["retention_limits"] = {"window_sessions": WINDOW_SESSIONS,
                               "max_cells_per_panel": MAX_CELLS,
                               "withheld_retention_days": WITHHELD_RETENTION_DAYS,
                               "event_cap": EVENT_CAP}
    firings = totals["runs"] + totals["runs_cache_skipped"]
    out["missing_evidence"] = {
        "panels_with_state": len(files),
        "runs_the_vendor_did_not_answer": totals["runs_unanswered"],
        "runs_served_from_cache_no_observation": totals["runs_cache_skipped"],
        "observation_coverage": (round(totals["runs"] / firings, 4)
                                 if firings else None),
        "unanswered_ticker_observations":
            totals["unanswered_ticker_observations"],
        "cycles_lost_to_retention": totals["cells_evicted_while_withheld"],
        "cells_evicted_at_cap": totals["cells_evicted_at_cap"],
        "working_set_evictions": totals["working_set_evictions"],
        "cells_whose_prior_state_was_forgotten":
            totals["prior_state_forgotten"],
        "comparison_opportunities": totals["comparison_opportunities"],
        "detection_coverage": (
            round(1 - totals["prior_state_forgotten"]
                  / totals["comparison_opportunities"], 4)
            if totals["comparison_opportunities"] else None),
        "withdrawals_that_could_not_be_detected":
            totals["withdrawals_undetectable"],
        "withdrawals_still_open": totals["open_withdrawals"],
        "ambiguous_changes_unresolved": totals["ambiguous"],
        "note": ("a run served entirely from cache performs no download and "
                 "therefore leaves no observation; observation_coverage is "
                 "the measured share of firings that asked the vendor"),
    }
    cycles = totals["restorations"]
    # A restoration that came back changed BY A SPLIT is not evidence that
    # the vendor restates prices. Only the unexplained remainder is.
    unexplained = max(0, totals["restorations_changed"]
                      - totals["restorations_changed_by_adjustment"]
                      - totals["restorations_changed_ambiguous"])
    # Detection was incomplete if prior state was forgotten, so "no
    # withdrawal was seen" cannot mean "none happened".
    blind = (totals["withdrawals_undetectable"] or totals["working_set_evictions"]
             or totals["prior_state_forgotten"])
    if not files:
        verdict = ("NO EVIDENCE: no vendor observation has been recorded. "
                   "The append-versus-revise question is untouched.")
    elif unexplained or totals["revisions"]:
        # Two distinct findings, stated separately. A changed restoration
        # answers the question directly; a same-basis revision of a
        # populated cell shows the series is not append-only even where no
        # withdrawal was involved. An ADJUSTMENT is neither, and never
        # reaches this branch.
        verdict = (f"REVISION OBSERVED: {unexplained} of {cycles} complete "
                   f"cycle(s) came back with a different value that no column "
                   f"re-adjustment explains, and {totals['revisions']} "
                   f"same-basis revision(s) of populated cells were seen "
                   f"outside a cycle. Prices already served are not immutable.")
    elif totals["ambiguous"] or blind:
        parts = []
        if totals["ambiguous"]:
            parts.append(f"{totals['ambiguous']} value change(s) carry a single "
                         f"ratio whose cause no independent evidence explains, "
                         f"so neither a corporate action nor a restatement is "
                         f"established")
        if blind:
            parts.append(f"detection was incomplete: the prior state of "
                         f"{totals['prior_state_forgotten']} cell(s) was "
                         f"forgotten and {totals['withdrawals_undetectable']} "
                         f"withdrawal(s) could not be detected")
        verdict = (f"UNRESOLVED: {'; '.join(parts)}. {cycles} complete cycle(s) "
                   f"observed. This is not a clean reading.")
    elif cycles == 0:
        verdict = (f"INSUFFICIENT EVIDENCE: {totals['withdrawals']} withdrawal(s) "
                   f"observed and {totals['open_withdrawals']} still open, but no "
                   f"served -> withheld -> restored cycle has completed. Zero "
                   f"cycles is not evidence of append-only behaviour. "
                   f"{totals['adjustments']} whole-column adjustment(s) are "
                   f"excluded from the revision evidence by design.")
    else:
        verdict = (f"BOUNDED, NOT PROVEN: {cycles} complete cycle(s), none of "
                   f"which changed the restored value other than by a column "
                   f"re-adjustment, and no same-basis revision of a populated "
                   f"cell. This bounds how often restoration revises; it does "
                   f"not establish that it never does.")
    out["verdict"] = verdict
    return out


def summarise(ledger: Path, *, since: str | None = None) -> dict:
    """Roll the CACHE-CHANGE ledger up, per panel.

    Secondary to ``cycle_summary``: see the caveat on ``diff_frames``.
    """
    out: dict[str, dict] = {}
    try:
        lines = Path(ledger).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if since and str(rec.get("asof", "")) < since:
            continue
        if not rec.get("comparable"):
            continue
        p = out.setdefault(rec.get("panel", "?"),
                           {"runs": 0, "fills": 0, "withdrawals": 0,
                            "removed_cells": 0, "revisions": 0,
                            "adjustments": 0, "ambiguous": 0,
                            "basis_changes": 0})
        p["runs"] += 1
        for k in ("fills", "withdrawals", "removed_cells", "revisions",
                  "adjustments", "ambiguous", "basis_changes"):
            p[k] += int(rec.get(k) or 0)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(root))
    ap.add_argument("--cache-ledger", default=None,
                    help="Also roll up the secondary cache-change ledger.")
    ap.add_argument("--since", default=None, help="ISO timestamp lower bound")
    args = ap.parse_args(argv)
    report = {"vendor_observations": cycle_summary(Path(args.repo))}
    if args.cache_ledger is not None:
        report["cache_changes"] = summarise(Path(args.cache_ledger or
                                                 cache_ledger_path(Path(args.repo))),
                                            since=args.since)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
