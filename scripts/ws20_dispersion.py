"""WS20 — within-panel trend-dispersion engine (Layer A primitives).

Registration: `C:\\dev\\KICKOFF_ws20-trend-dispersion.md`, frozen at vault-docs
`1cd0626` on 2026-09-18, §10 signed off the same day. This module is committed
WITH its selftests BEFORE any figure is computed (the WS5 / WS6d precedent).

The object, and what it is not
------------------------------
Sleeve A rotates on the breadth LEVEL — the share of a panel's constituents
above their own 200d MA, demeaned cross-sectionally. This module adds a second
moment of the same distribution: how far apart the constituents' trends are.

  per-name trend distance   d_i,t = close_i,t / SMA200_i,t - 1
  panel dispersion          D_p,t = cross-sectional IQR of d over valid names
  the regression object     Z_p,t = percentile of D_p,t within panel p's OWN
                                    history STRICTLY BEFORE t (expanding,
                                    minimum 252 sessions)

The interquartile range is primary and the standard deviation is reported
beside it: `d` is unbounded above, so one name at several multiples of its own
average would own a standard deviation and would not move an IQR. The
within-panel standardisation exists because the 14 sleeve A panels carry
between 21 and 602 constituents and differ systematically in sector
volatility — raw cross-panel dispersion is not a like-for-like comparison.

The verdict reads a CONDITIONAL coefficient, never a sort
---------------------------------------------------------
Dispersion and breadth are mechanically related: at 0% or 100% breadth every
name sits on the same side of its average and the spread is compressed by
construction, while readings near 50% admit the widest spread. An
unconditional sort on dispersion would therefore rediscover the breadth level
in different clothing — the failure `2026-07-17-breadth-thrust-etf-3` filed as
the placebo in costume. `cross_section_coefficients` regresses the forward
return on BOTH the demeaned breadth level and dispersion, and returns the
coefficient on dispersion alone; `evaluate_gates` is given nothing else.

Denominator discipline: the valid mask is the deployed one
(`run_ma200_sweep.compute_ma200_breadth` — price present AND the MA
computable at min_periods 180), so dispersion is measured over exactly the
names that produced the breadth reading it is being tested against.

No look-ahead: every quantity is a trailing window up to and including its own
date, and `prior_percentile` reads history STRICTLY before t. The CALLER
applies the deployed t-1 read at each rebalance, exactly as the engine does
(`run_portfolio`: `prev_idx = closes.index.get_loc(rd) - 1`).

Dates: pandas DatetimeIndex throughout; no manual date arithmetic. (Python
`datetime` months are 1-indexed; no month indexing is done here.)
"""

from __future__ import annotations

from bisect import bisect_left, insort

import numpy as np
import pandas as pd

# --- Deployed conventions, inherited and not varied here -------------------
MA_PERIOD = 200
MIN_PERIODS_FRACTION = 0.9          # -> min_periods 180, the deployed mask

# --- Registered WS20 parameters (§4, frozen 2026-09-18) --------------------
MIN_VALID_NAMES = 15                # a panel-week below this is dropped (§6.3)
STD_MIN_PERIODS = 252               # expanding standardisation floor (§4.1)
FORWARD_WEEKS = 4                   # primary horizon (§4.2, §10 item 4)
BLOCK_WEEKS = 13                    # > 3x the forward overlap (§4.2, §6.4)
N_BOOT = 2000                       # bootstrap resamples (§4.2)
N_NULL = 1000                       # shuffle-null paths (§4.3)
SEED = 20260918                     # frozen in the registration (§4.3)

# Reported-only neighbours; never verdict-bearing (§4.2, §5.5).
REPORTED_HORIZONS_WEEKS = (1, 13)
DISPERSION_MEASURES = ("iqr", "sd")


def _min_periods(period: int) -> int:
    return max(1, int(period * MIN_PERIODS_FRACTION))


# ---------------------------------------------------------------------------
# The statistic
# ---------------------------------------------------------------------------

def trend_distance(prices: pd.DataFrame, period: int = MA_PERIOD):
    """Per-name trend distance and the deployed validity mask.

    Returns (distance, valid) aligned to `prices`. `distance` is
    close / SMA(close) - 1 wherever valid and NaN elsewhere; `valid` is the
    deployed mask, price present AND the MA computable at min_periods 180.

    The mask is deliberately identical to `compute_ma200_breadth`'s
    `both_valid`, so the dispersion of a panel-day is measured over exactly the
    constituents that produced that day's breadth reading.
    """
    if period <= 1:
        raise ValueError("period must be > 1")
    ma = prices.rolling(period, min_periods=_min_periods(period)).mean()
    valid = prices.notna() & ma.notna()
    dist = (prices / ma - 1.0).where(valid)
    return dist, valid


def panel_dispersion(prices: pd.DataFrame, period: int = MA_PERIOD,
                     min_valid: int = MIN_VALID_NAMES) -> pd.DataFrame:
    """Daily cross-sectional dispersion of the per-name trend distance.

    Returns a frame indexed like `prices` with columns:
      iqr      - interquartile range of `d` across valid names (PRIMARY)
      sd       - standard deviation of `d` (reported beside, never a gate)
      n_valid  - the count the two were computed over

    Days with fewer than `min_valid` valid names give NaN for both measures
    and are dropped by the caller — dispersion estimated on a handful of names
    is a different quantity from the same estimate on hundreds (§6.3).
    """
    dist, valid = trend_distance(prices, period)
    n_valid = valid.sum(axis=1)
    q1 = dist.quantile(0.25, axis=1)
    q3 = dist.quantile(0.75, axis=1)
    out = pd.DataFrame(
        {"iqr": q3 - q1, "sd": dist.std(axis=1, ddof=1), "n_valid": n_valid},
        index=prices.index,
    )
    thin = n_valid < min_valid
    out.loc[thin, ["iqr", "sd"]] = np.nan
    return out


def prior_percentile(series: pd.Series,
                     min_periods: int = STD_MIN_PERIODS) -> pd.Series:
    """Percentile of each value within the SAME series' strictly-prior history.

    `out[t]` is the share of the non-NaN observations BEFORE t that are
    strictly below `series[t]`, or NaN while fewer than `min_periods` prior
    observations exist. The current value never enters its own reference
    window — that is the whole point, and `test_prior_percentile_excludes_self`
    plus the final-bar perturbation test pin it.

    Expanding rather than rolling: what was knowable at t is everything before
    t, and a rolling window would discard information the operator had.
    """
    if min_periods < 1:
        raise ValueError("min_periods must be >= 1")
    hist: list[float] = []
    out = np.full(len(series), np.nan)
    for i, v in enumerate(series.to_numpy(dtype=float)):
        if len(hist) >= min_periods and not np.isnan(v):
            out[i] = bisect_left(hist, v) / len(hist)
        if not np.isnan(v):
            insort(hist, v)
    return pd.Series(out, index=series.index)


# ---------------------------------------------------------------------------
# The conditional estimate
# ---------------------------------------------------------------------------

def _week_instrument(b: np.ndarray, z: np.ndarray) -> np.ndarray | None:
    """Frisch-Waugh-Lovell instrument for the dispersion coefficient.

    Regressing r on [1, b, z] gives a coefficient on z equal to
    (z~'r) / (z~'z~), where z~ is the residual of z on [1, b]. Returning that
    instrument (normalised so it dots with r to give the coefficient) buys the
    exact additive decomposition G-A4 needs: the coefficient is a weighted sum
    over panels, so each panel's contribution is its own term.

    `b` may be a single column or a control matrix (the ln(n) robustness leg).
    Returns None where the week is degenerate (no residual variation in z once
    breadth is projected out — e.g. z collinear with b, or too few panels for
    the intercept, the controls and a residual degree of freedom).
    """
    b = np.asarray(b, dtype=float)
    if b.ndim == 1:
        b = b[:, None]
    n, k = b.shape
    if n < k + 3:
        return None
    X = np.column_stack([np.ones(n), b])
    coef, *_ = np.linalg.lstsq(X, z, rcond=None)
    z_res = z - X @ coef
    denom = float(z_res @ z_res)
    if not np.isfinite(denom) or denom <= 1e-12:
        return None
    return z_res / denom


def cross_section_coefficients(returns: pd.DataFrame, breadth: pd.DataFrame,
                               dispersion: pd.DataFrame,
                               extra: dict[str, pd.DataFrame] | None = None):
    """Weekly cross-sectional regression of forward return on breadth AND
    dispersion — the Fama-MacBeth estimate at the heart of Layer A.

    All three frames are indexed by decision date with columns = panels, and
    carry values ALREADY read at the deployed t-1 offset by the caller.
    `extra` adds further controls (the ln(n) robustness leg of §5.5); it moves
    the instrument and is therefore never used for the primary estimate.

    Returns (coefficients, contributions, n_panels):
      coefficients  - Series, the per-week coefficient on dispersion
      contributions - DataFrame, per-panel additive terms summing to it
      n_panels      - Series, panels entering each week

    A week is used only where a panel has all of return, breadth and
    dispersion; panels are dropped pairwise, never imputed.
    """
    coefs, contribs, counts = {}, {}, {}
    controls = extra or {}
    for date in returns.index:
        r = returns.loc[date]
        b = breadth.loc[date]
        z = dispersion.loc[date]
        frame = pd.DataFrame({"r": r, "b": b, "z": z})
        for name, panel in controls.items():
            frame[name] = panel.loc[date]
        frame = frame.dropna()
        counts[date] = len(frame)
        if len(frame) < 4:
            continue
        b_cols = frame[["b"] + list(controls)].to_numpy(dtype=float)
        w = _week_instrument(b_cols, frame["z"].to_numpy(dtype=float))
        if w is None:
            continue
        terms = pd.Series(w * frame["r"].to_numpy(dtype=float), index=frame.index)
        contribs[date] = terms
        coefs[date] = float(terms.sum())
    coef_s = pd.Series(coefs).sort_index()
    contrib_df = pd.DataFrame(contribs).T.sort_index().reindex(columns=returns.columns)
    return coef_s, contrib_df, pd.Series(counts).sort_index()


def univariate_coefficients(returns: pd.DataFrame,
                            dispersion: pd.DataFrame) -> pd.Series:
    """Dispersion WITHOUT the breadth control — the confounded figure.

    Reported in §5.5 so the confound is visible as a number, and barred from
    every gate. `evaluate_gates` cannot see it: it is not one of its arguments.
    """
    out = {}
    for date in returns.index:
        frame = pd.DataFrame({"r": returns.loc[date], "z": dispersion.loc[date]}).dropna()
        if len(frame) < 3:
            continue
        z = frame["z"].to_numpy(dtype=float)
        z_res = z - z.mean()
        denom = float(z_res @ z_res)
        if denom <= 1e-12:
            continue
        out[date] = float(z_res @ frame["r"].to_numpy(dtype=float) / denom)
    return pd.Series(out).sort_index()


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def circular_block_bootstrap_mean(x: pd.Series | np.ndarray,
                                  block: int = BLOCK_WEEKS,
                                  n_boot: int = N_BOOT,
                                  seed: int = SEED) -> np.ndarray:
    """Circular block bootstrap of the mean of a weekly coefficient series.

    The block MUST exceed the forward-return overlap: consecutive weekly
    observations of a 4-week forward return share three quarters of their
    window, so an i.i.d. resample would understate the standard error and make
    noise look significant (§6.4). 13 weeks is more than three times the
    overlap and is fixed in the registration, not chosen after seeing an
    interval.
    """
    v = np.asarray(x, dtype=float)
    v = v[~np.isnan(v)]
    T = len(v)
    if T == 0:
        return np.array([])
    if block < 1:
        raise ValueError("block must be >= 1")
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(T / block))
    starts = rng.integers(0, T, size=(n_boot, n_blocks))
    offsets = np.arange(block)
    idx = (starts[:, :, None] + offsets[None, None, :]) % T
    return v[idx.reshape(n_boot, -1)[:, :T]].mean(axis=1)


def permute_within_week(z: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """One week's dispersion values, reordered across panels.

    Exposed so the preservation claim is testable rather than asserted: the
    returned array is a permutation of the input, so the weekly cross-sectional
    DISTRIBUTION of dispersion is untouched and only the panel-to-dispersion
    pairing is destroyed (`test_shuffle_preserves_weekly_distribution`).
    """
    return rng.permutation(z)


def shuffle_null_means(returns: pd.DataFrame, breadth: pd.DataFrame,
                       dispersion: pd.DataFrame, n_paths: int = N_NULL,
                       seed: int = SEED) -> np.ndarray:
    """N-shuffle: permute dispersion ACROSS panels WITHIN each week.

    Breadth, the returns and the week structure are untouched, and the weekly
    cross-sectional distribution of dispersion is preserved exactly — so the
    only thing destroyed is the panel-to-dispersion pairing. If dispersion is
    the breadth level in costume, the realised estimate lands inside this
    distribution.
    """
    rng = np.random.default_rng(seed)
    out = np.full(n_paths, np.nan)
    dates = list(returns.index)
    cache = []
    for date in dates:
        frame = pd.DataFrame({"r": returns.loc[date], "b": breadth.loc[date],
                              "z": dispersion.loc[date]}).dropna()
        if len(frame) >= 4:
            cache.append((frame["r"].to_numpy(dtype=float),
                          frame["b"].to_numpy(dtype=float),
                          frame["z"].to_numpy(dtype=float)))
    for p in range(n_paths):
        vals = []
        for r, b, z in cache:
            w = _week_instrument(b, permute_within_week(z, rng))
            if w is not None:
                vals.append(float(w @ r))
        out[p] = float(np.mean(vals)) if vals else np.nan
    return out


def percentile_of(value: float, distribution: np.ndarray) -> float:
    """Share of the distribution strictly below `value`, in [0, 1]."""
    d = np.asarray(distribution, dtype=float)
    d = d[~np.isnan(d)]
    if len(d) == 0 or not np.isfinite(value):
        return float("nan")
    return float((d < value).mean())


# ---------------------------------------------------------------------------
# G-A4 — thinness
# ---------------------------------------------------------------------------

def decompose(coefficients: pd.Series, contributions: pd.DataFrame) -> dict:
    """Additive decomposition of the mean coefficient by panel and by year.

    Both sum to the mean coefficient by construction: the per-week coefficient
    is the sum of its panels' terms, and the mean over weeks is the sum of the
    per-year means weighted by each year's share of weeks.
    """
    total = float(coefficients.mean())
    by_panel = contributions.sum(axis=0, skipna=True) / len(coefficients)
    by_year = coefficients.groupby(coefficients.index.year).sum() / len(coefficients)
    return {
        "total": total,
        "by_panel": by_panel.to_dict(),
        "by_year": {int(k): float(v) for k, v in by_year.items()},
        "panel_shares": {k: (float(v / total) if total else float("nan"))
                         for k, v in by_panel.items()},
        "year_shares": {int(k): (float(v / total) if total else float("nan"))
                        for k, v in by_year.items()},
    }


def thinness_flag(decomposition: dict, max_single_share: float = 0.60,
                  min_contributors: int = 3,
                  contributor_share: float = 0.10) -> dict:
    """G-A4 — can override a pass and force INCONCLUSIVE (§5.4).

    THIN if more than 60% of the coefficient comes from ONE panel, or more
    than 60% from ONE calendar year, or fewer than three panels contribute
    more than 10% each. Straight from `2026-07-15-crypto-breadth-7`: an effect
    that is one panel or one year is not a property of panels.

    The rule is fixed here rather than chosen after seeing the decomposition.
    """
    p_shares = decomposition.get("panel_shares", {})
    y_shares = decomposition.get("year_shares", {})
    top_panel = max(p_shares.values(), default=float("nan"))
    top_year = max(y_shares.values(), default=float("nan"))
    n_contrib = sum(1 for v in p_shares.values() if v > contributor_share)
    thin = bool(
        (np.isfinite(top_panel) and top_panel > max_single_share)
        or (np.isfinite(top_year) and top_year > max_single_share)
        or n_contrib < min_contributors
    )
    return {"thin": thin, "top_panel_share": float(top_panel),
            "top_year_share": float(top_year), "n_contributors": int(n_contrib)}


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def evaluate_gates(mean_coef: float, ci_low: float, ci_high: float,
                   half1_mean: float, half2_mean: float,
                   null_percentile: float, thin: bool) -> dict:
    """Layer A's three gates and the thinness override (§5.1, §5.4).

    Every argument is a property of the CONDITIONAL coefficient. The
    univariate figure, the raw dispersion level and the Layer B statistics are
    not arguments and cannot reach the verdict — which is the point, and is
    asserted by `test_verdict_cannot_see_univariate`.

      G-A1  the 95% block-bootstrap CI excludes zero
      G-A2  the sign holds in both halves
      G-A3  the estimate sits outside the central 95% of the shuffle null
      G-A4  thinness can override a pass and force INCONCLUSIVE

    Any gate failing gives NO-INFORMATION: the study closes, Layer B does not
    open, and nothing is built.
    """
    g1 = bool(np.isfinite(ci_low) and np.isfinite(ci_high)
              and (ci_low > 0 or ci_high < 0))
    g2 = bool(np.isfinite(half1_mean) and np.isfinite(half2_mean)
              and np.sign(half1_mean) == np.sign(half2_mean)
              and np.sign(half1_mean) != 0)
    g3 = bool(np.isfinite(null_percentile)
              and (null_percentile < 0.025 or null_percentile > 0.975))
    passed = g1 and g2 and g3
    if not passed:
        verdict = "NO-INFORMATION"
    elif thin:
        verdict = "INCONCLUSIVE (THIN)"
    else:
        verdict = "CONFIRMED"
    return {
        "G_A1_ci_excludes_zero": g1,
        "G_A2_sign_holds_in_halves": g2,
        "G_A3_outside_null": g3,
        "G_A4_thin": bool(thin),
        "layer_a_passes": bool(passed and not thin),
        "verdict": verdict,
        "mean_coefficient": float(mean_coef),
    }


def layer_b_tilt_sign(mean_coef: float) -> int:
    """The Layer B tilt orientation, FIXED by the Layer A coefficient (§6.7).

    Declared rather than chosen: if the sign were picked after seeing Layer B,
    the +0.10 bar would be decoration.
    """
    if not np.isfinite(mean_coef) or mean_coef == 0:
        raise ValueError("Layer B cannot open without a finite Layer A coefficient")
    return 1 if mean_coef > 0 else -1
