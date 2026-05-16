"""Signal-logic sanity checks for compute_breadth.

Verifies the Zweig MA trigger and the helper expanding-z / expanding-percentile
functions on synthetic series with known properties.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute_breadth import (  # noqa: E402
    expanding_percentile,
    expanding_zscore,
    zweig_trigger,
)


# ---------------------------------------------------------------------------
# Zweig thrust trigger
# ---------------------------------------------------------------------------


def _ma_series_with_5080_gap(gap_days: int) -> pd.Series:
    """Build a deliberately step-shaped ma_breadth series so the 50-to-80
    transition is precisely `gap_days` trading days long.

    Layout:
      - 30 days at 0.30  (well below the Zweig low threshold)
      - `gap_days` days at 0.55  (above low, below high)
      - 10 days at 0.85 (above the Zweig high threshold)

    On the first day at 0.85, looking back `window` trading days, we will
    see all 0.55 readings only if gap_days >= window — that is the case
    where the trigger should NOT fire.
    """
    values = [0.30] * 30 + [0.55] * gap_days + [0.85] * 10
    idx = pd.date_range("2020-01-02", periods=len(values), freq="B")
    return pd.Series(values, index=idx, name="ma_breadth")


def test_zweig_fires_when_50_to_80_gap_is_inside_window():
    """50-to-80 gap of 15 days, Zweig window 20 — the prior 20 days at the
    moment of the >=80 cross still contain some 0.30 readings, so trigger
    must fire."""
    s = _ma_series_with_5080_gap(gap_days=15)
    trig = zweig_trigger(s, window=20, low=0.50, high=0.80)
    first_above_80_idx = (s >= 0.80).idxmax()
    assert bool(trig.loc[first_above_80_idx]), (
        "Trigger should fire when the 50-to-80 traversal is shorter than the "
        "Zweig window"
    )


def test_zweig_does_not_fire_when_50_to_80_gap_exceeds_window():
    """50-to-80 gap of 25 days, Zweig window 20 — the prior 20 days at the
    moment of the >=80 cross contain only 0.55 readings, so the trigger
    must NOT fire."""
    s = _ma_series_with_5080_gap(gap_days=25)
    trig = zweig_trigger(s, window=20, low=0.50, high=0.80)
    first_above_80_idx = (s >= 0.80).idxmax()
    assert not bool(trig.loc[first_above_80_idx]), (
        "Trigger should NOT fire when the 50-to-80 traversal is slower than "
        "the Zweig window"
    )


def test_zweig_does_not_fire_when_never_below_low():
    """If ma_breadth never dips below 50 per cent in the lookback, the Zweig
    trigger must not fire even when above 80 per cent."""
    idx = pd.date_range("2020-01-02", periods=100, freq="B")
    s = pd.Series([0.65] * 50 + [0.85] * 50, index=idx)
    trig = zweig_trigger(s, window=20, low=0.50, high=0.80)
    assert not trig.any()


# ---------------------------------------------------------------------------
# Expanding z-score and percentile (no-look-ahead helpers)
# ---------------------------------------------------------------------------


def test_expanding_zscore_uses_only_prior_data():
    """A spike on the LAST day of a flat series must not change the z-score
    on EARLIER days. This is the core no-look-ahead invariant."""
    idx = pd.date_range("2020-01-02", periods=100, freq="B")
    base = pd.Series([0.50] * 100, index=idx)
    z_base = expanding_zscore(base, min_periods=20)

    spiked = base.copy()
    spiked.iloc[-1] = 5.0  # huge final-day spike
    z_spiked = expanding_zscore(spiked, min_periods=20)

    # Every value EXCEPT the last day must be identical (NaN-equal).
    pd.testing.assert_series_equal(z_base.iloc[:-1], z_spiked.iloc[:-1])


def test_expanding_percentile_uses_only_prior_data():
    """The expanding 90th percentile at T must be derived from data strictly
    before T."""
    idx = pd.date_range("2020-01-02", periods=100, freq="B")
    s = pd.Series([0.5] * 99 + [10.0], index=idx)  # spike on last day only
    p90 = expanding_percentile(s, q=0.90, min_periods=20)
    # The spike on day 99 should NOT influence p90 at day 99 (it uses days 0..98).
    assert p90.iloc[-1] == 0.5
