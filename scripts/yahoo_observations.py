"""Shared native Yahoo observation cache for the non-US feed gaps.

Only source observations enter these files, before FX or calendar alignment.
Python datetime months are 1-indexed. This does not authorise a new data source.
"""
from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from native_sessions import effective_calendar
from observed_price_cache import settle_observed_prices, project_close
from session_bounds import last_completed_session_on


def _read(path):
    try:
        return pd.read_parquet(path) if path.exists() else None
    except (OSError, ValueError):
        return None


def _publishable(frame, prior):
    from price_panel_guard import fetched_frame_is_worse
    if fetched_frame_is_worse(frame, None) is not None:
        return False  # Do not seed evidence from a degenerate vendor response.
    if prior is None or prior.empty:
        return True
    if frame.index.min() > prior.index.min() or frame.index.max() < prior.index.max():
        return False
    try:
        verified = project_close(prior)
        old_time = pd.Timestamp(verified.attrs["observed_price_provenance"]["acquired_at"])
    except (ValueError, KeyError, TypeError):
        return True  # An invalid record cannot veto a new valid capture.
    new_time = pd.Timestamp(frame.attrs["observed_price_provenance"]["acquired_at"])
    return new_time >= old_time


def _write(path, frame):
    """Publish one frame and its bound provenance together, never in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                     dir=path.parent)
    os.close(fd)
    try:
        frame.to_parquet(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _settle_native_yahoo(frame, symbol, data_dir, *, now_utc, end=None,
                        through=None, fetch_window=None):
    """Return only observed native prices, with bounded same-source recovery.

    Currently enabled for the Xetra and China instruments that lack a Norgate
    product. The existing US/Norgate and crypto source contracts are unchanged.
    A cache is acquisition evidence, never a freshness override.
    """
    venues = {".DE": "XETR", ".SZ": "XSHG", ".SS": "XSHG"}
    venue = next((v for suffix, v in venues.items() if symbol.endswith(suffix)), None)
    if venue is None:
        return frame
    if not re.fullmatch(r"[A-Za-z0-9._-]+", symbol):
        raise ValueError("invalid native observation cache symbol")
    bound = last_completed_session_on(effective_calendar(venue), now_utc)
    if bound is None:
        raise ValueError(f"{symbol}: completed venue session is unknown")
    if through is not None:
        bound = min(bound, pd.Timestamp(through).normalize())
    if end is not None:
        # Yahoo end is exclusive. Historical requests must not fetch the present.
        bound = min(bound, pd.Timestamp(end).normalize() - pd.Timedelta(days=1))
    # The consumer may use NYSE dates or an exclusive end on a weekend.
    # Resolve the bound to this instrument's most recent actual venue session.
    schedule = effective_calendar(venue).schedule(
        start_date=bound - pd.Timedelta(days=30), end_date=bound)
    if schedule.empty:
        raise ValueError(f"{symbol}: native session cutoff is unknown")
    bound = pd.Timestamp(schedule.index[-1]).tz_localize(None).normalize()
    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
    after_cutoff = frame.index[(frame.index > bound) & frame.notna().any(axis=1)]
    if len(after_cutoff):
        print(f"  {symbol} observed-price cutoff: excluded after {bound.date()}: "
              f"{[d.date().isoformat() for d in after_cutoff]}", flush=True)
    frame = frame.loc[frame.index <= bound].dropna(how="all")
    shape = "ohlc" if {"Open", "High", "Low", "Close"}.issubset(frame.columns) else "close"
    cache = Path(data_dir) / f"{symbol.lower()}_yahoo_observed_{shape}.parquet"
    cached = _read(cache)
    settled, record = settle_observed_prices(
        frame, symbol=symbol, venue=venue, through=bound, cached=cached,
        fetch_window=fetch_window, now_utc=now_utc, exclude_non_sessions=True)
    if record.get("excluded_non_session_dates") or record.get("excluded_retry_non_session_dates"):
        print(f"  {symbol} observed-price calendar: excluded non-trading dates "
              f"fresh={record['excluded_non_session_dates']}; "
              f"retry={record['excluded_retry_non_session_dates']}", flush=True)
    if record.get("retried_dates") or record.get("retained_dates") or record.get("unresolved_dates"):
        print(f"  {symbol} observed-price recovery: "
              f"retried={record.get('retried_dates', [])}; "
              f"retained={record.get('retained_dates', [])}; "
              f"unresolved={record.get('unresolved_dates', [])}; "
              f"refusals={record.get('refusals', [])}; "
              f"retry_error={record.get('retry_error')}", flush=True)
    # Never let an incidental short-window MTM fetch shrink the durable history.
    # A refused write does not substitute the cached frame for this acquisition.
    if not settled.empty:
        if _publishable(settled, cached):
            _write(cache, settled)
        if shape == "ohlc":
            close = project_close(settled)
            close_path = Path(data_dir) / f"{symbol.lower()}_yahoo_observed_close.parquet"
            prior = _read(close_path)
            if _publishable(close, prior):
                _write(close_path, close)
    return settled



@contextmanager
def _observation_lock(path):
    """One native-symbol writer; OS releases the lock if a process exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Native price capture already has a writer") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Native price capture already has a writer") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def settle_native_yahoo(frame, symbol, data_dir, *, now_utc, end=None,
                        through=None, fetch_window=None):
    if not symbol.endswith((".DE", ".SZ", ".SS")):
        return frame
    if not re.fullmatch(r"[A-Za-z0-9._-]+", symbol):
        raise ValueError("invalid native observation cache symbol")
    lock = Path(data_dir) / f"{symbol.lower()}_yahoo_observed.lock"
    with _observation_lock(lock):
        return _settle_native_yahoo(
            frame, symbol, data_dir, now_utc=now_utc, end=end,
            through=through, fetch_window=fetch_window)
