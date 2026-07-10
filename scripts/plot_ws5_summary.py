"""WS5 record charts — reproducible from data/ws5_results.json.

Two figures for reviews/2026-07-10_ws5_relative-trend.docx:
  fig1  walk-forward OOS Sharpe by arm, deployed A0 highlighted, the momentum
        placebo and the adopt threshold drawn as reference lines.
  fig2  scope funnel — configurations evaluated -> adopted -> on watch.

House chart conventions (research-review report_format.md): white theme,
sans-serif, navy #1e3a8a primary, teal #0891b2 / red #dc2626 secondary, green
#dcfce7 "same within noise" fill, every displayed number rounded.

Run: python scripts/plot_ws5_summary.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "ws5_results.json"
ASSETS = ROOT / "reviews" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

NAVY = "#1e3a8a"
TEAL = "#0891b2"
RED = "#dc2626"
GREY = "#9ca3af"
GREEN = "#dcfce7"
INK = "#1f2937"

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Segoe UI", "Arial", "DejaVu Sans"],
        "axes.edgecolor": "#d1d5db",
        "axes.linewidth": 0.8,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    }
)


def _load() -> dict:
    return json.loads(RESULTS.read_text(encoding="utf-8"))


def fig1_wf_sharpe(d: dict) -> Path:
    reg = d["register"]
    # display order: deployed, its union, placebo, then the four challenger arms
    order = [
        ("0_A0_absolute", "A0 absolute\n(deployed)", NAVY),
        ("4_OR", "OR\n(A0 or A1)", GREY),
        ("3_P_placebo", "P momentum\nplacebo", TEAL),
        ("1_A1_relative", "A1 relative", GREY),
        ("6_A2_rel250", "A2 dual\nrel-250d", GREY),
        ("5_A2_rel150", "A2 dual\nrel-150d", GREY),
        ("2_A2_dual", "A2 dual\nrel-200d", GREY),
    ]
    labels = [o[1] for o in order]
    vals = [reg[o[0]]["wf_1x_sharpe"] for o in order]
    colors = [o[2] for o in order]

    a0 = reg["0_A0_absolute"]["wf_1x_sharpe"]
    plac = reg["3_P_placebo"]["wf_1x_sharpe"]
    margin = d["config"]["adopt_margin"]
    adopt_bar = a0 + margin  # must also clear P + margin; A0+margin is the binding one

    fig, ax = plt.subplots(figsize=(9.0, 4.4))
    x = range(len(vals))
    bars = ax.bar(x, vals, color=colors, width=0.62, zorder=3)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.012, f"{v:+.3f}",
                ha="center", va="bottom", fontsize=9, color=INK, zorder=4)

    # reference lines: the placebo (bar to beat) and the adopt threshold
    ax.axhline(plac, color=TEAL, lw=1.1, ls=":", zorder=2)
    ax.text(len(vals) - 0.4, plac + 0.006, f"placebo {plac:+.3f}",
            ha="right", va="bottom", fontsize=8.5, color=TEAL)
    ax.axhline(adopt_bar, color=RED, lw=1.2, ls="--", zorder=2)
    ax.text(len(vals) - 0.4, adopt_bar + 0.006,
            f"adopt bar  A0 + {margin:.2f} = {adopt_bar:.3f}",
            ha="right", va="bottom", fontsize=8.5, color=RED)

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("Walk-forward OOS Sharpe (2 bps)", fontsize=10)
    ax.set_ylim(0, max(vals + [adopt_bar]) + 0.14)
    ax.set_title(
        "No challenger clears the bar — the relative leg loses to the deployed "
        "absolute leg and to plain momentum",
        fontsize=10.5, color=INK, pad=10, loc="left", wrap=True,
    )
    ax.grid(axis="y", color="#eef1f5", lw=0.8, zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    out = ASSETS / "ws5_fig1_wf_sharpe.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig2_scope(d: dict) -> Path:
    # categories of evaluated engine arms; the funnel count is the SUM of the
    # bars (the 7 register rows). The DSR is charged at N=8 — these 7 arms plus
    # one blend-context trial — a conservative multiple-testing pad noted in the
    # record's trial register, deliberately NOT shown on this chart so the
    # funnel number matches the bars.
    cats = [
        ("Incumbent + union (A0, OR)", 2),
        ("Relative / dual challengers (A1, A2)", 2),
        ("Asymmetric-window neighbours (rel-150, rel-250)", 2),
        ("Momentum placebo control (P)", 1),
    ]
    labels = [c[0] for c in cats]
    counts = [c[1] for c in cats]
    n = sum(counts)

    fig, ax = plt.subplots(figsize=(9.0, 3.5))
    y = range(len(cats))
    ax.barh(list(y), counts, color=NAVY, height=0.6, zorder=3)
    for i, c in enumerate(counts):
        ax.text(c + 0.05, i, str(c), va="center", ha="left", fontsize=9, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 2.6)
    ax.set_xlabel("configurations evaluated", fontsize=9.5)
    ax.set_title(
        f"Scope and restraint:  {n} arms evaluated  →  0 adopted  "
        f"→  0 on watch",
        fontsize=10.5, color=INK, pad=10, loc="left",
    )
    ax.grid(axis="x", color="#eef1f5", lw=0.8, zorder=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    fig.tight_layout()
    out = ASSETS / "ws5_fig2_scope.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main() -> None:
    d = _load()
    f1 = fig1_wf_sharpe(d)
    f2 = fig2_scope(d)
    wd = date(2026, 7, 10).strftime("%A")  # weekday check for the record
    print("weekday of 2026-07-10:", wd)
    print("wrote", f1.relative_to(ROOT))
    print("wrote", f2.relative_to(ROOT))


if __name__ == "__main__":
    main()
