"""The coverage floor on a panel's CURRENT roster.

The condition being guarded does not crash and does not look wrong.
Breadth is a RATIO, so a partial vendor download still returns a
plausible number — computed on whatever came back. On 2026-08-08 two
panels were refreshed and committed on partial downloads and nothing in
the pipeline objected:

  EXH2   2 of 37 current constituents. The display guard suppressed the
         bar, so nothing false was shown, but the panel was committed.
  IDP6   371 of 603, and DEPLOYED into Strategy A's universe. It
         published ma_breadth 0.6334 where full coverage gives 0.6173.
         Only 1.6pp out — and that was luck, not a property of the
         sample.

So the floors are tested against the real observed numbers, not invented
ones, and the calibration case below pins them to the distribution they
were derived from. Loosening either floor past a healthy panel, or
tightening one onto the structural tail (ITWN, ICHN), fails here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute_breadth import (  # noqa: E402
    COVERAGE_OVERRIDE_ENV,
    MIN_ROSTER_COVERAGE_FAIL,
    MIN_ROSTER_COVERAGE_WARN,
    coverage_verdict,
)

# Measured across all 38 committed panels on 2026-08-09: (n_with_ma50,
# n_constituents, expected verdict). Healthy sits at 97-100%; ITWN and
# ICHN are a structural tail where some Taiwanese and Chinese lines
# genuinely lack yfinance history; the last two are the incident.
OBSERVED = [
    ("SOXX",  30,  30, "ok"),      # 100.0%
    ("IUUS",  31,  31, "ok"),      # 100.0%
    ("CSP1", 502, 504, "ok"),      #  99.6%
    ("EXH3", 106, 107, "ok"),      #  99.1%
    ("CNDX", 100, 102, "ok"),      #  98.0%
    ("IUCS",  33,  34, "ok"),      #  97.1%
    ("EXV2",  20,  21, "ok"),      #  95.2%
    ("ICHN", 539, 576, "ok"),      #  93.6% — structural tail
    ("ITWN",  70,  78, "ok"),      #  89.7% — structural tail, nearest miss
    ("IDP6", 371, 603, "warn"),    #  61.5% — the deployed panel
    ("EXH2",   2,  37, "fail"),    #   5.4% — the broken one
]


@pytest.mark.parametrize("etf,n_ma,n_const,expected", OBSERVED)
def test_calibration_against_observed_panels(etf, n_ma, n_const, expected):
    verdict, _ = coverage_verdict(n_ma, n_const)
    assert verdict == expected, f"{etf} classified {verdict}, expected {expected}"


def test_floors_are_ordered_and_leave_room():
    """FAIL below WARN, and both inside (0, 1)."""
    assert 0 < MIN_ROSTER_COVERAGE_FAIL < MIN_ROSTER_COVERAGE_WARN < 1


def test_warn_floor_clears_the_structural_tail():
    """ITWN at 89.7% is the nearest healthy panel to the WARN floor.

    Raising WARN above it would make the weekly refresh warn on a panel
    that is simply what Taiwanese coverage looks like — the fastest way
    to teach an operator to ignore the warning.
    """
    assert coverage_verdict(70, 78)[0] == "ok"
    assert MIN_ROSTER_COVERAGE_WARN < 70 / 78


def test_fail_floor_sits_below_every_real_roster():
    """Nothing a real roster has produced should hard-fail except EXH2."""
    real = [(n_ma, n_c) for _, n_ma, n_c, exp in OBSERVED if exp != "fail"]
    assert all(coverage_verdict(a, b)[0] != "fail" for a, b in real)
    assert MIN_ROSTER_COVERAGE_FAIL < min(a / b for a, b in real)


def test_fail_floor_would_have_caught_the_incident():
    """The whole point: 2 of 37 must not be publishable."""
    assert coverage_verdict(2, 37)[0] == "fail"


# --- boundaries ---------------------------------------------------------

def test_exactly_at_the_warn_floor_is_ok():
    """Inclusive at the bottom of the better band, so the documented
    percentage reads the way people expect."""
    assert coverage_verdict(85, 100)[0] == "ok"


def test_just_below_the_warn_floor_warns():
    assert coverage_verdict(84, 100)[0] == "warn"


def test_exactly_at_the_fail_floor_warns():
    assert coverage_verdict(50, 100)[0] == "warn"


def test_just_below_the_fail_floor_fails():
    assert coverage_verdict(49, 100)[0] == "fail"


def test_full_and_empty_coverage():
    assert coverage_verdict(100, 100) == ("ok", 1.0)
    assert coverage_verdict(0, 100)[0] == "fail"


def test_empty_roster_fails_rather_than_dividing_by_zero():
    assert coverage_verdict(0, 0) == ("fail", 0.0)
    assert coverage_verdict(5, -1) == ("fail", 0.0)


def test_returned_coverage_is_the_actual_ratio():
    verdict, coverage = coverage_verdict(371, 603)
    assert verdict == "warn"
    assert coverage == pytest.approx(0.6153, abs=1e-4)


def test_override_env_name_is_stable():
    """Named in the stderr banner and in DATA_INTEGRITY_POLICY; renaming
    it silently would leave both pointing at nothing."""
    assert COVERAGE_OVERRIDE_ENV == "ALLOW_THIN_BREADTH"
