"""Edge-case tests for scripts/check_freshness_headroom.py.

Month- and year-boundary cases per CLAUDE.md date rules, plus the
holiday-consumes-budget property inherited from regime_publish (plain
weekday counting, no US holiday calendar — deliberate fail-early).

Python date months are 1-indexed (January = 1). Each expected lag states
its weekday walk in a comment so review does not rely on mental
calendar arithmetic.
"""

from __future__ import annotations

import json
from datetime import date

from scripts.check_freshness_headroom import (
    WARN_AT_LAG,
    build_report,
    classify,
    deadline_strings,
    email_tag,
    first_failing_run_date,
    panel_lag,
    weekend_between,
)


# ---------------------------------------------------------------------------
# Lag arithmetic (mirrors the pipeline hard guard via regime_publish)
# ---------------------------------------------------------------------------

def test_lag_zero_when_panel_current():
    assert panel_lag(date(2026, 7, 2), date(2026, 7, 2)) == 0
    # Panel dated after "today" (clock skew) clamps to 0, never negative.
    assert panel_lag(date(2026, 7, 3), date(2026, 7, 2)) == 0


def test_lag_month_boundary():
    # Fri 26 Jun -> Fri 3 Jul 2026 spans the June/July boundary.
    # Counted weekdays: Jun 26, 29, 30, Jul 1, 2 = 5.
    assert panel_lag(date(2026, 6, 26), date(2026, 7, 3)) == 5


def test_lag_year_boundary_counts_new_years_day():
    # Fri 26 Dec 2025 -> Fri 2 Jan 2026 spans the year boundary.
    # Counted weekdays: Dec 26, 29, 30, 31, Jan 1 = 5. New Year's Day is
    # a US market holiday but still counts — weekday arithmetic by design.
    assert panel_lag(date(2025, 12, 26), date(2026, 1, 2)) == 5


def test_holiday_consumes_budget():
    # Fri 26 Jun -> Mon 6 Jul 2026 = 6 weekdays although only 5 NYSE
    # sessions elapsed (Fri 3 Jul was the Independence Day observance).
    # The guard therefore trips one trading day early around US holidays;
    # this test documents that as intended behaviour, not a bug.
    assert panel_lag(date(2026, 6, 26), date(2026, 7, 6)) == 6


# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------

def test_classify_boundaries():
    assert classify(0) == "ok"
    assert classify(3) == "ok"
    assert classify(WARN_AT_LAG) == "warn"   # 4 — first warning email
    assert classify(5) == "warn"             # at budget: still publishes
    assert classify(6) == "fail"             # hard guard aborts builds


# ---------------------------------------------------------------------------
# First-failing-run forecast (the outage-prevention check)
# ---------------------------------------------------------------------------

def test_first_failing_run_is_the_post_holiday_monday():
    # The 2026-07-03 live case: panel at Fri 26 Jun, checked on Fri 3 Jul
    # (lag 5, passes at the boundary) -> the first scheduled run that
    # trips the guard is Mon 6 Jul (lag 6).
    assert first_failing_run_date(date(2026, 6, 26), date(2026, 7, 3)) == date(2026, 7, 6)


def test_first_failing_run_never_a_weekend():
    # Checked on Sat 4 Jul: Saturday itself already has lag 6, but no
    # cron fires on weekends — the answer must still be Monday.
    d = first_failing_run_date(date(2026, 6, 26), date(2026, 7, 4))
    assert d == date(2026, 7, 6)
    assert d.weekday() < 5


# ---------------------------------------------------------------------------
# Deadline rendering (UTC -> SGT crosses midnight)
# ---------------------------------------------------------------------------

def test_deadline_strings_cross_midnight_into_sgt():
    # 21:30 UTC on Mon 6 Jul is 05:30 SGT on Tue 7 Jul — the conversion
    # must move the calendar day as well as the clock.
    utc_s, sgt_s = deadline_strings(date(2026, 7, 6))
    assert utc_s == "Mon 2026-07-06 21:30 UTC"
    assert sgt_s == "Tue 2026-07-07 05:30 SGT"


# ---------------------------------------------------------------------------
# Email tier (2026-07-25): REMINDER for the structural end-of-week state,
# WARN when the weekend refresh window is gone or the hard stop is live
# ---------------------------------------------------------------------------

def test_weekend_between_excludes_endpoints():
    # Fri 24 Jul -> Mon 27 Jul: Sat 25 / Sun 26 lie strictly between.
    assert weekend_between(date(2026, 7, 24), date(2026, 7, 27)) is True
    # Tue 28 Jul -> Thu 30 Jul: only weekdays between.
    assert weekend_between(date(2026, 7, 28), date(2026, 7, 30)) is False
    # Same day and adjacent days: nothing strictly between.
    assert weekend_between(date(2026, 7, 24), date(2026, 7, 24)) is False
    assert weekend_between(date(2026, 7, 24), date(2026, 7, 25)) is False


def test_tag_structural_friday_is_reminder():
    # The 2026-07-24/25 live case: panel at Fri 17 Jul, Friday 24 Jul
    # run. Lag 5 (Jul 17, 20, 21, 22, 23), first failing run Mon 27 Jul,
    # weekend 25-26 Jul still ahead -> routine end-of-week REMINDER.
    today = date(2026, 7, 24)
    fail_day = first_failing_run_date(date(2026, 7, 17), today)
    assert fail_day == date(2026, 7, 27)
    assert email_tag("warn", today, fail_day) == "REMINDER"


def test_tag_structural_thursday_is_reminder():
    # Thu 23 Jul run at lag 4 (Jul 17, 20, 21, 22) — the first alert of
    # a normal week. Weekend still ahead of Mon 27 Jul -> REMINDER.
    today = date(2026, 7, 23)
    fail_day = first_failing_run_date(date(2026, 7, 17), today)
    assert fail_day == date(2026, 7, 27)
    assert email_tag("warn", today, fail_day) == "REMINDER"


def test_tag_midweek_staleness_is_warn():
    # Panel left at Wed 22 Jul (mid-week refresh, then skipped): the Tue
    # 28 Jul run has lag 4 (Jul 22, 23, 24, 27) and the first failing
    # run is Thu 30 Jul (lag 6) — no weekend between Tue and Thu, so the
    # routine weekend window cannot save it -> real WARN.
    today = date(2026, 7, 28)
    fail_day = first_failing_run_date(date(2026, 7, 22), today)
    assert fail_day == date(2026, 7, 30)
    assert email_tag("warn", today, fail_day) == "WARN"


def test_tag_fail_is_warn_regardless_of_weekend():
    # Hard stop is never softened to a reminder, weekend ahead or not.
    assert email_tag("fail", date(2026, 7, 24), date(2026, 7, 27)) == "WARN"


def test_tag_ok_sends_nothing():
    assert email_tag("ok", date(2026, 7, 24), date(2026, 7, 27)) == "OK"


def test_tag_reminder_across_month_boundary():
    # Fri 31 Jul run, panel at Fri 24 Jul: lag 5 (Jul 24, 27, 28, 29,
    # 30); first failing run Mon 3 Aug; the weekend 1-2 Aug spans the
    # July/August boundary -> REMINDER.
    today = date(2026, 7, 31)
    fail_day = first_failing_run_date(date(2026, 7, 24), today)
    assert fail_day == date(2026, 8, 3)
    assert email_tag("warn", today, fail_day) == "REMINDER"


def test_tag_reminder_across_year_boundary():
    # Thu 31 Dec 2026 run, panel at Fri 25 Dec: lag 4 (Dec 25, 28, 29,
    # 30); first failing run Mon 4 Jan 2027 (lag 6 counts Thu 31 Dec and
    # Fri 1 Jan — the holiday consumes budget by design); the weekend
    # 2-3 Jan 2027 spans the year boundary -> REMINDER.
    today = date(2026, 12, 31)
    fail_day = first_failing_run_date(date(2026, 12, 25), today)
    assert fail_day == date(2027, 1, 4)
    assert email_tag("warn", today, fail_day) == "REMINDER"


# ---------------------------------------------------------------------------
# build_report carries the tier through to the outputs the workflows read
# ---------------------------------------------------------------------------

def _panel(tmp_path, end_date_iso):
    p = tmp_path / "breadth_csp1.json"
    p.write_text(json.dumps({"end_date": end_date_iso}), encoding="utf-8")
    return p


def test_build_report_reminder_tier(tmp_path):
    report = build_report(_panel(tmp_path, "2026-07-17"), date(2026, 7, 24), WARN_AT_LAG)
    assert report["status"] == "warn"
    assert report["tag"] == "REMINDER"
    assert report["summary"].startswith("weekend panel refresh due")
    assert "Mon 2026-07-27 21:30 UTC" in report["summary"]
    assert "email tier                 : REMINDER" in report["detail"]


def test_build_report_warn_tier_midweek(tmp_path):
    report = build_report(_panel(tmp_path, "2026-07-22"), date(2026, 7, 28), WARN_AT_LAG)
    assert report["status"] == "warn"
    assert report["tag"] == "WARN"
    assert report["summary"].startswith("breadth_csp1 lag 4/5")


def test_build_report_fail_tier(tmp_path):
    # Mon 27 Jul run with the panel still at Fri 17 Jul: lag 6 -> the
    # hard guard aborts this very run; tag must be WARN, never REMINDER.
    report = build_report(_panel(tmp_path, "2026-07-17"), date(2026, 7, 27), WARN_AT_LAG)
    assert report["status"] == "fail"
    assert report["tag"] == "WARN"
    assert "aborts builds NOW" in report["summary"]
