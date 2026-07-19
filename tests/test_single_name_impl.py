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

  Amendment A1 (kickoff §5b) — instrument resolution:
      test_suffix_candidate_resolution (delisted "-YYYYMM" candidates, month-end
          slack, leap-February handling),
      test_recycled_ticker_disambiguation (disjoint life intervals -> the
          era-correct instrument; gap/pre-history dates stay unresolved;
          overlapping intervals -> ambiguous, never guessed),
      test_rename_table_hit (verified-entry fallback fires only when no native
          candidate contains the date; target interval still enforced),
      test_resolve_membership_counts (per-snapshot maps, unresolved counting),
      test_instrument_keys_do_not_blend_eras (a recycled ticker occupies two
          separate instrument columns, weights era-consistent)

  Amendment A2 (kickoff §5b) — base-ticker tenure disambiguation:
      test_tenure_rule_recycled_reit (dead REIT vs live acquirer with
          pre-recycle history under the same base: tenure partitions the
          membership dates, including inside the delisting month),
      test_tenure_override_only_disambiguates (tenure never touches the
          single-candidate path or the rename-target life check; no tenure
          information keeps the ambiguous never-guess outcome),
      test_fox_era_split (rename-at-death plus a recycled base: pre-recycle
          rows through the verified rename entry, post-recycle rows natively)

  Amendment A3 (kickoff §5b) — true-weight baskets:
      test_true_weights_renormalise_to_one (proportional to snapshot weights,
          sum exactly 1, weight_source "snapshot"),
      test_screened_arm_renormalises_over_survivors (survivors' true weights
          renormalised; the screened-out name's weight redistributed),
      test_missing_weight_snapshot_carries_forward (absent snapshot uses the
          line's last known weights; counter increments),
      test_ew_fallback_flag (no weights at all, and a selected member without
          a usable weight, both drop the line-week to equal weight, counted),
      test_true_weights_keyed_by_instrument_across_eras (a recycled ticker's
          weight lands on the era-correct instrument column, never blended)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from single_name_impl import (  # noqa: E402
    ARM_BY_ID,
    BROAD_SLICES,
    INSTRUMENT_RENAMES,
    SINGLE_NAMED_LINES,
    TICKER_TENURE_OVERRIDES,
    ArmSpec,
    Instrument,
    InstrumentDirectory,
    build_arm_name_weights,
    build_name_return_panel,
    demean,
    deployed_eligible_start,
    deployed_sector_layer,
    load_constituents,
    normalise_ticker,
    precompute_member_signals,
    resolve_instrument,
    resolve_membership,
    select_basket,
    simulate_arm,
    snapshot_asof,
    suffix_month_end,
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


# ---------------------------------------------------------------------------
# Amendment A1 (kickoff §5b) — (ticker, date) -> Norgate instrument resolution
# ---------------------------------------------------------------------------

def _a1_directory() -> InstrumentDirectory:
    """Synthetic directory: a plain delisted name (XLNX-style), a recycled base
    with DISJOINT eras (RCY), an overlapping pair (OVL, delisting-month overlap
    with the successor's first quote), live fillers, and a rename target."""
    return InstrumentDirectory([
        Instrument("XLNX-202202", "XLNX", pd.Timestamp("1990-06-12"),
                   suffix_month_end("202202")),
        Instrument("RCY-202001", "RCY", pd.Timestamp("2018-01-02"),
                   suffix_month_end("202001")),
        Instrument("RCY", "RCY", pd.Timestamp("2020-06-01"), None),
        Instrument("OVL-202012", "OVL", pd.Timestamp("2010-03-01"),
                   suffix_month_end("202012")),
        Instrument("OVL", "OVL", pd.Timestamp("2020-11-16"), None),
        Instrument("AAA", "AAA", pd.Timestamp("2018-01-02"), None),
        Instrument("BBB", "BBB", pd.Timestamp("2018-01-02"), None),
        Instrument("NEWCO", "NEWCO", pd.Timestamp("2015-01-02"), None),
    ])


def test_suffix_candidate_resolution():
    """A base whose only instrument is delisted-suffixed resolves within its life
    interval (first quoted date to the suffix month end, inclusive) and is
    unresolved outside it. suffix_month_end is pandas Period arithmetic (Python
    datetime months are 1-indexed), including the leap-February edge."""
    d = _a1_directory()
    assert resolve_instrument("XLNX", pd.Timestamp("2019-06-07"), d) == \
        ("XLNX-202202", "native")
    # Within the delisting month: the month-end slack keeps the name resolved.
    assert resolve_instrument("XLNX", pd.Timestamp("2022-02-15"), d) == \
        ("XLNX-202202", "native")
    assert resolve_instrument("XLNX", pd.Timestamp("2022-02-28"), d) == \
        ("XLNX-202202", "native")
    # After the delisting month, and before the first quote: unresolved.
    assert resolve_instrument("XLNX", pd.Timestamp("2022-03-04"), d) == \
        (None, "unresolved")
    assert resolve_instrument("XLNX", pd.Timestamp("1990-01-05"), d) == \
        (None, "unresolved")
    # Month-end arithmetic: ordinary, leap and year-end months.
    assert suffix_month_end("202202") == pd.Timestamp("2022-02-28")
    assert suffix_month_end("202402") == pd.Timestamp("2024-02-29")   # leap year
    assert suffix_month_end("202112") == pd.Timestamp("2021-12-31")


def test_recycled_ticker_disambiguation():
    """A recycled base with disjoint life intervals maps each membership date to
    the era-correct instrument; dates in the gap or before all history stay
    unresolved; overlapping intervals are AMBIGUOUS and never guessed — not even
    through a rename entry."""
    d = _a1_directory()
    # Old era -> the delisted instrument; new era -> the live one.
    assert resolve_instrument("RCY", pd.Timestamp("2019-06-07"), d) == \
        ("RCY-202001", "native")
    assert resolve_instrument("RCY", pd.Timestamp("2021-03-05"), d) == \
        ("RCY", "native")
    # Gap between the eras, and pre-history: unresolved.
    assert resolve_instrument("RCY", pd.Timestamp("2020-03-06"), d) == \
        (None, "unresolved")
    assert resolve_instrument("RCY", pd.Timestamp("2016-01-08"), d) == \
        (None, "unresolved")
    # Overlap (successor first quoted inside the predecessor's delisting month):
    # both intervals contain the date -> ambiguous, never guessed.
    assert resolve_instrument("OVL", pd.Timestamp("2020-11-20"), d) == \
        (None, "ambiguous")
    # Ambiguity does NOT fall through to the rename table.
    assert resolve_instrument("OVL", pd.Timestamp("2020-11-20"), d,
                              renames={"OVL": "NEWCO"}) == (None, "ambiguous")
    # Outside the overlap the same base resolves normally on both sides.
    assert resolve_instrument("OVL", pd.Timestamp("2019-05-03"), d) == \
        ("OVL-202012", "native")
    assert resolve_instrument("OVL", pd.Timestamp("2021-02-05"), d) == \
        ("OVL", "native")


def test_rename_table_hit():
    """The verified rename table fires only when the base has NO native
    instrument containing the date, and the target's own interval is enforced.
    A native-era instrument always wins over a table entry."""
    d = _a1_directory()
    renames = {"OLDT": "NEWCO", "RCY": "NEWCO"}
    # No native OLDT instrument -> the verified target, interval-checked.
    assert resolve_instrument("OLDT", pd.Timestamp("2019-04-05"), d,
                              renames=renames) == ("NEWCO", "renamed")
    # Date before the target's first quote -> unresolved, not guessed.
    assert resolve_instrument("OLDT", pd.Timestamp("2014-06-06"), d,
                              renames=renames) == (None, "unresolved")
    # A rename entry must never shadow a native-era instrument.
    assert resolve_instrument("RCY", pd.Timestamp("2019-06-07"), d,
                              renames=renames) == ("RCY-202001", "native")
    # A rename target absent from the directory -> unresolved.
    assert resolve_instrument("GHOST", pd.Timestamp("2019-04-05"), d,
                              renames={"GHOST": "NOSUCH"}) == (None, "unresolved")
    # Structural sanity of the shipped table: exact-symbol values, no identity
    # entries, no lowercase.
    for src, tgt in INSTRUMENT_RENAMES.items():
        assert src == src.upper() and tgt == tgt.upper()
        assert src != tgt
        assert re.fullmatch(r"[A-Z0-9.]+(-\d{6})?", tgt), tgt


def test_resolve_membership_counts():
    """resolve_membership maps every in-window snapshot roster through the
    resolver, unions the instruments, and counts unresolved names per ticker —
    never silently dropping them."""
    d = _a1_directory()
    snaps = {
        "2019-01-04": {"tickers": ["RCY", "AAA", "ZZZ"]},
        "2021-01-08": {"tickers": ["RCY", "AAA"]},
        "2030-01-04": {"tickers": ["AAA"]},          # beyond window_end: ignored
    }
    res = resolve_membership(snaps, d, window_end=pd.Timestamp("2026-06-30"))
    assert set(res["by_snapshot"]) == {pd.Timestamp("2019-01-04"),
                                       pd.Timestamp("2021-01-08")}
    assert res["by_snapshot"][pd.Timestamp("2019-01-04")] == \
        {"RCY": "RCY-202001", "AAA": "AAA", "ZZZ": None}
    assert res["by_snapshot"][pd.Timestamp("2021-01-08")] == \
        {"RCY": "RCY", "AAA": "AAA"}
    assert res["instruments"] == ["AAA", "RCY", "RCY-202001"]
    assert res["unresolved"] == {"ZZZ": {"status": "unresolved", "n_weeks": 1}}
    assert res["n_member_weeks"] == 5 and res["n_resolved_weeks"] == 4


def test_instrument_keys_do_not_blend_eras():
    """End-to-end: a recycled ticker in the membership snapshots occupies TWO
    separate instrument columns in the name-level weight panel — the old era's
    weight sits on the delisted instrument, the new era's on the live one, and
    no rebalance holds both. Prices, screens and weights are all keyed by
    instrument, so the two companies' histories never blend in one column."""
    sector, mem_idx = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx)

    # Override IUFS with a recycled-ticker construction. Era boundaries chosen
    # inside the fixture's calendar (mem_idx starts 2018-01-02; the sector
    # calendar and rebalances start around 2019; pandas Timestamp comparisons
    # only — no manual day arithmetic).
    old_end = pd.Timestamp("2020-01-31")
    new_start = pd.Timestamp("2020-06-01")
    growths = {"RCY-202001": 0.0012, "RCY": 0.0014,
               "AAA": 0.0010, "BBB": 0.0008}
    panel = _monotone_panel(growths, mem_idx)
    panel.loc[panel.index > old_end, "RCY-202001"] = np.nan
    panel.loc[panel.index < new_start, "RCY"] = np.nan
    directory = InstrumentDirectory([
        Instrument("RCY-202001", "RCY", mem_idx[0], suffix_month_end("202001")),
        Instrument("RCY", "RCY", new_start, None),
        Instrument("AAA", "AAA", mem_idx[0], None),
        Instrument("BBB", "BBB", mem_idx[0], None),
    ])
    snaps = {"2018-01-05": {"tickers": ["RCY", "AAA", "BBB"]},
             "2020-06-19": {"tickers": ["RCY", "AAA", "BBB"]}}
    res = resolve_membership(snaps, directory)
    membership["IUFS"] = snaps
    prices["IUFS"] = panel
    signals["IUFS"] = precompute_member_signals(panel)

    build = build_arm_name_weights(
        ARM_BY_ID["I0"], sector["weights"], sector["closes"],
        sector["rebal_dates"], sector["eligible"],
        membership, signals, prices,
        member_resolution={"IUFS": res["by_snapshot"]})

    nw = build.name_weights
    assert "RCY-202001" in nw.columns and "RCY" in nw.columns
    on_rebal = nw.loc[sector["rebal_dates"]]
    old_held = on_rebal["RCY-202001"] > 0
    new_held = on_rebal["RCY"] > 0
    assert old_held.any(), "old-era instrument never held"
    assert new_held.any(), "new-era instrument never held"
    # No rebalance holds both eras of the recycled ticker.
    assert not (old_held & new_held).any()
    # Era consistency: the old instrument only up to its final print month, the
    # new one only after the second snapshot takes effect.
    assert old_held[old_held].index.max() <= pd.Timestamp("2020-02-28")
    assert new_held[new_held].index.min() >= pd.Timestamp("2020-06-19")


# ---------------------------------------------------------------------------
# Amendment A2 (kickoff §5b) — base-ticker tenure disambiguation
# ---------------------------------------------------------------------------

def test_tenure_rule_recycled_reit():
    """A dead REIT and a live acquirer that carries pre-recycle history under
    the same base BOTH life-contain the old-era dates; the tenure refinement
    assigns each membership date to the instrument that actually traded under
    the base ticker — including inside the delisting month, where the dead
    instrument's day-granular last quote and the successor's rename date
    partition the month the suffix can only bound."""
    d = InstrumentDirectory([
        # Dead REIT: life 1993 -> suffix month end; traded the base to 07-20.
        Instrument("HRX-202207", "HRX", pd.Timestamp("1993-05-27"),
                   suffix_month_end("202207"),
                   tenure_end=pd.Timestamp("2022-07-20")),
        # Live acquirer: lineage history from 2012 under the SAME base after
        # the 2022 rename (the recycled-REIT shape of HR / DOC / RPT / COR).
        Instrument("HRX", "HRX", pd.Timestamp("2012-06-06"), None,
                   tenure_start=pd.Timestamp("2022-07-21")),
    ])
    # Old era: both life-contain, tenure separates -> the dead instrument.
    assert resolve_instrument("HRX", pd.Timestamp("2019-06-07"), d) == \
        ("HRX-202207", "tenure")
    assert resolve_instrument("HRX", pd.Timestamp("2022-07-20"), d) == \
        ("HRX-202207", "tenure")
    # Inside the delisting month but after the hand-over -> the successor.
    assert resolve_instrument("HRX", pd.Timestamp("2022-07-22"), d) == \
        ("HRX", "tenure")
    # After the suffix month only the live instrument life-contains -> native.
    assert resolve_instrument("HRX", pd.Timestamp("2023-03-03"), d) == \
        ("HRX", "native")
    # Before either instrument existed -> unresolved.
    assert resolve_instrument("HRX", pd.Timestamp("1990-01-05"), d) == \
        (None, "unresolved")


def test_tenure_override_only_disambiguates():
    """Tenure refines ONLY the multi-candidate case: a lone dead candidate
    keeps the A1 month-end slack past its last quote; claimants without tenure
    information stay ambiguous (never guess); and a rename-table target is
    validated on its LIFE interval, not its tenure over its own base."""
    d = InstrumentDirectory([
        # Lone dead instrument: no competing claimant for the base.
        Instrument("SOLO-202207", "SOLO", pd.Timestamp("2000-01-05"),
                   suffix_month_end("202207"),
                   tenure_end=pd.Timestamp("2022-07-20")),
        # Overlapping pair WITHOUT tenure metadata (the A1 ambiguous shape).
        Instrument("OVL-202012", "OVL", pd.Timestamp("2010-03-01"),
                   suffix_month_end("202012")),
        Instrument("OVL", "OVL", pd.Timestamp("2020-11-16"), None),
        # Rename target: live, lineage from 1990, base recycled only in 2024.
        Instrument("DOCX", "DOCX", pd.Timestamp("1990-01-02"), None,
                   tenure_start=pd.Timestamp("2024-03-01")),
    ])
    # Single-candidate month-end slack intact after the last quote.
    assert resolve_instrument("SOLO", pd.Timestamp("2022-07-25"), d) == \
        ("SOLO-202207", "native")
    # No tenure information on either claimant -> still ambiguous.
    assert resolve_instrument("OVL", pd.Timestamp("2020-11-20"), d) == \
        (None, "ambiguous")
    # Rename-target check is LIFE-based: the 2019 date precedes the target's
    # tenure over its own base, but the instrument existed -> renamed.
    assert resolve_instrument("HCPX", pd.Timestamp("2019-05-03"), d,
                              renames={"HCPX": "DOCX"}) == ("DOCX", "renamed")
    # Structural sanity of the shipped override table: plain uppercase live
    # symbols mapping to ISO dates.
    for sym, iso in TICKER_TENURE_OVERRIDES.items():
        assert sym == sym.upper() and "-" not in sym
        assert pd.Timestamp(iso) > pd.Timestamp("2018-01-01")


def test_fox_era_split():
    """Rename-at-death plus a recycled base: pre-recycle membership rows
    resolve through the verified rename entry to the dead lineage instrument;
    rows from the new instrument's first quote resolve natively — the era
    boundary needs no override because the successor is a fresh listing whose
    first quote IS its tenure start."""
    d = InstrumentDirectory([
        Instrument("FOX", "FOX", pd.Timestamp("2019-03-13"), None),
        Instrument("FOX-200503", "FOX", pd.Timestamp("1998-11-11"),
                   suffix_month_end("200503"),
                   tenure_end=pd.Timestamp("2005-03-21")),
        Instrument("TFCF-201903", "TFCF", pd.Timestamp("1990-01-02"),
                   suffix_month_end("201903"),
                   tenure_end=pd.Timestamp("2019-03-19")),
    ])
    renames = {"FOX": "TFCF-201903"}
    # Pre-recycle era (and the last pre-recycle Friday): the dead lineage.
    assert resolve_instrument("FOX", pd.Timestamp("2018-06-01"), d,
                              renames=renames) == ("TFCF-201903", "renamed")
    assert resolve_instrument("FOX", pd.Timestamp("2019-03-08"), d,
                              renames=renames) == ("TFCF-201903", "renamed")
    # From the new instrument's first quote the base resolves natively.
    assert resolve_instrument("FOX", pd.Timestamp("2019-03-15"), d,
                              renames=renames) == ("FOX", "native")
    assert resolve_instrument("FOX", pd.Timestamp("2022-05-06"), d,
                              renames=renames) == ("FOX", "native")


# ---------------------------------------------------------------------------
# Amendment A3 (kickoff §5b) — true-weight baskets
# ---------------------------------------------------------------------------

def _weighted_fixture(growths: dict[str, float]):
    """Panel + signals + single-snapshot membership for a weight-arm test."""
    mem_idx = _mem_index()
    panel = _monotone_panel(growths, mem_idx)
    sig = precompute_member_signals(panel)
    snaps = _snapshots_single(list(growths))
    return mem_idx, panel, sig, snaps


def test_true_weights_renormalise_to_one():
    """A3: basket weights are the TRUE snapshot weights renormalised over the
    selected members — proportional to the snapshot Weight (%), summing to
    exactly 1, with weight_source \"snapshot\"."""
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011, "DDD": 0.0009}
    mem_idx, panel, sig, snaps = _weighted_fixture(g)
    weights = {pd.Timestamp("2018-01-05"):
               {"AAA": 40.0, "BBB": 30.0, "CCC": 20.0, "DDD": 10.0}}
    res = select_basket(ARM_BY_ID["I0"], mem_idx[600], snaps, panel, sig,
                        weights=weights)
    assert not res.fallback and res.weight_source == "snapshot"
    assert abs(sum(res.weights.values()) - 1.0) < 1e-12
    assert np.isclose(res.weights["AAA"], 0.4)
    assert np.isclose(res.weights["BBB"], 0.3)
    assert np.isclose(res.weights["CCC"], 0.2)
    assert np.isclose(res.weights["DDD"], 0.1)


def test_screened_arm_renormalises_over_survivors():
    """A3: a screened arm renormalises the SURVIVING members' true weights —
    the screened-out name's weight is redistributed pro rata, not spread
    equally."""
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011, "DDD": -0.0013}  # DDD fails
    mem_idx, panel, sig, snaps = _weighted_fixture(g)
    weights = {pd.Timestamp("2018-01-05"):
               {"AAA": 40.0, "BBB": 30.0, "CCC": 20.0, "DDD": 10.0}}
    res = select_basket(ARM_BY_ID["I1"], mem_idx[600], snaps, panel, sig,
                        weights=weights)
    assert not res.fallback and res.weight_source == "snapshot"
    assert set(res.weights) == {"AAA", "BBB", "CCC"}
    # Survivors' true weights 40/30/20 renormalise over 90.
    assert np.isclose(res.weights["AAA"], 40.0 / 90.0)
    assert np.isclose(res.weights["BBB"], 30.0 / 90.0)
    assert np.isclose(res.weights["CCC"], 20.0 / 90.0)
    assert abs(sum(res.weights.values()) - 1.0) < 1e-12


def test_missing_weight_snapshot_carries_forward():
    """A3: a snapshot absent from the weight table uses the line's last known
    weights (weight_source \"carried\"), and the build-level counter records
    the carried line-week."""
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011}
    mem_idx, panel, sig, _ = _weighted_fixture(g)
    # Two membership snapshots; weights exist only for the FIRST.
    snaps = {"2018-01-05": {"tickers": list(g)},
             "2020-06-19": {"tickers": list(g)}}
    weights = {pd.Timestamp("2018-01-05"):
               {"AAA": 50.0, "BBB": 30.0, "CCC": 20.0}}
    eff = mem_idx[mem_idx.searchsorted(pd.Timestamp("2020-08-07"))]
    res = select_basket(ARM_BY_ID["I0"], eff, snaps, panel, sig,
                        weights=weights)
    assert not res.fallback and res.weight_source == "carried"
    assert np.isclose(res.weights["AAA"], 0.5)
    # End-to-end: the counter increments for the line using carried weights.
    sector, mem_idx2 = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx2)
    iufs_names = list(_line_member_growths("IUFS"))
    membership["IUFS"] = {"2018-01-05": {"tickers": iufs_names}}
    mw = {"IUFS": {pd.Timestamp("2017-01-06"):
                   {t: float(10 + i) for i, t in enumerate(iufs_names)}}}
    build = build_arm_name_weights(ARM_BY_ID["I0"], sector["weights"],
                                   sector["closes"], sector["rebal_dates"],
                                   sector["eligible"], membership, signals,
                                   prices, member_weights=mw)
    assert build.weight_carry_weeks["IUFS"] == build.weeks_evaluated["IUFS"]
    assert build.weight_carry_weeks["IUFS"] > 0
    assert build.weight_ew_weeks["IUFS"] == 0


def test_ew_fallback_flag():
    """A3: no weight table at all, and a selected member without a usable
    weight (absent or non-positive), both drop the line-week to EQUAL weight
    with weight_source \"ew\" — never a silently mixed basis — and the
    build-level counter records it."""
    g = {"AAA": 0.0015, "BBB": 0.0013, "CCC": 0.0011}
    mem_idx, panel, sig, snaps = _weighted_fixture(g)
    eff = mem_idx[600]
    # No weights at all (the pre-A3 degenerate path).
    res_none = select_basket(ARM_BY_ID["I0"], eff, snaps, panel, sig,
                             weights=None)
    assert res_none.weight_source == "ew"
    assert all(np.isclose(v, 1.0 / 3.0) for v in res_none.weights.values())
    # A selected member missing from the snapshot's weight map.
    weights_gap = {pd.Timestamp("2018-01-05"): {"AAA": 60.0, "BBB": 40.0}}
    res_gap = select_basket(ARM_BY_ID["I0"], eff, snaps, panel, sig,
                            weights=weights_gap)
    assert res_gap.weight_source == "ew"
    assert all(np.isclose(v, 1.0 / 3.0) for v in res_gap.weights.values())
    # A non-positive weight is unusable for renormalisation -> same fallback.
    weights_zero = {pd.Timestamp("2018-01-05"):
                    {"AAA": 60.0, "BBB": 40.0, "CCC": 0.0}}
    res_zero = select_basket(ARM_BY_ID["I0"], eff, snaps, panel, sig,
                             weights=weights_zero)
    assert res_zero.weight_source == "ew"
    # End-to-end counter: a line with no weight table counts every basket week.
    sector, mem_idx2 = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx2)
    build = build_arm_name_weights(ARM_BY_ID["I0"], sector["weights"],
                                   sector["closes"], sector["rebal_dates"],
                                   sector["eligible"], membership, signals,
                                   prices, member_weights={})
    assert build.weight_ew_weeks["IUFS"] == build.weeks_evaluated["IUFS"]
    assert build.weight_carry_weeks["IUFS"] == 0


def test_true_weights_keyed_by_instrument_across_eras():
    """A3 x A1: weight lookup is by SNAPSHOT ticker, the basket key is the
    RESOLVED instrument — so a recycled ticker's true weight lands on the
    era-correct instrument column and the two companies' weights never blend."""
    sector, mem_idx = _sector_fixture()
    membership, signals, prices, _ = _member_fixtures(mem_idx)
    old_end = pd.Timestamp("2020-01-31")
    new_start = pd.Timestamp("2020-06-01")
    growths = {"RCY-202001": 0.0012, "RCY": 0.0014,
               "AAA": 0.0010, "BBB": 0.0008}
    panel = _monotone_panel(growths, mem_idx)
    panel.loc[panel.index > old_end, "RCY-202001"] = np.nan
    panel.loc[panel.index < new_start, "RCY"] = np.nan
    directory = InstrumentDirectory([
        Instrument("RCY-202001", "RCY", mem_idx[0], suffix_month_end("202001")),
        Instrument("RCY", "RCY", new_start, None),
        Instrument("AAA", "AAA", mem_idx[0], None),
        Instrument("BBB", "BBB", mem_idx[0], None),
    ])
    snaps = {"2018-01-05": {"tickers": ["RCY", "AAA", "BBB"]},
             "2020-06-19": {"tickers": ["RCY", "AAA", "BBB"]}}
    res = resolve_membership(snaps, directory)
    membership["IUFS"] = snaps
    prices["IUFS"] = panel
    signals["IUFS"] = precompute_member_signals(panel)
    # Same snapshot-ticker key "RCY" carries a DIFFERENT true weight per era.
    mw = {"IUFS": {pd.Timestamp("2018-01-05"):
                   {"RCY": 50.0, "AAA": 30.0, "BBB": 20.0},
                   pd.Timestamp("2020-06-19"):
                   {"RCY": 60.0, "AAA": 25.0, "BBB": 15.0}}}
    build = build_arm_name_weights(
        ARM_BY_ID["I0"], sector["weights"], sector["closes"],
        sector["rebal_dates"], sector["eligible"],
        membership, signals, prices,
        member_resolution={"IUFS": res["by_snapshot"]}, member_weights=mw)
    nw = build.name_weights
    assert "RCY-202001" in nw.columns and "RCY" in nw.columns
    on_rebal = nw.loc[sector["rebal_dates"]]
    line_w = sector["weights"].loc[sector["rebal_dates"], "IUFS"]
    old_rows = on_rebal.index[(on_rebal["RCY-202001"] > 0)]
    new_rows = on_rebal.index[(on_rebal["RCY"] > 0)]
    assert len(old_rows) and len(new_rows)
    # Old era: the dead instrument carries the 50% within-line share.
    rd_old = old_rows[0]
    assert np.isclose(on_rebal.loc[rd_old, "RCY-202001"],
                      0.5 * line_w.loc[rd_old])
    # New era: the live instrument carries the 60% within-line share.
    rd_new = new_rows[-1]
    assert np.isclose(on_rebal.loc[rd_new, "RCY"], 0.6 * line_w.loc[rd_new])
    # Never both eras at once, and no EW fallback anywhere.
    assert not ((on_rebal["RCY-202001"] > 0) & (on_rebal["RCY"] > 0)).any()
    assert build.weight_ew_weeks["IUFS"] == 0
