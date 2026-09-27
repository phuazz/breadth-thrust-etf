"""A one-ticker yfinance refetch must return its series (offline).

With group_by="ticker", yfinance 1.1.0 keys even a single ticker as
(ticker, field). The single-ticker branch read a flat "Close" column, so every
one-ticker refetch came back empty; on 2026-09-27 IBIT was the only line to
refetch and the release preflight refused the book for want of its quote.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_holdings_prices as ehp  # noqa: E402

IDX = pd.bdate_range("2026-09-01", "2026-09-25")


def _frame(tickers, keyed):
    fields = ["Open", "High", "Low", "Close", "Volume"]
    if keyed:
        cols = pd.MultiIndex.from_product([tickers, fields])
    else:
        cols = pd.Index(fields)
    data = {c: range(1, len(IDX) + 1) for c in cols}
    return pd.DataFrame(data, index=IDX)


def _fetch(monkeypatch, tmp_path, tickers, keyed):
    fake = types.SimpleNamespace(__version__="test",
                                 download=lambda *a, **k: _frame(tickers, keyed))
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    return ehp.fetch_missing_from_yfinance(list(tickers))


def test_single_ticker_keyed_frame_is_read(monkeypatch, tmp_path):
    out = _fetch(monkeypatch, tmp_path, ["IBIT"], keyed=True)
    assert "IBIT" in out
    assert out["IBIT"].index[-1] == pd.Timestamp("2026-09-25")


def test_single_ticker_flat_frame_still_read(monkeypatch, tmp_path):
    """The older yfinance shape keeps working."""
    out = _fetch(monkeypatch, tmp_path, ["IBIT"], keyed=False)
    assert "IBIT" in out


def test_batch_keyed_frame_unchanged(monkeypatch, tmp_path):
    out = _fetch(monkeypatch, tmp_path, ["IBIT", "XBI"], keyed=True)
    assert set(out) == {"IBIT", "XBI"}
