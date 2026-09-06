"""Settle a price frame's tail against the vendor, one name at a time.

WHY THIS EXISTS (2026-09-06). Sleeve C reported HOLD for the 7/8 September
fill with 25 of 26 names on the decision row: BTC-USD's Friday close was
blank. The bar was not withheld on Sunday — yfinance served it by 06:00 UTC
and the Saturday 01:55 UTC fetch had simply been too early for it. What kept
it blank was the CACHE: the failed Saturday run wrote the Friday row (Norgate
had the 24 US lines, yfinance had not yet the crypto line), the gitignored
cache survived the clone's restore, and on Sunday the engine read
``cached.index.max()`` — Friday — called the cache current and reused it.
Sleeve B had already learnt this on 2026-08-31 (SPY blank on a row the
index reached) and measures currency per COLUMN; sleeve C never got the
same rule. One definition now, here, for both.

Two helpers, both pure given the fetch they are handed:

  cache_current_through   the least-current column's last priced date — a
                          cache is only as current as its emptiest name;
  heal_hollow_tail        for the rows past the last FULL row, ask the
                          vendor single-ticker for every blank cell of a
                          name that was priced on that full row, and fill
                          what it serves. Never drops a row and never
                          invents a value: a name the vendor still does not
                          serve stays blank, the row stays partial, and
                          live_targets' 100% coverage floor turns it into a
                          HOLD — which is the right answer for a signal that
                          is genuinely missing a member.

compute_breadth.verify_price_tail is the constituent-panel sibling with
different semantics (it may DROP a row the vendor confirms unserved, because
a breadth panel is a roster share and a row below the floor is not a
reading). It shares the single-ticker request below.

Python datetime months are 1-indexed (January = 1). Dates are compared as
dates, never as strings or weekday counts.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf

from stall_guard import run_with_deadline

PROBE_PERIOD = "10d"          # covers any tail row a refresh can carry
CALL_DEADLINE_S = 20.0        # per single-ticker request
HEAL_BUDGET_S = 300.0         # wall clock for one frame's worth of re-requests


def single_ticker_closes(symbol: str, period: str = PROBE_PERIOD,
                         deadline_s: float = CALL_DEADLINE_S) -> pd.Series | None:
    """The closes the vendor serves for ``symbol`` on its own, on a tz-naive
    daily index. None means NO ANSWER (the request failed or came back
    empty), which is different from a series that lacks a date — that is
    the vendor saying there is no bar. ``symbol`` is the vendor's symbol;
    callers with an iShares-style ticker normalise it first."""
    try:
        hist = run_with_deadline(
            lambda: yf.Ticker(symbol).history(period=period, auto_adjust=True),
            seconds=deadline_s, label=f"single-ticker probe {symbol}")
    except Exception:  # noqa: BLE001 — a failed probe is no answer, never a verdict
        return None
    if hist is None or len(hist) == 0 or "Close" not in hist.columns:
        return None
    s = hist["Close"].astype(float)
    idx = pd.to_datetime(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    return s[~s.index.duplicated(keep="last")]


def cache_current_through(cached: pd.DataFrame | None,
                          needed=None) -> date | None:
    """The date the cache is current through: the LEAST current column's last
    priced date, over ``needed`` (default: every column), ignoring columns
    with no data at all. None for an empty or absent cache.

    Measured on values, never on the index. ``index.max()`` is the union of
    every column's dates, so one name blank on the newest row is invisible
    to it — the 2026-08-31 SPY case on sleeve B and the 2026-09-06 BTC-USD
    case on sleeve C, both of which reused a cache a session short for the
    name that mattered."""
    if cached is None or len(cached) == 0 or cached.shape[1] == 0:
        return None
    cols = [c for c in (needed if needed is not None else cached.columns)
            if c in cached.columns]
    per_col = [cached[c].dropna().index.max() for c in cols
               if cached[c].notna().any()]
    if not per_col:
        return None
    return pd.Timestamp(min(per_col)).date()


def heal_hollow_tail(df: pd.DataFrame, names, through: date | None,
                     exclude=(), fetch_single=None,
                     budget_s: float = HEAL_BUDGET_S, clock=time.monotonic,
                     now_utc: datetime | None = None,
                     ) -> tuple[pd.DataFrame, dict | None]:
    """Fill blank tail cells from single-ticker requests; never drop a row.

    The tail is every row after the last row on which ALL of ``names`` are
    priced, up to and including ``through`` (the last completed session —
    rows beyond it are a partial-bar question for the caller's cap, not a
    vendor question). Only names priced on that last full row are asked, so
    a delisted column cannot be "healed" into existence, and ``exclude``
    names columns owned by another source (taken whole from Norgate) that
    must not receive a yfinance cell.

    Returns the frame (a copy when anything was asked) and a record of what
    was asked and what came back, or None when there was nothing to ask.
    """
    held = [n for n in names if n in df.columns]
    if not held or len(df) == 0:
        return df, None
    full = df[held].notna().all(axis=1)
    if not full.any():
        return df, None
    last_full = df.index[full][-1]
    tail = [ts for ts in df.index
            if ts > last_full and (through is None
                                   or pd.Timestamp(ts).date() <= through)]
    if not tail:
        return df, None
    excluded = set(exclude or ())
    live = [n for n in held if n not in excluded]
    hollow_by_row = {ts: [n for n in live if pd.isna(df.at[ts, n])] for ts in tail}
    if not any(hollow_by_row.values()):
        return df, None

    df = df.copy()
    fetch = fetch_single or single_ticker_closes
    answers: dict[str, pd.Series | None] = {}

    def ask(n: str) -> pd.Series | None:
        if n not in answers:
            try:
                answers[n] = fetch(n)
            except Exception:  # noqa: BLE001 — no answer, never a verdict
                answers[n] = None
        return answers[n]

    started = clock()
    rows: list[dict] = []
    exhausted = False
    for ts in tail:
        hollow = hollow_by_row[ts]
        if not hollow:
            continue
        rec = {"date": str(pd.Timestamp(ts).date()), "hollow": list(hollow),
               "filled": [], "unserved": [], "no_answer": []}
        for n in hollow:
            if clock() - started > budget_s:
                exhausted = True
                break
            s = ask(n)
            if s is None:
                rec["no_answer"].append(n)
            elif ts in s.index and pd.notna(s.loc[ts]):
                df.at[ts, n] = float(s.loc[ts])
                rec["filled"].append(n)
            else:
                rec["unserved"].append(n)
        rows.append(rec)
        if exhausted:
            break
    stamp = (now_utc or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return df, {
        "checked_at_utc": stamp,
        "probe": "yfinance single-ticker history",
        "last_full_row": str(pd.Timestamp(last_full).date()),
        "rows": rows,
        "budget_exhausted": exhausted,
    }


def report_heal(record: dict | None, label: str = "") -> None:
    """One log line per tail row, in the register the refresh log uses."""
    if not record:
        return
    tag = f"{label}: " if label else ""
    for r in record["rows"]:
        parts = []
        if r["filled"]:
            parts.append(f"filled {len(r['filled'])} single-ticker "
                         f"({', '.join(r['filled'][:6])}"
                         + (", ..." if len(r["filled"]) > 6 else "") + ")")
        if r["unserved"]:
            parts.append(f"{len(r['unserved'])} still not served by the vendor "
                         f"({', '.join(r['unserved'][:6])}) — left blank, the "
                         f"row stays partial")
        if r["no_answer"]:
            parts.append(f"{len(r['no_answer'])} unanswered "
                         f"({', '.join(r['no_answer'][:6])}) — left blank")
        print(f"  {tag}tail row {r['date']} had {len(r['hollow'])} blank "
              f"cell(s) the batch left behind: " + "; ".join(parts) + ".",
              flush=True)
    if record.get("budget_exhausted"):
        print(f"  {tag}tail heal stopped at its {HEAL_BUDGET_S:.0f}s budget; "
              f"remaining blank cells were not asked.", flush=True)
