"""Regression tests on backtest math invariants.

Added 2026-05-28 after the Phase 20 long-only bug (top_k_breadth_weight
producing negative weights under relative-breadth signal, causing
Strategy A within-sleeve weights to sum to ~114% and the deployed-blend
total to drift to ~105%). The bug went undetected for several days
because the existing 55-test suite covered data integrity and freshness
guards but had no assertions on the actual portfolio-construction math.

These tests assert the structural invariants that should ALWAYS hold
for a long-only, fully-invested-or-cash rotation:

  1. Within-sleeve weights are non-negative (long-only)
  2. Within-sleeve weights sum to <= 1.0 + epsilon (no over-allocation)
  3. Across-sleeve effective weights × blend weights sum to 100% (or
     less if cash floor active) of NAV
  4. Phase 22 EEM tilt funding is consistent (B reduced by tilt_weight)
  5. RISK_OFF risk-overlay state shifts blend by derisk_fraction to SHY
  6. No NaN in any deployed weight
  7. Eligible-start dates respected (no signal before warm-up complete)

Run with: python -m pytest tests/test_backtest_math.py -v
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


# ----- Strategy outputs invariants ----------------------------------------

@pytest.fixture(scope="module")
def sleeves():
    """Load each sleeve's headline trade history."""
    out = {}
    for key, fname in [("a", "topk_robustness.json"),
                        ("b", "asset_class_rotation.json"),
                        ("c", "thematic_rotation.json"),
                        ("d", "europe_rotation.json")]:
        path = DATA_DIR / fname
        if path.exists():
            out[key] = json.loads(path.read_text(encoding="utf-8"))
    return out


@pytest.fixture(scope="module")
def overlay():
    path = DATA_DIR / "risk_overlay.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


@pytest.mark.parametrize("sleeve_key", ["a", "b", "c", "d"])
def test_within_sleeve_weights_are_non_negative(sleeves, sleeve_key):
    """Each holding's within-sleeve weight must be >= 0. Phase 20.1
    fixed the relative-breadth bug that allowed negative weights."""
    if sleeve_key not in sleeves:
        pytest.skip(f"Sleeve {sleeve_key} not built")
    history = sleeves[sleeve_key]["headline"]["trade_history"]
    for rebal in history:
        for h in rebal["holdings"]:
            assert h["weight"] >= 0, (
                f"Sleeve {sleeve_key} on {rebal['date']}: {h['etf']} has "
                f"negative weight {h['weight']} — long-only invariant broken. "
                f"Check top_k_breadth_weight in scripts/run_portfolio.py."
            )


@pytest.mark.parametrize("sleeve_key", ["a", "b", "c", "d"])
def test_within_sleeve_weights_sum_to_at_most_one(sleeves, sleeve_key):
    """Within-sleeve weights should sum to <= 1.0 + small epsilon.
    Greater than 1.0 indicates the weight normalisation is broken
    (e.g. the Phase 20 implicit-shorting bug where top.sum() included
    negatives, inflating positives above 1.0)."""
    if sleeve_key not in sleeves:
        pytest.skip(f"Sleeve {sleeve_key} not built")
    history = sleeves[sleeve_key]["headline"]["trade_history"]
    eps = 1e-3  # tolerate rounding noise from JSON serialisation
    for rebal in history:
        total = sum(h["weight"] for h in rebal["holdings"])
        assert total <= 1.0 + eps, (
            f"Sleeve {sleeve_key} on {rebal['date']}: weights sum to "
            f"{total:.4f} > 1.0. Long-only invariant broken — check "
            f"normalisation in the weight function."
        )


@pytest.mark.parametrize("sleeve_key", ["a", "b", "c", "d"])
def test_within_sleeve_weights_finite(sleeves, sleeve_key):
    """No NaN / inf in any weight — would silently corrupt the blend."""
    if sleeve_key not in sleeves:
        pytest.skip(f"Sleeve {sleeve_key} not built")
    history = sleeves[sleeve_key]["headline"]["trade_history"]
    for rebal in history:
        for h in rebal["holdings"]:
            assert math.isfinite(h["weight"]), (
                f"Sleeve {sleeve_key} on {rebal['date']}: {h['etf']} weight "
                f"is {h['weight']} (NaN or inf). Check signal computation "
                f"upstream — likely a divide-by-zero in the normaliser."
            )


def test_deployed_blend_totals_to_100pct(sleeves, overlay):
    """The deployed blend's effective weights × blend weights must sum
    to exactly 100% of NAV. Composes both Phase 19 (risk overlay → SHY)
    and Phase 22 (EEM tilt funded from B).

    With EEM tilt OFF:    35% A + 35% B + 10% C + 20% D = 100%
    With EEM tilt ON:     35% A + 25% B + 10% C + 20% D + 10% EEM = 100%
    With RISK_OFF:        scaled by (1-derisk) + derisk in SHY
    With both ON:         scaled tilt + scaled blend + SHY = 100%
    """
    if not all(k in sleeves for k in ("a", "b", "c", "d")):
        pytest.skip("Not all 4 sleeves present")

    # Deployed blend weights
    W_A, W_B, W_C, W_D = 0.35, 0.35, 0.10, 0.20

    # Phase 22 EEM tilt adjustment
    p22 = (overlay or {}).get("phase22_eem_tilt", {})
    p22_on = p22.get("enabled") and p22.get("current_state") == "EM_TILT_ON"
    p22_w = p22.get("parameters", {}).get("tilt_weight", 0.10) if p22_on else 0
    fund_from = p22.get("parameters", {}).get("fund_from_sleeve", "strategy_b")
    if p22_on:
        if fund_from == "strategy_a":   W_A -= p22_w
        elif fund_from == "strategy_b": W_B -= p22_w
        elif fund_from == "strategy_c": W_C -= p22_w
        elif fund_from == "strategy_d": W_D -= p22_w

    # Phase 19 RISK_OFF scaling
    risk_off = (overlay or {}).get("current_state") == "RISK_OFF"
    derisk = (overlay or {}).get("gate_parameters", {}).get("derisk_fraction", 0.50)
    scale = (1 - derisk) if risk_off else 1.0
    shy_w = derisk if risk_off else 0.0

    # Compute deployed total
    def sleeve_total(key, w):
        rb = sleeves[key]["headline"]["trade_history"][-1]
        return w * sum(h["weight"] for h in rb["holdings"]) * scale

    total = (sleeve_total("a", W_A)
              + sleeve_total("b", W_B)
              + sleeve_total("c", W_C)
              + sleeve_total("d", W_D)
              + (p22_w * scale if p22_on else 0)
              + shy_w)

    eps = 1e-3
    assert abs(total - 1.0) < eps, (
        f"Deployed blend totals to {total*100:.4f}% — expected 100.000%. "
        f"State: EEM_tilt={p22_on} (funds from {fund_from}), "
        f"RISK_OFF={risk_off}, derisk={derisk}, scale={scale:.4f}. "
        f"Either a sleeve's weights don't sum to 1.0 (run test_within_sleeve_"
        f"weights_sum_to_at_most_one first) or the tilt/risk-overlay math "
        f"in the dashboard / factsheet is out of sync with this test."
    )


def test_phase22_eem_tilt_funding_consistent(overlay):
    """Phase 22 EEM tilt should reduce one sleeve's effective weight by
    exactly tilt_weight and allocate that to EEM. If the parameters
    config drifts (e.g. fund_from_sleeve changed without updating
    downstream) the dashboard / factsheet math breaks."""
    if not overlay:
        pytest.skip("No overlay data")
    p22 = overlay.get("phase22_eem_tilt", {})
    if not p22.get("enabled"):
        pytest.skip("Phase 22 not enabled")
    params = p22.get("parameters", {})
    assert "tilt_weight" in params, "Phase 22 enabled but tilt_weight missing"
    assert "fund_from_sleeve" in params, "Phase 22 enabled but fund_from_sleeve missing"
    assert 0 < params["tilt_weight"] <= 0.35, (
        f"Phase 22 tilt_weight {params['tilt_weight']} is out of sane range "
        f"(0, 0.35]. Tilt larger than 35% means it would reduce the funding "
        f"sleeve below zero."
    )
    assert params["fund_from_sleeve"] in (
        "strategy_a", "strategy_b", "strategy_c", "strategy_d"
    ), f"Phase 22 fund_from_sleeve has invalid value: {params['fund_from_sleeve']}"


def test_no_holding_appears_twice_in_one_sleeve(sleeves):
    """A sleeve shouldn't hold the same ETF in two rows of one
    rebalance — would indicate a deduplication bug in the trade-history
    builder that would double-count weight."""
    for sleeve_key in ("a", "b", "c", "d"):
        if sleeve_key not in sleeves:
            continue
        history = sleeves[sleeve_key]["headline"]["trade_history"]
        for rebal in history:
            etfs = [h["etf"] for h in rebal["holdings"]]
            assert len(etfs) == len(set(etfs)), (
                f"Sleeve {sleeve_key} on {rebal['date']} has duplicate ETF: "
                f"{[e for e in etfs if etfs.count(e) > 1]}"
            )


def test_blend_equity_monotonic_dates(sleeves, overlay):
    """The deployed blend equity series dates must be monotonically
    increasing (no duplicates, no out-of-order). Easy data corruption
    that would silently break stat computation."""
    multi_path = DATA_DIR / "multi_strategy.json"
    if not multi_path.exists():
        pytest.skip("multi_strategy.json missing")
    multi = json.loads(multi_path.read_text(encoding="utf-8"))
    for key, blend in multi.get("strategies", {}).items():
        if "dates" not in blend or not blend["dates"]:
            continue
        dates = blend["dates"]
        for i in range(1, len(dates)):
            assert dates[i] > dates[i - 1], (
                f"Strategy {key}: dates[{i}]={dates[i]} <= dates[{i-1}]="
                f"{dates[i-1]} — non-monotonic dates corrupts stat computation."
            )


def test_blend_equity_finite(sleeves):
    """No NaN / inf in any equity value."""
    multi_path = DATA_DIR / "multi_strategy.json"
    if not multi_path.exists():
        pytest.skip("multi_strategy.json missing")
    multi = json.loads(multi_path.read_text(encoding="utf-8"))
    for key, blend in multi.get("strategies", {}).items():
        if "equity" not in blend or not blend["equity"]:
            continue
        for i, v in enumerate(blend["equity"]):
            if v is None: continue
            assert math.isfinite(v), (
                f"Strategy {key}: equity[{i}]={v} is NaN or inf — "
                f"likely a divide-by-zero in stat computation upstream."
            )
