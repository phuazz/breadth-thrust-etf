"""Guards for the Stage-1 Norgate publisher's state naming.

The divergence check in publish_norgate_breadth compares its own
current_state string against data/risk_overlay.json's. A label mismatch
(the original "DERISK" vs the deployed "RISK_OFF") would print a false
FLAG on every day both feeds agree the gate is OFF — polluting the
Stage-1 soak log that the 2026-08-07 review reads. Pin the labels to the
deployed naming so the regression cannot return silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import publish_norgate_breadth as pub  # noqa: E402
from run_risk_overlay import _compute_states  # noqa: E402


def test_state_labels_match_deployed_naming():
    """run_risk_overlay emits "RISK_ON"/"RISK_OFF" (see its output schema);
    the publisher must use the identical strings."""
    assert pub.STATE_LABELS == {1.0: "RISK_ON", 0.0: "RISK_OFF"}


def test_state_labels_cover_every_hysteresis_state():
    """_compute_states emits only 0.0/1.0; every value it can produce must
    have a label, so the publisher can never KeyError on a real series."""
    breadth = pd.Series(
        [0.55, 0.15, 0.35, 0.55, 0.10],
        index=pd.date_range("2026-01-05", periods=5, freq="B"),
    )
    states = _compute_states(breadth, 0.20, 0.50)
    # Walk covers both regimes: ON -> OFF -> (hold) -> ON -> OFF.
    assert set(states.unique()) == {0.0, 1.0}
    assert set(states.unique()) <= set(pub.STATE_LABELS)
