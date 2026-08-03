"""Offline tests for the signal/instrument pair-integrity check.

Only the pure logic is tested here — basket construction, correlation, and
the verdict rules. The fetch itself needs network and belongs to the
weekly workflow.

The behaviour that matters most is the SKIP-versus-FAIL boundary. A guard
that cries wolf on a thin fetch gets ignored, and an ignored guard is
worse than none, so insufficient data must never produce FAIL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_pair_integrity as cpi  # noqa: E402


def _returns(**columns: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=len(next(iter(columns.values()))))
    return pd.DataFrame(columns, index=idx)


def _long(values: list[float], reps: int) -> list[float]:
    """Repeat a pattern to clear the MIN_OBS threshold."""
    out: list[float] = []
    while len(out) < reps:
        out.extend(values)
    return out[:reps]


N = cpi.MIN_OBS + 50


# =========================================================================
# Basket construction
# =========================================================================
def test_basket_is_the_equal_weight_mean():
    frame = _returns(A=[0.02] * N, B=[0.04] * N)
    basket, n_names = cpi.basket_returns(frame, ["A", "B"])
    assert n_names == 2
    assert basket.iloc[0] == pytest.approx(0.03)


def test_basket_ignores_names_with_too_little_data():
    """A newly listed constituent must not drag the basket or the count."""
    thin = [np.nan] * (N - 10) + [0.01] * 10
    frame = _returns(A=[0.02] * N, B=[0.04] * N, THIN=thin)
    basket, n_names = cpi.basket_returns(frame, ["A", "B", "THIN"])
    assert n_names == 2
    assert basket.iloc[0] == pytest.approx(0.03)


def test_basket_ignores_names_absent_from_the_panel():
    frame = _returns(A=[0.02] * N)
    basket, n_names = cpi.basket_returns(frame, ["A", "DELISTED"])
    assert n_names == 1
    assert not basket.empty


def test_basket_is_empty_when_nothing_resolves():
    frame = _returns(A=[0.02] * N)
    basket, n_names = cpi.basket_returns(frame, ["NOPE"])
    assert basket.empty and n_names == 0


# =========================================================================
# Correlation
# =========================================================================
def test_correlation_of_a_series_with_itself_is_one():
    series = pd.Series(_long([0.01, -0.02, 0.005, 0.013], N))
    corr, n_obs = cpi.pair_correlation(series, series)
    assert corr == pytest.approx(1.0)
    assert n_obs == N


def test_correlation_of_an_inverted_series_is_minus_one():
    series = pd.Series(_long([0.01, -0.02, 0.005, 0.013], N))
    corr, _ = cpi.pair_correlation(series, -series)
    assert corr == pytest.approx(-1.0)


def test_correlation_counts_only_overlapping_observations():
    priced = pd.Series([0.01, 0.02, np.nan, 0.03])
    basket = pd.Series([0.01, 0.02, 0.03, np.nan])
    _, n_obs = cpi.pair_correlation(priced, basket)
    assert n_obs == 2


def test_correlation_is_undefined_without_enough_overlap():
    corr, n_obs = cpi.pair_correlation(
        pd.Series([0.01, np.nan]), pd.Series([np.nan, 0.02])
    )
    assert np.isnan(corr) and n_obs == 0


# =========================================================================
# Verdicts — the SKIP versus FAIL boundary
# =========================================================================
def test_a_well_paired_member_passes():
    status, note = cpi.classify(0.973, n_names=12, n_obs=495)
    assert status == cpi.PASS and note == ""


def test_the_observed_defect_fails():
    """EXH3.DE against an industrials panel: 0.244 over 497 observations."""
    status, note = cpi.classify(0.244, n_names=12, n_obs=497)
    assert status == cpi.FAIL
    assert "0.244" in note and "below" in note


def test_a_thin_basket_skips_rather_than_fails():
    status, note = cpi.classify(0.1, n_names=cpi.MIN_NAMES - 1, n_obs=495)
    assert status == cpi.SKIP
    assert "constituents resolved" in note


def test_a_short_overlap_skips_rather_than_fails():
    status, note = cpi.classify(0.1, n_names=12, n_obs=cpi.MIN_OBS - 1)
    assert status == cpi.SKIP
    assert "overlapping observations" in note


def test_an_undefined_correlation_skips():
    status, _ = cpi.classify(float("nan"), n_names=12, n_obs=495)
    assert status == cpi.SKIP


def test_the_floor_is_inclusive_at_the_boundary():
    assert cpi.classify(cpi.DEFAULT_FLOOR, 12, 495)[0] == cpi.PASS
    assert cpi.classify(cpi.DEFAULT_FLOOR - 1e-9, 12, 495)[0] == cpi.FAIL


def test_the_floor_sits_between_the_observed_defect_and_the_worst_clean_pair():
    """Calibration, pinned so a future tightening has to face the evidence.

    The 2026-08-03 sweep put every correct pair between 0.777 (IDP6 -> IJR,
    an equal-weight small-cap basket against a cap-weighted index) and
    0.991, and the defect at 0.244. The floor must clear the defect and
    leave IDP6 room; it cannot go far above 0.75 without a false positive.
    """
    assert 0.244 < cpi.DEFAULT_FLOOR < 0.777


# =========================================================================
# Constituent loading
# =========================================================================
def test_latest_constituents_reads_the_newest_populated_snapshot():
    names = cpi.latest_constituents("EXH3", sample=5)
    assert len(names) == 5
    assert "SIE.DE" in names, "EXH3's panel is industrials — Siemens is in it"


def test_latest_constituents_is_empty_for_an_unknown_etf():
    assert cpi.latest_constituents("NOSUCHETF") == []


def test_traded_symbol_uses_the_registry_proxy():
    assert cpi.traded_symbol("EXH3") == "EXH4.DE"
    assert cpi.traded_symbol("IUES") == "XLE"
    assert cpi.traded_symbol("SOXX") == "SOXX"
