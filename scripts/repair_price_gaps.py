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

A DECLARED COLUMN BASIS IS A REFUSAL, added 2026-09-19 (WS21).

The return-splice above is safe because each ticker's sources are declared
against the construction its column actually carries. BTC-USD's Binance entry
is the clearest case: the cached series is the UTC-day spot close, and Binance
BTCUSDT is quoted on the same clock, so its return is the right return for
that column even though its level is 2.19% away.

That correspondence breaks the moment a column's construction changes. Under
BTE_C_BTC_BASIS=ibit the BTC-USD column follows IBIT at the 16:00 ET close,
and BOTH sources here would then be wrong in the same way: the primary
(yfinance BTC-USD) and the secondary (Binance BTCUSDT) each measure a UTC-day
return, so splicing either across a gap would print a move IBIT did not make,
missing its premium/discount entirely. A one-session Bitcoin return can differ
by a few per cent between those two clocks, on a sleeve whose eligibility
floor is +5%.

So a column whose cache sidecar DECLARES a basis is reported and not filled.
The right secondary for an IBIT-spliced column is IBIT itself, but choosing it
is a construction decision, and WS21 fixed one construction at registration
with no menu. Declaring that source belongs in a dated commit at promotion,
not in a repair script reaching for the nearest series. Caches with no
declared basis — every cache today — are unaffected.

WHAT IT REFUSES TO DO.

  - It will not fill a column whose cache sidecar declares a basis (above).
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
import math
import os
import statistics
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
MAX_BACKFILL_DIFFERENCE = 0.01

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
    if not all(math.isfinite(v) and v > 0 for v in (prev_value, sec_prev, sec_now)):
        raise RepairError("non-finite or non-positive price in the splice inputs")
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


def restore_recorded_btc_gaps(fresh: pd.DataFrame, cached: pd.DataFrame | None,
                              *, ledger: Path, completed_through,
                              cache_proved_through, decision_session,
                              same_source_and_basis: bool
                              ) -> tuple[pd.DataFrame, list[dict]]:
    """Carry already-published BTC repairs through a later primary download.

    A ledger row alone is not proof: the incumbent cache, its completed-day
    attestation and the rebuilt preceding price must all agree. A materially
    different primary backfill needs review before historical output changes.
    """
    ticker = "BTC-USD"
    if not ledger.exists() or ticker not in fresh.columns:
        return fresh, []
    try:
        with ledger.open(encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError, TypeError) as exc:
        raise RepairError("cannot verify the applied BTC repair ledger") from exc
    decision = pd.Timestamp(decision_session)
    bound = pd.Timestamp(completed_through)
    if (not same_source_and_basis or cached is None
            or ticker not in cached.columns):
        for rec in records:
            if rec.get("cache") != "thematic" or rec.get("ticker") != ticker:
                continue
            day = pd.Timestamp(rec.get("date"))
            if day <= bound and day < decision:
                raise RepairError("historical BTC repair has no matching "
                                  "completed cache to prove published history")
        return fresh, []
    proof_bound = pd.Timestamp(cache_proved_through)
    relevant = [r for r in records
                if r.get("cache") == "thematic" and r.get("ticker") == ticker
                and pd.Timestamp(r.get("date")) <= min(bound, decision)]
    if not relevant:
        return fresh, []
    if pd.isna(proof_bound):
        raise RepairError("applied BTC repair has no cache completion attestation")

    out = fresh.copy()
    retained = []
    seen = set()
    for rec in relevant:
        try:
            day = pd.Timestamp(rec["date"])
            prev = pd.Timestamp(rec["prev_date"])
            value = float(rec["value"])
            previous = float(rec["prev_value"])
            movement = float(rec["implied_return"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RepairError("invalid applied BTC repair record") from exc
        if day in seen:
            raise RepairError(f"duplicate applied BTC repair for {day.date()}")
        seen.add(day)
        if (rec.get("method") != "return_splice"
                or rec.get("source") not in ("primary:yfinance", "binance:BTCUSDT")
                or not prev < day <= bound or day > proof_bound
                or not all(math.isfinite(v) and v > 0 for v in (value, previous))
                or not math.isfinite(movement)
                or abs(movement) > MAX_PLAUSIBLE_MOVE
                or not math.isclose(value, previous * (1 + movement), rel_tol=1e-9)
                or day not in cached.index or prev not in cached.index
                or prev not in out.index
                or not math.isclose(float(cached.at[prev, ticker]), previous,
                                    rel_tol=1e-9)
                or not math.isclose(float(out.at[prev, ticker]), previous,
                                    rel_tol=1e-9)):
            raise RepairError(f"applied BTC repair {day.date()} no longer matches "
                              "its completed cache and price basis")
        incumbent = float(cached.at[day, ticker])
        drift = abs(incumbent / value - 1)
        if (not math.isfinite(drift) or incumbent <= 0
                or drift > MAX_BACKFILL_DIFFERENCE
                or (day == decision
                    and not math.isclose(incumbent, value, rel_tol=1e-9))):
            raise RepairError(f"BTC cache for {day.date()} conflicts with its "
                              "applied repair; review before restatement")
        if day in out.index and pd.notna(out.at[day, ticker]):
            difference = abs(float(out.at[day, ticker]) / incumbent - 1)
            if not math.isfinite(difference) or difference > MAX_BACKFILL_DIFFERENCE:
                raise RepairError(f"BTC primary backfill for {day.date()} differs "
                                  "materially from the published repair; review "
                                  "before restatement")
            # The released value stays fixed even after a modest backfill.
            # Publication history must not depend on when Yahoo serves a date.
        out.loc[day, ticker] = incumbent
        retained.append({"ticker": ticker, "date": str(day.date()),
                         "value": incumbent,
                         "source": (rec["source"] if math.isclose(incumbent, value,
                                                                rel_tol=1e-9)
                                    else "cache:completed"),
                         "method": (rec["method"] if math.isclose(incumbent, value,
                                                                rel_tol=1e-9)
                                    else "preserve_published_history")})
    return out.sort_index(), retained


def verify_auto_btc_cache(cached: pd.DataFrame, sidecar: dict,
                          ledger: Path) -> None:
    """An interrupted cache write cannot make an unlogged fallback reusable."""
    auto = sidecar.get("auto_btc_repair")
    retained = sidecar.get("retained_gap_repairs") or []
    if not auto and not retained:
        return
    try:
        with ledger.open(encoding="utf-8") as fh:
            matches = [json.loads(line) for line in fh if line.strip()]
        for rec in ([auto] if auto else []) + retained:
            day = pd.Timestamp(rec["date"])
            value = float(rec["value"])
            if (rec.get("ticker") != "BTC-USD" or day not in cached.index
                    or not math.isfinite(value) or value <= 0
                    or not math.isclose(float(cached.at[day, "BTC-USD"]), value,
                                        rel_tol=1e-9)):
                raise RepairError("BTC cache record does not match its price")
            applied = [r for r in matches
                       if r.get("cache") == "thematic"
                       and r.get("ticker") == "BTC-USD"
                       and r.get("date") == rec["date"]
                       and r.get("method") == "return_splice"
                       and r.get("applied_at_utc")]
            if len(applied) != 1:
                raise RepairError("BTC cache has no unique applied ledger record")
            prior = applied[0]
            if rec is auto or rec.get("source") != "cache:completed":
                if (prior.get("source") != rec.get("source")
                        or not math.isclose(float(prior["value"]), value,
                                            rel_tol=1e-9)):
                    raise RepairError("BTC cache provenance conflicts with ledger")
            elif (abs(value / float(prior["value"]) - 1)
                  > MAX_BACKFILL_DIFFERENCE):
                raise RepairError("BTC published cache drift exceeds repair guard")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RepairError("automatic BTC cache provenance is unverifiable") from exc


def record_auto_btc_repair(record: dict, ledger: Path = LEDGER,
                           *, check_only: bool = False) -> bool:
    """Durably append a new repair, or verify an identical applied record.

    A clean checkout has no ignored price cache, but it can carry the tracked
    ledger from an earlier run. Re-deriving that cell must not duplicate its
    identity or accept a different price under the same date.
    """
    try:
        with ledger.open(encoding="utf-8") as fh:
            prior = [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        prior = []
    except (OSError, ValueError, TypeError) as exc:
        raise RepairError("cannot verify the applied BTC repair ledger") from exc
    matches = [r for r in prior
               if r.get("cache") == "thematic" and r.get("ticker") == "BTC-USD"
               and r.get("date") == record["date"]]
    if matches:
        if (len(matches) != 1 or matches[0].get("source") != record["source"]
                or matches[0].get("method") != record["method"]
                or matches[0].get("prev_date") != record["prev_date"]
                or not matches[0].get("applied_at_utc")
                or not math.isclose(float(matches[0].get("prev_value", float("nan"))),
                                    record["prev_value"], rel_tol=1e-9)
                or not math.isclose(float(matches[0].get("value", float("nan"))),
                                    record["value"], rel_tol=1e-9)):
            raise RepairError("BTC repair date already has a different applied record")
        return False
    if check_only:
        return True
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**record,
                             "applied_at_utc": datetime.now(timezone.utc).isoformat()})
                 + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return True


def recover_completed_btc_tail(frame: pd.DataFrame, *, required_session,
                               completed_through, fetch=fetch_binance,
                               annual_fee_bps: float = 0.0
                               ) -> tuple[pd.DataFrame, dict | None]:
    """Fill one missing completed BTC decision close from Binance returns.

    The primary batch and single-name retry have already failed. This uses
    only the current NYSE decision session, never an earlier hole or an
    unfinished UTC day. The caller records the result after its cache write.
    """
    ticker = "BTC-USD"
    required = pd.Timestamp(required_session)
    if (ticker not in frame.columns or required > pd.Timestamp(completed_through)
            or required not in frame.index
            or pd.notna(frame.at[required, ticker])):
        return frame, None
    sessions = nyse_sessions_for(frame)
    if (len(sessions) < 7 or sessions[-1] != required
            or find_gaps(frame, ticker, sessions=sessions) !=
            [(required, sessions[-2])]):
        raise RepairError("BTC tail is not one isolated completed-session gap")
    prev = sessions[-2]
    previous = float(frame.at[prev, ticker])
    if not math.isfinite(previous) or previous <= 0:
        raise RepairError("BTC preceding cached-basis close is not usable")
    try:
        secondary = fetch("BTCUSDT", sessions[-7], required)
    except Exception as exc:
        raise RepairError("BTC secondary source unavailable") from exc
    secondary = secondary.dropna()
    if prev not in secondary.index or required not in secondary.index:
        raise RepairError("BTC secondary lacks a required completed daily bar")
    # Require agreement on recent returns, not raw levels: the cache carries
    # modelled fee drag while Binance quotes BTC in USDT.
    overlap = []
    for left, right in zip(sessions[-7:-2], sessions[-6:-1]):
        if (left in secondary.index and right in secondary.index
                and left in frame.index and right in frame.index
                and pd.notna(frame.at[left, ticker])
                and pd.notna(frame.at[right, ticker])):
            values = (float(frame.at[left, ticker]),
                      float(frame.at[right, ticker]),
                      float(secondary.at[left]), float(secondary.at[right]))
            if not all(math.isfinite(v) and v > 0 for v in values):
                raise RepairError("non-finite or non-positive BTC overlap close")
            ref = values[1] / values[0] - 1
            alt = values[3] / values[2] - 1
            if not all(math.isfinite(v) for v in (ref, alt)):
                raise RepairError("non-finite BTC overlap return")
            overlap.append(abs(ref - alt))
    if (len(overlap) < 3 or statistics.median(overlap) > 0.005
            or max(overlap) > 0.01):
        raise RepairError("BTC secondary return basis failed the overlap check")
    gross_return = float(secondary.at[required] / secondary.at[prev] - 1)
    if not math.isfinite(annual_fee_bps) or not 0 <= annual_fee_bps < 10_000:
        raise RepairError("invalid BTC annual fee basis")
    fee_factor = (1 - annual_fee_bps / 10_000) ** (
        (required - prev).days / 365)
    value = splice_value(previous, float(secondary.at[prev]),
                         float(secondary.at[required])) * fee_factor
    if abs(value / previous - 1) > MAX_PLAUSIBLE_MOVE:
        raise RepairError("BTC fee-adjusted secondary move is implausible")
    out = frame.copy()
    out.at[required, ticker] = value
    record = {"cache": "thematic", "ticker": ticker,
              "date": str(required.date()), "prev_date": str(prev.date()),
              "prev_value": previous, "source": "binance:BTCUSDT",
              "method": "return_splice", "value": value,
              "implied_return": value / previous - 1,
              "secondary_return": gross_return,
              "annual_fee_bps": annual_fee_bps,
              "overlap_returns": len(overlap),
              "max_overlap_return_difference": max(overlap)}
    return out, record


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
    # What the cache says each column's CONSTRUCTION is. Empty for every cache
    # written before 2026-09-19, and empty today; see the module docstring on
    # why a declared basis is a refusal rather than a different source.
    import price_source  # sibling module
    declared = price_source.read_cache_column_basis(path)
    tickers = [only_ticker] if only_ticker else list(frame.columns)
    repairs: list[dict] = []

    for t in tickers:
        gaps = find_gaps(frame, t, sessions=sessions)
        if not gaps:
            continue
        basis = declared.get(t)
        if basis:
            for g, prev in gaps:
                repairs.append({
                    "cache": key, "ticker": t, "date": str(g.date()),
                    "prev_date": str(prev.date()),
                    "prev_value": float(frame.loc[prev, t]),
                    "declared_basis": basis, "source": None, "method": None,
                    "refused": (
                        f"the column declares basis {basis!r}; this module's "
                        f"sources for {t} are declared against the incumbent "
                        f"construction and would splice a return measured on "
                        f"another clock. Declaring a source for this basis is "
                        f"a construction decision and belongs in a dated "
                        f"commit, not here")})
            continue
        lo = min(g for g, _ in gaps) - pd.Timedelta(days=10)
        hi = max(g for g, _ in gaps) + pd.Timedelta(days=2)

        source_errors = {}
        try:
            primary = fetch_primary(t, lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d"))
        except Exception as exc:
            # A transport failure is not evidence that a bar does not exist.
            primary = pd.Series(dtype=float)
            source_errors["primary"] = type(exc).__name__
        sec = None
        sec_label = ""

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

            # Only ask a secondary when the primary cannot repair this gap.
            # An unavailable optional source cannot discard a valid primary.
            if sec is None:
                try:
                    sec, sec_label = fetch_secondary(t, lo, hi)
                except Exception as exc:
                    sec = pd.Series(dtype=float)
                    source_errors["secondary"] = type(exc).__name__
            if source_errors:
                rec["source_errors"] = dict(source_errors)
            # 2. Otherwise the secondary (declared, or Norgate for a US line).
            if _splice_from(sec, sec_label, g, prev, prev_val, rec):
                repairs.append(rec)
                continue

            rec.update(source=None, method=None,
                       refused=("source request failed; see source_errors" if source_errors
                                else "no primary backfill and no usable secondary"))
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
