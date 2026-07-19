"""WS6 record chart: net Sharpe per register arm across the cost sweep.

Reads data/ws6_results.json (status COMPLETE, run 4) and writes the single
exhibit used in the 2026-07-19 technical record. Reproducible from the
committed results file; no recomputation.
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "data" / "ws6_results.json"
OUT = ROOT / "reviews" / "charts" / "ws6_register_cost_sweep.png"

# Plot order fixes the legend; colours per house chart conventions
# (navy primary, teal/red secondary, greys for placebo/report-only arms).
ARM_STYLE = [
    ("E0", "deployed ETF baseline (E0)", "#1e3a8a", "-", 2.6),
    ("I0", "unscreened replication (I0, control)", "#0891b2", "-", 2.0),
    ("I1", "screened replication (I1, Design 1)", "#dc2626", "-", 2.0),
    ("I1-all", "I1 without top-M cap (report-only)", "#f87171", ":", 1.4),
    ("I2", "top-10 strength (I2, Design 2)", "#4b5563", "--", 1.8),
    ("P2", "top-10 momentum placebo (P2)", "#9ca3af", "--", 1.8),
    ("I2-N15", "top-15 strength (report-only)", "#6b7280", ":", 1.4),
    ("P2-N15", "top-15 momentum placebo (report-only)", "#d1d5db", ":", 1.4),
]

def main() -> None:
    d = json.loads(RESULTS.read_text())
    assert d["status"] == "COMPLETE", "chart is only valid on a COMPLETE register"
    reg = d["register"]
    costs = [2, 5, 10, 20]

    fig, ax = plt.subplots(figsize=(8.6, 5.0), dpi=150)
    for arm, label, colour, ls, lw in ARM_STYLE:
        if arm == "E0":
            # E0 keeps the deployed 2-9 bps cost model; constant across the sweep.
            y = [reg["E0"]["by_cost"]["2"]["net_sharpe"]] * len(costs)
        else:
            y = [reg[arm]["by_cost"][str(c)]["net_sharpe"] for c in costs]
        ax.plot(costs, y, ls, color=colour, linewidth=lw, label=label,
                marker="o", markersize=3.5)

    e0 = reg["E0"]["by_cost"]["2"]["net_sharpe"]
    ax.scatter([5, 10], [e0 - 0.05, e0 - 0.10], marker="x", s=90,
               color="#111827", zorder=5)
    ax.annotate("ADOPT-D1 floor at 1x (E0 - 0.05)", (5, e0 - 0.05),
                textcoords="offset points", xytext=(8, 6), fontsize=8)
    ax.annotate("ADOPT-D1 floor at 2x (E0 - 0.10)", (10, e0 - 0.10),
                textcoords="offset points", xytext=(8, 6), fontsize=8)

    ax.set_xlabel("one-way cost per unit weight change (bps)")
    ax.set_ylabel("net Sharpe (full window)")
    ax.set_xticks(costs)
    ax.set_ylim(0.70, 1.05)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=7.5, loc="lower left", framealpha=0.95)
    ax.set_title("WS6 register: net Sharpe by arm across the cost sweep",
                 fontsize=11)
    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, facecolor="white")
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
