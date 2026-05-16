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

from fetch_constituents import (  # noqa: E402
    fridays_between,
    latest_completed_friday,
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
