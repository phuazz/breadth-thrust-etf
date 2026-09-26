"""Tests for the holdings monitor guard's fund-specific G7 contract."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from check_holdings_monitor_guard import (  # noqa: E402
    flow_turnover_check,
)


def _fund():
    return {
        "flow_basis": "2026-09-17",
        "fund_flow_pct": -9.305,
        "rows": [{"fs": "added"}] * 70 + [{"fs": "trimmed"}] * 71
                 + [{"fs": "new"}] * 14 + [{"fs": "held"}] * 2,
        "exits": [],
    }


def test_index_fund_turnover_warns_without_blocking():
    status, detail = flow_turnover_check("XBI", {"active": False}, _fund())

    assert status == "WARN"
    assert "mechanical rebalancing" in detail


def test_active_fund_turnover_still_blocks():
    status, detail = flow_turnover_check("ARKG", {"active": True}, _fund())

    assert status == "FAIL"
    assert "cap 50%" in detail
