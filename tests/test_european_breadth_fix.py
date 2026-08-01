"""Guard tests for the 2026-08-01 European breadth coverage fix.

Three defects are pinned down here so they cannot regress silently:

1. Holiday-NaN window poisoning — on multi-exchange panels, a single
   home-venue holiday NaN inside rolling(w, min_periods=w) used to
   invalidate a ticker's MA/high for the next w rows, erasing ~40% of
   European ma_breadth coverage in recurring annual blocks (April-July,
   late-December-February, September-October). per_ticker_apply computes
   indicators on each ticker's own traded sessions instead.

2. Exchange-string resolution — Nordic / Swiss / Vienna / Warsaw / Dublin /
   Prague venue names in iShares Europe CSVs were absent from the suffix
   map, so live names (SEB, Danske, DNB, Erste, Nordea, PKO, AIB, ...)
   were carried as unresolvable raw tickers for the entire history and
   inflated the apparent survivorship problem.

3. Trading-calendar selection — European sector funds are sampled on XETR,
   not NYSE, so European trading days are not dropped on US holidays.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute_breadth import (  # noqa: E402
    MA_PERIOD,
    compute_rsi,
    normalise_for_yfinance,
    per_ticker_apply,
)
from etf_registry import get_etf  # noqa: E402
from fetch_constituents import _resolve_yf_symbol, parse_holdings  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Holiday-NaN window poisoning
# ---------------------------------------------------------------------------


def _panel_with_single_holiday(n_days: int = 160, holiday_row: int = 80):
    """Two-ticker panel: AAA has full data; BBB is NaN on one mid-panel day
    (its home-exchange holiday while AAA's venue traded).

    Python datetime/pandas months are 1-indexed (January=1).
    """
    idx = pd.bdate_range("2023-01-02", periods=n_days)
    rng = np.random.default_rng(7)
    base = 100.0 + np.cumsum(rng.normal(0.05, 1.0, size=n_days))
    panel = pd.DataFrame({"AAA": base, "BBB": base * 1.5}, index=idx)
    panel.iloc[holiday_row, panel.columns.get_loc("BBB")] = np.nan
    return panel, idx, holiday_row


def test_single_holiday_nan_does_not_poison_ma50():
    panel, idx, hol = _panel_with_single_holiday()
    ma = per_ticker_apply(
        panel, lambda s: s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean())

    # Old behaviour (documented, not desired): plain rolling on the union
    # grid loses BBB's MA for MA_PERIOD rows after the holiday.
    ma_old = panel.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    poisoned = ma_old["BBB"].iloc[hol:hol + MA_PERIOD]
    assert poisoned.isna().all(), "test premise: old method poisons the window"

    # New behaviour: BBB's MA is defined on every session it traded from its
    # 50th traded session onward — including the whole ex-holiday stretch.
    traded = panel["BBB"].notna()
    eligible = traded & (traded.cumsum() >= MA_PERIOD)
    assert ma["BBB"][eligible].notna().all()
    # And it is NaN on the holiday itself (no fabricated reading).
    assert np.isnan(ma["BBB"].iloc[hol])


def test_per_ticker_matches_plain_rolling_on_clean_panel():
    idx = pd.bdate_range("2022-01-03", periods=120)
    rng = np.random.default_rng(11)
    panel = pd.DataFrame(
        {c: 50 + np.cumsum(rng.normal(0, 1, size=len(idx))) for c in "XYZ"},
        index=idx,
    )
    new = per_ticker_apply(
        panel, lambda s: s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean())
    old = panel.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    pd.testing.assert_frame_equal(new, old)


def test_per_ticker_ma_values_identical_where_old_defined():
    """Where the old method produced a value despite the panel containing a
    NaN elsewhere, the windows contain the same 50 closes, so values match
    exactly. The fix extends coverage; it must not move existing values."""
    panel, idx, hol = _panel_with_single_holiday()
    new = per_ticker_apply(
        panel, lambda s: s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean())
    old = panel.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    both = old["BBB"].notna()
    assert both.sum() > 0
    assert np.allclose(new["BBB"][both], old["BBB"][both], atol=0, rtol=0)


def test_rsi_defined_through_holiday_gap():
    panel, idx, hol = _panel_with_single_holiday()
    rsi = per_ticker_apply(
        panel, lambda s: compute_rsi(s.to_frame("_c"), 14)["_c"])
    after = rsi["BBB"].iloc[hol + 1:hol + 6]
    assert after.notna().all()


def test_per_ticker_apply_handles_all_nan_column():
    idx = pd.bdate_range("2023-01-02", periods=60)
    panel = pd.DataFrame(
        {"AAA": np.linspace(10, 20, len(idx)), "DEAD": np.nan}, index=idx)
    out = per_ticker_apply(
        panel, lambda s: s.rolling(5, min_periods=5).mean())
    assert out["DEAD"].isna().all()
    assert list(out.columns) == ["AAA", "DEAD"]


# ---------------------------------------------------------------------------
# 2. Exchange-string resolution
# ---------------------------------------------------------------------------


def test_resolver_new_european_venues():
    cases = [
        # (raw ticker, Exchange string, Location, expected)
        ("NDA FI", "Nasdaq Omx Helsinki Ltd.", "Sweden", "NDA-FI.HE"),
        ("SEB A", "Nasdaq Omx Nordic", "Sweden", "SEB-A.ST"),
        ("SWED A", "Nasdaq Omx Nordic", "Sweden", "SWED-A.ST"),
        ("DANSKE", "Omx Nordic Exchange Copenhagen A/S", "Denmark",
         "DANSKE.CO"),
        ("MAERSK B", "Omx Nordic Exchange Copenhagen A/S", "Denmark",
         "MAERSK-B.CO"),
        ("DNB", "Oslo Bors Asa", "Norway", "DNB.OL"),
        ("EBS", "Wiener Boerse Ag", "Austria", "EBS.VI"),
        ("PKO", "Warsaw Stock Exchange/Equities/Main Market", "Poland",
         "PKO.WA"),
        ("BCVN", "SIX Swiss Exchange", "Switzerland", "BCVN.SW"),
        ("ABBN", "Six Swiss Exchange Ag", "Switzerland", "ABBN.SW"),
        ("BIRG", "Irish Stock Exchange - All Market", "Ireland", "BIRG.IR"),
        ("KOMB", "Prague Stock Exchange", "Czech Republic", "KOMB.PR"),
        ("SAP", "Deutsche Boerse Xetra", "Germany", "SAP.DE"),
    ]
    for raw, exchange, location, expected in cases:
        got = _resolve_yf_symbol(raw, exchange, {}, location=location)
        assert got == expected, f"{raw!r} @ {exchange!r}: {got!r} != {expected!r}"


def test_resolver_nordic_group_disambiguates_by_location():
    assert _resolve_yf_symbol("XXX", "Nasdaq Omx Nordic", {},
                              location="Denmark") == "XXX.CO"
    assert _resolve_yf_symbol("XXX", "Nasdaq Omx Nordic", {},
                              location="Finland") == "XXX.HE"
    # Iceland listings have no reliable yfinance data — row skipped.
    assert _resolve_yf_symbol("XXX", "Nasdaq Omx Nordic", {},
                              location="Iceland") is None
    # Unknown / missing location defaults to Stockholm (observed reality:
    # every sampled row 2018-2026 was a Stockholm listing).
    assert _resolve_yf_symbol("XXX", "Nasdaq Omx Nordic", {}) == "XXX.ST"


def test_resolver_skips_unlisted_placeholder_rows():
    assert _resolve_yf_symbol("GHOST", "NO MARKET (E.G. UNLISTED)", {},
                              location="United Kingdom") is None


def test_resolver_lse_slash_notation():
    # iShares prints LSE codes ending in "." with a trailing slash; interior
    # slashes are share classes. Both are FTSE heavyweights that were dead
    # columns for the full history before this rule.
    assert _resolve_yf_symbol("BA/", "London Stock Exchange", {}) == "BA.L"
    assert _resolve_yf_symbol("NG/", "London Stock Exchange", {}) == "NG.L"
    assert _resolve_yf_symbol("BT/A", "London Stock Exchange", {}) == "BT-A.L"


def test_resolver_regressions_unchanged():
    # Pre-fix behaviour that must not move.
    assert _resolve_yf_symbol("HSBA", "London Stock Exchange", {}) == "HSBA.L"
    assert _resolve_yf_symbol("BP.L", "London Stock Exchange", {}) == "BP.L"
    assert _resolve_yf_symbol("DBK", "Xetra", {}) == "DBK.DE"
    assert _resolve_yf_symbol("BRK.B", "New York Stock Exchange Inc.", {}) == "BRK-B"
    assert _resolve_yf_symbol("BRK.B", None, {}) == "BRK-B"


def test_normalise_for_yfinance_preserves_new_suffixes():
    assert normalise_for_yfinance("KOMB.PR") == "KOMB.PR"
    assert normalise_for_yfinance("SEB-A.ST") == "SEB-A.ST"
    assert normalise_for_yfinance("NDA-FI.HE") == "NDA-FI.HE"
    assert normalise_for_yfinance("BRK.B") == "BRK-B"


def test_parse_holdings_resolves_nordic_rows_end_to_end():
    body = (
        'Fund Holdings as of,"12-May-2023"\n'
        "\n"
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),"
        "Notional Value,Shares,Price,Location,Exchange,Market Currency\n"
        '"HSBA","HSBC HOLDINGS PLC","Banks","Equity","1","10","1","1","1",'
        '"United Kingdom","London Stock Exchange","GBP"\n'
        '"NDA FI","NORDEA BANK","Banks","Equity","1","10","1","1","1",'
        '"Sweden","Nasdaq Omx Helsinki Ltd.","EUR"\n'
        '"SEB A","SKANDINAVISKA ENSKILDA BANKEN","Banks","Equity","1","10",'
        '"1","1","1","Sweden","Nasdaq Omx Nordic","SEK"\n'
        '"GHOST","DEAD UNLISTED LINE","Banks","Equity","1","0","1","1","1",'
        '"United Kingdom","NO MARKET (E.G. UNLISTED)","GBP"\n'
        '"EUR","EUR CASH","Cash and/or Derivatives","Cash","1","0","1","1",'
        '"1","European Union","-","EUR"\n'
        "\n"
        "Disclosure text follows.\n"
    )
    got = parse_holdings(body, apply_exchange_suffix=True)
    assert got == ["HSBA.L", "NDA-FI.HE", "SEB-A.ST"]


# ---------------------------------------------------------------------------
# 3. Trading-calendar selection
# ---------------------------------------------------------------------------


def test_registry_europe_sector_funds_use_xetr():
    for sym in ["EXV1", "EXH1", "EXV3", "EXH3", "EXH9"]:
        assert get_etf(sym).get("trading_calendar") == "XETR", sym
    # US-constituent funds keep the NYSE default (field absent).
    assert "trading_calendar" not in get_etf("SOXX")
    assert "trading_calendar" not in get_etf("CSP1")


def test_xetr_grid_keeps_european_days_and_drops_us_only_holidays():
    import pandas_market_calendars as mcal

    xetr = mcal.get_calendar("XETR")
    sched = xetr.schedule(start_date="2023-01-01", end_date="2023-12-31")
    days = set(sched.index.strftime("%Y-%m-%d"))
    # 2023-07-04 was a Tuesday: US Independence Day, normal European session.
    # The old NYSE grid dropped it from European breadth series.
    assert "2023-07-04" in days
    # Easter Monday (2023-04-10) and May Day (2023-05-01) fell on weekdays
    # when European venues were shut; NYSE traded both. Month indexing here
    # is Python/pandas 1-indexed via ISO date strings.
    assert "2023-04-10" not in days
    assert "2023-05-01" not in days
