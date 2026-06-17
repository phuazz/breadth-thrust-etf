"""Freshness guard tests for the assert_derived_not_stale_vs_source helper.

Regression guard for the 2026-06-17 silent-staleness class: the Live Signal
chart rendered May 15 data on a June 17 page because ``ma200_sweep.json``
had not been regenerated after its source ``breadth_*.json`` panels were
refreshed. Three sibling derivations (``phase7_bootstrap``,
``phase8_right_tail``, ``portfolio_construction``) had the same bug.

These tests use a tmp_path with synthetic JSON files whose mtimes are
manipulated via ``os.utime`` to simulate fresh-source / stale-derived
configurations. We do NOT touch the real data/ files.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pipeline import assert_derived_not_stale_vs_source  # noqa: E402


DAY = 86400  # seconds


def _touch(path: Path, days_ago: float) -> None:
    """Create the file (if missing) and set its mtime to `days_ago` ago."""
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    now = time.time()
    when = now - days_ago * DAY
    os.utime(path, (when, when))


def test_fresh_derived_passes(tmp_path: Path) -> None:
    """A derivation 1 day behind its source is well within the tolerance."""
    source = tmp_path / "multi_strategy.json"
    derived = tmp_path / "phase7_bootstrap.json"
    _touch(source, days_ago=1)
    _touch(derived, days_ago=2)
    # Should not raise.
    assert_derived_not_stale_vs_source(derived, [source], max_lag_days=7)


def test_derived_at_boundary_passes(tmp_path: Path) -> None:
    """Lag of exactly max_lag_days should pass (strict > in the assertion)."""
    source = tmp_path / "multi_strategy.json"
    derived = tmp_path / "phase7_bootstrap.json"
    _touch(source, days_ago=0)
    _touch(derived, days_ago=7)
    assert_derived_not_stale_vs_source(derived, [source], max_lag_days=7)


def test_derived_just_over_threshold_fails(tmp_path: Path) -> None:
    """A derivation 8 days behind its source must raise."""
    source = tmp_path / "multi_strategy.json"
    derived = tmp_path / "ma200_sweep.json"
    _touch(source, days_ago=0)
    _touch(derived, days_ago=8)
    with pytest.raises(RuntimeError) as excinfo:
        assert_derived_not_stale_vs_source(derived, [source], max_lag_days=7)
    msg = str(excinfo.value)
    assert "ma200_sweep.json" in msg
    assert "multi_strategy.json" in msg
    assert "refresh_all.py" in msg, "error must name the fix command"


def test_derived_much_older_fails_with_clear_message(tmp_path: Path) -> None:
    """Replays the 2026-06-17 incident: derivation 25 days behind sources."""
    source = tmp_path / "breadth_csp1.json"
    derived = tmp_path / "ma200_sweep.json"
    _touch(source, days_ago=2)
    _touch(derived, days_ago=27)
    with pytest.raises(RuntimeError) as excinfo:
        assert_derived_not_stale_vs_source(derived, [source], max_lag_days=7)
    msg = str(excinfo.value)
    assert "25.0 days older" in msg or "24.9 days older" in msg \
        or "25.1 days older" in msg, f"lag not reported clearly: {msg}"


def test_missing_derived_is_ignored(tmp_path: Path) -> None:
    """A missing derived file is a separate concern (pipeline already
    warns 'no foo.json found') — this assertion should be silent."""
    source = tmp_path / "multi_strategy.json"
    _touch(source, days_ago=0)
    derived = tmp_path / "phase7_bootstrap.json"  # does not exist
    # Should not raise.
    assert_derived_not_stale_vs_source(derived, [source], max_lag_days=7)


def test_missing_source_is_ignored(tmp_path: Path) -> None:
    """If a source is missing (e.g. fresh clone before any pipeline run),
    skip the check rather than fail confusingly."""
    derived = tmp_path / "phase7_bootstrap.json"
    _touch(derived, days_ago=100)
    missing_source = tmp_path / "multi_strategy.json"  # does not exist
    # Should not raise.
    assert_derived_not_stale_vs_source(
        derived, [missing_source], max_lag_days=7,
    )


def test_uses_newest_of_multiple_sources(tmp_path: Path) -> None:
    """Several breadth panels → pick newest as the reference."""
    derived = tmp_path / "ma200_sweep.json"
    sources = [
        tmp_path / "breadth_csp1.json",
        tmp_path / "breadth_soxx.json",
        tmp_path / "breadth_cndx.json",
    ]
    _touch(sources[0], days_ago=30)   # very old
    _touch(sources[1], days_ago=20)   # old
    _touch(sources[2], days_ago=2)    # newest — the reference
    _touch(derived, days_ago=15)      # older than newest source by 13d
    with pytest.raises(RuntimeError) as excinfo:
        assert_derived_not_stale_vs_source(derived, sources, max_lag_days=7)
    assert "breadth_cndx.json" in str(excinfo.value), (
        "must name the NEWEST source, not the oldest"
    )
