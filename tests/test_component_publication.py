"""Offline component/email policy tests. Python months are 1-indexed."""
from datetime import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from component_publication import Snapshot, email_decision, email_wording, resize_held_basket, review_window


def decide(at, *, d_ready=False, sent=None, core_ok=True, operator_hold=False):
    return email_decision(datetime.fromisoformat(at), anchor="2026-09-11",
        core=Snapshot("core1", core_ok, "2026-09-11", "2026-09-11"),
        europe=Snapshot("d1", d_ready, "2026-09-11", "2026-09-11"),
        sent=sent or {}, operator_hold=operator_hold)


def test_distribution_preview_does_not_wait_for_europe():
    r = decide("2026-09-12T16:00:00+08:00")
    assert (r["action"], r["audience"], r["d_hold"]) == ("preview", "distribution", True)


def test_both_stages_use_distribution_but_failures_are_operator_only():
    for at in ("2026-09-12T16:00:00+08:00", "2026-09-13T18:00:00+08:00"):
        assert decide(at)["audience"] == "distribution"
    assert decide("2026-09-13T18:00:00+08:00", core_ok=False)["audience"] == "operator"


def test_reader_can_distinguish_initial_hold_and_ready_updates():
    initial = email_wording(decide("2026-09-12T16:00:00+08:00"))
    held = email_wording(decide("2026-09-13T18:00:00+08:00"))
    ready = email_wording(decide("2026-09-13T18:00:00+08:00", d_ready=True))
    assert "Initial factsheet" in initial["subject"] and "D pending" in initial["subject"]
    assert "D remains on HOLD" in held["subject"]
    assert "all strategies ready" in ready["subject"]
    assert "unchanged" in held["difference"] and "unchanged" in ready["difference"]
    assert "risk adjustment" in held["d_instruction"]
    with pytest.raises(ValueError):
        email_wording({"action": "wait"})


def test_send_when_everything_ready_without_waiting_until_evening():
    assert decide("2026-09-13T14:00:00+08:00", d_ready=True)["action"] == "regular"


def test_deadline_d_hold_factsheet_and_bad_core_alert():
    assert decide("2026-09-13T18:00:00+08:00")["action"] == "regular"
    assert decide("2026-09-13T18:00:00+08:00", core_ok=False)["action"] == "alert"


def test_confirmed_sends_are_deduplicated():
    ledger = {"anchor": "2026-09-11", "preview": "core1"}
    assert decide("2026-09-13T17:00:00+08:00", sent=ledger)["action"] == "wait"
    ledger.update(regular="d_hold", core="core1")
    assert decide("2026-09-13T19:00:00+08:00", sent=ledger)["action"] == "wait"
    assert decide("2026-09-13T19:00:00+08:00", sent=ledger, d_ready=True)["action"] == "d_update"
    ledger["d_update"] = "d1"
    assert decide("2026-09-13T20:00:00+08:00", sent=ledger, d_ready=True)["action"] == "wait"


def test_late_updates_and_changed_core_need_review():
    ledger = {"anchor": "2026-09-11", "regular": "d_hold", "core": "core1"}
    assert decide("2026-09-14T07:00:00+08:00", sent=ledger, d_ready=True)["action"] == "alert"
    ledger["core"] = "another-core"
    assert decide("2026-09-13T20:00:00+08:00", sent=ledger, d_ready=True)["action"] == "alert"
    assert decide("2026-09-13T20:00:00+08:00", operator_hold=True)["action"] == "wait"


@pytest.mark.parametrize("at, sunday, monday", [
    ("2026-08-01T10:00:00+08:00", "2026-08-02T18:00:00+08:00", "2026-08-03T06:00:00+08:00"),
    ("2027-01-02T10:00:00+08:00", "2027-01-03T18:00:00+08:00", "2027-01-04T06:00:00+08:00"),
    ("2026-08-31T05:00:00+08:00", "2026-08-30T18:00:00+08:00", "2026-08-31T06:00:00+08:00"),
])
def test_month_year_and_monday_boundaries(at, sunday, monday):
    start, end = review_window(datetime.fromisoformat(at))
    assert start.isoformat() == sunday
    assert end.isoformat() == monday


def test_invalid_dates_and_wrong_week_fail_closed():
    assert not Snapshot("x", True, "bad", "bad").ready
    assert decide("2026-09-20T18:00:00+08:00")["action"] == "alert"
    with pytest.raises(ValueError):
        review_window(datetime(2026, 9, 13))


def test_gate_resizes_existing_selection_without_reranking():
    held = {"bank": 0.12, "energy": 0.08}
    target = resize_held_basket(held, 0.10)
    assert target == pytest.approx({"bank": 0.06, "energy": 0.04})
    assert held == {"bank": 0.12, "energy": 0.08}
    assert sum(target.values()) == pytest.approx(0.10)
    with pytest.raises(ValueError):
        resize_held_basket({}, 0.10)
    with pytest.raises(ValueError):
        resize_held_basket({"bank": float("nan")}, 0.10)
