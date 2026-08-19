"""Offline tests for the engine price-panel guard.

REPRODUCES 2026-08-15. Strategy A backtested against a broken SOXX series
and published Sharpe 0.76 / CAGR 11.2% / total return +130% against
committed values of 0.93 / 16.9% / +238%. The run raised nothing. Every
downstream artefact inherited it, and the only thing that fired was a pinned
literal moving in tests/test_figure_bindings.py — a tripwire on the
consequence, not a check on the input.

``test_engine_refuses_a_universe_member_with_a_dead_close_series`` and its
neighbours below drive the whole shape through the same code path the engine
uses: build a panel, kill one member's close column, run the rotation, and
assert the run FAILS rather than emitting a plausible backtest. The
un-guarded engine emits one happily, and the test proves that by first
showing the corrupted backtest is plausible — finite Sharpe, ordinary
equity curve, attribution row present — before showing that the guard stops
it.

The other half is calibration. Every threshold in price_panel_guard was set
from the committed panels of all four sleeves on 2026-08-15, and the tests
that pin those measurements exist so that a later tightening has to argue
with the data rather than with a comment.
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


SESSIONS = 600


def _sessions(n: int = SESSIONS) -> pd.DatetimeIndex:
    return pd.bdate_range("2020-01-01", periods=n)


def _healthy(n: int = SESSIONS, seed: int = 0) -> pd.Series:
    """A price series that behaves like a price series."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0004, 0.01, n)
    return pd.Series(100.0 * np.exp(np.cumsum(steps)), index=_sessions(n))


def _panel(**columns: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(columns)


# =========================================================================
# The defect itself — a member whose close column is not there
# =========================================================================
def test_all_nan_column_fails():
    """The 2026-08-15 shape. SOXX was allocated to and scored at zero."""
    dead = pd.Series([np.nan] * SESSIONS, index=_sessions())
    v = g.assess_close_series(dead, "SOXX", panel_index=_sessions())
    assert v.status == g.FAIL
    assert "no valid close" in v.note


def test_flat_column_fails():
    flat = pd.Series([42.0] * SESSIONS, index=_sessions())
    v = g.assess_close_series(flat, "XLU", panel_index=_sessions())
    assert v.status == g.FAIL
    assert "flat" in v.note


def test_healthy_column_passes():
    v = g.assess_close_series(_healthy(), "XLE", panel_index=_sessions())
    assert v.status == g.PASS
    assert v.reasons == ()


def test_truncated_history_fails_even_though_it_looks_healthy():
    """The two-year vendor-fallback shape.

    Dense, unflat, flush with the tail — every check except the start says
    this is fine. It is the failure mode a coverage floor cannot see, because
    coverage is measured after the member's own first bar.
    """
    idx = _sessions()
    truncated = _healthy().copy()
    truncated.iloc[:400] = np.nan
    v = g.assess_close_series(truncated, "SOXX", panel_index=idx)
    assert v.status == g.FAIL
    assert "truncated" in v.note
    # And the reason it is invisible otherwise:
    assert v.coverage == pytest.approx(1.0)
    assert v.n_distinct > 100


def test_declared_late_inception_is_exempt_from_the_start_rule_only():
    """Sleeve C's 159801.SZ is legitimately late. It is not legitimately flat."""
    idx = _sessions()
    late = _healthy().copy()
    late.iloc[:400] = np.nan
    assert g.assess_close_series(late, "159801.SZ", panel_index=idx,
                                 allow_late=True).status == g.PASS

    late_and_flat = pd.Series([np.nan] * 400 + [7.0] * (SESSIONS - 400),
                              index=idx)
    v = g.assess_close_series(late_and_flat, "159801.SZ", panel_index=idx,
                              allow_late=True)
    assert v.status == g.FAIL
    assert "flat" in v.note


def test_series_that_stops_mid_panel_fails_on_the_tail():
    idx = _sessions()
    stopped = _healthy().copy()
    stopped.iloc[-20:] = np.nan
    v = g.assess_close_series(stopped, "EXH1.DE", panel_index=idx)
    assert v.status == g.FAIL
    assert "trails the panel" in v.note


def test_interior_hole_fails_on_the_gap_not_on_coverage():
    """A hole short enough to clear the coverage floor must still fail."""
    idx = _sessions()
    holed = _healthy().copy()
    holed.iloc[300:330] = np.nan          # 30 sessions of 600 = 5% missing
    v = g.assess_close_series(holed, "EXV3.DE", panel_index=idx)
    assert v.status == g.FAIL
    assert "interior gap" in v.note


def test_thin_history_skips_and_never_fails():
    """A guard that cries wolf on a short history is a guard that gets
    switched off. This is the check_pair_integrity precedent."""
    idx = _sessions()
    thin = pd.Series(np.nan, index=idx)
    thin.iloc[-10:] = _healthy(10).values
    v = g.assess_close_series(thin, "NEWETF", panel_index=idx)
    assert v.status == g.SKIP
    assert v.status != g.FAIL


def test_assert_panel_usable_names_the_broken_member_and_spares_the_rest():
    idx = _sessions()
    panel = _panel(
        XLE=_healthy(seed=1),
        XLF=_healthy(seed=2),
        SOXX=pd.Series([np.nan] * SESSIONS, index=idx),
    )
    with pytest.raises(g.DegeneratePriceError) as exc:
        g.assert_panel_usable(panel, "Strategy A closes")
    message = str(exc.value)
    assert "SOXX" in message
    assert "1 member(s) cannot be backtested" in message
    # The healthy members are reported as passing, not swept up in the failure.
    assert "XLE" in message and "XLF" in message


def test_assert_panel_usable_respects_the_backtest_window():
    """A member absent BEFORE the window is not a defect. Only the window the
    engine actually runs over is the question."""
    idx = _sessions()
    late_start = _healthy().copy()
    late_start.iloc[:200] = np.nan
    panel = _panel(XLE=_healthy(seed=3), IDP6=late_start)
    with pytest.raises(g.DegeneratePriceError):
        g.assert_panel_usable(panel, "whole panel")
    g.assert_panel_usable(panel, "from the window",
                          window_start=idx[200])


# =========================================================================
# The tell, as it reaches the artefact
# =========================================================================
def _row(days_held, ann, pnl=0.31):
    return {"days_held": days_held, "ann_return_when_held": ann,
            "contribution_to_total_return": pnl}


def test_the_2026_08_15_tell_is_caught():
    """days_held large, ann_return_when_held exactly 0.0."""
    attribution = {
        "SOXX": _row(1221, 0.0, 0.0),
        "CSP1": _row(1008, 0.1805),
    }
    hits = g.zero_return_rows(attribution)
    assert [m for m, _ in hits] == ["SOXX"]
    with pytest.raises(g.AttributionTellError) as exc:
        g.assert_attribution_sane(attribution, "Strategy A attribution")
    assert "SOXX" in str(exc.value)


def test_a_zero_contribution_on_a_held_member_is_also_the_tell():
    attribution = {"SOXX": _row(1221, 0.4979, 0.0)}
    assert [m for m, _ in g.zero_return_rows(attribution)] == ["SOXX"]


def test_a_never_held_member_is_not_the_tell():
    """days_held 0 legitimately carries ann_return_when_held None, and a
    genuinely brief holding must not be judged on an exact-zero rule."""
    assert g.zero_return_rows({"XLK": _row(0, None, 0.0)}) == []
    assert g.zero_return_rows({"XLK": _row(1, 0.0, 0.0)}) == []


def test_the_healthy_committed_attribution_passes():
    """A tiny but real return is not zero, and must not be rounded into one."""
    attribution = {
        "IUUS": _row(1258, 0.021696525645607645, 0.07217887174454904),
        "IUMS": _row(869, 0.08374005542716145, -0.005876985576032254),
    }
    assert g.zero_return_rows(attribution) == []
    g.assert_attribution_sane(attribution, "Strategy A attribution")


def test_the_comparison_against_zero_is_exact_by_design():
    """A tolerance would fire on a quiet holding and be tuned away. One
    thousandth of a basis point of annualised return is still not zero."""
    assert g.zero_return_rows({"XLP": _row(500, 1e-12)}) == []


# =========================================================================
# End to end — the engine must fail rather than emit a plausible backtest
# =========================================================================
def _rotation(closes: pd.DataFrame, signal: pd.DataFrame,
              eligible: pd.Timestamp) -> dict:
    """A minimal top-2 weekly rotation, the same shape as run_portfolio:
    weights from the prior session's signal, returns from pct_change with
    NaN filled to zero. That fillna is what converts a missing close column
    into a confident zero return."""
    rebalance = closes.loc[closes.index >= eligible].resample("W-FRI").last().index
    rebalance = [d for d in closes.index if d in set(rebalance)] or list(
        closes.index[closes.index >= eligible][::5])
    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for date in rebalance:
        prev = closes.index.get_loc(date) - 1
        if prev < 0:
            continue
        top = signal.iloc[prev].nlargest(2).index
        weights.loc[date:, :] = 0.0
        weights.loc[date:, top] = 0.5
    weights.loc[weights.index < eligible] = 0.0
    rets = closes.pct_change().fillna(0)
    used_w = weights.shift(1).fillna(0)
    equity = (1.0 + (used_w * rets).sum(axis=1)).cumprod()
    attribution = {}
    for etf in closes.columns:
        held = used_w[etf] > 1e-6
        n_held = int(held.sum())
        if n_held == 0:
            attribution[etf] = {"days_held": 0, "ann_return_when_held": None,
                                "contribution_to_total_return": 0.0}
            continue
        mean_daily = float(rets.loc[held, etf].mean())
        attribution[etf] = {
            "days_held": n_held,
            "ann_return_when_held": (1.0 + mean_daily) ** 252 - 1.0,
            "contribution_to_total_return": float((used_w[etf] * rets[etf]).sum()),
        }
    return {"equity": equity, "attribution": attribution}


def _corrupted_panel(kind: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """A three-member universe with one member's close column destroyed, and
    a signal panel that keeps electing it — which is the whole problem: the
    breadth panel was healthy on 2026-08-15, so the sleeve went on holding
    the member whose prices had gone."""
    idx = _sessions()
    closes = _panel(XLE=_healthy(seed=11), XLF=_healthy(seed=12),
                    SOXX=_healthy(seed=13))
    if kind == "nan":
        closes["SOXX"] = np.nan
    else:
        closes["SOXX"] = 55.0
    signal = pd.DataFrame(0.0, index=idx, columns=closes.columns)
    signal["SOXX"] = 1.0      # always ranked first
    signal["XLE"] = 0.5
    return closes, signal, idx[210]


@pytest.mark.parametrize("kind", ["nan", "flat"])
def test_the_unguarded_engine_emits_a_plausible_backtest(kind):
    """The premise. Nothing about the corrupted result LOOKS wrong: the
    equity curve is finite and monotone-ish, the Sharpe is orderly, and the
    attribution row for the dead member is present and populated. This is why
    it shipped."""
    closes, signal, eligible = _corrupted_panel(kind)
    result = _rotation(closes, signal, eligible)
    equity = result["equity"].loc[eligible:]
    assert np.isfinite(equity.iloc[-1]) and equity.iloc[-1] > 0
    row = result["attribution"]["SOXX"]
    assert row["days_held"] > 100
    # The only thing that gives it away, and only if you know to look.
    assert row["ann_return_when_held"] == 0.0


@pytest.mark.parametrize("kind", ["nan", "flat"])
def test_engine_refuses_a_universe_member_with_a_dead_close_series(kind):
    """The regression. Fed a flat or NaN close series for one universe
    member, the guarded path must FAIL rather than emit that backtest."""
    closes, signal, eligible = _corrupted_panel(kind)
    with pytest.raises(g.DegeneratePriceError) as exc:
        g.assert_panel_usable(closes, "Strategy A closes",
                              window_start=eligible)
    assert "SOXX" in str(exc.value)


@pytest.mark.parametrize("kind", ["nan", "flat"])
def test_the_attribution_gate_catches_it_even_if_the_panel_guard_does_not(kind):
    """Belt and braces. If a future route into a dead return column slips
    past the panel guard, the sleeve still must not be written."""
    closes, signal, eligible = _corrupted_panel(kind)
    attribution = _rotation(closes, signal, eligible)["attribution"]
    with pytest.raises(g.AttributionTellError):
        g.assert_attribution_sane(attribution, "Strategy A attribution")


def test_the_same_engine_on_a_healthy_panel_is_untouched():
    """The guard must not fire on the run it is meant to allow — otherwise it
    gets switched off, and the next SOXX ships."""
    idx = _sessions()
    closes = _panel(XLE=_healthy(seed=21), XLF=_healthy(seed=22),
                    SOXX=_healthy(seed=23))
    signal = pd.DataFrame(
        {c: _healthy(seed=30 + i) for i, c in enumerate(closes.columns)},
        index=idx)
    eligible = idx[210]
    g.assert_panel_usable(closes, "Strategy A closes", window_start=eligible)
    attribution = _rotation(closes, signal, eligible)["attribution"]
    g.assert_attribution_sane(attribution, "Strategy A attribution")


# =========================================================================
# The fetch that wrote the broken cache in the first place
# =========================================================================
@pytest.fixture()
def ohlc_cache(tmp_path, monkeypatch):
    """Point backtest.download_soxx_ohlc at a throwaway cache path."""
    import backtest

    path = tmp_path / "soxx_ohlc_cache.parquet"
    monkeypatch.setattr(backtest, "paths_for",
                        lambda etf: {"ohlc_cache": path})
    return backtest, path


def _ohlc(n: int = 300, flat: bool = False) -> pd.DataFrame:
    close = pd.Series([50.0] * n, index=_sessions(n)) if flat else _healthy(n)
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close})


@pytest.mark.parametrize("bad", [
    pd.DataFrame(),                                    # vendor returned nothing
    pd.DataFrame({"Close": []}),                       # no usable bar
    _ohlc(flat=True),                                  # a flat quote, not a series
])
def test_a_degenerate_fetch_never_overwrites_a_good_cache(ohlc_cache, bad,
                                                          monkeypatch):
    """The write that broke SOXX. The engines re-fetch every run — the cache
    reuse branch cannot fire for a sleeve panel, because the requested window
    ends five days past the last session — so whatever the vendor returned
    used to land straight on top of nine years of good history."""
    backtest, path = ohlc_cache
    good = _ohlc()
    good.to_parquet(path)

    monkeypatch.setattr(backtest.yf, "download",
                        lambda *a, **k: bad, raising=False)
    out = backtest.download_soxx_ohlc("2020-01-01", "2099-01-01", etf="SOXX")

    on_disk = pd.read_parquet(path)
    assert len(on_disk) == len(good)
    assert on_disk["Close"].nunique() == good["Close"].nunique()
    # ... and the caller is handed the cache, not the degenerate response.
    assert len(out) == len(good)


def test_a_degenerate_fetch_with_no_cache_to_fall_back_on_raises(ohlc_cache,
                                                                 monkeypatch):
    """There is nothing usable and nothing to fall back on. Returning an
    empty frame here is what let an engine build a backtest out of nothing."""
    backtest, _ = ohlc_cache
    monkeypatch.setattr(backtest.yf, "download",
                        lambda *a, **k: pd.DataFrame(), raising=False)
    with pytest.raises(g.DegeneratePriceError) as exc:
        backtest.download_soxx_ohlc("2020-01-01", "2099-01-01", etf="SOXX")
    assert "SOXX" in str(exc.value)


def test_a_close_only_fetch_falls_back_rather_than_raising_keyerror(
        ohlc_cache, monkeypatch):
    """The response carries Close but no Open/High/Low, and the next line
    used to slice all four columns. A KeyError here would be a crash, not a
    guard — and a Close-only CACHE is still a perfectly good fallback, which
    is why the two frames are judged by different rules."""
    backtest, path = ohlc_cache
    good = _ohlc()
    good[["Close"]].to_parquet(path)      # the export backfill's cache shape
    monkeypatch.setattr(backtest.yf, "download",
                        lambda *a, **k: _ohlc()[["Close"]], raising=False)
    out = backtest.download_soxx_ohlc("2020-01-01", "2099-01-01", etf="SOXX")
    assert list(out.columns) == ["Close"]
    assert len(out) == len(good)


def test_a_healthy_fetch_still_refreshes_the_cache(ohlc_cache, monkeypatch):
    """The guard must not stop the normal path — a fetch that IS a price
    series is written and returned exactly as before."""
    backtest, path = ohlc_cache
    _ohlc(100).to_parquet(path)
    fresh = _ohlc(300)
    monkeypatch.setattr(backtest.yf, "download",
                        lambda *a, **k: fresh, raising=False)
    out = backtest.download_soxx_ohlc("2020-01-01", "2099-01-01", etf="SOXX")
    assert len(out) == 300
    assert len(pd.read_parquet(path)) == 300


def test_a_healthy_short_span_fetch_never_truncates_the_cache(ohlc_cache,
                                                              monkeypatch):
    """2026-08-19, sleeve D. The daily two-year backfill is dense, unflat
    and flush at the tail — every degeneracy rule passes — and it used to
    land on top of nine years of history; the next cold rebuild then
    collapsed onto the surviving stub (blend Sharpe +1.99 against a
    committed +1.29). The write is refused; the caller still gets exactly
    the window the vendor served."""
    backtest, path = ohlc_cache
    good = _ohlc()
    good.to_parquet(path)
    short = good.iloc[150:]              # starts 150 sessions later, same end
    monkeypatch.setattr(backtest.yf, "download",
                        lambda *a, **k: short, raising=False)
    out = backtest.download_soxx_ohlc("2020-01-01", "2099-01-01", etf="SOXX")

    on_disk = pd.read_parquet(path)
    assert on_disk.index.min() == good.index.min(), (
        "a short-span fetch truncated the cache's first bar")
    assert len(on_disk) == len(good)
    assert len(out) == len(short), "the caller must still get the fetch"


def test_a_healthy_earlier_ending_fetch_never_shears_the_cache_tail(
        ohlc_cache, monkeypatch):
    """The mirror image: an extended-history request that comes back healthy
    but ends before the cache's last bar must not delete the newer bars."""
    backtest, path = ohlc_cache
    good = _ohlc()
    good.to_parquet(path)
    early = good.iloc[:150]              # same first bar, ends 150 sessions early
    monkeypatch.setattr(backtest.yf, "download",
                        lambda *a, **k: early, raising=False)
    out = backtest.download_soxx_ohlc("2020-01-01", "2099-01-01", etf="SOXX")

    on_disk = pd.read_parquet(path)
    assert on_disk.index.max() == good.index.max(), (
        "an earlier-ending fetch sheared the cache's newest bars")
    assert len(on_disk) == len(good)
    assert len(out) == len(early)


def test_spy_close_cache_is_never_truncated_by_a_short_fetch(tmp_path,
                                                             monkeypatch):
    """download_spy_close shares the write pattern, so it shares the rule."""
    import backtest

    spy_path = tmp_path / "spy_close_cache.parquet"
    monkeypatch.setattr(backtest, "SPY_CACHE", spy_path)
    full = _ohlc()[["Close"]]
    full.to_parquet(spy_path)
    short = full.iloc[150:]
    monkeypatch.setattr(backtest.yf, "download",
                        lambda *a, **k: short, raising=False)
    out = backtest.download_spy_close("2020-01-01", "2099-01-01")

    assert pd.read_parquet(spy_path).index.min() == full.index.min(), (
        "a short-span SPY fetch truncated the close cache")
    assert out.index[0] == short.index[0]


# =========================================================================
# The step-2b repair — what it may and may not write over
# =========================================================================
def _ohlc_frame(n: int = 100, start: str = "2020-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n)
    close = pd.Series(np.linspace(100.0, 200.0, n), index=idx)
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close})


@pytest.mark.parametrize("fetched, expected", [
    (pd.DataFrame(), "nothing usable"),
    (pd.DataFrame({"X": [1, 2]}), "nothing usable"),
])
def test_the_repair_refuses_an_empty_or_shapeless_response(fetched, expected):
    import export_holdings_prices as ehp
    assert expected in ehp._fetched_frame_is_worse(fetched, _ohlc_frame())


def test_the_repair_refuses_a_response_that_ends_earlier_than_the_cache():
    """A vendor never un-prints a close. This is the rule that keeps the
    fallback the engines rely on from being replaced by the fault."""
    import export_holdings_prices as ehp
    cache = _ohlc_frame()
    assert "already ends" in ehp._fetched_frame_is_worse(cache.iloc[:50], cache)


def test_the_repair_refuses_a_response_truncated_at_the_front():
    """The two-year fallback shape again, caught one layer earlier — before
    it is written, rather than after the engine has priced a sleeve off it."""
    import export_holdings_prices as ehp
    cache = _ohlc_frame()
    assert "already starts" in ehp._fetched_frame_is_worse(cache.iloc[50:], cache)


def test_the_repair_writes_a_genuinely_better_response():
    import export_holdings_prices as ehp
    cache = _ohlc_frame(100)
    longer = _ohlc_frame(150)
    assert ehp._fetched_frame_is_worse(longer, cache) is None
    assert ehp._fetched_frame_is_worse(longer, None) is None


def test_the_write_rule_refuses_a_first_bar_that_slips_across_a_year_boundary():
    """Date edge case (year boundary): the cache opens 2019-12-24, the fetch
    opens 2020-01-01. A one-week shrink across the year end is still a
    refusal — pandas Timestamps compare; nothing is computed by hand."""
    idx = pd.bdate_range("2019-12-24", "2020-01-31")
    cache = pd.DataFrame(
        {"Close": pd.Series(np.linspace(100.0, 120.0, len(idx)), index=idx)})
    fetched = cache.loc["2020-01-01":]
    assert "already starts" in g.fetched_frame_is_worse(fetched, cache)
    # The same pair the other way round extends the start, and writes.
    assert g.fetched_frame_is_worse(cache, fetched) is None


def test_the_write_rule_refuses_a_last_bar_that_slips_across_a_month_boundary():
    """Date edge case (month boundary, leap February): the cache ends
    2020-03-05, the fetch ends 2020-02-28 — the last business day of a leap
    February. Ending a few sessions earlier across the month end is refused."""
    idx = pd.bdate_range("2020-01-15", "2020-03-05")
    cache = pd.DataFrame(
        {"Close": pd.Series(np.linspace(100.0, 120.0, len(idx)), index=idx)})
    fetched = cache.loc[:"2020-02-28"]
    assert "already ends" in g.fetched_frame_is_worse(fetched, cache)
    assert g.fetched_frame_is_worse(cache, fetched) is None


def test_the_repair_takes_its_window_from_the_constituent_panel_not_the_cache():
    """A cache truncated to a vendor fallback must not be 'repaired' back to
    its own truncated start — the window comes from the constituent panel,
    which step 1 refreshed and which the truncation never touched.

    Constituent caches are gitignored, so on a runner there is nothing to
    take a window from and the fallback start applies. Both branches are
    asserted; neither is a failure."""
    import export_holdings_prices as ehp

    start, end = ehp.engine_cache_window("SOXX")
    if (ehp.DATA_DIR / "prices_cache_soxx.parquet").exists():
        assert start < "2018-01-01"   # the full history, not a 2y window
        assert end is not None and end > start
    else:
        assert (start, end) == (ehp.DEFAULT_OHLC_START, None)


# =========================================================================
# Calibration — the thresholds are measurements, and these pin them
# =========================================================================
def test_thresholds_sit_clear_of_what_the_committed_panels_measured():
    """Measured 2026-08-15 across all four sleeves, 58 members:
    worst coverage-after-first-bar 0.9976 (159801.SZ), worst interior gap 4
    sessions (159801.SZ), worst tail lag 1 session (BTC-USD), worst
    undeclared late start 0 sessions. Each threshold must sit in the empty
    space beyond those, not just outside them."""
    assert g.MIN_COVERAGE_AFTER_FIRST < 0.9976
    assert g.MAX_INTERIOR_GAP_SESSIONS > 4
    assert g.MAX_MEMBER_LAG_SESSIONS > 1
    assert g.MAX_LATE_START_SESSIONS > 0
    # ... and not so far out that the defect walks through.
    assert g.MIN_COVERAGE_AFTER_FIRST >= 0.90
    assert g.MAX_INTERIOR_GAP_SESSIONS <= 21
    assert g.MAX_MEMBER_LAG_SESSIONS <= 5


def test_longest_nan_run_counts_the_longest_run_not_the_total():
    s = pd.Series([1.0, np.nan, np.nan, 2.0, np.nan, 3.0])
    assert g.longest_nan_run(s) == 2
    assert g.longest_nan_run(pd.Series([1.0, 2.0])) == 0


def test_infinities_are_treated_as_missing_not_as_prices():
    """A parquet round-trip can carry an infinity through, and it reads as
    present to notna()."""
    idx = _sessions(40)
    s = pd.Series(np.inf, index=idx)
    v = g.assess_close_series(s, "BROKEN", panel_index=idx, min_obs=10)
    assert v.status == g.FAIL
    assert "no valid close" in v.note
