"""WS2 candidate price panel fetch + ticker verification (review session 2).

Downloads USD adjusted-close (dividend-adjusted, i.e. total-return-style)
series for the Workstream 2 country-sleeve candidates and benchmarks into
data/ws2_prices_cache.parquet, WITHOUT touching any deployed cache. Also
writes data/ws2_ticker_verification.json recording, per ticker, the name
returned by yfinance and the name in data/ishares_catalogue.csv — two
independent sources per the data-integrity rule. EWW and EWS are absent
from the catalogue; they are verified against the issuer web page in the
session log and flagged here as needing that second source.

Not a backtest (no signal, no portfolio maths), so the three-silent-ways
statement reduces to data integrity: (1) FX/total return — all lines are
US-listed USD funds and auto_adjust=True folds distributions into the
close; (2) survivorship — every candidate is a live fund TODAY, so any
backtest on this panel inherits a mild live-fund bias, recorded in the
verification JSON; (3) staleness/alignment — the panel is written raw
(native NYSE calendar), consumers reindex to their own baseline calendar.

Run: python scripts/run_ws2_fetch_panel.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.stdout.reconfigure(encoding="utf-8")

OUT_PRICES = DATA / "ws2_prices_cache.parquet"
OUT_VERIFY = DATA / "ws2_ticker_verification.json"
CATALOGUE = DATA / "ishares_catalogue.csv"

START = "2003-01-01"

# Candidates per REVIEW_PROMPT.md Workstream 2 item 4, plus benchmarks.
TICKERS = {
    # 10 single-country candidates
    "EWZ":  "country", "EWW": "country", "EWY": "country", "INDA": "country",
    "EWT":  "country", "EWA": "country", "EWS": "country", "EWG":  "country",
    "EWU":  "country", "EWJ": "country",
    # frontier + broad EM
    "FM":   "frontier", "EEM": "em_broad", "IEMG": "em_broad",
    # benchmark + context + cash floor
    "EFA":  "benchmark", "SPY": "context", "SHY": "cash",
}


def main() -> int:
    tickers = list(TICKERS)
    print(f"Downloading {len(tickers)} tickers from {START} ...", flush=True)
    raw = yf.download(tickers, start=START,
                      end=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                      auto_adjust=True, progress=False, threads=True,
                      group_by="ticker")
    closes = {}
    for t in tickers:
        if (t, "Close") in raw.columns:
            closes[t] = raw[(t, "Close")]
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index().dropna(how="all")
    missing = [t for t in tickers if t not in df.columns]
    if missing:
        raise RuntimeError(f"no data returned for {missing} — abort")
    df.to_parquet(OUT_PRICES)
    print(f"  wrote {OUT_PRICES.name}: {df.shape[0]} rows x {df.shape[1]} "
          f"cols, {df.index.min().date()} -> {df.index.max().date()}")

    # first valid date per ticker (inception coverage vs the fixed window)
    first_valid = {t: str(df[t].first_valid_index().date()) for t in tickers}

    # yfinance metadata (source 1)
    yf_names = {}
    for t in tickers:
        try:
            info = yf.Ticker(t).info
            yf_names[t] = info.get("longName") or info.get("shortName")
        except Exception as exc:  # noqa: BLE001 — verification best-effort
            yf_names[t] = f"<info unavailable: {type(exc).__name__}>"
        print(f"  {t:5s} {first_valid[t]}  yf: {yf_names[t]}")

    # iShares catalogue (source 2)
    cat = {}
    with CATALOGUE.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cat[row["ticker"]] = row["name"]
    verification = {}
    for t in tickers:
        verification[t] = {
            "role": TICKERS[t],
            "first_data": first_valid[t],
            "yfinance_name": yf_names[t],
            "ishares_catalogue_name": cat.get(t),
            "needs_web_source": t in ("EWW", "EWS"),
        }
    OUT_VERIFY.write_text(json.dumps({
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "note": ("Two-source ticker verification: yfinance metadata vs "
                 "data/ishares_catalogue.csv. EWW/EWS absent from the "
                 "catalogue -> verified against issuer page in the session "
                 "record. SPY (SPDR) and SHY are deployed incumbents, not "
                 "new additions. Survivorship: all candidates are live "
                 "funds today (live-fund bias inherited by any backtest "
                 "on this panel)."),
        "tickers": verification,
    }, indent=1), encoding="utf-8")
    print(f"  wrote {OUT_VERIFY.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
