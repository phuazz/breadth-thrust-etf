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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "holdings_prices_1y.json"

LOOKBACK_DAYS = 252  # ~1 calendar year of trading days

# Strategy A trade-as proxies (the deployed-execution tickers) +
# Strategy D Xetra UCITS that have their own OHLC caches.
INDIVIDUAL_OHLC_TICKERS = [
    # Strategy A SPDR sector proxies + broad-market direct holds
    "XLE", "XLF", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLC", "XLRE",
    "XLK",
    "SPY", "QQQ", "IJR", "SOXX",
    # Strategy D Xetra UCITS
    "EXV1.DE", "EXH1.DE", "EXV3.DE", "EXH3.DE", "EXH9.DE",
    # Reference / extras
    "INDA", "MCHI",
]


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
    return tickers


def main() -> int:
    print(f"Exporting holdings 1Y price series at "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} ...")
    tickers = sorted(collect_all_tickers())
    print(f"  Candidate tickers: {len(tickers)}")

    out: dict[str, dict] = {}
    n_skipped: list[str] = []
    for ticker in tickers:
        close = load_close_series(ticker)
        if close is None or len(close) < 2:
            n_skipped.append(ticker)
            continue
        # Take last LOOKBACK_DAYS trading days. If less history available,
        # take whatever exists (chart will just show a shorter window).
        tail = close.iloc[-LOOKBACK_DAYS:]
        if len(tail) < 2:
            n_skipped.append(ticker)
            continue
        first = float(tail.iloc[0])
        last = float(tail.iloc[-1])
        change_pct = (last / first - 1.0) if first else None
        out[ticker] = {
            "dates": [d.strftime("%Y-%m-%d") for d in tail.index],
            "prices": _round_sig([float(v) for v in tail.values]),
            "change_pct": round(change_pct, 4) if change_pct is not None else None,
            "n_days": int(len(tail)),
        }

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "prices": out,
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"  Wrote {OUT_PATH.relative_to(ROOT)}  "
          f"({len(out)} tickers, {size_kb:.1f} KB)")
    if n_skipped:
        print(f"  Skipped (no cache / insufficient data): "
              f"{', '.join(n_skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
