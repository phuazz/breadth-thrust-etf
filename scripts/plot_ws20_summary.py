"""WS20 record charts — built from the run's own results file.

    python scripts/plot_ws20_summary.py

Reads `data_local/ws20/results.json` (the single registered pass) and writes
two figures to `reviews/charts/`. Nothing is recomputed here: every number
plotted is read from the file the run wrote, so the record and the chart cannot
drift apart.

House conventions (research-review skill): white theme, sans-serif, navy
primary, red for the realised estimate, a green band for "same within noise",
plain-language titles, every displayed number rounded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS = PROJECT_ROOT / "data_local" / "ws20" / "results.json"
OUT_DIR = PROJECT_ROOT / "reviews" / "charts"

NAVY = "#1e3a8a"
RED = "#dc2626"
TEAL = "#0891b2"
GREEN_FILL = "#dcfce7"
GREY = "#6b7280"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#d1d5db",
    "axes.labelcolor": "#111827",
    "text.color": "#111827",
    "xtick.color": "#374151",
    "ytick.color": "#374151",
    "axes.grid": True,
    "grid.color": "#e5e7eb",
    "grid.linewidth": 0.6,
})


def chart_null(d: dict, out: Path) -> Path:
    """The estimate against the distribution built by scrambling the pairing."""
    p = d["primary"]
    draws = np.array([v for v in p["null_draws"] if v is not None], dtype=float)
    est = float(p["mean_coefficient"])
    lo, hi = float(p["null"]["p2_5"]), float(p["null"]["p97_5"])

    fig, ax = plt.subplots(figsize=(8.4, 4.0))
    ax.hist(draws, bins=45, color=NAVY, alpha=0.75, edgecolor="white", linewidth=0.5)
    ax.axvspan(lo, hi, color=GREEN_FILL, zorder=0)
    ax.axvline(lo, color=TEAL, lw=1.0, ls="--")
    ax.axvline(hi, color=TEAL, lw=1.0, ls="--")
    ax.axvline(est, color=RED, lw=2.2)
    ymax = ax.get_ylim()[1]
    ax.annotate(f"measured  {est:+.5f}\n{p['null_percentile']*100:.0f}th percentile",
                xy=(est, ymax * 0.92), xytext=(12, 0), textcoords="offset points",
                color=RED, fontsize=9.5, fontweight="bold", va="top")
    ax.annotate("central 95% of the scrambled runs", xy=(0.985, 0.42),
                xycoords="axes fraction", ha="right", color=TEAL, fontsize=8.5)
    ax.set_title("The measured result is what scrambling the data also produces",
                 fontsize=12, fontweight="bold", pad=10)
    ax.set_xlabel("Average weekly coefficient on dispersion")
    ax.set_ylabel("Number of scrambled runs")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def chart_legs(d: dict, out: Path) -> Path:
    """Every leg computed, against zero. A null result, seen at once."""
    p, r = d["primary"], d["reported_only"]
    rows = [
        ("Primary: 4-week horizon", p["mean_coefficient"], p["ci95"], True),
        ("Primary — first half", p["half1"]["mean"], None, True),
        ("Primary — second half", p["half2"]["mean"], None, True),
        ("1-week horizon", r["horizon_1w"]["mean_coefficient"], None, False),
        ("13-week horizon", r["horizon_13w"]["mean_coefficient"], None, False),
        ("Standard deviation, not IQR", r["sd_variant_4w"]["mean_coefficient"], None, False),
        ("With panel-size control", r["with_ln_n_control"]["mean_coefficient"], None, False),
        ("No breadth control (confounded)",
         r["univariate_no_breadth_control"]["mean_coefficient"], None, False),
    ]
    labels = [x[0] for x in rows]
    vals = [float(x[1]) for x in rows]
    ys = np.arange(len(rows))[::-1]

    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    ci = rows[0][2]
    ax.axvspan(float(ci[0]), float(ci[1]), color=GREEN_FILL, zorder=0)
    ax.axvline(0, color=GREY, lw=1.2)
    for y, (lab, v, c, primary) in zip(ys, rows):
        colour = RED if primary else NAVY
        ax.plot([float(v)], [y], "o", color=colour, ms=7 if primary else 5.5, zorder=3)
        if c is not None:
            ax.plot([float(c[0]), float(c[1])], [y, y], color=colour, lw=2.0, zorder=2)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=9.5)
    ax.set_xlabel("Average weekly coefficient on dispersion "
                  "(0 = dispersion tells you nothing)")
    ax.set_title("Every way of cutting it lands on zero",
                 fontsize=12, fontweight="bold", pad=10)
    ax.annotate("shaded: the primary estimate's 95% interval",
                xy=(0.99, 0.04), xycoords="axes fraction", ha="right",
                fontsize=8.5, color=TEAL)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main() -> int:
    if not RESULTS.exists():
        print(f"missing {RESULTS} — run scripts/run_ws20_dispersion.py first")
        return 1
    d = json.loads(RESULTS.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for f in (chart_null(d, OUT_DIR / "ws20_null.png"),
              chart_legs(d, OUT_DIR / "ws20_legs.png")):
        print(f"wrote {f.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
