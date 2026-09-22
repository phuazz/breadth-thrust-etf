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
# probe_etf — the "never raises" contract
# ---------------------------------------------------------------------------
def _roster_payload(as_of: str, venues: list[str]) -> dict:
    """A holdings payload whose equity rows sit on the given venues."""
    n = len(venues)
    return {"componentsByNameMap": {"holdings": {"containersByNameMap": {
        "all": {"dataPointsByNameMap": {
            "ticker": {"value": [f"T{i}" for i in range(n)]},
            "assetClass": {"value": ["Equity"] * n},
            "exchange": {"value": venues},
            "asOfDate": {"value": as_of},
        }}}}}}


def test_probe_etf_records_an_unmapped_venue_instead_of_raising(monkeypatch):
    """A roster the breadth guard would refuse is still an observation.

    On 2026-09-22 four Greek banks entered EXV1 on "Athens Exchange S.A.
    Cash Market", which the venue map did not carry. The resulting
    UnmappedExchangeError escaped probe_etf, and the scheduled run recorded
    nothing for ANY ETF — the state this function's docstring says cannot
    happen. The probe counts published rows; it does not compute breadth,
    so an unrecognised venue must not cost it the window.
    """
    target = date(2026, 9, 18)
    # 4 of 8 rows on a venue no map carries: 50%, far past the 2% bound.
    venues = ["Xetra"] * 4 + ["Nowhere Exchange Cash Market"] * 4
    payload = _roster_payload(target.strftime("%Y%m%d"), venues)

    monkeypatch.setattr(mpl, "resolve_target",
                        lambda sym: {"symbol": sym, "ishares_region": "uk",
                                     "apply_exchange_suffix": True})
    monkeypatch.setattr(mpl, "fetch_product_data", lambda d, cfg: payload)

    row = mpl.probe_etf("EXV1", [target])

    assert row["etf"] == "EXV1"
    assert row["latest_with_data"] == target.isoformat()
    # All 8 published rows are counted, including the 4 on the venue the
    # breadth path would refuse: the probe measures what was published.
    assert row["n_tickers_latest"] == 8
    assert row["errors"] == {}


# ---------------------------------------------------------------------------
# cross_check_mismatch
# ---------------------------------------------------------------------------
def test_cross_check_agreement_and_disagreement():
    assert mpl.cross_check_mismatch(date(2026, 8, 7), "2026-08-07") is False
    assert mpl.cross_check_mismatch(date(2026, 8, 6), "2026-08-07") is True


def test_cross_check_is_lenient_when_either_side_is_missing():
    assert mpl.cross_check_mismatch(None, "2026-08-07") is False
    assert mpl.cross_check_mismatch(date(2026, 8, 7), None) is False
