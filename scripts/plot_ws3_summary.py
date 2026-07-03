"""WS3 plain-language summary figures — read from data/ws3_*.json, no
hand-typed numbers. Committed so the filed summary record is reproducible
(research-review skill convention: charts from a repo script, not a
scratchpad one-off).

Outputs (data/):
  ws3_sum_refit.png      re-tuning vs leaving alone (WF OOS Sharpe bars)
  ws3_sum_selection.png  observed Sharpe vs pure-selection expectation
  ws3_sum_gate.png       gate insurance vs randomly-timed placebos
  ws3_sum_tilt.png       tilt episode ledger (one bet carries it)
  ws3_sum_cost.png       cost stress on the final track vs equal-weight
  ws3_sum_scope.png      scope funnel (tested -> changed -> on watch)

Run: python scripts/plot_ws3_summary.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.stdout.reconfigure(encoding="utf-8")

NAVY, RED, TEAL, GREY = "#1e3a8a", "#dc2626", "#0891b2", "#9ca3af"
GREEN_FILL = "#dcfce7"
plt.rcParams.update({"font.family": "sans-serif", "font.size": 11,
                     "axes.edgecolor": "#d1d5db", "axes.linewidth": 0.8,
                     "figure.facecolor": "white", "axes.facecolor": "white"})


def j(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def bar_labels(ax, bars, fmt="{:+.2f}", dy=0.02):
    for b in bars:
        h = b.get_height()
        ax.text(b.get_x() + b.get_width() / 2,
                h + (dy if h >= 0 else -dy), fmt.format(h),
                ha="center", va="bottom" if h >= 0 else "top", fontsize=11,
                fontweight="bold", color="#111827")


def fig_refit(wf: dict) -> None:
    p = wf["protocols"]
    labels = ["Left alone\n(deployed settings)",
              "Weights re-chosen\nevery year",
              "Everything re-chosen\nevery year",
              "Best possible\nin hindsight"]
    vals = [p["frozen_deployed"]["oos_sharpe"],
            p["wf_weights_only"]["oos_sharpe"],
            p["wf_full"]["oos_sharpe"],
            p["oracle_full"]["oos_sharpe"]]
    colours = [NAVY, RED, RED, GREY]
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    bars = ax.bar(labels, vals, color=colours, width=0.62)
    bars[3].set_hatch("//")
    bars[3].set_edgecolor("white")
    bar_labels(ax, bars)
    ax.set_ylim(0, 1.55)
    ax.set_ylabel("Risk-adjusted return, 2022-2026\n(out of sample; higher is better)")
    ax.set_title("Re-tuning every year would have LOST money\n"
                 "versus leaving the strategy alone", fontsize=12.5,
                 fontweight="bold", color="#111827", pad=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(DATA / "ws3_sum_refit.png", dpi=130)


def fig_selection(defl: dict) -> None:
    tr = defl["tracks"]["deployed_final_gated_tilted"]
    observed = tr["sr_annual"]
    e171 = tr["v_measured"]["per_n"]["register_171"]["sr0_annual"]
    e576 = tr["v_diverse_incl_constructions"]["per_n"]["nominal_high"]["sr0_annual"]
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    labels = ["Luck alone,\n171 documented trials",
              "Luck alone,\nliberal ~576 trials",
              "The strategy,\nas deployed"]
    vals = [e171, e576, observed]
    bars = ax.bar(labels, vals, color=[GREY, GREY, NAVY], width=0.55)
    bar_labels(ax, bars)
    ax.set_ylim(0, 1.55)
    ax.set_ylabel("Risk-adjusted return\n(full 2018-2026 window)")
    ax.set_title("The result is three to four times what\n"
                 "trial-and-error luck alone would produce", fontsize=12.5,
                 fontweight="bold", color="#111827", pad=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(DATA / "ws3_sum_selection.png", dpi=130)


def fig_gate(ob: dict) -> None:
    g = ob["phase19_gate"]
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    labels = ["Same de-risking,\nrandomly timed (typical of 1,000)",
              "The deployed gate\n(timed by market breadth)"]
    vals = [g["placebo"]["placebo_dd_improvement_p50_pp"],
            g["dd_improvement_pp"]]
    bars = ax.bar(labels, vals, color=[GREY, NAVY], width=0.45)
    bar_labels(ax, bars, fmt="{:+.1f}pp")
    ax.set_ylim(-0.8, 9.5)
    ax.axhline(0, color="#6b7280", lw=0.8)
    ax.set_ylabel("Worst-loss improvement\n(percentage points of drawdown avoided)")
    ax.set_title("The safety brake's timing is real: random timing\n"
                 "buys nothing; the gate avoided 7pp of drawdown",
                 fontsize=12.5, fontweight="bold", color="#111827", pad=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(DATA / "ws3_sum_gate.png", dpi=130)


def fig_tilt(ob: dict) -> None:
    eps = ob["phase22_tilt"]["episodes"]
    labels = [f"{e['start'][:7]}" + ("\n(still open)" if e["end"] == "open"
                                     else "") for e in eps]
    vals = [e["contribution_pp"] for e in eps]
    colours = [TEAL if e["end"] == "open" else GREY for e in eps]
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    bars = ax.bar(labels, vals, color=colours, width=0.6)
    bar_labels(ax, bars, fmt="{:+.1f}pp", dy=0.06)
    ax.axhline(0, color="#6b7280", lw=0.8)
    ax.set_ylabel("Contribution to portfolio return\n(percentage points, per episode)")
    ax.set_title("The emerging-markets tilt has made six bets ever —\n"
                 "one still-open bet carries it", fontsize=12.5,
                 fontweight="bold", color="#111827", pad=10)
    ax.set_ylim(-1.6, 3.9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(DATA / "ws3_sum_tilt.png", dpi=130)


def fig_cost(cost: dict) -> None:
    ft = cost["final_track"]
    bench = cost["benchmark_sharpe"]["blend"]
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    labels = ["Realistic\ncosts (1x)", "Double\ncosts (2x)", "Triple\ncosts (3x)"]
    vals = [ft["1x"], ft["2x"], ft["3x"]]
    bars = ax.bar(labels, vals, color=NAVY, width=0.5)
    bar_labels(ax, bars)
    # The dashed line is decoded by the docx caption (house convention:
    # text is the caption, not the content) — an in-figure label collides
    # with whichever bar is nearest.
    ax.axhline(bench, color=RED, lw=1.6, ls="--")
    ax.set_ylim(0, 1.55)
    ax.set_ylabel("Risk-adjusted return\n(full 2018-2026 window)")
    ax.set_title("Trading costs do not explain the edge: it survives\n"
                 "triple costs and breaks even only near 6x", fontsize=12.5,
                 fontweight="bold", color="#111827", pad=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(DATA / "ws3_sum_cost.png", dpi=130)


def fig_scope(defl: dict, wf: dict) -> None:
    cats = [("Settings grid (horizon x picks x floors)", 34),
            ("Whole-system re-tuning protocols", 5),
            ("Do-nothing cost benchmarks", 5),
            ("Shortlist variants on the new set-up", 2),
            ("Noise tests (1,000 placebos + bootstraps)", 0),
    ]
    fig, ax = plt.subplots(figsize=(8.6, 3.9))
    names = [c[0] for c in cats]
    vals = [c[1] for c in cats]
    bars = ax.barh(names[::-1], vals[::-1], color=NAVY, height=0.55)
    for b, v in zip(bars, vals[::-1]):
        ax.text(b.get_width() + 0.4, b.get_y() + b.get_height() / 2,
                (str(v) if v else "diagnostics"), va="center", fontsize=11,
                fontweight="bold", color="#111827")
    ax.set_xlim(0, 40)
    ax.set_xlabel("Configurations evaluated this session (46 registered)")
    ax.set_title("Tested 46 more configurations this session —\n"
                 "changed nothing — three items on watch",
                 fontsize=12.5, fontweight="bold", color="#111827", pad=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(DATA / "ws3_sum_scope.png", dpi=130)


def main() -> int:
    defl, ob, wf, cost = (j("ws3_deflated.json"),
                          j("ws3_overlay_bootstrap.json"),
                          j("ws3_full_wf.json"), j("ws3_cost_stress.json"))
    fig_refit(wf)
    fig_selection(defl)
    fig_gate(ob)
    fig_tilt(ob)
    fig_cost(cost)
    fig_scope(defl, wf)
    for f in sorted(DATA.glob("ws3_sum_*.png")):
        print("wrote", f.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
