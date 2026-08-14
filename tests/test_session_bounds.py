"""Partial bars, holed sessions, and which session a decision ranked on.

Python months are 1-indexed (January = 1). Every literal below is 1-indexed.

THE INCIDENT. On 2026-08-14 at 13:15 UTC, with Xetra still two hours from its
15:30 close, yfinance served a bar stamped that day for the .DE lines.
Strategy D ranked on it. Separately, Thursday 13 August — a real Xetra session
— was absent from the vendor's series, so the engine's
``decision_date = full_idx[i - 1]`` silently fell back to Wednesday 12 August.
On Wednesday EXV3 breadth (73.6) beat EXH3 (71.6); by Thursday it had reversed
(EXH3 73.0, EXV3 71.7). A 1.3pp call, decided by the wrong session, with no
error anywhere.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from session_bounds import (  # noqa: E402
    decision_session_report,
    last_completed_session_on,
    trim_to_completed,
)

NYSE = mcal.get_calendar("NYSE")
XETR = mcal.get_calendar("XETR")


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _frame(dates, cols=("A", "B")):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame({c: range(len(idx)) for c in cols}, index=idx)


# ---------------------------------------------------------------------------
# trim_to_completed
# ---------------------------------------------------------------------------

def test_drops_the_in_progress_bar_the_vendor_served():
    """The exact 2026-08-14 frame, at the exact time it was observed."""
    df = _frame(["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-14"])
    out, dropped = trim_to_completed(df, XETR, _utc(2026, 8, 14, 13, 15))
    assert [str(d) for d in dropped] == ["2026-08-14"]
    assert out.index.max() == pd.Timestamp("2026-08-12")


def test_keeps_that_bar_once_the_session_has_closed():
    """Xetra closes 15:30 UTC. At 16:00 the same bar is a legitimate close."""
    df = _frame(["2026-08-12", "2026-08-14"])
    out, dropped = trim_to_completed(df, XETR, _utc(2026, 8, 14, 16, 0))
    assert dropped == []
    assert out.index.max() == pd.Timestamp("2026-08-14")


def test_the_minute_either_side_of_the_close():
    df = _frame(["2026-08-13", "2026-08-14"])
    before, _ = trim_to_completed(df, XETR, _utc(2026, 8, 14, 15, 29))
    after, _ = trim_to_completed(df, XETR, _utc(2026, 8, 14, 15, 31))
    assert before.index.max() == pd.Timestamp("2026-08-13")
    assert after.index.max() == pd.Timestamp("2026-08-14")


def test_historical_frames_are_untouched():
    """It removes a TAIL or nothing. A guard that could alter history would
    silently restate a backtest, which is worse than the bug it fixes."""
    dates = [d.strftime("%Y-%m-%d") for d in
             pd.bdate_range("2024-01-02", "2026-06-30")]
    df = _frame(dates)
    out, dropped = trim_to_completed(df, NYSE, _utc(2026, 8, 14, 13, 15))
    assert dropped == []
    assert out.equals(df)


def test_venue_awareness_is_the_whole_point():
    """US Independence Day, Fri 3 Jul 2026, 16:00 UTC — Xetra closed at 15:30,
    NYSE never opened. An NYSE-only cap truncates a completed European
    session; that is why cap_to_last_completed_session could not serve
    Strategy D."""
    df = _frame(["2026-07-02", "2026-07-03"])
    xe, xd = trim_to_completed(df, XETR, _utc(2026, 7, 3, 16, 0))
    ny, nd = trim_to_completed(df, NYSE, _utc(2026, 7, 3, 16, 0))
    assert xe.index.max() == pd.Timestamp("2026-07-03") and xd == []
    assert ny.index.max() == pd.Timestamp("2026-07-02")
    assert [str(d) for d in nd] == ["2026-07-03"]


def test_empty_and_tz_aware_frames():
    empty = pd.DataFrame(index=pd.DatetimeIndex([]))
    out, dropped = trim_to_completed(empty, NYSE, _utc(2026, 8, 14, 13, 15))
    assert len(out) == 0 and dropped == []

    aware = _frame(["2026-08-12", "2026-08-14"])
    aware.index = aware.index.tz_localize("UTC")
    out, dropped = trim_to_completed(aware, XETR, _utc(2026, 8, 14, 13, 15))
    assert [str(d) for d in dropped] == ["2026-08-14"]


def test_works_on_a_series_not_only_a_frame():
    s = pd.Series([1, 2, 3], index=pd.DatetimeIndex(
        ["2026-08-11", "2026-08-12", "2026-08-14"]))
    out, dropped = trim_to_completed(s, XETR, _utc(2026, 8, 14, 13, 15))
    assert len(out) == 2 and [str(d) for d in dropped] == ["2026-08-14"]


# ---------------------------------------------------------------------------
# decision_session_report
# ---------------------------------------------------------------------------

def test_names_the_hole_that_redated_the_decision():
    """The real EXV1/EXH1/EXV3 index: 13 Aug absent, so a ranking taken on
    Friday morning uses Wednesday. Previously silent."""
    df = _frame(["2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12"])
    rep = decision_session_report(df, XETR, _utc(2026, 8, 14, 13, 15))
    assert rep["expected_decision_session"] == pd.Timestamp("2026-08-13").date()
    assert rep["last_bar"] == pd.Timestamp("2026-08-12").date()
    assert rep["reaches_decision_session"] is False
    assert pd.Timestamp("2026-08-13").date() in rep["missing_recent_sessions"]


def test_a_healthy_panel_reports_clean():
    dates = [d.strftime("%Y-%m-%d") for d in
             mcal.get_calendar("NYSE").schedule(
                 start_date="2026-06-01", end_date="2026-08-13").index]
    rep = decision_session_report(_frame(dates), NYSE, _utc(2026, 8, 14, 13, 15))
    assert rep["reaches_decision_session"] is True
    assert rep["missing_recent_sessions"] == []


def test_a_gap_further_back_does_not_mask_a_healthy_tail():
    """Reaching the decision session is a different question from having no
    gaps, and the report answers both separately."""
    dates = [d.strftime("%Y-%m-%d") for d in
             NYSE.schedule(start_date="2026-06-01", end_date="2026-08-13").index]
    dates.remove("2026-08-05")
    rep = decision_session_report(_frame(dates), NYSE, _utc(2026, 8, 14, 13, 15))
    assert rep["reaches_decision_session"] is True
    assert pd.Timestamp("2026-08-05").date() in rep["missing_recent_sessions"]


# ---------------------------------------------------------------------------
# The NYSE wrapper must not drift from the shared implementation
# ---------------------------------------------------------------------------

def test_nyse_wrapper_delegates_and_agrees():
    from nyse_sessions import cap_to_last_completed_session, last_completed_session
    df = _frame(["2026-08-12", "2026-08-13", "2026-08-14"])
    when = _utc(2026, 8, 14, 13, 15)
    assert cap_to_last_completed_session(df, when).index.max() \
        == pd.Timestamp("2026-08-13")
    assert last_completed_session(when) == last_completed_session_on(NYSE, when).date()


# ---------------------------------------------------------------------------
# Every engine must record the session it ranked on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", [
    "run_topk_robustness", "run_asset_class_rotation",
    "run_thematic_rotation", "run_europe_rotation",
])
def test_every_engine_records_its_decision_date(module):
    """All four computed decision_date and discarded it, so a rebalance could
    not say which session decided it. Without this the 12-vs-13 August
    substitution is unreadable from the output."""
    import importlib
    import inspect
    m = importlib.import_module(module)
    src = inspect.getsource(m.build_trade_history)
    assert '"decision_date"' in src, (
        f"{module}.build_trade_history must emit decision_date")
    assert "decision_date.strftime" in src


# ---------------------------------------------------------------------------
# The book must not be assembled from mixed vintages
# ---------------------------------------------------------------------------

def _sleeves(dates_by_sleeve):
    out = {}
    for k, trades in dates_by_sleeve.items():
        out[k] = {"headline": {"trade_history": [
            {"date": d, "decision_date": dd,
             "holdings": [{"etf": f"{k.upper()}X", "weight": 1.0}]}
            for d, dd in trades]}}
    return out


def _overlay():
    return {"gate_parameters": {}, "phase22_eem_tilt": {}}


def test_book_never_takes_a_trade_after_the_asof_date():
    """The 2026-08-14 shape: D had run ahead on a partial bar while A sat on
    7 Aug. trades[-1] composed a book nobody ever held."""
    import build_factsheet as bf
    sleeves = _sleeves({
        "a": [("2026-07-31", "2026-07-30"), ("2026-08-07", "2026-08-06")],
        "d": [("2026-08-07", "2026-08-06"), ("2026-08-14", "2026-08-12")],
    })
    hold = bf._collect_deployed_holdings(sleeves, _overlay(), "2026-08-13")
    by = {h["sleeve"]: h for h in hold if h["sleeve"] in ("A", "D")}
    assert by["A"]["rebalance_date"] == "2026-08-07"
    assert by["D"]["rebalance_date"] == "2026-08-07", (
        "a 14 Aug trade must not appear in a book as of 13 Aug")


def test_book_surfaces_the_session_each_sleeve_ranked_on():
    import build_factsheet as bf
    sleeves = _sleeves({
        "a": [("2026-08-07", "2026-08-06")],
        "d": [("2026-08-07", "2026-08-05")],   # holed session -> earlier rank
    })
    hold = bf._collect_deployed_holdings(sleeves, _overlay(), "2026-08-13")
    by = {h["sleeve"]: h for h in hold if h["sleeve"] in ("A", "D")}
    assert by["A"]["decided_on"] == "2026-08-06"
    assert by["D"]["decided_on"] == "2026-08-05"


def test_legitimately_lagging_sleeve_is_kept_not_dropped():
    """Sleeve C only trades when its basket changes, so sitting weeks behind
    is normal. The fix must not mistake that for staleness and drop it."""
    import build_factsheet as bf
    sleeves = _sleeves({
        "a": [("2026-08-07", "2026-08-06")],
        "c": [("2026-07-31", "2026-07-30")],
    })
    hold = bf._collect_deployed_holdings(sleeves, _overlay(), "2026-08-13")
    assert {h["sleeve"] for h in hold} >= {"A", "C"}
    assert next(h for h in hold if h["sleeve"] == "C")["rebalance_date"] \
        == "2026-07-31"


def test_no_asof_keeps_the_previous_behaviour():
    import build_factsheet as bf
    sleeves = _sleeves({"a": [("2026-08-07", "2026-08-06"),
                              ("2026-08-14", "2026-08-13")]})
    hold = bf._collect_deployed_holdings(sleeves, _overlay(), None)
    assert next(h for h in hold if h["sleeve"] == "A")["rebalance_date"] \
        == "2026-08-14"
