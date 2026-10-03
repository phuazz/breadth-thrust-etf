"""Hold a constituent column's price basis: a re-based history is chosen, not suffered.

WHY THIS EXISTS (2026-10-03). Corteva separated on 2026-10-01 and Yahoo's
``CTVA`` symbol now carries Vylor at about $12 against a $77 close the day
before. Within hours the two price sources disagreed on the HISTORY of that
column: Norgate (TOTALRETURN) served the continuous spin-adjusted series,
every pre-separation close divided by 6.6652; yfinance served the raw
series with a 6.6x cliff on 2026-10-01 and no split event in its calendar.
The 01:21 UTC weekend refresh took Norgate's column whole under the WS19b
superset rule and wrote the adjusted basis into ``prices_cache_csp1`` and
``prices_cache_iums``. Nothing objected:

  * ``download_prices`` re-downloads the whole history every run and keeps a
    cached cell only where the source serves nothing, so a column arrives
    on whatever basis the source has that hour;
  * the WS15 step-defect guard asks the vendor's split calendar about a
    split-sized step in the last 40 sessions and fails OPEN when the
    calendar has nothing ("accepting the fresh series unverified") - and a
    spin-off is not a split, so the calendar never has it;
  * ``price_revisions.diff_frames`` walks the last 60 rows of the cache, so
    1,848 re-based cells were logged as 60 "ambiguous" cells per panel.

Had the sources flipped the other way on the next run, CTVA would have
read below its 50-day average for 50 sessions and below its 200-day
average for 200, on a fabricated -85% day. Breadth is scale-invariant per
column, so a WHOLE re-basing is harmless on its own; the harm is the
PARTIAL one that follows when the other basis comes back, and the two
arrive in either order. Either way the cache's basis changed and no one
chose it - the failure ``price_source.py`` was written against.

WHAT THIS DOES. Run on the FINISHED frame, after the cell-preservation
merge, the Norgate overlay and the tail verification, immediately before
the cache write - the one point where every path a basis can enter by has
already run. For every column the incumbent cache also holds:

  1. DETECT. Over the cells both frames populate, a split-style re-basing
     is a run of split-sized changes (|ln new/old| >= 0.20, the WS15
     threshold) that share one ratio (log spread <= 0.10) and fill the
     history up to the last such cell (>= 80% of it, so a few cells the
     merge preserved from the cache do not hide it). Whole or prefix, any
     age: the shape of a split, a spin-off or a currency-unit change, and
     not the shape of dividend drift (ratio within a few per cent of one)
     or of scattered restatements.
  2. DECIDE. A declaration in ``data/corporate_action_basis.json`` naming
     the ticker, the first session on the new basis, the vendor's factor
     and the CHOSEN basis settles it: the column is admitted whole when it
     is on the chosen basis and refused when it is the other one. Failing
     that, the vendor's split calendar may confirm an ordinary split: the
     adjusted series is admitted, the raw series against a known split (the
     WS15 defect) is refused at any age. Failing both, REFUSE.
  3. HOLD. A refused column keeps the cached history cell for cell. New
     sessions after the cache's last populated cell are appended only when
     the source's close on that last cell equals the cached close exactly
     (the seam method of the 2026-10-03 recovery); otherwise nothing is
     appended and the column waits. No cell is forward-filled, invented or
     taken from before the cached history starts, and no cached populated
     cell is ever lost.

Every decision prints in the refresh log, travels in the ``basis_guard``
record of ``logs/cache_changes.jsonl`` and in the cache sidecar, and names
the declaration that would change it. The coverage floors and the
never-go-backwards guards are untouched: a column held at its seam is one
blank name on the newest rows, which those guards already judge.

Python datetime months are 1-indexed (January = 1). Dates are compared as
pandas Timestamps; nothing here counts days by hand.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import price_revisions

# A split-sized change: |ln ratio| >= 0.20, a 5:4 split or larger. ONE
# definition for the two guards that reason about split magnitude -
# compute_breadth imports it as VENDOR_STEP_LOG_RETURN.
SPLIT_SIZED_LOG_RATIO = 0.20
# Log-ratio spread tolerated inside one re-basing, and the tolerance a
# declared or calendar factor is matched with (= WS15's match tolerance).
REBASE_LOG_DISPERSION = 0.10
# Fewest re-based cells that raise the question (= price_revisions'
# ADJUSTMENT_MIN_CELLS).
REBASE_MIN_CELLS = 3
# The re-based cells must fill at least this share of the shared history up
# to the last re-based cell. Below it the changes are scattered
# restatements, not a re-basing.
REBASE_PREFIX_FILL = 0.80
# Seam equality. Parquet round-trips float64 exactly and both vendors
# serve the same float32-representable closes run to run, so an unchanged
# close compares equal bit for bit; this only forgives a last-place
# formatting difference (= price_revisions.REL_TOL).
SEAM_REL_TOL = price_revisions.REL_TOL
# A whole re-basing with no new bar yet (a factor pre-applied on the
# ex-date morning) may declare an event up to this many calendar days past
# the last re-based cell.
PRE_APPLIED_GRACE_DAYS = 7

DECLARATIONS_FILENAME = "corporate_action_basis.json"
ADJUSTED = "adjusted"
UNADJUSTED = "unadjusted"
BASES = (ADJUSTED, UNADJUSTED)

REFUSED = "refused"
REFUSED_BY_DECLARATION = "refused_by_declaration"
REFUSED_BY_SPLIT_CALENDAR = "refused_by_split_calendar"
ADMITTED_BY_DECLARATION = "admitted_by_declaration"
ADMITTED_BY_SPLIT_CALENDAR = "admitted_by_split_calendar"
ADMISSIONS = (ADMITTED_BY_DECLARATION, ADMITTED_BY_SPLIT_CALENDAR)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts) -> str:
    return str(pd.Timestamp(ts).date())


# ---------------------------------------------------------------------------
# 1. Detect
# ---------------------------------------------------------------------------
def detect_rebasing(old: pd.Series, new: pd.Series) -> dict | None:
    """A split-style re-basing of ``new`` relative to ``old``, or None.

    Compared over the cells BOTH series populate with a positive close, in
    date order. The finding carries the ratio new/old (median over the
    re-based cells), the cell counts, the first and last re-based date and
    the shape: ``whole`` when every shared cell after the first re-based
    one is re-based, ``prefix`` when unchanged shared cells follow the last
    re-based one. Pure.
    """
    o = pd.to_numeric(old, errors="coerce")
    n = pd.to_numeric(new, errors="coerce")
    o = o[~o.index.duplicated(keep="last")]
    n = n[~n.index.duplicated(keep="last")]
    idx = o.index.intersection(n.index).sort_values()
    if len(idx) < REBASE_MIN_CELLS:
        return None
    ov = o.reindex(idx).to_numpy(dtype=float)
    nv = n.reindex(idx).to_numpy(dtype=float)
    shared = np.isfinite(ov) & np.isfinite(nv) & (ov > 0) & (nv > 0)
    if int(shared.sum()) < REBASE_MIN_CELLS:
        return None
    dates = idx[shared]
    lr = np.log(nv[shared] / ov[shared])
    big = np.abs(lr) >= SPLIT_SIZED_LOG_RATIO
    cells = int(big.sum())
    if cells < REBASE_MIN_CELLS:
        return None
    where = np.flatnonzero(big)
    first, last = int(where[0]), int(where[-1])
    prefix_cells = last + 1
    fill = cells / prefix_cells
    if fill < REBASE_PREFIX_FILL:
        return None
    sub = lr[big]
    dispersion = float(sub.max() - sub.min())
    if dispersion > REBASE_LOG_DISPERSION:
        return None
    log_ratio = float(np.median(sub))
    suffix_cells = int(len(lr) - prefix_cells)
    return {
        "ratio": float(math.exp(log_ratio)),
        "log_ratio": log_ratio,
        "cells": cells,
        "shared_cells": int(len(lr)),
        "prefix_cells": int(prefix_cells),
        "prefix_fill": round(fill, 4),
        "share_of_history": round(cells / len(lr), 4),
        "log_dispersion": round(dispersion, 8),
        "first": _iso(dates[first]),
        "through": _iso(dates[last]),
        "suffix_cells": suffix_cells,
        "first_suffix": _iso(dates[last + 1]) if suffix_cells else None,
        "shape": "prefix" if suffix_cells else "whole",
    }


# ---------------------------------------------------------------------------
# 2. Decide
# ---------------------------------------------------------------------------
def load_declarations(path: Path | None) -> tuple[list[dict], list[str]]:
    """``(declarations, warnings)`` from the corporate-action basis file.

    A missing file is no declaration. A malformed file or entry is SKIPPED
    with a warning, never admitted on a guess: with nothing declared the
    guard refuses, which is the safe side, and the refresh log names the
    entry so it can be fixed. Never raises.
    """
    warnings: list[str] = []
    if path is None:
        return [], warnings
    p = Path(path)
    if not p.exists():
        return [], warnings
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"{p.name} could not be read ({type(exc).__name__}: "
                        f"{exc}); treating it as empty, so every re-basing "
                        f"is refused")
        return [], warnings
    entries = blob.get("declarations") if isinstance(blob, dict) else None
    if not isinstance(entries, list):
        warnings.append(f"{p.name} carries no 'declarations' list; treating "
                        f"it as empty")
        return [], warnings
    out: list[dict] = []
    for i, e in enumerate(entries):
        try:
            ticker = str(e["ticker"]).strip()
            effective = date.fromisoformat(str(e["effective"]))
            factor = float(e["factor"])
            basis = str(e["basis"]).strip().lower()
            if not ticker or basis not in BASES or not math.isfinite(factor) \
                    or factor <= 0 or math.isclose(factor, 1.0):
                raise ValueError("ticker, basis or factor out of range")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            warnings.append(f"{p.name} entry {i} skipped: {exc} "
                            f"(needs ticker, effective ISO date, factor > 0 "
                            f"and != 1, basis in {BASES})")
            continue
        out.append({"ticker": ticker, "effective": effective.isoformat(),
                    "factor": factor, "basis": basis,
                    "event": str(e.get("event") or ""),
                    "source": "declaration"})
    return out, warnings


def _calendar_evidence(ticker: str, splits_for) -> tuple[list[dict], str]:
    """The vendor's split calendar as evidence entries, and a status word:
    ``unavailable`` (no answer), ``empty`` or ``ok``. A vendor that applies
    a split retroactively produces the ADJUSTED series, so the chosen basis
    for calendar evidence is always ``adjusted``: the raw series against a
    known split is the WS15 defect and is refused."""
    if splits_for is None:
        return [], "unavailable"
    try:
        splits = splits_for(ticker)
    except Exception:  # noqa: BLE001 - no answer is not a verdict
        splits = None
    if splits is None or len(splits) == 0:
        return [], "unavailable" if splits is None else "empty"
    out: list[dict] = []
    for d, ratio in splits.items():
        try:
            ts = pd.Timestamp(d)
            if ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            f = float(ratio)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and f > 0 and not math.isclose(f, 1.0):
            out.append({"ticker": ticker, "effective": _iso(ts.normalize()),
                        "factor": f, "basis": ADJUSTED, "event": "split",
                        "source": "vendor split calendar"})
    return out, "ok"


def event_window(finding: dict, incoming_end) -> tuple[pd.Timestamp, pd.Timestamp]:
    """``(after, through)``: a matching event's first session on the new
    basis lies strictly after the last re-based cell and no later than the
    first unchanged shared cell. With no unchanged cell after it (a WHOLE
    re-basing) the bound is the source's newest bar, or a short grace past
    the last re-based cell when the source has no newer bar yet.
    Python datetime months are 1-indexed; pandas does the arithmetic."""
    after = pd.Timestamp(finding["through"])
    if finding.get("first_suffix"):
        return after, pd.Timestamp(finding["first_suffix"])
    grace = after + pd.Timedelta(days=PRE_APPLIED_GRACE_DAYS)
    if incoming_end is None or pd.isna(incoming_end):
        return after, grace
    return after, max(pd.Timestamp(incoming_end), grace)


def match_evidence(finding: dict, evidence: list[dict], incoming_end) -> dict | None:
    """The first evidence entry whose event sits in the finding's window
    and whose factor matches the observed ratio in either direction.

    Returns the entry extended with ``incoming_is`` (``adjusted`` when the
    incoming column is the factor-adjusted series relative to the cache,
    ``unadjusted`` when it is the raw one) or None.
    """
    after, through = event_window(finding, incoming_end)
    lr = float(finding["log_ratio"])
    for e in evidence:
        try:
            eff = pd.Timestamp(e["effective"])
            lf = math.log(float(e["factor"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not (after < eff <= through):
            continue
        if abs(lr + lf) <= REBASE_LOG_DISPERSION:
            return {**e, "incoming_is": ADJUSTED}
        if abs(lr - lf) <= REBASE_LOG_DISPERSION:
            return {**e, "incoming_is": UNADJUSTED}
    return None


def decide(ticker: str, finding: dict, *, declarations: list[dict],
           splits_for=None, incoming_end=None) -> dict:
    """ADMIT or REFUSE one re-based column, with the reason and the evidence.

    Declarations are read first: an operator's decision wins over the
    vendor's calendar. The calendar is consulted - one network call - only
    when no declaration decides the column, so an undeclared spin-off costs
    one lookup per refresh and an ordinary split needs no declaration.
    """
    mine = [d for d in declarations if str(d.get("ticker")) == str(ticker)]
    hit = match_evidence(finding, mine, incoming_end)
    if hit is not None:
        admit = hit["incoming_is"] == hit["basis"]
        return {"decision": ADMITTED_BY_DECLARATION if admit else REFUSED_BY_DECLARATION,
                "evidence": hit,
                "reason": (f"declared basis '{hit['basis']}' for the "
                           f"{hit['effective']} event (factor {hit['factor']:g}"
                           f"{', ' + hit['event'] if hit['event'] else ''}) and "
                           f"this column is the {hit['incoming_is']} series")}
    calendar, status = _calendar_evidence(ticker, splits_for)
    hit = match_evidence(finding, calendar, incoming_end)
    if hit is not None:
        admit = hit["incoming_is"] == ADJUSTED
        return {"decision": ADMITTED_BY_SPLIT_CALENDAR if admit else REFUSED_BY_SPLIT_CALENDAR,
                "evidence": hit,
                "reason": (f"the vendor split calendar carries a "
                           f"{hit['factor']:g}-for-1 split on {hit['effective']} "
                           f"and this column is the {hit['incoming_is']} series"
                           + ("" if admit else
                              " - the split served unapplied, the WS15 defect"))}
    other = ""
    if mine:
        other = (f"; {len(mine)} declaration(s) for {ticker} exist but none "
                 f"matches this event's window and factor")
    cal = {"unavailable": "the vendor split calendar is unavailable",
           "empty": "the vendor split calendar carries no split",
           "ok": "the vendor split calendar carries no matching split"}[status]
    return {"decision": REFUSED, "evidence": None,
            "reason": f"no declaration covers it and {cal}{other}"}


# ---------------------------------------------------------------------------
# 3. Hold
# ---------------------------------------------------------------------------
def hold_column(prior_col: pd.Series, incoming_col: pd.Series, index
                ) -> tuple[pd.Series, dict]:
    """The cached column, extended only across an exact seam.

    Returns the column to write on ``index`` - every cached populated cell
    as it was, plus the source's cells AFTER the cache's last populated
    cell when the source's close ON that cell equals the cached close - and
    the seam record. Nothing is forward-filled, nothing is taken from
    before the cached history starts, and a seam that does not match
    appends nothing.
    """
    prior_col = prior_col[~prior_col.index.duplicated(keep="last")]
    # The cached dtype is kept (Norgate columns sit in the cache as float32,
    # yfinance ones as float64); an appended value only widens it when the
    # source's dtype needs the width, so no value is ever narrowed.
    held = prior_col.reindex(index)
    populated = pd.to_numeric(prior_col, errors="coerce").dropna()
    if populated.empty:
        return held, {"seam_date": None, "matched": False,
                      "appended_sessions": [], "reason": "no cached history"}
    seam = populated.index[-1]
    cache_close = float(populated.iloc[-1])
    source_close = incoming_col.get(seam, np.nan) if seam in incoming_col.index else np.nan
    try:
        source_close = float(source_close)
    except (TypeError, ValueError):
        source_close = float("nan")
    matched = (math.isfinite(source_close)
               and price_revisions._same(cache_close, source_close))
    rec = {"seam_date": _iso(seam), "cache_close": cache_close,
           "source_close": None if not math.isfinite(source_close) else source_close,
           "matched": bool(matched), "appended_sessions": []}
    if not matched:
        rec["reason"] = ("the source does not serve the seam session"
                         if not math.isfinite(source_close)
                         else "the source's close on the seam session differs "
                              "from the cached close")
        return held, rec
    after = pd.to_numeric(incoming_col, errors="coerce")
    after = after[(after.index > seam)].dropna()
    after = after[(after > 0) & np.isfinite(after)]
    after = after[after.index.isin(index)]
    if not after.empty:
        width = np.result_type(held.dtype, after.dtype)
        if held.dtype != width:
            held = held.astype(width)
        held.loc[after.index] = after.to_numpy(dtype=width)
        rec["appended_sessions"] = [_iso(d) for d in after.index]
    return held, rec


def _column_record(col: str, finding: dict, verdict: dict, prior_basis: str,
                   incoming_basis: str) -> dict:
    return {"column": str(col), **verdict, **finding,
            "prior_basis": prior_basis, "incoming_basis": incoming_basis}


def hold_rebased_columns(close: pd.DataFrame, prior: pd.DataFrame | None, *,
                         old_sidecar: dict | None = None,
                         new_sidecar: dict | None = None,
                         declarations: list[dict] | None = None,
                         declarations_path: Path | None = None,
                         declaration_warnings: list[str] | None = None,
                         splits_for=None,
                         now_utc: datetime | None = None,
                         ) -> tuple[pd.DataFrame, dict]:
    """Detect, decide and hold over every column the cache also holds.

    ``close`` is the finished frame about to be written; ``prior`` the
    incumbent cache. Returns the frame to write (a copy when any column was
    held) and the guard record for the log, the ledger and the sidecar.
    Pure given ``splits_for`` and the declarations.
    """
    stamp = (now_utc or _now()).isoformat(timespec="seconds")
    record: dict = {"checked_at_utc": stamp, "columns_checked": 0,
                    "declarations_file": (str(declarations_path)
                                          if declarations_path else None),
                    "declarations_loaded": len(declarations or []),
                    "warnings": list(declaration_warnings or []),
                    "refused": [], "admitted": []}
    if prior is None or not isinstance(close, pd.DataFrame) or close.empty \
            or prior.empty:
        return close, record
    prior = prior[~prior.index.duplicated(keep="last")]
    common = [c for c in close.columns if c in prior.columns]
    record["columns_checked"] = len(common)
    if not common:
        return close, record
    # An unrecorded cache is a yfinance cache (price_source.read_cache_source:
    # every cache written before the sidecar existed was a yfinance download),
    # so a refused overlay column on such a cache is recorded as kept on the
    # incumbent rather than under a basis nobody can name.
    old_b = price_revisions.resolve_column_basis(
        old_sidecar if old_sidecar is not None
        else {"source": price_revisions.YFINANCE})
    new_b = price_revisions.resolve_column_basis(new_sidecar)
    out = None
    for col in common:
        finding = detect_rebasing(prior[col], close[col])
        if finding is None:
            continue
        incoming = pd.to_numeric(close[col], errors="coerce").dropna()
        incoming_end = incoming.index.max() if len(incoming) else None
        verdict = decide(str(col), finding, declarations=list(declarations or []),
                         splits_for=splits_for, incoming_end=incoming_end)
        rec = _column_record(col, finding, verdict,
                             price_revisions.basis_of(old_b, str(col)),
                             price_revisions.basis_of(new_b, str(col)))
        if verdict["decision"] in ADMISSIONS:
            record["admitted"].append(rec)
            continue
        if out is None:
            out = close.copy()
        held, seam = hold_column(prior[col], close[col], out.index)
        out[col] = held
        rec["seam"] = seam
        record["refused"].append(rec)
    return (out if out is not None else close), record


# ---------------------------------------------------------------------------
# Provenance and the log
# ---------------------------------------------------------------------------
def split_provenance(record: dict | None, norgate_columns: list[str]
                     ) -> tuple[list[str], list[str]]:
    """``(columns_from_norgate, held_on_incumbent)`` after the hold.

    A refused column carries the CACHE'S history, so a column the overlay
    took from Norgate this run but whose cached basis was not Norgate is
    recorded as kept on the incumbent, not as a Norgate column. A refused
    column whose cached basis already was Norgate stays one.
    """
    refused = {r["column"]: r for r in (record or {}).get("refused", [])}
    taken, held = [], []
    for t in norgate_columns:
        r = refused.get(str(t))
        if r is not None and r.get("prior_basis") != price_revisions.NORGATE:
            held.append(t)
        else:
            taken.append(t)
    return taken, held


def _declare_hint(ticker: str, rec: dict) -> str:
    f = math.exp(abs(float(rec["log_ratio"])))
    incoming_is = ADJUSTED if float(rec["log_ratio"]) < 0 else UNADJUSTED
    other = UNADJUSTED if incoming_is == ADJUSTED else ADJUSTED
    return (f'To decide it, declare {{"ticker": "{ticker}", "effective": '
            f'"<first session on the new basis>", "factor": {f:.6g}, '
            f'"basis": ...}} in {DECLARATIONS_FILENAME}: '
            f"'{incoming_is}' admits this series, '{other}' keeps the cached one")


def report(record: dict | None, label: str = "") -> None:
    """One summary line per panel, one line per decision, in the register
    the refresh log uses. Silent only when there was no cache to compare."""
    if not record or not record.get("columns_checked"):
        return
    tag = f"{label}: " if label else ""
    for w in record.get("warnings") or []:
        print(f"  WARN {tag}{w}", flush=True)
    refused, admitted = record.get("refused", []), record.get("admitted", [])
    n = len(refused) + len(admitted)
    if not n:
        print(f"  {tag}Basis guard: no re-based column among "
              f"{record['columns_checked']} shared with the cache.", flush=True)
        return
    print(f"  {tag}Basis guard: {n} of {record['columns_checked']} column(s) "
          f"re-based against the cache; {len(refused)} refused (cached history "
          f"kept), {len(admitted)} admitted. Declarations loaded: "
          f"{record.get('declarations_loaded', 0)}.", flush=True)
    for r in admitted:
        print(f"  ADMITTED {r['column']}: history re-based x{r['ratio']:.6g} over "
              f"{r['cells']} of {r['shared_cells']} shared sessions "
              f"({r['first']} .. {r['through']}, {r['shape']}); {r['reason']}. "
              f"The re-based column is taken whole.", flush=True)
    for r in refused:
        seam = r.get("seam") or {}
        if seam.get("matched"):
            app = seam.get("appended_sessions") or []
            tail = (f"{len(app)} new session(s) appended across an exact seam "
                    f"at {seam['seam_date']}: {app[0]} .. {app[-1]}" if app else
                    f"the seam at {seam['seam_date']} matches and the source "
                    f"has no newer session to append")
        else:
            tail = (f"no session appended - the seam at {seam.get('seam_date')} "
                    f"does not match (cache {seam.get('cache_close')}, source "
                    f"{seam.get('source_close')})")
        hint = "" if r["decision"] != REFUSED else " " + _declare_hint(r["column"], r)
        print(f"  REFUSED {r['column']}: history re-based x{r['ratio']:.6g} over "
              f"{r['cells']} of {r['shared_cells']} shared sessions "
              f"({r['first']} .. {r['through']}, {r['shape']}); {r['reason']}. "
              f"The cached history is kept; {tail}.{hint}", flush=True)
