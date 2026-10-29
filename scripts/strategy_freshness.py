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

ONE ASYMMETRY IN WHAT A CHECKOUT CONTAINS. The breadth panels are committed;
the two price caches are gitignored bulk vendor downloads, so on a fresh
checkout -- the CI runner that recomputes this report daily -- B and C's
inputs are simply not there. Absent is not unknown: those caches move only
when a local refresh writes them, and the bound they reached when last
measured is recorded in the committed copy of this very report. A missing
cache therefore CARRIES that bound forward and re-judges it against today's
clock -- the verdict refreshes daily, the measurement refreshes when the
caches do, and the row's `source` says which of the two happened. A cache
that EXISTS but yields nothing stays unknown (that is a fault to surface,
not to paper over), and so does a missing prior report: the module never
invents a date.

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

A FILLED NATIVE ROW IS NOT A CURRENT ONE (2026-10-28). Sleeve C's 159801.SZ is
a Shenzhen close forward-filled onto NYSE for up to ten days, so its cached
column reaches the last NYSE row whether or not Shenzhen printed that session
-- the same false-current live_targets now HOLDs on. Its reach is therefore
read from the native-observation record beside the cache (native_sessions),
never from the column: the newest row whose required native close, and that
of every row in its 200-row signal window (MA_PERIOD), was observed
(2026-10-29; the first cut checked ten rows). No matching record means
unknown, not current; a carried bound is accepted only from a prior row that
was itself measured that way, over at least the same window.

Python datetime months are 1-indexed (January = 1). Session arithmetic goes
through pandas_market_calendars via session_bounds -- never a manual weekday.

Usage:
    python scripts/strategy_freshness.py
    python scripts/strategy_freshness.py --json data/strategy_freshness.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from venue_calendars import get_calendar as _venue_cal  # noqa: E402
from etf_registry import (  # noqa: E402
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
    get_etf,
)
from session_bounds import last_completed_session_on  # noqa: E402
import native_sessions  # noqa: E402
import price_source  # noqa: E402

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
    sched = _venue_cal(cal_name).schedule(
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
    missing = []
    for etf in universe:
        p = DATA_DIR / f"breadth_{etf.lower()}.json"
        if not p.exists():
            missing.append(etf)
            continue
        blob = json.loads(p.read_text(encoding="utf-8"))
        end = blob.get("end_date")
        if end:
            ends[etf] = end
        else:
            missing.append(etf)
        cap = blob.get("tail_cap")
        if cap:
            caps[etf] = cap
    if missing:
        return None, sorted(missing), caps
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
                tickers: list[str] | None = None,
                override: dict[str, str | None] | None = None,
                ) -> tuple[str | None, list[str]]:
    """Stalest last-priced date across the tickers in a price cache.

    ``override`` replaces the column's own last date for the named tickers --
    the native-venue lines, whose cached column is forward-filled and cannot
    say how far it really reaches (see native_reach). A None there is unknown
    and, like an empty column, makes the whole answer unknown.
    """
    if not cache_path.exists():
        return None, []
    frame = pd.read_parquet(cache_path)
    cols = list(tickers if tickers is not None else frame.columns)
    override = override or {}
    missing = [c for c in cols if c not in frame.columns or not frame[c].notna().any()
               or (c in override and override[c] is None)]
    if missing:
        return None, sorted(missing)
    last: dict[str, str] = {}
    for c in cols:
        if c in override:
            last[c] = override[c]
            continue
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


def _blank_native(ticker: str, error: str | None,
                  window_rows: int | None = None) -> dict:
    return {"venue": native_sessions.venue_for(ticker), "last_valid": None,
            "last_observed_native_close": None,
            "required_native_session": None, "native_close_carried": None,
            "missing_native_sessions": None, "window_rows": window_rows,
            "holiday_data_through": None, "error": error}


def _iso(value) -> str | None:
    return None if value is None or pd.isna(value) else str(pd.Timestamp(value).date())


def native_reach(cache_path: Path, tickers: list[str], *,
                 window_rows: int) -> dict[str, dict]:
    """How far each native-venue line in a cache REALLY reaches.

    Per ticker: ``last_valid`` is the newest cache row whose required native
    session, and that of every row in its ``window_rows``-row signal window,
    was observed (native_sessions.provenance) -- the line's reach, or None
    when no row can be vouched for. ``required_native_session``,
    ``native_close_carried`` and ``missing_native_sessions`` describe the
    cache's last row; ``last_observed_native_close`` is the newest observed
    native date.

    Rows are the cache's own index -- the rows compute_signal averages over --
    not the line's non-blank rows, so the window is counted as the signal
    counts it. The record comes from the cache sidecar and must belong to THIS
    cache (parse_sidecar checks the construction, ``through``, every line, the
    record's length and the digest of the cache's rows and native values);
    otherwise each line is unknown with the reason in ``error``. Never reads
    the filled column as evidence.
    """
    frame = pd.read_parquet(cache_path)
    if not len(frame):
        return {t: _blank_native(t, "the cache has no rows", window_rows)
                for t in tickers}
    through = native_sessions.session_dates([frame.index.max()])[0]
    observed = native_sessions.parse_sidecar(
        price_source.read_cache_sidecar(cache_path), frame, tickers)
    floor = through - pd.Timedelta(days=native_sessions.SIDECAR_WINDOW_DAYS)
    out: dict[str, dict] = {}
    for t in tickers:
        if observed is None:
            out[t] = _blank_native(t, "no native-observation record in the cache "
                                      "sidecar matching this cache", window_rows)
            continue
        rec = out[t] = _blank_native(t, None, window_rows)
        obs = observed[t]
        if len(obs):
            rec["last_observed_native_close"] = str(obs[-1].date())
        rows = (native_sessions.session_dates(frame.index)
                if t in frame.columns and frame[t].notna().any()
                else pd.DatetimeIndex([]))
        # A suffix of the cache's rows, so window positions are unchanged.
        rows = rows[rows >= floor]
        if not len(rows):
            rec["error"] = "no priced cache row within the record window"
            continue
        prov = native_sessions.provenance(rows, obs, rec["venue"],
                                          window_rows=window_rows)
        rec["required_native_session"] = _iso(prov["required_native_session"].iloc[-1])
        rec["native_close_carried"] = _iso(prov["native_close_carried"].iloc[-1])
        rec["missing_native_sessions"] = [
            _iso(s) for s in prov["missing_native_sessions"].iloc[-1]]
        rec["holiday_data_through"] = _iso(prov.attrs.get("holiday_data_through"))
        ok = prov.index[prov["current"].astype(bool).to_numpy()]
        if len(ok):
            rec["last_valid"] = str(ok[-1].date())
        rec["error"] = prov.attrs.get("error") or (
            None if len(ok) else
            f"no cache row within the record window carries its required "
            f"native close across its {window_rows}-row signal window")
    return out


def _prior_row(sleeve: str) -> tuple[dict | None, str | None]:
    """The committed report's row for ``sleeve`` that measured a bound, and
    the report's ``computed_at_utc``."""
    path = DATA_DIR / "strategy_freshness.json"
    if not path.exists():
        return None, None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None, None
    for row in blob.get("strategies") or []:
        if row.get("sleeve") == sleeve and row.get("data_through"):
            return row, blob.get("computed_at_utc")
    return None, None


def _last_measured(sleeve: str) -> tuple[str | None, list[str], str | None]:
    """``(data_through, laggards, computed_at_utc)`` as last measured on disk.

    Reads the committed copy of this module's own report. Only a row with a
    real ``data_through`` counts -- carrying a None forward would launder
    "unknown" into "known", which is the one thing the fallback must not do.
    """
    row, measured_at = _prior_row(sleeve)
    if row is None:
        return None, [], None
    return row["data_through"], list(row.get("laggards") or []), measured_at


def cache_reach_or_carried(
        cache_path: Path, tickers: list[str], sleeve: str,
        source: str) -> tuple[str | None, list[str], str, bool]:
    """Measure the cache, or carry the last measured bound when it is ABSENT.

    The two engine price caches are gitignored (bulk vendor downloads), so a
    fresh checkout -- the CI runner recomputing this report daily since
    2026-08-25 -- does not have them, while every breadth panel, being
    committed, is present. The first such recompute wrote
    ``data_through: null`` for B and C and the reduced page failed its own
    parity gate on rows that answered nothing -- while the bound it needed,
    2026-08-21, sat in the committed report it had just overwritten.

    Absence of the FILE is the trigger, not emptiness of the frame: a cache
    that exists but yields no dates has lost its columns, a fault this report
    must surface rather than smooth over. The carried bound errs only on the
    stale side -- nothing but a local refresh writes these caches, and if one
    ran without committing, the carried date understates, which is the safe
    direction for a freshness claim.
    """
    reach, laggards = cache_reach(cache_path, tickers)
    if reach is not None or cache_path.exists():
        return reach, laggards, source, False
    carried, laggards, measured_at = _last_measured(sleeve)
    if carried is None:
        return None, [], source, False
    stamp = f" of {measured_at[:16]}Z" if measured_at else ""
    return carried, laggards, (
        f"{source} — bound carried from the committed report{stamp}; the "
        f"cache is not present on this checkout"), True


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _strict_date(value) -> date | None:
    """``value`` as a date only if it is exactly ``YYYY-MM-DD`` and a real
    calendar date; otherwise None. date.fromisoformat alone is too lenient on
    Python 3.11+ (it takes "20261028" and week dates), hence the pattern.
    Python datetime months are 1-indexed (January = 1)."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def native_cache_reach_or_carried(
        cache_path: Path, tickers: list[str], native_tickers: list[str],
        sleeve: str, source: str, *, window_rows: int,
) -> tuple[str | None, list[str], str, bool, dict[str, dict]]:
    """cache_reach_or_carried for a cache holding native-venue lines.

    The same four values, plus ``{ticker: native record}`` (see native_reach).
    A present cache measures each native line from its observation record
    across the ``window_rows``-row signal window. An absent one carries the
    prior bound ONLY if that row recorded a valid native reach for every line,
    measured over at least the same window, and no later than it -- a bound
    measured on the filled column, or over a shorter window (the ten-row tail
    of 2026-10-28), is exactly the claim this must not repeat.
    """
    native_tickers = [t for t in native_tickers if t in tickers]
    if not native_tickers:
        return (*cache_reach_or_carried(cache_path, tickers, sleeve, source), {})
    if cache_path.exists():
        native = native_reach(cache_path, native_tickers, window_rows=window_rows)
        reach, laggards = cache_reach(
            cache_path, tickers, {t: r["last_valid"] for t, r in native.items()})
        return reach, laggards, source, False, native
    reach, laggards, src, carried = cache_reach_or_carried(
        cache_path, tickers, sleeve, source)
    if not carried:
        return reach, laggards, src, False, {
            t: _blank_native(t, "the cache is not present on this checkout and "
                                "no prior bound was measured", window_rows)
            for t in native_tickers}
    prior = (_prior_row(sleeve)[0] or {}).get("native_provenance") or {}
    recs = {t: prior.get(t) for t in native_tickers}

    def _covers(t, r) -> bool:
        """A record for this line's venue, measured over at least this
        window, whose ``last_valid`` is a real date no earlier than the
        carried bound. Dates are PARSED, never compared as strings: until
        2026-10-29 "2026-9-01" and "garbled" both sorted after "2026-10-28"
        and carried sleeve C as current (review finding 2)."""
        if not isinstance(r, dict) or r.get("venue") != native_sessions.venue_for(t):
            return False
        w = r.get("window_rows")
        if not (isinstance(w, int) and not isinstance(w, bool) and w >= window_rows):
            return False
        valid = _strict_date(r.get("last_valid"))
        return valid is not None and bound is not None and valid >= bound

    bound = _strict_date(reach)
    if all(_covers(t, r) for t, r in recs.items()):
        return reach, laggards, src, True, recs
    return None, sorted(native_tickers), source, False, {
        t: _blank_native(t, "the cache is not present on this checkout and the "
                            "committed report records no native-observation "
                            f"provenance over the {window_rows}-row signal "
                            "window for this line", window_rows)
        for t in native_tickers}


def _native_reason(native: dict[str, dict], laggards: list[str],
                   ) -> tuple[str | None, str | None]:
    """Explain a row held back by a native-venue line, in both registers.

    Only when that line is NAMED: a native line sharing the sleeve's date is
    not the cause, and the ordinary channels speak for it.
    """
    named = [t for t in laggards if t in native]
    if not named:
        return None, None
    technical, plain = [], []
    for t in named:
        r = native[t]
        if r.get("last_valid") is None:
            technical.append(
                f"{t}: no {r.get('venue')} close is shown to be observed across "
                f"a recent row's signal window ({r.get('error')}); its cached "
                f"price is "
                f"forward-filled and is not evidence {r.get('venue')} printed "
                f"those sessions")
            plain.append(
                "one fund in this strategy trades on an overseas exchange, and "
                "there is no record that its recent prices were actually "
                "published there, so they are not counted as current")
        elif r.get("missing_native_sessions"):
            technical.append(
                f"{t} is valid only through {r['last_valid']}: no "
                f"{r.get('venue')} close was observed for "
                f"{', '.join(r['missing_native_sessions'])}, so the cache "
                f"carries a forward-filled price there and every later row "
                f"whose {r.get('window_rows')}-row signal window reaches it "
                f"draws on that price")
            plain.append(
                f"one fund in this strategy trades on an overseas exchange "
                f"whose prices are confirmed only up to {r['last_valid']}; "
                f"a later day repeats an earlier price")
        else:
            technical.append(
                f"{t} is valid only through {r['last_valid']}: its newest "
                f"observed {r.get('venue')} close is "
                f"{r.get('native_close_carried')} while the cache's last row "
                f"requires the {r.get('required_native_session')} session — "
                f"later rows carry a forward-filled price")
            plain.append(
                f"one fund in this strategy trades on an overseas exchange "
                f"whose prices are confirmed only up to {r['last_valid']}; "
                f"later days repeat an earlier price")
        bound = r.get("holiday_data_through")
        unannounced = [s for s in (r.get("missing_native_sessions") or [])
                       if bound and s > bound]
        if unannounced:
            technical.append(
                f"{t}: {', '.join(unannounced)} lie past the {r.get('venue')} "
                f"holiday data (through {bound}); if the exchange was shut, "
                f"pin the closure in china_market_holidays.py from the "
                f"official notice")
    return "; ".join(technical) + ".", "; ".join(dict.fromkeys(plain)) + "."


def build(now_utc: datetime | None = None) -> dict:
    now = now_utc or datetime.now(timezone.utc)
    rows: list[dict] = []

    def add(key: str, venue: str, reach: str | None, laggards: list[str],
            source: str, why: str | None = None,
            why_plain: str | None = None, *, inputs: str = "inputs",
            carried: bool = False, extra: dict | None = None) -> None:
        cal = _venue_cal(venue)
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
            # True when `data_through` is the last MEASURED bound rather than
            # a measurement made now — the gitignored-cache case above. The
            # verdict beside it is still judged against today's clock.
            "carried": carried,
            "why": why,
            "why_plain": why_plain,
            **(extra or {}),
        })

    # --- A: US sector breadth panels -------------------------------------
    a_reach, a_lag, a_caps = panel_reach(list(UNIVERSE_ETFS))
    add("A", _venue_for(list(UNIVERSE_ETFS)), a_reach, a_lag,
        "breadth panels (constituents above their 200-day average)",
        *_cap_reason(a_caps, a_reach), inputs="breadth panels")

    # --- B: asset-class prices -------------------------------------------
    import run_asset_class_rotation as ac
    b_reach, b_lag, b_source, b_carried = cache_reach_or_carried(
        ac.PRICE_CACHE, list(ac.TICKERS), "B", "price cache (asset-class ETFs)")
    add("B", "NYSE", b_reach, b_lag, b_source, None,
        inputs="cached price series", carried=b_carried)

    # --- C: thematic prices ----------------------------------------------
    import run_thematic_rotation as th
    # 159801.SZ's column is forward-filled onto NYSE; its reach comes from
    # the cache's native-observation record instead (module docstring).
    c_reach, c_lag, c_source, c_carried, c_native = native_cache_reach_or_carried(
        th.PRICE_CACHE, list(th.TICKERS), th._native_tickers(), "C",
        "price cache (thematic ETFs)", window_rows=th.MA_PERIOD)
    add("C", "NYSE", c_reach, c_lag, c_source, *_native_reason(c_native, c_lag),
        inputs="cached price series", carried=c_carried,
        extra={"native_provenance": c_native})

    # --- D: European sector breadth panels --------------------------------
    d_reach, d_lag, d_caps = panel_reach(list(UNIVERSE_EUROPE_SECTORS))
    add("D", _venue_for(list(UNIVERSE_EUROPE_SECTORS)), d_reach, d_lag,
        "breadth panels (constituents above their 200-day average)",
        *_cap_reason(d_caps, d_reach), inputs="breadth panels")

    behind = [r for r in rows if r["status"] == BEHIND]
    return {
        "computed_at_utc": now.isoformat(),
        "strategies": rows,
        "all_current": bool(rows) and all(r["status"] == CURRENT for r in rows),
        "n_unknown": sum(r["status"] == UNKNOWN for r in rows),
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
    # STATE THE FACT, NOT A CAUSE THIS CANNOT KNOW (2026-08-31).
    #
    # Both sentences used to assert the routine European publication lag —
    # "the vendor publishes these prices about a session late", and in the
    # plain register "publishes European prices about a day late". That was
    # written when only sleeve D could trip this, and it is now wrong twice
    # over. On 2026-08-31 sleeve A tripped it, so the PUBLIC page would have
    # explained a US-sectors gap as a European publishing delay. And the cause
    # itself was wrong: the vendor had served that session and RETRACTED it,
    # which is a different fault with a different remedy from a late publish.
    #
    # A cap records WHAT was missing, never WHY. Saying only what is known
    # keeps the sentence true for a late publish, a retraction and an outage
    # alike, and stops a caveat elsewhere on the page being quietly softened
    # by a confident explanation sitting two lines below it.
    technical = (f"the constituents are priced to {priced} while the venue has "
                 f"traded to {traded} — the newest session was not available "
                 f"from the price source when this was built.")
    plain = (f"the shares held inside these funds are priced up to {priced}, "
             f"while the exchange itself has traded to {traded} — the newest "
             f"day's prices were not available when this was built.")
    return technical, plain


def verdict_has_lapsed(report: dict | None,
                       now_utc: datetime | None = None) -> bool:
    """True when a venue has completed a session this report never saw.

    THE VERDICT EXPIRES; THE FILE DOES NOT. Every row here is a comparison
    against ``venue_last_session`` as it stood at ``computed_at_utc``, so the
    moment a venue closes again the stored answer describes a market that has
    moved on. `status` and `sessions_behind` do not decay into vagueness --
    they stay confidently wrong, which is the worst way for a freshness widget
    to fail.

    That is not hypothetical. The report committed on Sun 2026-08-23 said all
    four sleeves were `current` with a zero gap, which was TRUE when computed:
    Friday was the last completed session. From Monday's close it was false,
    and the dashboard published "current" over a book a session behind until
    Tuesday. Nothing regenerates this file except a local refresh_all.py run,
    and the mtime guard beside it in pipeline.main compares DERIVED against
    SOURCE -- when neither moves, both look agreed and the check is silent.
    Only real time can catch this, so this is the check that reads the clock.

    FAILS CLOSED. An unreadable calendar returns True rather than False: a
    verdict that cannot be verified is treated as expired, because the cost of
    withholding the strip for one build is a blank space, and the cost of the
    other mistake is a false claim on a published page.
    """
    now = now_utc or datetime.now(timezone.utc)
    for row in (report or {}).get("strategies") or []:
        seen, venue = row.get("venue_last_session"), row.get("venue")
        if not seen or not venue:
            continue
        try:
            lcs = last_completed_session_on(_venue_cal(venue), now)
        except Exception:
            return True
        if lcs is not None and str(lcs.date()) > seen:
            return True
    return False


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
        if s.get("carried"):
            # The one state a reader of this log cannot infer from the table:
            # the bound above was not measured on this checkout.
            print(f"     {s['source']}")

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
