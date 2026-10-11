"""The breadth level published beside the gate state comes from the feed
that decided the state (2026-10-11).

Before this, risk_overlay.json took its STATES from the Norgate feed and its
LEVELS from the CSP1 scrape. Two visible results: the 2026-03-20 RISK_OFF
event was annotated 20.68% (scrape) while the vendor read 19.09%, a de-risk
printed beside a level above its own 20% threshold; and the dashboards'
gauge showed 36.2% against the deciding 35.26% for the 9 Oct 2026 bar.

Dates are built with pandas (Python months are 1-indexed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_risk_overlay as ro  # noqa: E402


def _march_2026():
    idx = pd.to_datetime(["2026-03-18", "2026-03-19", "2026-03-20",
                          "2026-03-23"])
    scrape = pd.Series([0.2922, 0.2803, 0.2068, 0.2465], index=idx)
    # vendor states: the Norgate series read 0.1909 on 20 Mar, so RISK_OFF
    states = pd.Series([1.0, 1.0, 0.0, 0.0], index=idx)
    return idx, scrape, states


def test_norgate_feed_publishes_vendor_level_with_its_date():
    _, scrape, _ = _march_2026()
    lv = ro.resolve_gate_level("norgate-local", 0.1909, "2026-03-20",
                               scrape, "2026-03-23")
    assert lv["current_breadth"] == pytest.approx(0.1909)
    assert lv["current_breadth_source"] == "norgate-local"
    assert lv["current_breadth_as_of"] == "2026-03-20"
    assert lv["scrape_current_breadth"] == pytest.approx(0.2465)


def test_norgate_feed_without_scalar_falls_back_and_says_so():
    _, scrape, _ = _march_2026()
    lv = ro.resolve_gate_level("norgate-local", None, "2026-03-20",
                               scrape, "2026-03-23")
    assert lv["current_breadth"] == pytest.approx(0.2465)
    assert lv["current_breadth_source"] == "csp1-scrape"
    assert lv["current_breadth_as_of"] == "2026-03-23"


def test_scrape_feed_publishes_scrape_level():
    _, scrape, _ = _march_2026()
    lv = ro.resolve_gate_level("csp1-scrape", 0.1909, "2026-03-20",
                               scrape, "2026-03-23")
    assert lv["current_breadth"] == pytest.approx(0.2465)
    assert lv["current_breadth_source"] == "csp1-scrape"


def test_norgate_feed_events_carry_no_level_from_another_feed():
    """The 2026-03-20 regression: RISK_OFF must not print beside 20.68%."""
    _, scrape, states = _march_2026()
    ev = ro.build_gate_events(states, scrape, "norgate-local")
    assert [e["date"] for e in ev] == ["2026-03-20"]
    assert ev[0]["direction"] == "RISK_OFF"
    assert ev[0]["breadth"] is None
    assert ev[0]["scrape_breadth"] == pytest.approx(0.2068)


def test_scrape_feed_events_never_contradict_their_threshold():
    """On the scrape feed the annotated level decided the flip, so every
    RISK_OFF sits strictly below OFF and every RISK_ON strictly above ON."""
    idx = pd.bdate_range("2025-12-29", periods=8)   # spans the year boundary
    scrape = pd.Series([0.40, 0.25, 0.19, 0.30, 0.49, 0.50, 0.51, 0.45],
                       index=idx)
    states = ro._compute_states(scrape, ro.OFF_THRESHOLD, ro.ON_THRESHOLD)
    ev = ro.build_gate_events(states, scrape, "csp1-scrape")
    assert [e["direction"] for e in ev] == ["RISK_OFF", "RISK_ON"]
    for e in ev:
        if e["direction"] == "RISK_OFF":
            assert e["breadth"] < ro.OFF_THRESHOLD
        else:
            assert e["breadth"] > ro.ON_THRESHOLD
        assert "scrape_breadth" not in e


@pytest.fixture
def states_path(tmp_path, monkeypatch):
    p = tmp_path / "gate_states_norgate.json"
    monkeypatch.setattr(ro, "NORGATE_STATES_PATH", p)
    return p


def test_level_loader_reads_scalar(states_path):
    states_path.write_text(json.dumps({"last_bar": "2026-10-09",
                                       "current_breadth": 0.3526}),
                           encoding="utf-8")
    assert ro._load_norgate_level() == pytest.approx(0.3526)


@pytest.mark.parametrize("body", [None, "{not json", json.dumps({"last_bar": "2026-10-09"}),
                                  json.dumps({"current_breadth": "0.35"})])
def test_level_loader_fails_to_none(states_path, body):
    if body is not None:
        states_path.write_text(body, encoding="utf-8")
    assert ro._load_norgate_level() is None
