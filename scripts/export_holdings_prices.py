"""Export last 1Y daily prices for every ETF that can appear in any of
the four deployed strategies' holdings tables. Output:
``data/holdings_prices_1y.json``.

Used by the Monitor tab's holdings click-to-expand mini-chart. Reads
from existing parquet caches (no network calls) so it is cheap to
re-run as part of the pipeline.

TWO HALVES, TWO SLOTS IN THE REFRESH (2026-08-15)
-------------------------------------------------
This script does two separable things, and until 2026-08-15 both ran at
step 6 of ``refresh_all.py`` — AFTER the strategy engines at step 3.

  * ``--refresh-caches-only`` repairs the per-ETF ``{ticker}_ohlc_cache.
    parquet`` files that Strategy A and D price their universes off. It
    reads the REGISTRY and the constituent caches from step 1, so it needs
    nothing the engines produce and now runs BEFORE them.
  * The default run exports ``holdings_prices_1y.json``. It resolves its
    ticker set partly from the deployed book — ``collect_book_symbols``
    reads the four sleeve JSONs the engines write at step 3 — so it must
    stay downstream of them. That is the circular dependency, and it lives
    entirely in this half.

WHAT WENT WRONG. On 2026-08-15 SOXX's cache was broken when Strategy A ran
at 16:17 local, and was repaired here at 16:36. Sleeve A published Sharpe
0.76 / CAGR 11.2% / total return +130% against committed values of 0.93 /
16.9% / +238%, dragging the deployed blend from 1.24 / +15.0% to 1.20 /
+13.0%. Re-running the engine afterwards, with nothing else changed,
restored it. Every downstream artefact had already inherited the corrupted
sleeve.

WHAT THE ORDERING FIX DOES AND DOES NOT DO. Strategy A and D do not read
these caches on the happy path: ``backtest.download_soxx_ohlc`` reuses a
cache only when it spans the whole requested window, and the window is
``[constituent_start - 10d, constituent_end + 5d]``, and no cache can reach
five days past the last session, so the end bound always fails and the
reuse branch never fires. The engines therefore re-fetch every run. Refreshing
the caches first is what gives that fetch something sound to FALL BACK ON
when it comes back degenerate — see the validation in
``download_soxx_ohlc`` and the panel guard in ``price_panel_guard.py``.
Ordering alone would not have stopped this; ordering plus those two does.

Sources tapped:
  * data/asset_class_prices_cache.parquet — Strategy B (14 ETFs)
  * data/thematic_prices_cache.parquet    — Strategy C (24 ETFs)
  * data/{ticker}_ohlc_cache.parquet      — Strategy A trade-as proxies
                                              (XLE, XLF, XLV, XLI, XLP,
                                              XLY, XLU, XLB, XLC, XLRE,
                                              IJR, SOXX, SPY, QQQ) and
                                              Strategy D Xetra UCITS
                                              (EXV1.DE, EXH1.DE, etc).

If a ticker is in MULTIPLE caches (e.g. SPY is in asset_class plus its
own ohlc cache), the asset_class file wins (later writes overwrite).

Output schema:
  {
    "computed_at_utc": "2026-05-26T...",
    "lookback_days": 252,
    "prices": {
      "XLE": {
        "dates":  ["2025-05-23", ..., "2026-05-22"],
        "prices": [120.45, ..., 145.32],     // raw close, 4 sig figs
        "change_pct": 0.207,                  // total 1Y return
      },
      ...
    }
  }
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "holdings_prices_1y.json"

LOOKBACK_DAYS = 252  # ~1 calendar year of trading days
MA_PERIODS = [50, 100, 200]  # standard trend-context moving averages

# Strategy A trade-as proxies (the deployed-execution tickers) +
# Strategy D Xetra UCITS that have their own OHLC caches.
INDIVIDUAL_OHLC_TICKERS = [
    # Strategy A SPDR sector proxies + broad-market direct holds
    "XLE", "XLF", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLC", "XLRE",
    "XLK",
    "SPY", "QQQ", "IJR", "SOXX",
    # Strategy D Xetra UCITS. EXH4.DE, not EXH3.DE: the Industrial Goods &
    # Services panel (registry key EXH3) trades as EXH4.DE — corrected
    # 2026-08-03, see the EXH3 entry in etf_registry.py.
    "EXV1.DE", "EXH1.DE", "EXV3.DE", "EXH4.DE", "EXH9.DE",
    # EM sleeve — overlay-only since Phase 29, no longer in the rotation
    # parquet, so it must be sourced explicitly (em_regime_context or yfinance).
    "EEM",
    # Reference / extras
    "INDA", "MCHI",
]

# Book-critical tickers whose ONLY on-disk source (the individual OHLC and
# multi-ETF price caches) is gitignored, so a fresh CI runner never has them.
# Prior to this, the weekly Actions run emitted only the ~38 tickers that its
# Strategy B/C rotation steps download live, silently dropping SOXX, the US
# sector proxies (XLE/XLU/XLRE/XLB), the Xetra lines and EEM — 55-60% of NAV.
# For any of these still missing after the cache sweep, fetch from yfinance so
# the exported universe is complete regardless of which caches exist locally.
#
# 2026-07-18: the static list alone was NOT complete — the daily Actions run
# (which refreshes no caches before exporting) emitted only these 23 names,
# silently dropping every Strategy B rotation holding beyond SPY/IJR/QQQ and
# the whole thematic sleeve, and it committed that 23-ticker panel OVER the
# weekly run's full one. The candidate set is therefore now also derived from
# the DEPLOYED BOOK itself (each sleeve's latest trade_history holdings,
# resolved to their trading symbols), so any runner exports the complete book
# via the yfinance fallback regardless of which caches exist. See
# ``collect_book_symbols``.
NETWORK_FALLBACK_TICKERS = sorted(set(INDIVIDUAL_OHLC_TICKERS))

# Maximum age of a cache-sourced series before the yfinance fallback re-fetches
# it anyway. em_regime_context.parquet (the only committed EEM source after
# Phase 29) froze at 2026-07-06 and every panel vintage shipped a 9-session-old
# EEM row — 10% of NAV — under a current as-of stamp. Seven calendar days spans
# any weekend + holiday cluster without tolerating a genuinely stalled feed.
MAX_CACHE_AGE_DAYS = 7

# Exit code for an unrepaired regression: at least one ticker's last bar ended
# EARLIER than the panel already on disk and the re-fetch could not restore it.
# Deliberately distinct from 1 so the CI workflows can hard-fail a backwards
# panel while still soft-failing a transient vendor error, which must never
# block the live-track publish.
REGRESSION_EXIT_CODE = 2

# Sleeve holdings files that define the deployed book (same set the email,
# factsheet and live mark-to-market read).
SLEEVE_FILES = [
    "topk_robustness.json",        # A — US sectors
    "asset_class_rotation.json",   # B — asset class
    "thematic_rotation.json",      # C — thematic
    "europe_rotation.json",        # D — Europe sectors
]

# Xetra-listed Europe sleeve tickers (trade with a .DE suffix). Mirrors
# mark_to_market_live.EUROPE_TICKERS.
EUROPE_TICKERS = {"EXV1", "EXH1", "EXV3", "EXH3", "EXH9"}

# Fallback fetch start for a per-ETF OHLC cache with no constituent panel to
# take its window from. Comfortably before the earliest existing cache
# (2017-06-30 across every sleeve A and D proxy on 2026-08-15), so a repair
# never restores less history than the file it replaces.
DEFAULT_OHLC_START = "2015-01-01"

# Columns a per-ETF OHLC cache carries. The engines only read Close, but
# backtest.py's ATR path reads High/Low, so a repair that wrote Close alone
# would quietly strip a column set that nothing rebuilds.
OHLC_COLUMNS = ["Open", "High", "Low", "Close"]

# Exit code for a cache refresh that finished with at least one engine-facing
# series still unusable. Distinct from 1 (transient vendor error) and from
# REGRESSION_EXIT_CODE so refresh_all can say which of the three happened.
UNUSABLE_CACHE_EXIT_CODE = 3


def resolve_book_symbol(etf: str) -> str:
    """Map a holdings ticker to the yfinance symbol its prices trade under.

    Same convention as ``mark_to_market_live._resolve_yf_symbol`` minus the
    FX handling (this exporter publishes native-currency closes; consumers
    that need USD conversion do it themselves): every ticker that has a
    registry trading proxy uses it, China A-shares (.SZ/.SS) pass through,
    and a Europe-sleeve key with no proxy recorded falls back to a .DE
    suffix.

    The registry lookup now precedes the Europe branch. Appending ".DE" to
    the key assumes the Xetra ticker equals the registry key, which is
    false for EXH3 — its panel is Industrial Goods & Services, traded as
    EXH4.DE, while EXH3.DE is a food & beverage fund. See the EXH3 entry
    in etf_registry.py and tests/test_europe_symbol_contract.py.
    """
    if etf.endswith((".SZ", ".SS")):
        return etf
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from etf_registry import ETF_REGISTRY
        proxy = (ETF_REGISTRY.get(etf) or {}).get("yfinance_trading_proxy")
        if proxy:
            return proxy
    except Exception:
        pass
    if etf in EUROPE_TICKERS:
        return f"{etf}.DE"
    return etf


def collect_book_symbols() -> set[str]:
    """Trading symbols for every CURRENT holding across the four sleeves,
    plus EEM (overlay-only since Phase 29). Missing sleeve files are
    skipped — the static list still provides the floor."""
    symbols: set[str] = {"EEM"}
    for fname in SLEEVE_FILES:
        path = DATA_DIR / fname
        if not path.exists():
            continue
        try:
            sleeve = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        trades = (sleeve.get("headline") or {}).get("trade_history") or []
        if not trades:
            continue
        for h in trades[-1].get("holdings", []):
            etf = h.get("etf")
            if etf:
                symbols.add(resolve_book_symbol(etf))
    return symbols


def load_close_series(ticker: str) -> pd.Series | None:
    """Return the FRESHEST Close series for this ticker across every known
    cache location, or None if no source has it.

    Freshest, not first-found. This function used to return the first cache
    that carried the ticker, in a fixed source order. That ordering is a
    silent data-integrity trap, because ``fetch_missing_from_yfinance``
    WRITES ``{ticker}_ohlc_cache.parquet`` (source 3) — so a one-off backfill
    permanently shadows a fresher committed source that sits later in the
    order, and the shadow never expires.

    EEM did exactly this. A backfill wrote data/eem_ohlc_cache.parquet
    terminating 2026-08-03; data/em_regime_context.parquet carried EEM
    current to 2026-08-07. First-found returned the 3 August series, so the
    published panel went BACKWARDS four sessions for the largest holding in
    the book (10.0% of NAV) while the header stamp still read 7 August, and
    the digest's 200-DMA proximity for EEM was computed on the stale bar.
    See reviews/ and tests/test_export_holdings_prices.py.

    Ties on last date are broken by series length, so the 200d MA gets the
    most history available.
    """
    candidates: list[tuple[pd.Timestamp, int, pd.Series]] = []

    def offer(ser: "pd.Series | None") -> None:
        if ser is None:
            return
        ser = ser.dropna()
        if ser.empty:
            return
        candidates.append((ser.index[-1], len(ser), ser))

    # 1. Asset-class multi-ETF parquet
    ac = DATA_DIR / "asset_class_prices_cache.parquet"
    if ac.exists():
        try:
            df = pd.read_parquet(ac)
            if ticker in df.columns:
                offer(df[ticker])
        except Exception:
            pass
    # 2. Thematic multi-ETF parquet
    tc = DATA_DIR / "thematic_prices_cache.parquet"
    if tc.exists():
        try:
            df = pd.read_parquet(tc)
            if ticker in df.columns:
                offer(df[ticker])
        except Exception:
            pass
    # 3. Individual ETF OHLC parquet — file naming is lowercase. Written by
    #    the yfinance backfill, so it can be older than a committed source.
    ohlc = DATA_DIR / f"{ticker.lower()}_ohlc_cache.parquet"
    if ohlc.exists():
        try:
            df = pd.read_parquet(ohlc)
            # OHLC dataframes carry Open / High / Low / Close columns
            if "Close" in df.columns:
                ser = df["Close"]
                if isinstance(ser, pd.DataFrame):
                    ser = ser.iloc[:, 0]
                offer(ser)
        except Exception:
            pass
    # 4. EM regime-context parquet — the only COMMITTED (non-gitignored)
    #    price source for EEM and SPY. Phase 29 removed EEM from the
    #    Strategy B rotation universe, so EEM left asset_class_prices_cache;
    #    the book still holds it (overlay-only), and this is where its close
    #    survives. Committed, so it is present on a fresh CI runner.
    em = DATA_DIR / "em_regime_context.parquet"
    if em.exists():
        try:
            df = pd.read_parquet(em)
            if ticker in df.columns:
                offer(df[ticker])
        except Exception:
            pass

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[-1][2]


def last_date(entry: dict | None) -> str | None:
    """Last bar date of an exported per-ticker record, or None."""
    if not entry or not entry.get("dates"):
        return None
    return entry["dates"][-1]


def find_regressions(new: dict[str, dict],
                     prev: dict[str, dict]) -> dict[str, tuple[str, str]]:
    """Tickers whose new last bar is EARLIER than the published one.

    Returns {ticker: (previous_last_date, new_last_date)}. A published price
    panel must never move backwards: markets do not un-print closes, so a
    regression is always a sourcing fault, never real data. This is the
    date-level counterpart to the coverage guard below, which only ever
    caught a ticker vanishing ENTIRELY — EEM stayed present and merely lost
    four sessions, so nothing fired.
    """
    out: dict[str, tuple[str, str]] = {}
    for tk, prev_entry in (prev or {}).items():
        p, n = last_date(prev_entry), last_date(new.get(tk))
        if p and n and n < p:
            out[tk] = (p, n)
    return out


def _round_sig(values: list[float], sig: int = 4) -> list[float]:
    """Round to ``sig`` significant figures so the JSON stays compact."""
    import math
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(None); continue
        if v == 0:
            out.append(0.0); continue
        digits = sig - int(math.floor(math.log10(abs(v)))) - 1
        out.append(round(v, max(0, digits)))
    return out


def universe_sources_present() -> bool:
    """True when the caches that DEFINE the requested universe are on disk.

    ``collect_all_tickers`` reads the asset-class and thematic rotation
    parquets, both gitignored. Where they are absent the requested set is a
    floor, not the universe, and no conclusion may be drawn from a ticker's
    absence from it. Used to gate the retirement branch in ``main``.
    """
    return ((DATA_DIR / "asset_class_prices_cache.parquet").exists()
            and (DATA_DIR / "thematic_prices_cache.parquet").exists())


def collect_all_tickers() -> set[str]:
    """Union of every ticker any of the four strategies' price caches
    can offer plus the individual-OHLC list."""
    tickers: set[str] = set(INDIVIDUAL_OHLC_TICKERS)
    ac = DATA_DIR / "asset_class_prices_cache.parquet"
    if ac.exists():
        try:
            tickers.update(pd.read_parquet(ac).columns)
        except Exception:
            pass
    tc = DATA_DIR / "thematic_prices_cache.parquet"
    if tc.exists():
        try:
            tickers.update(pd.read_parquet(tc).columns)
        except Exception:
            pass
    tickers.update(NETWORK_FALLBACK_TICKERS)
    tickers.update(collect_book_symbols())
    return tickers


def build_entry(close: "pd.Series | None") -> dict | None:
    """Turn a raw Close series into the exported per-ticker record (dates,
    prices, MA overlays and trend stats), or None if there is too little
    history. MAs are computed on the FULL series then sliced to the last
    LOOKBACK_DAYS, so the 200d MA is populated across the whole 1Y window
    whenever >=200 prior sessions exist; for young tickers the leading MA
    values are NaN and serialise to None (Plotly skips those points)."""
    if close is None or len(close) < 2:
        return None
    ma_series: dict[int, pd.Series] = {
        p: close.rolling(p, min_periods=p).mean() for p in MA_PERIODS
    }
    tail = close.iloc[-LOOKBACK_DAYS:]
    if len(tail) < 2:
        return None
    first = float(tail.iloc[0])
    last = float(tail.iloc[-1])
    change_pct = (last / first - 1.0) if first else None
    # Distance of last close above the 200d MA, as a decimal (0.05 = 5%).
    ma200_last = ma_series[200].iloc[-1] if not ma_series[200].empty else None
    vs_ma200 = None
    if (ma200_last is not None and not pd.isna(ma200_last)
            and ma200_last != 0):
        vs_ma200 = float(last / ma200_last - 1.0)

    def _ma_tail_arr(p: int) -> list:
        series_tail = ma_series[p].iloc[-LOOKBACK_DAYS:]
        return [
            round(float(v), max(0, 4 - int(__import__("math").floor(
                __import__("math").log10(abs(v)) if v != 0 else 0
            )) - 1)) if not pd.isna(v) else None
            for v in series_tail.values
        ]

    return {
        "dates": [d.strftime("%Y-%m-%d") for d in tail.index],
        "prices": _round_sig([float(v) for v in tail.values]),
        "ma50": _ma_tail_arr(50),
        "ma100": _ma_tail_arr(100),
        "ma200": _ma_tail_arr(200),
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "vs_ma200": round(vs_ma200, 4) if vs_ma200 is not None else None,
        "n_days": int(len(tail)),
    }


def fetch_missing_from_yfinance(tickers: list[str],
                                gaps_out: dict[str, list[str]] | None = None
                                ) -> dict[str, pd.Series]:
    """Last-resort fetch for book-critical tickers whose local caches are
    absent (the CI-runner case — those caches are gitignored). Downloads ~2
    calendar years of daily closes in one batched call so the 200d MA is
    populated, writes each back to its ``{ticker}_ohlc_cache.parquet`` so
    subsequent runs are cheap, and returns {ticker: Close series}. Any
    failure degrades gracefully to an empty mapping — the caller then simply
    reports the ticker as skipped rather than crashing the (soft-fail) step.

    ``gaps_out``, when supplied, is filled with {ticker: [ISO dates]} for every
    session the VENDOR ITSELF returned empty — the date is in the response's
    own index and at least one other ticker in the batch printed a close on it,
    but this line did not. That is the evidence ``reinstate_vendor_gaps`` needs
    to tell a vendor hole apart from a stale local source, and it is only
    visible BEFORE the ``dropna()`` below collapses a NaN tail into a shorter
    series. See the vendor-gap note on ``reinstate_vendor_gaps``.
    """
    if not tickers:
        return {}
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - env without yfinance
        print(f"  WARN: yfinance unavailable, cannot backfill {tickers}: {exc}")
        return {}
    # ~2y of calendar days comfortably covers 200 trading sessions + the 1Y
    # window. auto_adjust=True matches the convention used by the Strategy B/C
    # rotations and the EEM loader (adjusted closes).
    print(f"  Backfilling {len(tickers)} ticker(s) from yfinance "
          f"(yfinance {getattr(yf, '__version__', 'unknown')}): "
          f"{', '.join(tickers)}")
    out: dict[str, pd.Series] = {}
    try:
        raw = yf.download(tickers, period="2y", auto_adjust=True,
                          progress=False, threads=True, group_by="ticker")
    except Exception as exc:
        print(f"  WARN: yfinance batch download failed: {exc}")
        return {}
    if raw is None or len(raw) == 0:
        print("  WARN: yfinance returned an EMPTY frame for the whole batch")
        return {}
    print(f"  Vendor frame: {len(raw)} rows, "
          f"{str(pd.Timestamp(raw.index.min()).date())} .. "
          f"{str(pd.Timestamp(raw.index.max()).date())}")

    # How many of the batch printed a close on each date. A date where SOME
    # ticker traded is a real session at the vendor, so a NaN there is the
    # vendor withholding one line, not a market holiday.
    close_cols = [c for c in raw.columns
                  if (isinstance(c, tuple) and c[-1] == "Close") or c == "Close"]
    printed = raw[close_cols].notna().sum(axis=1) if close_cols else None

    for tk in tickers:
        try:
            if len(tickers) == 1:
                # Single-ticker downloads are not MultiIndex-keyed by ticker.
                ser = raw["Close"] if "Close" in raw.columns else None
            else:
                ser = raw[(tk, "Close")] if (tk, "Close") in raw.columns else None
            if ser is None:
                continue
            if isinstance(ser, pd.DataFrame):
                ser = ser.iloc[:, 0]
            if gaps_out is not None and printed is not None:
                # Recent tail only. A batch spans several venues, so every US
                # holiday is a date where the Xetra lines printed and the US
                # ones did not (and vice versa) — hundreds of blanks that are
                # simply closed markets. Withholding is a fresh-tail
                # behaviour; anything older is judged by interior_gaps, which
                # uses the venue's own calendar and cannot confuse the two.
                recent = ser.iloc[-VENDOR_GAP_LOOKBACK_ROWS:]
                blank = recent.isna() & (printed.reindex(recent.index) > 0)
                if bool(blank.any()):
                    gaps_out[tk] = [
                        str(pd.Timestamp(d).date())
                        for d in recent.index[blank]
                    ]
            ser = ser.dropna()
            if ser.empty:
                continue
            ser.index = pd.to_datetime(ser.index).tz_localize(None)
            out[tk] = ser
            # Persist as an OHLC-style cache so load_close_series finds it next
            # time and local runs stay network-free.
            #
            # Never overwrite a cache that already ends LATER than what the
            # vendor just returned. This is the write that manufactured the
            # 2026-08-04 stubs: a short response was persisted over good data
            # and then read back as authoritative on every later run. The
            # freshest-wins rule in load_close_series stops a stale cache
            # WINNING; this stops one being created in the first place.
            try:
                cache_path = DATA_DIR / f"{tk.lower()}_ohlc_cache.parquet"
                on_disk = None
                if cache_path.exists():
                    try:
                        prev = pd.read_parquet(cache_path)
                        if "Close" in prev.columns:
                            col = prev["Close"]
                            if isinstance(col, pd.DataFrame):
                                col = col.iloc[:, 0]
                            col = col.dropna()
                            on_disk = col if not col.empty else None
                    except Exception:
                        on_disk = None
                if on_disk is not None and on_disk.index[-1] > ser.index[-1]:
                    print(f"  REFUSED cache write for {tk}: fetched series ends "
                          f"{ser.index[-1].date()} but {cache_path.name} already "
                          f"ends {on_disk.index[-1].date()}")
                    out[tk] = on_disk
                    continue
                # ... and never overwrite one that already starts EARLIER.
                # The 2y fetch above is always fresh at the tail, so the end
                # rule cannot catch it; on 2026-08-13/14 it overwrote the five
                # sleeve-D Xetra caches' 2017 history, and the next cold
                # rebuild collapsed onto the surviving two years (blend Sharpe
                # +1.99). The fetched series still feeds the export — freshest
                # wins for the panel — only the FILE keeps its longer span.
                # Canonical rule: price_panel_guard.fetched_frame_is_worse,
                # applied at every OHLC cache write site.
                if on_disk is not None and on_disk.index[0] < ser.index[0]:
                    print(f"  REFUSED cache write for {tk}: fetched series starts "
                          f"{ser.index[0].date()} but {cache_path.name} already "
                          f"starts {on_disk.index[0].date()}")
                    continue
                # Keep Open/High/Low when the vendor served them. Writing
                # Close alone used to be harmless because this ran after the
                # engines and they re-fetched anyway; now that a repaired
                # cache is what a degenerate engine fetch falls back on,
                # stripping three columns would silently disarm backtest.py's
                # ATR path for that ticker.
                frame = {"Close": ser}
                for field_name in ("Open", "High", "Low"):
                    col = None
                    if len(tickers) == 1:
                        col = raw[field_name] if field_name in raw.columns else None
                    elif (tk, field_name) in raw.columns:
                        col = raw[(tk, field_name)]
                    if col is not None:
                        col = col.copy()
                        col.index = pd.to_datetime(col.index).tz_localize(None)
                        frame[field_name] = col.reindex(ser.index)
                pd.DataFrame(frame)[
                    [c for c in OHLC_COLUMNS if c in frame]
                ].to_parquet(cache_path)
            except Exception:
                pass
        except Exception:
            continue

    # What the vendor actually served, per line. Three nights of failures were
    # read as a stale SPY because the log said nothing about the fetch itself:
    # which lines came back short, and how short. Only the lines that trail the
    # frame are named — on a good night this prints one word.
    frame_last = str(pd.Timestamp(raw.index.max()).date())
    trailing = sorted((tk, str(s.index[-1].date()))
                      for tk, s in out.items()
                      if len(s) and str(s.index[-1].date()) < frame_last)
    missing = [tk for tk in tickers if tk not in out]
    print(f"  Fetched {len(out)}/{len(tickers)} line(s); frame ends {frame_last}"
          + (f"; behind it: {', '.join(f'{t} ({d})' for t, d in trailing)}"
             if trailing else "; all current with the frame"))
    if missing:
        print(f"  WARN: vendor returned NOTHING for: {', '.join(missing)}")
    return out


# --------------------------------------------------------------------------
# Vendor gaps — the nightly XETR / SZSE hole (2026-08-27)
#
# WHAT HAPPENS. For non-US venues yfinance serves the most recently COMPLETED
# session as NaN for roughly 12-20 hours after that session, having served it
# during and just after the session itself. Measured, not assumed: every 00:00
# and 06:00 UTC row in data/vendor_availability_log.jsonl sits one session
# behind the 12:00/18:00 rows that preceded it, on all five Xetra lines, on
# every weekday from 2026-08-18 to 2026-08-27 without exception. Confirmed
# directly at 15:44 UTC on 2026-08-27: EXV1.DE's 2026-08-26 close came back
# NaN in a batched frame where SPY printed normally, and the date was absent
# altogether from a single-ticker fetch.
#
# WHY IT BROKE THE NIGHTLY PUBLISH. fetch_missing_from_yfinance drops NaNs, so
# a withheld tail becomes a SHORTER series; its last bar then sits behind the
# published panel, find_regressions fires, and the run exits 2 — which the
# daily_live_track workflow turns into a hard failure that blocks live_track,
# the dashboard, the factsheet and the public page. The engine cron ('30 21 *
# * 1-5') sits inside the withholding window, and GitHub's cron delay pushes
# it deeper: the 26 Aug 21:30 slot did not fire until 00:55 on 27 Aug.
#
# WHY REINSTATING IS A REPAIR AND NOT A RELAXATION. The bar being restored is
# one THIS repo already published, for a session the vendor's own response
# still lists as real (peers printed on it), at a price the vendor served us
# hours earlier and which still agrees with everything around it. Nothing is
# invented: a date the previous panel does not carry can never be filled. And
# because the evidence required is the vendor returning a blank for a live
# session, a merely stale LOCAL source produces no gaps at all — so the 2026-
# 08-08 EEM shape (a frozen cache four sessions behind a good panel) still
# regresses, is still held back, and still exits 2. That distinction is the
# whole point, and tests/test_export_holdings_prices.py pins both directions.
# --------------------------------------------------------------------------

# Published prices are rounded to 4 significant figures by _round_sig, so the
# overlap comparison has to tolerate that rounding — and nothing looser. Any
# genuine change of adjustment vintage (a dividend going ex re-scales the whole
# auto_adjust=True history) moves closes by far more than 5e-4, so it fails the
# check and the splice is refused rather than silently mixing two vintages.
VENDOR_GAP_RTOL = 5e-4

# How many overlapping bars to compare before trusting a splice.
VENDOR_GAP_OVERLAP_BARS = 10

# How far back a withheld session is looked for in the vendor's response. The
# behaviour being caught lasts 12-20 hours, so two trading weeks is generous;
# the limit exists to keep ordinary market holidays out of the evidence.
VENDOR_GAP_LOOKBACK_ROWS = 10


def reinstate_vendor_gaps(ticker: str,
                          fetched: "pd.Series",
                          prev_entry: dict | None,
                          gap_dates: list[str] | None,
                          ) -> tuple["pd.Series", list[str], str | None]:
    """Put back the sessions the vendor withheld, from the published panel.

    Returns ``(series, reinstated_dates, refusal_reason)``. A date is only
    reinstated when ALL of the following hold, which is what keeps this a
    repair rather than a way of manufacturing data:

      * the vendor returned a blank for it while other lines in the same
        response printed a close (so the session is real at the vendor);
      * the previously published panel carries a price for that exact date;
      * the fetched and published series agree, within VENDOR_GAP_RTOL, on
        the bars they do share — otherwise the adjustment vintage has moved
        and splicing would join two different scales.
    """
    if fetched is None or not gap_dates or not prev_entry:
        return fetched, [], None
    prev_dates = prev_entry.get("dates") or []
    prev_prices = prev_entry.get("prices") or []
    if not prev_dates or len(prev_dates) != len(prev_prices):
        return fetched, [], None
    published = {d: p for d, p in zip(prev_dates, prev_prices) if p is not None}

    have = {str(pd.Timestamp(d).date()) for d in fetched.index}
    fillable = sorted(set(gap_dates) & set(published) - have)
    if not fillable:
        return fetched, [], None

    # Adjustment-vintage check on the shared bars.
    shared = [d for d in prev_dates if d in have and d in published]
    if len(shared) < 2:
        return fetched, [], "too little overlap to verify the price scale"
    by_date = {str(pd.Timestamp(d).date()): float(v)
               for d, v in fetched.items()}
    tail = shared[-VENDOR_GAP_OVERLAP_BARS:]
    for d in tail:
        a, b = by_date[d], float(published[d])
        if b == 0 or abs(a - b) / abs(b) > VENDOR_GAP_RTOL:
            return fetched, [], (
                f"published and fetched closes disagree on {d} "
                f"({b} vs {round(a, 6)}) — adjustment vintage changed, "
                f"refusing to splice")

    repaired = fetched.copy()
    for d in fillable:
        repaired.loc[pd.Timestamp(d)] = float(published[d])
    repaired = repaired.sort_index()
    return repaired, fillable, None


# Suffix -> trading calendar. Shenzhen has no separate pandas_market_calendars
# entry; it keeps the same session dates and holidays as Shanghai, so XSHG is
# the right proxy for a DATE-level completeness check (it is never used for
# prices). An unmapped suffix reports nothing rather than guessing.
VENUE_BY_SUFFIX = {".DE": "XETR", ".SZ": "XSHG", ".SS": "XSHG"}


@lru_cache(maxsize=8)
def _venue_calendar(name: str):
    """Cached calendar handle — building one per ticker costs more than the
    whole export."""
    import pandas_market_calendars as mcal
    return mcal.get_calendar(name)


def venue_calendar_for(ticker: str) -> str | None:
    """Trading calendar name for a panel ticker, or None if not known."""
    if ticker.endswith("-USD"):     # crypto trades every day; no calendar
        return None
    for suffix, cal in VENUE_BY_SUFFIX.items():
        if ticker.endswith(suffix):
            return cal
    return "NYSE" if ticker.isalpha() else None


def interior_gaps(entry: dict | None, ticker: str) -> list[str]:
    """Sessions this ticker's venue held that the series is missing, strictly
    INSIDE its own span.

    find_regressions compares last bars only, so a hole that is not at the tail
    passes it silently. That is not hypothetical: the run that published at
    15:30 UTC on 2026-08-27 went green while committing a panel with no
    2026-08-26 bar for any of the five Xetra lines, because the vendor had
    already restored 08-27 over the top of the hole it left at 08-26.

    Interior only, deliberately: the tail is where a legitimate publication lag
    lives (Europe routinely trails the US by a session), and flagging that would
    fire every night for a condition that is not a fault.
    """
    dates = (entry or {}).get("dates") or []
    if len(dates) < 3:
        return []
    cal_name = venue_calendar_for(ticker)
    if not cal_name:
        return []
    try:
        sched = _venue_calendar(cal_name).schedule(
            start_date=dates[0], end_date=dates[-1])
    except Exception:
        return []
    expected = {str(pd.Timestamp(d).date()) for d in sched.index}
    return sorted(expected - set(dates))


# --------------------------------------------------------------------------
# Cache-refresh half — runs BEFORE the strategy engines (refresh_all step 2b)
# --------------------------------------------------------------------------
def engine_ohlc_tickers() -> dict[str, str]:
    """``{trading symbol: registry key}`` for the sleeve A and D universes.

    These are the per-ETF OHLC caches the strategy engines price off, and
    this mapping is derived from ``etf_registry`` alone. That is the point:
    it reads nothing the engines write, so it can run ahead of them.
    Contrast ``collect_book_symbols``, which reads last run's sleeve JSONs
    and therefore pins the export half downstream of step 3.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from etf_registry import (  # noqa: PLC0415
        ETF_REGISTRY, UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS,
    )
    out: dict[str, str] = {}
    for key in list(UNIVERSE_ETFS) + list(UNIVERSE_EUROPE_SECTORS):
        proxy = (ETF_REGISTRY.get(key) or {}).get("yfinance_trading_proxy")
        out[proxy or key] = key
    return out


def engine_cache_window(registry_key: str) -> tuple[str, str | None]:
    """The fetch window ``run_portfolio._build_panels_for`` uses for a member.

    Taken from the member's own constituent price cache — refreshed at step 1,
    so it is available before the engines run — which is what makes the
    repaired cache a drop-in fallback for the engine's own fetch rather than
    a differently-shaped file that happens to share a name.

    Returns ``(start, end)`` as YYYY-MM-DD, with ``end`` None when there is no
    constituent panel to bound it.
    """
    cache = DATA_DIR / f"prices_cache_{registry_key.lower()}.parquet"
    if not cache.exists():
        return DEFAULT_OHLC_START, None
    try:
        cp = pd.read_parquet(cache)
    except Exception:
        return DEFAULT_OHLC_START, None
    if len(cp) == 0:
        return DEFAULT_OHLC_START, None
    # The constituent span, not the existing cache's span. A cache truncated
    # to a two-year vendor fallback would otherwise be "repaired" back to its
    # own truncated start and stay broken.
    start = (cp.index.min() - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (cp.index.max() + timedelta(days=5)).strftime("%Y-%m-%d")
    return start, end


def _read_ohlc_cache(symbol: str) -> pd.DataFrame | None:
    path = DATA_DIR / f"{symbol.lower()}_ohlc_cache.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    return df if len(df) else None


def _fetched_frame_is_worse(fetched: pd.DataFrame,
                            on_disk: pd.DataFrame | None) -> str | None:
    """Reason to REFUSE writing ``fetched`` over ``on_disk``, or None.

    The canonical statement moved to ``price_panel_guard.
    fetched_frame_is_worse`` on 2026-08-19 so that ``backtest.py``'s write
    sites share it — the daily two-year backfill had truncated the five
    sleeve-D Xetra caches through a site this rule did not cover. This name
    stays for its callers and the tests that pin the rule.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from price_panel_guard import fetched_frame_is_worse  # noqa: PLC0415
    return fetched_frame_is_worse(fetched, on_disk)


def _download_ohlc(symbols: list[str], start: str,
                   end: str | None) -> dict[str, pd.DataFrame]:
    """Batched OHLC download, one frame per symbol. Never raises."""
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - env without yfinance
        print(f"  WARN: yfinance unavailable, cannot refresh caches: {exc}")
        return {}
    kwargs = {"start": start, "auto_adjust": True, "progress": False,
              "threads": True, "group_by": "ticker"}
    if end:
        kwargs["end"] = end
    try:
        raw = yf.download(symbols, **kwargs)
    except Exception as exc:
        print(f"  WARN: yfinance batch download failed: {exc}")
        return {}
    out: dict[str, pd.DataFrame] = {}
    multi = isinstance(raw.columns, pd.MultiIndex)
    for sym in symbols:
        try:
            if multi and sym in raw.columns.get_level_values(0):
                frame = raw[sym]
            elif multi and len(symbols) == 1:
                # A single-symbol request can still come back MultiIndexed;
                # the ticker level is then redundant, not missing.
                frame = raw.copy()
                frame.columns = frame.columns.get_level_values(0)
            elif not multi and len(symbols) == 1:
                frame = raw
            else:
                continue
            cols = [c for c in OHLC_COLUMNS if c in frame.columns]
            if "Close" not in cols:
                continue
            frame = frame[cols].copy()
            frame.index = pd.to_datetime(frame.index).tz_localize(None)
            frame = frame.dropna(how="all").sort_index()
            if len(frame):
                out[sym] = frame
        except Exception:
            continue
    return out


def refresh_ohlc_caches(symbols: dict[str, str] | None = None) -> int:
    """Repair the per-ETF OHLC caches the strategy engines read.

    Fetches each engine-facing symbol over the window its own constituent
    panel implies, refuses any response that is degenerate or worse than what
    is already on disk, and then judges what the caches actually hold. Returns
    0 when every engine-facing series is usable, ``UNUSABLE_CACHE_EXIT_CODE``
    otherwise — so the operator sees the fault before step 3 rather than in a
    published Sharpe.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from price_panel_guard import (  # noqa: PLC0415
        FAIL, SKIP, assess_close_series,
    )

    mapping = symbols if symbols is not None else engine_ohlc_tickers()
    if not mapping:
        print("  No engine-facing symbols resolved; nothing to refresh.")
        return 0

    windows = {sym: engine_cache_window(key) for sym, key in mapping.items()}
    # One batched call over the union window. The per-member windows differ
    # only where a constituent panel starts later, and a cache that spans MORE
    # than the engine asks for is sliced correctly on read.
    start = min(w[0] for w in windows.values())
    ends = [w[1] for w in windows.values() if w[1]]
    end = max(ends) if ends else None
    ordered = sorted(mapping)
    print(f"Refreshing {len(ordered)} engine-facing OHLC cache(s) "
          f"({start} -> {end or 'today'}) ...", flush=True)

    fetched = _download_ohlc(ordered, start, end)
    written, refused, unfetched = [], [], []
    for sym in ordered:
        path = DATA_DIR / f"{sym.lower()}_ohlc_cache.parquet"
        on_disk = _read_ohlc_cache(sym)
        frame = fetched.get(sym)
        if frame is None:
            unfetched.append(sym)
            continue
        why = _fetched_frame_is_worse(frame, on_disk)
        if why:
            refused.append(f"{sym} ({why})")
            continue
        try:
            frame.to_parquet(path)
            written.append(sym)
        except Exception as exc:
            refused.append(f"{sym} (write failed: {exc})")

    if written:
        print(f"  Refreshed {len(written)}: {', '.join(written)}")
    if refused:
        print(f"  REFUSED {len(refused)} write(s), keeping the cache on disk:")
        for r in refused:
            print(f"    - {r}")
    if unfetched:
        print(f"  Not returned by the vendor, keeping the cache on disk: "
              f"{', '.join(unfetched)}")

    # Judge what is on disk NOW, which is what the engines will read. The
    # window is the member's own constituent span, so a cache that lost its
    # history reads as truncated here rather than as merely short.
    bad: list[str] = []
    for sym in ordered:
        frame = _read_ohlc_cache(sym)
        close = None
        if frame is not None and "Close" in frame.columns:
            close = frame["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
        w_start = windows[sym][0]
        verdict = assess_close_series(close, sym, window_start=pd.Timestamp(w_start))
        if verdict.status == FAIL:
            bad.append(f"{sym}: {verdict.note}")
        elif verdict.status == SKIP:
            print(f"  {sym}: SKIP — {verdict.note}")

    if bad:
        print(f"\n  FAIL: {len(bad)} engine-facing cache(s) are still unusable:")
        for b in bad:
            print(f"    - {b}")
        print("  Do NOT run the strategy engines against these. A sleeve "
              "backtested on a missing close column allocates to the member "
              "anyway and scores it at exactly zero — the 2026-08-15 SOXX "
              "defect. Re-run this step, or fetch the symbol by hand.")
        return UNUSABLE_CACHE_EXIT_CODE
    print(f"  All {len(ordered)} engine-facing cache(s) usable.")
    return 0


def entry_is_stale(entry: dict | None, now_utc: datetime,
                   max_age_days: int = MAX_CACHE_AGE_DAYS) -> bool:
    """True when a per-ticker record's last date is older than
    ``max_age_days`` calendar days before ``now_utc``. Calendar days, not
    sessions, so a weekend + holiday cluster never trips it."""
    if not entry or not entry.get("dates"):
        return True
    cutoff = (now_utc - timedelta(days=max_age_days)).date().isoformat()
    return entry["dates"][-1] < cutoff


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    # An unrepaired regression now fails by DEFAULT. It was originally opt-in
    # behind --strict, but nothing passed the flag — not refresh_all.py, not
    # either workflow — so the run that rewrote EEM backwards would still have
    # exited 0 and been committed. A guard whose failure mode is off by
    # default is documentation, not a guard. --strict is kept as an accepted
    # no-op so any stray invocation does not crash.
    ap.add_argument("--strict", action="store_true",
                    help="deprecated, now the default: an unrepaired "
                         "regression always exits "
                         f"{REGRESSION_EXIT_CODE}")
    ap.add_argument("--refresh-caches-only", action="store_true",
                    help="repair the per-ETF OHLC caches the strategy "
                         "engines read, and write NO panel. This is the half "
                         "with no dependency on engine output, so it runs "
                         "before them (refresh_all step 2b). Exits "
                         f"{UNUSABLE_CACHE_EXIT_CODE} if any engine-facing "
                         "series is still unusable afterwards.")
    args = ap.parse_args(argv)
    if args.strict:
        print("  NOTE: --strict is now the default and can be dropped.")

    if args.refresh_caches_only:
        return refresh_ohlc_caches()

    now_utc = datetime.now(timezone.utc)
    print(f"Exporting holdings 1Y price series at "
          f"{now_utc.isoformat(timespec='seconds')} ...")
    tickers = sorted(collect_all_tickers())
    book = collect_book_symbols()
    critical = sorted(set(NETWORK_FALLBACK_TICKERS) | book)
    print(f"  Candidate tickers: {len(tickers)} "
          f"(book-critical: {len(critical)})")

    # The previously published panel, read up front: it is the baseline both
    # for the never-go-backwards check and for the carry-forward guard.
    prev_prices: dict[str, dict] = {}
    if OUT_PATH.exists():
        try:
            prev_prices = (json.loads(OUT_PATH.read_text(encoding="utf-8"))
                           .get("prices") or {})
        except Exception as exc:
            print(f"  WARN: could not read previous panel: {exc}")

    out: dict[str, dict] = {}
    # The close series behind each entry, kept so a repair can be applied to
    # the SERIES and the moving averages rebuilt from it. Splicing an entry
    # would mean hand-patching three MA arrays in step with the price array.
    series: dict[str, "pd.Series"] = {}
    n_skipped: list[str] = []
    for ticker in tickers:
        close = load_close_series(ticker)
        entry = build_entry(close)
        if entry is None:
            n_skipped.append(ticker)
            continue
        series[ticker] = close
        out[ticker] = entry

    # Second pass: any book-critical ticker that is missing, whose only
    # on-disk source is stale, OR whose last bar has gone BACKWARDS against
    # the published panel gets fetched from yfinance. Missing happens on
    # runners whose caches are gitignored; stale happened to EEM, whose only
    # committed source (em_regime_context.parquet) froze at 2026-07-06 while
    # the panel shipped it under a current as-of stamp for two weeks.
    #
    # Regressed is the third case, added 2026-08-10. A stale cache that is
    # merely a few sessions old passes entry_is_stale (7 CALENDAR days is
    # deliberately loose enough to span a weekend + holiday cluster) yet can
    # still be older than what has already been published. EEM lost four
    # sessions inside that tolerance and no guard fired.
    regressed = find_regressions(out, prev_prices)
    if regressed:
        print("  REGRESSION: last bar moved backwards vs the published "
              "panel for " + ", ".join(
                  f"{t} ({p} -> {n})" for t, (p, n) in sorted(regressed.items())))
    refetch = sorted({t for t in critical
                      if t not in out or entry_is_stale(out.get(t), now_utc)}
                     | set(regressed))
    vendor_gaps: dict[str, list[str]] = {}
    reinstated: dict[str, list[str]] = {}
    if refetch:
        stale_names = [t for t in refetch
                       if t in out and t not in regressed]
        if stale_names:
            print(f"  Stale beyond {MAX_CACHE_AGE_DAYS}d, re-fetching: "
                  f"{', '.join(stale_names)}")
        fetched = fetch_missing_from_yfinance(refetch, gaps_out=vendor_gaps)
        if vendor_gaps:
            print("  Vendor returned NO close on a live session for: "
                  + ", ".join(f"{t} ({', '.join(d)})"
                              for t, d in sorted(vendor_gaps.items())))
        for tk, close in fetched.items():
            close, filled, refused = reinstate_vendor_gaps(
                tk, close, prev_prices.get(tk), vendor_gaps.get(tk))
            if filled:
                reinstated[tk] = filled
            if refused:
                print(f"  NOT SPLICED: {tk} — {refused}")
            entry = build_entry(close)
            if entry is not None:
                series[tk] = close
                out[tk] = entry
                if tk in n_skipped:
                    n_skipped.remove(tk)

    # Second repair, against the venue calendar rather than the vendor's own
    # response. A hole can reach the panel from a CACHE as well as from a
    # fetch: the reinstated bar above is not written back to the per-ETF OHLC
    # parquet (that file feeds the strategy engines, and a Close with no
    # Open/High/Low would disarm backtest.py's ATR path), so the next run
    # reads the holed cache and the gap re-opens. This closes it every run.
    #
    # It can only ever fill a session STRICTLY INSIDE the series, so it never
    # moves a last bar and therefore cannot mask a regression. The 2026-08-08
    # EEM shape — a frozen source four sessions short at the TAIL — is
    # untouched by it and still exits 2.
    for tk, entry in list(out.items()):
        gap_dates = interior_gaps(entry, tk)
        if not gap_dates or tk not in series or series[tk] is None:
            continue
        repaired, filled, refused = reinstate_vendor_gaps(
            tk, series[tk], prev_prices.get(tk), gap_dates)
        if refused:
            print(f"  NOT SPLICED: {tk} — {refused}")
            continue
        if not filled:
            continue
        rebuilt = build_entry(repaired)
        if rebuilt is not None:
            series[tk] = repaired
            out[tk] = rebuilt
            reinstated.setdefault(tk, []).extend(filled)
            reinstated[tk] = sorted(set(reinstated[tk]))

    if reinstated:
        print("  REINSTATED from the published panel (vendor withheld a live "
              "session): " + ", ".join(f"{t} ({', '.join(d)})"
                                       for t, d in sorted(reinstated.items())))

    # Never-go-backwards. Any regression the re-fetch did not repair keeps the
    # PREVIOUSLY published series: it is the more truthful of the two, and a
    # shrinking date range under an advancing as-of stamp is precisely the
    # failure this guard exists to stop.
    unrepaired = find_regressions(out, prev_prices)
    for tk, (prev_last, new_last) in sorted(unrepaired.items()):
        out[tk] = prev_prices[tk]
        print(f"  HELD BACK: {tk} re-fetch did not restore it "
              f"({new_last} < {prev_last}); keeping the published series")

    # Coverage guard: never let one degraded run shrink the published panel.
    # The daily Actions job (no cache refresh) used to export 23 tickers and
    # commit that OVER the weekly job's 58 — the dashboard, the nightly
    # factsheet rebuild and the digest's risk visuals then ran all week on a
    # panel missing ~25% of the book. Any ticker present in the existing
    # panel but absent from this run is carried forward unchanged (its own
    # dates array keeps its staleness honest) and reported loudly.
    #
    # The guard must distinguish two cases it originally conflated:
    # a ticker this run WANTED but failed to source (carry it forward, the
    # panel must not shrink), versus one that is no longer requested at all
    # (drop it). Without that split a retired symbol is carried forward for
    # ever with frozen prices, and it pollutes the WARN below with a name
    # that will never resolve again. EXH3.DE became exactly that on
    # 2026-08-03 when sleeve D's industrials panel was repointed to
    # EXH4.DE — see reviews/2026-08-03_sleeve-d-exh3-correction.md.
    # Retiring is a LOCAL privilege (2026-08-27). ``wanted`` is derived partly
    # from the rotation parquets, which are gitignored — so on a CI runner the
    # requested set collapses to the static list plus the book, and every
    # cache-derived name in the published panel looks retired. The run that
    # published at 15:30 UTC on 2026-08-27 duly dropped 26 of them (TLT, GLD,
    # VNQ, XME, ...) and committed a 32-ticker panel over the 58-ticker one a
    # local refresh had written: the exact shrink this guard block was written
    # to prevent, re-opened by the retirement branch added for the EXH3 ghost.
    # run_c_seat_watch.py reads the panel for the whole thematic UNIVERSE, not
    # just current holdings, so those names are not spare.
    #
    # A run that cannot see the universe definition cannot judge what is no
    # longer in it, so it carries everything forward and leaves retirement to
    # the local refresh, which can. EXH3.DE still goes when refresh_all runs.
    wanted = collect_all_tickers() | set(critical)
    can_retire = universe_sources_present()
    carried: list[str] = []
    retired: list[str] = []
    for tk, entry in prev_prices.items():
        if tk in out or not entry or not entry.get("dates"):
            continue
        if tk in wanted or not can_retire:
            out[tk] = entry
            carried.append(tk)
        else:
            retired.append(tk)

    # Completeness, reported rather than assumed. find_regressions compares
    # LAST BARS, so a session missing from the middle of a series passes it
    # without a word — and one did: see interior_gaps. This says so in the log
    # and carries it in the artefact, so a consumer can show the series as
    # holed instead of drawing a straight line across the gap.
    holes = {tk: g for tk, g in ((t, interior_gaps(e, t))
                                 for t, e in out.items()) if g}

    payload = {
        "computed_at_utc": now_utc.isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "interior_gaps": holes,
        "prices": out,
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    size_kb = OUT_PATH.stat().st_size / 1024
    try:
        shown = OUT_PATH.relative_to(ROOT)
    except ValueError:  # OUT_PATH redirected outside the repo (tests)
        shown = OUT_PATH
    print(f"  Wrote {shown}  ({len(out)} tickers, {size_kb:.1f} KB)")
    if carried:
        print(f"  WARN: carried {len(carried)} ticker(s) forward from the "
              f"previous panel (this run could not source them): "
              f"{', '.join(sorted(carried))}")
    if retired:
        print(f"  Dropped {len(retired)} retired ticker(s) no longer "
              f"requested by any sleeve: {', '.join(sorted(retired))}")
    still_missing = [t for t in critical if t not in out]
    if still_missing:
        print(f"  WARN: book-critical ticker(s) STILL missing after "
              f"fallback: {', '.join(still_missing)}")
    if n_skipped:
        print(f"  Skipped (no cache / insufficient data): "
              f"{', '.join(n_skipped)}")

    # Panel currency, reported per ticker rather than as one headline date.
    # Consumers stamp the panel with max() across series, so a single lagging
    # line is invisible in the header — say it here instead.
    newest = max((last_date(e) for e in out.values() if last_date(e)),
                 default=None)
    behind = sorted((tk, last_date(e)) for tk, e in out.items()
                    if last_date(e) and newest and last_date(e) < newest)
    if behind:
        print(f"  Panel newest bar {newest}; {len(behind)} ticker(s) behind it: "
              + ", ".join(f"{t} ({d})" for t, d in behind))
    if holes:
        print(f"  WARN: {len(holes)} ticker(s) are missing a session INSIDE "
              "their own span (the vendor withheld it and the published panel "
              "had nothing to restore): "
              + ", ".join(f"{t} ({', '.join(g)})" for t, g in sorted(holes.items())))
    if unrepaired:
        print(f"  FAIL: {len(unrepaired)} ticker(s) regressed and could not be "
              f"re-sourced: {', '.join(sorted(unrepaired))}")
        print("  The published series were kept, so the panel on disk is "
              "sound. Investigate the source cache before committing it.")
        return REGRESSION_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
