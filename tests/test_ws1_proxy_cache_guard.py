"""Offline tests for the WS1 proxy-cache span guard.

2026-08-19: the daily two-year backfill had overwritten the five sleeve-D
Xetra OHLC caches' 2017 history (observed first bar 2024-08-14), and the WS2
baseline force-rebuild read the stubs back as authoritative — the blend
collapsed onto a two-year window and reported Sharpe +1.99 against a
committed +1.29. A downstream guard caught the number; nothing had judged
the input. The write sites now refuse span-shrinking writes
(``price_panel_guard.fetched_frame_is_worse``); the loader guard pinned here
covers every OTHER route to a short cache — deleted and refetched into a
void, copied from a short vintage — because every WS1/WS2 consumer evaluates
the fixed window from ``COMMON_START``, so a cache that cannot reach it is
truncated, never merely late-listed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import price_panel_guard as g  # noqa: E402
import ws1_common as W  # noqa: E402


def _write_cache(tmp_path: Path, name: str, start: str, end: str) -> None:
    idx = pd.bdate_range(start, end)
    close = pd.Series(np.linspace(50.0, 150.0, len(idx)), index=idx)
    pd.DataFrame({"Open": close, "High": close, "Low": close,
                  "Close": close}).to_parquet(tmp_path / name)


def test_a_truncated_proxy_cache_refuses_to_load(tmp_path, monkeypatch):
    """The exact shape found in the primary tree on 2026-08-19."""
    monkeypatch.setattr(W, "DATA", tmp_path)
    _write_cache(tmp_path, "exv1.de_ohlc_cache.parquet",
                 "2024-08-14", "2026-08-13")
    with pytest.raises(g.DegeneratePriceError) as exc:
        W._proxy_close_from_cache("EXV1")
    assert "--refresh-caches-only" in str(exc.value), (
        "the error must tell the operator how to repair the cache")


def test_a_full_history_proxy_cache_loads_unchanged(tmp_path, monkeypatch):
    """The guard must not fire on the run it is meant to allow."""
    monkeypatch.setattr(W, "DATA", tmp_path)
    _write_cache(tmp_path, "exv1.de_ohlc_cache.parquet",
                 "2017-06-30", "2026-08-14")
    close = W._proxy_close_from_cache("EXV1")
    assert close.index.min() == pd.Timestamp("2017-06-30")
    assert close.index.max() == pd.Timestamp("2026-08-14")


def test_a_legitimately_late_listed_proxy_still_loads(tmp_path, monkeypatch):
    """XLC listed 2018-06-19 — 344 calendar days after its panel start and
    the latest legitimate proxy inception in either universe (measured
    2026-08-19 against the repaired full-depth vintage). It precedes
    COMMON_START, so the guard must not read it as truncated."""
    monkeypatch.setattr(W, "DATA", tmp_path)
    _write_cache(tmp_path, "xlc_ohlc_cache.parquet",
                 "2018-06-19", "2026-08-14")
    close = W._proxy_close_from_cache("IUCM")
    assert close.index.min() == pd.Timestamp("2018-06-19")


def test_an_empty_proxy_cache_refuses_to_load(tmp_path, monkeypatch):
    """An empty frame used to flow onward as an empty column and shorten the
    panel silently; NaT compares False against any bound, so it needs its
    own branch."""
    monkeypatch.setattr(W, "DATA", tmp_path)
    pd.DataFrame({"Open": [], "High": [], "Low": [], "Close": []},
                 index=pd.DatetimeIndex([])).to_parquet(
        tmp_path / "exv1.de_ohlc_cache.parquet")
    with pytest.raises(g.DegeneratePriceError):
        W._proxy_close_from_cache("EXV1")


def test_the_guard_boundary_sits_exactly_on_common_start(tmp_path, monkeypatch):
    """The threshold is COMMON_START itself, not a tuned constant. A boundary
    pair pins it: a first bar ON the anchor loads; the NEXT business day
    refuses. The neighbour is computed with pd.bdate_range, not by hand
    (dates here are ISO strings parsed by pandas; nothing is offset
    manually)."""
    monkeypatch.setattr(W, "DATA", tmp_path)
    on_anchor, after_anchor = pd.bdate_range(W.COMMON_START, periods=2)
    assert on_anchor == W.COMMON_START

    _write_cache(tmp_path, "exv1.de_ohlc_cache.parquet",
                 str(on_anchor.date()), "2026-08-14")
    assert W._proxy_close_from_cache("EXV1").index.min() == on_anchor

    _write_cache(tmp_path, "exv1.de_ohlc_cache.parquet",
                 str(after_anchor.date()), "2026-08-14")
    with pytest.raises(g.DegeneratePriceError):
        W._proxy_close_from_cache("EXV1")
