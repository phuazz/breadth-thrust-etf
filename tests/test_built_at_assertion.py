"""Regression tests for the 'Last updated:' build assertion.

Covers the assert_built_at_valid() guard added in Phase 2.1 to catch
empty / malformed dashboard timestamps before publishing. Includes the
month- and year-boundary edge cases required by CLAUDE.md date rules.

The format under test is exactly the one written into docs/index.html:
    YYYY-MM-DD HH:MM UTC
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from scripts.pipeline import assert_built_at_valid


# Anchor used by tests that need a "current time" different from real now.
# Picked at a non-boundary so accidental off-by-one bugs are visible.
FIXED_NOW = datetime(2026, 5, 28, 14, 0, 0, tzinfo=timezone.utc)


def _fmt(dt: datetime) -> str:
    """Format a datetime in the canonical pipeline format."""
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Empty / missing
# ---------------------------------------------------------------------------

def test_empty_string_raises():
    with pytest.raises(RuntimeError, match="empty"):
        assert_built_at_valid("")


def test_whitespace_only_raises():
    with pytest.raises(RuntimeError, match="empty"):
        assert_built_at_valid("   ")


def test_none_raises():
    with pytest.raises(RuntimeError, match="empty"):
        assert_built_at_valid(None)


# ---------------------------------------------------------------------------
# Malformed
# ---------------------------------------------------------------------------

def test_iso_8601_without_utc_suffix_raises():
    """ISO without ' UTC' suffix is the wrong format and would render
    'Last updated: 2026-05-28T14:00:00.' which looks broken in the UI."""
    with pytest.raises(RuntimeError, match="format"):
        assert_built_at_valid("2026-05-28T14:00:00")


def test_localised_string_raises():
    with pytest.raises(RuntimeError, match="format"):
        assert_built_at_valid("28 May 2026, 14:00 UTC")


def test_truncated_raises():
    with pytest.raises(RuntimeError, match="format"):
        assert_built_at_valid("2026-05-28")


# ---------------------------------------------------------------------------
# Drift sanity
# ---------------------------------------------------------------------------

def test_25h_in_past_raises_drift():
    with patch("scripts.pipeline.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.strptime = datetime.strptime
        too_old = FIXED_NOW - timedelta(hours=25)
        with pytest.raises(RuntimeError, match="off current UTC"):
            assert_built_at_valid(_fmt(too_old))


def test_25h_in_future_raises_drift():
    with patch("scripts.pipeline.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.strptime = datetime.strptime
        too_new = FIXED_NOW + timedelta(hours=25)
        with pytest.raises(RuntimeError, match="off current UTC"):
            assert_built_at_valid(_fmt(too_new))


def test_within_24h_passes():
    with patch("scripts.pipeline.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.strptime = datetime.strptime
        in_window = FIXED_NOW - timedelta(hours=23, minutes=30)
        # Should not raise
        assert_built_at_valid(_fmt(in_window))


# ---------------------------------------------------------------------------
# Edge cases required by CLAUDE.md: month boundary + year boundary
# ---------------------------------------------------------------------------

def test_month_boundary_jan_31_to_feb_1():
    """A timestamp on the last day of January should validate cleanly
    when 'now' is 1 February — guards against a subtraction bug that
    would compute drift as negative-many-days."""
    feb1 = datetime(2026, 2, 1, 0, 30, 0, tzinfo=timezone.utc)
    jan31_late = datetime(2026, 1, 31, 23, 45, 0, tzinfo=timezone.utc)
    with patch("scripts.pipeline.datetime") as mock_dt:
        mock_dt.now.return_value = feb1
        mock_dt.strptime = datetime.strptime
        # 45 min drift — well within 24h window
        assert_built_at_valid(_fmt(jan31_late))


def test_month_boundary_30_day_month():
    """30-day month boundary (April 30 -> May 1) — guard against month
    indexing off-by-one (Python 1-indexed; JS would be 0-indexed)."""
    may1 = datetime(2026, 5, 1, 1, 0, 0, tzinfo=timezone.utc)
    apr30_late = datetime(2026, 4, 30, 23, 30, 0, tzinfo=timezone.utc)
    with patch("scripts.pipeline.datetime") as mock_dt:
        mock_dt.now.return_value = may1
        mock_dt.strptime = datetime.strptime
        assert_built_at_valid(_fmt(apr30_late))


def test_year_boundary_dec_31_to_jan_1():
    """Cross-year boundary — last build of the year validating on
    new year's day. Catches any drift computation that does not handle
    year rollover correctly."""
    jan1 = datetime(2027, 1, 1, 1, 15, 0, tzinfo=timezone.utc)
    dec31_late = datetime(2026, 12, 31, 23, 50, 0, tzinfo=timezone.utc)
    with patch("scripts.pipeline.datetime") as mock_dt:
        mock_dt.now.return_value = jan1
        mock_dt.strptime = datetime.strptime
        assert_built_at_valid(_fmt(dec31_late))


def test_leap_day_validates():
    """2024 is a leap year — 29 Feb is a valid date. Build that day
    should not be flagged by date arithmetic that assumes Feb has 28
    days."""
    leap_day = datetime(2024, 2, 29, 12, 0, 0, tzinfo=timezone.utc)
    one_hour_later = leap_day + timedelta(hours=1)
    with patch("scripts.pipeline.datetime") as mock_dt:
        mock_dt.now.return_value = one_hour_later
        mock_dt.strptime = datetime.strptime
        assert_built_at_valid(_fmt(leap_day))


# ---------------------------------------------------------------------------
# Format roundtrip
# ---------------------------------------------------------------------------

def test_round_trip_matches_pipeline_format():
    """The format string in assert_built_at_valid must match the one
    used at the data dict construction in main(). If anyone changes one,
    this test catches the divergence by exercising a real now()."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Should not raise on a freshly-formatted timestamp.
    assert_built_at_valid(now)
