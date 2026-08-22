"""The deployed cadence is Monday, forward-rolled. Pinned, not assumed.

ADOPTED 2026-08-22 by WS18 (reviews/2026-08-22_prereg_ws18_monday-cadence.md).

WHY A TEST AND NOT A COMMENT. The cadence lives in five places — a constant in
each of four engines plus the calendar's DEFAULT_MODE — and the study that
moved it turned on the interaction between two of them. A future session
editing one engine, or reverting DEFAULT_MODE to fix something unrelated,
would produce a book that is Monday in three sleeves and Friday in the fourth,
or Monday with a backward roll. Neither fails loudly. Both are wrong.

WHY THE MODE IS PART OF THE SAME CONTRACT. Under HOLIDAY_AWARE a holiday Monday
rolls BACK three days onto the previous Friday (39 of 406 rebalances on NYSE).
The Monday cadence decides on Saturday from Friday's close, so that fill would
precede the decision producing it — the backtest would credit trades nobody
could place, on 9.6% of weeks for 70% of NAV. Register record
2026-08-22-breadth-thrust-etf-2 records that as rejected on structural grounds.
So "Monday" and "forward roll" are one decision and are tested as one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import rebalance_calendar as rc  # noqa: E402

ENGINES = ["run_topk_robustness", "run_asset_class_rotation",
           "run_thematic_rotation", "run_europe_rotation"]


@pytest.mark.parametrize("module", ENGINES)
def test_every_engine_rebalances_on_monday(module):
    import importlib
    m = importlib.import_module(module)
    assert m.HEADLINE_FREQ == "W-MON", (
        f"{module} is on {m.HEADLINE_FREQ}; a book that is Monday in some "
        f"sleeves and Friday in others fails nothing and is wrong")


@pytest.mark.parametrize("module", ENGINES)
def test_the_label_matches_the_cadence(module):
    """A label saying Friday over a Monday book is the kind of confidently
    wrong statement that reaches a reader unchallenged."""
    import importlib
    m = importlib.import_module(module)
    assert m.HEADLINE_FREQ_NAME == "Weekly Mon"


def test_the_calendar_rolls_forward():
    assert rc.DEFAULT_MODE == rc.HOLIDAY_AWARE_NEXT


def test_a_holiday_monday_does_not_roll_back_onto_the_prior_friday():
    """The structural defect the mode change exists for.

    Memorial Day, Monday 2019-05-27: NYSE shut. Under a backward roll the
    rebalance lands on Friday 2019-05-24 — before the Saturday decision that
    produces it. It must land AFTER the scheduled Monday, not before.
    """
    sched = mcal.get_calendar("NYSE").schedule(start_date="2019-05-01",
                                               end_date="2019-06-15")
    idx = pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])
    rd = rc.weekly_rebalance_dates(idx, idx[0], "W-MON",
                                   mode=rc.DEFAULT_MODE, calendar="NYSE")
    holiday = pd.Timestamp("2019-05-27")
    assert holiday not in set(idx), "fixture assumes Memorial Day is shut"
    near = [d for d in rd if abs((pd.Timestamp(d) - holiday).days) <= 4]
    assert near, "the holiday week produced no rebalance at all"
    landed = min(near, key=lambda x: abs((pd.Timestamp(x) - holiday).days))
    assert pd.Timestamp(landed) > holiday, (
        f"rolled BACK to {pd.Timestamp(landed).date()} — a fill before the "
        f"decision that produces it")


def test_no_rebalance_lacks_a_prior_session():
    """The look-ahead invariant, re-checked because the roll direction moved.
    Every engine ranks at get_loc(rd) - 1, so a rebalance on the first index
    entry would rank on nothing."""
    for venue in ("NYSE", "XETR"):
        sched = mcal.get_calendar(venue).schedule(start_date="2018-11-08",
                                                  end_date="2026-08-21")
        idx = pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])
        rd = rc.weekly_rebalance_dates(idx, idx[0], "W-MON",
                                       mode=rc.DEFAULT_MODE, calendar=venue)
        assert all(idx.get_loc(d) - 1 >= 0 for d in rd), venue


def test_the_weekly_chart_resample_is_untouched():
    """build_panel_series resamples to W-FRI for weekly CHART BARS and
    fetch_constituents builds a W-FRI grid because iShares publishes Friday
    rosters. Neither is the rebalance cadence, and a sweep that changed them
    would corrupt the panels while looking like a tidy rename."""
    for rel in ("scripts/build_panel_series.py", "scripts/fetch_constituents.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert '"W-FRI"' in src, f"{rel} lost its W-FRI grid/resample"


@pytest.mark.parametrize("module", ENGINES)
def test_the_headline_cell_exists_in_the_engine_grid(module):
    """main() captures headline_payload by matching HEADLINE_FREQ_NAME against
    REBAL_FREQS. If the name is not in the grid the payload stays None and the
    engine dies thirty lines later on a subscript of None — which is what
    happened on the WS18 adoption run, in all four engines at once.

    The constant and the grid are one decision. This asserts the contract
    where it lives, rather than leaving it to be discovered downstream.
    """
    import importlib
    m = importlib.import_module(module)
    names = [n for n, _ in m.REBAL_FREQS]
    assert m.HEADLINE_FREQ_NAME in names, (
        f"{module}: headline {m.HEADLINE_FREQ_NAME!r} absent from grid {names} "
        f"— headline_payload would stay None")
    freqs = dict(m.REBAL_FREQS)
    assert freqs[m.HEADLINE_FREQ_NAME] == m.HEADLINE_FREQ, (
        f"{module}: grid maps {m.HEADLINE_FREQ_NAME!r} to "
        f"{freqs[m.HEADLINE_FREQ_NAME]!r}, constant says {m.HEADLINE_FREQ!r}")


@pytest.mark.parametrize("module", ENGINES)
def test_the_incumbent_cadence_stays_in_the_grid_as_a_comparison(module):
    """Weekly Fri is kept deliberately. After a cadence move the incumbent is
    the most useful row in the engine's own diagnostic table, and dropping it
    would make the restatement harder to audit later, not easier."""
    import importlib
    m = importlib.import_module(module)
    assert "Weekly Fri" in dict(m.REBAL_FREQS)
