"""Offline failure alerts preserve fail-closed behaviour and invocation scope."""
from scripts.scheduled_refresh import refresh_failure_details


SEAL_ERROR = "ValueError: performance does not reach a required venue session\n"
MISSING = "  Incomplete session 2026-11-06: EXH1, EXV3, EXV1; extension stopped\n"


def test_seal_failure_names_required_session_and_held_prices(tmp_path):
    log = tmp_path / "refresh.log"
    log.write_text(MISSING + MISSING + SEAL_ERROR, encoding="utf-8")
    subject, body = refresh_failure_details(log)
    assert subject == "component seal blocked by incomplete performance"
    assert body.count("Required session 2026-11-06: EXH1, EXV3, EXV1") == 1
    assert "Nothing was committed or pushed" in body
    assert "Once available, rerun the scheduled refresh" in body
    assert "do not bypass the seal" in body


def test_unrelated_failure_does_not_blame_vendor(tmp_path):
    log = tmp_path / "refresh.log"
    log.write_text(MISSING + "pytest FAILED\n", encoding="utf-8")
    subject, body = refresh_failure_details(log)
    assert subject == "refresh_all.py reported failed steps"
    assert "vendor" not in body


def test_previous_attempt_cannot_contaminate_current_failure(tmp_path):
    log = tmp_path / "refresh.log"
    previous = "Earlier attempt: \u00a3\n" + MISSING + SEAL_ERROR
    log.write_text(previous + "pytest FAILED\n", encoding="utf-8")
    subject, _ = refresh_failure_details(log, len(previous.encode("utf-8")))
    assert subject == "refresh_all.py reported failed steps"


def test_seal_failure_without_price_evidence_does_not_invent_symbols(tmp_path):
    log = tmp_path / "refresh.log"
    log.write_text(SEAL_ERROR, encoding="utf-8")
    subject, body = refresh_failure_details(log)
    assert subject == "component seal blocked by incomplete performance"
    assert "EXH1" not in body
    assert "Required session" not in body


def test_unreadable_log_preserves_original_failure(tmp_path):
    subject, body = refresh_failure_details(tmp_path / "absent.log")
    assert subject == "refresh_all.py reported failed steps"
    assert "nothing was committed or pushed" in body
