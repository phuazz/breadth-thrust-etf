"""No-send integration rehearsal of the shipped sender, including its ledger.

Python months are 1-indexed. Fixtures are synthetic, never live trade output.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import math
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest
import component_release as cr
import send_component_factsheet as sender
from component_contract import expected_budgets, hold_rounding_residual
from component_basis import validate as validate_basis
from live_targets import _intended_lines, next_fill_date, decision_session_for
from nyse_sessions import week_final_anchor

NOW = datetime(2026, 9, 12, 6, tzinfo=timezone.utc)


def fixture_book(now=NOW, d_ready=False, gate=False, tilt=False, rounded_d=False):
    anchor = week_final_anchor(now).isoformat()
    overlay = {"as_of": anchor, "gate_input_date": anchor, "tilt_input_date": anchor,
               "gate_on": gate, "tilt_on": tilt, "gate_feed": "fixture"}
    overlay["weights"] = expected_budgets(overlay)
    names = dict(A="SPY", B="QQQ", C="SMH", D="EXV1")
    held = {(s, names[s]): w for s, w in zip("ABCD", (.35, .35, .1, .2))}
    if rounded_d:
        # The three D weights reproduce the failed production baseline.
        held.pop(("D", "EXV1"))
        held.update({("D", "EXV1"): .07558, ("D", "EXV3"): .06318,
                     ("D", "EXH1"): .06126})
        held[("A", "SPY")] += .000035  # reproduce total held NAV 1.000055
    sleeves = []
    for s in "ABCD":
        venue = "XETR" if s == "D" else "NYSE"
        fill = next_fill_date(venue, now)
        decision = decision_session_for(venue, fill)
        ready = s != "D" or d_ready
        sleeves.append({"sleeve": s, "venue": venue, "status": "READY" if ready else "HOLD",
            "decision_session": decision if ready else (datetime.fromisoformat(decision) - timedelta(days=1)).date().isoformat(),
            "decision_session_for_fill": decision, "last_completed_session": decision,
            "fill_date": fill, "weights": {names[s]: 1.0}, "reason": None if ready else "vendor tail incomplete"})
    basis = {"anchor": anchor, "model_as_of": anchor,
             "lines": [{"sleeve": s, "etf": e, "held": w} for (s, e), w in held.items()]}
    book = {"as_of": anchor, "computed_at_utc": now.isoformat(), "executed": False,
            "targets_final": d_ready, "sleeves": sleeves, "overlay_decision": overlay,
            "lines": _intended_lines(sleeves, held, overlay["weights"], adjust_overlays=True)}
    book["rounding_residual_nav"] = hold_rounding_residual(sleeves, book["lines"], overlay["weights"])
    return book, basis


def install(root, monkeypatch, *, now=NOW, ready=False, gate=False, rounded_d=False):
    book, basis = fixture_book(now, ready, gate, rounded_d=rounded_d)
    for p in cr.source_paths(root):
        cr.write(p, {})
    cr.write(root / "data/live_targets.json", book)
    cr.write(root / "data/component_held_basis.json", basis)
    cr.write(root / "data/overlay_decision.json", book["overlay_decision"])
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
    for s, universe in ((book["sleeves"][0], UNIVERSE_ETFS), (book["sleeves"][-1], UNIVERSE_EUROPE_SECTORS)):
        for name in universe:
            cr.write(root / f"data/breadth_{name.lower()}.json", {"end_date": s["decision_session"]})
    prices = {r["etf"]: {"dates": [book["as_of"]], "prices": [100]} for r in book["lines"]}
    cr.write(root / "data/holdings_prices_1y.json", {"prices": prices})
    stats = {"as_of": book["as_of"], "series": "synthetic-rehearsal", "wtd_start": book["as_of"],
             "values": {"WTD": .01, "YTD": .02, "1Y": None, "Sharpe": 1., "Max drawdown": -.1}}
    labels = {r["etf"]: f"Synthetic fund {r['etf']}" for r in book["lines"]}
    monkeypatch.setattr(cr, "performance", lambda _root, *a: (stats, labels))
    # Local capture guards use private caches; this fixture tests the contract
    # and sender. Separate tests below prove a failing guard cannot seal.
    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    return cr.seal(root, now=now)


def test_no_send_rehearsal_writes_preview_but_no_ledger(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    assert sender.prepare(tmp_path, NOW)["action"] == "preview"
    assert (tmp_path / sender.OUT / "preview.html").exists()
    assert not (tmp_path / sender.LEDGER).exists()


def test_preview_then_hold_consolidation_are_deduplicated(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    sent = []
    sender.prepare(tmp_path, NOW, reserve=True)
    assert sender.plan(tmp_path, NOW)[0]["action"] == "alert"
    sender.send(tmp_path, NOW, transport=lambda c, _: sent.append(c), env={})
    assert sender.prepare(tmp_path, NOW)["action"] == "wait"
    sunday = datetime(2026, 9, 13, 10, tzinfo=timezone.utc)
    assert sender.prepare(tmp_path, sunday, reserve=True)["action"] == "regular"
    sender.send(tmp_path, sunday, transport=lambda c, _: sent.append(c), env={})
    assert len(sent) == 2
    assert sender.prepare(tmp_path, sunday)["action"] == "wait"
    assert cr.read(tmp_path / "docs/factsheet_published.json")["anchor"] == "2026-09-11"


def test_all_ready_sends_once_without_preview(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, ready=True)
    assert sender.prepare(tmp_path, NOW, reserve=True)["action"] == "regular"
    sent = []
    sender.send(tmp_path, NOW, transport=lambda c, _: sent.append(c), env={})
    assert len(sent) == 1
    assert sender.plan(tmp_path, NOW)[0]["action"] == "wait"


def test_uncertain_transport_is_never_automatically_retried(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    sender.prepare(tmp_path, NOW, reserve=True)
    def fail(*_):
        raise TimeoutError("SMTP outcome unknown")
    with pytest.raises(TimeoutError):
        sender.send(tmp_path, NOW, transport=fail, env={})
    assert sender.plan(tmp_path, NOW)[0]["action"] == "alert"
    assert cr.read(tmp_path / sender.LEDGER)["anchors"]["2026-09-11"]["pending"]


def test_changed_source_between_reservation_and_send_blocks_transport(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    sender.prepare(tmp_path, NOW, reserve=True)
    cr.write(tmp_path / "data/breadth_csp1.json", {"end_date": "2099-01-01"})
    with pytest.raises(ValueError, match="source changed"):
        sender.send(tmp_path, NOW, transport=lambda *_: pytest.fail("transport reached"))


def test_changed_core_after_preview_requires_review(tmp_path, monkeypatch):
    release = install(tmp_path, monkeypatch)
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {release["anchor"]:
        {"anchor": release["anchor"], "preview": "old", "core": "different-core"}}})
    assert sender.plan(tmp_path, NOW)[0]["action"] == "alert"


def test_one_d_followup_after_hold(tmp_path, monkeypatch):
    release = install(tmp_path, monkeypatch, ready=True)
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {release["anchor"]:
        {"anchor": release["anchor"], "regular": "d_hold", "core": release["core_identity"]}}})
    at = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
    assert sender.prepare(tmp_path, at, reserve=True)["action"] == "d_update"
    sender.send(tmp_path, at, transport=lambda *_: None)
    assert sender.plan(tmp_path, at)[0]["action"] == "wait"


@pytest.mark.parametrize("now", [datetime(2026, 8, 1, 6, tzinfo=timezone.utc),
                                  datetime(2027, 1, 2, 6, tzinfo=timezone.utc),
                                  datetime(2026, 6, 20, 6, tzinfo=timezone.utc)])
def test_shipped_contract_month_and_year_boundaries(now):
    book, basis = fixture_book(now)
    assert cr.validate_book(book, basis, now)["anchor"] == week_final_anchor(now).isoformat()


@pytest.mark.parametrize("gate,tilt", [(False, False), (True, False), (False, True), (True, True)])
def test_overlay_and_hold_preserve_budget_and_selection(gate, tilt):
    book, basis = fixture_book(gate=gate, tilt=tilt)
    assert cr.validate_book(book, basis, NOW)["d_ready"] is False
    assert math.isclose(sum(r["target"] for r in book["lines"]), 1.)
    assert {r["etf"] for r in book["lines"] if r["sleeve"] == "D"} == {"EXV1"}


@pytest.mark.parametrize("mutation", ["duplicate", "nan", "stale_overlay", "bad_delta", "missing_exit", "wrong_fill", "core_hold"])
def test_corrupt_book_fails_closed(mutation):
    book, basis = fixture_book(gate=True)
    if mutation == "duplicate": book["lines"].append(deepcopy(book["lines"][0]))
    if mutation == "nan": book["lines"][0]["target"] = float("nan")
    if mutation == "stale_overlay": book["overlay_decision"]["gate_input_date"] = "2026-09-10"
    if mutation == "bad_delta": book["lines"][0]["delta"] = .7
    if mutation == "missing_exit": book["lines"].pop(0)
    if mutation == "wrong_fill": book["sleeves"][0]["fill_date"] = "2026-09-15"
    if mutation == "core_hold": book["sleeves"][0]["status"] = "HOLD"
    with pytest.raises((ValueError, KeyError)):
        cr.validate_book(book, basis, NOW)


def test_negative_basis_is_not_hidden_by_filtering():
    _, basis = fixture_book()
    basis["lines"].append({"sleeve": "D", "etf": "bad", "held": -1})
    with pytest.raises(ValueError):
        validate_basis(basis, basis["anchor"])


def test_sender_uses_all_configured_recipients_and_reports_partial_failure(monkeypatch):
    observed = {}
    class SMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, *a): pass
        def send_message(self, msg, **kw):
            observed.update(kw)
            return {"second@example.invalid": (550, b"fixture refusal")}
    monkeypatch.setattr(sender.smtplib, "SMTP_SSL", SMTP)
    with pytest.raises(RuntimeError, match="Some recipients"):
        sender.smtp_send({"id": "fixture", "subject": "test", "html": "<p>test</p>",
                          "book_html": "<p>Complete synthetic book</p>",
                          "book": {}, "decision": {"action": "preview"}},
            {"GMAIL_USER": "sender@example.invalid", "GMAIL_APP_PASSWORD": "fixture",
             "RECIPIENT_EMAIL": "first@example.invalid, second@example.invalid"})
    assert observed["to_addrs"] == ["first@example.invalid", "second@example.invalid"]


def test_workflow_has_no_rebuild_and_reserves_before_send():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/weekly_factsheet.yml").read_text(encoding="utf-8")
    assert "data/component_release.json" in workflow
    assert "run_asset_class_rotation.py" not in workflow
    assert workflow.index("Persist reservation") < workflow.index("Send once") < workflow.index("Persist confirmed")
    assert "secrets.RECIPIENT_EMAIL" in workflow and "default: true" in workflow


def test_committed_snapshot_survives_daily_source_updates(tmp_path, monkeypatch):
    real_run = subprocess.run
    install(tmp_path, monkeypatch)
    monkeypatch.setattr(cr.subprocess, "run", real_run)
    for args in (["init", "-q"], ["config", "user.name", "Fixture"],
                 ["config", "user.email", "fixture@example.invalid"],
                 ["add", "."], ["commit", "-qm", "Synthetic release"]):
        real_run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    # A routine valuation write must not alter the source of a waiting email.
    cr.write(tmp_path / "data/live_track.json", {"daily": "new value"})
    assert cr.verify(tmp_path, NOW, committed=True)["anchor"] == "2026-09-11"
    with pytest.raises(ValueError, match="source changed"):
        cr.verify(tmp_path, NOW)


def test_local_capture_guard_failure_prevents_seal(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    (tmp_path / cr.MANIFEST).unlink()
    def failed(*args, **kw):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(cr.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        cr.seal(tmp_path, now=NOW)
    assert not (tmp_path / cr.MANIFEST).exists()
