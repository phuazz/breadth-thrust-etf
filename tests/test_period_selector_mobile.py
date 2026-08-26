"""The period selector must WRAP on a phone, never clip.

Found 2026-08-26 while measuring the next-fill card. The selector holds eight
buttons in an `inline-flex` row with `overflow: hidden`; at a 375px viewport
the row measured 389px inside a 375px container, so MAX -- the longest window
and the one a reader reaches for to see the whole history -- was silently cut
off. It read as a deliberate design rather than a bug precisely because the
clip is clean: no scrollbar, no ragged edge, just a missing control.

`overflow: hidden` is load-bearing (it clips the buttons' square corners to the
container's rounded ones), so the fix is to let the row wrap rather than to
remove the clip. Separators moved from a border-right per button to a 1px gap
over a border-coloured container, because a per-button border cannot draw the
line BETWEEN rows once the row wraps, and a gap needs no nth-child arithmetic
that would break the next time a period is added or removed.

These assertions are on the stylesheet rather than on a rendered page, which is
the weaker half of the check -- the rendered half was measured directly at 375
and 390 CSS px (2 rows of 4, zero clipped, every button 86x40 and hit-testable)
and is recorded in the commit. What is pinned here is the mechanism, so that a
future edit cannot quietly reintroduce a single unwrappable row.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path("template.html")


@pytest.fixture(scope="module")
def css() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _block(css: str, selector: str, media: str | None = None) -> str:
    """The declaration block for `selector`, optionally inside a media query."""
    hay = css
    if media:
        i = hay.find(media)
        assert i != -1, f"media query {media!r} not found"
        hay = hay[i:i + 1200]
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", hay)
    assert m, f"no block found for {selector!r}" + (f" inside {media!r}" if media else "")
    return m.group(1)


def test_the_row_is_allowed_to_wrap(css):
    """The whole fix. Without this the eighth button is clipped on a phone."""
    assert "flex-wrap: wrap" in _block(css, ".period-selector")


def test_separators_survive_wrapping(css):
    """A border-right per button cannot draw the line between ROWS. The 1px gap
    over a border-coloured container draws it in both axes."""
    container = _block(css, ".period-selector")
    assert "gap: 1px" in container
    assert "background: var(--border)" in container
    button = _block(css, ".period-selector button")
    assert "border-right" not in button, (
        "a per-button right border reintroduces the axis the gap replaced")


def test_the_clip_that_rounds_the_corners_is_kept(css):
    """overflow:hidden is what clips square button corners to the rounded
    container. Removing it would 'fix' the clipping by breaking the shape."""
    container = _block(css, ".period-selector")
    assert "overflow: hidden" in container
    assert "border-radius" in container


def test_phone_layout_is_four_per_row_with_a_real_touch_target(css):
    block = _block(css, ".period-selector button", media="@media (max-width: 700px)")
    assert "min-height: 40px" in block, "31px rows are fiddly on a phone"
    m = re.search(r"flex:\s*1\s+0\s+(\d+)%", block)
    assert m, "expected a flex-basis giving a fixed number per row"
    basis = int(m.group(1))
    assert 4 * basis <= 100, (
        f"flex-basis {basis}% cannot fit four per row once the 1px gaps are "
        f"counted; the eighth button would wrap alone")


def test_the_selector_takes_the_full_width_on_a_phone(css):
    """Without it the eight buttons wrap ragged (5+3) instead of 4+4."""
    assert "width: 100%" in _block(css, ".period-selector",
                                   media="@media (max-width: 700px)")


def test_every_period_is_still_offered(css):
    """The fix must not have quietly dropped a window to make the row fit."""
    sel = re.search(r'id="perf-period-selector".*?</div>', css, re.S)
    assert sel, "period selector markup not found"
    periods = re.findall(r'data-period="([^"]+)"', sel.group(0))
    assert periods == ["1d", "1w", "1m", "ytd", "1y", "3y", "5y", "max"], periods
