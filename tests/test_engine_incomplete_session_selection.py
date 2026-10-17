"""An unfinished Yahoo row must not veto a complete Norgate selection.

Python datetime/pandas months are 1-indexed. The two cases cross a month
and a year boundary; no vendor or network service is called.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import norgate_prices as npx  # noqa: E402
import price_source as ps  # noqa: E402
import backtest as bt  # noqa: E402
import compute_breadth as cb  # noqa: E402
import run_asset_class_rotation as ac  # noqa: E402
import run_thematic_rotation as th  # noqa: E402


@pytest.mark.parametrize("complete,partial", [
    ("2024-01-31", "2024-02-01"),
    ("2024-12-31", "2025-01-02"),
])
@pytest.mark.parametrize("engine", ["B", "C"])
@pytest.mark.parametrize("served_complete", [True, False])
def test_partial_row_cannot_block_strict_norgate_take(
        monkeypatch, tmp_path, engine, complete, partial, served_complete):
    module = ac if engine == "B" else th
    needed = (ac.TICKERS + ac.CASH_ONLY_TICKERS if engine == "B"
              else th.TICKERS + [th.CASH_PROXY])
    idx = pd.bdate_range(end=complete, periods=4).append(
        pd.DatetimeIndex([partial]))
    frame = pd.DataFrame({t: np.linspace(100.0 + i, 104.0 + i, len(idx))
                          for i, t in enumerate(needed)}, index=idx)
    raw = pd.concat({t: pd.DataFrame({"Close": frame[t]}) for t in needed},
                    axis=1)
    monkeypatch.setattr(module, "PRICE_CACHE", tmp_path / f"{engine}.parquet")
    monkeypatch.setattr(module, "last_completed_session",
                        lambda now: pd.Timestamp(complete).date())
    monkeypatch.setattr(module, "cap_to_last_completed_session",
                        lambda df, now_utc=None: df.loc[:pd.Timestamp(complete)])
    monkeypatch.setattr(module, "yf", type("Y", (), {
        "download": staticmethod(lambda *a, **k: raw.copy())})())
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setattr(npx, "available", lambda: True)
    if engine == "C":
        monkeypatch.setattr(th, "_fx_convert_to_usd", lambda df: df)
        monkeypatch.setattr(th.completed_prices, "crypto_completed_through",
                            lambda now: pd.Timestamp(complete))

    def select(base, tickers, start, end, label=""):
        # Model a feed with every completed bar but no intraday bar. The
        # actual superset gate may take these columns only after the cap.
        taken = ([t for t in tickers if ps.plain_us_listing(t)]
                 if served_complete and base.index.max() == pd.Timestamp(complete)
                 else [])
        return base, {"replaced": taken,
                      "kept": [t for t in tickers
                               if ps.plain_us_listing(t) and t not in taken],
                      "unresolved": [t for t in tickers
                                     if not ps.plain_us_listing(t)]}

    monkeypatch.setattr(npx, "select_columns", select)
    if not served_complete:
        with pytest.raises(RuntimeError, match="Norgate supplied 0"):
            module.download_prices()
        assert not module.PRICE_CACHE.exists()
        return
    out = module.download_prices()
    assert out.index.max() == pd.Timestamp(complete)
    assert pd.Timestamp(partial) not in out.index
    assert ps.read_cache_source(module.PRICE_CACHE) == "norgate"


@pytest.mark.parametrize("complete,partial", [
    ("2024-01-31", "2024-02-01"),
    ("2024-12-31", "2025-01-02"),
])
def test_breadth_overlay_compares_only_completed_dates(
        monkeypatch, tmp_path, complete, partial):
    idx = pd.bdate_range(end=complete, periods=4).append(
        pd.DatetimeIndex([partial]))
    raw = pd.DataFrame({"Close": np.arange(len(idx), dtype=float) + 100.0},
                       index=idx)
    norgate = pd.DataFrame({"AAA": np.arange(len(idx) - 1, dtype=float) + 200.0},
                           index=idx[:-1])
    monkeypatch.setattr(cb, "yf", type("Y", (), {
        "download": staticmethod(lambda *a, **k: raw.copy())})())
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(npx, "fetch_closes",
                        lambda tickers, start, end, verbose=False: (
                            norgate.copy(), ["AAA"], []))
    cache = tmp_path / "data" / "prices_cache_test.parquet"
    out = cb.download_prices(["AAA"], str(idx[0].date()), partial,
                             cache_path=cache, roster=None, tail_probe=False,
                             required_through=complete, price_source="auto")
    assert out.index.max() == pd.Timestamp(complete)
    assert out["AAA"].equals(norgate["AAA"])
    assert ps.read_cache_source(cache) == "auto"
    assert ps.read_cache_sidecar(cache)["columns_from_norgate"] == ["AAA"]


@pytest.mark.parametrize("complete,partial", [
    ("2024-01-31", "2024-02-01"),
    ("2024-12-31", "2025-01-02"),
])
@pytest.mark.parametrize("served_complete", [True, False])
def test_ohlc_source_compares_only_completed_sessions(
        monkeypatch, tmp_path, complete, partial, served_complete):
    idx = pd.bdate_range(end=complete, periods=4).append(
        pd.DatetimeIndex([partial]))

    def ohlc(dates, level):
        prices = np.linspace(level, level * 1.1, len(dates))
        return pd.DataFrame({"Open": prices, "High": prices * 1.01,
                             "Low": prices * 0.99, "Close": prices},
                            index=dates)

    yahoo = ohlc(idx, 100.0)
    served = idx[:-1] if served_complete else idx[:-2]
    norgate = ohlc(served, 1000.0)
    monkeypatch.setattr(bt, "paths_for",
                        lambda etf: {"ohlc_cache": tmp_path / "ohlc.parquet"})
    monkeypatch.setattr(bt, "yf", type("Y", (), {
        "download": staticmethod(lambda *a, **k: yahoo.copy())})())
    monkeypatch.setattr(npx, "fetch_ohlc", lambda *a, **k: norgate.copy())
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")

    out = bt.download_soxx_ohlc(
        str(idx[0].date()), partial, etf="TEST",
        completed_through=pd.Timestamp(complete))
    assert out.index.max() == pd.Timestamp(complete)
    assert out["Close"].between(999, 1111).all() == served_complete
    assert out["Close"].between(99, 112).all() != served_complete
    assert pd.read_parquet(tmp_path / "ohlc.parquet").equals(out)
