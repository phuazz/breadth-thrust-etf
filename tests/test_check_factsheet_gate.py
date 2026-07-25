"""Tests for scripts/check_factsheet_gate.py and the week-final anchor.

Month- and year-boundary cases per CLAUDE.md date rules, plus the
holiday-Friday cadence rule (a Friday-holiday factsheet dated Thursday
is correct, not stale). pandas_market_calendars is the calendar
authority — expected dates below are asserted against it, with the
session walk stated in comments.

Python date months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from scripts.check_factsheet_gate import build_gate_report, read_marker_anchor
from scripts.nyse_sessions import week_final_anchor


def _utc(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# week_final_anchor — the publishable weekly anchor
# ---------------------------------------------------------------------------

def test_anchor_saturday_after_normal_week():
    # Sat 25 Jul 2026: the week Mon 20 - Fri 24 completed at Friday's
    # close -> anchor is Fri 24 Jul.
    assert week_final_anchor(_utc(2026, 7, 25)) == date(2026, 7, 24)


def test_anchor_midweek_returns_previous_week():
    # Wed 29 Jul 2026 midday UTC: last completed session is Tue 28, but
    # that week still has sessions ahead -> anchor stays Fri 24 Jul.
    assert week_final_anchor(_utc(2026, 7, 29)) == date(2026, 7, 24)


def test_anchor_friday_before_and_after_close():
    # Fri 24 Jul 12:00 UTC is before the 20:00 UTC close -> the week is
    # not complete yet, anchor is the PREVIOUS Friday 17 Jul. By 22:30
    # UTC the close has passed -> anchor flips to 24 Jul.
    assert week_final_anchor(_utc(2026, 7, 24, 12, 0)) == date(2026, 7, 17)
    assert week_final_anchor(_utc(2026, 7, 24, 22, 30)) == date(2026, 7, 24)


def test_anchor_holiday_friday_week_anchors_on_thursday():
    # Sat 4 Jul 2026: Fri 3 Jul was the Independence Day observance, so
    # the week's final session is Thu 2 Jul — the cadence rule's
    # "factsheet dated Thursday is correct" case.
    assert week_final_anchor(_utc(2026, 7, 4)) == date(2026, 7, 2)


def test_anchor_month_boundary():
    # Sat 1 Aug 2026 -> the completed week ended Fri 31 Jul; the anchor
    # crosses the July/August boundary.
    assert week_final_anchor(_utc(2026, 8, 1)) == date(2026, 7, 31)


def test_anchor_year_boundary_new_year_friday():
    # Sat 2 Jan 2027: Fri 1 Jan 2027 is the New Year's Day holiday, so
    # the completed week's final session is Thu 31 Dec 2026 — year
    # boundary and holiday-Friday in one.
    assert week_final_anchor(_utc(2027, 1, 2)) == date(2026, 12, 31)


# ---------------------------------------------------------------------------
# Gate decisions — publish mode
# ---------------------------------------------------------------------------

def _panel(tmp_path, end_iso):
    p = tmp_path / "breadth_csp1.json"
    p.write_text(json.dumps({"end_date": end_iso}), encoding="utf-8")
    return p


def _marker(tmp_path, anchor_iso):
    m = tmp_path / "factsheet_published.json"
    m.write_text(json.dumps({"anchor": anchor_iso}), encoding="utf-8")
    return m


def _no_marker(tmp_path):
    return tmp_path / "factsheet_published.json"  # never written


def test_publish_after_weekend_refresh(tmp_path):
    # The normal flow: Saturday push after refresh_all.py, panel current
    # to Fri 24 Jul, nothing published yet -> publish.
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
    )
    assert r["publish"] is True
    assert r["anchor"] == date(2026, 7, 24)


def test_publish_held_when_panel_stale(tmp_path):
    # The 2026-07-24/25 live case: panel still at 17 Jul -> hold.
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-17"), _no_marker(tmp_path),
    )
    assert r["publish"] is False
    assert "behind the anchor" in r["summary"]


def test_publish_not_repeated_same_week(tmp_path):
    # Second refresh push in the same weekend (e.g. a --skip-soxx-fetch
    # fix-up rerun) must not email the distribution list twice.
    r = build_gate_report(
        "publish", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _marker(tmp_path, "2026-07-24"),
    )
    assert r["publish"] is False
    assert "already published" in r["summary"]


def test_publish_dispatch_can_force_republish(tmp_path):
    r = build_gate_report(
        "publish", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _marker(tmp_path, "2026-07-24"),
        allow_republish=True,
    )
    assert r["publish"] is True


def test_publish_late_refresh_still_friday_anchored(tmp_path):
    # Refresh landing Tuesday SGT morning after the US Monday close:
    # panel ends Mon 27 Jul, anchor is still Fri 24 Jul (mid-week ->
    # previous completed week) -> the Friday-anchored factsheet still
    # goes out, a week late but complete.
    r = build_gate_report(
        "publish", _utc(2026, 7, 28),
        _panel(tmp_path, "2026-07-27"), _marker(tmp_path, "2026-07-17"),
    )
    assert r["anchor"] == date(2026, 7, 24)
    assert r["publish"] is True


def test_corrupt_marker_treated_as_never_published(tmp_path):
    m = tmp_path / "factsheet_published.json"
    m.write_text("{not json", encoding="utf-8")
    assert read_marker_anchor(m) is None


# ---------------------------------------------------------------------------
# Gate decisions — sunday-check mode
# ---------------------------------------------------------------------------

def test_sunday_check_quiet_when_published(tmp_path):
    r = build_gate_report(
        "sunday-check", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _marker(tmp_path, "2026-07-24"),
    )
    assert r["warn"] is False


def test_sunday_check_warns_when_refresh_missing(tmp_path):
    # Sun 26 Jul, panel still at 17 Jul: warn, and quote the Monday
    # 21:30 UTC hard-guard deadline so the operator knows the runway.
    r = build_gate_report(
        "sunday-check", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-17"), _no_marker(tmp_path),
    )
    assert r["warn"] is True
    assert "run refresh_all.py before Mon 2026-07-27 21:30 UTC" in r["summary"]


def test_sunday_check_warns_when_refreshed_but_unpublished(tmp_path):
    # Refresh landed but no email went out (failed run, mail outage):
    # a different remediation — inspect/dispatch, not refresh.
    r = build_gate_report(
        "sunday-check", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
    )
    assert r["warn"] is True
    assert "NOT published although the panel is current" in r["summary"]
