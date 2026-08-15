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
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
]


def _sessions_behind(cal, last_bar, lcs) -> int | None:
    if last_bar is None or lcs is None:
        return None
    lo, hi = sorted((pd.Timestamp(last_bar), pd.Timestamp(lcs)))
    sched = cal.schedule(start_date=lo, end_date=hi)
    n = max(len(sched) - 1, 0)
    return n if pd.Timestamp(last_bar) <= pd.Timestamp(lcs) else -n


def probe(now_utc: datetime | None = None) -> dict:
    import yfinance as yf
    now = now_utc or datetime.now(timezone.utc)
    start = (now - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    end = (now + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    tickers = [t for t, _, _ in PROBES]
    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                      progress=False, group_by="column")
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]

    rows = []
    for tk, venue, role in PROBES:
        cal = mcal.get_calendar(venue)
        lcs = last_completed_session_on(cal, now)
        last_bar = None
        if tk in close.columns:
            s = close[tk].dropna()
            if len(s):
                last_bar = pd.Timestamp(s.index.max()).normalize()
        rows.append({
            "ticker": tk, "venue": venue, "role": role,
            "last_bar": str(last_bar.date()) if last_bar is not None else None,
            "last_completed_session": str(lcs.date()) if lcs is not None else None,
            "sessions_behind": _sessions_behind(cal, last_bar, lcs),
        })
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
    print(f"  {'line':34s} {'n':>3s} {'mean':>6s} {'max':>4s}  distribution")
    for k, v in sorted(by.items()):
        dist = {b: v.count(b) for b in sorted(set(v))}
        print(f"  {k:34s} {len(v):3d} {sum(v)/len(v):6.2f} {max(v):4d}  "
              + ", ".join(f"{b}:{c}" for b, c in dist.items()))
    print("\n  0 = current at probe time; 1 = one session late.")
    print("  A cadence decision needs weeks of this, not days.")


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
