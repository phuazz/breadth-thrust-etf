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
# "The same adjusted close" between two requests for the same history.
#
# MEASURED 2026-11-11, the first scheduled run of this module. Yahoo's
# adjusted closes are not reproducible to the last digit: one cached column
# compared with a full-history response fetched a day later differed on up to
# half of its older cells by a few float32 units. Across the 454 Yahoo-basis
# columns with comparable cells in the ten panels that failed, 444 differed
# by at most 9.6e-07 relative, ratio 1.0 throughout, with no difference at
# all in the most recent 25 shared sessions of any of them; 327 of the 444
# exceeded the original 1e-07, which therefore read a rounding difference as
# an adjustment change. Two responses minutes apart disagree with each other
# the same way: IUES passed at 15:22 SGT and failed at 16:29 on APC against
# the cache the first run had just written.
#
# The other ten columns carried real changes, the smallest 6.9e-04 (SATS, a
# reassigned symbol accruing a daily distribution), then 1.1e-03 (one
# restated close) and 1.7e-03 (an NSE dividend). 1e-05 is ten times the
# largest noise and about seventy times below the smallest real change. An
# adjustment smaller than 0.001% of price is not separable from the vendor's
# own serialisation, so nothing this module could act on is given up.
RTOL = 1e-5
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


def _iso(ts):
    return None if ts is None or pd.isna(ts) else str(pd.Timestamp(ts).date())


def describe_unserved(old, new):
    """What a full-history response leaves uncovered in one cached column.

    Evidence for the log and the request record; decides nothing. ``old`` is
    the cached column and ``new`` the vendor's full-history answer. Cells
    before the vendor's first bar are counted apart from interior ones: the
    first is the shape of a reused or truncated symbol, the second of a bar
    the vendor once served and later withdrew.
    """
    old, new = old.dropna(), new.dropna()
    unserved = old.index.difference(new.index)
    shared = old.index.intersection(new.index)
    rel = (new.loc[shared] / old.loc[shared] - 1.0).abs()
    changed = rel[rel > RTOL]
    leading = unserved[unserved < new.index.min()] if len(new) else unserved
    return {
        "cached_cells": int(len(old)),
        "vendor_cells": int(len(new)),
        "vendor_first": _iso(new.index.min()) if len(new) else None,
        "vendor_last": _iso(new.index.max()) if len(new) else None,
        "unserved_cells": int(len(unserved)),
        "unserved_first": _iso(unserved.min()) if len(unserved) else None,
        "unserved_last": _iso(unserved.max()) if len(unserved) else None,
        "unserved_before_vendor_start": int(len(leading)),
        "shared_cells": int(len(shared)),
        "changed_cells": int(len(changed)),
        "changed_first": _iso(changed.index.min()) if len(changed) else None,
        "changed_last": _iso(changed.index.max()) if len(changed) else None,
        "max_rel_change": float(rel.max()) if len(rel) else None,
    }


def _unserved_line(ticker, d):
    """One log line per column the vendor could not cover."""
    interior = d["unserved_cells"] - d["unserved_before_vendor_start"]
    where = []
    if d["unserved_before_vendor_start"]:
        where.append(f"{d['unserved_before_vendor_start']} before the vendor's first bar "
                     f"({d['vendor_first']})")
    if interior:
        where.append(f"{interior} inside the vendor's own span")
    change = ("no shared cell to compare" if d["max_rel_change"] is None else
              f"{d['changed_cells']} of {d['shared_cells']} shared cell(s) changed, "
              f"largest {d['max_rel_change']:.2e}, {d['changed_first']} to {d['changed_last']}")
    return (f"  Reconciliation incomplete {ticker}: {d['unserved_cells']} cached cell(s) not served, "
            f"{d['unserved_first']} to {d['unserved_last']} ({'; '.join(where)}); {change}")


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
        # A SPLICE NEEDS TWO BASES. The refusal below exists to stop cached
        # cells on one adjustment basis meeting fresh cells on another. Cells
        # the vendor does not serve are not that on their own: the caller's
        # cell-preservation merge has always kept them (an earlier occupant
        # of a reused ticker, a bar the vendor later withdrew, a delisted
        # name), and where every cell the vendor DOES serve still equals the
        # cache there is one basis and nothing to reconcile. Until 2026-11-11
        # an unserved cell alone refused the column whenever it reached this
        # loop, so one withdrawn close inside the overlap, or a name the
        # vendor stopped serving, failed its whole panel. Refused now: a
        # changed shared cell beside an unserved one, or a vendor series that
        # shares no cell with the cache at all and so cannot be placed on it.
        uncovered = {}
        for t in check:
            found = describe_unserved(prior[t], history[t])
            if found["unserved_cells"] and (
                    found["changed_cells"]
                    or (not found["shared_cells"] and found["vendor_cells"])):
                failures.append(t)
                uncovered[t] = found
        parts.append(history)
    out = pd.concat(parts, axis=1).reindex(columns=tickers).sort_index()
    if observe is not None:
        observe(out.loc[recent:])
    if failures:
        # Name what the vendor left uncovered before refusing: the symbol
        # list alone could not tell a reused ticker from a withdrawn bar on
        # 2026-11-11, and the cache that could is the one left untouched.
        for t in failures:
            print(_unserved_line(t, uncovered[t]), flush=True)
        raise RuntimeError(f"{failures}: full-history reconciliation incomplete; incumbent cache untouched")
    return out, recent, {"incremental_columns": len(short), "full_columns": len(full),
                         "reconciled_columns": retry, "observation_start": str(recent.date()),
                         "full_requested": full, "full_answered": answered,
                         "incremental_requested": short, "full_start": full_start}
