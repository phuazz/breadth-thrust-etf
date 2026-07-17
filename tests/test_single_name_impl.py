"""Selftests for the WS6 single-name implementation engine
(scripts/single_name_impl.py), frozen and committed BEFORE any WS6 register
results are computed (KICKOFF_ws6-single-name-implementation.md; em-rotation-lab
and WS5 precedent — engine + tests land first, results are T3).

Coverage maps to the WS6 pre-registration failure modes:

  Failure mode 1 — survivorship through the price feed or ticker mapping:
      test_normalise_ticker_cases,
      test_unmapped_name_counts_against_coverage_and_never_enters_basket,
      test_missing_price_name_excluded_and_reported
  Failure mode 2 — look-ahead in screen, rank or membership:
      test_snapshot_asof_selects_latest_on_or_before,
      test_membership_change_uses_pre_change_roster,
      test_basket_no_lookahead_future_prices,
      test_arm_weights_no_lookahead_end_to_end,
      test_final_bar_perturbation_leaves_weights_unchanged
  Failure mode 3 — cost/turnover understatement at name level:
      test_name_book_preserves_sector_book (no weight leakage -> the full-vector
      turnover the cost model charges is exactly the book's own churn)

  Fallback / edge behaviour (§2 edge rule):
      test_fallback_when_fewer_than_three_pass

  Deployed-parity anchor (E0 must reproduce the deployed sleeve to 0.0):
      test_e0_reproduces_deployed_sector_path_synthetic,
      test_simulate_arm_matches_run_portfolio,
      test_e0_reproduces_deployed_sleeve_a_on_committed_caches (offline-guarded)

  Structural invariants and selection mechanics:
      test_basket_weights_sum_to_one,
      test_broad_slices_stay_etf_in_basket_arm,
      test_top_n_selection_ranks_by_strength,
      test_latest_snapshot_top5_plausible (committed membership, §4 sanity)

  Date-boundary rule (vault CLAUDE.md — one month, one year boundary):
      test_rebalance_calendar_month_boundary,
      test_rebalance_calendar_year_boundary
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from single_name_impl import (  # noqa: E402
    ARM_BY_ID,
    BROAD_SLICES,
    SINGLE_NAMED_LINES,
    ArmSpec,
    build_arm_name_weights,
    build_name_return_panel,
    demean,
    deployed_eligible_start,
    deployed_sector_layer,
    load_constituents,
    normalise_ticker,
    precompute_member_signals,
    select_basket,
    simulate_arm,
    snapshot_asof,
)
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
from rebalance_calendar import weekly_rebalance_dates  # noqa: E402

# NOTE (vault CLAUDE.md date rule): all date arithmetic below is via pandas /
# dateutil, never manual day offsets. Python datetime months are 1-indexed
# (relevant to the month/year-boundary tests, which locate a Jan boundary).

UNIVERSE = list(SINGLE_NAMED_LINES) + list(BROAD_SLICES)   # the 14 lines
# The 7 lines held by the constant-breadth fixture: 3 broad slices stay ETFs,
# 4 single-named lines get baskets. The other 7 sit below the cross-sectional
# mean and are never picked.
HELD = ["CSP1", "CNDX", "IDP6", "SOXX", "IUFS", "IUHC", "IUIS"]
HELD_SINGLE = ["SOXX", "IUFS", "IUHC", "IUIS"]


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

def _mem_index(n_days: int = 820, start: str = "2018-01-02") -> pd.DatetimeIndex:
    """Member price calendar with a lead-in, so the 200d SMA is warm before the
    (later-starting) sector calendar — mirrors the real Norgate pre-2018 warm-up."""
    return pd.date_range(start, periods=n_days, freq="B")


def _closes_index(mem_idx: pd.DatetimeIndex, lead: int = 300) -> pd.DatetimeIndex:
    """Sector trade calendar — a suffix of the member calendar (shared dates)."""
    return mem_idx[lead:]


def _monotone_panel(growths: dict[str, float], index: pd.DatetimeIndex,
                    base: float = 100.0) -> pd.DataFrame:
    """Deterministic price panel: column t = base * (1 + growth)^step. A positive
    growth sits above its own 200d SMA (trend state True); a negative growth
    sits below (False). Distinct positive slopes give a deterministic strength
    and 126d-momentum ordering."""
    steps = np.arange(len(index))
    cols = {tk: base * (1.0 + g) ** steps for tk, g in growths.items()}
    return pd.DataFrame(cols, index=index)


def _snapshots_single(tickers: list[str], key: str = "2018-01-05") -> dict:
    """One membership snapshot (schema-faithful) keyed to a target Friday, listing
    ``tickers`` in cap-rank order."""
    return {key: {"actual_date": key, "n_tickers": len(tickers),
                  "tickers": list(tickers)}}


def _line_member_growths(line: str) -> dict[str, float]:
    """Per-line member trends. IUIS is engineered with only 2 risers so the
    screen fails there (fallback edge); the other held lines carry >= 3 risers."""
    up = [0.0016, 0.0014, 0.0012, 0.0010, 0.0008]      # risers (state True)
    down = [-0.0012, -0.0014, -0.0016, -0.0018]        # fallers (state False)
    if line == "IUIS":
        risers, fallers = up[:2], down + [-0.0010, -0.0011]   # only 2 pass
    else:
        risers, fallers = up, down
    g: dict[str, float] = {}
    for i, val in enumerate(risers):
        g[f"{line}_U{i}"] = val
    for i, val in enumerate(fallers):
        g[f"{line}_D{i}"] = val
    return g


def _member_fixtures(mem_idx: pd.DatetimeIndex):
    """Membership, precomputed signals and price panels for every single-named
    line, plus the combined member price panel for the return side."""
    membership, signals, prices = {}, {}, {}
    for line in SINGLE_NAMED_LINES:
        g = _line_member_growths(line)
        panel = _monotone_panel(g, mem_idx)
        prices[line] = panel
        signals[line] = precompute_member_signals(panel)
        membership[line] = _snapshots_single(list(g.keys()))
    combined = pd.concat(list(prices.values()), axis=1)
    return membership, signals, prices, combined


def _sector_fixture():
    """Constant-breadth 14-line sector book: HELD lines get breadth 0.8, the rest
    0.2, so the demeaned top-7 is exactly HELD every week (deterministic picks,
    each held line weighted 1/7). Returns a deployed-shaped sector dict plus the
    raw closes / breadths."""
    mem_idx = _mem_index()
    cidx = _closes_index(mem_idx)
    rng = np.random.default_rng(20260717)
    closes = pd.DataFrame(
        {L: 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, len(cidx))))
         for L in UNIVERSE}, index=cidx)
    breadths = pd.DataFrame(
        {L: np.full(len(cidx), 0.8 if L in HELD else 0.2) for L in UNIVERSE},
        index=cidx)
    eligible = deployed_eligible_start(closes, breadths, UNIVERSE)
    signal = demean(breadths)
    res = run_portfolio(closes, signal, top_k_breadth_weight(7), eligible,
                        cost=2 / 10_000, rebalance_freq="W-FRI")
    rebal_dates = weekly_rebalance_dates(closes.index, eligible, "W-FRI")
    sector = {"closes": closes, "breadths": breadths, "used": UNIVERSE,
              "eligible": eligible, "signal": signal, "rebal_dates": rebal_dates,
              "weights": res["weights"], "equity": res["equity"]}
    return sector, mem_idx


# ---------------------------------------------------------------------------
# Failure mode 1 — mapping and coverage
# ---------------------------------------------------------------------------

def test_normalise_ticker_cases():
    """Share-class punctuation dash->dot, known renames, and identity for plain
    tickers — the explicit iShares->Norgate mapping (failure mode 1)."""
    assert normalise_ticker("BRK-B") == "BRK.B"
    assert normalise_ticker("BF-B") == "BF.B"
    assert normalise_ticker("brk-b") == "BRK.B"       # case + whitespace normalised
    assert normalise_ticker(" AAPL ") == "AAPL"
    assert normalise_ticker("JPM") == "JPM"
    assert normalise_ticker("FB") == "META"           # genuine rename


def test_unmapped_name_counts_against_coverage_and_never_enters_basket():
    """A roster name whose Norgate symbol has no price column is UNCOVERED —
    counted, and never silently present in the basket."""
    mem_idx = _mem_index()
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011}   # three risers with prices
    panel = _monotone_panel(g, mem_idx)
    sig = precompute_member_signals(panel)
    # Roster lists a fourth name ("ZZZ") that Norgate does not carry.
    snaps = _snapshots_single(["AAA", "BBB", "CCC", "ZZZ"])
    eff = mem_idx[600]
    res = select_basket(ARM_BY_ID["I1"], eff, snaps, panel, sig)
    assert not res.fallback
    assert "ZZZ" in res.uncovered
    assert "ZZZ" not in res.weights
    assert set(res.weights) == {"AAA", "BBB", "CCC"}


def test_missing_price_name_excluded_and_reported():
    """A covered name without a price as of t-1 (a gap / not-yet-listed) is
    reported in missing_price and excluded — never silently dropped or held."""
    mem_idx = _mem_index()
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011, "DDD": 0.0009}
    panel = _monotone_panel(g, mem_idx)
    panel.loc[:, "DDD"] = np.nan          # DDD has no valid price anywhere
    sig = precompute_member_signals(panel)
    snaps = _snapshots_single(["AAA", "BBB", "CCC", "DDD"])
    eff = mem_idx[600]
    res = select_basket(ARM_BY_ID["I0"], eff, snaps, panel, sig)   # unscreened
    assert "DDD" in res.missing_price
    assert "DDD" not in res.weights
    assert set(res.weights) == {"AAA", "BBB", "CCC"}
    assert abs(sum(res.weights.values()) - 1.0) < 1e-12   # renormalised EW


# ---------------------------------------------------------------------------
# Failure mode 2 — as-of membership and look-ahead
# ---------------------------------------------------------------------------

def test_snapshot_asof_selects_latest_on_or_before():
    """snapshot_asof returns the newest snapshot on or before the as-of date, and
    (None, []) before the first snapshot."""
    snaps = {"2020-01-03": {"tickers": ["A", "B"]},
             "2020-01-10": {"tickers": ["C", "D"]}}
    d0, t0 = snapshot_asof(snaps, pd.Timestamp("2020-01-09"))
    assert d0 == pd.Timestamp("2020-01-03") and t0 == ["A", "B"]
    d1, t1 = snapshot_asof(snaps, pd.Timestamp("2020-01-10"))
    assert d1 == pd.Timestamp("2020-01-10") and t1 == ["C", "D"]
    d2, t2 = snapshot_asof(snaps, pd.Timestamp("2019-12-31"))
    assert d2 is None and t2 == []


def test_membership_change_uses_pre_change_roster():
    """Engineer a membership change dated the rebalance Friday. Because the roster
    is read as of t-1, the PRE-change list is used, never the same week's forward
    file (failure mode 2)."""
    mem_idx = _mem_index()
    # Two rosters; the new one is keyed to a Friday and its members also rise, so
    # any leakage would show up as new-only names in the basket.
    old = {"OLD0": 0.0015, "OLD1": 0.0013, "OLD2": 0.0011}
    new = {"NEW0": 0.0016, "NEW1": 0.0014, "NEW2": 0.0012}
    panel = _monotone_panel({**old, **new}, mem_idx)
    sig = precompute_member_signals(panel)
    change_friday = pd.Timestamp("2020-06-05")        # a Friday within the panel
    assert change_friday.dayofweek == 4
    snaps = {"2018-01-05": {"tickers": list(old)},
             change_friday.strftime("%Y-%m-%d"): {"tickers": list(new)}}
    # Rebalance on the change Friday reads t-1 (the prior trading day).
    pos = mem_idx.searchsorted(change_friday)
    eff = mem_idx[pos - 1]
    res = select_basket(ARM_BY_ID["I1"], eff, snaps, panel, sig)
    assert set(res.weights) == set(old)
    assert not (set(res.weights) & set(new))


def test_basket_no_lookahead_future_prices():
    """Basket at t-1 must not change when member prices strictly AFTER t-1 are
    mutated (shift(1) discipline at the basket level)."""
    mem_idx = _mem_index()
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011, "DDD": -0.0013}
    panel = _monotone_panel(g, mem_idx)
    sig = precompute_member_signals(panel)
    snaps = _snapshots_single(list(g))
    eff = mem_idx[600]
    before = select_basket(ARM_BY_ID["I2"], eff, snaps, panel, sig).weights

    panel2 = panel.copy()
    epos = panel2.index.get_loc(eff)
    rng = np.random.default_rng(7)
    panel2.iloc[epos + 1:, :] *= rng.uniform(0.3, 1.8, size=(len(panel2) - epos - 1,
                                                             panel2.shape[1]))
    sig2 = precompute_member_signals(panel2)
    after = select_basket(ARM_BY_ID["I2"], eff, snaps, panel2, sig2).weights
    assert before == after


def test_arm_weights_no_lookahead_end_to_end():
    """Full arm: the name-level weights on a given rebalance must not change when
    both the sector closes and the member prices strictly after that rebalance are
    mutated."""
    sector, mem_idx = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx)
    spec = ARM_BY_ID["I1"]
    build = build_arm_name_weights(spec, sector["weights"], sector["closes"],
                                   sector["rebal_dates"], sector["eligible"],
                                   membership, signals, prices)
    rd = sector["rebal_dates"][len(sector["rebal_dates"]) // 2]
    before = build.name_weights.loc[rd].copy()

    # Mutate closes AND member prices strictly after rd; rebuild the whole path.
    cpos = sector["closes"].index.get_loc(rd)
    closes2 = sector["closes"].copy()
    closes2.iloc[cpos + 1:, :] *= 1.3
    signal2 = demean(sector["breadths"])              # breadths unchanged
    res2 = run_portfolio(closes2, signal2, top_k_breadth_weight(7),
                         sector["eligible"], cost=2 / 10_000,
                         rebalance_freq="W-FRI")
    prices2 = {}
    signals2 = {}
    for line, panel in prices.items():
        p2 = panel.copy()
        mpos = p2.index.get_loc(rd)
        p2.iloc[mpos + 1:, :] *= 1.4
        prices2[line] = p2
        signals2[line] = precompute_member_signals(p2)
    build2 = build_arm_name_weights(spec, res2["weights"], closes2,
                                    sector["rebal_dates"], sector["eligible"],
                                    membership, signals2, prices2)
    after = build2.name_weights.reindex(columns=before.index).loc[rd]
    pd.testing.assert_series_equal(before, after)


def test_final_bar_perturbation_leaves_weights_unchanged():
    """em-rotation convention: perturbing ONLY the final bar of the member panels
    (and closes) must leave EVERY rebalance weight unchanged — no weight is ever
    read from the final bar (weights use t-1)."""
    sector, mem_idx = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx)
    spec = ARM_BY_ID["I2"]
    base = build_arm_name_weights(spec, sector["weights"], sector["closes"],
                                  sector["rebal_dates"], sector["eligible"],
                                  membership, signals, prices)
    prices2, signals2 = {}, {}
    for line, panel in prices.items():
        p2 = panel.copy()
        p2.iloc[-1, :] *= 1.5
        prices2[line] = p2
        signals2[line] = precompute_member_signals(p2)
    pert = build_arm_name_weights(spec, sector["weights"], sector["closes"],
                                  sector["rebal_dates"], sector["eligible"],
                                  membership, signals2, prices2)
    pd.testing.assert_frame_equal(base.name_weights, pert.name_weights)


# ---------------------------------------------------------------------------
# Fallback edge (§2 edge rule)
# ---------------------------------------------------------------------------

def test_fallback_when_fewer_than_three_pass():
    """A screened line with fewer than 3 names passing reverts to its ETF; the
    fallback counter increments and the line's weight is held as the ETF (line
    code), with no member weights for that line."""
    mem_idx = _mem_index()
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": -0.0011, "DDD": -0.0013}  # 2 pass
    panel = _monotone_panel(g, mem_idx)
    sig = precompute_member_signals(panel)
    snaps = _snapshots_single(list(g))
    eff = mem_idx[600]
    res = select_basket(ARM_BY_ID["I1"], eff, snaps, panel, sig)
    assert res.fallback and res.n_pass == 2 and not res.weights

    # End-to-end: IUIS carries only 2 risers in the fixture, so it falls back to
    # the ETF while the other held single-named lines form baskets.
    sector, mem_idx2 = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx2)
    build = build_arm_name_weights(ARM_BY_ID["I1"], sector["weights"],
                                   sector["closes"], sector["rebal_dates"],
                                   sector["eligible"], membership, signals, prices)
    assert build.fallback_weeks["IUIS"] > 0
    # On a warmed-up rebalance IUIS is held as its ETF (the line code carries the
    # 1/7 sector weight); no IUIS member ever receives weight.
    rd = sector["rebal_dates"][-1]
    row = build.name_weights.loc[rd]
    assert row.get("IUIS", 0.0) > 0.0
    assert not any(c.startswith("IUIS_") and row.get(c, 0.0) > 0 for c in row.index)


# ---------------------------------------------------------------------------
# Failure mode 3 — no weight leakage (full-vector turnover is the book's churn)
# ---------------------------------------------------------------------------

def test_name_book_preserves_sector_book():
    """For every arm, on each warmed-up rebalance the summed name-level weight
    equals the summed sector-line weight — baskets neither leak nor inflate
    capital, so the full-vector cost model charges exactly the book's own churn
    (failure mode 3)."""
    sector, mem_idx = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx)
    sector_total = sector["weights"].sum(axis=1)
    for arm_id in ("I0", "I1", "I2", "P2", "I1-all"):
        build = build_arm_name_weights(ARM_BY_ID[arm_id], sector["weights"],
                                       sector["closes"], sector["rebal_dates"],
                                       sector["eligible"], membership, signals,
                                       prices)
        name_total = build.name_weights.sum(axis=1)
        # Compare on the daily calendar from the first rebalance onward.
        rd0 = sector["rebal_dates"][0]
        a = name_total.loc[name_total.index >= rd0]
        b = sector_total.loc[sector_total.index >= rd0]
        assert np.allclose(a.values, b.values, atol=1e-12), (
            f"{arm_id}: name book diverged from sector book")


def test_broad_slices_stay_etf_in_basket_arm():
    """Broad slices (CSP1, CNDX, IDP6) are held as their own ETF line codes in a
    basket arm — never expanded into member baskets."""
    sector, mem_idx = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx)
    build = build_arm_name_weights(ARM_BY_ID["I2"], sector["weights"],
                                   sector["closes"], sector["rebal_dates"],
                                   sector["eligible"], membership, signals, prices)
    rd = sector["rebal_dates"][-1]
    row = build.name_weights.loc[rd]
    for slice_code in ("CSP1", "CNDX", "IDP6"):
        assert row.get(slice_code, 0.0) > 0.0


def test_top_n_selection_ranks_by_strength():
    """Design 2 keeps the top-N strongest passing names by close/SMA200 - 1. With
    monotone risers the strongest are the steepest, and N caps the basket."""
    mem_idx = _mem_index()
    g = {f"U{i}": 0.0006 + i * 0.0002 for i in range(12)}   # 12 risers, distinct
    panel = _monotone_panel(g, mem_idx)
    sig = precompute_member_signals(panel)
    snaps = _snapshots_single(list(g))
    eff = mem_idx[700]
    res = select_basket(ARM_BY_ID["I2"], eff, snaps, panel, sig)   # N = 10
    assert res.n_selected == 10
    # The two shallowest risers (U0, U1) must be excluded by the top-10 cap.
    assert "U0" not in res.weights and "U1" not in res.weights
    assert {f"U{i}" for i in range(2, 12)} == set(res.weights)


def test_return_panel_dedups_shared_member():
    """A member sitting in two lines' rosters (e.g. a semiconductor in both SOXX
    and IUIS) arrives as a duplicate column in the combined panel; the return
    builder keeps one copy so simulate_arm does not double-count or raise."""
    cidx = pd.date_range("2020-01-02", periods=60, freq="B")
    closes = pd.DataFrame({"SOXX": np.linspace(100, 120, 60),
                           "IUIS": np.linspace(100, 110, 60)}, index=cidx)
    nvda = pd.Series(np.linspace(50, 90, 60), index=cidx)
    combined = pd.concat([nvda.rename("NVDA"), nvda.rename("NVDA")], axis=1)
    assert combined.columns.duplicated().any()
    ret = build_name_return_panel(closes, combined)
    assert list(ret.columns).count("NVDA") == 1
    weights = pd.DataFrame(0.0, index=cidx, columns=["SOXX", "NVDA"])
    weights.iloc[10:, :] = 0.5
    sim = simulate_arm(weights, ret, cost_bps=5)      # must not raise
    assert np.isfinite(sim["equity"].iloc[-1])


def test_basket_weights_sum_to_one():
    """Every non-fallback basket is equal-weight and sums to 1.0 within the line."""
    mem_idx = _mem_index()
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011, "DDD": 0.0009}
    panel = _monotone_panel(g, mem_idx)
    sig = precompute_member_signals(panel)
    snaps = _snapshots_single(list(g))
    eff = mem_idx[600]
    for arm_id in ("I0", "I1", "I2", "P2", "I1-all"):
        res = select_basket(ARM_BY_ID[arm_id], eff, snaps, panel, sig)
        assert not res.fallback
        assert abs(sum(res.weights.values()) - 1.0) < 1e-12
        vals = list(res.weights.values())
        assert all(abs(v - vals[0]) < 1e-12 for v in vals)   # equal weight


# ---------------------------------------------------------------------------
# Date-boundary rule (vault CLAUDE.md): one month boundary, one year boundary
# ---------------------------------------------------------------------------

def test_rebalance_calendar_month_boundary():
    """The rebalance calendar and the as-of roster selection behave correctly
    across a month boundary. (Python datetime months are 1-indexed; here a
    May->June boundary is located and both sides must carry Friday rebalances.)"""
    idx = pd.date_range("2021-05-03", "2021-07-02", freq="B")
    eligible = idx[0]
    rebals = weekly_rebalance_dates(idx, eligible, "W-FRI")
    months = {d.month for d in rebals}
    assert 5 in months and 6 in months            # rebalances on both sides
    # A roster dated in May is still the as-of roster for an early-June rebalance
    # until a June snapshot appears.
    snaps = {"2021-05-28": {"tickers": ["MAYNAME"]},
             "2021-06-25": {"tickers": ["JUNNAME"]}}
    june_first_rebal = [d for d in rebals if d.month == 6][0]
    eff = idx[idx.get_loc(june_first_rebal) - 1]
    _, roster = snapshot_asof(snaps, eff)
    assert roster == ["MAYNAME"]


def test_rebalance_calendar_year_boundary():
    """Same, across a December->January year boundary — the December roster
    carries into early January until the next snapshot."""
    idx = pd.date_range("2021-12-01", "2022-02-01", freq="B")
    eligible = idx[0]
    rebals = weekly_rebalance_dates(idx, eligible, "W-FRI")
    years = {d.year for d in rebals}
    assert 2021 in years and 2022 in years
    snaps = {"2021-12-31": {"tickers": ["DECNAME"]},
             "2022-01-28": {"tickers": ["JANNAME"]}}
    jan_first_rebal = [d for d in rebals if d.year == 2022][0]
    eff = idx[idx.get_loc(jan_first_rebal) - 1]
    _, roster = snapshot_asof(snaps, eff)
    assert roster == ["DECNAME"]


# ---------------------------------------------------------------------------
# Deployed-parity anchor — E0 must reproduce the deployed sleeve to 0.0
# ---------------------------------------------------------------------------

def test_simulate_arm_matches_run_portfolio():
    """simulate_arm reproduces run_portfolio's equity to 0.0 given identical
    weights, returns and cost — the shared mechanics the parity rests on."""
    sector, _ = _sector_fixture()
    line_rets = sector["closes"].pct_change().fillna(0.0)
    sim = simulate_arm(sector["weights"], line_rets, cost_bps=2)
    diff = float((sim["equity"] - sector["equity"]).abs().max())
    assert diff < 1e-12, f"simulate_arm vs run_portfolio equity |diff| = {diff:.2e}"


def test_e0_reproduces_deployed_sector_path_synthetic():
    """E0 built through the name-level machinery reproduces the deployed sector
    weights AND equity to 0.0 (every line expressed as its own ETF). This is the
    structural parity anchor, mirroring how WS5 pinned A0 parity."""
    sector, _ = _sector_fixture()
    build = build_arm_name_weights(ARM_BY_ID["E0"], sector["weights"],
                                   sector["closes"], sector["rebal_dates"],
                                   sector["eligible"], {}, {}, {})
    # Weights: compare on the 14 line columns (unheld lines are 0 in both).
    e0 = build.name_weights.reindex(columns=UNIVERSE).fillna(0.0)
    dep = sector["weights"].reindex(columns=UNIVERSE).fillna(0.0)
    wdiff = float((e0 - dep).abs().max().max())
    assert wdiff == 0.0, f"E0 weight parity |diff| = {wdiff:.2e}"
    # Equity through simulate_arm at the deployed 2 bps.
    line_rets = sector["closes"].pct_change().fillna(0.0)
    sim = simulate_arm(build.name_weights, line_rets, cost_bps=2)
    ediff = float((sim["equity"] - sector["equity"]).abs().max())
    assert ediff < 1e-12, f"E0 equity parity |diff| = {ediff:.2e}"


def test_e0_reproduces_deployed_sleeve_a_on_committed_caches():
    """E0 reproduces the REAL deployed Sleeve A weights + equity to 0.0 on the
    committed membership and local price caches. Skipped when the deployed panels
    cannot be assembled offline (missing local caches or no network) — the
    synthetic parity test above always pins the structural guard regardless."""
    try:
        sector = deployed_sector_layer()
    except Exception as exc:  # noqa: BLE001 — offline / missing-cache -> skip
        pytest.skip(f"deployed panels unavailable offline: {type(exc).__name__}: {exc}")
    build = build_arm_name_weights(ARM_BY_ID["E0"], sector["weights"],
                                   sector["closes"], sector["rebal_dates"],
                                   sector["eligible"], {}, {}, {})
    e0 = build.name_weights.reindex(columns=sector["weights"].columns).fillna(0.0)
    wdiff = float((e0 - sector["weights"]).abs().max().max())
    assert wdiff == 0.0, f"E0 weight parity vs deployed |diff| = {wdiff:.2e}"
    line_rets = sector["closes"].pct_change().fillna(0.0)
    sim = simulate_arm(build.name_weights, line_rets, cost_bps=2)
    ediff = float((sim["equity"] - sector["equity"]).abs().max())
    assert ediff < 1e-10, f"E0 equity parity vs deployed |diff| = {ediff:.2e}"


# ---------------------------------------------------------------------------
# Committed-membership sanity (§4) — top-5 of the latest snapshot are plausible
# ---------------------------------------------------------------------------

def test_latest_snapshot_top5_plausible():
    """§4 sanity: the top-5 of each single-named line's latest committed snapshot
    are well-formed cap-rank tickers that the mapping accepts (offline; membership
    is committed, no prices needed)."""
    for line in SINGLE_NAMED_LINES:
        try:
            data = load_constituents(line)
        except FileNotFoundError:
            pytest.skip(f"no committed constituents cache for {line}")
        snaps = data.get("snapshots", {})
        assert snaps, f"{line}: empty snapshots"
        latest = sorted(snaps.keys())[-1]
        top5 = list(snaps[latest].get("tickers", []))[:5]
        assert len(top5) == 5, f"{line}: latest snapshot has < 5 names"
        for t in top5:
            assert t and isinstance(t, str)
            sym = normalise_ticker(t)
            assert sym and "-" not in sym       # dash normalised to dot
