"""Guard for scripts/norgate_symbols.resolve.

The failure this module exists to prevent is SILENT: resolving a roster ticker
by name alone attaches whichever company holds that ticker today to a
constituent from years ago. It is not hypothetical — the committed yfinance
cache already carries 281 bars under "FB" from 2025-06-26, which belong to the
ProShares S&P 500 Dynamic Buffer ETF and not to Facebook.

A wrong price here does not raise; it produces a plausible breadth number
computed from the wrong security. So these tests assert the SECURITY NAME
behind every resolution, not merely that something resolved.

Skipped wholesale when Norgate is unavailable — it is a local Windows
install, absent in CI, which is already true of the constituent refresh.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import norgate_symbols as ns  # noqa: E402


def _nd():
    try:
        import norgatedata as nd
        if not nd.status():
            pytest.skip("local Norgate service is not running")
        return nd
    except ImportError:
        pytest.skip("norgatedata is not installed")


def _name(sym):
    return _nd().security_name(sym) if sym else None


# --------------------------------------------------------------------------
# The reuse trap.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ticker,as_of,expect_name", [
    # Same ticker, two different companies, decided by date.
    ("FB",   date(2018, 1, 5),  "Meta Platforms Inc Class A Common"),
    ("FB",   date(2026, 1, 5),  "ProShares S&P 500 Dynamic Buffer ETF"),
    ("CA",   date(2018, 1, 5),  "CA Inc Common"),
    ("CA",   date(2025, 1, 3),  "Xtrackers California Municipal Bond ETF"),
    ("SPLK", date(1998, 6, 5),  "Spanlink Communications Inc Common"),
    ("SPLK", date(2018, 1, 5),  "Splunk Inc Common"),
])
def test_reused_tickers_resolve_by_date(ticker, as_of, expect_name):
    _nd()
    assert _name(ns.resolve(ticker, as_of)) == expect_name


def test_facebook_is_not_the_proshares_etf():
    """The specific defect already sitting in the yfinance cache."""
    _nd()
    sym = ns.resolve("FB", date(2018, 1, 5))
    assert sym == "META", f"2018 FB resolved to {sym!r}, not Facebook's history"
    assert "ProShares" not in (_name(sym) or "")


# --------------------------------------------------------------------------
# Delistings and renames.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ticker,expect_name", [
    ("XLNX",  "Xilinx Inc Common"),
    ("ATVI",  "Activision Blizzard Inc Common"),
    ("CELG",  "Celgene Corp Common"),
    ("SHPG",  "Shire PLC ADR"),
    ("HOLX",  "Hologic Inc Common"),
    ("WBA",   "Walgreens Boots Alliance Inc Common"),
    ("TFCFA", "Twenty-First Century Fox Inc Class A Common"),
])
def test_delisted_names_resolve_to_themselves(ticker, expect_name):
    _nd()
    assert _name(ns.resolve(ticker, date(2018, 1, 5))) == expect_name


@pytest.mark.parametrize("ticker,expect_name", [
    ("CTRP", "Trip.com Group Ltd ADR"),
    ("MYL",  "Viatris Inc Common"),
    ("SYMC", "Gen Digital Inc Common"),
    ("WLTW", "Willis Towers Watson Public Ltd Co Common"),
    ("QVCA", "Qurate Retail Group Series A Common"),
])
def test_renamed_names_resolve_to_the_successor(ticker, expect_name):
    _nd()
    assert _name(ns.resolve(ticker, date(2018, 1, 5))) == expect_name


# --------------------------------------------------------------------------
# Deliberate non-answers. None must mean "do not price this", never "guess".
# --------------------------------------------------------------------------

def test_non_equity_lines_are_excluded_not_guessed():
    _nd()
    for t in ns.NOT_EQUITY:
        assert ns.resolve(t, date(2020, 6, 26)) is None, (
            f"{t} is not an ordinary listing and must not resolve")


def test_a_name_that_did_not_exist_yet_resolves_to_none():
    """Covetrus was spun out of Henry Schein in 2019; asking for it in 2018
    must return nothing rather than the nearest available window."""
    _nd()
    assert ns.resolve("CVET", date(2018, 1, 5)) is None
    assert _name(ns.resolve("CVET", date(2021, 1, 8))) == "Covetrus Inc Common"


def test_unknown_ticker_resolves_to_none():
    _nd()
    assert ns.resolve("ZZZZNOTAREALTICKER", date(2020, 1, 3)) is None


# --------------------------------------------------------------------------
# Date handling. Norgate returns quotation dates as strings, and comparing
# those to a date raised rather than mis-ordering — pin the normalisation.
# --------------------------------------------------------------------------

def test_as_date_normalises_every_shape():
    from datetime import datetime
    assert ns._as_date("2018-01-05") == date(2018, 1, 5)
    assert ns._as_date(date(2018, 1, 5)) == date(2018, 1, 5)
    assert ns._as_date(datetime(2018, 1, 5, 16, 0)) == date(2018, 1, 5)
    assert ns._as_date(None) is None
    assert ns._as_date("not-a-date") is None


def test_boundary_dates_are_inclusive():
    """A constituent priced on its final quoted day must still resolve —
    that day is a real close and belongs in the panel."""
    _nd()
    import norgatedata as nd
    last = ns._as_date(nd.last_quoted_date("XLNX-202202"))
    first = ns._as_date(nd.first_quoted_date("XLNX-202202"))
    assert ns.resolve("XLNX", last) == "XLNX-202202"
    assert ns.resolve("XLNX", first) == "XLNX-202202"


def test_month_and_year_boundary():
    # CLAUDE.md date rule: one month boundary, one year boundary. Xilinx was
    # quoting across both.
    _nd()
    assert ns.resolve("XLNX", date(2018, 1, 31)) == "XLNX-202202"
    assert ns.resolve("XLNX", date(2018, 12, 31)) == "XLNX-202202"
    assert ns.resolve("XLNX", date(2019, 1, 1)) == "XLNX-202202"
