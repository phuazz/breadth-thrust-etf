"""A sleeve that rebalances and holds must not read as stale.

Regression guard for the 2026-08-15 false positive: the dashboard hero and
the factsheet provenance table both printed ``trade_history[-1]["date"]``
— the last date HOLDINGS CHANGED — under a "last rebalance" label, and the
staleness banner aged that. Strategy C, which had rebalanced 8 days
earlier and produced no trades, showed 2026-07-31, aged at 15 days, and
tripped the >14-day amber banner on the deployed page while A/B/D sat at
8 days.

The central case here is `test_sleeve_that_rebalances_without_trading_is_not_stale`.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from sleeve_asof import (  # noqa: E402
    STALE_AFTER_DAYS,
    age_days,
    is_stale,
    last_rebalance,
    last_traded,
)

TEMPLATE = ROOT / "template.html"

# The shape that produced the incident: the grid ran on 2026-08-07 with
# every other sleeve, the rerank returned the same book, so trade_history
# still ends 2026-07-31.
HELD = {
    "headline": {
        "weekly_allocation_dates": ["2026-07-17", "2026-07-24",
                                     "2026-07-31", "2026-08-07"],
        "trade_history": [{"date": "2026-07-24", "holdings": []},
                           {"date": "2026-07-31", "holdings": []}],
    }
}

# A sleeve that traded on the most recent grid date — the two dates agree.
TRADED = {
    "headline": {
        "weekly_allocation_dates": ["2026-07-31", "2026-08-07"],
        "trade_history": [{"date": "2026-07-31", "holdings": []},
                           {"date": "2026-08-07", "holdings": []}],
    }
}

AS_AT = date(2026, 8, 15)   # the Saturday the false positive was observed


# --- the defect ------------------------------------------------------------

def test_sleeve_that_rebalances_without_trading_is_not_stale():
    """THE regression. Grid ran 2026-08-07, no trades since 2026-07-31.
    Read from the grid the sleeve is 8 days old and fresh; read from the
    trade history it is 15 days old and trips the banner."""
    assert last_rebalance(HELD) == "2026-08-07"
    assert age_days(last_rebalance(HELD), AS_AT) == 8
    assert not is_stale(HELD, AS_AT)

    # And the quantity that USED to drive it would indeed have warned —
    # so this test fails if anyone routes the banner back through it.
    assert age_days(last_traded(HELD), AS_AT) == 15
    assert age_days(last_traded(HELD), AS_AT) > STALE_AFTER_DAYS


def test_last_traded_stays_available_and_distinct():
    """Still exposed, under its own name, for the UI field that wants it."""
    assert last_traded(HELD) == "2026-07-31"
    assert last_traded(HELD) != last_rebalance(HELD)
    assert last_traded(TRADED) == last_rebalance(TRADED) == "2026-08-07"


def test_a_genuinely_stalled_sleeve_still_warns():
    """The banner must keep firing on the case it exists for: the grid
    itself stopped running. Guards against 'fix' by suppression."""
    stalled = {"headline": {
        "weekly_allocation_dates": ["2026-07-10", "2026-07-17"],
        "trade_history": [{"date": "2026-07-17", "holdings": []}],
    }}
    assert age_days(last_rebalance(stalled), AS_AT) == 29
    assert is_stale(stalled, AS_AT)


def test_threshold_boundary_is_strictly_greater_than():
    """14 days is fresh, 15 is stale — the exact edge C sat on."""
    at_14 = {"headline": {"weekly_allocation_dates": ["2026-08-01"]}}
    at_15 = {"headline": {"weekly_allocation_dates": ["2026-07-31"]}}
    assert age_days("2026-08-01", AS_AT) == STALE_AFTER_DAYS
    assert not is_stale(at_14, AS_AT)
    assert is_stale(at_15, AS_AT)


# --- edge cases ------------------------------------------------------------

def test_month_and_year_boundaries():
    """Date arithmetic across a month end and a year end (house rule:
    two edge-case tests minimum on any date logic)."""
    # Month boundary: 2026-01-31 -> 2026-02-03.
    assert age_days("2026-01-31", date(2026, 2, 3)) == 3
    # Year boundary, across a leap day too: 2027-12-30 -> 2028-01-02.
    assert age_days("2027-12-30", date(2028, 1, 2)) == 3
    assert age_days("2028-02-28", date(2028, 3, 1)) == 2   # 2028 is a leap year


def test_missing_grid_falls_back_to_the_trade_date():
    """Pre-Phase-10.1 sleeve JSONs carry no weekly_allocation_dates. The
    fallback restores the old reading rather than reporting no date."""
    legacy = {"headline": {"trade_history": [{"date": "2026-08-07"}]}}
    assert last_rebalance(legacy) == "2026-08-07"


def test_absent_or_empty_sleeve_resolves_to_none_and_is_not_stale():
    for blob in (None, {}, {"headline": {}},
                 {"headline": {"weekly_allocation_dates": [],
                                "trade_history": []}}):
        assert last_rebalance(blob) is None
        assert last_traded(blob) is None
        assert age_days(last_rebalance(blob), AS_AT) is None
        assert not is_stale(blob, AS_AT)


# --- the live data ---------------------------------------------------------

def test_live_sleeve_jsons_agree_with_the_grid_contract():
    """Every trade date is a grid date, and the grid is sorted. If an
    engine ever emitted a trade outside its own rebalance grid, reading
    the grid for freshness would be unsound."""
    import json
    files = {"a": "topk_robustness.json", "b": "asset_class_rotation.json",
             "c": "thematic_rotation.json", "d": "europe_rotation.json"}
    checked = 0
    for key, name in files.items():
        path = ROOT / "data" / name
        if not path.exists():
            continue
        blob = json.loads(path.read_text(encoding="utf-8"))
        grid = blob["headline"].get("weekly_allocation_dates") or []
        if not grid:
            continue
        trades = {t["date"] for t in blob["headline"].get("trade_history") or []}
        assert grid == sorted(grid), f"sleeve {key}: grid not sorted"
        assert trades <= set(grid), (
            f"sleeve {key}: trade dates outside the rebalance grid: "
            f"{sorted(trades - set(grid))[:5]}")
        assert last_rebalance(blob) >= (last_traded(blob) or ""), (
            f"sleeve {key}: last trade is AFTER the last grid date")
        checked += 1
    if not checked:
        pytest.skip("no sleeve JSONs present in data/")


# --- the JS port -----------------------------------------------------------

def _js(pattern: str, label: str) -> re.Match:
    html = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(pattern, html, re.DOTALL)
    assert m, f"{label} not found in template.html"
    return m


def test_dashboard_js_reads_the_grid_not_the_trade_history():
    """_lastRebalance must resolve from weekly_allocation_dates. A revert
    to trade_history reinstates the false positive silently — the page
    still renders a plausible date."""
    body = _js(r"function _lastRebalance\(sleeve\)\s*\{(.*?)\n\}",
               "_lastRebalance").group(1)
    assert "weekly_allocation_dates" in body
    assert "_lastTraded" in body, "the legacy fallback path is missing"


def test_dashboard_staleness_threshold_matches_python():
    """Pin the JS literal to STALE_AFTER_DAYS so a change to one side
    fails rather than drifting the dashboard away from the factsheet."""
    m = _js(r"const SLEEVE_STALE_AFTER_DAYS\s*=\s*(\d+)\s*;",
            "SLEEVE_STALE_AFTER_DAYS")
    assert int(m.group(1)) == STALE_AFTER_DAYS


def test_dashboard_ages_the_rebalance_date_not_the_trade_date():
    """The banner's inputs are the *Date consts, and those must be fed by
    _lastRebalance."""
    html = TEMPLATE.read_text(encoding="utf-8")
    for sleeve, src in (("aDate", "topk"), ("bDate", "ac"),
                         ("cDate", "tc"), ("dDate", "eu")):
        assert re.search(rf"const {sleeve} = .*_lastRebalance\({src}\)", html), (
            f"{sleeve} is no longer derived from _lastRebalance({src})")
    assert re.search(r"const aAge = _ageDays\(aDate\)", html), \
        "the staleness age is no longer computed from the rebalance date"


def test_js_age_computation_is_utc_on_both_sides():
    """A UTC-parsed ISO date compared against a local-midnight `today`
    shifts the rounded age by a day in either direction, which decides a
    14-day cutoff. Both sides must be Date.UTC."""
    body = _js(r"function _ageDays\(iso\)\s*\{(.*?)\n\}", "_ageDays").group(1)
    assert "getUTCFullYear" in body and "Date.UTC" in body
    assert "new Date(iso)" not in body


def test_every_surface_that_says_rebalance_reads_the_grid():
    """The four "current selection" headers and the "held by" tooltip all
    printed ``trade_history[-1].date`` beside the word "rebalance" — the
    Strategy C panel read "as of the 2026-07-31 rebalance" on a book that
    was current as at 2026-08-07. Each must resolve through
    _lastRebalance."""
    html = TEMPLATE.read_text(encoding="utf-8")
    for label in ("up to K=${K} above the mean",
                  "top K=${K} above the +5% floor",
                  "top K=${K} by breadth",
                  "top K=${K} by trend"):
        line = next((ln for ln in html.splitlines() if label in ln), None)
        assert line, f"the selection header for {label!r} has moved"
        assert "rebalance" in line
        assert "_lastRebalance(" in line, (
            f"selection header for {label!r} still dates itself off "
            f"trade_history: {line.strip()[:120]}")

    body = _js(r"function _momRebalDate\(root\)\s*\{(.*?)\n\}",
               "_momRebalDate").group(1)
    assert "_lastRebalance(" in body and "trade_history" not in body


def test_no_surface_calls_a_trade_date_a_rebalance():
    """The label bug, not just the arithmetic: 'last rebalanced' beside a
    date drawn from trade activity is what made a healthy sleeve read as
    broken. Guards the wording across all four renderers."""
    for path in (TEMPLATE,
                 ROOT / "scripts" / "build_email_body.py",
                 ROOT / "scripts" / "build_factsheet.py"):
        text = path.read_text(encoding="utf-8")
        # Allow the phrase in comments explaining the fix; reject it in
        # anything that reaches a reader.
        emitted = [ln for ln in text.splitlines()
                   if "last rebalanced" in ln
                   and not ln.lstrip().startswith(("#", "//", "*"))]
        assert not emitted, (
            f"{path.name} still prints 'last rebalanced' beside a trade "
            f"date: {emitted}")
