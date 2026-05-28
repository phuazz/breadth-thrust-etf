"""Direct unit tests on top_k_breadth_weight — synthetic stress inputs.

Added 2026-05-28 alongside test_backtest_math.py. Where that file
asserts INVARIANTS on the deployed JSON outputs, this one stress-tests
the WEIGHT FUNCTION ITSELF with synthetic inputs. Together they prove
the Phase 20.1 fix is genuinely robust, not just a patched-and-rerun.

Critical cases (all should produce long-only, sum<=1.0, no NaN):

  1. All positives (typical absolute-breadth case)
  2. All negatives (relative-breadth, broad market collapse)
  3. Mixed sign (the original Phase 20 bug condition)
  4. NaN in some entries (insufficient history for some sectors)
  5. All NaN
  6. Exactly K positives
  7. Fewer than K positives
  8. Zero values
  9. Single-element input
  10. Tied values at the K boundary
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from run_portfolio import top_k_breadth_weight  # noqa: E402


def _check_invariants(w: pd.Series, K: int):
    """All cases must satisfy these. Asserts in plain English so a
    failing test's traceback is immediately readable."""
    assert (w >= 0).all(), (
        f"Long-only invariant broken: some weights are negative.\n"
        f"  weights = {dict(w[w < 0])}\n"
        f"  full = {dict(w)}"
    )
    total = w.sum()
    assert total <= 1.0 + 1e-9, (
        f"Sum invariant broken: weights sum to {total:.6f} > 1.0.\n"
        f"  weights = {dict(w[w > 0])}"
    )
    assert not w.isna().any(), f"NaN in weights: {dict(w[w.isna()])}"
    n_held = int((w > 0).sum())
    assert n_held <= K, (
        f"Held {n_held} positions but K={K} — should hold at most K.\n"
        f"  weights = {dict(w[w > 0])}"
    )


# ----- Synthetic stress cases ----------------------------------------------

def test_all_positives_typical_absolute_breadth():
    """Typical absolute-breadth case: 14 sectors with breadth in [0,1].
    Should pick top K and weight by signal share, sum to 1.0."""
    signal = pd.Series({f"SEC{i}": 0.30 + 0.05*i for i in range(14)})  # 0.30 .. 0.95
    w = top_k_breadth_weight(7)(signal)
    _check_invariants(w, 7)
    assert (w > 0).sum() == 7, "Should hold exactly 7 with 14 positives"
    assert abs(w.sum() - 1.0) < 1e-9, "Sum should be exactly 1.0"


def test_all_negatives_broad_relative_collapse():
    """All sectors below cross-sectional mean (all relative-breadths
    negative). Strategy should NOT short anything — go flat / cash."""
    signal = pd.Series({f"SEC{i}": -0.05 - 0.01*i for i in range(14)})
    w = top_k_breadth_weight(7)(signal)
    _check_invariants(w, 7)
    assert w.sum() == 0.0, (
        f"All-negative input should produce zero weights (go flat), "
        f"not {dict(w[w > 0])}"
    )


def test_mixed_sign_the_phase20_bug_condition():
    """The exact condition that produced the Phase 20 bug: top-K
    contains both positive and negative values. Pre-fix: positives
    weighted up >1.0, negatives implicitly short. Post-fix: drop the
    negatives, renormalise positives to sum to 1.0."""
    signal = pd.Series({
        "A": 0.30, "B": 0.20, "C": 0.10,    # positives
        "D": -0.05, "E": -0.10, "F": -0.15, "G": -0.20,  # negatives
    })
    w = top_k_breadth_weight(7)(signal)
    _check_invariants(w, 7)
    # Only positives should be held
    assert set(w[w > 0].index) == {"A", "B", "C"}, (
        f"Expected only positives in held set, got {set(w[w > 0].index)}"
    )
    # Held weights sum to exactly 1.0 (renormalisation)
    assert abs(w.sum() - 1.0) < 1e-9, (
        f"Renormalisation broken: positives should sum to 1.0, got {w.sum()}"
    )
    # Weights are share of positives
    assert abs(w["A"] - 0.30/0.60) < 1e-9, "A should get 50% of positives"
    assert abs(w["B"] - 0.20/0.60) < 1e-9, "B should get 33% of positives"
    assert abs(w["C"] - 0.10/0.60) < 1e-9, "C should get 17% of positives"


def test_nan_in_some_entries():
    """Some sectors have NaN signal (insufficient history). Function
    should drop NaN and operate on the valid subset."""
    signal = pd.Series({
        "A": 0.50, "B": 0.40, "C": float("nan"),
        "D": 0.30, "E": float("nan"), "F": 0.10,
    })
    w = top_k_breadth_weight(3)(signal)
    _check_invariants(w, 3)
    assert (w[["C", "E"]] == 0).all(), "NaN sectors should get zero weight"
    assert (w[["A", "B", "D"]] > 0).all(), "Valid sectors should get positive weight"


def test_all_nan_zero_weights():
    """Pathological: all signals NaN. Should produce zero weights."""
    signal = pd.Series({f"SEC{i}": float("nan") for i in range(5)})
    w = top_k_breadth_weight(3)(signal)
    _check_invariants(w, 3)
    assert w.sum() == 0.0


def test_exactly_K_positives_with_negatives_below():
    """K positives at the top, the rest negative. Should hold all K
    positives at signal-share weights, drop negatives."""
    signal = pd.Series({
        "A": 0.40, "B": 0.30, "C": 0.20, "D": 0.10,    # 4 positives
        "E": -0.05, "F": -0.10, "G": -0.15,             # 3 negatives
    })
    w = top_k_breadth_weight(4)(signal)
    _check_invariants(w, 4)
    assert (w > 0).sum() == 4
    assert abs(w.sum() - 1.0) < 1e-9


def test_fewer_than_K_positives():
    """Only 2 positives when K=7. Function should hold the 2 at
    signal-share, sum to 1.0 (renormalised across just 2)."""
    signal = pd.Series({
        "A": 0.30, "B": 0.10,
        "C": -0.05, "D": -0.10, "E": -0.15, "F": -0.20, "G": -0.25,
    })
    w = top_k_breadth_weight(7)(signal)
    _check_invariants(w, 7)
    assert set(w[w > 0].index) == {"A", "B"}
    assert abs(w.sum() - 1.0) < 1e-9


def test_zero_values_treated_as_below_mean():
    """A relative-breadth value of exactly zero (sector exactly at the
    mean) should be DROPPED — we only weight strictly-positive
    relatives. Otherwise the boundary case is ambiguous and a sector
    that's barely below mean could sneak in via signal noise."""
    signal = pd.Series({"A": 0.30, "B": 0.0, "C": -0.10})
    w = top_k_breadth_weight(3)(signal)
    _check_invariants(w, 3)
    assert w["A"] == 1.0
    assert w["B"] == 0.0
    assert w["C"] == 0.0


def test_single_positive_element():
    """Only one sector ever exists / clears the filter. Should hold
    that one at 100%."""
    signal = pd.Series({"A": 0.50})
    w = top_k_breadth_weight(7)(signal)
    _check_invariants(w, 7)
    assert w["A"] == 1.0


def test_K_larger_than_universe():
    """K=20 but only 5 sectors exist. Should hold all 5 positives,
    weight by signal share."""
    signal = pd.Series({"A": 0.5, "B": 0.4, "C": 0.3, "D": 0.2, "E": 0.1})
    w = top_k_breadth_weight(20)(signal)
    _check_invariants(w, 20)
    assert (w > 0).sum() == 5
    assert abs(w.sum() - 1.0) < 1e-9


def test_tied_values_at_K_boundary():
    """Two sectors tied at the value that lands at position K. nlargest
    breaks ties by index order — verify we get exactly K names (not K+1
    by accident due to tie-handling)."""
    signal = pd.Series({
        "A": 0.50, "B": 0.40, "C": 0.30,  # top 3
        "D": 0.20, "E": 0.20,             # tied at K=4 boundary
    })
    w = top_k_breadth_weight(4)(signal)
    _check_invariants(w, 4)
    held = (w > 0).sum()
    assert held == 4, f"Expected exactly 4 holdings, got {held}"


# ----- Verifies the pre-Phase-20.1 bug would have been caught -------------

def test_phase20_bug_would_have_failed_old_function():
    """Documentation-only test: shows that the OLD function (which
    just did `normed = top / top.sum()`) would have failed
    test_within_sleeve_weights_sum_to_at_most_one. This is the smoke
    that proves the test layer catches the bug."""
    signal = pd.Series({
        "A": 0.30, "B": 0.20, "C": 0.10,
        "D": -0.05, "E": -0.10, "F": -0.15, "G": -0.20,
    })
    # Simulate the OLD broken behavior — divide each top-K by top.sum()
    top = signal.nlargest(7)
    old_normed = top / top.sum()  # this is what was broken
    # The positives in old_normed would sum to >1.0
    old_pos_sum = old_normed[old_normed > 0].sum()
    assert old_pos_sum > 1.0, (
        f"Sanity check: the OLD function would have produced positives "
        f"summing to {old_pos_sum:.4f} > 1.0. If this assertion ever "
        f"fails, the bug-condition has changed and the regression test "
        f"may no longer be guarding against the original failure."
    )
    # And the FIX produces a clean sum-to-1.0
    fixed = top_k_breadth_weight(7)(signal)
    assert abs(fixed.sum() - 1.0) < 1e-9, "Post-fix should sum to exactly 1.0"
    # Confirms the difference is real and the regression suite would
    # have caught the original bug if it had existed then.
