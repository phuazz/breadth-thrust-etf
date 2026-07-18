"""Tests for scripts/run_c_seat_watch.py — WS7 evidence accumulator.

The registered constants are pinned so the pass bar cannot be tuned after
evidence exists; the without-C algebra is checked as an identity; the
append-only merge is checked to NEVER replace a recorded week (the
point-in-time property the review depends on), including across a year
boundary (CLAUDE.md date rule).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_c_seat_watch import (  # noqa: E402
    ANCHOR_DATE,
    NOISE_BAND_PP,
    TRIPWIRE_PP,
    append_rows,
    ew_week_return,
    without_c_return,
)


def test_registered_constants_are_frozen():
    """KICKOFF_ws7-c-seat.md fixes these before any evidence is read; a
    change here must be a deliberate, signed amendment — not a drive-by."""
    assert ANCHOR_DATE == "2026-07-03"
    assert TRIPWIRE_PP == -5.0
    assert NOISE_BAND_PP == 2.0


def test_ew_week_return_equal_moves_charge_no_cost():
    """When every name moves identically there is no drift, hence no
    re-equalisation turnover and no cost: net = gross."""
    p0 = {"AAA": 100.0, "BBB": 50.0}
    p1 = {"AAA": 102.0, "BBB": 51.0}
    r, n = ew_week_return(p0, p1, {"AAA": 8.0, "BBB": 12.0})
    assert n == 2
    assert r == pytest.approx(0.02, abs=1e-12)


def test_ew_week_return_drift_costs_match_hand_calculation():
    p0 = {"AAA": 100.0, "BBB": 100.0}
    p1 = {"AAA": 110.0, "BBB": 100.0}      # +10% / 0% -> gross +5%
    bps = {"AAA": 10.0, "BBB": 10.0}
    r, n = ew_week_return(p0, p1, bps)
    gross = 0.05
    # Drifted weights: 0.5*1.10/1.05 and 0.5*1.00/1.05; turnover is
    # symmetric, |0.5 - 0.52381| = 0.02381 per name.
    turnover = abs(0.5 - 0.5 * 1.10 / 1.05) + abs(0.5 - 0.5 * 1.00 / 1.05)
    assert r == pytest.approx(gross - turnover * 0.0010, abs=1e-9)
    assert n == 2


def test_ew_week_return_drops_missing_names_and_reports_count():
    p0 = {"AAA": 100.0, "BBB": 100.0, "CCC": None}
    p1 = {"AAA": 101.0, "CCC": 50.0}       # BBB missing t1, CCC missing t0
    r, n = ew_week_return(p0, p1, {})
    assert n == 1
    assert r == pytest.approx(0.01, abs=1e-6)  # AAA only (default bps, ~0 cost)


def test_without_c_return_is_an_exact_decomposition():
    """Construct the blend from components; removing C must recover the
    rest-of-book return exactly."""
    r_rest, r_c, w_c = 0.004, -0.020, 0.10
    r_blend = w_c * r_c + (1 - w_c) * r_rest
    assert without_c_return(r_blend, r_c, w_c) == pytest.approx(r_rest,
                                                                 abs=1e-15)
    # Gate-halved slice (RISK_OFF week): same identity at w_c = 0.05.
    w_c = 0.05
    r_blend = w_c * r_c + (1 - w_c) * r_rest
    assert without_c_return(r_blend, r_c, w_c) == pytest.approx(r_rest,
                                                                 abs=1e-15)
    with pytest.raises(ValueError):
        without_c_return(0.0, 0.0, 1.0)


def test_append_rows_never_replaces_a_recorded_week():
    existing = [{"week_end": "2026-07-10", "r_rotation": 0.010}]
    revised = [{"week_end": "2026-07-10", "r_rotation": 0.999},   # revision
               {"week_end": "2026-07-17", "r_rotation": 0.002}]
    out = append_rows(existing, revised)
    assert [r["week_end"] for r in out] == ["2026-07-10", "2026-07-17"]
    assert out[0]["r_rotation"] == 0.010          # original PRESERVED


def test_append_rows_sorts_across_the_year_boundary():
    existing = [{"week_end": "2027-01-02"}]
    out = append_rows(existing, [{"week_end": "2026-12-26"}])
    assert [r["week_end"] for r in out] == ["2026-12-26", "2027-01-02"]
