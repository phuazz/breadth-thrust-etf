"""Did a non-US line's own venue actually print the close a NYSE row carries?

WHY THIS EXISTS — 2026-10-28.

Sleeve C's 159801.SZ is priced in CNY on Shenzhen and converted onto the NYSE
calendar by run_thematic_rotation._fx_convert_to_usd, which forward-fills up to
ten calendar days so Golden Week and Chinese New Year do not blank the line.
That fill is right for the backtest. It is wrong as evidence: when the vendor
withholds or withdraws a completed Shenzhen session, the fill carries the
previous close onto the NYSE row, compute_signal produces a value there, and
live_targets._rank sees a complete, current row and ranks READY on a price the
venue never printed for that session. Nothing downstream can tell a holiday
fill from a missing bar, because after alignment they are the same number.

So the question is asked BEFORE alignment, of the dates on which a native close
was actually observed, and answered from the native venue's own close schedule:
for a NYSE decision row D, the required native session is the last native
session whose close is at or before NYSE's close on D. Shenzhen closes 15:00
Asia/Shanghai (07:00 UTC), so on a day both trade it is D itself; across a
Chinese holiday it is the last session before the closure, and the fill is
legitimate. The row is current only when the native close it carries IS that
session's.

A ROW IS CURRENT ONLY WITH ITS WHOLE SIGNAL WINDOW. Sleeve C's signal is a
200-row average of the ALIGNED column, so a withdrawn interior session -- 27
October missing, 28 October present -- leaves the 28 October row carrying its
own close while its signal averages in 26 October's close filled onto the
27th. The native sessions that enter NYSE rows are exactly the rows' required
sessions (Golden Week rows all require the pre-holiday close; a Shenzhen bar
on a US-only holiday is needed only when the next NYSE row falls on a
Shenzhen closure -- Good Friday 3 April 2026 before Qingming on the 6th --
and otherwise sits on no NYSE row), so ``current`` requires every one of
the ``window_rows`` rows the signal reads -- the row itself and the
``window_rows - 1`` before it -- to be row-current. The caller states the
window (sleeve C passes MA_PERIOD); there is no default, so no caller can get
a shorter certificate than its signal by omission.

Until 2026-10-29 this checked only the last ten rows, so a session withdrawn
eleven or more rows back still averaged a filled price into a signal marked
current. The production cache shows the shape is real: its 159801.SZ column
carries a fill on 2025-10-24, a Shenzhen trading day (250 NYSE rows before
2026-10-23, so outside that day's window).

FAILS CLOSED. No observation record, no venue, or a calendar that cannot be
read all make the row not-current, and so does a row whose window reaches
before the first row supplied, or whose required session predates the
bounded record. A false HOLD costs a week's re-rank; a false READY trades on
a price that does not exist. A session the calendar wrongly calls open (an
unannounced holiday) holds the sleeve while it sits in the window; the
missing date is named in ``missing_native_sessions``, and one past the
venue's holiday data (``attrs["holiday_data_through"]``) is labelled as
possibly unannounced. Pinning the closure in china_market_holidays clears
the HOLD on the next run, since every run recomputes the requirement.

Shenzhen shares Shanghai's date calendar (XSHG), as in mixed_market_tail.

THE RECORD BELONGS TO ONE FRAME, BUILT ONE WAY (2026-10-29 review). The cache
parquet and its sidecar are two files written one after the other, so a
failure between the writes leaves a new frame beside an old record with the
same last row; and a record written before the FX conversion moved ahead of
the NYSE-only reindex omits every Shenzhen session on a US holiday. A record
is therefore accepted only for the frame whose native columns and row index
hash to its ``content_sha256`` (content_digest), and only when its
``construction`` is SIDECAR_CONSTRUCTION. Either mismatch reads as no record:
the cache is refreshed and the freshness row is unknown.

ONE EFFECTIVE CALENDAR. native_schedule is the XSHG schedule with the
validated pins removed (china_market_holidays). Anything that asks "did
Shenzhen trade on this date" for sleeve C must ask it, so the window check and
any other reader cannot disagree about a pinned closure.

Python datetime months are 1-indexed (January = 1). Sessions come from
pandas_market_calendars via venue_calendars, never a weekday count.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pandas as pd

import china_market_holidays
from mixed_market_tail import venue_for
from venue_calendars import get_calendar

SIDECAR_KEY = "native_observations"
# How much observation history the cache sidecar keeps. live_targets inspects
# NATIVE_LOOKBACK_ROWS (15) NYSE rows plus each one's 200-row signal window --
# 214 rows, at most 318 calendar days on the NYSE calendar 2000-2026 -- and
# the oldest may require a session before the longest Chinese closure (about
# ten days). 400 days covers both with room to spare. A record written with a
# shorter window (the 60-day one of 2026-10-28) is not accepted as provenance
# (parse_sidecar), and a window that outgrows this one fails closed (HOLD),
# never open.
SIDECAR_WINDOW_DAYS = 400
# How far before the first row the native schedule is read, so the required
# session can be found across Golden Week or Chinese New Year.
NATIVE_WINDOW_DAYS = 30
SIDECAR_MEANING = (
    "dates on which a native-currency close and an FX rate were both observed "
    "and converted to USD; the cached USD column is forward-filled onto NYSE "
    "rows between these dates and those filled rows are NOT native "
    "observations")
# How the recorded dates were produced. Records without this value were
# written by 30ac1ed (FX conversion AFTER the NYSE-only crypto reindex), which
# dropped Shenzhen sessions on US holidays before they were recorded; they are
# refused. Change the value whenever the conversion or the recording changes
# what a record would list for the same vendor inputs.
SIDECAR_CONSTRUCTION = "fx-before-nyse-reindex/2026-10-29"

__all__ = ["venue_for", "session_dates", "observed_dates", "provenance",
           "native_schedule", "content_digest", "sidecar_record",
           "parse_sidecar"]


def session_dates(dates) -> pd.DatetimeIndex:
    """Normalise stamps to their session DATE, tz-naive, unique and sorted.

    A tz-aware stamp keeps its own wall-clock date: a Shenzhen bar stamped
    2026-10-28 00:00+08:00 is the 28 October session, not 27 October in UTC.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize().unique().sort_values()


def observed_dates(series: pd.Series) -> pd.DatetimeIndex:
    """Dates of finite, positive values in ``series`` — the observations."""
    vals = pd.to_numeric(series, errors="coerce").astype(float)
    ok = vals.notna() & (vals > 0) & ~vals.isin([math.inf, -math.inf])
    return session_dates(vals.index[ok.to_numpy()])


def native_schedule(venue: str | None, start, end):
    """``(schedule, holiday_data_through)`` for ``venue`` between ``start``
    and ``end``: the library's schedule with the validated pinned closures
    removed from a COPY (the shared calendar handle is never mutated).

    Raises when the venue is unmapped, the calendar cannot be read, or a pin
    disagrees with the library's own list for a year the library knows
    (china_market_holidays.checked_pins). Callers that judge a sleeve must
    treat a raise as "not established".
    """
    if not venue:
        raise ValueError("no native venue is mapped for this line")
    cal = get_calendar(venue)
    sched = cal.schedule(start_date=pd.Timestamp(start).strftime("%Y-%m-%d"),
                         end_date=pd.Timestamp(end).strftime("%Y-%m-%d"))
    pins = china_market_holidays.checked_pins(cal, venue)
    if len(pins):
        sched = sched.loc[~pd.DatetimeIndex(sched.index).normalize().isin(pins)]
    return sched, china_market_holidays.holiday_data_through(cal, venue)


def provenance(rows, observed, venue: str | None,
               reference: str = "NYSE", *,
               window_rows: int) -> pd.DataFrame:
    """Per NYSE row: the required native session, the native close carried,
    and whether the row and its whole signal window agree.

    ``rows`` are the rows the signal is computed over, in order (the frame's
    own index, not a filtered one). Columns ``required_native_session`` and
    ``native_close_carried`` hold Timestamps or None; ``row_current`` says the
    two agree on this row alone; ``window_complete`` says ``rows`` reaches the
    full ``window_rows`` back from this row; ``current`` says the window is
    complete and every row in it is row-current; ``missing_native_sessions``
    lists the required sessions in the window that were not carried. A row
    whose window is incomplete is never current, so a caller that passes too
    little history gets HOLD, not a shorter check. ``attrs["error"]`` carries
    the reason when the answer could not be established at all.
    """
    rows = session_dates(rows)
    out = pd.DataFrame({"required_native_session": [None] * len(rows),
                        "native_close_carried": [None] * len(rows),
                        "row_current": [False] * len(rows),
                        "window_complete": [False] * len(rows),
                        "current": [False] * len(rows),
                        "missing_native_sessions": [None] * len(rows)},
                       index=rows, dtype=object)
    out.attrs["error"] = None
    out.attrs["window_rows"] = window_rows
    # Last date the native venue's holiday data covers (china_market_holidays);
    # a required session after it may be an unannounced holiday.
    out.attrs["holiday_data_through"] = None
    if not len(rows):
        return out
    if not isinstance(window_rows, int) or isinstance(window_rows, bool) \
            or window_rows < 1:
        out.attrs["error"] = f"signal window not stated ({window_rows!r})"
        out["missing_native_sessions"] = pd.Series([[]] * len(rows),
                                                   index=rows, dtype=object)
        return out
    obs = None if observed is None else session_dates(observed)
    if obs is None:
        out.attrs["error"] = "no native observation record for this run"
    ref = nat = None
    try:
        if not venue:
            raise ValueError("no native venue is mapped for this line")
        ref = get_calendar(reference).schedule(
            start_date=rows.min().strftime("%Y-%m-%d"),
            end_date=rows.max().strftime("%Y-%m-%d"))
        # Closures the library does not know yet come from the official
        # notice (china_market_holidays); a pin that contradicts the library
        # raises here and the row fails closed.
        nat, bound = native_schedule(
            venue, rows.min() - pd.Timedelta(days=NATIVE_WINDOW_DAYS),
            rows.max() + pd.Timedelta(days=1))
        out.attrs["holiday_data_through"] = bound
    except Exception as exc:  # noqa: BLE001 — any calendar fault fails closed
        out.attrs["error"] = f"native calendar unavailable ({exc})"
        ref = nat = None
    ref_idx = session_dates(ref.index) if ref is not None else None
    for r in rows:
        if obs is not None:
            before = obs[obs <= r]
            if len(before):
                out.at[r, "native_close_carried"] = before[-1]
        if ref is None or nat is None or r not in ref_idx:
            continue
        ref_close = ref["market_close"].iloc[ref_idx.get_loc(r)]
        closed = nat.index[(nat["market_close"] <= ref_close).to_numpy()]
        if not len(closed):
            continue
        required = session_dates(closed)[-1]
        out.at[r, "required_native_session"] = required
        out.at[r, "row_current"] = bool(
            out.at[r, "native_close_carried"] is not None
            and out.at[r, "native_close_carried"] == required)
    ok = out["row_current"].astype(bool).to_numpy()
    req = out["required_native_session"].to_numpy()
    # Running count of failed rows, so each window is O(1) over 200 rows.
    bad = (~ok).cumsum()
    current, complete, missing = [], [], []
    for i in range(len(rows)):
        lo = i - window_rows + 1
        full = lo >= 0
        lo = max(0, lo)
        n_bad = int(bad[i] - (bad[lo - 1] if lo > 0 else 0))
        complete.append(full)
        current.append(bool(full and n_bad == 0))
        missing.append(sorted({req[j] for j in range(lo, i + 1)
                               if not ok[j] and req[j] is not None})
                       if n_bad else [])
    out["window_complete"] = pd.Series(complete, index=rows, dtype=object)
    out["current"] = pd.Series(current, index=rows, dtype=object)
    out["missing_native_sessions"] = pd.Series(missing, index=rows, dtype=object)
    return out


def content_digest(frame: pd.DataFrame, tickers) -> str:
    """SHA-256 of ``frame``'s row index and its ``tickers`` columns.

    What a native record attests to: which dates were observed for THESE
    values on THESE rows. Other columns are left out on purpose, so a repair
    to a US line (repair_price_gaps rewrites the parquet without touching the
    sidecar) does not void the native record, while any change to a native
    value, or any added or removed row, does. Canonical bytes, independent of
    parquet encoding and of the index's datetime resolution: ISO stamps, then
    each column's name and little-endian float64 values with one NaN form.
    """
    h = hashlib.sha256()
    idx = pd.DatetimeIndex(frame.index)
    h.update(f"tz={idx.tz}\n".encode())
    h.update("\n".join(idx.strftime("%Y-%m-%dT%H:%M:%S.%f")).encode())
    for t in sorted(str(x) for x in tickers):
        h.update(f"\ncolumn={t}\n".encode())
        if t not in frame.columns:
            h.update(b"<absent>")
            continue
        vals = pd.to_numeric(frame[t], errors="coerce").to_numpy(
            dtype="float64", na_value=np.nan).copy()
        vals[np.isnan(vals)] = np.nan
        h.update(vals.astype("<f8").tobytes())
    return h.hexdigest()


def sidecar_record(observed_by_line: dict, frame: pd.DataFrame) -> dict:
    """The bounded, truthful record of native observations beside a cache.

    Only dates that WERE observed are listed. ``frame`` is the frame written
    to the cache: its last row is ``through``, and the digest of its index and
    the recorded lines' columns is ``content_sha256``, so a reader can tell
    whether this record belongs to the frame it sits beside (parse_sidecar).
    Dates older than SIDECAR_WINDOW_DAYS before ``through`` are dropped.
    """
    end = pd.Timestamp(frame.index.max()).normalize()
    start = end - pd.Timedelta(days=SIDECAR_WINDOW_DAYS)
    lines = {}
    for ticker, dates in (observed_by_line or {}).items():
        d = session_dates(dates) if dates is not None else pd.DatetimeIndex([])
        kept = d[(d >= start) & (d <= end)]
        lines[str(ticker)] = {
            "venue": venue_for(str(ticker)),
            "dates": [str(x.date()) for x in kept],
            "last": str(d[-1].date()) if len(d) else None,
        }
    return {"meaning": SIDECAR_MEANING,
            "construction": SIDECAR_CONSTRUCTION,
            "through": str(end.date()),
            "window_days": SIDECAR_WINDOW_DAYS,
            "content_sha256": content_digest(frame, lines),
            "lines": lines}


def parse_sidecar(sidecar: dict | None, frame: pd.DataFrame | None,
                  tickers) -> dict | None:
    """``{ticker: DatetimeIndex}`` from a cache sidecar, or None.

    None unless the record exists, has the minimum shape, was built under
    SIDECAR_CONSTRUCTION, names every one of ``tickers``, was written for a
    frame ending on ``frame``'s last row, keeps at least SIDECAR_WINDOW_DAYS
    of history, and its ``content_sha256`` is the digest of ``frame``'s rows
    and recorded columns as they are now. A partial, mismatched, too-short or
    differently built record is treated as absent: provenance that does not
    belong to the frame in hand, or cannot reach back over the signal window,
    is not provenance. Never raises.
    """
    try:
        if frame is None or not len(frame):
            return None
        rec = (sidecar or {}).get(SIDECAR_KEY)
        if not isinstance(rec, dict) or not isinstance(rec.get("lines"), dict):
            return None
        if rec.get("construction") != SIDECAR_CONSTRUCTION:
            return None
        through = pd.Timestamp(frame.index.max()).normalize()
        if rec.get("through") != str(through.date()):
            return None
        window = rec.get("window_days")
        if (not isinstance(window, int) or isinstance(window, bool)
                or window < SIDECAR_WINDOW_DAYS):
            return None
        digest = rec.get("content_sha256")
        if (not isinstance(digest, str)
                or any(str(t) not in frame.columns for t in rec["lines"])
                or digest != content_digest(frame, rec["lines"])):
            return None
        out = {}
        for t in tickers:
            line = rec["lines"].get(t)
            if not isinstance(line, dict) or not isinstance(line.get("dates"), list):
                return None
            out[t] = session_dates(line["dates"])
        return out
    except (TypeError, ValueError, AttributeError):
        return None
