"""A strict Norgate run must fail when Norgate is reachable but not serving.

2026-09-03 review finding. ``norgate_prices.available()`` answers only "is the
service up"; ``select_columns`` then returns the yfinance frame unchanged when
every symbol is unserved, and keeps a column on yfinance when Norgate is a
session behind (not a date superset). Before this guard the engine recorded
source "norgate" for a frame with no Norgate column in it — the vacuous switch
WS19 measured, in a new costume. The gate-states publisher showed the mode is
real the same day: "access denied" on a symbol at 14:12 SGT with the service
up, served again by evening.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import price_source as ps  # noqa: E402

B_LINES = ["SPY", "IJR", "QQQ", "EFA", "VGK", "EWJ", "VNQ", "GLD", "DBC",
           "TLT", "IEF", "TIP", "SHY"]
C_MIXED = ["ARKK", "XBI", "SHY", "159801.SZ", "BTC-USD"]


def test_plain_us_listing_rule():
    assert ps.plain_us_listing("SPY") and ps.plain_us_listing("SHY")
    for t in ("159801.SZ", "BTC-USD", "EXV1.DE", "EURUSD=X", "CSP1.L"):
        assert not ps.plain_us_listing(t), t


def test_a_full_take_passes():
    rep = {"replaced": list(B_LINES), "kept": [], "unresolved": []}
    assert ps.norgate_shortfall(rep, B_LINES) == {
        "kept_on_incumbent": [], "unresolved": [], "unserved": []}
    ps.assert_norgate_complete(rep, B_LINES, "Strategy B")   # no raise


def test_non_us_lines_are_never_expected_from_norgate():
    """Sleeve C's Shenzhen and crypto lines resolve to nothing at Norgate by
    design; they must not fail a strict run."""
    rep = {"replaced": ["ARKK", "XBI", "SHY"], "kept": [],
           "unresolved": ["159801.SZ", "BTC-USD"]}
    ps.assert_norgate_complete(rep, C_MIXED, "Strategy C")


def test_a_column_kept_on_the_incumbent_fails_closed():
    """Norgate a session behind at 09:00: the superset rule keeps yfinance for
    that line, and the run must say so rather than record a Norgate basis."""
    rep = {"replaced": [t for t in B_LINES if t != "SPY"], "kept": ["SPY"],
           "unresolved": []}
    with pytest.raises(RuntimeError) as exc:
        ps.assert_norgate_complete(rep, B_LINES, "Strategy B")
    msg = str(exc.value)
    assert "SPY" in msg and "12 of 13" in msg
    assert "BTE_PRICE_SOURCE=yfinance" in msg, \
        "the refusal must say how to accept the other basis explicitly"


def test_served_nothing_fails_closed():
    rep = {"replaced": [], "kept": [], "unresolved": [], "status": "ok"}
    with pytest.raises(RuntimeError) as exc:
        ps.assert_norgate_complete(rep, B_LINES, "Strategy B")
    assert "0 of 13" in str(exc.value)


def test_no_report_at_all_fails_closed():
    with pytest.raises(RuntimeError):
        ps.assert_norgate_complete(None, B_LINES, "Strategy B")


def test_reasons_are_sorted_into_buckets():
    rep = {"replaced": ["SPY"], "kept": ["QQQ"], "unresolved": ["IJR"]}
    short = ps.norgate_shortfall(rep, ["SPY", "QQQ", "IJR", "TLT", "BTC-USD"])
    assert short == {"kept_on_incumbent": ["QQQ"], "unresolved": ["IJR"],
                     "unserved": ["TLT"]}


def test_both_engines_assert_completeness_before_writing_the_cache():
    """The guard has to sit between the selection and the cache write, or a
    mixed frame is still recorded as Norgate-built."""
    for name in ("run_asset_class_rotation.py", "run_thematic_rotation.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        sel = src.index("norgate_prices.select_columns(")
        guard = src.index("assert_norgate_complete(", sel)
        write = src.index("df.to_parquet(PRICE_CACHE)", sel)
        assert sel < guard < write, name
