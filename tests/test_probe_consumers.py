"""Probe CONSUMERS honour the hole fields (2026-10-11, second verification).

The raw crypto observation was fixed in the first round; this round pins
the readers. The deceptive fixture throughout is the reviewer's: Saturday
2026-10-10 served, Friday 2026-10-09 missing — lag reads zero, so any
consumer that reads lag alone reports CURRENT over a hole in the decision
inputs. Served, current and complete are three different statements.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_vendor_probe as cv  # noqa: E402
import probe_vendor_availability as pv  # noqa: E402

NOW = datetime(2026, 10, 11, 1, 30, tzinfo=timezone.utc)


def _row(ticker, venue, role, behind, *, missing=None, required=None,
         last_bar="2026-10-10", lcs="2026-10-10"):
    row = {"ticker": ticker, "venue": venue, "role": role,
           "last_bar": last_bar, "last_completed_session": lcs,
           "sessions_behind": behind}
    if missing is not None:
        row["missing_completed_days"] = list(missing)
    if required is not None:
        row["required_day_bar_present"] = required
    return row


def _holed_record(stamp="2026-10-11T01:16:51+00:00"):
    """Saturday present, Friday missing: behind 0, one hole."""
    return {"probed_at_utc": stamp, "rows": [
        _row("SPY", "NYSE", "US ETF proxy", 0,
             last_bar="2026-10-09", lcs="2026-10-09"),
        _row("BTC-USD", "CRYPTO", "C crypto line (UTC day bars)", 0,
             missing=["2026-10-09"], required=True),
    ]}


def _clean_record(stamp="2026-10-11T01:16:51+00:00"):
    return {"probed_at_utc": stamp, "rows": [
        _row("SPY", "NYSE", "US ETF proxy", 0,
             last_bar="2026-10-09", lcs="2026-10-09"),
        _row("BTC-USD", "CRYPTO", "C crypto line (UTC day bars)", 0,
             missing=[], required=True),
    ]}


def _write(tmp_path, *records):
    p = tmp_path / "log.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records),
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The classifier itself
# ---------------------------------------------------------------------------

def test_completeness_is_distinct_from_lag():
    deceptive = _row("BTC-USD", "CRYPTO", "x", 0,
                     missing=["2026-10-09"], required=True)
    assert deceptive["sessions_behind"] == 0
    assert pv.decision_input_complete(deceptive) is False
    assert pv.decision_input_complete(
        _row("BTC-USD", "CRYPTO", "x", 0, missing=[], required=False)) is False
    assert pv.decision_input_complete(
        _row("BTC-USD", "CRYPTO", "x", 0, missing=[], required=True)) is True


def test_rows_without_the_fields_are_unknown_never_complete():
    """Venue rows and pre-2026-10-11 crypto rows carry no verdict: None,
    so a consumer cannot read historical rows as proven-complete."""
    assert pv.decision_input_complete(_row("SPY", "NYSE", "x", 0)) is None


# ---------------------------------------------------------------------------
# summarise(): the operator-facing tables
# ---------------------------------------------------------------------------

def test_summary_marks_holes_and_does_not_count_current(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pv, "LOG", _write(tmp_path, _holed_record()))
    pv.summarise()
    out = capsys.readouterr().out
    assert "!DECISION-INPUT HOLES" in out
    assert "2026-10-09" in out
    # The hourly table (everything after its header): the holed Bitcoin
    # probe must not count CURRENT despite sessions_behind == 0, while the
    # venue row keeps its historical lag-only semantics.
    hourly = out.split("by firing hour", 1)[1].splitlines()
    btc_line = next(l for l in hourly if l.strip().startswith("BTC-USD"))
    spy_line = next(l for l in hourly if l.strip().startswith("SPY"))
    assert "0/1" in btc_line and "1/1" not in btc_line
    assert "1/1" in spy_line


def test_summary_is_quiet_when_inputs_are_complete(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pv, "LOG", _write(tmp_path, _clean_record()))
    pv.summarise()
    out = capsys.readouterr().out
    assert "!DECISION-INPUT HOLES" not in out


# ---------------------------------------------------------------------------
# check_vendor_probe.evaluate(): the guard's verdict and summary
# ---------------------------------------------------------------------------

def test_guard_summary_cannot_imply_completeness_over_a_hole(tmp_path):
    verdict = cv.evaluate(_write(tmp_path, _holed_record()), NOW)
    assert verdict["ok"] is True  # observation succeeded; that verdict stands
    assert verdict["decision_inputs_complete"] is False
    assert verdict["decision_input_holes"] == {"BTC-USD": ["2026-10-09"]}
    assert "lines served" in verdict["summary"]
    assert "decision-input holes" in verdict["summary"]
    assert "BTC-USD missing 2026-10-09" in verdict["summary"]


def test_guard_reports_complete_when_the_fields_are_clean(tmp_path):
    verdict = cv.evaluate(_write(tmp_path, _clean_record()), NOW)
    assert verdict["ok"] is True
    assert verdict["decision_inputs_complete"] is True
    assert verdict["decision_input_holes"] == {}
    assert "decision-input holes" not in verdict["summary"]


def test_guard_emits_no_new_email_trigger_for_holes(tmp_path, monkeypatch):
    """Holes are informational: the workflow email condition reads the
    retraction outputs, and a hole must not flip them. The consequence
    routing belongs to the alert-policy owner."""
    out_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    verdict = cv.evaluate(_write(tmp_path, _holed_record()), NOW)
    cv._emit_outputs(verdict["anomalous"], verdict["summary"],
                     verdict["routine"], verdict["measuring"])
    text = out_file.read_text(encoding="utf-8")
    assert "retracted=false" in text
    assert "retracted_count=0" in text
