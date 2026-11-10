"""Conservative incremental Yahoo requests; never splice an unverified basis.

Python datetime months are 1-indexed. Requests remain end-exclusive.
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import price_revisions

OVERLAP_BARS = 20
RTOL = 1e-7
AUDIT_DAYS = 7


def fingerprint(series):
    """Bind scan evidence to exact cached observations, including missing cells."""
    return hashlib.sha256(pd.util.hash_pandas_object(series, index=True).values.tobytes()).hexdigest()


def current_audit(prior, ticker, start, sidecar, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        item = sidecar["historical_scan"][ticker]
        stamp = datetime.fromisoformat(item["scanned_at_utc"])
        age = now - stamp
        return (0 <= age.total_seconds() < AUDIT_DAYS * 86400
                and pd.Timestamp(item["requested_start"]) <= pd.Timestamp(start)
                and item["fingerprint"] == fingerprint(prior[ticker]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def carry_audits(close, prior_sidecar, request_record, source_sidecar, now=None):
    """Record recent full requests, not a claim that missing data is complete.

    The cache fingerprint is written after all guards. Source changes and
    mixed columns cannot inherit Yahoo scan evidence. Incremental writes keep
    the original audit time; only an actual full request resets the clock.
    """
    now = now or datetime.now(timezone.utc)
    basis = price_revisions.resolve_column_basis(source_sidecar)
    mixed = {t for t, _ in price_revisions.norgate_tail_cells(source_sidecar)}
    old = (prior_sidecar or {}).get("historical_scan", {})
    if not isinstance(old, dict):
        old = {}
    result = {}
    for t in close:
        if price_revisions.basis_of(basis, t) != "yfinance" or t in mixed:
            continue
        if t in request_record["full_answered"]:
            item = {"scanned_at_utc": now.isoformat(),
                    "requested_start": request_record["full_start"]}
        elif t in old and t in request_record["incremental_requested"]:
            item = dict(old[t])
        else:
            continue
        item["fingerprint"] = fingerprint(close[t])
        result[t] = item
    return result


def plan(prior, tickers, start, end, sidecar, state_file=None, calendar="NYSE", audit_required=True, now=None):
    """Return common observation start and names requiring full history.

    Only explicitly Yahoo-basis columns qualify. Internal holes are retried
    conservatively, including possible venue holidays: no invented sessions
    or forward fills. Missing leading history is retried, not assumed pre-IPO.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if (not isinstance(prior, pd.DataFrame) or prior.empty
            or not isinstance(prior.index, pd.DatetimeIndex)
            or prior.index.tz is not None or prior.index.has_duplicates
            or not prior.index.is_monotonic_increasing
            or prior.columns.has_duplicates):
        return start, list(tickers)
    base = prior.loc[start:end - pd.Timedelta(days=1)]
    if base.empty:
        return start, list(tickers)
    from venue_calendars import get_calendar
    expected = get_calendar(calendar).schedule(
        start_date=start, end_date=base.index[-1]).index
    if len(expected) and prior.index.min() > expected[0]:
        return start, list(tickers)
    dates = base.index
    recent = max(start, dates[max(0, len(dates) - OVERLAP_BARS)])
    audited = {t for t in tickers if t in base and current_audit(prior, t, start, sidecar, now)}
    missing_rows = expected.difference(base.index)
    if audit_required and len(audited) == len(tickers):
        missing_rows = missing_rows[missing_rows >= recent]
    if len(missing_rows):
        recent = min(recent, missing_rows[0])
    # Retained withdrawal cycles must still be queried outside the overlap.
    if state_file is not None and Path(state_file).exists():
        try:
            state = json.loads(Path(state_file).read_text(encoding="utf-8"))
            for key, value in state["cells"].items():
                if value.get("status") == "withheld":
                    recent = min(recent, max(start, pd.Timestamp(key.split("|", 1)[0])))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return start, list(tickers)
    basis = price_revisions.resolve_column_basis(sidecar)
    mixed = {t for t, _ in price_revisions.norgate_tail_cells(sidecar)}
    full = []
    for t in tickers:
        if audit_required and t not in audited:
            full.append(t)
            continue
        if t not in base or price_revisions.basis_of(basis, t) != "yfinance" or t in mixed:
            full.append(t)
            continue
        col = base[t].dropna()
        if (len(col) < OVERLAP_BARS or (not audit_required and len(expected) and col.index[0] > expected[0])
                or not np.isfinite(col).all()
                or (col <= 0).any()):
            full.append(t)
            continue
        # Date-union holes are not deleted or interpolated. Retry the affected
        # name from full history, without enlarging every other name's request.
        if not audit_required and base.loc[col.index[0]:col.index[-1], t].isna().any():
            full.append(t)
            continue
        recent = min(recent, col.index[-OVERLAP_BARS])
    return recent, full


def incompatible(prior, fresh, tickers):
    """Names needing whole-history reconciliation, never a scaled splice."""
    retry = []
    for t in tickers:
        shared = prior[t].dropna().index.intersection(fresh[t].dropna().index)
        if len(shared) < OVERLAP_BARS:
            retry.append(t)
            continue
        old, new = prior.loc[shared, t], fresh.loc[shared, t]
        if not np.isclose(old, new, rtol=RTOL, atol=1e-10).all():
            retry.append(t)
    return retry


def download(fetch, tickers, start, end, prior, sidecar, state_file=None, calendar="NYSE",
             observe=None, full_history=False, audit_required=True, now=None):
    """Fetch raw closes plus observation scope; caller retains all existing guards.

    An adjusted overlap change (split, dividend or revision) causes a full
    same-source fetch. If that response cannot cover incumbent priced dates,
    fail before cache publication rather than splice two adjustment bases.
    """
    recent, full = plan(prior, tickers, start, end, sidecar, state_file, calendar, audit_required, now)
    if full_history:
        recent, full = pd.Timestamp(start), list(tickers)
    short = [t for t in tickers if t not in full]
    parts = []
    failures = []
    answered = []
    if short:
        tail = fetch(short, str(recent.date()), end)
        retry = incompatible(prior, tail, short)
        keep = [t for t in short if t not in retry]
        if keep:
            parts.append(tail[keep])
        full = list(dict.fromkeys(full + retry))
    else:
        retry = []
    full_start = start
    if full:
        if isinstance(prior, pd.DataFrame) and not prior.empty:
            full_start = str(min(pd.Timestamp(start), prior.index.min()).date())
        history = fetch(full, full_start, end)
        answered = [t for t in full if history[t].notna().sum() >= OVERLAP_BARS
                    and (not isinstance(prior, pd.DataFrame) or t not in prior
                         or prior[t].dropna().index.difference(history[t].dropna().index).empty)]
        basis = price_revisions.resolve_column_basis(sidecar)
        mixed = {t for t, _ in price_revisions.norgate_tail_cells(sidecar)}
        check = list(retry)
        if isinstance(prior, pd.DataFrame):
            for t in full:
                if (t in prior and t not in check and t not in mixed
                        and price_revisions.basis_of(basis, t) == "yfinance"):
                    shared = prior[t].dropna().index.intersection(history[t].dropna().index)
                    if len(shared) and not np.isclose(prior.loc[shared, t], history.loc[shared, t],
                                                      rtol=RTOL, atol=1e-10).all():
                        check.append(t)
        for t in check:
            old = prior[t].dropna()
            new = history[t].dropna()
            if not old.index.difference(new.index).empty:
                failures.append(t)
        parts.append(history)
    out = pd.concat(parts, axis=1).reindex(columns=tickers).sort_index()
    if observe is not None:
        observe(out.loc[recent:])
    if failures:
        raise RuntimeError(f"{failures}: full-history reconciliation incomplete; incumbent cache untouched")
    return out, recent, {"incremental_columns": len(short), "full_columns": len(full),
                         "reconciled_columns": retry, "observation_start": str(recent.date()),
                         "full_requested": full, "full_answered": answered,
                         "incremental_requested": short, "full_start": full_start}
