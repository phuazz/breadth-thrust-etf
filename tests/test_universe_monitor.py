"""Guards for the universe monitor's capture-integrity layer.

The monitor's dangerous failure is not a crash — it is a truncated or
stale catalogue that parses fine and reports "no new launches", which is
indistinguishable from a clean run. The vault rule is that no unattended
job ships without a layer that catches a silently-wrong step, so these
tests exist to prove the layer actually fires rather than merely existing.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from run_universe_monitor import (  # noqa: E402
    MAX_FEED_AGE_DAYS,
    RANGE_TOLERANCE,
    FeedIntegrityError,
    check_freshness,
    check_volume,
    parse_feed,
)

HEADER = ("Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market "
          "Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS "
          "Symbol|NASDAQ Symbol|NextShares")
FOOTER = "File Creation Time: 0804202621:33|||||"


def _row(symbol: str, etf: str = "Y", test: str = "N",
         nextshares: str = "N") -> str:
    return (f"Y|{symbol}|{symbol} Fund|P| |{etf}|100|{test}|N|{symbol}|"
            f"{symbol}|{nextshares}")


def _feed(rows: list[str], header: str = HEADER,
          footer: str = FOOTER) -> str:
    return "\n".join([header, *rows, footer])


def test_parses_etfs_and_excludes_non_etf_test_and_nextshares():
    raw = _feed([_row(f"AAA{i}") for i in range(120)]
                + [_row("STOCK", etf="N"),
                   _row("TESTX", test="Y"),
                   _row("NEXTX", nextshares="Y")])
    rows, stamp = parse_feed(raw)
    symbols = {r["symbol"] for r in rows}
    assert len(rows) == 120
    assert "STOCK" not in symbols and "TESTX" not in symbols
    assert "NEXTX" not in symbols
    assert stamp == datetime(2026, 8, 4, 21, 33)


def test_schema_change_is_fatal():
    """A renamed column must stop the run, not silently drop every row."""
    bad = HEADER.replace("Test Issue", "TestIssue")
    raw = _feed([_row(f"AAA{i}") for i in range(120)], header=bad)
    with pytest.raises(FeedIntegrityError, match="schema changed"):
        parse_feed(raw)


def test_missing_or_unparseable_creation_time_is_fatal():
    rows = [_row(f"AAA{i}") for i in range(120)]
    with pytest.raises(FeedIntegrityError, match="File Creation Time"):
        parse_feed(_feed(rows, footer="some other trailer|||||"))
    with pytest.raises(FeedIntegrityError, match="unparseable"):
        parse_feed(_feed(rows, footer="File Creation Time: not-a-date|||||"))


def test_truncated_feed_is_fatal():
    with pytest.raises(FeedIntegrityError, match="only"):
        parse_feed(_feed([_row("AAA")]))


def test_stale_feed_is_fatal():
    """The exact failure the guard exists for: an old file reads as calm."""
    stamp = datetime(2026, 8, 4, 21, 33)
    check_freshness(stamp, date(2026, 8, 5))                    # fresh, fine
    check_freshness(stamp, date(2026, 8, 4))                    # same day
    stale_by = stamp.date() + timedelta(days=MAX_FEED_AGE_DAYS + 1)
    with pytest.raises(FeedIntegrityError, match="days old"):
        check_freshness(stamp, stale_by)


def test_future_dated_feed_is_fatal():
    stamp = datetime(2026, 8, 20, 12, 0)
    with pytest.raises(FeedIntegrityError, match="future"):
        check_freshness(stamp, date(2026, 8, 5))


def test_row_count_collapse_is_fatal():
    """5,573 -> 2,700 is a truncation, not 2,873 closures."""
    check_volume(5573, None)          # first run has nothing to compare
    check_volume(5573, 5500)          # ordinary churn
    with pytest.raises(FeedIntegrityError, match="outside"):
        check_volume(2700, 5573)
    with pytest.raises(FeedIntegrityError, match="outside"):
        check_volume(9000, 5573)


def test_tolerance_band_edges_behave():
    prev = 1000
    lo = int(prev * (1 - RANGE_TOLERANCE)) + 1
    hi = int(prev * (1 + RANGE_TOLERANCE)) - 1
    check_volume(lo, prev)
    check_volume(hi, prev)
    with pytest.raises(FeedIntegrityError):
        check_volume(int(prev * (1 - RANGE_TOLERANCE)) - 1, prev)
