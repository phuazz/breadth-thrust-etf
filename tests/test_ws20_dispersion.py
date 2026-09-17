"""Selftests for the WS20 within-panel trend-dispersion engine
(scripts/ws20_dispersion.py), committed BEFORE any WS20 figure is computed
(the WS5 / WS6d precedent, and §7 of the registration).

Coverage maps to §6 of `C:\\dev\\KICKOFF_ws20-trend-dispersion.md`:

  6.1 dispersion is the breadth level in costume:
      test_conditional_coefficient_kills_the_confound,
      test_verdict_cannot_see_univariate
  6.2 one extreme name drives the statistic:
      test_iqr_survives_one_outlier_where_sd_does_not
  6.3 panel size and sector volatility contaminate the comparison:
      test_panel_week_below_min_names_is_dropped,
      test_control_matrix_is_accepted
  6.4 overlapping forward windows understate the standard error:
      test_block_exceeds_forward_overlap,
      test_block_bootstrap_widens_on_autocorrelated_series
  6.5 look-ahead through the standardisation:
      test_prior_percentile_excludes_self, test_prior_percentile_min_periods,
      test_final_bar_perturbation_invariance
  6.6 a menu appears by accident:
      (guarded in the runner, which records every cell computed)
  6.7 the Layer B tilt is chosen rather than declared:
      test_layer_b_sign_is_fixed_by_layer_a

  Deployed-parity anchor:
      test_dispersion_mask_matches_deployed_breadth
  Estimator correctness:
      test_estimator_recovers_planted_coefficient,
      test_contributions_sum_to_the_coefficient,
      test_constant_returns_give_zero_coefficient
  Null construction:
      test_shuffle_preserves_weekly_distribution, test_seed_reproduces
  Verdict rule:
      test_each_gate_is_required, test_thinness_overrides_a_pass

  Date-boundary rule (vault CLAUDE.md — one month, one year boundary):
      test_month_boundary_continuity, test_year_boundary_continuity
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ws20_dispersion import (  # noqa: E402
    BLOCK_WEEKS,
    FORWARD_WEEKS,
    MA_PERIOD,
    MIN_VALID_NAMES,
    circular_block_bootstrap_mean,
    cross_section_coefficients,
    decompose,
    evaluate_gates,
    layer_b_tilt_sign,
    panel_dispersion,
    percentile_of,
    permute_within_week,
    prior_percentile,
    shuffle_null_means,
    thinness_flag,
    trend_distance,
    univariate_coefficients,
)
from run_ma200_sweep import compute_ma200_breadth  # noqa: E402 (deployed leg)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

def _prices(n_days=900, n_tickers=30, seed=20, start="2019-01-02"):
    """Deterministic constituent adjusted-close panel (random walk + drift)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n_days, freq="B")
    rets = rng.normal(loc=0.0003, scale=0.016, size=(n_days, n_tickers))
    prices = 100.0 * np.exp(np.cumsum(rets, axis=0))
    cols = [f"T{i:02d}" for i in range(n_tickers)]
    return pd.DataFrame(prices, index=idx, columns=cols)


def _weekly_panel(n_weeks=300, n_panels=14, seed=5, start="2019-09-27"):
    """Weekly decision-date frames: (returns, breadth, dispersion)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n_weeks, freq="W-FRI")
    cols = [f"P{i:02d}" for i in range(n_panels)]
    b = pd.DataFrame(rng.normal(0, 0.15, (n_weeks, n_panels)), idx, cols)
    z = pd.DataFrame(rng.uniform(0, 1, (n_weeks, n_panels)), idx, cols)
    r = pd.DataFrame(rng.normal(0, 0.03, (n_weeks, n_panels)), idx, cols)
    return r, b, z


# ---------------------------------------------------------------------------
# 6.2 — the statistic is robust to one extreme name
# ---------------------------------------------------------------------------

def test_iqr_survives_one_outlier_where_sd_does_not():
    px = _prices()
    base = panel_dispersion(px)
    spiked = px.copy()
    # One name doubles-and-doubles-again over the last 30 sessions: a genuine
    # extreme trend, not a data error, and exactly what owns a variance.
    spiked.iloc[-30:, 0] = spiked.iloc[-30:, 0] * 5.0
    after = panel_dispersion(spiked)
    d_iqr = abs(after["iqr"].iloc[-1] - base["iqr"].iloc[-1])
    d_sd = abs(after["sd"].iloc[-1] - base["sd"].iloc[-1])
    assert d_sd > 5 * d_iqr, (
        f"the outlier must move the standard deviation far more than the "
        f"interquartile range (d_sd={d_sd:.4f}, d_iqr={d_iqr:.4f})")
    # Stated relatively, which is the claim that matters: the standard
    # deviation more than DOUBLES while the interquartile range moves by a
    # fraction of its own level. With 30 names one outlier still shifts a
    # quartile position by about a slot, so the IQR is robust, not immune.
    assert d_sd / base["sd"].iloc[-1] > 1.0
    assert d_iqr / base["iqr"].iloc[-1] < 0.20


def test_panel_week_below_min_names_is_dropped():
    px = _prices(n_tickers=MIN_VALID_NAMES - 1)
    disp = panel_dispersion(px)
    assert disp["iqr"].isna().all(), "a thin panel must not produce a value"
    assert disp["n_valid"].max() <= MIN_VALID_NAMES - 1


# ---------------------------------------------------------------------------
# Deployed-parity anchor
# ---------------------------------------------------------------------------

def test_dispersion_mask_matches_deployed_breadth():
    """The share of valid names with a POSITIVE trend distance is exactly the
    deployed breadth, so dispersion is measured over the same denominator that
    produced the breadth reading it is tested against."""
    px = _prices()
    dist, valid = trend_distance(px, MA_PERIOD)
    ours = (dist > 0).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    deployed = compute_ma200_breadth(px, MA_PERIOD)
    both = ours.notna() & deployed.notna()
    assert both.sum() > 100, "fixture must produce a usable overlap"
    assert float((ours[both] - deployed[both]).abs().max()) < 1e-12


# ---------------------------------------------------------------------------
# 6.5 — no look-ahead through the standardisation
# ---------------------------------------------------------------------------

def test_prior_percentile_excludes_self():
    s = pd.Series(np.arange(300, dtype=float))
    pct = prior_percentile(s, min_periods=252)
    # A strictly increasing series: every value is above ALL of its history.
    assert float(pct.dropna().min()) == 1.0
    # And the reference window never contains the observation itself, so a
    # 1.0 reading is reachable at all — with self included it could not be.
    assert pct.notna().sum() == 300 - 252


def test_prior_percentile_min_periods():
    s = pd.Series(np.random.default_rng(1).normal(size=400))
    pct = prior_percentile(s, min_periods=252)
    assert pct.iloc[:252].isna().all()
    assert pct.iloc[252:].notna().all()


def test_final_bar_perturbation_invariance():
    """Every quantity at a date before T is invariant to any mutation of the
    final bar — the look-ahead test the WS5 engine also carries."""
    px = _prices()
    disp = panel_dispersion(px)
    pct = prior_percentile(disp["iqr"])

    mutated = px.copy()
    mutated.iloc[-1] = mutated.iloc[-1] * 3.0
    disp_m = panel_dispersion(mutated)
    pct_m = prior_percentile(disp_m["iqr"])

    head = slice(None, -1)
    assert disp["iqr"][head].equals(disp_m["iqr"][head])
    pd.testing.assert_series_equal(pct[head], pct_m[head])


# ---------------------------------------------------------------------------
# 6.1 — the conditional coefficient, and only it
# ---------------------------------------------------------------------------

def test_conditional_coefficient_kills_the_confound():
    """Dispersion built as a deterministic function of breadth, with returns
    driven ONLY by breadth. The univariate coefficient must be large and the
    conditional one must be ~0 — that is the whole design of §3."""
    rng = np.random.default_rng(11)
    idx = pd.date_range("2019-09-27", periods=200, freq="W-FRI")
    cols = [f"P{i:02d}" for i in range(14)]
    b = pd.DataFrame(rng.normal(0, 0.15, (200, 14)), idx, cols)
    # Dispersion is breadth plus a little of its own noise — the realistic
    # confound. Perfect collinearity would simply make the week degenerate and
    # the estimator would skip it, which tests nothing.
    z = b * 2.0 + 0.5 + rng.normal(0, 0.05, (200, 14))
    r = b * 0.4 + rng.normal(0, 0.001, (200, 14))   # returns depend on breadth

    uni = univariate_coefficients(r, z)
    coef, _, _ = cross_section_coefficients(r, b, z)
    assert abs(float(uni.mean())) > 0.15, "the confounded figure must be large"
    assert abs(float(coef.mean())) < 0.02, "the conditional figure must vanish"
    assert abs(float(coef.mean())) < 0.05 * abs(float(uni.mean()))


def test_estimator_recovers_planted_coefficient():
    rng = np.random.default_rng(3)
    idx = pd.date_range("2019-09-27", periods=400, freq="W-FRI")
    cols = [f"P{i:02d}" for i in range(14)]
    b = pd.DataFrame(rng.normal(0, 0.15, (400, 14)), idx, cols)
    z = pd.DataFrame(rng.uniform(0, 1, (400, 14)), idx, cols)
    r = 0.4 * b + 2.0 * z + pd.DataFrame(
        rng.normal(0, 0.02, (400, 14)), idx, cols)
    coef, _, _ = cross_section_coefficients(r, b, z)
    assert abs(float(coef.mean()) - 2.0) < 0.05


def test_contributions_sum_to_the_coefficient():
    r, b, z = _weekly_panel()
    coef, contrib, counts = cross_section_coefficients(r, b, z)
    row_sums = contrib.sum(axis=1, skipna=True)
    assert float((row_sums - coef).abs().max()) < 1e-10
    dec = decompose(coef, contrib)
    assert abs(sum(dec["by_panel"].values()) - dec["total"]) < 1e-10
    assert abs(sum(dec["by_year"].values()) - dec["total"]) < 1e-10
    assert int(counts.max()) == 14


def test_constant_returns_give_zero_coefficient():
    """The instrument is a regression residual, so it sums to zero: a week in
    which every panel earns the same return cannot produce a coefficient."""
    r, b, z = _weekly_panel(n_weeks=50)
    flat = r.copy()
    for d in flat.index:
        flat.loc[d] = 0.012
    coef, _, _ = cross_section_coefficients(flat, b, z)
    assert float(coef.abs().max()) < 1e-10


def test_control_matrix_is_accepted():
    """The ln(n) robustness leg of §5.5 adds a control; the estimator must take
    it without special-casing, and the answer must move."""
    r, b, z = _weekly_panel()
    rng = np.random.default_rng(9)
    ln_n = pd.DataFrame(rng.uniform(3, 6.5, r.shape), r.index, r.columns)
    base, _, _ = cross_section_coefficients(r, b, z)
    with_ctl, _, _ = cross_section_coefficients(r, b, z, extra={"ln_n": ln_n})
    assert with_ctl.notna().sum() > 0.9 * len(r)
    assert float(base.mean()) != float(with_ctl.mean())


# ---------------------------------------------------------------------------
# 6.4 — the bootstrap respects the forward overlap
# ---------------------------------------------------------------------------

def test_block_exceeds_forward_overlap():
    assert BLOCK_WEEKS > FORWARD_WEEKS
    assert BLOCK_WEEKS >= 3 * FORWARD_WEEKS, (
        "the registration fixes the block at more than three times the overlap")


def test_block_bootstrap_widens_on_autocorrelated_series():
    """On a persistent series an i.i.d. resample understates the spread. The
    registered 13-week block must give a WIDER interval than block=1 — which
    is the reason the block is registered at all."""
    rng = np.random.default_rng(4)
    x = np.zeros(400)
    for i in range(1, 400):
        x[i] = 0.9 * x[i - 1] + rng.normal(0, 0.1)
    narrow = circular_block_bootstrap_mean(x, block=1, n_boot=1000, seed=1)
    wide = circular_block_bootstrap_mean(x, block=BLOCK_WEEKS, n_boot=1000, seed=1)
    assert wide.std() > 1.5 * narrow.std()


# ---------------------------------------------------------------------------
# The null
# ---------------------------------------------------------------------------

def test_shuffle_preserves_weekly_distribution():
    rng = np.random.default_rng(7)
    z = np.array([0.1, 0.4, 0.4, 0.9, 0.2, 0.7])
    out = permute_within_week(z, rng)
    assert sorted(out.tolist()) == sorted(z.tolist())
    assert len(out) == len(z)


def test_shuffle_null_is_centred_near_zero_on_random_data():
    r, b, z = _weekly_panel(n_weeks=120)
    null = shuffle_null_means(r, b, z, n_paths=200, seed=1)
    assert np.isfinite(null).all()
    assert null.std() > 0
    assert abs(float(np.median(null))) < 3 * null.std()


def test_seed_reproduces():
    r, b, z = _weekly_panel(n_weeks=60)
    a = shuffle_null_means(r, b, z, n_paths=50, seed=20260918)
    c = shuffle_null_means(r, b, z, n_paths=50, seed=20260918)
    assert np.array_equal(a, c)
    d = shuffle_null_means(r, b, z, n_paths=50, seed=1)
    assert not np.array_equal(a, d)
    x = np.arange(100, dtype=float)
    assert np.array_equal(
        circular_block_bootstrap_mean(x, seed=5, n_boot=50),
        circular_block_bootstrap_mean(x, seed=5, n_boot=50))


def test_percentile_of():
    dist = np.arange(1000, dtype=float)
    assert abs(percentile_of(500.0, dist) - 0.5) < 0.01
    assert percentile_of(-1.0, dist) == 0.0
    assert percentile_of(1e9, dist) == 1.0


# ---------------------------------------------------------------------------
# The verdict rule
# ---------------------------------------------------------------------------

def _passing(**over):
    args = dict(mean_coef=0.5, ci_low=0.2, ci_high=0.8, half1_mean=0.4,
                half2_mean=0.6, null_percentile=0.99, thin=False)
    args.update(over)
    return evaluate_gates(**args)


def test_each_gate_is_required():
    assert _passing()["verdict"] == "CONFIRMED"
    assert _passing(ci_low=-0.2)["verdict"] == "NO-INFORMATION"        # G-A1
    assert _passing(half2_mean=-0.6)["verdict"] == "NO-INFORMATION"    # G-A2
    assert _passing(null_percentile=0.5)["verdict"] == "NO-INFORMATION"  # G-A3
    assert _passing(null_percentile=0.01)["verdict"] == "CONFIRMED"    # two-sided


def test_thinness_overrides_a_pass():
    out = _passing(thin=True)
    assert out["verdict"] == "INCONCLUSIVE (THIN)"
    assert out["layer_a_passes"] is False


def test_verdict_cannot_see_univariate():
    """The confounded figure is not an argument of the verdict function, so it
    cannot reach a gate however large it is (§5.3, §6.1)."""
    import inspect
    params = set(inspect.signature(evaluate_gates).parameters)
    assert "univariate" not in " ".join(params)
    assert params == {"mean_coef", "ci_low", "ci_high", "half1_mean",
                      "half2_mean", "null_percentile", "thin"}
    # A null conditional estimate reads NO-INFORMATION whatever else is true.
    assert _passing(ci_low=-0.1, ci_high=0.1)["verdict"] == "NO-INFORMATION"


def test_thinness_flag_rules():
    dec = {"panel_shares": {"A": 0.7, "B": 0.2, "C": 0.1},
           "year_shares": {2020: 0.5, 2021: 0.5}}
    assert thinness_flag(dec)["thin"] is True          # one panel above 60%
    dec2 = {"panel_shares": {"A": 0.3, "B": 0.3, "C": 0.4},
            "year_shares": {2020: 0.9, 2021: 0.1}}
    assert thinness_flag(dec2)["thin"] is True         # one year above 60%
    dec3 = {"panel_shares": {"A": 0.5, "B": 0.5},
            "year_shares": {2020: 0.5, 2021: 0.5}}
    assert thinness_flag(dec3)["thin"] is True         # fewer than three panels
    dec4 = {"panel_shares": {"A": 0.4, "B": 0.35, "C": 0.25},
            "year_shares": {2020: 0.4, 2021: 0.35, 2022: 0.25}}
    assert thinness_flag(dec4)["thin"] is False


def test_layer_b_sign_is_fixed_by_layer_a():
    assert layer_b_tilt_sign(0.4) == 1
    assert layer_b_tilt_sign(-0.4) == -1
    for bad in (0.0, float("nan")):
        try:
            layer_b_tilt_sign(bad)
        except ValueError:
            continue
        raise AssertionError("Layer B must not open without a Layer A estimate")


# ---------------------------------------------------------------------------
# Date boundaries (vault CLAUDE.md)
# ---------------------------------------------------------------------------

# The fixture starts in 2017 so that BOTH warm-ups are complete at the
# boundaries under test: 200 sessions for the moving average, then 252 prior
# dispersion observations for the percentile.
def _warm_panel():
    return _prices(n_days=1100, start="2017-01-02")


def test_month_boundary_continuity():
    """A month boundary is not a special date: the statistic and its
    percentile are continuous across 2020-01-31 / 2020-02-03."""
    disp = panel_dispersion(_warm_panel())
    pct = prior_percentile(disp["iqr"], min_periods=252)
    jan = pd.Timestamp("2020-01-31")
    feb = pd.Timestamp("2020-02-03")
    assert jan in disp.index and feb in disp.index
    assert np.isfinite(disp["iqr"].loc[jan]) and np.isfinite(disp["iqr"].loc[feb])
    assert np.isfinite(pct.loc[jan]) and np.isfinite(pct.loc[feb])
    assert abs(disp["iqr"].loc[feb] - disp["iqr"].loc[jan]) < 0.05


def test_year_boundary_continuity():
    """The expanding reference window must not reset at the turn of the year.
    (A `freq='B'` index treats 1 January as a session, which is what makes it
    a usable boundary fixture here.)"""
    disp = panel_dispersion(_warm_panel())
    pct = prior_percentile(disp["iqr"], min_periods=252)
    dec = pd.Timestamp("2019-12-31")
    assert dec in disp.index
    nxt = disp.index[disp.index.get_loc(dec) + 1]
    assert nxt.year == 2020, "the next session must cross the year boundary"
    assert np.isfinite(disp["iqr"].loc[nxt])
    assert abs(disp["iqr"].loc[nxt] - disp["iqr"].loc[dec]) < 0.05
    assert np.isfinite(pct.loc[dec]) and np.isfinite(pct.loc[nxt])
    # A reset would show as the percentile going NaN, or as its history
    # collapsing to a handful of observations right after the boundary.
    assert pct.loc[dec:].notna().all()
