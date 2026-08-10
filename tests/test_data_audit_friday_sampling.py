"""Guard for build_data_audit._sample_at_fridays.

The Data tab used to sample the breadth series at LITERAL Fridays, so every
market-holiday Friday showed a roster with em-dashes across all three breadth
columns. That reads as missing data when the truth is a closed market — and it
disagreed with the engines, which since 2026-08-10 rebalance a shut Friday on
the Thursday close (WS10, rebalance_calendar.HOLIDAY_AWARE).

The distinction these tests pin is the one that matters: a Friday the exchange
did not trade gets the prior session's reading and a substitution flag, while a
Friday the exchange DID trade but the vendor never priced gets nothing at all.
Collapsing the two would present a data gap as a closed market.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from build_data_audit import _sample_at_fridays  # noqa: E402


def test_open_friday_uses_its_own_session():
    dates = ["2026-06-19", "2026-06-22", "2026-06-26"]
    idx, fri, sub = _sample_at_fridays(dates, ["2026-06-26"], "NYSE")
    assert idx == [2] and fri == ["2026-06-26"] and sub == [0]


def test_holiday_friday_falls_back_and_is_flagged():
    """2026-07-03 is Independence Day observed — NYSE shut, so the reading is
    Thursday's and the row says so."""
    dates = ["2026-07-01", "2026-07-02", "2026-07-06"]
    idx, fri, sub = _sample_at_fridays(dates, ["2026-07-03"], "NYSE")
    assert fri == ["2026-07-03"], "row must stay keyed to the rebalance Friday"
    assert dates[idx[0]] == "2026-07-02", "must read the last prior session"
    assert sub == [1], "substitution must be visible to the reader"


def test_vendor_gap_emits_no_row():
    """2026-06-26 is an ordinary NYSE session. A panel missing that bar has a
    DATA problem, and must not be dressed up as a holiday fallback."""
    dates = ["2026-06-24", "2026-06-25", "2026-07-02"]
    idx, fri, sub = _sample_at_fridays(dates, ["2026-06-26"], "NYSE")
    assert idx == [] and fri == [] and sub == []


def test_holiday_and_gap_are_distinguished_in_one_pass():
    dates = ["2026-06-24", "2026-06-25", "2026-07-01", "2026-07-02"]
    #        2026-06-26 traded but unpriced -> gap; 2026-07-03 shut -> fallback
    idx, fri, sub = _sample_at_fridays(dates, ["2026-06-26", "2026-07-03"], "NYSE")
    assert fri == ["2026-07-03"] and sub == [1]
    assert dates[idx[0]] == "2026-07-02"


def test_calendar_is_per_panel_not_global():
    """Europe trades on XETR: 2026-05-01 is shut there and open in New York,
    2026-07-03 the reverse. Judging a Europe panel against the US calendar
    would substitute the wrong weeks."""
    dates = ["2026-04-29", "2026-04-30", "2026-07-02", "2026-07-03"]
    _, fri_de, sub_de = _sample_at_fridays(dates, ["2026-05-01"], "XETR")
    assert fri_de == ["2026-05-01"] and sub_de == [1], "May Day is shut on XETR"
    # The same Friday is an ordinary NYSE session, so for a US panel a missing
    # bar there is a gap, not a holiday.
    idx_us, fri_us, _ = _sample_at_fridays(dates, ["2026-05-01"], "NYSE")
    assert fri_us == [], "May Day trades in New York — absence is a vendor gap"
    # And July 3 is a normal XETR session but a US holiday.
    _, fri2_de, _ = _sample_at_fridays(
        ["2026-07-01", "2026-07-02"], ["2026-07-03"], "XETR")
    assert fri2_de == [], "XETR traded 2026-07-03 — absence is a vendor gap"


def test_month_and_year_boundary():
    # CLAUDE.md date rule: one month boundary, one year boundary. New Year's
    # Day 2027 is a Friday and an NYSE holiday.
    idx, fri, sub = _sample_at_fridays(
        ["2026-12-30", "2026-12-31", "2027-01-04"], ["2027-01-01"], "NYSE")
    assert fri == ["2027-01-01"] and sub == [1]
    assert ["2026-12-30", "2026-12-31", "2027-01-04"][idx[0]] == "2026-12-31"
    # Month boundary: 2026-07-31 is an ordinary Friday and must not substitute.
    idx2, fri2, sub2 = _sample_at_fridays(
        ["2026-07-30", "2026-07-31"], ["2026-07-31"], "NYSE")
    assert fri2 == ["2026-07-31"] and sub2 == [0]


def test_empty_inputs_do_not_raise():
    assert _sample_at_fridays([], ["2026-07-03"], "NYSE") == ([], [], [])
    assert _sample_at_fridays(["2026-07-02"], [], "NYSE") == ([], [], [])


def test_committed_payload_has_no_unexplained_holes():
    """End-to-end on the real file: every roster Friday either carries a
    breadth row or is a genuine vendor gap. Before this change CNDX had 16
    unexplained holes."""
    import json
    root = Path(__file__).resolve().parent.parent
    path = root / "docs" / "data_audit.json"
    if not path.exists():
        import pytest
        pytest.skip("docs/data_audit.json not built")
    payload = json.loads(path.read_text(encoding="utf-8"))
    br = payload["breadth"].get("CNDX")
    ros = payload["roster"].get("CNDX")
    if not br or not ros or "friday" not in br:
        import pytest
        pytest.skip("CNDX panel or the friday key is absent")
    missing = set(ros["friday"]) - set(br["friday"])
    assert not missing, f"roster Fridays with no breadth row: {sorted(missing)[:5]}"
    assert sum(br["substituted"]) > 0, "no holiday Fridays flagged — sampling regressed"
