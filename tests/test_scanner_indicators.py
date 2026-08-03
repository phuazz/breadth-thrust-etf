"""Guard layer for the ETF scanner's indicator library.

The scanner is a monitoring page with no downstream consumer that would
notice a wrong number, so a silently-wrong indicator is its defining risk.
Four kinds of test, doing four different jobs:

1. **Arithmetic anchors** — hand-verifiable answers on constructed series
   (the SMA of 1..20 is 10.5; RSI of a monotonic rise is 100). These are
   the only tests that establish the mechanics are *correct* rather than
   merely stable, because the expected value is checkable by inspection.

2. **Naive-divergence** — the vectorised pandas path versus the
   deliberately naive loop path in ``scanner_indicators``, on real
   fixture data. Catches rolling-window and alignment regressions, which
   change every number at once and break no test that shares the
   implementation under test.

3. **Frozen pins** — exact indicator values on the committed fixture.
   These lock behaviour: any refactor that shifts a number fails here.
   They are regression anchors, NOT external validation — they were
   produced by this code. Spec §9.1's cross-check of SOXX / EXV1.DE /
   159801.SZ against TradingView is a separate, human acceptance step and
   is not discharged by this file. Where an independent recomputation is
   cheap (a 200-bar mean, a 252-bar max) the pin is checked against
   straight numpy as well, so those are cross-validated rather than
   merely pinned.

4. **Invariants** — the properties ``run_scanner`` will assert on every
   build: ranks form a permutation, percentiles are withheld rather than
   guessed on short history, deltas are absent rather than fabricated.

Fixture: ``tests/fixtures/scanner_prices.parquet``, three tickers on three
calendars, built by ``scripts/build_scanner_fixture.py``. Prices are raw
(unconverted); FX is not this module's concern.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scanner_indicators as si  # noqa: E402
from build_scanner_fixture import FIXTURE_PATH, load_fixture  # noqa: E402

FIXTURE_TICKERS = ("SOXX", "EXV1.DE", "159801.SZ")


@pytest.fixture(scope="module")
def prices() -> dict[str, pd.DataFrame]:
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"fixture missing: {FIXTURE_PATH.name} — build it with "
            f"`python scripts/build_scanner_fixture.py`"
        )
    return load_fixture()


def _series(values: list[float], start: str = "2020-01-01") -> pd.Series:
    """Series on a business-day index, so date-sensitive helpers see dates."""
    idx = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=idx, dtype="float64")


# =========================================================================
# 1. Arithmetic anchors — expected values verifiable by inspection
# =========================================================================
def test_sma_of_arithmetic_series():
    """Mean of 1..20 is 210/20 = 10.5."""
    assert si.sma(_series([float(i) for i in range(1, 21)]), 20).iloc[-1] == 10.5


def test_sma_is_nan_before_the_window_fills():
    """No partial-window averages: 19 bars cannot produce a 20-bar mean."""
    s = _series([float(i) for i in range(1, 20)])
    assert np.isnan(si.sma(s, 20).iloc[-1])


def test_rsi_monotone_rise_is_100():
    """Every bar a gain: average loss is zero, RSI saturates at 100."""
    s = _series([100.0 + i for i in range(40)])
    assert si.rsi(s).iloc[-1] == pytest.approx(100.0)
    assert si.naive_rsi_latest(s.tolist()) == pytest.approx(100.0)


def test_rsi_monotone_fall_is_zero():
    s = _series([200.0 - i for i in range(40)])
    assert si.rsi(s).iloc[-1] == pytest.approx(0.0)
    assert si.naive_rsi_latest(s.tolist()) == pytest.approx(0.0)


def test_rsi_flat_series_is_neutral_and_paths_agree():
    """A flat series has no gains and no losses.

    This is the case where the two implementations previously disagreed:
    the vectorised mask order returned 0 (maximally oversold) while the
    naive short-circuit returned 100. Both now return the documented
    neutral 50, and the point of the test is that they AGREE.
    """
    s = _series([50.0] * 40)
    assert si.rsi(s).iloc[-1] == pytest.approx(50.0)
    assert si.naive_rsi_latest(s.tolist()) == pytest.approx(50.0)


def test_true_range_uses_the_prior_close():
    """TR = max(H-L, |H-Cprev|, |L-Cprev|), and bar one has no prior close.

    Bar 2: H-L = 12-8 = 4; |H-Cprev| = |12-10| = 2; |L-Cprev| = |8-10| = 2.
    Bar 3 gaps up: H-L = 30-25 = 5; |30-9| = 21; |25-9| = 16 -> 21.
    """
    high = _series([11.0, 12.0, 30.0])
    low = _series([9.0, 8.0, 25.0])
    close = _series([10.0, 9.0, 28.0])
    tr = si.true_range(high, low, close)
    assert np.isnan(tr.iloc[0])
    assert tr.iloc[1] == pytest.approx(4.0)
    assert tr.iloc[2] == pytest.approx(21.0)


def test_wilder_smooth_seed_then_recursion():
    """Seed is the simple mean of the first n; then (prev*(n-1)+x)/n.

    Fourteen 1.0s seed at 1.0. The fifteenth observation of 15.0 gives
    (1.0*13 + 15.0)/14 = 28/14 = 2.0.
    """
    s = _series([1.0] * 14 + [15.0])
    out = si.wilder_smooth(s, 14)
    assert np.isnan(out.iloc[12])
    assert out.iloc[13] == pytest.approx(1.0)
    assert out.iloc[14] == pytest.approx(2.0)


def test_percentile_of_score_splits_ties():
    """Window [1,2,2,3] with value 2: one below, two equal -> (1+1)/4 = 50%."""
    assert si.percentile_of_score(np.array([1.0, 2.0, 2.0, 3.0]), 2.0) == pytest.approx(
        50.0
    )


def test_percentile_of_score_at_the_extremes():
    """The minimum of four distinct values sits at (0 + 0.5)/4 = 12.5%."""
    window = np.array([1.0, 2.0, 3.0, 4.0])
    assert si.percentile_of_score(window, 1.0) == pytest.approx(12.5)
    assert si.percentile_of_score(window, 4.0) == pytest.approx(87.5)


def test_bollinger_bandwidth_of_a_constant_series_is_zero():
    """No dispersion means no bandwidth — and no division-by-zero."""
    s = _series([25.0] * 40)
    assert si.bollinger_bandwidth(s).iloc[-1] == pytest.approx(0.0)


def test_vs_52w_high_is_zero_at_a_fresh_high_and_negative_below():
    rising = _series([100.0 + i for i in range(300)])
    assert si.vs_52w_high(rising) == pytest.approx(0.0)
    pulled_back = _series([100.0 + i for i in range(300)] + [200.0])
    assert si.vs_52w_high(pulled_back) < 0.0


def test_momentum_12_1_ignores_the_most_recent_month():
    """The skip month is the whole point: a crash in the last 21 sessions
    must not touch 12-1, which measures t-252 to t-21."""
    steady = [100.0 + i for i in range(260)]
    crashed = steady[:-21] + [10.0] * 21
    assert si.momentum_12_1(_series(steady)) == pytest.approx(
        si.momentum_12_1(_series(crashed))
    )


def test_momentum_12_1_is_nan_without_a_full_year():
    assert np.isnan(si.momentum_12_1(_series([100.0] * 200)))


# =========================================================================
# Trend-state precedence (the overlapping-category decision)
# =========================================================================
def test_trend_state_is_none_before_ma200_warms_up():
    assert si.trend_state(_series([100.0 + i for i in range(150)])) is None


def test_trend_state_strong_up_on_a_clean_uptrend():
    s = _series([100.0 + 0.5 * i for i in range(400)])
    assert si.trend_state(s) == si.TREND_STRONG_UP


def test_trend_state_strong_down_on_a_clean_downtrend():
    s = _series([300.0 - 0.5 * i for i in range(400)])
    assert si.trend_state(s) == si.TREND_STRONG_DOWN


def test_trend_state_range_when_the_mas_are_flat():
    """A constant series has MA50 == MA200, inside the 1% flat tolerance."""
    s = _series([120.0] * 400)
    assert si.trend_state(s) == si.TREND_RANGE


def test_trend_state_range_when_price_sits_between_the_mas():
    """Long rally then a pullback that lands price between the two MAs."""
    s = _series([100.0 + 0.5 * i for i in range(360)] + [250.0] * 5)
    ma50 = si.sma(s, si.MA_MID).iloc[-1]
    ma200 = si.sma(s, si.MA_LONG).iloc[-1]
    close = s.iloc[-1]
    assert min(ma50, ma200) <= close <= max(ma50, ma200), (
        f"construction drifted: close={close:.2f} is not between "
        f"MA50={ma50:.2f} and MA200={ma200:.2f}"
    )
    assert si.trend_state(s) == si.TREND_RANGE


def test_trend_state_up_when_above_ma200_but_ma50_is_below_it():
    """Long decline (MA50 under MA200) then a sharp rally above both.

    Strong up requires C > MA50 > MA200; with MA50 below MA200 that fails
    and the state falls through to plain Up.
    """
    s = _series([300.0 - 0.5 * i for i in range(300)] + [150.0 + 11.0 * i for i in range(1, 16)])
    ma50 = si.sma(s, si.MA_MID).iloc[-1]
    ma200 = si.sma(s, si.MA_LONG).iloc[-1]
    assert ma50 < ma200 < s.iloc[-1], (
        f"construction drifted: MA50={ma50:.2f}, MA200={ma200:.2f}, "
        f"close={s.iloc[-1]:.2f}"
    )
    assert si.trend_state(s) == si.TREND_UP


def test_trend_state_down_when_below_ma200_but_ma50_is_above_it():
    s = _series([100.0 + 0.5 * i for i in range(300)] + [250.0 - 13.0 * i for i in range(1, 16)])
    ma50 = si.sma(s, si.MA_MID).iloc[-1]
    ma200 = si.sma(s, si.MA_LONG).iloc[-1]
    assert s.iloc[-1] < ma200 < ma50, (
        f"construction drifted: close={s.iloc[-1]:.2f}, MA200={ma200:.2f}, "
        f"MA50={ma50:.2f}"
    )
    assert si.trend_state(s) == si.TREND_DOWN


# =========================================================================
# Date-boundary cases (vault rule: one month boundary, one year boundary)
# =========================================================================
def test_total_return_across_a_month_boundary_picks_the_exact_bar():
    """Lookback is positional, so it must land on the bar 5 sessions back
    even when the span crosses the end of a month."""
    idx = pd.bdate_range("2026-01-26", periods=6)   # Mon 26 Jan -> Mon 2 Feb
    s = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 20.0], index=idx)
    assert s.index[-1].month == 2 and s.index[-6].month == 1, "span must cross January"
    assert si.total_return(s, 5) == pytest.approx(20.0 / 10.0 - 1.0)


def test_total_return_across_a_year_boundary_picks_the_exact_bar():
    idx = pd.bdate_range("2025-12-29", periods=5)   # Mon 29 Dec -> Fri 2 Jan
    s = pd.Series([50.0, 51.0, 52.0, 53.0, 100.0], index=idx)
    assert s.index[-1].year == 2026 and s.index[0].year == 2025, "span must cross 2025/26"
    assert si.total_return(s, 4) == pytest.approx(1.0)


# =========================================================================
# 2. Naive-divergence guard, on real fixture data
# =========================================================================
@pytest.mark.parametrize("ticker", FIXTURE_TICKERS)
def test_vectorised_sma_matches_the_naive_path(prices, ticker):
    close = prices[ticker]["close"]
    assert si.sma(close, 20).iloc[-1] == pytest.approx(
        si.naive_sma_latest(close.tolist(), 20), rel=1e-12
    )


@pytest.mark.parametrize("ticker", FIXTURE_TICKERS)
def test_vectorised_rsi_matches_the_naive_path(prices, ticker):
    close = prices[ticker]["close"]
    assert si.rsi(close).iloc[-1] == pytest.approx(
        si.naive_rsi_latest(close.tolist()), rel=1e-9
    )


@pytest.mark.parametrize("ticker", FIXTURE_TICKERS)
def test_vectorised_atr_matches_the_naive_path(prices, ticker):
    f = prices[ticker]
    assert si.atr_pct(f["high"], f["low"], f["close"]) == pytest.approx(
        si.naive_atr_pct_latest(
            f["high"].tolist(), f["low"].tolist(), f["close"].tolist()
        ),
        rel=1e-9,
    )


# =========================================================================
# 3. Frozen pins on the committed fixture (last bar 2026-07-31)
# =========================================================================
# Full float precision, not rounded for readability: a pin rounded to six
# decimals is coarser than the tolerance it is asserted with, so small
# values (dev_200d ~0.086) fail on the pin's own rounding rather than on a
# real change. Regenerate deliberately if the fixture is ever rebuilt.
EXPECTED = {
    "SOXX": {
        "bars": 1045,
        "close": 504.8900146484375,
        "ma200": 402.5118858337402,
        "rsi14": 42.60394008681776,
        "atr_pct": 0.06297255120713766,
        "mom_12_1": 1.4299189998971817,
        "vs_52w_high": -0.22918732977974376,
        "dev_200d": 0.25434808863504,
        "rv_pctl": 87.20238095238095,
        "bbw_pctl": 81.64682539682539,
        "vol_ratio": 1.2171884259846104,
        "trend": si.TREND_RANGE,
    },
    "EXV1.DE": {
        "bars": 1061,
        "close": 41.9900016784668,
        "ma200": 34.81875039100647,
        "rsi14": 64.02004396687136,
        "atr_pct": 0.016740107688016113,
        "mom_12_1": 0.4457567795048656,
        "vs_52w_high": 0.0,
        "dev_200d": 0.20595946744006732,
        "rv_pctl": 65.97222222222223,
        "bbw_pctl": 25.892857142857142,
        "vol_ratio": 0.876849896286092,
        "trend": si.TREND_STRONG_UP,
    },
    "159801.SZ": {
        "bars": 1011,
        "close": 1.1089999675750732,
        "ma200": 1.0211499992012978,
        "rsi14": 36.53420886156162,
        "atr_pct": 0.08888989971876216,
        "mom_12_1": 1.4750830801621087,
        "vs_52w_high": -0.33791044827106675,
        "dev_200d": 0.08603042495469637,
        "rv_pctl": 98.51190476190476,
        "bbw_pctl": 95.93253968253968,
        "vol_ratio": 1.550389507658542,
        "trend": si.TREND_RANGE,
    },
}


@pytest.mark.parametrize("ticker", FIXTURE_TICKERS)
def test_fixture_shape_is_stable(prices, ticker):
    """Bar counts and the last date are pinned: a vendor restatement that
    changes the panel would otherwise silently move every pin below."""
    frame = prices[ticker]
    assert len(frame) == EXPECTED[ticker]["bars"]
    assert frame.index[-1] == pd.Timestamp("2026-07-31")


@pytest.mark.parametrize("ticker", FIXTURE_TICKERS)
def test_pinned_indicator_values(prices, ticker):
    f = prices[ticker]
    close, high, low, volume = f["close"], f["high"], f["low"], f["volume"]
    want = EXPECTED[ticker]

    # rel=1e-12: the pins carry full precision, so this is a genuine
    # exact-equality guard rather than a loose sanity band.
    assert close.iloc[-1] == pytest.approx(want["close"], rel=1e-12)
    assert si.sma(close, si.MA_LONG).iloc[-1] == pytest.approx(want["ma200"], rel=1e-12)
    assert si.rsi(close).iloc[-1] == pytest.approx(want["rsi14"], rel=1e-12)
    assert si.atr_pct(high, low, close) == pytest.approx(want["atr_pct"], rel=1e-12)
    assert si.momentum_12_1(close) == pytest.approx(want["mom_12_1"], rel=1e-12)
    assert si.vs_52w_high(close) == pytest.approx(want["vs_52w_high"], abs=1e-12)
    assert si.dev_from_ma(close) == pytest.approx(want["dev_200d"], rel=1e-12)
    assert si.volume_ratio(volume) == pytest.approx(want["vol_ratio"], rel=1e-12)
    assert si.trend_state(close) == want["trend"]

    rv = si.percentile_of_latest(si.realised_vol(close))
    bbw = si.percentile_of_latest(si.bollinger_bandwidth(close))
    assert rv.value == pytest.approx(want["rv_pctl"], rel=1e-12)
    assert bbw.value == pytest.approx(want["bbw_pctl"], rel=1e-12)
    assert rv.n_obs == si.PCTL_WINDOW and not rv.truncated
    assert bbw.n_obs == si.PCTL_WINDOW and not bbw.truncated


@pytest.mark.parametrize("ticker", FIXTURE_TICKERS)
def test_pins_agree_with_straight_numpy(prices, ticker):
    """Independent recomputation of the two pins that admit one cheaply.

    A 200-bar mean and a 252-bar maximum need no library: if the pinned
    MA200 and 52-week-high figures came from a broken rolling window,
    these disagree.
    """
    close = prices[ticker]["close"].to_numpy()
    want = EXPECTED[ticker]
    assert float(np.mean(close[-si.MA_LONG:])) == pytest.approx(
        want["ma200"], rel=1e-6
    )
    assert float(
        close[-1] / np.max(close[-si.TRADING_DAYS_YEAR:]) - 1.0
    ) == pytest.approx(want["vs_52w_high"], abs=1e-6)


# =========================================================================
# 4. Invariants the daily build asserts on
# =========================================================================
def _panel(n_rows: int = 300, n_tickers: int = 8) -> pd.DataFrame:
    """Deterministic panel: each column trends at its own rate, so the
    cross-sectional ordering is known and stable without any randomness."""
    idx = pd.bdate_range("2024-01-01", periods=n_rows)
    return pd.DataFrame(
        {
            f"T{j}": [100.0 * (1.0 + 0.0002 * j) ** i for i in range(n_rows)]
            for j in range(n_tickers)
        },
        index=idx,
    )


def test_composite_rank_is_a_permutation():
    result = si.composite_rank(_panel())
    ranks = result.ranks
    assert sorted(ranks.tolist()) == list(range(1, len(ranks) + 1))
    assert not result.unrankable


def test_composite_rank_orders_the_strongest_first():
    """T7 compounds fastest in the synthetic panel, so it must rank 1."""
    ranks = si.composite_rank(_panel()).ranks
    assert ranks.idxmin() == "T7"
    assert ranks.loc["T7"] == 1


def test_composite_rank_can_be_recomputed_as_of_an_earlier_bar():
    panel = _panel()
    early = si.composite_rank(panel, asof=-1 - si.SLOPE_LOOKBACK).ranks
    late = si.composite_rank(panel, asof=-1).ranks
    assert sorted(early.tolist()) == sorted(late.tolist())


def test_composite_rank_rejects_an_out_of_range_asof():
    with pytest.raises(IndexError):
        si.composite_rank(_panel(n_rows=50), asof=99)


def test_composite_rank_excludes_rows_without_the_short_horizons():
    """A brand-new listing cannot be ranked, and must be reported rather
    than silently given a middling rank."""
    panel = _panel()
    panel["NEW"] = np.nan
    panel.iloc[-3:, panel.columns.get_loc("NEW")] = [10.0, 11.0, 12.0]
    result = si.composite_rank(panel)
    assert "NEW" in result.unrankable
    assert "NEW" not in result.ranks.index
    assert sorted(result.ranks.tolist()) == list(range(1, len(result.ranks) + 1))


def test_rank_delta_is_empty_when_the_panel_is_too_short():
    """Spec §9.2: delta-R shows nothing for the first 20 sessions. It holds
    by construction here, not by a special case in the renderer."""
    assert si.rank_delta(_panel(n_rows=si.SLOPE_LOOKBACK)).empty


def test_rank_delta_sign_is_positive_when_a_rank_improves():
    """Rank 1 is strongest, so an improving ticker gives Rank(t-20) - Rank(t) > 0."""
    panel = _panel(n_rows=300, n_tickers=5)
    # Lift the weakest column sharply, but only INSIDE the 20-session
    # comparison window. A boost starting before t-20 would already be in
    # the earlier ranking too, and the delta would correctly read zero —
    # which is what a 30-bar ramp did on the first attempt.
    boost = si.SLOPE_LOOKBACK - 5
    panel.iloc[-boost:, panel.columns.get_loc("T0")] *= np.linspace(1.0, 3.0, boost)
    delta = si.rank_delta(panel)
    assert delta.loc["T0"] > 0


def test_percentile_is_withheld_rather_than_guessed_on_short_history():
    short = _series([float(i) for i in range(100)])
    result = si.percentile_of_latest(short)
    assert np.isnan(result.value)
    assert result.n_obs == 100
    assert result.truncated


def test_percentile_flags_a_truncated_but_usable_window():
    """Between min_obs and the full 504, the value publishes with a flag —
    this is what drives the "^" marker on the page."""
    n = 300
    result = si.percentile_of_latest(_series([float(i) for i in range(n)]))
    assert result.value == pytest.approx(100.0 * (n - 0.5) / n)
    assert result.n_obs == n
    assert result.truncated


def test_percentile_of_a_stale_current_bar_is_withheld():
    """A trailing NaN means the current reading is missing. Ranking the
    last VALID observation instead would publish a confident percentile
    for a stale bar — the carry-forward failure this repo has hit twice.
    """
    s = _series([float(i) for i in range(600)])
    s.iloc[-1] = np.nan
    assert np.isnan(si.percentile_of_latest(s).value)


def test_relative_strength_is_zero_against_itself():
    s = _series([100.0 + i for i in range(60)])
    assert si.relative_strength_1m(s, s) == pytest.approx(0.0)


def test_relative_strength_sign_follows_the_spread():
    strong = _series([100.0 * 1.01**i for i in range(60)])
    weak = _series([100.0 * 1.001**i for i in range(60)])
    assert si.relative_strength_1m(strong, weak) > 0
    assert si.relative_strength_1m(weak, strong) < 0
