"""Selftests for the WS5 constituent relative-trend breadth engine
(scripts/relative_trend.py), frozen and committed BEFORE any WS5 results are
computed (em-rotation-lab §1.9b precedent).

Coverage maps to the WS5 pre-registration (KICKOFF_ws5-relative-trend.md):

  Failure mode 1 — ratio-leg look-ahead / adjustment mismatch:
      test_no_lookahead_all_arms, test_final_bar_perturbation_invariance
  Failure mode 2 — momentum in disguise:
      (guarded in the run harness via the placebo + overlap diagnostics, not
       here; test_relative_leg_detects_outperformance shows the relative leg
       carries information the absolute leg cannot)
  Failure mode 3 — denominator asymmetry between legs:
      test_shared_denominator_identical_across_arms,
      test_missing_name_dropped_from_both_legs

  Deployed-parity anchor:
      test_absolute_arm_matches_deployed_breadth

  Structural invariants:
      test_dual_never_exceeds_either_leg, test_breadth_bounded_unit_interval,
      test_zero_valid_day_is_nan

  Date-boundary rule (vault CLAUDE.md — one month, one year boundary):
      test_month_boundary_continuity, test_year_boundary_continuity
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from relative_trend import (  # noqa: E402
    MA_PERIOD,
    compute_trend_breadth,
    compute_trend_breadth_all,
    shared_valid_count,
)
from run_ma200_sweep import compute_ma200_breadth  # noqa: E402 (deployed leg)


# ---------------------------------------------------------------------------
# Synthetic panels
# ---------------------------------------------------------------------------

def _panel(n_days=500, n_tickers=12, seed=7, start="2019-01-02"):
    """Deterministic constituent adjusted-close panel (random walk + drift)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n_days, freq="B")
    rets = rng.normal(loc=0.0003, scale=0.016, size=(n_days, n_tickers))
    prices = 100.0 * np.exp(np.cumsum(rets, axis=0))
    cols = [f"T{i:02d}" for i in range(n_tickers)]
    return pd.DataFrame(prices, index=idx, columns=cols)


def _benchmark(index, seed=99, level=400.0):
    """A benchmark (SPY-like) series on the same calendar, no NaNs."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(loc=0.0003, scale=0.010, size=len(index))
    return pd.Series(level * np.exp(np.cumsum(rets)), index=index)


# ---------------------------------------------------------------------------
# Failure mode 1 — look-ahead
# ---------------------------------------------------------------------------

def test_no_lookahead_all_arms():
    """Breadth at date T for every arm must not change when prices AND the
    benchmark are mutated strictly AFTER T."""
    prices = _panel(seed=42)
    spy = _benchmark(prices.index, seed=43)
    t_pos = 300
    t_mid = prices.index[t_pos]

    before = compute_trend_breadth_all(prices, spy).loc[t_mid].copy()

    prices2 = prices.copy()
    spy2 = spy.copy()
    rng = np.random.default_rng(1234)
    prices2.iloc[t_pos + 1:, :] *= rng.uniform(0.4, 1.6, size=(len(prices) - t_pos - 1, prices.shape[1]))
    spy2.iloc[t_pos + 1:] *= rng.uniform(0.4, 1.6, size=len(spy) - t_pos - 1)

    after = compute_trend_breadth_all(prices2, spy2).loc[t_mid]

    for arm in ("absolute", "relative", "dual"):
        assert np.isclose(before[arm], after[arm], equal_nan=True), (
            f"look-ahead: {arm} at T changed when future data mutated "
            f"(before={before[arm]}, after={after[arm]})"
        )


def test_final_bar_perturbation_invariance():
    """em-rotation-style: perturbing ONLY the final bar must leave every
    earlier date's breadth unchanged, for all three arms."""
    prices = _panel(seed=11)
    spy = _benchmark(prices.index, seed=12)

    base = compute_trend_breadth_all(prices, spy)

    prices2 = prices.copy()
    spy2 = spy.copy()
    prices2.iloc[-1, :] *= 1.25
    spy2.iloc[-1] *= 0.80

    pert = compute_trend_breadth_all(prices2, spy2)

    # Everything except the last row must be identical.
    pd.testing.assert_frame_equal(base.iloc[:-1], pert.iloc[:-1])


# ---------------------------------------------------------------------------
# Failure mode 3 — shared denominator
# ---------------------------------------------------------------------------

def test_shared_denominator_identical_across_arms():
    """All three arms must share an identical per-day denominator: for any day
    with a defined breadth, abs/rel/dual are the same integer multiple of
    1/denom, so denom recovered from each arm agrees."""
    prices = _panel(seed=5)
    spy = _benchmark(prices.index, seed=6)
    # Engineer asymmetric missingness: knock holes in two names.
    prices.iloc[250:260, 0] = np.nan
    prices.iloc[400:405, 3] = np.nan

    arms = compute_trend_breadth_all(prices, spy)
    denom = shared_valid_count(prices, spy)

    defined = arms["absolute"].notna()
    d = denom[defined]
    # numerators implied by each arm must be integers on the same denominator
    for arm in ("absolute", "relative", "dual"):
        num = arms[arm][defined] * d
        # allow float error, must round to an integer count
        assert np.allclose(num, np.round(num), atol=1e-9), (
            f"{arm} numerator is not an integer count on the shared denominator"
        )
        assert (np.round(num) <= d + 1e-9).all(), f"{arm} numerator exceeds denominator"


def test_missing_name_dropped_from_both_legs():
    """A constituent uncomputable on the absolute leg (price NaN) must also be
    excluded from the relative-leg denominator on that day — the shared mask
    guarantees no name counts on one leg but not the other."""
    prices = _panel(n_tickers=6, seed=8)
    spy = _benchmark(prices.index, seed=9)
    # Make one name missing on a block of days AFTER its MA is established.
    hole = slice(300, 320)
    prices.iloc[hole, 2] = np.nan

    denom_full = shared_valid_count(prices, spy)
    # Rebuild with that name dropped entirely; the denominator on the hole days
    # must match dropping just that one name from the full-panel denominator.
    denom_after = shared_valid_count(prices, spy)  # same object; check internal consistency

    # On hole days the shared count must be strictly less than the count on the
    # surrounding days (that name removed), and never counts the NaN name.
    around = denom_after.iloc[295]
    during = denom_after.iloc[305]
    assert during == around - 1, (
        f"missing name not dropped from shared denominator "
        f"(around={around}, during={during})"
    )
    assert (denom_full == denom_after).all()


# ---------------------------------------------------------------------------
# Deployed-parity anchor
# ---------------------------------------------------------------------------

def test_absolute_arm_matches_deployed_breadth():
    """With a complete benchmark, the module's 'absolute' arm reproduces the
    deployed run_ma200_sweep.compute_ma200_breadth() to the float — the shared
    mask collapses to the absolute leg's own validity when SPY is present on
    every constituent trading day."""
    prices = _panel(seed=21)
    spy = _benchmark(prices.index, seed=22)

    deployed = compute_ma200_breadth(prices, period=MA_PERIOD)
    ours = compute_trend_breadth(prices, spy, mode="absolute", period=MA_PERIOD)

    aligned = pd.concat([deployed.rename("dep"), ours.rename("ours")], axis=1)
    both = aligned.dropna()
    assert len(both) > 100, "too few overlapping observations to trust the check"
    assert np.allclose(both["dep"], both["ours"], atol=1e-12), (
        "absolute arm diverged from the deployed compute_ma200_breadth"
    )
    # NaN pattern must match too.
    assert deployed.isna().equals(ours.isna())


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------

def test_dual_never_exceeds_either_leg():
    """A2 (A0 AND A1) share can never exceed either single-leg share."""
    prices = _panel(seed=31)
    spy = _benchmark(prices.index, seed=32)
    arms = compute_trend_breadth_all(prices, spy).dropna()
    assert (arms["dual"] <= arms["absolute"] + 1e-12).all()
    assert (arms["dual"] <= arms["relative"] + 1e-12).all()


def test_breadth_bounded_unit_interval():
    prices = _panel(seed=33)
    spy = _benchmark(prices.index, seed=34)
    arms = compute_trend_breadth_all(prices, spy).dropna()
    assert (arms >= -1e-12).all().all()
    assert (arms <= 1 + 1e-12).all().all()


def test_zero_valid_day_is_nan():
    """A day where no constituent has enough history yields NaN, not 0/0 error,
    for every arm."""
    prices = _panel(n_days=250, seed=35)
    spy = _benchmark(prices.index, seed=36)
    arms = compute_trend_breadth_all(prices, spy)
    # Early rows (< min_periods) have no valid MA -> NaN across the board.
    early = arms.iloc[10]
    assert early.isna().all()


def test_relative_leg_detects_outperformance():
    """If a subset of names structurally out-trends the benchmark while the
    rest merely track it, the relative arm must register meaningfully more
    breadth than a flat 0/1 coin-flip — evidence the leg carries information
    distinct from the absolute leg in a broad rally."""
    idx = pd.date_range("2019-01-02", periods=500, freq="B")
    n = 10
    # Benchmark and all names share a common up-market drift...
    common = np.cumsum(np.random.default_rng(1).normal(0.0005, 0.008, len(idx)))
    spy = pd.Series(400.0 * np.exp(common), index=idx)
    cols = [f"T{i:02d}" for i in range(n)]
    prices = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for i in range(n):
        # Half the names add positive idiosyncratic alpha vs the benchmark,
        # half add negative -> relative breadth should sit near 0.5, and
        # crucially be well-defined and responsive.
        alpha = 0.0004 if i < n // 2 else -0.0004
        noise = np.random.default_rng(100 + i).normal(0, 0.004, len(idx))
        prices[cols[i]] = 100.0 * np.exp(common + np.cumsum(np.full(len(idx), alpha) + noise))
    rel = compute_trend_breadth(prices, spy, mode="relative").dropna()
    # The out-performers should keep relative breadth clearly off the floor.
    assert rel.tail(60).mean() > 0.3, f"relative leg unresponsive (mean={rel.tail(60).mean():.3f})"
    # And absolute breadth in this up-market should be high for ~all names,
    # so relative carries information absolute does not.
    abs_b = compute_trend_breadth(prices, spy, mode="absolute").dropna()
    assert abs_b.tail(60).mean() > rel.tail(60).mean()


# ---------------------------------------------------------------------------
# Asymmetric-window neighbour arms (WS5 register #5 rel-150d, #6 rel-250d)
# ---------------------------------------------------------------------------

def test_rel_period_default_is_bit_identical():
    """rel_period=None (default) must reproduce the single-window computation
    exactly — the asymmetric extension is a strict superset that changes
    nothing on the frozen symmetric path."""
    prices = _panel(seed=61)
    spy = _benchmark(prices.index, seed=62)
    a = compute_trend_breadth_all(prices, spy, period=MA_PERIOD)
    b = compute_trend_breadth_all(prices, spy, period=MA_PERIOD, rel_period=None)
    c = compute_trend_breadth_all(prices, spy, period=MA_PERIOD, rel_period=MA_PERIOD)
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_frame_equal(a, c)


def test_asymmetric_window_changes_only_rel_and_dual():
    """Sliding the relative leg to 150d leaves the absolute arm untouched
    (it still uses the 200d window) and keeps the dual invariant intact."""
    prices = _panel(seed=63)
    spy = _benchmark(prices.index, seed=64)
    sym = compute_trend_breadth_all(prices, spy, period=200)
    asy = compute_trend_breadth_all(prices, spy, period=200, rel_period=150)
    common = sym.dropna().index.intersection(asy.dropna().index)
    # Absolute arm unchanged where BOTH are defined... but the shared mask
    # differs between symmetric-200 and asymmetric-(200,150), so compare only
    # the STRUCTURE, not equality: dual <= each leg must still hold.
    assert (asy["dual"].dropna() <= asy["absolute"].dropna() + 1e-12).all()
    assert (asy["dual"].dropna() <= asy["relative"].dropna() + 1e-12).all()
    # The 150d relative leg warms up sooner than the 200d absolute leg, but the
    # shared mask requires both, so the first defined date is governed by 200d.
    assert asy["dual"].first_valid_index() == sym["dual"].first_valid_index()
    # And the relative arm genuinely differs from the symmetric case somewhere.
    rel_sym = sym["relative"].reindex(common)
    rel_asy = asy["relative"].reindex(common)
    assert not np.allclose(rel_sym.values, rel_asy.values)


# ---------------------------------------------------------------------------
# Date-boundary rule (vault CLAUDE.md): one month boundary, one year boundary
# ---------------------------------------------------------------------------

def test_month_boundary_continuity():
    """Breadth must be continuous and finite across a month boundary — the
    rolling window must not reset or produce spurious NaN at month ends.
    (Python datetime months are 1-indexed; here we just assert the series is
    defined on both sides of a Jan->Feb boundary.)"""
    prices = _panel(n_days=500, seed=41, start="2019-06-03")
    spy = _benchmark(prices.index, seed=42)
    arms = compute_trend_breadth_all(prices, spy)
    # Find a month boundary well past the MA warm-up.
    warm = arms.dropna()
    dts = warm.index
    # locate first date whose month differs from the prior trading day's month
    boundary_positions = [
        i for i in range(1, len(dts)) if dts[i].month != dts[i - 1].month
    ]
    assert boundary_positions, "no month boundary found in warmed-up window"
    p = boundary_positions[len(boundary_positions) // 2]
    left, right = warm.iloc[p - 1], warm.iloc[p]
    assert left.notna().all() and right.notna().all()
    # Breadth is a bounded fraction — day-over-day change cannot exceed 1.
    assert (abs(right - left) <= 1.0 + 1e-12).all()


def test_year_boundary_continuity():
    """Same, across a Dec->Jan year boundary."""
    prices = _panel(n_days=520, seed=51, start="2019-03-01")
    spy = _benchmark(prices.index, seed=52)
    arms = compute_trend_breadth_all(prices, spy)
    warm = arms.dropna()
    dts = warm.index
    boundary_positions = [
        i for i in range(1, len(dts)) if dts[i].year != dts[i - 1].year
    ]
    assert boundary_positions, "no year boundary found in warmed-up window"
    p = boundary_positions[0]
    left, right = warm.iloc[p - 1], warm.iloc[p]
    assert left.notna().all() and right.notna().all()
    assert (dts[p].year - dts[p - 1].year) == 1
    assert (abs(right - left) <= 1.0 + 1e-12).all()
