"""Edge cases for the date logic and CSV parser in fetch_constituents.

Per CLAUDE.md: any date arithmetic must use a date library, comment month
indexing, and include at least one month-boundary and one year-boundary test.
We additionally include a leap-year case and a Friday-on-today case.

Python's datetime constructor is 1-indexed for months (Jan=1, Dec=12). All
date literals below use that convention.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Add the scripts/ folder to sys.path so the test can import the module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_constituents as fc  # noqa: E402
from fetch_constituents import (  # noqa: E402
    fetch_with_retry,
    fridays_between,
    latest_completed_friday,
    looks_like_ishares_holdings_csv,
    parse_holdings,
)


def test_fridays_month_boundary():
    """Span Jan 26 (Fri) → Feb 2 (Fri) 2018. Both Fridays must appear."""
    out = fridays_between(date(2018, 1, 26), date(2018, 2, 2))
    assert out == [date(2018, 1, 26), date(2018, 2, 2)]


def test_fridays_year_boundary():
    """Span Dec 28 2018 (Fri) → Jan 4 2019 (Fri). Both must appear."""
    out = fridays_between(date(2018, 12, 28), date(2019, 1, 4))
    assert out == [date(2018, 12, 28), date(2019, 1, 4)]


def test_fridays_leap_year_february():
    """Feb 2024 is a leap year. Feb 29 2024 was a Thursday — it must NOT
    appear in a Friday list, while Feb 23 and Mar 1 (Fridays) must appear.
    """
    out = fridays_between(date(2024, 2, 23), date(2024, 3, 1))
    assert out == [date(2024, 2, 23), date(2024, 3, 1)]
    assert date(2024, 2, 29) not in out


def test_latest_completed_friday_when_today_is_friday():
    """If today is a Friday, return last Friday — today's file is not yet
    settled overnight."""
    # 2026-05-15 is a Friday (verified via pandas, not from memory).
    assert latest_completed_friday(date(2026, 5, 15)) == date(2026, 5, 8)


def test_latest_completed_friday_when_today_is_monday():
    """Monday should resolve to the immediately prior Friday."""
    # 2026-05-11 is a Monday.
    assert latest_completed_friday(date(2026, 5, 11)) == date(2026, 5, 8)


def test_latest_completed_friday_year_boundary():
    """Sun 2026-01-04 — the most recent settled Friday is Fri 2026-01-02."""
    assert latest_completed_friday(date(2026, 1, 4)) == date(2026, 1, 2)


def test_parse_holdings_empty_template():
    """iShares returns this preamble pattern when no data exists for a date."""
    body = (
        '﻿iShares Semiconductor ETF\n'
        'Fund Holdings as of,"-"\n'
        'Inception Date,"Jul 10, 2001"\n'
        'Shares Outstanding,"-"\n'
    )
    assert parse_holdings(body) == []


def test_parse_holdings_filters_non_equity():
    """USD, futures, and cash-management vehicles must be dropped — only
    Asset Class == "Equity" rows belong in the constituent list."""
    body = (
        'iShares Semiconductor ETF\n'
        'Fund Holdings as of,"Jun 28, 2024"\n'
        'Inception Date,"Jul 10, 2001"\n'
        'Shares Outstanding,"60,650,000.00"\n'
        '\n'
        'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,'
        'Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n'
        '"AVGO","BROADCOM INC","Information Technology","Equity","1","1","1","1","1","US","NASDAQ","USD","1.00","USD","-"\n'
        '"NVDA","NVIDIA CORP","Information Technology","Equity","1","1","1","1","1","US","NASDAQ","USD","1.00","USD","-"\n'
        '"USD","US DOLLAR","-","Cash","1","1","1","1","1","-","-","USD","1.00","USD","-"\n'
        '"XTSLA","BLACKROCK CASH FUNDS","-","Cash Collateral and Margins","1","1","1","1","1","-","-","USD","1.00","USD","-"\n'
        '"RTYU4","RUSSELL 2000 EMINI SEP 24","-","Future","1","1","1","1","1","-","CME","USD","1.00","USD","-"\n'
        '\n'
    )
    assert parse_holdings(body) == ["AVGO", "NVDA"]


def test_html_product_page_is_not_cacheable_holdings_csv():
    """iShares anti-bot HTML 200 responses must not be cacheable as a CSV.

    Caching an HTML body would cause downstream parsing to treat the date
    as 'no holdings' → silent stale-constituent carry-forward (the same
    class of silent-data-corruption bug we hit in Phase 4)."""
    body = "<!doctype html><html><body>" + ("x" * 5000) + "</body></html>"
    assert not looks_like_ishares_holdings_csv(body)


def test_empty_template_holdings_csv_is_cacheable():
    """Empty-template responses (Fund Holdings as of '-') are legitimately
    empty for old / no-data dates and should be cached."""
    body = (
        'iShares Test ETF\n'
        'Fund Holdings as of,"-"\n'
        '\n'
    )
    assert looks_like_ishares_holdings_csv(body)


def test_real_holdings_csv_is_cacheable():
    """Populated iShares holdings CSVs (with the Ticker / Asset Class
    column header row) are the canonical valid response."""
    body = (
        'iShares Semiconductor ETF\n'
        'Fund Holdings as of,"Jun 28, 2024"\n'
        '\n'
        'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,'
        'Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n'
        '"NVDA","NVIDIA CORP","Information Technology","Equity","1","1","1","1","1","US","NASDAQ","USD","1.00","USD","-"\n'
    )
    assert looks_like_ishares_holdings_csv(body)


def test_fetch_discards_and_re_fetches_poisoned_html_cache(tmp_path, monkeypatch):
    """If a prior run cached an HTML 200 body (anti-bot stand-in) as a
    CSV, the next read should detect it and re-fetch from the network."""
    cfg = {"symbol": "TEST", "csv_url_template": "https://example.test/holdings"}
    cache_path = tmp_path / "TEST_20240628.csv"
    cache_path.write_text("<html>" + ("x" * 5000) + "</html>", encoding="utf-8")

    valid_body = (
        'iShares Test ETF\n'
        'Fund Holdings as of,"Jun 28, 2024"\n'
        '\n'
        'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,'
        'Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n'
        '"NVDA","NVIDIA CORP","Information Technology","Equity","1","1","1","1","1","US","NASDAQ","USD","1.00","USD","-"\n'
        + ("#" * 1200)
    )

    class FakeResponse:
        status_code = 200
        text = valid_body

    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fc.requests, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(fc.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fc.random, "uniform", lambda *_args, **_kwargs: 0)

    body = fetch_with_retry(date(2024, 6, 28), cfg)

    assert body == valid_body
    assert cache_path.read_text(encoding="utf-8") == valid_body


def test_parse_holdings_normalises_us_share_class_without_exchange_suffix():
    """Default US parsing should still emit yfinance-ready share classes."""
    body = (
        'iShares Test ETF\n'
        'Fund Holdings as of,"Jun 28, 2024"\n'
        '\n'
        'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,'
        'Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n'
        '"BRK.B","BERKSHIRE HATHAWAY","Financials","Equity","1","1","1","1","1","US","NYSE","USD","1.00","USD","-"\n'
        '\n'
    )
    assert parse_holdings(body) == ["BRK-B"]


def test_parse_holdings_normalises_non_us_ticker_roots_before_suffix():
    """Non-US symbols must not get double dots or dot-separated roots."""
    body = (
        'iShares Test ETF\n'
        'Fund Holdings as of,"Jun 28, 2024"\n'
        '\n'
        'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,'
        'Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n'
        '"BP.","BP PLC","Energy","Equity","1","1","1","1","1","GB","London Stock Exchange","GBP","1.00","GBP","-"\n'
        '"REP.D","REPSOL","Energy","Equity","1","1","1","1","1","ES","Bolsa De Madrid","EUR","1.00","EUR","-"\n'
        '"BAJAJ.AUTO","BAJAJ AUTO","Consumer Discretionary","Equity","1","1","1","1","1","IN","National Stock Exchange Of India","INR","1.00","INR","-"\n'
        '"GRASIM.RE","GRASIM RIGHTS","Materials","Equity","1","1","1","1","1","IN","National Stock Exchange Of India","INR","1.00","INR","-"\n'
        '"REPSM.RI","REPSOL RIGHTS","Energy","Equity","1","1","1","1","1","ES","Bolsa De Madrid","EUR","1.00","EUR","-"\n'
        '"HSBA.L","HSBC","Financials","Equity","1","1","1","1","1","GB","London Stock Exchange","GBP","1.00","GBP","-"\n'
        '\n'
    )
    assert parse_holdings(body, apply_exchange_suffix=True) == [
        "BP.L",
        "REP.MC",
        "BAJAJ-AUTO.NS",
        "GRASIM.NS",
        "HSBA.L",
    ]


def test_parse_holdings_skips_placeholders_and_dedupes():
    """Parser output should be a clean constituent set, not raw CSV rows."""
    body = (
        'iShares Test ETF\n'
        'Fund Holdings as of,"Jun 28, 2024"\n'
        '\n'
        'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,'
        'Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n'
        '"AAPL","APPLE","Information Technology","Equity","1","1","1","1","1","US","NASDAQ","USD","1.00","USD","-"\n'
        '"AAPL","APPLE DUP","Information Technology","Equity","1","1","1","1","1","US","NASDAQ","USD","1.00","USD","-"\n'
        '"-","PLACEHOLDER","Industrials","Equity","1","1","1","1","1","US","NASDAQ","USD","1.00","USD","-"\n'
        '\n'
    )
    assert parse_holdings(body) == ["AAPL"]
