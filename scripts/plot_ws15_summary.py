"""WS15 — the record's two charts, from committed evidence only.

Chart 1: the published CNDX OOS headline variant re-priced leg by leg
         (total return bars, Sharpe markers) — the decomposition exhibit.
Chart 2: median constituent-price coverage by year across the three panels.

Inputs:  reviews/ws15/ws15_oos_legs.json, reviews/ws15/ws15_breadth_compare.json
Outputs: reviews/charts/ws15_decomposition.png, reviews/charts/ws15_coverage.png

Run: python scripts/plot_ws15_summary.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "reviews" / "ws15"
CHARTS = ROOT / "reviews" / "charts"

NAVY, RED, TEAL = "#1e3a8a", "#dc2626", "#0891b2"
VARIANT = "regime_time_only_delay5_trend"

LEG_LABELS = {
    "T1_published_repro": "Published\n(May code,\nMay panel)",
    "T2_code_today": "Today's code,\nMay panel",
    "T3_aug_survivor": "Aug panel,\nsurvivor\nprices",
    "T4_aug_corrected": "Aug panel,\nWS11\ncorrected",
    "T5_ws15_residual": "Aug panel,\nWS15\nresidual-fixed",
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    CHARTS.mkdir(parents=True, exist_ok=True)
    legs = json.loads((WS / "ws15_oos_legs.json").read_text(encoding="utf-8"))["legs"]
    comp = json.loads((WS / "ws15_breadth_compare.json").read_text(encoding="utf-8"))

    # ---- Chart 1: decomposition ----------------------------------------
    names, tot, shp, sig = [], [], [], []
    for k, label in LEG_LABELS.items():
        row = next(r for r in legs[k]["summary_table"] if r["variant"] == VARIANT)
        names.append(label)
        tot.append(100 * row["equity_curve_total_return"])
        shp.append(row["sharpe_annualised"])
        sig.append(legs[k]["n_signal_fire_days"])

    fig, ax = plt.subplots(figsize=(9.0, 4.6), dpi=150)
    bars = ax.bar(range(5), tot, width=0.58, color=NAVY, alpha=0.9)
    for i, (b, t, n) in enumerate(zip(bars, tot, sig)):
        ax.annotate(f"{t:+.1f}%", (b.get_x() + b.get_width() / 2, t),
                    textcoords="offset points", xytext=(0, 6),
                    ha="center", fontsize=10, fontweight="bold", color=NAVY)
        ax.annotate(f"{n} signals", (b.get_x() + b.get_width() / 2, t / 2),
                    ha="center", va="center", fontsize=9, color="white")
    ax2 = ax.twinx()
    ax2.plot(range(5), shp, marker="D", color=TEAL, lw=1.6, ms=6)
    for i, s in enumerate(shp):
        ax2.annotate(f"{s:+.2f}", (i, s), textcoords="offset points",
                     xytext=(8, 14), fontsize=9, color=TEAL)
    ax.set_xticks(range(5), names, fontsize=9)
    ax.set_ylabel("Total return over the window (%)", fontsize=10)
    ax2.set_ylabel("Sharpe (annualised)", fontsize=10, color=TEAL)
    ax2.tick_params(axis="y", colors=TEAL)
    ax.set_ylim(0, 55)
    ax2.set_ylim(0, 0.62)
    ax.spines[["top"]].set_visible(False)
    ax2.spines[["top"]].set_visible(False)
    ax.set_title("The published CNDX OOS result, re-priced one change at a time "
                 f"(variant: {VARIANT})", fontsize=11)
    fig.tight_layout()
    fig.savefig(CHARTS / "ws15_decomposition.png")
    plt.close(fig)

    # ---- Chart 2: coverage by year -------------------------------------
    years = sorted(int(y) for y in comp["by_year"])
    series = {
        "Survivor prices (as published)": ("survivor", RED),
        "WS11 corrected (2026-08-10)": ("corrected", NAVY),
        "WS15 residual-fixed": ("ws15", TEAL),
    }
    fig, ax = plt.subplots(figsize=(9.0, 3.9), dpi=150)
    for label, (key, colour) in series.items():
        vals = [comp["by_year"][str(y)]["coverage_median_pct"][key]
                for y in years]
        ax.plot(years, vals, marker="o", ms=4.5, lw=1.8, color=colour,
                label=label)
    ax.set_ylim(75, 101.5)
    ax.set_ylabel("Median share of the roster priced (%)", fontsize=10)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title("CNDX constituents with a usable price, by year", fontsize=11)
    ax.grid(axis="y", lw=0.4, alpha=0.35)
    fig.tight_layout()
    fig.savefig(CHARTS / "ws15_coverage.png")
    plt.close(fig)

    print(f"Wrote {CHARTS / 'ws15_decomposition.png'}")
    print(f"Wrote {CHARTS / 'ws15_coverage.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
