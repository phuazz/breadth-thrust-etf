"""Stage-2 loader guards for the Norgate derived-states gate feed.

The Stage-2 swap (reviews/2026-07-17_norgate-feed-migration.md §4) makes
run_risk_overlay consume vendor-derived gate states from
data/gate_states_norgate.json when present and fresh, falling back to the
scrape-computed path under the deployed GATE_MAX_STALE_DAYS cap. These
tests pin:

  * the freshness boundary at exactly the cap, including a month boundary
    and a year boundary (CLAUDE.md date-edge rule);
  * hold-state alignment semantics (ffill past the file's last bar equals
    the scrape path's NaN-hold degradation);
  * every fallback branch — absent, stale, malformed, empty, non-binary —
    returns (None, None) and never raises.

Dates are constructed with pandas/datetime (Python months are 1-indexed);
each boundary test asserts the calendar-day gap it claims to exercise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_risk_overlay as ro  # noqa: E402


def _write_states(path: Path, dates: list[str], states: list[int],
                  last_bar: str | None = None) -> None:
    doc = {
        "generated_at_utc": "2026-07-17T00:00:00+00:00",
        "source": "test fixture (derived states only)",
        "state_machine": "run_risk_overlay._compute_states (off 0.2, on 0.5)",
        "last_bar": last_bar or dates[-1],
        "current_state": ("RISK_ON" if states and states[-1] == 1
                          else "RISK_OFF"),
        "series": {"dates": dates, "state": states},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def states_path(tmp_path, monkeypatch):
    p = tmp_path / "gate_states_norgate.json"
    monkeypatch.setattr(ro, "NORGATE_STATES_PATH", p)
    return p


def test_fresh_file_consumed_and_held_to_calendar_end(states_path):
    """A fresh file is consumed on the blend calendar; days past its last
    bar hold the last state (the scrape path's NaN-hold degradation,
    bounded above by the freshness cap)."""
    _write_states(states_path,
                  ["2026-07-13", "2026-07-14", "2026-07-15"], [1, 0, 0])
    common = pd.date_range("2026-07-13", "2026-07-21", freq="B")
    states, last_bar = ro._load_norgate_states(common)
    assert last_bar == "2026-07-15"
    assert list(states.index) == list(common)
    assert states.loc["2026-07-13"] == 1.0
    assert states.loc["2026-07-14"] == 0.0
    # Held flat past the file's last bar, through to the calendar end.
    assert (states.loc["2026-07-16":] == 0.0).all()


def test_stale_file_rejected_past_cap(states_path):
    """Last bar more than GATE_MAX_STALE_DAYS calendar days behind the
    blend calendar end: loader declines, scrape path governs."""
    _write_states(states_path, ["2026-07-01", "2026-07-02"], [1, 1])
    common = pd.date_range("2026-07-01", "2026-07-13", freq="B")
    gap = (common[-1] - pd.Timestamp("2026-07-02")).days
    assert gap == 11 > ro.GATE_MAX_STALE_DAYS
    assert ro._load_norgate_states(common) == (None, None)


def test_freshness_boundary_at_cap_across_month_end(states_path):
    """Month-boundary edge: last bar 2026-07-24, calendar end 2026-08-03
    is exactly the 10-day cap across the July/August boundary — still
    fresh. One more calendar day is stale."""
    _write_states(states_path, ["2026-07-23", "2026-07-24"], [1, 1])
    at_cap = pd.date_range("2026-07-23", "2026-08-03", freq="B")
    assert (at_cap[-1] - pd.Timestamp("2026-07-24")).days == \
        ro.GATE_MAX_STALE_DAYS == 10
    states, _ = ro._load_norgate_states(at_cap)
    assert states is not None and (states == 1.0).all()

    past_cap = pd.date_range("2026-07-23", "2026-08-04", freq="B")
    assert (past_cap[-1] - pd.Timestamp("2026-07-24")).days == 11
    assert ro._load_norgate_states(past_cap) == (None, None)


def test_freshness_boundary_at_cap_across_year_end(states_path):
    """Year-boundary edge: last bar 2026-12-28, calendar end 2027-01-07 is
    exactly the 10-day cap across the year boundary — still fresh. One
    more calendar day is stale."""
    _write_states(states_path, ["2026-12-24", "2026-12-28"], [0, 0])
    at_cap = pd.date_range("2026-12-24", "2027-01-07", freq="B")
    assert (at_cap[-1] - pd.Timestamp("2026-12-28")).days == \
        ro.GATE_MAX_STALE_DAYS == 10
    states, _ = ro._load_norgate_states(at_cap)
    assert states is not None
    # OFF state held across the year boundary, never silently reset.
    assert (states.loc["2026-12-28":] == 0.0).all()

    past_cap = pd.date_range("2026-12-24", "2027-01-08", freq="B")
    assert (past_cap[-1] - pd.Timestamp("2026-12-28")).days == 11
    assert ro._load_norgate_states(past_cap) == (None, None)


def test_absent_file_falls_back(states_path):
    common = pd.date_range("2026-07-01", "2026-07-10", freq="B")
    assert not states_path.exists()
    assert ro._load_norgate_states(common) == (None, None)


def test_malformed_file_falls_back_without_raising(states_path):
    states_path.write_text("{this is not json", encoding="utf-8")
    common = pd.date_range("2026-07-01", "2026-07-10", freq="B")
    assert ro._load_norgate_states(common) == (None, None)


def test_missing_series_key_falls_back(states_path):
    states_path.write_text(json.dumps({"last_bar": "2026-07-10"}),
                           encoding="utf-8")
    common = pd.date_range("2026-07-01", "2026-07-10", freq="B")
    assert ro._load_norgate_states(common) == (None, None)


def test_empty_series_falls_back(states_path):
    _write_states(states_path, [], [], last_bar="2026-07-10")
    common = pd.date_range("2026-07-01", "2026-07-10", freq="B")
    assert ro._load_norgate_states(common) == (None, None)


def test_non_binary_states_rejected(states_path):
    """States other than 0/1 (a corrupted or wrong-schema file) must not
    reach the weight arithmetic, where a 2.0 would lever the blend."""
    _write_states(states_path, ["2026-07-09", "2026-07-10"], [1, 2])
    common = pd.date_range("2026-07-01", "2026-07-10", freq="B")
    assert ro._load_norgate_states(common) == (None, None)


def test_hold_semantics_match_scrape_nan_hold(states_path):
    """Equivalence pin: published states ffilled onto the blend calendar
    equal _compute_states run on a breadth series whose tail has gone NaN
    under the cap — the two degradation paths must agree."""
    idx = pd.date_range("2026-01-05", "2026-01-16", freq="B")
    # ON -> OFF flip on 2026-01-07, then the feed stalls (NaN tail).
    breadth = pd.Series(
        [0.55, 0.40, 0.15, 0.30, 0.30, np.nan, np.nan, np.nan, np.nan,
         np.nan],
        index=idx,
    )
    scrape_states = ro._compute_states(
        breadth, ro.OFF_THRESHOLD, ro.ON_THRESHOLD)

    # Publisher view: real bars only (padding NONE), states from the same
    # machine, file then ffilled by the loader onto the full calendar.
    real = breadth.dropna()
    pub_states = ro._compute_states(real, ro.OFF_THRESHOLD, ro.ON_THRESHOLD)
    _write_states(states_path,
                  [d.strftime("%Y-%m-%d") for d in pub_states.index],
                  [int(s) for s in pub_states.values])
    loaded, _ = ro._load_norgate_states(idx)

    pd.testing.assert_series_equal(loaded, scrape_states,
                                   check_names=False)
