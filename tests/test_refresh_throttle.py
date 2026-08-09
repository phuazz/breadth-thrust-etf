"""Step 1's inter-ETF throttle.

The first 38-ETF refresh lost EXV5-EXV8 in a row to a yfinance rate limit.
Nothing was damaged — compute_breadth stops before writing — but four panels
went unrefreshed and needed re-running by hand.

The throttle paces the loop so the limiter can refill. These test the two
things that would make it useless: a pause that never fires, and a pause
that fires so indiscriminately it dominates a cache-warm run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_all as ra  # noqa: E402


def _should_throttle(throttle, i, timings):
    """The predicate as written in step 1, isolated so it can be exercised
    without running a 30-minute refresh."""
    return bool(throttle and i > 1 and timings
                and timings[-1][1] >= ra.THROTTLE_SKIP_UNDER_S)


def test_default_is_on():
    """A throttle that ships disabled protects nobody."""
    assert ra.THROTTLE_DEFAULT_S > 0


def test_default_cost_stays_a_minority_of_the_run():
    """Ceiling check. Every ETF pausing would be the worst case; against a
    run of roughly 25-35 minutes that must stay well under half."""
    worst_case_s = ra.THROTTLE_DEFAULT_S * len(ra.ETFS_REFRESH)
    assert worst_case_s < 10 * 60, (
        f"throttle {ra.THROTTLE_DEFAULT_S}s x {len(ra.ETFS_REFRESH)} ETFs = "
        f"{worst_case_s / 60:.1f} min added in the worst case"
    )


def test_no_pause_before_the_first_etf():
    assert _should_throttle(15, 1, []) is False


def test_pauses_after_a_slow_step():
    """A step that took real time did real fetching, and is what the limiter
    is counting."""
    assert _should_throttle(15, 2, [("compute_breadth CSP1", 42.0)]) is True


def test_does_not_pause_after_a_step_that_did_no_fetching():
    """The skip's logic, which is sound but currently unreachable.

    Measured on the 2026-08-08 38-ETF run: the fastest compute_breadth was
    10.7s and every one of the 38 logged a yfinance download, so nothing
    falls under the threshold and skipping would have been wrong anyway.
    The branch stays as a guard for a future no-fetch path; this pins its
    behaviour if that day comes.
    """
    assert _should_throttle(15, 2, [("compute_breadth CSP1", 1.2)]) is False


def test_measured_reality_no_step_falls_under_the_skip_threshold():
    """Records the measurement so a later reader does not assume the skip
    is doing work. If compute_breadth ever gains a genuine cache-hit path
    this stops being true, and the constant becomes live."""
    assert ra.THROTTLE_SKIP_UNDER_S < 10.7, (
        "the fastest observed compute_breadth step was 10.7s; a threshold at "
        "or above that would start skipping steps that DID fetch"
    )


def test_zero_disables_it():
    assert _should_throttle(0, 5, [("compute_breadth CSP1", 90.0)]) is False


@pytest.mark.parametrize("elapsed,expected", [
    (ra.THROTTLE_SKIP_UNDER_S - 0.1, False),
    (ra.THROTTLE_SKIP_UNDER_S, True),
    (ra.THROTTLE_SKIP_UNDER_S + 0.1, True),
])
def test_skip_boundary(elapsed, expected):
    assert _should_throttle(15, 3, [("x", elapsed)]) is expected


def test_step_one_actually_calls_sleep():
    """Guard the wiring, not just the arithmetic: the constants and the
    predicate are worthless if the loop never sleeps."""
    src = (ROOT / "scripts" / "refresh_all.py").read_text(encoding="utf-8")
    step1 = src.split("# ----- Step 1", 1)[1].split("# ----- Step 2", 1)[0]
    assert "time.sleep(args.throttle)" in step1, (
        "step 1 no longer sleeps; the --throttle flag would be inert"
    )
    assert "THROTTLE_SKIP_UNDER_S" in step1, (
        "the cache-warm skip is gone; a warm refresh would pause needlessly"
    )
