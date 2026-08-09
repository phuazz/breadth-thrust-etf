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
