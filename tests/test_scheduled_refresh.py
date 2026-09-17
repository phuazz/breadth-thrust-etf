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
from scripts import publication_debt as _pd  # noqa: E402
from scripts import run_status  # noqa: E402


def _commit_branch() -> str:
    src = inspect.getsource(_sr.main)
    # The component launcher also passes --commit to a child. Inspect the
    # actual output-commit branch, not the earlier argument assembly.
    start = src.rindex("elif args.commit")
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
    assert "BTE_SCHED_REEXEC" in src, (
        "the re-run must be guarded against looping")
    # WAITED, NOT EXEC'D (2026-09-03). os.execv is spawn-and-exit on Windows:
    # probed, the caller saw exit 0 after one second while the child kept
    # running and later exited 7. Under Task Scheduler that recorded the
    # firing as a success within seconds, left the real run outside the
    # eight-hour limit and the single-instance guard, and let every hourly
    # repeat start a fresh instance against the tree the detached run was
    # writing. The re-run must be a child the wrapper WAITS for, whose exit
    # code it returns.
    # The CALL, not the name: the wrapper's own note explains why it left.
    assert "os.execv(" not in src, (
        "os.execv does not replace the process on Windows — run a waited child")
    rerun = src.index("str(Path(__file__).resolve())")
    assert "returncode" in src[rerun:rerun + 400], (
        "the wrapper must return the re-run child's exit code")
    # The comparison has to straddle the pull: captured before, checked after.
    before = src.index("_self_before")
    pull = src.index('"pull", "--rebase"')
    assert before < pull < rerun, (
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


# ---------------------------------------------------------------------------
# One green run per local day per cadence (2026-09-03)
#
# THE SUNDAY SHORT-CIRCUIT. The early exit used to ask "does the S&P panel
# already reach the last completed session". After a green Saturday it does,
# so Sunday exited at once — and Sunday is the run that exists because sleeve
# D's European close settles only the day after its session. Nobody had seen
# it because the scheduled run had never yet succeeded on its own. The test
# is now "did a green run of THIS cadence already complete on THIS local
# date", recorded by the run itself. tz is injected, as for log_path_for.
# ---------------------------------------------------------------------------

import io  # noqa: E402
import subprocess  # noqa: E402

from scripts.scheduled_refresh import (  # noqa: E402
    RESTORE_ON_EXIT_CODES,
    already_ran_today,
    record_green_run,
    restore_tracked_outputs,
)


def test_no_marker_means_run(tmp_path):
    assert already_ran_today(tmp_path / "missing.json", "weekend",
                             _utc(2026, 9, 5, 1), SGT) is False


def test_a_green_run_today_stops_the_hourly_retries(tmp_path):
    marker = tmp_path / "last_green_run.json"
    started = datetime(2026, 9, 5, 1, 5, tzinfo=timezone.utc)   # Sat 09:05 SGT
    record_green_run(marker, "weekend", started, SGT)
    retry = datetime(2026, 9, 5, 2, 0, tzinfo=timezone.utc)     # Sat 10:00 SGT
    assert already_ran_today(marker, "weekend", retry, SGT) is True


def test_sunday_runs_even_though_saturday_left_the_panel_current(tmp_path):
    marker = tmp_path / "last_green_run.json"
    record_green_run(marker, "weekend",
                     datetime(2026, 9, 5, 1, 5, tzinfo=timezone.utc), SGT)
    sunday = datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc)    # Sun 09:00 SGT
    assert already_ran_today(marker, "weekend", sunday, SGT) is False


def test_the_two_cadences_do_not_share_a_marker(tmp_path):
    marker = tmp_path / "last_green_run.json"
    when = datetime(2026, 9, 8, 1, 5, tzinfo=timezone.utc)      # Tue 09:05 SGT
    record_green_run(marker, "post-fill", when, SGT)
    assert already_ran_today(marker, "post-fill", when, SGT) is True
    assert already_ran_today(marker, "weekend", when, SGT) is False


def test_marker_month_and_year_boundaries(tmp_path):
    """Per CLAUDE.md: one month boundary, one year boundary; the marker is
    keyed on the LOCAL date, so 22:30Z is already the next day in SGT."""
    marker = tmp_path / "last_green_run.json"
    # Month: a run at 2026-08-31 22:30Z is 1 Sep 06:30 SGT.
    record_green_run(marker, "weekend",
                     datetime(2026, 8, 31, 22, 30, tzinfo=timezone.utc), SGT)
    assert already_ran_today(marker, "weekend",
                             datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc), SGT)
    assert not already_ran_today(marker, "weekend",
                                 datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc), SGT)
    # ...and read in UTC the same marker names a different day: the zone is
    # part of the contract, exactly as it is for the log file name.
    assert not already_ran_today(marker, "weekend",
                                 datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc),
                                 timezone.utc)
    # Year: 2026-12-31 22:30Z is 1 Jan 2027 06:30 SGT.
    record_green_run(marker, "weekend",
                     datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc), SGT)
    assert already_ran_today(marker, "weekend",
                             datetime(2027, 1, 1, 2, 0, tzinfo=timezone.utc), SGT)
    assert not already_ran_today(marker, "weekend",
                                 datetime(2027, 1, 2, 1, 0, tzinfo=timezone.utc), SGT)


def test_a_corrupt_marker_means_run(tmp_path):
    marker = tmp_path / "last_green_run.json"
    marker.write_text("{not json", encoding="utf-8")
    assert already_ran_today(marker, "weekend", _utc(2026, 9, 5, 1), SGT) is False


def test_main_keys_the_early_exit_on_the_marker_not_the_panel():
    src = inspect.getsource(_sr.main)
    assert "already_ran_today(" in src
    assert "record_green_run(" in src
    assert "ALREADY CURRENT" not in src, (
        "the panel-current early exit is what swallowed the Sunday run")
    # Written on the green paths only: never before the refresh, never on a
    # preflight-only run.
    assert src.index("record_green_run(") > src.index('"push", "origin", "main"')


# ---------------------------------------------------------------------------
# A failed refresh restores the clone (2026-09-03)
#
# Otherwise the clean-tree preflight refuses every later firing until a person
# resets the clone — sixteen firings were consumed that way over 2026-08-29 to
# 09-01, and the hourly repetition was worth nothing.
# ---------------------------------------------------------------------------

def _git_repo(tmp_path, monkeypatch):
    """A throwaway clone with tracked outputs, an ignored cache and a base
    commit. Isolated git config, with core.longpaths for the Windows tmp
    path (see the 2026-09-02 note in test_scheduled_refresh_push)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t.test\n"
                   "[core]\n\tlongpaths = true\n\tautocrlf = false\n",
                   encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "clone"
    (repo / "data").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "build").mkdir()
    (repo / ".gitignore").write_text("data/*.parquet\nlogs/\n", encoding="utf-8")
    (repo / "data" / "panel.json").write_text('{"end_date": "2026-08-28"}',
                                              encoding="utf-8")
    (repo / "docs" / "index.html").write_text("old", encoding="utf-8")
    (repo / "build" / "portfolio.html").write_text("old", encoding="utf-8")
    (repo / "template.html").write_text("old", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, check=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return repo


def test_a_failed_refresh_restores_the_clone(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)
    # What a run that died at VERIFY leaves behind.
    (repo / "data" / "panel.json").write_text('{"end_date": "2026-09-04"}',
                                              encoding="utf-8")
    (repo / "docs" / "index.html").write_text("new", encoding="utf-8")
    (repo / "build" / "portfolio.html").write_text("new", encoding="utf-8")
    (repo / "template.html").write_text("new", encoding="utf-8")
    (repo / "data" / "new_output.json").write_text("{}", encoding="utf-8")
    (repo / "data" / "prices_cache.parquet").write_bytes(b"cache")   # ignored
    log = io.StringIO()
    assert restore_tracked_outputs(log, repo) is True
    assert (repo / "data" / "panel.json").read_text(encoding="utf-8") == \
        '{"end_date": "2026-08-28"}'
    assert (repo / "docs" / "index.html").read_text(encoding="utf-8") == "old"
    assert (repo / "build" / "portfolio.html").read_text(encoding="utf-8") == "old"
    assert (repo / "template.html").read_text(encoding="utf-8") == "old"
    assert not (repo / "data" / "new_output.json").exists()
    # The gitignored cache survives: -fd without -x.
    assert (repo / "data" / "prices_cache.parquet").exists()
    porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                               capture_output=True, text=True).stdout
    assert porcelain.strip() == ""
    assert "discarding the failed run's outputs" in log.getvalue()
    assert "panel.json" in log.getvalue(), "what was discarded must be logged"


def test_restore_on_a_clean_tree_is_a_no_op(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)
    log = io.StringIO()
    assert restore_tracked_outputs(log, repo) is True
    assert "already clean" in log.getvalue()


def test_restore_is_wired_to_the_in_run_failures_only():
    """Exit 2 found a tree a person dirtied and must leave it alone; exit 5
    has already committed. Only a failure INSIDE the refresh restores."""
    assert RESTORE_ON_EXIT_CODES == (3, 4)
    src = inspect.getsource(_sr.main)
    assert "restore_tracked_outputs(log)" in src
    fail_body = src[src.index("def fail("):src.index("# ----- Preflight")]
    assert "RESTORE_ON_EXIT_CODES" in fail_body


# ---------------------------------------------------------------------------
# Publication debt, wired into the run (2026-09-16)
#
# tests/test_publication_debt.py pins the verdict. These pin the WIRING,
# which is where the original defect lived: the debt was evaluated after the
# green-run marker had already returned.
# ---------------------------------------------------------------------------
import json as _json                                        # noqa: E402
from types import SimpleNamespace as _NS                     # noqa: E402

import publication_debt as _pd                               # noqa: E402


def _report(**kw):
    base = dict(owed=False, unknown=False, escalate=False, cadence="post-fill",
                asof="2026-09-16", oldest_owed_fill=None, age_days=None,
                grace_days=3, obligations=(), problems=(), evidence={},
                reason="fixture", notices=())
    base.update(kw)
    return _pd.DebtReport(**base)


def _local_today() -> str:
    """The local date main() will compute, so a marker fixture matches it."""
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def _drive(monkeypatch, tmp_path, *, debt, argv, marker_day=None,
           debt_reruns=0, child_rc=9, green_on_success=False,
           reset_marker=True, email_sink=None, email_outcome=None):
    """Drive main() through the gate and report what it decided.

    The refresh body itself is stubbed - ``subprocess.run`` returns
    ``child_rc`` - because what is under test is the SEQUENCE: debt, marker,
    daily budget, and the marker write that follows a green completion.
    ``green_on_success`` calls the real ``record_green_run`` afterwards, so a
    multi-firing test exercises the same marker the production path writes
    rather than a fixture standing in for it.
    """
    monkeypatch.setattr(_sr, "LOG_DIR", tmp_path)
    monkeypatch.setattr(_sr, "GREEN_MARKER", tmp_path / "last_green_run.json")
    monkeypatch.setattr(_sr, "log_path_for", lambda now: tmp_path / "run.log")
    monkeypatch.setattr(_sr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_sr, "price_source_preflight",
                        lambda source: (True, "fixture"))
    # ``email_sink`` collects (subject, body) instead of dropping the mail,
    # for the tests whose subject IS the mail. Nothing is ever sent.
    #
    # The stub returns an OUTCOME, because the real ``_email`` does
    # (2026-09-17, eighth review) and the notice path branches on it.
    # ``email_outcome`` makes an unconfigured or refused channel testable;
    # the default is the sent case, which is what most callers assume.
    outcome = email_outcome or run_status.SENT
    monkeypatch.setattr(_sr, "_email",
                        (lambda subject, body, log:
                         (email_sink.append((subject, body)), outcome)[1])
                        if email_sink is not None
                        else (lambda *a, **kw: outcome))
    monkeypatch.setattr(_sr, "_git", lambda args, log, **kw: _NS(
        returncode=0, stdout="", stderr=""))
    marker_path = tmp_path / "last_green_run.json"
    if marker_day is not None and reset_marker:
        rec = {"cadence": "post-fill", "local_date": marker_day,
               "started_utc": "2026-09-16T01:00:00+00:00"}
        if debt_reruns:
            rec.update(debt_attempt_date=marker_day, debt_attempts=debt_reruns)
        marker_path.write_text(_json.dumps(rec), encoding="utf-8")
    if isinstance(debt, BaseException):
        def _debt(*a, **kw):
            raise debt
    else:
        def _debt(*a, **kw):
            return debt
    monkeypatch.setattr(_sr.publication_debt, "current_debt", _debt)
    monkeypatch.setattr(_sr.publication_debt, "escalation_due",
                        lambda rep, on: ["NYSE|2026-09-14"])
    monkeypatch.setattr(_sr.publication_debt, "mark_escalated",
                        lambda *a, **kw: True)
    ran = []
    monkeypatch.setattr(_sr.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or _NS(
                            returncode=child_rc))
    rc = _sr.main(argv)
    if green_on_success and ran:
        # The production path: a run that completes green writes the marker.
        _sr.record_green_run(marker_path, "post-fill",
                             datetime.now(timezone.utc))
    return rc, ran, (tmp_path / "run.log").read_text(encoding="utf-8")


def test_catch_up_with_nothing_owed_exits_without_running(monkeypatch, tmp_path):
    rc, ran, log = _drive(monkeypatch, tmp_path, debt=_report(owed=False),
                          argv=["--cadence", "post-fill", "--catch-up"])
    assert rc == 0
    assert ran == []
    assert "NOTHING OWED" in log


def test_catch_up_with_something_owed_runs(monkeypatch, tmp_path):
    rc, ran, log = _drive(monkeypatch, tmp_path,
                          debt=_report(owed=True, oldest_owed_fill="2026-09-14",
                                       age_days=2),
                          argv=["--cadence", "post-fill", "--catch-up"])
    assert ran, "a catch-up that owed a publication did not run the refresh"


def test_catch_up_with_an_unknown_verdict_runs(monkeypatch, tmp_path):
    """Unknown must not be spelled "nothing owed". The defect being repaired
    is an unreadable state reading green."""
    rc, ran, log = _drive(monkeypatch, tmp_path,
                          debt=_report(owed=False, unknown=True,
                                       reason="evidence unreadable"),
                          argv=["--cadence", "post-fill", "--catch-up"])
    assert ran, "an unknown debt verdict suppressed the run"


def test_a_debt_evaluation_that_raises_does_not_suppress_the_run(
        monkeypatch, tmp_path):
    rc, ran, log = _drive(monkeypatch, tmp_path,
                          debt=RuntimeError("git exploded"),
                          argv=["--cadence", "post-fill", "--catch-up"])
    assert ran, "a broken debt check suppressed the refresh"
    assert "UNREADABLE" in log
    assert "git exploded" in log


def test_the_green_marker_still_suppresses_an_hourly_retry(monkeypatch, tmp_path):
    rc, ran, log = _drive(monkeypatch, tmp_path, debt=_report(owed=False),
                          argv=["--cadence", "post-fill"],
                          marker_day=_local_today())
    assert rc == 0 and ran == []
    assert "ALREADY RAN TODAY" in log


def test_outstanding_debt_overrides_a_green_marker_once(monkeypatch, tmp_path):
    """DEFECT (predecessor), in its wiring form. On 2026-09-13 a green run
    left Monday's fill unpublished and the marker suppressed everything
    behind it. Reproduced in session on 2026-09-16 against the uncommitted
    draft; the modules are absent at 47d1b3a^, so this cannot be shown as a
    behavioural failure against a commit."""
    rc, ran, log = _drive(monkeypatch, tmp_path,
                          debt=_report(owed=True, oldest_owed_fill="2026-09-14",
                                       age_days=2),
                          argv=["--cadence", "post-fill"],
                          marker_day=_local_today())
    assert ran, "the marker suppressed a run that still owed a publication"
    assert "attempt 1/" in log
    marker = _json.loads((tmp_path / "last_green_run.json").read_text(
        encoding="utf-8"))
    assert marker["debt_attempts"] == 1


def test_the_debt_override_is_bounded_per_day(monkeypatch, tmp_path):
    """A vendor mid-retraction is not cleared by retrying, and an unbounded
    override would re-run a four-hour refresh every hour until the window
    closed."""
    rc, ran, log = _drive(monkeypatch, tmp_path,
                          debt=_report(owed=True, oldest_owed_fill="2026-09-14",
                                       age_days=2),
                          argv=["--cadence", "post-fill"],
                          marker_day=_local_today(),
                          debt_reruns=_sr.MAX_DEBT_ATTEMPTS_PER_DAY)
    assert rc == 0 and ran == []
    assert "DEBT ATTEMPT BUDGET SPENT" in log
    assert "STILL OWED: fill 2026-09-14" in log, \
        "the outstanding obligation must still be named when the budget stops"


def test_the_debt_is_evaluated_before_the_marker_is_consulted():
    src = inspect.getsource(_sr.main)
    assert src.index("Publication debt (2026-09-16)") < \
        src.index("already_ran_today(marker"), \
        "the debt must be read BEFORE the green-run marker short-circuits"


def test_the_budget_survives_a_green_completion_over_a_whole_day(
        monkeypatch, tmp_path):
    """REPRODUCED 2026-09-16 against 47d1b3a, driven through main().

    ``record_green_run`` replaced the marker wholesale, which zeroed the
    debt counter. A green run that left its debt standing therefore refunded
    the attempt it had just spent, and the bounded daily override was not
    bounded: every firing of the day ran the full refresh again.

    Here the refresh SUCCEEDS each time and the debt stays owed - the
    2026-09-13 shape. The budget must still run out.
    """
    today = _local_today()
    owed = _report(owed=True, oldest_owed_fill="2026-09-14", age_days=2)
    started = []
    for _ in range(4):
        rc, ran, log = _drive(monkeypatch, tmp_path, debt=owed,
                              argv=["--cadence", "post-fill", "--catch-up"],
                              green_on_success=True, reset_marker=False)
        started.append(bool(ran))
    assert started[:_sr.MAX_DEBT_ATTEMPTS_PER_DAY] == \
        [True] * _sr.MAX_DEBT_ATTEMPTS_PER_DAY
    assert not any(started[_sr.MAX_DEBT_ATTEMPTS_PER_DAY:]), \
        "a green completion refunded the daily debt budget"
    marker = _json.loads((tmp_path / "last_green_run.json").read_text(
        encoding="utf-8"))
    assert marker["debt_attempts"] == _sr.MAX_DEBT_ATTEMPTS_PER_DAY
    assert marker["local_date"] == today, "the green marker was lost"


def test_a_persistent_unknown_verdict_is_bounded_too(monkeypatch, tmp_path):
    """An unreadable diagnostic must not authorise unlimited multi-hour
    catch-up attempts. UNKNOWN proceeds - that is the fail-safe direction -
    but it spends the same daily budget as owed work."""
    unknown = _report(owed=False, unknown=True, reason="evidence unreadable")
    started = []
    for _ in range(4):
        rc, ran, log = _drive(monkeypatch, tmp_path, debt=unknown,
                              argv=["--cadence", "post-fill", "--catch-up"])
        started.append(bool(ran))
    assert sum(started) == _sr.MAX_DEBT_ATTEMPTS_PER_DAY
    assert "DEBT ATTEMPT BUDGET SPENT" in log


def test_the_budget_resets_on_the_next_local_day(monkeypatch, tmp_path):
    owed = _report(owed=True, oldest_owed_fill="2026-09-14", age_days=2)
    for _ in range(_sr.MAX_DEBT_ATTEMPTS_PER_DAY):
        _drive(monkeypatch, tmp_path, debt=owed,
               argv=["--cadence", "post-fill", "--catch-up"])
    rc, ran, log = _drive(monkeypatch, tmp_path, debt=owed,
                          argv=["--cadence", "post-fill", "--catch-up"])
    assert ran == [], "the budget did not bind within the day"
    # Roll the marker's stamp back a day; the next firing must be free again.
    marker = tmp_path / "last_green_run.json"
    rec = _json.loads(marker.read_text(encoding="utf-8"))
    rec["debt_attempt_date"] = "2026-01-01"
    marker.write_text(_json.dumps(rec), encoding="utf-8")
    rc, ran, log = _drive(monkeypatch, tmp_path, debt=owed,
                          argv=["--cadence", "post-fill", "--catch-up"],
                          reset_marker=False)
    assert ran, "the budget did not reset on a new local date"


def test_a_failed_debt_driven_run_still_spends_its_attempt(monkeypatch, tmp_path):
    """The attempt is recorded BEFORE the work. A budget that only counted
    completions would be refilled by every crash."""
    owed = _report(owed=True, oldest_owed_fill="2026-09-14", age_days=2)
    rc, ran, log = _drive(monkeypatch, tmp_path, debt=owed,
                          argv=["--cadence", "post-fill", "--catch-up"],
                          child_rc=9)
    assert ran and rc != 0
    marker = _json.loads((tmp_path / "last_green_run.json").read_text(
        encoding="utf-8"))
    assert marker["debt_attempts"] == 1


def test_the_re_exec_does_not_double_spend_the_budget(monkeypatch, tmp_path):
    """Confirmed by the third review and pinned here. When the preflight
    pull rewrites this script, main() re-runs itself as a waited child. That
    happens BEFORE any attempt is counted, so the child spends exactly one."""
    src = inspect.getsource(_sr.main)
    assert src.index("BTE_SCHED_REEXEC") < src.index("record_debt_attempt("), \
        "the re-exec now happens after an attempt is spent"
    # And behaviourally: one firing spends exactly one attempt.
    owed = _report(owed=True, oldest_owed_fill="2026-09-14", age_days=2)
    rc, ran, log = _drive(monkeypatch, tmp_path, debt=owed,
                          argv=["--cadence", "post-fill", "--catch-up"])
    assert ran
    marker = _json.loads((tmp_path / "last_green_run.json").read_text(
        encoding="utf-8"))
    assert marker["debt_attempts"] == 1


def test_the_budget_is_per_marker_so_components_have_their_own(monkeypatch,
                                                              tmp_path):
    """Corrected scope. Core and Europe keep separate green markers, so a
    catch-up day allows two attempts EACH, not two in total. Four full
    refreshes on the worst day is the intended shape, not a leak."""
    owed = _report(owed=True, oldest_owed_fill="2026-09-14", age_days=2)
    started = {"core": 0, "europe": 0}
    for component in ("core", "europe"):
        for _ in range(3):
            rc, ran, log = _drive(
                monkeypatch, tmp_path, debt=owed,
                argv=["--cadence", "post-fill", "--catch-up",
                      "--component", component])
            started[component] += bool(ran)
    assert started == {"core": _sr.MAX_DEBT_ATTEMPTS_PER_DAY,
                       "europe": _sr.MAX_DEBT_ATTEMPTS_PER_DAY}
    assert (tmp_path / "last_green_core.json").exists()
    assert (tmp_path / "last_green_europe.json").exists()


def test_capture_only_work_is_outside_the_publication_budget(monkeypatch,
                                                             tmp_path):
    """Corrected scope. A collection run publishes nothing, so it owes
    nothing and returns before the debt is read. Six recover-europe-first
    firings legitimately produce six captures: bringing collection under a
    publication budget would stop the one activity that clears a vendor
    retraction."""
    ran = []
    for _ in range(6):
        monkeypatch.setattr(_sr, "LOG_DIR", tmp_path)
        monkeypatch.setattr(_sr, "log_path_for", lambda now: tmp_path / "c.log")
        monkeypatch.setattr(_sr, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(_sr, "price_source_preflight",
                            lambda source: (True, "fixture"))
        monkeypatch.setattr(_sr, "_email", lambda *a, **kw: None)
        monkeypatch.setattr(_sr, "_git", lambda args, log, **kw: _NS(
            returncode=0, stdout="", stderr=""))
        monkeypatch.setattr(_sr, "restore_tracked_outputs", lambda log: True)
        monkeypatch.setattr(_sr.publication_debt, "current_debt",
                            lambda *a, **kw: pytest.fail(
                                "a collection run read the publication debt"))
        monkeypatch.setattr(_sr.subprocess, "run",
                            lambda cmd, **kw: ran.append(cmd) or _NS(returncode=0))
        _sr.main(["--component", "europe", "--capture-only"])
    assert len(ran) == 6, "a collection firing was suppressed by a debt budget"


def test_a_frozen_book_notice_reaches_the_operator(monkeypatch, tmp_path):
    """A frozen venue owes nothing, so `escalate` is false and the old code
    emailed only on escalation: the condition produced catch-up work and a
    log line that nothing read (2026-09-17, seventh review). It is now a
    bounded, deduplicated NOTICE on the same channel."""
    notice = {"kind": "frozen_book", "key": "frozen_book|XETR|2026-09-14",
              "venue": "XETR", "fill": "2026-09-14", "age_days": 31,
              "detail": "XETR has stopped advancing"}
    sent, marked = [], []
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    monkeypatch.setattr(_sr.publication_debt, "record_notice_delivery",
                        lambda led, keys, on, outcome:
                        marked.append((list(keys), outcome)) or True)
    # The record is gated on durable state like every other write, so the
    # production path has to be exercised. REPO_ROOT is tmp_path here, so
    # nothing outside the test directory is touched.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    rc, ran, log = _drive(
        monkeypatch, tmp_path,
        debt=_report(owed=False, unknown=True, escalate=False,
                     notices=(notice,)),
        argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert any("[NOTICE]" in subject and "XETR" in subject
               for subject, _ in sent), "a frozen venue reached no one"
    assert marked == [(["frozen_book|XETR|2026-09-14"], _pd.NOTICE_DELIVERED)], \
        "the notice was not recorded, so it would mail again tomorrow"
    # The attempt was written BEFORE the send, into the real ledger under
    # tmp_path, and that is what bounds an hourly schedule.
    book, _ = _pd.load_ledger(_pd.ledger_path(tmp_path))
    assert book["notices"]["frozen_book|XETR|2026-09-14"]["attempts"] == 1


def test_an_unconfigured_mailer_does_not_spend_the_delivered_budget(
        monkeypatch, tmp_path):
    """The defect the whole workstream exists for was GMAIL_USER unset, and
    the seventh pass reproduced it inside its own repair (2026-09-17, eighth
    review): ``_email`` returned nothing, so the notice path recorded a
    delivery whatever happened, and five silent firings exhausted
    MAX_NOTICES without an operator being told once."""
    notice = {"kind": "frozen_book", "key": "frozen_book|XETR|2026-09-14",
              "venue": "XETR", "fill": "2026-09-14", "age_days": 31,
              "detail": "XETR has stopped advancing"}
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    sent = []
    _drive(monkeypatch, tmp_path,
           debt=_report(owed=False, unknown=True, escalate=False,
                        notices=(notice,)),
           argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent,
           email_outcome=run_status.UNCONFIGURED)
    assert sent, "the attempt was not even made"
    rec = _pd.load_ledger(_pd.ledger_path(tmp_path))[0]["notices"][notice["key"]]
    assert rec["attempts"] == 1, "the attempt was not recorded"
    assert rec["count"] == 0, \
        "an unconfigured mailer spent the budget that exists to stop the " \
        "channel being muted"
    assert rec["last_outcome"] == _pd.NOTICE_UNCONFIRMED
    assert "notified_on" not in rec, "it claimed an operator was told"


def test_a_notice_is_withheld_when_its_dedupe_cannot_be_persisted(
        monkeypatch, tmp_path):
    """Twelve hourly firings against a ledger that cannot be written sent
    twelve identical emails, because the record came AFTER the send and
    nothing survived it. Nothing durable to bound the repetition means
    nothing is sent - the claim is what carries that now."""
    notice = {"kind": "frozen_book", "key": "frozen_book|XETR|2026-09-14",
              "venue": "XETR", "fill": "2026-09-14", "age_days": 31,
              "detail": "XETR has stopped advancing"}
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    monkeypatch.setattr(_sr.publication_debt, "claim_notice",
                        lambda *a, **kw: _pd.NOTICE_UNCLAIMABLE)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    sent = []
    for _ in range(12):
        _drive(monkeypatch, tmp_path,
               debt=_report(owed=False, unknown=True, escalate=False,
                            notices=(notice,)),
               argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert [s for s, _ in sent if "[NOTICE]" in s] == [], \
        "an unbounded mailer is what trains an operator to ignore the channel"
    assert "notice not sent" in (tmp_path / "run.log").read_text(), \
        "it was withheld silently, which is the defect in the other direction"


def test_a_claim_alone_does_not_authorise_a_send(monkeypatch, tmp_path):
    """The claim file bounds REPETITION; the attempt and delivery counts,
    which are the budget that stops an unfixable condition training the
    operator to ignore the channel, live in the ledger. A successful claim
    over a ledger that cannot be written would mail on a budget nothing is
    keeping (2026-09-17, tenth review)."""
    notice = {"kind": "frozen_book", "key": "frozen_book|XETR|2026-09-14",
              "venue": "XETR", "fill": "2026-09-14", "age_days": 31,
              "detail": "XETR has stopped advancing"}
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    released = []
    monkeypatch.setattr(_sr.publication_debt, "release_notice_claim",
                        lambda led, key, on: released.append(key) or True)
    # The claim succeeds. The ledger write does not.
    monkeypatch.setattr(_sr.publication_debt, "claim_notice",
                        lambda *a, **kw: _pd.NOTICE_CLAIMED)
    monkeypatch.setattr(_sr.publication_debt, "record_notice_attempt",
                        lambda *a, **kw: False)
    sent = []
    _drive(monkeypatch, tmp_path,
           debt=_report(owed=False, unknown=True, escalate=False,
                        notices=(notice,)),
           argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert [s for s, _ in sent if "[NOTICE]" in s] == [], \
        "a claim alone authorised the send"
    log = (tmp_path / "run.log").read_text()
    assert "the attempt could not be recorded" in log, "withheld silently"
    assert released == [notice["key"]], \
        "the claim was kept, burning the whole day for a transient failure"


def test_a_failed_observation_withholds_every_notice(monkeypatch, tmp_path):
    """Same rule one step earlier: if this run cannot record its observation
    of the live conditions, the budget cannot be maintained for any of them."""
    notice = {"kind": "frozen_book", "key": "frozen_book|XETR|2026-09-14",
              "venue": "XETR", "fill": "2026-09-14", "age_days": 31,
              "detail": "XETR has stopped advancing"}
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    monkeypatch.setattr(_sr.publication_debt, "record_notice_conditions",
                        lambda *a, **kw: False)
    claimed = []
    monkeypatch.setattr(_sr.publication_debt, "claim_notice",
                        lambda led, key, on: claimed.append(key) or
                        _pd.NOTICE_CLAIMED)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    sent = []
    _drive(monkeypatch, tmp_path,
           debt=_report(owed=False, unknown=True, escalate=False,
                        notices=(notice,)),
           argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert [s for s, _ in sent if "[NOTICE]" in s] == []
    assert claimed == [], "a claim was taken although nothing could be sent"
    assert "notices withheld" in (tmp_path / "run.log").read_text()


def test_a_preflight_only_run_raises_no_escalation_and_marks_nothing(
        monkeypatch, tmp_path):
    """The eighth pass suppressed NOTICES under --preflight-only and left
    escalations mailing and marking, so the documented smoke test still raised
    an overdue-publication alert and spent that obligation's escalation budget
    (2026-09-17, ninth review). There is no reading under which one alert type
    is a side effect and the other is not."""
    marked = []
    monkeypatch.setattr(_sr.publication_debt, "mark_escalated",
                        lambda *a, **kw: marked.append(a) or True)
    sent = []
    _drive(monkeypatch, tmp_path,
           debt=_report(owed=True, escalate=True),
           argv=["--cadence", "post-fill", "--preflight-only"],
           email_sink=sent)
    assert [s for s, _ in sent if "[ESCALATION]" in s] == [], \
        "a smoke test mailed an overdue-publication alert"
    assert marked == [], "a smoke test spent the escalation budget"
    log = (tmp_path / "run.log").read_text()
    assert "escalation suppressed" in log, \
        "it was suppressed silently; the smoke test must still SHOW what it " \
        "found"
    assert "publication debt" in log, "the verdict left the log too"
    # And a real firing still escalates.
    _drive(monkeypatch, tmp_path, debt=_report(owed=True, escalate=True),
           argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert any("[ESCALATION]" in s for s, _ in sent)


def test_two_overlapping_firings_send_one_notice_between_them(monkeypatch,
                                                              tmp_path):
    """The scheduled task retries hourly and a refresh takes one to four
    hours, so overlapping firings are routine. Both pass notices_due - it is
    a read - and the eighth pass let both mail."""
    notice = {"kind": "frozen_book", "key": "frozen_book|XETR|2026-09-14",
              "venue": "XETR", "fill": "2026-09-14", "age_days": 31,
              "detail": "XETR has stopped advancing"}
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    sent = []
    for _ in range(2):
        _drive(monkeypatch, tmp_path,
               debt=_report(owed=False, unknown=True, escalate=False,
                            notices=(notice,)),
               argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert len([s for s, _ in sent if "[NOTICE]" in s]) == 1, \
        "the same notice was mailed twice on the same day"
    assert "already_claimed" in (tmp_path / "run.log").read_text()


def test_a_preflight_only_run_sends_no_notice_and_spends_no_state(
        monkeypatch, tmp_path):
    """--preflight-only is the documented smoke test. It ran the notice
    block, so a rehearsal mailed an operational alert and consumed the day's
    dedupe, leaving the real firing silent."""
    notice = {"kind": "frozen_book", "key": "frozen_book|XETR|2026-09-14",
              "venue": "XETR", "fill": "2026-09-14", "age_days": 31,
              "detail": "XETR has stopped advancing"}
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    sent = []
    _drive(monkeypatch, tmp_path,
           debt=_report(owed=False, unknown=True, escalate=False,
                        notices=(notice,)),
           argv=["--cadence", "post-fill", "--preflight-only"],
           email_sink=sent)
    assert [s for s, _ in sent if "[NOTICE]" in s] == [], \
        "a smoke test mailed an operational notice"
    assert not _pd.ledger_path(tmp_path).exists() or not _pd.load_ledger(
        _pd.ledger_path(tmp_path))[0]["notices"], \
        "a smoke test spent the day's dedupe"
    # And the real firing that follows still says it.
    _drive(monkeypatch, tmp_path,
           debt=_report(owed=False, unknown=True, escalate=False,
                        notices=(notice,)),
           argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert any("[NOTICE]" in s for s, _ in sent)


def test_no_notice_means_no_mail(monkeypatch, tmp_path):
    sent = []
    _drive(monkeypatch, tmp_path, debt=_report(owed=False),
           argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert sent == [], "a healthy book mailed anyway"


def test_the_notice_says_nothing_about_the_broker(monkeypatch, tmp_path):
    """Publication is not execution, and the one surface that reaches a
    person must not blur them."""
    notice = {"kind": "frozen_book", "key": "k", "venue": "XETR",
              "fill": "2026-09-14", "age_days": 31, "detail": "stopped"}
    sent = []
    monkeypatch.setattr(_sr.publication_debt, "notices_due",
                        lambda rep, led, on: list(rep.notices))
    _drive(monkeypatch, tmp_path,
           debt=_report(owed=False, unknown=True, notices=(notice,)),
           argv=["--cadence", "post-fill", "--catch-up"], email_sink=sent)
    assert sent and "not the broker" in sent[0][1]


def test_catch_up_propagates_to_component_children(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(_sr, "LOG_DIR", tmp_path)
    monkeypatch.setattr(_sr, "log_path_for", lambda now: tmp_path / "run.log")
    monkeypatch.setattr(_sr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_sr, "price_source_preflight",
                        lambda source: (True, "fixture"))
    monkeypatch.setattr(_sr, "_email", lambda *a, **kw: None)
    monkeypatch.setattr(_sr, "_git", lambda args, log, **kw: _NS(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(_sr.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or _NS(returncode=0))
    _sr.main(["--cadence", "weekend", "--catch-up", "--push"])
    assert seen, "no child was started"
    assert all("--catch-up" in cmd for cmd in seen), \
        "a child without the flag suppresses itself on its own green marker"
