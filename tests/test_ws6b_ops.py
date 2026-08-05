"""Tests for the WS6b T1 ops classifiers.

Offline and synthetic throughout, same discipline as test_ws6b_friction: the
suite pins the classifier arithmetic, not any data vintage. Dates are built
with pandas (a date library — no manual day arithmetic), and the fixtures
deliberately span a month boundary and a year boundary.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from ws6b_ops import (  # noqa: E402
    capital_structure_events,
    death_events,
    held_mask,
    held_on_or_before,
    operator_time_model,
    rename_candidates,
    special_distribution_events,
    weekly_order_stats,
)

# Ten sessions spanning the 2023 -> 2024 year boundary (and hence a month
# boundary), pandas-generated.
DATES = pd.bdate_range("2023-12-25", periods=10)


def _book(weights: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(weights, index=DATES)


# --- held_mask --------------------------------------------------------------

def test_held_mask_clips_warmup_and_thresholds_at_zero():
    book = _book({"AAA": [0.0] * 5 + [0.1] * 5, "ETF": [0.2] * 10})
    held = held_mask(book, ["AAA"], start=DATES[2])
    assert list(held.columns) == ["AAA"]
    assert held.index[0] == DATES[2]
    assert not held["AAA"].iloc[0]
    assert held["AAA"].iloc[-1]


def test_held_on_or_before_sees_recent_sessions_only():
    book = _book({"AAA": [0.1] * 3 + [0.0] * 7})
    held = held_mask(book, ["AAA"], start=DATES[0])
    # Held two sessions before the query date -> seen within a 5-session look.
    assert held_on_or_before(held, "AAA", DATES[4], sessions=5)
    # A 2-session look from the last date no longer sees the early holding.
    assert not held_on_or_before(held, "AAA", DATES[-1], sessions=2)


# --- deaths -----------------------------------------------------------------

def _prices(cols: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(cols, index=DATES)


def test_death_counted_when_held_at_last_print():
    px = _prices({"DEAD-202401": [10.0] * 4 + [np.nan] * 6,
                  "LIVE": [10.0] * 10})
    book = _book({"DEAD-202401": [0.1] * 4 + [0.0] * 6, "LIVE": [0.1] * 10})
    held = held_mask(book, ["DEAD-202401", "LIVE"], DATES[0])
    ev = death_events(held, px)
    assert len(ev) == 1
    assert ev[0]["name"] == "DEAD-202401"
    assert ev[0]["held_at_death"]
    assert ev[0]["delist_suffix"] == "202401"
    assert ev[0]["last_price_date"] == str(DATES[3].date())


def test_death_not_counted_as_held_when_book_rotated_out_first():
    # Death mid-panel (clear of the clip guard); held only on the first
    # session, more than the lookback before the final print.
    px = _prices({"DEAD-202401": [10.0] * 5 + [np.nan] * 5})
    book = _book({"DEAD-202401": [0.1] * 1 + [0.0] * 9})
    held = held_mask(book, ["DEAD-202401"], DATES[0])
    ev = death_events(held, px, lookback_sessions=3)
    assert len(ev) == 1
    assert not ev[0]["held_at_death"]
    assert ev[0]["held_any_time_before"]


def test_series_reaching_panel_end_is_not_a_death():
    px = _prices({"LIVE": [10.0] * 10, "ALSO": [10.0] * 9 + [np.nan]})
    book = _book({"LIVE": [0.1] * 10, "ALSO": [0.1] * 10})
    held = held_mask(book, ["LIVE", "ALSO"], DATES[0])
    # ALSO ends one session early — inside the clip guard, not a death.
    assert death_events(held, px, clip_sessions=5) == []


# --- special distributions --------------------------------------------------

def test_special_distribution_on_held_day_counted_and_sized():
    cap = _prices({"AAA": [100.0] * 10})
    spec = cap.copy()
    # A 5% special reinvested on the 6th session: CAPITALSPECIAL return is 5%
    # while CAPITAL is flat.
    spec.loc[DATES[5]:, "AAA"] = 105.0
    book = _book({"AAA": [0.1] * 10})
    held = held_mask(book, ["AAA"], DATES[0])
    ev = special_distribution_events(cap, spec, held)
    assert len(ev) == 1
    assert ev[0]["date"] == str(DATES[5].date())
    assert ev[0]["distribution_frac_of_price"] == pytest.approx(0.05)
    assert ev[0]["spin_off_scale"]


def test_special_distribution_small_and_unheld_filtered():
    cap = _prices({"AAA": [100.0] * 10, "BBB": [100.0] * 10})
    spec = cap.copy()
    spec.loc[DATES[5]:, "AAA"] = 100.1     # 0.1% — below the noise floor
    spec.loc[DATES[5]:, "BBB"] = 105.0     # 5% but never held
    book = _book({"AAA": [0.1] * 10, "BBB": [0.0] * 10})
    held = held_mask(book, ["AAA", "BBB"], DATES[0])
    assert special_distribution_events(cap, spec, held) == []


def test_special_distribution_below_two_percent_not_spin_off_scale():
    cap = _prices({"AAA": [100.0] * 10})
    spec = cap.copy()
    spec.loc[DATES[5]:, "AAA"] = 101.0     # 1% cash special
    book = _book({"AAA": [0.1] * 10})
    held = held_mask(book, ["AAA"], DATES[0])
    ev = special_distribution_events(cap, spec, held)
    assert len(ev) == 1
    assert not ev[0]["spin_off_scale"]


# --- capital structure ------------------------------------------------------

def test_split_detected_with_factor_and_scale():
    # 10:1 split on the 6th session: unadjusted drops tenfold, CAPITAL is
    # continuous.
    unadj = _prices({"AAA": [100.0] * 5 + [10.0] * 5})
    cap = _prices({"AAA": [10.0] * 10})
    book = _book({"AAA": [0.1] * 10})
    held = held_mask(book, ["AAA"], DATES[0])
    ev = capital_structure_events(unadj, cap, held)
    assert len(ev) == 1
    assert ev[0]["date"] == str(DATES[5].date())
    assert ev[0]["factor"] == pytest.approx(10.0)
    assert ev[0]["split_scale"]


def test_flat_ratio_produces_no_capital_events():
    unadj = _prices({"AAA": list(np.linspace(100, 110, 10))})
    cap = unadj / 4.0
    book = _book({"AAA": [0.1] * 10})
    held = held_mask(book, ["AAA"], DATES[0])
    assert capital_structure_events(unadj, cap, held) == []


def test_unheld_split_not_counted():
    unadj = _prices({"AAA": [100.0] * 5 + [10.0] * 5})
    cap = _prices({"AAA": [10.0] * 10})
    book = _book({"AAA": [0.0] * 10})
    held = held_mask(book, ["AAA"], DATES[0])
    assert capital_structure_events(unadj, cap, held) == []


# --- renames ----------------------------------------------------------------

def test_rename_candidates_filter_to_ever_held_and_skip_identity():
    inst = {"OLD": "NEW", "GONE": "NEVERHELD"}
    known = {"SAME": "SAME", "FB": "META"}
    out = rename_candidates(inst, known, ever_held={"NEW", "META", "SAME"})
    pairs = {(r["snapshot_ticker"], r["instrument"]) for r in out}
    assert pairs == {("OLD", "NEW"), ("FB", "META")}


# --- order stats and the time model ----------------------------------------

def test_weekly_order_stats_separates_inception():
    trades = pd.DataFrame({
        "date": [DATES[0]] * 4 + [DATES[5]] * 2 + [DATES[9]] * 2,
        "name": list("ABCD") + list("AB") + list("CD"),
        "abs_delta": [0.25] * 4 + [0.05] * 2 + [0.10] * 2,
    })
    st = weekly_order_stats(trades)
    assert st["n_rebalances"] == 3
    assert st["inception_orders"] == 4
    assert st["inception_one_way_turnover"] == pytest.approx(1.0)
    assert st["orders_median"] == pytest.approx(2.0)
    assert st["turnover_weekly_median"] == pytest.approx(0.15)


def test_operator_time_model_monotone_and_budget_flags():
    lo = operator_time_model(10, 20, touch_events_per_year=1.0)
    hi = operator_time_model(50, 120, touch_events_per_year=1.0)
    assert hi["typical_week_min"] > lo["typical_week_min"]
    assert hi["p90_week_min"] > lo["p90_week_min"]
    assert lo["typical_within_budget"]
    # 120 staged orders costs 12 + 5 + 2 + 5 = 30 -> over once CA load lands.
    assert not hi["p90_within_budget"]
    assert "ESTIMATE" in lo["_estimate"]
