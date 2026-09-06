"""The per-sleeve status table on the Monitor tab (2026-09-06).

WHY. On Sunday 2026-09-06 the answer to "which sleeve is updated to when,
and will it trade?" was: A, B and D final on the Friday close, C held for one
blank member. That answer was spread over three places — the freshness chips
(data reach), the next-fill card (fill dates by venue, HOLD note at the
bottom) and the holdings footnotes (last traded) — and the card's banner,
which has one flag for the weakest sleeve, printed the mid-week sentence
("every session between now and then re-ranks it") after the decision close
had passed. Nothing joined the facts per sleeve.

The table joins them: one row per sleeve, data reach against the venue's own
last close, last rebalance (decided → filled), next fill (ranked on → fills,
venue) and a verdict. The card gained a third state for "these are final,
that one is held". These tests pin the template mechanism, the data contract
the join relies on, and that the built page carries the table — the rendered
artefact, not the source.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template.html"
BUILT = ROOT / "docs" / "index.html"
DATA = ROOT / "data"


def _template_text() -> str:
    if not TEMPLATE.exists():
        pytest.skip("template.html not present")
    return TEMPLATE.read_text(encoding="utf-8")


def _renderer(t: str) -> str:
    m = re.search(r"function renderStrategyFreshness\(\)\s*\{(.*?)\nfunction renderMonitor",
                  t, re.S)
    assert m, "renderStrategyFreshness() not found ahead of renderMonitor()"
    return m.group(1)


def _next_fill_renderer(t: str) -> str:
    m = re.search(r"function renderNextFill\(labelMap\)\s*\{(.*?)\n// Long date without",
                  t, re.S)
    assert m, "renderNextFill() not found"
    return m.group(1)


# ---------------------------------------------------------------------------
# The table: one row per sleeve, five answers, all from data
# ---------------------------------------------------------------------------
def test_the_table_replaces_the_chips_in_the_same_slot():
    t = _template_text()
    assert 'id="live-freshness"' in t
    r = _renderer(t)
    assert "sleeve-status-table" in r
    assert "fs-item" not in r, "the chip strip must not be rendered beside the table"


def test_the_table_carries_the_five_columns_verdict_second():
    """Status sits beside the sleeve: on a phone the table scrolls and the
    first two columns are what is on screen, so the verdict must not be the
    column that scrolled away (measured at 375px: a fifth column sat 151px
    past the edge)."""
    r = _renderer(_template_text())
    cols = re.findall(r"<th>([^<]+)</th>", r)
    assert cols == ["Sleeve", "Status", "Signal data through", "Last rebalance", "Next fill"], cols


def test_every_cell_is_rendered_from_data_not_typed():
    """Reach from strategy_freshness, fill and verdict from live_targets, the
    last fill from each engine's latest_rebalance, the held share from the
    card's own lines. A typed date anywhere here would go stale silently."""
    r = _renderer(_template_text())
    assert "strategy_freshness" in r
    assert "live_targets" in r
    assert "_momLatestRebal(" in r
    assert "lt.lines" in r
    assert not re.search(r"20\d\d-\d\d-\d\d", r), "a literal date in the renderer"


def test_reach_is_judged_against_the_venue_not_the_calendar():
    r = _renderer(_template_text())
    assert "venue_last_session" in r
    assert "sessions_behind" in r
    assert "session behind" in r, "a lag is stated in sessions, never days"


def test_the_verdict_has_all_four_states():
    r = _renderer(_template_text())
    for cls in ("ss-final", "ss-hold", "ss-prov", "ss-stale"):
        # Pills are built as pill('<state>', ...) from the ss- prefix.
        assert f"pill('{cls[3:]}'" in r, cls
    assert "decision_session_for_fill" in r, "FINAL means ranked on the fill's own close"
    assert "t.fill_date < today" in r, "the lapse rule of the next-fill card applies here too"


def test_the_table_renders_nothing_without_the_freshness_block():
    r = _renderer(_template_text())
    assert "el.innerHTML = ''; return;" in r


def test_the_wrapper_scrolls_and_type_stays_readable():
    t = _template_text()
    m = re.search(r"\.sleeve-status\s*\{([^}]*)\}", t)
    assert m and "overflow-x: auto" in m.group(1)
    for size in re.findall(r"\.ss-(?:sub|pill)[^{]*\{[^}]*font-size:\s*([\d.]+)px", t):
        assert float(size) >= 11.0, f"{size}px is under the 11px floor"


# ---------------------------------------------------------------------------
# The card's third state: these are final, that one is held
# ---------------------------------------------------------------------------
def test_the_card_knows_partly_held():
    nf = _next_fill_renderer(_template_text())
    assert "partlyHeld" in nf
    assert "'partly held'" in nf, "the pill must say so"
    assert "setOpen(isFinal || partlyHeld)" in nf, "final sleeves' moves are actionable"


def test_partly_held_requires_every_sleeve_ranked_on_its_fill_close():
    """The state is not 'some sleeve is HOLD'. Mid-week, with sleeves still
    to re-rank, the provisional sentence remains the truthful one."""
    nf = _next_fill_renderer(_template_text())
    assert "s.decision_session === s.decision_session_for_fill" in nf
    assert "_rankedOnFillClose" in nf


def test_partly_held_keeps_the_not_traded_sentence_and_names_the_reason():
    nf = _next_fill_renderer(_template_text())
    i = nf.find("partlyHeld\n    ?")
    assert i != -1
    branch = nf[i:i + 900]
    assert "Nothing here has been traded" in branch
    assert "h.reason" in branch
    assert "must be left as held" in branch


# ---------------------------------------------------------------------------
# The data contract the join relies on
# ---------------------------------------------------------------------------
def test_freshness_and_targets_describe_the_same_sleeves():
    fp = DATA / "strategy_freshness.json"
    tp = DATA / "live_targets.json"
    if not (fp.exists() and tp.exists()):
        pytest.skip("live data files not present")
    f = json.loads(fp.read_text(encoding="utf-8"))
    t = json.loads(tp.read_text(encoding="utf-8"))
    fs = {s["sleeve"] for s in f["strategies"]}
    ts = {s["sleeve"] for s in t["sleeves"]}
    assert fs == ts == {"A", "B", "C", "D"}, (fs, ts)
    for s in t["sleeves"]:
        assert s["status"] in ("READY", "HOLD"), s
        assert "fill_date" in s and "decision_session_for_fill" in s, s


# ---------------------------------------------------------------------------
# The built page actually carries it
# ---------------------------------------------------------------------------
def test_the_built_page_carries_the_table_and_its_inputs():
    if not BUILT.exists():
        pytest.skip("docs/index.html not built")
    found = {"table": False, "freshness": False, "targets": False}
    with BUILT.open(encoding="utf-8", errors="replace") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), ""):
            if "sleeve-status-table" in chunk:
                found["table"] = True
            if '"strategy_freshness":' in chunk:
                found["freshness"] = True
            if '"live_targets":' in chunk:
                found["targets"] = True
            if all(found.values()):
                break
    assert all(found.values()), found
