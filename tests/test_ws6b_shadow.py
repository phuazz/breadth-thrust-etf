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
    shadow_status,
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
