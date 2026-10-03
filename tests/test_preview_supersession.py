"""Synthetic fail-closed delivery tests; all SMTP is injected, never real.

Python datetime months are 1-indexed. No production files are used.
"""
import copy
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import component_release as cr
import preview_supersession as ps
import send_component_factsheet as sender
from component_publication import Snapshot, email_decision
from test_component_sender import fixture_book, NOW
from test_factsheet_restatement import install_book, restated_book

TO = ["owner@example.invalid", "colleague@example.invalid"]


@pytest.fixture
def sample(tmp_path, monkeypatch):
    old, basis = fixture_book(NOW, d_ready=False, hold=("C",))
    prior = install_book(tmp_path / "old", monkeypatch, old, basis)
    current = install_book(tmp_path, monkeypatch, *restated_book())
    original = {"anchor": prior["anchor"], "preview": prior["identity"],
                "core": prior["core_identity"], "europe": prior["europe_identity"],
                "last_confirmed_at": NOW.isoformat()}
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {prior["anchor"]: original}})
    options = dict(expected_preview=prior["identity"], authority="Owner reviewed synthetic correction",
                   supersession="preview-2026-09-11-v1", allowed_holds=[],
                   followup_policy="suppress_unchanged", to=TO, now=NOW, committed=False, prior=prior)
    return tmp_path, prior, current, original, options


def test_derived_changes_and_original_receipt(sample):
    root, prior, current, original, options = sample
    d, release = ps.plan(root, **options)
    assert d["released_holds"] == ["C", "D"]
    assert d["original_delivery_state"] == original
    assert d["expected_preview"] == prior["identity"]
    assert release == current and d["restated_lines"]
    assert "regular" not in sender.ledger_at(root)["anchors"][prior["anchor"]]


@pytest.mark.parametrize("change,reason", [
    ({"authority": ""}, "authority"), ({"supersession": "bad id"}, "identifier"),
    ({"followup_policy": None}, "policy"), ({"expected_preview": "wrong"}, "preview"),
    ({"allowed_holds": ["C"]}, "HOLD"), ({"to": []}, "recipient"),
    ({"to": [TO[0], TO[0]]}, "recipient"),
])
def test_explicit_review_scope_required(sample, change, reason):
    root, _, _, _, options = sample
    with pytest.raises(ValueError, match=reason):
        ps.plan(root, **(options | change))


@pytest.mark.parametrize("field,value", [
    ("regular", "d_hold"), ("d_update", "yes"), ("restatements", {"earlier": {}}),
    ("pending", {"id": "uncertain", "action": "preview"}),
    ("preview_supersessions", {"already": {}}), ("last_confirmed_at", ""),
])
def test_ineligible_delivery_state_refused(sample, field, value):
    root, prior, _, _, options = sample
    ledger = sender.ledger_at(root)
    ledger["anchors"][prior["anchor"]][field] = value
    cr.write(root / sender.LEDGER, ledger)
    with pytest.raises(ValueError):
        ps.plan(root, **options)


def test_operator_hold_and_tampered_prior_refused(sample):
    root, prior, _, _, options = sample
    cr.write(root / "docs/factsheet_hold.json", {"reason": "test"})
    with pytest.raises(ValueError, match="operator hold"):
        ps.plan(root, **options)
    (root / "docs/factsheet_hold.json").unlink()
    bad = copy.deepcopy(prior)
    bad["book"]["lines"].pop()
    with pytest.raises(ValueError, match="reconstruct"):
        ps.plan(root, **(options | {"prior": bad}))


def test_remaining_hold_requires_exact_reviewed_allowlist(tmp_path, monkeypatch):
    old, basis = fixture_book(NOW, d_ready=False, hold=("C",))
    prior = install_book(tmp_path / "old", monkeypatch, old, basis)
    current, basis = restated_book()
    # Retain D's actual HOLD from the original fixture; only C recovers.
    current["sleeves"][-1] = old["sleeves"][-1]
    current["lines"] = [r for r in current["lines"] if r["sleeve"] != "D"] + [r for r in old["lines"] if r["sleeve"] == "D"]
    current["targets_final"] = False
    install_book(tmp_path, monkeypatch, current, basis)
    state = {"anchor": prior["anchor"], "preview": prior["identity"], "core": prior["core_identity"],
             "europe": prior["europe_identity"], "last_confirmed_at": NOW.isoformat()}
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {prior["anchor"]: state}})
    options = dict(expected_preview=prior["identity"], authority="Owner reviewed D HOLD",
                   supersession="partial-v1", allowed_holds=["D"], followup_policy="suppress_unchanged",
                   to=TO, now=NOW, committed=False, prior=prior)
    d, _ = ps.plan(tmp_path, **options)
    assert d["d_hold"] and d["allowed_holds"] == ["D"]
    path = ps.prepare(tmp_path, **options)
    payload = cr.read(path)
    assert "Strategy D remains HOLD" in payload["text"]
    assert "Remaining HOLDs: D" in payload["html"]
    with pytest.raises(ValueError, match="HOLD"):
        ps.plan(tmp_path, **(options | {"allowed_holds": []}))


def test_full_prepare_reserve_send_preserves_preview_and_blocks_duplicates(sample):
    root, prior, current, original, options = sample
    path = ps.prepare(root, **options)
    c = cr.read(path)
    assert "initial preview" in c["subject"]
    assert "initial preview" in c["html"]
    assert "audit history" in c["text"]
    check = dict(now=NOW, committed=False, prior=prior)
    ps.reserve(root, path, c["id"], **check)
    sent = []
    ps.send(root, path, c["id"], transport=lambda c, e: sent.append(c),
            env={"RECIPIENT_EMAIL": ",".join(TO)}, **check)
    state = sender.ledger_at(root)["anchors"][prior["anchor"]]
    assert len(sent) == 1 and "regular" not in state and "d_update" not in state
    assert state["preview"] == original["preview"]
    assert state["last_confirmed_at"] == original["last_confirmed_at"]
    receipt = state["preview_supersessions"][options["supersession"]]
    assert receipt["decision"]["original_delivery_state"] == original
    assert receipt["recipient_fingerprint"] == cr.digest(sorted(TO))
    assert all(address not in json.dumps(state) for address in TO)
    assert state["core"] == current["core_identity"]
    with pytest.raises(ValueError, match="already exists"):
        ps.send(root, path, c["id"], transport=lambda c, e: sent.append(c), env={}, **check)
    assert len(sent) == 1
    decision = sender.plan(root, NOW, committed=False)[0]
    assert decision["action"] == "wait" and "suppressed" in decision["reason"]


@pytest.mark.parametrize("fault", ["payload", "approval", "recipient", "transport", "receipt"])
def test_fail_closed_delivery_and_uncertainty_lock(sample, monkeypatch, fault):
    root, prior, _, _, options = sample
    path = ps.prepare(root, **options)
    c = cr.read(path)
    check = dict(now=NOW, committed=False, prior=prior)
    ps.reserve(root, path, c["id"], **check)
    sent = []
    def transport(payload, env):
        sent.append(payload)
        if fault == "transport":
            raise RuntimeError("uncertain SMTP outcome")
    if fault == "payload":
        cr.write(path, c | {"subject": "tampered"})
    if fault == "receipt":
        original_write = cr.write
        def failed_write(path, value):
            if value["anchors"][prior["anchor"]].get("preview_supersession"):
                raise OSError("simulated receipt write failure")
            return original_write(path, value)
        monkeypatch.setattr(cr, "write", failed_write)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        ps.send(root, path, "wrong" if fault == "approval" else c["id"], transport=transport,
                env={"RECIPIENT_EMAIL": "other@example.invalid" if fault == "recipient" else ",".join(TO)}, **check)
    state = sender.ledger_at(root)["anchors"][prior["anchor"]]
    assert state["pending"]["id"] == c["id"]
    assert len(sent) == (1 if fault in ("transport", "receipt") else 0)
    if fault in ("transport", "receipt"):
        assert state["pending"]["attempted_at"]
        with pytest.raises(ValueError, match="already attempted"):
            ps.send(root, path, c["id"], transport=transport,
                    env={"RECIPIENT_EMAIL": ",".join(TO)}, **check)
        assert len(sent) == 1


def test_changed_source_or_reviewed_policy_after_reservation_refused(sample):
    root, prior, _, _, options = sample
    path = ps.prepare(root, **options)
    c = cr.read(path)
    check = dict(now=NOW, committed=False, prior=prior)
    ps.reserve(root, path, c["id"], **check)
    cr.write(root / "data/live_targets.json", {})
    with pytest.raises(ValueError, match="sealed source changed"):
        ps.send(root, path, c["id"], transport=lambda *_: pytest.fail("must not send"),
                env={"RECIPIENT_EMAIL": ",".join(TO)}, **check)


@pytest.mark.parametrize("now", [datetime(2026, 10, 3, 3, tzinfo=timezone.utc),
                                 datetime(2027, 1, 2, 3, tzinfo=timezone.utc)])
def test_calendar_month_and_year_boundaries(now):
    from component_publication import review_window
    regular, end = review_window(now)
    assert regular.weekday() == 6 and end.weekday() == 0
    assert regular < end and (end - regular) == timedelta(hours=12)


def test_changed_d_after_supersession_alerts_even_before_regular(sample):
    root, prior, current, original, options = sample
    state = original | {"core": current["core_identity"], "europe": current["europe_identity"],
        "preview_supersession": {"protocol_version": 2, "d_hold": False, "confirmed_at": NOW.isoformat(), "core": current["core_identity"],
                                 "europe": current["europe_identity"], "followup_policy": "suppress_unchanged"}}
    anchor = current["anchor"]
    d = email_decision(NOW, anchor=anchor, core=Snapshot(current["core_identity"], True, anchor, anchor),
        europe=Snapshot("changed", True, anchor, anchor), sent=state)
    assert d["action"] == "alert" and "D changed" in d["reason"]


@pytest.mark.parametrize("fault", [None, "remote", "dirty", "push"])
def test_live_send_protocol_persists_attempt_before_smtp(sample, monkeypatch, fault):
    root, prior, current, _, options = sample
    path = ps.prepare(root, **options)
    c = cr.read(path)
    ps.reserve(root, path, c["id"], now=NOW, committed=False, prior=prior)
    remote = sender.ledger_at(root)
    if fault == "remote":
        remote["anchors"][prior["anchor"]].pop("pending")
    events = []
    def git(cmd, **kwargs):
        events.append(cmd[1])
        if cmd[1] == "show":
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(remote).encode())
        if cmd[1] == "status":
            return subprocess.CompletedProcess(cmd, 0, stdout=" M unrelated.py" if fault == "dirty" else "")
        assert sender.ledger_at(root)["anchors"][prior["anchor"]]["pending"]["attempted_at"]
        if cmd[1] == "push" and fault == "push":
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cr, "verify", lambda *a, **kw: current)
    monkeypatch.setattr(ps.subprocess, "run", git)
    def transport(*_):
        events.append("smtp")
        assert events[-4:] == ["add", "commit", "push", "smtp"]
    kwargs = dict(now=NOW, committed=True, prior=prior, transport=transport,
                  env={"RECIPIENT_EMAIL": ",".join(TO)})
    if fault:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            ps.send(root, path, c["id"], **kwargs)
        assert "smtp" not in events
    else:
        ps.send(root, path, c["id"], **kwargs)
        assert events.count("smtp") == 1
