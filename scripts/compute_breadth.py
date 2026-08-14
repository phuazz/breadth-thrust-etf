"""Step 2 — compute breadth components, z-scores, and signal flags for SOXX.

Inputs : data/constituents_soxx.json (Step 1 output)
Outputs: data/breadth_soxx.json + data/prices_cache.parquet (gitignored cache)

Pipeline:
  1. Build the union universe of all tickers that ever appeared in a SOXX
     weekly snapshot 2018-2026.
  2. Download adjusted-close history for that universe from yfinance with
     parquet-backed disk cache.
  3. For each trading day in [start_friday, end_friday] on the ETF's own
     calendar (registry `trading_calendar`, default NYSE; XETR for the
     Europe sector funds), resolve the active constituent roster (most
     recent Friday snapshot with date <= T) and compute three breadth
     components on data available at T:
       - RSI breadth   : share of constituents with 14d Wilder-RSI > 70
       - MA breadth    : share above 50d simple MA
       - Highs breadth : share at a 63d closing high
     Each component's denominator is "constituents with enough price
     history at T", which by design excludes the yfinance-missing names.
     Per-day n_constituents and n_with_price are logged as a quality flag.
  4. Z-score each component on an EXPANDING window of past data (the day-T
     stat uses only data through T-1). Composite = mean of the three z's.
  5. Component triggers:
       - RSI    : top decile of its own expanding history (>= expanding p90 of past)
       - MA     : Zweig thrust — crosses from < 50% to >= 80% within 20 trading days
       - Highs  : top decile of expanding history
  6. Composite "fires" when (a) composite_z crosses above its expanding 90th
     percentile AND (b) at least 2 of 3 components are triggered AND (c) at
     least SIGNAL_ELIGIBLE_AFTER days of breadth history have accumulated.
  7. Also compute the composite expanding 10th percentile threshold so Step 3
     can detect the regime-exit condition without recomputing.

Three ways this could be silently wrong (and our defences):
  - Look-ahead in threshold      -> .shift(1).expanding() throughout.
  - Stale-roster contamination   -> binary-walk through Friday snapshots,
                                    never use future or current rosters.
  - Differential missingness     -> cannot be fixed at this data source;
                                    logged per day so Step 3 can quarantine
                                    suspect periods or weight signals down.
  - Holiday-NaN window poisoning -> indicators are computed on each ticker's
                                    own traded sessions (per_ticker_apply),
                                    not on the union date grid. On multi-
                                    exchange panels a single home-venue
                                    holiday NaN inside rolling(w,
                                    min_periods=w) otherwise invalidates the
                                    ticker's MA/high for the next w rows,
                                    which erased ~40% of European ma_breadth
                                    coverage in annual April-July blocks.

Run:
    python scripts/compute_breadth.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import get_etf  # noqa: E402
from stall_guard import (  # noqa: E402
    DEFAULT_DOWNLOAD_DEADLINE_S,
    run_with_deadline,
)

# Force UTF-8 stdout for Windows console.
sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_ETF = "SOXX"


def paths_for(etf: str) -> dict:
    """Return per-ETF input / output file paths."""
    e = etf.lower()
    return {
        "constituents": DATA_DIR / f"constituents_{e}.json",
        "prices_cache": DATA_DIR / f"prices_cache_{e}.parquet",
        "out": DATA_DIR / f"breadth_{e}.json",
    }


# Back-compat module-level constants (SOXX). Existing callers / tests that
# imported these by name still work.
_paths = paths_for(DEFAULT_ETF)
CONSTITUENTS_PATH = _paths["constituents"]
PRICES_CACHE = _paths["prices_cache"]
OUT_PATH = _paths["out"]

# Indicator periods, all in trading days.
RSI_PERIOD = 14
MA_PERIOD = 50
HIGH_PERIOD = 63

# Minimum number of constituents with a usable indicator before a breadth
# value is emitted at all. Below this the bar is NaN (null in the JSON),
# the same representation already used for the pre-warmup rows, so
# consumers see a shape they already handle.
#
# WHY THIS EXISTS. The guard used to be `if ma_valid.any()` — one single
# name was enough. On 2026-08-07 the price vendor had not yet published
# Friday European closes, so every Europe panel computed its breadth from
# TWO constituents out of 26-32 and published 0.0, 0.5 or 1.0. Those are
# not breadth readings; a proportion over two names can only take three
# values, and Strategy D would have been reading one as a signal.
#
# WHY 5, AND NOT MORE. This is calibrated against how thin the deployed
# panels legitimately get, not chosen for roundness. EXH1's historical
# MINIMUM coverage is 8 names and its 10th percentile is 14, so a floor of
# 10 would delete 1,270 genuine bars across 16 panels. A floor of 5 removes
# 40 bars in the whole history (0.05%), all of them degenerate.
#
# Note this is deliberately an ABSOLUTE floor, not a share of the roster
# and not a drop against recent coverage. IDP6 legitimately carries 332
# unpriced names out of 603, so a share-of-roster rule would delete a
# healthy panel; and ICHN once fell to 131 valid names of 540, which is a
# collapse in relative terms but still a perfectly good sample for a
# proportion. What makes breadth meaningless is a small denominator.
MIN_BREADTH_NAMES = 5
RSI_OVERBOUGHT = 70.0

# Statistical window controls.
# - Z-scores need a minimum number of past observations before they are
#   meaningful; under this they are NaN.
# - Percentile thresholds need a wider base — 63 trading days (~3 months).
# - Signal eligibility kicks in only after 252 trading days (~1 year) of
#   breadth history so the percentile thresholds are stable.
Z_SCORE_MIN_PERIODS = 20
PCT_MIN_PERIODS = 63
SIGNAL_ELIGIBLE_AFTER = 252

# ---- Coverage floor on the CURRENT roster (2026-08-09) -------------------
#
# Measured as n_with_ma50 / n_constituents on the latest date: of the names
# actually IN the index today, how many did the vendor price deeply enough
# to carry a 50-day average. This is the denominator that matters. The
# existing data_quality ratio (tickers_with_any_yf_data / universe_size)
# runs 70-90% on healthy panels because the universe carries every name
# that has ever been a constituent, so it cannot separate "delisted names,
# as expected" from "the vendor returned nothing today".
#
# Why a floor at all: breadth is a RATIO, so a thin fetch does not look
# broken. It returns a plausible number computed on whatever came back. On
# 2026-08-08 two panels were refreshed and committed on partial downloads —
# EXH2 on 2 of 37 constituents (the display guard suppressed the bar, so
# nothing false was shown) and IDP6, a DEPLOYED Strategy A panel, on 371 of
# 603. IDP6 published 0.6334; recomputed at 99.5% coverage it is 0.66, so
# the thin sample was 2.7pp out. Nothing in the pipeline objected to
# either.
#
# Calibrated against all 38 committed panels on 2026-08-09. Healthy sits at
# 97-100% (30 of 38 at 100%), with a structural tail at ITWN 89.7% and
# ICHN 93.6% where some Taiwanese and Chinese lines genuinely lack yfinance
# history. Then a clean gap to the two failures at 61.5% and 5.4%. WARN
# sits below the structural tail so ITWN does not cry wolf every week;
# FAIL sits below anything a real roster has produced.
MIN_ROSTER_COVERAGE_WARN = 0.85
MIN_ROSTER_COVERAGE_FAIL = 0.50

# Never set in CI. Lets a local run publish a knowingly thin panel.
COVERAGE_OVERRIDE_ENV = "ALLOW_THIN_BREADTH"


def coverage_verdict(n_with_ma: int, n_constituents: int) -> tuple[str, float]:
    """Classify roster coverage as 'ok', 'warn' or 'fail'.

    Pure so the floors can be tested without a network fetch. Both floors
    are inclusive at the bottom of the better band: exactly at a floor
    passes it, so the documented percentages read the way people expect.

    A roster of zero is 'fail', not a division error — a panel with no
    current constituents has nothing to compute breadth on.
    """
    if n_constituents <= 0:
        return "fail", 0.0
    coverage = n_with_ma / n_constituents
    if coverage < MIN_ROSTER_COVERAGE_FAIL:
        return "fail", coverage
    if coverage < MIN_ROSTER_COVERAGE_WARN:
        return "warn", coverage
    return "ok", coverage

# Zweig MA thrust parameters.
ZWEIG_LOW = 0.50
ZWEIG_HIGH = 0.80
ZWEIG_WINDOW = 20

# Composite signal threshold percentile.
COMPOSITE_HIGH_PCT = 0.90
COMPOSITE_LOW_PCT = 0.10  # logged for use by Step 3 regime exit

# Warmup period (calendar days) of price data to download before
# START_FRIDAY so RSI / MA / high computations have full lookback on
# the first breadth date.
PRICE_WARMUP_CALENDAR_DAYS = 180


# ---------------------------------------------------------------------------
# Helper functions (importable for tests)
# ---------------------------------------------------------------------------


# yfinance exchange suffixes that should be PRESERVED, not converted to dash.
# These are populated upstream by fetch_constituents._resolve_yf_symbol for
# non-US ETFs (e.g. Tokyo 6592 -> 6592.T, London HSBA -> HSBA.L, Paris BNP
# -> BNP.PA). Converting these dots to dashes would break the symbol.
_YF_EXCHANGE_SUFFIXES = {
    "L", "DE", "F", "PA", "MI", "AS", "MC", "SW", "BR", "ST", "HE", "CO",
    "OL", "LS", "VI", "WA", "PR", "HM", "AT", "IR", "T", "HK", "NS", "BO",
    "KS", "TW", "SS", "SZ", "SI", "AX", "JO", "SA", "MX",
}


def normalise_for_yfinance(ticker: str) -> str:
    """Convert iShares dot-separated share classes (e.g. BRK.B) to yfinance's
    dash convention (BRK-B). Preserves dots that introduce a recognised
    exchange suffix (e.g. 6592.T, HSBA.L, BNP.PA) used by non-US ETFs.

    Logic: if the substring after the LAST dot matches one of the known
    yfinance exchange suffixes (Tokyo .T, London .L, Paris .PA, etc.), leave
    the ticker untouched. Otherwise (e.g. BRK.B share class) convert dot to
    dash. Tickers without dots are returned as-is.
    """
    if ticker in YF_TICKER_OVERRIDES:
        return YF_TICKER_OVERRIDES[ticker]
    if "." not in ticker:
        return ticker
    base, _, suffix = ticker.rpartition(".")
    if suffix in _YF_EXCHANGE_SUFFIXES:
        clean_base = base.rstrip(".")
        # Spanish .D entitlement marker on the local root (e.g. REP.D.MC → REP.MC)
        if suffix == "MC" and clean_base.endswith(".D"):
            clean_base = clean_base[:-2]
        # NSE: .RE rights row maps to the ordinary listing root; dots in
        # compound roots like BAJAJ.AUTO must become dashes (BAJAJ-AUTO.NS)
        if suffix == "NS":
            if clean_base.endswith(".RE"):
                clean_base = clean_base[:-3]
            clean_base = clean_base.replace(".", "-")
        return f"{clean_base}.{suffix}" if clean_base else ticker
    return ticker.replace(".", "-")  # share class — convert


def compute_rsi(prices: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder-smoothed RSI across multiple price series simultaneously.

    Implemented with EMA of gains / losses, alpha = 1/period (Wilder).
    Vectorised across columns; the result has NaN until `period` rows are
    available per column.
    """
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # Phase 10.2: when avg_loss = 0 the rs becomes NaN above, so rsi
    # would silently become NaN. Mathematically, a stock with no losses
    # has RSI = 100 (perfectly overbought), and a fully flat series has
    # RSI = 50 (neither overbought nor oversold). Previously these
    # tickers were dropped from the breadth count entirely, biasing the
    # breadth signal away from genuine momentum leaders.
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return rsi


def zweig_trigger(
    ma_breadth: pd.Series,
    window: int = ZWEIG_WINDOW,
    low: float = ZWEIG_LOW,
    high: float = ZWEIG_HIGH,
) -> pd.Series:
    """Zweig-style breadth thrust: ma_breadth >= high today AND any value in
    the prior `window` trading days was below `low`.

    "Prior" means strictly before today (shift(1)) so the trigger respects
    no-look-ahead.
    """
    prior_min = ma_breadth.shift(1).rolling(window, min_periods=1).min()
    return (ma_breadth >= high) & (prior_min < low)


def expanding_zscore(s: pd.Series, min_periods: int = Z_SCORE_MIN_PERIODS) -> pd.Series:
    """z-score where the (mean, std) used at time T are computed on data
    strictly before T — guarantees no look-ahead.
    """
    prior = s.shift(1)
    mean_t = prior.expanding(min_periods=min_periods).mean()
    std_t = prior.expanding(min_periods=min_periods).std()
    z = (s - mean_t) / std_t
    return z.replace([np.inf, -np.inf], np.nan)


def expanding_percentile(
    s: pd.Series,
    q: float,
    min_periods: int = PCT_MIN_PERIODS,
) -> pd.Series:
    """`q` quantile of s's history strictly prior to each date."""
    return s.shift(1).expanding(min_periods=min_periods).quantile(q)


# ---- Era barriers (WS15 adoption, 2026-08-13) -----------------------------
#
# A reused ticker can leave one COLUMN holding two different securities'
# bars: the roster-era security (recovered from Norgate) before the barrier,
# and the unrelated later occupant of the ticker after it. Indicator windows
# must never span the boundary — a 50-day average mixing Facebook's 2022
# closes with a 2025 ETF's is not an indicator of anything. Splitting at the
# barrier also exactly preserves the previous treatment of the later era,
# which warmed up as a fresh listing when the column held nothing else.
#
# Ticker-level facts, not panel-level: FB's barrier applies in every panel
# that ever held FB. On a column that has no pre-barrier bars (the sector
# panels until their own fills are adopted) the split is a no-op.
# Dates are the FIRST BAR of the LATER security, verified against Norgate
# security_name in WS15 (reviews/ws15_gate.json).
ERA_BARRIERS = {
    "FB":   "2025-06-26",   # Meta era (from META) | ProShares ETF took FB
    "PCLN": "2025-10-16",   # Priceline era (from BKNG) | Pictet ETF
    "FOXA": "2019-03-12",   # 21st Century Fox A (TFCFA-201903) | Fox Corp A
    "FOX":  "2019-03-13",   # 21st Century Fox B (TFCF-201903) | Fox Corp B
    # WS16 (2026-08-13):
    "LB":   "2024-06-28",   # L Brands era (from BBWI) | LandBridge took LB
    "CHK":  "2021-02-10",   # old Chesapeake (CHKAQ-202102) | relisted
                            #   Chesapeake, whose lineage lives under EXE
    "OPI":  "2026-06-22",   # old Office Properties (OPITQ-202606) | the
                            #   post-restructuring OPI line
    "ARNC": "2020-04-01",   # old Arconic (lineage under HWM) | the new
                            #   Arconic Corp spun out of Howmet
}

# Roster tickers whose yfinance symbol is not derivable by the share-class
# dot rule. iShares prints Brown-Forman Class B as "BFB" — no separator —
# so normalise_for_yfinance passed it through unchanged, it resolved at no
# vendor, and a live mega-cap staple sat unpriced for the panel's entire
# history (WS16 finding, IUCS).
YF_TICKER_OVERRIDES = {
    "BFB": "BF-B",
}


def per_ticker_apply(prices: pd.DataFrame, fn,
                     barriers: dict[str, str] | None = None) -> pd.DataFrame:
    """Apply `fn` to each column on its own traded sessions (NaNs dropped),
    then reindex the result back to the shared panel index. Columns named in
    ``barriers`` are computed per security era — `fn` runs separately on the
    bars before and from the barrier date, so no window spans two
    securities.

    Multi-exchange panels (Europe sectors) contain single-day NaNs wherever
    one venue was shut while another traded (May Day, the three UK bank
    holidays, Boxing Day, Ferragosto, ...). A plain
    rolling(window, min_periods=window) on such a panel treats every such
    holiday as missing data and invalidates the ticker's indicator for the
    next `window` rows; compounded across venues this erased roughly 40% of
    European ma_breadth coverage in recurring annual blocks (April-July,
    late-December-February, September-October).

    Computing on the ticker's own sessions makes each window "the last N
    traded closes". Wherever the plain rolling window happened to be
    NaN-free, both methods use the same N closes, so values are identical —
    the fix strictly extends coverage without moving existing values.
    """
    barriers = barriers or {}
    out = {}
    for c in prices.columns:
        s = prices[c].dropna()
        if s.empty:
            out[c] = pd.Series(np.nan, index=prices.index)
        elif c in barriers:
            cut = pd.Timestamp(barriers[c])
            parts = [p for p in (s.loc[: cut - pd.Timedelta(days=1)],
                                 s.loc[cut:]) if not p.empty]
            out[c] = pd.concat([fn(p) for p in parts]).reindex(prices.index)
        else:
            out[c] = fn(s).reindex(prices.index)
    return pd.DataFrame(out, index=prices.index)[list(prices.columns)]


def _display_path(path: Path) -> str:
    """Return ``path`` relative to PROJECT_ROOT for display, falling back
    to the absolute path when ``path`` is outside the project tree (e.g.,
    a tmp_path under test, or a CI workdir on a different mount)."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def active_roster_at(snapshot_dates: list[str], snapshot_map: dict, d: str) -> list[str]:
    """Return the constituent roster active on date `d` (ISO YYYY-MM-DD).

    Uses bisect on the sorted snapshot_dates list so this is O(log N) per
    call rather than O(N). The active roster on day d is the snapshot whose
    target Friday is the rightmost one <= d.

    Defensive dedup: parse_holdings dedupes upstream (Phase 13), but a
    malformed legacy snapshot or third-party constituent file could still
    contain duplicates. dict.fromkeys preserves order, is O(n), and is a
    no-op for already-clean snapshots.
    """
    idx = bisect_right(snapshot_dates, d) - 1
    if idx < 0:
        return []
    return list(dict.fromkeys(snapshot_map[snapshot_dates[idx]]["tickers"]))


# ---------------------------------------------------------------------------
# Price download with cache
# ---------------------------------------------------------------------------

# ---- Vendor step-defect guard (WS15, 2026-08-13) --------------------------
#
# Around its 2026-08-11 two-for-one split, yfinance served MNST with the
# split factor UNAPPLIED: pre-split bars unhalved beside post-split bars —
# byte-identical output under auto_adjust=True and False — while the
# vendor's OWN split calendar carried the split. Ingested, that column
# fabricates a −49.6% day and reads "below trend" for ~50 sessions of MA
# breadth (~200 sessions of Strategy A's 200-day panel). The coverage
# floors count names, not levels, so nothing else would have objected.
#
# The guard inspects each freshly-downloaded column for a one-day move of
# split magnitude, then asks the vendor's own split calendar whether a
# split sits within a few sessions at a matching ratio. A match means the
# series is mis-adjusted: that ticker REVERTS to the prior cached column
# and the refresh says so loudly. A genuine crash has no matching split
# and passes untouched. The referee fails OPEN (accept, with a warning):
# a frozen column is also wrong, so only a CONFIRMED vendor artefact
# justifies refusing fresh data.
VENDOR_STEP_LOG_RETURN = 0.20      # |ln r| >= 0.20 — a 5:4 split or larger
VENDOR_STEP_MATCH_TOL = 0.10      # |ln step| within this of |ln split ratio|
VENDOR_STEP_WINDOW_SESSIONS = 5
# Only the TAIL of the fresh series is examined. A mis-adjustment manifests
# at the split date, which on any sane refresh cadence is within the last
# few sessions; historical crash days (PTON, ZM and friends carry many
# split-sized moves) are baked into prior and fresh alike and are not an
# ingestion hazard. Scanning them all burned one split-calendar lookup per
# name per refresh and rate-limited into noise. 40 sessions ≈ two months
# of margin for a machine left off.
VENDOR_STEP_RECENT_SESSIONS = 40


def _splits_for(ticker: str):
    """The vendor's own split calendar for ``ticker``; None when it cannot
    be fetched. Isolated so tests can stub it and so the network call is
    made only for tickers that actually show a split-sized step."""
    try:
        s = yf.Ticker(ticker).splits
        return s if s is not None and len(s) else None
    except Exception:
        return None


def _vendor_step_defect(fresh: pd.Series, ticker: str) -> str | None:
    """Reason string when ``fresh`` carries a split-sized one-day step that
    the vendor's own split calendar attributes to a split — i.e. the series
    was served with the split unapplied — else None."""
    s = fresh.dropna()
    if len(s) < 2:
        return None
    logret = np.log(s / s.shift(1)).dropna()
    recent = logret.iloc[-VENDOR_STEP_RECENT_SESSIONS:]
    steps = recent[recent.abs() >= VENDOR_STEP_LOG_RETURN]
    if steps.empty:
        return None
    splits = _splits_for(ticker)
    if splits is None:
        print(f"  WARN {ticker}: {len(steps)} split-sized move(s), largest "
              f"{steps.abs().max():.2f} log-return, and the split calendar "
              f"is unavailable — accepting the fresh series unverified.",
              flush=True)
        return None
    events = []
    for d, ratio in splits.items():
        ts = pd.Timestamp(d)
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        if float(ratio) > 0:
            events.append((ts.normalize(), float(ratio)))
    for d, lr in steps.items():
        pos = s.index.get_loc(d)
        lo = s.index[max(0, pos - VENDOR_STEP_WINDOW_SESSIONS)]
        hi = s.index[min(len(s) - 1, pos + VENDOR_STEP_WINDOW_SESSIONS)]
        for sd, ratio in events:
            if lo <= sd <= hi and (abs(abs(lr) - abs(np.log(ratio)))
                                   <= VENDOR_STEP_MATCH_TOL):
                return (f"{d.date()} move of {lr:+.2f} log-return matches "
                        f"the {ratio:g}-for-1 split of {sd.date()} — the "
                        f"vendor served the split unapplied")
    return None


def _revert_vendor_step_defects(close: pd.DataFrame, prior: pd.DataFrame,
                                tickers: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Revert any confirmed mis-adjusted column to its prior cached values.

    Pure given ``_splits_for`` (stubbed in tests). Only tickers that HAVE a
    usable prior column can revert — with nothing to fall back on, the
    fresh series is accepted and the defect merely reported."""
    reverted = []
    for t in tickers:
        if t not in close.columns or close[t].dropna().empty:
            continue
        has_prior = t in prior.columns and not prior[t].dropna().empty
        reason = _vendor_step_defect(close[t], t)
        if reason and has_prior:
            close = close.reindex(close.index.union(prior.index))
            close[t] = prior[t].reindex(close.index)
            reverted.append(t)
            print(f"  REFUSED {t}: {reason}. The prior cached column is "
                  f"kept; re-run once the vendor repairs the series.",
                  flush=True)
        elif reason:
            print(f"  WARN {t}: {reason}, and there is no prior column to "
                  f"fall back on — fresh series accepted.", flush=True)
    return close, reverted


def last_completed_session_on(cal, now_utc: datetime,
                              horizon_days: int = 14) -> pd.Timestamp | None:
    """Most recent session on `cal` whose CLOSE has already passed.

    Deliberately not "today", and deliberately not "the last row the vendor
    returned". Both admit a PARTIAL BAR: yfinance serves an intraday quote for
    a session still in progress, and a breadth reading built on one would move
    after it was published — the same defect as rebalancing on an unfinished
    bar, which the rebalance calendar already refuses to do.

    Venue-aware because the answer differs by venue and the difference is not
    cosmetic: on a US-holiday Friday, Xetra has closed for the day and NYSE
    never opened, so a single NYSE-derived cap would either truncate the
    European funds by a session or admit a partial US one.

    Returns None when the calendar yields nothing in the horizon, which the
    caller treats as "keep the previous bound" rather than as an error.
    """
    now = pd.Timestamp(now_utc)
    if now.tz is None:
        now = now.tz_localize("UTC")
    end = now.tz_convert("UTC").normalize() + pd.Timedelta(days=1)
    sched = cal.schedule(start_date=end - pd.Timedelta(days=horizon_days),
                         end_date=end)
    if sched.empty or "market_close" not in sched.columns:
        return None
    closed = sched[sched["market_close"] <= now]
    if closed.empty:
        return None
    last = pd.Timestamp(closed.index[-1])
    if last.tz is not None:
        last = last.tz_convert("UTC").tz_localize(None)
    return last.normalize()


def download_prices(
    tickers: list[str],
    start: str,
    end: str,
    cache_path: Path = PRICES_CACHE,
    force: bool = False,
    deadline_s: float | None = None,
) -> pd.DataFrame:
    """Download adjusted-close history. Cache to parquet so reruns are free.

    Cache hit policy: reuse cache when it covers the requested date range
    and all requested tickers. Any change to either invalidates and re-pulls.
    """
    requested = set(tickers)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if not force and cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
            covers_dates = (
                cached.index.min() <= start_ts and cached.index.max() >= end_ts
            )
            covers_tickers = requested.issubset(set(cached.columns))
            if covers_dates and covers_tickers:
                print(f"  Using cached prices: {cache_path.name}", flush=True)
                return cached[list(tickers)].loc[start_ts:end_ts]
        except Exception as e:
            print(f"  Cache read failed ({e}); re-downloading.", flush=True)

    yf_syms = [normalise_for_yfinance(t) for t in tickers]
    print(f"  Downloading {len(yf_syms)} tickers from yfinance "
          f"({start} -> {end}) ...", flush=True)
    # Deadline, not a circuit. yf.download is one opaque call over the whole
    # universe — there is no per-item boundary to time from out here, so the
    # only available instrument is a wall clock. Placed AFTER the cache-hit
    # return above, so a run whose prices are entirely cached still succeeds
    # with the vendor unreachable; that is the same rule EndpointCircuit
    # follows in declining to pre-probe.
    #
    # This has not yet been the failure. On 2026-08-14 the DNS outage cost
    # compute_breadth 91s on a 373-ticker universe — yfinance's own timeouts
    # bounded it, and the n_with_any_data == 0 guard below would have caught
    # the total case. The four hours went to fetch_constituents. The deadline
    # is here because the download is the one step in this file whose runtime
    # is set by someone else's infrastructure, and an unbounded call in a
    # Friday-morning pipeline is a schedule risk whether or not it has fired.
    raw = run_with_deadline(
        lambda: yf.download(
            yf_syms,
            start=start,
            end=end,
            auto_adjust=True,
            threads=True,
            progress=False,
            group_by="column",
        ),
        seconds=(deadline_s if deadline_s is not None
                 else DEFAULT_DOWNLOAD_DEADLINE_S),
        label=f"yfinance download of {len(yf_syms)} tickers",
    )
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            raise RuntimeError("yfinance multi-index download lacks 'Close'")
        close = raw["Close"].copy()
    else:
        # Single ticker — DataFrame has one level, with OHLCV columns.
        close = raw[["Close"]].copy()
        close.columns = [yf_syms[0]]

    # Reverse the dot->dash normalisation so column names match input tickers.
    rev_map = {normalise_for_yfinance(t): t for t in tickers}
    close = close.rename(columns=rev_map)

    # Ensure all requested tickers exist as columns (NaN if yfinance returned nothing).
    for t in tickers:
        if t not in close.columns:
            close[t] = np.nan
    close = close[list(tickers)]
    close.index = pd.to_datetime(close.index).tz_localize(None)

    # PRESERVE CELLS YFINANCE CANNOT SERVE. The frame above is built purely
    # from the download, so anything the vendor no longer serves would be
    # silently deleted from the cache. That failure has now occurred at BOTH
    # granularities: column-level — a delisted name comes back all-NaN, and
    # the first Norgate backfill (25 of CNDX's 27 empty columns) was wiped by
    # the next run; and cell-level — a REUSED ticker comes back with only the
    # new occupant's bars, so a column-level keep judged FB "served" on the
    # strength of a 2025 ETF's prices and would have deleted Facebook's
    # 2018-2022 fill (WS15 adoption). The merge is therefore per CELL,
    # date-aligned: a fresh close always wins where it exists, and prior
    # values fill only the dates the download left NaN. A live ticker's
    # cached values are never preferred over a fresh close, so this cannot
    # freeze stale prices — the failure the staleness guards exist to catch.
    if not force and cache_path.exists():
        try:
            prior = pd.read_parquet(cache_path)
        except Exception:
            prior = None
        if prior is not None:
            # WS15 guard first: a mis-adjusted vendor series must not reach
            # the cache; a confirmed defect reverts to the prior column.
            close, _reverted = _revert_vendor_step_defects(
                close, prior, list(tickers))
            if _reverted:
                print(f"  Vendor step-defect guard reverted "
                      f"{len(_reverted)} column(s): {_reverted}", flush=True)
            preservable = [t for t in tickers
                           if t in prior.columns and prior[t].notna().any()]
            if preservable:
                close = close.reindex(close.index.union(prior.index))
            merged: dict[str, int] = {}
            for t in preservable:
                pcol = prior[t].reindex(close.index)
                fill_mask = close[t].isna() & pcol.notna()
                if fill_mask.any():
                    close.loc[fill_mask, t] = pcol[fill_mask]
                    merged[t] = int(fill_mask.sum())
            if merged:
                whole = [t for t in merged if close[t].notna().sum() == merged[t]]
                print(f"  Preserved {sum(merged.values())} externally-sourced "
                      f"cell(s) across {len(merged)} column(s) the download "
                      f"could not serve"
                      + (f"; {len(whole)} column(s) wholly from cache: "
                         f"{whole[:8]}" if whole else ""), flush=True)
            close = close.sort_index()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    close.to_parquet(cache_path)
    return close


# ---------------------------------------------------------------------------
# Main breadth pipeline
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--etf", default=DEFAULT_ETF,
        help=f"ETF symbol to compute breadth for. Default: {DEFAULT_ETF}",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    paths = paths_for(args.etf)
    constituents_path = paths["constituents"]
    out_path = paths["out"]
    prices_cache = paths["prices_cache"]

    print(f"Loading constituents JSON ({constituents_path.name}) ...", flush=True)
    consts = json.loads(constituents_path.read_text(encoding="utf-8"))
    snapshot_dates = sorted(consts["snapshots"].keys())
    snapshot_map = consts["snapshots"]
    universe = sorted({t for snap in snapshot_map.values() for t in snap["tickers"]})
    print(f"  ETF: {consts['etf']}")
    print(f"  Snapshots: {len(snapshot_dates)}")
    print(f"  Unique tickers across history: {len(universe)}")

    start_friday = pd.Timestamp(consts["start_friday"])
    end_friday = pd.Timestamp(consts["end_friday"])

    # Resolve the venue calendar HERE rather than at the schedule call below,
    # because the download bound now depends on it too.
    try:
        cal_name = get_etf(consts["etf"]).get("trading_calendar", "NYSE")
    except KeyError:
        cal_name = "NYSE"  # synthetic / test ETFs not present in the registry
    cal = mcal.get_calendar(cal_name)

    # WHY THE PANEL NO LONGER STOPS AT THE ROSTER'S LAST FRIDAY.
    #
    # It used to end at `end_friday`, the last PUBLISHED roster Friday. That is
    # a bound on when the constituent LIST was last refreshed, and it was being
    # used as a bound on how far breadth could be computed. Those are different
    # questions, and conflating them cost four sessions every Friday morning:
    # on Fri 14 Aug 2026 the newest roster was 7 Aug, so the panel ended 7 Aug
    # while the decision that morning reads Thursday 13 Aug. The refresh was
    # re-timed to Friday mornings precisely so it could feed a Friday fill, and
    # this bound made that impossible — the wrapper's anchor guard could not
    # pass, because the data it demanded was never computed.
    #
    # The roster being a week old is not a defect and never was. Rosters
    # publish weekly, so EVERY mid-week day already resolves against the most
    # recent snapshot <= T (see active_roster_at). Thursday 13 Aug is an
    # ordinary mid-week day under the 7 Aug roster, exactly as Wednesday 12 Aug
    # is.
    #
    # This is therefore value-preserving, not a new approximation: next week's
    # run computes 13 Aug against the 7 Aug roster too, because 14 Aug is not
    # <= 13 Aug. Extending the tail produces the identical numbers, earlier.
    # tools/verify_tail_extension.py asserts that by exact equality.
    #
    # Both bounds take a max() against the old value so this can only ever
    # lengthen the window. A change that could shorten it would silently drop
    # breadth days, which is the failure this is fixing.
    panel_end = last_completed_session_on(cal, datetime.now(timezone.utc))
    schedule_end = max(end_friday, panel_end) if panel_end is not None else end_friday

    dl_start = (start_friday - pd.Timedelta(days=PRICE_WARMUP_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    dl_end = max(end_friday + pd.Timedelta(days=5),
                 schedule_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    print("Downloading prices ...", flush=True)
    prices = download_prices(universe, dl_start, dl_end, cache_path=prices_cache)
    n_with_any_data = int((prices.notna().any(axis=0)).sum())
    print(f"  Prices shape: {prices.shape}, tickers with any data: "
          f"{n_with_any_data}/{len(universe)}")

    # Stop here when the vendor returned nothing at all.
    #
    # Without this the run continues into an empty breadth loop and dies at
    # `df["date"]` with KeyError: 'date' — an error about a missing column,
    # thirty lines from the actual problem and naming none of it. That
    # message has now misdirected diagnosis twice: once on a DNS failure
    # (2026-08-08) and once on a yfinance rate limit that took out EXV5-EXV8
    # in one refresh. The cause is always the same and is already visible
    # right here, so say it here.
    #
    # This raises rather than writing a degenerate panel: an empty breadth
    # series would overwrite a good committed one with a file that is
    # well-formed and asserts nothing. The previous panel is strictly better
    # than anything this run can produce, so the run must fail and leave it.
    if n_with_any_data == 0:
        raise RuntimeError(
            f"{args.etf}: the price vendor returned no data for any of "
            f"{len(universe)} constituents, so no breadth can be computed. "
            f"This is a fetch failure, not a data condition — the usual "
            f"causes are a yfinance rate limit (YFRateLimitError above, "
            f"most likely when many ETFs are refreshed back to back) or a "
            f"DNS/network outage. The existing data/breadth_"
            f"{args.etf.lower()}"
            f".json is untouched; re-run this ETF once the vendor recovers."
        )

    # Pre-compute per-ticker indicators on each ticker's own traded sessions
    # (see per_ticker_apply for why the union date grid must not be used).
    print("Computing per-ticker indicators ...", flush=True)
    barriers = {t: d for t, d in ERA_BARRIERS.items() if t in prices.columns}
    if barriers:
        print(f"  Era barriers active on {sorted(barriers)} — indicators "
              f"computed per security era", flush=True)
    rsi = per_ticker_apply(
        prices, lambda s: compute_rsi(s.to_frame("_c"), RSI_PERIOD)["_c"],
        barriers)
    ma50 = per_ticker_apply(
        prices, lambda s: s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean(),
        barriers)
    rolling_high = per_ticker_apply(
        prices, lambda s: s.rolling(HIGH_PERIOD, min_periods=HIGH_PERIOD).max(),
        barriers)
    above_ma = (prices > ma50) & ma50.notna()
    at_high = (prices >= rolling_high) & rolling_high.notna()
    rsi_overbought = (rsi > RSI_OVERBOUGHT) & rsi.notna()

    # Trading days in the breadth window, on the ETF's own calendar.
    # Registry entries may carry `trading_calendar` (a pandas_market_calendars
    # name); the default NYSE preserves behaviour for US-constituent funds.
    # The Europe sector funds use XETR so European trading days are sampled
    # and US-only holidays are not.
    schedule = cal.schedule(start_date=start_friday, end_date=schedule_end)
    trading_days = pd.DatetimeIndex(schedule.index.normalize().tz_localize(None))
    print(f"  Trading calendar: {cal_name}; trading days in window: "
          f"{len(trading_days)}")
    if panel_end is not None and schedule_end > end_friday:
        print(f"  Panel extends past the roster's last Friday "
              f"({end_friday.date()}) to the last completed {cal_name} "
              f"session ({schedule_end.date()}), on the carried-forward "
              f"roster.", flush=True)

    # Walk each trading day, build the breadth panel.
    print("Building breadth series ...", flush=True)
    rows = []
    for d in trading_days:
        d_str = d.strftime("%Y-%m-%d")
        roster = active_roster_at(snapshot_dates, snapshot_map, d_str)
        if not roster:
            continue
        # Skip dates the price panel does not cover.
        if d not in prices.index:
            continue
        roster_in_panel = [t for t in roster if t in prices.columns]
        # Per-component "has enough history" masks for this date.
        row_price = prices.loc[d, roster_in_panel]
        has_price = row_price.notna()
        n_with_price = int(has_price.sum())

        rsi_at = rsi.loc[d, roster_in_panel]
        rsi_valid = rsi_at.notna() & has_price
        ma_at = ma50.loc[d, roster_in_panel]
        ma_valid = ma_at.notna() & has_price
        high_at = rolling_high.loc[d, roster_in_panel]
        high_valid = high_at.notna() & has_price

        # Each component needs MIN_BREADTH_NAMES valid constituents, not
        # merely one — see the constant's rationale. Each is tested against
        # its own validity mask, because RSI, the 50-day average and the
        # 63-day high have different warmup lengths and so become available
        # at different times.
        if rsi_valid.sum() >= MIN_BREADTH_NAMES:
            rsi_b = float(rsi_overbought.loc[d, roster_in_panel][rsi_valid].sum() /
                          rsi_valid.sum())
        else:
            rsi_b = np.nan
        if ma_valid.sum() >= MIN_BREADTH_NAMES:
            ma_b = float(above_ma.loc[d, roster_in_panel][ma_valid].sum() /
                         ma_valid.sum())
        else:
            ma_b = np.nan
        if high_valid.sum() >= MIN_BREADTH_NAMES:
            high_b = float(at_high.loc[d, roster_in_panel][high_valid].sum() /
                           high_valid.sum())
        else:
            high_b = np.nan

        rows.append({
            "date": d_str,
            "n_constituents": len(roster),
            "n_with_price": n_with_price,
            "n_with_rsi": int(rsi_valid.sum()),
            "n_with_ma50": int(ma_valid.sum()),
            "n_with_high63": int(high_valid.sum()),
            "rsi_breadth": rsi_b,
            "ma_breadth": ma_b,
            "highs_breadth": high_b,
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    # Per-component z-scores (no look-ahead).
    print("Computing z-scores, thresholds, triggers ...", flush=True)
    df["rsi_breadth_z"] = expanding_zscore(df["rsi_breadth"])
    df["ma_breadth_z"] = expanding_zscore(df["ma_breadth"])
    df["highs_breadth_z"] = expanding_zscore(df["highs_breadth"])
    df["composite_z"] = df[["rsi_breadth_z", "ma_breadth_z", "highs_breadth_z"]].mean(
        axis=1, skipna=False
    )

    # Component triggers.
    df["rsi_p90"] = expanding_percentile(df["rsi_breadth"], COMPOSITE_HIGH_PCT)
    df["rsi_trigger"] = (df["rsi_breadth"] >= df["rsi_p90"]) & df["rsi_p90"].notna()
    df["highs_p90"] = expanding_percentile(df["highs_breadth"], COMPOSITE_HIGH_PCT)
    df["highs_trigger"] = (df["highs_breadth"] >= df["highs_p90"]) & df["highs_p90"].notna()
    df["ma_zweig_trigger"] = zweig_trigger(df["ma_breadth"])

    # Composite high threshold + crossover.
    df["composite_p90"] = expanding_percentile(df["composite_z"], COMPOSITE_HIGH_PCT)
    df["composite_p10"] = expanding_percentile(df["composite_z"], COMPOSITE_LOW_PCT)
    above_p90 = (df["composite_z"] >= df["composite_p90"]) & df["composite_p90"].notna()
    df["composite_above_p90"] = above_p90
    # shift(1) on a bool series introduces NaN at row 0; fillna(False)
    # then yields object dtype, and ~ on object dtype is brittle across
    # pandas versions. Use the explicit fill_value + astype(bool) form.
    prev_above_p90 = above_p90.shift(1, fill_value=False).astype(bool)
    df["composite_crosses_p90"] = above_p90 & ~prev_above_p90

    df["trigger_count"] = (
        df["rsi_trigger"].astype(int)
        + df["highs_trigger"].astype(int)
        + df["ma_zweig_trigger"].astype(int)
    )

    # Signal eligibility: enough history accumulated.
    history_position = np.arange(len(df))
    df["signal_eligible"] = history_position >= SIGNAL_ELIGIBLE_AFTER
    df["signal_fires"] = (
        df["composite_crosses_p90"] & (df["trigger_count"] >= 2) & df["signal_eligible"]
    )

    # ----- Output -----------------------------------------------------------
    missing_pct = 1.0 - (df["n_with_price"] / df["n_constituents"])

    # Coverage of the CURRENT roster on the latest date — see the floors.
    # n_with_ma50 rather than n_with_price: a name priced for three days
    # has a price but no 50-day average, so it contributes to neither
    # numerator nor denominator of ma_breadth and must not count as covered.
    _last_const = int(df["n_constituents"].iloc[-1]) if len(df) else 0
    _last_ma = int(df["n_with_ma50"].iloc[-1]) if len(df) else 0
    coverage_status, roster_coverage = coverage_verdict(_last_ma, _last_const)
    signal_rows = df[df["signal_fires"]]
    signals_list = []
    for ts, r in signal_rows.iterrows():
        triggered = []
        if bool(r["rsi_trigger"]):
            triggered.append("rsi")
        if bool(r["ma_zweig_trigger"]):
            triggered.append("ma_zweig")
        if bool(r["highs_trigger"]):
            triggered.append("highs")
        signals_list.append({
            "date": ts.strftime("%Y-%m-%d"),
            "composite_z": _safe_float(r["composite_z"]),
            "composite_p90": _safe_float(r["composite_p90"]),
            "rsi_breadth": _safe_float(r["rsi_breadth"]),
            "ma_breadth": _safe_float(r["ma_breadth"]),
            "highs_breadth": _safe_float(r["highs_breadth"]),
            "triggered_components": triggered,
            "n_constituents": int(r["n_constituents"]),
            "n_with_price": int(r["n_with_price"]),
        })

    def col_to_jsonlist(s: pd.Series, ndigits: int | None = 6) -> list:
        out = []
        for v in s.tolist():
            if isinstance(v, (bool, np.bool_)):
                out.append(int(v))
            elif v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                out.append(None)
            elif isinstance(v, float) and ndigits is not None:
                out.append(round(v, ndigits))
            else:
                out.append(v)
        return out

    no_data_names = sorted(
        t for t in universe
        if t not in prices.columns or not prices[t].notna().any()
    )

    payload = {
        "etf": consts["etf"],
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "constituents_source": _display_path(constituents_path),
        "trading_calendar": cal_name,
        "start_date": df.index[0].strftime("%Y-%m-%d"),
        "end_date": df.index[-1].strftime("%Y-%m-%d"),
        "n_trading_days": int(len(df)),
        "n_signals": int(df["signal_fires"].sum()),
        "first_eligible_signal_date": df.index[SIGNAL_ELIGIBLE_AFTER].strftime("%Y-%m-%d")
            if len(df) > SIGNAL_ELIGIBLE_AFTER else None,
        "config": {
            "rsi_period": RSI_PERIOD,
            "rsi_overbought_threshold": RSI_OVERBOUGHT,
            "ma_period": MA_PERIOD,
            "high_period": HIGH_PERIOD,
            "z_score_min_periods": Z_SCORE_MIN_PERIODS,
            "pct_min_periods": PCT_MIN_PERIODS,
            "signal_eligible_after": SIGNAL_ELIGIBLE_AFTER,
            "zweig_low": ZWEIG_LOW,
            "zweig_high": ZWEIG_HIGH,
            "zweig_window": ZWEIG_WINDOW,
            "composite_high_pct": COMPOSITE_HIGH_PCT,
            "composite_low_pct": COMPOSITE_LOW_PCT,
            "price_warmup_calendar_days": PRICE_WARMUP_CALENDAR_DAYS,
        },
        "data_quality": {
            "roster_coverage_latest": _safe_float(roster_coverage),
            "roster_coverage_warn_floor": MIN_ROSTER_COVERAGE_WARN,
            "roster_coverage_fail_floor": MIN_ROSTER_COVERAGE_FAIL,
            "universe_size": len(universe),
            "tickers_with_any_yf_data": n_with_any_data,
            "tickers_with_no_yf_data": len(universe) - n_with_any_data,
            "max_missing_constituent_pct": _safe_float(missing_pct.max()),
            "mean_missing_constituent_pct": _safe_float(missing_pct.mean()),
            "first_date_below_10pct_missing": (
                missing_pct[missing_pct < 0.10].index[0].strftime("%Y-%m-%d")
                if (missing_pct < 0.10).any() else None
            ),
            "note": (
                f"{len(no_data_names)} of {len(universe)} historical "
                "constituents have no yfinance price history (delisted or "
                "acquired names, plus identifiers yfinance does not resolve"
                + (" — e.g. " + ", ".join(no_data_names[:8])
                   if no_data_names else "")
                + "). They are dropped from both numerator and denominator, "
                "which biases breadth toward survivors until a delisted-price "
                "source is integrated. Daily n_with_price vs n_constituents "
                "lets Step 3 quarantine suspect periods."
            ),
        },
        "signals": signals_list,
        "series": {
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "n_constituents": df["n_constituents"].astype(int).tolist(),
            "n_with_price": df["n_with_price"].astype(int).tolist(),
            "n_with_rsi": df["n_with_rsi"].astype(int).tolist(),
            "n_with_ma50": df["n_with_ma50"].astype(int).tolist(),
            "n_with_high63": df["n_with_high63"].astype(int).tolist(),
            "rsi_breadth": col_to_jsonlist(df["rsi_breadth"]),
            "ma_breadth": col_to_jsonlist(df["ma_breadth"]),
            "highs_breadth": col_to_jsonlist(df["highs_breadth"]),
            "rsi_breadth_z": col_to_jsonlist(df["rsi_breadth_z"]),
            "ma_breadth_z": col_to_jsonlist(df["ma_breadth_z"]),
            "highs_breadth_z": col_to_jsonlist(df["highs_breadth_z"]),
            "composite_z": col_to_jsonlist(df["composite_z"]),
            "composite_p90": col_to_jsonlist(df["composite_p90"]),
            "composite_p10": col_to_jsonlist(df["composite_p10"]),
            "rsi_p90": col_to_jsonlist(df["rsi_p90"]),
            "highs_p90": col_to_jsonlist(df["highs_p90"]),
            "rsi_trigger": col_to_jsonlist(df["rsi_trigger"]),
            "ma_zweig_trigger": col_to_jsonlist(df["ma_zweig_trigger"]),
            "highs_trigger": col_to_jsonlist(df["highs_trigger"]),
            "trigger_count": df["trigger_count"].astype(int).tolist(),
            "composite_above_p90": col_to_jsonlist(df["composite_above_p90"]),
            "composite_crosses_p90": col_to_jsonlist(df["composite_crosses_p90"]),
            "signal_eligible": col_to_jsonlist(df["signal_eligible"]),
            "signal_fires": col_to_jsonlist(df["signal_fires"]),
        },
    }

    # ---- Coverage floor -------------------------------------------------
    # Checked BEFORE the write, so a thin panel cannot replace a good one.
    # Refusing to write leaves the previous breadth file in place, which is
    # the better artefact by definition — the same reasoning as the
    # build-time cache guards.
    cov_pct = roster_coverage * 100
    if coverage_status == "fail":
        if os.environ.get(COVERAGE_OVERRIDE_ENV):
            print(f"\n  {COVERAGE_OVERRIDE_ENV} set — writing a panel with "
                  f"only {cov_pct:.1f}% roster coverage", flush=True)
        else:
            print()
            print("!" * 72, file=sys.stderr)
            print(f"  THIN BREADTH — {_display_path(out_path)} NOT written",
                  file=sys.stderr)
            print(f"  Roster coverage {cov_pct:.1f}% ({_last_ma} of "
                  f"{_last_const} current constituents carry a "
                  f"{MA_PERIOD}-day average) is below the "
                  f"{MIN_ROSTER_COVERAGE_FAIL:.0%} floor.", file=sys.stderr)
            print(f"  Breadth is a ratio, so a partial download still "
                  f"returns a plausible number — it would just be computed "
                  f"on {_last_ma} names. The previous file was left in "
                  f"place.", file=sys.stderr)
            print(f"  Fix: re-run this fetch (the usual cause is a "
                  f"transient vendor failure); or set "
                  f"{COVERAGE_OVERRIDE_ENV}=1 to publish it anyway.",
                  file=sys.stderr)
            print("!" * 72, file=sys.stderr)
            return 2
    elif coverage_status == "warn":
        print(f"\n  WARN: roster coverage {cov_pct:.1f}% ({_last_ma} of "
              f"{_last_const}) is below the "
              f"{MIN_ROSTER_COVERAGE_WARN:.0%} floor — breadth is computed "
              f"on a thin sample. Written, but check the vendor.",
              flush=True)

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print()
    print(f"Wrote {_display_path(out_path)}")
    print(f"  Roster coverage: {cov_pct:.1f}% "
          f"({_last_ma}/{_last_const} carry a {MA_PERIOD}d average)")
    print(f"  Trading days   : {len(df)}")
    print(f"  Signals fired  : {int(df['signal_fires'].sum())}")
    print(f"  Max missing %  : {missing_pct.max() * 100:.1f}%")
    print(f"  Mean missing % : {missing_pct.mean() * 100:.1f}%")
    print(f"  Signal-eligible from : {payload['first_eligible_signal_date']}")
    if signals_list:
        print("  Signal dates   :")
        for s in signals_list:
            print(f"    {s['date']}  comp_z={s['composite_z']:.2f}  "
                  f"trig={s['triggered_components']}  "
                  f"n={s['n_with_price']}/{s['n_constituents']}")
    return 0


def _safe_float(x) -> float | None:
    """Convert numpy/pandas scalars to float, mapping NaN/inf to None."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


if __name__ == "__main__":
    sys.exit(main())
