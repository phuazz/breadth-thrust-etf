"""Conservative recovery of observed, adjusted native-currency vendor prices.

The checksum detects accidental cache/metadata drift, not a malicious signer.
Never call this after FX conversion, filling, or price adjustment. No disk writes.
"""
from __future__ import annotations

import copy
import hashlib
import json

import numpy as np
import pandas as pd

from stall_guard import StallTimeout, run_with_deadline
from native_sessions import effective_calendar

PROVENANCE_ATTR = "observed_price_provenance"
RETRY_DEADLINE_SECONDS = 30.0


def _day(value):
    value = pd.Timestamp(value)
    return value.tz_localize(None).normalize() if value.tz else value.normalize()


def _digest(frame, metadata):
    payload = {
        "metadata": metadata, "columns": list(frame.columns),
        "index": [d.isoformat() for d in frame.index],
        "values": [[None if pd.isna(v) else float(v).hex() for v in row]
                   for row in frame.to_numpy()],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _normalise(frame):
    out = frame.copy(deep=True)
    out.attrs = copy.deepcopy(frame.attrs)
    out.index = pd.DatetimeIndex([_day(d) for d in out.index])
    if out.index.has_duplicates or out.columns.has_duplicates:
        raise ValueError("Ambiguous duplicate price dates or columns")
    if "Close" not in out or (set(out.columns) & {"Open", "High", "Low"}
                              and not {"Open", "High", "Low", "Close"} <= set(out)):
        raise ValueError("Prices require Close or all four OHLC columns")
    return out.sort_index()


def _valid(frame, sessions, through):
    if any(d not in sessions or d > through for d in frame.index):
        raise ValueError("Price row is not a completed venue session in the requested span")
    prices = [c for c in ("Open", "High", "Low", "Close") if c in frame]
    values = frame[prices].to_numpy(dtype=float)
    present = ~np.isnan(values)
    if np.any(present & (~np.isfinite(values) | (values <= 0))):
        raise ValueError("Non-positive or non-finite observed price")
    complete = frame[prices].notna().all(axis=1)
    rows = frame.loc[complete]
    if "Open" in frame and ((rows.High < rows[["Open", "Close", "Low"]].max(axis=1)) |
                             (rows.Low > rows[["Open", "Close", "High"]].min(axis=1))).any():
        raise ValueError("Invalid OHLC relationships")
    return complete


def _same_basis(current, other, current_valid, other_valid):
    overlap = current.index[current_valid].intersection(other.index[other_valid])
    prices = [c for c in ("Open", "High", "Low", "Close") if c in current]
    return bool(len(overlap)) and np.array_equal(
        current.loc[overlap, prices].to_numpy(), other.loc[overlap, prices].to_numpy())


def project_close(frame):
    """Project a certified raw native frame to Close, preserving row lineage.

    Refuse an uncertified or modified input. This is not an attestation for an
    arbitrary transformed frame; verification precedes the projection.
    """
    original = _normalise(frame)
    metadata = copy.deepcopy(original.attrs.get(PROVENANCE_ATTR, {}))
    digest = metadata.pop("digest", None)
    if (not digest or digest != _digest(original, metadata)
            or metadata.get("source") != "yfinance"
            or metadata.get("basis") != "auto_adjust=True"
            or metadata.get("currency") != original.attrs.get("currency")):
        raise ValueError("Cannot project an uncertified or modified price frame")
    result = original[["Close"]].copy()
    result.attrs = copy.deepcopy(original.attrs)
    result.attrs[PROVENANCE_ATTR] = dict(metadata, digest=_digest(result, metadata))
    return result


def settle_observed_prices(frame, *, symbol, venue, through, cached=None,
                           fetch_window=None, now_utc=None, lookback_days=10,
                           exclude_non_sessions=False):
    """Return a new frame and audit record; unresolved gaps stay missing.

    ``fetch_window(start, end)`` receives ISO dates, with end exclusive. Transport
    failures are recorded, other errors propagate. One narrow request is made.
    ``through`` is an inclusive completed-session cutoff, not today's live bar.
    Acquisition callers may exclude reported non-trading dates, with an audit.
    This never filters unfinished trading sessions or relaxes cached evidence.
    Python date-library months are 1-indexed; arithmetic uses Timedelta only.
    """
    if not symbol or not venue or lookback_days < 1:
        raise ValueError("Symbol, known venue and positive lookback are required")
    # Match publication guards, including validated Chinese holiday pins.
    cal = effective_calendar(venue)  # Unknown venues fail, never default to NYSE.
    now = pd.Timestamp.now(tz="UTC") if now_utc is None else pd.Timestamp(now_utc)
    now = now.tz_localize("UTC") if now.tz is None else now.tz_convert("UTC")
    cap = _day(through)
    out = _normalise(frame)
    if out.attrs.get("symbol", symbol) != symbol:
        raise ValueError("Observed frame symbol does not match the requested symbol")
    record = {"symbol": symbol, "venue": venue, "through": cap.date().isoformat(),
              "retried_dates": [], "retained_dates": [], "unresolved_dates": [],
              "refusals": [], "excluded_non_session_dates": [],
              "excluded_retry_non_session_dates": []}
    if out.empty:
        return out, record  # No span or anchors: never fabricate a cold history.
    start = out.index.min()
    schedule = cal.schedule(start_date=start, end_date=max(cap, out.index.max()))
    completed = schedule.index[pd.to_datetime(schedule.market_close, utc=True) <= now]
    sessions = pd.DatetimeIndex([_day(d) for d in completed])
    if cap not in sessions:
        raise ValueError("through must name a completed venue session")
    calendar_sessions = pd.DatetimeIndex([_day(d) for d in schedule.index])

    def screen_non_sessions(candidate, audit_key):
        # Use the full venue calendar, not only completed sessions: an open
        # intraday session is not a holiday and must still fail validation.
        excluded = candidate.index[(candidate.index <= cap) &
                                   ~candidate.index.isin(calendar_sessions)]
        if not exclude_non_sessions or not len(excluded):
            return candidate
        record[audit_key] = [d.date().isoformat() for d in excluded]
        return candidate.loc[~candidate.index.isin(excluded)].copy()

    out = screen_non_sessions(out, "excluded_non_session_dates")
    if out.empty:
        return out, record  # A holiday-only response cannot become evidence.
    valid = _valid(out, sessions, cap)
    currency = out.attrs.get("currency")
    stamp = now.isoformat()
    lineage = {d.date().isoformat(): {"method": "vendor", "acquired_at": stamp}
               for d in out.index[valid]}
    expected = sessions[(sessions >= start) & (sessions <= cap)]

    def missing():
        return expected.difference(out.index[_valid(out, sessions, cap)])

    def merge(candidate, candidate_valid, method, prior=None):
        for day in missing().intersection(candidate.index[candidate_valid]):
            # A partial current row may not lose any actually observed field.
            if day in out.index:
                observed = out.loc[day].notna()
                if not np.array_equal(out.loc[day, observed].to_numpy(),
                                      candidate.loc[day, observed].to_numpy()):
                    record["refusals"].append(f"{method}: partial row conflicts {day.date()}")
                    continue
            out.loc[day] = candidate.loc[day]
            date = day.date().isoformat()
            record["retained_dates" if method == "retained" else "retried_dates"].append(date)
            lineage[date] = ({"method": "retained", "acquired_at": prior[date]["acquired_at"],
                              "retained_at": stamp, "origin": prior[date].get("origin", prior[date])}
                             if prior is not None else {"method": "retry", "acquired_at": stamp})

    recent = missing()[missing() >= cap - pd.Timedelta(days=lookback_days)]
    if len(recent):
        anchors = out.index[valid]
        lower = anchors[anchors < recent.min()]
        upper = anchors[anchors > recent.max()]
        lo = lower[-1] if len(lower) else recent.min()
        hi = upper[0] if len(upper) else recent.max()
        # Do not let an old anchor turn a narrow retry into a full refresh.
        lo = max(lo, cap - pd.Timedelta(days=lookback_days + 7))
        if fetch_window is None:
            import yfinance as yf
            def fetch_window(start, end):
                return yf.Ticker(symbol).history(start=start, end=end,
                                                auto_adjust=True, timeout=15)
        try:
            retry = run_with_deadline(
                lambda: fetch_window(lo.date().isoformat(),
                                     (hi + pd.Timedelta(days=1)).date().isoformat()),
                seconds=RETRY_DEADLINE_SECONDS, label=f"{symbol} missing-session retry")
        except (StallTimeout, TimeoutError, ConnectionError, OSError) as exc:
            retry = None
            record["retry_error"] = f"{type(exc).__name__}: {exc}"
        if retry is not None and not retry.empty:
            retry = _normalise(retry)
            # yfinance history also returns Volume/Dividends/Stock Splits.
            if not set(out.columns) <= set(retry.columns):
                record["refusals"].append("retry: column mismatch")
            else:
                retry = retry.loc[(retry.index >= start) & (retry.index <= cap), out.columns]
                retry = screen_non_sessions(retry, "excluded_retry_non_session_dates")
                rv = _valid(retry, sessions, cap)
                if (retry.attrs.get("symbol", symbol) != symbol or
                        retry.attrs.get("currency", currency) != currency or
                        not _same_basis(out, retry, valid, rv)):
                    record["refusals"].append("retry: no exact adjustment-basis overlap")
                else:
                    merge(retry, rv, "retry")

    if cached is not None and len(missing()):
        try:
            old = _normalise(cached)
            att = copy.deepcopy(old.attrs.get(PROVENANCE_ATTR, {}))
            digest = att.pop("digest", None)
            if (not digest or digest != _digest(old, att) or att.get("source") != "yfinance"
                    or att.get("basis") != "auto_adjust=True" or att.get("symbol") != symbol
                    or att.get("venue") != venue or att.get("currency") != currency
                    or old.attrs.get("currency") != att.get("currency")
                    or list(old.columns) != list(out.columns)):
                raise ValueError("unbound provenance, identity, currency or schema mismatch")
            oldcap = _day(att["through"])
            oldschedule = cal.schedule(start_date=old.index.min(), end_date=old.index.max())
            acquired = pd.Timestamp(att["acquired_at"])
            if acquired.tz is None or acquired > now:
                raise ValueError("cache acquisition is ambiguous or later than this run")
            oldsessions = pd.DatetimeIndex([_day(d) for d in oldschedule.index[
                pd.to_datetime(oldschedule.market_close, utc=True) <= acquired]])
            ov = _valid(old, oldsessions, oldcap)
            if not _same_basis(out, old, _valid(out, sessions, cap), ov):
                raise ValueError("no exact adjustment-basis overlap")
            merge(old, ov, "retained", att["rows"])
        except (ValueError, KeyError, TypeError) as exc:
            record["refusals"].append(f"cache: {exc}")
    out = out.sort_index()
    record["unresolved_dates"] = [d.date().isoformat() for d in missing()]
    metadata = {"version": 1, "source": "yfinance", "basis": "auto_adjust=True",
                "symbol": symbol, "venue": venue, "currency": currency,
                "through": cap.date().isoformat(), "acquired_at": stamp, "rows": lineage,
                "excluded_non_session_dates": record["excluded_non_session_dates"],
                "excluded_retry_non_session_dates": record["excluded_retry_non_session_dates"]}
    out.attrs[PROVENANCE_ATTR] = dict(metadata, digest=_digest(out, metadata))
    return out, record
