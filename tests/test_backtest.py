"""Trade-mechanics sanity checks for backtest.py.

Tests the no-overlap guarantee, the time-stop, the trailing-stop trigger,
and the entry-at-next-trading-day-open rule on synthetic price series.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtest import (  # noqa: E402
    compute_atr_wilder,
    run_strategy,
)


def _make_breadth(idx: pd.DatetimeIndex, regime_safe: bool = True) -> pd.DataFrame:
    """A breadth panel that never triggers a regime exit (so we can isolate
    other exit conditions in tests)."""
    return pd.DataFrame({
        "composite_z": [1.0] * len(idx),       # well above p10
        "composite_p10": [-1.0] * len(idx),    # so composite_z > p10 always
        "ma_breadth": [0.70] * len(idx),       # above the 0.40 floor
        "signal_fires": [0] * len(idx),
    }, index=idx)


def _make_flat_soxx(n: int = 400, base: float = 100.0) -> pd.DataFrame:
    """Flat close, tight intraday range so ATR is positive but no real
    decline ever occurs — trailing stop must therefore never trigger.
    """
    idx = pd.date_range("2020-01-02", periods=n, freq="B")
    return pd.DataFrame({
        "Open":  [base] * n,
        "High":  [base + 0.50] * n,
        "Low":   [base - 0.50] * n,
        "Close": [base] * n,
    }, index=idx)


def test_entry_is_at_next_trading_day_open():
    """A signal-fire on date T should enter at the OPEN of T+1, with the
    entry_open field equal to soxx['Open'].iloc[T+1]."""
    soxx = _make_flat_soxx(n=100)
    breadth = _make_breadth(soxx.index)
    # Single signal on day 30
    signal_dates = [soxx.index[30].strftime("%Y-%m-%d")]
    trades = run_strategy(signal_dates, soxx, breadth)
    assert len(trades) == 1
    assert trades[0].entry_date == soxx.index[31].strftime("%Y-%m-%d")
    assert trades[0].entry_open == soxx["Open"].iloc[31]


def test_no_reentry_while_trade_open():
    """If the strategy fires three signals on days 30, 35, 50 but the first
    trade does not exit until day 280 (time stop), only one trade opens
    and the in-cluster signals on days 35 and 50 are skipped."""
    soxx = _make_flat_soxx(n=400)
    breadth = _make_breadth(soxx.index)
    signal_dates = [
        soxx.index[30].strftime("%Y-%m-%d"),
        soxx.index[35].strftime("%Y-%m-%d"),
        soxx.index[50].strftime("%Y-%m-%d"),
    ]
    trades = run_strategy(signal_dates, soxx, breadth)
    assert len(trades) == 1
    assert trades[0].exit_reason == "time_stop"
    assert trades[0].holding_days == 252


def test_second_signal_after_exit_opens_new_trade():
    """Signal at day 30, time-stop at day 282 (entry+252). A second signal
    at day 290 should open a NEW trade because it is strictly after the
    first trade's exit."""
    soxx = _make_flat_soxx(n=600)
    breadth = _make_breadth(soxx.index)
    signal_dates = [
        soxx.index[30].strftime("%Y-%m-%d"),
        soxx.index[290].strftime("%Y-%m-%d"),
    ]
    trades = run_strategy(signal_dates, soxx, breadth)
    assert len(trades) == 2
    assert trades[1].entry_date == soxx.index[291].strftime("%Y-%m-%d")


def test_trailing_stop_fires_on_drawdown():
    """Construct a price path that ramps up then drops sharply. The trailing
    stop at 2*ATR below the peak must fire when the close breaches it."""
    idx = pd.date_range("2020-01-02", periods=300, freq="B")
    # Ramp up steady (so ATR is small), then a sharp -15 per cent drop.
    closes = [100.0 + i * 0.1 for i in range(200)]  # 100 -> 120 over 200 days
    closes += [closes[-1] * 0.85] * 10              # -15 per cent shock
    closes += [closes[-1]] * (300 - len(closes))
    soxx = pd.DataFrame({
        "Open":  closes,
        "High":  [c * 1.001 for c in closes],
        "Low":   [c * 0.999 for c in closes],
        "Close": closes,
    }, index=idx)
    breadth = _make_breadth(soxx.index)
    signal_dates = [soxx.index[30].strftime("%Y-%m-%d")]
    trades = run_strategy(signal_dates, soxx, breadth)
    assert len(trades) == 1
    assert trades[0].exit_reason == "trailing_stop"
    # The shock starts at day 200, stop should fire within a handful of days
    exit_pos = soxx.index.get_loc(pd.Timestamp(trades[0].exit_date))
    assert 200 <= exit_pos <= 215


def test_regime_exit_on_ma_floor():
    """When ma_breadth drops below 0.40 mid-trade, regime exit must fire
    before the time stop."""
    soxx = _make_flat_soxx(n=400)
    # Build breadth where ma_breadth flips below 0.40 at day 100
    idx = soxx.index
    ma = [0.70] * 100 + [0.30] * (len(idx) - 100)
    breadth = pd.DataFrame({
        "composite_z": [1.0] * len(idx),
        "composite_p10": [-1.0] * len(idx),
        "ma_breadth": ma,
        "signal_fires": [0] * len(idx),
    }, index=idx)
    signal_dates = [soxx.index[30].strftime("%Y-%m-%d")]
    trades = run_strategy(signal_dates, soxx, breadth)
    assert len(trades) == 1
    assert trades[0].exit_reason == "regime_exit_ma_floor"
    assert trades[0].exit_date == soxx.index[100].strftime("%Y-%m-%d")


def test_trailing_stop_disabled_via_config():
    """Setting trailing_stop_k=None must disable the trailing stop. On a
    sharp -15 per cent shock that would normally fire it, the trade should
    instead run to time stop because regime exits also do not trigger in
    our test breadth panel."""
    idx = pd.date_range("2020-01-02", periods=300, freq="B")
    closes = [100.0 + i * 0.1 for i in range(200)]
    closes += [closes[-1] * 0.85] * 100  # -15 per cent shock, stays low
    soxx = pd.DataFrame({
        "Open":  closes,
        "High":  [c * 1.001 for c in closes],
        "Low":   [c * 0.999 for c in closes],
        "Close": closes,
    }, index=idx)
    breadth = _make_breadth(soxx.index)
    signal_dates = [soxx.index[30].strftime("%Y-%m-%d")]
    # Disable trailing stop
    from backtest import DEFAULT_CONFIG
    cfg = {**DEFAULT_CONFIG, "trailing_stop_k": None}
    trades = run_strategy(signal_dates, soxx, breadth, config=cfg)
    assert len(trades) == 1
    assert trades[0].exit_reason != "trailing_stop"


def test_profit_anchored_stop_does_not_fire_below_threshold():
    """With stop_active_after_profit_pct=0.10, the stop is inert until
    the trade reaches +10 per cent profit. A small -3 per cent dip
    before profit threshold should NOT trigger the stop."""
    idx = pd.date_range("2020-01-02", periods=80, freq="B")
    # Rise 5 per cent over 30 days, then drop 3 per cent, then recover.
    base = 100.0
    rise = [base + i * (0.05 * base / 30) for i in range(30)]
    dip = [rise[-1] * (1 - 0.03 * i / 10) for i in range(11)]
    recover = [dip[-1] + i * 0.5 for i in range(80 - len(rise) - len(dip))]
    closes = rise + dip + recover
    closes = closes[:80]
    soxx = pd.DataFrame({
        "Open":  closes,
        "High":  [c * 1.005 for c in closes],
        "Low":   [c * 0.995 for c in closes],
        "Close": closes,
    }, index=idx)
    breadth = _make_breadth(soxx.index)
    signal_dates = [soxx.index[3].strftime("%Y-%m-%d")]
    from backtest import DEFAULT_CONFIG
    cfg = {**DEFAULT_CONFIG, "stop_active_after_profit_pct": 0.10}
    trades = run_strategy(signal_dates, soxx, breadth, config=cfg)
    assert len(trades) == 1
    # Trade should still be running through the dip — exit only at
    # time-stop or end of data, not via trailing stop.
    assert trades[0].exit_reason != "trailing_stop"


def test_atr_wilder_matches_manual():
    """Sanity-check ATR computation against a manual EMA-of-TR on a tiny
    series."""
    idx = pd.date_range("2020-01-02", periods=30, freq="B")
    rng = np.random.default_rng(0)
    closes = pd.Series(100.0 + np.cumsum(rng.normal(0, 1, 30)), index=idx)
    highs = closes + rng.uniform(0.5, 1.5, 30)
    lows = closes - rng.uniform(0.5, 1.5, 30)
    atr = compute_atr_wilder(highs, lows, closes, period=14)
    # Manual computation
    prev = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev).abs(),
        (lows - prev).abs(),
    ], axis=1).max(axis=1)
    manual = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    pd.testing.assert_series_equal(atr, manual)
