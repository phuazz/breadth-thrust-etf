"""Findings from the 320px narrow-width audit (2026-08-26).

320px is an iPhone SE. The audit swept all fourteen tabs at that width and
found thirteen clean; everything was on the Monitor tab, and it made the whole
PAGE scroll sideways -- 73 elements past the viewport edge.

Two independent causes, both of the same shape: something that could not shrink,
inside something that assumed it would.

1. `#positions-preview` used `grid-template-columns: 1fr`. A bare `1fr` is
   `minmax(auto, 1fr)`, and that `auto` floor is the item's MIN-CONTENT width.
   `#preview-activity-table` has a min-content of 322px (a table will not shrink
   past its cells), so the track was pinned at 352px inside a 292px container
   and the page scrolled 32px. Every other grid in the file already used the
   `minmax(0, ...)` form; this one was the exception.

   Fixing the track alone is not enough -- it lets the CARD shrink while the
   table still overflows it -- so the preview tables also get their own
   horizontal scroller below 720px, which is the remedy the wide tables
   elsewhere on the page already use.

2. `.fs-item` (the per-sleeve freshness pills) is `white-space: nowrap`. The
   strip wraps BETWEEN pills but a pill could not wrap WITHIN itself, so the
   longest one overflowed. The longest is always a `behind` pill, because it
   carries an extra "N sessions behind" clause -- which means the overflow
   appeared exactly when a sleeve was late, i.e. when the strip most needed
   reading. "D Europe sectors 2026-08-24 - 1 session behind" measured 293px
   inside a 231px strip.

Rendered verification is recorded in the commit: after the fix all fourteen
tabs report zero overflowing elements, zero sub-11px type and no page scroll at
320px, and 390 / 768 / 1280 are unchanged. What is pinned here is the CSS
mechanism, since a stylesheet edit is what would undo it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path("template.html")


@pytest.fixture(scope="module")
def css() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _media_blocks(css: str, query: str = "@media (max-width: 720px)") -> list[str]:
    """Every block for `query`, brace-matched.

    There is more than one 720px block in this stylesheet, so taking the first
    hit silently tests the wrong rules -- which is exactly what the first
    version of this file did.
    """
    out, start = [], 0
    while True:
        i = css.find(query, start)
        if i == -1:
            break
        j = css.find("{", i)
        depth, k = 0, j
        while k < len(css):
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(css[i:k + 1])
        start = k + 1
    assert out, f"no block found for {query!r}"
    return out


def _phone_css(css: str) -> str:
    """All 720px rules concatenated — the phone layer as the browser sees it."""
    return "\n".join(_media_blocks(css))


def _phone_block(css: str) -> str:
    return _phone_css(css)


def test_the_preview_grid_track_can_shrink(css):
    """A bare `1fr` cannot go below its content and is what scrolled the page."""
    m = re.search(r"#positions-preview\s*\{([^}]*)\}", css)
    assert m, "#positions-preview rule not found"
    cols = re.search(r"grid-template-columns:\s*([^;]+);", m.group(1))
    assert cols, "no grid-template-columns on #positions-preview"
    value = cols.group(1).strip()
    assert "minmax(0" in value, (
        f"grid-template-columns is {value!r}; a track without a minmax(0, ...) "
        f"floor is pinned by its widest child's min-content")
    assert not re.search(r"(^|\s)1fr(\s|$)", value), (
        f"bare 1fr found in {value!r} -- that is minmax(auto, 1fr)")


def test_the_phone_grid_track_can_shrink_too(css):
    block = _phone_block(css)
    m = re.search(r"#positions-preview\s*\{\s*grid-template-columns:\s*([^;}]+)", block)
    assert m, "the phone rule for #positions-preview is missing"
    assert "minmax(0" in m.group(1), (
        "the single-column phone track has the same auto-floor problem")


def test_preview_tables_get_a_scroller_on_a_phone(css):
    """The track shrinking only moves the overflow into the card unless the
    table itself has somewhere to go."""
    block = _phone_block(css)
    assert "table.prev-table" in block, "no phone rule for the preview tables"
    assert "overflow-x: auto" in block, "the table has no horizontal scroller"
    # Must be .prev-card's OWN min-width. A bare substring search passes on any
    # unrelated `min-width: 0` elsewhere in the phone layer -- it did, and the
    # mutation check caught the test rather than the code.
    m = re.search(r"\.prev-card\s*\{([^}]*)\}", block)
    assert m, ".prev-card has no phone rule"
    assert "min-width: 0" in m.group(1), (
        ".prev-card needs min-width:0 or the grid item keeps its min-content "
        "floor regardless of the track")


def test_a_freshness_pill_may_wrap_within_itself_on_a_phone(css):
    """nowrap on the pill is what pushed the page sideways when a sleeve was
    behind -- and a behind pill is the longest one by construction."""
    i = css.find(".fresh-strip .fs-item")
    assert i != -1
    assert "white-space: nowrap" in css[i:i + 400], (
        "base style changed; re-check whether the phone override is still needed")
    m = re.search(r"\.fresh-strip \.fs-item\s*\{([^}]*)\}", _phone_css(css))
    assert m, "no phone override for .fs-item"
    assert "white-space: normal" in m.group(1)
    assert "max-width: 100%" in m.group(1)


def test_the_fixes_are_confined_to_the_phone_breakpoint(css):
    """Desktop must keep a real table and a single-line pill. Measured at
    1280px: table display `table`, pill 27px on one line."""
    block = _phone_block(css)
    # The two overrides must live INSIDE the media block, not at top level.
    assert "display: block; overflow-x: auto" in block
    top_level = css[:css.find("@media (max-width: 720px)")]
    assert "display: block; overflow-x: auto" not in top_level, (
        "a table-to-block override outside the breakpoint would hit desktop")
    assert "white-space: normal" not in re.sub(
        r"@media[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "", top_level), (
        "the pill must stay nowrap outside the phone breakpoint")
