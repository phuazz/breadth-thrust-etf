"""The latest rebalance must be decided on the session its venue closed before it.

REPRODUCES 2026-08-28. yfinance served no Friday bar for ten of thirteen
sleeve-B lines and for SHY; sleeve B drops any row with a gap and sleeve C
takes its calendar from SHY, so both panels lost the session outright, and the
2026-08-31 rebalance was published decided on Thursday 2026-08-27. Every guard
in the repo measured against the panel's own index and passed. These tests
drive the venue-calendar check through the shapes that matter: the absent
decision session (FAIL), the hollow decision row (FAIL for a price-signal
engine, WARN for a breadth engine), an older interior gap (WARN only), and the
two calendar boundaries a false positive would come from — a holiday Friday and
the year end.

Calendar facts below come from pandas_market_calendars, not from memory:
  - 2026-08-28 (Fri) and 2026-08-31 (Mon) are normal NYSE sessions.
  - 2026-07-03 (Fri) is a NYSE holiday (Independence Day observed);
    2026-07-02 (Thu) is the session before Mon 2026-07-06.
  - 2027-01-01 (Fri) is a NYSE holiday; 2026-12-31 (Thu) is the session
    before Mon 2027-01-04.
  - 2026-05-29 (Fri) is the session before Mon 2026-06-01 (month boundary).

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_refresh_guard as guard  # noqa: E402
import price_panel_guard as g  # noqa: E402
from rebalance_calendar import _exchange_sessions  # noqa: E402


def _nyse_panel(start: str, end: str, members=("AAA", "BBB", "CCC"), seed=0):
    sessions = sorted(_exchange_sessions("NYSE", start, end))
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in sessions])
    rng = np.random.default_rng(seed)
    data = {m: 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(idx))))
            for m in members}
    return pd.DataFrame(data, index=idx)


def _report(panel, **kw):
    return g.decision_session_report(panel, "NYSE", "W-MON", panel.index[0], **kw)


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------
def test_absent_decision_session_fails_and_names_the_session():
    panel = _nyse_panel("2026-03-02", "2026-09-01")
    holed = panel.drop(pd.Timestamp("2026-08-28"))          # the withheld Friday
    rep = _report(holed)
    assert rep["rebalance_date"] == "2026-08-31"
    assert rep["expected_decision"] == "2026-08-28"
    assert rep["present"] is False
    assert rep["status"] == g.FAIL
    assert "2026-08-27" in rep["reasons"][0], "must say which session the engine would use instead"
    with pytest.raises(g.DegeneratePriceError) as exc:
        g.assert_decision_session_present(holed, "NYSE", "W-MON", holed.index[0],
                                          "Strategy B closes")
    assert "2026-08-28" in str(exc.value)


def test_hollow_decision_row_fails_a_price_signal_engine():
    panel = _nyse_panel("2026-03-02", "2026-09-01")
    panel.loc[pd.Timestamp("2026-08-28"), "BBB"] = np.nan
    rep = _report(panel)
    assert rep["status"] == g.FAIL
    assert rep["hollow_members"] == ["BBB"]


def test_hollow_decision_row_only_warns_a_breadth_engine():
    """Sleeves A and D rank on breadth: an unpriced member mis-marks a day,
    it does not mis-decide the rebalance."""
    panel = _nyse_panel("2026-03-02", "2026-09-01")
    panel.loc[pd.Timestamp("2026-08-28"), "BBB"] = np.nan
    rep = _report(panel, hollow_is_fail=False)
    assert rep["status"] == g.PASS
    assert rep["hollow_members"] == ["BBB"]
    assert any("mis-marked" in w for w in rep["warnings"])
    g.assert_decision_session_present(panel, "NYSE", "W-MON", panel.index[0],
                                      "Strategy A closes", hollow_is_fail=False)


def test_an_older_interior_gap_warns_but_does_not_fail():
    panel = _nyse_panel("2026-03-02", "2026-09-01")
    holed = panel.drop(pd.Timestamp("2026-08-14"))           # a Friday two weeks back
    rep = _report(holed)
    assert rep["status"] == g.PASS
    assert rep["present"] is True
    assert "2026-08-14" in rep["missing_sessions"]
    assert any("2026-08-14" in w for w in rep["warnings"])


def test_a_complete_panel_passes_with_nothing_missing():
    rep = _report(_nyse_panel("2026-03-02", "2026-09-01"))
    assert rep["status"] == g.PASS
    assert rep["missing_sessions"] == []
    assert rep["warnings"] == []


# ---------------------------------------------------------------------------
# The boundaries a false positive would come from
# ---------------------------------------------------------------------------
def test_holiday_friday_ranks_on_thursday_and_is_not_a_gap():
    """Mon 2026-07-06 follows the Independence Day Friday: the decision
    session is Thu 2026-07-02, and 2026-07-03 is not a missing session."""
    panel = _nyse_panel("2026-01-05", "2026-07-06")
    rep = _report(panel)
    assert rep["rebalance_date"] == "2026-07-06"
    assert rep["expected_decision"] == "2026-07-02"
    assert rep["status"] == g.PASS
    assert "2026-07-03" not in rep["missing_sessions"]


def test_year_boundary_ranks_on_the_last_session_of_the_old_year():
    panel = _nyse_panel("2026-07-01", "2027-01-04")
    rep = _report(panel)
    assert rep["rebalance_date"] == "2027-01-04"
    assert rep["expected_decision"] == "2026-12-31"
    assert rep["status"] == g.PASS


def test_month_boundary_ranks_on_the_last_session_of_the_old_month():
    panel = _nyse_panel("2026-01-05", "2026-06-01")
    rep = _report(panel)
    assert rep["rebalance_date"] == "2026-06-01"
    assert rep["expected_decision"] == "2026-05-29"
    assert rep["status"] == g.PASS


def test_a_panel_that_stops_short_of_friday_is_not_this_guards_question():
    """Saturday shape with the Friday withheld at the TAIL: the last rebalance
    on the panel is the previous Monday, decided on the previous Friday, which
    is present. The tail is live_targets' and G1's question."""
    panel = _nyse_panel("2026-03-02", "2026-08-27")
    rep = _report(panel)
    assert rep["rebalance_date"] == "2026-08-24"
    assert rep["expected_decision"] == "2026-08-21"
    assert rep["status"] == g.PASS


def test_no_rebalance_in_window_skips():
    panel = _nyse_panel("2026-08-25", "2026-08-28")
    rep = g.decision_session_report(panel, "NYSE", "W-MON",
                                    pd.Timestamp("2026-09-01"))
    assert rep["status"] == g.SKIP


# ---------------------------------------------------------------------------
# Every engine calls it. The first restatement run on 2026-09-03 died on a
# NameError in sleeve C because the call was inserted without its import;
# the other three engines had it. Pinned so the four cannot drift again.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", ["run_topk_robustness", "run_asset_class_rotation",
                                    "run_thematic_rotation", "run_europe_rotation"])
def test_every_engine_imports_and_calls_the_guard(module):
    import importlib
    import inspect
    m = importlib.import_module(module)
    assert m.assert_decision_session_present is g.assert_decision_session_present
    assert "assert_decision_session_present(" in inspect.getsource(m.main)
    assert m.latest_rebalance_record is not None


# ---------------------------------------------------------------------------
# The refresh-guard verdict (G7)
# ---------------------------------------------------------------------------
def test_g7_fails_the_holed_cache_and_passes_the_complete_one():
    complete = _nyse_panel("2026-03-02", "2026-09-01")
    holed = complete.drop(pd.Timestamp("2026-08-28"))
    results = guard.check_decision_sessions({"B": holed, "C": complete})
    by = {r["check"]: r for r in results}
    assert by["G7 decision session B"]["status"] == guard.FAIL
    assert "2026-08-28" in by["G7 decision session B"]["evidence"]
    assert by["G7 decision session C"]["status"] == guard.OK


def test_g7_warns_when_the_cache_is_not_on_this_machine():
    (r,) = guard.check_decision_sessions({"B": None})
    assert r["status"] == guard.WARN
    assert "not readable" in r["evidence"]


def test_g7_warns_on_an_older_gap_without_failing():
    complete = _nyse_panel("2026-03-02", "2026-09-01")
    (r,) = guard.check_decision_sessions({"C": complete.drop(pd.Timestamp("2026-08-14"))})
    assert r["status"] == guard.WARN
    assert "2026-08-14" in r["evidence"]
