"""Bar annotations must fit the axis, and ticks must state their own position.

BOTH DEFECTS SHIPPED IN THE 2026-08-21 FACTSHEET.

The visible one: the SOXX annotation ran 0.095 units past the left edge and
collided with its y tick label, printing "SOXX (A3pp (-5.5%)". The offset was
a fixed 0.5 in DATA units while a label's width is fixed in POINTS, so the
padding heuristic could not know how much room a label needed.

The worse one, which nobody reported: the x axis formats to one decimal while
matplotlib was free to choose 0.25 steps, so ticks sat at -0.75 and -0.25 and
printed "-0.8pp" and "-0.2pp". FOUR OF NINE labels misstated their own
position on a chart whose whole purpose is measuring bars against gridlines,
and 0.25 shown as 0.2 is a 20% error.

The same heuristic also over-reserved: the bars occupied 28% of the plot
width, which is why they looked small.
"""

from __future__ import annotations

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

from scripts.build_factsheet import (  # noqa: E402
    _fit_bar_labels,
    _honest_tick_locator,
)

# The exact bars from the factsheet that shipped the defect.
SHIPPED = [0.29, 0.28, 0.19, 0.17, 0.11, 0.09,
           -0.10, -0.10, -0.12, -0.12, -0.15, -0.23]


def _render(values, decimals=1, fontsize=8, figw=8.0):
    fig, ax = plt.subplots(figsize=(figw, 4.0), facecolor="white")
    fig.subplots_adjust(left=0.16, right=0.96, top=0.97, bottom=0.18)
    y = np.arange(len(values))
    ax.barh(y, values, height=0.62)
    texts = [
        ax.text(v, i, f"{v:+.2f}pp  ({v * 20:+.1f}%)", fontsize=fontsize,
                va="center", ha=("left" if v >= 0 else "right"))
        for i, v in enumerate(values)
    ]
    ax.set_yticks(y)
    ax.set_yticklabels([f"T{i} (A)" for i in range(len(values))], fontsize=9)
    ax.invert_yaxis()
    fmt = f"{{v:+.{decimals}f}}pp"
    ax.xaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: fmt.format(v=v)))
    _honest_tick_locator(ax, decimals=decimals)
    _fit_bar_labels(fig, ax, texts, values)
    fig.canvas.draw()
    return fig, ax, texts


def _clipping(fig, ax, texts):
    r = fig.canvas.get_renderer()
    inv = ax.transData.inverted()
    lo, hi = ax.get_xlim()
    out = []
    for t in texts:
        bb = t.get_window_extent(renderer=r)
        x0, _ = inv.transform((bb.x0, bb.y0))
        x1, _ = inv.transform((bb.x1, bb.y1))
        if x0 < lo - 1e-9 or x1 > hi + 1e-9:
            out.append((t.get_text(), x0, x1, lo, hi))
    return out


# ---------------------------------------------------------------------------
# No annotation may clip
# ---------------------------------------------------------------------------
def test_the_shipped_chart_no_longer_clips():
    """The exact case the owner reported."""
    fig, ax, texts = _render(SHIPPED)
    assert _clipping(fig, ax, texts) == []
    plt.close(fig)


@pytest.mark.parametrize("values", [
    SHIPPED,
    [0.5, 0.3, 0.1],                         # all positive
    [-0.5, -0.3, -0.1],                      # all negative
    [-2.0, 0.01],                            # one dominant negative
    [2.0, -0.01],                            # one dominant positive
    [0.0, 0.0, 0.0],                         # degenerate: nothing moved
    [1e-4, -1e-4],                           # near-zero span
    [40.0, -35.0, 12.0],                     # sleeve-chart magnitudes
])
def test_no_annotation_clips_on_any_shape(values):
    fig, ax, texts = _render(values)
    assert _clipping(fig, ax, texts) == [], values
    plt.close(fig)


def test_a_narrow_figure_still_fits_its_labels():
    """The failure was width-dependent, so the guard must be too."""
    fig, ax, texts = _render(SHIPPED, figw=5.0)
    assert _clipping(fig, ax, texts) == []
    plt.close(fig)


def test_a_larger_font_still_fits():
    fig, ax, texts = _render(SHIPPED, fontsize=11)
    assert _clipping(fig, ax, texts) == []
    plt.close(fig)


# ---------------------------------------------------------------------------
# Every tick must state its own position
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("values,decimals", [
    (SHIPPED, 1),
    ([0.5, 0.3, 0.1], 1),
    ([-2.0, 0.01], 1),
    ([40.0, -35.0, 12.0], 0),
    ([3.0, -2.0], 0),
])
def test_no_tick_label_misstates_its_position(values, decimals):
    fig, ax, _ = _render(values, decimals=decimals)
    lo, hi = ax.get_xlim()
    visible = [t for t in ax.get_xticks() if lo <= t <= hi]
    assert visible, "no ticks drawn"
    bad = [t for t in visible if abs(t - round(t, decimals)) > 1e-9]
    assert not bad, (
        f"ticks at {bad} print to {decimals}dp as something they are not")
    plt.close(fig)


def test_the_shipped_tick_positions_would_have_failed_this():
    """A guard that cannot fail on the original defect is not a guard.

    0.25 steps formatted to one decimal is exactly what shipped.
    """
    bad = [t for t in (-0.75, -0.25, 0.25, 0.75)
           if abs(t - round(t, 1)) > 1e-9]
    assert len(bad) == 4


# ---------------------------------------------------------------------------
# And the fit must not over-reserve
# ---------------------------------------------------------------------------
def test_the_bars_are_not_squeezed_into_a_narrow_band():
    """The old heuristic left the bars at 28% of the plot width. The point of
    measuring rather than guessing is that the padding is what the labels
    need and no more."""
    fig, ax, _ = _render(SHIPPED)
    lo, hi = ax.get_xlim()
    share = (max(SHIPPED) - min(SHIPPED)) / (hi - lo)
    assert share > 0.45, f"bars occupy only {share:.0%} of the width"
    plt.close(fig)


def test_zero_is_always_in_view():
    """A contribution chart with the zero line off-screen would be unreadable
    regardless of how well the labels fit."""
    for values in ([0.5, 0.3, 0.1], [-0.5, -0.3, -0.1], SHIPPED):
        fig, ax, _ = _render(values)
        lo, hi = ax.get_xlim()
        assert lo <= 0.0 <= hi, values
        plt.close(fig)


def test_every_bar_end_is_inside_the_axis():
    for values in (SHIPPED, [-2.0, 0.01], [40.0, -35.0, 12.0]):
        fig, ax, _ = _render(values)
        lo, hi = ax.get_xlim()
        assert lo <= min(values) and max(values) <= hi, values
        plt.close(fig)
