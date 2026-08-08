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
