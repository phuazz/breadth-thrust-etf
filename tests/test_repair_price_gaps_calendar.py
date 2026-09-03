"""The repair finds a withheld SESSION, not only a withheld ticker.

REPRODUCES 2026-08-28. The vendor withheld the Friday for ten of thirteen
sleeve-B lines and for SHY. The B cache carried a row with three names priced,
which the peer rule (five priced peers) did not call a gap; the C cache, whose
calendar comes from SHY, carried no row at all, which nothing could compare.
Gap detection now reads the NYSE schedule. And a primary that has backfilled
is spliced by RETURN like a secondary -- a level copied into a drag-adjusted
or FX-converted column is the defect the module docstring warns about.

Calendar facts from pandas_market_calendars, not memory: 2026-08-28 (Fri) is
a normal NYSE session; 2026-07-03 (Fri) is a NYSE holiday.
Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import repair_price_gaps as rp  # noqa: E402
from rebalance_calendar import _exchange_sessions  # noqa: E402


def _sessions(start="2026-06-01", end="2026-09-01"):
    return pd.DatetimeIndex([pd.Timestamp(d) for d in
                             sorted(_exchange_sessions("NYSE", start, end))])


def _frame(sessions, members=("AAA", "BBB", "CCC"), seed=1):
    rng = np.random.default_rng(seed)
    data = {m: 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(sessions))))
            for m in members}
    return pd.DataFrame(data, index=sessions)


FRI = pd.Timestamp("2026-08-28")
THU = pd.Timestamp("2026-08-27")


def test_an_absent_scheduled_session_is_a_gap():
    """The C-cache shape: the row is not there at all."""
    sessions = _sessions()
    frame = _frame(sessions).drop(FRI)
    gaps = rp.find_gaps(frame, "AAA", sessions=sessions)
    assert gaps == [(FRI, THU)]


def test_a_hollow_row_with_few_priced_peers_is_a_gap_under_the_schedule():
    """The B-cache shape: three of thirteen priced. The peer rule saw nothing."""
    sessions = _sessions()
    frame = _frame(sessions, members=tuple(f"M{i}" for i in range(13)))
    frame.loc[FRI, [f"M{i}" for i in range(3, 13)]] = np.nan
    assert rp.find_gaps(frame, "M5") == [], "peer rule: fewer than five peers priced"
    assert rp.find_gaps(frame, "M5", sessions=sessions) == [(FRI, THU)]


def test_a_holiday_is_not_a_gap():
    sessions = _sessions()
    frame = _frame(sessions)
    assert pd.Timestamp("2026-07-03") not in sessions
    assert rp.find_gaps(frame, "AAA", sessions=sessions, lookback=0) == []


def test_a_run_of_two_missing_sessions_is_still_refused():
    sessions = _sessions()
    frame = _frame(sessions).drop([THU, FRI])
    assert rp.find_gaps(frame, "AAA", sessions=sessions) == []


def test_sessions_after_the_cache_end_are_the_tail_not_a_gap():
    sessions = _sessions(end="2026-09-04")
    frame = _frame(_sessions())                    # ends 2026-09-01
    assert rp.find_gaps(frame, "AAA", sessions=sessions) == []


def test_secondary_rule():
    assert rp.secondary_for("BTC-USD")["venue"] == "binance"
    assert rp.secondary_for("SHY") == {"venue": "norgate", "symbol": "SHY"}
    assert rp.secondary_for("IJR")["venue"] == "norgate"
    assert rp.secondary_for("159801.SZ") is None
    assert rp.secondary_for("EXV1.DE") is None
    assert rp.secondary_for("CNY=X") is None


def test_primary_backfill_is_spliced_by_return_never_copied(monkeypatch, tmp_path):
    """The cache sits 2% below the vendor (drag). A backfilled primary bar
    must move the cache by the vendor's RETURN, not land at the vendor's level."""
    sessions = _sessions()
    frame = _frame(sessions).drop(FRI)
    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rp, "LEDGER", tmp_path / "ledger.jsonl")   # never the tracked one
    monkeypatch.setitem(rp.CACHES, "unit", ("unit.parquet", "n/a"))
    frame.to_parquet(tmp_path / "unit.parquet")
    vendor = _frame(sessions)["AAA"] * 1.02          # complete, 2% higher basis
    monkeypatch.setattr(rp, "fetch_primary", lambda t, s, e: vendor)
    monkeypatch.setattr(rp, "fetch_secondary",
                        lambda t, s, e: (pd.Series(dtype=float), ""))
    reps = rp.repair_cache("unit", only_ticker="AAA", apply=True, sessions=sessions)
    (r,) = [x for x in reps if x["ticker"] == "AAA"]
    assert r["method"] == "return_splice" and r["source"] == "primary:yfinance"
    expected = float(frame.loc[THU, "AAA"]) * float(vendor.loc[FRI] / vendor.loc[THU])
    assert r["value"] == pytest.approx(expected)
    assert r["value"] != pytest.approx(float(vendor.loc[FRI]))
    written = pd.read_parquet(tmp_path / "unit.parquet")
    assert FRI in written.index, "an absent session must gain its row on apply"
    assert written.loc[FRI, "AAA"] == pytest.approx(expected)
    assert np.isnan(written.loc[FRI, "BBB"]), "only the repaired cell is filled"
    assert (tmp_path / "ledger.jsonl").exists(), "an applied fill is recorded"


def test_norgate_secondary_is_used_when_the_primary_still_lacks_the_bar(monkeypatch, tmp_path):
    sessions = _sessions()
    frame = _frame(sessions)
    frame.loc[FRI, "AAA"] = np.nan
    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)
    monkeypatch.setitem(rp.CACHES, "unit", ("unit.parquet", "n/a"))
    frame.to_parquet(tmp_path / "unit.parquet")
    monkeypatch.setattr(rp, "fetch_primary",
                        lambda t, s, e: _frame(sessions)["AAA"].drop(FRI))
    ng = _frame(sessions, seed=7)["AAA"] * 0.5
    monkeypatch.setattr(rp, "fetch_norgate", lambda sym, s, e: ng)
    reps = rp.repair_cache("unit", only_ticker="AAA", apply=False, sessions=sessions)
    (r,) = reps
    assert r["source"] == "norgate:AAA"
    assert r["value"] == pytest.approx(float(frame.loc[THU, "AAA"]) * float(ng.loc[FRI] / ng.loc[THU]))
