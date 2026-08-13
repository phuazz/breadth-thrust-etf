"""WS15 — the vendor step-defect guard must refuse a mis-adjusted split.

Around its 2026-08-11 two-for-one split, yfinance served MNST with the
split factor unapplied (pre-split bars unhalved beside post-split bars,
identical under auto_adjust=True and False) while the vendor's own split
calendar carried the split. Ingested, that fabricates a -49.6% day and
poisons ~50 sessions of MA breadth. These tests pin the guard that refuses
such a column, and — just as important — the cases it must NOT refuse.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compute_breadth as cb  # noqa: E402


def _series(values, start="2026-06-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series([float(v) for v in values], index=idx)


def _flat_then_halved(n_pre=40, n_post=5, level=90.0):
    """The MNST shape: pre-split bars unhalved, post-split bars halved."""
    return _series([level] * n_pre + [level / 2] * n_post)


def _splits_stub(date, ratio):
    def stub(ticker):
        return pd.Series([float(ratio)], index=[pd.Timestamp(date)])
    return stub


def test_missed_forward_split_is_detected(monkeypatch):
    s = _flat_then_halved()
    split_day = s.index[40]  # the first halved bar
    monkeypatch.setattr(cb, "_splits_for", _splits_stub(split_day, 2.0))
    reason = cb._vendor_step_defect(s, "MNST")
    assert reason is not None and "2-for-1" in reason


def test_missed_reverse_split_is_detected(monkeypatch):
    # 1-for-10 reverse split served unapplied: price steps x10.
    s = _series([5.0] * 40 + [50.0] * 5)
    split_day = s.index[40]
    monkeypatch.setattr(cb, "_splits_for", _splits_stub(split_day, 0.1))
    assert cb._vendor_step_defect(s, "X") is not None


def test_genuine_crash_is_accepted(monkeypatch):
    # A real -30% day with the nearest split years away must pass.
    s = _series([100.0] * 40 + [70.0] * 5)
    monkeypatch.setattr(cb, "_splits_for", _splits_stub("2016-11-10", 3.0))
    assert cb._vendor_step_defect(s, "X") is None


def test_split_of_wrong_ratio_is_accepted(monkeypatch):
    # A -50% step beside a 5-for-4 split is NOT explained by it.
    s = _flat_then_halved()
    monkeypatch.setattr(cb, "_splits_for",
                        _splits_stub(s.index[40], 1.25))
    assert cb._vendor_step_defect(s, "X") is None


def test_no_calendar_fails_open(monkeypatch, capsys):
    s = _flat_then_halved()
    monkeypatch.setattr(cb, "_splits_for", lambda t: None)
    assert cb._vendor_step_defect(s, "X") is None
    assert "unavailable" in capsys.readouterr().out


def test_constant_rebasing_is_accepted(monkeypatch):
    # A correctly re-adjusted vendor series (every bar halved, no internal
    # step) must never trigger — MA breadth is scale-invariant per column.
    s = _series(list(np.linspace(45.0, 47.0, 45)))
    monkeypatch.setattr(
        cb, "_splits_for",
        lambda t: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert cb._vendor_step_defect(s, "X") is None


def test_historical_step_outside_recent_window_is_ignored(monkeypatch):
    # A split-sized move 60 sessions back (PTON-style crash history) must
    # not even consult the split calendar — it is baked into prior and
    # fresh alike and is not an ingestion hazard.
    s = _series([100.0] * 40 + [50.0] * 60)
    monkeypatch.setattr(
        cb, "_splits_for",
        lambda t: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert cb._vendor_step_defect(s, "X") is None


def test_revert_swaps_in_prior_column(monkeypatch):
    fresh_col = _flat_then_halved()
    prior_col = _series([90.0] * 42)          # clean cached history
    close = pd.DataFrame({"MNST": fresh_col, "AAPL": _series([200.0] * 45)})
    prior = pd.DataFrame({"MNST": prior_col, "AAPL": _series([199.0] * 42)})
    monkeypatch.setattr(cb, "_splits_for",
                        _splits_stub(fresh_col.index[40], 2.0))
    out, reverted = cb._revert_vendor_step_defects(
        close, prior, ["MNST", "AAPL"])
    assert reverted == ["MNST"]
    # MNST now carries the prior values; AAPL is untouched fresh data.
    assert float(out["MNST"].dropna().iloc[-1]) == 90.0
    assert float(out["AAPL"].dropna().iloc[-1]) == 200.0


def test_defect_without_prior_column_is_accepted_with_warning(monkeypatch, capsys):
    fresh_col = _flat_then_halved()
    close = pd.DataFrame({"NEWCO": fresh_col})
    prior = pd.DataFrame({"OTHER": _series([1.0] * 10)})
    monkeypatch.setattr(cb, "_splits_for",
                        _splits_stub(fresh_col.index[40], 2.0))
    out, reverted = cb._revert_vendor_step_defects(close, prior, ["NEWCO"])
    assert reverted == []
    assert float(out["NEWCO"].dropna().iloc[-1]) == 45.0
    assert "no prior column" in capsys.readouterr().out
