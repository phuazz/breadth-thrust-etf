"""Real roster aliases must price without concealing genuine vendor gaps."""
from datetime import date
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compute_breadth as cb
import norgate_symbols as ns
import fetch_constituents as fc
from capture_status import describe_capture


@pytest.mark.parametrize("roster,vendor", [
    ("BF B", "BF-B"), ("BRK B", "BRK-B"), ("BFB", "BF-B"),
    ("BRK.B", "BRK-B"), ("135.HK", "0135.HK"),
    ("20.HK", "0020.HK"), ("0135.HK", "0135.HK"),
    ("12345.HK", "12345.HK"), ("BT.A.L", "BT-A.L"),
    ("BT-A.L", "BT-A.L"), ("BP.L", "BP.L"),
    ("HOLX", "HOLX"), ("HOLX US", "HOLX US"),
    ("VSNTV UW", "VSNTV UW"), ("UNKNOWN B", "UNKNOWN B"),
])
def test_only_supported_aliases_are_normalised(roster, vendor):
    assert cb.normalise_for_yfinance(roster) == vendor


def test_download_preserves_both_roster_aliases_and_real_holes(tmp_path, monkeypatch):
    # Python/pandas months are 1-indexed. Span a year boundary; missing bars
    # stay missing even when two roster names share the same vendor series.
    idx = pd.to_datetime(["2025-12-31", "2026-01-02"])
    frame = pd.DataFrame({"BF-B": [25., 26.], "BRK-B": [500., np.nan],
                          "0135.HK": [7., 7.1], "BT-A.L": [180., 181.]}, index=idx)
    wanted = ["BF B", "BFB", "BF-B", "BRK B", "BRK-B", "135.HK",
              "0135.HK", "BT.A.L", "MISSING"]
    def download(symbols, **kwargs):
        assert len(symbols) == len(set(symbols))
        assert set(symbols) == set(frame.columns) | {"MISSING"}
        return pd.concat({"Close": frame}, axis=1)
    monkeypatch.setattr(cb.yf, "download", download)
    monkeypatch.setattr(cb, "run_with_deadline", lambda fn, **kw: fn())
    monkeypatch.setattr(cb, "_splits_for", lambda *a: pd.Series(dtype=float))
    monkeypatch.setattr(cb, "DECLARATIONS_PATH", tmp_path / "none.json")
    out = cb.download_prices(wanted, "2025-12-31", "2026-01-03",
        cache_path=tmp_path / "data" / "prices.parquet", tail_probe=False)
    for t in wanted:
        vendor = cb.normalise_for_yfinance(t)
        if vendor in frame:
            pd.testing.assert_series_equal(out[t], frame[vendor], check_names=False)
        else:
            assert out[t].isna().all()
    capture = describe_capture({"snapshots": {"2026-01-02": {"tickers": wanted}}},
                               out, idx[-1], "2026-01-02", "yfinance")
    assert capture["no_price_history"] == ["MISSING"]
    assert capture["behind_required_session"] == {
        "BRK B": "2025-12-31", "BRK-B": "2025-12-31"}


@pytest.mark.parametrize("asof", [date(2026, 1, 31), date(2026, 2, 1),
                                 date(2025, 12, 31), date(2026, 1, 1)])
def test_norgate_aliases_keep_quotation_window_guards(monkeypatch, asof):
    monkeypatch.setattr(ns, "_candidates", lambda: {})
    monkeypatch.setattr(ns, "_window", lambda s: (date(1996, 5, 9), date(2026, 1, 31)))
    for alias, canonical in [("BF B", "BF.B"), ("BF-B", "BF.B"),
                             ("BRK B", "BRK.B"), ("BRK-B", "BRK.B")]:
        assert ns.resolve(alias, asof) == (canonical if asof <= date(2026, 1, 31) else None)
    assert ns.resolve("HOLX US", asof) is None


@pytest.mark.parametrize("suffix", [False, True])
@pytest.mark.parametrize("exchange,expected", [
    ("NO MARKET (E.G. UNLISTED)", ["AAPL"]),
    ("NASDAQ", ["HOLX", "AAPL"]),
])
def test_unlisted_cvr_is_not_the_historical_common_stock(suffix, exchange, expected):
    # The issuer labels the CVR Equity but marks it UNLISTED. Filter by the
    # observed venue, never a blanket HOLX exclusion or an inferred date.
    points = {"asOfDate": {"value": "20261009"},
              "ticker": {"value": ["HOLX", "AAPL"]},
              "assetClass": {"value": ["Equity", "Equity"]},
              "exchange": {"value": [exchange, "NASDAQ"]}}
    payload = {"componentsByNameMap": {"holdings": {"containersByNameMap": {
        "all": {"dataPointsByNameMap": points}}}}}
    csv = ('Ticker,Name,Asset Class,Exchange\n'
           f'HOLX,Hologic,Equity,{exchange}\nAAPL,Apple,Equity,NASDAQ\n\n')
    assert fc.parse_holdings_json(payload, date(2026, 10, 9),
        apply_exchange_suffix=suffix) == expected
    assert fc.parse_holdings(csv, apply_exchange_suffix=suffix) == expected


def test_refresh_prices_preserves_history_and_does_not_fill_real_holes(tmp_path, monkeypatch):
    # Python months are 1-indexed; cover both month and year boundaries.
    idx = pd.to_datetime(["2025-12-30", "2025-12-31", "2026-01-02"])
    incumbent = pd.DataFrame({"AAA": [10., np.nan, 11.]}, index=idx)
    cache = tmp_path / "data" / "prices.parquet"
    cache.parent.mkdir()
    incumbent.to_parquet(cache)
    vendor = pd.DataFrame({"AAA": [np.nan, np.nan, 11.2]}, index=idx)
    calls = []
    def download(*args, **kwargs):
        calls.append(True)
        return pd.concat({"Close": vendor}, axis=1)
    monkeypatch.setattr(cb.yf, "download", download)
    monkeypatch.setattr(cb, "run_with_deadline", lambda fn, **kw: fn())
    monkeypatch.setattr(cb, "_splits_for", lambda *a: pd.Series(dtype=float))
    monkeypatch.setattr(cb, "DECLARATIONS_PATH", tmp_path / "none.json")
    monkeypatch.setattr(cb.price_source_mod, "cache_matches", lambda *a: True)
    out = cb.download_prices(["AAA"], "2025-12-30", "2026-01-02",
                             cache_path=cache, tail_probe=False, refresh=True)
    assert calls == [True]
    assert out.loc[idx[0], "AAA"] == 10.
    assert pd.isna(out.loc[idx[1], "AAA"])
    assert out.loc[idx[2], "AAA"] == 11.2
