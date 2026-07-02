"""Render the WS1 threshold surfaces as annotated heatmaps.

Reads  data/ws1_threshold_surface.json
Writes data/ws1_threshold_surface.png

Three panels: Sleeve C floor x gate (full-window Sharpe), the same grid on
the TEST half (exposes the train-concentrated ridge), and the Phase 19
off x on hysteresis surface. Deployed cells outlined; degenerate C cells
(average invested share < 60%) greyed with an asterisk.

Run: python scripts/plot_ws1_thresholds.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.patches as mpatches  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = DATA / "ws1_threshold_surface.json"
OUT = DATA / "ws1_threshold_surface.png"

plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 9,
    "figure.facecolor": "white", "axes.facecolor": "white",
})


def heat(ax, mat, xlabels, ylabels, title, deployed_xy, degenerate=None,
         vmin=None, vmax=None):
    im = ax.imshow(mat, aspect="auto", cmap="RdYlGn", vmin=vmin, vmax=vmax)
    ax.set_xticks(range(len(xlabels)), xlabels, fontsize=8)
    ax.set_yticks(range(len(ylabels)), ylabels, fontsize=8)
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            if np.isnan(mat[i, j]):
                continue
            mark = "*" if degenerate is not None and degenerate[i, j] else ""
            col = "#666666" if mark else "black"
            ax.text(j, i, f"{mat[i, j]:+.2f}{mark}", ha="center", va="center",
                    fontsize=7.5, color=col)
    if deployed_xy is not None:
        ax.add_patch(mpatches.Rectangle(
            (deployed_xy[0] - 0.5, deployed_xy[1] - 0.5), 1, 1, fill=False,
            edgecolor="#111111", lw=2.2))
    ax.set_title(title, fontsize=9.5)
    ax.grid(False)
    return im


def main() -> int:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    floors, gates = d["c_floors"], d["c_gates"]
    offs, ons = d["gate_offs"], d["gate_ons"]

    def c_mat(key):
        m = np.full((len(floors), len(gates)), np.nan)
        for i, fl in enumerate(floors):
            for j, gt in enumerate(gates):
                cell = d["c_surface"][f"floor={fl}|gate={gt}"]
                m[i, j] = (cell[key]["sharpe"] if key in ("full", "test")
                           else cell[key])
        return m

    c_full = c_mat("full")
    c_test = c_mat("test")
    c_deg = np.array([[d["c_surface"][f"floor={fl}|gate={gt}"]["degenerate"]
                       for gt in gates] for fl in floors])
    g_full = np.full((len(offs), len(ons)), np.nan)
    for i, off in enumerate(offs):
        for j, on in enumerate(ons):
            cell = d["phase19_surface"].get(f"off={off}|on={on}")
            if cell:
                g_full[i, j] = cell["full"]["sharpe"]
    ungated = d["phase19_surface"]["no_gate"]["full"]["sharpe"]

    fig, axes = plt.subplots(1, 3, figsize=(13.0, 3.9), dpi=150)
    vlo = min(np.nanmin(c_full), np.nanmin(c_test))
    vhi = max(np.nanmax(c_full), np.nanmax(c_test))
    heat(axes[0], c_full, [f"{g*100:.0f}%" for g in gates],
         [f"{f*100:.1f}%" for f in floors],
         "Sleeve C — full-window Sharpe (floor x gate)",
         (gates.index(0.30), floors.index(0.05)), c_deg, vlo, vhi)
    axes[0].set_xlabel("sleeve-gate threshold", fontsize=8)
    axes[0].set_ylabel("signal floor", fontsize=8)
    heat(axes[1], c_test, [f"{g*100:.0f}%" for g in gates],
         [f"{f*100:.1f}%" for f in floors],
         "Sleeve C — TEST-half Sharpe (ridge fades OOS)",
         (gates.index(0.30), floors.index(0.05)), c_deg, vlo, vhi)
    axes[1].set_xlabel("sleeve-gate threshold", fontsize=8)
    heat(axes[2], g_full, [f"{o*100:.0f}%" for o in ons],
         [f"{o*100:.0f}%" for o in offs],
         f"Phase 19 gate — full Sharpe (no gate {ungated:+.2f})",
         (ons.index(0.50), offs.index(0.20)))
    axes[2].set_xlabel("re-engage (on) threshold", fontsize=8)
    axes[2].set_ylabel("de-risk (off) threshold", fontsize=8)
    fig.suptitle("WS1 threshold surfaces — deployed cells outlined; "
                 "* = degenerate (avg invested < 60%)",
                 fontsize=10.5, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(OUT, bbox_inches="tight")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
