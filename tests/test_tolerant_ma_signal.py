"""The trend signal must survive an isolated missing bar.

THE DEFECT (found 2026-08-22). Sleeves B and C computed

    ma = closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()

and pandas counts non-NaN observations against ``min_periods``. With the two
equal, ONE absent close makes every window containing it short by one, so a
single missing bar blanked the average for the next 200 sessions -- and both
engines "drop NaN signal (insufficient history)", silently removing the
ticker from candidacy for about ten months.

It was not hypothetical. The vendor served no BTC-USD bar for Fri 2026-08-21
(the 17th to 20th and the 22nd are all present), and BTC-USD is held in 95 of
212 sleeve-C rebalances at a 20% within-sleeve weight -- 2% of NAV.

Sleeves A and D never had the defect: run_ma200_sweep.compute_ma200_breadth
has always used ``int(period * 0.9)``. The fix is that same convention,
shared rather than copied a third time.

These tests pin the three things that make the fix safe rather than merely
convenient: it changes no existing value, it does not start any series
earlier, and it still refuses to rank on a close that does not exist.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.price_panel_guard import (
    MA_WINDOW_TOLERANCE,
    ma_distance_signal,
    tolerant_moving_average,
)

PERIOD = 200


def _series(n=600, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=n)
    # A gently trending series; the exact path does not matter, only the holes.
    return pd.Series(np.linspace(100.0, 200.0, n), index=idx)


def _strict(closes, period=PERIOD):
    """The definition this replaced, kept here as the comparison basis."""
    ma = closes.rolling(period, min_periods=period).mean()
    return (closes - ma) / ma


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------
def test_one_hole_no_longer_blanks_two_hundred_sessions():
    s = _series()
    victim = s.index[300]
    hurt = s.copy()
    hurt.loc[victim] = np.nan

    strict_dmg = _strict(hurt).isna() & ~_strict(s).isna()
    tolerant_dmg = ma_distance_signal(hurt, PERIOD).isna() & \
        ~ma_distance_signal(s, PERIOD).isna()

    assert strict_dmg.sum() == PERIOD, "the old behaviour should blank a full window"
    assert tolerant_dmg.sum() == 1, "a hole must cost its own session and no more"
    assert tolerant_dmg.loc[victim]


def test_the_missing_session_is_the_one_that_is_lost():
    s = _series()
    victim = s.index[400]
    hurt = s.copy()
    hurt.loc[victim] = np.nan
    sig = ma_distance_signal(hurt, PERIOD)
    assert pd.isna(sig.loc[victim])
    # The very next session is usable again.
    assert not pd.isna(sig.loc[s.index[401]])


# ---------------------------------------------------------------------------
# Value preservation — the reason this could be adopted without restating
# ---------------------------------------------------------------------------
def test_identical_to_the_strict_definition_on_a_complete_series():
    s = _series()
    a, b = _strict(s), ma_distance_signal(s, PERIOD)
    both = a.notna() & b.notna()
    assert both.sum() > 0
    # Exact equality, not a tolerance: on a hole-free series the two
    # definitions are the same arithmetic.
    assert (a[both] - b[both]).abs().max() == 0.0
    assert (a.isna() == b.isna()).all()


def test_the_series_does_not_start_earlier():
    """The warm-up gate is why this is not a restatement. Loosening
    min_periods alone would define the signal ~20 sessions sooner and move
    every early rebalance."""
    s = _series()
    assert _strict(s).first_valid_index() == \
        ma_distance_signal(s, PERIOD).first_valid_index()


def test_warm_up_counts_real_observations_not_rows():
    """A series whose first rows are NaN must still wait for PERIOD actual
    observations, or a sparse opening would let the signal start early."""
    s = _series()
    s.iloc[:50] = np.nan
    sig = ma_distance_signal(s, PERIOD)
    first = sig.first_valid_index()
    assert s.loc[:first].notna().sum() >= PERIOD


# ---------------------------------------------------------------------------
# What it must still refuse
# ---------------------------------------------------------------------------
def test_a_missing_current_bar_is_still_nan():
    """THE LINE THIS MUST NOT CROSS. The tolerance is for holes in the
    window's history. Ranking a position on a close that does not exist is
    the partial-bar defect wearing a different hat."""
    s = _series()
    s.iloc[-1] = np.nan
    assert pd.isna(ma_distance_signal(s, PERIOD).iloc[-1])


def test_it_is_a_tolerance_not_a_blanket():
    """Enough holes and the window genuinely cannot support an average."""
    s = _series()
    hurt = s.copy()
    # Blow a hole larger than the tolerance allows, inside one window.
    n_holes = int(PERIOD * (1 - MA_WINDOW_TOLERANCE)) + 5
    hurt.iloc[300:300 + n_holes] = np.nan
    sig = ma_distance_signal(hurt, PERIOD)
    assert pd.isna(sig.iloc[300 + n_holes + 1]), \
        "a window past the tolerance must go NaN, not average a fragment"


def test_tolerance_matches_the_convention_sleeves_a_and_d_already_use():
    """One number, one place. If run_ma200_sweep's floor ever moves, this
    test is what says the two halves of the book have diverged."""
    from scripts.run_ma200_sweep import MA_PERIOD as sweep_period
    expected = max(1, int(sweep_period * 0.9))
    assert max(1, int(sweep_period * MA_WINDOW_TOLERANCE)) == expected


# ---------------------------------------------------------------------------
# Both engines use the shared definition — no third copy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", ["run_thematic_rotation",
                                    "run_asset_class_rotation"])
def test_engine_delegates_rather_than_reimplementing(module):
    import ast
    import importlib
    import inspect
    import textwrap
    m = importlib.import_module(f"scripts.{module}")
    # Parse the function and read only its STATEMENTS. The docstring
    # deliberately quotes the old expression to explain the defect, so a
    # plain string search would fail on the explanation, not the code.
    tree = ast.parse(textwrap.dedent(inspect.getsource(m.compute_signal)))
    fn = tree.body[0]
    stmts = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                            and isinstance(fn.body[0].value, ast.Constant)
                            and isinstance(fn.body[0].value.value, str)) \
        else fn.body
    body = "\n".join(ast.unparse(n) for n in stmts)
    assert "ma_distance_signal" in body, f"{module} should delegate: {body}"
    assert "min_periods" not in body, \
        f"{module} still computes its own window: {body}"


@pytest.mark.parametrize("module", ["run_thematic_rotation",
                                    "run_asset_class_rotation"])
def test_engine_signal_survives_a_hole_end_to_end(module):
    """Through the engine's own compute_signal, not just the helper."""
    import importlib
    m = importlib.import_module(f"scripts.{module}")
    idx = pd.bdate_range("2020-01-01", periods=600)
    frame = pd.DataFrame({"X": np.linspace(100.0, 200.0, 600),
                          "Y": np.linspace(50.0, 150.0, 600)}, index=idx)
    hurt = frame.copy()
    hurt.loc[idx[300], "X"] = np.nan
    sig = m.compute_signal(hurt)
    lost = sig["X"].isna() & ~m.compute_signal(frame)["X"].isna()
    assert lost.sum() == 1, f"{module}: one hole cost {int(lost.sum())} sessions"
    # The untouched column is unaffected either way.
    assert (m.compute_signal(frame)["Y"].isna() == sig["Y"].isna()).all()


def test_tolerant_moving_average_handles_a_dataframe():
    idx = pd.bdate_range("2020-01-01", periods=400)
    df = pd.DataFrame({"A": np.arange(400.0) + 1, "B": np.arange(400.0) + 100},
                      index=idx)
    ma = tolerant_moving_average(df, PERIOD)
    assert list(ma.columns) == ["A", "B"]
    assert ma["A"].first_valid_index() == idx[PERIOD - 1]
