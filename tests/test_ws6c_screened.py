"""Tests for the WS6c screened arm (I1X) and its guard layer.

Registration: ``C:\\dev\\KICKOFF_ws6c-screened-arm.md``, frozen at vault-docs
``cc84122``. The house unattended-agent rule is that no scheduled run may be
trusted without a guard that can catch a silently-wrong step, so every failure
branch is driven here, not just the happy path — a guard that has only ever been
seen to pass is not evidence.

Offline and synthetic throughout.

NOTE (vault CLAUDE.md date rule): every date below is built with a date library,
never by manual day arithmetic. Python ``datetime`` months are 1-INDEXED (so
``date(2027, 1, 8)`` is January), unlike JavaScript's 0-indexed months. Weekdays
are asserted against ``weekday()`` (Monday = 0, so Friday = 4), never recalled.
"""

from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import ws6c_screened as sc  # noqa: E402
from single_name_impl import (  # noqa: E402
    ARM_BY_ID,
    BROAD_SLICES,
    MIN_PASS,
    M_POOL,
    SINGLE_NAMED_LINES,
    BasketResult,
    build_arm_name_weights,
    demean,
    deployed_eligible_start,
    precompute_member_signals,
    select_basket,
)
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
from ws6b_shadow import GuardResult  # noqa: E402
from ws6c_screened import (  # noqa: E402
    ScreenedWeek,
    append_week,
    check_constants_unchanged,
    check_identity,
    check_inert,
    check_paired,
    check_return_sanity,
    check_subset_invariant,
    check_weighting_basis,
    constants,
    constants_line,
    downside_deviation,
    evaluate_week,
    line_identity,
    max_drawdown,
    name_week_returns,
    overbought_k,
    screened_basket_fn,
    select_screened_basket,
    status,
    verify_log_chain,
    within_line_weights,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures (same idiom as tests/test_single_name_impl.py)
# ---------------------------------------------------------------------------

def _mem_index(n_days: int = 820, start: str = "2018-01-02") -> pd.DatetimeIndex:
    """Member calendar with a lead-in so the 200d SMA is warm."""
    return pd.date_range(start, periods=n_days, freq="B")


def _monotone_panel(growths: dict[str, float], index: pd.DatetimeIndex,
                    base: float = 100.0) -> pd.DataFrame:
    """Deterministic panel: column t = base * (1 + g)^step. Positive g sits above
    its own 200d SMA (state True) and gives a strictly higher strength than a
    smaller positive g, so both the screen and the strength ranking are exact.
    Two columns sharing a g are bit-identical, which is how the tie-break test
    manufactures an EXACT tie rather than a near one."""
    steps = np.arange(len(index))
    return pd.DataFrame({tk: base * (1.0 + g) ** steps for tk, g in growths.items()},
                        index=index)


def _snapshots(tickers: list[str], key: str = "2018-01-05") -> dict:
    return {key: {"actual_date": key, "n_tickers": len(tickers),
                  "tickers": list(tickers)}}


def _weights_table(tickers: list[str], key: str = "2018-01-05") -> dict:
    """An A3 Step-0 weight table with DISTINCT, non-uniform weights, so a
    pro-rata renormalisation is distinguishable from an equal-weight fallback."""
    return {pd.Timestamp(key): {t: float(10 + 3 * i) for i, t in enumerate(tickers)}}


def _roster_line(n_pool_risers: int = 8, n_pool_fallers: int = 7,
                 n_outside: int = 5, hot_outside: bool = True):
    """One line's roster, prices, signals and A3 weights.

    Cap-rank order is pool risers, pool fallers, then the names beyond M_POOL.
    ``hot_outside`` puts the two strongest names OUTSIDE the 15-name pool, which
    is the configuration that isolates §3.3's eligible-set choice: the overbought
    ranks are filled by roster names the basket never held, so no pool name is
    excluded and the arm must reduce to the base I1 screen exactly.
    """
    growths: dict[str, float] = {}
    # Pool risers, descending strength: 0.0016, 0.0015, ...
    for i in range(n_pool_risers):
        growths[f"PU{i:02d}"] = 0.0016 - 0.0001 * i
    for i in range(n_pool_fallers):
        growths[f"PD{i:02d}"] = -0.0011 - 0.0001 * i
    # Outside the pool. Hot = strictly stronger than every pool name.
    for i in range(n_outside):
        growths[f"OU{i:02d}"] = (0.0030 - 0.0001 * i) if hot_outside \
            else (0.0004 - 0.0001 * i)
    roster = ([f"PU{i:02d}" for i in range(n_pool_risers)]
              + [f"PD{i:02d}" for i in range(n_pool_fallers)]
              + [f"OU{i:02d}" for i in range(n_outside)])
    assert len(roster) - n_outside == M_POOL, "pool must be exactly M_POOL deep"
    idx = _mem_index()
    panel = _monotone_panel(growths, idx)
    return {"roster": roster, "prices": panel,
            "sig": precompute_member_signals(panel),
            "snaps": _snapshots(roster), "weights": _weights_table(roster),
            "eff": idx[600]}


def _select(fx, **kw):
    return select_screened_basket(ARM_BY_ID["I1"], fx["eff"], fx["snaps"],
                                  fx["prices"], fx["sig"],
                                  weights=kw.get("weights", fx["weights"]))


# ---------------------------------------------------------------------------
# §3.3 — the decile arithmetic
# ---------------------------------------------------------------------------

def test_decile_k_matches_the_documents_worked_cases():
    """The four cases §3.3 states, and why they are the four.

    The silent-wrongness here is the ROUNDING RULE, not float representation:
    ``math.ceil(0.10 * n)`` happens to agree with exact arithmetic for every n up
    to 2,000,000 (checked), so a float ceiling would have been right. What is NOT
    right is any of the three plausible near-misses, and the document's cases
    discriminate all of them:

      * truncation, ``int(0.10 * n)``     — wrong at 15 (1) and 22 (2)
      * banker's rounding, ``round(...)`` — wrong at 15 (2 by luck) and 22 (2)
      * "add one", ``int(0.10 * n) + 1``  — wrong at 30 (4) and 70 (8)

    n = 30 and n = 70 are the exact multiples of ten, and they exist in the list
    precisely to catch an implementation that rounds a whole number up.
    """
    assert overbought_k(15) == 2
    assert overbought_k(22) == 3
    assert overbought_k(30) == 3
    assert overbought_k(70) == 7
    # The three near-misses, stated as executable facts rather than comments.
    assert int(0.10 * 22) == 2 != overbought_k(22)
    assert round(0.10 * 22) == 2 != overbought_k(22)
    assert int(0.10 * 30) + 1 == 4 != overbought_k(30)


def test_decile_k_never_rounds_to_zero_on_a_populated_line():
    """§3.3: 'ceil ... never rounds to zero on a populated line'."""
    for n in range(1, 200):
        assert overbought_k(n) >= 1
    assert overbought_k(0) == 0               # an empty cross-section excludes none


def test_decile_k_is_monotone_and_exactly_a_tenth_rounded_up():
    """Swept against exact integer arithmetic across the whole plausible range of
    roster sizes, so a float creeping back in is caught wherever it first bites
    rather than only at the four documented cases."""
    prev = 0
    for n in range(0, 300):
        k = overbought_k(n)
        assert k == (n + 9) // 10             # ceil(n/10) in exact integers
        assert k >= prev
        prev = k


# ---------------------------------------------------------------------------
# §3.3 — the eligible set, the ranking and the tie-break
# ---------------------------------------------------------------------------

def test_eligible_set_is_the_full_roster_not_the_pool():
    """§3.3's central choice. The roster is 20 names, so n = 20 and k = 2 — if
    the eligible set were the 15-name pool, n would be 15 and k would still be 2,
    but the two overbought names would be pool names. The fixture puts the two
    strongest names OUTSIDE the pool precisely so the two readings diverge."""
    fx = _roster_line(hot_outside=True)
    sel = _select(fx)
    assert sel.n_eligible == 20
    assert sel.k == 2
    assert sel.overbought == ["OU00", "OU01"]      # both beyond the pool
    assert sel.overbought_excluded == []           # so nothing is dropped


def test_exclusion_applies_to_pool_names_only_and_only_after_the_state_screen():
    """Two halves of §3.3 in one fixture.

    Pool-only: the overbought set is drawn from the whole roster, but only a POOL
    name can be excluded from a basket that only ever holds pool names.
    After-the-screen: a faller in the overbought set changes nothing, because it
    failed the state screen already and was never a survivor to exclude.
    """
    fx = _roster_line(hot_outside=False)     # strongest names are pool risers now
    sel = _select(fx)
    assert sel.overbought == ["PU00", "PU01"]
    assert sel.overbought_excluded == ["PU00", "PU01"]      # pool names, dropped
    # Pool-only: nothing beyond the 15-name pool can ever be excluded from a
    # basket that only ever held pool names.
    assert set(sel.overbought_excluded) <= set(fx["roster"][:M_POOL])
    # After the screen: every excluded name passed the state screen first, and
    # the fallers were removed by the SCREEN, never counted as an exclusion.
    assert all(s not in sel.state_failed for s in sel.overbought_excluded)
    assert all(n.startswith("PD") for n in sel.state_failed)
    assert not (set(sel.overbought_excluded) & set(sel.state_failed))


def test_a_state_failed_name_in_the_overbought_set_is_not_double_counted():
    """The ordering §3.3 fixes, in its sharpest form: when EVERY roster name is
    below its SMA the overbought set is non-empty (the least-weak name still
    ranks first) but nothing passed the state screen, so the exclusion has
    nothing to act on. An implementation that excluded before screening — or
    that counted the same name under both filters — would show it here."""
    idx = _mem_index()
    growths = {f"DN{i:02d}": -0.0008 - 0.0001 * i for i in range(10)}
    panel = _monotone_panel(growths, idx)
    sig = precompute_member_signals(panel)
    roster = list(growths)
    sel = select_screened_basket(ARM_BY_ID["I1"], idx[600], _snapshots(roster),
                                 panel, sig, weights=_weights_table(roster))
    assert sel.n_eligible == 10 and sel.k == 1
    assert sel.overbought == ["DN00"]         # least weak, still overbought-ranked
    assert sel.overbought_excluded == []      # but it never passed the screen
    assert set(sel.state_failed) == set(roster)
    assert sel.fallback and sel.survivors == []


def test_overbought_ranking_is_strength_descending():
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    strength = fx["sig"]["strength"].loc[fx["eff"]]
    ranked = sel.overbought
    assert strength[ranked[0]] >= strength[ranked[1]]
    # and both beat every name outside the overbought set
    rest = [s for s in fx["roster"] if s not in ranked
            and pd.notna(strength.get(s))]
    assert min(strength[r] for r in ranked) >= max(strength[r] for r in rest)


def test_exact_tie_breaks_towards_the_earlier_cap_rank():
    """§3.3: 'an exact tie is broken in favour of the name earlier in the
    snapshot's cap-rank order'. Two names share a growth rate, so their prices,
    SMAs and strengths are bit-identical; with n = 10 the decile is k = 1, so
    exactly one of the pair can be overbought and the tie-break decides which."""
    idx = _mem_index()
    growths = {"TIEA": 0.0016, "TIEB": 0.0016}      # the exact tie
    for i in range(8):
        growths[f"OTH{i}"] = 0.0009 - 0.0001 * i
    panel = _monotone_panel(growths, idx)
    sig = precompute_member_signals(panel)
    strength = sig["strength"].loc[idx[600]]
    assert strength["TIEA"] == strength["TIEB"], "the fixture must tie exactly"

    # TIEA listed first in cap-rank order -> TIEA is the overbought one.
    roster = ["TIEA", "TIEB"] + [f"OTH{i}" for i in range(8)]
    sel = select_screened_basket(ARM_BY_ID["I1"], idx[600], _snapshots(roster),
                                 panel, sig, weights=_weights_table(roster))
    assert sel.n_eligible == 10 and sel.k == 1
    assert sel.overbought == ["TIEA"]

    # Reverse the cap-rank order and the tie-break follows it, not the ticker.
    roster_rev = ["TIEB", "TIEA"] + [f"OTH{i}" for i in range(8)]
    sel_rev = select_screened_basket(ARM_BY_ID["I1"], idx[600],
                                     _snapshots(roster_rev), panel, sig,
                                     weights=_weights_table(roster_rev))
    assert sel_rev.overbought == ["TIEB"]


def test_a_name_without_a_defined_strength_is_not_eligible():
    """§3.3 eligibility needs a DEFINED strength at t-1. A name with no price
    history has NaN strength and must not enter the cross-section — otherwise n
    inflates and k with it, and the decile silently widens."""
    fx = _roster_line(hot_outside=True)
    prices = fx["prices"].copy()
    prices.loc[:, "OU04"] = np.nan
    sig = precompute_member_signals(prices)
    sel = select_screened_basket(ARM_BY_ID["I1"], fx["eff"], fx["snaps"],
                                 prices, sig, weights=fx["weights"])
    assert sel.n_eligible == 19          # 20 roster names less the blank one
    assert "OU04" not in sel.overbought


# ---------------------------------------------------------------------------
# §3.2 — the base screen is WS6 arm I1, reused verbatim (drift guard B)
# ---------------------------------------------------------------------------

def test_reduces_exactly_to_arm_I1_when_no_pool_name_is_overbought():
    """The drift guard. §3.3 forces the exclusion BETWEEN the state screen and
    MIN_PASS, so this arm cannot be a post-filter on ``select_basket`` and has to
    re-walk the pipeline. When the overbought names all sit outside the pool the
    screened arm IS arm I1, so any divergence in coverage, resolution,
    missing-price handling or A3 weighting shows up here as an inequality."""
    fx = _roster_line(hot_outside=True)
    screened = _select(fx)
    i1 = select_basket(ARM_BY_ID["I1"], fx["eff"], fx["snaps"], fx["prices"],
                       fx["sig"], weights=fx["weights"])
    assert not screened.fallback and not i1.fallback
    assert screened.basket.weights == i1.weights
    assert screened.basket.weight_source == i1.weight_source
    assert screened.basket.n_pass == i1.n_pass
    assert screened.basket.n_selected == i1.n_selected


def test_unscreened_side_is_the_engines_own_I0_basket():
    """w_x is 'measured in the UNSCREENED basket' (§3.3), and the subset
    invariant is checked against the arm this one pairs with — so the I0 side is
    taken from the engine rather than rebuilt."""
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    i0 = select_basket(ARM_BY_ID["I0"], fx["eff"], fx["snaps"], fx["prices"],
                       fx["sig"], weights=fx["weights"])
    assert sel.i0_weights == i0.weights
    assert set(sel.survivors) <= set(i0.weights)


# ---------------------------------------------------------------------------
# §3.3 — MIN_PASS after BOTH filters
# ---------------------------------------------------------------------------

def test_min_pass_applies_after_both_filters_and_reverts_the_line():
    """Four risers pass the state screen; the exclusion removes two, leaving two
    survivors — below MIN_PASS = 3, so the line reverts to its ETF for the week.
    The base screen alone would have kept it, which is the whole point: the valve
    is applied to the survivors of BOTH filters."""
    fx = _roster_line(n_pool_risers=4, n_pool_fallers=11, n_outside=5,
                      hot_outside=False)
    i1 = select_basket(ARM_BY_ID["I1"], fx["eff"], fx["snaps"], fx["prices"],
                       fx["sig"], weights=fx["weights"])
    assert not i1.fallback and i1.n_pass == 4      # the base screen holds it

    sel = _select(fx)
    assert sel.k == 2 and sel.overbought_excluded == ["PU00", "PU01"]
    assert len(sel.survivors) == 2
    assert sel.fallback
    assert f"< {MIN_PASS}" in sel.reason
    assert not sel.basket.weights                  # nothing held, it is the ETF


def test_min_pass_boundary_holds_the_line_at_exactly_three():
    fx = _roster_line(n_pool_risers=5, n_pool_fallers=10, n_outside=5,
                      hot_outside=False)
    sel = _select(fx)
    assert sel.k == 2 and len(sel.survivors) == MIN_PASS
    assert not sel.fallback and len(sel.basket.weights) == 3


def test_i0_fallback_is_inherited_verbatim():
    """No unscreened basket to screen: the line is its ETF in BOTH arms, and the
    reason is the engine's, not an invented screened-arm one."""
    fx = _roster_line()
    sel = select_screened_basket(ARM_BY_ID["I1"], None, fx["snaps"],
                                 fx["prices"], fx["sig"], weights=fx["weights"])
    assert sel.fallback and sel.reason == "no effective date (pre-window)"
    assert sel.i0_weights == {}


# ---------------------------------------------------------------------------
# §3.4 — pro-rata redistribution
# ---------------------------------------------------------------------------

def test_weights_sum_to_one_and_are_the_unscreened_weights_renormalised():
    """§3.4: 'the basket is the survivors' true snapshot weights renormalised to
    one'. Nothing goes to cash or to the ETF, so the excluded weight lands on the
    remaining survivors PRO RATA — which is the same thing as renormalising the
    unscreened weights over the survivor set."""
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    w = sel.basket.weights
    assert sel.overbought_excluded, "the fixture must actually exclude something"
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-12)

    denom = sum(sel.i0_weights[s] for s in sel.survivors)
    expected = {s: sel.i0_weights[s] / denom for s in sel.survivors}
    assert set(w) == set(expected)
    for s in w:
        assert w[s] == pytest.approx(expected[s], abs=1e-12)
    # A3 weights are non-uniform, so this is a real renormalisation and not an
    # equal-weight fallback wearing its clothes.
    assert len(set(round(v, 12) for v in w.values())) > 1
    assert sel.basket.weight_source == "snapshot"


def test_no_weight_leaks_to_cash_or_to_the_etf():
    """The two treatments §3.4 rejected. Every unit of the line's weight stays in
    surviving names — the line remains fully invested at E0's line weight."""
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    assert set(sel.basket.weights) == set(sel.survivors)
    assert not (set(sel.basket.weights) & set(SINGLE_NAMED_LINES))
    assert not (set(sel.basket.weights) & set(BROAD_SLICES))
    assert sum(sel.basket.weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_excluded_weight_is_measured_in_the_unscreened_basket():
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    assert sel.w_overbought == pytest.approx(
        sum(sel.i0_weights[s] for s in sel.overbought_excluded), abs=1e-15)
    # w_x is §3.5's X — the state failures AND the overbought exclusions, which
    # together with the survivors partition the I0 basket (implementation note 1).
    assert sel.w_x == pytest.approx(
        sum(sel.i0_weights[s] for s in sel.dropped_vs_i0), abs=1e-15)
    assert set(sel.dropped_vs_i0) | set(sel.survivors) == set(sel.i0_weights)
    assert not (set(sel.dropped_vs_i0) & set(sel.survivors))
    assert sel.w_x >= sel.w_overbought


# ---------------------------------------------------------------------------
# §3.5 — the identity
# ---------------------------------------------------------------------------

def _returns_for(sel, seed=20260909):
    rng = np.random.default_rng(seed)
    return {s: float(r) for s, r in
            zip(sel.i0_weights, rng.normal(0.004, 0.03, len(sel.i0_weights)))}


def test_identity_holds_numerically():
    """r_I1X - r_I0 == w_x * (r_S - r_x), to 1e-12."""
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    ident = line_identity(sel, _returns_for(sel))
    assert ident["w_x"] > 0
    assert ident["lhs"] == pytest.approx(ident["rhs"], abs=1e-12)
    assert abs(ident["residual"]) <= sc.IDENTITY_TOL
    assert ident["spread"] == pytest.approx(ident["r_s"] - ident["r_x"], abs=1e-15)


def test_identity_logs_concentration_for_both_arms():
    """§3.5 / guard 6.2: the effective number of names and the largest single
    weight, for BOTH arms, so a profile difference can be read against
    concentration rather than attributed to selection by default."""
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    ident = line_identity(sel, _returns_for(sel))
    c0, c1 = ident["concentration_i0"], ident["concentration_i1x"]
    assert c1["n_names"] < c0["n_names"]
    assert c1["effective_n"] < c0["effective_n"]
    assert c1["max_weight"] > c0["max_weight"]
    assert c0["effective_n"] == pytest.approx(
        1.0 / sum(w * w for w in sel.i0_weights.values()))


def test_identity_guard_fires_on_a_broken_decomposition():
    """A residual means the screened weights are not the unscreened weights
    renormalised over the survivors, so w_x and the spread describe a book that
    was not held. Fatal, because guard 6.2 rests on this decomposition."""
    g = GuardResult(publishable=True)
    check_identity({"SOXX": {"residual": 1e-14}}, g)
    assert g.publishable

    g = GuardResult(publishable=True)
    check_identity({"SOXX": {"residual": 1e-9}}, g)
    assert not g.publishable
    assert "identity_3_5" in g.failures[0]


def test_identity_guard_is_green_when_no_line_carried_it():
    g = GuardResult(publishable=True)
    check_identity({}, g)
    assert g.publishable and g.checks["identity_3_5"]["ok"]


def test_mismatched_a3_basis_is_named_rather_than_left_to_be_inferred():
    """``_true_basket_weights`` drops a whole line-week to equal weight when any
    selected member lacks a usable weight. The arms select different members, so
    one can land on 'snapshot' and the other on 'ew' — a weighting-basis
    difference masquerading as a composition difference."""
    g = GuardResult(publishable=True)
    check_weighting_basis(
        {"SOXX": {"weight_source_i0": "ew", "weight_source_i1x": "snapshot"}}, g)
    assert g.publishable                      # warns, does not block
    assert any("A3 basis" in w for w in g.warnings)


# ---------------------------------------------------------------------------
# §6.3(i) — pairing
# ---------------------------------------------------------------------------

def test_pairing_passes_within_tolerance():
    g = GuardResult(publishable=True)
    assert check_paired(0.0123456789, 0.0123456789 + 5e-10, g)
    assert g.publishable


def test_unpaired_on_a_1e_8_reconciliation_miss():
    """§6.3(i)'s tolerance is 1e-9. A 1e-8 miss means the screened arm did not
    reproduce the WS6b record's own I0 from the same inputs, so the two are not
    on the same book and the week must not enter the §4.1 statistics."""
    g = GuardResult(publishable=True)
    assert not check_paired(0.0123456789, 0.0123456789 + 1e-8, g)
    assert not g.publishable
    assert "UNPAIRED" in g.checks["paired_to_ws6b"]["detail"]


def test_the_ws6b_record_hash_is_sealed_into_the_screened_record():
    """§6.3(i): 'that record's hash is sealed into the screened record'. Inside
    the hashed payload, so a screened week cannot later be re-pointed at a
    different WS6b week without breaking its own chain."""
    recs = append_week([], _week(ws6b_record_hash="abc123"))
    ok, _ = verify_log_chain(recs)
    assert ok
    tampered = copy.deepcopy(recs)
    tampered[0]["ws6b_record_hash"] = "def456"
    ok, detail = verify_log_chain(tampered)
    assert not ok and "altered" in detail


# ---------------------------------------------------------------------------
# §6.3(ii) — the subset invariant
# ---------------------------------------------------------------------------

def test_subset_invariant_passes_on_a_real_selection():
    fx = _roster_line(hot_outside=False)
    g = GuardResult(publishable=True)
    check_subset_invariant({"SOXX": _select(fx)}, g)
    assert g.publishable


def test_subset_invariant_failure_is_fatal():
    """A survivor outside the I0 basket means the screened arm holds something
    the unscreened arm never did, so a difference between them is no longer the
    screen. Must block the week, not warn."""
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    sel.survivors = sel.survivors + ["NOT_IN_I0"]
    g = GuardResult(publishable=True)
    check_subset_invariant({"SOXX": sel}, g)
    assert not g.publishable
    assert "NOT_IN_I0" in g.checks["subset_invariant"]["detail"]


# ---------------------------------------------------------------------------
# §6.3(iii) — inert line-weeks
# ---------------------------------------------------------------------------

def test_inert_lines_warn_and_never_block():
    """A line the WS6b week carries on its ETF is on its ETF here identically.
    That is the screen having nothing to act on — information, not a fault."""
    g = GuardResult(publishable=True)
    assert not check_inert(["SOXX", "IUES"], ["IUCS"], g)
    assert g.publishable
    assert any("screen inert" in w for w in g.warnings)


def test_a_wholly_inert_week_is_kept_and_flagged_not_measurable():
    g = GuardResult(publishable=True)
    assert check_inert([], ["SOXX", "IUES", "IUCS"], g)
    assert g.publishable                       # kept in the record
    assert any("no effect measurable" in w for w in g.warnings)


def test_a_wholly_inert_week_is_excluded_from_the_4_1_statistics():
    recs = []
    for i, we in enumerate(["2026-09-11", "2026-09-18", "2026-09-25"]):
        w = _week(week_ending=we, all_inert=(i == 1), i1x_return=0.01,
                  i0_return=0.01)
        recs = append_week(recs, w)
        recs[-1]["publishable"] = True
    st = status(recs)
    assert st["n_published"] == 3
    assert st["n_inert"] == 1
    assert st["n_in_statistics"] == 2          # the inert week stays in the log
    assert st["primary"]["n"] == 2


# ---------------------------------------------------------------------------
# Guard 6.1 — the constants
# ---------------------------------------------------------------------------

def test_the_frozen_construction_is_exactly_what_the_document_says():
    c = constants()
    assert c["decile_fraction"] == 0.10
    assert c["decile_rounding"] == "ceil"
    assert c["eligible_set"] == "full_roster"
    assert c["excluded_weight_rule"] == "pro_rata"
    assert c["min_pass"] == MIN_PASS == 3
    assert c["m_pool"] == M_POOL == 15
    assert c["arm_id"] == "I1X"
    assert c["registration_frozen_at"] == "cc84122"


def test_constants_line_names_the_whole_construction():
    line = constants_line()
    for token in ("I1X", "0.10", "ceil", "full_roster", "MIN_PASS 3",
                  "pro_rata", "cc84122"):
        assert token in line, f"{token!r} missing from the weekly log line"


def test_a_record_sealed_with_different_constants_is_refused():
    """Guard 6.1. Re-tuned on the shadow record, the decile would become a fitted
    parameter with n equal to the record's length — so a week computed under a
    different construction cannot join the same series."""
    g = GuardResult(publishable=True)
    check_constants_unchanged(constants(), g)
    assert g.publishable

    for field_, value in (("decile_fraction", 0.20),
                          ("decile_rounding", "round"),
                          ("eligible_set", "pool"),
                          ("excluded_weight_rule", "cash")):
        sealed = {**constants(), field_: value}
        g = GuardResult(publishable=True)
        check_constants_unchanged(sealed, g)
        assert not g.publishable, field_
        assert field_ in g.checks["constants_unchanged"]["detail"]


def test_the_constants_are_sealed_inside_the_record_hash():
    recs = append_week([], _week())
    ok, _ = verify_log_chain(recs)
    assert ok
    tampered = copy.deepcopy(recs)
    tampered[0]["constants"]["decile_fraction"] = 0.20
    ok, detail = verify_log_chain(tampered)
    assert not ok and "altered" in detail


# ---------------------------------------------------------------------------
# The record and its chain
# ---------------------------------------------------------------------------

def _week(**kw) -> ScreenedWeek:
    base = dict(
        week_ending="2026-09-11", i1x_return=0.0125, i0_return=0.0120,
        e0_return=0.0115, gap_i1x_e0=0.0010, gap_i1x_i0=0.0005,
        turnover_i1x=0.32, turnover_i0=0.30,
        lines_held=["IUES", "SOXX", "IUFS"],
        lines_basketed=["IUES", "SOXX"], lines_inert=["IUFS"],
        lines_screen_fallback=[], all_inert=False,
        ws6b_i0_return=0.0120, ws6b_record_hash="0" * 64,
        paired=True, pairing_error=0.0,
        selection={}, attribution={}, weights_in_force_from="2026-09-04",
        unresolved_shared=[], unresolved_screen_specific=[],
        data_asof="2026-09-11", engine_commit="deadbee",
        params_sha="cafe1234", constants=constants())
    base.update(kw)
    return ScreenedWeek(**base)


def test_append_seals_each_week_into_the_chain():
    recs = append_week([], _week(week_ending="2026-09-11"))
    recs = append_week(recs, _week(week_ending="2026-09-18"))
    assert recs[1]["prev_hash"] == recs[0]["record_hash"]
    ok, detail = verify_log_chain(recs)
    assert ok, detail


def test_chain_detects_an_altered_published_week():
    recs = append_week([], _week(week_ending="2026-09-11"))
    recs = append_week(recs, _week(week_ending="2026-09-18"))
    tampered = copy.deepcopy(recs)
    tampered[0]["i1x_return"] = 0.0400        # quietly improve an old week
    ok, detail = verify_log_chain(tampered)
    assert not ok and ("altered" in detail or "chain break" in detail)


def test_chain_detects_reordering():
    recs = append_week([], _week(week_ending="2026-09-11"))
    recs = append_week(recs, _week(week_ending="2026-09-18"))
    ok, _ = verify_log_chain([recs[1], recs[0]])
    assert not ok


def test_append_refuses_to_rewrite_history():
    recs = append_week([], _week(week_ending="2026-09-18"))
    with pytest.raises(ValueError, match="append-only"):
        append_week(recs, _week(week_ending="2026-09-11"))
    with pytest.raises(ValueError, match="append-only"):
        append_week(recs, _week(week_ending="2026-09-18"))


# ---------------------------------------------------------------------------
# The full guard layer, end to end
# ---------------------------------------------------------------------------

def _ok_guard_args():
    fx = _roster_line(hot_outside=False)
    sel = _select(fx)
    return dict(
        selections={"IUES": sel, "SOXX": sel},
        line_weights={"IUES": 0.14, "SOXX": 0.13, "IUFS": 0.10},
        basket_weights={"IUES": {"PU02": 0.6, "PU03": 0.4},
                        "SOXX": {"PU02": 1.0}},
        e0_total_weight=0.37)


def test_a_clean_screened_week_is_publishable():
    g = evaluate_week(_week(), **_ok_guard_args())
    assert g.publishable, g.failures


def test_an_unpaired_week_is_not_publishable_end_to_end():
    g = evaluate_week(_week(ws6b_i0_return=0.0120 + 1e-8), **_ok_guard_args())
    assert not g.publishable
    assert any("paired_to_ws6b" in f for f in g.failures)


def test_an_off_sum_basket_blocks_the_week():
    args = _ok_guard_args()
    args["basket_weights"] = {"IUES": {"PU02": 0.6, "PU03": 0.3}}
    g = evaluate_week(_week(), **args)
    assert not g.publishable


def test_an_implausible_weekly_return_blocks_the_week():
    g = GuardResult(publishable=True)
    check_return_sanity({"i1x": 0.90, "i0": 0.01, "e0": 0.01}, g)
    assert not g.publishable
    assert "i1x" in g.checks["weekly_return_within_bound"]["detail"]


def test_a_large_but_plausible_weekly_return_passes():
    g = GuardResult(publishable=True)
    check_return_sanity({"i1x": -0.18, "i0": -0.17, "e0": -0.16}, g)
    assert g.publishable


# ---------------------------------------------------------------------------
# No look-ahead — the WS6 perturbation test, on the SCREENED path
# ---------------------------------------------------------------------------

UNIVERSE = list(SINGLE_NAMED_LINES) + list(BROAD_SLICES)
HELD = ["CSP1", "CNDX", "IDP6", "SOXX", "IUFS", "IUHC", "IUIS"]


def _screened_sector_fixture():
    """Constant-breadth 14-line sector book; the demeaned top-7 is exactly HELD
    every week, so the picks are deterministic."""
    mem_idx = _mem_index()
    cidx = mem_idx[300:]
    rng = np.random.default_rng(20260909)
    closes = pd.DataFrame(
        {L: 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, len(cidx))))
         for L in UNIVERSE}, index=cidx)
    breadths = pd.DataFrame(
        {L: np.full(len(cidx), 0.8 if L in HELD else 0.2) for L in UNIVERSE},
        index=cidx)
    eligible = deployed_eligible_start(closes, breadths, UNIVERSE)
    res = run_portfolio(closes, demean(breadths), top_k_breadth_weight(7),
                        eligible, cost=2 / 10_000, rebalance_freq="W-FRI")
    sector = {"closes": closes, "eligible": eligible,
              "rebal_dates": res["rebalance_dates"], "weights": res["weights"]}

    membership, signals, prices, weights = {}, {}, {}, {}
    for L in SINGLE_NAMED_LINES:
        fx = _roster_line(hot_outside=(L != "SOXX"))
        roster = [f"{L}_{t}" for t in fx["roster"]]
        panel = fx["prices"].rename(
            columns={t: f"{L}_{t}" for t in fx["prices"].columns})
        membership[L] = _snapshots(roster)
        prices[L] = panel
        signals[L] = precompute_member_signals(panel)
        weights[L] = _weights_table(roster)
    return sector, membership, signals, prices, weights


def _build_screened(sector, membership, signals, prices, weights, sink=None):
    return build_arm_name_weights(
        ARM_BY_ID["I1"], sector["weights"], sector["closes"],
        sector["rebal_dates"], sector["eligible"], membership, signals, prices,
        member_weights=weights, basket_fn=screened_basket_fn(sink))


def test_final_bar_perturbation_leaves_every_screened_weight_unchanged():
    """The WS6 perturbation test, taken by the screened path (§6, inherited).

    Perturbing ONLY the final bar of the member panels must leave EVERY
    rebalance's weights unchanged: strength, state and the decile ranking are all
    read at t-1, so no weight may ever depend on the final bar. The screened arm
    adds a whole new read of the panel — the full-roster strength cross-section —
    and this is what proves that read is as-of too.
    """
    sector, membership, signals, prices, weights = _screened_sector_fixture()
    base = _build_screened(sector, membership, signals, prices, weights)

    prices2, signals2 = {}, {}
    for L, panel in prices.items():
        p2 = panel.copy()
        p2.iloc[-1, :] *= 1.5
        prices2[L] = p2
        signals2[L] = precompute_member_signals(p2)
    pert = _build_screened(sector, membership, signals2, prices2, weights)
    pd.testing.assert_frame_equal(base.name_weights, pert.name_weights)


def test_the_screened_arm_actually_excludes_on_the_end_to_end_fixture():
    """Guard against the perturbation test above passing vacuously: SOXX's
    fixture puts the strongest names inside the pool, so the exclusion must fire
    on real line-weeks and the screened book must differ from the unscreened one."""
    sector, membership, signals, prices, weights = _screened_sector_fixture()
    sink: dict = {}
    i1x = _build_screened(sector, membership, signals, prices, weights, sink)
    i0 = build_arm_name_weights(
        ARM_BY_ID["I0"], sector["weights"], sector["closes"],
        sector["rebal_dates"], sector["eligible"], membership, signals, prices,
        member_weights=weights)
    fired = [s for (L, _rd), s in sink.items()
             if L == "SOXX" and s.overbought_excluded]
    assert fired, "the exclusion never fired — the fixture proves nothing"
    assert not i1x.name_weights.equals(
        i0.name_weights.reindex(columns=i1x.name_weights.columns).fillna(0.0))


# ---------------------------------------------------------------------------
# The builder hook: E0 and I0 are unchanged, proven rather than assumed
# ---------------------------------------------------------------------------

def test_e0_and_i0_panels_are_identical_with_and_without_the_hook():
    """``build_arm_name_weights`` gained a ``basket_fn`` parameter for this arm.
    The register's own arms must be byte-identical with the default and with an
    explicit pass-through, or the observational arm has changed the signed one."""
    sector, membership, signals, prices, weights = _screened_sector_fixture()

    def passthrough(spec, eff_date, snaps, px, sig, *, resolution=None,
                    weights=None, line=None, rebal_date=None) -> BasketResult:
        del line, rebal_date
        return select_basket(spec, eff_date, snaps, px, sig,
                             resolution=resolution, weights=weights)

    for arm in ("E0", "I0", "I1"):
        common = (ARM_BY_ID[arm], sector["weights"], sector["closes"],
                  sector["rebal_dates"], sector["eligible"], membership,
                  signals, prices)
        default = build_arm_name_weights(*common, member_weights=weights)
        hooked = build_arm_name_weights(*common, member_weights=weights,
                                        basket_fn=passthrough)
        pd.testing.assert_frame_equal(default.name_weights, hooked.name_weights)
        assert default.fallback_weeks == hooked.fallback_weeks
        assert default.basket_sizes == hooked.basket_sizes


def test_the_screened_basket_fn_never_touches_e0():
    """E0 expresses every line as its own ETF before any selection happens, so an
    injected basket_fn must be unreachable from it."""
    sector, membership, signals, prices, weights = _screened_sector_fixture()
    sink: dict = {}
    e0_default = build_arm_name_weights(
        ARM_BY_ID["E0"], sector["weights"], sector["closes"],
        sector["rebal_dates"], sector["eligible"], membership, signals, prices,
        member_weights=weights)
    e0_hooked = build_arm_name_weights(
        ARM_BY_ID["E0"], sector["weights"], sector["closes"],
        sector["rebal_dates"], sector["eligible"], membership, signals, prices,
        member_weights=weights, basket_fn=screened_basket_fn(sink))
    pd.testing.assert_frame_equal(e0_default.name_weights, e0_hooked.name_weights)
    assert sink == {}, "E0 must never reach the selection function"


# ---------------------------------------------------------------------------
# Reconstructing a line's basket off the panel — the 2026-09-10 smoke finding
# ---------------------------------------------------------------------------

def test_within_line_weights_excludes_every_line_code_not_just_the_named_ones():
    """Caught by the first live run of the publisher, on the 2026-09-04 week.

    An isolated single-line build still carries the three BROAD SLICES as their
    own ETFs, so they appear as columns in the panel row. Filtering only the
    single-named line codes leaves CSP1's weight in the row, where it is divided
    by this line's weight and counted into this line's basket: the live baskets
    summed to 1.077 (IUES), 1.370 (IUCS) and 1.075 (IUFS), which the
    weight-integrity guard refuses. The guard did its job; the reconstruction was
    wrong.
    """
    line_codes = set(SINGLE_NAMED_LINES) | set(BROAD_SLICES)
    row = pd.Series({"XOM": 0.09, "CVX": 0.06,      # IUES members, line weight .15
                     "CSP1": 0.0128,                # a broad slice, still held
                     "IUHC": 0.14,                  # another line, held as its ETF
                     "IUES": 0.0})
    out = within_line_weights(row, line_codes, 0.15)
    assert set(out) == {"XOM", "CVX"}
    assert sum(out.values()) == pytest.approx(1.0, abs=1e-12)

    # The filter that shipped in the first draft, for contrast: it lets the broad
    # slice through and the basket no longer sums to one.
    named_only = set(SINGLE_NAMED_LINES)
    leaky = {n: v / 0.15 for n, v in row.items()
             if n not in named_only and v > 0}
    assert sum(leaky.values()) == pytest.approx(1.0 + 0.0128 / 0.15, abs=1e-12)
    assert sum(leaky.values()) > 1.05


def test_within_line_weights_is_empty_for_a_reverted_line():
    """A line the builder reverted to its ETF carries no member columns, so the
    reconstruction is empty and the guard sees no basket rather than a zero-sum
    one — the distinction the WS6b all-fallback week turned on."""
    line_codes = set(SINGLE_NAMED_LINES) | set(BROAD_SLICES)
    row = pd.Series({"IUES": 0.15, "CSP1": 0.0128})
    assert within_line_weights(row, line_codes, 0.15) == {}
    # A line with no weight at all is empty too, and never divides by zero.
    assert within_line_weights(row, line_codes, 0.0) == {}


# ---------------------------------------------------------------------------
# §4.1 measures
# ---------------------------------------------------------------------------

def test_max_drawdown_is_a_positive_peak_to_trough_fall():
    assert max_drawdown([]) is None
    assert max_drawdown([0.01, 0.02]) == pytest.approx(0.0)
    # +10%, -20%, +5%: trough is 1.1*0.8 = 0.88 against a peak of 1.10.
    assert max_drawdown([0.10, -0.20, 0.05]) == pytest.approx(1 - 0.88 / 1.10)


def test_downside_deviation_divides_by_all_weeks_not_only_the_losers():
    """MAR = 0, the mean taken over ALL published weeks (§4.1). Dividing by the
    count of losing weeks instead would score a series with few losses as though
    it were as volatile as one that loses constantly."""
    assert downside_deviation([]) is None
    assert downside_deviation([0.02, 0.03]) == pytest.approx(0.0)
    r = [0.02, -0.04, 0.01, 0.0]
    expected = ((0.04 ** 2) / 4) ** 0.5 * (52 ** 0.5)
    assert downside_deviation(r) == pytest.approx(expected)


def test_status_reports_the_difference_and_never_a_sharpe():
    """§4.3: Sharpe, net return and the return gap as a performance statistic are
    not measured here, in either direction."""
    recs = []
    for we, a, b in (("2026-09-11", 0.01, 0.012), ("2026-09-18", -0.03, -0.04),
                     ("2026-09-25", 0.02, 0.018)):
        recs = append_week(recs, _week(week_ending=we, i1x_return=a,
                                       i0_return=b))
        recs[-1]["publishable"] = True
    st = status(recs)
    assert st["chain_intact"], st["chain_detail"]
    assert st["primary"]["n"] == 3
    assert st["primary"]["max_drawdown_diff_pp"] == pytest.approx(
        (max_drawdown([0.01, -0.03, 0.02]) - max_drawdown([0.012, -0.04, 0.018]))
        * 100.0)
    assert st["primary"]["downside_deviation_diff_pp"] is not None
    assert st["citable_at_published_weeks"] == 52
    # Sharpe appears nowhere except in the list naming what is NOT measured.
    assert st["not_measured"] == ["sharpe", "net_return",
                                  "return_gap_as_performance"]
    reported = {**st["primary"], **st["secondary"]}
    assert not [k for k in reported if "sharpe" in k.lower()]
    # ... and no bar or verdict is reported anywhere in the output (§4.1).
    assert not [k for k in st if k in ("bar_met", "verdict", "pass", "adopt")]


def test_status_prints_the_ws6b_figures_as_reference_lines_only():
    """§4.2: 'neither is a bar for this arm, and a breach changes nothing'."""
    recs = append_week([], _week(gap_i1x_e0=0.0200))     # 200bp, way past both
    recs[-1]["publishable"] = True
    st = status(recs)
    assert st["secondary"]["reference_line_registered_bp"] == pytest.approx(66.0)
    assert st["secondary"]["reference_line_adopted_set_bp"] == pytest.approx(
        42.9, abs=0.1)
    assert st["n_published"] == 1            # the breach changed nothing


def test_status_splits_the_unresolved_count():
    """§4.2 / §6.3(iv): the shared component and the screened-arm-specific one."""
    recs = append_week([], _week(unresolved_shared=["AAA"],
                                 unresolved_screen_specific=["BBB", "CCC"]))
    recs[-1]["publishable"] = True
    st = status(recs)
    assert st["secondary"]["unresolved_shared_total"] == 1
    assert st["secondary"]["unresolved_screen_specific_total"] == 2


def test_status_counts_screen_fallback_line_weeks():
    """§5's ops finding: 'how often and on which lines the exclusion fired'."""
    recs = append_week([], _week(lines_screen_fallback=["SOXX", "IUES"]))
    recs[-1]["publishable"] = True
    assert status(recs)["secondary"]["screen_fallback_line_weeks"] == 2


# ---------------------------------------------------------------------------
# Date boundaries (house rule: one month boundary, one year boundary)
# ---------------------------------------------------------------------------

def _daily_returns(index: pd.DatetimeIndex, per_day: float) -> pd.DataFrame:
    return pd.DataFrame({"AAA": per_day, "BBB": per_day / 2}, index=index)


def test_week_window_month_boundary():
    """Edge case 1 of 2: the week ending Friday 2026-10-02 spans the September /
    October turn. Python datetime months are 1-INDEXED, so month 10 is October;
    the weekday is asserted rather than recalled (Monday = 0, so Friday = 4)."""
    we = date(2026, 10, 2)
    assert we.weekday() == 4, "2026-10-02 must be a Friday"
    idx = pd.bdate_range("2026-09-28", "2026-10-02")
    assert len(idx) == 5 and idx[0].month == 9 and idx[-1].month == 10
    out = name_week_returns(_daily_returns(idx, 0.01), pd.Timestamp(we))
    assert out["AAA"] == pytest.approx(1.01 ** 5 - 1)
    assert out["BBB"] == pytest.approx(1.005 ** 5 - 1)


def test_week_window_year_boundary():
    """Edge case 2 of 2: the year turn. Friday 2027-01-01 is an NYSE holiday and
    the deployed calendar drops that rebalance (§5), so the boundary week to test
    is the one ending Friday 2027-01-08 — whose six-day window reaches back into
    2026, which is the arithmetic that could go wrong."""
    we = date(2027, 1, 8)
    assert we.weekday() == 4, "2027-01-08 must be a Friday"
    assert date(2027, 1, 1).weekday() == 4, "the dropped NYSE holiday is a Friday"
    idx = pd.bdate_range("2027-01-04", "2027-01-08")
    assert len(idx) == 5
    out = name_week_returns(_daily_returns(idx, 0.01), pd.Timestamp(we))
    assert out["AAA"] == pytest.approx(1.01 ** 5 - 1)

    # The window is six calendar days back, so a bar on 2027-01-01 is inside it
    # and a bar on 2026-12-31 is not — the boundary that a manual day count gets
    # wrong.
    wide = pd.DatetimeIndex([pd.Timestamp("2026-12-31"), pd.Timestamp("2027-01-04"),
                             pd.Timestamp("2027-01-08")])
    out2 = name_week_returns(_daily_returns(wide, 0.01), pd.Timestamp(we))
    assert out2["AAA"] == pytest.approx(1.01 ** 2 - 1)    # two bars, not three


def test_schedule_header_records_the_start_week_and_flags_a_late_start():
    """§7: the start week is recorded in the log header, and a start later than
    the intended 2026-09-11 says so — the record must not have to be
    reconstructed later from commit dates."""
    import run_ws6c_screened_arm as R

    hdr = R.schedule_header([])
    assert hdr["start_week_ending"] == "not yet published"
    assert "2026-09-11 (Friday)" in hdr["intended_week_1_ending"]
    assert "2026-09-12 (Saturday)" in hdr["first_scheduled_fire"]

    on_time = R.schedule_header([{"week_ending": "2026-09-11"}])
    assert on_time["start_week_ending"] == "2026-09-11 (Friday)"
    assert "start_week_note" not in on_time

    late = R.schedule_header([{"week_ending": "2026-09-18"}])
    assert late["start_week_ending"] == "2026-09-18 (Friday)"
    assert "NOT backfilled" in late["start_week_note"]


def test_operator_minutes_read_not_logged_never_zero():
    """§4.2: 'An unfilled week is recorded as "not logged", never as zero.' A
    zero would enter a mean and understate the arm's operational load."""
    import run_ws6c_screened_arm as R

    mins = {"2026-09-11": 12.0}
    assert R.minutes_for("2026-09-11", mins) == "12.0"
    assert R.minutes_for("2026-09-18", mins) == "not logged"
    assert R.minutes_for("2026-09-18", {}) != "0"
