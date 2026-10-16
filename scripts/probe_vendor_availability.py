"""How late is the vendor, per venue, measured rather than assumed.

WHY.

On 2026-08-14 the Xetra .DE lines had not published Thursday 13 August at
Friday decision time, and had by Saturday. Friday's own bar was still missing
nine hours after Friday's close and had arrived by Saturday morning. That is
consistent with a roughly one-session publication lag on the European ETF
lines, against none on the US proxies.

It is also TWO OBSERVATIONS. It was enough to know the old publish guard had
its comparison backwards, and it is nowhere near enough to move a rebalance
day on. A cadence decision — Friday against Monday, or splitting Strategy D
onto its own day — should rest on weeks of measurement, not on a weekend's
worth of anecdote, and this exists to produce that measurement.

WHAT IT RECORDS. One line per run: for each probed ticker, the last bar the
vendor serves, and how many sessions that sits behind the venue's last
COMPLETED session. Zero means current. One means a session late. Append-only
JSONL, so a run is a sample and the file is the series.

SCHEMA 2 (2026-10-15, vendor-resilience Phase 1). Venue rows now PROVE the
required completed session rather than reading the newest row: last_bar is the
newest valid (finite, positive) close on a venue session at or before the
required session, an unfinished later row is recorded as unsettled_bar and
never counts, and checked_days / missing_completed_days state exactly which
sessions the row can vouch for. Earlier rows are left as written; they stay
readable and prove no session (session_evidence returns None for them).

Deliberately NOT a guard. It never fails a pipeline and nothing gates on it.
Its output is evidence for a decision a human makes later, and a probe that
can break a refresh would get switched off before it had collected anything.

Usage:
    python scripts/probe_vendor_availability.py
    python scripts/probe_vendor_availability.py --summary
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from completed_prices import crypto_completed_through  # noqa: E402
from venue_calendars import get_calendar as _venue_cal  # noqa: E402
from session_bounds import last_completed_session_on  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG = PROJECT_ROOT / "data" / "vendor_availability_log.jsonl"

# One liquid line per venue-and-role, not the whole universe: the question is
# about the VENUE's publication behaviour, and probing 24 tickers to answer it
# would cost a rate limit for no extra information.
PROBES = [
    ("SPY", "NYSE", "US ETF proxy"),
    ("XLF", "NYSE", "US ETF proxy"),
    ("EXV1.DE", "XETR", "Europe ETF line"),
    ("EXH4.DE", "XETR", "Europe ETF line"),
    ("EXV3.DE", "XETR", "Europe ETF line"),
    ("SAP.DE", "XETR", "Europe constituent"),   # the SIGNAL side, which was
    ("SIE.DE", "XETR", "Europe constituent"),   # never late on 2026-08-14
    # ADDED 2026-09-09. SPY and XLF cannot answer "has the US side settled":
    # both carried 2026-09-08 by 20:38 UTC that day and kept it, while the
    # IUMS panel capped at 2026-09-04 because LIN, CRH, SW and AMCR were
    # unserved at 01:24 UTC — and sleeve A reported HOLD on 13 of its 14
    # panels for it. All four carried 09-08 by 06:42 UTC, so the hour at
    # which they settle sits between those two and nothing measures it. That
    # hour is what decides whether the 01:00 UTC refresh is simply too early.
    #
    # Norgate does not cover for this group. Under --price-source auto their
    # columns stay on the incumbent because the Norgate series is not a date
    # superset of the cached one — measured 2026-09-09 on IUMS, where AMCR,
    # CRH, DD, LIN and SW all start later at Norgate than in the cache
    # (Praxair/Linde, Bemis/Amcor, WestRock/Smurfit Westrock, CRH's US
    # listing, DuPont). The names that drag are exactly the names the mixed
    # source cannot reach, so the lag is a property of the schedule, not of
    # the feed choice. LIN is the liquid representative.
    ("LIN", "NYSE", "US constituent (foreign domicile)"),
    # ADDED 2026-10-11. The two sleeve C lines Norgate cannot reach, and the
    # two this log was blind on when it mattered. The 2026-10-10 core run
    # held sleeve C because yfinance was not serving BTC-USD's Friday close
    # (the engine's single-ticker re-probe answered without the bar); the
    # vendor backfilled it by Sunday 01:16 UTC, and establishing those two
    # facts took a log archaeology session because no probe row covered
    # either name. "CRYPTO" is not an exchange venue: BTC-USD trades every
    # UTC day, so its reference is the last COMPLETED UTC day
    # (completed_prices.crypto_completed_through) and sessions_behind counts
    # calendar days. 159801.SZ rides the SSE calendar (Shenzhen shares the
    # holiday schedule), which is also what exonerates it on Golden Week.
    ("BTC-USD", "CRYPTO", "C crypto line (UTC day bars)"),
    ("159801.SZ", "SSE", "C China A-share line"),
]


# ROW SCHEMA (2026-10-15, vendor-resilience Phase 1). Schema 2 records carry
# "schema": 2 at the top level and, on every venue row, the completed-session
# proof below. Rows without the marker are schema 1: still readable, never
# rewritten, and never able to prove that an unseen session was present.
PROBE_SCHEMA = 2

# How many of the venue's own completed sessions a row inspects for a valid
# bar. Five spans the retraction lookback (twelve rows, about three days)
# plus a weekend, so an incident session the tripwire can still see is one
# the newest row can prove present or absent.
VENUE_EVIDENCE_WINDOW_SESSIONS = 5

# Venues whose holidays are announced year by year. Beyond the last year the
# calendar's ad-hoc list covers (last listed holiday 2026-10-07 in
# pandas_market_calendars 5.4.0, measured 2026-10-15), a required session is
# UNKNOWN, not a fact: XSHG then invents sessions (it trades 2027-01-01 and
# 2027-02-05..11), and SSE's 2027 closures (2027-01-01 and a Chinese New Year
# week) come from rules, not from the exchange's announcement. Corrected
# 2026-10-15: the first version of this note said SSE traded 2027-01-01; it
# is XSHG that does. Rule-based calendars (NYSE, XETR) are not listed: their
# ad-hoc lists are historical closures, not their holiday source.
ADHOC_HOLIDAY_VENUES = frozenset({"SSE", "XSHG"})


class CalendarUnknown(RuntimeError):
    """The venue calendar cannot place the required session."""


def calendar_covered_through(cal, venue: str):
    """Last date the venue calendar's holiday data is known to cover, or
    None when the calendar is rule-based and has no such bound. For an
    ad-hoc venue the bound is 31 December of the last listed holiday's
    year (Python months are 1-indexed)."""
    if venue not in ADHOC_HOLIDAY_VENUES:
        return None
    listed = list(getattr(cal, "adhoc_holidays", []) or [])
    if not listed:
        raise CalendarUnknown(f"{venue} calendar lists no holidays at all")
    last = pd.Timestamp(max(listed))
    return pd.Timestamp(year=last.year, month=12, day=31)


def required_session(venue: str, now_utc: datetime) -> pd.Timestamp:
    """The venue session a current verdict must prove: the most recent one
    whose CLOSE (from the venue's own schedule, so early closes and DST are
    the calendar's answer, never a constant) is at or before ``now_utc``.

    Raises CalendarUnknown when the calendar is missing, yields nothing, or
    the date lies beyond the calendar's known holiday data. Never falls back
    to another venue's calendar."""
    try:
        cal = _venue_cal(venue)
    except Exception as exc:  # noqa: BLE001 — unknown venue or library absent
        raise CalendarUnknown(f"no calendar for venue {venue!r}: {exc!r}") from exc
    try:
        bound = last_completed_session_on(cal, now_utc)
    except Exception as exc:  # noqa: BLE001
        raise CalendarUnknown(f"{venue} schedule failed: {exc!r}") from exc
    if bound is None:
        raise CalendarUnknown(f"{venue} calendar yields no completed session")
    covered = calendar_covered_through(cal, venue)
    today = pd.Timestamp(now_utc).tz_convert("UTC").tz_localize(None).normalize()
    if covered is not None and today > covered:
        raise CalendarUnknown(
            f"{venue} holiday data ends {covered.date()}; sessions after it "
            f"are the calendar's guess, not the venue's schedule")
    return bound


def _daily_dates(index) -> pd.DatetimeIndex:
    """The calendar date each daily label names. A timezone-aware label is
    read at its own wall clock (a Shanghai bar stamped 2026-10-14 00:00+08:00
    is the 14 October session), never converted to UTC first, which would
    move every Asian bar to the previous day."""
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def _clean_daily_close(series) -> tuple[pd.Series, list[str], list[str]]:
    """(valid closes by date, invalid days, ambiguous days).

    A valid close is finite and strictly positive. NaN is absence (a batch
    download aligns every ticker to the union of dates, so NaN is how "no
    bar" arrives). Zero, negative and infinite values are INVALID: a row
    exists and is rejected. Duplicate labels are judged conservatively: one
    date carrying an invalid value beside a valid one, or two valid values
    that disagree, is AMBIGUOUS and proves nothing for that date."""
    if series is None or len(series) == 0:
        return pd.Series(dtype=float), [], []
    s = pd.to_numeric(pd.Series(series), errors="coerce")
    s.index = _daily_dates(s.index)
    valid, invalid, ambiguous = {}, set(), set()
    for day, group in s.groupby(level=0, sort=True):
        values = [float(v) for v in group.to_numpy(dtype=float)]
        good = [v for v in values if math.isfinite(v) and v > 0]
        has_invalid = any(not math.isnan(v) and not (math.isfinite(v) and v > 0)
                          for v in values)
        vals = set(good)
        if has_invalid and vals:
            ambiguous.add(day)
        elif len(vals) > 1:
            ambiguous.add(day)
        elif has_invalid:
            invalid.add(day)
        elif vals:
            valid[day] = vals.pop()
    out = pd.Series(valid, dtype=float).sort_index()
    return (out, sorted(d.date().isoformat() for d in invalid),
            sorted(d.date().isoformat() for d in ambiguous))


def venue_evidence(series, venue: str, now_utc: datetime,
                   window_sessions: int = VENUE_EVIDENCE_WINDOW_SESSIONS) -> dict:
    """Completed-session proof for one non-crypto line from one response.

    ``last_bar`` is the newest VALID bar on a venue session at or before the
    required session, so an unfinished later row can never make a line read
    current and the lag is never negative. ``required_day_bar_present`` is
    True only when the required session itself carries a valid close.
    ``checked_days`` is the explicit scope of the proof: a session outside
    it is unknown, whatever ``last_bar`` says."""
    try:
        bound = required_session(venue, now_utc)
        cal = _venue_cal(venue)
        sched = cal.schedule(start_date=bound - pd.Timedelta(days=45),
                             end_date=bound)
        sessions = [pd.Timestamp(d).normalize() for d in sched.index]
        if not sessions or sessions[-1] != bound:
            raise CalendarUnknown(f"{venue} schedule does not end on {bound.date()}")
    except CalendarUnknown as exc:
        return {"calendar_error": str(exc), "last_bar": None,
                "last_completed_session": None, "sessions_behind": None,
                "required_day_bar_present": None,
                "missing_completed_days": None, "checked_days": [],
                "invalid_days": [], "ambiguous_days": [],
                "unsettled_bar": None, "off_calendar_days": []}
    valid, invalid, ambiguous = _clean_daily_close(series)
    session_set = set(sessions)
    settled = valid[[d in session_set for d in valid.index]] if len(valid) else valid
    later = valid[valid.index > bound] if len(valid) else valid
    off = [d for d in valid.index if d <= bound and d not in session_set]
    window = sessions[-window_sessions:]
    present = set(settled.index)
    last_bar = pd.Timestamp(settled.index.max()) if len(settled) else None
    return {
        "last_bar": str(last_bar.date()) if last_bar is not None else None,
        "last_completed_session": str(bound.date()),
        "sessions_behind": _sessions_behind(cal, last_bar, bound),
        "required_day_bar_present": bound in present,
        "missing_completed_days": [d.date().isoformat() for d in window
                                   if d not in present],
        "checked_days": [d.date().isoformat() for d in window],
        "invalid_days": [d for d in invalid if pd.Timestamp(d) <= bound],
        "ambiguous_days": [d for d in ambiguous if pd.Timestamp(d) <= bound],
        # Recorded, never proof: a later row is the session still trading
        # (or a vendor stamp ahead of the close), whatever its value.
        "unsettled_bar": (str(pd.Timestamp(later.index.max()).date())
                          if len(later) else None),
        "off_calendar_days": [d.date().isoformat() for d in off],
    }


def session_evidence(row: dict | None, day) -> bool | None:
    """Does this probe row PROVE a valid bar for ``day``? True, False, or
    None when the row cannot say.

    Only a COMPLETE and CONSISTENT proof record answers, and only for days
    inside its ``checked_days`` scope (review F4). Anything else is None:
    a schema-1 row, a partial row, a calendar failure, a day outside the
    window, a missing or unparseable completed bound, a window that does not
    end on that bound, missing days outside the window, a required-bar flag
    that disagrees with the missing list, invalid or ambiguous days not
    counted missing, an evidence source that is not a provider response, or
    a last_bar earlier than a day the record calls present. A later
    ``last_bar`` is never read as proof that an earlier session is present.

    The ENCLOSING record's schema is the caller's check: this sees one row."""
    if not isinstance(row, dict) or row.get("calendar_error"):
        return None
    checked, missing = row.get("checked_days"), row.get("missing_completed_days")
    if not isinstance(checked, list) or not checked or not isinstance(missing, list):
        return None
    bound = _iso_day(row.get("last_completed_session"))
    checked_d = [_iso_day(d) for d in checked]
    missing_d = [_iso_day(d) for d in missing]
    if bound is None or None in checked_d or None in missing_d:
        return None
    if max(checked_d) != bound or not set(missing_d) <= set(checked_d):
        return None
    required = row.get("required_day_bar_present")
    if not isinstance(required, bool) or required != (bound not in missing_d):
        return None
    for name in ("invalid_days", "ambiguous_days"):
        listed = row.get(name, [])
        if not isinstance(listed, list):
            return None
        listed_d = [_iso_day(d) for d in listed]
        if None in listed_d or not ({d for d in listed_d if d in checked_d}
                                    <= set(missing_d)):
            return None
    # Completion and calendar contradictions (review R5): a settled last_bar
    # can never lie past the completed bound, a later unfinished row can never
    # sit at or before it, and a checked day cannot be off the venue calendar.
    lb = row.get("last_bar")
    if lb is not None and (_iso_day(lb) is None or _iso_day(lb) > bound):
        return None
    unsettled = row.get("unsettled_bar")
    if unsettled is not None and (_iso_day(unsettled) is None
                                  or _iso_day(unsettled) <= bound):
        return None
    off = row.get("off_calendar_days", [])
    if not isinstance(off, list):
        return None
    off_d = [_iso_day(d) for d in off]
    if None in off_d or set(off_d) & set(checked_d):
        return None
    evidence = row.get("evidence")
    if evidence not in ("batch", "refetch", "none"):
        return None
    if evidence == "none" and set(missing_d) != set(checked_d):
        return None
    target = _iso_day(day)
    if target is None or target not in checked_d:
        return None
    if target in missing_d:
        return False
    last_bar = _iso_day(row.get("last_bar"))
    if last_bar is None or last_bar < target:
        return None
    return True


def _iso_day(value):
    """A strict ISO calendar date (YYYY-MM-DD), or None."""
    from datetime import date as _date
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError:
        return None


def _sessions_behind(cal, last_bar, lcs) -> int | None:
    if last_bar is None or lcs is None:
        return None
    lo, hi = sorted((pd.Timestamp(last_bar), pd.Timestamp(lcs)))
    sched = cal.schedule(start_date=lo, end_date=hi)
    n = max(len(sched) - 1, 0)
    return n if pd.Timestamp(last_bar) <= pd.Timestamp(lcs) else -n


def _days_behind(last_bar, lcs) -> int | None:
    """Crypto variant of _sessions_behind: every UTC day is a session, so
    behind is a plain calendar-day difference. Crypto callers pass a SETTLED
    bar (at or before the completed bound), so the crypto row never reports
    negative: an in-progress row beyond the bound is ignored outright rather
    than read as "ahead" — a newer intraday bar proves nothing about the
    settled days beneath it (adversarial review P2, 2026-10-11)."""
    if last_bar is None or lcs is None:
        return None
    return int((pd.Timestamp(lcs).normalize()
                - pd.Timestamp(last_bar).normalize()).days)


# The trailing completed-day window the crypto row inspects for holes. Five
# days spans a weekend plus the decision Friday with margin, and matches the
# practical depth of the batch download the probe rides on.
CRYPTO_HOLE_WINDOW_DAYS = 5


def _settled_crypto(series: pd.Series, bound: pd.Timestamp,
                    window_days: int = CRYPTO_HOLE_WINDOW_DAYS,
                    ) -> tuple[pd.Timestamp | None, list[str], bool]:
    """(settled last bar, missing completed days, bound-day bar present).

    The settled bar is the newest FINITE POSITIVE value at or before
    ``bound``. Rows after ``bound`` are in-progress bars and are ignored
    entirely: the adversarial review reproduced a Sunday response carrying
    8 and 11 October but neither 9 nor 10 October that the previous logic
    read as sessions_behind=-1, current. Missing days are the completed UTC
    days in the trailing window with no finite positive value, so an
    internal hole stays visible even when a newer settled bar exists. A
    non-finite or non-positive value is a hole, not a bar."""
    bound = pd.Timestamp(bound).normalize()
    s = series.dropna() if series is not None else pd.Series(dtype=float)
    if len(s):
        idx = pd.to_datetime(s.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        s.index = idx.normalize()
        s = s[(s > 0) & (s < float("inf"))]
        s = s[~s.index.duplicated(keep="last")]
    settled = s[s.index <= bound] if len(s) else s
    last_bar = pd.Timestamp(settled.index.max()) if len(settled) else None
    window = [bound - pd.Timedelta(days=i) for i in range(window_days)]
    present = set(settled.index)
    missing = sorted(d.date().isoformat() for d in window if d not in present)
    return last_bar, missing, bound in present


# SINGLE-TICKER RE-FETCH (2026-09-30). Every null last_bar in the log up to
# 2026-09-30 (12 in ~180 probes) was ONE ticker returned empty by the batch
# download while its venue peers were served, and every one was back on the
# next probe. check_vendor_probe reads a null after a recorded bar as a total
# withdrawal, so each of these mailed a [WARN] retraction (SIE.DE, 2026-09-30
# 03:46 UTC, while EXV1/EXH4/EXV3/SAP took the routine one-session step). A
# line that comes back empty is asked again on its own before a null is
# written. A whole-venue outage still returns nothing on the re-fetch and is
# still recorded as null, which is the shape the tripwire exists to catch.
REFETCH_ATTEMPTS = 2
REFETCH_PAUSE_SECONDS = 3.0


# _last_bar (the newest non-null row, intraday included) was removed on
# 2026-10-15: it is the shape that let an unfinished 159801.SZ row read as
# current over a possibly absent required session. venue_evidence replaces it.


def _close_frame(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """The Close panel, or an empty frame when the response has none. A
    response without a Close column is an unserved line, not a crash."""
    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            return pd.DataFrame()
        return raw["Close"]
    if "Close" not in raw.columns:
        return pd.DataFrame()
    return raw[["Close"]].rename(columns={"Close": tickers[0]})


def decision_input_complete(row: dict) -> bool | None:
    """Three different statements hide in one probe row: something was
    SERVED (observation), the newest settled bar is FRESH (lag), and the
    REQUIRED completed days are all PRESENT (completeness). Consumers that
    collapse them re-create the 2026-10-11 false assurance, where a
    Saturday bar over a missing Friday read as current.

    True/False for rows that carry the hole fields (crypto rows since
    2026-10-11, venue rows since schema 2 on 2026-10-15); None for rows that
    do not (schema-1 venue rows, earlier crypto rows) and for rows whose
    calendar could not place the required session — unknown is never
    reported as complete."""
    if ("missing_completed_days" not in row
            and "required_day_bar_present" not in row):
        return None
    if row.get("calendar_error") or (
            "required_day_bar_present" in row
            and row.get("required_day_bar_present") is None):
        return None
    if row.get("missing_completed_days") is None and "missing_completed_days" in row:
        return None
    return (not row.get("missing_completed_days")
            and row.get("required_day_bar_present") is not False)


def _refetch_single_series(download, tk: str, start: str, end: str,
                           attempts: int = REFETCH_ATTEMPTS,
                           pause: float = REFETCH_PAUSE_SECONDS,
                           sleep=None) -> pd.Series | None:
    """The single-ticker close series, or None when the vendor gave nothing.
    The crypto row needs the SERIES (its verdict reads specific completed
    days, not only the newest bar); the venue rows keep the last-bar shape
    via _refetch_single below."""
    import time
    sleep = sleep or time.sleep
    for i in range(attempts):
        if i:
            sleep(pause)
        try:
            raw = download([tk], start=start, end=end, auto_adjust=True,
                           progress=False, group_by="column")
        except Exception:  # noqa: BLE001 — a failed re-fetch is still unserved
            continue
        frame = _close_frame(raw, [tk])
        if tk in frame.columns:
            s = frame[tk].dropna()
            if len(s):
                return s
    return None


def _refetch_single(download, tk: str, start: str, end: str,
                    attempts: int = REFETCH_ATTEMPTS,
                    pause: float = REFETCH_PAUSE_SECONDS,
                    sleep=None) -> pd.Timestamp | None:
    """Ask for one ticker alone, a few times, before recording it unserved."""
    s = _refetch_single_series(download, tk, start, end,
                               attempts=attempts, pause=pause, sleep=sleep)
    return pd.Timestamp(s.index.max()).normalize() if s is not None and len(s) else None


def probe(now_utc: datetime | None = None, download=None) -> dict:
    if download is None:
        import yfinance as yf
        download = yf.download
    now = now_utc or datetime.now(timezone.utc)
    start = (now - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    end = (now + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    tickers = [t for t, _, _ in PROBES]
    raw = download(tickers, start=start, end=end, auto_adjust=True,
                   progress=False, group_by="column")
    close = _close_frame(raw, tickers)

    rows = []
    for tk, venue, role in PROBES:
        if venue == "CRYPTO":
            # Not an exchange: the reference is the last completed UTC day,
            # lateness counts calendar days, and the verdict reads SETTLED
            # bars only — plus the explicit holes and bound-day presence the
            # adversarial review required (see _settled_crypto). Holes
            # trigger the single-ticker re-ask just as an empty batch line
            # does, so "the batch missed it" and "the vendor has no bar"
            # stay distinguishable in the record.
            bound = pd.Timestamp(crypto_completed_through(now))
            series = close[tk] if tk in close.columns else pd.Series(dtype=float)
            last_bar, missing, required_present = _settled_crypto(series, bound)
            refetched = False
            if last_bar is None or missing:
                again = _refetch_single_series(download, tk, start, end)
                if again is not None:
                    last_bar, missing, required_present = _settled_crypto(again, bound)
                    refetched = True
            row = {
                "ticker": tk, "venue": venue, "role": role,
                "last_bar": str(last_bar.date()) if last_bar is not None else None,
                "last_completed_session": str(bound.date()),
                "sessions_behind": _days_behind(last_bar, bound),
                "required_day_bar_present": required_present,
                "missing_completed_days": missing,
                # Schema 2: the explicit scope of the hole verdict above,
                # the same trailing window _settled_crypto inspected.
                "checked_days": sorted(
                    (bound - pd.Timedelta(days=i)).date().isoformat()
                    for i in range(CRYPTO_HOLE_WINDOW_DAYS)),
                "evidence": "refetch" if refetched else "batch",
            }
            if refetched:
                row["refetched"] = True
            rows.append(row)
            continue
        row = {"ticker": tk, "venue": venue, "role": role}
        series = close[tk] if tk in close.columns else None
        batch = venue_evidence(series, venue, now)
        chosen, source, refetch = batch, "batch", None
        if batch.get("calendar_error") is None and not batch["required_day_bar_present"]:
            # The REQUIRED session is absent or invalid in the batch answer,
            # whether or not older rows came back: ask for the line alone, a
            # bounded number of times. The two answers are kept apart; the
            # verdict reads whichever one proves the required session, and
            # otherwise the batch, unless the batch served nothing at all.
            refetch = _refetch_venue(download, tk, venue, now, start, end)
            if refetch["evidence"] is not None and (
                    refetch["evidence"]["required_day_bar_present"]
                    or batch["last_bar"] is None):
                chosen, source = refetch["evidence"], "refetch"
        row.update(chosen)
        row["evidence"] = source if chosen.get("last_bar") is not None else "none"
        if refetch is not None:
            row["refetch"] = {"attempts": refetch["attempts"],
                              "result": refetch["result"]}
        # Recorded so the series stays honest about which bars the batch
        # call missed; absent on the ordinary path.
        if source == "refetch" and chosen.get("last_bar") is not None:
            row["refetched"] = True
        rows.append(row)
    return {"schema": PROBE_SCHEMA,
            "probed_at_utc": now.isoformat(timespec="seconds"), "rows": rows}


def _refetch_venue(download, tk: str, venue: str, now, start: str, end: str,
                   attempts: int | None = None, pause: float | None = None,
                   sleep=None) -> dict:
    """Ask for one venue line alone until a response proves the required
    session, at most ``attempts`` times. Returns the attempts made, a
    result label and the evidence of the last non-empty response."""
    import time
    attempts = REFETCH_ATTEMPTS if attempts is None else attempts
    pause = REFETCH_PAUSE_SECONDS if pause is None else pause
    sleep = sleep or time.sleep
    best, made, errors = None, 0, 0
    for i in range(attempts):
        if i:
            sleep(pause)
        made += 1
        try:
            raw = download([tk], start=start, end=end, auto_adjust=True,
                           progress=False, group_by="column")
        except Exception:  # noqa: BLE001 — a failed re-fetch is still unserved
            errors += 1
            continue
        frame = _close_frame(raw, [tk])
        if tk not in frame.columns or frame[tk].dropna().empty:
            continue
        ev = venue_evidence(frame[tk], venue, now)
        best = ev
        if ev.get("required_day_bar_present"):
            return {"attempts": made, "result": "proved", "evidence": ev}
    if best is not None:
        result = "not_proved"
    else:
        result = "error" if errors == made else "empty"
    return {"attempts": made, "result": result, "evidence": best}


def summarise() -> None:
    if not LOG.exists():
        print(f"no log yet at {LOG}")
        return
    recs = [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
    print(f"{len(recs)} probe(s) since {recs[0]['probed_at_utc'][:10]}\n")
    by: dict[str, list[int]] = {}
    holes_latest: dict[str, list[str]] = {}
    holes_count: dict[str, int] = {}
    for r in recs:
        for row in r["rows"]:
            key = f"{row['ticker']} ({row['role']})"
            if row["sessions_behind"] is not None:
                by.setdefault(key, []).append(row["sessions_behind"])
            if decision_input_complete(row) is False:
                holes_count[key] = holes_count.get(key, 0) + 1
                holes_latest[key] = list(row.get("missing_completed_days") or [])
    print(f"  {'line':40s} {'n':>3s} {'mean':>6s} {'max':>4s}  distribution")
    for k, v in sorted(by.items()):
        dist = {b: v.count(b) for b in sorted(set(v))}
        line = (f"  {k:40s} {len(v):3d} {sum(v)/len(v):6.2f} {max(v):4d}  "
                + ", ".join(f"{b}:{c}" for b, c in dist.items()))
        if holes_count.get(k):
            # Lag and completeness are different questions: a zero-lag row
            # can still be missing a REQUIRED earlier day (the 2026-10-09
            # Friday under a Saturday bar). The hole mark must sit on the
            # same line the lag is read from, or the lag gets read alone.
            line += (f"  !DECISION-INPUT HOLES in {holes_count[k]} probe(s); "
                     f"latest missing: {', '.join(holes_latest[k]) or 'required day'}")
        print(line)
    print("\n  0 = newest settled bar is current at probe time; 1 = one "
          "session late. Served, current and COMPLETE are three different "
          "statements: a line whose required completed days are not all "
          "present carries an inline hole mark with the missing days, "
          "whatever the lag says.")
    print("  A cadence decision needs weeks of this, not days.")

    # BY FIRING HOUR (2026-09-09; completeness-aware 2026-10-11). "How late
    # is this line on average" and "by which hour of the day is it usable"
    # are different questions, and only the second one can move a refresh.
    #
    # Each row is bucketed to the firing it belongs to (hour // 6 * 6), not
    # to the hour it landed in: the cron is 00/06/12/18 UTC and GitHub runs
    # it late, so the 02:50 row is the 00:00 firing. CURRENT requires
    # sessions_behind <= 0 AND no recorded decision-input hole: the second
    # adversarial verification showed a Bitcoin row with Saturday served and
    # Friday MISSING reading as current on lag alone, which is exactly the
    # state a reader must not mistake for usable decision inputs. Rows that
    # predate the hole fields carry no completeness verdict and keep the
    # historical lag-only semantics (venue rows, and crypto rows logged
    # before 2026-10-11).
    buckets = (0, 6, 12, 18)
    hourly: dict[str, dict[int, list[bool]]] = {}
    for r in recs:
        try:
            fired = datetime.fromisoformat(r["probed_at_utc"]).hour // 6 * 6
        except (KeyError, TypeError, ValueError):
            continue  # an unreadable stamp is one lost row, never a crash
        for row in r["rows"]:
            if row["sessions_behind"] is not None:
                usable = (row["sessions_behind"] <= 0
                          and decision_input_complete(row) is not False)
                hourly.setdefault(f"{row['ticker']} ({row['role']})", {}) \
                      .setdefault(fired, []).append(usable)
    print("\n  probes CURRENT (sessions_behind <= 0 and no decision-input "
          "hole) / probes taken, by firing hour")
    print(f"  {'line':40s} " + " ".join(f"{b:02d}Z".rjust(8) for b in buckets))
    for k in sorted(hourly):
        cells = []
        for b in buckets:
            v = hourly[k].get(b, [])
            cells.append((f"{sum(1 for x in v if x)}/{len(v)}"
                          if v else "-").rjust(8))
        print(f"  {k:40s} " + " ".join(cells))
    print("\n  The refresh fires at ~01:00 UTC, inside the 00Z bucket.")


def main(argv: list[str] | None = None, *, download=None,
         now_utc: datetime | None = None, log: Path | None = None) -> int:
    """``download``, ``now_utc`` and ``log`` exist for offline tests only.
    The probe writes exactly one file, the availability log; it never
    writes a price cache."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", action="store_true",
                    help="report the log instead of adding to it")
    args = ap.parse_args(argv)
    if args.summary:
        summarise()
        return 0

    log = Path(log) if log is not None else LOG
    r = probe(now_utc=now_utc, download=download)
    print(f"vendor availability probe — {r['probed_at_utc']}\n")
    print(f"  {'ticker':10s} {'venue':6s} {'last bar':11s} {'last close':11s} "
          f"{'behind':>6s}  decision inputs")
    for row in r["rows"]:
        complete = decision_input_complete(row)
        if row.get("calendar_error"):
            note = f"UNKNOWN: {row['calendar_error']}"
        elif complete is None:
            note = "-"
        elif complete:
            note = "complete"
        else:
            missing = ", ".join(row.get("missing_completed_days") or [])
            note = f"HOLES: {missing or 'required day absent'}"
        print(f"  {row['ticker']:10s} {row['venue']:6s} "
              f"{str(row['last_bar']):11s} "
              f"{str(row['last_completed_session']):11s} "
              f"{str(row['sessions_behind']):>6s}  {note}")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")
    print(f"\n  appended to {log.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
