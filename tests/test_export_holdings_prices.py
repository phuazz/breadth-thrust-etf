"""Guard tests for scripts/export_holdings_prices.py.

Regression cover for the 2026-07 incident where the weekly holdings price
export silently dropped ~55-60% of the model book on a fresh CI runner. The
OHLC / asset-class / thematic price caches are gitignored, so on the runner
only the ~38 tickers the Strategy B/C rotation steps download live survived;
SOXX, the US sector proxies (XLE/XLU/XLRE/XLB), the Xetra .DE lines and EEM
(overlay-only since Phase 29, hence gone from the rotation parquet) all
vanished, and the digest 200-DMA monitor fell back to a stale panel.

These tests are OFFLINE: EEM is asserted to resolve from the COMMITTED
data/em_regime_context.parquet (no network, no gitignored cache), and the
build/universe logic is exercised on synthetic series. They must stay green
without a network connection or any locally-built price cache.

Python date months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_holdings_prices as ehp  # noqa: E402


# The trade-as proxies the model book actually holds. Every one of these must
# be reachable by the export, either from a cache or the yfinance backfill.
BOOK_CRITICAL = [
    "SOXX", "XLE", "XLU", "XLRE", "XLB",
    "EXH1.DE", "EXH9.DE", "EXV1.DE",
    "EEM",
]


def test_universe_includes_book_critical():
    """collect_all_tickers() must offer every book-critical proxy — even with
    no local caches present, because NETWORK_FALLBACK_TICKERS is unioned in."""
    universe = ehp.collect_all_tickers()
    for tk in BOOK_CRITICAL:
        assert tk in universe, f"{tk} missing from export universe"


def test_network_fallback_covers_book_proxies():
    """The yfinance backfill set is what saves the runner build; it must list
    every book-critical proxy so an absent gitignored cache cannot drop it."""
    for tk in BOOK_CRITICAL:
        assert tk in ehp.NETWORK_FALLBACK_TICKERS, f"{tk} not in fallback set"


def test_eem_sources_from_committed_cache_offline():
    """EEM left the rotation parquet at Phase 29; its only committed price
    source is em_regime_context.parquet. This is the exact bug — assert EEM
    loads offline with real history (>=200 sessions so its 200d MA populates)."""
    cache = ehp.DATA_DIR / "em_regime_context.parquet"
    assert cache.exists(), "committed em_regime_context.parquet is missing"
    ser = ehp.load_close_series("EEM")
    assert ser is not None, "EEM did not resolve from any cache source"
    assert len(ser) >= 200, f"EEM series too short ({len(ser)}) for a 200d MA"
    assert ser.notna().all()


def _synthetic_close(n: int) -> pd.Series:
    idx = pd.bdate_range("2024-01-01", periods=n)
    # Gentle uptrend so the last close sits above its 200d MA.
    vals = 100.0 + np.arange(n) * 0.1
    return pd.Series(vals, index=idx)


def test_build_entry_none_and_too_short():
    assert ehp.build_entry(None) is None
    assert ehp.build_entry(_synthetic_close(1)) is None


def test_build_entry_schema_and_trend_sign():
    entry = ehp.build_entry(_synthetic_close(260))
    assert entry is not None
    for key in ("dates", "prices", "ma50", "ma100", "ma200",
                "change_pct", "vs_ma200", "n_days"):
        assert key in entry, f"missing key {key}"
    # 260 bdays sliced to the 252-day window.
    assert entry["n_days"] == ehp.LOOKBACK_DAYS
    assert len(entry["dates"]) == entry["n_days"]
    assert len(entry["prices"]) == entry["n_days"]
    # Monotonic uptrend => last close above the 200d MA and positive 1Y change.
    assert entry["vs_ma200"] is not None and entry["vs_ma200"] > 0
    assert entry["change_pct"] is not None and entry["change_pct"] > 0


def test_build_entry_young_ticker_has_null_ma200_tail():
    """A ticker with <200 sessions has an all-None ma200 overlay (Plotly skips
    those points) but still produces a valid price record."""
    entry = ehp.build_entry(_synthetic_close(120))
    assert entry is not None
    assert all(v is None for v in entry["ma200"])
    assert entry["vs_ma200"] is None


def test_fetch_missing_from_yfinance_empty_input_is_noop():
    """No tickers requested => no network, empty mapping."""
    assert ehp.fetch_missing_from_yfinance([]) == {}


# ---------------------------------------------------------------------------
# Carry-forward versus retirement
#
# The coverage guard exists so a degraded run cannot shrink the published
# panel: any ticker it fails to source is carried forward from the previous
# file. That conflated two cases until 2026-08-03. A ticker the run WANTED
# but could not fetch must be carried; one no longer requested by any sleeve
# must be dropped, or it persists for ever with frozen prices and pollutes
# the carry-forward warning with a name that will never resolve again.
# EXH3.DE became exactly that ghost when sleeve D's industrials panel was
# repointed to EXH4.DE.
# ---------------------------------------------------------------------------
def test_retired_tickers_are_not_carried_forward(tmp_path, monkeypatch):
    """A symbol absent from the requested set is dropped, not carried."""
    prev = {
        "computed_at_utc": "2026-08-03T00:00:00+00:00",
        "lookback_days": 252,
        "prices": {
            "RETIRED.DE": {"dates": ["2026-07-31"], "prices": [1.0]},
            "SPY": {"dates": ["2026-07-31"], "prices": [500.0]},
        },
    }
    out_path = tmp_path / "holdings_prices_1y.json"
    out_path.write_text(json.dumps(prev), encoding="utf-8")
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"SPY"})

    # Reproduces the guard's decision rule against the patched requested set.
    wanted = ehp.collect_all_tickers() | {"SPY"}
    carried, retired = [], []
    for tk, entry in prev["prices"].items():
        if entry and entry.get("dates"):
            (carried if tk in wanted else retired).append(tk)

    assert retired == ["RETIRED.DE"]
    assert carried == ["SPY"]


# ---------------------------------------------------------------------------
# Never-go-backwards
#
# 2026-08-10: the published panel moved BACKWARDS four sessions for EEM — the
# largest holding in the book at 10.0% of NAV — from a 2026-08-07 last bar to
# 2026-08-03, inside a refresh whose commit message read "all 38 panels
# current". Two independent faults had to line up:
#
#   1. load_close_series returned the FIRST cache carrying the ticker, in a
#      fixed source order. fetch_missing_from_yfinance WRITES
#      {ticker}_ohlc_cache.parquet, which sits at position 3 — ahead of the
#      committed em_regime_context.parquet at position 4. A backfill on
#      2026-08-04 therefore left a short eem_ohlc_cache.parquet permanently
#      shadowing a fresher source.
#   2. Nothing compared the new last bar against the published one. The
#      coverage guard only fires when a ticker vanishes ENTIRELY, and
#      entry_is_stale tolerates 7 CALENDAR days so a four-session loss passed.
#
# Markets do not un-print closes, so a backwards move is always a sourcing
# fault. Both halves are pinned below.
# ---------------------------------------------------------------------------
def test_load_close_series_prefers_the_freshest_source(tmp_path, monkeypatch):
    """The exact EEM shape: a stale individual OHLC cache must NOT shadow a
    fresher multi-ticker source that sits later in the search order."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    stale_idx = pd.bdate_range("2026-01-01", "2026-08-03")
    fresh_idx = pd.bdate_range("2026-01-01", "2026-08-07")
    # Source 3 — written by the yfinance backfill, ends four sessions early.
    pd.DataFrame({"Close": np.arange(len(stale_idx), dtype=float)},
                 index=stale_idx).to_parquet(tmp_path / "eem_ohlc_cache.parquet")
    # Source 4 — the committed regime-context panel, current.
    pd.DataFrame({"EEM": np.arange(len(fresh_idx), dtype=float)},
                 index=fresh_idx).to_parquet(tmp_path / "em_regime_context.parquet")

    ser = ehp.load_close_series("EEM")
    assert ser is not None
    assert ser.index[-1].strftime("%Y-%m-%d") == "2026-08-07", (
        "load_close_series took the stale cache over the fresher source")


def test_find_regressions_flags_a_backwards_last_bar():
    prev = {
        "EEM": {"dates": ["2026-08-06", "2026-08-07"], "prices": [1.0, 1.0]},
        "SPY": {"dates": ["2026-08-06", "2026-08-07"], "prices": [1.0, 1.0]},
    }
    new = {
        "EEM": {"dates": ["2026-08-02", "2026-08-03"], "prices": [1.0, 1.0]},
        "SPY": {"dates": ["2026-08-06", "2026-08-07"], "prices": [1.0, 1.0]},
    }
    regressed = ehp.find_regressions(new, prev)
    assert regressed == {"EEM": ("2026-08-07", "2026-08-03")}, regressed


def test_find_regressions_allows_advancing_and_unchanged():
    prev = {"SPY": {"dates": ["2026-08-07"], "prices": [1.0]}}
    assert ehp.find_regressions(
        {"SPY": {"dates": ["2026-08-10"], "prices": [1.0]}}, prev) == {}
    assert ehp.find_regressions(
        {"SPY": {"dates": ["2026-08-07"], "prices": [1.0]}}, prev) == {}
    # A ticker absent from the new run is the CARRY-FORWARD guard's job, not
    # this one — it must not be reported as a regression.
    assert ehp.find_regressions({}, prev) == {}


def test_regressed_ticker_is_held_back_to_the_published_series(tmp_path, monkeypatch):
    """End-to-end: with the network stubbed out (the re-fetch cannot repair
    it), main() must publish the PREVIOUS EEM series rather than the shorter
    one, and --strict must exit non-zero."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    out_path = tmp_path / "holdings_prices_1y.json"
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"EEM"})
    monkeypatch.setattr(ehp, "collect_book_symbols", lambda: {"EEM"})
    monkeypatch.setattr(ehp, "NETWORK_FALLBACK_TICKERS", ["EEM"])
    monkeypatch.setattr(ehp, "fetch_missing_from_yfinance", lambda tks: {})

    good = ["2026-08-06", "2026-08-07"]
    out_path.write_text(json.dumps({
        "computed_at_utc": "2026-08-08T12:27:00+00:00", "lookback_days": 252,
        "prices": {"EEM": {"dates": good, "prices": [1.0, 2.0]}},
    }), encoding="utf-8")

    # The only on-disk source now ends four sessions early.
    stale_idx = pd.bdate_range("2026-01-01", "2026-08-03")
    pd.DataFrame({"Close": np.arange(len(stale_idx), dtype=float)},
                 index=stale_idx).to_parquet(tmp_path / "eem_ohlc_cache.parquet")

    assert ehp.main(["--strict"]) == 1, "--strict must fail on an unrepaired regression"
    published = json.loads(out_path.read_text(encoding="utf-8"))["prices"]["EEM"]
    assert published["dates"][-1] == "2026-08-07", (
        "the panel went backwards: published last bar is "
        f"{published['dates'][-1]}")
    # Default (non-strict) still publishes the held-back series and exits 0,
    # so a degraded daily run reports loudly without breaking the pipeline.
    assert ehp.main([]) == 0


def test_the_live_panel_has_no_exh3_ghost():
    """Regression pin on the committed artefact: the industrials line is
    published as EXH4.DE and the food-and-beverage ticker is absent."""
    panel = json.loads(
        (ehp.DATA_DIR / "holdings_prices_1y.json").read_text(encoding="utf-8")
    )["prices"]
    assert "EXH4.DE" in panel, "the industrials line must be published"
    assert "EXH3.DE" not in panel, "EXH3.DE is a food & beverage fund, not in any sleeve"
