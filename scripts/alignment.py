"""Freshness-aware time-series alignment helpers.

Phase 14 generalisation of the `align_breadth_to_index` helper that
Phase 10.2 introduced in `run_ma200_sweep.py`. The original was specific
to one script; every other consumer of a breadth panel still did the
raw `series.reindex(index, method='ffill')` pattern, which silently
carries the last good signal forward forever when the source data
goes stale (the Phase 4 European breadth freeze).

The structural fix is to separate two responsibilities:

1. "Compute breadth" — can legitimately produce NaN gaps when the
   underlying constituent prices are missing.
2. "Align breadth to the trading calendar" — explicitly decides the
   freshness policy (here: forward-fill up to ``max_stale_days``
   calendar days, beyond which the value becomes NaN and downstream
   consumers decide what to do — usually treat as 'no signal').

Both helpers are pure: they do not log, mutate, or rely on global state.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# 7 calendar days = ~5 trading days, generous enough to bridge a weekend
# plus a public holiday but tight enough to surface real freshness bugs.
MAX_STALE_DAYS = 7


def align_series_to_index(
    series: pd.Series,
    index: pd.DatetimeIndex,
    max_stale_days: int = MAX_STALE_DAYS,
) -> pd.Series:
    """Forward-fill ``series`` onto ``index``, capped by observation age.

    For each target date in ``index``, returns the most recent real
    observation in ``series`` — unless the gap to that observation
    exceeds ``max_stale_days`` calendar days, in which case the value
    is NaN.
    """
    observed = series.dropna()
    if observed.empty:
        return pd.Series(np.nan, index=index, name=series.name)
    aligned = observed.reindex(index, method="ffill")
    last_observed = pd.Series(observed.index, index=observed.index).reindex(
        index, method="ffill"
    )
    age = index.to_series(index=index) - last_observed
    return aligned.mask(age > pd.Timedelta(days=max_stale_days))


def align_frame_to_index(
    frame: pd.DataFrame,
    index: pd.DatetimeIndex,
    max_stale_days: int = MAX_STALE_DAYS,
) -> pd.DataFrame:
    """Forward-fill each column in ``frame`` onto ``index``, capped by age.

    Each column carries its own freshness cap — a frame with sparse
    columns will not borrow freshness from its denser siblings.
    """
    return pd.DataFrame({
        col: align_series_to_index(frame[col], index, max_stale_days)
        for col in frame.columns
    }, index=index)
