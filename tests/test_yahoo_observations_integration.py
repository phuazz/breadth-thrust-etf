"""Integration: the engine capture survives a later Yahoo withdrawal in live NAV."""
import sys
from pathlib import Path
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import yahoo_observations as yo


NOW = pd.Timestamp("2026-11-07T09:00:00Z").to_pydatetime()
DATES = pd.to_datetime(["2026-11-02", "2026-11-03", "2026-11-04",
                        "2026-11-05", "2026-11-06"])


def prices():
    return pd.DataFrame({k: [100., 101., 102., 103., 104.]
                         for k in ["Open", "High", "Low", "Close"]}, index=DATES)


def no_answer(start, end):
    return pd.DataFrame(columns=["Open", "High", "Low", "Close"],
                        index=pd.DatetimeIndex([]))


def test_shared_close_cache_preserves_engine_capture_for_live_consumer(tmp_path):
    original = prices()
    yo.settle_native_yahoo(original, "EXH1.DE", tmp_path, now_utc=NOW,
                           fetch_window=no_answer)
    assert (tmp_path / "exh1.de_yahoo_observed_close.parquet").exists()
    withdrawn = original.iloc[:-1][["Close"]]
    out = yo.settle_native_yahoo(withdrawn, "EXH1.DE", tmp_path, now_utc=NOW,
                                 fetch_window=no_answer)
    assert out.loc[DATES[-1], "Close"] == 104.
    assert out.attrs["observed_price_provenance"]


def test_legacy_engine_cache_cannot_supply_observation_evidence(tmp_path):
    prices().to_parquet(tmp_path / "exh1.de_ohlc_cache.parquet")
    out = yo.settle_native_yahoo(prices().iloc[:-1], "EXH1.DE", tmp_path,
                                 now_utc=NOW, fetch_window=no_answer)
    assert DATES[-1] not in out.dropna().index


def test_rebased_fetch_does_not_retain_old_vintage(tmp_path):
    yo.settle_native_yahoo(prices(), "EXH1.DE", tmp_path, now_utc=NOW,
                           fetch_window=no_answer)
    out = yo.settle_native_yahoo(prices().iloc[:-1] * .99, "EXH1.DE",
                                 tmp_path, now_utc=NOW, fetch_window=no_answer)
    assert DATES[-1] not in out.dropna().index


def test_incomplete_row_never_lands_in_observation_cache(tmp_path):
    # Xetra is still open at this instant.
    out = yo.settle_native_yahoo(
        prices(), "EXH1.DE", tmp_path,
        now_utc=pd.Timestamp("2026-11-06T10:00:00Z").to_pydatetime(),
        fetch_window=no_answer)
    assert DATES[-1] not in out.index
    assert DATES[-1] not in pd.read_parquet(
        tmp_path / "exh1.de_yahoo_observed_ohlc.parquet").index


def test_short_consumer_request_does_not_shrink_raw_archive(tmp_path):
    yo.settle_native_yahoo(prices(), "EXH1.DE", tmp_path, now_utc=NOW,
                           fetch_window=no_answer)
    path = tmp_path / "exh1.de_yahoo_observed_close.parquet"
    before = path.read_bytes()
    yo.settle_native_yahoo(prices().iloc[-2:][["Close"]], "EXH1.DE",
                           tmp_path, now_utc=NOW, fetch_window=no_answer)
    assert path.read_bytes() == before


@pytest.mark.parametrize("symbol", ["SPY", "BTC-USD", "EURUSD=X", "UNKNOWN.L"])
def test_unrelated_source_contracts_unchanged(tmp_path, symbol):
    original = prices()
    assert yo.settle_native_yahoo(original, symbol, tmp_path,
                                  now_utc=NOW, fetch_window=no_answer) is original
    assert not list(tmp_path.iterdir())


def test_atomic_cache_write_failure_leaves_existing_frame(tmp_path, monkeypatch):
    path = tmp_path / "cache.parquet"
    prices().to_parquet(path)
    before = path.read_bytes()
    monkeypatch.setattr(yo.os, "replace",
                        lambda *a: (_ for _ in ()).throw(PermissionError("busy")))
    with pytest.raises(PermissionError):
        yo._write(path, prices() * 2)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))



@pytest.mark.parametrize("end", ["2026-11-08", "2026-11-09"])
def test_exclusive_weekend_end_resolves_to_friday(tmp_path, end):
    out = yo.settle_native_yahoo(prices(), "EXH1.DE", tmp_path,
                                 now_utc=NOW, end=end, fetch_window=no_answer)
    assert out.index[-1] == DATES[-1]


def test_union_padding_does_not_count_as_a_native_observation(tmp_path):
    native = pd.DataFrame({"Close": [100., 101.]},
                          index=pd.to_datetime(["2026-09-30", "2026-10-09"]))
    union = native.reindex(pd.bdate_range("2026-09-30", "2026-10-09"))
    out = yo.settle_native_yahoo(
        union, "159801.SZ", tmp_path,
        now_utc=pd.Timestamp("2026-10-10T09:00:00Z").to_pydatetime(),
        through="2026-10-07", fetch_window=no_answer)
    # Golden Week padding must not be certified as an observed Chinese close.
    assert list(out.index) == [pd.Timestamp("2026-09-30")]


def test_native_symbol_lock_refuses_overlapping_writer(tmp_path):
    path = tmp_path / "symbol.lock"
    with yo._observation_lock(path):
        with pytest.raises(RuntimeError, match="already has a writer"):
            with yo._observation_lock(path):
                pytest.fail("concurrent writer admitted")
    with yo._observation_lock(path):
        pass


def test_live_nav_fetch_reuses_engine_capture_before_fx(tmp_path, monkeypatch):
    import mark_to_market_live as mtm
    import yfinance as yf
    monkeypatch.setattr(mtm, "DATA_DIR", tmp_path)
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(mtm, "datetime", Clock)
    original = prices()
    yo.settle_native_yahoo(original, "EXH1.DE", tmp_path,
                           now_utc=NOW, fetch_window=no_answer)
    batch = original.iloc[:-1].copy()
    batch.columns = pd.MultiIndex.from_product([["EXH1.DE"], batch.columns])
    monkeypatch.setattr(yf, "download", lambda *a, **k: batch.copy())
    class Ticker:
        def history(self, **kwargs):
            return original.iloc[:-1].copy()
    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    out = mtm._yf_close_series(["EXH1.DE"], "2026-11-02", "2026-11-09")
    assert out.loc[DATES[-1], "EXH1.DE"] == 104.


def test_engine_fetch_uses_certified_raw_cache_on_withdrawal(tmp_path, monkeypatch):
    import backtest as bt
    import yfinance as yf
    monkeypatch.setenv("BTE_PRICE_SOURCE", "yfinance")
    monkeypatch.setattr(bt, "paths_for",
                        lambda etf: {"ohlc_cache": tmp_path / "engine.parquet"})
    original = prices()
    yo.settle_native_yahoo(original, "EXH1.DE", tmp_path,
                           now_utc=NOW, fetch_window=no_answer)
    monkeypatch.setattr(yf, "download", lambda *a, **k: original.iloc[:-1].copy())
    class Ticker:
        def history(self, **kwargs):
            return original.iloc[:-1].copy()
    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    out = bt.download_soxx_ohlc(
        "2026-11-02", "2026-11-09", etf="EXH1.DE",
        completed_through=pd.Timestamp("2026-11-06"))
    assert out.loc[DATES[-1], "Close"] == 104.



# Reuse the realistic native/NYSE/FX fixture without disabling raw recovery.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_native_us_holiday_load_path import load  # noqa: E402, F401
import test_native_us_holiday_load_path as native_fixture  # noqa: E402


@pytest.mark.parametrize("raw_evidence", ["valid", "corrupt", "absent"])
def test_thematic_load_uses_only_certified_native_observations(load, raw_evidence):
    th, first, derived_cache = load()
    raw_path = derived_cache.parent / "159801.sz_yahoo_observed_close.parquet"
    assert raw_path.exists()
    if raw_evidence == "corrupt":
        raw = pd.read_parquet(raw_path)
        raw.loc[native_fixture.GOOD_FRIDAY, "Close"] += .01
        raw.to_parquet(raw_path)  # mismatched frame/provenance, never usable
    elif raw_evidence == "absent":
        raw_path.unlink()
    derived_cache.unlink()  # force source acquisition, retaining only raw evidence
    th, second, _ = load(withdraw_good_friday=True)
    if raw_evidence == "valid":
        assert native_fixture.GOOD_FRIDAY in th.NATIVE_OBSERVATIONS["159801.SZ"]
        assert second.at[native_fixture.QINGMING_MONDAY, "159801.SZ"] == pytest.approx(
            first.at[native_fixture.QINGMING_MONDAY, "159801.SZ"])
    else:
        assert native_fixture.GOOD_FRIDAY not in th.NATIVE_OBSERVATIONS["159801.SZ"]
        spec = th.native_inputs()["159801.SZ"]
        import native_sessions
        check = native_sessions.provenance(
            second.index, spec["observed"], spec["venue"],
            window_rows=th.MA_PERIOD)
        assert not bool(check.iloc[-1]["current"])


def test_older_acquisition_cannot_overwrite_newer_archive(tmp_path):
    newer = prices() * 1.01
    later = pd.Timestamp("2026-11-07T10:00:00Z").to_pydatetime()
    yo.settle_native_yahoo(newer, "EXH1.DE", tmp_path, now_utc=later,
                           fetch_window=no_answer)
    path = tmp_path / "exh1.de_yahoo_observed_ohlc.parquet"
    before = path.read_bytes()
    out = yo.settle_native_yahoo(prices(), "EXH1.DE", tmp_path,
                                 now_utc=NOW, fetch_window=no_answer)
    assert out.loc[DATES[-1], "Close"] == 104.
    assert path.read_bytes() == before
    close = pd.read_parquet(tmp_path / "exh1.de_yahoo_observed_close.parquet")
    assert close.loc[DATES[-1], "Close"] == pytest.approx(105.04)


def test_final_holdings_refetch_uses_capture_without_fabricating_ohlc(tmp_path, monkeypatch):
    import export_holdings_prices as holdings
    import yfinance as yf
    monkeypatch.setattr(holdings, "DATA_DIR", tmp_path)
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(holdings, "datetime", Clock)
    original = prices()
    yo.settle_native_yahoo(original, "EXH1.DE", tmp_path,
                           now_utc=NOW, fetch_window=no_answer)
    cache = tmp_path / "exh1.de_ohlc_cache.parquet"
    original.to_parquet(cache)
    before = cache.read_bytes()
    batch = original.iloc[:-1].copy()
    batch.columns = pd.MultiIndex.from_product([["EXH1.DE"], batch.columns])
    monkeypatch.setattr(yf, "download", lambda *a, **k: batch.copy())
    class Ticker:
        def history(self, **kwargs):
            return original.iloc[:-1].copy()
    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    out = holdings.fetch_missing_from_yfinance(["EXH1.DE"])
    assert out["EXH1.DE"].loc[DATES[-1]] == 104.
    assert cache.read_bytes() == before


def test_flat_response_cannot_seed_durable_recovery_evidence(tmp_path):
    flat = prices() * 0 + 100.
    yo.settle_native_yahoo(flat, "EXH1.DE", tmp_path, now_utc=NOW,
                           fetch_window=no_answer)
    assert not list(tmp_path.glob("*.parquet"))


@pytest.mark.parametrize("symbol", ["EXH1.DE", "EXH4.DE", "EXH9.DE", "EXV1.DE", "EXV3.DE"])
def test_full_xetra_history_excludes_historical_vendor_holidays(tmp_path, capsys, symbol):
    # Official Xetra calendars: these were settlement-only, not trading days.
    # 2017: cashmarket.deutsche-boerse.com/resource/blob/336572/247517da046f56e5b4dae39723f29b00/data/trading-calendar-2017.pdf
    # 2018: cashmarket.deutsche-boerse.com/resource/blob/154430/366c1c9be4ce7cdb923bbf246db1d1bf/data/trading-calendar-2018.pdf
    holidays = pd.to_datetime(["2017-10-03", "2017-10-31", "2018-05-21"])
    sessions = yo.effective_calendar("XETR").schedule(
        start_date="2017-06-30", end_date="2026-11-06").index
    assert not holidays.isin(sessions).any()
    observed = pd.DataFrame({field: [100. + n / 100 for n in range(len(sessions))]
                             for field in ["Open", "High", "Low", "Close"]}, index=sessions)
    raw = observed.copy()
    for day in holidays:
        raw.loc[day] = 101.
    raw = raw.sort_index()
    untouched = raw.copy(deep=True)
    out = yo.settle_native_yahoo(raw, symbol, tmp_path, now_utc=NOW,
                                 fetch_window=no_answer)
    pd.testing.assert_frame_equal(out, observed, check_freq=False)
    pd.testing.assert_frame_equal(raw, untouched)
    record = out.attrs["observed_price_provenance"]
    assert record["excluded_non_session_dates"] == [d.date().isoformat() for d in holidays]
    assert not holidays.isin(pd.to_datetime(list(record["rows"]))).any()
    assert out.index[-1] == pd.Timestamp("2026-11-06")
    archive = pd.read_parquet(tmp_path / f"{symbol.lower()}_yahoo_observed_ohlc.parquet")
    pd.testing.assert_frame_equal(archive, out)
    assert "excluded non-trading dates" in capsys.readouterr().out


@pytest.mark.parametrize("dates", [
    ["2017-10-30", "2017-10-31", "2017-11-01"],
    ["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"],
])
def test_acquisition_calendar_filter_month_and_year_boundaries(tmp_path, dates):
    raw = pd.DataFrame({"Close": range(100, 100 + len(dates))}, index=pd.to_datetime(dates))
    out = yo.settle_native_yahoo(raw, "EXH1.DE", tmp_path,
                                 now_utc=NOW, through=dates[-1], fetch_window=no_answer)
    assert list(out.index) == list(pd.to_datetime([dates[0], dates[-1]]))
    assert out.attrs["observed_price_provenance"]["excluded_non_session_dates"] == dates[1:-1]


def test_holiday_only_response_is_not_cached(tmp_path, capsys):
    raw = pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2017-10-03"]))
    out = yo.settle_native_yahoo(raw, "EXH1.DE", tmp_path, now_utc=NOW,
                                 through="2017-10-04", fetch_window=no_answer)
    assert out.empty
    assert not list(tmp_path.glob("*.parquet"))
    assert "2017-10-03" in capsys.readouterr().out


def test_after_cutoff_is_reported_separately_from_non_trading_filter(tmp_path, capsys):
    out = yo.settle_native_yahoo(prices(), "EXH1.DE", tmp_path,
                                 now_utc=pd.Timestamp("2026-11-06T10:00:00Z"),
                                 fetch_window=no_answer)
    assert DATES[-1] not in out.index
    log = capsys.readouterr().out
    assert "cutoff: excluded after 2026-11-05: ['2026-11-06']" in log
    assert "excluded non-trading dates" not in log



def test_engine_cache_refresh_handles_complete_history_with_off_session_rows(tmp_path, monkeypatch):
    import export_holdings_prices as holdings
    monkeypatch.setattr(holdings, "DATA_DIR", tmp_path)
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(holdings, "datetime", Clock)
    sessions = yo.effective_calendar("XETR").schedule(
        start_date="2017-06-30", end_date="2026-11-06").index
    observed = pd.DataFrame({field: [100. + n / 100 for n in range(len(sessions))]
                             for field in ["Open", "High", "Low", "Close"]}, index=sessions)
    raw = observed.copy()
    for day in ["2017-10-03", "2017-10-31", "2018-05-21"]:
        raw.loc[pd.Timestamp(day)] = 101.
    raw = raw.sort_index()
    symbols = {s: s[:-3] for s in ["EXH1.DE", "EXH4.DE", "EXH9.DE", "EXV1.DE", "EXV3.DE"]}
    for symbol in symbols:
        # Existing unbound engine caches may still contain the holiday rows.
        raw.to_parquet(tmp_path / f"{symbol.lower()}_ohlc_cache.parquet")
    monkeypatch.setattr(holdings, "engine_cache_window", lambda key: ("2017-06-30", "2026-11-07"))
    monkeypatch.setattr(holdings, "_download_ohlc", lambda *args: {s: raw.copy() for s in symbols})
    assert holdings.refresh_ohlc_caches(symbols) == 0
    for symbol in symbols:
        updated = pd.read_parquet(tmp_path / f"{symbol.lower()}_ohlc_cache.parquet")
        pd.testing.assert_frame_equal(updated, observed, check_freq=False)
        assert len(updated.attrs["observed_price_provenance"]["excluded_non_session_dates"]) == 3



def test_invalid_ohlc_reports_symbol_date_and_does_not_publish_cache(tmp_path):
    raw = prices()
    raw.loc[DATES[2], "High"] = 101.
    with pytest.raises(ValueError, match="EXH1.DE: Invalid OHLC relationships at 2026-11-04") as failure:
        yo.settle_native_yahoo(raw, "EXH1.DE", tmp_path, now_utc=NOW,
                               fetch_window=no_answer)
    assert "High=101" in str(failure.value)
    assert not list(tmp_path.glob("*.parquet"))
