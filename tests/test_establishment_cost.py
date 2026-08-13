"""The flat -> invested establishment trade must be charged.

2026-08-13, external code review, finding F2 — assessed a FALSE POSITIVE:
the reviewer read ``turnover = weight_panel.diff().abs().sum(axis=1)
.fillna(0)`` as skipping the cost of the initial establishment trade. It
does not: the engines reindex rebalance weights across the full price
panel and force pre-eligible rows to zero, so the flat -> invested
transition is a mid-series diff and IS charged. The ``fillna(0)`` only
masks the panel's literal first row, which always carries zero weight
because signals need MA warm-up history before the first rebalance can
fire (``prev_idx < 0`` skips the rebalance outright).

These tests pin that convention behaviourally, per engine, so a future
refactor of the turnover line cannot make the establishment trade free
without going red here. Prices are flat, so the only move in equity on
the establishment day is the cost charge itself: equity must step down
by exactly (sum of absolute weights) x cost.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_portfolio import run_portfolio, top_k_eq_weight  # noqa: E402
import run_asset_class_rotation as sleeve_b  # noqa: E402
import run_thematic_rotation as sleeve_c  # noqa: E402

COST = 0.01  # deliberately large so the charge dwarfs float noise

# pandas bdate_range; Python-style dates, months 1-indexed. Early January
# 2024 weekdays hold no NYSE holidays: starting on the 2nd skips New Year's
# Day, and MLK Day (2024-01-15) falls outside the 8-session window.
IDX = pd.bdate_range("2024-01-02", periods=8)


def _flat_closes(columns) -> pd.DataFrame:
    return pd.DataFrame({c: 100.0 for c in columns}, index=IDX)


def _constant_signal(values: dict) -> pd.DataFrame:
    return pd.DataFrame(values, index=IDX)


def _assert_establishment_charged(res: dict, label: str):
    exposure = res["weights"].abs().sum(axis=1)
    invested = exposure[exposure > 0]
    assert not invested.empty, f"{label}: engine never invested"
    first = invested.index[0]
    wsum = float(exposure.loc[first])

    equity = res["equity"]
    pos = equity.index.get_loc(first)
    prev = float(equity.iloc[pos - 1]) if pos > 0 else 1.0
    assert float(equity.iloc[pos]) == pytest.approx(prev * (1.0 - wsum * COST)), (
        f"{label}: establishment trade on {first.date()} was not charged "
        f"(sum |w| = {wsum:.3f}, cost = {COST}). Prices are flat, so equity "
        f"must step down by exactly the cost. If this fires after a refactor "
        f"of the turnover line, the diff()-based turnover has stopped seeing "
        f"the flat -> invested transition."
    )
    return first, wsum


def test_sleeve_a_engine_charges_establishment():
    closes = _flat_closes(["X", "Y", "Z"])
    breadths = _constant_signal({"X": 0.9, "Y": 0.6, "Z": 0.3})
    res = run_portfolio(
        closes, breadths, top_k_eq_weight(2),
        eligible_start=IDX[2], cost=COST, rebalance_freq="D",
    )
    _assert_establishment_charged(res, "run_portfolio (sleeve A/D engine)")


def test_sleeve_b_engine_charges_establishment():
    closes = _flat_closes(["X", "Y", "Z"])
    signal = _constant_signal({"X": 0.30, "Y": 0.20, "Z": 0.10})
    res = sleeve_b.run_rotation(
        closes, signal, sleeve_b.top_k_by_signal(2),
        eligible_start=IDX[2], rebalance_freq="D", cost=COST,
    )
    first, wsum = _assert_establishment_charged(res, "sleeve B run_rotation")
    assert float(res["turnover"].loc[first]) == pytest.approx(wsum)


def test_sleeve_c_engine_charges_establishment():
    closes = _flat_closes(["X", "Y", "Z"])
    signal = _constant_signal({"X": 0.30, "Y": 0.20, "Z": 0.10})
    res = sleeve_c.run_rotation(
        closes, signal, sleeve_c.top_k_equal_weight(2),
        eligible_start=IDX[2], rebalance_freq="D", cost=COST,
    )
    first, wsum = _assert_establishment_charged(res, "sleeve C run_rotation")
    assert float(res["turnover"].loc[first]) == pytest.approx(wsum)
