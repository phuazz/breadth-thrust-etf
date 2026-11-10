"""The pending selection and pending-fill line across the fill (2026-11-10).

WHY. Both lapsed once the fill date was behind the reader's clock, but the
held tables are dated by the engines' latest_rebalance, which moves only when
the post-fill refresh publishes it. On Tue 10 Nov 2026 the Strategy A tab read
"as of the 2026-11-02 rebalance" after the 9 Nov fill and nothing below it
named the book that had traded. They now render while fill_date > heldDate,
with a fill-day head and a post-fill head, and still withhold for HOLD, a
provisional ranking and a held table that has caught up.

These tests execute the shipped template functions under Node with a pinned
clock, so they test the code that renders, not a transcription of it.
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
  const hosts = {};
  const RealDate = Date;
  class FixedDate extends RealDate {
    constructor(...a) { if (a.length) super(...a); else super(c.now); }
  }
  const ctx = {
    Date: FixedDate, Math, JSON, Object, Array, Set, Number, String,
    window: { DATA: { live_targets: c.lt } },
    document: { getElementById: (id) => (hosts[id] = hosts[id] || { id, innerHTML: 'STALE' }) },
    fmtDateLongSafe: (d) => `<${d}>`,
    _displaySym: (e) => e,
    _etfSwatch: () => '',
    _sleeveExpandAttrs: () => ({ attrs: '', caret: '' }),
    _initHoldingsClickExpand: () => {},
    ETF_DESCRIPTIONS: {},
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  ctx._renderPendingSelection(c.sleeve, c.held);
  out[c.name] = {
    note: ctx._pendingFillNote(c.sleeve, c.held),
    table: (hosts[`${c.sleeve.toLowerCase()}-pending-selection`] || {}).innerHTML,
  };
}
process.stdout.write(JSON.stringify(out));
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the template functions")
    return node


def _functions() -> str:
    t = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\nfunction _pendingFillNote\(.*?(?=\nfunction _renderCHoldings\()", t, re.S)
    assert m, "pending-selection functions not found in template.html"
    return m.group(0)


def _lt(sleeve: str, *, status="READY", ranked="2026-11-06") -> dict:
    """A live_targets payload for one sleeve: fill Mon 9 Nov 2026, decided on
    the Fri 6 Nov close; XLK exits, XLU enters, XLF stays."""
    return {
        "computed_at_utc": "2026-11-08T04:48:32+00:00", "targets_final": True,
        "sleeves": [{
            "sleeve": sleeve, "status": status, "fill_date": "2026-11-09",
            "decision_session": ranked, "decision_session_for_fill": "2026-11-06",
            "top_k": 2, "weights": {"XLF": 0.5, "XLU": 0.5},
            "signals": {"XLF": 0.04, "XLU": 0.02, "XLK": -0.01},
            "signals_prev": {"XLF": 0.03, "XLU": -0.01, "XLK": 0.02},
        }],
        "lines": [
            {"sleeve": sleeve, "etf": "XLF", "held": 0.05, "target": 0.05},
            {"sleeve": sleeve, "etf": "XLU", "held": 0.0, "target": 0.05},
            {"sleeve": sleeve, "etf": "XLK", "held": 0.05, "target": 0.0},
        ],
    }


# Clock instants in UTC. Python datetime months are 1-indexed; the page reads
# the UTC date, so 23:59 UTC on Mon 9 Nov is still the fill date and 00:00
# UTC on Tue 10 Nov (08:00 SGT) is the first post-fill instant.
PRE, DUE, DUE_LATE, POST = ("2026-11-08T12:00:00Z", "2026-11-09T01:00:00Z",
                            "2026-11-09T23:59:00Z", "2026-11-10T00:00:00Z")


def _cases() -> list[dict]:
    cases = []
    for sl in ("A", "D"):
        for name, now in (("pre", PRE), ("due", DUE), ("due_late", DUE_LATE), ("post", POST)):
            cases.append({"name": f"{sl}_{name}", "sleeve": sl, "now": now,
                          "held": "2026-11-02", "lt": _lt(sl)})
        cases.append({"name": f"{sl}_caught_up", "sleeve": sl, "now": POST,
                      "held": "2026-11-09", "lt": _lt(sl)})
        cases.append({"name": f"{sl}_hold_post", "sleeve": sl, "now": POST,
                      "held": "2026-11-02", "lt": _lt(sl, status="HOLD")})
        cases.append({"name": f"{sl}_provisional", "sleeve": sl, "now": "2026-11-04T12:00:00Z",
                      "held": "2026-11-02", "lt": _lt(sl, ranked="2026-11-03")})
    for sl in ("B", "C"):
        for name, now in (("pre", PRE), ("post", POST)):
            cases.append({"name": f"{sl}_{name}", "sleeve": sl, "now": now,
                          "held": "2026-11-02", "lt": _lt(sl)})
    # Year boundary: a fill on Mon 4 Jan 2027 read on Tue 5 Jan, held table
    # still on the Mon 28 Dec 2026 rebalance.
    yb = _lt("A")
    yb["sleeves"][0].update(fill_date="2027-01-04", decision_session="2026-12-31",
                            decision_session_for_fill="2026-12-31")
    cases.append({"name": "A_year_boundary_post", "sleeve": "A",
                  "now": "2027-01-05T03:00:00Z", "held": "2026-12-28", "lt": yb})
    return cases


@pytest.fixture(scope="module")
def rendered() -> dict:
    r = subprocess.run([_node(), "-e", HARNESS, json.dumps(_cases())],
                       input=_functions(), capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("sl", ["A", "D"])
def test_pre_fill_wording_unchanged(rendered, sl):
    out = rendered[f"{sl}_pre"]
    assert "Pending selection — the <2026-11-09> rebalance, decided on the <2026-11-06> close and not yet filled." in out["table"]
    assert "Nothing here has been traded" in out["table"]
    assert "Leaves the book at the fill: XLK" in out["table"]
    assert out["note"].startswith('<div class="pending-fill-note"')
    assert "<strong>Next fill <2026-11-09></strong>" in out["note"]
    assert out["note"].endswith(". See the Next Fill card.</div>")


@pytest.mark.parametrize("sl", ["A", "D"])
@pytest.mark.parametrize("when", ["due", "due_late"])
def test_fill_day_claims_neither_filled_nor_untraded(rendered, sl, when):
    t = rendered[f"{sl}_{when}"]["table"]
    assert "for the fill due today." in t
    assert "Nothing here has been traded" not in t
    assert "filled on" not in t
    assert "XLU" in t and "XLF" in t


@pytest.mark.parametrize("sl", ["A", "D"])
def test_post_fill_keeps_table_and_relabels(rendered, sl):
    out = rendered[f"{sl}_post"]
    t = out["table"]
    assert ("Latest selection — the <2026-11-09> rebalance, filled on <2026-11-09> "
            "and not yet recorded by the post-fill refresh.") in t
    assert "The table above is the previous book until the refresh lands" in t
    assert "Nothing here has been traded" not in t
    assert "Left the book at the fill: XLK" in t
    assert 'id="' + sl.lower() + '-pending-table"' in t
    assert "<strong>Filled <2026-11-09></strong>" in out["note"]
    assert "See the Next Fill card" not in out["note"]
    assert out["note"].endswith("the table above is the previous book until it lands.</div>")


@pytest.mark.parametrize("sl", ["A", "D"])
def test_withholding_conditions_survive(rendered, sl):
    assert rendered[f"{sl}_caught_up"] == {"note": "", "table": ""}
    hold = rendered[f"{sl}_hold_post"]
    assert hold["table"] == ""
    assert "selection held pending complete data" in hold["note"]
    prov = rendered[f"{sl}_provisional"]
    assert prov["table"] == ""
    assert "provisional: ranked on the <2026-11-03> close" in prov["note"]


@pytest.mark.parametrize("sl", ["B", "C"])
def test_b_and_c_line_follows_the_same_rule(rendered, sl):
    assert "<strong>Next fill <2026-11-09></strong>" in rendered[f"{sl}_pre"]["note"]
    assert rendered[f"{sl}_pre"]["note"].endswith(". See the Next Fill card.</div>")
    assert "<strong>Filled <2026-11-09></strong>" in rendered[f"{sl}_post"]["note"]


def test_year_boundary(rendered):
    t = rendered["A_year_boundary_post"]["table"]
    assert "filled on <2027-01-04>" in t and "decided on the <2026-12-31> close" in t
