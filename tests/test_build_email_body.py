"""Tests for scripts/build_email_body.py activity collection.

Covers _collect_activity, which feeds the weekly email's 'latest rebalance
changes' table. The HTML rendering is verified by generating the email and
inspecting it; here we guard the pure move-detection + NAV-impact logic
that must stay in step with the dashboard's renderPositionsPreview
(portfolio-level NAV weights, per-move dates, and NO materiality gate —
the caller applies the 0.5%-NAV filter and computes the net over all moves).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from build_email_body import (  # noqa: E402
    _collect_activity,
    _current_week_moves,
    _order_activity,
)


def _sleeve(prev_date, prev_holdings, curr_date, curr_holdings):
    """A sleeve JSON with two trade_history entries (prior, current)."""
    def _h(pairs):
        return [{"etf": e, "weight": w} for e, w in pairs]
    return {"headline": {"trade_history": [
        {"date": prev_date, "holdings": _h(prev_holdings)},
        {"date": curr_date, "holdings": _h(curr_holdings)},
    ]}}


def test_collect_activity_detects_moves_with_nav_impact_and_dates():
    """ENTER/EXIT/RESIZE are detected; weights and nav_impact are at
    PORTFOLIO level (within-sleeve x sleeve weight); each move carries its
    own sleeve rebalance date; unchanged sleeves produce nothing."""
    sleeves = {
        "a": _sleeve("2026-06-12", [("SPY", 0.5), ("XLF", 0.5)],
                     "2026-06-26", [("SPY", 0.7), ("XLE", 0.3)]),
        "b": _sleeve("2026-06-12", [("QQQ", 1.0)], "2026-06-26", [("QQQ", 1.0)]),
        "c": _sleeve("2026-06-12", [("TAN", 1.0)], "2026-06-26", [("TAN", 1.0)]),
        "d": _sleeve("2026-06-19", [("EXH1", 1.0)], "2026-07-03", [("EXH1", 1.0)]),
    }
    rows = _collect_activity(sleeves, p22_active=False)
    by = {(r["sleeve"], r["action"], r["etf"]): r for r in rows}

    # A sleeve weight = 0.35 throughout.
    assert by[("A", "ENTER", "XLE")]["new"] == pytest.approx(0.3 * 0.35)
    assert by[("A", "ENTER", "XLE")]["nav_impact"] == pytest.approx(0.3 * 0.35)
    assert by[("A", "ENTER", "XLE")]["date"] == "2026-06-26"
    assert by[("A", "ENTER", "XLE")]["prev"] is None

    assert by[("A", "EXIT", "XLF")]["prev"] == pytest.approx(0.5 * 0.35)
    assert by[("A", "EXIT", "XLF")]["new"] is None
    assert by[("A", "EXIT", "XLF")]["nav_impact"] == pytest.approx(0.5 * 0.35)

    # SPY resized 0.5 -> 0.7 within-sleeve => ΔNAV = 0.2 * 0.35.
    assert by[("A", "RESIZE", "SPY")]["nav_impact"] == pytest.approx(0.2 * 0.35)
    assert by[("A", "RESIZE", "SPY")]["prev"] == pytest.approx(0.5 * 0.35)
    assert by[("A", "RESIZE", "SPY")]["new"] == pytest.approx(0.7 * 0.35)

    # B/C/D holdings unchanged -> no rows from them.
    assert all(r["sleeve"] == "A" for r in rows)


def test_collect_activity_keeps_sub_half_percent_moves():
    """No materiality gate lives in the collector: sub-0.5%-NAV moves are
    returned (the caller filters). SPY 0.500 -> 0.505 within-sleeve is only
    0.5pp x 0.35 = 0.175% NAV, well under 0.5%, but must still appear."""
    sleeves = {
        "a": _sleeve("2026-06-12", [("SPY", 0.5), ("QQQ", 0.5)],
                     "2026-06-26", [("SPY", 0.505), ("QQQ", 0.495)]),
        "b": _sleeve("2026-06-12", [("IWM", 1.0)], "2026-06-26", [("IWM", 1.0)]),
        "c": _sleeve("2026-06-12", [("TAN", 1.0)], "2026-06-26", [("TAN", 1.0)]),
        "d": _sleeve("2026-06-12", [("EXH1", 1.0)], "2026-06-26", [("EXH1", 1.0)]),
    }
    resizes = [r for r in _collect_activity(sleeves, p22_active=False)
               if r["action"] == "RESIZE"]
    assert {r["etf"] for r in resizes} == {"SPY", "QQQ"}
    assert all(r["nav_impact"] < 0.005 for r in resizes)  # sub-threshold, kept


def test_collect_activity_eem_tilt_scales_sleeve_b_weight():
    """When the EEM tilt is ON, sleeve B drops from 35% to 25% NAV, so a
    B-sleeve move's nav_impact scales accordingly."""
    sleeves = {
        "a": _sleeve("2026-06-12", [("SPY", 1.0)], "2026-06-26", [("SPY", 1.0)]),
        "b": _sleeve("2026-06-12", [("QQQ", 0.5)], "2026-06-26", [("QQQ", 1.0)]),
        "c": _sleeve("2026-06-12", [("TAN", 1.0)], "2026-06-26", [("TAN", 1.0)]),
        "d": _sleeve("2026-06-12", [("EXH1", 1.0)], "2026-06-26", [("EXH1", 1.0)]),
    }
    off = {r["etf"]: r for r in _collect_activity(sleeves, p22_active=False)}
    assert off["QQQ"]["nav_impact"] == pytest.approx(0.5 * 0.35)  # B at 35%
    on = {r["etf"]: r for r in _collect_activity(sleeves, p22_active=True)}
    assert on["QQQ"]["nav_impact"] == pytest.approx(0.5 * 0.25)   # B at 25%


def _resize(etf, prev, new, sleeve="B"):
    return {"sleeve": sleeve, "action": "RESIZE", "etf": etf, "prev": prev,
            "new": new, "date": "2026-07-17", "nav_impact": abs(new - prev)}


def test_order_activity_ranks_by_magnitude_not_resulting_size():
    """The card must lead with the BIGGEST MOVE, not the biggest resulting
    position. Regression for the 2026-07-18 owner report: IUFS resized
    0.5% -> 3.9% (+3.4pp, by far the largest change) but rendered fourth,
    below DBC 3.0% -> 4.0%, because rows were sorted by resulting weight
    (4.0 > 3.9 > 3.8 > 3.6)."""
    rows = [
        _resize("DBC", 0.030, 0.040),   # +1.0pp, biggest RESULTING weight
        _resize("IUFS", 0.005, 0.039),  # +3.4pp, biggest MOVE
        _resize("QQQ", 0.050, 0.038),   # -1.2pp
        _resize("VNQ", 0.025, 0.036),   # +1.1pp
    ]
    assert [a["etf"] for a in _order_activity(rows)] == ["IUFS", "QQQ", "VNQ", "DBC"]


def test_order_activity_keeps_action_groups_and_ranks_within_them():
    """Action grouping (ENTER, then EXIT, then RESIZE) still wins over
    magnitude, so a small ENTER stays above a large RESIZE; magnitude only
    orders rows WITHIN a group. Direction is irrelevant — a big trim
    outranks a small add."""
    rows = [
        _resize("BIGTRIM", 0.090, 0.010),   # RESIZE, -8.0pp
        _resize("SMALLADD", 0.010, 0.012),  # RESIZE, +0.2pp
        {"sleeve": "A", "action": "ENTER", "etf": "NEWCO", "prev": None,
         "new": 0.004, "date": "2026-07-17", "nav_impact": 0.004},
        {"sleeve": "A", "action": "EXIT", "etf": "GONE", "prev": 0.070,
         "new": None, "date": "2026-07-17", "nav_impact": 0.070},
    ]
    assert [a["etf"] for a in _order_activity(rows)] == [
        "NEWCO", "GONE", "BIGTRIM", "SMALLADD"]


def _move(sleeve, date, etf="XYZ"):
    return {"sleeve": sleeve, "action": "RESIZE", "etf": etf,
            "prev": 0.05, "new": 0.06, "date": date, "nav_impact": 0.01}


def test_current_week_moves_filters_to_latest_date_and_footnotes_rest():
    """Mixed-date activity (the 2026-07-17 build: A/B/D on 07-17, C still
    on 07-10) keeps only the latest date's rows; older-dated sleeves come
    back as (sleeve, date) footnote pairs, deduplicated."""
    activity = [
        _move("A", "2026-07-17", "SOXX"),
        _move("B", "2026-07-17", "IJR"),
        _move("D", "2026-07-17", "EXV1"),
        _move("C", "2026-07-10", "CIBR"),
        _move("C", "2026-07-10", "PAVE"),
    ]
    latest, current, stale = _current_week_moves(activity)
    assert latest == "2026-07-17"
    assert {a["etf"] for a in current} == {"SOXX", "IJR", "EXV1"}
    assert stale == [("C", "2026-07-10")]  # two C moves -> one footnote


def test_current_week_moves_single_date_has_no_footnote():
    activity = [_move("A", "2026-07-17"), _move("B", "2026-07-17")]
    latest, current, stale = _current_week_moves(activity)
    assert latest == "2026-07-17"
    assert len(current) == 2
    assert stale == []


def test_current_week_moves_month_and_year_boundaries():
    """ISO date strings must order correctly across month and year ends
    (string max is only safe because the format is YYYY-MM-DD)."""
    # Month boundary: 2026-07-31 vs 2026-08-07.
    latest, current, stale = _current_week_moves(
        [_move("A", "2026-07-31"), _move("B", "2026-08-07")])
    assert latest == "2026-08-07"
    assert stale == [("A", "2026-07-31")]
    # Year boundary: 2026-12-31 vs 2027-01-08.
    latest, current, stale = _current_week_moves(
        [_move("C", "2026-12-31"), _move("D", "2027-01-08")])
    assert latest == "2027-01-08"
    assert stale == [("C", "2026-12-31")]


def test_current_week_moves_empty_and_undated():
    assert _current_week_moves([]) == (None, [], [])
    undated = [_move("A", "")]  # corrupt input: no dates at all
    assert _current_week_moves(undated) == (None, [], [])
