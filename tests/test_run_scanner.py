"""Offline tests for the scanner's daily build.

Everything here runs without network. The fetch itself is exercised by
running the script; what is tested is the logic that can be silently
wrong — FX direction, alert rules, the ETF layer, the snapshot append,
and the three guards that abort the build.

The snapshot tests matter most in the near term. That CSV is the only
part of the scanner that cannot be rebuilt: yfinance exposes no history
for navPrice or sharesOutstanding, so a row lost or duplicated today is
lost or duplicated permanently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_scanner as rs  # noqa: E402
import scanner_indicators as si  # noqa: E402
from scanner_universe import FX_DIVIDE, FX_MULTIPLY, Origin, ScannerRow  # noqa: E402


def _frame(n: int = 400, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    """Deterministic OHLCV on a business-day index."""
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series([start + step * i for i in range(n)], index=idx)
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": pd.Series([1_000_000.0] * n, index=idx),
    })


def _oscillating(n: int = 400) -> pd.DataFrame:
    """Rising, but with a deterministic sawtooth so daily returns vary.

    The plain ``_frame`` ramp is too clean to test anything that compares a
    move against its own sigma: its returns are near-deterministic, so a
    typical bar still scores several sigma and every squeeze reads as a
    squeeze RELEASE. This adds a period-5 wobble, which gives sigma a real
    magnitude and leaves the final bar unremarkable.
    """
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(
        [100.0 + 0.5 * i + 2.0 * ((i % 5) - 2) for i in range(n)], index=idx
    )
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": pd.Series([1_000_000.0] * n, index=idx),
    })


def _row(ticker: str, currency: str = "USD", fx=None, direction=None) -> ScannerRow:
    return ScannerRow(
        scan_ticker=ticker,
        origins=(Origin("C", ticker, None),),
        name=ticker,
        currency=currency,
        fx_ticker=fx,
        fx_direction=direction,
    )


# =========================================================================
# FX conversion — a silent scale error, not a crash, if inverted
# =========================================================================
def test_usd_rows_pass_through_untouched():
    frame = _frame()
    out, note = rs.apply_fx(_row("SPY"), frame, {})
    assert out is frame and note is None


def test_eur_rows_are_multiplied_by_the_rate():
    """EURUSD=X quotes USD per 1 EUR, so a EUR price is multiplied."""
    frame = _frame(n=30)
    fx = pd.Series(2.0, index=frame.index)
    out, note = rs.apply_fx(
        _row("EXV1.DE", "EUR", "EURUSD=X", FX_MULTIPLY), frame, {"EURUSD=X": fx}
    )
    assert out["close"].iloc[-1] == pytest.approx(frame["close"].iloc[-1] * 2.0)
    assert "EUR->USD" in note


def test_cny_rows_are_divided_by_the_rate():
    """USDCNY=X quotes CNY per 1 USD, so a CNY price is divided."""
    frame = _frame(n=30)
    fx = pd.Series(7.0, index=frame.index)
    out, _ = rs.apply_fx(
        _row("159801.SZ", "CNY", "USDCNY=X", FX_DIVIDE), frame, {"USDCNY=X": fx}
    )
    assert out["close"].iloc[-1] == pytest.approx(frame["close"].iloc[-1] / 7.0)


def test_fx_does_not_scale_volume():
    """Volume is a share count. Converting it would be meaningless and would
    corrupt the volume-ratio column and its alert."""
    frame = _frame(n=30)
    fx = pd.Series(2.0, index=frame.index)
    out, _ = rs.apply_fx(
        _row("EXV1.DE", "EUR", "EURUSD=X", FX_MULTIPLY), frame, {"EURUSD=X": fx}
    )
    assert out["volume"].equals(frame["volume"])


def test_missing_fx_series_aborts_rather_than_publishing_local_currency():
    with pytest.raises(rs.ScannerBuildError, match="unavailable"):
        rs.apply_fx(_row("EXV1.DE", "EUR", "EURUSD=X", FX_MULTIPLY), _frame(30), {})


def test_market_classification():
    assert rs._market_of("SPY") == "US"
    assert rs._market_of("EXH4.DE") == "DE"
    assert rs._market_of("159801.SZ") == "CN"


# =========================================================================
# Snapshot append — the one artefact that cannot be rebuilt
# =========================================================================
@pytest.fixture
def snapshot_path(tmp_path, monkeypatch):
    path = tmp_path / "scanner_snapshots.csv"
    monkeypatch.setattr(rs, "SNAPSHOT_PATH", path)
    return path


def _snap(date: str, ticker: str, nav=None, so=None, close=None) -> pd.DataFrame:
    return pd.DataFrame([{
        "date": date, "ticker": ticker, "nav": nav, "so": so, "close": close
    }])


def test_snapshots_accrue_across_days(snapshot_path):
    rs.append_snapshots(_snap("2026-08-03", "SPY", 100.0, 1e9, 100.1))
    out = rs.append_snapshots(_snap("2026-08-04", "SPY", 101.0, 1e9, 101.2))
    assert len(out) == 2
    assert sorted(out["date"]) == ["2026-08-03", "2026-08-04"]


def test_rerunning_the_same_day_is_idempotent(snapshot_path):
    """Spec §9.5. An operator must be able to run the build twice without
    corrupting a history that cannot be refetched."""
    first = _snap("2026-08-03", "SPY", 100.0, 1e9, 100.1)
    rs.append_snapshots(first)
    out = rs.append_snapshots(first)
    assert len(out) == 1
    assert out.iloc[0]["nav"] == 100.0


def test_a_rerun_replaces_rather_than_duplicates(snapshot_path):
    """Second run of a day wins — a later observation is the better one."""
    rs.append_snapshots(_snap("2026-08-03", "SPY", 100.0, 1e9, 100.1))
    out = rs.append_snapshots(_snap("2026-08-03", "SPY", 100.5, 1e9, 100.6))
    assert len(out) == 1
    assert out.iloc[0]["nav"] == 100.5


def test_snapshots_keep_tickers_separate(snapshot_path):
    rs.append_snapshots(_snap("2026-08-03", "SPY", 100.0))
    out = rs.append_snapshots(_snap("2026-08-03", "QQQ", 200.0))
    assert len(out) == 2
    assert set(out["ticker"]) == {"SPY", "QQQ"}


# =========================================================================
# ETF layer
# =========================================================================
def test_premium_discount_is_close_over_nav():
    snaps = _snap("2026-08-03", "SPY", nav=100.0, close=100.3)
    assert rs.etf_layer("SPY", snaps)["pd_pct"] == pytest.approx(0.003)


def test_missing_nav_yields_no_premium_rather_than_a_proxy():
    """Spec §6: never fill an unavailable NAV. 159801.SZ is the live case."""
    out = rs.etf_layer("159801.SZ", _snap("2026-08-03", "159801.SZ", nav=None, close=1.1))
    assert out["pd_pct"] is None and out["pd_alert"] is None


def test_no_premium_alert_before_the_sigma_baseline_exists():
    """The value publishes; the alert does not, until there is history.

    Spec §6 proposed an interim absolute threshold. The first live run
    disproved it: six of twelve chips were P/D alerts on values from -0.4%
    to -1.2% for BOTZ, GLD, ICLN, LIT, TIP and URA, which are not real
    premiums for those funds. yfinance publishes navPrice with no as-of
    date, and the readings track fund SIZE rather than stress — SPY +0.057%
    and QQQ +0.023% are plausible while the smaller funds show large
    spurious discounts from a stale NAV against a current close. Staleness
    cannot be detected here, only absorbed, which is what the sigma test
    does once a baseline exists.
    """
    big = rs.etf_layer("SPY", _snap("2026-08-03", "SPY", nav=100.0, close=101.2))
    assert big["pd_pct"] == pytest.approx(0.012), "the value is still published"
    assert big["pd_alert"] is None, "one observation cannot support an assertion"


def test_the_premium_alert_arms_once_history_reaches_the_baseline():
    """With a baseline, a persistent bias sits in the mean and only a real
    deviation fires — so a stale-NAV offset does not generate alerts."""
    rows = [
        {"date": f"2026-{2 + d // 28:02d}-{1 + d % 28:02d}", "ticker": "SPY",
         "nav": 100.0, "so": 1e9, "close": 100.5}
        for d in range(rs.PD_SIGMA_MIN_OBS + 5)
    ]
    steady = rs.etf_layer("SPY", pd.DataFrame(rows))
    assert steady["pd_alert"] is None, "a constant 0.5% bias is this fund's normal"

    rows[-1]["close"] = 108.0
    shocked = rs.etf_layer("SPY", pd.DataFrame(rows))
    assert shocked["pd_alert"] is not None
    assert "sigma" in shocked["pd_alert"]


def test_five_day_flow_needs_six_observations():
    rows = [
        {"date": f"2026-08-{d:02d}", "ticker": "SPY", "nav": 100.0,
         "so": 1_000_000.0 * (1.01 if d == 10 else 1.0), "close": 100.0}
        for d in range(5, 11)
    ]
    out = rs.etf_layer("SPY", pd.DataFrame(rows))
    assert out["flow_5d"] == pytest.approx(0.01)

    short = rs.etf_layer("SPY", pd.DataFrame(rows[:4]))
    assert short["flow_5d"] is None


def test_a_one_day_share_count_jump_alerts():
    rows = pd.DataFrame([
        {"date": "2026-08-03", "ticker": "SPY", "nav": 100.0, "so": 1e9, "close": 100.0},
        {"date": "2026-08-04", "ticker": "SPY", "nav": 100.0, "so": 1.02e9, "close": 100.0},
    ])
    assert "Shares outstanding" in rs.etf_layer("SPY", rows)["flow_alert"]


def test_etf_layer_is_blank_without_snapshots():
    out = rs.etf_layer("SPY", pd.DataFrame())
    assert out == {"pd_pct": None, "flow_5d": None,
                   "pd_alert": None, "flow_alert": None}


# =========================================================================
# Alerts
# =========================================================================
def _data(frame: pd.DataFrame, ticker: str = "TEST") -> rs.TickerData:
    return rs.TickerData(ticker, frame)


def _cols(frame: pd.DataFrame) -> dict:
    return rs.build_columns(_data(frame), pd.Series(dtype=float))


def test_a_fresh_high_alerts():
    frame = _frame()
    kinds = [(a.kind, a.label) for a in rs.build_alerts(_data(frame), _cols(frame))]
    assert ("52w", "52-week high") in kinds


def test_a_fresh_low_alerts():
    frame = _frame(start=300.0, step=-0.5)
    kinds = [(a.kind, a.label) for a in rs.build_alerts(_data(frame), _cols(frame))]
    assert ("52w", "52-week low") in kinds


def test_crossing_above_ma200_alerts():
    """Long decline, then a jump that clears the 200-day average today."""
    frame = _frame(start=300.0, step=-0.5)
    frame.iloc[-1, frame.columns.get_loc("close")] = 400.0
    ma200 = si.sma(frame["close"], si.MA_LONG)
    assert frame["close"].iloc[-2] < ma200.iloc[-2] < frame["close"].iloc[-1], (
        "construction drifted — yesterday must be below and today above"
    )
    labels = [a.label for a in rs.build_alerts(_data(frame), _cols(frame))]
    assert "Crossed above MA200" in labels


def test_a_large_daily_move_alerts_with_its_sigma():
    frame = _frame()
    frame.iloc[-1, frame.columns.get_loc("close")] *= 1.25
    alerts = [a for a in rs.build_alerts(_data(frame), _cols(frame))
              if a.kind == "sigma_move"]
    assert alerts and "sigma move" in alerts[0].label


def test_a_volume_spike_alerts():
    frame = _frame()
    frame.iloc[-1, frame.columns.get_loc("volume")] *= 5
    labels = [a.label for a in rs.build_alerts(_data(frame), _cols(frame))]
    assert any("Volume" in x for x in labels)


def test_squeeze_requires_both_measures_low():
    """The conjunction is the rule (spec §4.5). One low reading is not a
    squeeze, so a fabricated single-low state must not fire."""
    frame = _oscillating()
    cols = _cols(frame)
    cols["rv_pctl"] = 5.0          # low
    cols["bbw_pctl"] = 50.0        # not low
    assert not [a for a in rs.build_alerts(_data(frame), cols) if "squeeze" in a.kind]

    cols["bbw_pctl"] = 2.0
    fired = [a for a in rs.build_alerts(_data(frame), cols) if a.kind == "squeeze"]
    assert fired, "both low must fire a squeeze"
    assert "RV p5 / BBW p2" in fired[0].value


def test_a_squeeze_that_breaks_reports_release_not_squeeze():
    frame = _oscillating()
    frame.iloc[-1, frame.columns.get_loc("close")] *= 1.25
    cols = _cols(frame)
    cols["rv_pctl"], cols["bbw_pctl"] = 5.0, 2.0
    kinds = {a.kind for a in rs.build_alerts(_data(frame), cols)}
    assert "squeeze_release" in kinds and "squeeze" not in kinds


def test_rsi_extremes_alert_in_both_directions():
    up = _frame()
    assert any(a.kind == "rsi" and "overbought" in a.label
               for a in rs.build_alerts(_data(up), _cols(up)))
    down = _frame(start=300.0, step=-0.5)
    assert any(a.kind == "rsi" and "oversold" in a.label
               for a in rs.build_alerts(_data(down), _cols(down)))


# --- rank crossings ------------------------------------------------------
# The panel is five clean ramps, so the ranking is the ordering of their
# slopes and nothing else. A crossing is then produced by moving ONE bar:
# multiply the weakest ramp's last close, and all four horizon returns lift
# together, which is exactly the "strengthened today, not before" case the
# rule is meant to catch.
def _ranked_panel(steps: dict[str, float]) -> dict[str, rs.TickerData]:
    return {t: _data(_frame(step=s), t) for t, s in steps.items()}


def _rank_of(panel: dict[str, rs.TickerData]) -> "pd.Series":
    returns = pd.DataFrame({t: rs.horizon_returns(d) for t, d in panel.items()}).T
    return rs.si.rank_from_horizon_returns(returns)


STEPS = {"A": 0.5, "B": 0.4, "C": 0.3, "D": 0.2, "E": 0.1}


def test_a_row_that_climbs_into_the_cut_today_fires_a_crossing():
    panel = _ranked_panel(STEPS)
    panel["E"].frame.iloc[-1, panel["E"].frame.columns.get_loc("close")] *= 3.0

    alerts = rs.rank_crossings(panel, _rank_of(panel), cut=2, confirm=2)
    by_ticker = {a.ticker: a.label for a in alerts}
    assert by_ticker.get("E") == "Entered the top 2"
    # B held rank 2 for the confirmation window and was displaced today. The
    # exit is a real crossing and is reported as one — the fixed cut means a
    # row entering pushes a row out, which the guide states.
    assert by_ticker.get("B") == "Left the top 2"
    assert set(by_ticker) == {"E", "B"}, "no other row changed side"


def test_no_crossing_fires_when_nothing_changed_side():
    panel = _ranked_panel(STEPS)
    assert rs.rank_crossings(panel, _rank_of(panel), cut=2, confirm=2) == []


def test_the_confirmation_window_suppresses_a_row_that_was_recently_inside():
    """A row oscillating either side of the cut must not fire every day."""
    panel = _ranked_panel(STEPS)
    close = panel["E"].frame.columns.get_loc("close")
    panel["E"].frame.iloc[-1, close] *= 3.0
    panel["E"].frame.iloc[-2, close] *= 3.0   # it was already inside yesterday

    fired = {a.ticker for a in rs.rank_crossings(panel, _rank_of(panel),
                                                 cut=2, confirm=2)}
    assert "E" not in fired


def test_stale_rows_cannot_manufacture_a_crossing():
    panel = _ranked_panel(STEPS)
    panel["E"].frame.iloc[-1, panel["E"].frame.columns.get_loc("close")] *= 3.0

    fired = {a.ticker for a in rs.rank_crossings(panel, _rank_of(panel),
                                                 exclude={"E"}, cut=2, confirm=2)}
    assert "E" not in fired


def test_crossings_outrank_statistical_noise_but_not_the_state_changes():
    order = rs.ALERT_PRIORITY
    assert order["ma200_cross"] < order["rank_cross"] < order["sigma_move"]


def test_alerts_are_priority_ordered_and_truncated_with_a_count():
    alerts = (
        [rs.Alert(f"T{i}", "rsi", "RSI") for i in range(20)]
        + [rs.Alert("X", "etf_layer", "P/D wide")]
    )
    shown, truncated = rs.rank_alerts(alerts)
    assert len(shown) == rs.MAX_CHIPS
    assert shown[0].kind == "etf_layer", "ETF-layer chips outrank RSI noise"
    assert truncated == 21 - rs.MAX_CHIPS


# =========================================================================
# Guards
# =========================================================================
def _ok_rows(n: int = 3) -> list[dict]:
    return [
        {"ticker": f"T{i}", "rank": i + 1, "rv_pctl": 50.0, "bbw_pctl": 50.0,
         "n_bars": 3000}
        for i in range(n)
    ]


def test_invariants_pass_on_a_clean_cross_section():
    rs.assert_invariants(_ok_rows(), expected=3)


def test_duplicate_ranks_abort_the_build():
    rows = _ok_rows()
    rows[1]["rank"] = 1
    with pytest.raises(rs.ScannerBuildError, match="permutation"):
        rs.assert_invariants(rows, expected=3)


def test_a_row_count_mismatch_aborts_the_build():
    with pytest.raises(rs.ScannerBuildError, match="resolver expected"):
        rs.assert_invariants(_ok_rows(3), expected=54)


def test_an_out_of_range_percentile_aborts_the_build():
    rows = _ok_rows()
    rows[0]["rv_pctl"] = 140.0
    rows[0]["n_bars"] = 3000
    with pytest.raises(rs.ScannerBuildError, match=r"outside \[0,100\]"):
        rs.assert_invariants(rows, expected=3)


def test_a_withheld_percentile_is_allowed_on_short_history():
    """percentile_of_latest returns NaN by contract when there is too little
    history to rank against. Failing on that set two of our own guards
    against each other, which is how the first CI run aborted."""
    rows = _ok_rows(1)
    rows[0].update({"rv_pctl": None, "bbw_pctl": float("nan"), "n_bars": 60})
    rs.assert_invariants(rows, expected=1)


def test_a_missing_percentile_on_long_history_still_aborts():
    """The strictness that matters is kept: with the observations present, a
    missing percentile is a bug, not a withholding."""
    rows = _ok_rows(1)
    rows[0].update({"rv_pctl": float("nan"), "n_bars": 3000})
    with pytest.raises(rs.ScannerBuildError, match="missing despite 3000 bars"):
        rs.assert_invariants(rows, expected=1)


def test_unranked_rows_do_not_break_the_permutation_check():
    """A row too new to rank carries rank None and is excluded, not zero."""
    rows = _ok_rows(3) + [
        {"ticker": "NEW", "rank": None, "rv_pctl": None, "bbw_pctl": None}
    ]
    rs.assert_invariants(rows, expected=4)


def test_the_naive_divergence_sample_is_deterministic_for_a_date():
    panel = {t: _data(_frame(n=300), t) for t in ["AAA", "BBB", "CCC", "DDD", "EEE"]}
    first = rs.assert_no_naive_divergence(panel, "2026-08-03")
    second = rs.assert_no_naive_divergence(panel, "2026-08-03")
    assert first == second, "same date must check the same tickers"
    assert len(first) == 3


def test_the_naive_divergence_sample_rotates_across_dates():
    panel = {t: _data(_frame(n=300), t) for t in
             ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]}
    picks = {
        tuple(rs.assert_no_naive_divergence(panel, d))
        for d in ["2026-08-03", "2026-08-04", "2026-08-05"]
    }
    assert len(picks) > 1, "the sample must not check the same tickers forever"


def test_a_planted_divergence_aborts_the_build(monkeypatch):
    """The guard is worthless unless it actually fires — so break the
    vectorised path and confirm the naive one catches it."""
    panel = {"AAA": _data(_frame(n=300), "AAA")}
    monkeypatch.setattr(rs.si, "naive_sma_latest", lambda values, window: 1.0)
    with pytest.raises(rs.ScannerBuildError, match="naive-recompute divergence"):
        rs.assert_no_naive_divergence(panel, "2026-08-03")


# =========================================================================
# Rank plumbing
# =========================================================================
def test_horizon_returns_offset_steps_back_the_tickers_own_sessions():
    data = _data(_frame(n=400))
    now = rs.horizon_returns(data)
    prior = rs.horizon_returns(data, offset=si.SLOPE_LOOKBACK)
    assert now.keys() == prior.keys() == set(si.RANK_HORIZONS)
    # A steadily rising series compounds, so the same lookback measured 20
    # sessions earlier spans lower prices and gives a larger percentage gain.
    assert prior[21] > now[21]


def test_build_columns_withholds_rs_for_the_benchmark_row():
    frame = _frame()
    cols = rs.build_columns(_data(frame, rs.BENCHMARK), frame["close"])
    assert cols["rs_1m"] is None
    assert cols["ret_1m"] is not None, "the raw 1M return is still published"


# =========================================================================
# Chart history for the expandable row charts
#
# Two size decisions are load-bearing and worth pinning: dates are published
# once per market as a shared calendar rather than repeated per ticker, and
# only closes are published because the browser computes the moving averages.
# Together those took the payload from over a megabyte to ~200 KB. A future
# edit that reintroduces per-ticker dates or server-side MAs would quietly
# quadruple what every chart-opening reader downloads.
# =========================================================================
def test_history_publishes_one_calendar_per_market():
    panel = {
        "SPY": _data(_frame(n=600), "SPY"),
        "QQQ": _data(_frame(n=600), "QQQ"),
        "EXH4.DE": _data(_frame(n=600), "EXH4.DE"),
    }
    h = rs.build_history(panel)
    assert set(h["calendars"]) == {"US", "DE"}, "one calendar per market, not per ticker"
    assert h["series"]["SPY"]["calendar"] == "US"
    assert h["series"]["EXH4.DE"]["calendar"] == "DE"


def test_history_series_align_to_their_calendar():
    panel = {"SPY": _data(_frame(n=600), "SPY")}
    h = rs.build_history(panel)
    axis = h["calendars"]["US"]
    assert len(axis) == rs.HISTORY_SESSIONS
    assert len(h["series"]["SPY"]["close"]) == len(axis)


def test_history_publishes_closes_only():
    """MAs are the browser's job — publishing them would triple the payload."""
    h = rs.build_history({"SPY": _data(_frame(n=600), "SPY")})
    assert set(h["series"]["SPY"]) == {"calendar", "close"}


def test_history_window_matches_the_percentile_window():
    """The chart must show the history the RV and BBW columns rank within."""
    assert rs.HISTORY_SESSIONS == si.PCTL_WINDOW


def test_history_pads_a_short_series_with_nulls_not_zeros():
    """A young listing shares the market calendar but has no early bars.

    IBIT is the live case: it lists in 2024 while its neighbours run back a
    decade. Its recent bars must sit at the RIGHT of the shared calendar with
    nulls before them — zeros would draw a price collapse, and nulls draw
    nothing. The young frame is sliced off the end of the long one so its
    dates are recent, which is what makes this the real case rather than two
    disjoint calendars.
    """
    long_frame = _frame(n=600)
    panel = {
        "SPY": _data(long_frame, "SPY"),
        "NEW": _data(long_frame.iloc[-30:], "NEW"),
    }
    h = rs.build_history(panel)
    closes = h["series"]["NEW"]["close"]
    assert len(closes) == len(h["calendars"]["US"])
    assert closes[0] is None, "no bars at the start of the shared window"
    assert closes[-1] is not None, "its recent bars are at the right"
    assert 0 not in [c for c in closes if c is not None]
    assert sum(1 for c in closes if c is not None) == 30


def test_round_sig_serves_both_price_scales():
    """One rule has to cover SOXX near 500 and 159801.SZ near 0.15."""
    assert rs._round_sig(504.8900146484375) == pytest.approx(504.89)
    assert rs._round_sig(0.16069123) == pytest.approx(0.16069)
    assert rs._round_sig(None) is None
    assert rs._round_sig(float("nan")) is None
    assert rs._round_sig(0.0) == 0.0
