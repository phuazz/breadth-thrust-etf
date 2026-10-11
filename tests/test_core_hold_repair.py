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
