"""Tests for scripts/overlay_state.py — point-in-time overlay state.

CLAUDE.md date rules: at least two edge-case tests for any date logic.
Covered here: a year-boundary flip, a month-boundary flip, and the
flip-day-inclusive convention (an event dated D takes effect ON D) that
the monitor repo's ``build_weight_history`` also uses. A live-data test
pins the helper to the real 2025-04-07 EM_TILT_ON event that the audit
replayed (prior B rebalance 2025-04-04 must price at 0.35, the
2025-04-11 rebalance at 0.25).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from overlay_state import (  # noqa: E402
    derisk_active_on,
    sleeve_nav_weights,
    state_active_on,
    tilt_active_on,
)

TILT_EVENTS = [
    {"date": "2023-01-20", "direction": "EM_TILT_ON"},
    {"date": "2023-04-12", "direction": "EM_TILT_OFF"},
    {"date": "2025-04-07", "direction": "EM_TILT_ON"},
]
GATE_EVENTS = [
    {"date": "2025-12-18", "direction": "RISK_OFF"},
    {"date": "2026-01-21", "direction": "RISK_ON"},
]


def _overlay(tilt_events=TILT_EVENTS, gate_events=GATE_EVENTS):
    return {
        "events": gate_events,
        "gate_parameters": {"derisk_fraction": 0.50},
        "phase22_eem_tilt": {
            "enabled": True,
            "events": tilt_events,
            "parameters": {"tilt_weight": 0.10},
        },
    }


def test_flip_day_is_inclusive():
    """An event dated D takes effect ON D — the day before it does not."""
    assert not tilt_active_on(_overlay(), "2025-04-04")
    assert not tilt_active_on(_overlay(), "2025-04-06")
    assert tilt_active_on(_overlay(), "2025-04-07")
    assert tilt_active_on(_overlay(), "2025-04-11")


def test_month_boundary_flip():
    """OFF event 2023-04-12: the tilt is ON through 2023-03-31 (month
    boundary inside the ON span) and OFF from 2023-04-12."""
    assert tilt_active_on(_overlay(), "2023-03-31")
    assert tilt_active_on(_overlay(), "2023-04-11")
    assert not tilt_active_on(_overlay(), "2023-04-12")


def test_year_boundary_derisk_span():
    """RISK_OFF 2025-12-18 -> RISK_ON 2026-01-21 spans the year end."""
    assert not derisk_active_on(_overlay(), "2025-12-17")
    assert derisk_active_on(_overlay(), "2025-12-31")
    assert derisk_active_on(_overlay(), "2026-01-02")   # new year, still off
    assert derisk_active_on(_overlay(), "2026-01-20")
    assert not derisk_active_on(_overlay(), "2026-01-21")


def test_no_events_means_inactive():
    assert not state_active_on([], "2026-01-01", "EM_TILT_ON")
    assert not state_active_on(None, "2026-01-01", "EM_TILT_ON")
    assert not tilt_active_on({}, "2026-01-01")
    assert not tilt_active_on(None, "2026-01-01")


def test_disabled_tilt_never_active():
    ov = _overlay()
    ov["phase22_eem_tilt"]["enabled"] = False
    assert not tilt_active_on(ov, "2025-04-11")


def test_unsorted_events_are_handled():
    ov = _overlay(tilt_events=list(reversed(TILT_EVENTS)))
    assert tilt_active_on(ov, "2025-04-07")
    assert not tilt_active_on(ov, "2025-04-04")


def test_sleeve_nav_weights_tilt_only():
    w = sleeve_nav_weights(_overlay(), "2025-04-11")   # tilt ON, gate RISK_ON
    assert w["a"] == pytest.approx(0.35)
    assert w["b"] == pytest.approx(0.25)
    assert w["c"] == pytest.approx(0.10)
    assert w["d"] == pytest.approx(0.20)
    assert w["tilt_on"] and not w["derisk_on"]
    assert w["tilt_nav"] == pytest.approx(0.10)
    assert w["shy_overlay"] == 0.0
    # NAV closes: sleeves + tilt = 100%
    assert w["a"] + w["b"] + w["c"] + w["d"] + w["tilt_nav"] == pytest.approx(1.0)


def test_sleeve_nav_weights_before_tilt():
    w = sleeve_nav_weights(_overlay(), "2025-04-04")
    assert w["b"] == pytest.approx(0.35)
    assert w["tilt_nav"] == 0.0


def test_sleeve_nav_weights_derisk_scales_everything():
    """RISK_OFF week inside the 2025-12-18 span, tilt also ON: every
    equity leg is halved and 50% sits in the SHY overlay."""
    w = sleeve_nav_weights(_overlay(), "2026-01-02")
    assert w["derisk_on"] and w["tilt_on"]
    assert w["equity_scaler"] == pytest.approx(0.50)
    assert w["a"] == pytest.approx(0.175)
    assert w["b"] == pytest.approx(0.125)
    assert w["tilt_nav"] == pytest.approx(0.05)
    assert w["shy_overlay"] == pytest.approx(0.50)
    total = (w["a"] + w["b"] + w["c"] + w["d"] + w["tilt_nav"]
             + w["shy_overlay"])
    assert total == pytest.approx(1.0)


def test_against_live_overlay_flip_week():
    """Pin to the real 2025-04-07 EM_TILT_ON event: the 2025-04-04 B
    rebalance prices at 0.35, the 2025-04-11 one at 0.25 (the audit's
    replay showed the current-state shortcut misstating GLD's prior
    weight by 5.7pp NAV on exactly this pair). Soft-skip on a minimal
    checkout."""
    p = Path(__file__).resolve().parent.parent / "data" / "risk_overlay.json"
    if not p.exists():
        pytest.skip("risk_overlay.json not present in this checkout")
    ov = json.loads(p.read_text(encoding="utf-8"))
    events = (ov.get("phase22_eem_tilt") or {}).get("events") or []
    if not any(e.get("date") == "2025-04-07" for e in events):
        pytest.skip("2025-04-07 tilt event not in this overlay vintage")
    before = sleeve_nav_weights(ov, "2025-04-04")
    after = sleeve_nav_weights(ov, "2025-04-11")
    assert not before["tilt_on"] and after["tilt_on"]
    # Isolate the tilt leg from the gate: B equals A before the flip and
    # is one (scaled) tilt weight lighter after it. On this real pair the
    # 2025-04-04 RISK_OFF flip is ALSO active, so the raw multipliers are
    # 0.175 -> 0.125 — the gate and the tilt compound, which is exactly
    # why per-date weights (not current-state shortcuts) are required.
    assert before["b"] == pytest.approx(before["a"])
    assert after["b"] == pytest.approx(
        after["a"] - 0.10 * after["equity_scaler"])
