"""Observed-price recovery never invents prices or hides basis changes."""
import copy
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import observed_price_cache as opc

NOW = "2026-11-07T12:00:00Z"
DATES = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]


def prices(dates=DATES, close_only=False):
    close = np.arange(len(dates), dtype=float) + 100
    frame = pd.DataFrame({"Open": close - 1, "High": close + 2,
                          "Low": close - 2, "Close": close},
                         index=pd.to_datetime(dates))
    return frame[["Close"]] if close_only else frame


def settle(frame, **kwargs):
    params = dict(symbol="TEST", venue="NYSE", through=DATES[-1], now_utc=NOW,
                  fetch_window=lambda start, end: pd.DataFrame())
    params.update(kwargs)
    return opc.settle_observed_prices(frame, **params)


@pytest.mark.parametrize("missing", [2, 4])
def test_narrow_retry_complete_rows_and_no_mutation(missing):
    original = prices()
    raw = original.drop(original.index[missing])
    untouched = raw.copy(deep=True)
    calls = []
    def fetch(start, end):
        calls.append((start, end))
        return original.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1)]
    result, record = settle(raw, fetch_window=fetch)
    pd.testing.assert_frame_equal(result, original)
    pd.testing.assert_frame_equal(raw, untouched)
    assert len(calls) == 1
    assert calls[0][0] == DATES[missing - 1]
    assert record["retried_dates"] == [DATES[missing]]
    assert not record["retained_dates"]


def test_withdrawal_and_repeated_retention_preserve_origin():
    original, _ = settle(prices())
    original_attrs = copy.deepcopy(original.attrs)
    missing = prices().drop(pd.Timestamp(DATES[2]))
    first, record = settle(missing, cached=original)
    second, again = settle(missing, cached=first, now_utc="2026-11-08T12:00:00Z")
    assert record["retained_dates"] == again["retained_dates"] == [DATES[2]]
    assert not again["retried_dates"]
    row = second.attrs[opc.PROVENANCE_ATTR]["rows"][DATES[2]]
    assert row["origin"]["method"] == "vendor"
    assert row["acquired_at"] == original_attrs[opc.PROVENANCE_ATTR]["acquired_at"]
    assert original.attrs == original_attrs


@pytest.mark.parametrize("change", ["legacy", "value", "metadata", "symbol", "currency", "schema"])
def test_uncertified_or_mismatched_cache_refused(change):
    cache, _ = settle(prices())
    fresh = prices().drop(pd.Timestamp(DATES[2]))
    if change == "legacy":
        cache.attrs = {}
    elif change == "value":
        cache.iloc[2, 3] += 0.25
    elif change == "metadata":
        cache.attrs[opc.PROVENANCE_ATTR]["rows"][DATES[2]]["method"] = "retry"
    elif change == "symbol":
        cache, _ = settle(prices(), symbol="OTHER")
    elif change == "currency":
        base = prices()
        base.attrs["currency"] = "EUR"
        cache, _ = settle(base)
        fresh.attrs["currency"] = "USD"
    elif change == "schema":
        cache = opc.project_close(cache)
    result, record = settle(fresh, cached=cache)
    assert DATES[2] in record["unresolved_dates"]
    assert not record["retained_dates"]
    assert record["refusals"]


@pytest.mark.parametrize("factor", [0.5, 0.999])
def test_split_or_dividend_rebase_never_rescales_or_retains(factor):
    cache, _ = settle(prices())
    fresh = (prices() * factor).drop(pd.Timestamp(DATES[2]))
    result, record = settle(fresh, cached=cache, fetch_window=lambda s, e: prices())
    pd.testing.assert_frame_equal(result, fresh)
    assert not record["retained_dates"] and not record["retried_dates"]


def test_vendor_correction_wins_even_when_it_blocks_retention():
    cache, _ = settle(prices())
    fresh = prices().drop(pd.Timestamp(DATES[2]))
    fresh.iloc[0] += 0.1
    result, record = settle(fresh, cached=cache)
    pd.testing.assert_frame_equal(result, fresh)
    assert record["unresolved_dates"] == [DATES[2]]


def test_missing_cell_replaced_only_by_matching_whole_row():
    fresh = prices()
    fresh.loc[DATES[2], "Close"] = np.nan
    result, record = settle(fresh, fetch_window=lambda s, e: prices())
    pd.testing.assert_frame_equal(result, prices())
    assert record["retried_dates"] == [DATES[2]]
    different = prices()
    different.loc[DATES[2], "Open"] += 0.5
    result, record = settle(fresh, fetch_window=lambda s, e: different)
    assert pd.isna(result.loc[DATES[2], "Close"])


@pytest.mark.parametrize("value", [np.inf, -np.inf, 0, -1])
def test_bad_numeric_prices_raise(value):
    fresh = prices()
    fresh.loc[DATES[2], "Close"] = value
    with pytest.raises(ValueError):
        settle(fresh)


def test_bad_ohlc_relationship_raises():
    fresh = prices()
    fresh.loc[DATES[2], "High"] = 1
    with pytest.raises(ValueError, match="OHLC"):
        settle(fresh)


def test_intraday_and_non_session_rejected():
    with pytest.raises(ValueError, match="completed"):
        settle(prices(), now_utc="2026-11-06T15:00:00Z")
    with pytest.raises(ValueError):
        settle(prices(["2026-11-01", "2026-11-06"]))
    with pytest.raises((RuntimeError, ValueError)):
        settle(prices(), venue="NOT_A_VENUE")


def test_xetra_us_holiday_differences():
    dates = ["2026-07-02", "2026-07-03", "2026-07-06"]
    fresh = prices(dates).drop(pd.Timestamp("2026-07-03"))
    _, us = settle(fresh, through=dates[-1])
    _, de = settle(fresh, venue="XETR", through=dates[-1])
    assert us["unresolved_dates"] == []
    assert de["unresolved_dates"] == ["2026-07-03"]


@pytest.mark.parametrize("dates", [
    ["2026-04-30", "2026-05-01", "2026-05-04"],
    ["2025-12-31", "2026-01-02", "2026-01-05"],
])
def test_month_and_year_boundaries(dates):
    original = prices(dates)
    _, record = settle(original.drop(original.index[1]), through=dates[-1],
                       fetch_window=lambda s, e: original)
    assert record["retried_dates"] == [dates[1]]


def test_timeout_is_recorded_not_served_but_certified_cache_can_retain(monkeypatch):
    original, _ = settle(prices())
    monkeypatch.setattr(opc, "RETRY_DEADLINE_SECONDS", 0.01)
    def slow(start, end):
        time.sleep(0.1)
        return prices()
    result, record = settle(prices().iloc[:-1], cached=original, fetch_window=slow)
    assert "StallTimeout" in record["retry_error"]
    assert record["retained_dates"] == [DATES[-1]]
    assert record["retried_dates"] == []
    pd.testing.assert_frame_equal(result, prices())


def test_programming_errors_propagate():
    def broken(start, end):
        raise ValueError("broken schema")
    with pytest.raises(ValueError, match="broken schema"):
        settle(prices().iloc[:-1], fetch_window=broken)


def test_no_overlap_or_no_data_cannot_manufacture_prices():
    _, record = settle(prices().iloc[:-1], fetch_window=lambda s, e: prices().iloc[-1:])
    assert record["unresolved_dates"] == [DATES[-1]]
    empty = prices().iloc[:0]
    result, _ = settle(empty, cached=prices())
    assert result.empty


def test_close_projection_verifies_and_rebinds_full_frame():
    certified, _ = settle(prices())
    close = opc.project_close(certified)
    result, record = settle(prices(close_only=True).iloc[:-1], cached=close)
    assert record["retained_dates"] == [DATES[-1]]
    pd.testing.assert_frame_equal(result, prices(close_only=True))
    certified.iloc[0, 0] += 0.1
    with pytest.raises(ValueError, match="modified"):
        opc.project_close(certified)


def test_old_gaps_not_retried_and_cached_outside_span_not_restored():
    dates = pd.bdate_range("2026-10-01", DATES[-1])
    cache, _ = settle(prices(dates))
    fresh = prices(dates).drop(pd.Timestamp("2026-10-02"))
    def unexpected(start, end):
        raise AssertionError("Historical gap must not trigger retry")
    _, record = settle(fresh, fetch_window=unexpected)
    assert record["unresolved_dates"] == ["2026-10-02"]
    _, record = settle(prices(dates).loc[DATES[0]:], cached=cache, fetch_window=unexpected)
    assert record["retained_dates"] == []


def test_retry_wrong_symbol_and_currency_not_accepted():
    fresh = prices().iloc[:-1]
    for key, value in [("symbol", "OTHER"), ("currency", "EUR")]:
        retry = prices()
        retry.attrs[key] = value
        _, record = settle(fresh, fetch_window=lambda s, e: retry)
        assert record["retried_dates"] == []
        assert record["unresolved_dates"] == [DATES[-1]]


def test_cache_acquired_after_run_refused():
    cache, _ = settle(prices(), now_utc="2026-11-08T12:00:00Z")
    _, record = settle(prices().iloc[:-1], cached=cache)
    assert record["retained_dates"] == []
    assert "later than" in record["refusals"][0]


def test_nan_price_not_certified_as_observed_or_retained():
    fresh = prices()
    fresh.loc[DATES[-1], "Close"] = np.nan
    cache, record = settle(fresh)
    assert record["unresolved_dates"] == [DATES[-1]]
    assert DATES[-1] not in cache.attrs[opc.PROVENANCE_ATTR]["rows"]
    _, record = settle(prices().iloc[:-1], cached=cache)
    assert record["retained_dates"] == []


def test_pinned_china_closures_never_requested_or_certified(monkeypatch):
    import china_market_holidays
    # Isolate the validated-pin interface. This fixture is not a holiday notice.
    monkeypatch.setattr(china_market_holidays, "checked_pins",
                        lambda cal, venue: pd.DatetimeIndex(["2027-01-01"]))
    fresh = prices(["2026-12-31", "2027-01-04"])
    def unexpected(start, end):
        raise AssertionError("Pinned closure must not trigger missing-session retry")
    _, record = settle(fresh, venue="XSHG", through="2027-01-04",
                       now_utc="2027-01-05T12:00:00Z",
                       fetch_window=unexpected)
    assert record["unresolved_dates"] == []
    fabricated = prices(["2026-12-31", "2027-01-01", "2027-01-04"])
    with pytest.raises(ValueError, match="completed venue session"):
        settle(fabricated, venue="XSHG", through="2027-01-04",
               now_utc="2027-01-05T12:00:00Z", fetch_window=unexpected)


@pytest.mark.parametrize("later", ["2026-11-06", "2026-11-07"])
def test_non_session_screen_does_not_hide_unfinished_or_future_rows(later):
    fresh = prices(["2026-11-05", later])
    with pytest.raises(ValueError, match="completed venue session"):
        settle(fresh, through="2026-11-05", now_utc="2026-11-06T10:00:00Z",
               exclude_non_sessions=True)


def test_opted_acquisition_audits_retry_holiday_without_creating_observation():
    dates = ["2017-10-02", "2017-10-03", "2017-10-04"]
    retry = prices(dates)
    fresh = retry.iloc[:1].copy()
    out, record = settle(fresh, venue="XETR", through="2017-10-04",
                         now_utc="2017-10-05T12:00:00Z",
                         fetch_window=lambda s, e: retry,
                         exclude_non_sessions=True)
    assert record["excluded_retry_non_session_dates"] == ["2017-10-03"]
    assert record["retried_dates"] == ["2017-10-04"]
    assert list(out.index) == list(pd.to_datetime([dates[0], dates[-1]]))
    assert dates[1] not in out.attrs[opc.PROVENANCE_ATTR]["rows"]
    assert out.attrs[opc.PROVENANCE_ATTR]["excluded_retry_non_session_dates"] == [dates[1]]


def test_non_session_screen_never_sanitises_cached_evidence():
    original = prices(["2026-04-02", "2026-04-06", "2026-04-07"])
    cached, _ = settle(original, through="2026-04-07", now_utc="2026-04-08T12:00:00Z")
    # A bound record is still invalid if its calendar semantics are wrong.
    cached.loc[pd.Timestamp("2026-04-03")] = original.iloc[0]
    cached = cached.sort_index()
    metadata = copy.deepcopy(cached.attrs[opc.PROVENANCE_ATTR])
    metadata.pop("digest")
    metadata["rows"]["2026-04-03"] = dict(metadata["rows"]["2026-04-02"])
    cached.attrs[opc.PROVENANCE_ATTR] = dict(metadata, digest=opc._digest(cached, metadata))
    out, record = settle(original.iloc[:-1], through="2026-04-07",
                         now_utc="2026-04-08T12:00:00Z", cached=cached,
                         exclude_non_sessions=True)
    assert record["retained_dates"] == []
    assert record["unresolved_dates"] == ["2026-04-07"]
    assert "not a completed venue session" in record["refusals"][0]
    assert pd.Timestamp("2026-04-03") not in out.index
