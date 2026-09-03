"""Repair isolated holes in a price cache, without changing its basis.

WHY THIS EXISTS.

The vendor served no BTC-USD bar for Fri 2026-08-21 -- the 17th to 20th and
the 22nd are all present, and it had not backfilled a day later. BTC-USD is
held in 95 of 212 sleeve-C rebalances at a 20% within-sleeve weight, and a
missing close on a ranking date drops the name from candidacy for that
rebalance (correctly: you cannot rank on a price that does not exist).

The 200-session amplification of that gap was fixed separately, in
price_panel_guard. This module addresses the remaining, bounded cost: the one
decision the gap can still spoil.

RETURNS, NEVER LEVELS -- THE WHOLE DESIGN.

The obvious repair is to drop the secondary source's close into the cache.
That would be badly wrong here, and the reason generalises. These caches are
NOT raw vendor prices: run_thematic_rotation reindexes crypto onto the equity
calendar, FX-converts non-USD lines, and compounds a modelled expense ratio
onto synthetic proxies (BTC-USD carries 25bps/yr since inception). By
2026-08-20 the cached BTC-USD series therefore sat 2.19% BELOW raw spot --
deliberately, and growing. A raw Binance close spliced in at level would have
read as a +2.2% jump, on a sleeve whose eligibility floor is +5%.

So a repair borrows only the secondary's RETURN across the gap and applies it
to the last good CACHED value. Whatever basis the cache carries -- fee drag,
FX, calendar -- is inherited untouched, and any constant offset between the
sources (exchange spread, USDT peg) cancels.

Measured before trusting it: over 157 overlapping sessions in 2026, Binance
BTCUSDT and the cached series had daily-return correlation 0.9998, mean
difference -0.00000, median absolute difference 3.2bp and worst 33bp.

THE 2026-08-28 CLASS, added 2026-09-03. The first version found a gap only
where "the rest of the panel priced and this ticker did not" -- at least five
peers with a close on the session. On Friday 2026-08-28 the vendor withheld
the session for ten of thirteen sleeve-B lines and for SHY, so the B cache
carried a row three names priced (no gap by that rule) and the C cache, which
takes its calendar from SHY, carried no row at all (nothing to compare). Both
engines published the 2026-08-31 rebalance decided on Thursday, and this
script reported nothing. Gaps are now found against the NYSE SCHEDULE: a
scheduled session absent from the frame, or unpriced for the ticker, is a gap
whatever the peers did. The peer rule remains for callers that pass no
schedule.

Two consequences follow. The primary may have backfilled by the time this
runs, and its value is spliced by RETURN like any other -- never dropped in at
level, because these caches are not raw vendor prices (see above). And
US-listed lines default to the locally licensed Norgate feed as their
secondary, which carried 2026-08-28 for every one of them; non-US and synthetic
lines still need an explicit SECONDARY entry, because absence is meaningful.

WHAT IT REFUSES TO DO.

  - It will not fill a RUN of missing bars. A single absent print is a vendor
    hiccup; a run is an outage or a delisting, and inventing a week of prices
    from a second venue is a different and much worse decision.
  - It will not fill a gap at the very start of a series, where there is no
    prior cached value to splice onto.
  - It will not accept an implausible move. A repair that can print any number
    is a repair that can print a wrong one.
  - It writes NOTHING without --apply. Filling a price the book ranks on is a
    state-changing action, so the vault rule (CLAUDE.md, session discipline)
    puts a human in front of it. refresh_all runs this in report mode.
  - Every filled bar is recorded in a sidecar with its source, its method and
    the return used. A silent fill would be indistinguishable from vendor data.

Python datetime months are 1-indexed (January = 1). Binance kline open times
are epoch milliseconds UTC and are derived, never typed as literals.

Usage:
    python scripts/repair_price_gaps.py                 # report only
    python scripts/repair_price_gaps.py --apply         # write the repairs
    python scripts/repair_price_gaps.py --cache thematic --ticker BTC-USD
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LEDGER = DATA_DIR / "price_gap_repairs.jsonl"

# The largest single-session move a repair may print. Bitcoin has moved more
# than this intraday, so this is not "impossible" -- it is the line past which
# a machine should stop and a human should look.
MAX_PLAUSIBLE_MOVE = 0.25

# How far back to bother looking. Older gaps are already baked into a
# published record; repairing them would restate history silently.
LOOKBACK_SESSIONS = 30

# Secondary sources, declared per ticker. Absence is meaningful for anything
# that is not a plain US listing: a ticker with no entry here and a suffix or
# a hyphen is never filled from anywhere but its primary vendor.
SECONDARY = {
    "BTC-USD": {"venue": "binance", "symbol": "BTCUSDT"},
}

# Plain US listings (no exchange suffix, no pair hyphen) default to the
# locally licensed Norgate feed as their secondary (2026-09-03). It is the
# source that carried the withheld 2026-08-28 session for every US line in
# both caches, and its ETF-level agreement with yfinance's adjusted closes is
# 6.3e-5 at worst (WS19). Off the licensed machine it is simply unavailable
# and the gap stays reported, not filled.
US_LISTED_SECONDARY_VENUE = "norgate"


class RepairError(RuntimeError):
    pass


def us_listed(ticker: str) -> bool:
    """A plain US listing: no '.XX' venue suffix, no '-USD' pair, no '=X' FX."""
    return "." not in ticker and "-" not in ticker and "=" not in ticker


def secondary_for(ticker: str) -> dict | None:
    """The declared secondary, else Norgate for a plain US listing, else None."""
    meta = SECONDARY.get(ticker)
    if meta:
        return meta
    if us_listed(ticker):
        return {"venue": US_LISTED_SECONDARY_VENUE, "symbol": ticker}
    return None


def nyse_sessions_for(frame: pd.DataFrame,
                      lookback: int = LOOKBACK_SESSIONS) -> pd.DatetimeIndex:
    """The last ``lookback`` NYSE sessions at or before the cache's last row.
    Both engine caches sit on the NYSE calendar (crypto is reindexed onto it,
    Shenzhen is FX-aligned onto it)."""
    if frame is None or len(frame) == 0:
        return pd.DatetimeIndex([])
    from price_panel_guard import venue_sessions_through  # sibling module
    return pd.DatetimeIndex(venue_sessions_through(
        "NYSE", pd.Timestamp(frame.index.max()), lookback))


# ---------------------------------------------------------------------------
# Gap detection — pure, unit-tested
# ---------------------------------------------------------------------------
def find_gaps(frame: pd.DataFrame, ticker: str,
              lookback: int = LOOKBACK_SESSIONS,
              min_peers: int = 5,
              sessions: pd.DatetimeIndex | None = None,
              ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Sessions the market traded and ``ticker`` has no close for.

    With ``sessions`` (the venue schedule, see nyse_sessions_for) a gap is a
    scheduled session that is absent from the frame or unpriced for the
    ticker, whatever the peers did -- the 2026-08-28 class, where the vendor
    withheld a whole session and the peer rule below saw nothing. Without it,
    the original rule: sessions where at least ``min_peers`` peers priced and
    the ticker did not.

    Returns (gap, previous_session) PAIRS rather than bare dates. That is
    deliberate: the caller needs a previous bar to splice onto, and if it
    derived one itself there would be two definitions of "previous" -- this
    one on the session index used here, the caller's on the raw frame index
    -- which are not the same whenever the frame carries a row that almost
    nothing priced on. One definition, returned with the gap.

    ISOLATED gaps only. A run of two or more consecutive holes is returned as
    nothing at all, because a run is an outage rather than a hiccup and must
    be looked at rather than papered over.
    """
    if ticker not in frame.columns:
        return []
    if sessions is not None:
        traded = pd.DatetimeIndex(sessions)
        if len(frame.index):
            traded = traded[traded <= pd.Timestamp(frame.index.max())]
    else:
        peers = frame.drop(columns=[ticker])
        traded = frame.index[peers.notna().sum(axis=1) >= min_peers]
    if len(traded) == 0:
        return []
    window = traded[-lookback:] if lookback else traded
    col = frame[ticker].reindex(traded)          # an absent row reads as NaN
    missing = [d for d in window if pd.isna(col.loc[d])]
    if not missing:
        return []

    # Reject runs. Adjacency is measured on the panel's own session index, so
    # a weekend between two holes still counts as consecutive.
    pos = {d: i for i, d in enumerate(traded)}
    idx = sorted(pos[d] for d in missing)
    isolated = [i for i in idx if (i - 1) not in idx and (i + 1) not in idx]
    # And a gap needs a prior cached value to splice onto.
    return [(traded[i], traded[i - 1]) for i in isolated
            if i > 0 and not pd.isna(col.loc[traded[i - 1]])]


def splice_value(prev_value: float, sec_prev: float, sec_now: float,
                 max_move: float = MAX_PLAUSIBLE_MOVE) -> float:
    """Carry the secondary source's RETURN onto the cached level.

    Never the secondary's level: see the module docstring on the 2.19% fee
    basis. Any constant offset between the two sources cancels in the ratio.
    """
    if not (prev_value > 0 and sec_prev > 0 and sec_now > 0):
        raise RepairError("non-positive price in the splice inputs")
    ret = sec_now / sec_prev - 1.0
    if abs(ret) > max_move:
        raise RepairError(
            f"implied move {ret:+.1%} exceeds the {max_move:.0%} plausibility "
            f"bound — refusing to print it; look at the source by hand")
    return prev_value * (1.0 + ret)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def fetch_primary(ticker: str, start: str, end: str) -> pd.Series:
    """Re-query the primary vendor. Most holes simply backfill."""
    import yfinance as yf
    d = yf.download(ticker, start=start, end=end, progress=False,
                    auto_adjust=True)
    if d is None or not len(d):
        return pd.Series(dtype=float)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    s = d["Close"].dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s


def fetch_binance(symbol: str, start: pd.Timestamp,
                  end: pd.Timestamp) -> pd.Series:
    """Daily closes from Binance spot. Read-only public market data."""
    start_ms = int(pd.Timestamp(start).tz_localize(timezone.utc).timestamp() * 1000)
    url = ("https://api.binance.com/api/v3/klines"
           f"?symbol={symbol}&interval=1d&startTime={start_ms}&limit=200")
    with urllib.request.urlopen(url, timeout=30) as r:
        raw = json.load(r)
    out = {pd.to_datetime(k[0], unit="ms", utc=True).tz_localize(None).normalize():
           float(k[4]) for k in raw}
    s = pd.Series(out).sort_index()
    return s[s.index <= pd.Timestamp(end)]


def fetch_norgate(symbol: str, start: pd.Timestamp,
                  end: pd.Timestamp) -> pd.Series:
    """TOTALRETURN closes from the local Norgate feed; empty when the feed is
    unreachable or does not carry the symbol (both mean "not here")."""
    import norgate_prices  # sibling module
    if not norgate_prices.available():
        return pd.Series(dtype=float)
    df = norgate_prices.fetch_ohlc(symbol, str(pd.Timestamp(start))[:10],
                                   str(pd.Timestamp(end))[:10])
    if df is None or "Close" not in df.columns:
        return pd.Series(dtype=float)
    s = df["Close"].dropna()
    s.index = pd.to_datetime(s.index).normalize()
    return s


def fetch_secondary(ticker: str, start: pd.Timestamp,
                    end: pd.Timestamp) -> tuple[pd.Series, str]:
    meta = secondary_for(ticker)
    if not meta:
        return pd.Series(dtype=float), ""
    if meta["venue"] == "binance":
        return fetch_binance(meta["symbol"], start, end), \
            f"binance:{meta['symbol']}"
    if meta["venue"] == "norgate":
        return fetch_norgate(meta["symbol"], start, end), \
            f"norgate:{meta['symbol']}"
    raise RepairError(f"unknown secondary venue {meta['venue']!r}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
CACHES = {
    "thematic": ("thematic_prices_cache.parquet", "run_thematic_rotation"),
    "asset_class": ("asset_class_prices_cache.parquet", "run_asset_class_rotation"),
}


def _splice_from(series: pd.Series, label: str, g: pd.Timestamp,
                 prev: pd.Timestamp, prev_val: float, rec: dict) -> bool:
    """Try to fill ``rec`` from ``series`` by RETURN. True when it produced a
    value or a refusal (either way the record is complete), False when the
    series does not cover both sessions."""
    if not (len(series) and g in series.index and prev in series.index):
        return False
    try:
        v = splice_value(prev_val, float(series.loc[prev]), float(series.loc[g]))
    except RepairError as e:
        rec.update(source=label, method="return_splice", refused=str(e))
        return True
    rec.update(source=label, method="return_splice", value=v,
               implied_return=float(series.loc[g] / series.loc[prev] - 1))
    return True


def repair_cache(key: str, only_ticker: str | None = None,
                 apply: bool = False,
                 sessions: pd.DatetimeIndex | None = None) -> list[dict]:
    fname, _ = CACHES[key]
    path = DATA_DIR / fname
    if not path.exists():
        raise RepairError(f"no cache at {path}")
    frame = pd.read_parquet(path)
    if sessions is None:
        sessions = nyse_sessions_for(frame)
    tickers = [only_ticker] if only_ticker else list(frame.columns)
    repairs: list[dict] = []

    for t in tickers:
        gaps = find_gaps(frame, t, sessions=sessions)
        if not gaps:
            continue
        lo = min(g for g, _ in gaps) - pd.Timedelta(days=10)
        hi = max(g for g, _ in gaps) + pd.Timedelta(days=2)

        primary = fetch_primary(t, lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d"))
        sec, sec_label = fetch_secondary(t, lo, hi)

        for g, prev in gaps:
            prev_val = float(frame.loc[prev, t])
            rec = {"cache": key, "ticker": t, "date": str(g.date()),
                   "prev_date": str(prev.date()), "prev_value": prev_val}

            # 1. The primary may have backfilled. Its RETURN is spliced like
            #    any other -- never its level: the cache carries drag, FX and
            #    calendar work that a raw close would undo at the junction.
            if _splice_from(primary, "primary:yfinance", g, prev, prev_val, rec):
                repairs.append(rec)
                continue

            # 2. Otherwise the secondary (declared, or Norgate for a US line).
            if _splice_from(sec, sec_label, g, prev, prev_val, rec):
                repairs.append(rec)
                continue

            rec.update(source=None, method=None,
                       refused="no primary backfill and no usable secondary")
            repairs.append(rec)

    usable = [r for r in repairs if r.get("value") is not None
              and "refused" not in r]
    if apply and usable:
        for r in usable:
            # .loc on a new row label enlarges the frame: an ABSENT session
            # (the C cache's 2026-08-28) gains its row here, NaN elsewhere.
            frame.loc[pd.Timestamp(r["date"]), r["ticker"]] = r["value"]
        frame = frame.sort_index()
        frame.to_parquet(path)
        stamp = datetime.now(timezone.utc).isoformat()
        with LEDGER.open("a", encoding="utf-8") as fh:
            for r in usable:
                fh.write(json.dumps({**r, "applied_at_utc": stamp}) + "\n")
    return repairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", choices=sorted(CACHES) + ["all"], default="all")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs (default is report only)")
    args = ap.parse_args(argv)

    keys = sorted(CACHES) if args.cache == "all" else [args.cache]
    all_reps: list[dict] = []
    for k in keys:
        all_reps.extend(repair_cache(k, args.ticker, apply=args.apply))

    if not all_reps:
        print("\nNo isolated price gaps in the last "
              f"{LOOKBACK_SESSIONS} NYSE sessions (checked against the "
              f"exchange schedule).")
        return 0

    print(f"\nPRICE GAP REPAIR — {'APPLIED' if args.apply else 'REPORT ONLY'}")
    print("  gaps are scheduled NYSE sessions absent or unpriced; values are "
          "spliced by RETURN from the primary if it has backfilled, else from "
          "the declared secondary (Norgate for plain US listings)\n")
    for r in all_reps:
        head = f"  {r['ticker']:10s} {r['date']}  ({r['cache']})"
        if "refused" in r:
            print(f"{head}  NOT REPAIRED — {r['refused']}")
            continue
        extra = (f"  implied {r['implied_return']:+.2%}"
                 if "implied_return" in r else "")
        print(f"{head}  {r['prev_value']:,.2f} -> {r['value']:,.2f}"
              f"  via {r['source']} ({r['method']}){extra}")
    if not args.apply:
        print("\n  Nothing written. Re-run with --apply to commit these.")
    else:
        print(f"\n  Recorded in {LEDGER.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
