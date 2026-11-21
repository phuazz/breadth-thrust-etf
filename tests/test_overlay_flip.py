"""check_overlay_flip.assess: flip detection, sign of gate_on, and the staleness guard.

Python datetime months are 1-indexed. Weekdays are asserted with the date
library, never assumed; session counts come from the NYSE calendar.
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_overlay_flip import assess, run_start, sessions_between
from venue_calendars import get_calendar

CAL = get_calendar("NYSE")


def _gate(last_bar, states, dates=None, breadth=0.49):
    dates = dates or [str(d.date()) for d in pd.bdate_range(end=last_bar, periods=len(states))]
    assert dates[-1] == last_bar
    return {"last_bar": last_bar, "current_breadth": breadth,
            "current_state": "RISK_ON" if states[-1] == 1 else "RISK_OFF",
            "series": {"dates": dates, "state": states}}


def _decision(gate_on=False, tilt_on=False, as_of="2026-11-13"):
    return {"as_of": as_of, "gate_on": gate_on, "tilt_on": tilt_on}


def _tilt(end, values):
    idx = pd.DatetimeIndex([d for d in pd.bdate_range(end=end, periods=len(values))])
    return pd.Series([float(v) for v in values], index=idx)


def test_no_flip_is_ok():
    r = assess(_gate("2026-11-19", [1, 1, 1]), _decision(),
               _tilt("2026-11-19", [0, 0, 0]), pd.Timestamp("2026-11-19"), CAL)
    assert r["status"] == "OK"


def test_gate_flip_reported_with_start_date():
    r = assess(_gate("2026-11-19", [1, 1, 0, 0], breadth=0.18), _decision(),
               _tilt("2026-11-19", [0, 0, 0]), pd.Timestamp("2026-11-19"), CAL)
    assert r["status"] == "FLIP"
    assert "GATE RISK_ON -> RISK_OFF on 2026-11-18" in r["subject"]


def test_gate_on_means_derisk_so_risk_off_book_matches_risk_off_feed():
    # gate_on True in overlay_decision.json = de-risk on = RISK_OFF
    r = assess(_gate("2026-11-19", [0, 0]), _decision(gate_on=True),
               _tilt("2026-11-19", [0, 0]), pd.Timestamp("2026-11-19"), CAL)
    assert r["status"] == "OK"


def test_tilt_flip_reported():
    r = assess(_gate("2026-11-19", [1, 1]), _decision(),
               _tilt("2026-11-19", [0, 0, 1]), pd.Timestamp("2026-11-19"), CAL)
    assert r["status"] == "FLIP"
    assert "TILT EM_TILT_OFF -> EM_TILT_ON on 2026-11-19" in r["subject"]


def test_tilt_input_a_session_short_is_stale_not_a_flip():
    # Yahoo's overnight withdrawal leaves the ratio ending a session early.
    r = assess(_gate("2026-11-19", [1, 1]), _decision(),
               _tilt("2026-11-18", [0, 1]), pd.Timestamp("2026-11-19"), CAL)
    assert r["status"] == "STALE"


def test_month_boundary_two_sessions_behind_is_stale():
    # Fri 27 Nov 2026 (post-Thanksgiving half day) to Tue 1 Dec 2026.
    assert date(2026, 11, 27).strftime("%A") == "Friday"
    assert date(2026, 12, 1).strftime("%A") == "Tuesday"
    assert sessions_between(CAL, pd.Timestamp("2026-11-27"), pd.Timestamp("2026-12-01")) == 2
    r = assess(_gate("2026-11-27", [1, 0]), _decision(),
               _tilt("2026-12-01", [0, 0]), pd.Timestamp("2026-12-01"), CAL)
    # A stale gate feed must never be read as a flip or an all-clear.
    assert r["status"] == "STALE"


def test_year_boundary_holiday_gap_is_one_session_and_tolerated():
    # Thu 31 Dec 2026 -> Mon 4 Jan 2027; Fri 1 Jan 2027 is the New Year holiday.
    assert date(2026, 12, 31).strftime("%A") == "Thursday"
    assert date(2027, 1, 1).strftime("%A") == "Friday"
    assert date(2027, 1, 4).strftime("%A") == "Monday"
    assert sessions_between(CAL, pd.Timestamp("2026-12-31"), pd.Timestamp("2027-01-04")) == 1
    r = assess(_gate("2026-12-31", [1, 0]), _decision(),
               _tilt("2027-01-04", [0, 0]), pd.Timestamp("2027-01-04"), CAL)
    assert r["status"] == "FLIP"
    assert r["gate_sessions_behind"] == 1


def test_missing_tilt_is_stale():
    r = assess(_gate("2026-11-19", [1, 1]), _decision(), None,
               pd.Timestamp("2026-11-19"), CAL)
    assert r["status"] == "STALE"


def test_run_start():
    d =["2026-11-16", "2026-11-17", "2026-11-18"]
    assert run_start(d, [1, 0, 0]) == "2026-11-17"
    assert run_start(d, [0, 0, 0]) == "2026-11-16"


def test_unknown_gate_state_raises():
    g = _gate("2026-11-19", [1, 1])
    g["current_state"] = "DERISK"
    with pytest.raises(ValueError):
        assess(g, _decision(), _tilt("2026-11-19", [0]), pd.Timestamp("2026-11-19"), CAL)
