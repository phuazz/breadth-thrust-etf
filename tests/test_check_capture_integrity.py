"""Tests for scripts/check_capture_integrity.py verdict logic.

Classification is tested against synthetic series files with a fixed
expected session, so no live calendar or network is involved (the
calendar arithmetic itself is covered by tests/test_nyse_sessions.py).
Python date months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scripts.check_capture_integrity import RETURN_BOUND, evaluate_target

# Fri 2 Jul 2026 close as the reference "expected" session; Wed 1 Jul
# and Tue 30 Jun are the 1- and 2-session-behind cases (no holiday in
# between, verified in test_nyse_sessions).
EXPECTED = date(2026, 7, 2)


def _write(tmp_path: Path, dates, equity) -> Path:
    p = tmp_path / "series.json"
    p.write_text(json.dumps({"live_dates": dates, "live_equity": equity}),
                 encoding="utf-8")
    return p


def _eval(tmp_path: Path, dates, equity):
    p = _write(tmp_path, dates, equity)
    return evaluate_target("Test", p, ("live_dates",), ("live_equity",),
                           EXPECTED)


def test_ok_when_series_ends_on_expected_session(tmp_path):
    r = _eval(tmp_path, ["2026-07-01", "2026-07-02"], [1.00, 1.01])
    assert r["status"] == "ok"


def test_warn_when_one_session_behind(tmp_path):
    r = _eval(tmp_path, ["2026-06-30", "2026-07-01"], [1.00, 1.01])
    assert r["status"] == "warn"


def test_fail_when_two_sessions_behind(tmp_path):
    r = _eval(tmp_path, ["2026-06-29", "2026-06-30"], [1.00, 1.01])
    assert r["status"] == "fail"


def test_fail_on_implausible_last_return(tmp_path):
    # A one-day move beyond RETURN_BOUND on a strategy-level series is a
    # data error by construction (diversified sleeve).
    bad = 1.0 + RETURN_BOUND + 0.05
    r = _eval(tmp_path, ["2026-07-01", "2026-07-02"], [1.00, bad])
    assert r["status"] == "fail"
    assert "implausible" in r["evidence"]


def test_fail_on_non_increasing_tail_dates(tmp_path):
    r = _eval(tmp_path, ["2026-07-02", "2026-07-02"], [1.00, 1.01])
    assert r["status"] == "fail"
    assert "non-increasing" in r["evidence"]


def test_fail_on_length_mismatch(tmp_path):
    r = _eval(tmp_path, ["2026-07-01", "2026-07-02"], [1.00])
    assert r["status"] == "fail"


def test_fail_on_missing_file(tmp_path):
    r = evaluate_target("Test", tmp_path / "absent.json",
                        ("live_dates",), ("live_equity",), EXPECTED)
    assert r["status"] == "fail"
    assert "unreadable" in r["evidence"]


def test_year_boundary_lag_is_sessions_not_days(tmp_path):
    # Series ending Wed 31 Dec 2025 checked against Fri 2 Jan 2026:
    # 1 Jan is a holiday, so this is 1 session behind (warn), although
    # 2 calendar days and 2 weekdays elapsed.
    p = _write(tmp_path, ["2025-12-30", "2025-12-31"], [1.00, 1.01])
    r = evaluate_target("Test", p, ("live_dates",), ("live_equity",),
                        date(2026, 1, 2))
    assert r["status"] == "warn"
