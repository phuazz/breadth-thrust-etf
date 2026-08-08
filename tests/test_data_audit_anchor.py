"""Anchor selection for the Data tab's constituent table.

The table pairs TODAY's roster with the moving-average state at an anchor
date, so a stale anchor silently prices the current roster at an old date.
That is what happened to ITWN: the original rule demanded 90% of the roster
have a usable average, ITWN runs at 89.7% (70 of 78 Taiwanese listings,
8 without yfinance history), and it anchored to 2026-05-28 — two and a half
months back — producing a 2.2pp discrepancy against the panel's own number.

The rule is now relative to the panel's own recent coverage rather than to
its roster size, for the same reason MIN_BREADTH_NAMES is an absolute count
rather than a share: what makes a bar untrustworthy is a break from the
panel's norm, not its ratio to a roster that may hold structurally
unpriceable names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_audit import _reference_date  # noqa: E402

DATA = ROOT / "data"


def _series(n_with_ma50, ma_breadth=None, dates=None):
    n = len(n_with_ma50)
    return {"series": {
        "dates": dates or [f"2026-01-{i + 1:02d}" for i in range(n)],
        "n_with_ma50": n_with_ma50,
        "n_constituents": [max(n_with_ma50) or 1] * n,
        "ma_breadth": ma_breadth if ma_breadth is not None else [0.5] * n,
    }}


def test_steady_sub_90_percent_coverage_still_anchors_at_the_latest_bar():
    """The ITWN case. Coverage steady at 70 of 78 is a healthy panel, not a
    reason to reach two months back for an anchor."""
    b = _series([70] * 30)
    assert _reference_date(b) == b["series"]["dates"][-1]


def test_collapsed_coverage_is_skipped():
    """The case the anchor exists for: the vendor has not published, so the
    final bar rests on a fraction of the usual names."""
    b = _series([26] * 25 + [2])
    assert _reference_date(b) == b["series"]["dates"][-2]


def test_null_breadth_bar_is_skipped():
    """compute_breadth nulls anything under MIN_BREADTH_NAMES; a bar with no
    value cannot be an anchor even if its count looks plausible."""
    b = _series([26] * 26, ma_breadth=[0.5] * 25 + [None])
    assert _reference_date(b) == b["series"]["dates"][-2]


def test_structurally_low_coverage_panel_still_anchors():
    """IDP6 carries ~332 unpriced constituents out of ~603 as normal
    operation. A share-of-roster rule would never anchor it at all."""
    b = {"series": {
        "dates": [f"2026-02-{i + 1:02d}" for i in range(25)],
        "n_with_ma50": [271] * 25,
        "n_constituents": [603] * 25,
        "ma_breadth": [0.5] * 25,
    }}
    assert _reference_date(b) == "2026-02-25"


def test_gradual_drift_is_not_treated_as_a_collapse():
    """Coverage easing from 80 to 70 over a month is attrition, not an
    outage, and must not push the anchor backwards."""
    b = _series(list(range(80, 70, -1)) + [70] * 15)
    assert _reference_date(b) == b["series"]["dates"][-1]


def test_returns_none_when_nothing_qualifies():
    assert _reference_date({"series": {}}) is None
    assert _reference_date({}) is None


@pytest.mark.parametrize("panel", sorted(p.stem.replace("breadth_", "").upper()
                                          for p in DATA.glob("breadth_*.json")))
def test_every_built_panel_anchors_to_its_own_last_valid_bar(panel):
    """End-to-end on real panels: the anchor must never be older than the
    last bar the panel itself published a breadth value for, unless that bar
    is a coverage collapse. Guards the class of regression where a threshold
    tweak silently ages every anchor."""
    p = DATA / f"breadth_{panel.lower()}.json"
    b = json.loads(p.read_text(encoding="utf-8"))
    ref = _reference_date(b)
    ser = b.get("series") or {}
    dates, ma = ser.get("dates") or [], ser.get("ma_breadth") or []
    last_valued = next((dates[i] for i in range(len(ma) - 1, -1, -1)
                        if ma[i] is not None), None)
    if last_valued is None:
        pytest.skip(f"{panel} has no populated bars")
    assert ref is not None, f"{panel} produced no anchor"
    # Within a fortnight of the panel's newest real bar. A larger gap means
    # the rule is rejecting healthy sessions, which is the ITWN failure.
    assert ref >= dates[max(0, dates.index(last_valued) - 10)], (
        f"{panel} anchored to {ref} but its last valued bar is {last_valued}"
    )
