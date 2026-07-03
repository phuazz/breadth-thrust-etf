"""Edge-case tests for scripts/check_freshness_headroom.py.

Month- and year-boundary cases per CLAUDE.md date rules, plus the
holiday-consumes-budget property inherited from regime_publish (plain
weekday counting, no US holiday calendar — deliberate fail-early).

Python date months are 1-indexed (January = 1). Each expected lag states
its weekday walk in a comment so review does not rely on mental
calendar arithmetic.
"""

from __future__ import annotations

from datetime import date

from scripts.check_freshness_headroom import (
    WARN_AT_LAG,
    classify,
    deadline_strings,
    first_failing_run_date,
    panel_lag,
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
