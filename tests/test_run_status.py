"""The instrument must not take out the patient.

These pin the three reproduced failures - a malformed prior record raising
inside the refresh's own failure handler, a JSON array doing the same, and
an unrecognised health value reading OK - plus the integration the helper
tests could not see: that ``scheduled_refresh`` survives a corrupt
diagnostic and still reports its ORIGINAL failure.

Python datetime months are 1-indexed (January = 1).

PROVENANCE OF THE LABELS BELOW. Tests marked "DEFECT (predecessor)" pin a
defect of the UNCOMMITTED draft that preceded commit 47d1b3a. All three
modules are absent at 47d1b3a^, so those cases cannot be demonstrated as
behavioural failures against any commit: the draft existed only as untracked
working-tree files, and the reproductions were run against it in session on
2026-09-16 before it was overwritten. Tests marked "REPRODUCED 2026-09-16"
are different - they were reproduced against 47d1b3a itself, which is in the
history, and they fail there.
"""
from __future__ import annotations

import io
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
    """DEFECT (predecessor). A record whose status was a word the module
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


# ---------------------------------------------------------------------------
# 6. Credential VALUES must not ride out on an exception (finding 7)
#
# Synthetic sentinels only. Nothing here reads, prints or sends a real
# credential, and no message leaves the machine.
# ---------------------------------------------------------------------------
SENTINEL_USER = "sentinel-user@invalid.example"
SENTINEL_PW = "SENTINEL-PASSWORD-abcd-efgh"


@pytest.fixture
def sentinel_credentials(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", SENTINEL_USER)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", SENTINEL_PW)
    return SENTINEL_USER, SENTINEL_PW


def test_redact_removes_both_credential_values(sentinel_credentials):
    text = (f"SMTPRecipientsRefused: {{'{SENTINEL_USER}': (550, b'no such')}} "
            f"and auth failed for {SENTINEL_PW}")
    out = rs.redact(text)
    assert SENTINEL_USER not in out and SENTINEL_PW not in out
    assert "<GMAIL_USER>" in out and "<GMAIL_APP_PASSWORD>" in out
    # The diagnostic survives the scrub.
    assert "SMTPRecipientsRefused" in out and "550" in out


def test_redact_removes_the_unspaced_form_of_a_four_block_password(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.delenv("GMAIL_USER", raising=False)
    assert "abcdefghijklmnop" not in rs.redact("echo abcdefghijklmnop back")


def test_redact_leaves_ordinary_text_alone(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert rs.redact("connection refused by smtp.gmail.com") == \
        "connection refused by smtp.gmail.com"
    assert rs.redact(None) == ""


def test_an_exception_naming_the_recipient_never_reaches_the_record(
        monkeypatch, tmp_path, sentinel_credentials):
    """REPRODUCED 2026-09-16 against 47d1b3a: SMTPRecipientsRefused stringifies to a dict keyed by the
    recipient address, which IS GMAIL_USER, and it was written verbatim to
    the text log and to logs/alert_delivery.json."""
    import smtplib

    class Refused:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p):
            raise smtplib.SMTPRecipientsRefused({SENTINEL_USER: (550, b"no")})
        def send_message(self, m): pass

    monkeypatch.setattr(sr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sr.smtplib, "SMTP_SSL", Refused)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = io.StringIO()
    sr._email("subject", "body", log)

    rec = rs.read_json(rs.alert_state_path(tmp_path))
    verdict = rs.observe(tmp_path, write_marker=False)
    assert SENTINEL_USER not in log.getvalue()
    assert SENTINEL_USER not in json.dumps(rec)
    assert SENTINEL_USER not in json.dumps(verdict)
    assert "SMTPRecipientsRefused" in rec["detail"], "the diagnostic was lost"
    assert rec["status"] == rs.FAILED


def test_a_transport_echoing_the_password_never_reaches_the_record(
        monkeypatch, tmp_path, sentinel_credentials):
    import smtplib

    class Echo:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(
                535, f"auth failed for {p}".encode())
        def send_message(self, m): pass

    monkeypatch.setattr(sr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sr.smtplib, "SMTP_SSL", Echo)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = io.StringIO()
    sr._email("subject", "body", log)
    rec = rs.read_json(rs.alert_state_path(tmp_path))
    assert SENTINEL_PW not in log.getvalue()
    assert SENTINEL_PW not in json.dumps(rec)
    assert SENTINEL_PW not in json.dumps(rs.observe(tmp_path, write_marker=False))


def test_a_legacy_record_written_before_redaction_is_scrubbed_on_output(
        tmp_path, sentinel_credentials):
    """A record written by an older build may still carry a value. The
    observer prints the record, so it scrubs on the way out too."""
    rs._write_json(rs.alert_state_path(tmp_path), {
        "schema": 1, "asof": NOW.isoformat(timespec="seconds"),
        "status": rs.FAILED, "consecutive_failures": 2,
        "subject": "x", "detail": f"refused for {SENTINEL_USER}",
        "last_sent_utc": NOW.isoformat(timespec="seconds")})
    verdict = rs.observe(tmp_path, NOW, write_marker=False)
    assert SENTINEL_USER not in json.dumps(verdict)


def test_a_credential_in_a_GENERATED_diagnostic_field_never_escapes(
        tmp_path, sentinel_credentials):
    """REPRODUCED 2026-09-16 against the second pass: a corrupt record whose
    ``status`` held the password had that value interpolated into
    ``prior_record_problems`` by the validator, written to the next record,
    and echoed whole by the observer. Only ``subject`` and ``detail`` were
    scrubbed, and this field was neither."""
    rs._write_json(rs.alert_state_path(tmp_path),
                   {"schema": 1, "status": SENTINEL_PW,
                    "consecutive_failures": 0,
                    "asof": NOW.isoformat(timespec="seconds")})
    rec = rs.record_alert(rs.alert_state_path(tmp_path), subject="s",
                          status=rs.FAILED)
    assert rec["prior_record_problems"], "the corruption went unreported"
    assert SENTINEL_PW not in json.dumps(rec)
    assert SENTINEL_PW not in json.dumps(rs.observe(tmp_path, NOW,
                                                    write_marker=False))
    # The diagnostic still locates the fault.
    assert "not one of" in rec["prior_record_problems"][0]
    assert "length" in rec["prior_record_problems"][0]


def test_a_validator_message_carries_no_value_at_all(monkeypatch):
    """The structural half of the fix, and the one that holds when the
    reader's environment differs from the writer's: an unknown value is
    never echoed, so there is nothing for redaction to have to catch."""
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    ok, problems = rs.validate_alert_record(
        {"status": "hunter2-not-a-status", "asof": NOW.isoformat(),
         "consecutive_failures": 0})
    assert ok is False
    assert "hunter2" not in " ".join(problems)


def test_the_observer_scrubs_every_string_in_the_record(tmp_path,
                                                        sentinel_credentials):
    rs._write_json(rs.alert_state_path(tmp_path), {
        "schema": 1, "asof": NOW.isoformat(timespec="seconds"),
        "status": rs.FAILED, "consecutive_failures": 1,
        "subject": "s", "detail": "d",
        "last_sent_utc": NOW.isoformat(timespec="seconds"),
        "nested": {"anything": [f"leaked {SENTINEL_USER}"]}})
    verdict = rs.observe(tmp_path, NOW, write_marker=False)
    assert SENTINEL_USER not in json.dumps(verdict)
    assert "<GMAIL_USER>" in json.dumps(verdict)


def test_redaction_is_bounded_in_depth_and_never_raises(sentinel_credentials):
    deep = cur = {}
    for _ in range(30):
        cur["next"] = {"v": SENTINEL_USER}
        cur = cur["next"]
    assert rs.redact_obj(deep) is not None
    assert rs.redact_obj(None) is None
    assert rs.redact_obj(7) == 7


def test_a_short_credential_is_below_the_redaction_floor(monkeypatch):
    """STATED LIMIT. A value shorter than four characters is not scrubbed,
    because at that length a "value" is more likely to be a substring of
    ordinary prose and redacting it would corrupt the diagnostic. Not
    echoing unknown values is what protects this case."""
    monkeypatch.setenv("GMAIL_USER", "ab")
    assert rs.redact("connection to ab refused") == "connection to ab refused"


def test_an_observer_without_the_environment_cannot_scrub_by_value(
        tmp_path, monkeypatch):
    """STATED LIMIT. Redaction removes values this process can see. A record
    written by a process holding the credential, read by one that does not,
    is only protected by what the writer already scrubbed."""
    monkeypatch.setenv("GMAIL_USER", SENTINEL_USER)
    rs.record_alert(rs.alert_state_path(tmp_path), subject=f"to {SENTINEL_USER}",
                    status=rs.FAILED, detail=f"refused {SENTINEL_USER}")
    monkeypatch.delenv("GMAIL_USER", raising=False)
    verdict = rs.observe(tmp_path, NOW, write_marker=False)
    assert SENTINEL_USER not in json.dumps(verdict), \
        "the WRITER must have scrubbed it, because the reader cannot"


def test_the_run_ledger_outcome_is_redacted(tmp_path, sentinel_credentials):
    rs.record_run(tmp_path / "runs.jsonl", cadence="post-fill", exit_code=5,
                  subject=f"push failed for {SENTINEL_USER}")
    assert SENTINEL_USER not in (tmp_path / "runs.jsonl").read_text(
        encoding="utf-8")


def test_the_startup_probe_records_an_unconfigured_channel(monkeypatch, tmp_path):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    _drive_fail(monkeypatch, tmp_path, break_diagnostics=False)
    rec = rs.read_json(rs.alert_state_path(tmp_path))
    assert rec["status"] == rs.UNCONFIGURED
    assert rs.alert_health(rec, NOW)[0] == "BREACH"
