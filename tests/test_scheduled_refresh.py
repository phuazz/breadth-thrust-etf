"""Tests for the pure helpers in scripts/scheduled_refresh.py.

The subprocess/git orchestration is exercised operationally (preflight
smoke run at setup, then the soak Saturdays); these tests pin the date
logic and the commit-message contract. Month- and year-boundary cases
per CLAUDE.md date rules. Python date months are 1-indexed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from scripts.scheduled_refresh import (
    panel_is_week_current,
    scheduled_commit_message,
)


def _utc(y, m, d, hh=12):
    return datetime(y, m, d, hh, 0, tzinfo=timezone.utc)


def test_current_panel_passes_on_saturday():
    # Sat 25 Jul 2026, panel at Fri 24 Jul -> publishable.
    assert panel_is_week_current(date(2026, 7, 24), _utc(2026, 7, 25)) is True


def test_thursday_panel_fails_when_friday_exists():
    # The quietly-stale case: every step green but the panel stopped at
    # Thu 23 Jul although Fri 24 Jul traded.
    assert panel_is_week_current(date(2026, 7, 23), _utc(2026, 7, 25)) is False


def test_holiday_friday_week_thursday_panel_passes():
    # Sat 4 Jul 2026: Fri 3 Jul was the Independence Day observance, so
    # a Thursday-dated panel IS the week-final anchor.
    assert panel_is_week_current(date(2026, 7, 2), _utc(2026, 7, 4)) is True


def test_month_boundary_catchup_run():
    # Machine off on Sat 1 Aug 2026; catch-up fires Mon 3 Aug before the
    # US close. Anchor is still Fri 31 Jul (the completed week), so a
    # panel at 31 Jul passes and a 24 Jul panel fails.
    assert panel_is_week_current(date(2026, 7, 31), _utc(2026, 8, 3)) is True
    assert panel_is_week_current(date(2026, 7, 24), _utc(2026, 8, 3)) is False


def test_year_boundary():
    # Sat 2 Jan 2027 after the New Year's Day holiday Friday: the
    # completed week's final session is Thu 31 Dec 2026.
    assert panel_is_week_current(date(2026, 12, 31), _utc(2027, 1, 2)) is True
    assert panel_is_week_current(date(2026, 12, 24), _utc(2027, 1, 2)) is False


def test_commit_message_contract():
    msg = scheduled_commit_message(date(2026, 8, 1), date(2026, 7, 31))
    assert msg == (
        "Local weekly refresh 2026-08-01 (scheduled): "
        "panels current to 2026-07-31, all steps OK"
    )
    # The CI factsheet workflow's push trigger fires on the panel path,
    # not the message, but the "Local weekly refresh" prefix is the
    # commit-heartbeat convention VERIFY_DASHBOARD greps for.
    assert msg.startswith("Local weekly refresh ")


# --------------------------------------------------------------------------
# panel_is_current — the guard the Friday-morning cadence actually needs.
#
# The distinction these pin down: week_final_anchor asks "has the completed
# WEEK been captured", which mid-week points at the PREVIOUS week and so goes
# blind on a Friday-morning run. last_completed_session asks "has the session
# the decision reads been captured", which is the question that matters when
# the refresh feeds a fill the same day.
# --------------------------------------------------------------------------

from scripts.scheduled_refresh import panel_is_current  # noqa: E402


def test_friday_morning_requires_thursday_not_last_week():
    """The defect that prompted the change. Fri 14 Aug 2026 08:00 SGT is
    2026-08-14 00:00 UTC; the decision that morning reads Thu 13 Aug."""
    now = _utc(2026, 8, 14, 0)
    assert panel_is_current(date(2026, 8, 13), now) is True
    # A panel still at the previous week's Friday must FAIL...
    assert panel_is_current(date(2026, 8, 7), now) is False
    # ...even though the old week-anchored guard waves it through.
    assert panel_is_week_current(date(2026, 8, 7), now) is True


def test_agrees_with_the_week_anchor_on_a_saturday():
    """On the old cadence the two are the same test, so the change cannot
    have loosened anything for a Saturday run."""
    for panel in (date(2026, 8, 7), date(2026, 8, 6), date(2026, 7, 31)):
        now = _utc(2026, 8, 8, 22)
        assert panel_is_current(panel, now) == panel_is_week_current(panel, now)


def test_holiday_friday_week_thursday_panel_is_current():
    """Fri 3 Jul 2026 was the Independence Day observance. On Sat 4 Jul the
    last completed session is Thu 2 Jul, so a Thursday panel is current."""
    assert panel_is_current(date(2026, 7, 2), _utc(2026, 7, 4)) is True


def test_month_boundary_friday_run():
    """Fri 4 Sep 2026 morning: the decision reads Thu 3 Sep, which is a
    different month from the Monday that follows. Panel at 31 Aug fails."""
    now = _utc(2026, 9, 4, 0)
    assert panel_is_current(date(2026, 9, 3), now) is True
    assert panel_is_current(date(2026, 8, 31), now) is False


def test_year_boundary_friday_run():
    """Fri 8 Jan 2027 morning reads Thu 7 Jan; a panel left at 31 Dec 2026
    is stale across the year boundary and must fail."""
    now = _utc(2027, 1, 8, 0)
    assert panel_is_current(date(2027, 1, 7), now) is True
    assert panel_is_current(date(2026, 12, 31), now) is False


# --------------------------------------------------------------------------
# log_path_for — named by LOCAL date.
#
# The defect these pin: the task is scheduled in local time, but the log was
# named from the UTC date. Under the old Saturday 06:00 SGT cadence that is
# 22:00 UTC on the Friday, so every run was filed under the previous day and
# the 8 Aug 2026 run appeared not to exist. tz is injected so these assert the
# behaviour regardless of the machine or CI runner's own zone.
# --------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from scripts.scheduled_refresh import log_path_for  # noqa: E402

SGT = timezone(timedelta(hours=8))


def test_saturday_0600_sgt_run_is_filed_under_saturday():
    """The exact case that misled: 2026-08-07T22:00Z IS Sat 8 Aug 06:00 SGT."""
    run = datetime(2026, 8, 7, 22, 0, tzinfo=timezone.utc)
    assert run.astimezone(SGT).strftime("%A") == "Saturday"
    assert log_path_for(run, SGT).name == "scheduled_refresh_2026-08-08.log"
    # ...whereas naming by UTC date produced the previous day, the old bug.
    assert log_path_for(run, timezone.utc).name == "scheduled_refresh_2026-08-07.log"


def test_new_friday_0800_sgt_cadence_files_under_friday():
    """Friday 08:00 SGT is 00:00 UTC the same day, so both conventions agree
    here — the fix matters for the catch-up window, not the happy path."""
    run = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)
    assert run.astimezone(SGT).strftime("%A") == "Friday"
    assert log_path_for(run, SGT).name == "scheduled_refresh_2026-08-14.log"


def test_catch_up_run_before_0800_sgt_still_files_locally():
    """A StartWhenAvailable catch-up at 02:00 SGT Saturday is 18:00 UTC Friday;
    it must be filed under the Saturday the operator saw it run."""
    run = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    assert log_path_for(run, SGT).name == "scheduled_refresh_2026-08-15.log"


def test_log_name_month_and_year_boundaries():
    # Month: 2026-08-31T22:00Z is 1 Sep 06:00 SGT.
    assert log_path_for(datetime(2026, 8, 31, 22, 0, tzinfo=timezone.utc),
                        SGT).name == "scheduled_refresh_2026-09-01.log"
    # Year: 2026-12-31T22:00Z is 1 Jan 2027 06:00 SGT.
    assert log_path_for(datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc),
                        SGT).name == "scheduled_refresh_2027-01-01.log"



# ---------------------------------------------------------------------------
# The two-run weekend must not silently collapse to one
#
# WS18 put the book on a Monday fill, and the vendor probe showed the European
# close settles only the day AFTER its session. So the weekend runs twice:
# Saturday for sleeves A/B/C, Sunday once sleeve D's data has settled.
#
# Soak mode never commits. Without --commit, Saturday leaves a dirty tree,
# Sunday's clean-tree preflight refuses, and the second run never happens —
# with the schedule looking healthy throughout. Six consecutive catch-up
# firings were consumed in exactly that way on 2026-08-14.
# ---------------------------------------------------------------------------

import inspect  # noqa: E402

from scripts import scheduled_refresh as _sr  # noqa: E402


def _commit_branch() -> str:
    src = inspect.getsource(_sr.main)
    start = src.index("elif args.commit")
    return src[start:src.index("else:", start)]


def test_commit_mode_exists_and_publishes_nothing():
    """--commit must not imply --push: the Saturday run publishes nothing,
    because sleeve D is knowingly incomplete at that hour."""
    assert "--commit" in inspect.getsource(_sr), "--commit flag missing"
    assert "args.commit" in inspect.getsource(_sr.main)
    body = _commit_branch()
    assert '"push"' not in body and "'push'" not in body, (
        "the commit branch pushes — it must publish nothing")


def test_commit_mode_stages_what_the_preflight_would_call_dirty():
    assert '"add", "data/", "docs/"' in _commit_branch(), (
        "commit mode must stage the same paths the preflight sees as dirty, "
        "or the next run still refuses")


def test_a_no_change_commit_is_not_a_failure():
    """A run with nothing new to write is a CLEAN run. Treating 'nothing to
    commit' as an error would fail every quiet Sunday and train the operator
    to ignore the alert."""
    assert "nothing to commit" in _commit_branch()
