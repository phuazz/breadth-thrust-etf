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
    one, and must exit non-zero BY DEFAULT.

    The failure was originally opt-in behind --strict, but nothing passed the
    flag — not refresh_all.py, not either workflow — so the 2026-08-08 run
    that rewrote EEM backwards would still have exited 0 and been committed.
    """
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    out_path = tmp_path / "holdings_prices_1y.json"
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"EEM"})
    monkeypatch.setattr(ehp, "collect_book_symbols", lambda: {"EEM"})
    monkeypatch.setattr(ehp, "NETWORK_FALLBACK_TICKERS", ["EEM"])
    monkeypatch.setattr(ehp, "fetch_missing_from_yfinance",
                        lambda tks, gaps_out=None: {})

    good = ["2026-08-06", "2026-08-07"]
    out_path.write_text(json.dumps({
        "computed_at_utc": "2026-08-08T12:27:00+00:00", "lookback_days": 252,
        "prices": {"EEM": {"dates": good, "prices": [1.0, 2.0]}},
    }), encoding="utf-8")

    # The only on-disk source now ends four sessions early.
    stale_idx = pd.bdate_range("2026-01-01", "2026-08-03")
    pd.DataFrame({"Close": np.arange(len(stale_idx), dtype=float)},
                 index=stale_idx).to_parquet(tmp_path / "eem_ohlc_cache.parquet")

    # No flag: the DEFAULT invocation, which is the one every caller uses.
    assert ehp.main([]) == ehp.REGRESSION_EXIT_CODE, (
        "an unrepaired regression must fail the run without needing a flag")
    published = json.loads(out_path.read_text(encoding="utf-8"))["prices"]["EEM"]
    assert published["dates"][-1] == "2026-08-07", (
        "the panel went backwards: published last bar is "
        f"{published['dates'][-1]}")

    # --strict is retained as an accepted no-op so a stray invocation from an
    # older caller does not crash; it must not change the outcome.
    assert ehp.main(["--strict"]) == ehp.REGRESSION_EXIT_CODE

    # The exit code is distinct from 1 so the workflows can hard-fail a
    # backwards panel while still soft-failing a transient vendor error.
    assert ehp.REGRESSION_EXIT_CODE == 2


def test_a_repaired_panel_exits_zero(tmp_path, monkeypatch):
    """Control for the test above: the same path with an ADVANCING series
    writes normally and exits 0, so the guard cannot pass by always failing."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    out_path = tmp_path / "holdings_prices_1y.json"
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"EEM"})
    monkeypatch.setattr(ehp, "collect_book_symbols", lambda: {"EEM"})
    monkeypatch.setattr(ehp, "NETWORK_FALLBACK_TICKERS", ["EEM"])
    monkeypatch.setattr(ehp, "fetch_missing_from_yfinance",
                        lambda tks, gaps_out=None: {})

    out_path.write_text(json.dumps({
        "computed_at_utc": "2026-08-03T00:00:00+00:00", "lookback_days": 252,
        "prices": {"EEM": {"dates": ["2026-08-02", "2026-08-03"],
                           "prices": [1.0, 2.0]}},
    }), encoding="utf-8")

    idx = pd.bdate_range("2026-01-01", "2026-08-07")
    pd.DataFrame({"Close": np.arange(len(idx), dtype=float)},
                 index=idx).to_parquet(tmp_path / "eem_ohlc_cache.parquet")

    assert ehp.main([]) == 0
    published = json.loads(out_path.read_text(encoding="utf-8"))["prices"]["EEM"]
    assert published["dates"][-1] == "2026-08-07"


def test_yfinance_backfill_refuses_to_overwrite_a_newer_cache(tmp_path, monkeypatch):
    """The write that manufactured the 2026-08-04 stubs. A short vendor
    response must not be persisted over a cache that already ends later.

    load_close_series' freshest-wins rule stops a stale cache WINNING; this
    stops one being created. Without it the same 8-9 KB stub is rewritten on
    every run that touches the backfill path.
    """
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    cache = tmp_path / "eem_ohlc_cache.parquet"
    good_idx = pd.bdate_range(end="2026-08-07", periods=300)
    pd.DataFrame({"Close": 100.0 + np.arange(300) * 0.1},
                 index=good_idx).to_parquet(cache)

    short_idx = pd.bdate_range(end="2026-08-03", periods=250)
    short = pd.DataFrame(
        {("EEM", "Close"): 100.0 + np.arange(250) * 0.1,
         ("SPY", "Close"): 400.0 + np.arange(250) * 0.1},
        index=short_idx)
    short.columns = pd.MultiIndex.from_tuples(short.columns)

    fake_yf = type("_YF", (), {"download": staticmethod(lambda *a, **k: short)})
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    got = ehp.fetch_missing_from_yfinance(["EEM", "SPY"])

    on_disk = pd.read_parquet(cache)["Close"].dropna()
    assert str(on_disk.index[-1].date()) == "2026-08-07", (
        "the newer cache was overwritten by a shorter vendor response")
    assert str(got["EEM"].index[-1].date()) == "2026-08-07", (
        "the refused write must return the newer on-disk series, not the stub")
    # SPY had no cache to protect, so its fetched series is persisted normally.
    assert str(got["SPY"].index[-1].date()) == "2026-08-03"
    assert (tmp_path / "spy_ohlc_cache.parquet").exists()


def test_yfinance_backfill_refuses_to_truncate_history(tmp_path, monkeypatch):
    """2026-08-13/14: the ``period="2y"`` backfill is always FRESH at the
    tail, so the newer-cache rule above cannot catch it — and it overwrote
    the five sleeve-D Xetra caches' 2017 history with two-year stubs. The
    next cold rebuild read a stub back as authoritative and collapsed a
    blend onto the surviving window (Sharpe +1.99 against a committed
    +1.29). A cache write must never move the first bar later: the fetched
    series may still feed the export, but the FILE keeps its history."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    cache = tmp_path / "exv1.de_ohlc_cache.parquet"
    full_idx = pd.bdate_range("2017-06-30", "2026-08-14")
    pd.DataFrame({"Close": 100.0 + np.arange(len(full_idx)) * 0.01},
                 index=full_idx).to_parquet(cache)

    fresh_idx = pd.bdate_range("2024-08-14", "2026-08-18")   # the 2y shape
    fresh = pd.DataFrame(
        {("EXV1.DE", "Close"): 50.0 + np.arange(len(fresh_idx)) * 0.01,
         ("EXH1.DE", "Close"): 40.0 + np.arange(len(fresh_idx)) * 0.01},
        index=fresh_idx)
    fresh.columns = pd.MultiIndex.from_tuples(fresh.columns)

    fake_yf = type("_YF", (), {"download": staticmethod(lambda *a, **k: fresh)})
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    got = ehp.fetch_missing_from_yfinance(["EXV1.DE", "EXH1.DE"])

    on_disk = pd.read_parquet(cache)["Close"].dropna()
    assert str(on_disk.index[0].date()) == "2017-06-30", (
        "a fresh two-year response truncated nine years of cache history")
    assert str(on_disk.index[-1].date()) == "2026-08-14", (
        "the refused write must leave the cache exactly as it was")
    # The export still uses the fetched series — freshest wins for the panel.
    assert str(got["EXV1.DE"].index[0].date()) == "2024-08-14"
    assert str(got["EXV1.DE"].index[-1].date()) == "2026-08-18"
    # A ticker with no cache to protect is persisted normally.
    assert (tmp_path / "exh1.de_ohlc_cache.parquet").exists()


def test_the_live_panel_has_no_exh3_ghost():
    """Regression pin on the committed artefact: the industrials line is
    published as EXH4.DE and the food-and-beverage ticker is absent."""
    panel = json.loads(
        (ehp.DATA_DIR / "holdings_prices_1y.json").read_text(encoding="utf-8")
    )["prices"]
    assert "EXH4.DE" in panel, "the industrials line must be published"
    assert "EXH3.DE" not in panel, "EXH3.DE is a food & beverage fund, not in any sleeve"


# ---------------------------------------------------------------------------
# Vendor gaps — the nightly XETR / SZSE hole
#
# 2026-08-25/26/27: three consecutive unattended "Daily live mark-to-market"
# runs died at the holdings price export. The failing tickers were 159801.SZ
# and the five Xetra .DE lines, not SPY — SPY appears in the pre-refetch
# REGRESSION line of the run that SUCCEEDED too, because its 2026-08-20 bar
# comes from the committed em_regime_context.parquet and the refetch repairs
# it every time.
#
# The cause: for non-US venues yfinance returns the most recently completed
# session as NaN for 12-20 hours, having served it earlier the same day.
# dropna() turns that into a shorter series, the last bar moves backwards
# against the published panel, and the run exits 2.
#
# The repair reinstates ONLY a bar that (a) the vendor's own response marks as
# a live session it withheld from this line, and (b) this repo already
# published for that exact date, and (c) sits on the same price scale as what
# the vendor is serving now. The tests below pin the repair AND every one of
# the three refusals — a guard that cannot say no is not a guard.
# ---------------------------------------------------------------------------
XETR_AUG = ["2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
            "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"]


def _panel_entry(dates, prices):
    return {"dates": list(dates),
            "prices": ehp._round_sig([float(p) for p in prices])}


def _series(dates, prices):
    return pd.Series([float(p) for p in prices],
                     index=pd.to_datetime(list(dates)))


def _long_dates(n=260, end="2026-08-25"):
    return [str(d.date()) for d in pd.bdate_range(end=end, periods=n)]


def test_vendor_gaps_are_captured_before_dropna(tmp_path, monkeypatch):
    """fetch_missing_from_yfinance must report a session the vendor blanked
    for ONE line while other lines in the same response printed a close.
    That evidence only exists before dropna(), which is why it is collected
    inside the fetch rather than inferred afterwards."""
    idx = pd.to_datetime(["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"])
    frame = pd.DataFrame(
        {("EXV1.DE", "Close"): [42.40, 42.31, np.nan, 41.84],   # 08-26 withheld
         ("SPY", "Close"): [763.47, 765.91, 766.08, 769.67]},
        index=idx)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    fake_yf = type("_YF", (), {"download": staticmethod(lambda *a, **k: frame),
                               "__version__": "1.1.0"})
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)

    gaps = {}
    got = ehp.fetch_missing_from_yfinance(["EXV1.DE", "SPY"], gaps_out=gaps)

    assert gaps == {"EXV1.DE": ["2026-08-26"]}, gaps
    # dropna() still shortens the returned series — the gap record is what lets
    # the caller put the bar back.
    assert str(got["EXV1.DE"].index[-1].date()) == "2026-08-27"
    assert "2026-08-26" not in [str(d.date()) for d in got["EXV1.DE"].index]


def test_a_market_holiday_is_not_reported_as_a_vendor_gap(tmp_path, monkeypatch):
    """A date where NOTHING in the batch printed is a closed market, not a
    withheld line. It must not be recorded as a gap."""
    idx = pd.to_datetime(["2026-08-24", "2026-08-25", "2026-08-26"])
    frame = pd.DataFrame(
        {("EXV1.DE", "Close"): [42.40, 42.31, np.nan],
         ("EXH4.DE", "Close"): [120.32, 121.76, np.nan]},
        index=idx)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    fake_yf = type("_YF", (), {"download": staticmethod(lambda *a, **k: frame)})
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)

    gaps = {}
    ehp.fetch_missing_from_yfinance(["EXV1.DE", "EXH4.DE"], gaps_out=gaps)
    assert gaps == {}, f"a closed session was mistaken for a vendor gap: {gaps}"


def test_reinstate_puts_back_a_withheld_session():
    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    prev = _panel_entry(dates + ["2026-08-26"], prices + [102.6])
    fetched = _series(dates + ["2026-08-27"], prices + [102.7])   # 08-26 missing

    got, filled, refused = ehp.reinstate_vendor_gaps(
        "EXV1.DE", fetched, prev, ["2026-08-26"])

    assert refused is None
    assert filled == ["2026-08-26"]
    assert str(got.index[-2].date()) == "2026-08-26"
    assert got.loc[pd.Timestamp("2026-08-26")] == pytest.approx(102.6)
    assert got.index.is_monotonic_increasing


def test_reinstate_refuses_when_the_adjustment_vintage_moved():
    """auto_adjust=True re-scales the WHOLE history when a dividend goes ex.
    Splicing a bar from the old scale onto the new one would join two
    vintages, so a disagreeing overlap must refuse rather than repair."""
    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    prev = _panel_entry(dates + ["2026-08-26"], prices + [102.6])
    # Same shape, every close re-scaled 0.4% down: a dividend adjustment.
    fetched = _series(dates + ["2026-08-27"],
                      [p * 0.996 for p in prices] + [102.3])

    got, filled, refused = ehp.reinstate_vendor_gaps(
        "EXV1.DE", fetched, prev, ["2026-08-26"])

    assert filled == []
    assert refused and "adjustment vintage" in refused
    assert "2026-08-26" not in [str(d.date()) for d in got.index]


def test_reinstate_cannot_invent_a_date_the_panel_never_published():
    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    prev = _panel_entry(dates, prices)          # no 08-26 bar to restore
    fetched = _series(dates + ["2026-08-27"], prices + [102.7])

    got, filled, refused = ehp.reinstate_vendor_gaps(
        "EXV1.DE", fetched, prev, ["2026-08-26"])

    assert filled == [] and refused is None
    assert "2026-08-26" not in [str(d.date()) for d in got.index]


def test_reinstate_does_nothing_without_vendor_evidence():
    """The 2026-08-08 EEM shape: a local source simply ENDS early. There is no
    vendor blank, so there is nothing to reinstate and the regression stands."""
    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    prev = _panel_entry(dates + ["2026-08-26"], prices + [102.6])
    short = _series(dates[:-4], prices[:-4])

    for evidence in (None, []):
        got, filled, refused = ehp.reinstate_vendor_gaps(
            "EEM", short, prev, evidence)
        assert filled == [] and refused is None
        assert str(got.index[-1].date()) == dates[-5]


def test_withheld_session_run_exits_zero_and_keeps_the_bar(tmp_path, monkeypatch):
    """End-to-end, the exact shape of the 2026-08-26/27 failures: the vendor
    withholds the newest completed Xetra session, the published panel has it,
    and the run must publish a complete series and exit 0 — WITHOUT the guard
    being told to look the other way."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    out_path = tmp_path / "holdings_prices_1y.json"
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"EXV1.DE"})
    monkeypatch.setattr(ehp, "collect_book_symbols", lambda: {"EXV1.DE"})
    monkeypatch.setattr(ehp, "NETWORK_FALLBACK_TICKERS", ["EXV1.DE"])

    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    out_path.write_text(json.dumps({
        "computed_at_utc": "2026-08-26T18:00:00+00:00", "lookback_days": 252,
        "prices": {"EXV1.DE": _panel_entry(dates + ["2026-08-26"],
                                           prices + [102.6])},
    }), encoding="utf-8")

    def _fetch(tks, gaps_out=None):
        if gaps_out is not None:
            gaps_out["EXV1.DE"] = ["2026-08-26"]
        return {"EXV1.DE": _series(dates + ["2026-08-27"], prices + [102.7])}

    monkeypatch.setattr(ehp, "fetch_missing_from_yfinance", _fetch)

    assert ehp.main([]) == 0, "a repairable vendor gap must not block the publish"
    published = json.loads(
        out_path.read_text(encoding="utf-8"))["prices"]["EXV1.DE"]
    assert published["dates"][-2:] == ["2026-08-26", "2026-08-27"], (
        f"the withheld session was not restored: {published['dates'][-3:]}")


def test_same_run_without_vendor_evidence_still_fails(tmp_path, monkeypatch):
    """Control for the test above, identical in every respect EXCEPT that the
    vendor reports no withheld session. The truncation is then indistinguishable
    from source rot, so it must still be held back and still exit 2."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    out_path = tmp_path / "holdings_prices_1y.json"
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"EXV1.DE"})
    monkeypatch.setattr(ehp, "collect_book_symbols", lambda: {"EXV1.DE"})
    monkeypatch.setattr(ehp, "NETWORK_FALLBACK_TICKERS", ["EXV1.DE"])

    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    out_path.write_text(json.dumps({
        "computed_at_utc": "2026-08-26T18:00:00+00:00", "lookback_days": 252,
        "prices": {"EXV1.DE": _panel_entry(dates + ["2026-08-26"],
                                           prices + [102.6])},
    }), encoding="utf-8")

    monkeypatch.setattr(
        ehp, "fetch_missing_from_yfinance",
        lambda tks, gaps_out=None: {"EXV1.DE": _series(dates, prices)})

    assert ehp.main([]) == ehp.REGRESSION_EXIT_CODE
    published = json.loads(
        out_path.read_text(encoding="utf-8"))["prices"]["EXV1.DE"]
    assert published["dates"][-1] == "2026-08-26", "the panel went backwards"


# ---------------------------------------------------------------------------
# Interior gaps — the hole find_regressions cannot see
# ---------------------------------------------------------------------------
def test_interior_gap_is_detected():
    """The panel committed at 15:30 UTC on 2026-08-27 carried no 2026-08-26
    bar for any of the five Xetra lines and went green: the vendor had already
    restored 08-27 over the hole, so the last-bar check passed."""
    holed = {"dates": [d for d in XETR_AUG if d != "2026-08-26"],
             "prices": [1.0] * (len(XETR_AUG) - 1)}
    assert ehp.interior_gaps(holed, "EXV1.DE") == ["2026-08-26"]


def test_complete_series_and_tail_lag_report_no_interior_gap():
    """Two controls, so the check cannot pass by always complaining: a full
    series is clean, and a series that merely STOPS early is a publication lag
    (Europe routinely trails the US by a session), not a hole."""
    full = {"dates": XETR_AUG, "prices": [1.0] * len(XETR_AUG)}
    assert ehp.interior_gaps(full, "EXV1.DE") == []

    lagging = {"dates": XETR_AUG[:-2], "prices": [1.0] * (len(XETR_AUG) - 2)}
    assert ehp.interior_gaps(lagging, "EXV1.DE") == []


def test_us_holiday_is_not_an_interior_gap():
    """4 July 2025 fell on a Friday and NYSE was shut. A NYSE-calendar ticker
    with no bar that day is correct, not holed."""
    sessions = ["2025-07-01", "2025-07-02", "2025-07-03", "2025-07-07"]
    entry = {"dates": sessions, "prices": [1.0] * len(sessions)}
    assert ehp.interior_gaps(entry, "SPY") == []


# ---------------------------------------------------------------------------
# Retirement is a local privilege
#
# 2026-08-27: the daily runner dropped 26 tickers as "retired" (159801.SZ, TLT,
# GLD, VNQ, XME, ...) and committed a 32-ticker panel over the 58-ticker one a
# local refresh had written. They were never retired — collect_all_tickers
# reads the gitignored rotation parquets, so on a runner the requested set
# collapses to a floor and every cache-derived name looks absent. This is the
# same shrink the coverage guard was written to stop, re-opened by the
# retirement branch added for the EXH3 ghost.
# ---------------------------------------------------------------------------
def test_universe_sources_present_reflects_the_rotation_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    assert ehp.universe_sources_present() is False
    idx = pd.bdate_range(end="2026-08-25", periods=5)
    pd.DataFrame({"TLT": np.arange(5, dtype=float)}, index=idx).to_parquet(
        tmp_path / "asset_class_prices_cache.parquet")
    assert ehp.universe_sources_present() is False, "one cache is not the universe"
    pd.DataFrame({"ARKG": np.arange(5, dtype=float)}, index=idx).to_parquet(
        tmp_path / "thematic_prices_cache.parquet")
    assert ehp.universe_sources_present() is True


def test_runner_without_the_universe_caches_carries_forward(tmp_path, monkeypatch):
    """A run that cannot see the universe must not shrink the panel."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    out_path = tmp_path / "holdings_prices_1y.json"
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"SPY"})
    monkeypatch.setattr(ehp, "collect_book_symbols", lambda: {"SPY"})
    monkeypatch.setattr(ehp, "NETWORK_FALLBACK_TICKERS", ["SPY"])
    monkeypatch.setattr(ehp, "fetch_missing_from_yfinance",
                        lambda tks, gaps_out=None: {})

    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    out_path.write_text(json.dumps({
        "computed_at_utc": "2026-08-26T08:56:00+00:00", "lookback_days": 252,
        "prices": {"SPY": _panel_entry(dates, prices),
                   "TLT": _panel_entry(dates, prices),
                   "GLD": _panel_entry(dates, prices)},
    }), encoding="utf-8")

    assert ehp.main([]) == 0
    published = json.loads(out_path.read_text(encoding="utf-8"))["prices"]
    assert set(published) == {"SPY", "TLT", "GLD"}, (
        f"the runner shrank the panel to {sorted(published)}")


def test_a_local_run_that_sees_the_universe_still_retires(tmp_path, monkeypatch):
    """Control: with both rotation caches present the run CAN judge the
    universe, so a genuinely dropped line (the EXH3 ghost) is still retired."""
    monkeypatch.setattr(ehp, "DATA_DIR", tmp_path)
    out_path = tmp_path / "holdings_prices_1y.json"
    monkeypatch.setattr(ehp, "OUT_PATH", out_path)
    monkeypatch.setattr(ehp, "collect_all_tickers", lambda: {"SPY"})
    monkeypatch.setattr(ehp, "collect_book_symbols", lambda: {"SPY"})
    monkeypatch.setattr(ehp, "NETWORK_FALLBACK_TICKERS", ["SPY"])
    monkeypatch.setattr(ehp, "fetch_missing_from_yfinance",
                        lambda tks, gaps_out=None: {})

    idx = pd.bdate_range(end="2026-08-25", periods=5)
    for name in ("asset_class_prices_cache", "thematic_prices_cache"):
        pd.DataFrame({"SPY": np.arange(5, dtype=float)}, index=idx).to_parquet(
            tmp_path / f"{name}.parquet")

    dates = _long_dates()
    prices = [100.0 + i * 0.01 for i in range(len(dates))]
    out_path.write_text(json.dumps({
        "computed_at_utc": "2026-08-26T08:56:00+00:00", "lookback_days": 252,
        "prices": {"SPY": _panel_entry(dates, prices),
                   "EXH3.DE": _panel_entry(dates, prices)},
    }), encoding="utf-8")

    assert ehp.main([]) == 0
    published = json.loads(out_path.read_text(encoding="utf-8"))["prices"]
    assert "EXH3.DE" not in published, "a genuinely retired line must still go"
    assert "SPY" in published
