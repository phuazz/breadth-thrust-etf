"""Tests for scripts/check_refresh_guard.py — the post-refresh guard layer.

Only the pure verdict logic is tested: synthetic panel dicts stand in for
the on-disk JSON, and git access is not exercised (load_committed_json is
integration plumbing; its None path is covered via check_no_lost_state's
old_breadth=None handling).

Calendar expectations below were derived with pandas_market_calendars on
2026-08-08, not from memory:
  - 2026-08-07 (Fri) is a normal NYSE and XETR session.
  - 2026-07-03 (Fri) is a NYSE holiday (Independence Day observed) but a
    normal XETR session — the mixed-calendar boundary.
  - 2027-01-01 (Fri) is a holiday on both; last NYSE session 2026-12-31,
    last XETR session 2026-12-30 (Xetra closes New Year's Eve).
  - 2026-05-01 (Fri) is a normal NYSE session but a XETR holiday
    (Labour Day); last XETR session 2026-04-30.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_refresh_guard as guard  # noqa: E402


def statuses(results):
    return [r["status"] for r in results]


# ---------------------------------------------------------------------------
# G1 — shared end_friday
# ---------------------------------------------------------------------------
def test_g1_ok_when_all_panels_share_the_expected_friday():
    ends = {"CSP1": "2026-08-07", "SOXX": "2026-08-07", "EXV1": "2026-08-07"}
    (r,) = guard.check_shared_end_friday(ends, date(2026, 8, 7))
    assert r["status"] == guard.OK


def test_g1_fails_when_panels_disagree():
    ends = {"CSP1": "2026-08-07", "SOXX": "2026-07-31"}
    (r,) = guard.check_shared_end_friday(ends, date(2026, 8, 7))
    assert r["status"] == guard.FAIL
    assert "SOXX" in r["evidence"]


def test_g1_fails_when_unanimous_but_wrong_week():
    # A refresh that ran a week late agrees with itself and is still wrong.
    ends = {"CSP1": "2026-07-31", "SOXX": "2026-07-31"}
    (r,) = guard.check_shared_end_friday(ends, date(2026, 8, 7))
    assert r["status"] == guard.FAIL
    assert "wrong week" in r["evidence"]


# ---------------------------------------------------------------------------
# G2 / G3 — endpoint health and staleness
# ---------------------------------------------------------------------------
def test_g2_fails_on_any_unhealthy_endpoint():
    (r,) = guard.check_endpoint_health({"CSP1": "ok", "SOXX": "unavailable"})
    assert r["status"] == guard.FAIL
    assert "SOXX" in r["evidence"]


def test_g2_ok_when_all_healthy():
    (r,) = guard.check_endpoint_health({"CSP1": "ok", "SOXX": "ok"})
    assert r["status"] == guard.OK


def test_g3_critical_fails_warning_warns_fresh_passes():
    (r,) = guard.check_staleness({"A": "fresh", "B": "critical"})
    assert r["status"] == guard.FAIL
    (r,) = guard.check_staleness({"A": "fresh", "B": "warning"})
    assert r["status"] == guard.WARN
    (r,) = guard.check_staleness({"A": "fresh", "B": "fresh"})
    assert r["status"] == guard.OK


def test_g3_no_real_fetches_fails():
    (r,) = guard.check_staleness({"A": "no_real_fetches"})
    assert r["status"] == guard.FAIL


# ---------------------------------------------------------------------------
# G4 — breadth panel end dates on each ETF's own calendar
# ---------------------------------------------------------------------------
def test_expected_panel_end_normal_friday_both_calendars():
    assert guard.expected_panel_end("NYSE", date(2026, 8, 7)) == date(2026, 8, 7)
    assert guard.expected_panel_end("XETR", date(2026, 8, 7)) == date(2026, 8, 7)


def test_expected_panel_end_us_holiday_friday_mixed_calendars():
    # 2026-07-03: NYSE closed, XETR open — the boundary that collapsed the
    # live track before the 0a95173 guard fix.
    assert guard.expected_panel_end("NYSE", date(2026, 7, 3)) == date(2026, 7, 2)
    assert guard.expected_panel_end("XETR", date(2026, 7, 3)) == date(2026, 7, 3)


def test_expected_panel_end_year_boundary():
    # Fri 2027-01-01: both closed; the two calendars re-open from
    # DIFFERENT last sessions (Xetra closes New Year's Eve).
    assert guard.expected_panel_end("NYSE", date(2027, 1, 1)) == date(2026, 12, 31)
    assert guard.expected_panel_end("XETR", date(2027, 1, 1)) == date(2026, 12, 30)


def test_expected_panel_end_month_boundary_europe_holiday():
    # Fri 2026-05-01 (Labour Day): NYSE open, XETR closed — the mirror
    # image of the US-holiday case, across a month boundary.
    assert guard.expected_panel_end("NYSE", date(2026, 5, 1)) == date(2026, 5, 1)
    assert guard.expected_panel_end("XETR", date(2026, 5, 1)) == date(2026, 4, 30)


def test_g4_ok_when_each_panel_ends_on_its_own_calendar():
    ends = {"CSP1": "2026-07-02", "EXV1": "2026-07-03"}
    cals = {"CSP1": "NYSE", "EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3))
    assert r["status"] == guard.OK


def test_g4_fails_when_a_panel_stops_a_session_short():
    ends = {"CSP1": "2026-08-06", "EXV1": "2026-08-07"}
    cals = {"CSP1": "NYSE", "EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 8, 7))
    assert r["status"] == guard.FAIL
    assert "CSP1" in r["evidence"]


def test_g4_fails_when_us_panel_carries_a_europe_only_phantom_bar():
    # A NYSE-calendar panel dated the US-holiday Friday itself would mean
    # a phantom bar leaked in (the 2026-06-22 / 2026-07-06 class).
    ends = {"CSP1": "2026-07-03"}
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3))
    assert r["status"] == guard.FAIL


# ---------------------------------------------------------------------------
# G5 — nothing the previous commit had may vanish
# ---------------------------------------------------------------------------
def test_g5_silent_snapshot_loss_fails():
    out = guard.check_no_lost_state(
        "CSP1",
        old_snapshot_keys={"2026-07-31", "2026-08-07"},
        new_snapshot_keys={"2026-08-07"},
        old_breadth=None, new_breadth={},
    )
    assert statuses(out) == [guard.FAIL]
    assert "2026-07-31" in out[0]["evidence"]


def test_g5_growth_passes():
    out = guard.check_no_lost_state(
        "CSP1",
        old_snapshot_keys={"2026-07-31"},
        new_snapshot_keys={"2026-07-31", "2026-08-07"},
        old_breadth={"n_trading_days": 2150, "end_date": "2026-07-31"},
        new_breadth={"n_trading_days": 2155, "end_date": "2026-08-07"},
    )
    assert out == []


def test_g5_breadth_shrink_fails():
    # The 2026-08-04 SPY class: a later refresh silently lost sessions.
    out = guard.check_no_lost_state(
        "CSP1", set(), set(),
        old_breadth={"n_trading_days": 2158, "end_date": "2026-08-07"},
        new_breadth={"n_trading_days": 2156, "end_date": "2026-08-07"},
    )
    assert statuses(out) == [guard.FAIL]
    assert "shrank" in out[0]["evidence"]


def test_g5_breadth_end_moving_backwards_fails():
    out = guard.check_no_lost_state(
        "CSP1", set(), set(),
        old_breadth={"n_trading_days": 2158, "end_date": "2026-08-07"},
        new_breadth={"n_trading_days": 2158, "end_date": "2026-07-31"},
    )
    assert statuses(out) == [guard.FAIL]
    assert "backwards" in out[0]["evidence"]


def test_g5_no_baseline_is_not_a_failure_here():
    # Missing baseline is reported by main() as a WARN; the pure check
    # simply has nothing to compare.
    out = guard.check_no_lost_state("NEW", set(), {"2026-08-07"}, None, {})
    assert out == []


# ---------------------------------------------------------------------------
# W1 — universal walkback (the 2026-08-08 finding)
# ---------------------------------------------------------------------------
def test_w1_warns_when_every_panel_walked_back():
    actuals = {k: "2026-08-06" for k in ("CSP1", "SOXX", "EXV1")}
    (r,) = guard.check_universal_walkback(actuals, date(2026, 8, 7))
    assert r["status"] == guard.WARN
    assert "before iShares published" in r["evidence"]


def test_w1_ok_when_only_some_panels_walked_back():
    actuals = {"CSP1": "2026-08-07", "SOXX": "2026-08-06"}
    (r,) = guard.check_universal_walkback(actuals, date(2026, 8, 7))
    assert r["status"] == guard.OK


def test_w1_ok_when_all_exact():
    actuals = {"CSP1": "2026-08-07", "SOXX": "2026-08-07"}
    (r,) = guard.check_universal_walkback(actuals, date(2026, 8, 7))
    assert r["status"] == guard.OK


# ---------------------------------------------------------------------------
# latest_snapshot_actual helper
# ---------------------------------------------------------------------------
def test_latest_snapshot_actual_picks_newest_key():
    consts = {"snapshots": {
        "2026-07-31": {"actual_date": "2026-07-31"},
        "2026-08-07": {"actual_date": "2026-08-06"},
    }}
    assert guard.latest_snapshot_actual(consts) == "2026-08-06"


def test_latest_snapshot_actual_empty_is_none():
    assert guard.latest_snapshot_actual({"snapshots": {}}) is None


# ---------------------------------------------------------------------------
# G6 — roster coverage
#
# The band this closes is the one compute_breadth deliberately leaves open.
# That writer refuses to WRITE below 50% but only warns above it, on the
# reasoning that a thin current panel beats a stale one. Committing a thin
# panel is a different question, and 2026-08-08 answered it: IDP6 went to
# main at 61.5% coverage and changed Strategy A's holdings — the sleeve
# kept IDP6 at 6.3% within-sleeve and ejected IUMS entirely.
# ---------------------------------------------------------------------------
FLOOR = guard.MIN_ROSTER_COVERAGE_WARN


def _statuses(results, check_prefix="G6 roster coverage"):
    return [r["status"] for r in results if r["check"].startswith(check_prefix)]


def test_g6_all_panels_above_floor_is_ok():
    res = guard.check_roster_coverage({"CSP1": 1.0, "SOXX": 0.98}, FLOOR)
    assert _statuses(res) == [guard.OK]


def test_g6_fails_on_the_actual_incident():
    """IDP6 at 371/603. The regression test that matters."""
    res = guard.check_roster_coverage({"CSP1": 1.0, "IDP6": 371 / 603}, FLOOR)
    assert guard.FAIL in _statuses(res)
    assert "IDP6" in res[0]["evidence"]
    assert "CSP1" not in res[0]["evidence"]


def test_g6_does_not_trip_on_the_structural_tail():
    """ICHN 93.6% is what that market's coverage looks like, and ITWN now
    runs at 98.7%. Failing the weekly refresh on either would be a standing
    false alarm, which is how a guard gets ignored.

    ITWN sat in this test at 89.7% and was the reason the floor stayed at
    0.85. That number was a resolver bug — an unmapped Taipei Exchange
    venue — not a fact about Taiwanese listings, so it no longer belongs
    among the panels a floor must tolerate. See
    test_g6_fails_on_the_gap_the_old_floor_excused.
    """
    res = guard.check_roster_coverage({"ITWN": 77 / 78, "ICHN": 539 / 576},
                                      FLOOR)
    assert _statuses(res) == [guard.OK]


def test_g6_fails_on_the_gap_the_old_floor_excused():
    """ITWN's pre-fix 89.7% must now block a commit.

    It published a plausible breadth number on a universe 9% smaller than
    the fund for 451 roster-days, and the 0.85 floor was explicitly set
    beneath it. At 0.90 the guard catches that class of loss."""
    res = guard.check_roster_coverage({"ITWN": 70 / 78, "CSP1": 1.0}, FLOOR)
    assert guard.FAIL in _statuses(res)
    assert "ITWN" in res[0]["evidence"]


def test_g6_names_every_thin_panel_not_just_the_first():
    res = guard.check_roster_coverage(
        {"IDP6": 371 / 603, "EXH2": 2 / 37, "CSP1": 1.0}, FLOOR)
    assert "IDP6" in res[0]["evidence"] and "EXH2" in res[0]["evidence"]


def test_g6_indeterminable_coverage_warns_rather_than_fails():
    """An unreadable panel is not evidence of thinness. It warns so it
    cannot pass unnoticed, but it must not fail a refresh on absence."""
    res = guard.check_roster_coverage({"CSP1": 1.0, "MYSTERY": None}, FLOOR)
    assert guard.FAIL not in _statuses(res)
    assert guard.WARN in _statuses(res)
    assert "MYSTERY" in res[-1]["evidence"]


def test_g6_boundary_exactly_at_the_floor_passes():
    res = guard.check_roster_coverage({"EDGE": FLOOR}, FLOOR)
    assert _statuses(res) == [guard.OK]
    res = guard.check_roster_coverage({"EDGE": FLOOR - 0.001}, FLOOR)
    assert guard.FAIL in _statuses(res)


# --- panel_roster_coverage: recorded field, with a fallback ---------------

def test_coverage_prefers_the_recorded_field():
    blob = {"data_quality": {"roster_coverage_latest": 0.42},
            "series": {"n_with_ma50": [600], "n_constituents": [603]}}
    assert guard.panel_roster_coverage(blob) == 0.42


def test_coverage_falls_back_to_the_series():
    """Every panel written before 2026-08-09 lacks the field. A check that
    silently skipped 23 of 24 panels would be worse than no check."""
    blob = {"data_quality": {},
            "series": {"n_with_ma50": [371], "n_constituents": [603]}}
    assert guard.panel_roster_coverage(blob) == 371 / 603


def test_coverage_is_none_when_neither_source_is_usable():
    assert guard.panel_roster_coverage({}) is None
    assert guard.panel_roster_coverage(
        {"series": {"n_with_ma50": [5], "n_constituents": [0]}}) is None
