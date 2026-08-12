"""Tests for scripts/check_pretrade_ready.py — the pre-trade backstop.

The check exists because the Friday fill needs the instruction built BEFORE
it, and the local refresh that builds it cannot run in CI. These pin the
question it asks, which is deliberately not the one the factsheet gate asks:
"does the panel reach the session today's decision reads", not "has the
completed week been published".

Month- and year-boundary cases per CLAUDE.md date rules. Python date months
are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.check_pretrade_ready import build_report


def _utc(y, m, d, hh=4):
    return datetime(y, m, d, hh, 0, tzinfo=timezone.utc)


def _panel(tmp_path, end_date: str):
    p = tmp_path / "breadth_csp1.json"
    p.write_text(json.dumps({"end_date": end_date}), encoding="utf-8")
    return p


def test_ready_when_panel_reaches_the_decision_session(tmp_path):
    """Fri 14 Aug 2026 04:00 UTC (12:00 SGT). The decision reads Thu 13 Aug."""
    r = build_report(_panel(tmp_path, "2026-08-13"), _utc(2026, 8, 14))
    assert r["status"] == "ready"
    assert r["warn"] == "false"
    assert r["tag"] == "OK"


def test_not_ready_when_the_refresh_did_not_run(tmp_path):
    """The failure this check exists for: machine off, panel still at the
    previous week's Friday while the fill is hours away."""
    r = build_report(_panel(tmp_path, "2026-08-07"), _utc(2026, 8, 14))
    assert r["status"] == "not_ready"
    assert r["warn"] == "true"
    assert r["tag"] == "PRE-TRADE"
    assert "2026-08-13" in r["summary"]
    # The body has to be actionable, not just an alarm.
    assert "scheduled_refresh.py" in r["detail"]
    assert "15:00 SGT" in r["detail"] and "21:30 SGT" in r["detail"]


def test_a_panel_ahead_of_the_session_is_ready(tmp_path):
    """Defensive: a panel dated later than the last completed session (an
    early or manual run) must not be reported as stale."""
    r = build_report(_panel(tmp_path, "2026-08-14"), _utc(2026, 8, 14))
    assert r["status"] == "ready"


def test_unreadable_panel_fails_toward_alerting(tmp_path):
    """A checker that cannot read the panel must warn, never reassure."""
    p = tmp_path / "breadth_csp1.json"
    p.write_text("{ this is not json", encoding="utf-8")
    r = build_report(p, _utc(2026, 8, 14))
    assert r["warn"] == "true"
    assert r["status"] == "error"


def test_missing_panel_fails_toward_alerting(tmp_path):
    r = build_report(tmp_path / "does_not_exist.json", _utc(2026, 8, 14))
    assert r["warn"] == "true"
    assert r["status"] == "error"


def test_holiday_shortened_week(tmp_path):
    """Fri 3 Jul 2026 was the Independence Day observance. Running that
    morning, the last completed session is Wed 1 Jul, so a Wednesday panel
    is ready even though no Thursday exists to reach."""
    r = build_report(_panel(tmp_path, "2026-07-01"), _utc(2026, 7, 2, 4))
    assert r["status"] == "ready"


def test_month_boundary(tmp_path):
    """Fri 4 Sep 2026 morning reads Thu 3 Sep; a panel left at 31 Aug is in
    the previous month and must fail."""
    now = _utc(2026, 9, 4)
    assert build_report(_panel(tmp_path, "2026-09-03"), now)["status"] == "ready"
    assert build_report(_panel(tmp_path, "2026-08-31"), now)["status"] == "not_ready"


def test_year_boundary(tmp_path):
    """Fri 8 Jan 2027 morning reads Thu 7 Jan; a panel at 31 Dec 2026 is
    stale across the year boundary."""
    now = _utc(2027, 1, 8)
    assert build_report(_panel(tmp_path, "2027-01-07"), now)["status"] == "ready"
    assert build_report(_panel(tmp_path, "2026-12-31"), now)["status"] == "not_ready"


def test_shares_one_definition_with_the_local_guard(tmp_path):
    """The whole point of importing panel_is_current rather than restating
    it: the CI backstop and the local push guard must never disagree about
    what 'ready' means."""
    from datetime import date

    from scripts.scheduled_refresh import panel_is_current
    now = _utc(2026, 8, 14)
    for end in ("2026-08-13", "2026-08-07", "2026-08-12"):
        report_ready = build_report(_panel(tmp_path, end), now)["status"] == "ready"
        assert report_ready is panel_is_current(date.fromisoformat(end), now)
