"""Export last 1Y daily prices for every ETF that can appear in any of
the four deployed strategies' holdings tables. Output:
``data/holdings_prices_1y.json``.

Used by the Monitor tab's holdings click-to-expand mini-chart. Reads
from existing parquet caches (no network calls) so it is cheap to
re-run as part of the pipeline.

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
    """Try every known cache location for this ticker; return Close series
    or None if not found / empty."""
    # 1. Asset-class multi-ETF parquet
    ac = DATA_DIR / "asset_class_prices_cache.parquet"
    if ac.exists():
        try:
            df = pd.read_parquet(ac)
            if ticker in df.columns and df[ticker].notna().any():
                return df[ticker].dropna()
        except Exception:
            pass
    # 2. Thematic multi-ETF parquet
    tc = DATA_DIR / "thematic_prices_cache.parquet"
    if tc.exists():
        try:
            df = pd.read_parquet(tc)
            if ticker in df.columns and df[ticker].notna().any():
                return df[ticker].dropna()
        except Exception:
            pass
    # 3. Individual ETF OHLC parquet — file naming is lowercase
    ohlc = DATA_DIR / f"{ticker.lower()}_ohlc_cache.parquet"
    if ohlc.exists():
        try:
            df = pd.read_parquet(ohlc)
            # OHLC dataframes carry Open / High / Low / Close columns
            if "Close" in df.columns:
                ser = df["Close"]
                if isinstance(ser, pd.DataFrame):
                    ser = ser.iloc[:, 0]
                return ser.dropna()
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
            if ticker in df.columns and df[ticker].notna().any():
                return df[ticker].dropna()
        except Exception:
            pass
    return None


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


def fetch_missing_from_yfinance(tickers: list[str]) -> dict[str, pd.Series]:
    """Last-resort fetch for book-critical tickers whose local caches are
    absent (the CI-runner case — those caches are gitignored). Downloads ~2
    calendar years of daily closes in one batched call so the 200d MA is
    populated, writes each back to its ``{ticker}_ohlc_cache.parquet`` so
    subsequent runs are cheap, and returns {ticker: Close series}. Any
    failure degrades gracefully to an empty mapping — the caller then simply
    reports the ticker as skipped rather than crashing the (soft-fail) step.
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
    print(f"  Backfilling {len(tickers)} ticker(s) from yfinance: "
          f"{', '.join(tickers)}")
    out: dict[str, pd.Series] = {}
    try:
        raw = yf.download(tickers, period="2y", auto_adjust=True,
                          progress=False, threads=True, group_by="ticker")
    except Exception as exc:
        print(f"  WARN: yfinance batch download failed: {exc}")
        return {}
    for tk in tickers:
        try:
            if len(tickers) == 1:
                # Single-ticker downloads are not MultiIndex-keyed by ticker.
                ser = raw["Close"] if "Close" in raw.columns else None
            else:
                ser = raw[(tk, "Close")] if (tk, "Close") in raw.columns else None
            if ser is None:
                continue
            ser = ser.dropna()
            if ser.empty:
                continue
            ser.index = pd.to_datetime(ser.index).tz_localize(None)
            out[tk] = ser
            # Persist as an OHLC-style cache so load_close_series finds it next
            # time and local runs stay network-free.
            try:
                pd.DataFrame({"Close": ser}).to_parquet(
                    DATA_DIR / f"{tk.lower()}_ohlc_cache.parquet")
            except Exception:
                pass
        except Exception:
            continue
    return out


def entry_is_stale(entry: dict | None, now_utc: datetime,
                   max_age_days: int = MAX_CACHE_AGE_DAYS) -> bool:
    """True when a per-ticker record's last date is older than
    ``max_age_days`` calendar days before ``now_utc``. Calendar days, not
    sessions, so a weekend + holiday cluster never trips it."""
    if not entry or not entry.get("dates"):
        return True
    cutoff = (now_utc - timedelta(days=max_age_days)).date().isoformat()
    return entry["dates"][-1] < cutoff


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    print(f"Exporting holdings 1Y price series at "
          f"{now_utc.isoformat(timespec='seconds')} ...")
    tickers = sorted(collect_all_tickers())
    book = collect_book_symbols()
    critical = sorted(set(NETWORK_FALLBACK_TICKERS) | book)
    print(f"  Candidate tickers: {len(tickers)} "
          f"(book-critical: {len(critical)})")

    out: dict[str, dict] = {}
    n_skipped: list[str] = []
    for ticker in tickers:
        entry = build_entry(load_close_series(ticker))
        if entry is None:
            n_skipped.append(ticker)
            continue
        out[ticker] = entry

    # Second pass: any book-critical ticker that is missing OR whose only
    # on-disk source is stale gets fetched from yfinance. Missing happens on
    # runners whose caches are gitignored; stale happened to EEM, whose only
    # committed source (em_regime_context.parquet) froze at 2026-07-06 while
    # the panel shipped it under a current as-of stamp for two weeks.
    refetch = [t for t in critical
               if t not in out or entry_is_stale(out.get(t), now_utc)]
    if refetch:
        stale_names = [t for t in refetch if t in out]
        if stale_names:
            print(f"  Stale beyond {MAX_CACHE_AGE_DAYS}d, re-fetching: "
                  f"{', '.join(stale_names)}")
        for tk, close in fetch_missing_from_yfinance(refetch).items():
            entry = build_entry(close)
            if entry is not None:
                out[tk] = entry
                if tk in n_skipped:
                    n_skipped.remove(tk)

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
    wanted = collect_all_tickers() | set(critical)
    carried: list[str] = []
    retired: list[str] = []
    if OUT_PATH.exists():
        try:
            prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            for tk, entry in (prev.get("prices") or {}).items():
                if tk in out or not entry or not entry.get("dates"):
                    continue
                if tk in wanted:
                    out[tk] = entry
                    carried.append(tk)
                else:
                    retired.append(tk)
        except Exception as exc:
            print(f"  WARN: could not read previous panel for the "
                  f"carry-forward guard: {exc}")

    payload = {
        "computed_at_utc": now_utc.isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "prices": out,
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"  Wrote {OUT_PATH.relative_to(ROOT)}  "
          f"({len(out)} tickers, {size_kb:.1f} KB)")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
