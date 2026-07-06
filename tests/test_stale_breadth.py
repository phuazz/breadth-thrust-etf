"""Freshness checks for MA200 breadth alignment (Phase 10.2).

Codex-flagged data-integrity bug: previously the breadth → trading-calendar
alignment used unbounded `.reindex(..., method='ffill').fillna(0)` which
would silently carry stale breadth forward forever if the source data
froze. The Phase 10.2 fix routes alignment through align_breadth_to_index
which caps forward-fill at MAX_BREADTH_STALE_DAYS (=7) and emits NaN past
the limit.

These tests guard against regression to the old behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from alignment import align_frame_to_index, align_series_to_index  # noqa: E402
from run_extended_history import compute_ma_breadth as compute_extended_ma_breadth  # noqa: E402
from run_ma200_sweep import align_breadth_to_index, compute_ma200_breadth  # noqa: E402
from run_robustness import family_d_alloc_series  # noqa: E402
from run_risk_overlay import (  # noqa: E402
    EEM_MAX_STALE_DAYS,
    GATE_MAX_STALE_DAYS,
    _build_eem_tilted_blend,
    _compute_states,
)
import mark_to_market_live as mtm  # noqa: E402


def test_align_breadth_drops_stale_forward_fill():
    """A 2-day breadth source aligned onto a 2-week calendar should
    forward-fill within the stale window then go NaN past it."""
    source_idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    breadth = pd.Series([0.4, 0.8], index=source_idx)
    target_idx = pd.date_range("2024-01-02", "2024-01-15", freq="B")

    aligned = align_breadth_to_index(breadth, target_idx, max_stale_days=3)

    # Within 3 days of last observation: ffilled
    assert aligned.loc["2024-01-05"] == 0.8
    # Past 3 days: NaN (the strategy will treat this as no signal)
    assert np.isnan(aligned.loc["2024-01-08"])


def test_align_breadth_handles_fully_empty_source():
    """If breadth has no real observations at all, alignment returns
    all-NaN — never silently zeros."""
    breadth = pd.Series([np.nan, np.nan], index=pd.to_datetime([
        "2024-01-02", "2024-01-03"
    ]))
    target_idx = pd.date_range("2024-01-02", "2024-01-10", freq="B")
    aligned = align_breadth_to_index(breadth, target_idx)
    assert aligned.isna().all()


def test_align_frame_to_index_caps_each_column_independently():
    """The frame helper applies the freshness cap per-column. A column
    that goes silent should drop to NaN past the window even when its
    siblings still have fresh observations."""
    source_idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    frame = pd.DataFrame({
        "composite_z": [0.2, 0.4],
        "ma_breadth": [0.6, np.nan],
    }, index=source_idx)
    target_idx = pd.date_range("2024-01-02", "2024-01-10", freq="B")

    aligned = align_frame_to_index(frame, target_idx, max_stale_days=3)

    # Within the freshness window: forward-filled.
    assert aligned.loc["2024-01-05", "composite_z"] == 0.4
    # Past the freshness window: NaN.
    assert np.isnan(aligned.loc["2024-01-08", "composite_z"])
    # ma_breadth never observed on 2024-01-03 (NaN), so the last good
    # observation is 2024-01-02 — 2024-01-05 is within 3 days of that.
    assert aligned.loc["2024-01-05", "ma_breadth"] == 0.6


def test_compute_ma200_breadth_emits_nan_when_no_constituent_is_valid():
    """When ALL constituents go missing (e.g., source data stops), the
    breadth computation returns NaN — does NOT silently propagate the
    last observed value via .ffill().fillna(0)."""
    idx = pd.date_range("2024-01-02", periods=12, freq="B")
    prices = pd.DataFrame({
        "A": [10, 11, 12, 13, 14, np.nan, np.nan,
              np.nan, np.nan, np.nan, np.nan, np.nan]
    }, index=idx)

    breadth = compute_ma200_breadth(prices, period=3)

    # Once prices stop, both_valid becomes False everywhere → n_valid=0
    # → breadth becomes NaN. Phase 10.2 removed the .ffill().fillna(0)
    # that previously masked this with a stale carry-forward.
    assert np.isnan(breadth.iloc[5])
    assert np.isnan(breadth.iloc[-1])


def test_stale_guard_uses_true_observation_date_not_synthetic_fill():
    """The age check must measure from the last REAL observation, not
    from whatever the synthetic ffill produced. If breadth has a real
    value on day 4 and then NaN gaps, day 6 should be marked stale
    relative to day 4 — not relative to a fake ffill of day 5."""
    idx = pd.date_range("2024-01-02", periods=12, freq="B")
    prices = pd.DataFrame({
        "A": [10, 11, 12, 13, 14, np.nan, np.nan,
              np.nan, np.nan, np.nan, np.nan, np.nan]
    }, index=idx)
    breadth = compute_ma200_breadth(prices, period=3)

    aligned = align_breadth_to_index(breadth, idx, max_stale_days=3)

    # Day 4 (a real observation) → preserved
    assert aligned.iloc[4] == breadth.iloc[4]
    # Day 6 (real obs was day 4, gap=2 calendar days) — should still
    # be within the 3-day stale window. Day 11 (gap=7 calendar days)
    # is past the window → NaN.
    assert np.isnan(aligned.iloc[-1])


def test_extended_history_ma_breadth_does_not_forward_fill_missing_prices():
    """compute_ma_breadth should return NaN once n_valid drops to zero,
    NOT carry the last good reading forward (the silent staleness bug
    that Phase 10.2 fixed for the SOXX path and Phase 14 generalises
    here)."""
    idx = pd.date_range("2024-01-02", periods=8, freq="B")
    prices = pd.DataFrame({
        "A": [10, 11, 12, 13, np.nan, np.nan, np.nan, np.nan],
    }, index=idx)

    breadth = compute_extended_ma_breadth(prices, period=3)

    # Once the 3-day MA is built (day 2), breadth is defined.
    assert pd.notna(breadth.iloc[2])
    # Once prices drop out, n_valid = 0 → breadth NaN. Must not be 0.
    assert np.isnan(breadth.iloc[-1])


def test_robustness_alloc_series_drops_stale_breadth():
    """family_d_alloc_series uses the freshness-capped alignment. A
    single breadth observation more than MAX_STALE_DAYS old should
    yield base allocation (0.0 here), not the on-allocation (1.0)."""
    dates = pd.date_range("2024-01-02", "2024-01-15", freq="B")
    breadth = pd.Series(1.0, index=pd.to_datetime(["2024-01-02"]))

    alloc = family_d_alloc_series(
        breadth, dates, L_pct=50, base=0.0, on=1.0, window_start=dates[0]
    )

    # Day after the observation → still within freshness window, alloc on.
    assert alloc.loc["2024-01-03"] == 1.0
    # 10 calendar days later → past the freshness window, alloc base.
    assert alloc.loc["2024-01-12"] == 0.0


# ===========================================================================
# WS3 maintenance patch — staleness caps at the four flagged ffill sites.
# Each test drives its feed stale and asserts the DOCUMENTED degradation
# state (hold-flat / hold-state / NaN), never a silently frozen value.
# ===========================================================================

_BLEND_W = {"strategy_a": 0.35, "strategy_b": 0.35,
            "strategy_c": 0.10, "strategy_d": 0.20}


def _fake_multi(common: pd.DatetimeIndex, paths: dict[str, np.ndarray]) -> dict:
    """Minimal multi_strategy.json-shaped dict for _build_eem_tilted_blend."""
    return {
        "strategies": {
            key: {
                "dates": [d.strftime("%Y-%m-%d") for d in common],
                "equity": [float(v) for v in path],
            }
            for key, path in paths.items()
        }
    }


def test_tilt_holds_flat_when_eem_feed_goes_stale(capsys):
    """Site 1 (run_risk_overlay.py Phase 22). When the EEM/SPY feed stops,
    the tilt must be HELD FLAT — the blend reverts to the baseline
    35/35/10/20 — instead of freezing the signal ON and marking the 10pp
    EEM sleeve at a fake 0% daily return. A WARN is emitted at a stale
    as-of."""
    common = pd.date_range("2024-01-01", periods=40, freq="B")
    t = np.arange(len(common))
    # Distinct nonzero sleeve paths so baseline != frozen-tilt return.
    paths = {
        "strategy_a": 100 * (1.002) ** t,
        "strategy_b": 100 * (1.001) ** t,   # nonzero B return is the tell
        "strategy_c": 100 * (1.0005) ** t,
        "strategy_d": 100 * (1.0015) ** t,
    }
    multi = _fake_multi(common, paths)
    # EEM price + tilt signal are fresh (ON) for the first 20 sessions, then
    # the cache stops — the tail is stale.
    fresh = common[:20]
    eem_prices = pd.Series(50 * (1.003) ** np.arange(len(fresh)), index=fresh)
    eem_signal = pd.Series(1.0, index=fresh)

    eq = _build_eem_tilted_blend(
        multi, eem_prices, eem_signal, common,
        fund_from_sleeve="strategy_b", tilt_weight=0.10, switch_cost_bps=0,
    )
    assert eq is not None

    rets = {k: pd.Series(paths[k], index=common).pct_change().fillna(0)
            for k in _BLEND_W}
    tilt_off_ret = sum(_BLEND_W[k] * rets[k] for k in _BLEND_W)   # baseline
    daily = eq.pct_change().fillna(0)

    stale = common[common > fresh[-1] + pd.Timedelta(days=EEM_MAX_STALE_DAYS)]
    assert len(stale) > 0
    # Hold-flat: on stale days the blend return equals the baseline exactly
    # (switch cost 0), NOT the frozen-tilt return (which under-weights B by
    # 10pp and books EEM at 0%).
    frozen_tilt_ret = tilt_off_ret - 0.10 * rets["strategy_b"]
    for d in stale:
        assert daily.loc[d] == pytest.approx(tilt_off_ret.loc[d])
    mid = stale[len(stale) // 2]
    assert not np.isclose(daily.loc[mid], frozen_tilt_ret.loc[mid])
    # WARN surfaced because the as-of (last common day) is stale.
    assert "stale" in capsys.readouterr().err.lower()


def test_gate_breadth_stale_degrades_to_nan_and_holds_state():
    """Site 2 (run_risk_overlay.py Phase 19 gate). A stalled CSP1 feed must
    degrade to NaN past the cap — so _compute_states holds the last regime
    state ("gate holds state on NaN") — rather than freezing a stale breadth
    NUMBER that the gate would keep acting on."""
    common = pd.date_range("2024-01-01", periods=40, freq="B")
    fresh = common[:15]
    # Comfortably RISK_ON, then the feed stops at 0.55.
    breadth = pd.Series([0.60] * 14 + [0.55], index=fresh)

    aligned = align_series_to_index(breadth, common,
                                    max_stale_days=GATE_MAX_STALE_DAYS)
    stale = common[common > fresh[-1] + pd.Timedelta(days=GATE_MAX_STALE_DAYS)]
    assert len(stale) > 0
    # NEW: capped alignment is NaN past the window — never a frozen number.
    assert aligned.loc[stale].isna().all()
    assert np.isnan(aligned.iloc[-1])
    # Contrast: the OLD uncapped ffill froze 0.55 forward.
    frozen = breadth.reindex(common, method="ffill")
    assert (frozen.loc[stale] == 0.55).all()
    # Degradation: the gate holds its last real RISK_ON state across the NaN
    # tail (no crash, no acting on absent breadth).
    states = _compute_states(aligned, 0.20, 0.50)
    assert states.loc[fresh[-1]] == 1.0
    assert (states.loc[stale] == 1.0).all()


def test_europe_fx_cap_degrades_stale_rate_to_nan():
    """Site 3 (run_europe_rotation.py Sleeve D EUR->USD). The site now uses
    the same one-liner as Sleeve C — align_series_to_index(..., 10). A
    stalled EURUSD feed degrades to NaN past the cap (D's USD prices drop
    out for that span) instead of freezing the last rate."""
    fx = pd.Series([1.10, 1.11, 1.12],
                   index=pd.to_datetime(["2024-01-02", "2024-01-03",
                                         "2024-01-04"]))
    cal = pd.date_range("2024-01-02", "2024-01-31", freq="B")
    capped = align_series_to_index(fx, cal, max_stale_days=10)

    # Within 10 cal days of the last obs (2024-01-04): forward-filled.
    assert capped.loc["2024-01-10"] == 1.12
    # Past the cap: NaN — never the frozen 1.12.
    assert np.isnan(capped.loc["2024-01-31"])
    # Contrast: the OLD reindex(ffill).bfill() froze 1.12 forward.
    old = fx.reindex(cal, method="ffill").bfill()
    assert old.loc["2024-01-31"] == 1.12


def test_live_fx_stale_drops_usd_price_to_nan(monkeypatch):
    """Site 4 (mark_to_market_live.py live path). When the FX feed stalls,
    the USD price must drop to NaN past the 10-day cap — engine parity —
    rather than silently freezing the last rate into the live mark."""
    idx = pd.date_range("2026-06-01", periods=20, freq="B")
    etf = pd.Series(100.0 + np.arange(len(idx)), index=idx)   # EXH1.DE, fresh
    fx = pd.Series([1.10, 1.10, 1.10], index=idx[:3])         # EURUSD stops
    fake_raw = pd.DataFrame({"EXH1.DE": etf, "EURUSD=X": fx})
    monkeypatch.setattr(mtm, "_yf_close_series",
                        lambda syms, start, end: fake_raw.copy())

    df = mtm._fetch_usd_prices({"EXH1": 1.0}, "2026-06-01", {})
    usd = df["EXH1"]
    last_fx = idx[2]                                          # 2026-06-03
    # Within 10 cal days: USD = EUR * 1.10.
    early = pd.Timestamp("2026-06-05")
    assert usd.loc[early] == pytest.approx(etf.loc[early] * 1.10)
    # Past the 10-day cap: NaN, not the frozen last rate.
    late = idx[idx > last_fx + pd.Timedelta(days=10)][0]
    assert np.isnan(usd.loc[late])
