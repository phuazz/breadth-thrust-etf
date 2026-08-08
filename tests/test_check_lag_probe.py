"""The guard on the scheduled publication-lag probe.

A guard is only worth having if it fails when it should, so these test the
failure paths harder than the success one. The condition being guarded is
not a crash — it is a probe that exits 0 having recorded nothing usable, so
the log grows, the workflow stays green, and the measurement window is
quietly empty.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_lag_probe import EXPECTED_ETFS, check, load_rows  # noqa: E402

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _row(etf, minutes_ago=1, latest="2026-08-06", errors=None):
    return {
        "probe_utc": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "etf": etf,
        "latest_with_data": latest,
        "sessions_behind_nyse": 1,
        "errors": errors or {},
    }


def _full_run(**kw):
    return [_row(e, **kw) for e in sorted(EXPECTED_ETFS)]


def test_healthy_run_passes():
    code, msgs = check(_full_run(), NOW, 45)
    assert code == 0
    assert any(m.startswith("OK") for m in msgs)


def test_empty_log_fails():
    code, msgs = check([], NOW, 45)
    assert code == 1
    assert "wrote nothing" in " ".join(msgs)


def test_stale_rows_fail_even_though_the_log_is_populated():
    """The trap this exists for. Yesterday's rows would otherwise satisfy
    every other assertion, and a probe that silently stopped writing would
    keep passing for as long as the file existed."""
    code, msgs = check(_full_run(minutes_ago=60 * 26), NOW, 45)
    assert code == 1
    assert "appended nothing" in " ".join(msgs)


def test_run_with_no_data_for_any_etf_fails():
    """Rows exist, so the log grew and the job would look successful — but
    an endpoint outage recorded as data is not an observation."""
    code, msgs = check(_full_run(latest=None), NOW, 45)
    assert code == 1
    assert "recorded no observation" in " ".join(msgs)


def test_missing_etf_fails():
    rows = [_row(e) for e in sorted(EXPECTED_ETFS)][:-1]
    code, msgs = check(rows, NOW, 45)
    assert code == 1
    assert "missing ETFs" in " ".join(msgs)


def test_partial_data_warns_but_passes():
    """One quiet ETF is a real observation about that fund, not a failed
    run — a fund whose holdings are genuinely unpublished is exactly what
    the probe is measuring."""
    rows = _full_run()
    rows[0]["latest_with_data"] = None
    code, msgs = check(rows, NOW, 45)
    assert code == 0
    assert any(m.startswith("WARN") for m in msgs)


def test_per_date_errors_warn_but_pass():
    rows = _full_run()
    rows[1]["errors"] = {"2026-08-07": "HTTP 503"}
    code, msgs = check(rows, NOW, 45)
    assert code == 0
    assert "per-date errors" in " ".join(msgs)


def test_malformed_line_fails(tmp_path):
    """An append-only log with a broken line means a run died mid-write."""
    p = tmp_path / "log.jsonl"
    p.write_text('{"probe_utc": "2026-08-08T11:59:00+00:00", "etf": "CSP1"}\n'
                  '{ truncated\n', encoding="utf-8")
    code, msgs = check(load_rows(p), NOW, 45)
    assert code == 1
    assert "malformed" in " ".join(msgs)


def test_naive_timestamps_are_treated_as_utc():
    """The probe writes tz-aware stamps, but a naive one must not be read as
    local time and silently fall outside the freshness window."""
    rows = _full_run()
    for r in rows:
        r["probe_utc"] = (NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    code, _ = check(rows, NOW, 45)
    assert code == 0


def test_missing_log_file_reads_as_empty(tmp_path):
    assert load_rows(tmp_path / "nope.jsonl") == []
