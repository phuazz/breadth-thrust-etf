"""Tests for the pure helpers in scripts/scheduled_refresh.py.

The subprocess/git orchestration is exercised operationally (preflight
smoke run at setup, then the soak Saturdays); these tests pin the date
logic and the commit-message contract. Month- and year-boundary cases
per CLAUDE.md date rules. Python date months are 1-indexed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import inspect
import re

import pytest

from scripts import scheduled_refresh
from scripts.scheduled_refresh import (
    CADENCES,
    RELEASE,
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
    # Default must stay the weekend pair: the post-fill cadence was added
    # later and must never capture a caller that did not ask for it.
    assert msg == scheduled_commit_message(date(2026, 8, 1), date(2026, 7, 31),
                                           "weekend")


def test_post_fill_commit_message_contract():
    """The post-fill pair carries its OWN prefix.

    fleet_watch greps the two apart. Sharing a prefix would let the weekend
    commit keep the heartbeat fresh while the post-fill pair silently stopped
    running — the blind spot the row exists to close.
    """
    msg = scheduled_commit_message(date(2026, 8, 25), date(2026, 8, 24),
                                   "post-fill")
    assert msg == (
        "Local post-fill refresh 2026-08-25 (scheduled): "
        "panels current to 2026-08-24, all steps OK"
    )
    assert msg.startswith("Local post-fill refresh ")
    assert "weekly" not in msg


def test_the_two_cadences_never_collide():
    """Same day, same panel — the messages must still be distinguishable."""
    today, panel = date(2026, 8, 25), date(2026, 8, 24)
    msgs = {c: scheduled_commit_message(today, panel, c) for c in CADENCES}
    assert len(set(msgs.values())) == len(CADENCES)
    # The weekend grep must not match a post-fill commit, in either direction.
    assert not msgs["post-fill"].startswith("Local weekly refresh ")
    assert not msgs["weekend"].startswith("Local post-fill refresh ")


def test_gate_preview_passes_the_release_marker():
    """The preview must be able to say PUBLISH, not only HOLD.

    build_gate_report treats a missing release_path as NOT RELEASED by
    construction, so calling it without one made the preview report HOLD
    unconditionally — including in the single case that matters, where CI
    would actually send. It looked like a passing guard for as long as
    nobody checked it against a release marker that named the anchor.
    """
    src = inspect.getsource(scheduled_refresh.main)
    call = re.search(r"build_gate_report\((?:[^()]|\([^()]*\))*\)", src)
    assert call, "gate preview call not found — did main() get restructured?"
    assert "release_path" in call.group(0), (
        "the gate preview must pass release_path, or it reports HOLD whatever "
        "the true state is"
    )
    assert RELEASE.name == "factsheet_release.json"


def test_unknown_cadence_is_refused():
    """Fail loudly rather than mint an unwatched commit prefix."""
    with pytest.raises(ValueError):
        scheduled_commit_message(date(2026, 8, 25), date(2026, 8, 24), "monday")


def test_post_fill_message_at_month_and_year_boundaries():
    """Per CLAUDE.md: one month boundary, one year boundary. 1-indexed months.

    Tue 1 Sep 2026 records the Mon 31 Aug fill (month boundary); Tue 5 Jan
    2027 records the Mon 4 Jan fill against a panel still in 2026 (year
    boundary).
    """
    assert scheduled_commit_message(date(2026, 9, 1), date(2026, 8, 31),
                                    "post-fill") == (
        "Local post-fill refresh 2026-09-01 (scheduled): "
        "panels current to 2026-08-31, all steps OK"
    )
    assert scheduled_commit_message(date(2027, 1, 5), date(2026, 12, 31),
                                    "post-fill") == (
        "Local post-fill refresh 2027-01-05 (scheduled): "
        "panels current to 2026-12-31, all steps OK"
    )


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
    """THE DEADLOCK THIS PREVENTS, and did not (2026-09-01).

    The preflight refuses to start on ANY dirty path. So every path the
    refresh writes must be staged, or one armed run leaves the tree dirty and
    every subsequent run exits 2 — a deadlock that tightens rather than
    self-clears, because nothing ever cleans up.

    This test was written for exactly that and pinned an INCOMPLETE list.
    `build/portfolio.html` is written by build_simple_page in step 6 and was
    staged by neither add list, so from the 2026-08-26 arming onward every run
    failed preflight. It ran unnoticed for a fortnight: the task's own alert
    could not send (no GMAIL_* in the automation environment) and only the
    Saturday fleet watch caught it. `daily_live_track.yml` had the identical
    defect fixed on 2026-08-16; this file never got the same fix.

    Assert the PROPERTY, not one literal string: every written path is staged.
    """
    body = _commit_branch()
    # template.html joined the list on 2026-09-02, when pipeline began
    # rewriting its prose fallbacks from the data. It was omitted at first and
    # reintroduced the deadlock this test exists for, within hours of the
    # build/portfolio.html fix -- the same mistake, a fresh file. Any NEW
    # output path must be added here and to the add lists together.
    for path in ("data/", "docs/", "build/portfolio.html", "template.html"):
        assert f'"{path}"' in body, (
            f"{path} is written by the refresh but not staged — the preflight "
            f"will see it as dirty and the next run will refuse")


def test_a_no_change_commit_is_not_a_failure():
    """A run with nothing new to write is a CLEAN run. Treating 'nothing to
    commit' as an error would fail every quiet Sunday and train the operator
    to ignore the alert."""
    assert "nothing to commit" in _commit_branch()


def test_post_fill_narrows_the_panel_set_but_never_skips_it():
    """CADENCE DECIDES SCOPE (2026-09-02).

    A Monday fill ranks on the FRIDAY close, which the committed panels
    already carry, so a post-fill run has nothing to gain from re-fetching 38
    rosters -- and step 1 is where the entire cost and the entire vendor
    exposure sit. On 2026-09-01 a post-fill run spent 13.3 hours inside one
    compute_breadth once the rate limiter throttled it, held the automation
    clone dirty across two scheduled fires, and never reached the engines it
    existed to re-anchor. The book sat on the 2026-08-24 rebalance for two
    days because of it.

    The weekend cadence must KEEP the full run: that is the one that rebuilds
    rosters and panels, and quietly narrowing it would freeze the breadth
    record while every guard stayed green.
    """
    src = inspect.getsource(_sr.main)
    assert '"--deployed-only"' in src, "post-fill must narrow step 1's scope"
    assert 'args.cadence == "post-fill"' in src, (
        "the narrowing must be tied to the cadence, not unconditional")
    # The weekend cadence must not acquire it by accident: it is the run that
    # rebuilds the candidate panels, and narrowing it would freeze them while
    # every guard stayed green.
    guarded = src.split('args.cadence == "post-fill"')[1][:200]
    assert "--deployed-only" in guarded, (
        "--deployed-only must sit inside the post-fill branch")
    # And it must NOT be a panel skip. Skipping the panels lets the engines
    # advance past them; build_simple_page refused exactly that on 2026-09-02
    # ("freshness says sleeve B reaches 2026-09-01, past the newest data this
    # refresh produced"). Sleeve A ranks on the panels, so a re-anchor that
    # omits them is incoherent by construction.
    # Check the CALL, not any mention: the re-exec note below quotes the old
    # flag by name when explaining the 2026-09-02 version skew, and a bare
    # substring ban would fail on the history rather than on the behaviour.
    assert 'append("--skip-panels")' not in src, (
        "post-fill must NARROW the panel set, never skip it")


def test_a_pull_that_rewrites_this_script_re_execs_once():
    """VERSION SKEW INSIDE ONE PROCESS (2026-09-02).

    The preflight pull updates the clone this script is RUNNING FROM. A commit
    touching both this file and something it invokes therefore leaves the
    process holding the old half: on 2026-09-02 the 09:00 run executed the
    previous scheduled_refresh against the freshly pulled refresh_all and died
    on "unrecognized arguments: --skip-panels", a flag renamed in the very
    commit the pull had just applied. Neither version was wrong; they were a
    commit apart inside one interpreter.

    Re-exec rather than abort, so a run still happens on the schedule it was
    given -- and guarded by an environment marker, because a file that keeps
    changing must not spin.
    """
    src = inspect.getsource(_sr.main)
    assert "os.execv" in src, "a self-rewriting pull must re-exec"
    assert "BTE_SCHED_REEXEC" in src, (
        "the re-exec must be guarded against looping")
    # The comparison has to straddle the pull: captured before, checked after.
    before = src.index("_self_before")
    pull = src.index('"pull", "--rebase"')
    exec_at = src.index("os.execv")
    assert before < pull < exec_at, (
        "the script's own contents must be captured BEFORE the pull and "
        "compared AFTER it, or the skew is invisible")


def test_the_push_retries_after_rebasing():
    """A 40-minute run races every other writer in the repo (2026-09-02).

    Three probes a day, the scanner, the daily live track and whoever is at
    the keyboard all push to the same ref, so origin moves UNDER a healthy run
    as a matter of course and the first push comes back non-fast-forward
    through no fault of the refresh. On 2026-09-02 that lost a complete,
    correct, fully-guarded post-fill run at the final step — the commit sat in
    the automation clone until it was rebased by hand.

    A run that did everything right must not need a human for the last thirty
    seconds. Same shape the workflows already use.
    """
    src = inspect.getsource(_sr.main)
    push_at = src.index('"push", "origin", "main"')
    tail = src[push_at:]
    assert "--autostash" in tail, (
        "the retry must rebase onto origin, and --autostash because the build "
        "may have left tracked outputs dirty")
    assert "attempt" in tail, "the push must retry, not fail on the first race"
    # ...and it must still give up rather than loop for ever: a push that
    # cannot land after three rebases is not a race, it is something else.
    assert "3 attempts" in tail or "(1, 2, 3)" in tail, (
        "the retry must be bounded")
