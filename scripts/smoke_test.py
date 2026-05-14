"""Step 0 smoke test — verify yfinance coverage of point-in-time SOXX constituents.

Pulls SOXX holdings on three reference dates spanning the available
iShares history, then queries yfinance for the full constituent set on
each date to see how many tickers return usable historical price data
in a 200 trading-day window ending at the snapshot date.

Decision gate (per user instruction): if 2009-era coverage is materially
below 80 per cent, the backtest window will start in 2014 rather than at
the iShares earliest available date. The report from this script informs
that choice; it does not auto-decide.

This script is exploratory and intentionally NOT cached — the iShares
pull is small (three CSVs) and the yfinance pull is bounded.

Run:
    python scripts/smoke_test.py
"""

from __future__ import annotations

import io
import csv
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import requests
import yfinance as yf

# Force UTF-8 stdout so the BOM in iShares CSVs and any non-ASCII ticker
# names do not crash on the Windows cp1252 console.
sys.stdout.reconfigure(encoding="utf-8")


ISHARES_BASE = (
    "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/"
    "1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Reference dates spanning the iShares history. All are real trading days.
# 2017 is intentionally omitted — direct probing showed iShares' history
# for SOXX has a year-long gap covering most of 2017 (responses return an
# empty "Fund Holdings as of '-'" template). The fetch pipeline will need
# a gap-handling strategy; here we just sample populated dates.
PROBE_DATES = [
    date(2009, 6, 30),
    date(2012, 6, 29),
    date(2014, 6, 30),
    date(2016, 6, 30),
    date(2018, 6, 29),
    date(2020, 6, 30),
    date(2024, 6, 28),
]

# Minimum trading days of price data required to count a ticker as "covered"
# in the ~200 trading-day window ending at the snapshot date.
MIN_DAYS_REQUIRED = 100


@dataclass
class Coverage:
    snapshot: date
    total: int
    covered: int
    missing: list[str]

    @property
    def pct(self) -> float:
        return self.covered / self.total * 100 if self.total else 0.0


def fetch_holdings(d: date) -> list[str]:
    """Download the iShares SOXX holdings CSV for a given date and return the
    Equity-only ticker list.

    CSV layout: preamble of fund-level metadata, then a row beginning
    'Ticker,Name,Sector,Asset Class,...', then one row per holding, then a
    blank line that terminates the holdings block. Cash, futures, and cash-
    management vehicles (USD, RTYU4, IXTU4, XTSLA, WFFUT) appear with
    Asset Class values like 'Cash', 'Cash Collateral and Margins', 'Currency',
    or 'Future' — we filter to 'Equity' only.
    """
    url = f"{ISHARES_BASE}&asOfDate={d.strftime('%Y%m%d')}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    lines = r.text.splitlines()
    tickers: list[str] = []
    header: list[str] | None = None
    asset_class_idx: int | None = None
    for ln in lines:
        if header is None:
            if "Ticker" in ln[:20] and "Asset Class" in ln:
                header = next(csv.reader(io.StringIO(ln)))
                asset_class_idx = header.index("Asset Class")
            continue
        if not ln.strip():
            break  # blank line ends the holdings block
        row = next(csv.reader(io.StringIO(ln)))
        if not row or not row[0]:
            continue
        if asset_class_idx is not None and len(row) > asset_class_idx:
            if row[asset_class_idx].strip() != "Equity":
                continue
        tickers.append(row[0].strip())
    return tickers


def check_yfinance_coverage(tickers: list[str], end: date) -> Coverage:
    """For each ticker, attempt to fetch ~200 trading days of history ending on `end`.

    Counts a ticker as covered if at least MIN_DAYS_REQUIRED rows are returned.
    """
    # Pull ~300 calendar days back to cover ~200 trading days plus buffer.
    start = end - timedelta(days=300)
    covered = 0
    missing: list[str] = []
    # yfinance can batch-download. Use threads=True for speed but query one
    # ticker at a time for clearer error attribution in this smoke test.
    for t in tickers:
        try:
            df = yf.download(
                t,
                start=start.isoformat(),
                end=(end + timedelta(days=2)).isoformat(),
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            n = len(df) if df is not None else 0
        except Exception:
            n = 0
        if n >= MIN_DAYS_REQUIRED:
            covered += 1
        else:
            missing.append(t)
        time.sleep(0.05)
    return Coverage(snapshot=end, total=len(tickers), covered=covered, missing=missing)


def main() -> None:
    print("Step 0 smoke test — SOXX point-in-time constituent → yfinance coverage")
    print("=" * 72)
    results: list[Coverage] = []
    for d in PROBE_DATES:
        print(f"\nPulling SOXX holdings as of {d.isoformat()} ...")
        tickers = fetch_holdings(d)
        print(f"  {len(tickers)} tickers in snapshot")
        print(f"  Sample: {tickers[:8]} ... {tickers[-4:] if len(tickers) > 8 else ''}")
        print(f"  Probing yfinance for {len(tickers)} tickers "
              f"(window ending {d.isoformat()}, require >= {MIN_DAYS_REQUIRED} rows) ...")
        cov = check_yfinance_coverage(tickers, d)
        results.append(cov)
        print(f"  Coverage: {cov.covered}/{cov.total} = {cov.pct:.1f}%")
        if cov.missing:
            print(f"  Missing : {cov.missing}")

    print("\n" + "=" * 72)
    print("Summary")
    print("=" * 72)
    for cov in results:
        flag = "OK" if cov.pct >= 80 else "BELOW 80%"
        print(f"  {cov.snapshot.isoformat()}: {cov.covered:>3}/{cov.total:<3} "
              f"= {cov.pct:5.1f}%  [{flag}]")
    print()
    print("Decision rule: if any snapshot is materially below 80%, recommend")
    print("starting the backtest window after the first snapshot that hits >= 80%.")


if __name__ == "__main__":
    main()
