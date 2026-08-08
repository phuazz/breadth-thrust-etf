"""Tests for scripts/measure_publication_lag.py — the pure logic only.

The network probe itself (fetch_product_data) is fetch_constituents'
responsibility and is not exercised here; these tests cover the window
construction, the summarisation, the payload-echo extraction (against a
synthetic payload with the real contract shape) and the cross-check.

Date edge cases follow the CLAUDE.md rule: one month boundary, one year
boundary, both computed with datetime.timedelta rather than by hand.
Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import measure_publication_lag as mpl  # noqa: E402
from fetch_constituents import PayloadContractError  # noqa: E402


# ---------------------------------------------------------------------------
# probe_window
# ---------------------------------------------------------------------------
def test_probe_window_is_inclusive_oldest_first():
    win = mpl.probe_window(date(2026, 8, 8), 3)
    assert win == [date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 8)]


def test_probe_window_month_boundary():
    # 2026-08-02 minus 4 days crosses into July.
    win = mpl.probe_window(date(2026, 8, 2), 5)
    assert win[0] == date(2026, 7, 29)
    assert win[-1] == date(2026, 8, 2)
    assert all(b - a == timedelta(days=1) for a, b in zip(win, win[1:]))


def test_probe_window_year_boundary():
    # 2027-01-02 minus 5 days crosses into December 2026.
    win = mpl.probe_window(date(2027, 1, 2), 6)
    assert win[0] == date(2026, 12, 28)
    assert win[-1] == date(2027, 1, 2)
    assert len(win) == 6


def test_probe_window_rejects_non_positive_days():
    with pytest.raises(ValueError):
        mpl.probe_window(date(2026, 8, 8), 0)


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------
def test_summarise_picks_most_recent_date_with_data():
    probed = [date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7),
              date(2026, 8, 8)]
    has = {date(2026, 8, 5): True, date(2026, 8, 6): True,
           date(2026, 8, 7): False, date(2026, 8, 8): False}
    with_data, latest = mpl.summarise(probed, has)
    assert with_data == [date(2026, 8, 5), date(2026, 8, 6)]
    assert latest == date(2026, 8, 6)


def test_summarise_all_empty_is_none():
    probed = [date(2026, 8, 8)]
    with_data, latest = mpl.summarise(probed, {})
    assert with_data == []
    assert latest is None


# ---------------------------------------------------------------------------
# echoed_iso — against the real payload contract shape
# ---------------------------------------------------------------------------
def _payload(as_of):
    return {"componentsByNameMap": {"holdings": {"containersByNameMap": {
        "all": {"dataPointsByNameMap": {
            "ticker": {"value": None},
            "assetClass": {"value": None},
            "asOfDate": {"value": as_of},
        }}}}}}


def test_echoed_iso_normalises_to_iso():
    assert mpl.echoed_iso(_payload("20260807")) == "2026-08-07"


def test_echoed_iso_null_is_none():
    assert mpl.echoed_iso(_payload(None)) is None


def test_echoed_iso_raises_on_contract_drift():
    # A reshaped payload must raise (via _holdings_datapoints), never be
    # silently misread as "no data".
    with pytest.raises(PayloadContractError):
        mpl.echoed_iso({"componentsByNameMap": {}})


# ---------------------------------------------------------------------------
# cross_check_mismatch
# ---------------------------------------------------------------------------
def test_cross_check_agreement_and_disagreement():
    assert mpl.cross_check_mismatch(date(2026, 8, 7), "2026-08-07") is False
    assert mpl.cross_check_mismatch(date(2026, 8, 6), "2026-08-07") is True


def test_cross_check_is_lenient_when_either_side_is_missing():
    assert mpl.cross_check_mismatch(None, "2026-08-07") is False
    assert mpl.cross_check_mismatch(date(2026, 8, 7), None) is False
