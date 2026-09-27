"""One restated instruction for an already-delivered anchor, on owner authority.

The restatement route exists to deliver a CHANGED instruction after the weekly
factsheet went out: the 2026-09-25 anchor was sent with sleeve C on HOLD for a
late BTC-USD bar, the bar arrived, and the owner had the book restated. These
tests prove the route refuses by default, derives what changed rather than
asserting it, sends once, and leaves the scheduled sender quiet afterwards.
Python datetime months are 1-indexed. Fixtures are synthetic.
"""
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import component_release as cr
import send_component_factsheet as sender
from component_contract import hold_rounding_residual
from component_publication import email_wording
from live_targets import _intended_lines
from test_component_sender import fixture_book, NOW

RESTATEMENT = "restated-c-2026-09-11"
AUTHORITY = "Owner instruction: Strategy C HOLD cleared once the late bar was served"
# Monday 07:00 SGT: past the review checkpoint, which a restatement ignores.
AFTER_CHECKPOINT = datetime(2026, 9, 13, 23, tzinfo=timezone.utc)
# The restated C line: the fixture's BTC-USD is traded as IBIT, as in production.
NEW_C = "BTC-USD"


def install_book(root, monkeypatch, book, basis, now=NOW):
    """test_component_sender.install, for a book the caller built."""
    for p in cr.source_paths(root):
        cr.write(p, {})
    cr.write(root / "data/live_targets.json", book)
    cr.write(root / "data/component_held_basis.json", basis)
    cr.write(root / "data/overlay_decision.json", book["overlay_decision"])
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
    for s, universe in ((book["sleeves"][0], UNIVERSE_ETFS), (book["sleeves"][-1], UNIVERSE_EUROPE_SECTORS)):
        for name in universe:
            cr.write(root / f"data/breadth_{name.lower()}.json", {"end_date": s["decision_session"]})
    prices = {}
    for r in book["lines"]:
        # Quote the traded instrument as well as the key: BTC-USD prices as IBIT.
        for key in {r["etf"], r["traded"]}:
            prices[key] = {"dates": [book["as_of"]], "prices": [100]}
    cr.write(root / "data/holdings_prices_1y.json", {"prices": prices})
    stats = {"as_of": book["as_of"], "series": "synthetic-rehearsal", "wtd_start": book["as_of"],
             "values": {"WTD": .01, "YTD": .02, "1Y": None, "Sharpe": 1., "Max drawdown": -.1}}
    labels = {r["etf"]: f"Synthetic fund {r['etf']}" for r in book["lines"]}
    monkeypatch.setattr(cr, "performance", lambda _root, *a: (stats, labels))
    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    return cr.seal(root, now=now)


def restated_book(now=NOW):
    """All-ready book whose C sleeve ranks into BTC-USD instead of the held SMH."""
    book, basis = fixture_book(now, d_ready=True)
    c = next(s for s in book["sleeves"] if s["sleeve"] == "C")
    c["weights"] = {NEW_C: 1.0}
    held = {(r["sleeve"], r["etf"]): r["held"] for r in basis["lines"]}
    book["lines"] = _intended_lines(book["sleeves"], held, book["overlay_decision"]["weights"],
                                    adjust_overlays=True)
    book["rounding_residual_nav"] = hold_rounding_residual(
        book["sleeves"], book["lines"], book["overlay_decision"]["weights"])
    return book, basis


@pytest.fixture
def delivered(tmp_path, monkeypatch):
    """(prior release sealed elsewhere, current release in tmp_path)."""
    held_book, held_basis = fixture_book(NOW, d_ready=True, hold=("C",))
    prior = install_book(tmp_path / "delivered", monkeypatch, held_book, held_basis)
    current = install_book(tmp_path, monkeypatch, *restated_book())
    return prior, current


def settle(tmp_path, prior, **ledger):
    """The ordinary weekly delivery completed on the PRIOR (held) book."""
    state = {"anchor": prior["anchor"], "core": prior["core_identity"],
             "europe": prior["europe_identity"], "preview": "an-earlier-identity",
             "regular": "all_ready", "last_confirmed_at": NOW.isoformat()}
    state.update(ledger)
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {prior["anchor"]: state}})
    return state


def plan(tmp_path, prior, now=NOW, restatement=RESTATEMENT, authority=AUTHORITY, **kw):
    return sender.plan_restatement(tmp_path, now, restatement, authority,
                                   prior_release=prior, **kw)[0]


def deliver(tmp_path, prior, now=NOW):
    sent = []
    sender.prepare_restatement(tmp_path, now, RESTATEMENT, AUTHORITY, reserve=True,
                               prior_release=prior)
    sender.send_restatement(tmp_path, now, RESTATEMENT, AUTHORITY,
                            transport=lambda c, _: sent.append(c), env={}, prior_release=prior)
    return sent


# --- the difference is derived ------------------------------------------

def test_the_restated_lines_and_released_hold_are_derived(tmp_path, delivered):
    prior, current = delivered
    assert prior["core_identity"] != current["core_identity"]
    assert prior["europe_identity"] == current["europe_identity"]
    settle(tmp_path, prior)
    decision = plan(tmp_path, prior)
    assert decision["action"] == sender.RESTATEMENT_ACTION
    assert decision["released_holds"] == ["C"] and decision["imposed_holds"] == []
    lines = {r["etf"]: r for r in decision["restated_lines"]}
    assert set(lines) == {"SMH", NEW_C}
    assert {r["sleeve"] for r in lines.values()} == {"C"}
    assert lines["SMH"]["new_target"] == 0 and lines["SMH"]["prior_target"] > 0
    assert lines[NEW_C]["prior_target"] == 0
    assert lines[NEW_C]["new_target"] == pytest.approx(lines["SMH"]["prior_target"])
    assert lines[NEW_C]["traded"] == "IBIT"
    assert decision["supersedes"] == {"core": prior["core_identity"],
                                      "europe": prior["europe_identity"]}
    assert decision["authority"] == AUTHORITY


def test_a_missing_delivered_release_is_refused(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    decision, _ = sender.plan_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY)
    assert decision["action"] == "blocked"
    assert "cannot be reconstructed" in decision["reason"]


def test_a_prior_release_that_is_not_the_delivered_one_is_refused(tmp_path, delivered):
    prior, current = delivered
    settle(tmp_path, prior)
    # The current release, offered as the delivered one, does not reconstruct.
    assert "cannot be reconstructed" in plan(tmp_path, current)["reason"]
    # Nor does a delivered release whose book was altered under its label.
    tampered = {**prior, "book": {**prior["book"], "lines": prior["book"]["lines"][:-1]}}
    assert "cannot be reconstructed" in plan(tmp_path, tampered)["reason"]


def test_the_delivered_release_is_found_in_git_history(tmp_path):
    """The committed walk: newest first, matched on both delivered identities."""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    seals = [{"anchor": "2026-09-04", "core_identity": "old", "europe_identity": "e"},
             {"anchor": "2026-09-11", "core_identity": "held", "europe_identity": "e"},
             {"anchor": "2026-09-11", "core_identity": "restated", "europe_identity": "e"}]
    for seal in seals:
        cr.write(root / cr.MANIFEST, seal)
        git("add", cr.MANIFEST)
        git("commit", "-q", "-m", seal["core_identity"])
    assert sender.delivered_release(root, "2026-09-11", "held", "e") == seals[1]
    assert sender.delivered_release(root, "2026-09-11", "held", "other") is None
    # A match from an earlier week is never taken.
    assert sender.delivered_release(root, "2026-09-11", "old", "e") is None


# --- every guard refuses ------------------------------------------------

def test_identical_identities_are_refused(tmp_path, delivered):
    prior, current = delivered
    settle(tmp_path, prior, core=current["core_identity"], europe=current["europe_identity"])
    assert "instruction unchanged; use a presentation revision" in plan(tmp_path, prior)["reason"]


def test_missing_authority_is_refused(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    for authority in ("", "   ", None):
        decision = plan(tmp_path, prior, authority=authority)
        assert decision["action"] == "blocked" and "authority" in decision["reason"]


@pytest.mark.parametrize("identifier", ["", "   ", "../escape", "has space", "a" * 65])
def test_an_unusable_identifier_is_refused(tmp_path, delivered, identifier):
    prior, _ = delivered
    settle(tmp_path, prior)
    assert "restatement identifier" in plan(tmp_path, prior, restatement=identifier)["reason"]


def test_operator_hold_is_refused(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    cr.write(tmp_path / "docs/factsheet_hold.json", {"reason": "operator review"})
    assert "operator hold" in plan(tmp_path, prior)["reason"]


def test_an_outstanding_attempt_is_refused(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior, pending={"id": "someone-elses", "action": "regular"})
    assert "unconfirmed delivery attempt" in plan(tmp_path, prior)["reason"]


def test_an_undelivered_or_unsettled_anchor_is_refused(tmp_path, delivered):
    prior, _ = delivered
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {}})
    assert "no confirmed delivery" in plan(tmp_path, prior)["reason"]
    settle(tmp_path, prior, regular=None)
    assert "has not completed" in plan(tmp_path, prior)["reason"]
    settle(tmp_path, prior, regular="d_hold")
    assert "D follow-up" in plan(tmp_path, prior)["reason"]
    # A settled D follow-up is a settled week.
    settle(tmp_path, prior, regular="d_hold", d_update="an-identity")
    assert plan(tmp_path, prior)["action"] == sender.RESTATEMENT_ACTION


def test_a_release_with_d_on_hold_is_refused(tmp_path, monkeypatch):
    held_book, held_basis = fixture_book(NOW, d_ready=False, hold=("C",))
    prior = install_book(tmp_path / "delivered", monkeypatch, held_book, held_basis)
    book, basis = fixture_book(NOW, d_ready=False)
    install_book(tmp_path, monkeypatch, book, basis)
    settle(tmp_path, prior)
    assert "D is not verified" in plan(tmp_path, prior)["reason"]


def test_a_second_restatement_is_refused(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    deliver(tmp_path, prior)
    assert "was already delivered" in plan(tmp_path, prior)["reason"]
    decision = plan(tmp_path, prior, restatement="restated-again")
    assert decision["action"] == "blocked"
    # The identities now match the restated book, and the one-per-anchor rule
    # stands regardless: either refusal is final, and there is no waiver.
    assert "already delivered" in decision["reason"] or "unchanged" in decision["reason"]
    state = cr.read(tmp_path / sender.LEDGER)["anchors"][prior["anchor"]]
    state["core"] = prior["core_identity"]
    cr.write(tmp_path / sender.LEDGER, {"schema": 1, "anchors": {prior["anchor"]: state}})
    assert "a restatement was already delivered" in plan(
        tmp_path, prior, restatement="restated-again")["reason"]


def test_a_stale_fill_date_is_refused(tmp_path, delivered, monkeypatch):
    prior, current = delivered
    settle(tmp_path, prior)
    stale = {**current, "book": {**current["book"], "sleeves": [
        {**s, "fill_date": "2026-09-01"} for s in current["book"]["sleeves"]]}}
    monkeypatch.setattr(sender, "verify", lambda *a, **kw: stale)
    assert "fill date has passed" in plan(tmp_path, prior)["reason"]


def test_the_review_checkpoint_does_not_refuse_a_restatement(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    assert plan(tmp_path, prior, now=AFTER_CHECKPOINT)["action"] == sender.RESTATEMENT_ACTION


# --- the scheduled sender cannot reach it -------------------------------

def test_the_scheduled_sender_cannot_reach_the_restatement(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    assert sender.RESTATEMENT_ACTION not in sender.SEND_ACTIONS
    # Before the restatement the scheduler only alerts the operator.
    decision = sender.plan(tmp_path, NOW)[0]
    assert decision["action"] == "alert" and "core changed" in decision["reason"]
    sender.prepare_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY, reserve=True,
                               prior_release=prior)
    with pytest.raises(ValueError, match="not an ordinary factsheet stage"):
        sender.send(tmp_path, NOW, transport=lambda *_: pytest.fail("transport reached"), env={})
    # A reserved restatement blocks the scheduler like any unconfirmed attempt.
    assert sender.plan(tmp_path, NOW)[0]["action"] == "alert"


def test_a_dry_run_writes_no_reservation(tmp_path, delivered):
    prior, _ = delivered
    before = settle(tmp_path, prior)
    decision = sender.prepare_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY,
                                          prior_release=prior)
    assert decision["action"] == sender.RESTATEMENT_ACTION
    assert (tmp_path / sender.OUT / "preview.html").exists()
    assert (tmp_path / sender.OUT / f"factsheet_{prior['anchor']}_restatement-{RESTATEMENT}.pdf").exists()
    assert cr.read(tmp_path / sender.LEDGER)["anchors"][prior["anchor"]] == before


# --- the send and its receipt -------------------------------------------

def test_a_confirmed_restatement_records_its_receipt_and_moves_the_identities(tmp_path, delivered):
    prior, current = delivered
    before = settle(tmp_path, prior)
    sent = deliver(tmp_path, prior)
    assert len(sent) == 1
    assert sent[0]["subject"].startswith("Restated instruction - supersedes this week's factsheet")
    state = cr.read(tmp_path / sender.LEDGER)["anchors"][prior["anchor"]]
    receipt = state["restatements"][RESTATEMENT]
    assert receipt["authority"] == AUTHORITY
    assert receipt["supersedes"] == {"core": prior["core_identity"], "europe": prior["europe_identity"]}
    assert receipt["release"] == current["identity"]
    assert receipt["candidate"] == sent[0]["id"] and receipt["subject"] == sent[0]["subject"]
    assert receipt["confirmed_at"] == NOW.isoformat()
    assert "pending" not in state
    assert state["core"] == current["core_identity"] and state["europe"] == current["europe_identity"]
    # The ordinary stages' evidence is untouched.
    for key in ("regular", "preview", "last_confirmed_at"):
        assert state[key] == before[key]
    assert not (tmp_path / "docs/factsheet_published.json").exists()


def test_after_a_restatement_the_scheduled_plan_is_quiet(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    deliver(tmp_path, prior)
    for at in (NOW, datetime(2026, 9, 13, 10, tzinfo=timezone.utc)):
        decision = sender.plan(tmp_path, at)[0]
        assert decision["action"] == "wait"
        assert decision["reason"] == "already distributed or update window closed"
        assert sender.prepare(tmp_path, at, reserve=True)["action"] == "wait"
    assert "pending" not in cr.read(tmp_path / sender.LEDGER)["anchors"][prior["anchor"]]


def test_an_uncertain_transport_leaves_the_reservation_and_moves_nothing(tmp_path, delivered):
    prior, _ = delivered
    before = settle(tmp_path, prior)
    sender.prepare_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY, reserve=True,
                               prior_release=prior)
    def fail(*_):
        raise TimeoutError("SMTP outcome unknown")
    with pytest.raises(TimeoutError):
        sender.send_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY, transport=fail, env={},
                                prior_release=prior)
    state = cr.read(tmp_path / sender.LEDGER)["anchors"][prior["anchor"]]
    assert state["pending"]["restatement"] == RESTATEMENT and "restatements" not in state
    assert state["core"] == before["core"]
    assert sender.plan(tmp_path, NOW)[0]["action"] == "alert"


def test_authority_cannot_change_between_reservation_and_send(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    sender.prepare_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY, reserve=True,
                               prior_release=prior)
    for other in ("a different reason entirely", ""):
        with pytest.raises(ValueError, match="authority does not match"):
            sender.send_restatement(tmp_path, NOW, RESTATEMENT, other,
                                    transport=lambda *_: pytest.fail("transport reached"),
                                    env={}, prior_release=prior)


def test_the_transport_needs_a_matching_reservation(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    sender.prepare_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY, prior_release=prior)
    with pytest.raises(ValueError, match="no matching durable reservation"):
        sender.send_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY,
                                transport=lambda *_: pytest.fail("transport reached"),
                                env={}, prior_release=prior)


# --- wording and banner -------------------------------------------------

CONTRACTION = re.compile(r"\b\w+(n't|'re|'ll|'ve|'m|'d)\b|\b(it's|that's|there's|what's|here's)\b", re.I)
ERROR_WORDS = ("error", "mistake", "incorrect", "wrong")


def test_the_wording_is_plain_and_implies_no_error(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    wording = email_wording(plan(tmp_path, prior))
    assert wording["subject"] == "Restated instruction - supersedes this week's factsheet"
    assert wording["heading"] == "Restated for this week: Strategy C is now ready"
    assert wording["difference"].startswith(
        "The factsheet sent earlier for this week showed Strategy C on HOLD because its "
        "required data was incomplete, as it stated.")
    assert "supersedes the earlier instructions" in wording["difference"]
    for text in wording.values():
        assert not CONTRACTION.search(text), text
        assert not any(w in text.lower() for w in ERROR_WORDS), text


def test_the_wording_names_whichever_sleeves_were_released():
    decision = {"action": "restatement", "released_holds": ["B", "D"], "restated_lines": []}
    wording = email_wording(decision)
    assert wording["heading"] == "Restated for this week: Strategies B and D are now ready"
    assert "showed Strategies B and D on HOLD because their required data" in wording["difference"]
    assert "Review the restated D lines" in wording["d_instruction"]


def test_the_banner_lists_prior_and_new_targets_and_what_stands(tmp_path, delivered):
    from component_factsheet_view import (render_html, render_pdf, render_text,
                                          restatement_banner, pct, position_name)
    prior, current = delivered
    settle(tmp_path, prior)
    decision = plan(tmp_path, prior)
    banner = restatement_banner(decision, current)
    for row in decision["restated_lines"]:
        assert (f"{position_name(row, current)} from {pct(row['prior_target'])} to "
                f"{pct(row['new_target'])} of NAV") in banner
    # The registry names the traded line, not the model's working label.
    assert "iShares Bitcoin Trust ETF (IBIT)" in banner and "Synthetic fund BTC-USD" not in banner
    assert "arrives after the factsheet already sent for this week" in banner
    assert "supersedes" in banner
    assert "Strategy C was on HOLD in the earlier factsheet and is now ready." in banner
    assert "Strategies A, B and D, and the portfolio overlays, stand as sent." in banner
    assert not CONTRACTION.search(banner)
    assert not any(w in banner.lower() for w in ERROR_WORDS)
    html = render_html(decision, current)
    assert "RESTATED INSTRUCTION ·" in html and escape(banner) in html
    assert "(IBIT)" in render_text(decision, current)
    assert render_pdf(decision, current).startswith(b"%PDF")


def test_restatement_workflow_is_manual_dry_by_default_and_reserves_before_send():
    workflow = (Path(__file__).resolve().parents[1]
                / ".github/workflows/factsheet_restatement.yml").read_text(encoding="utf-8")
    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in triggers
    assert "push:" not in triggers and "schedule:" not in triggers
    assert "group: weekly-factsheet" in workflow
    assert workflow.index("Persist restatement reservation") < workflow.index("Send the restated")
    assert workflow.index("Send the restated") < workflow.index("Record the confirmed restatement")
    dry = triggers.split("dry_run:", 1)[1]
    assert "default: true" in dry.split("\n\n", 1)[0]
    assert "restate-send" in workflow and "--authority" in workflow and "--restatement" in workflow
    assert "if: always()" in workflow


# --- first-issue sleeves (owner input 2026-09-27) ------------------------

def test_first_issue_is_normalised_and_invalid_values_refused(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    assert plan(tmp_path, prior, first_issue="d")["first_issue"] == ["D"]
    assert plan(tmp_path, prior, first_issue="")["first_issue"] == []
    assert plan(tmp_path, prior, first_issue="D, B")["first_issue"] == ["B", "D"]
    refused = plan(tmp_path, prior, first_issue="E")
    assert refused["action"] == "blocked" and "first-issue" in refused["reason"]


def test_first_issue_d_states_d_in_full_and_not_as_standing(tmp_path, delivered):
    from component_factsheet_view import restatement_banner
    prior, current = delivered
    settle(tmp_path, prior)
    decision = plan(tmp_path, prior, first_issue="D")
    wording = email_wording(decision)
    assert "Strategy D is stated in full here" in wording["difference"]
    assert wording["d_instruction"].startswith("Strategy D's proposed changes are stated in full")
    banner = restatement_banner(decision, current)
    assert "Strategy D is stated in full in this email; act on its proposed changes as shown below." in banner
    assert "Strategies A and B, and the portfolio overlays, stand as sent." in banner
    for text in list(wording.values()) + [banner]:
        assert not CONTRACTION.search(text), text
        assert not any(w in text.lower() for w in ERROR_WORDS), text


def test_first_issue_is_reserved_and_recorded(tmp_path, delivered):
    prior, _ = delivered
    settle(tmp_path, prior)
    sender.prepare_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY, reserve=True,
                               prior_release=prior, first_issue="D")
    with pytest.raises(ValueError, match="first-issue sleeves do not match"):
        sender.send_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY,
                                transport=lambda *_: pytest.fail("transport reached"),
                                env={}, prior_release=prior, first_issue="")
    sent = []
    sender.send_restatement(tmp_path, NOW, RESTATEMENT, AUTHORITY,
                            transport=lambda c, _: sent.append(c), env={},
                            prior_release=prior, first_issue="D")
    assert len(sent) == 1
    receipt = cr.read(tmp_path / sender.LEDGER)["anchors"][prior["anchor"]]["restatements"][RESTATEMENT]
    assert receipt["first_issue"] == ["D"]


def test_the_workflow_passes_first_issue_to_both_steps():
    workflow = (Path(__file__).resolve().parents[1]
                / ".github/workflows/factsheet_restatement.yml").read_text(encoding="utf-8")
    assert "first_issue:" in workflow
    assert workflow.count('--first-issue "$FIRST_ISSUE"') == 2


def test_a_panel_less_registry_key_prints_its_traded_name():
    from component_factsheet_view import position_name
    row = {"etf": "BTC-USD", "traded": "IBIT"}
    release = {"labels": {"BTC-USD": "Bitcoin (CoinDesk spot - deployed via IBIT, 25bps ER)"}}
    assert position_name(row, release) == "iShares Bitcoin Trust ETF (IBIT)"
    # A key with a constituent panel keeps its sealed label.
    assert position_name({"etf": "XBI", "traded": "XBI"},
                         {"labels": {"XBI": "SPDR S&P Biotech (eq-weight)"}}) == \
        "SPDR S&P Biotech (eq-weight) (XBI)"
