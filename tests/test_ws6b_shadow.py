"""Tests for the WS6b T2 shadow publisher and its guard layer.

The house unattended-agent rule is that no scheduled run may be trusted without
a guard that can catch a silently-wrong step. These tests exist to prove the
guard actually fires — every failure branch is driven, not just the happy path,
because a guard that has only ever been seen to pass is not evidence.

Offline and synthetic throughout.
"""

from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import ws6b_shadow as sh  # noqa: E402
from ws6b_shadow import (  # noqa: E402
    GuardResult,
    ShadowWeek,
    append_week,
    check_capture_integrity,
    check_data_gaps,
    check_divergence,
    check_return_sanity,
    check_turnover,
    check_weight_integrity,
    consecutive_publishable_weeks,
    evaluate_week,
    rulings,
    rulings_line,
    shadow_status,
    strict_snapshot_fallbacks,
    verify_log_chain,
    weekly_gap_from_daily,
)


def _week(**kw) -> ShadowWeek:
    base = dict(
        week_ending="2026-07-17", i0_return=0.0120, e0_return=0.0115,
        gap=0.0005, turnover_i0=0.30,
        lines_held=["IUES", "SOXX", "IUFS"],
        lines_basketed=["IUES", "SOXX", "IUFS"],
        fallback_lines=[], unresolved_gaps=[], corporate_actions=[],
        snapshot_dates={"IUES": "2026-07-16"}, data_asof="2026-07-17",
        engine_commit="deadbee", params_sha="cafe1234")
    base.update(kw)
    return ShadowWeek(**base)


def _ok_args():
    return dict(
        expected_session=date(2026, 7, 17),
        line_weights={"IUES": 0.14, "SOXX": 0.13, "IUFS": 0.10},
        basket_weights={"IUES": {"XOM": 0.6, "CVX": 0.4},
                        "SOXX": {"NVDA": 0.7, "AVGO": 0.3},
                        "IUFS": {"JPM": 1.0}},
        e0_total_weight=0.37, prior_turnovers=[])


# --- happy path ------------------------------------------------------------

def test_clean_week_is_publishable():
    g = evaluate_week(_week(), **_ok_args())
    assert g.publishable, g.failures
    assert not g.failures


# --- capture integrity -----------------------------------------------------

def test_capture_integrity_fails_on_stale_data():
    """The silent-failure class the house rule exists for: the job succeeds
    while the fetched series quietly stopped at an older session."""
    g = GuardResult(publishable=True)
    check_capture_integrity(date(2026, 7, 10), date(2026, 7, 17), g)
    assert not g.publishable
    assert "capture_integrity" in g.failures[0]


def test_capture_integrity_passes_when_current():
    g = GuardResult(publishable=True)
    check_capture_integrity(date(2026, 7, 17), date(2026, 7, 17), g)
    assert g.publishable


def test_stale_week_is_not_publishable_end_to_end():
    args = _ok_args()
    g = evaluate_week(_week(data_asof="2026-07-09"), **args)
    assert not g.publishable


# --- weight integrity ------------------------------------------------------

def test_basket_not_summing_to_one_fails():
    """A basket that silently dropped a name would still publish without this,
    understating both the line's exposure and its divergence."""
    g = GuardResult(publishable=True)
    check_weight_integrity({"IUES": 0.14}, {"IUES": {"XOM": 0.6, "CVX": 0.3}},
                           0.14, g)
    assert not g.publishable
    assert "basket_weights_sum_to_one" in g.checks
    assert not g.checks["basket_weights_sum_to_one"]["ok"]


def test_book_not_preserving_e0_weight_fails():
    g = GuardResult(publishable=True)
    check_weight_integrity({"IUES": 0.14}, {"IUES": {"XOM": 1.0}}, 0.20, g)
    assert not g.publishable


def test_weight_integrity_passes_on_a_clean_book():
    g = GuardResult(publishable=True)
    check_weight_integrity({"IUES": 0.14, "SOXX": 0.13},
                           {"IUES": {"XOM": 1.0}, "SOXX": {"NVDA": 1.0}},
                           0.27, g)
    assert g.publishable


# --- data gaps -------------------------------------------------------------

def test_unresolved_gap_blocks_the_week():
    g = GuardResult(publishable=True)
    check_data_gaps(["IUCS: no snapshot"], [], g)
    assert not g.publishable


def test_fired_fallback_counts_as_resolved_not_a_gap():
    """Registered explicitly in bar (b): a fired fallback is the safety valve
    working, not a data gap. It must warn, never block."""
    g = GuardResult(publishable=True)
    check_data_gaps([], ["IUCS"], g)
    assert g.publishable
    assert g.warnings and "fallback" in g.warnings[0]


# --- return sanity ---------------------------------------------------------

def test_implausible_weekly_return_blocks_the_week():
    g = GuardResult(publishable=True)
    check_return_sanity(0.90, 0.01, g)
    assert not g.publishable


def test_large_but_plausible_weekly_return_passes():
    g = GuardResult(publishable=True)
    check_return_sanity(-0.18, -0.17, g)
    assert g.publishable


# --- divergence ------------------------------------------------------------

def test_divergence_within_bar_passes():
    g = GuardResult(publishable=True)
    check_divergence(0.0030, [], g)
    assert g.publishable


def test_divergence_breach_blocks_when_unexplained():
    g = GuardResult(publishable=True)
    check_divergence(0.0200, [], g)
    assert not g.publishable


def test_divergence_breach_is_excused_by_a_logged_corporate_action():
    """Bar (b) allows a wide week that carries a logged corporate-action
    explanation. It must warn rather than block."""
    g = GuardResult(publishable=True)
    check_divergence(0.0200, ["PXD acquired by XOM, 2026-07-15"], g)
    assert g.publishable
    assert g.warnings


def test_divergence_records_both_bars_regardless_of_which_governs():
    """A later ruling on which bar binds must never require re-running the
    shadow, so every week logs its gap against both."""
    g = GuardResult(publishable=True)
    check_divergence(0.0050, [], g)
    d = g.checks["divergence_detail"]
    assert d["within_registered"] is True        # 50bp <= 66bp
    assert d["within_adopted_set"] is False      # 50bp  > 42.9bp
    assert d["bar_registered_bp"] == pytest.approx(66.0)
    assert d["bar_adopted_set_bp"] == pytest.approx(42.9, abs=0.1)


def test_adopted_set_bar_is_three_times_partial5_te():
    assert sh.DIVERGENCE_BAR_ADOPTED_SET == pytest.approx(
        3 * sh.BACKTEST_WEEKLY_TE_PARTIAL5)
    assert sh.DIVERGENCE_BAR_ADOPTED_SET < sh.DIVERGENCE_BAR_REGISTERED


# --- turnover --------------------------------------------------------------

def test_turnover_bar_uses_the_running_average_not_the_week():
    """A per-week reading exceeds this bar in 15.2% of backtest weeks, so it
    would fail the shadow on behaviour the backtest shows is normal. One hot
    week inside an otherwise calm run must not block."""
    g = GuardResult(publishable=True)
    check_turnover([0.30, 0.30, 0.30, 0.90], g)   # mean 0.45 < 0.5086 bar
    assert g.publishable


def test_turnover_bar_fails_on_a_sustained_breach():
    g = GuardResult(publishable=True)
    check_turnover([0.60, 0.65, 0.70], g)
    assert not g.publishable


def test_turnover_bar_matches_the_frozen_constant():
    assert sh.TURNOVER_BAR == pytest.approx(1.5 * sh.BACKTEST_MEAN_WEEKLY_TURNOVER)


# --- log chain -------------------------------------------------------------

def test_append_seals_each_week_into_the_chain():
    recs = append_week([], _week(week_ending="2026-07-10"))
    recs = append_week(recs, _week(week_ending="2026-07-17"))
    assert recs[1]["prev_hash"] == recs[0]["record_hash"]
    ok, detail = verify_log_chain(recs)
    assert ok, detail


def test_chain_detects_an_altered_published_week():
    """The 8-consecutive-week bar is only meaningful if the record is
    tamper-evident."""
    recs = append_week([], _week(week_ending="2026-07-10"))
    recs = append_week(recs, _week(week_ending="2026-07-17"))
    tampered = copy.deepcopy(recs)
    tampered[0]["gap"] = 0.0001            # quietly improve an old week
    ok, detail = verify_log_chain(tampered)
    assert not ok
    assert "altered" in detail or "chain break" in detail


def test_chain_detects_reordering():
    recs = append_week([], _week(week_ending="2026-07-10"))
    recs = append_week(recs, _week(week_ending="2026-07-17"))
    ok, _ = verify_log_chain([recs[1], recs[0]])
    assert not ok


def test_append_refuses_to_rewrite_history():
    recs = append_week([], _week(week_ending="2026-07-17"))
    with pytest.raises(ValueError, match="append-only"):
        append_week(recs, _week(week_ending="2026-07-10"))
    with pytest.raises(ValueError, match="append-only"):
        append_week(recs, _week(week_ending="2026-07-17"))


# --- consecutive-week accounting ------------------------------------------

def test_consecutive_run_resets_on_a_failed_week():
    """Bar (b) requires 8 CONSECUTIVE weeks. A failure must reset the run to
    zero, not merely fail to increment it."""
    recs = [{"publishable": True}] * 5 + [{"publishable": False}] \
        + [{"publishable": True}] * 2
    assert consecutive_publishable_weeks(recs) == 2


def test_consecutive_run_counts_a_clean_streak():
    assert consecutive_publishable_weeks([{"publishable": True}] * 9) == 9


def test_bar_b_needs_eight_consecutive_and_an_intact_chain():
    recs = []
    for i in range(8):
        w = _week(week_ending=f"2026-0{5 + i // 4}-{(i % 4) * 7 + 1:02d}")
        recs = append_week(recs, w)
        recs[-1]["publishable"] = True
    st = shadow_status(recs)
    assert st["chain_intact"], st["chain_detail"]
    assert st["consecutive_publishable"] == 8
    assert st["bar_b_met"] is True


def test_bar_b_not_met_on_seven_weeks():
    recs = []
    for i in range(7):
        recs = append_week(recs, _week(week_ending=f"2026-05-{i * 4 + 1:02d}"))
        recs[-1]["publishable"] = True
    assert shadow_status(recs)["bar_b_met"] is False


# --- weekly compounding ----------------------------------------------------

def test_weekly_gap_compounds_rather_than_sums():
    """Summing daily returns understates the gap in exactly the volatile weeks
    the divergence bar exists to catch."""
    idx = pd.date_range("2026-07-13", periods=5, freq="B")
    i0 = pd.Series([0.05, 0.05, 0.05, 0.05, 0.05], index=idx)
    e0 = pd.Series([0.04, 0.04, 0.04, 0.04, 0.04], index=idx)
    a, b, gap = weekly_gap_from_daily(i0, e0, pd.Timestamp("2026-07-17"))
    assert a == pytest.approx(1.05 ** 5 - 1)
    assert b == pytest.approx(1.04 ** 5 - 1)
    assert gap == pytest.approx((1.05 ** 5) - (1.04 ** 5))
    assert gap > 0.05 - 0.04       # strictly wider than the naive sum


def test_weekly_gap_month_boundary():
    """Edge case 1 of 2 (house rule): week spanning a month boundary."""
    idx = pd.date_range("2026-06-29", periods=5, freq="B")
    i0 = pd.Series(0.01, index=idx)
    e0 = pd.Series(0.005, index=idx)
    a, b, _g = weekly_gap_from_daily(i0, e0, pd.Timestamp("2026-07-03"))
    assert a == pytest.approx(1.01 ** 5 - 1)
    assert b == pytest.approx(1.005 ** 5 - 1)


def test_weekly_gap_year_boundary():
    """Edge case 2 of 2: week spanning a year boundary."""
    idx = pd.date_range("2025-12-29", periods=5, freq="B")
    i0 = pd.Series(0.01, index=idx)
    e0 = pd.Series(0.005, index=idx)
    a, _b, _g = weekly_gap_from_daily(i0, e0, pd.Timestamp("2026-01-02"))
    assert a == pytest.approx(1.01 ** 5 - 1)


# --- frozen bars must not drift -------------------------------------------

def test_adopted_set_is_the_signed_partial_five():
    assert sh.ADOPTED_SET == ("IUES", "IUUS", "IUCS", "SOXX", "IUFS")


def test_required_weeks_matches_the_signed_bar():
    assert sh.REQUIRED_CONSECUTIVE_WEEKS == 8


# --- 2026-08-05 pre-arm dry-run findings, pinned -----------------------------

def test_all_fallback_week_publishes_with_fallback_warning_not_weight_failure():
    """A week where EVERY basketed line reverted to its ETF must publish as a
    logged all-fallback week (bar (b): a fired fallback is resolved), NOT fail
    weight integrity on empty baskets. The 2026-08-05 pre-arm dry run hit the
    unfixed form of this: stale member data reverted all four held lines, the
    old cumulative-counter classification reported no fallback, and the guard
    failed on baskets summing to zero."""
    week = _week(lines_basketed=[], fallback_lines=["IUES", "SOXX", "IUFS"],
                 gap=0.0, i0_return=0.0115)
    guard = evaluate_week(
        week, date(2026, 7, 17),
        line_weights={"IUES": 0.14, "SOXX": 0.13, "IUFS": 0.10},
        basket_weights={},          # reverted lines carry NO basket entries
        e0_total_weight=0.37, prior_turnovers=[])
    assert guard.publishable
    assert guard.checks["basket_weights_sum_to_one"]["ok"]
    assert any("fallback" in w for w in guard.warnings)


def test_member_fetch_end_threads_to_universe_and_meta(tmp_path, monkeypatch):
    """The T3 shadow passes a live end date; it must reach BOTH the membership
    universe (resolution window) and the fetch window recorded in the cache
    meta. The committed T2 called the T1 form, silently clamped to the frozen
    2026-06-30 study end — the root cause of the all-fallback dry run."""
    import run_ws6_single_name as R

    seen = {}

    def fake_universe(line, window_end):
        seen["window_end"] = pd.Timestamp(window_end)
        return ["AAA"], {"resolution": {}, "n_ishares_unique": 1,
                         "n_instruments": 1, "n_member_weeks": 1,
                         "n_resolved_weeks": 1, "unresolved": []}

    def fake_fetch(symbols, start, end, report=None):
        seen["fetch_end"] = end
        if report is not None:
            report["resolved"] = list(symbols)
            report["uncovered"] = []
        idx = pd.DatetimeIndex(["2026-07-31"])
        return pd.DataFrame({"AAA": [1.0]}, index=idx)

    monkeypatch.setattr(R, "DATA_LOCAL_WS6", tmp_path)
    monkeypatch.setattr(R, "line_member_universe", fake_universe)
    monkeypatch.setattr(R, "fetch_member_prices", fake_fetch)
    monkeypatch.setattr(R, "member_cache_path",
                        lambda line: tmp_path / f"prices_{line.lower()}.parquet")
    monkeypatch.setattr(R, "_meta_path",
                        lambda line: tmp_path / f"prices_{line.lower()}.meta.json")

    R.load_or_fetch_member_prices(("SOXX",), end="2026-08-01")
    assert seen["window_end"] == pd.Timestamp("2026-08-01")
    assert seen["fetch_end"] == "2026-08-01"

    import json as _json
    meta = _json.loads((tmp_path / "prices_soxx.meta.json").read_text())
    assert meta["fetch_end"] == "2026-08-01"

    # Default keeps the frozen study end — T1 semantics byte-identical.
    R.load_or_fetch_member_prices(("SOXX",))
    assert seen["window_end"] == R.WINDOW_END
    assert seen["fetch_end"] == R.WINDOW_END.strftime("%Y-%m-%d")


# --- SS6 owner rulings (ZH, 2026-09-09) ------------------------------------
#
# The three readings SS6 of the pre-shadow review pack left open. These tests
# pin the rulings themselves, not merely the behaviour they select: a bar that
# can drift without a test failing is not a bar, and at T4 the question "which
# reading governed week 3" has to be answerable from the record.

def test_rulings_record_all_three_with_the_ruling_date():
    r = rulings()
    assert r["ruled_on"] == "2026-09-09"
    assert r["ruled_by"] == "ZH"
    assert r["divergence_bar"] == "registered"
    assert r["turnover_reading"] == "running_average"
    assert r["missing_snapshot_semantics"] == "strict"


def test_divergence_ruling_selects_the_registered_66bp_bar():
    # "Keep it loose" — the signed parenthetical governs, not the tighter
    # adopted-set figure, which stays logged so the ruling can be revisited on
    # evidence already collected.
    assert sh.BINDING_DIVERGENCE_BAR == "registered"
    assert rulings()["divergence_bar_bp"] == pytest.approx(66.0)
    assert sh.DIVERGENCE_BAR_ADOPTED_SET < sh.DIVERGENCE_BAR_REGISTERED


def test_rulings_line_names_every_ruling():
    line = rulings_line()
    for token in ("2026-09-09", "SS6.1", "SS6.2", "SS6.3",
                  "registered", "66.0bp", "running_average", "strict"):
        assert token in line, f"{token!r} missing from the weekly log line"


def test_rulings_are_sealed_into_the_record_hash():
    # A record that carries its governing rulings outside the hash could be
    # re-labelled after the fact; inside it, re-labelling breaks the chain.
    recs = append_week([], _week(rulings=rulings()))
    ok, _ = verify_log_chain(recs)
    assert ok
    tampered = copy.deepcopy(recs)
    tampered[0]["rulings"]["divergence_bar"] = "adopted_set"
    ok, detail = verify_log_chain(tampered)
    assert not ok and "altered" in detail


def test_shadow_status_reports_the_rulings_in_force():
    recs = append_week([], _week(rulings=rulings()))
    recs[-1]["publishable"] = True
    st = shadow_status(recs)
    assert st["rulings_in_force"]["missing_snapshot_semantics"] == "strict"
    assert len(st["rulings_seen_in_log"]) == 1


# --- SS6.3 STRICT missing-snapshot semantics -------------------------------

def _fresh(*lines, d="2026-09-04"):
    return {L: d for L in lines}


def test_strict_passes_when_both_halves_are_one_cadence_old():
    # The normal W-FRI week: the rebalance is Friday 2026-09-11, the t-1 read is
    # Thursday 2026-09-10, and the newest usable entry is the previous Friday's.
    # Six days is the frozen cadence, not staleness.
    assert strict_snapshot_fallbacks(_fresh("SOXX", "IUES"),
                                     _fresh("SOXX", "IUES"),
                                     date(2026, 9, 10)) == []


def test_strict_withholds_a_line_whose_membership_missed_a_week():
    assert strict_snapshot_fallbacks(
        {"SOXX": "2026-09-04", "IUES": "2026-08-28"},
        _fresh("SOXX", "IUES"), date(2026, 9, 10)) == ["IUES"]


def test_strict_withholds_a_line_whose_WEIGHTS_are_stale_though_membership_is_current():
    # The 2026-09-09 state exactly, and the reason this ruling is not inert:
    # membership current to 2026-09-04 on every line, A3 weights stuck at
    # 2026-07-10. A check on membership alone would have passed all five.
    assert strict_snapshot_fallbacks(
        _fresh("IUES", "IUUS", "IUCS", "SOXX", "IUFS"),
        {"IUES": "2026-07-10", "IUUS": "2026-07-10", "IUCS": "2026-07-10",
         "SOXX": "2026-07-31", "IUFS": "2026-07-10"},
        date(2026, 9, 10)) == ["IUCS", "IUES", "IUFS", "IUUS", "SOXX"]


def test_strict_withholds_a_line_with_no_usable_entry_at_all():
    assert strict_snapshot_fallbacks(
        {"SOXX": "none", "IUES": ""}, _fresh("SOXX", "IUES"),
        date(2026, 9, 10)) == ["IUES", "SOXX"]
    # ... and the same on the weights side.
    assert strict_snapshot_fallbacks(
        _fresh("SOXX"), {"SOXX": "none"}, date(2026, 9, 10)) == ["SOXX"]


def test_strict_boundary_is_exactly_one_cadence():
    # Seven days passes, eight fires. Pinned because an off-by-one here either
    # fires every single week (the shadow measures nothing) or never fires (the
    # ruling has no effect), and neither would be visible in a log.
    assert strict_snapshot_fallbacks({"SOXX": "2026-09-03"}, {"SOXX": "2026-09-03"},
                                     date(2026, 9, 10)) == []
    assert strict_snapshot_fallbacks({"SOXX": "2026-09-02"}, {"SOXX": "2026-09-02"},
                                     date(2026, 9, 10)) == ["SOXX"]


def test_strict_month_boundary():
    # Month boundary: 2026-08-28 to 2026-09-03 is six days across the turn.
    assert strict_snapshot_fallbacks({"SOXX": "2026-08-28"}, {"SOXX": "2026-08-28"},
                                     date(2026, 9, 3)) == []
    assert strict_snapshot_fallbacks({"SOXX": "2026-08-21"}, {"SOXX": "2026-08-21"},
                                     date(2026, 9, 3)) == ["SOXX"]


def test_strict_year_boundary():
    # Year boundary: 2026-12-31 to 2027-01-06 is six days across the turn.
    assert strict_snapshot_fallbacks({"SOXX": "2026-12-31"}, {"SOXX": "2026-12-31"},
                                     date(2027, 1, 6)) == []
    assert strict_snapshot_fallbacks({"SOXX": "2026-12-24"}, {"SOXX": "2026-12-24"},
                                     date(2027, 1, 6)) == ["SOXX"]


def test_carry_forward_reading_disables_the_check(monkeypatch):
    # The softer reading the engine had before the ruling. Kept reachable so a
    # later owner can reverse SS6.3 without a code change disguised as a fix.
    monkeypatch.setattr(sh, "MISSING_SNAPSHOT_SEMANTICS", "carry_forward")
    assert strict_snapshot_fallbacks({"SOXX": "2026-06-05"}, {"SOXX": "2026-06-05"},
                                     date(2026, 9, 10)) == []


def test_snapshot_at_normalises_both_key_types():
    """The two halves of the SS6.3 check are keyed differently.

    load_constituents gives ISO date STRINGS, load_member_weights gives
    pd.Timestamps. A bare str() on the latter yields "2026-07-10 00:00:00",
    which sorts correctly and then raises out of date.fromisoformat inside the
    guard - a crash that would only ever have appeared on the live weights half.
    """
    import run_ws6b_shadow as R
    from datetime import date as _d

    ts_keyed = {pd.Timestamp("2026-07-03"): {}, pd.Timestamp("2026-07-10"): {}}
    str_keyed = {"2026-08-28": {}, "2026-09-04": {}}

    assert R._snapshot_at(ts_keyed, _d(2026, 9, 10)) == "2026-07-10"
    assert R._snapshot_at(str_keyed, _d(2026, 9, 10)) == "2026-09-04"
    # Never returns an entry the t-1 read could not have seen.
    assert R._snapshot_at(str_keyed, _d(2026, 8, 31)) == "2026-08-28"
    assert R._snapshot_at(str_keyed, _d(2020, 1, 1)) == "none"
    # And what it returns must survive the guard it feeds.
    assert strict_snapshot_fallbacks(
        {"SOXX": R._snapshot_at(str_keyed, _d(2026, 9, 10))},
        {"SOXX": R._snapshot_at(ts_keyed, _d(2026, 9, 10))},
        _d(2026, 9, 10)) == ["SOXX"]


def test_status_counts_weeks_that_measured_nothing():
    """Eight total-reversion weeks would satisfy bar (b) while measuring nothing.

    A fired fallback is registered as resolved rather than as a gap, so a week
    in which every adopted held line reverted is publishable, carries a 0.0 bp
    gap, and looks identical to perfect tracking. That is the state the shadow
    was armed in on 2026-09-09 with the weight route walled, so the count has to
    be on the face of the status read, not inferred at T4.
    """
    recs = []
    for i, d in enumerate(("2026-09-04", "2026-09-11", "2026-09-18")):
        reverted = i < 2
        recs = append_week(recs, _week(
            week_ending=d, gap=0.0,
            lines_basketed=[] if reverted else ["SOXX"],
            fallback_lines=["IUES", "IUUS", "IUCS", "SOXX", "IUFS"]
            if reverted else []))
        recs[-1]["publishable"] = True

    st = shadow_status(recs)
    assert st["consecutive_publishable"] == 3      # all three "count"
    assert st["weeks_fully_reverted"] == 2         # but two measured nothing
    assert st["max_abs_gap_bp"] == 0.0             # and tracked perfectly by construction
