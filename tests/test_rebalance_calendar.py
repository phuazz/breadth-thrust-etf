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
from rebalance_calendar import weekly_rebalance_dates  # noqa: E402


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
