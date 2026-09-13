"""One labelled presentation revision of an already-confirmed anchor.

The revision path exists to re-send the SAME sealed book under a clearly
labelled subject. These tests exist to prove it cannot become anything else:
no second send, no changed instruction, no cleared receipt, no retry.
"""
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import component_release as cr
import send_component_factsheet as sender
from component_publication import email_decision, email_wording
from test_component_sender import install, NOW

REVISION = "presentation-2026-09-13"
# Monday 07:00 SGT: the anchor still stands but the review checkpoint has gone.
AFTER_CHECKPOINT = datetime(2026, 9, 13, 23, tzinfo=timezone.utc)


def settled(tmp_path, monkeypatch, now=NOW, **ledger):
    """An anchor whose ordinary two-stage delivery completed all-ready."""
    release = install(tmp_path, monkeypatch, ready=True, now=now)
    state = {"anchor": release["anchor"], "core": release["core_identity"],
             "europe": release["europe_identity"], "preview": "an-earlier-identity",
             "regular": "all_ready", "last_confirmed_at": now.isoformat()}
    state.update(ledger)
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {release["anchor"]: state}})
    return release


def deliver(tmp_path, now=NOW, revision=REVISION):
    sent = []
    sender.prepare_revision(tmp_path, now, revision, reserve=True)
    sender.send_revision(tmp_path, now, revision, transport=lambda c, _: sent.append(c), env={})
    return sent


def test_revision_is_labelled_and_leaves_the_original_receipt_intact(tmp_path, monkeypatch):
    release = settled(tmp_path, monkeypatch)
    before = cr.read(tmp_path / sender.LEDGER)["anchors"][release["anchor"]]
    sent = deliver(tmp_path)
    assert len(sent) == 1
    assert sent[0]["subject"].startswith("Revised presentation - same portfolio instructions")
    assert "changes the presentation only" in sent[0]["html"]
    assert "not a second set of orders" in sent[0]["html"]
    assert sent[0]["release_identity"] == release["identity"]
    state = cr.read(tmp_path / sender.LEDGER)["anchors"][release["anchor"]]
    assert state["revisions"][REVISION]["release"] == release["identity"]
    assert "pending" not in state
    # The delivered instruction's own evidence is untouched.
    assert {k: state[k] for k in before} == before
    assert not (tmp_path / "docs/factsheet_published.json").exists()


def test_retry_after_a_confirmed_revision_cannot_resend(tmp_path, monkeypatch):
    release = settled(tmp_path, monkeypatch)
    deliver(tmp_path)
    # The prepared payload is still on disk; the ledger is what refuses.
    assert (tmp_path / sender.OUT / "candidate.json").exists()
    with pytest.raises(ValueError, match="eligibility changed after reservation"):
        sender.send_revision(tmp_path, NOW, REVISION,
                             transport=lambda *_: pytest.fail("transport reached"), env={})
    assert "already delivered" in sender.plan_revision(tmp_path, NOW, REVISION)[0]["reason"]
    assert "pending" not in cr.read(tmp_path / sender.LEDGER)["anchors"][release["anchor"]]


def test_only_one_revision_is_allowed_whatever_its_identifier(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch)
    deliver(tmp_path)
    decision, _ = sender.plan_revision(tmp_path, NOW, "presentation-second-try")
    assert decision["action"] == "blocked" and "already delivered" in decision["reason"]
    assert sender.prepare_revision(tmp_path, NOW, "presentation-second-try",
                                   reserve=True)["action"] == "blocked"
    assert "pending" not in cr.read(tmp_path / sender.LEDGER)["anchors"]["2026-09-11"]


@pytest.mark.parametrize("field", ["core", "europe"])
def test_a_changed_instruction_identity_is_refused(tmp_path, monkeypatch, field):
    settled(tmp_path, monkeypatch, **{field: "an-instruction-that-was-never-sent"})
    decision, _ = sender.plan_revision(tmp_path, NOW, REVISION)
    assert decision["action"] == "blocked" and "identities differ" in decision["reason"]


def test_an_unfinished_weekly_sequence_is_refused(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch, regular="d_hold")
    decision, _ = sender.plan_revision(tmp_path, NOW, REVISION)
    assert decision["action"] == "blocked" and "D follow-up" in decision["reason"]


def test_an_undelivered_anchor_is_refused(tmp_path, monkeypatch):
    release = install(tmp_path, monkeypatch, ready=True)
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {}})
    assert "no confirmed delivery" in sender.plan_revision(tmp_path, NOW, REVISION)[0]["reason"]
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {
        release["anchor"]: {"anchor": release["anchor"], "preview": release["identity"]}}})
    assert "has not completed" in sender.plan_revision(tmp_path, NOW, REVISION)[0]["reason"]


def test_any_outstanding_attempt_blocks_a_revision(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch, pending={"id": "someone-elses", "action": "regular"})
    decision, _ = sender.plan_revision(tmp_path, NOW, REVISION)
    assert decision["action"] == "blocked" and "unconfirmed delivery attempt" in decision["reason"]


def test_operator_hold_blocks_a_revision(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch)
    cr.write(tmp_path / "docs/factsheet_hold.json", {"reason": "operator review"})
    assert "operator hold" in sender.plan_revision(tmp_path, NOW, REVISION)[0]["reason"]


def test_a_passed_review_checkpoint_is_refused(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch)
    decision, _ = sender.plan_revision(tmp_path, AFTER_CHECKPOINT, REVISION)
    assert decision["action"] == "blocked" and "checkpoint has passed" in decision["reason"]


@pytest.mark.parametrize("identifier", ["", "   ", "../escape", "has space", "a" * 65])
def test_an_unusable_revision_identifier_is_refused(tmp_path, monkeypatch, identifier):
    settled(tmp_path, monkeypatch)
    assert "revision identifier" in sender.plan_revision(tmp_path, NOW, identifier)[0]["reason"]


def test_an_uncertain_transport_is_never_retried_and_blocks_the_sender(tmp_path, monkeypatch):
    release = settled(tmp_path, monkeypatch)
    sender.prepare_revision(tmp_path, NOW, REVISION, reserve=True)
    def fail(*_):
        raise TimeoutError("SMTP outcome unknown")
    with pytest.raises(TimeoutError):
        sender.send_revision(tmp_path, NOW, REVISION, transport=fail, env={})
    state = cr.read(tmp_path / sender.LEDGER)["anchors"][release["anchor"]]
    assert state["pending"]["revision"] == REVISION and "revisions" not in state
    # Both the ordinary sender and a second revision now refuse to proceed.
    assert sender.plan(tmp_path, NOW)[0]["action"] == "alert"
    assert "unconfirmed delivery attempt" in sender.plan_revision(tmp_path, NOW, "another")[0]["reason"]


def test_a_source_change_after_reservation_blocks_the_transport(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch)
    sender.prepare_revision(tmp_path, NOW, REVISION, reserve=True)
    cr.write(tmp_path / "data/breadth_csp1.json", {"end_date": "2099-01-01"})
    with pytest.raises(ValueError, match="source changed"):
        sender.send_revision(tmp_path, NOW, REVISION,
                             transport=lambda *_: pytest.fail("transport reached"), env={})


def test_a_tampered_payload_or_mismatched_identifier_is_refused(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch)
    sender.prepare_revision(tmp_path, NOW, REVISION, reserve=True)
    with pytest.raises(ValueError, match="does not match the reserved payload"):
        sender.send_revision(tmp_path, NOW, "a-different-identifier",
                             transport=lambda *_: pytest.fail("transport reached"), env={})
    candidate = cr.read(tmp_path / sender.OUT / "candidate.json")
    candidate["subject"] = "Something else entirely"
    cr.write(tmp_path / sender.OUT / "candidate.json", candidate)
    with pytest.raises(ValueError, match="payload changed after reservation"):
        sender.send_revision(tmp_path, NOW, REVISION,
                             transport=lambda *_: pytest.fail("transport reached"), env={})


def test_the_scheduled_sender_cannot_reach_the_revision_path(tmp_path, monkeypatch):
    settled(tmp_path, monkeypatch)
    sender.prepare_revision(tmp_path, NOW, REVISION, reserve=True)
    with pytest.raises(ValueError, match="not an ordinary factsheet stage"):
        sender.send(tmp_path, NOW, transport=lambda *_: pytest.fail("transport reached"), env={})
    assert sender.REVISION_ACTION not in sender.SEND_ACTIONS


def test_no_ordinary_decision_can_produce_a_revision(tmp_path, monkeypatch):
    """email_decision drives the schedule; it must never choose this action."""
    from component_publication import Snapshot
    release = install(tmp_path, monkeypatch, ready=True)
    europe = next(s for s in release["book"]["sleeves"] if s["sleeve"] == "D")
    for sent in ({}, {"anchor": release["anchor"], "regular": "all_ready"},
                 {"anchor": release["anchor"], "regular": "d_hold"}):
        decision = email_decision(NOW, anchor=release["anchor"],
            core=Snapshot(release["core_identity"], True, release["anchor"], release["anchor"]),
            europe=Snapshot(release["europe_identity"], release["d_ready"],
                            europe["last_completed_session"], europe["decision_session_for_fill"]),
            sent=sent)
        assert decision["action"] != sender.REVISION_ACTION


def test_revision_wording_promises_no_new_instruction():
    wording = email_wording({"action": "revision"})
    assert wording["subject"] == "Revised presentation - same portfolio instructions"
    assert "identical" in wording["summary"]
    assert "supersedes" in wording["difference"]
    assert "no new D selection" in wording["d_instruction"]


def test_revision_workflow_is_manual_and_reserves_before_send():
    workflow = (Path(__file__).resolve().parents[1]
                / ".github/workflows/factsheet_revision.yml").read_text(encoding="utf-8")
    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in triggers
    assert "push:" not in triggers and "schedule:" not in triggers
    assert workflow.index("Persist revision reservation") < workflow.index("Send the revised")
    assert workflow.index("Send the revised") < workflow.index("Record the confirmed revision")
    assert "secrets.RECIPIENT_EMAIL" in workflow and "default: true" in workflow
    assert "revise-send" in workflow and "--revision" in workflow
