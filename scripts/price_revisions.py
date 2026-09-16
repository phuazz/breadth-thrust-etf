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
# Hard ceiling on cells held per panel. Oldest admitted dates are dropped
# first, and the drop is recorded.
MAX_CELLS = 6000
# A cell left WITHHELD is kept past the window for this long, so a cycle
# that spans the window boundary is not lost to eviction. One evicted
# while still withheld is counted: it is a cycle we will never close.
WITHHELD_RETENTION_DAYS = 30
# Event ledger tail.
EVENT_CAP = 2000
# Cache-diff per-cell detail. Counts are exact regardless.
SAMPLE_CAP = 40

# Relative tolerance for "the same price". Parquet round-trips float64
# exactly, so an unchanged cell compares equal bit for bit and this never
# fires on one. It exists to stop a last-place difference from a vendor's
# own float formatting reading as a revision.
REL_TOL = 1e-12
# A whole-column re-adjustment shows up as a CONSTANT ratio across every
# historical cell. Three cells is the least that can distinguish a
# proportional re-adjustment from coincidence; the tolerance is loose
# enough to survive float64 rounding on a ratio and far tighter than any
# real price move.
ADJUSTMENT_MIN_CELLS = 3
ADJUSTMENT_REL_TOL = 1e-6

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


def _append_bounded(path: Path, record: dict, cap: int) -> bool:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if p.exists():
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                     if ln.strip()][-(cap - 1):]
        lines.append(json.dumps(record))
        return _atomic_write(p, "\n".join(lines) + "\n")
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
                         max_cells: int = MAX_CELLS) -> dict:
    """One bounded, provenanced observation of what the vendor SERVED.

    Takes the raw download frame, before any preservation, overlay or tail
    processing. ``answered`` separates a request that came back with
    nothing at all - a dead endpoint, a timeout swallowed upstream - from a
    vendor that answered and declared specific bars missing. The two cannot
    be conflated: the first is our failure and the second is evidence.
    """
    now_utc = now_utc or _now()
    rec = {"schema": SCHEMA, "panel": str(panel), "source": str(source),
           "asof": now_utc.isoformat(timespec="seconds"),
           "answered": False, "dates": [], "cells": {},
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
            "runs_unanswered": 0, "cells": {},
            "counters": {"first_served": 0, "withdrawals": 0,
                         "restorations": 0, "restorations_identical": 0,
                         "restorations_changed": 0, "revisions": 0,
                         "adjustments": 0, "adjustment_columns": 0,
                         "cells_evicted_while_withheld": 0},
            "retention": {"window_sessions": WINDOW_SESSIONS,
                          "max_cells": MAX_CELLS,
                          "withheld_retention_days": WITHHELD_RETENTION_DAYS}}


def _classify_revisions(pending: dict[str, list[tuple]]) -> dict[str, str]:
    """{column: 'adjustment'|'revision'} for this run's value changes.

    A vendor re-adjusting a column applies ONE ratio to every historical
    cell in it. That is a basis event for the series, not a restatement of
    individual closes, and pooling the two would let a single split
    dominate the evidence the module exists to gather.
    """
    out: dict[str, str] = {}
    for col, items in pending.items():
        ratios = [new / old for _, old, new in items if old]
        if (len(items) >= ADJUSTMENT_MIN_CELLS and len(ratios) == len(items)
                and ratios):
            lo, hi = min(ratios), max(ratios)
            if lo > 0 and (hi / lo - 1.0) <= ADJUSTMENT_REL_TOL:
                out[col] = "adjustment"
                continue
        out[col] = "revision"
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
                       "max_cells": int(obs.get("max_cells") or MAX_CELLS),
                       "withheld_retention_days": WITHHELD_RETENTION_DAYS}
    events: list[dict] = []
    if not obs.get("answered"):
        st["runs_unanswered"] = int(st.get("runs_unanswered") or 0) + 1
        st["last_unanswered_reason"] = str(obs.get("reason") or "")[:200]
        return st, events

    cells = st["cells"]
    stamp = now_utc.isoformat(timespec="seconds")
    pending: dict[str, list[tuple]] = {}
    for d, row in (obs.get("cells") or {}).items():
        for col, value in row.items():
            key = f"{d}|{col}"
            cur = cells.get(key)
            if cur is None:
                cells[key] = ({"status": "served", "value": value,
                               "first_seen": stamp, "last_served": stamp,
                               "cycles": 0}
                              if value is not None else
                              {"status": "absent", "value": None,
                               "first_seen": stamp, "cycles": 0})
                if value is not None:
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
                events.append({"asof": stamp, "panel": st["panel"],
                               "kind": "restoration", "date": d, "column": col,
                               "old": old, "new": value, "changed": changed})
            elif status == "absent":
                cur.update(status="served", value=value, last_served=stamp)
                st["counters"]["first_served"] += 1
            else:                                   # served -> served
                old = cur.get("value")
                if isinstance(old, (int, float)) and not _same(float(old),
                                                               float(value)):
                    pending.setdefault(col, []).append((d, float(old),
                                                        float(value)))
                cur["value"] = value
                cur["last_served"] = stamp

    verdicts = _classify_revisions(pending)
    for col, items in pending.items():
        kind = verdicts.get(col, "revision")
        if kind == "adjustment":
            st["counters"]["adjustment_columns"] += 1
        for d, old, new in items:
            st["counters"]["adjustments" if kind == "adjustment"
                           else "revisions"] += 1
            events.append({"asof": stamp, "panel": st["panel"], "kind": kind,
                           "date": d, "column": col, "old": old, "new": new,
                           "rel_change": (abs(new - old) / abs(old)) if old else None})

    # --- eviction, and the cycles it costs -------------------------------
    admitted = set(obs.get("dates") or [])
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
    return st, events


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
        obs = observe_vendor_frame(raw, panel=panel, source=source,
                                   now_utc=now_utc,
                                   required_through=required_through,
                                   window=window, max_cells=max_cells)
        sp = state_path(root, panel)
        prior = None
        try:
            doc = json.loads(sp.read_text(encoding="utf-8"))
            prior = doc if isinstance(doc, dict) else None
        except (OSError, ValueError):
            prior = None
        st, events = update_state(prior, obs, now_utc)
        _atomic_write(sp, json.dumps(st, sort_keys=True))
        for ev in events:
            _append_bounded(events_path(root), ev, EVENT_CAP)
        return {"panel": panel, "answered": obs.get("answered"),
                "dates": obs.get("dates"), "events": len(events),
                "counters": st.get("counters")}
    except Exception:  # noqa: BLE001 - a diagnostic never breaks a build
        return None


# ---------------------------------------------------------------------------
# 2. Cache changes - the secondary diagnostic
# ---------------------------------------------------------------------------
def diff_frames(old, new, *, old_sidecar: dict | None = None,
                new_sidecar: dict | None = None,
                sample_cap: int = SAMPLE_CAP) -> dict:
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
                "revisions": 0, "adjustments": 0, "basis_changes": 0,
                "samples": {}}
    try:
        old_b = resolve_column_basis(old_sidecar)
        new_b = resolve_column_basis(new_sidecar)
        rows = old.index.intersection(new.index)
        cols = old.columns.intersection(new.columns)
        counts = {"fills": 0, "withdrawals": 0, "removed_cells": 0,
                  "revisions": 0, "adjustments": 0, "basis_changes": 0}
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
        verdicts = _classify_revisions(pending)
        for col, items in pending.items():
            kind = "adjustments" if verdicts.get(col) == "adjustment" else "revisions"
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
                "revisions": 0, "adjustments": 0, "basis_changes": 0,
                "samples": {}}


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
    totals = {"runs": 0, "runs_unanswered": 0, "withdrawals": 0,
              "restorations": 0, "restorations_identical": 0,
              "restorations_changed": 0, "revisions": 0, "adjustments": 0,
              "cells_evicted_while_withheld": 0, "open_withdrawals": 0}
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
               "cells_tracked": len(st.get("cells") or {}),
               "open_withdrawals": open_w,
               "complete_cycles": int(c.get("restorations") or 0),
               "retention": st.get("retention"),
               **{k: int(c.get(k) or 0) for k in
                  ("withdrawals", "restorations", "restorations_identical",
                   "restorations_changed", "revisions", "adjustments",
                   "cells_evicted_while_withheld")}}
        out["panels"][f.stem] = row
        for k in totals:
            totals[k] += int(row.get(k) or 0)
    out["totals"] = totals
    out["retention_limits"] = {"window_sessions": WINDOW_SESSIONS,
                               "max_cells_per_panel": MAX_CELLS,
                               "withheld_retention_days": WITHHELD_RETENTION_DAYS,
                               "event_cap": EVENT_CAP}
    out["missing_evidence"] = {
        "panels_with_state": len(files),
        "runs_the_vendor_did_not_answer": totals["runs_unanswered"],
        "cycles_lost_to_retention": totals["cells_evicted_while_withheld"],
        "withdrawals_still_open": totals["open_withdrawals"],
        "note": ("a run served entirely from cache performs no download and "
                 "therefore leaves no observation"),
    }
    cycles = totals["restorations"]
    if not files:
        verdict = ("NO EVIDENCE: no vendor observation has been recorded. "
                   "The append-versus-revise question is untouched.")
    elif totals["restorations_changed"] or totals["revisions"]:
        # Two distinct findings, stated separately. A changed restoration
        # answers the question directly; a same-basis revision of a
        # populated cell shows the series is not append-only even where no
        # withdrawal was involved. An ADJUSTMENT is neither, and never
        # reaches this branch.
        verdict = (f"REVISION OBSERVED: {totals['restorations_changed']} of "
                   f"{cycles} complete cycle(s) came back with a different "
                   f"value, and {totals['revisions']} same-basis revision(s) "
                   f"of populated cells were seen outside a cycle. Prices "
                   f"already served are not immutable.")
    elif cycles == 0:
        verdict = (f"INSUFFICIENT EVIDENCE: {totals['withdrawals']} withdrawal(s) "
                   f"observed and {totals['open_withdrawals']} still open, but no "
                   f"served -> withheld -> restored cycle has completed. Zero "
                   f"cycles is not evidence of append-only behaviour. "
                   f"{totals['adjustments']} whole-column adjustment(s) are "
                   f"excluded from the revision evidence by design.")
    else:
        verdict = (f"BOUNDED, NOT PROVEN: {cycles} complete cycle(s), none of "
                   f"which changed the restored value, and no same-basis "
                   f"revision of a populated cell. This bounds how often "
                   f"restoration revises; it does not establish that it never "
                   f"does.")
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
                            "adjustments": 0, "basis_changes": 0})
        p["runs"] += 1
        for k in ("fills", "withdrawals", "removed_cells", "revisions",
                  "adjustments", "basis_changes"):
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
