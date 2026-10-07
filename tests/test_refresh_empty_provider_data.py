"""Provider outages must not fabricate results or prevent a licensed fetch."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import norgate_prices as npx
import price_source as ps
import run_portfolio as rp


@pytest.mark.parametrize("with_columns", [False, True])
def test_empty_yahoo_base_still_fetches_norgate(monkeypatch, with_columns):
    index = pd.date_range("2024-01-02", periods=3)
    ng = pd.DataFrame({"SPY": [100., 101., 102.]}, index=index)
    calls = []
    monkeypatch.setattr(npx, "available", lambda: True)

    def fetch(tickers, start, end, verbose=False):
        calls.append(tickers)
        return ng, ["SPY"], ["BTC-USD"]

    monkeypatch.setattr(npx, "fetch_closes", fetch)
    base = pd.DataFrame(columns=["SPY", "BTC-USD"] if with_columns else [])
    out, report = npx.select_columns(base, ["SPY", "BTC-USD"],
                                     "2024-01-01", "2024-01-05", verbose=False)
    assert calls == [["SPY", "BTC-USD"]]
    pd.testing.assert_series_equal(out["SPY"], ng["SPY"])
    assert out["BTC-USD"].isna().all()
    assert report["replaced"] == ["SPY"]
    assert report["unresolved"] == ["BTC-USD"]
    ps.assert_norgate_complete(report, ["SPY", "BTC-USD"], "test")
    with pytest.raises(RuntimeError, match="1 of 2"):
        ps.assert_norgate_complete(report, ["SPY", "SHY"], "test")


@pytest.mark.parametrize("values", [[], [np.nan] * 4,
                                     [100., 101., np.nan, 103.],
                                     [100., 101., 102., np.nan],
                                     [0., 101., 102., 103.],
                                     [100., np.inf, 102., 103.]])
def test_unavailable_spy_fails_before_output_write(monkeypatch, tmp_path, values,
                                                  capsys):
    index = pd.bdate_range("2023-01-02", periods=205)
    closes = pd.DataFrame({"SOXX": np.linspace(100., 120., len(index))}, index=index)
    breadths = pd.DataFrame({"SOXX": .5}, index=index)
    # Existing eligibility logic advances 200 calendar days from the first breadth.
    eligible = index[index >= index[0] + pd.Timedelta(days=rp.MA_PERIOD)][0]
    expected = index[index >= eligible]
    spy = pd.Series(100., index=expected, name="Close")
    if not values:
        spy = spy.iloc[:0]
    else:
        spy.iloc[:4] = values
    out = tmp_path / "portfolio_construction.json"
    out.write_text("previous verified report", encoding="utf-8")
    monkeypatch.setattr(rp, "OUT_PATH", out)
    monkeypatch.setattr(rp, "build_panels", lambda: (closes, breadths, ["SOXX"]))
    monkeypatch.setattr(rp, "download_spy_close", lambda *args: spy)
    monkeypatch.setattr(rp, "run_portfolio", lambda *a, **k: pytest.fail(
        "variants must not run without the benchmark"))
    assert rp.main() == 1
    assert "SPY benchmark unavailable" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "previous verified report"


def test_complete_spy_benchmark_is_unchanged():
    index = pd.bdate_range("2024-01-02", periods=4)
    spy = pd.Series([100., 101., 102., 103.], index=index)
    assert rp.spy_benchmark_problem(spy, index, index[0]) is None


def test_empty_base_does_not_weaken_unavailable_source_guard(monkeypatch):
    base = pd.DataFrame(columns=["SPY"])
    monkeypatch.setattr(npx, "available", lambda: False)
    monkeypatch.setattr(npx, "fetch_closes", lambda *a, **k: pytest.fail(
        "unavailable service must not be fetched"))
    out, report = npx.select_columns(base, ["SPY"], "2024-01-01",
                                     "2024-01-05", verbose=False)
    assert out is base and report["status"] == "unavailable"
    with pytest.raises(RuntimeError, match="0 of 1"):
        ps.assert_norgate_complete(report, ["SPY"], "test")


def test_empty_base_with_no_served_columns_still_fails_closed(monkeypatch):
    base = pd.DataFrame(columns=["SPY"])
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(npx, "fetch_closes", lambda *a, **k: (
        pd.DataFrame(), [], ["SPY"]))
    out, report = npx.select_columns(base, ["SPY"], "2024-01-01",
                                     "2024-01-05", verbose=False)
    assert out.empty and report["unresolved"] == ["SPY"]
    with pytest.raises(RuntimeError, match="0 of 1"):
        ps.assert_norgate_complete(report, ["SPY"], "test")


def test_strict_engine_can_build_from_norgate_when_yahoo_is_empty(monkeypatch,
                                                                tmp_path):
    import run_asset_class_rotation as ac
    index = pd.bdate_range("2024-01-02", periods=250)
    needed = ac.TICKERS + ac.CASH_ONLY_TICKERS
    ng = pd.DataFrame({t: np.linspace(100., 120., len(index)) for t in needed},
                      index=index)
    cache = tmp_path / "asset_class_prices_cache.parquet"
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setattr(ac, "PRICE_CACHE", cache)
    monkeypatch.setattr(ac, "START_DATE", "2024-01-01")
    monkeypatch.setattr(ac, "END_DATE", "2025-01-01")
    monkeypatch.setattr(ac, "last_completed_session", lambda now: index[-1].date())
    monkeypatch.setattr(ac, "cap_to_last_completed_session", lambda df: df)
    monkeypatch.setattr(ac.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(npx, "fetch_closes", lambda tickers, *a, **k: (
        ng, list(tickers), []))
    out = ac.download_prices()
    pd.testing.assert_frame_equal(out[needed], ng[needed])
    assert ps.read_cache_source(cache) == "norgate"
    assert set(ps.read_cache_sidecar(cache)["columns_from_norgate"]) == set(needed)
