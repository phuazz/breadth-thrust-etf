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

from scripts.check_capture_integrity import (
    RETURN_BOUND,
    apply_strict,
    evaluate_target,
)

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


# ---------------------------------------------------------------------------
# --strict escalation (2026-07-18): the weekly run must not email a
# factsheet whose B/C series are missing the Friday rebalance bar.
# ---------------------------------------------------------------------------

def test_strict_escalates_warn_to_fail_for_named_targets_only():
    results = [
        {"label": "Strategy B (asset-class)", "status": "warn", "evidence": "e"},
        {"label": "Live track", "status": "warn", "evidence": "e"},
    ]
    out = apply_strict(results, ("b", "live"), {"b", "c"})
    assert out[0]["status"] == "fail"
    assert "strict" in out[0]["evidence"]
    # The live track keeps the warn-and-publish cadence rule.
    assert out[1]["status"] == "warn"


def test_strict_leaves_ok_and_fail_untouched():
    results = [
        {"label": "Strategy B (asset-class)", "status": "ok", "evidence": "e"},
        {"label": "Strategy C (thematic)", "status": "fail", "evidence": "e"},
    ]
    out = apply_strict(results, ("b", "c"), {"b", "c"})
    assert [r["status"] for r in out] == ["ok", "fail"]


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


# --- Short forward-only live track (anchor-aware branch) -------------------
# A live track is a strictly forward-only extension of the deployed blend.
# When the backtest anchor is already current there is nothing to extend, so
# 0-1 points is correct, not corrupt — freshness is judged on the anchor.

def _eval_live(tmp_path: Path, dates, equity, anchor, expected=EXPECTED) -> dict:
    p = tmp_path / "live.json"
    p.write_text(json.dumps({"anchor_date": anchor, "live_dates": dates,
                             "live_equity": equity}), encoding="utf-8")
    return evaluate_target("Live track", p, ("live_dates",), ("live_equity",),
                           expected, anchor_path=("anchor_date",))


def test_live_one_point_ok_when_anchor_current(tmp_path):
    # Anchor already on the expected session, one live point past it.
    r = _eval_live(tmp_path, ["2026-07-02"], [1.01], anchor="2026-07-02")
    assert r["status"] == "ok", r["evidence"]


def test_live_zero_points_ok_when_anchor_current(tmp_path):
    # Backtest fully current; nothing to extend at all.
    r = _eval_live(tmp_path, [], [], anchor="2026-07-02")
    assert r["status"] == "ok", r["evidence"]


def test_live_europe_only_bar_ahead_is_ok(tmp_path):
    # The exact 2026-07-03 boundary: US closed (Independence Day observed),
    # Europe open, so the lone live bar is 03 Jul — ahead of the expected
    # 02 Jul NYSE session. Anchor 01 Jul. Must not fail.
    r = _eval_live(tmp_path, ["2026-07-03"], [1.001], anchor="2026-07-01")
    assert r["status"] == "ok", r["evidence"]


def test_live_one_point_warn_when_anchor_one_session_behind(tmp_path):
    # Anchor Wed 01 Jul, no live points, expected Fri 02 Jul -> 1 behind.
    r = _eval_live(tmp_path, [], [], anchor="2026-07-01")
    assert r["status"] == "warn", r["evidence"]


def test_live_one_point_fail_when_anchor_stale(tmp_path):
    # Genuinely stale capture: anchor 29 Jun, single old point, expected
    # 02 Jul -> 2+ sessions behind. The leniency must not mask this.
    r = _eval_live(tmp_path, ["2026-06-29"], [1.00], anchor="2026-06-29")
    assert r["status"] == "fail", r["evidence"]


def test_short_series_without_anchor_still_fails(tmp_path):
    # B/C (no anchor path) must remain strict: a short series is corruption.
    p = _write(tmp_path, ["2026-07-02"], [1.01])
    r = evaluate_target("Strategy B", p, ("live_dates",), ("live_equity",),
                        EXPECTED)  # anchor_path defaults to None
    assert r["status"] == "fail"
    assert "malformed" in r["evidence"]


def test_live_short_series_month_boundary(tmp_path):
    # Anchor Tue 30 Jun, lone live bar Wed 01 Jul, expected 01 Jul -> current.
    r = _eval_live(tmp_path, ["2026-07-01"], [1.0], anchor="2026-06-30",
                   expected=date(2026, 7, 1))
    assert r["status"] == "ok", r["evidence"]


def test_live_short_series_year_boundary(tmp_path):
    # Anchor Wed 31 Dec 2025, no live points, expected Fri 2 Jan 2026.
    # 1 Jan is a holiday, so the anchor is 1 session behind -> warn.
    r = _eval_live(tmp_path, [], [], anchor="2025-12-31",
                   expected=date(2026, 1, 2))
    assert r["status"] == "warn", r["evidence"]


def test_live_length_mismatch_still_fails_even_with_anchor(tmp_path):
    # Corruption check runs before the short-series leniency.
    p = tmp_path / "live.json"
    p.write_text(json.dumps({"anchor_date": "2026-07-02",
                             "live_dates": ["2026-07-02"],
                             "live_equity": []}), encoding="utf-8")
    r = evaluate_target("Live track", p, ("live_dates",), ("live_equity",),
                        EXPECTED, anchor_path=("anchor_date",))
    assert r["status"] == "fail"
    assert "malformed" in r["evidence"]
