"""The same-anchor core repair route (2026-10-11, adversarial review P1).

The verified-release skip in scheduled_refresh.main returns success before
any recomputation whenever the committed release matches the current weekly
anchor — correct for every ordinary firing, and exactly why the originally
proposed recovery command could not repair a held core: it exited at that
skip. The explicit --repair-core-hold route must proceed past the skip ONLY
for an eligible data-hold on the same anchor, refuse a wrong anchor or an
unverifiable release, and change nothing else: downstream guards, seals,
markers and ordinary deduplication stay as they are.

These tests pin the eligibility rule as a pure function, the argparse
coupling (side-effect-free: parser.error exits before any logging or git
work), and the wiring order inside main() — the same source-inspection
style test_scheduled_refresh.py already uses for the gate preview, because
main() itself opens logs, pulls origin and probes credentials and is not a
unit. Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scheduled_refresh as sr  # noqa: E402


def _release(sleeves):
    return {"anchor": "2026-10-09", "book": {"sleeves": sleeves}}


# ---------------------------------------------------------------------------
# Eligibility: ONLY a core sleeve held for missing signal inputs
# ---------------------------------------------------------------------------

def test_data_hold_on_a_core_sleeve_is_eligible():
    rel = _release([
        {"sleeve": "A", "status": "READY"},
        {"sleeve": "B", "status": "READY"},
        {"sleeve": "C", "status": "HOLD", "missing_signal_inputs": ["BTC-USD"]},
        {"sleeve": "D", "status": "READY"},
    ])
    assert sr.repairable_held_core(rel) == ["C"]


def test_risk_or_discretionary_hold_is_not_eligible():
    """A HOLD without missing_signal_inputs is a decision, not a data gap.
    Repairing it would overrule the decision."""
    rel = _release([{"sleeve": "C", "status": "HOLD"},
                    {"sleeve": "B", "status": "HOLD",
                     "missing_signal_inputs": []}])
    assert sr.repairable_held_core(rel) == []


def test_a_held_europe_sleeve_never_qualifies_for_a_core_repair():
    rel = _release([{"sleeve": "D", "status": "HOLD",
                     "missing_signal_inputs": ["EXH1.DE"]}])
    assert sr.repairable_held_core(rel) == []


def test_ready_sleeves_and_empty_books_are_quiet():
    assert sr.repairable_held_core(_release([{"sleeve": "C", "status": "READY"}])) == []
    assert sr.repairable_held_core({"anchor": "2026-10-09"}) == []
    assert sr.repairable_held_core({}) == []


def test_multiple_data_holds_are_all_named_sorted():
    rel = _release([
        {"sleeve": "C", "status": "HOLD", "missing_signal_inputs": ["BTC-USD"]},
        {"sleeve": "A", "status": "HOLD", "missing_signal_inputs": ["IUMS", "IUCM"]},
    ])
    assert sr.repairable_held_core(rel) == ["A", "C"]


# ---------------------------------------------------------------------------
# Argparse coupling — side-effect-free refusals
# ---------------------------------------------------------------------------

def test_repair_flag_requires_an_explicit_core_component():
    with pytest.raises(SystemExit):
        sr.main(["--repair-core-hold"])  # component defaults to auto
    with pytest.raises(SystemExit):
        sr.main(["--repair-core-hold", "--component", "europe"])
    with pytest.raises(SystemExit):
        sr.main(["--repair-core-hold", "--component", "all"])


def test_repair_flag_refuses_preflight_and_capture_modes():
    with pytest.raises(SystemExit):
        sr.main(["--repair-core-hold", "--component", "core", "--preflight-only"])
    with pytest.raises(SystemExit):
        sr.main(["--repair-core-hold", "--component", "core", "--capture-only"])


# ---------------------------------------------------------------------------
# Wiring order inside main() — the skip must not fire first
# ---------------------------------------------------------------------------

def _main_src() -> str:
    return inspect.getsource(sr.main)


def test_repair_branch_precedes_the_verified_release_skip():
    src = _main_src()
    repair = src.index("if args.repair_core_hold:")
    skip = src.index("CORE ALREADY VERIFIED")
    assert repair < skip, (
        "the repair branch must be consulted before the verified-release "
        "skip, or the originally proposed command's failure mode returns"
    )


def test_repair_refusals_fail_rather_than_fall_through():
    src = _main_src()
    assert "repair refused - wrong anchor" in src
    assert "repair refused - committed release unverifiable" in src
    # The ineligible case exits cleanly without a refresh.
    assert "nothing to repair" in src


def test_repair_eligibility_reads_the_shared_helper():
    assert "repairable_held_core(released)" in _main_src()


def test_scheduled_children_never_carry_the_repair_flag():
    """The auto-sequence children are what the Task Scheduler firings run;
    the repair route must remain exclusively operator-invoked."""
    src = _main_src()
    child_block = src[src.index("def run_child"):src.index("return component_sequence")]
    assert "--repair-core-hold" not in child_block


def test_repair_announcement_does_not_promise_recovery():
    """The console line must not guarantee the re-run clears the hold
    (review P1 follow-on): the re-run re-verifies and may hold again."""
    src = _main_src()
    # Matched on a single source fragment: the sentence spans f-string
    # pieces, and getsource sees source text, not the joined literal.
    assert "legitimately hold again" in src
    assert "would clear the hold" not in src


# ---------------------------------------------------------------------------
# Behavioural gate tests (2026-10-11, second verification). Source-string
# assertions prove wiring exists; these execute main() to its decision with
# every boundary mocked — the pattern is the reviewer's own harness
# (logs/btc-verification/adversarial_checks.py). subprocess.run is replaced
# by a sentinel exception, so reaching the refresh child IS the assertion
# and no code beyond that boundary ever runs.
# ---------------------------------------------------------------------------

import json as _json
from datetime import datetime as _dt, timezone as _tz
from types import SimpleNamespace
from unittest.mock import patch

import component_release as cr
import publication_debt as pdm
import run_status


class _ReachedRefresh(BaseException):
    """Raised by the subprocess sentinel: the gate let the refresh start."""


_NOW = _dt(2026, 10, 11, 10, 26, 29, tzinfo=_tz.utc)


class _FixedTime(_dt):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is not None else _NOW.replace(tzinfo=None)


def _gate_release(c_status="HOLD", missing=("BTC-USD",), anchor="2026-10-09"):
    sleeves = [
        {"sleeve": "A", "status": "READY"},
        {"sleeve": "B", "status": "READY"},
        {"sleeve": "C", "status": c_status},
        {"sleeve": "D", "status": "READY"},
    ]
    if missing is not None:
        sleeves[2]["missing_signal_inputs"] = list(missing)
    return {"anchor": anchor, "book": {"sleeves": sleeves}}


def _drive(tmp_path, monkeypatch, release, *, repair, ran_today,
           verify_raises=False, capsys=None):
    """Run sr.main() to its gate decision with every boundary mocked.
    Returns (outcome, emails): outcome is the exit code, or the string
    'REACHED_REFRESH' when the sentinel fired."""
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "component_release.json").write_text(
        _json.dumps(release), encoding="utf-8")
    emails = []
    debt = SimpleNamespace(should_run=False, escalate=False, notices=[],
                           reason="no debt", as_dict=lambda: {})
    args = ["--component", "core", "--cadence", "weekend"]
    if repair:
        args.append("--repair-core-hold")
    replacements = {
        "LOG_DIR": tmp_path,
        "REPO_ROOT": tmp_path,
        "datetime": _FixedTime,
        "log_path_for": lambda t: tmp_path / "gate-test.log",
        "price_source_preflight": lambda *a: (True, "mocked feed"),
        "abort_stuck_operation": lambda *a: [],
        "_git": lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        "reconcile_unpushed_commits": lambda *a, **k: (True, "mocked clean", 0),
        "_durable_state_enabled": lambda: False,
        "refusal_report": lambda: "",
        "_email": lambda *a, **k: emails.append(a[0] if a else k),
        "already_ran_today": lambda *a, **k: ran_today,
    }
    with patch.object(run_status, "credential_state",
                      return_value={"configured": True, "missing": []}), \
         patch.object(pdm, "current_debt", return_value=debt), \
         patch.object(cr, "verify",
                      side_effect=ValueError("injected invalid seal") if verify_raises else None,
                      return_value=release), \
         patch.object(sr.subprocess, "run", side_effect=_ReachedRefresh):
        stack_targets = [patch.object(sr, n, v) for n, v in replacements.items()]
        for p in stack_targets:
            p.start()
        try:
            try:
                outcome = sr.main(args)
            except _ReachedRefresh:
                outcome = "REACHED_REFRESH"
        finally:
            for p in stack_targets:
                p.stop()
    return outcome, emails


def test_behaviour_ordinary_repeat_skips_at_verified_release(tmp_path, monkeypatch, capsys):
    out, _ = _drive(tmp_path, monkeypatch, _gate_release(), repair=False, ran_today=False)
    assert out == 0
    assert "CORE ALREADY VERIFIED" in capsys.readouterr().out


def test_behaviour_eligible_repair_runs_with_stale_marker(tmp_path, monkeypatch, capsys):
    out, _ = _drive(tmp_path, monkeypatch, _gate_release(), repair=True, ran_today=False)
    assert out == "REACHED_REFRESH"
    assert "SAME-ANCHOR REPAIR" in capsys.readouterr().out


def test_behaviour_eligible_repair_runs_past_a_same_day_green_marker(tmp_path, monkeypatch, capsys):
    """The second verification's finding 1: Saturday publishes a legitimate
    data HOLD (green marker written), the vendor backfills that afternoon,
    and the explicit repair must RUN, not exit ALREADY RAN TODAY."""
    out, _ = _drive(tmp_path, monkeypatch, _gate_release(), repair=True, ran_today=True)
    assert out == "REACHED_REFRESH"
    text = capsys.readouterr().out
    assert "same-day green marker overridden" in text
    assert "ALREADY RAN TODAY" not in text


def test_behaviour_ordinary_same_day_retry_still_deduplicates(tmp_path, monkeypatch, capsys):
    """The marker's ordinary job is untouched: a flag-less repeat on a day
    with a green run still exits without refreshing. The release here is
    NOT verified-current (wrong anchor) so the run reaches the dedupe."""
    out, _ = _drive(tmp_path, monkeypatch, _gate_release(anchor="2026-10-02"),
                    repair=False, ran_today=True)
    assert out == 0
    assert "ALREADY RAN TODAY" in capsys.readouterr().out


def test_behaviour_repaired_release_makes_repair_a_noop(tmp_path, monkeypatch, capsys):
    """After a successful repair the release holds nothing: the next
    invocation exits at 'nothing to repair' before any marker question."""
    ready = _gate_release(c_status="READY", missing=None)
    out, _ = _drive(tmp_path, monkeypatch, ready, repair=True, ran_today=True)
    assert out == 0
    assert "nothing to repair" in capsys.readouterr().out


def test_behaviour_vendor_still_missing_allows_a_deliberate_repeat(tmp_path, monkeypatch, capsys):
    """If the recompute legitimately held again (vendor withdrew the bar),
    the release still carries the data-hold and a second explicit repair
    the same day proceeds — each invocation is a deliberate operator
    action, never a scheduled firing."""
    for _ in range(2):
        out, _ = _drive(tmp_path, monkeypatch, _gate_release(), repair=True, ran_today=True)
        assert out == "REACHED_REFRESH"


def test_behaviour_wrong_anchor_refuses_loudly(tmp_path, monkeypatch):
    out, emails = _drive(tmp_path, monkeypatch, _gate_release(anchor="2026-10-02"),
                         repair=True, ran_today=False)
    assert out == 2
    assert any("wrong anchor" in str(e) for e in emails)


def test_behaviour_unverifiable_release_refuses(tmp_path, monkeypatch):
    out, emails = _drive(tmp_path, monkeypatch, _gate_release(), repair=True,
                         ran_today=False, verify_raises=True)
    assert out == 2
    assert any("unverifiable" in str(e) for e in emails)
