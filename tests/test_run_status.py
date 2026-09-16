"""The instrument must not take out the patient.

These pin the three reproduced failures - a malformed prior record raising
inside the refresh's own failure handler, a JSON array doing the same, and
an unrecognised health value reading OK - plus the integration the helper
tests could not see: that ``scheduled_refresh`` survives a corrupt
diagnostic and still reports its ORIGINAL failure.

Python datetime months are 1-indexed (January = 1).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_status as rs                 # noqa: E402
import scheduled_refresh as sr          # noqa: E402

NOW = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)


def _rec(**kw):
    base = {"asof": NOW.isoformat(timespec="seconds"), "status": rs.SENT,
            "consecutive_failures": 0,
            "last_sent_utc": NOW.isoformat(timespec="seconds")}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. Malformed state, reproduced
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("prior", [
    '{"consecutive_failures": "bad"}',      # raised ValueError
    "[1]",                                  # raised AttributeError
    '"a string"',
    "null",
    "{truncated",                           # a run killed mid-write
    "",
])
def test_a_malformed_prior_record_never_raises(tmp_path, prior):
    p = tmp_path / "alert_delivery.json"
    p.write_text(prior, encoding="utf-8")
    rec = rs.record_alert(p, subject="s", status=rs.FAILED)
    assert rec is not None
    assert rec["consecutive_failures"] == 1
    assert rec["status"] == rs.FAILED


def test_a_lost_prior_record_says_so_on_the_new_one(tmp_path):
    p = tmp_path / "alert_delivery.json"
    p.write_text("[1]", encoding="utf-8")
    rec = rs.record_alert(p, subject="s", status=rs.FAILED)
    assert rec["prior_record_problems"], "the streak was lost silently"


def test_a_valid_streak_is_carried_and_reset_by_a_delivery(tmp_path):
    p = tmp_path / "alert_delivery.json"
    for expected in (1, 2, 3):
        assert rs.record_alert(p, subject="s",
                               status=rs.FAILED)["consecutive_failures"] == expected
    assert rs.record_alert(p, subject="s",
                           status=rs.SENT)["consecutive_failures"] == 0


def test_an_unknown_status_is_recorded_as_failed_not_accepted(tmp_path):
    rec = rs.record_alert(tmp_path / "a.json", subject="s", status="nonsense")
    assert rec["status"] == rs.FAILED
    assert "unknown status" in rec["detail"]


def test_read_json_refuses_anything_that_is_not_an_object(tmp_path):
    p = tmp_path / "x.json"
    for body in ("[1]", '"s"', "3", "null", "{bad"):
        p.write_text(body, encoding="utf-8")
        assert rs.read_json(p) is None


@pytest.mark.parametrize("value,expected", [
    ("bad", 0), (None, 0), ({}, 0), ([], 0), (True, 0), ("7", 7), (7, 7),
])
def test_coerce_int_never_raises(value, expected):
    assert rs._coerce_int(value) == expected


# ---------------------------------------------------------------------------
# 2. Health: unknown is visibly an error
# ---------------------------------------------------------------------------
def test_an_unrecognised_status_is_an_ERROR_not_OK():
    """THE REPRODUCED DEFECT. A record whose status was a word the module
    had never heard of returned OK, which is the one answer it cannot
    honestly give."""
    status, detail = rs.alert_health(_rec(status="nonsense"), NOW)
    assert status == "ERROR"
    assert "not one of" in detail


@pytest.mark.parametrize("rec", [None, {}, {"status": rs.SENT},
                                 {"asof": "not a date", "status": rs.SENT,
                                  "consecutive_failures": 0}])
def test_a_missing_or_unusable_record_is_an_ERROR(rec):
    assert rs.alert_health(rec, NOW)[0] == "ERROR"


def test_never_delivered_is_a_breach_not_a_grace_period():
    status, detail = rs.alert_health(_rec(last_sent_utc=None), NOW)
    assert status == "BREACH"
    assert "not commissioned" in detail


def test_a_recent_success_followed_by_a_failure_breaches_at_once():
    """The gap in an age-only watcher: alert_ok.json is hours old and the
    channel is already dead."""
    rec = _rec(status=rs.FAILED, consecutive_failures=1,
               last_sent_utc=(NOW - timedelta(hours=2)).isoformat(timespec="seconds"))
    status, detail = rs.alert_health(rec, NOW)
    assert status == "BREACH"
    assert "failing" in detail


def test_an_unconfigured_channel_breaches_regardless_of_age():
    rec = _rec(status=rs.UNCONFIGURED, consecutive_failures=1,
               credentials={"missing": ["GMAIL_USER"]})
    status, detail = rs.alert_health(rec, NOW)
    assert status == "BREACH" and "GMAIL_USER" in detail


def test_a_stale_last_SUCCESS_breaches_even_while_attempts_continue():
    rec = _rec(status=rs.SENT,
               last_sent_utc=(NOW - timedelta(hours=400)).isoformat(timespec="seconds"))
    assert rs.alert_health(rec, NOW)[0] == "BREACH"


def test_a_healthy_channel_is_OK():
    status, detail = rs.alert_health(_rec(), NOW)
    assert status == "OK" and "last delivery" in detail


# ---------------------------------------------------------------------------
# 3. Writes: atomic, bounded, and never a credential value
# ---------------------------------------------------------------------------
def test_only_a_real_delivery_moves_alert_ok(tmp_path):
    p = tmp_path / "alert_delivery.json"
    rs.record_alert(p, subject="s", status=rs.UNCONFIGURED)
    rs.record_alert(p, subject="s", status=rs.FAILED)
    assert not (tmp_path / "alert_ok.json").exists()
    rs.record_alert(p, subject="s", status=rs.SENT)
    assert (tmp_path / "alert_ok.json").exists()


def test_no_credential_VALUE_ever_reaches_a_record(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "someone@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    rec = rs.record_alert(tmp_path / "a.json", subject="s", status=rs.SENT)
    blob = json.dumps(rec)
    assert "abcd efgh ijkl mnop" not in blob
    assert "someone@example.com" not in blob
    assert rec["credentials"]["configured"] is True


def test_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    rs.record_alert(tmp_path / "a.json", subject="s", status=rs.SENT)
    rs.record_run(tmp_path / "runs.jsonl", cadence="post-fill", exit_code=3,
                  subject="x")
    assert not list(tmp_path.glob("*.tmp"))


def test_an_unwritable_path_returns_None_rather_than_raising(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    assert rs.record_alert(target, subject="s", status=rs.SENT) is None
    assert rs.record_run(target, cadence="post-fill", exit_code=1,
                         subject="x") is None


def test_the_run_ledger_is_bounded(tmp_path):
    p = tmp_path / "runs.jsonl"
    for i in range(520):
        rs.record_run(p, cadence="post-fill", exit_code=0, subject=str(i))
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 500
    assert json.loads(lines[-1])["outcome"] == "519"


def test_a_corrupt_run_ledger_tail_does_not_stop_the_next_record(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text("{not json\n", encoding="utf-8")
    assert rs.record_run(p, cadence="post-fill", exit_code=3,
                         subject="x") is not None


# ---------------------------------------------------------------------------
# 4. The independent observer
# ---------------------------------------------------------------------------
def test_the_observer_marker_moves_only_on_an_OK_verdict(tmp_path):
    rs.record_alert(rs.alert_state_path(tmp_path), subject="s",
                    status=rs.UNCONFIGURED)
    v = rs.observe(tmp_path, NOW)
    assert v["status"] == "BREACH"
    assert not rs.watch_ok_path(tmp_path).exists()

    rs.record_alert(rs.alert_state_path(tmp_path), subject="s", status=rs.SENT)
    v = rs.observe(tmp_path)
    assert v["status"] == "OK"
    assert rs.watch_ok_path(tmp_path).exists()


def test_the_observer_reports_ERROR_on_a_clone_that_has_never_run(tmp_path):
    v = rs.observe(tmp_path, NOW)
    assert v["status"] == "ERROR"
    assert not rs.watch_ok_path(tmp_path).exists()


def test_observer_cli_exit_codes(tmp_path, capsys):
    assert rs.main(["--repo", str(tmp_path)]) == 2          # no record
    rs.record_alert(rs.alert_state_path(tmp_path), subject="s",
                    status=rs.UNCONFIGURED)
    assert rs.main(["--repo", str(tmp_path)]) == 1          # breach
    rs.record_alert(rs.alert_state_path(tmp_path), subject="s", status=rs.SENT)
    assert rs.main(["--repo", str(tmp_path), "--observe"]) == 0
    assert rs.watch_ok_path(tmp_path).exists()


def test_probe_credentials_reports_presence_only(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret value")
    assert rs.main(["--repo", str(tmp_path), "--probe-credentials"]) == 1
    out = capsys.readouterr().out
    assert "secret value" not in out
    assert json.loads(out)["missing"] == ["GMAIL_USER"]


def test_notify_is_best_effort(tmp_path):
    assert rs.notify("x", script=tmp_path / "absent.ps1") is False


# ---------------------------------------------------------------------------
# 5. The integration: a broken diagnostic must not replace a real failure
# ---------------------------------------------------------------------------
def _drive_fail(monkeypatch, tmp_path, *, break_diagnostics: bool):
    """Run scheduled_refresh.main() to its first real failure."""
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sr, "log_path_for", lambda now: tmp_path / "run.log")
    monkeypatch.setattr(sr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sr, "price_source_preflight",
                        lambda source: (True, "fixture"))
    monkeypatch.setattr(sr, "_email", lambda *a, **kw: None)
    # The real failure under test: a dirty tree, which exits 2.
    monkeypatch.setattr(sr, "_git", lambda args, log, **kw: SimpleNamespace(
        returncode=0, stdout="M data/live_targets.json\n", stderr=""))
    if break_diagnostics:
        def boom(*a, **kw):
            raise RuntimeError("diagnostic exploded")
        monkeypatch.setattr(sr.run_status, "record_run", boom)
        monkeypatch.setattr(sr.run_status, "credential_state", boom)
        monkeypatch.setattr(sr.publication_debt, "current_debt", boom)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    return sr.main(["--cadence", "post-fill"])


def test_a_real_failure_is_reported_as_itself(monkeypatch, tmp_path):
    assert _drive_fail(monkeypatch, tmp_path, break_diagnostics=False) == 2


def test_an_exploding_diagnostic_does_not_replace_the_real_failure(
        monkeypatch, tmp_path):
    """The shape of the reproduced defect: ``_email`` and the run ledger are
    both called from inside ``fail()``. Anything that raises there unwinds
    out of the failure handler and the operator learns about the instrument
    instead of the outage."""
    assert _drive_fail(monkeypatch, tmp_path, break_diagnostics=True) == 2
    log = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "working tree not clean" in log
    assert "diagnostic exploded" in log, "the failure was swallowed silently"


def test_the_failure_path_writes_a_durable_run_record(monkeypatch, tmp_path):
    _drive_fail(monkeypatch, tmp_path, break_diagnostics=False)
    ledger = rs.run_ledger_path(tmp_path)
    assert ledger.exists()
    rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["exit_code"] == 2
    assert rec["cadence"] == "post-fill"


def test_the_startup_probe_records_an_unconfigured_channel(monkeypatch, tmp_path):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    _drive_fail(monkeypatch, tmp_path, break_diagnostics=False)
    rec = rs.read_json(rs.alert_state_path(tmp_path))
    assert rec["status"] == rs.UNCONFIGURED
    assert rs.alert_health(rec, NOW)[0] == "BREACH"
