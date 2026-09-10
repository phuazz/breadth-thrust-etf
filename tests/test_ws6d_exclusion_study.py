"""Tests for the WS6d exclusion study core.

Registration: ``C:\\dev\\KICKOFF_ws6d-overbought-exclusion.md``, FROZEN at
vault-docs ``37022b7``. These tests exist for the same reason the study has a
placebo: a pre-registered verdict rule that has only ever been seen to pass is
not evidence that it can fail. Every gate branch is driven.

Offline and synthetic throughout.

NOTE (vault CLAUDE.md date rule): dates below are built with a date library,
never by manual arithmetic. Python months are 1-INDEXED and weekday() is
Monday = 0, so Friday is 4.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import ws6d_exclusion_study as st  # noqa: E402
from single_name_impl import MIN_PASS  # noqa: E402
from ws6c_screened import constants as ws6c_constants  # noqa: E402
from ws6d_exclusion_study import (  # noqa: E402
    LineWeekPlan,
    concentration,
    constants,
    drawdown_episodes,
    episode_attribution,
    gate,
    path_seeds,
    percentile_rank,
    placebo_rebalance_rows,
    placebo_survivors,
    rows_to_panel,
    seen_data_caveat,
    summarise_arm,
    thinness,
    verdict,
    weekly_returns_from_daily,
)


# ---------------------------------------------------------------------------
# The frozen construction (§4-§5)
# ---------------------------------------------------------------------------

def test_the_frozen_study_constants_are_what_the_registration_says():
    c = constants()
    assert c["placebo_paths"] == 1000
    assert c["placebo_seed"] == 20260910
    assert c["pass_percentile"] == 0.10
    assert c["gates_required"] == ["G1_max_drawdown", "G2_downside_deviation"]
    assert c["thin_single_episode_share"] == 0.60
    assert c["thin_min_contributing"] == 3
    assert c["binding_cost_bps"] == 10
    assert c["cost_sweep_bps"] == [2, 5, 10, 20]
    assert c["registration_frozen_at"] == "37022b7"
    assert c["episode_reference_arm"] == "I0"


def test_the_exclusion_construction_is_the_shadows_not_a_copy():
    """Guard 6.6. If WS6c's decile ever changed, this study's own sealed record
    would show a different nested block rather than silently measuring a
    different rule from the one the shadow publishes."""
    assert constants()["exclusion_construction"] == ws6c_constants()
    assert constants()["exclusion_construction"]["decile_fraction"] == 0.10
    assert constants()["exclusion_construction"]["eligible_set"] == "full_roster"


# ---------------------------------------------------------------------------
# §4.2 — the placebo
# ---------------------------------------------------------------------------

def test_placebo_drops_exactly_k_names_from_i1s_basket():
    """Count-matched by construction, which is what holds concentration fixed
    across I1X and P1X (guard 6.3)."""
    names = [f"N{i:02d}" for i in range(12)]
    rng = np.random.default_rng(1)
    for k in range(0, 12):
        keep = placebo_survivors(names, k, rng)
        assert len(keep) == 12 - k
        assert set(keep) <= set(names)
        # Order is preserved, so the renormalisation is deterministic.
        assert keep == [n for n in names if n in set(keep)]


def test_placebo_never_draws_from_outside_i1s_basket():
    """Guard 6.4 — the failure that would flatter I1X. Drawing from the pool
    would let the placebo remove state-failing names I1X never held, which is a
    different intervention entirely."""
    names = ["AAA", "BBB", "CCC", "DDD"]
    rng = np.random.default_rng(7)
    for _ in range(200):
        keep = placebo_survivors(names, 2, rng)
        assert set(keep) <= set(names)
        assert len(keep) == 2


def test_placebo_with_k_zero_is_the_unmodified_basket():
    names = ["AAA", "BBB", "CCC"]
    assert placebo_survivors(names, 0, np.random.default_rng(3)) == names


def test_placebo_actually_varies_across_paths():
    """A placebo that returned the same draw every time would be a constant, and
    every percentile would be degenerate."""
    names = [f"N{i}" for i in range(10)]
    draws = {tuple(placebo_survivors(names, 3, r)) for r in path_seeds(50, 99)}
    assert len(draws) > 5, "the placebo is barely varying — check the seeding"


def test_path_seeds_are_reproducible_from_the_frozen_seed():
    """The seed is in the registration; the run must be reproducible from it."""
    a = [placebo_survivors(list("ABCDEFGH"), 3, r) for r in path_seeds(20, 20260910)]
    b = [placebo_survivors(list("ABCDEFGH"), 3, r) for r in path_seeds(20, 20260910)]
    assert a == b
    c = [placebo_survivors(list("ABCDEFGH"), 3, r) for r in path_seeds(20, 1)]
    assert a != c


def test_line_week_plan_refuses_an_i1x_basket_outside_i1s():
    """Structural guard 6.4: the plan cannot be built in a state where the
    placebo's draw set differs from the set under test."""
    ok = LineWeekPlan("SOXX", pd.Timestamp("2026-09-04"), 0.14,
                      {"AAA": 0.5, "BBB": 0.5}, {"AAA": 1.0}, 1, False, False)
    assert ok.k_excluded == 1

    with pytest.raises(AssertionError, match="outside I1's basket"):
        LineWeekPlan("SOXX", pd.Timestamp("2026-09-04"), 0.14,
                     {"AAA": 0.5, "BBB": 0.5}, {"ZZZ": 1.0}, 1, False, False)


def test_line_week_plan_refuses_an_impossible_k():
    with pytest.raises(AssertionError, match="outside I1's basket of"):
        LineWeekPlan("SOXX", pd.Timestamp("2026-09-04"), 0.14,
                     {"AAA": 0.5, "BBB": 0.5}, {}, 5, False, False)


# ---------------------------------------------------------------------------
# The placebo book preserves E0's weight and honours the valve
# ---------------------------------------------------------------------------

def _sector(rebal, lines, weights):
    return pd.DataFrame({L: [weights[L]] * len(rebal) for L in lines}, index=rebal)


def test_placebo_book_preserves_the_line_weights_exactly():
    """The same weight-preservation property every register arm has: whatever the
    draw, the book's total weight is E0's."""
    rebal = pd.DatetimeIndex(["2026-08-28", "2026-09-04"])
    lines = ["CSP1", "SOXX", "IUES"]
    lw = {"CSP1": 0.20, "SOXX": 0.30, "IUES": 0.50}
    sector = _sector(rebal, lines, lw)
    plans = [
        LineWeekPlan(L, rd, lw[L],
                     {f"{L}_{i}": 0.2 for i in range(5)},
                     {f"{L}_{i}": 0.25 for i in range(4)}, 1, False, False)
        for L in ("SOXX", "IUES") for rd in rebal
    ]
    for rng in path_seeds(25, 5):
        rows = placebo_rebalance_rows(plans, sector, rebal, ("SOXX", "IUES"), rng)
        for rd in rebal:
            assert sum(rows[rd].values()) == pytest.approx(1.0, abs=1e-12)
            assert rows[rd]["CSP1"] == pytest.approx(0.20)   # broad slice, ETF


def test_placebo_applies_min_pass_identically_to_i1x():
    """§3.3's valve. Four names less two leaves two, below MIN_PASS = 3, so the
    line reverts to its ETF — exactly as I1X would."""
    rebal = pd.DatetimeIndex(["2026-09-04"])
    lines = ["SOXX"]
    sector = _sector(rebal, lines, {"SOXX": 1.0})
    plans = [LineWeekPlan("SOXX", rebal[0], 1.0,
                          {f"N{i}": 0.25 for i in range(4)},
                          {f"N{i}": 0.5 for i in range(2)}, 2, False, False)]
    assert MIN_PASS == 3
    for rng in path_seeds(10, 11):
        rows = placebo_rebalance_rows(plans, sector, rebal, ("SOXX",), rng)
        assert rows[rebal[0]] == {"SOXX": 1.0}      # reverted, not a 2-name book


def test_a_reverted_i1_line_carries_no_exclusion_into_its_plan():
    """Found by the first live run, on IUUS in the March 2020 crash.

    §3.3 computes the exclusion BEFORE the MIN_PASS valve, so a line-week where
    I1 itself reverted still records excluded names — k = 2 against a basket of
    zero. There is nothing to exclude from: I1, I1X and the placebo are all on
    the ETF that week, and I1X cannot avoid the revert because its survivors are
    a subset of a set already too small for I1. The plan must therefore carry
    k = 0, and the constructor refuses anything else rather than quietly
    accepting an impossible draw instruction."""
    with pytest.raises(AssertionError, match="outside I1's basket of 0"):
        LineWeekPlan("IUUS", pd.Timestamp("2020-03-13"), 0.14, {}, {}, 2,
                     True, True)
    ok = LineWeekPlan("IUUS", pd.Timestamp("2020-03-13"), 0.14, {}, {}, 0,
                      True, True)
    assert ok.i1_fallback and ok.k_excluded == 0


def test_a_line_i1_reverted_is_its_etf_in_the_placebo_too():
    rebal = pd.DatetimeIndex(["2026-09-04"])
    sector = _sector(rebal, ["SOXX"], {"SOXX": 1.0})
    plans = [LineWeekPlan("SOXX", rebal[0], 1.0, {}, {}, 0, True, True)]
    rows = placebo_rebalance_rows(plans, sector, rebal, ("SOXX",),
                                  np.random.default_rng(1))
    assert rows[rebal[0]] == {"SOXX": 1.0}


def test_placebo_weights_are_pro_rata_over_the_survivors():
    """§3.4's one weighting rule: renormalising the parent basket over the
    survivors, never equal weight and never anything to cash."""
    rebal = pd.DatetimeIndex(["2026-09-04"])
    sector = _sector(rebal, ["SOXX"], {"SOXX": 1.0})
    parent = {"AAA": 0.4, "BBB": 0.3, "CCC": 0.2, "DDD": 0.1}
    plans = [LineWeekPlan("SOXX", rebal[0], 1.0, parent,
                          {"AAA": 0.5, "BBB": 0.375, "CCC": 0.125}, 1,
                          False, False)]
    rows = placebo_rebalance_rows(plans, sector, rebal, ("SOXX",),
                                  np.random.default_rng(2))
    row = rows[rebal[0]]
    assert sum(row.values()) == pytest.approx(1.0, abs=1e-12)
    held = [n for n in row if n in parent]
    assert len(held) == 3
    denom = sum(parent[n] for n in held)
    for n in held:
        assert row[n] == pytest.approx(parent[n] / denom, abs=1e-12)


def test_rows_to_panel_matches_the_register_builders_mechanics():
    """Same reindex, same forward fill, same zeroing before eligible."""
    rebal = pd.DatetimeIndex(["2026-09-04", "2026-09-11"])
    idx = pd.bdate_range("2026-09-03", "2026-09-15")
    rows = {rebal[0]: {"AAA": 0.6, "BBB": 0.4}, rebal[1]: {"AAA": 1.0}}
    panel = rows_to_panel(rows, idx, rebal, pd.Timestamp("2026-09-04"))
    assert panel.loc[pd.Timestamp("2026-09-03")].sum() == 0.0     # before eligible
    assert panel.loc[pd.Timestamp("2026-09-04"), "AAA"] == pytest.approx(0.6)
    assert panel.loc[pd.Timestamp("2026-09-10"), "BBB"] == pytest.approx(0.4)
    assert panel.loc[pd.Timestamp("2026-09-14"), "BBB"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Weekly series and the cost split
# ---------------------------------------------------------------------------

def test_weekly_returns_partition_the_timeline():
    """The cumulative weekly index needs a PARTITION, so each interval is
    (previous rebalance, this rebalance] and no day is counted twice or lost."""
    idx = pd.bdate_range("2026-09-04", "2026-09-18")
    daily = pd.Series(0.001, index=idx)
    rebal = pd.DatetimeIndex(["2026-09-04", "2026-09-11", "2026-09-18"])
    w = weekly_returns_from_daily(daily, rebal)
    assert list(w.index) == list(rebal[1:])
    assert w.iloc[0] == pytest.approx(1.001 ** 5 - 1)      # 5 business days
    # The whole series compounds back to the full-period return, no gaps.
    total = float((1.0 + daily.loc[rebal[0]:].iloc[1:]).prod() - 1.0)
    assert float((1.0 + w).prod() - 1.0) == pytest.approx(total, abs=1e-12)


def test_weekly_returns_absorb_a_skipped_holiday_week():
    """The deployed calendar drops holiday-week rebalances, so a fortnight falls
    between two of them. A fixed six-day window would silently discard a week of
    return from the index; the partition keeps it in one longer interval."""
    idx = pd.bdate_range("2026-12-18", "2027-01-08")
    daily = pd.Series(0.002, index=idx)
    # Friday 2027-01-01 is an NYSE holiday, so the calendar jumps a fortnight.
    rebal = pd.DatetimeIndex(["2026-12-18", "2026-12-25", "2027-01-08"])
    w = weekly_returns_from_daily(daily, rebal)
    assert len(w) == 2
    n_days = len(daily.loc[rebal[1]:rebal[2]]) - 1
    assert n_days > 5, "the fixture must actually span the fortnight"
    assert w.iloc[1] == pytest.approx(1.002 ** n_days - 1)


def test_net_daily_matches_simulate_arm_exactly():
    """The cost split is an optimisation, so it has to be arithmetically the same
    thing as the register's simulator, not merely close."""
    from single_name_impl import simulate_arm

    idx = pd.bdate_range("2026-01-05", periods=40)
    rng = np.random.default_rng(4)
    panel = pd.DataFrame(rng.uniform(0, 0.2, (40, 3)), index=idx,
                         columns=["AAA", "BBB", "CCC"])
    rets = pd.DataFrame(rng.normal(0, 0.01, (40, 3)), index=idx,
                        columns=["AAA", "BBB", "CCC"])
    gross, turn = st.gross_and_turnover(panel, rets)
    for bps in (0, 2, 10, 20):
        mine = st.net_daily(gross, turn, bps)
        theirs = simulate_arm(panel, rets, bps)["daily"]
        pd.testing.assert_series_equal(mine, theirs, check_names=False)


def test_recording_basket_fn_returns_the_registers_own_selection():
    """The recorder must not alter what the builder sees — it records and passes
    through, or I1 in this study is not the WS6 I1."""
    from single_name_impl import ARM_BY_ID, precompute_member_signals, select_basket

    idx = pd.bdate_range("2018-01-02", periods=820)
    steps = np.arange(len(idx))
    panel = pd.DataFrame({f"N{i}": 100.0 * (1 + g) ** steps
                          for i, g in enumerate([0.0016, 0.0014, 0.0012,
                                                 0.0010, -0.0012])}, index=idx)
    sig = precompute_member_signals(panel)
    snaps = {"2018-01-05": {"tickers": list(panel.columns)}}
    eff = idx[600]

    sink = {}
    got = st.recording_basket_fn(sink)(ARM_BY_ID["I1"], eff, snaps, panel, sig,
                                       line="SOXX", rebal_date=idx[601])
    want = select_basket(ARM_BY_ID["I1"], eff, snaps, panel, sig)
    assert got.weights == want.weights
    assert sink[("SOXX", idx[601])].weights == want.weights


# ---------------------------------------------------------------------------
# §5.1-5.2 — the gates
# ---------------------------------------------------------------------------

def test_percentile_rank_is_the_fraction_at_or_below():
    null = list(range(100))          # 0..99
    assert percentile_rank(-1, null) == pytest.approx(0.0)
    assert percentile_rank(9, null) == pytest.approx(0.10)
    assert percentile_rank(49, null) == pytest.approx(0.50)
    assert percentile_rank(999, null) == pytest.approx(1.0)


def test_gate_passes_only_in_the_favourable_tail():
    """Lower is better for both statistics, so a pass is a LOW rank."""
    null = np.linspace(0.10, 0.40, 1000)          # placebo max drawdowns
    assert gate("G1", 0.11, null)["passed"] is True       # shallow: good
    assert gate("G1", 0.25, null)["passed"] is False      # middling
    assert gate("G1", 0.39, null)["passed"] is False      # deep: bad


def test_gate_boundary_is_at_or_below_the_tenth_percentile():
    """Ties count as a pass, the conservative direction at the boundary."""
    null = list(range(1000))
    assert gate("G1", 99, null)["passed"] is True         # rank exactly 0.100
    assert gate("G1", 100, null)["passed"] is False       # rank 0.101


def test_gate_reports_the_null_shape_not_only_the_verdict():
    g = gate("G1", 0.2, np.linspace(0.1, 0.5, 500))
    for k in ("null_n", "null_p10", "null_p50", "null_p90", "percentile_rank"):
        assert g[k] is not None
    assert g["null_n"] == 500
    assert g["pass_line"] == 0.10


# ---------------------------------------------------------------------------
# §5.3 — episodes and the thinness gate
# ---------------------------------------------------------------------------

def test_drawdown_episodes_finds_a_simple_peak_trough_recovery():
    # +10%, -20%, +30%: one episode, trough at week 1, recovered at week 2.
    eps = drawdown_episodes([0.10, -0.20, 0.30])
    assert len(eps) == 1
    assert eps[0]["trough"] == 1
    assert eps[0]["recovered"] is True
    assert eps[0]["depth"] == pytest.approx(-0.20)


def test_an_unrecovered_drawdown_is_kept_not_discarded():
    """It is usually the deepest, so dropping it would bias every attribution."""
    eps = drawdown_episodes([0.10, -0.30, -0.10])
    assert len(eps) == 1
    assert eps[0]["recovered"] is False
    assert eps[0]["end"] == 2


def test_a_series_that_only_rises_has_no_episodes():
    assert drawdown_episodes([0.01, 0.02, 0.03]) == []


def test_episode_attribution_shares_sum_to_one():
    ref = [0.05, -0.20, 0.30, -0.15, 0.25, -0.10, 0.12]
    a = episode_attribution(ref, [0.05, -0.18, 0.30, -0.14, 0.25, -0.09, 0.12],
                            [0.05, -0.20, 0.30, -0.15, 0.25, -0.10, 0.12])
    assert a["n_episodes"] >= 1
    assert sum(r["share"] for r in a["episodes"]) == pytest.approx(1.0)


def test_thinness_fires_when_one_episode_carries_the_effect():
    """The 2026-07-15-crypto-breadth-7 pattern: a gain that is one window."""
    attribution = {"episodes": [{"share": 0.80}, {"share": 0.12}, {"share": 0.08}]}
    t = thinness(attribution)
    assert t["thin"] is True
    assert "one episode carries" in t["reasons"][0]


def test_thinness_fires_when_too_few_episodes_contribute():
    attribution = {"episodes": [{"share": 0.55}, {"share": 0.40}, {"share": 0.05}]}
    t = thinness(attribution)
    assert t["thin"] is True
    assert any("contribute more than" in r for r in t["reasons"])


def test_thinness_passes_on_a_broadly_spread_effect():
    attribution = {"episodes": [{"share": 0.30}, {"share": 0.28},
                                {"share": 0.24}, {"share": 0.18}]}
    t = thinness(attribution)
    assert t["thin"] is False and t["reasons"] == []
    assert t["n_contributing_episodes"] == 4


def test_thinness_counts_an_episode_where_the_exclusion_HURT():
    """Shares are of the total ABSOLUTE difference, so a harmful episode counts
    towards concentration rather than silently cancelling a helpful one."""
    a = episode_attribution([0.05, -0.20, 0.30, -0.15, 0.25],
                            [0.05, -0.10, 0.30, -0.25, 0.25],   # helps then hurts
                            [0.05, -0.20, 0.30, -0.15, 0.25])
    assert a["total_abs_difference"] > 0
    assert all(r["share"] >= 0 for r in a["episodes"])


# ---------------------------------------------------------------------------
# The verdict rule — every branch, and what cannot reach it
# ---------------------------------------------------------------------------

def _g(passed: bool, rank: float = 0.05) -> dict:
    return {"gate": "G", "passed": passed,
            "percentile_rank": rank if passed else 0.55}


def test_verdict_confirmed_needs_both_gates_and_a_broad_effect():
    v = verdict(_g(True), _g(True), {"thin": False, "reasons": []})
    assert v["outcome"] == "CONFIRMED"


def test_verdict_conditional_on_one_gate():
    v = verdict(_g(True), _g(False), {"thin": False, "reasons": []})
    assert v["outcome"] == "CONDITIONAL"
    assert "G2 downside deviation" in v["reason"]
    v2 = verdict(_g(False), _g(True), {"thin": False, "reasons": []})
    assert v2["outcome"] == "CONDITIONAL"
    assert "G1 max drawdown" in v2["reason"]


def test_verdict_rejected_when_neither_gate_passes():
    v = verdict(_g(False), _g(False), {"thin": False, "reasons": []})
    assert v["outcome"] == "REJECTED"
    assert "count-matched random drop" in v["reason"]


def test_thinness_overrides_a_double_pass():
    """§5.3: the gate that can override a pass, and the one most likely to bind."""
    v = verdict(_g(True), _g(True), {"thin": True, "reasons": ["one episode"]})
    assert v["outcome"] == "INCONCLUSIVE"
    assert "overrides" in v["reason"]
    # The gates are still reported, so the override is auditable.
    assert v["g1_passed"] is True and v["g2_passed"] is True


def test_every_verdict_carries_its_scope():
    """In-sample, seen window, authorises nothing — stated on the verdict itself
    so it cannot be dropped when the number is quoted later."""
    for g1, g2, thin in ((True, True, False), (False, False, False),
                         (True, True, True)):
        v = verdict(_g(g1), _g(g2), {"thin": thin, "reasons": ["x"] if thin else []})
        assert "never about forward" in v["scope"]
        assert "Authorises nothing" in v["scope"]


def test_the_verdict_cannot_read_the_seen_comparison():
    """Guard 6.1, structurally: verdict() takes the two placebo gates and the
    thinness gate. I0 is not a parameter, so however tempting the
    I1X-versus-I0 number turns out to be, it cannot reach the outcome."""
    import inspect
    params = set(inspect.signature(verdict).parameters)
    assert params == {"g1", "g2", "thin"}
    assert "i0" not in " ".join(params).lower()


def test_the_seen_data_caveat_names_the_actual_figures():
    c = seen_data_caveat()
    assert "28.0%" in c and "30.3%" in c
    assert "not evidence" in c


# ---------------------------------------------------------------------------
# §5.5 — the descriptive block
# ---------------------------------------------------------------------------

def test_summarise_arm_reports_sharpe_but_names_it_unbarred():
    s = summarise_arm([0.01, -0.02, 0.015, 0.004])
    assert "sharpe_annualised_no_bar" in s
    assert "sharpe" not in [k for k in s if not k.endswith("_no_bar")]
    assert s["max_drawdown"] is not None
    assert s["downside_deviation"] is not None
    assert s["n_weeks"] == 4


def test_concentration_moves_the_right_way_when_names_are_dropped():
    """Guard 6.3's reported pair."""
    wide = concentration({f"N{i}": 0.1 for i in range(10)})
    narrow = concentration({f"N{i}": 0.2 for i in range(5)})
    assert wide["effective_n"] == pytest.approx(10.0)
    assert narrow["effective_n"] == pytest.approx(5.0)
    assert narrow["max_weight"] > wide["max_weight"]
    assert concentration({}) == {"effective_n": None, "max_weight": None,
                                 "n_names": 0}


# ---------------------------------------------------------------------------
# Window and date boundaries (house rule: one month, one year)
# ---------------------------------------------------------------------------

def test_the_window_is_the_ws6_register_window():
    assert st.WINDOW_START == pd.Timestamp("2018-10-12")
    assert st.WINDOW_END == pd.Timestamp("2026-06-30")
    assert date(2018, 10, 12).weekday() == 4, "the window opens on a Friday"


def test_episode_indices_survive_a_month_boundary():
    """Edge case 1 of 2. Episodes are positional on the weekly series, so a month
    turn inside a drawdown must not split or drop one. Weeks ending Fridays
    2026-09-25 through 2026-10-16 — month 10 is October, months being 1-indexed."""
    weeks = pd.DatetimeIndex(["2026-09-25", "2026-10-02", "2026-10-09",
                              "2026-10-16"])
    assert all(d.weekday() == 4 for d in weeks)
    assert weeks[0].month == 9 and weeks[1].month == 10
    eps = drawdown_episodes([0.02, -0.10, -0.05, 0.20])
    assert len(eps) == 1 and eps[0]["trough"] == 2


def test_episode_indices_survive_a_year_boundary():
    """Edge case 2 of 2. Friday 2027-01-01 is an NYSE holiday the deployed
    calendar drops, so the weeks straddling the turn are 2026-12-25 and
    2027-01-08."""
    weeks = pd.DatetimeIndex(["2026-12-18", "2026-12-25", "2027-01-08"])
    assert all(d.weekday() == 4 for d in weeks)
    assert date(2027, 1, 1).weekday() == 4        # the dropped holiday Friday
    eps = drawdown_episodes([0.01, -0.15, 0.30])
    assert len(eps) == 1 and eps[0]["recovered"] is True
