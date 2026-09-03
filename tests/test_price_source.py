"""The price source is chosen once, honoured by the cache, and never flips silently.

WS19 (2026-08-30) measured the defect these pin: under BTE_PRICE_SOURCE=norgate
with a current yfinance cache, sleeves B and C took their cache-reuse branch and
never touched Norgate — the switch was vacuous. And an unreachable feed fell
back to yfinance with one log line, which for a scheduled run is a restatement
nobody chose. Adopted 2026-09-03 for the scheduled runs (WS19c).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import price_source as ps  # noqa: E402
import scheduled_refresh as sr  # noqa: E402


# ---------------------------------------------------------------------------
# resolve_source
# ---------------------------------------------------------------------------
def test_yfinance_is_the_default_and_never_consults_the_feed():
    assert ps.requested_source({}) == "yfinance"
    assert ps.resolve_source("yfinance", available=lambda: (_ for _ in ()).throw(
        AssertionError("feed consulted"))) == ("yfinance", "requested")


def test_norgate_request_fails_closed_when_the_feed_is_unreachable():
    with pytest.raises(RuntimeError) as exc:
        ps.resolve_source("norgate", available=lambda: False)
    assert "unreachable" in str(exc.value)
    assert "BTE_PRICE_SOURCE=yfinance" in str(exc.value), \
        "the refusal must say how to accept the fallback explicitly"


def test_norgate_request_resolves_when_reachable():
    assert ps.resolve_source("norgate", available=lambda: True)[0] == "norgate"


def test_auto_falls_back_and_says_so():
    src, why = ps.resolve_source("auto", available=lambda: False)
    assert src == "yfinance" and "fell back" in why
    src, why = ps.resolve_source("auto", available=lambda: True)
    assert src == "norgate"


def test_unknown_source_is_refused():
    with pytest.raises(ValueError):
        ps.requested_source({"BTE_PRICE_SOURCE": "bloomberg"})
    with pytest.raises(ValueError):
        ps.resolve_source("bloomberg", available=lambda: True)


# ---------------------------------------------------------------------------
# the sidecar
# ---------------------------------------------------------------------------
def test_sidecar_records_and_reads_the_source(tmp_path):
    cache = tmp_path / "asset_class_prices_cache.parquet"
    assert ps.read_cache_source(cache) is None
    path = ps.write_cache_source(cache, "norgate",
                                 {"replaced": ["SPY"], "kept": ["IJR"],
                                  "unresolved": []})
    assert path.name == "asset_class_prices_cache.source.json"
    assert ps.read_cache_source(cache) == "norgate"
    blob = json.loads(path.read_text(encoding="utf-8"))
    assert blob["columns_from_norgate"] == ["SPY"]


def test_an_unrecorded_cache_is_a_yfinance_cache():
    """Every cache written before 2026-09-03 was a yfinance download."""
    assert ps.cache_matches(None, "yfinance") is True
    assert ps.cache_matches(None, "norgate") is False
    assert ps.cache_matches("norgate", "norgate") is True
    assert ps.cache_matches("yfinance", "norgate") is False


def test_sidecar_is_gitignored_with_its_cache():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/*_prices_cache.source.json" in ignore


# ---------------------------------------------------------------------------
# the engines honour it — a current cache from the other source is refused
# ---------------------------------------------------------------------------
@pytest.fixture
def engine_site(monkeypatch, tmp_path):
    import run_asset_class_rotation as ac
    idx = pd.bdate_range("2024-01-02", "2024-01-19")
    needed = ac.TICKERS + ac.CASH_ONLY_TICKERS
    cached = pd.DataFrame({t: np.linspace(100.0, 110.0, len(idx)) for t in needed},
                          index=idx)
    cache = tmp_path / "asset_class_prices_cache.parquet"
    cached.to_parquet(cache)
    fresh = cached * 10.0                      # unmistakably the download
    yf_frame = pd.concat({t: pd.DataFrame({"Close": fresh[t]}) for t in needed},
                         axis=1)
    monkeypatch.setattr(ac, "PRICE_CACHE", cache)
    monkeypatch.setattr(ac, "last_completed_session",
                        lambda now: idx[-1].date())
    monkeypatch.setattr(ac, "cap_to_last_completed_session", lambda df: df)
    monkeypatch.setattr(ac, "yf", type("Y", (), {
        "download": staticmethod(lambda *a, **k: yf_frame.copy())})())
    import norgate_prices as npx
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(npx, "select_columns",
                        lambda df, tickers, s, e, label="": (df, {"replaced": [], "kept": [], "unresolved": []}))
    return ac, cache, cached, fresh


def test_current_unrecorded_cache_is_reused_on_yfinance(engine_site, monkeypatch):
    ac, cache, cached, fresh = engine_site
    monkeypatch.delenv("BTE_PRICE_SOURCE", raising=False)
    out = ac.download_prices()
    assert out["SPY"].iloc[-1] == pytest.approx(cached["SPY"].iloc[-1])


def test_current_yfinance_cache_is_refused_under_norgate(engine_site, monkeypatch):
    """WS19's vacuous switch: the reuse branch must not return before the
    selection runs when a different source was asked for."""
    ac, cache, cached, fresh = engine_site
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    out = ac.download_prices()
    assert out["SPY"].iloc[-1] == pytest.approx(fresh["SPY"].iloc[-1]), \
        "a yfinance-built cache was reused under BTE_PRICE_SOURCE=norgate"
    assert ps.read_cache_source(cache) == "norgate"


def test_current_norgate_cache_is_reused_under_norgate(engine_site, monkeypatch):
    ac, cache, cached, fresh = engine_site
    ps.write_cache_source(cache, "norgate")
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    out = ac.download_prices()
    assert out["SPY"].iloc[-1] == pytest.approx(cached["SPY"].iloc[-1])


def test_engine_refuses_norgate_when_the_feed_is_down(engine_site, monkeypatch):
    ac, cache, cached, fresh = engine_site
    import norgate_prices as npx
    monkeypatch.setattr(npx, "available", lambda: False)
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    with pytest.raises(RuntimeError):
        ac.download_prices()


# ---------------------------------------------------------------------------
# the scheduled runner
# ---------------------------------------------------------------------------
def test_scheduled_default_is_norgate():
    assert sr.DEFAULT_PRICE_SOURCE == "norgate"


def test_scheduled_preflight_refuses_an_unreachable_feed():
    ok, msg = sr.price_source_preflight("norgate", available=lambda: False)
    assert ok is False
    assert "--price-source yfinance" in msg


def test_scheduled_preflight_passes_when_reachable_and_on_yfinance():
    assert sr.price_source_preflight("norgate", available=lambda: True)[0] is True
    ok, msg = sr.price_source_preflight("yfinance",
                                        available=lambda: (_ for _ in ()).throw(
                                            AssertionError("feed consulted")))
    assert ok is True


def test_scheduled_runner_passes_the_source_to_refresh_all():
    import inspect
    src = inspect.getsource(sr.main)
    assert '"--price-source", args.price_source' in src
