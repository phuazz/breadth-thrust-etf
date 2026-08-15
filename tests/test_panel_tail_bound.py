"""The panel's tail bound: last COMPLETED session, per venue.

Python months are 1-indexed (January = 1), unlike JavaScript's 0-indexed Date.
Every literal below is 1-indexed.

What this protects. compute_breadth used to end its daily loop at the last
published roster Friday, so a Friday-morning run produced a panel ending the
PREVIOUS Friday while the decision that morning reads Thursday's close. The
bound now follows the venue calendar instead. The risk it introduces is the
opposite one — running past the close of a session that has not finished, and
computing breadth on a partial bar the vendor will revise. These tests exist
for that risk.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute_breadth import last_completed_session_on  # noqa: E402

NYSE = mcal.get_calendar("NYSE")
XETR = mcal.get_calendar("XETR")


def _utc(y, m, d, hh=0, mm=0):
    """1-indexed month, as Python uses."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_before_the_close_returns_the_previous_session():
    """The live case. Fri 14 Aug 2026, 12:05 UTC — NYSE opens at 13:30.

    This is the exact moment the refresh asked the question. Answering "today"
    would have put an unfinished session into a panel feeding that evening's
    fill.
    """
    got = last_completed_session_on(NYSE, _utc(2026, 8, 14, 12, 5))
    assert got == pd.Timestamp("2026-08-13")


def test_after_the_close_includes_that_session():
    got = last_completed_session_on(NYSE, _utc(2026, 8, 13, 21, 0))
    assert got == pd.Timestamp("2026-08-13")


def test_the_minute_before_the_close_still_excludes_it():
    """NYSE closes 20:00 UTC. 19:59 must not count the session as complete."""
    assert last_completed_session_on(NYSE, _utc(2026, 8, 13, 19, 59)) \
        == pd.Timestamp("2026-08-12")
    assert last_completed_session_on(NYSE, _utc(2026, 8, 13, 20, 1)) \
        == pd.Timestamp("2026-08-13")


def test_weekend_falls_back_to_friday():
    assert last_completed_session_on(NYSE, _utc(2026, 8, 16, 6, 0)) \
        == pd.Timestamp("2026-08-14")


def test_month_boundary():
    """1 June 2026 is a Monday; the last completed session is Fri 29 May."""
    got = last_completed_session_on(NYSE, _utc(2026, 6, 1, 6, 0))
    assert got == pd.Timestamp("2026-05-29")
    assert got.month == 5, "must not roll forward into the new month"


def test_year_boundary():
    """1 January is a holiday on both venues; roll back into the prior year."""
    got = last_completed_session_on(NYSE, _utc(2026, 1, 1, 12, 0))
    assert got == pd.Timestamp("2025-12-31")
    assert got.year == 2025


def test_venues_disagree_and_that_is_the_point():
    """US Independence Day, Fri 3 Jul 2026 — NYSE shut, Xetra open.

    A single NYSE-derived cap would truncate the European funds by a session.
    """
    when = _utc(2026, 7, 3, 20, 0)          # after Xetra's close
    assert last_completed_session_on(XETR, when) == pd.Timestamp("2026-07-03")
    assert last_completed_session_on(NYSE, when) == pd.Timestamp("2026-07-02")


def test_naive_datetimes_are_treated_as_utc_not_rejected():
    aware = last_completed_session_on(NYSE, _utc(2026, 8, 14, 12, 5))
    naive = last_completed_session_on(NYSE, datetime(2026, 8, 14, 12, 5))
    assert aware == naive


def test_returns_none_when_the_horizon_holds_no_session():
    """Caller treats None as "keep the previous bound", so it must not raise."""
    assert last_completed_session_on(NYSE, _utc(2026, 8, 14, 12, 5),
                                     horizon_days=0) is None


def test_result_is_tz_naive_and_normalised():
    """It is compared against tz-naive roster Timestamps; a tz would raise."""
    got = last_completed_session_on(XETR, _utc(2026, 8, 14, 12, 5))
    assert got.tz is None
    assert (got.hour, got.minute, got.second) == (0, 0, 0)
    # and it must be comparable with the roster's end_friday without error
    assert max(pd.Timestamp("2026-08-07"), got) == got


@pytest.mark.parametrize("cal", [NYSE, XETR])
def test_never_returns_a_future_session(cal):
    now = _utc(2026, 8, 14, 12, 5)
    got = last_completed_session_on(cal, now)
    assert got <= pd.Timestamp("2026-08-14")


# ---------------------------------------------------------------------------
# The tail must not run past the DATA
#
# Bug introduced by the tail extension itself and caught 2026-08-15. The bound
# was the last COMPLETED session on the venue calendar, which asks about the
# EXCHANGE. Whether the vendor has published the CONSTITUENT prices for that
# session is a different question, and on the European lines it lags by about a
# session. Run after Xetra closes but before the constituents publish, the panel
# gained a final row on which no current constituent had a price; the file-level
# coverage floor read that row, saw "0 of 8 carry a 50-day average", and refused
# to write. It presented as fourteen broken research panels whose coverage was
# in fact 100%, and every deployed Europe panel would have failed identically.
# ---------------------------------------------------------------------------

def _cap(schedule_end, end_friday, prices, held, floor=5, cov_fail=0.50,
         roster_n=None):
    """The rule as implemented in compute_breadth.main.

    The threshold is the FILE guard's own: a share of the roster, floored at
    MIN_BREADTH_NAMES. Calibrating it to the absolute floor alone let EXH3
    through at 8 of 107 priced names on 2026-08-14 — above 5, far below 50% —
    so the cap admitted a session the guard then refused.
    """
    if not held:
        return schedule_end
    n = roster_n if roster_n is not None else len(held)
    need = max(floor, int(cov_fail * n) + 1)
    covered = prices[held].notna().sum(axis=1)
    ok = covered.index[covered >= need]
    if not len(ok):
        return schedule_end
    return max(end_friday, min(schedule_end, pd.Timestamp(ok.max())))


def _px(dates, cols, last_full=None):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    df = pd.DataFrame(1.0, index=idx, columns=list(cols))
    if last_full is not None:                       # blank the tail rows
        df.loc[df.index > pd.Timestamp(last_full)] = float("nan")
    return df


def test_cap_pulls_the_panel_back_to_the_last_priced_session():
    """The live 2026-08-15 case: XETR closed on the 14th, constituents priced
    to the 13th."""
    px = _px(["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"],
             [f"T{i}" for i in range(8)], last_full="2026-08-13")
    out = _cap(pd.Timestamp("2026-08-14"), pd.Timestamp("2026-08-07"),
               px, list(px.columns))
    assert out == pd.Timestamp("2026-08-13")


def test_cap_is_inert_when_the_data_reaches_the_session():
    px = _px(["2026-08-12", "2026-08-13", "2026-08-14"],
             [f"T{i}" for i in range(8)])
    out = _cap(pd.Timestamp("2026-08-14"), pd.Timestamp("2026-08-07"),
               px, list(px.columns))
    assert out == pd.Timestamp("2026-08-14"), "must not shorten a healthy panel"


def test_cap_never_pulls_below_the_roster_friday():
    """Shortening past end_friday would delete history the old bound produced,
    turning a tail guard into a silent restatement."""
    px = _px(["2026-08-03", "2026-08-04", "2026-08-13"],
             [f"T{i}" for i in range(8)], last_full="2026-08-04")
    out = _cap(pd.Timestamp("2026-08-14"), pd.Timestamp("2026-08-07"),
               px, list(px.columns))
    assert out == pd.Timestamp("2026-08-07")


def test_a_thin_tail_row_does_not_hold_the_panel_open():
    """One straggler with a price is not coverage. The cap uses the same
    MIN_BREADTH_NAMES floor the row-level guard uses, so a single name on the
    final session cannot keep a row nothing else can support."""
    cols = [f"T{i}" for i in range(8)]
    px = _px(["2026-08-12", "2026-08-13", "2026-08-14"], cols, last_full="2026-08-13")
    px.loc[pd.Timestamp("2026-08-14"), "T0"] = 1.0     # exactly one straggler
    out = _cap(pd.Timestamp("2026-08-14"), pd.Timestamp("2026-08-07"),
               px, cols)
    assert out == pd.Timestamp("2026-08-13")


def test_no_held_names_leaves_the_bound_alone():
    px = _px(["2026-08-13", "2026-08-14"], ["A"])
    out = _cap(pd.Timestamp("2026-08-14"), pd.Timestamp("2026-08-07"), px, [])
    assert out == pd.Timestamp("2026-08-14")


def test_cap_uses_the_file_guards_threshold_not_just_a_name_count():
    """EXH3, 2026-08-14: 8 of 107 names priced. Above MIN_BREADTH_NAMES, far
    below the 50% coverage floor. Calibrating the cap to the absolute floor
    admitted the session and the guard then refused the whole panel."""
    cols = [f"T{i}" for i in range(107)]
    px = _px(["2026-08-12", "2026-08-13", "2026-08-14"], cols, last_full="2026-08-13")
    for t in cols[:8]:                              # the 8 stragglers
        px.loc[pd.Timestamp("2026-08-14"), t] = 1.0
    out = _cap(pd.Timestamp("2026-08-14"), pd.Timestamp("2026-08-07"), px, cols)
    assert out == pd.Timestamp("2026-08-13"), \
        "8 of 107 must not hold the panel open"


def test_cap_admits_a_session_that_clears_the_coverage_floor():
    cols = [f"T{i}" for i in range(107)]
    px = _px(["2026-08-12", "2026-08-13", "2026-08-14"], cols, last_full="2026-08-13")
    for t in cols[:70]:                             # 70/107 = 65%, clears 50%
        px.loc[pd.Timestamp("2026-08-14"), t] = 1.0
    out = _cap(pd.Timestamp("2026-08-14"), pd.Timestamp("2026-08-07"), px, cols)
    assert out == pd.Timestamp("2026-08-14")
