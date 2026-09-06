"""The email's next-fill section (2026-09-06): planned, labelled, derived.

The factsheet now goes out on the weekend before the Monday fill, so its
actionable payload is the planned fill. The section mirrors the dashboard
card's three states and its safety labels, prints the moves with the
derived "why" lines from the commentary, and never invents a reason when
the commentary is absent. The dashboard's own "why" block and the pipeline
loader are pinned alongside, since all three read the same file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_email_body import _next_fill_section  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template.html"


def _name(etf):
    return f"<strong>{etf}</strong>"


def _lt(statuses=("READY", "READY", "READY", "READY"), final=True,
        decision="2026-09-04", fill_decision="2026-09-04"):
    return {
        "as_of": "2026-09-04", "targets_final": final, "one_way_turnover": 0.0454,
        "next_fill": {"by_venue": {"NYSE": "2026-09-08", "XETR": "2026-09-07"}},
        "sleeves": [{"sleeve": s, "status": st, "decision_session": decision,
                     "decision_session_for_fill": fill_decision,
                     "reason": None if st == "READY" else "decision row carries 25 of 26 names"}
                    for s, st in zip("ABCD", statuses)],
        "lines": [
            {"sleeve": "A", "etf": "IUES", "held": 0.0977, "target": 0.1113, "delta": 0.0135, "status": "READY"},
            {"sleeve": "A", "etf": "IUMS", "held": 0.0100, "target": 0.0, "delta": -0.0100, "status": "READY"},
            {"sleeve": "B", "etf": "SHY", "held": 0.0, "target": 0.02, "delta": 0.02, "status": "READY"},
            {"sleeve": "A", "etf": "IUFS", "held": 0.1056, "target": 0.1057, "delta": 0.0001, "status": "READY"},
        ],
    }


NF = {"summary": "3 moves at the next fill.",
      "moves": [{"sleeve": "A", "etf": "IUES", "text": "IUES 9.8% → 11.1% of NAV: breadth 88.9% → 91.2%."},
                {"sleeve": "A", "etf": "IUMS", "text": "IUMS exits from 1.0% of NAV: out of the top 7."}]}


def test_planned_state_carries_the_labels_and_the_fill_dates():
    html = _next_fill_section(_lt(), NF, _name)
    assert "PLANNED" in html
    assert "Nothing here has been traded." in html
    assert "NYSE Tue 8 Sep 2026, XETR Mon 7 Sep 2026" in html
    assert "ranked on the Fri 4 Sep 2026 close" in html


def test_moves_are_listed_by_size_with_drift_omitted_and_why_lines_attached():
    html = _next_fill_section(_lt(), NF, _name)
    rows = re.findall(r"<tr><td[^>]*>([A-D])</td><td[^>]*>([A-Z ]+?)(?: <span|</td>)", html)
    assert [r[1] for r in rows] == ["BUY", "ADD", "SELL ALL"]
    assert "IUFS" not in html, "drift under 5bp of NAV is not an instruction"
    assert "9.8% &rarr; 11.1%" in html and "(+1.4pp)" in html
    assert "Why these moves" in html
    assert "breadth 88.9% → 91.2%" in html and "out of the top 7" in html
    assert "3 moves, one-way turnover 4.54% of NAV." in html


def test_partly_held_names_the_final_and_the_held_sleeves():
    html = _next_fill_section(_lt(statuses=("READY", "READY", "HOLD", "READY"), final=False), NF, _name)
    assert "PARTLY HELD" in html
    assert "Sleeves A, B and D are final" in html
    assert "Sleeve C is held and must be left as held" in html
    assert "Do not trade sleeve C." in html and "25 of 26 names" in html


def test_provisional_state_says_every_session_re_ranks_it():
    html = _next_fill_section(_lt(final=False, decision="2026-09-02"), NF, _name)
    assert "PROVISIONAL" in html
    assert "these are not final" in html
    assert "re-ranks it" in html


def test_without_commentary_no_reason_is_invented():
    html = _next_fill_section(_lt(), {}, _name)
    assert "Why these moves" not in html
    assert "breadth" not in html
    assert "SELL ALL" in html, "the table still shows the moves"


def test_no_material_moves_says_so():
    lt = _lt()
    lt["lines"] = [lt["lines"][-1]]
    html = _next_fill_section(lt, {}, _name)
    assert "No position changes above 0.05pp of NAV" in html


# ---------------------------------------------------------------------------
# The dashboard block and the pipeline loader read the same file
# ---------------------------------------------------------------------------
def test_dashboard_renders_why_only_for_the_same_decision_session():
    t = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="nf-why"' in t
    m = re.search(r"function renderNextFillWhy\(lt\)\s*\{(.*?)\n\}", t, re.S)
    assert m, "renderNextFillWhy() missing"
    body = m.group(1)
    assert "c.as_of !== lt.as_of" in body, "a stale commentary must not narrate this week"
    assert "_escapeHtml(m.text)" in body, "commentary is data, never markup"
    assert "renderNextFillWhy(lt);" in t
    assert ".prev-card.next-fill.nf-collapsed #nf-why" in t, "collapses with the card"


def test_pipeline_injects_the_commentary():
    src = (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
    assert "def load_commentary()" in src
    assert '"commentary": load_commentary(),' in src
    refresh = (ROOT / "scripts" / "refresh_all.py").read_text(encoding="utf-8")
    assert "scripts/build_commentary.py" in refresh
    # Ordered after the targets it explains and before the page that shows it.
    assert refresh.index("scripts/live_targets.py") < refresh.index("scripts/build_commentary.py") \
        < refresh.index("scripts/pipeline.py")
