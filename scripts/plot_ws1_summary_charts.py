"""Allocator-facing charts for the plain-language summary (reproducible).

Reads  data/ws1_ma_surface.json
Writes data/ws1_blend_surface_simple.png    (summary finding 1)
       data/ws1_ma_surface_summary.png       (appendix A2)
       data/ws1_sum_scope.png                 (appendix A1)

Design decision (2026-07-03): the shaded band represents the FLAT ZONE —
the range of trend lengths whose full-window results are statistically
indistinguishable (differences are far inside the Sharpe standard error of
about 0.4 on 7.6 years). It is a fixed 200-325 days, matching across every
panel, because the finding "flat beyond ~10 months" is uniform. The
deployed 200d sits at the FAST EDGE of that flat zone — the most responsive
setting that still captures the full benefit. The apparent single-best
point (~275d) is labelled a backtest mirage: the walk-forward test (finding
2) showed that chasing it loses out of sample. This replaces the earlier
"within 0.05 of the peak" band, which started at 250 and misleadingly left
the deployed setting outside the shaded region.

Run: python scripts/plot_ws1_summary_charts.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = DATA / "ws1_ma_surface.json"

FLAT_LO, FLAT_HI = 200, 325   # the statistically-flat zone (days)
DEPLOYED = 200
NAVY, RED, TEAL, GREEN_FILL = "#1e3a8a", "#dc2626", "#0891b2", "#dcfce7"

plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 11,
    "axes.grid": True, "grid.color": "#e5e7eb", "grid.linewidth": 0.7,
    "axes.edgecolor": "#9ca3af", "figure.facecolor": "white",
    "axes.facecolor": "white",
})


def _load():
    blob = json.loads(SRC.read_text(encoding="utf-8"))
    return blob, blob["grid"], blob["surface"]


# ---------------------------------------------------------------------------
# Chart 1 — single-panel blend chart (summary finding 1)
# ---------------------------------------------------------------------------
def blend_simple(grid, surf):
    b = surf["blend"]
    full = [b[str(w)]["full"]["sharpe"] for w in grid]
    y2022 = [b[str(w)]["sub_period_sharpe"]["2022_inflation_shock"] for w in grid]
    flat_vals = [b[str(w)]["full"]["sharpe"] for w in grid if FLAT_LO <= w <= FLAT_HI]

    fig, ax = plt.subplots(figsize=(9.2, 4.6), dpi=150)
    ax.axvspan(FLAT_LO, FLAT_HI, color=GREEN_FILL, zorder=0)
    # horizontal ribbon: the flat-zone results all sit in this narrow band
    ax.axhspan(min(flat_vals) - 0.02, max(flat_vals) + 0.02, xmin=0, xmax=1,
               color="#bbf7d0", alpha=0.35, zorder=0)
    ax.text((FLAT_LO + FLAT_HI) / 2, 1.63,
            "flat zone — every setting here performs\nthe same within the margin of error",
            ha="center", fontsize=9.8, color="#166534")
    ax.axhline(0, color="#9ca3af", lw=0.8)

    ax.plot(grid, full, color=NAVY, lw=2.6, marker="o", ms=4.5,
            label="Whole 7.6-year test")
    ax.plot(grid, y2022, color=RED, lw=1.6, ls="--", marker="o", ms=3.5,
            label="The choppy 2022 market only")

    ax.plot([DEPLOYED], [b[str(DEPLOYED)]["full"]["sharpe"]], marker="o", ms=11,
            markerfacecolor="none", markeredgecolor="#111111", markeredgewidth=2)
    ax.annotate("current setting (≈10 months)\nfast edge of the flat zone",
                (DEPLOYED, b[str(DEPLOYED)]["full"]["sharpe"]),
                textcoords="offset points", xytext=(-140, 6), fontsize=9.8,
                color="#111111")
    # peak-is-a-mirage callout
    peak_w = max((w for w in grid if FLAT_LO <= w <= FLAT_HI),
                 key=lambda w: b[str(w)]["full"]["sharpe"])
    ax.annotate("highest on the backtest —\nbut chasing it lost money\non unseen data (finding 2)",
                (peak_w, b[str(peak_w)]["full"]["sharpe"]),
                textcoords="offset points", xytext=(10, -58), fontsize=8.6,
                color="#6b7280",
                arrowprops=dict(arrowstyle="->", color="#9ca3af", lw=0.9))
    ax.annotate("fast settings were whipsawed in 2022",
                (85, -0.72), textcoords="offset points", xytext=(-8, -16),
                fontsize=10, color="#991b1b")

    ax.set_xlim(10, 340)
    ax.set_ylim(-1.1, 1.9)
    ax.set_xticks([25, 75, 125, 200, 275, 325],
                  ["25 days\n(≈1 month)", "75", "125", "200\n(≈10 months)",
                   "275", "325\n(≈15 months)"])
    ax.set_xlabel("Trend length the strategy measures", fontsize=11)
    ax.set_ylabel("Risk-adjusted return (Sharpe ratio)", fontsize=11)
    ax.set_title("Beyond ~10 months the setting barely matters — the strategy uses the "
                 "fast edge of that flat zone", fontsize=11.8, fontweight="bold", pad=12)
    ax.legend(fontsize=10, loc="lower right", framealpha=0.95)
    fig.tight_layout()
    out = DATA / "ws1_blend_surface_simple.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


# ---------------------------------------------------------------------------
# Chart 2 — six-panel "holds for every book" (appendix A2)
# ---------------------------------------------------------------------------
PANELS = [
    ("blend", "Whole portfolio"),
    ("A", "US sectors"),
    ("B", "Global asset classes"),
    ("C", "Thematics"),
    ("D", "European sectors"),
]


def six_panel(grid, surf):
    fig, axes = plt.subplots(2, 3, figsize=(12.5, 6.6), dpi=150)
    fig.suptitle("The same test on every book: risk-adjusted return vs trend length "
                 "(flat zone shaded, current setting dashed)",
                 fontsize=12.5, fontweight="bold")
    for ax, (key, title) in zip(axes.flat[:5], PANELS):
        s = surf[key]
        full = np.array([s[str(w)]["full"]["sharpe"] for w in grid], float)
        train = np.array([s[str(w)]["train"]["sharpe"] for w in grid], float)
        test = np.array([s[str(w)]["test"]["sharpe"] for w in grid], float)
        ax.axvspan(FLAT_LO, FLAT_HI, color=GREEN_FILL, zorder=0,
                   label="flat zone")
        ax.axvline(DEPLOYED, color="#374151", lw=0.9, ls=(0, (4, 2)),
                   label="current setting")
        ax.plot(grid, full, color=NAVY, lw=2.2, marker="o", ms=3,
                label="full 7.6-year test")
        ax.plot(grid, train, color=TEAL, lw=1.1, ls="--", marker="o", ms=2.2,
                label="first half")
        ax.plot(grid, test, color=RED, lw=1.1, ls=":", marker="o", ms=2.2,
                label="second half")
        ax.set_title(title, fontsize=10.5)
        ax.set_xlim(15, 335)
        ax.set_ylim(-0.45, 1.75)
        ax.set_xticks([50, 125, 200, 275])
        ax.set_xlabel("trend length (trading days)", fontsize=8.5)
        ax.set_ylabel("risk-adjusted return", fontsize=8.5)
    axes.flat[0].legend(fontsize=7.2, loc="lower right", framealpha=0.9)

    axh = axes.flat[5]
    mat = np.array([[surf[k][str(w)]["full"]["sharpe"] for w in grid]
                    for k, _ in PANELS], float)
    axh.imshow(mat, aspect="auto", cmap="RdYlGn",
               vmin=np.nanmin(mat), vmax=np.nanmax(mat))
    axh.set_xticks(range(len(grid)), [str(w) for w in grid], fontsize=7)
    axh.set_yticks(range(len(PANELS)), [p[0] for p in PANELS], fontsize=8)
    axh.grid(False)
    for i in range(len(PANELS)):
        for j in range(len(grid)):
            axh.text(j, i, f"{mat[i, j]:.2f}".lstrip("0"), ha="center",
                     va="center", fontsize=5.6, color="black")
    axh.axvline(grid.index(DEPLOYED), color="#111111", lw=1.4, ls=(0, (4, 2)))
    axh.set_title("Higher is greener — the whole flat zone is green",
                  fontsize=10.5)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = DATA / "ws1_ma_surface_summary.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


# ---------------------------------------------------------------------------
# Chart 3 — scope-of-testing tally (appendix A1)
# ---------------------------------------------------------------------------
def scope():
    cats = [
        ("Trend-length settings  (13 speeds × 4 books + whole portfolio)", 65),
        ("Safety-brake trigger settings", 25),
        ("Thematic-hurdle settings", 25),
        ("Alternative signal recipes", 17),
        ("Real-time yearly re-tuning simulations", 5),
        ("Head-to-head precision tests  (current vs best rival)", 2),
        ("Portfolio risk-sizing overlays", 2),
    ][::-1]
    labels = [c[0] for c in cats]
    vals = [c[1] for c in cats]
    fig, ax = plt.subplots(figsize=(9.6, 3.9), dpi=150)
    ax.barh(range(len(vals)), vals, height=0.62, color=NAVY)
    for i, v in enumerate(vals):
        ax.text(v + 1.1, i, str(v), va="center", fontsize=13,
                fontweight="bold", color="#222222")
        if v >= 25:
            ax.text(1.4, i, labels[i], va="center", ha="left", fontsize=11.3,
                    color="white", fontweight="bold")
        else:
            ax.text(v + 5.2, i, labels[i], va="center", ha="left",
                    fontsize=11.3, color="#333333")
    ax.set_xlim(0, 78)
    ax.set_ylim(-0.6, len(vals) - 0.4)
    ax.set_yticks([])
    ax.set_xlabel("Number of settings combinations tested", fontsize=11.5)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(left=False)
    ax.set_axisbelow(True)
    ax.set_title("The extent of the review: 141 settings combinations tested — "
                 "zero changes made", fontsize=13.5, fontweight="bold",
                 loc="left", pad=12)
    fig.text(0.5, -0.02, "141 tested   →   0 changed   →   "
             "2 items placed on watch for the final robustness review",
             ha="center", fontsize=12.5, color=NAVY, fontweight="bold")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    out = DATA / "ws1_sum_scope.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


def main():
    _, grid, surf = _load()
    blend_simple(grid, surf)
    six_panel(grid, surf)
    scope()


if __name__ == "__main__":
    main()
