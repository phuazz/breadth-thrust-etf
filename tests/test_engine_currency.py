"""CI must re-run sleeves B and C when it can add to them, and only then.

WHY THIS EXISTS (2026-08-30). The weekly workflow re-runs both ETF-level
sleeves from yfinance on every publish, which is how they stay current between
local refreshes. Over 2026-08-28/30 yfinance withheld Friday's closes for more
than 43 hours after serving and retracting them, and a local run sourcing the
same ETFs from Norgate reached Friday while no CI runner could. The re-run
would have dragged both sleeves back a session and published it.

The rule is one-directional: re-run when the committed output ends BEFORE the
last completed session, skip when it already reaches or passes it. CI may move
a sleeve forward and must never move one back.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_engine_currency as cec  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cec, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cec, "last_completed_session",
                        lambda *_a, **_k: pd.Timestamp("2026-08-28"),
                        raising=False)
    import nyse_sessions
    monkeypatch.setattr(nyse_sessions, "last_completed_session",
                        lambda *_a, **_k: pd.Timestamp("2026-08-28"))
    return tmp_path


def _write(d: Path, name: str, last: str):
    (d / name).write_text(json.dumps(
        {"headline": {"headline_equity_dates": ["2026-08-01", last]}}),
        encoding="utf-8")


def test_stale_output_is_rerun(data_dir):
    _write(data_dir, "asset_class_rotation.json", "2026-08-25")
    _write(data_dir, "thematic_rotation.json", "2026-08-25")
    r = cec.evaluate(NOW)
    assert r["rerun"] == {"b": True, "c": True}


def test_current_output_is_skipped(data_dir):
    """THE CASE THIS GUARD EXISTS FOR. A local Norgate-sourced build reached
    the last completed session; CI cannot, and must leave it alone."""
    _write(data_dir, "asset_class_rotation.json", "2026-08-28")
    _write(data_dir, "thematic_rotation.json", "2026-08-28")
    r = cec.evaluate(NOW)
    assert r["rerun"] == {"b": False, "c": False}, \
        "CI would have overwritten a build it cannot reproduce"


def test_output_ahead_of_the_session_is_skipped(data_dir):
    """One-directional: forward only, never back."""
    _write(data_dir, "asset_class_rotation.json", "2026-09-02")
    _write(data_dir, "thematic_rotation.json", "2026-09-02")
    r = cec.evaluate(NOW)
    assert r["rerun"] == {"b": False, "c": False}


def test_the_two_sleeves_are_judged_independently(data_dir):
    _write(data_dir, "asset_class_rotation.json", "2026-08-28")
    _write(data_dir, "thematic_rotation.json", "2026-08-25")
    r = cec.evaluate(NOW)
    assert r["rerun"] == {"b": False, "c": True}


def test_unreadable_output_fails_safe_to_rerun(data_dir):
    (data_dir / "asset_class_rotation.json").write_text("{ not json",
                                                        encoding="utf-8")
    _write(data_dir, "thematic_rotation.json", "2026-08-28")
    r = cec.evaluate(NOW)
    assert r["rerun"]["b"] is True, \
        "an unreadable committed output must re-run, not be trusted"
    assert r["rerun"]["c"] is False


def test_missing_output_fails_safe_to_rerun(data_dir):
    _write(data_dir, "thematic_rotation.json", "2026-08-28")
    r = cec.evaluate(NOW)
    assert r["rerun"]["b"] is True


def test_force_overrides_everything(data_dir, monkeypatch, capsys):
    """The escape hatch for a deliberate engine change."""
    _write(data_dir, "asset_class_rotation.json", "2026-08-28")
    _write(data_dir, "thematic_rotation.json", "2026-08-28")
    monkeypatch.setenv("BTE_FORCE_ENGINE_RERUN", "1")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert cec.main([]) == 0
    out = capsys.readouterr().out
    assert "rerun_b = true" in out and "rerun_c = true" in out
