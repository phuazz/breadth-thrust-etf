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

Python datetime months are 1-indexed (January = 1). Sessions come from
pandas_market_calendars via venue_calendars, never a weekday count.
"""

from __future__ import annotations

import math

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

__all__ = ["venue_for", "session_dates", "observed_dates", "provenance",
           "sidecar_record", "parse_sidecar"]


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
        nat_cal = get_calendar(venue)
        nat = nat_cal.schedule(
            start_date=(rows.min() - pd.Timedelta(days=NATIVE_WINDOW_DAYS)
                        ).strftime("%Y-%m-%d"),
            end_date=(rows.max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
        # Closures the library does not know yet, from the official notice
        # (china_market_holidays). Removed from a COPY: the shared calendar
        # handle is never mutated (venue_calendars).
        pins = china_market_holidays.pinned_closures(venue)
        if len(pins):
            nat = nat.loc[~pd.DatetimeIndex(nat.index).normalize().isin(pins)]
        bound = china_market_holidays.holiday_data_through(nat_cal, venue)
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


def sidecar_record(observed_by_line: dict, through) -> dict:
    """The bounded, truthful record of native observations beside a cache.

    Only dates that WERE observed are listed; ``through`` is the cache's last
    row, so a reader can tell whether this record belongs to the cache it sits
    beside. Dates older than SIDECAR_WINDOW_DAYS before ``through`` are dropped.
    """
    end = pd.Timestamp(through).normalize()
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
            "through": str(end.date()),
            "window_days": SIDECAR_WINDOW_DAYS,
            "lines": lines}


def parse_sidecar(sidecar: dict | None, tickers, through) -> dict | None:
    """``{ticker: DatetimeIndex}`` from a cache sidecar, or None.

    None unless the record exists, has the minimum shape, names every one of
    ``tickers``, was written for a cache ending at ``through`` and keeps at
    least SIDECAR_WINDOW_DAYS of history. A partial, mismatched or too-short
    record is treated as absent: provenance that does not belong to the frame
    in hand, or cannot reach back over the signal window, is not provenance.
    Never raises.
    """
    try:
        rec = (sidecar or {}).get(SIDECAR_KEY)
        if not isinstance(rec, dict) or not isinstance(rec.get("lines"), dict):
            return None
        if rec.get("through") != str(pd.Timestamp(through).normalize().date()):
            return None
        window = rec.get("window_days")
        if (not isinstance(window, int) or isinstance(window, bool)
                or window < SIDECAR_WINDOW_DAYS):
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
