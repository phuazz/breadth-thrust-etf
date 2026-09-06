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


def _release(tmp_path, anchor_iso):
    """Operator release marker for `anchor_iso` (2026-08-08)."""
    r = tmp_path / "factsheet_release.json"
    r.write_text(json.dumps({"approved_anchor": anchor_iso}), encoding="utf-8")
    return r


def _no_release(tmp_path):
    return tmp_path / "factsheet_release.json"  # never written


def test_publish_after_weekend_refresh(tmp_path):
    # The normal flow: Saturday push after refresh_all.py, panel current
    # to Fri 24 Jul, nothing published yet -> publish.
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_release(tmp_path, "2026-07-24"),
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
        release_path=_release(tmp_path, "2026-07-24"),
    )
    assert r["publish"] is False
    assert "already published" in r["summary"]


def test_publish_dispatch_can_force_republish(tmp_path):
    r = build_gate_report(
        "publish", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _marker(tmp_path, "2026-07-24"),
        release_path=_release(tmp_path, "2026-07-24"),
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
        release_path=_release(tmp_path, "2026-07-24"),
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
        release_path=_release(tmp_path, "2026-07-24"),
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


def test_sunday_check_warns_when_refreshed_but_unreleased(tmp_path):
    # Refresh landed but nothing released it — since 2026-09-06 that means
    # the weekend run's automatic release did not fire, and the remediation
    # is to read why, not to inspect a publish run that never ran.
    r = build_gate_report(
        "sunday-check", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
    )
    assert r["warn"] is True
    assert "NOT released" in r["summary"]
    assert "auto_release.py --dry-run" in r["summary"]


def test_sunday_check_warns_when_released_but_unpublished(tmp_path):
    # Released (by the automation or by hand) but no email went out: the
    # publish run failed or never triggered — inspect/dispatch.
    r = build_gate_report(
        "sunday-check", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_release(tmp_path, "2026-07-24"),
    )
    assert r["warn"] is True
    assert "released but NOT published" in r["summary"]
    assert "push-triggered run" in r["summary"]


def test_sunday_check_reports_an_operator_hold_as_chosen_not_broken(tmp_path):
    r = build_gate_report(
        "sunday-check", _utc(2026, 7, 26),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_release(tmp_path, "2026-07-24"),
        hold_path=_hold(tmp_path, "restating sleeve D"),
    )
    assert r["warn"] is True
    assert "held by operator" in r["summary"] and "restating sleeve D" in r["summary"]
    assert "--unhold" in r["detail"]


# ---------------------------------------------------------------------------
# Release gate (2026-08-08)
# ---------------------------------------------------------------------------
# The gate could tell whether the panel was CURRENT but not whether anyone
# had CHECKED it, so every refresh landing on main emailed the distribution
# list automatically. Holding a send meant disabling the workflow by hand
# around the push. These pin the countersignature.

def test_current_panel_does_not_publish_without_a_release(tmp_path):
    """The default posture. A refresh landing on main must not email."""
    r = build_gate_report(
        "publish", datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_no_release(tmp_path),
    )
    assert r["publish"] is False
    assert "NOT released" in r["summary"]


def test_release_for_a_different_anchor_does_not_publish(tmp_path):
    """Last week's approval must not carry into this week — otherwise one
    release would authorise every future send."""
    r = build_gate_report(
        "publish", datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_release(tmp_path, "2026-07-17"),
    )
    assert r["publish"] is False


def test_unparseable_release_marker_holds_the_email(tmp_path):
    """Fails closed: the marker guards an outward send, so anything other
    than an explicit, parseable approval holds."""
    bad = tmp_path / "factsheet_release.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    r = build_gate_report(
        "publish", datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=bad,
    )
    assert r["publish"] is False


def test_dispatch_publishes_without_a_release_marker(tmp_path):
    """Dispatching the workflow IS the operator acting deliberately, so it
    does not additionally require the marker — and stays the way to force a
    corrected or trial re-send."""
    r = build_gate_report(
        "publish", datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        allow_republish=True, release_path=_no_release(tmp_path),
    )
    assert r["publish"] is True


def test_stale_panel_still_holds_even_when_released(tmp_path):
    """Releasing a week cannot substitute for the data actually being
    there — the freshness condition is independent of the approval."""
    r = build_gate_report(
        "publish", datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc),
        _panel(tmp_path, "2026-07-17"), _no_marker(tmp_path),
        release_path=_release(tmp_path, "2026-07-24"),
    )
    assert r["publish"] is False
    assert "behind the anchor" in r["summary"]


def test_pure_core_ignores_the_repository_release_marker(tmp_path):
    """No release_path means not released. If this core read the real
    docs/factsheet_release.json by default, its verdict would depend on
    working-tree state and tests would pass or fail by the calendar."""
    r = build_gate_report(
        "publish", datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
    )
    assert r["publish"] is False


# ---------------------------------------------------------------------------
# Automatic release and the operator hold (2026-09-06)
# ---------------------------------------------------------------------------
# The marker is now normally written by the weekend refresh itself
# (auto_release.py). The gate reads one shape for both paths and reports
# which it was; the hold file is the operator's veto over the automatic one.

def _auto_release(tmp_path, anchor_iso):
    r = tmp_path / "factsheet_release.json"
    r.write_text(json.dumps({"approved_anchor": anchor_iso, "auto": True,
                             "conditions": [{"check": "weekend cadence", "ok": True}]}),
                 encoding="utf-8")
    return r


def _hold(tmp_path, note):
    h = tmp_path / "factsheet_hold.json"
    h.write_text(json.dumps({"held_at_utc": "2026-07-25T08:00:00Z", "note": note}),
                 encoding="utf-8")
    return h


def test_an_automatic_release_publishes_and_is_reported_as_such(tmp_path):
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_auto_release(tmp_path, "2026-07-24"),
        hold_path=tmp_path / "factsheet_hold.json",   # absent
    )
    assert r["publish"] is True and r["auto"] is True
    assert "released automatically" in r["summary"]
    assert "(automatic)" in r["detail"]


def test_a_manual_release_is_not_reported_as_automatic(tmp_path):
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_release(tmp_path, "2026-07-24"),
    )
    assert r["publish"] is True and r["auto"] is False


def test_an_operator_hold_vetoes_a_released_week(tmp_path):
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_auto_release(tmp_path, "2026-07-24"),
        hold_path=_hold(tmp_path, "restating sleeve D"),
    )
    assert r["publish"] is False and r["hold"] is True
    assert "operator hold in place" in r["summary"]
    assert "restating sleeve D" in r["summary"]


def test_a_manual_dispatch_overrides_the_hold(tmp_path):
    """Dispatching IS the operator acting; the hold guards the automatic path."""
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_auto_release(tmp_path, "2026-07-24"),
        hold_path=_hold(tmp_path, "restating sleeve D"),
        allow_republish=True,
    )
    assert r["publish"] is True and r["auto"] is False


def test_a_malformed_hold_file_still_holds(tmp_path):
    h = tmp_path / "factsheet_hold.json"
    h.write_text("{ not json", encoding="utf-8")
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_auto_release(tmp_path, "2026-07-24"), hold_path=h,
    )
    assert r["publish"] is False and r["hold"] is True


def test_publish_hold_reason_points_at_the_automatic_release(tmp_path):
    r = build_gate_report(
        "publish", _utc(2026, 7, 25),
        _panel(tmp_path, "2026-07-24"), _no_marker(tmp_path),
        release_path=_no_release(tmp_path),
    )
    assert r["publish"] is False
    assert "automatic release did not fire" in r["summary"]
