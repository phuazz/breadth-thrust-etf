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


def _settled_venue(series: pd.Series | None, lcs: pd.Timestamp | None,
                   ) -> tuple[pd.Timestamp | None, float | None, str | None]:
    """(settled last bar, its close, newest in-progress bar date or None).

    SETTLED ONLY, as the crypto row has been since 2026-10-11. The venue rows
    took the newest non-null close in the response, so a probe firing while a
    venue was open logged the in-progress bar as ``last_bar`` with a negative
    sessions_behind: 159801.SZ at 2026-10-12 04:05 UTC (the Shenzhen lunch
    break), and every NYSE and Xetra line at 14:17 UTC the same day. The
    tripwire already skipped those, but the firing-hour summary counted them
    as CURRENT. The close is recorded beside the date, so a later withdrawal
    can be compared against the value that was served, not only its date:
    the 2026-10-12 159801.SZ retraction left a date and no price. A
    non-finite or non-positive value is a hole, not a bar."""
    if series is None:
        return None, None, None
    s = series.dropna()
    if not len(s):
        return None, None, None
    idx = pd.to_datetime(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    s = s[(s > 0) & (s < float("inf"))]
    s = s[~s.index.duplicated(keep="last")]
    if lcs is None:
        settled, ahead = s, s.iloc[0:0]
    else:
        bound = pd.Timestamp(lcs).normalize()
        settled, ahead = s[s.index <= bound], s[s.index > bound]
    in_progress = str(ahead.index.max().date()) if len(ahead) else None
    if not len(settled):
        return None, None, in_progress
    return (pd.Timestamp(settled.index.max()), float(settled.iloc[-1]),
            in_progress)


def _close_frame(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        return raw["Close"]
    return raw[["Close"]].rename(columns={"Close": tickers[0]})


def decision_input_complete(row: dict) -> bool | None:
    """Three different statements hide in one probe row: something was
    SERVED (observation), the newest settled bar is FRESH (lag), and the
    REQUIRED completed days are all PRESENT (completeness). Consumers that
    collapse them re-create the 2026-10-11 false assurance, where a
    Saturday bar over a missing Friday read as current.

    True/False for rows that carry the hole fields (crypto rows since
    2026-10-11); None for rows that do not (venue rows, and crypto rows
    logged earlier) — unknown is never reported as complete."""
    if ("missing_completed_days" not in row
            and "required_day_bar_present" not in row):
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
            used = again if refetched else series
            _, crypto_close, _ = _settled_venue(used, bound)
            row = {
                "ticker": tk, "venue": venue, "role": role,
                "last_bar": str(last_bar.date()) if last_bar is not None else None,
                "last_close": crypto_close,
                "last_completed_session": str(bound.date()),
                "sessions_behind": _days_behind(last_bar, bound),
                "required_day_bar_present": required_present,
                "missing_completed_days": missing,
            }
            if refetched:
                row["refetched"] = True
            rows.append(row)
            continue
        cal = _venue_cal(venue)
        lcs = last_completed_session_on(cal, now)
        series = close[tk] if tk in close.columns else None
        last_bar, last_close, in_progress = _settled_venue(series, lcs)
        refetched = False
        if last_bar is None:
            again = _refetch_single_series(download, tk, start, end)
            if again is not None:
                last_bar, last_close, in_progress = _settled_venue(again, lcs)
                refetched = last_bar is not None
        row = {
            "ticker": tk, "venue": venue, "role": role,
            "last_bar": str(last_bar.date()) if last_bar is not None else None,
            "last_close": last_close,
            "last_completed_session": str(lcs.date()) if lcs is not None else None,
            "sessions_behind": _sessions_behind(cal, last_bar, lcs),
        }
        # Recorded so the series stays honest about which bars the batch
        # call missed; absent on the ordinary path.
        if refetched:
            row["refetched"] = True
        # An unfinished session the vendor already serves, kept out of
        # last_bar and recorded by date only: it is not a close yet.
        if in_progress:
            row["in_progress_bar"] = in_progress
        rows.append(row)
    return {"probed_at_utc": now.isoformat(timespec="seconds"), "rows": rows}


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", action="store_true",
                    help="report the log instead of adding to it")
    args = ap.parse_args(argv)
    if args.summary:
        summarise()
        return 0

    r = probe()
    print(f"vendor availability probe — {r['probed_at_utc']}\n")
    print(f"  {'ticker':10s} {'venue':6s} {'last bar':11s} {'last close':11s} "
          f"{'behind':>6s}  decision inputs")
    for row in r["rows"]:
        complete = decision_input_complete(row)
        if complete is None:
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
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")
    print(f"\n  appended to {LOG.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
