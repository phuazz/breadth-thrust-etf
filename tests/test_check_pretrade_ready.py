"""Tests for scripts/check_pretrade_ready.py — the Monday pre-trade backstop.

The check exists because the Monday fill needs the instruction built BEFORE
it, and the local refresh that builds it cannot run in CI. These pin the
question it asks, which is deliberately not the one the factsheet gate asks:
"does the panel reach Friday's decision session", not "has the completed week
been published".

Month- and year-boundary cases per CLAUDE.md date rules. Python date months
are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.check_pretrade_ready import build_report


def _utc(y, m, d, hh=4):
    return datetime(y, m, d, hh, 0, tzinfo=timezone.utc)


def _panel(tmp_path, end_date: str):
    p = tmp_path / "breadth_csp1.json"
    p.write_text(json.dumps({"end_date": end_date}), encoding="utf-8")
    return p


def test_ready_when_panel_reaches_the_decision_session(tmp_path):
    """Sun 13 Sep 2026 06:00 UTC (14:00 SGT) reads Fri 11 Sep."""
    r = build_report(_panel(tmp_path, "2026-09-11"), _utc(2026, 9, 13, 6))
    assert r["status"] == "ready"
    assert r["warn"] == "false"
    assert r["tag"] == "OK"


def test_not_ready_when_the_refresh_did_not_run(tmp_path):
    """The failure this check exists for: machine off, panel still at the
    previous week's Friday while Monday's fill is hours away."""
    r = build_report(_panel(tmp_path, "2026-09-04"), _utc(2026, 9, 13, 6))
    assert r["status"] == "not_ready"
    assert r["warn"] == "true"
    assert r["tag"] == "PRE-TRADE"
    assert "2026-09-11" in r["summary"]
    # The body has to be actionable, not just an alarm.
    assert "scheduled_refresh.py" in r["detail"]
    # CLOSING auction times, not the opens or the retired Friday-fill dates.
    assert "23:30 SGT" in r["detail"] and "04:00 SGT" in r["detail"]
    assert "15:50 New York" in r["detail"]


def test_a_panel_ahead_of_the_session_is_invalid(tmp_path):
    """A future observation must not pass as current or be called stale."""
    r = build_report(_panel(tmp_path, "2026-09-14"), _utc(2026, 9, 13, 6))
    assert r["status"] == "error"
    assert r["tag"] == "DATA-ERROR"
    assert r["warn"] == "true"


def test_unreadable_panel_fails_toward_alerting(tmp_path):
    """A checker that cannot read the panel must warn, never reassure."""
    p = tmp_path / "breadth_csp1.json"
    p.write_text("{ this is not json", encoding="utf-8")
    r = build_report(p, _utc(2026, 9, 13, 6))
    assert r["warn"] == "true"
    assert r["status"] == "error"


def test_missing_panel_fails_toward_alerting(tmp_path):
    r = build_report(tmp_path / "does_not_exist.json", _utc(2026, 9, 13, 6))
    assert r["warn"] == "true"
    assert r["status"] == "error"


def test_holiday_shortened_week(tmp_path):
    """Mon 7 Sep was the Labor Day closure. Sunday afternoon reads the
    preceding Friday, so the Friday panel is ready for Tuesday's fill."""
    r = build_report(_panel(tmp_path, "2026-09-04"), _utc(2026, 9, 6, 6))
    assert r["status"] == "ready"


def test_month_boundary(tmp_path):
    """Sun 30 Aug afternoon reads Fri 28 Aug; July's final panel is stale."""
    now = _utc(2026, 8, 30, 6)
    assert build_report(_panel(tmp_path, "2026-08-28"), now)["status"] == "ready"
    assert build_report(_panel(tmp_path, "2026-07-31"), now)["status"] == "not_ready"


def test_year_boundary(tmp_path):
    """Sun 3 Jan 2027 afternoon reads Thu 31 Dec: New Year's Day is closed."""
    now = _utc(2027, 1, 3, 6)
    assert build_report(_panel(tmp_path, "2026-12-31"), now)["status"] == "ready"
    assert build_report(_panel(tmp_path, "2026-12-24"), now)["status"] == "not_ready"


def test_agrees_with_local_guard_for_non_future_observations(tmp_path):
    """Pre-trade adds future-date rejection; non-future observations agree."""
    from datetime import date

    from scripts.scheduled_refresh import panel_is_current
    now = _utc(2026, 9, 13, 6)
    for end in ("2026-09-11", "2026-09-04", "2026-09-10"):
        report_ready = build_report(_panel(tmp_path, end), now)["status"] == "ready"
        assert report_ready is panel_is_current(date.fromisoformat(end), now)


def test_a_late_sunday_run_still_reads_fridays_close(tmp_path):
    """The workflow has fired up to 11.8 hours late. Monday 01:48 SGT still
    precedes Monday's NYSE close and therefore retains Friday's anchor."""
    now = datetime(2026, 9, 13, 17, 48, tzinfo=timezone.utc)
    r = build_report(_panel(tmp_path, "2026-09-11"), now)
    assert r["status"] == "ready"


def test_workflow_is_pinned_to_the_sunday_review_slot():
    """A cadence re-timing must update this CI guard in the same commit."""
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
                / "pretrade_check.yml").read_text(encoding="utf-8")
    assert "cron: '0 6 * * 0'" in workflow
    assert "cron: '0 22 * * 0'" in workflow
    assert "cron: '0 4 * * 5'" not in workflow
    assert "Next fill" in workflow
    assert "--phase" in workflow
    assert "steps.pretrade.outputs.warn != 'false'" in workflow
    assert "last_green_run" not in workflow
