"""Cross-sectional ETF scanner — indicator library.

Every column defined in the scanner spec (§3) is computed here. The
module is pure: no I/O, no logging, no global state, no network. That is
deliberate — these functions are the part of the scanner that can be
silently wrong without anything failing, so they are isolated where a
frozen-fixture test can pin every output to an exact value.

Four conventions the spec leaves open, decided here and stated so they
can be overruled rather than discovered:

1. **MA slope estimator.** ``trend_state`` needs "the MA's 20-day slope".
   Implemented as the plain difference ``MA(t) - MA(t-20) > 0`` rather
   than an OLS fit. It is monotone in the same information, has no
   window-weighting choice to tune, and matches how the rest of the repo
   reads trend (level now versus level then).

2. **Trend-state precedence.** The five spec categories overlap: a fresh
   golden cross can satisfy both "Strong up" (C > MA50 > MA200, both
   rising) and "Range" (|MA50/MA200 - 1| < 1%). Precedence is
   Strong up -> Strong down -> Range -> Up -> Down, so a rising
   tight-MA configuration reads as Strong up. The alternative order
   would suppress exactly the fresh crossovers that are worth seeing.

3. **Percentile convention.** "Percentile vs own trailing 504 days" is
   the mean percentile-of-score: the fraction of the window strictly
   below the current value plus half the ties, times 100. The window
   includes the current observation.

4. **Composite rank with missing horizons.** A ticker short of 252
   sessions cannot contribute a 12M return. Rather than drop it from the
   cross-section — which would break the "ranks are a permutation of
   1..N" invariant — the composite averages whatever horizons are
   available, and the row is flagged truncated so the page can mark it.
   Rows missing the two shortest horizons are excluded outright.

All price inputs are expected to be yfinance ``auto_adjust=True``
adjusted series, and OHLC must come from that same adjusted frame — one
consistent source, per spec §3.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Frozen parameters (spec §8). These are industry defaults and NONE of them
# has been validated on this universe. Changing any value here requires the
# multi-sample out-of-sample process — not a tweak because a sample day
# looked better. The freeze is the whole point of centralising them.
# --------------------------------------------------------------------------
MA_SHORT = 20
MA_MID = 50
MA_LONG = 200
SLOPE_LOOKBACK = 20
RSI_PERIOD = 14
ATR_PERIOD = 14
BBW_WINDOW = 20
BBW_SIGMA = 2.0
RV_WINDOW = 20
VOL_RATIO_WINDOW = 20
PCTL_WINDOW = 504
TRADING_DAYS_YEAR = 252
MOMENTUM_SKIP = 21           # the "1" in 12-1: skip the most recent month
FLAT_MA_TOLERANCE = 0.01     # |MA50/MA200 - 1| below this reads as flat
RANK_HORIZONS = (21, 63, 126, 252)   # 1M / 3M / 6M / 12M in trading days
MIN_RANK_HORIZONS = (21, 63)         # a row missing either is unrankable
MIN_PCTL_OBS = 252           # below this, percentiles are not published

TREND_STRONG_UP = "Strong up"
TREND_UP = "Up"
TREND_RANGE = "Range"
TREND_DOWN = "Down"
TREND_STRONG_DOWN = "Strong down"


# --------------------------------------------------------------------------
# Moving averages and trend state
# --------------------------------------------------------------------------
def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average. The spec specifies SMAs, not EMAs (§3.3)."""
    return close.rolling(window, min_periods=window).mean()


def slope_positive(ma: pd.Series, lookback: int = SLOPE_LOOKBACK) -> pd.Series:
    """True where the MA is above its own value ``lookback`` sessions ago.

    NaN-safe: the comparison is False wherever either endpoint is missing,
    so an MA that has not yet warmed up never reads as rising.
    """
    return (ma - ma.shift(lookback)) > 0


def trend_state(close: pd.Series) -> str | None:
    """Discrete trend badge for the latest bar (spec §3.3).

    Returns None when MA200 has not warmed up — the caller shows "—"
    rather than a badge computed on a partial window.
    """
    ma50 = sma(close, MA_MID)
    ma200 = sma(close, MA_LONG)
    up50 = slope_positive(ma50)
    up200 = slope_positive(ma200)

    c = close.iloc[-1]
    m50 = ma50.iloc[-1]
    m200 = ma200.iloc[-1]
    if not np.isfinite(c) or not np.isfinite(m50) or not np.isfinite(m200):
        return None

    rising = bool(up50.iloc[-1]) and bool(up200.iloc[-1])
    falling = not bool(up50.iloc[-1]) and not bool(up200.iloc[-1])

    # Precedence per convention 2 in the module docstring.
    if c > m50 > m200 and rising:
        return TREND_STRONG_UP
    if c < m50 < m200 and falling:
        return TREND_STRONG_DOWN
    flat_mas = abs(m50 / m200 - 1.0) < FLAT_MA_TOLERANCE
    between_mas = min(m50, m200) <= c <= max(m50, m200)
    if flat_mas or between_mas:
        return TREND_RANGE
    return TREND_UP if c > m200 else TREND_DOWN


def dev_from_ma(close: pd.Series, window: int = MA_LONG) -> float:
    """C / MA(window) - 1 for the latest bar (spec §3.14, "Dev 200D")."""
    ma = sma(close, window)
    if not np.isfinite(ma.iloc[-1]):
        return float("nan")
    return float(close.iloc[-1] / ma.iloc[-1] - 1.0)


# --------------------------------------------------------------------------
# Returns and momentum
# --------------------------------------------------------------------------
def total_return(close: pd.Series, lookback: int) -> float:
    """P(t)/P(t-lookback) - 1, or NaN when the history is too short."""
    if len(close) <= lookback:
        return float("nan")
    prior = close.iloc[-1 - lookback]
    latest = close.iloc[-1]
    if not np.isfinite(prior) or not np.isfinite(latest) or prior == 0:
        return float("nan")
    return float(latest / prior - 1.0)


def momentum_12_1(close: pd.Series) -> float:
    """P(t-21)/P(t-252) - 1 (spec §3.6) — 12-month return, last month skipped."""
    if len(close) <= TRADING_DAYS_YEAR:
        return float("nan")
    recent = close.iloc[-1 - MOMENTUM_SKIP]
    old = close.iloc[-1 - TRADING_DAYS_YEAR]
    if not np.isfinite(recent) or not np.isfinite(old) or old == 0:
        return float("nan")
    return float(recent / old - 1.0)


def vs_52w_high(close: pd.Series) -> float:
    """C(t) / max(C over trailing 252 sessions) - 1 (spec §3.7), close basis.

    Zero means a fresh closing high; the value is never positive because
    the current close is inside the window it is measured against.
    """
    window = close.iloc[-TRADING_DAYS_YEAR:].dropna()
    if window.empty:
        return float("nan")
    peak = window.max()
    if not np.isfinite(peak) or peak == 0:
        return float("nan")
    return float(close.iloc[-1] / peak - 1.0)


def relative_strength_1m(
    close: pd.Series, benchmark: pd.Series, lookback: int = MOMENTUM_SKIP
) -> float:
    """Log-return spread versus the benchmark over ``lookback`` sessions.

    ln(P_etf,t / P_etf,t-n) - ln(P_bm,t / P_bm,t-n), in return units
    (the page renders percentage points). Benchmark is SPY for every row
    per the spec's §3.13 owner decision; with a single benchmark this is
    the row's own 1M log return shifted by one constant, which is why
    ``run_scanner`` also emits the raw 1M return and the page picks.
    """
    if len(close) <= lookback or len(benchmark) <= lookback:
        return float("nan")
    etf = np.log(close.iloc[-1] / close.iloc[-1 - lookback])
    bm = np.log(benchmark.iloc[-1] / benchmark.iloc[-1 - lookback])
    if not np.isfinite(etf) or not np.isfinite(bm):
        return float("nan")
    return float(etf - bm)


# --------------------------------------------------------------------------
# Risk: realised vol, Bollinger bandwidth, ATR
# --------------------------------------------------------------------------
def realised_vol(close: pd.Series, window: int = RV_WINDOW) -> pd.Series:
    """Annualised standard deviation of daily log returns (spec §3.8)."""
    logret = np.log(close / close.shift(1))
    return logret.rolling(window, min_periods=window).std(ddof=1) * np.sqrt(
        TRADING_DAYS_YEAR
    )


def bollinger_bandwidth(
    close: pd.Series, window: int = BBW_WINDOW, sigma: float = BBW_SIGMA
) -> pd.Series:
    """(2 * sigma * sd_of_price_levels) / MA(window) — spec §3.9.

    Note this is the standard Bollinger definition on price *levels*, not
    on returns: band width is 2*sigma standard deviations either side of
    the mean, hence the 2*sigma multiplier, normalised by the mean.
    """
    sd = close.rolling(window, min_periods=window).std(ddof=1)
    mid = close.rolling(window, min_periods=window).mean()
    return (2.0 * sigma * sd) / mid


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """max(H-L, |H-C_prev|, |L-C_prev|). First bar is NaN (no prior close)."""
    prev_close = close.shift(1)
    a = high - low
    b = (high - prev_close).abs()
    c = (low - prev_close).abs()
    tr = pd.concat([a, b, c], axis=1).max(axis=1)
    tr.iloc[0] = np.nan
    return tr


def wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder's recursive smoothing, seeded by the first simple mean.

    seed = mean(first ``period`` valid observations); thereafter
    s(t) = (s(t-1) * (period - 1) + x(t)) / period. Used by both ATR and
    RSI so the two share one implementation and one seeding rule.
    """
    out = pd.Series(np.nan, index=values.index, dtype="float64")
    valid = values.dropna()
    if len(valid) < period:
        return out
    seed_idx = valid.index[period - 1]
    prev = float(valid.iloc[:period].mean())
    out.loc[seed_idx] = prev
    for idx, x in valid.loc[valid.index > seed_idx].items():
        prev = (prev * (period - 1) + float(x)) / period
        out.loc[idx] = prev
    return out


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD
) -> pd.Series:
    """Average True Range, Wilder-smoothed (spec §3.10)."""
    return wilder_smooth(true_range(high, low, close), period)


def atr_pct(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD
) -> float:
    """ATR / C for the latest bar, as a fraction (the page renders a %)."""
    a = atr(high, low, close, period)
    if not np.isfinite(a.iloc[-1]) or close.iloc[-1] == 0:
        return float("nan")
    return float(a.iloc[-1] / close.iloc[-1])


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder RSI (spec §4.6).

    Degenerate cases, ordered so the both-zero case cannot fall through to
    a directional answer: no movement at all reads 50 (neutral — a flat
    series carries no information and must not read as maximally
    oversold), gains-only reads 100, losses-only reads 0.
    ``naive_rsi_latest`` implements the same order.
    """
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = wilder_smooth(gains, period)
    avg_loss = wilder_smooth(losses, period)
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    flat = (avg_gain == 0.0) & (avg_loss == 0.0)
    return (
        out.mask(avg_loss == 0.0, 100.0)
        .mask(avg_gain == 0.0, 0.0)
        .mask(flat, 50.0)
    )


def volume_ratio(volume: pd.Series, window: int = VOL_RATIO_WINDOW) -> float:
    """V(t) / SMA(V, window) for the latest bar (spec §3.12)."""
    avg = volume.rolling(window, min_periods=window).mean()
    if not np.isfinite(avg.iloc[-1]) or avg.iloc[-1] == 0:
        return float("nan")
    return float(volume.iloc[-1] / avg.iloc[-1])


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Percentile:
    """A percentile reading plus the window it was computed on.

    ``truncated`` is what drives the "^" marker on the page: the value is
    publishable but its window is shorter than the frozen 504 sessions,
    so it is not comparable with a full-window neighbour.
    """

    value: float
    n_obs: int
    truncated: bool


def percentile_of_score(window: np.ndarray, value: float) -> float:
    """Mean percentile-of-score: strictly-below plus half the ties, x100."""
    arr = np.asarray(window, dtype="float64")
    arr = arr[np.isfinite(arr)]
    if arr.size == 0 or not np.isfinite(value):
        return float("nan")
    below = float((arr < value).sum())
    equal = float((arr == value).sum())
    return 100.0 * (below + 0.5 * equal) / arr.size


def percentile_of_latest(
    series: pd.Series, window: int = PCTL_WINDOW, min_obs: int = MIN_PCTL_OBS
) -> Percentile:
    """Percentile of the latest value within its own trailing window.

    Short histories use ``min(window, available)`` per spec §7, and are
    flagged truncated. Below ``min_obs`` the reading is withheld (NaN)
    rather than published on a window too short to mean anything.

    The value being ranked is the series' LAST element, not its last
    non-null one. Dropping to the most recent valid observation would
    publish a confident percentile for a bar that is not current — the
    stale-carry-forward failure this repo has already been bitten by
    twice. A missing current reading yields NaN.
    """
    if series.empty:
        return Percentile(float("nan"), 0, False)
    current = float(series.iloc[-1])
    if not np.isfinite(current):
        return Percentile(float("nan"), 0, False)
    tail = series.dropna().iloc[-window:]
    n = int(tail.size)
    if n < min_obs:
        return Percentile(float("nan"), n, True)
    return Percentile(
        percentile_of_score(tail.to_numpy(), current),
        n,
        n < window,
    )


# --------------------------------------------------------------------------
# Cross-sectional rank
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RankResult:
    """Composite ranks plus the diagnostics the guard layer asserts on."""

    ranks: pd.Series           # ticker -> 1..N, 1 = strongest
    scores: pd.Series          # ticker -> mean cross-sectional percentile
    truncated: pd.Series       # ticker -> True if any horizon was missing
    unrankable: list[str]      # tickers dropped for lacking short horizons


def rank_from_horizon_returns(returns: pd.DataFrame) -> RankResult:
    """Rank a cross-section from a ticker x horizon table of total returns.

    This is the ranking core, separated so it can be fed two ways. The
    scanner computes each ticker's horizon returns on its OWN trading
    calendar and calls this directly, because forcing 54 instruments
    across three venues onto one index would either fabricate bars or
    drop them. ``composite_rank`` is the wide-panel convenience wrapper
    over the same logic.

    Per-horizon returns become cross-sectional percentiles before
    averaging, so one wild horizon cannot dominate the composite the way
    raw-return averaging would let it.
    """
    pctiles = returns.rank(pct=True, na_option="keep")

    have_short = pctiles[list(MIN_RANK_HORIZONS)].notna().all(axis=1)
    unrankable = sorted(pctiles.index[~have_short].tolist())

    scores = pctiles.loc[have_short].mean(axis=1, skipna=True)
    truncated = pctiles.loc[have_short].isna().any(axis=1)

    ranks = scores.rank(ascending=False, method="first").astype("int64")
    return RankResult(
        ranks=ranks.sort_values(),
        scores=scores,
        truncated=truncated,
        unrankable=unrankable,
    )


def composite_rank(
    closes: pd.DataFrame,
    asof: int = -1,
    horizons: tuple[int, ...] = RANK_HORIZONS,
) -> RankResult:
    """Equal-weight multi-horizon momentum rank across the cross-section.

    ``closes`` is dates x tickers. ``asof`` is a positional index into
    that frame, so ranks can be recomputed as of an earlier bar — which
    is how "delta-R 20D" is derived. Ranks are never persisted: a rank
    history file would carry one bad day forward permanently, and
    recomputing from the same panel costs nothing.

    Per-horizon returns are converted to cross-sectional percentiles
    before averaging, so a single wild horizon cannot dominate the
    composite the way raw-return averaging would let it.
    """
    if asof < 0:
        asof = len(closes) + asof
    if asof < 0 or asof >= len(closes):
        raise IndexError(f"asof position {asof} outside panel of {len(closes)} rows")
    panel = closes.iloc[: asof + 1]

    per_horizon: dict[int, pd.Series] = {}
    for h in horizons:
        if len(panel) <= h:
            per_horizon[h] = pd.Series(np.nan, index=panel.columns, dtype="float64")
            continue
        prior = panel.iloc[-1 - h]
        latest = panel.iloc[-1]
        ret = latest / prior - 1.0
        ret[~np.isfinite(prior) | ~np.isfinite(latest) | (prior == 0)] = np.nan
        per_horizon[h] = ret

    return rank_from_horizon_returns(pd.DataFrame(per_horizon))


def rank_delta(
    closes: pd.DataFrame, lookback: int = SLOPE_LOOKBACK
) -> pd.Series:
    """Rank(t-lookback) - Rank(t): positive means the rank improved.

    Returns an empty series when the panel cannot support the lookback,
    which is what makes the spec's "delta-R shows — for the first 20
    trading days" acceptance test hold by construction rather than by a
    special case in the renderer.
    """
    if len(closes) <= lookback:
        return pd.Series(dtype="float64")
    now = composite_rank(closes, asof=-1).ranks
    then = composite_rank(closes, asof=-1 - lookback).ranks
    common = now.index.intersection(then.index)
    return (then.loc[common] - now.loc[common]).astype("float64")


# --------------------------------------------------------------------------
# Reference implementations — the per-build divergence guard
#
# Deliberately naive: plain Python loops, no pandas rolling, no vectorised
# shortcuts. run_scanner calls these on a rotating sample of tickers each
# build and compares against the vectorised path above. They exist to catch
# the failure mode that has no other symptom — a rolling-window or
# alignment regression that changes every number silently and breaks no
# test that shares the implementation being tested.
# --------------------------------------------------------------------------
def naive_sma_latest(values: list[float], window: int) -> float:
    """Mean of the last ``window`` values, by explicit accumulation."""
    if len(values) < window:
        return float("nan")
    total = 0.0
    for v in values[-window:]:
        total += v
    return total / window


def naive_rsi_latest(values: list[float], period: int = RSI_PERIOD) -> float:
    """Wilder RSI of the final value, by explicit iteration."""
    if len(values) < period + 1:
        return float("nan")
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(change if change > 0 else 0.0)
        losses.append(-change if change < 0 else 0.0)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def naive_atr_pct_latest(
    highs: list[float], lows: list[float], closes: list[float], period: int = ATR_PERIOD
) -> float:
    """Wilder ATR / last close, by explicit iteration."""
    if len(closes) < period + 1:
        return float("nan")
    trs: list[float] = []
    for i in range(1, len(closes)):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    if closes[-1] == 0:
        return float("nan")
    return a / closes[-1]
