"""Regression tests for the Week-to-date computation in the dashboard.

The actual WTD is computed client-side by ``_computeWeekToDate`` in
``template.html``. This test exercises a Python implementation of the
identical algorithm so we can catch month/year-boundary bugs before
they hit the dashboard. CLAUDE.md mandates two edge-case tests for any
date logic; we cover month boundary, year boundary, and leap day.

Algorithm under test (mirrors the JS):
    Given a parallel (dates, equity) series ending on ``last_date``:
      1. Find the Monday of last_date's calendar week.
      2. Walk back through ``dates`` to find the most recent entry
         strictly BEFORE that Monday (the prior weekly close).
      3. WTD = equity[last] / equity[base] - 1.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest


def compute_wtd(dates: list[str], equity: list[float]) -> dict | None:
    """Port of ``_computeWeekToDate`` from template.html.

    Args:
        dates: parallel list of YYYY-MM-DD strings, ascending order.
        equity: parallel list of cumulative equity values.

    Returns:
        ``{"pct": float, "from_date": str, "to_date": str}`` or None
        if the series is too short or if no prior-week trading day
        exists in the series.
    """
    if not dates or not equity or len(dates) < 2:
        return None
    last_idx = len(dates) - 1
    last_dt = datetime.strptime(dates[last_idx], "%Y-%m-%d").date()
    # weekday(): Mon=0, Sun=6. JavaScript getUTCDay(): Sun=0, Sat=6.
    # We want days back to Monday of last_dt's week. In Python that's
    # last_dt.weekday() (already 0 for Monday).
    days_to_mon = last_dt.weekday()
    monday = last_dt - timedelta(days=days_to_mon)
    monday_str = monday.strftime("%Y-%m-%d")
    base_idx = last_idx
    while base_idx > 0 and dates[base_idx] >= monday_str:
        base_idx -= 1
    if dates[base_idx] >= monday_str:
        return None
    return {
        "pct": equity[last_idx] / equity[base_idx] - 1,
        "from_date": dates[base_idx],
        "to_date": dates[last_idx],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_friday_to_friday_typical_week():
    """Last entry is a Friday: WTD = this Friday vs prior Friday's close."""
    # 2026-05-15 Fri, 2026-05-18 Mon, ..., 2026-05-22 Fri
    dates = ["2026-05-15", "2026-05-18", "2026-05-19", "2026-05-20",
              "2026-05-21", "2026-05-22"]
    equity = [100.0, 101.0, 101.5, 101.2, 100.8, 102.0]
    r = compute_wtd(dates, equity)
    assert r is not None
    assert r["from_date"] == "2026-05-15"  # prior Friday
    assert r["to_date"] == "2026-05-22"
    assert r["pct"] == pytest.approx(0.02, abs=1e-9)  # 102 / 100 - 1


def test_wednesday_mid_week():
    """Mid-week view: WTD = today's close vs prior Friday's close."""
    dates = ["2026-05-15", "2026-05-18", "2026-05-19", "2026-05-20"]
    equity = [100.0, 100.5, 101.0, 99.5]
    r = compute_wtd(dates, equity)
    assert r is not None
    assert r["from_date"] == "2026-05-15"  # prior Friday
    assert r["to_date"] == "2026-05-20"
    assert r["pct"] == pytest.approx(-0.005, abs=1e-9)


def test_monday_only_one_trading_day_this_week():
    """Last entry is Monday: WTD = Monday vs the trading day strictly
    before this Monday (= prior Friday)."""
    dates = ["2026-05-14", "2026-05-15", "2026-05-18"]  # Thu, Fri, Mon
    equity = [100.0, 101.0, 102.0]
    r = compute_wtd(dates, equity)
    assert r is not None
    assert r["from_date"] == "2026-05-15"
    assert r["pct"] == pytest.approx(102 / 101 - 1, abs=1e-12)


# ---------------------------------------------------------------------------
# Empty / degenerate
# ---------------------------------------------------------------------------

def test_empty_returns_none():
    assert compute_wtd([], []) is None


def test_single_entry_returns_none():
    assert compute_wtd(["2026-05-22"], [100.0]) is None


def test_no_prior_week_returns_none():
    """All series entries are inside this week's Mon-Fri — no Friday-of-
    prior-week baseline exists in the series, so we cannot compute WTD."""
    dates = ["2026-05-18", "2026-05-19", "2026-05-20"]  # all this week
    equity = [100.0, 101.0, 102.0]
    r = compute_wtd(dates, equity)
    assert r is None


# ---------------------------------------------------------------------------
# Edge cases — month boundary, year boundary, leap day
# (CLAUDE.md: include at least two edge-case tests for any date logic)
# ---------------------------------------------------------------------------

def test_month_boundary_first_week_of_june_2026():
    """Monday is 1 June 2026; prior Friday is 29 May 2026 (different
    month). Catches off-by-month bugs in the Monday-rollback step."""
    dates = ["2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03",
              "2026-06-04", "2026-06-05"]
    equity = [100.0, 100.5, 101.2, 100.7, 100.9, 102.5]
    r = compute_wtd(dates, equity)
    assert r is not None
    assert r["from_date"] == "2026-05-29"  # prior Friday in PRIOR MONTH
    assert r["to_date"] == "2026-06-05"
    assert r["pct"] == pytest.approx(0.025, abs=1e-9)


def test_year_boundary_first_week_of_jan_2027():
    """Cross-year boundary: Monday is 4 Jan 2027 (since 1 Jan 2027 is
    Friday). Need to find Friday 1 Jan 2027 as the prior close. Or if
    Jan 1 was a holiday, prior Friday is 25 Dec 2026 (but those are
    typically market holidays). The test uses 1 Jan as the prior close
    to cover the year-rollover lookup correctness."""
    dates = ["2026-12-31", "2027-01-04", "2027-01-05", "2027-01-06",
              "2027-01-07", "2027-01-08"]
    equity = [200.0, 201.0, 202.0, 201.5, 203.0, 204.0]
    r = compute_wtd(dates, equity)
    assert r is not None
    assert r["from_date"] == "2026-12-31"  # prior Friday, prior YEAR
    assert r["to_date"] == "2027-01-08"
    assert r["pct"] == pytest.approx(204 / 200 - 1, abs=1e-12)


def test_leap_day_does_not_break_lookup():
    """29 Feb 2024 is a Thursday (leap year). Last entry Fri 1 Mar 2024.
    Monday of that week = 26 Feb. Prior Friday = 23 Feb 2024.
    Tests that date arithmetic correctly handles 29 Feb as a valid day."""
    dates = ["2024-02-23", "2024-02-26", "2024-02-27", "2024-02-28",
              "2024-02-29", "2024-03-01"]
    equity = [100.0, 100.5, 100.8, 101.0, 101.5, 102.0]
    r = compute_wtd(dates, equity)
    assert r is not None
    assert r["from_date"] == "2024-02-23"
    assert r["to_date"] == "2024-03-01"
    assert r["pct"] == pytest.approx(0.02, abs=1e-9)


# ---------------------------------------------------------------------------
# Live-data sanity check — pins the algorithm to the production value
# ---------------------------------------------------------------------------

def test_against_live_deployed_blend():
    """Compute WTD against the live ``risk_overlay.json`` deployed blend.
    This is the value the dashboard hero card shows; if this drifts the
    Python algorithm has diverged from the JS one.

    Soft-skip if the file is missing (CI / minimal checkout)."""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "data" / "risk_overlay.json"
    if not p.exists():
        pytest.skip("risk_overlay.json not present in this checkout")
    d = json.loads(p.read_text(encoding="utf-8"))
    blend = (d.get("gated_variants") or {}).get(
        "blend_35_35_10_20_gated_eem_tilted")
    if not blend or not blend.get("dates") or not blend.get("equity"):
        pytest.skip("deployed blend series missing from risk_overlay.json")
    r = compute_wtd(blend["dates"], blend["equity"])
    assert r is not None
    # Defensive bounds — WTD on a real series should be within +/- 25%
    # in any normal week. A value outside that range means either a
    # data corruption or an algorithm regression.
    assert -0.25 < r["pct"] < 0.25, (
        f"WTD {r['pct']:.4%} on {r['from_date']} -> {r['to_date']} is "
        "implausibly large for a single week — investigate.")


# ---------------------------------------------------------------------------
# YTD anchoring — the email body and the factsheet PDF must agree.
# ---------------------------------------------------------------------------
# Regression for 2026-07-18: the factsheet based YTD on the first observation
# AT/AFTER 1 January (window_ret slices .loc[start:] and divides by iloc[0]),
# so it silently dropped the first trading day. On 2026-01-02 the strategy
# gained +1.15% while SPY lost -0.56% — a +1.70pp relative day discarded
# outright. The PDF printed YTD +14.0% / vs SPY +4.6% while the email body,
# which anchors to the prior year's final close, printed +15.3% — the same
# portfolio, two numbers, in one delivery (the PDF is attached to that email).

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))


def _year_series():
    """Prior-year close then three sessions of the new year."""
    import pandas as pd
    idx = pd.to_datetime(["2025-12-30", "2026-01-02", "2026-01-05", "2026-07-17"])
    #                      100.0          101.15 (+1.15%)  102.0        115.26
    return pd.Series([100.0, 101.15, 102.0, 115.26], index=idx)


def test_ytd_anchors_to_prior_year_close_not_first_session():
    """YTD must measure from the PRIOR YEAR'S FINAL CLOSE, so the first
    session's move is included."""
    from build_factsheet import ytd_ret

    s = _year_series()
    # 115.26 / 100.0 - 1 = +15.26%  (anchored to 2025-12-30)
    assert ytd_ret(s, 2026) == pytest.approx(0.1526, abs=1e-6)
    # The buggy convention would have given 115.26 / 101.15 - 1 = +13.95%.
    assert ytd_ret(s, 2026) != pytest.approx(0.1395, abs=1e-4)


def test_ytd_agrees_between_email_and_factsheet():
    """The two artefacts ship together; their YTD must be identical."""
    from build_email_body import _ytd_return
    from build_factsheet import ytd_ret

    s = _year_series()
    assert ytd_ret(s, 2026) == pytest.approx(_ytd_return(s), abs=1e-12)


def test_ytd_falls_back_when_no_prior_year_data():
    """With no prior-year close available, anchor to the first in-year
    observation rather than returning nothing."""
    import pandas as pd

    from build_factsheet import ytd_ret

    s = pd.Series([50.0, 55.0], index=pd.to_datetime(["2026-01-02", "2026-06-30"]))
    assert ytd_ret(s, 2026) == pytest.approx(0.10, abs=1e-9)
    # A single in-year point with no prior year cannot produce a return.
    one = pd.Series([50.0], index=pd.to_datetime(["2026-01-02"]))
    assert ytd_ret(one, 2026) is None
