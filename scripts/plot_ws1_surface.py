"""Render the WS1 MA-lookback surfaces as charts (research artefacts).

Reads  data/ws1_ma_surface.json
Writes data/ws1_ma_surface.png      (Sharpe surfaces: small multiples + heatmap)
       data/ws1_ma_dd_surface.png   (blend drawdown surface)

Chart-form rationale: the surface is one-dimensional (lookback W), so small-
multiples LINE charts per sleeve with full/train/test overlaid are the
readable form; a sleeve x W heatmap gives the compact plateau overview. The
light-green band in each panel marks the plateau (within 0.05 Sharpe of that
panel's maximum, contiguous around the peak); the vertical line marks the
deployed 200d. White theme per house style.

Run: python scripts/plot_ws1_surface.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = DATA / "ws1_ma_surface.json"
OUT_SHARPE = DATA / "ws1_ma_surface.png"
OUT_DD = DATA / "ws1_ma_dd_surface.png"

SLEEVES = ["blend", "A", "B", "C", "D"]
TITLES = {
    "blend": "Blend 35/35/10/20 (ungated)",
    "A": "A — US sectors (rel. breadth, K=7)",
    "B": "B — asset class (momentum, K=7)",
    "C": "C — thematic (momentum, K=5)",
    "D": "D — Europe sectors (abs. breadth, K=3)",
}
C_FULL, C_TRAIN, C_TEST, C_2X = "#1e3a8a", "#0891b2", "#dc2626", "#9ca3af"
PLATEAU_TOL = 0.05

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 9,
    "axes.grid": True,
    "grid.color": "#e5e7eb",
    "grid.linewidth": 0.6,
    "axes.edgecolor": "#9ca3af",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
})


def plateau_span(ws: list[int], sharpes: np.ndarray, tol: float) -> tuple[int, int]:
    """Contiguous W-range around the peak within `tol` of the maximum."""
    i_pk = int(np.nanargmax(sharpes))
    lo = hi = i_pk
    while lo > 0 and sharpes[lo - 1] >= sharpes[i_pk] - tol:
        lo -= 1
    while hi < len(ws) - 1 and sharpes[hi + 1] >= sharpes[i_pk] - tol:
        hi += 1
    return ws[lo], ws[hi]


def main() -> int:
    blob = json.loads(SRC.read_text(encoding="utf-8"))
    grid = blob["grid"]
    surf = blob["surface"]
    deployed = blob.get("deployed_w", 200)
    window = f"{blob['common_start']} to {blob['common_end']}"

    # ------------------------- Sharpe figure -------------------------
    fig, axes = plt.subplots(2, 3, figsize=(12.5, 7.0), dpi=150)
    fig.suptitle(
        f"WS1 — Sharpe vs MA lookback per sleeve and blend  ({window}, "
        f"split {blob['split_date']}, deployed costs)",
        fontsize=11, fontweight="bold")

    for ax, sleeve in zip(axes.flat[:5], SLEEVES):
        s = surf[sleeve]
        full = np.array([s[str(w)]["full"]["sharpe"] for w in grid], float)
        train = np.array([s[str(w)]["train"]["sharpe"] for w in grid], float)
        test = np.array([s[str(w)]["test"]["sharpe"] for w in grid], float)
        lo, hi = plateau_span(grid, full, PLATEAU_TOL)
        ax.axvspan(lo, hi, color="#dcfce7", zorder=0,
                   label=f"plateau (peak −{PLATEAU_TOL})")
        ax.axvline(deployed, color="#374151", lw=0.9, ls=(0, (4, 2)),
                   label=f"deployed {deployed}d")
        if f"sharpe_2x_cost" in s[str(grid[0])]:
            twox = np.array([s[str(w)]["sharpe_2x_cost"] for w in grid], float)
            ax.plot(grid, twox, color=C_2X, lw=0.9, label="full, 2x cost")
        ax.plot(grid, full, color=C_FULL, lw=2.0, marker="o", ms=3.2,
                label="full window")
        ax.plot(grid, train, color=C_TRAIN, lw=1.2, ls="--", marker="o",
                ms=2.5, label="train (18-11→22-09)")
        ax.plot(grid, test, color=C_TEST, lw=1.2, ls=":", marker="o",
                ms=2.5, label="test (22-09→26-06)")
        i_pk = int(np.nanargmax(full))
        ax.annotate(f"peak {grid[i_pk]}d {full[i_pk]:+.2f}",
                    (grid[i_pk], full[i_pk]), textcoords="offset points",
                    xytext=(4, 6), fontsize=7.5, color=C_FULL)
        ax.set_title(TITLES[sleeve], fontsize=9.5)
        ax.set_xlim(grid[0] - 10, grid[-1] + 10)
        ax.set_ylim(-0.45, 1.75)
        ax.set_xticks(grid[1::2])
        ax.set_xlabel("MA lookback (trading days)", fontsize=8)
        ax.set_ylabel("Sharpe", fontsize=8)
    axes.flat[0].legend(fontsize=6.8, loc="lower right", framealpha=0.9)

    # Heatmap panel: sleeve x W, full-window Sharpe
    axh = axes.flat[5]
    mat = np.array([[surf[s][str(w)]["full"]["sharpe"] for w in grid]
                    for s in SLEEVES], float)
    im = axh.imshow(mat, aspect="auto", cmap="RdYlGn",
                    vmin=np.nanmin(mat), vmax=np.nanmax(mat))
    axh.set_xticks(range(len(grid)), [str(w) for w in grid], fontsize=7)
    axh.set_yticks(range(len(SLEEVES)), SLEEVES, fontsize=8)
    axh.grid(False)
    for i in range(len(SLEEVES)):
        for j in range(len(grid)):
            axh.text(j, i, f"{mat[i, j]:.2f}".lstrip("0"), ha="center",
                     va="center", fontsize=5.6, color="black")
    axh.axvline(grid.index(deployed), color="#374151", lw=1.2,
                ls=(0, (4, 2)))
    axh.set_title("Full-window Sharpe heatmap", fontsize=9.5)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(OUT_SHARPE, bbox_inches="tight")
    print(f"wrote {OUT_SHARPE.relative_to(ROOT)}")

    # ------------------------- Drawdown figure -------------------------
    b = surf["blend"]
    have_dd = "dd_metrics" in b[str(grid[0])]
    if have_dd:
        fig2, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.0, 3.8), dpi=150)
        fig2.suptitle(
            f"WS1 — blend drawdown surface vs MA lookback  ({window})",
            fontsize=11, fontweight="bold")
        maxdd = [b[str(w)]["full"]["max_dd"] * 100 for w in grid]
        w12 = [b[str(w)]["dd_metrics"]["worst_rolling_12m_return"] * 100
               for w in grid]
        dd22 = [b[str(w)]["dd_metrics"]["dd_2022"] * 100 for w in grid]
        ax1.plot(grid, maxdd, color=C_FULL, lw=2.0, marker="o", ms=3.2,
                 label="max DD (COVID at every W)")
        ax1.plot(grid, w12, color=C_TEST, lw=1.4, ls="--", marker="o",
                 ms=2.8, label="worst rolling 12m")
        ax1.plot(grid, dd22, color=C_TRAIN, lw=1.4, ls=":", marker="o",
                 ms=2.8, label="DD within 2022")
        ax1.axvline(deployed, color="#374151", lw=0.9, ls=(0, (4, 2)))
        ax1.set_xlabel("MA lookback (trading days)", fontsize=8)
        ax1.set_ylabel("drawdown / return (%)", fontsize=8)
        ax1.set_xticks(grid[1::2])
        ax1.legend(fontsize=7.5, loc="lower right")
        ax1.set_title("Depth metrics (less negative = better)", fontsize=9.5)

        uw = [b[str(w)]["dd_metrics"]["longest_underwater_days"] for w in grid]
        bars = ax2.bar(grid, uw, width=16, color="#93c5fd",
                       edgecolor="#1e3a8a", linewidth=0.6)
        bars[grid.index(deployed)].set_color("#1e3a8a")
        ax2.axvline(deployed, color="#374151", lw=0.9, ls=(0, (4, 2)))
        ax2.set_xlabel("MA lookback (trading days)", fontsize=8)
        ax2.set_ylabel("trading days", fontsize=8)
        ax2.set_xticks(grid[1::2])
        ax2.set_title("Longest underwater spell (deployed W solid)",
                      fontsize=9.5)
        fig2.tight_layout(rect=(0, 0, 1, 0.94))
        fig2.savefig(OUT_DD, bbox_inches="tight")
        print(f"wrote {OUT_DD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
