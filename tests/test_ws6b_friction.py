"""Tests for the WS6b T1 friction / income model.

Offline and synthetic throughout: nothing here touches Norgate, yfinance or the
cached member panels, so the suite runs in CI and pins the model's arithmetic
rather than any particular data vintage.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import single_name_impl as sni  # noqa: E402
from ws6b_costs import income_costs, trading_costs  # noqa: E402
from ws6b_friction import (  # noqa: E402
    PARTIAL_5,
    BrokerSchedule,
    LineEconomics,
    Uncertain,
    restricted_to,
    trade_ledger,
)

FIXED = BrokerSchedule(
    name="test_fixed",
    per_share=Uncertain(0.005, "test"),
    min_order=Uncertain(1.00, "test"),
    max_pct_value=Uncertain(0.01, "test"),
    fractional_min_applies=True,
)


# --- adoption-set restriction ---------------------------------------------

def test_restricted_to_holds_non_adopted_lines_as_etfs():
    with restricted_to(PARTIAL_5) as broad:
        for L in sni.SINGLE_NAMED_LINES:
            assert (L in broad) is (L not in PARTIAL_5)
        # the genuine broad slices must survive the widening
        assert {"CSP1", "CNDX", "IDP6"} <= set(broad)


def test_restricted_to_restores_on_exception():
    original = sni.BROAD_SLICES
    with pytest.raises(RuntimeError):
        with restricted_to(("IUES",)):
            raise RuntimeError("boom")
    assert sni.BROAD_SLICES == original


def test_restricted_to_rejects_unknown_line():
    original = sni.BROAD_SLICES
    with pytest.raises(ValueError):
        with restricted_to(("NOTALINE",)):
            pass
    assert sni.BROAD_SLICES == original


# --- trade ledger ----------------------------------------------------------

def _panel(rows: dict[str, list[float]], dates) -> pd.DataFrame:
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates))


def test_trade_ledger_first_rebalance_is_a_full_trade():
    d = pd.DatetimeIndex(["2019-01-04", "2019-01-11"])
    panel = _panel({"AAA": [0.5, 0.5], "BBB": [0.5, 0.4]}, d)
    led = trade_ledger(panel, d)
    first = led[led["date"] == d[0]].set_index("name")["abs_delta"]
    assert first["AAA"] == pytest.approx(0.5)
    assert first["BBB"] == pytest.approx(0.5)


def test_trade_ledger_drops_zero_delta_rows():
    """A name whose weight does not move is NOT an order and must not attract
    the per-order minimum — the term that drives minimum viable NAV."""
    d = pd.DatetimeIndex(["2019-01-04", "2019-01-11"])
    panel = _panel({"AAA": [0.5, 0.5], "BBB": [0.5, 0.4]}, d)
    led = trade_ledger(panel, d)
    second = led[led["date"] == d[1]]
    assert set(second["name"]) == {"BBB"}
    assert second["abs_delta"].iloc[0] == pytest.approx(0.1)


def test_trade_ledger_month_boundary():
    """Edge case 1 of 2 (house rule): a rebalance across a month boundary."""
    d = pd.DatetimeIndex(["2021-01-29", "2021-02-05"])
    panel = _panel({"AAA": [0.30, 0.55]}, d)
    led = trade_ledger(panel, d)
    assert len(led) == 2
    assert led[led["date"] == d[1]]["abs_delta"].iloc[0] == pytest.approx(0.25)


def test_trade_ledger_year_boundary():
    """Edge case 2 of 2: a rebalance across a year boundary."""
    d = pd.DatetimeIndex(["2020-12-31", "2021-01-08"])
    panel = _panel({"AAA": [0.40, 0.10]}, d)
    led = trade_ledger(panel, d)
    assert led[led["date"] == d[1]]["abs_delta"].iloc[0] == pytest.approx(0.30)
    assert led[led["date"] == d[1]]["delta"].iloc[0] == pytest.approx(-0.30)


# --- commission ------------------------------------------------------------

def test_commission_per_order_minimum_binds_on_small_orders():
    c = FIXED.commission_usd(np.array([200.0]), np.array([100.0]))
    # 2 shares * $0.005 = $0.01, floored to the $1.00 per-order minimum
    assert c[0] == pytest.approx(1.00)


def test_commission_per_share_region():
    # 10,000 shares * $0.005 = $50; 1% cap on $1,000,000 is $10,000, not binding
    c = FIXED.commission_usd(np.array([1_000_000.0]), np.array([100.0]))
    assert c[0] == pytest.approx(50.0)


def test_commission_one_percent_cap_binds_on_penny_prices():
    # 10,000 shares at $0.50 = $5,000 notional; per-share would be $50, but the
    # 1% cap is $50 too — push the price lower so the cap genuinely binds
    c = FIXED.commission_usd(np.array([1_000.0]), np.array([0.10]))
    assert c[0] == pytest.approx(10.0)   # 1% of $1,000, not 10,000*$0.005=$50


def test_commission_zero_order_is_free():
    c = FIXED.commission_usd(np.array([0.0]), np.array([100.0]))
    assert c[0] == 0.0


def test_commission_survives_a_missing_price():
    """A price the panel could not resolve must not produce NaN or inf — the
    order falls back to the per-order minimum and is counted upstream."""
    c = FIXED.commission_usd(np.array([5_000.0]), np.array([0.0]))
    assert np.isfinite(c[0]) and c[0] == pytest.approx(1.00)


# --- income / fee algebra --------------------------------------------------

def _ucits(y: float, held_ter: float, proxy_ter: float) -> LineEconomics:
    return LineEconomics(
        line="TEST", held_instrument="UCITS Acc", proxy_instrument="US proxy",
        held_ter=Uncertain(held_ter, "t"), proxy_ter=Uncertain(proxy_ter, "t"),
        gross_yield=Uncertain(y, "t"),
        fund_level_wht=Uncertain(0.15, "t"),
        investor_wht_direct=Uncertain(0.30, "t"),
        investor_wht_on_e0=Uncertain(0.0, "t"),   # accumulating: no distribution
        us_situs=False)


def _us_fund(y: float, ter: float) -> LineEconomics:
    return LineEconomics(
        line="SOXX", held_instrument="US ETF", proxy_instrument="self",
        held_ter=Uncertain(ter, "t"), proxy_ter=Uncertain(ter, "t"),
        gross_yield=Uncertain(y, "t"),
        fund_level_wht=Uncertain(0.0, "t"),
        investor_wht_direct=Uncertain(0.30, "t"),
        investor_wht_on_e0=Uncertain(0.30, "t"),
        us_situs=True)


def test_ucits_drag_is_half_the_withholding_less_the_fee_differential():
    """Replication swaps a 15% fund-level withholding for a 30% investor-level
    one, and the fee it saves is measured against the PROXY's TER, not the
    UCITS line's."""
    econ = _ucits(y=0.04, held_ter=0.0015, proxy_ter=0.0008)
    assert econ.income_fee_drag() == pytest.approx(0.15 * 0.04 - (0.0015 - 0.0008))


def test_us_listed_fund_drag_is_only_seventy_percent_of_its_ter():
    """The non-obvious one: a US fund's TER reduces the distribution the
    investor is taxed on, so replicating it recovers only (1 - 30%) of the
    headline fee."""
    econ = _us_fund(y=0.013, ter=0.0035)
    assert econ.income_fee_drag() == pytest.approx(0.30 * 0.0035)


def test_us_listed_fund_drag_is_capped_when_the_fee_exceeds_the_income():
    """A fund whose TER exceeds its post-withholding yield distributes nothing;
    the offset must not go negative and start paying the investor."""
    econ = _us_fund(y=0.001, ter=0.0035)
    assert econ.income_fee_drag() == pytest.approx(0.30 * 0.001)


# --- income_costs daily accounting ----------------------------------------

def test_income_costs_ter_offset_accrues_on_non_dividend_days():
    """Regression: deriving the distributed fraction daily and clipping it at
    zero zeroed the TER offset on every day without a dividend, understating a
    US-listed line's drag by roughly the ratio of dividend days to all days.
    The drag must equal 0.30 * weight * TER regardless of dividend timing."""
    idx = pd.date_range("2020-01-01", periods=252, freq="B")
    book = pd.DataFrame({"AAA": np.full(len(idx), 0.5)}, index=idx)
    # All of the year's 2.0% yield lands on four days, not spread evenly.
    div = pd.DataFrame({"AAA": np.zeros(len(idx))}, index=idx)
    div.iloc[[10, 70, 130, 190]] = 0.02 / 4
    econ = _us_fund(y=0.02, ter=0.0035)
    total, per_line = income_costs({"SOXX": book}, div, {"SOXX": econ}, idx)
    assert per_line["SOXX"]["annual_total_drag"] == pytest.approx(
        0.30 * 0.5 * 0.0035, rel=1e-6)


def test_income_costs_matches_the_closed_form_for_a_ucits_line():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")
    book = pd.DataFrame({"AAA": np.full(len(idx), 0.4)}, index=idx)
    div = pd.DataFrame({"AAA": np.zeros(len(idx))}, index=idx)
    div.iloc[[30, 120]] = 0.03 / 2
    econ = _ucits(y=0.03, held_ter=0.0015, proxy_ter=0.0008)
    _total, per_line = income_costs({"L": book}, div, {"L": econ}, idx)
    expected = 0.4 * (0.15 * 0.03 - (0.0015 - 0.0008))
    assert per_line["L"]["annual_total_drag"] == pytest.approx(expected, rel=1e-6)


def test_income_costs_scales_with_time_held():
    """A line held half the time bears half the annual drag.

    Both legs must scale, and they scale for different reasons: the TER accrues
    on held weight, while the withholding leg is only incurred on dividends
    that actually land while the line is held. The four dividends are spread
    evenly so that exactly half fall inside the holding window.
    """
    idx = pd.date_range("2020-01-01", periods=252, freq="B")
    w = np.where(np.arange(len(idx)) < 126, 0.4, 0.0)
    book = pd.DataFrame({"AAA": w}, index=idx)
    div = pd.DataFrame({"AAA": np.zeros(len(idx))}, index=idx)
    div.iloc[[30, 90, 150, 210]] = 0.03 / 4    # two held, two not
    econ = _ucits(y=0.03, held_ter=0.0015, proxy_ter=0.0008)
    _t, per_line = income_costs({"L": book}, div, {"L": econ}, idx)
    full = 0.4 * (0.15 * 0.03 - (0.0015 - 0.0008))
    assert per_line["L"]["annual_total_drag"] == pytest.approx(full / 2, rel=1e-6)


def test_income_costs_withholding_leg_ignores_dividends_while_flat():
    """A dividend landing in a week the line is not held costs nothing — the
    guard against charging withholding on income the book never received."""
    idx = pd.date_range("2020-01-01", periods=252, freq="B")
    book = pd.DataFrame({"AAA": np.where(np.arange(len(idx)) < 126, 0.4, 0.0)},
                        index=idx)
    div = pd.DataFrame({"AAA": np.zeros(len(idx))}, index=idx)
    div.iloc[[200]] = 0.03                      # lands after the line goes flat
    econ = _ucits(y=0.03, held_ter=0.0015, proxy_ter=0.0008)
    _t, per_line = income_costs({"L": book}, div, {"L": econ}, idx)
    assert per_line["L"]["annual_wht_drag"] == pytest.approx(0.0, abs=1e-12)


# --- trading-cost calendar -------------------------------------------------

def _one_order_ledger(date: str) -> pd.DataFrame:
    return pd.DataFrame({"date": [pd.Timestamp(date)], "name": ["AAA"],
                         "delta": [0.10], "abs_delta": [0.10],
                         "weight_after": [0.10]})


def test_trading_costs_returns_the_full_calendar_not_just_trade_days():
    """Regression: the cost series was indexed on rebalance dates only. An
    annualised mean over 389 weekly dates treated as 252 trading days inflated
    every drag figure roughly fivefold."""
    cal = pd.date_range("2020-01-01", periods=252, freq="B")
    led = _one_order_ledger("2020-01-02")
    px = pd.DataFrame({"AAA": np.full(len(cal), 100.0)}, index=cal)
    daily, _ = trading_costs(led, px, FIXED, pd.Series({"__default__": 1.5}),
                             nav=1_000_000, calendar=cal)
    assert daily.index.equals(cal)
    trade_day = pd.Timestamp("2020-01-02")
    assert daily.loc[trade_day] > 0
    assert (daily.drop(index=trade_day) == 0).all()


def test_trading_costs_series_adds_to_a_daily_series_without_creating_nan():
    """Regression: adding a rebalance-indexed cost series to a daily-indexed
    income series aligned to the union and produced NaN on every non-rebalance
    day, which a downstream fillna(0) then converted to 'no cost' — silently
    deleting most of the income leg."""
    cal = pd.date_range("2020-01-01", periods=252, freq="B")
    led = _one_order_ledger("2020-01-02")
    px = pd.DataFrame({"AAA": np.full(len(cal), 100.0)}, index=cal)
    daily, _ = trading_costs(led, px, FIXED, pd.Series({"__default__": 1.5}),
                             nav=1_000_000, calendar=cal)
    income = pd.Series(1e-5, index=cal)
    combined = daily + income
    assert not combined.isna().any()
    assert combined.sum() == pytest.approx(daily.sum() + income.sum())


def test_trading_costs_annualisation_matches_the_elapsed_window():
    """A known total cost must annualise to total/years, whatever the mix of
    trade and non-trade days."""
    cal = pd.date_range("2020-01-01", periods=504, freq="B")   # ~2 years
    led = pd.concat([_one_order_ledger(str(d.date()))
                     for d in (cal[0], cal[100], cal[300])], ignore_index=True)
    px = pd.DataFrame({"AAA": np.full(len(cal), 100.0)}, index=cal)
    daily, detail = trading_costs(led, px, FIXED,
                                  pd.Series({"__default__": 0.0}),
                                  nav=1_000_000, calendar=cal)
    annual = daily.mean() * 252
    expected = detail["commission_usd_total"] / 1_000_000 / (len(cal) / 252)
    assert annual == pytest.approx(expected, rel=1e-9)


def test_trading_costs_counts_names_falling_back_to_the_default_spread():
    cal = pd.date_range("2020-01-01", periods=10, freq="B")
    led = _one_order_ledger("2020-01-02")
    px = pd.DataFrame({"AAA": np.full(len(cal), 100.0)}, index=cal)
    _d, detail = trading_costs(led, px, FIXED, pd.Series({"__default__": 2.0}),
                               nav=1_000_000, calendar=cal)
    assert detail["n_orders_default_spread"] == 1


# --- provenance ------------------------------------------------------------

def test_uncertain_carries_its_source_and_flag():
    u = Uncertain(0.0015, "BlackRock product 280503", uncertain=False)
    assert float(u) == 0.0015
    assert u.describe()["source"] == "BlackRock product 280503"
    assert u.describe()["uncertain"] is False


def test_uncertain_behaves_as_a_float_in_arithmetic():
    u = Uncertain(0.15, "treaty")
    assert u * 0.04 == pytest.approx(0.006)


def test_shipped_params_flag_every_unverified_figure():
    """Nothing may enter the model from a 'PENDING' or unsourced field without
    also being marked uncertain — the guard against a placeholder silently
    becoming a result."""
    import json
    raw = json.loads((PROJECT_ROOT / "data" / "ws6b_params.json")
                     .read_text(encoding="utf-8"))

    def _walk(node):
        if isinstance(node, dict):
            if "value" in node and "source" in node:
                src = node["source"]
                if "PENDING" in src or not src.strip():
                    assert node.get("uncertain") is True, (
                        f"unsourced figure not marked uncertain: {node}")
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(raw)
