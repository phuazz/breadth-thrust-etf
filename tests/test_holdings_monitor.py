"""Tests for the theme-constituent monitor (holdings_sources + run_holdings_monitor).

No live HTTP: rosters are built in-process and prices are synthetic, so
these run in milliseconds and do not break when an issuer's CDN is slow.

The load-bearing test here is ``test_pure_price_move_produces_no_flow``.
The whole point of the active-weight decomposition is that a price move
must NOT register as trading; if that assertion ever goes red, the flow
column is reporting the market as if it were the manager, which is the
exact failure the decomposition exists to prevent and which a naive
weight difference would produce silently.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from holdings_sources import (  # noqa: E402
    Holding, MONITOR_FUNDS, RosterSnapshot, normalise_ticker,
)
import run_holdings_monitor as rhm  # noqa: E402


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("AAPL", "AAPL"),
    ("  msft ", "MSFT"),
    ("BRK.B", "BRK-B"),      # documented vendor convention, not a guess
    ("TXG", "TXG"),
])
def test_normalise_accepts_real_symbols(raw, expect):
    sym, reason = normalise_ticker(raw)
    assert (sym, reason) == (expect, None)


@pytest.mark.parametrize("raw", ["-", "", "CASH", "N/A", None, "nan"])
def test_normalise_drops_non_equity_rows(raw):
    sym, reason = normalise_ticker(raw)
    assert sym is None and reason


def test_normalise_rejects_bloomberg_composite_by_default():
    """A venue-coded symbol must not be pattern-stripped into a guess."""
    sym, reason = normalise_ticker("ARCT UQ")
    assert sym is None
    assert "composite" in reason


def test_normalise_rejects_issuer_placeholder_identifiers():
    """SSGA's unsettled-position rows start with a digit and price nowhere."""
    for raw in ("2200963D", "2200964D"):
        sym, reason = normalise_ticker(raw)
        assert sym is None and reason == "not a US equity symbol"


def test_override_admits_a_verified_composite():
    sym, reason = normalise_ticker("ARCT UQ", {"ARCT UQ": "ARCT"})
    assert (sym, reason) == ("ARCT", None)


def test_registry_overrides_are_documented_and_scoped():
    """Overrides are verified mappings; each must resolve to a plain symbol."""
    for etf, cfg in MONITOR_FUNDS.items():
        for raw, mapped in (cfg.get("ticker_overrides") or {}).items():
            assert raw.upper() == raw, f"{etf}: override key must be upper-case"
            sym, reason = normalise_ticker(mapped)
            assert sym == mapped and reason is None, (
                f"{etf}: override target {mapped!r} is not itself a valid symbol")


def test_every_registered_fund_has_a_known_adapter():
    from holdings_sources import ADAPTERS
    for etf, cfg in MONITOR_FUNDS.items():
        assert cfg["adapter"] in ADAPTERS, f"{etf} names an unknown adapter"
        assert cfg["url"].startswith("https://"), f"{etf} url must be https"
        assert isinstance(cfg["active"], bool), f"{etf} must declare active"
        lo, hi = cfg["expected_holdings"]
        assert 0 < lo < hi, f"{etf} expected_holdings band is malformed"


# ---------------------------------------------------------------------------
# Flow — the active-weight decomposition
# ---------------------------------------------------------------------------

def _snap(etf, as_of, rows):
    """rows: (ticker, weight_pct, shares)"""
    return RosterSnapshot(
        etf=etf, as_of=as_of, source="test", url="https://example.invalid",
        holdings=[Holding(ticker=t, name=t, weight_pct=w, shares=s)
                  for t, w, s in rows],
    )


def _prev(as_of, rows):
    return {"as_of": as_of.isoformat(),
            "holdings": [{"ticker": t, "weight_pct": w, "shares": s}
                         for t, w, s in rows]}


def test_pure_price_move_produces_no_flow():
    """A doubling in one name with NO trading must show zero active flow.

    Yesterday: 100 shares of A at 10 (=1000) and 100 of B at 10 (=1000),
    so 50/50. Today A trades at 20 with share counts untouched: the fund
    is now 2000/1000, i.e. 66.7/33.3. A naive weight difference would
    report +16.7pp of "buying" in A and -16.7pp of "selling" in B. The
    decomposition must report zero for both.
    """
    prev = _prev(date(2026, 8, 18), [("A", 50.0, 100), ("B", 50.0, 100)])
    now = _snap("T", date(2026, 8, 19),
                [("A", 66.667, 100), ("B", 33.333, 100)])
    flow = rhm.compute_flow(now, prev, {"A": 20.0, "B": 10.0})
    assert abs(flow["A"]["active_bp"]) < 1.0
    assert abs(flow["B"]["active_bp"]) < 1.0
    assert flow["A"]["status"] == "held"
    assert flow["B"]["status"] == "held"


def test_real_buy_is_detected_through_a_price_move():
    """Doubling the share count while the price also moves reads as a buy."""
    prev = _prev(date(2026, 8, 18), [("A", 50.0, 100), ("B", 50.0, 100)])
    # A doubles in price AND the manager doubles the position.
    now = _snap("T", date(2026, 8, 19), [("A", 80.0, 200), ("B", 20.0, 100)])
    flow = rhm.compute_flow(now, prev, {"A": 20.0, "B": 10.0})
    assert flow["A"]["status"] == "added"
    assert flow["A"]["active_bp"] > 100      # more than 1pp of genuine adding
    assert flow["B"]["status"] == "held"
    assert flow["B"]["active_bp"] < 0        # diluted by A's purchase


def test_creations_do_not_register_as_buying():
    """A 10% creation lifts every share count and must net to zero flow."""
    prev = _prev(date(2026, 8, 18), [("A", 50.0, 100), ("B", 50.0, 100)])
    now = _snap("T", date(2026, 8, 19), [("A", 50.0, 110), ("B", 50.0, 110)])
    flow = rhm.compute_flow(now, prev, {"A": 10.0, "B": 10.0})
    assert abs(flow["A"]["active_bp"]) < 1.0
    assert abs(flow["B"]["active_bp"]) < 1.0


def test_new_and_exited_positions_are_flagged():
    prev = _prev(date(2026, 8, 18), [("A", 100.0, 100)])
    now = _snap("T", date(2026, 8, 19), [("B", 100.0, 50)])
    flow = rhm.compute_flow(now, prev, {"A": 10.0, "B": 20.0})
    assert flow["B"]["status"] == "new"
    assert flow["A"]["status"] == "exited"
    assert flow["A"]["active_bp"] < 0


def test_unpriced_name_reports_unavailable_not_zero():
    """Zero would read as 'held', which is a claim we cannot make."""
    prev = _prev(date(2026, 8, 18), [("A", 50.0, 100), ("B", 50.0, 100)])
    now = _snap("T", date(2026, 8, 19), [("A", 50.0, 100), ("B", 50.0, 100)])
    flow = rhm.compute_flow(now, prev, {"A": 10.0})     # B has no price
    assert flow["B"]["status"] == "unpriced"
    assert flow["B"]["active_bp"] is None


def test_no_previous_snapshot_yields_no_flow():
    now = _snap("T", date(2026, 8, 19), [("A", 100.0, 100)])
    assert rhm.compute_flow(now, None, {"A": 10.0}) == {}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _series(n, start=100.0, step=1.0):
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series([start + step * i for i in range(n)], index=idx)


def test_short_history_has_no_200d_state():
    """Fewer than 200 sessions means no average exists — not 'below'."""
    m = rhm.name_metrics(_series(120))
    assert m["vs_m200"] is None
    assert m["state"] is None, "a missing average must not read as bearish"
    assert m["vs_m50"] is not None


def test_rising_series_is_above_its_averages():
    m = rhm.name_metrics(_series(400))
    assert m["state"] == "above"
    assert m["vs_m200"] > 0
    assert m["range52"] == pytest.approx(1.0, abs=1e-6)   # at the 52w high
    assert m["off_high"] == pytest.approx(0.0, abs=1e-9)


def test_empty_series_is_handled():
    m = rhm.name_metrics(pd.Series(dtype=float))
    assert m["px"] is None and m["state"] is None


def test_weekly_series_moving_average_is_daily_then_sampled():
    """A 50-period average on weekly bars would be a 50-WEEK average."""
    s = _series(400)
    ws = rhm.weekly_series(s)
    assert len(ws["dates"]) <= rhm.CHART_WEEKS
    daily_ma50_last = float(s.rolling(50).mean().iloc[-1])
    assert ws["m50"][-1] == pytest.approx(daily_ma50_last, abs=0.02)


# ---------------------------------------------------------------------------
# Snapshot immutability
# ---------------------------------------------------------------------------

def test_snapshot_rewrite_with_different_content_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rhm, "SNAP_DIR", tmp_path)
    snap = _snap("T", date(2026, 8, 19), [("A", 100.0, 100)])
    p, action = rhm.write_snapshot(snap)
    assert action == "written" and p.exists()

    # Identical re-run is a no-op.
    _, action2 = rhm.write_snapshot(_snap("T", date(2026, 8, 19), [("A", 100.0, 100)]))
    assert action2 == "unchanged"

    # A restated roster must surface, never overwrite.
    with pytest.raises(rhm.MonitorError, match="restated"):
        rhm.write_snapshot(_snap("T", date(2026, 8, 19), [("A", 90.0, 90)]))


def test_previous_snapshot_picks_the_newest_earlier_date(tmp_path, monkeypatch):
    monkeypatch.setattr(rhm, "SNAP_DIR", tmp_path)
    for d in (date(2026, 8, 14), date(2026, 8, 17), date(2026, 8, 19)):
        rhm.write_snapshot(_snap("T", d, [("A", 100.0, 100)]))
    prev = rhm.previous_snapshot("T", date(2026, 8, 19))
    assert prev["as_of"] == "2026-08-17"
    assert rhm.previous_snapshot("T", date(2026, 8, 14)) is None
