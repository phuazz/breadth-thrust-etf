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

FOUR SELECTORS, NOT ONE, and the other three matter more than they first
looked. The Risk Overlay, EM Tilt and Multi-Strategy tabs each carry a
seven-button copy. Measured at 375px they needed 337px against 347px available
and squeaked through, which is why the bug was first found only on the
eight-button one. But that 10px is not headroom:

    viewport   7-button row needs   available   pre-fix result
    375px      337px                347px       fits, 10px spare
    360px      337px                332px       CLIPPED by 5px
    320px      337px                292px       CLIPPED by 45px

360px is an ordinary Android width and 320px is an iPhone SE, so MAX was
already being cut on both -- silently, in three more places. Applying the wrap
to `.period-selector` rather than to `#perf-period-selector` is therefore the
fix, not tidiness, and test_the_rules_are_not_scoped_to_one_selector pins it.

These assertions are on the stylesheet rather than on a rendered page, which is
the weaker half of the check. The rendered half was measured directly: all four
selectors at 320 / 375 / 390 CSS px give two clean rows, zero clipped buttons,
every button >= 62x40 and returning itself from elementFromPoint at its own
centre, with no horizontal page scroll. What is pinned here is the mechanism,
so that a future edit cannot quietly reintroduce a single unwrappable row.
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


def test_the_rules_are_not_scoped_to_one_selector(css):
    """The fix must reach all four copies of the control.

    Scoping it to #perf-period-selector would look correct -- that is the one
    where the clip was found -- and would leave the Risk Overlay, EM Tilt and
    Multi-Strategy selectors clipping MAX at 360px and below, which is where
    they actually break.
    """
    for selector in (".period-selector", ".period-selector button"):
        block_start = css.find(selector + " {")
        assert block_start != -1, f"{selector} block not found"
    assert "#perf-period-selector {" not in css, (
        "an id-scoped block would strand the other three selectors")
    # The phone block must key off the class too.
    mq = css.find("@media (max-width: 700px)")
    window = css[mq:mq + 1200]
    assert ".period-selector {" in window and ".period-selector button {" in window
    assert "#perf-period-selector" not in window


def test_all_four_selectors_exist_and_carry_the_shared_class(css):
    """If a selector is ever added without the class it inherits none of this."""
    ids = re.findall(r'id="([a-z0-9-]*period[a-z0-9-]*)"', css)
    assert sorted(set(ids)) == sorted(
        {"perf-period-selector", "overlay-dd-period", "phase22-period",
         "multi-contrib-period"}), ids
    for sid in set(ids):
        m = re.search(r'class="([^"]*)"[^>]*id="' + re.escape(sid) + r'"', css) \
            or re.search(r'id="' + re.escape(sid) + r'"[^>]*class="([^"]*)"', css) \
            or re.search(r'class="([^"]*)"\s*\n?\s*id="' + re.escape(sid) + r'"', css)
        assert m and "period-selector" in m.group(1), f"{sid} lacks .period-selector"
