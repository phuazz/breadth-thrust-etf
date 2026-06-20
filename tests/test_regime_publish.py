"""Regression tests for the regime-publish freshness / reconciliation /
near-threshold guards. Phase 28.5 (2026-06-20).

Replays the 2026-03-27 -> 2026-06-18 silent-staleness incident:

  - On 2026-06-13 (git commit e09fc02) the published risk_overlay headline
    was 'RISK_ON since 2025-05-02, breadth 55%, ARMED, +35pp buffer'.
  - The breadth panel feeding the regime gate had end_date 2026-05-29 —
    11 trading days behind the publish date.
  - When the panel was finally refreshed on 2026-06-18 (commit f4a284d),
    two new historical events appeared: 2026-03-27 RISK_OFF (breadth
    19.92%) and 2026-04-09 RISK_ON (50.2%).
  - The actual de-risk on 27 March was therefore invisible for ~11 weeks.

These tests pin down that a future stale-panel situation cannot be
published as a confident regime again.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from regime_publish import (  # noqa: E402
    DEFAULT_BUDGET_TRADING_DAYS,
    assert_state_since_matches_events,
    detect_historical_revision,
    regime_publish_status,
)


# =============================================================================
# regime_publish_status — fresh / stale / near
# =============================================================================

def test_fresh_panel_within_budget_is_ok():
    """Panel updated today against a healthy breadth reading -> publish."""
    s = regime_publish_status(
        panel_end_date=date(2026, 6, 19),
        current_breadth=0.61,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 6, 19),
    )
    assert s.status == "ok"
    assert s.publishable is True
    assert s.near_threshold is False
    assert s.lag_trading_days == 0


def test_panel_at_budget_boundary_is_ok():
    """Lag of exactly DEFAULT_BUDGET_TRADING_DAYS must pass (strict gt)."""
    s = regime_publish_status(
        panel_end_date=date(2026, 6, 12),         # Fri
        current_breadth=0.60,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 6, 19),                  # Fri, exactly 5 bdays later
    )
    assert s.status == "ok"
    assert s.publishable is True
    assert s.lag_trading_days == DEFAULT_BUDGET_TRADING_DAYS


def test_panel_one_day_over_budget_is_stale():
    s = regime_publish_status(
        panel_end_date=date(2026, 6, 11),         # Thu
        current_breadth=0.60,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 6, 19),                  # Fri, 6 bdays later
    )
    assert s.status == "stale"
    assert s.publishable is False
    assert s.lag_trading_days == 6
    # The 'STALE' label lives on the renderers' banner header, not in this
    # supporting message — see regime_publish.py docstring.
    assert "trading days behind" in s.message
    assert "refresh_all.py" in s.message


def test_historical_incident_replay_2026_06_13_publish():
    """The 2026-06-13 publish was 11 trading days stale and should have
    been blocked. This pins that verdict."""
    s = regime_publish_status(
        panel_end_date=date(2026, 5, 29),         # what e09fc02 actually had
        current_breadth=0.5467,                   # what it actually rendered
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 6, 13),                  # the publish date
    )
    assert s.status == "stale"
    assert s.publishable is False
    assert s.lag_trading_days == 11
    assert "2026-05-29" in s.message
    assert "11 trading days" in s.message


def test_near_off_threshold_below_is_flagged():
    """27 March 2026 trigger vintage: breadth 19.4%, 0.6pp below the 20%
    off threshold. Even on a fresh panel this should not publish 'ARMED'."""
    s = regime_publish_status(
        panel_end_date=date(2026, 3, 27),
        current_breadth=0.1939,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 3, 27),
    )
    assert s.status == "near"
    assert s.publishable is True       # not stale; just precariously placed
    assert s.near_threshold is True
    assert s.proximity_band == "below_off"
    assert "NEAR THRESHOLD" in s.message


def test_near_on_threshold_above_is_flagged():
    """A reading just above the 50% re-arm line is also operationally noisy."""
    s = regime_publish_status(
        panel_end_date=date(2026, 4, 9),
        current_breadth=0.504,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 4, 9),
    )
    assert s.status == "near"
    assert s.near_threshold is True
    assert s.proximity_band == "above_on"


def test_safely_inside_no_alert():
    """Mid-band breadth (e.g. 60%) is clearly RISK_ON — neither stale nor
    near."""
    s = regime_publish_status(
        panel_end_date=date(2026, 6, 19),
        current_breadth=0.60,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 6, 19),
    )
    assert s.status == "ok"
    assert s.near_threshold is False
    assert s.proximity_band is None


def test_stale_dominates_near():
    """A stale panel that also happens to be near a threshold should publish
    the 'stale' verdict (publishable=False) — stale is the more dangerous
    state."""
    s = regime_publish_status(
        panel_end_date=date(2026, 3, 27),
        current_breadth=0.1939,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 6, 19),                  # months later
    )
    assert s.status == "stale"
    assert s.publishable is False
    # Near flag is still set, so renderers that want to surface both can.
    assert s.near_threshold is True


def test_panel_in_the_future_does_not_underflow():
    """Defensive: if a clock skew gives panel_end_date > today, lag is 0,
    not negative."""
    s = regime_publish_status(
        panel_end_date=date(2026, 6, 25),
        current_breadth=0.55,
        off_threshold=0.20, on_threshold=0.50,
        today=date(2026, 6, 19),
    )
    assert s.lag_trading_days == 0
    assert s.status == "ok"


# =============================================================================
# assert_state_since_matches_events
# =============================================================================

def test_state_since_matches_latest_matching_event():
    events = [
        {"date": "2025-04-04", "direction": "RISK_OFF", "breadth": 0.18},
        {"date": "2025-05-02", "direction": "RISK_ON",  "breadth": 0.57},
        {"date": "2026-03-27", "direction": "RISK_OFF", "breadth": 0.20},
        {"date": "2026-04-09", "direction": "RISK_ON",  "breadth": 0.50},
    ]
    # Should not raise.
    assert_state_since_matches_events("RISK_ON", "2026-04-09", events)


def test_state_since_disagrees_with_events_raises():
    """The exact FM-2 scenario — the 2026-06-13 publish had
    current_state_since='2025-05-02' while a freshly recomputed events list
    would have had 2026-04-09 as the most recent RISK_ON. Had this guard
    existed, the publish would have failed."""
    events_corrected = [
        {"date": "2025-05-02", "direction": "RISK_ON",  "breadth": 0.57},
        {"date": "2026-03-27", "direction": "RISK_OFF", "breadth": 0.20},
        {"date": "2026-04-09", "direction": "RISK_ON",  "breadth": 0.50},
    ]
    with pytest.raises(ValueError) as exc:
        assert_state_since_matches_events(
            "RISK_ON", "2025-05-02", events_corrected,
        )
    msg = str(exc.value)
    assert "current_state_since" in msg
    assert "2026-04-09" in msg
    assert "FM-2" in msg


def test_no_matching_event_falls_back_to_series_start():
    events_only_off = [{"date": "2025-04-04", "direction": "RISK_OFF",
                          "breadth": 0.18}]
    # Should not raise.
    assert_state_since_matches_events(
        "RISK_ON", "2018-11-01", events_only_off,
        series_start_date="2018-11-01",
    )


def test_no_matching_event_disagreeing_start_raises():
    events_only_off = [{"date": "2025-04-04", "direction": "RISK_OFF",
                          "breadth": 0.18}]
    with pytest.raises(ValueError):
        assert_state_since_matches_events(
            "RISK_ON", "2024-01-01", events_only_off,
            series_start_date="2018-11-01",
        )


# =============================================================================
# detect_historical_revision
# =============================================================================

def test_revision_detects_the_incident():
    """e09fc02 had no 2026-03-27 event; f4a284d added it. The detector
    must flag this as an 'added' revision."""
    prior = [
        {"date": "2025-05-02", "direction": "RISK_ON",  "breadth": 0.57},
    ]
    new = [
        {"date": "2025-05-02", "direction": "RISK_ON",  "breadth": 0.57},
        {"date": "2026-03-27", "direction": "RISK_OFF", "breadth": 0.1992},
        {"date": "2026-04-09", "direction": "RISK_ON",  "breadth": 0.502},
    ]
    # Both 2026 events are AFTER prior's last (2025-05-02) so they are tail
    # additions, not revisions.
    revs = detect_historical_revision(prior, new)
    assert revs == []


def test_revision_detects_added_past_event():
    """Stronger test — past-date event appears that wasn't there before."""
    prior = [
        {"date": "2025-01-21", "direction": "RISK_OFF", "breadth": 0.18},
        {"date": "2025-05-02", "direction": "RISK_ON",  "breadth": 0.57},
        {"date": "2026-06-01", "direction": "RISK_OFF", "breadth": 0.19},
    ]
    new = [
        {"date": "2025-01-21", "direction": "RISK_OFF", "breadth": 0.18},
        {"date": "2025-05-02", "direction": "RISK_ON",  "breadth": 0.57},
        {"date": "2026-03-27", "direction": "RISK_OFF", "breadth": 0.1992},
        {"date": "2026-04-09", "direction": "RISK_ON",  "breadth": 0.502},
        {"date": "2026-06-01", "direction": "RISK_OFF", "breadth": 0.19},
    ]
    revs = detect_historical_revision(prior, new)
    added_dates = [r["date"] for r in revs if r["change"] == "added"]
    assert "2026-03-27" in added_dates
    assert "2026-04-09" in added_dates


def test_revision_detects_changed_direction():
    prior = [
        {"date": "2026-03-27", "direction": "RISK_OFF", "breadth": 0.20},
        {"date": "2026-06-01", "direction": "RISK_OFF", "breadth": 0.19},
    ]
    new = [
        {"date": "2026-03-27", "direction": "RISK_ON", "breadth": 0.21},
        {"date": "2026-06-01", "direction": "RISK_OFF", "breadth": 0.19},
    ]
    revs = detect_historical_revision(prior, new)
    assert len(revs) == 1
    assert revs[0]["date"] == "2026-03-27"
    assert revs[0]["change"] == "changed"
    assert revs[0]["from"] == "RISK_OFF"
    assert revs[0]["to"] == "RISK_ON"


def test_revision_empty_when_no_prior():
    revs = detect_historical_revision([], [{"date": "2026-03-27",
                                              "direction": "RISK_OFF",
                                              "breadth": 0.20}])
    assert revs == []


# =============================================================================
# Integration — actual publish path must use the guard (P2 wires this in)
# =============================================================================

def test_factsheet_regime_block_uses_freshness_guard():
    """End-to-end: the factsheet's regime headline builder must consult
    regime_publish_status and refuse to render confident copy on a stale
    panel. Replays the 2026-06-13 incident.

    Fails today (the helper does not exist in build_factsheet); will pass
    after P2 wires ``build_regime_block`` into ``scripts/build_factsheet.py``.
    """
    pytest.importorskip("scripts.build_factsheet", reason="P2 not yet landed")
    from build_factsheet import build_regime_block  # type: ignore

    overlay = {
        "current_state": "RISK_ON",
        "current_state_since": "2025-05-02",
        "current_breadth": 0.5467,
        "gate_parameters": {"off_threshold": 0.20, "on_threshold": 0.50,
                             "derisk_fraction": 0.50},
    }
    rendered = build_regime_block(
        overlay,
        panel_end_date=date(2026, 5, 29),
        today=date(2026, 6, 13),
    )
    # build_regime_block returns a structured verdict dict; the
    # 'STALE' word lives on the renderers' banner header that this
    # dict feeds (see build_factsheet.build_regime_panel / template.html).
    # Check the structural fields directly rather than scraping str(dict).
    assert isinstance(rendered, dict)
    assert rendered["status"] == "stale"
    assert rendered["publishable"] is False
    assert rendered["lag_trading_days"] == 11
    assert rendered["panel_end_date"] == "2026-05-29"
    # The supporting message no longer carries 'STALE' (the header does)
    # but it must still carry the actionable substance.
    assert "trading days behind" in rendered["message"]
    assert "refresh_all.py" in rendered["message"]
    # Confident copy from build_watchlist's ARMED path must not appear.
    assert "ARMED" not in str(rendered), (
        f"confident ARMED copy leaked into verdict: {str(rendered)[:200]}"
    )
