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

from datetime import date  # noqa: E402

from pipeline import (  # noqa: E402
    assert_derived_not_stale_vs_source,
    assert_source_panel_fresh_vs_today,
)


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


# =============================================================================
# Phase 28.5 P3 — assert_source_panel_fresh_vs_today
# =============================================================================
# Regression guard for the 2026-03-27 -> 2026-06-18 silent-staleness incident.
# The Phase 28 derived-vs-source check above only catches "derived forgot to
# refresh after source moved"; it cannot catch "source itself stopped
# advancing while everything downstream kept refreshing against it" — which is
# what the breadth_csp1 panel did for ~11 weeks.

import json as _json  # noqa: E402


def _write_panel(path: Path, end_date_iso: str) -> None:
    path.write_text(_json.dumps({"end_date": end_date_iso}), encoding="utf-8")


def test_source_panel_fresh_today_passes(tmp_path: Path) -> None:
    panel = tmp_path / "breadth_csp1.json"
    _write_panel(panel, "2026-06-19")  # Fri
    assert_source_panel_fresh_vs_today(
        panel, today=date(2026, 6, 19), max_lag_trading_days=5,
    )


def test_source_panel_at_budget_boundary_passes(tmp_path: Path) -> None:
    """5 trading days lag (exactly the default budget) must pass."""
    panel = tmp_path / "breadth_csp1.json"
    _write_panel(panel, "2026-06-12")  # Fri
    assert_source_panel_fresh_vs_today(
        panel, today=date(2026, 6, 19), max_lag_trading_days=5,
    )


def test_source_panel_just_over_budget_aborts(tmp_path: Path) -> None:
    panel = tmp_path / "breadth_csp1.json"
    _write_panel(panel, "2026-06-11")  # Thu
    with pytest.raises(RuntimeError) as exc:
        assert_source_panel_fresh_vs_today(
            panel, today=date(2026, 6, 19), max_lag_trading_days=5,
        )
    msg = str(exc.value)
    assert "breadth_csp1.json" in msg
    assert "6 trading days" in msg
    assert "refresh_all.py" in msg
    assert "ALLOW_STALE_REGIME" in msg


def test_source_panel_replays_2026_incident(tmp_path: Path) -> None:
    """The actual 2026-06-13 vintage — panel end_date 2026-05-29, run date
    2026-06-13 (Sat). Phase 28 derived-vs-source checks all PASSED that
    day because ma200_sweep mtime was newer than breadth_csp1 mtime. This
    new gate would have caught the silent failure."""
    panel = tmp_path / "breadth_csp1.json"
    _write_panel(panel, "2026-05-29")
    with pytest.raises(RuntimeError) as exc:
        assert_source_panel_fresh_vs_today(
            panel, today=date(2026, 6, 13), max_lag_trading_days=5,
        )
    msg = str(exc.value)
    assert "2026-05-29" in msg
    assert "11 trading days" in msg


def test_source_panel_missing_file_is_silent(tmp_path: Path) -> None:
    """A fresh clone before any pipeline run will not have the panel.
    Missing-source warnings are handled elsewhere; this gate is silent."""
    assert_source_panel_fresh_vs_today(
        tmp_path / "breadth_csp1.json",
        today=date(2026, 6, 19),
        max_lag_trading_days=5,
    )


def test_source_panel_missing_end_date_aborts(tmp_path: Path) -> None:
    """A panel JSON that exists but lacks an end_date field is a broken
    write — we want this loud, not silent."""
    panel = tmp_path / "breadth_csp1.json"
    panel.write_text(_json.dumps({"foo": "bar"}), encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        assert_source_panel_fresh_vs_today(
            panel, today=date(2026, 6, 19), max_lag_trading_days=5,
        )
    assert "no end_date" in str(exc.value)


def test_source_panel_future_end_date_passes(tmp_path: Path) -> None:
    """Clock skew defensive — if end_date somehow exceeds today, lag is 0
    rather than negative."""
    panel = tmp_path / "breadth_csp1.json"
    _write_panel(panel, "2026-06-25")
    assert_source_panel_fresh_vs_today(
        panel, today=date(2026, 6, 19), max_lag_trading_days=5,
    )
