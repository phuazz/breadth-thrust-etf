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
from datetime import date, datetime, timezone
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


# The band's upper bound is "now"-dependent, so every case below pins a clock.
# Fri 2026-07-10 22:00 UTC: both NYSE (20:00 UTC) and XETR (15:30 UTC) have closed.
_NOW_0710 = datetime(2026, 7, 10, 22, 0, tzinfo=timezone.utc)
# ...and 18:00 UTC on the same day, when XETR has closed but NYSE has not.
_NOW_0710_MIDCLOSE = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


def test_g4_ok_when_each_panel_ends_on_its_own_calendar():
    ends = {"CSP1": "2026-07-02", "EXV1": "2026-07-03"}
    cals = {"CSP1": "NYSE", "EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=datetime(2026, 7, 3, 22, 0,
                                                     tzinfo=timezone.utc))
    assert r["status"] == guard.OK


def test_g4_fails_when_a_panel_stops_a_session_short():
    ends = {"CSP1": "2026-08-06", "EXV1": "2026-08-07"}
    cals = {"CSP1": "NYSE", "EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 8, 7),
                                    now_utc=datetime(2026, 8, 7, 22, 0,
                                                     tzinfo=timezone.utc))
    assert r["status"] == guard.FAIL
    assert "CSP1" in r["evidence"]
    assert "TRUNCATED" in r["evidence"]


def test_g4_fails_when_us_panel_carries_a_europe_only_phantom_bar():
    # A NYSE-calendar panel dated the US-holiday Friday itself would mean
    # a phantom bar leaked in (the 2026-06-22 / 2026-07-06 class). Caught by
    # the session-membership test now, not by the old equality.
    ends = {"CSP1": "2026-07-03"}
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=_NOW_0710)
    assert r["status"] == guard.FAIL
    assert "not a NYSE session" in r["evidence"]


# --- the 2026-08-15 tail extension: the band's whole reason for existing ----
def test_g4_admits_a_panel_extended_past_the_target_friday():
    # THE REGRESSION THIS FIXES. compute_breadth extends to the last
    # completed session (register 2026-08-15-breadth-thrust-etf-1); the old
    # equality failed all 24 panels every run from 2026-08-15.
    ends = {"CSP1": "2026-07-10", "EXV1": "2026-07-10"}
    cals = {"CSP1": "NYSE", "EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=_NOW_0710)
    assert r["status"] == guard.OK


def test_g4_admits_a_partial_extension_from_the_price_cap():
    # The writer may cap the tail back down on thin price coverage, to any
    # session at or above the end_friday bound. Mid-band must pass.
    ends = {"CSP1": "2026-07-08"}
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=_NOW_0710)
    assert r["status"] == guard.OK


def test_g4_still_fails_a_bar_whose_close_has_not_happened():
    # Upper bound: 2026-07-13 is a Monday NYSE session, but at Friday
    # 18:00 UTC it has not opened, let alone closed.
    ends = {"CSP1": "2026-07-13"}
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=_NOW_0710)
    assert r["status"] == guard.FAIL
    assert "partial bar" in r["evidence"]


def test_g4_upper_bound_is_venue_aware_mid_close():
    # 18:00 UTC Friday: Xetra shut at 15:30 so its Friday bar is final, but
    # NYSE trades until 20:00 so the same date is still a partial bar there.
    # A single US-derived ceiling would truncate Europe by a session; a single
    # European one would admit a live US quote. Both directions, one clock.
    ends = {"EXV1": "2026-07-10"}
    cals = {"EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=_NOW_0710_MIDCLOSE)
    assert r["status"] == guard.OK

    ends = {"CSP1": "2026-07-10"}
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=_NOW_0710_MIDCLOSE)
    assert r["status"] == guard.FAIL
    assert "partial bar" in r["evidence"]


def test_g4_fails_a_malformed_end_date_rather_than_raising():
    ends = {"CSP1": "not-a-date"}
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(ends, cals, date(2026, 7, 3),
                                    now_utc=_NOW_0710)
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
    res = guard.check_roster_coverage({"IUSP": 70 / 78, "CSP1": 1.0}, FLOOR)
    assert guard.FAIL in _statuses(res)
    assert "IUSP" in res[0]["evidence"]


def test_g6_names_every_thin_panel_not_just_the_first():
    res = guard.check_roster_coverage(
        {"IDP6": 371 / 603, "IUSP": 2 / 37, "CSP1": 1.0}, FLOOR)
    assert "IDP6" in res[0]["evidence"] and "IUSP" in res[0]["evidence"]


def test_g6_indeterminable_coverage_warns_rather_than_fails():
    """An unreadable panel is not evidence of thinness. It warns so it
    cannot pass unnoticed, but it must not fail a refresh on absence."""
    res = guard.check_roster_coverage({"CSP1": 1.0, "MYSTERY": None}, FLOOR)
    assert guard.FAIL not in _statuses(res)
    assert guard.WARN in _statuses(res)
    assert "MYSTERY" in res[-1]["evidence"]


def test_g6_boundary_exactly_at_the_floor_passes():
    res = guard.check_roster_coverage({"CSP1": FLOOR}, FLOOR)
    assert _statuses(res) == [guard.OK]
    res = guard.check_roster_coverage({"CSP1": FLOOR - 0.001}, FLOOR)
    assert guard.FAIL in _statuses(res)


# ---------------------------------------------------------------------------
# G6 splits on the TRADED book (2026-08-22)
#
# The incident G6 exists for — IDP6 published at 61.5% on 2026-08-08 —
# changed Strategy A's holdings. That is what makes thinness a FAIL: the
# panel moved money. NDIA at 72.7% cannot, because no engine reads it, and
# failing the refresh on it blocks a commit that is correct for every panel
# the book actually trades.
#
# The split is deliberate and it is a real loosening: a thin MONITORED panel
# now gets committed with a warning rather than blocking. It still shows on
# the dashboard, so the WARN has to name it. These pin both halves — if the
# traded set is ever widened or the split removed, the pair below is the
# evidence of what the verdict used to be.
# ---------------------------------------------------------------------------
def test_g6_same_thinness_fails_deployed_but_warns_monitored():
    thin = 70 / 78                                  # 89.7%, under the floor

    deployed = guard.check_roster_coverage({"IUSP": thin, "CSP1": 1.0}, FLOOR)
    assert guard.FAIL in _statuses(deployed)
    assert "IUSP" in deployed[0]["evidence"]

    monitored = guard.check_roster_coverage({"NDIA": thin, "CSP1": 1.0}, FLOOR)
    assert guard.FAIL not in _statuses(monitored)
    assert guard.WARN in _statuses(monitored)
    # A warning nobody can act on is not a warning: name the panel.
    assert "NDIA" in " ".join(r["evidence"] for r in monitored)


def test_g6_a_thin_monitored_panel_does_not_mask_a_thin_deployed_one():
    res = guard.check_roster_coverage(
        {"NDIA": 0.60, "IUSP": 0.60, "CSP1": 1.0}, FLOOR)
    assert guard.FAIL in _statuses(res)
    fail = next(r for r in res if r["status"] == guard.FAIL)
    assert "IUSP" in fail["evidence"]
    assert "NDIA" not in fail["evidence"]      # reported, but not as the block


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


# ---------------------------------------------------------------------------
# G4 honours a declared tail cap (2026-08-22)
#
# The roster can legitimately lead the prices: on 2026-08-22 iShares had
# published Friday's holdings for the European panels while the vendor had
# the constituents priced only to Thursday. G4's floor is derived from the
# roster, so it was unreachable, and five DEPLOYED panels were called
# TRUNCATED for ending exactly where their data ends.
#
# compute_breadth now records WHY it stopped. These pin that the guard reads
# that rather than re-deriving a second bound — and, importantly, that the
# relaxation is narrow: a panel shorter than its own declared cap still
# fails, and a panel with no cap keeps the strict bound.
# ---------------------------------------------------------------------------
def _europe_cap(priced_to: str) -> dict:
    return {"capped_at": priced_to,
            "venue_last_completed": "2026-08-21",
            "constituents_priced_to": priced_to,
            "roster_end_friday": "2026-08-21"}


def test_g4_accepts_a_panel_that_ends_where_its_declared_cap_says():
    ends = {"EXV1": "2026-08-20"}
    cals = {"EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(
        ends, cals, date(2026, 8, 21),
        now_utc=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
        tail_caps={"EXV1": _europe_cap("2026-08-20")})
    assert r["status"] == guard.OK


def test_g4_still_fails_that_same_panel_when_it_declares_no_cap():
    """The relaxation must come from the panel, not from the date."""
    ends = {"EXV1": "2026-08-20"}
    cals = {"EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(
        ends, cals, date(2026, 8, 21),
        now_utc=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
        tail_caps=None)
    assert r["status"] == guard.FAIL
    assert "TRUNCATED" in r["evidence"]


def test_g4_still_fails_a_panel_shorter_than_its_own_declared_cap():
    """Silent data loss is still caught: the cap is a floor, not an amnesty."""
    ends = {"EXV1": "2026-08-19"}          # cap says it reaches the 20th
    cals = {"EXV1": "XETR"}
    (r,) = guard.check_breadth_ends(
        ends, cals, date(2026, 8, 21),
        now_utc=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
        tail_caps={"EXV1": _europe_cap("2026-08-20")})
    assert r["status"] == guard.FAIL
    assert "TRUNCATED" in r["evidence"]


def test_g4_cap_cannot_raise_the_floor_only_lower_it():
    """A cap claiming MORE than the roster bound must not tighten the check.

    Otherwise a malformed or future-dated cap could start failing panels
    that the roster-derived bound accepts.
    """
    ends = {"CSP1": "2026-08-21"}
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(
        ends, cals, date(2026, 8, 21),
        now_utc=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
        tail_caps={"CSP1": {"constituents_priced_to": "2026-09-30"}})
    assert r["status"] == guard.OK


def test_g4_ignores_a_malformed_cap_and_keeps_the_strict_bound():
    ends = {"EXV1": "2026-08-20"}
    cals = {"EXV1": "XETR"}
    for bad in ({"constituents_priced_to": "not-a-date"},
                {"constituents_priced_to": None},
                {}):
        (r,) = guard.check_breadth_ends(
            ends, cals, date(2026, 8, 21),
            now_utc=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
            tail_caps={"EXV1": bad})
        assert r["status"] == guard.FAIL, bad


def test_g4_cap_on_one_panel_does_not_excuse_another():
    ends = {"EXV1": "2026-08-20", "EXH1": "2026-08-20"}
    cals = {"EXV1": "XETR", "EXH1": "XETR"}
    (r,) = guard.check_breadth_ends(
        ends, cals, date(2026, 8, 21),
        now_utc=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
        tail_caps={"EXV1": _europe_cap("2026-08-20")})
    assert r["status"] == guard.FAIL
    assert "EXH1" in r["evidence"]
    assert "EXV1" not in r["evidence"]


def test_g4_cap_does_not_loosen_the_upper_bound():
    """A cap is a floor. It must never admit a bar that has not closed."""
    ends = {"CSP1": "2026-08-24"}          # a session that has not happened
    cals = {"CSP1": "NYSE"}
    (r,) = guard.check_breadth_ends(
        ends, cals, date(2026, 8, 21),
        now_utc=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
        tail_caps={"CSP1": _europe_cap("2026-08-24")})
    assert r["status"] == guard.FAIL
