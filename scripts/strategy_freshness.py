"""How fresh is each strategy's data, and against what would you judge it?

WHY THIS EXISTS.

On a Saturday the four sleeves are NOT equally current, and nothing published
said so. European constituent prices reach the market about a session late, so
on Sat 22 Aug 2026 sleeves A, B and C carried Friday's data while sleeve D --
20% of NAV -- carried Thursday's. A reader of either public page saw one as-of
date over the whole book and had no way to tell.

This reports, per strategy, the last session its signal inputs ALL reach, the
last completed session on that strategy's OWN venue, and the gap between them.

THE THREE SOURCES THAT LOOK RIGHT AND ARE NOT.

Getting this wrong is easy, because three published fields sit close to the
answer and each describes something else. All three were checked and rejected:

  1. The engines' `per_etf_signal` / `per_etf_breadth` chart series. These are
     resampled WEEKLY for plotting. BTC-USD was missing exactly one daily bar
     and its series therefore ended 2026-08-14 while every other ticker ended
     2026-08-21 -- one absent day reading as a week of staleness.

  2. `europe_rotation`'s breadth series, which is forward-filled onto the ETF
     price calendar (align_breadth_to_index). On 2026-08-22 all five European
     panels repeated their 2026-08-20 value on 2026-08-21, so the series looks
     current precisely when the data is not. That fill is CORRECT for the
     backtest -- it ranks on the most recent real observation and adds no
     look-ahead -- but it cannot be read as freshness.

  3. The book's single `as_of`. It is one date over four sleeves on three
     venues, which is the thing this module exists to break apart.

So each strategy is measured at the UNDERLYING data bound: the breadth panels
for A and D (whose `tail_cap` field states why a panel stops where it does),
and the price caches for B and C. Read from disk, never fetched -- a freshness
report that goes to the network would describe the vendor, not the artefacts.

STALEST INPUT WINS. A strategy is only as fresh as its stalest ranking input,
because one lagging name can change the selection. The laggards are NAMED, or
the reader has a warning they cannot act on.

THREE REASONS A SLEEVE STOPS SHORT, and each needs its own sentence:

  1. One input lags the rest. `laggards` names it. Deliberately EMPTY when the
     whole sleeve shares one date, because listing every member is noise.
  2. The vendor has yet to publish. The panel declares a `tail_cap` and
     `_cap_reason` reads it out. A UCITS artefact -- no US panel carries one.
  3. Nothing has been rebuilt since. Every input stops at the same date and no
     cap applies, so BOTH channels above are empty BY CONSTRUCTION and the row
     said "behind" with nothing attached -- on 2026-08-24 all four sleeves did.
     `_uniform_stale_reason` is the third sentence, and it also covers the
     inverse fault: data dated PAST the venue's last close, which is a partial
     bar and must not be described as "not refreshed yet".

Python datetime months are 1-indexed (January = 1). Session arithmetic goes
through pandas_market_calendars via session_bounds -- never a manual weekday.

Usage:
    python scripts/strategy_freshness.py
    python scripts/strategy_freshness.py --json data/strategy_freshness.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etf_registry import (  # noqa: E402
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
    get_etf,
)
from session_bounds import last_completed_session_on  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Plain-English labels. These must match the sleeve names the public pages
# already print, so a reader does not have to map "A" onto "US sectors".
SLEEVE_LABELS = {
    "A": "US sectors",
    "B": "Asset classes",
    "C": "Thematic",
    "D": "Europe sectors",
}

CURRENT = "current"
BEHIND = "behind"
UNKNOWN = "unknown"


class FreshnessError(RuntimeError):
    """Raised when freshness cannot be established, rather than guessed."""


# ---------------------------------------------------------------------------
# Pure verdict logic — unit-tested offline
# ---------------------------------------------------------------------------
def sessions_between(cal_name: str, start: str, end: str) -> int | None:
    """Count sessions on ``cal_name`` after ``start`` up to and including ``end``.

    Returns 0 when the dates are equal, a positive count when ``end`` is later,
    and a NEGATIVE count when the data runs PAST the venue's last close -- a
    state that should never occur and must be surfaced rather than clamped to
    zero, since it means a bar exists for a session that has not finished.
    """
    if not start or not end:
        return None
    try:
        s, e = pd.Timestamp(start), pd.Timestamp(end)
    except (TypeError, ValueError):
        return None
    lo, hi, sign = (s, e, 1) if s <= e else (e, s, -1)
    sched = mcal.get_calendar(cal_name).schedule(
        start_date=lo.strftime("%Y-%m-%d"), end_date=hi.strftime("%Y-%m-%d"))
    # The schedule includes both endpoints; the gap is the count between them.
    return sign * max(0, len(sched) - 1)


def classify(data_through: str | None, venue_last: str | None,
             cal_name: str) -> tuple[str, int | None]:
    """Status and session gap for one strategy."""
    if not data_through or not venue_last:
        return UNKNOWN, None
    gap = sessions_between(cal_name, data_through, venue_last)
    if gap is None:
        return UNKNOWN, None
    if gap <= 0:
        # gap < 0 means the data runs past the venue's last completed session.
        # Report it as behind-by-a-negative rather than pretending it is fine;
        # the caller renders it and the refresh guard is what should fail.
        return (CURRENT, gap) if gap == 0 else (BEHIND, gap)
    return BEHIND, gap


# ---------------------------------------------------------------------------
# Reading the artefacts
# ---------------------------------------------------------------------------
def _venue_for(universe: list[str]) -> str:
    cals = {get_etf(e).get("trading_calendar", "NYSE") for e in universe}
    return sorted(cals)[0] if len(cals) == 1 else "NYSE"


def panel_reach(universe: list[str]) -> tuple[str | None, list[str], dict]:
    """Stalest breadth-panel end_date across ``universe``, and who is stalest.

    Also returns each panel's declared ``tail_cap`` where present, so the page
    can say WHY a sleeve stops short instead of only that it does.
    """
    ends: dict[str, str] = {}
    caps: dict[str, dict] = {}
    for etf in universe:
        p = DATA_DIR / f"breadth_{etf.lower()}.json"
        if not p.exists():
            continue
        blob = json.loads(p.read_text(encoding="utf-8"))
        end = blob.get("end_date")
        if end:
            ends[etf] = end
        cap = blob.get("tail_cap")
        if cap:
            caps[etf] = cap
    if not ends:
        return None, [], {}
    stalest = min(ends.values())
    laggards = sorted(e for e, v in ends.items() if v == stalest)
    # Only a laggard if something else is fresher; otherwise the whole sleeve
    # shares one date and naming every member is noise, not information.
    if len(laggards) == len(ends):
        laggards = []
    return stalest, laggards, caps


def cache_reach(cache_path: Path,
                tickers: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Stalest last-priced date across the tickers in a price cache."""
    if not cache_path.exists():
        return None, []
    frame = pd.read_parquet(cache_path)
    cols = [c for c in (tickers or frame.columns) if c in frame.columns]
    last: dict[str, str] = {}
    for c in cols:
        s = frame[c].dropna()
        if len(s):
            last[c] = str(pd.Timestamp(s.index[-1]).date())
    if not last:
        return None, []
    stalest = min(last.values())
    laggards = sorted(c for c, v in last.items() if v == stalest)
    if len(laggards) == len(last):
        laggards = []
    return stalest, laggards


def build(now_utc: datetime | None = None) -> dict:
    now = now_utc or datetime.now(timezone.utc)
    rows: list[dict] = []

    def add(key: str, venue: str, reach: str | None, laggards: list[str],
            source: str, why: str | None = None,
            why_plain: str | None = None, *, inputs: str = "inputs") -> None:
        cal = mcal.get_calendar(venue)
        lcs = last_completed_session_on(cal, now)
        venue_last = str(lcs.date()) if lcs is not None else None
        status, gap = classify(reach, venue_last, venue)
        # Reason 3 (see the module docstring). Reached only when the two
        # specific channels have nothing to say, so a named laggard or a
        # declared tail_cap always wins over the generic sentence.
        if status == BEHIND and not laggards and not why:
            why, why_plain = _uniform_stale_reason(
                reach, venue_last, gap, venue, inputs)
        rows.append({
            "sleeve": key,
            "label": SLEEVE_LABELS[key],
            "venue": venue,
            "data_through": reach,
            "venue_last_session": venue_last,
            "sessions_behind": gap,
            "status": status,
            "laggards": laggards,
            "source": source,
            "why": why,
            "why_plain": why_plain,
        })

    # --- A: US sector breadth panels -------------------------------------
    a_reach, a_lag, a_caps = panel_reach(list(UNIVERSE_ETFS))
    add("A", _venue_for(list(UNIVERSE_ETFS)), a_reach, a_lag,
        "breadth panels (constituents above their 200-day average)",
        *_cap_reason(a_caps, a_reach), inputs="breadth panels")

    # --- B: asset-class prices -------------------------------------------
    import run_asset_class_rotation as ac
    b_reach, b_lag = cache_reach(ac.PRICE_CACHE, list(ac.TICKERS))
    add("B", "NYSE", b_reach, b_lag, "price cache (asset-class ETFs)", None,
        inputs="cached price series")

    # --- C: thematic prices ----------------------------------------------
    import run_thematic_rotation as th
    c_reach, c_lag = cache_reach(th.PRICE_CACHE, list(th.TICKERS))
    add("C", "NYSE", c_reach, c_lag, "price cache (thematic ETFs)", None,
        inputs="cached price series")

    # --- D: European sector breadth panels --------------------------------
    d_reach, d_lag, d_caps = panel_reach(list(UNIVERSE_EUROPE_SECTORS))
    add("D", _venue_for(list(UNIVERSE_EUROPE_SECTORS)), d_reach, d_lag,
        "breadth panels (constituents above their 200-day average)",
        *_cap_reason(d_caps, d_reach), inputs="breadth panels")

    behind = [r for r in rows if r["status"] == BEHIND]
    return {
        "computed_at_utc": now.isoformat(),
        "strategies": rows,
        "all_current": not behind,
        "stalest": min((r["data_through"] for r in rows
                        if r["data_through"]), default=None),
        "n_behind": len(behind),
    }


def _cap_reason(caps: dict, reach: str | None) -> tuple[str | None, str | None]:
    """Turn a panel's declared tail_cap into one sentence, twice.

    compute_breadth records WHY it stopped a panel short; without this a page
    could only say a sleeve is behind, not that the vendor has yet to publish.

    TWO REGISTERS, because there are two audiences and one sentence cannot
    serve both. The dashboard and the operator CLI want the precise version;
    the reduced public page is written for a non-specialist reader and already
    refuses to print a bare ticker to one, so "constituents" and "venue" would
    be the same failure in a different vocabulary. Dates stay ISO in both -- the
    renderer formats them, so a date is never displayed two ways on one page.
    """
    if not caps or not reach:
        return None, None
    match = [c for c in caps.values() if c.get("capped_at") == reach]
    if not match:
        return None, None
    c = match[0]
    priced, traded = c.get("constituents_priced_to"), c.get("venue_last_completed")
    technical = (f"the constituents are priced to {priced} while the venue has "
                 f"traded to {traded} — the vendor publishes these prices about "
                 f"a session late.")
    plain = (f"the shares held inside these funds are priced up to {priced}, "
             f"while the exchange itself has traded to {traded} — the data "
             f"provider publishes European prices about a day late.")
    return technical, plain


def _uniform_stale_reason(reach: str | None, venue_last: str | None,
                          gap: int | None, venue: str,
                          inputs: str) -> tuple[str | None, str | None]:
    """Explain a BEHIND row whose inputs are ALL stale at the same date.

    Reason 3 of the three in the module docstring, and the one that had no
    voice until 2026-08-25. The other two channels cannot speak here BY
    CONSTRUCTION: `laggards` is deliberately emptied when the whole sleeve
    shares one date, and `_cap_reason` fires only on a declared `tail_cap`,
    which none of the US panels and neither price cache carries. So the most
    ordinary cause of all -- the pipeline has not been run since -- produced a
    row that said "behind" and stopped. Both the dashboard note and the public
    page render `why`/`why_plain` or nothing, so on 2026-08-24 all four sleeves
    printed a gap with no explanation beside it.

    TWO BRANCHES, because they are not the same fault and must not share a
    sentence:

      gap > 0  the data stops short of the venue. Nothing has been rebuilt.
               The reader's action is to run the refresh.
      gap < 0  the data runs PAST the last completed session, which is a
               partial bar -- a defect, not a staleness. Calling that "not
               refreshed yet" would point the reader at the wrong action and
               hide the case classify() goes out of its way to keep visible.

    TWO REGISTERS, matching `_cap_reason`: the technical string names the venue
    and goes to the dashboard and the operator CLI; the plain one goes to the
    reduced public page, which is written for a non-specialist and refuses the
    method vocabulary. Dates stay ISO in both -- the renderer formats them.
    """
    if not reach or not venue_last or gap is None or gap == 0:
        return None, None
    if gap > 0:
        technical = (f"every one of the {inputs} stops at {reach} while "
                     f"{venue} has completed {venue_last} — none lags the "
                     f"others, so they have not been rebuilt since.")
        plain = (f"the data behind this strategy stops on {reach}, while the "
                 f"market has traded to {venue_last} — it has not been "
                 f"rebuilt since.")
        return technical, plain
    technical = (f"the {inputs} carry {reach}, past {venue}'s last completed "
                 f"session {venue_last} — that session has not closed, so this "
                 f"is a partial bar rather than a refresh.")
    plain = (f"the data behind this strategy is dated {reach}, later than the "
             f"last full trading day, {venue_last} — that day has not "
             f"finished, so the figure is incomplete.")
    return technical, plain


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default=str(DATA_DIR / "strategy_freshness.json"))
    args = ap.parse_args(argv)
    r = build()

    print(f"\nSTRATEGY DATA FRESHNESS — {r['computed_at_utc'][:16]}Z\n")
    print(f"  {'':2s} {'strategy':16s} {'venue':6s} {'data through':13s} "
          f"{'venue last':13s} status")
    for s in r["strategies"]:
        gap = s["sessions_behind"]
        tag = ("current" if s["status"] == CURRENT
               else f"{gap} session{'s' if gap not in (1, -1) else ''} behind"
               if gap is not None else "unknown")
        print(f"  {s['sleeve']:2s} {s['label']:16s} {s['venue']:6s} "
              f"{str(s['data_through']):13s} {str(s['venue_last_session']):13s} {tag}")
        if s["laggards"]:
            print(f"     stalest input: {', '.join(s['laggards'])}")
        if s["why"] and s["status"] != CURRENT:
            print(f"     {s['why']}")

    if r["all_current"]:
        print("\n  All four strategies are current to their own venue's last close.")
    else:
        names = ", ".join(f"{s['sleeve']} ({s['label']})"
                          for s in r["strategies"] if s["status"] == BEHIND)
        print(f"\n  BEHIND: {names} — do not read these as refreshed.")

    Path(args.json).write_text(json.dumps(r, indent=1), encoding="utf-8")
    print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
