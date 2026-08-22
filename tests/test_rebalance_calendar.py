"""Tests for scripts/rebalance_calendar.weekly_rebalance_dates.

Guards the shared weekly rebalance-date rule extracted from the engines
(2026-07-06 dedup of five identical sites). Behaviour must match the old
inline expression EXACTLY:
    target = pd.date_range(eligible_start, index[-1], freq); index[index.isin(target)]
including that a market-holiday Friday drops that whole week (the current
behaviour the held rebalance-cadence change would later replace).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pytest  # noqa: E402

from rebalance_calendar import (  # noqa: E402
    scheduled_data_gaps,
    weekly_rebalance_dates,
)


def _old_inline(index, eligible_start, freq="W-FRI"):
    """The pre-refactor inline logic, verbatim, for equivalence checks."""
    target = pd.date_range(eligible_start, index[-1], freq=freq)
    return index[index.isin(target)]


def test_matches_old_inline_on_full_trading_calendar():
    idx = pd.bdate_range("2026-01-01", "2026-03-31")  # Mon-Fri business days
    elig = idx[0]
    out = weekly_rebalance_dates(idx, elig)
    assert list(out) == list(_old_inline(idx, elig))
    assert all(d.dayofweek == 4 for d in out)  # every rebalance is a Friday


def test_skips_a_holiday_friday_week_current_behaviour():
    """Drop a Friday (market shut) and confirm that week gets NO rebalance
    and NO Thursday substitute -- the documented current behaviour."""
    idx = pd.bdate_range("2026-06-01", "2026-07-10")
    holiday_friday = pd.Timestamp("2026-07-03")
    assert holiday_friday in idx
    idx = idx[idx != holiday_friday]                  # market closed that Fri
    out = weekly_rebalance_dates(idx, idx[0])
    assert holiday_friday not in out                  # week dropped...
    assert pd.Timestamp("2026-07-02") not in out      # ...no Thursday fallback
    assert pd.Timestamp("2026-06-26") in out          # neighbours unaffected
    assert pd.Timestamp("2026-07-10") in out
    assert list(out) == list(_old_inline(idx, idx[0]))


def test_respects_eligible_start():
    idx = pd.bdate_range("2026-01-01", "2026-02-28")
    elig = pd.Timestamp("2026-02-02")
    out = weekly_rebalance_dates(idx, elig)
    assert out.min() >= elig
    assert list(out) == list(_old_inline(idx, elig))


def test_month_and_year_boundary_edges():
    # CLAUDE.md date rule: exercise a month boundary and a year boundary.
    idx = pd.bdate_range("2025-12-01", "2026-01-31")
    out = weekly_rebalance_dates(idx, idx[0])
    assert list(out) == list(_old_inline(idx, idx[0]))
    assert all(d.dayofweek == 4 for d in out)
    assert any(d.year == 2025 for d in out) and any(d.year == 2026 for d in out)


# --------------------------------------------------------------------------
# mode="last_session" -- the candidate replacement, not yet the default.
# --------------------------------------------------------------------------

def test_default_mode_is_unchanged_scheduled():
    """The deployed default must stay byte-identical to the old inline rule."""
    idx = pd.bdate_range("2026-01-01", "2026-03-31")
    assert list(weekly_rebalance_dates(idx, idx[0])) == list(_old_inline(idx, idx[0]))


def test_last_session_falls_back_to_thursday():
    """The holiday Friday that prompted this: 2026-07-03 (July 4 observed)."""
    idx = pd.bdate_range("2026-06-01", "2026-07-10")
    idx = idx[idx != pd.Timestamp("2026-07-03")]      # market closed that Fri
    out = weekly_rebalance_dates(idx, idx[0], mode="last_session")
    assert pd.Timestamp("2026-07-02") in out          # Thursday substitute
    assert pd.Timestamp("2026-06-26") in out          # neighbours unaffected
    assert pd.Timestamp("2026-07-10") in out
    # Exactly one decision per calendar week, same count as a clean calendar.
    clean = weekly_rebalance_dates(pd.bdate_range("2026-06-01", "2026-07-10"),
                                   pd.Timestamp("2026-06-01"))
    assert len(out) == len(clean)


def test_last_session_matches_scheduled_when_no_holidays():
    """With every Friday open the two modes must agree exactly."""
    idx = pd.bdate_range("2026-01-01", "2026-03-31")
    assert list(weekly_rebalance_dates(idx, idx[0], mode="last_session")) == \
           list(weekly_rebalance_dates(idx, idx[0]))


def test_last_session_never_precedes_eligible_start():
    idx = pd.bdate_range("2026-01-01", "2026-02-28")
    elig = pd.Timestamp("2026-02-02")                 # a Monday
    out = weekly_rebalance_dates(idx, elig, mode="last_session")
    assert out.min() >= elig


def test_last_session_dedupes_a_fully_shut_week():
    """If an entire week has no sessions, two scheduled Fridays fall back to
    the same day; the result must not carry it twice."""
    idx = pd.bdate_range("2026-03-02", "2026-03-27")
    shut = (idx >= "2026-03-09") & (idx <= "2026-03-13")   # whole week out
    out = weekly_rebalance_dates(idx[~shut], idx[0], mode="last_session")
    assert out.is_unique
    assert pd.Timestamp("2026-03-06") in out          # the collapsed target


def test_last_session_month_and_year_boundary():
    # CLAUDE.md date rule: one month boundary, one year boundary. New Year's
    # Day 2027 falls on a Friday, so this exercises a year-boundary fallback.
    idx = pd.bdate_range("2026-12-01", "2027-01-29")
    idx = idx[idx != pd.Timestamp("2027-01-01")]      # New Year's Day, a Fri
    out = weekly_rebalance_dates(idx, idx[0], mode="last_session")
    assert out.is_unique
    assert pd.Timestamp("2026-12-31") in out          # Thu 31 Dec substitute
    assert any(d.year == 2026 for d in out) and any(d.year == 2027 for d in out)
    # Month boundary: every calendar week in range still gets exactly one.
    assert len(out) == len(pd.date_range(idx[0], idx[-1], freq="W-FRI"))


# --------------------------------------------------------------------------
# mode="holiday_aware" -- fallback ONLY when the exchange really was shut.
# --------------------------------------------------------------------------

def test_holiday_aware_requires_a_calendar():
    idx = pd.bdate_range("2026-01-01", "2026-03-31")
    with pytest.raises(ValueError, match="requires calendar"):
        weekly_rebalance_dates(idx, idx[0], mode="holiday_aware")


def test_calendar_rejected_for_other_modes():
    """A calendar= passed to a mode that ignores it is a caller error, not a
    silent no-op -- it would read as holiday-aware while behaving otherwise."""
    idx = pd.bdate_range("2026-01-01", "2026-03-31")
    with pytest.raises(ValueError, match="only meaningful"):
        weekly_rebalance_dates(idx, idx[0], mode="last_session",
                               calendar="NYSE")


def test_holiday_aware_falls_back_on_a_real_nyse_holiday():
    """Fri 2026-07-03 (July 4 observed) is genuinely shut -> use Thursday."""
    idx = pd.bdate_range("2026-06-01", "2026-07-10")
    idx = idx[idx != pd.Timestamp("2026-07-03")]
    out = weekly_rebalance_dates(idx, idx[0], mode="holiday_aware",
                                 calendar="NYSE")
    assert pd.Timestamp("2026-07-02") in out
    assert pd.Timestamp("2026-07-10") in out


def test_holiday_aware_skips_a_vendor_gap_instead_of_trading_it():
    """The live 2025-10-24 case: XETR TRADED, our panel lacks the bar. The
    week must be skipped (as today), never rebalanced onto the Thursday."""
    idx = pd.bdate_range("2025-10-06", "2025-11-07")
    assert pd.Timestamp("2025-10-24") in idx
    idx = idx[idx != pd.Timestamp("2025-10-24")]      # vendor dropped it
    out = weekly_rebalance_dates(idx, idx[0], mode="holiday_aware",
                                 calendar="XETR")
    assert pd.Timestamp("2025-10-23") not in out      # no silent early trade
    assert pd.Timestamp("2025-10-24") not in out      # and no phantom session
    assert pd.Timestamp("2025-10-17") in out          # neighbours unaffected
    assert pd.Timestamp("2025-10-31") in out
    # last_session cannot make this distinction -- that is why it is unsafe.
    naive = weekly_rebalance_dates(idx, idx[0], mode="last_session")
    assert pd.Timestamp("2025-10-23") in naive


def test_scheduled_data_gaps_reports_only_true_gaps():
    idx = pd.bdate_range("2026-06-01", "2026-07-10")
    idx = idx[idx != pd.Timestamp("2026-07-03")]      # real holiday
    assert scheduled_data_gaps(idx, idx[0], calendar="NYSE") == []
    idx2 = idx[idx != pd.Timestamp("2026-06-26")]     # real session, dropped
    gaps = scheduled_data_gaps(idx2, idx2[0], calendar="NYSE")
    assert [str(g.date()) for g in gaps] == ["2026-06-26"]


def test_holiday_aware_month_and_year_boundary():
    # CLAUDE.md date rule. New Year's Day 2027 is a Friday and a real NYSE
    # holiday, so this covers the year boundary with a genuine fallback.
    idx = pd.bdate_range("2026-12-01", "2027-01-29")
    idx = idx[idx != pd.Timestamp("2027-01-01")]
    out = weekly_rebalance_dates(idx, idx[0], mode="holiday_aware",
                                 calendar="NYSE")
    assert out.is_unique
    assert pd.Timestamp("2026-12-31") in out          # Thu 31 Dec substitute
    assert any(d.year == 2026 for d in out) and any(d.year == 2027 for d in out)
    # Month boundary: Fri 2027-01-29 is an ordinary session and must appear.
    assert pd.Timestamp("2027-01-29") in out


def test_holiday_aware_matches_scheduled_on_a_clean_calendar():
    """No holidays, no gaps -> the two modes must be identical."""
    idx = pd.bdate_range("2026-02-02", "2026-03-27")  # no NYSE holidays
    assert list(weekly_rebalance_dates(idx, idx[0], mode="holiday_aware",
                                       calendar="NYSE")) == \
           list(weekly_rebalance_dates(idx, idx[0]))


# --------------------------------------------------------------------------
# mode="holiday_aware_next" -- the FORWARD twin, for grids whose scheduled day
# sits one session after the signal bar (W-MON: engines read Friday's close).
# Every dated fixture below was checked against pandas_market_calendars, not
# recalled: 2026-09-07 Labor Day, 2026-05-25 Memorial Day and 2024-01-01 New
# Year's Day are all Mondays and all genuinely shut on the NYSE.
# --------------------------------------------------------------------------

def test_holiday_aware_next_requires_a_calendar():
    idx = pd.bdate_range("2026-01-01", "2026-03-31")
    with pytest.raises(ValueError, match="requires calendar"):
        weekly_rebalance_dates(idx, idx[0], mode="holiday_aware_next")


def test_holiday_aware_next_rolls_forward_not_back():
    """Labor Day, Mon 2026-09-07. Forward mode takes the Tuesday; the deployed
    backward mode takes the prior Friday. The contrast IS the mode."""
    idx = pd.bdate_range("2026-08-03", "2026-09-25")
    idx = idx[idx != pd.Timestamp("2026-09-07")]      # Labor Day, shut
    fwd = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                 mode="holiday_aware_next", calendar="NYSE")
    back = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                  mode="holiday_aware", calendar="NYSE")
    assert pd.Timestamp("2026-09-08") in fwd          # Tuesday, rolled forward
    assert pd.Timestamp("2026-09-04") not in fwd
    assert pd.Timestamp("2026-09-04") in back         # prior Friday, backward
    assert pd.Timestamp("2026-09-08") not in back
    assert pd.Timestamp("2026-08-31") in fwd          # neighbours unaffected
    assert pd.Timestamp("2026-09-14") in fwd


def _signal_bars_are_weekly_closes(idx, rebalance_dates) -> tuple[int, int]:
    """(n_checked, n_whose_signal_bar_is_its_week's_final_session).

    The engines read the session BEFORE the rebalance date. The property a
    W-MON grid is built for is that this bar is a genuine WEEKLY CLOSE - the
    last session of the preceding week. Usually that is a Friday, but in a
    Good Friday week it is the Thursday, so the test is "last session of its
    ISO week", not "is a Friday". Asserting the weekday would fail on Easter
    for a reason that has nothing to do with the mode.
    """
    last_of_week: dict[tuple[int, int], pd.Timestamp] = {}
    for ts in idx:                       # ascending, so the last write wins
        c = ts.isocalendar()
        last_of_week[(c.year, c.week)] = ts
    checked = closes = 0
    for d in rebalance_dates:
        i = idx.get_loc(d)
        if i == 0:
            continue
        prev = idx[i - 1]
        c = prev.isocalendar()
        checked += 1
        closes += int(last_of_week[(c.year, c.week)] == prev)
    return checked, closes


def test_holiday_aware_next_signal_bar_is_always_a_weekly_close():
    """The property the mode exists for, on a real NYSE session index."""
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar("NYSE").schedule("2024-01-01", "2026-08-07")
    idx = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in sched.index])

    fwd = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                 mode="holiday_aware_next", calendar="NYSE")
    checked, closes = _signal_bars_are_weekly_closes(idx, fwd)
    assert checked > 100
    assert closes == checked, (
        f"{checked - closes} of {checked} W-MON rebalances read a mid-week "
        "bar instead of the prior weekly close")

    # The deployed backward mode fails exactly this on the rolled weeks, which
    # is why it is the wrong roll direction for a forward-offset grid.
    back = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                  mode="holiday_aware", calendar="NYSE")
    b_checked, b_closes = _signal_bars_are_weekly_closes(idx, back)
    assert b_closes < b_checked


def test_holiday_aware_next_skips_a_vendor_gap_instead_of_rolling_it():
    """Mon 2026-09-14 is an ordinary NYSE session. If our panel lacks the bar
    that is OUR defect, so the week is skipped -- never rolled onto Tuesday."""
    idx = pd.bdate_range("2026-08-31", "2026-10-02")
    assert pd.Timestamp("2026-09-14") in idx
    idx = idx[idx != pd.Timestamp("2026-09-14")]      # vendor dropped it
    out = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                 mode="holiday_aware_next", calendar="NYSE")
    assert pd.Timestamp("2026-09-15") not in out      # no silent late trade
    assert pd.Timestamp("2026-09-14") not in out      # and no phantom session
    assert pd.Timestamp("2026-09-21") in out          # neighbours unaffected


def test_holiday_aware_next_rolls_past_a_multi_day_closure():
    """A roll is not capped at a day or two. Xetra shut Mon 2018-12-24 through
    Wed 2018-12-26, so the decision belongs on Thu 27 Dec, still reading the
    Fri 21 Dec close. Verified against pandas_market_calendars, not recalled.
    """
    idx = pd.bdate_range("2018-12-03", "2019-01-18")
    # Xetra's actual closures across the turn: 24-26 Dec, 31 Dec and 1 Jan.
    shut = pd.DatetimeIndex(["2018-12-24", "2018-12-25", "2018-12-26",
                             "2018-12-31", "2019-01-01"])
    idx = idx[~idx.isin(shut)]
    out = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                 mode="holiday_aware_next", calendar="XETR")
    assert pd.Timestamp("2018-12-27") in out           # Thursday, 3 sessions on
    assert pd.Timestamp("2018-12-21") not in out       # NOT the prior Friday
    assert pd.Timestamp("2019-01-02") in out           # Mon 31 Dec shut too
    assert pd.Timestamp("2018-12-28") not in out
    # One decision per ISO week, which a backward roll would break.
    weeks = [(d.isocalendar().year, d.isocalendar().week) for d in out]
    assert len(set(weeks)) == len(out)


def test_holiday_aware_next_never_merges_two_weeks():
    """A roll must not reach the next scheduled day. Shut Labor Day plus a
    missing rest-of-week would otherwise land the 07 Sep decision on 14 Sep,
    which already has its own -- one decision, counted twice."""
    idx = pd.bdate_range("2026-08-24", "2026-09-25")
    drop = pd.DatetimeIndex(["2026-09-07", "2026-09-08", "2026-09-09",
                             "2026-09-10", "2026-09-11"])
    idx = idx[~idx.isin(drop)]
    out = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                 mode="holiday_aware_next", calendar="NYSE")
    assert out.is_unique
    assert list(out).count(pd.Timestamp("2026-09-14")) == 1
    assert pd.Timestamp("2026-09-14") in out


def test_holiday_aware_next_runs_off_the_tail():
    """A shut scheduled Monday with no later session is dropped, not carried
    onto a date the panel does not have."""
    idx = pd.bdate_range("2026-08-10", "2026-09-04")   # ends Fri before Labor Day
    out = weekly_rebalance_dates(idx, pd.Timestamp("2026-08-10"), "W-MON",
                                 mode="holiday_aware_next", calendar="NYSE")
    assert out.max() <= idx[-1]
    assert pd.Timestamp("2026-09-07") not in out


def test_holiday_aware_next_matches_scheduled_on_a_clean_calendar():
    idx = pd.bdate_range("2026-03-02", "2026-04-24")   # no Monday holidays
    assert list(weekly_rebalance_dates(idx, idx[0], "W-MON",
                                       mode="holiday_aware_next",
                                       calendar="NYSE")) == \
           list(weekly_rebalance_dates(idx, idx[0], "W-MON"))


def test_holiday_aware_next_year_boundary():
    """CLAUDE.md date rule -- year boundary. Mon 2024-01-01 is New Year's Day
    and genuinely shut, so the roll crosses from 2023 into 2024."""
    idx = pd.bdate_range("2023-12-04", "2024-01-26")
    idx = idx[idx != pd.Timestamp("2024-01-01")]
    out = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                 mode="holiday_aware_next", calendar="NYSE")
    assert out.is_unique
    assert pd.Timestamp("2024-01-02") in out           # Tue 2 Jan substitute
    assert pd.Timestamp("2023-12-29") not in out       # NOT the prior Friday
    assert any(d.year == 2023 for d in out) and any(d.year == 2024 for d in out)


def test_holiday_aware_next_month_boundary():
    """CLAUDE.md date rule -- month boundary. Memorial Day, Mon 2026-05-25,
    rolls to Tue 26 May inside a range spanning May into June."""
    idx = pd.bdate_range("2026-05-04", "2026-06-19")
    idx = idx[idx != pd.Timestamp("2026-05-25")]
    out = weekly_rebalance_dates(idx, idx[0], "W-MON",
                                 mode="holiday_aware_next", calendar="NYSE")
    assert pd.Timestamp("2026-05-26") in out
    assert pd.Timestamp("2026-06-01") in out           # first Monday of June
    assert any(d.month == 5 for d in out) and any(d.month == 6 for d in out)
    # Every scheduled week in range still gets exactly one decision.
    assert len(out) == len(pd.date_range(idx[0], idx[-1], freq="W-MON"))


def test_adding_a_mode_does_not_change_the_deployed_default():
    """Adding a mode must not move deployed behaviour — only a decision may.

    REWRITTEN 2026-08-22. It asserted DEFAULT_MODE == HOLIDAY_AWARE, which was
    right when WS12/WS13 added the forward-roll mode and deliberately did NOT
    deploy it. WS18 then deployed it on evidence, so the old assertion is
    superseded rather than wrong.

    Kept, not deleted, because the guard it provides is still needed: the next
    mode added must not move the default either. Only the expected value moves,
    and it now carries its provenance — register record
    2026-08-22-breadth-thrust-etf-2, where HOLIDAY_AWARE was rejected for a
    Monday cadence because a holiday Monday rolls BACK onto the previous
    Friday, landing a fill before the decision that produces it.
    """
    import rebalance_calendar as rc
    assert rc.DEFAULT_MODE == rc.HOLIDAY_AWARE_NEXT
    idx = pd.bdate_range("2026-06-01", "2026-07-10")
    idx = idx[idx != pd.Timestamp("2026-07-03")]
    assert list(rc.engine_rebalance_dates(idx, idx[0])) == \
           list(weekly_rebalance_dates(idx, idx[0], mode="holiday_aware_next",
                                       calendar="NYSE"))


def test_the_two_holiday_modes_still_differ_where_it_matters():
    """The rewrite above would pass vacuously if the modes had converged, so
    pin that they genuinely disagree on a holiday — otherwise the WS18
    decision would be untestable and the record misleading."""
    import rebalance_calendar as rc
    idx = pd.bdate_range("2026-06-01", "2026-07-10")
    idx = idx[idx != pd.Timestamp("2026-07-03")]      # US Independence Day
    back = list(weekly_rebalance_dates(idx, idx[0], mode="holiday_aware",
                                       calendar="NYSE"))
    fwd = list(weekly_rebalance_dates(idx, idx[0], mode="holiday_aware_next",
                                      calendar="NYSE"))
    assert back != fwd, "the two modes must not have converged"
