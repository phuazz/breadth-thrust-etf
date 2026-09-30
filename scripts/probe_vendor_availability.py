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
]


def _sessions_behind(cal, last_bar, lcs) -> int | None:
    if last_bar is None or lcs is None:
        return None
    lo, hi = sorted((pd.Timestamp(last_bar), pd.Timestamp(lcs)))
    sched = cal.schedule(start_date=lo, end_date=hi)
    n = max(len(sched) - 1, 0)
    return n if pd.Timestamp(last_bar) <= pd.Timestamp(lcs) else -n


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


def _last_bar(close: pd.DataFrame, tk: str) -> pd.Timestamp | None:
    if tk not in close.columns:
        return None
    s = close[tk].dropna()
    return pd.Timestamp(s.index.max()).normalize() if len(s) else None


def _close_frame(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        return raw["Close"]
    return raw[["Close"]].rename(columns={"Close": tickers[0]})


def _refetch_single(download, tk: str, start: str, end: str,
                    attempts: int = REFETCH_ATTEMPTS,
                    pause: float = REFETCH_PAUSE_SECONDS,
                    sleep=None) -> pd.Timestamp | None:
    """Ask for one ticker alone, a few times, before recording it unserved."""
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
        bar = _last_bar(_close_frame(raw, [tk]), tk)
        if bar is not None:
            return bar
    return None


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
        cal = _venue_cal(venue)
        lcs = last_completed_session_on(cal, now)
        last_bar = _last_bar(close, tk)
        refetched = False
        if last_bar is None:
            last_bar = _refetch_single(download, tk, start, end)
            refetched = last_bar is not None
        row = {
            "ticker": tk, "venue": venue, "role": role,
            "last_bar": str(last_bar.date()) if last_bar is not None else None,
            "last_completed_session": str(lcs.date()) if lcs is not None else None,
            "sessions_behind": _sessions_behind(cal, last_bar, lcs),
        }
        # Recorded so the series stays honest about which bars the batch
        # call missed; absent on the ordinary path.
        if refetched:
            row["refetched"] = True
        rows.append(row)
    return {"probed_at_utc": now.isoformat(timespec="seconds"), "rows": rows}


def summarise() -> None:
    if not LOG.exists():
        print(f"no log yet at {LOG}")
        return
    recs = [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
    print(f"{len(recs)} probe(s) since {recs[0]['probed_at_utc'][:10]}\n")
    by: dict[str, list[int]] = {}
    for r in recs:
        for row in r["rows"]:
            if row["sessions_behind"] is not None:
                by.setdefault(f"{row['ticker']} ({row['role']})", []).append(
                    row["sessions_behind"])
    print(f"  {'line':40s} {'n':>3s} {'mean':>6s} {'max':>4s}  distribution")
    for k, v in sorted(by.items()):
        dist = {b: v.count(b) for b in sorted(set(v))}
        print(f"  {k:40s} {len(v):3d} {sum(v)/len(v):6.2f} {max(v):4d}  "
              + ", ".join(f"{b}:{c}" for b, c in dist.items()))
    print("\n  0 = current at probe time; 1 = one session late.")
    print("  A cadence decision needs weeks of this, not days.")

    # BY FIRING HOUR (2026-09-09). "How late is this line on average" and "by
    # which hour of the day is it current" are different questions, and only
    # the second one can move a refresh. The table above answers the first and
    # was read as if it answered the second.
    #
    # Each row is bucketed to the firing it belongs to (hour // 6 * 6), not to
    # the hour it landed in: the cron is 00/06/12/18 UTC and GitHub runs it
    # late, so the 02:50 row is the 00:00 firing. CURRENT means
    # sessions_behind <= 0 — zero is the last completed session served, and -1
    # is an in-progress bar ahead of it, which means the settled one is there
    # too.
    buckets = (0, 6, 12, 18)
    hourly: dict[str, dict[int, list[int]]] = {}
    for r in recs:
        try:
            fired = datetime.fromisoformat(r["probed_at_utc"]).hour // 6 * 6
        except (KeyError, TypeError, ValueError):
            continue  # an unreadable stamp is one lost row, never a crash
        for row in r["rows"]:
            if row["sessions_behind"] is not None:
                hourly.setdefault(f"{row['ticker']} ({row['role']})", {}) \
                      .setdefault(fired, []).append(row["sessions_behind"])
    print("\n  probes CURRENT (sessions_behind <= 0) / probes taken, "
          "by firing hour")
    print(f"  {'line':40s} " + " ".join(f"{b:02d}Z".rjust(8) for b in buckets))
    for k in sorted(hourly):
        cells = []
        for b in buckets:
            v = hourly[k].get(b, [])
            cells.append((f"{sum(1 for x in v if x <= 0)}/{len(v)}"
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
    print(f"  {'ticker':10s} {'venue':6s} {'last bar':11s} {'last close':11s} behind")
    for row in r["rows"]:
        print(f"  {row['ticker']:10s} {row['venue']:6s} "
              f"{str(row['last_bar']):11s} "
              f"{str(row['last_completed_session']):11s} "
              f"{row['sessions_behind']}")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")
    print(f"\n  appended to {LOG.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
