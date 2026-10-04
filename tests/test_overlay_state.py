"""Tests for scripts/overlay_state.py — point-in-time overlay state.

CLAUDE.md date rules: at least two edge-case tests for any date logic.
Covered here: a year-boundary flip, a month-boundary flip, and the
flip-day-inclusive convention (an event dated D takes effect ON D) that
the monitor repo's ``build_weight_history`` also uses. A live-data test
pins the helper to the real 2025-04-07 EM_TILT_ON event that the audit
replayed (prior B rebalance 2025-04-04 must price at 0.35, the
2025-04-11 rebalance at 0.25).

S2-2 (2026-10-04): the gate's state before its first logged event is the
payload's ``initial_state``, bounded by ``history_start`` (inactive before
the first published close); the counts the engine publishes are
reproduced from that seed and the events list, on the synthetic calendar
below and on the committed payload, and the engine's published fields are
checked without writing the real payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from overlay_state import (  # noqa: E402
    assert_initial_state_reproduces,
    derisk_active_on,
    derive_daily_states,
    gate_counts,
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
# The live history's opening: RISK_OFF on the blend's first close
# (Wednesday 2018-10-31), re-risked on Wednesday 2018-11-28.
INCEPTION_GATE_EVENTS = [
    {"date": "2018-11-28", "direction": "RISK_ON"},
    {"date": "2018-12-17", "direction": "RISK_OFF"},
    {"date": "2019-01-17", "direction": "RISK_ON"},
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


# ----- S2-2: the state before the first logged event -----------------------

def test_initial_state_seeds_the_gate_before_its_first_event():
    """A payload that opens RISK_OFF reads de-risked on every date before
    its first event (the month boundary 2018-10-31 -> 2018-11-01 sits
    inside the span) and re-risked from the event day itself."""
    ov = _overlay(gate_events=INCEPTION_GATE_EVENTS)
    ov["initial_state"] = "RISK_OFF"
    ov["history_start"] = "2018-10-31"
    assert derisk_active_on(ov, "2018-10-31")
    assert derisk_active_on(ov, "2018-11-01")
    assert derisk_active_on(ov, "2018-11-27")
    assert not derisk_active_on(ov, "2018-11-28")
    assert derisk_active_on(ov, "2018-12-17")
    w = sleeve_nav_weights(ov, "2018-11-09")
    assert w["derisk_on"]
    assert w["equity_scaler"] == pytest.approx(0.50)
    assert w["shy_overlay"] == pytest.approx(0.50)


def test_payload_without_initial_state_keeps_the_inactive_reading():
    """Archived payloads predate the field: unchanged behaviour. The seed
    is symmetric across overlays, and the tilt's engine series opens OFF,
    so publishing EM_TILT_OFF changes nothing."""
    ov = _overlay(gate_events=INCEPTION_GATE_EVENTS)
    assert not derisk_active_on(ov, "2018-11-27")
    assert not derisk_active_on(ov, "2018-11-28")
    ov["phase22_eem_tilt"]["initial_state"] = "EM_TILT_OFF"
    assert not tilt_active_on(ov, "2023-01-19")
    ov["phase22_eem_tilt"]["initial_state"] = "EM_TILT_ON"
    assert tilt_active_on(ov, "2023-01-19")
    assert not tilt_active_on(ov, "2023-04-12")


def test_gate_counts_follow_the_engine_convention():
    """Five closes opening RISK_OFF, re-risked on the third, de-risked
    again on the fifth. The engine's lagged book holds each close's state
    on the next close and never holds the fifth close's: two switches
    (the inception de-risk, held from the second close, and the re-risk)
    and two closes held de-risked; the fifth close's de-risk is not yet
    held. The unlagged reading gives three days (2, 3); dropping the
    inception term gives one switch (1, 2). Either mutant fails here."""
    dates = ["2018-10-31", "2018-11-01", "2018-11-02", "2018-11-05",
             "2018-11-06"]
    events = [{"date": "2018-11-02", "direction": "RISK_ON"},
              {"date": "2018-11-06", "direction": "RISK_OFF"}]
    derived = derive_daily_states(True, events, dates, "RISK_OFF")
    assert derived == [True, True, False, False, True]
    assert gate_counts(derived) == (2, 2)
    # Opening RISK_ON instead: the inception term and the first two
    # de-risked closes go, leaving the one de-risk that is never held.
    opened_on = derive_daily_states(False, events, dates, "RISK_OFF")
    assert opened_on == [False, False, False, False, True]
    assert gate_counts(opened_on) == (0, 0)
    assert gate_counts([]) == (0, 0)
    with pytest.raises(ValueError):
        derive_daily_states(True, events, list(reversed(dates)), "RISK_OFF")


def test_publish_guard_refuses_a_seed_that_disagrees_with_the_engine():
    """The engine's series opens RISK_OFF; a payload that says RISK_ON (or
    nothing) is refused, as is a wrong headline count."""
    dates = ["2018-10-31", "2018-11-01", "2018-11-02"]
    engine = [True, True, False]
    events = [{"date": "2018-11-02", "direction": "RISK_ON"}]
    good = {"initial_state": "RISK_OFF", "events": events}
    assert_initial_state_reproduces(good, "RISK_OFF", dates, engine,
                                    n_switches=1, days_active=2)
    with pytest.raises(ValueError, match="2018-10-31"):
        assert_initial_state_reproduces({"events": events}, "RISK_OFF",
                                        dates, engine)
    with pytest.raises(ValueError, match="publish refused"):
        assert_initial_state_reproduces(good, "RISK_OFF", dates, engine,
                                        n_switches=0, days_active=2)
    with pytest.raises(ValueError, match="against an engine series"):
        assert_initial_state_reproduces(good, "RISK_OFF", dates, engine[:2])


def test_live_payload_initial_state_reproduces_published_counts():
    """The committed payload, seeded from its own ``initial_state`` and
    walking its own events over the gated variant's calendar, reproduces
    ``n_switches``, ``days_risk_off`` and ``pct_days_risk_off`` exactly;
    the inactive seed does not whenever the history opens de-risked (236
    days and 18 switches against 255 and 20 on the 2026-10-03 vintage).
    Soft-skip on a minimal checkout or a payload predating the field."""
    p = Path(__file__).resolve().parent.parent / "data" / "risk_overlay.json"
    if not p.exists():
        pytest.skip("risk_overlay.json not present in this checkout")
    ov = json.loads(p.read_text(encoding="utf-8"))
    if "initial_state" not in ov:
        pytest.skip("payload predates initial_state (S2-2, 2026-10-04)")
    key = ov["underlying_blend_key"] + "_gated"
    dates = ov["gated_variants"][key]["dates"]
    seeded = derive_daily_states(ov["initial_state"] == "RISK_OFF",
                                 ov["events"], dates, "RISK_OFF")
    n_sw, days = gate_counts(seeded)
    assert (n_sw, days) == (ov["n_switches"], ov["days_risk_off"])
    assert days / len(dates) * 100 == pytest.approx(
        ov["pct_days_risk_off"], abs=0.005)
    if ov["initial_state"] == "RISK_OFF":
        unseeded = derive_daily_states(False, ov["events"], dates, "RISK_OFF")
        assert gate_counts(unseeded) != (n_sw, days)


# ----- S2-2 hardening (Codex review, 2026-10-04) -----------------------------

def test_history_start_bounds_the_seed():
    """Before the first published close neither overlay existed: a seeded
    payload reads inactive there (the boundary 2018-10-30 -> 2018-10-31),
    seeded from that close to the day before its first event, and from the
    event log after. The tilt is bounded by the same top-level start."""
    ov = _overlay(gate_events=INCEPTION_GATE_EVENTS)
    ov["history_start"] = "2018-10-31"
    ov["initial_state"] = "RISK_OFF"
    ov["phase22_eem_tilt"]["initial_state"] = "EM_TILT_ON"
    assert not derisk_active_on(ov, "2018-10-30")
    assert derisk_active_on(ov, "2018-10-31")
    assert derisk_active_on(ov, "2018-11-27")
    assert not derisk_active_on(ov, "2018-11-28")
    assert not tilt_active_on(ov, "2018-10-30")
    assert tilt_active_on(ov, "2018-10-31")
    assert tilt_active_on(ov, "2023-01-19")
    assert not tilt_active_on(ov, "2023-04-12")
    before = sleeve_nav_weights(ov, "2018-10-30")
    assert not before["derisk_on"] and not before["tilt_on"]
    assert before["equity_scaler"] == pytest.approx(1.0)


def test_without_history_start_the_seed_is_unbounded():
    """A payload carrying initial_state but no history_start keeps the
    unbounded reading of the first S2-2 patch, and the generic event reader
    keeps its explicit seed whatever the payload says."""
    ov = _overlay(gate_events=INCEPTION_GATE_EVENTS)
    ov["initial_state"] = "RISK_OFF"
    assert derisk_active_on(ov, "2018-10-30")
    assert derisk_active_on(ov, "1990-01-01")
    assert state_active_on(INCEPTION_GATE_EVENTS, "1990-01-01", "RISK_OFF",
                           initial_active=True)
    assert not state_active_on(INCEPTION_GATE_EVENTS, "1990-01-01",
                               "RISK_OFF")
    # A bounded payload without a seed is inactive on both sides of the start.
    ov2 = _overlay(gate_events=INCEPTION_GATE_EVENTS)
    ov2["history_start"] = "2018-10-31"
    assert not derisk_active_on(ov2, "2018-10-30")
    assert not derisk_active_on(ov2, "2018-10-31")


def _unlagged(active):
    """The reading the engine does NOT use: every close counted as held,
    the last included, and only changes counted as switches. Returned as
    ``(n_switches, days)`` so it can be set against ``gate_counts``."""
    return (sum(1 for i in range(1, len(active)) if active[i] != active[i - 1]),
            sum(1 for a in active if a))


def test_gate_counts_first_close_event():
    """An event dated on the first close sets the opening state, so the
    inception switch is counted from it rather than from the seed. The
    unlagged reading omits that switch."""
    dates = ["2018-10-31", "2018-11-01", "2018-11-02", "2018-11-05"]
    events = [{"date": "2018-10-31", "direction": "RISK_OFF"},
              {"date": "2018-11-02", "direction": "RISK_ON"}]
    derived = derive_daily_states(False, events, dates, "RISK_OFF")
    assert derived == [True, True, False, False]
    assert gate_counts(derived) == (2, 2)
    assert _unlagged(derived) == (1, 2)
    assert gate_counts(derived) != _unlagged(derived)


def test_gate_counts_event_free_history():
    """No events at all: a history that opens RISK_OFF is held de-risked on
    every close after the first and has exactly one switch, the inception
    de-risk; one that opens RISK_ON has none. Counting the last close as
    held, or omitting the inception switch, gives different numbers."""
    dates = ["2018-10-31", "2018-11-01", "2018-11-02", "2018-11-05"]
    off = derive_daily_states(True, [], dates, "RISK_OFF")
    assert off == [True, True, True, True]
    assert gate_counts(off) == (1, 3)
    assert _unlagged(off) == (0, 4)
    on = derive_daily_states(False, None, dates, "RISK_OFF")
    assert on == [False, False, False, False]
    assert gate_counts(on) == (0, 0)
    assert gate_counts([True]) == (0, 0)   # one close: nothing held yet


def test_gate_counts_history_ending_risk_off():
    """A de-risk on the last close is a signal not yet held: it adds
    neither a day nor a switch until a further close exists, on which it
    counts once as a day and once as a switch."""
    dates = ["2018-10-31", "2018-11-01", "2018-11-02"]
    events = [{"date": "2018-11-02", "direction": "RISK_OFF"}]
    derived = derive_daily_states(False, events, dates, "RISK_OFF")
    assert derived == [False, False, True]
    assert gate_counts(derived) == (0, 0)
    assert _unlagged(derived) == (1, 1)
    assert gate_counts(derived + [True]) == (1, 1)


def test_engine_initial_state_fields_come_from_the_first_close():
    """The engine derives both published fields from its own daily series:
    the first index is history_start and the first value the initial state,
    for the gate (0.0 = RISK_OFF) and the tilt (1.0 = EM_TILT_ON)."""
    import pandas as pd
    import run_risk_overlay as ro
    idx = pd.to_datetime(["2018-10-31", "2018-11-01", "2018-11-02"])
    assert ro.initial_state_fields(pd.Series([0.0, 0.0, 1.0], index=idx)) == {
        "history_start": "2018-10-31", "initial_state": "RISK_OFF"}
    assert ro.initial_state_fields(
        pd.Series([1.0, 0.0, 1.0], index=idx))["initial_state"] == "RISK_ON"
    assert ro.tilt_initial_state(pd.Series([1.0, 0.0], index=idx[:2])) == "EM_TILT_ON"
    assert ro.tilt_initial_state(pd.Series([0.0, 1.0], index=idx[:2])) == "EM_TILT_OFF"
    fields = ro.initial_state_fields(pd.Series([0.0, 1.0], index=idx[:2]))
    assert_initial_state_reproduces(
        {**fields, "events": [{"date": "2018-11-01", "direction": "RISK_ON"}]},
        "RISK_OFF", ["2018-10-31", "2018-11-01"], [True, False],
        n_switches=1, days_active=1)


def test_engine_publishes_the_fields_and_guards_the_payload_it_writes():
    """Source-level check, the house pattern for a publish path that has
    no offline harness: the payload literal carries both fields, the tilt
    block carries its own, and the guard runs on the payload object that is
    written, not on a reconstruction from local variables."""
    import re
    src = (Path(__file__).resolve().parent.parent / "scripts"
           / "run_risk_overlay.py").read_text(encoding="utf-8")
    assert '"history_start": initial_fields["history_start"]' in src
    assert '"initial_state": initial_fields["initial_state"]' in src
    assert '"initial_state": tilt_initial_state(sig_aligned)' in src
    assert re.search(r"assert_initial_state_reproduces\(\s*payload,", src)
    assert re.search(
        r'assert_initial_state_reproduces\(\s*payload\["phase22_eem_tilt"\],',
        src)
    assert 'n_switches=payload["n_switches"]' in src
    assert 'days_active=payload["days_risk_off"]' in src
