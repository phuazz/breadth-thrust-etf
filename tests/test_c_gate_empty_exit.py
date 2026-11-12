"""Sleeve C pending-fill line when the gate fires on a cash-only book (2026-11-11).

WHY. With the C sleeve-breadth gate already on at the previous fill, the held
book is SHY alone. The 9 Nov 2026 live_targets payload then rendered "All 0
holdings exit () and the sleeve moves to cash": an empty exit set listed, and
a move that does not happen. The gate-fired branch now says the sleeve stays
in cash and names the cash line held. Every case with a non-empty exit set
keeps its wording, pinned below.

The template function runs under Node with a pinned clock, so the test reads
the code that renders, not a transcription of it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template.html"

HARNESS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(0, 'utf8');
const cases = JSON.parse(process.argv[1]);
const out = {};
for (const c of cases) {
  const RealDate = Date;
  class FixedDate extends RealDate {
    constructor(...a) { if (a.length) super(...a); else super(c.now); }
  }
  const ctx = {
    Date: FixedDate, Math, JSON, Object, Array, Set, Number, String,
    window: { DATA: { live_targets: c.lt } },
    document: { getElementById: () => ({ innerHTML: '' }) },
    fmtDateLongSafe: (d) => `<${d}>`,
    _displaySym: (e) => e,
    _etfSwatch: () => '',
    _sleeveExpandAttrs: () => ({ attrs: '', caret: '' }),
    _initHoldingsClickExpand: () => {},
    ETF_DESCRIPTIONS: {},
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  out[c.name] = ctx._pendingFillNote('C', c.held);
}
process.stdout.write(JSON.stringify(out));
"""

GATE = {"fired": True, "n_above": 3, "n_universe": 25, "threshold": 0.32, "floor": 0.05}
HEAD = ("sleeve-breadth gate on: 3 of 25 themes above the +5% floor, 8 needed. ")


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the template functions")
    return node


def _functions() -> str:
    t = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\nfunction _pendingFillNote\(.*?(?=\nfunction _renderCHoldings\()", t, re.S)
    assert m, "pending-fill functions not found in template.html"
    return m.group(0)


def _lt(lines: list[dict]) -> dict:
    """Fill Mon 9 Nov 2026, decided on the Fri 6 Nov close, C gate on."""
    return {
        "computed_at_utc": "2026-11-08T04:48:32+00:00", "targets_final": True,
        "sleeves": [{
            "sleeve": "C", "status": "READY", "fill_date": "2026-11-09",
            "decision_session": "2026-11-06", "decision_session_for_fill": "2026-11-06",
            "top_k": 3, "weights": {"SHY": 1.0}, "gate": GATE,
            "signals": {"KWEB": 0.01, "URA": 0.02}, "signals_prev": {},
        }],
        "lines": [dict(sleeve="C", **l) for l in lines],
    }


CASES = {
    # The 9 Nov 2026 shape: SHY held, SHY kept, nothing else.
    "cash_only": [{"etf": "SHY", "held": 0.15, "target": 0.15}],
    # Cash-only book with the cash line resized.
    "cash_only_resized": [{"etf": "SHY", "held": 0.15, "target": 0.14}],
    # Empty book entering cash: no exit set either.
    "empty_book_enters_cash": [{"etf": "SHY", "held": 0.0, "target": 0.15}],
    # Unchanged wording: risk names exit, SHY enters.
    "exits_to_shy": [{"etf": "KWEB", "held": 0.05, "target": 0.0},
                     {"etf": "URA", "held": 0.10, "target": 0.0},
                     {"etf": "SHY", "held": 0.0, "target": 0.15}],
    # Unchanged wording: risk names exit, SHY already part-held.
    "exits_shy_part_held": [{"etf": "KWEB", "held": 0.05, "target": 0.0},
                            {"etf": "SHY", "held": 0.10, "target": 0.15}],
}


@pytest.fixture(scope="module")
def notes() -> dict:
    cases = [{"name": k, "now": "2026-11-08T12:00:00Z", "held": "2026-11-02", "lt": _lt(v)}
             for k, v in CASES.items()]
    r = subprocess.run([_node(), "-e", HARNESS, json.dumps(cases)],
                       input=_functions(), capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def _body(note: str) -> str:
    m = re.search(r"\(decided on the <2026-11-06> close\): (.*)\. See the Next Fill card\.</div>$", note)
    assert m, note
    return m.group(1)


@pytest.mark.parametrize("name", ["cash_only", "cash_only_resized", "empty_book_enters_cash"])
def test_no_empty_exit_set(notes, name):
    assert "All 0 holdings" not in notes[name]
    assert "exit ()" not in notes[name]


def test_cash_only_book_stays_in_cash(notes):
    assert _body(notes["cash_only"]) == (
        HEAD + "No risk holdings to exit; the sleeve stays in cash (SHY at 15.0% of NAV)")
    assert "moves to cash" not in notes["cash_only"]


def test_cash_only_resized_names_target(notes):
    assert _body(notes["cash_only_resized"]) == (
        HEAD + "No risk holdings to exit; the sleeve stays in cash (SHY at 14.0% of NAV)")


def test_empty_book_names_cash_entry(notes):
    assert _body(notes["empty_book_enters_cash"]) == (
        HEAD + "No holdings to exit; SHY takes the whole 15.0% of NAV")


def test_non_empty_exit_wording_unchanged(notes):
    assert _body(notes["exits_to_shy"]) == (
        HEAD + "All 2 holdings exit (KWEB, URA) and SHY takes the whole 15.0% of NAV")
    assert _body(notes["exits_shy_part_held"]) == (
        HEAD + "All 1 holdings exit (KWEB) and the sleeve moves to cash")
