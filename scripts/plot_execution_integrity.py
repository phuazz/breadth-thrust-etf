"""The 2026-08-14 decision-session reversal, drawn from the committed panels.

One exhibit, because the whole finding is one comparison: what Strategy D's
ranking said on Wednesday 12 August, which is the session a vendor hole pushed
the decision back to, against what it said on Thursday 13 August, which is the
session the deployed convention actually reads.

Chart conventions per the research-review skill: white theme, navy primary,
absolute scale (0-100% breadth, not each panel's own range, so a strong reading
looks strong), plain-language title, and each bar carrying its RANK that day —
sleeve D holds the top three, so rank is the decision.

Usage:
    python scripts/plot_execution_integrity.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT = PROJECT_ROOT / "reviews" / "assets"

NAVY, RED, TEAL, GREY = "#1e3a8a", "#dc2626", "#0891b2", "#94a3b8"
UNIVERSE = ["EXV1", "EXH1", "EXV3", "EXH3", "EXH9"]
LABEL = {"EXV1": "EXV1\nBanks", "EXH1": "EXH1\nOil & Gas",
         "EXV3": "EXV3\nTechnology", "EXH3": "EXH4\nIndustrials",
         "EXH9": "EXH9\nUtilities"}
WED, THU = "2026-08-12", "2026-08-13"


def _ma200_breadth_at(etf: str, day: str) -> float | None:
    """Share of constituents above their 200d average, from the committed panel.

    Read from data/ma200_sweep.json where available so the figure traces to a
    published artefact rather than to a recomputation.
    """
    p = DATA_DIR / "ma200_sweep.json"
    if not p.exists():
        return None
    blob = json.loads(p.read_text(encoding="utf-8"))
    series = (blob.get("series") or {}).get(etf) or {}
    dates, vals = series.get("dates") or [], series.get("ma200_breadth") or []
    if day in dates:
        v = vals[dates.index(day)]
        return None if v is None else float(v) * (100 if v <= 1 else 1)
    return None


# Measured 2026-08-14/15 and recorded in RESEARCH_MEMO.md. Used when the
# committed sweep does not carry a per-day series; the values are the ones the
# record quotes, so the figure cannot drift from the prose.
FALLBACK = {
    WED: {"EXV1": 94.0, "EXH1": 80.6, "EXV3": 73.6, "EXH3": 71.6, "EXH9": 63.6},
    THU: {"EXV1": 93.9, "EXH1": 81.2, "EXV3": 71.7, "EXH3": 73.0, "EXH9": 60.6},
}


def build() -> Path:
    wed = [_ma200_breadth_at(e, WED) or FALLBACK[WED][e] for e in UNIVERSE]
    thu = [_ma200_breadth_at(e, THU) or FALLBACK[THU][e] for e in UNIVERSE]

    fig, ax = plt.subplots(figsize=(8.4, 4.0), dpi=170)
    x = range(len(UNIVERSE))
    w = 0.36
    ax.bar([i - w / 2 for i in x], wed, w, label="Wed 12 Aug — the session the hole fell back to",
           color=GREY, edgecolor="white")
    ax.bar([i + w / 2 for i in x], thu, w, label="Thu 13 Aug — the session the rule reads",
           color=NAVY, edgecolor="white")

    # Sleeve D holds the TOP 3, so rank is the decision. Two dotted cut-off
    # lines were tried first and sat almost on top of each other (73.6 against
    # 73.0) — technically the finding, visually a smudge. Ranks say it outright.
    def _ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: -vals[i])
        return {i: r + 1 for r, i in enumerate(order)}

    rw, rt = _ranks(wed), _ranks(thu)
    for i in range(len(UNIVERSE)):
        for vals, rk, off, col in ((wed, rw, -w / 2, "#475569"),
                                   (thu, rt, w / 2, "white")):
            held = rk[i] <= 3
            # Held ranks must read at a glance; dropped ones must still be
            # legible against their own bar, not merely faded into it.
            if col == "white":                       # Thursday, navy bar
                shade = "white" if held else "#c7d2fe"
            else:                                    # Wednesday, grey bar
                shade = "#0f172a" if held else "#475569"
            ax.text(i + off, 3.5, f"{rk[i]}",
                    ha="center", va="bottom", fontsize=10,
                    fontweight="bold" if held else "normal", color=shade)

    for i, (a, b) in enumerate(zip(wed, thu)):
        ax.text(i - w / 2, a + 1.4, f"{a:.1f}", ha="center", fontsize=8, color="#475569")
        ax.text(i + w / 2, b + 1.4, f"{b:.1f}", ha="center", fontsize=8,
                color=NAVY, fontweight="bold")

    # ONE continuous band over the pair that swaps, not two with a seam.
    ax.add_patch(plt.Rectangle((1.53, 0), 1.94, 100, color=RED, alpha=0.07, zorder=0))
    ax.annotate("rank 3 and rank 4 trade places:\n"
                "EXV3 in on Wednesday, EXH4 in on Thursday",
                xy=(2.5, 92), ha="center", fontsize=9, color=RED, fontweight="bold")
    # The rank key lives in the docx caption, not on the plate: in-chart it
    # either sat over the bars or faded to unreadable against them.

    ax.set_xticks(list(x))
    ax.set_xticklabels([LABEL[e] for e in UNIVERSE], fontsize=9)
    ax.set_ylim(0, 108)                       # ABSOLUTE scale, not per-panel
    ax.set_ylabel("Constituents above their 200-day average (%)", fontsize=9)
    ax.set_title("One session changes which European sectors the book holds",
                 fontsize=11.5, fontweight="bold", color="#0f172a", pad=10)
    # BELOW the plate. Inside it, the legend covered either the tallest pair's
    # value labels or the rank row — there is no free interior on a chart whose
    # bars run the full height by design (absolute 0-100 scale).
    ax.legend(fontsize=8.5, frameon=False, loc="upper center",
              bbox_to_anchor=(0.5, -0.16), ncol=2)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e2e8f0", lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "2026-08-15_decision_session_reversal.png"
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {out}")
    return out


if __name__ == "__main__":
    sys.exit(0 if build() else 1)
