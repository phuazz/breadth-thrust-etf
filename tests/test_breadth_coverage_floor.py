"""The coverage floor on a panel's CURRENT roster.

The condition being guarded does not crash and does not look wrong.
Breadth is a RATIO, so a partial vendor download still returns a
plausible number — computed on whatever came back. On 2026-08-08 two
panels were refreshed and committed on partial downloads and nothing in
the pipeline objected:

  EXH2   2 of 37 current constituents. The display guard suppressed the
         bar, so nothing false was shown, but the panel was committed.
  IDP6   371 of 603, and DEPLOYED into Strategy A's universe. It
         published ma_breadth 0.6334; recomputed at 99.5% coverage the
         same date reads 0.66, so the thin sample was 2.7pp out.

So the floors are tested against the real observed numbers, not invented
ones, and the calibration case below pins them to the distribution they
were derived from. Loosening either floor past a healthy panel, or
tightening one onto the structural tail, fails here.

The tail was smaller than it looked. ITWN's 89.7% was read as "what
Taiwanese coverage looks like" and the WARN floor was set beneath it on
that basis; it was in fact the unmapped "Gretai Securities Market" venue
dropping 7 Taipei Exchange names into the roster unpriceable. Fixed
2026-08-16, ITWN now runs at 98.7%, and WARN moved to 0.90 — which is why
the ITWN row below now expects "warn" at its old thin number, and the
nearest-healthy-panel test is anchored on ICHN.
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
# n_constituents, expected verdict). Healthy sits at 97-100%; ICHN is the
# structural tail, where some Chinese lines genuinely lack yfinance
# history; the last three are the ones a floor must catch.
OBSERVED = [
    ("SOXX",  30,  30, "ok"),      # 100.0%
    ("IUUS",  31,  31, "ok"),      # 100.0%
    ("CSP1", 502, 504, "ok"),      #  99.6%
    ("EXH3", 106, 107, "ok"),      #  99.1%
    ("CNDX", 100, 102, "ok"),      #  98.0%
    ("ITWN",  77,  78, "ok"),      #  98.7% — repaired, 2026-08-16
    ("IUCS",  33,  34, "ok"),      #  97.1%
    ("EXV2",  20,  21, "ok"),      #  95.2%
    ("ICHN", 539, 576, "ok"),      #  93.6% — structural tail, nearest miss
    ("ITWN",  70,  78, "warn"),    #  89.7% — the bug, as it read before
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
    """ICHN at 93.6% is now the nearest healthy panel to the WARN floor.

    Raising WARN above it would make the weekly refresh fire on a panel
    that is simply what Chinese coverage looks like — the fastest way to
    teach an operator to ignore the guard. ITWN used to hold this role at
    89.7%, which is what kept the floor at 0.85; that number turned out to
    be a resolver bug rather than a fact about Taiwanese listings, so the
    anchor moved down the list and the floor moved up.
    """
    assert coverage_verdict(539, 576)[0] == "ok"
    assert MIN_ROSTER_COVERAGE_WARN < 539 / 576


def test_warn_floor_catches_the_itwn_gap_it_used_to_excuse():
    """The old ITWN reading must no longer pass as healthy.

    7 of 78 Taipei Exchange names resolved at no vendor, and breadth is a
    ratio, so the panel published a plausible number on a universe 9%
    smaller than the fund for 451 roster-days. At 0.90 that reading is
    caught; at the old 0.85 it was explicitly excused.
    """
    assert coverage_verdict(70, 78)[0] == "warn"


def test_fail_floor_sits_below_every_real_roster():
    """Nothing a real roster has produced should hard-fail except EXH2."""
    real = [(n_ma, n_c) for _, n_ma, n_c, exp in OBSERVED if exp != "fail"]
    assert all(coverage_verdict(a, b)[0] != "fail" for a, b in real)
    assert MIN_ROSTER_COVERAGE_FAIL < min(a / b for a, b in real)


def test_fail_floor_would_have_caught_the_incident():
    """The whole point: 2 of 37 must not be publishable."""
    assert coverage_verdict(2, 37)[0] == "fail"


# --- boundaries ---------------------------------------------------------
#
# Derived from the constants, not hard-coded to today's values. These pin
# the INCLUSIVITY semantics — exactly at a floor passes it — which is what
# makes the documented percentages read the way people expect. Writing them
# as literals meant that retuning a floor broke tests that were never about
# its value; that is what happened when WARN moved 0.85 -> 0.90.

def _pct(floor: float) -> int:
    """Whole-percent numerator for a floor, e.g. 0.90 -> 90."""
    n = round(floor * 100)
    assert n / 100 == floor, (
        f"floor {floor} is not a whole percent; these boundary tests "
        f"construct their fixtures as n/100 and need adjusting"
    )
    return n


def test_exactly_at_the_warn_floor_is_ok():
    """Inclusive at the bottom of the better band."""
    assert coverage_verdict(_pct(MIN_ROSTER_COVERAGE_WARN), 100)[0] == "ok"


def test_just_below_the_warn_floor_warns():
    assert coverage_verdict(_pct(MIN_ROSTER_COVERAGE_WARN) - 1, 100)[0] == "warn"


def test_exactly_at_the_fail_floor_warns():
    assert coverage_verdict(_pct(MIN_ROSTER_COVERAGE_FAIL), 100)[0] == "warn"


def test_just_below_the_fail_floor_fails():
    assert coverage_verdict(_pct(MIN_ROSTER_COVERAGE_FAIL) - 1, 100)[0] == "fail"


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
