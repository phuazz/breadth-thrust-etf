"""Finding charts for the plain-language summary — one visual per finding.

Promoted from a scratchpad one-off so the allocator summary's page-1/2/3
charts are reproducible from the repo. All numbers trace to the WS1
artefacts (ws1_wf_horizon.json, ws1_vol_variants.json,
ws1_threshold_surface.json, risk_overlay.json).

Writes: data/ws1_sum_structure.png  (portfolio structure)
        data/ws1_sum_retuning.png    (finding 2 — re-tuning loses OOS)
        data/ws1_sum_smarter.png     (finding 3 — vol-adjusted upgrade lost)
        data/ws1_sum_brake.png       (finding 4 — the safety brake)
        data/ws1_sum_thematic.png    (finding 5 — the declined trade-off)

Run: python scripts/plot_ws1_summary_findings.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"

NAVY = "#1e3a8a"
STEEL = "#7d96c4"
GREY = "#b9bfc9"
GREEN = "#15803d"
RED = "#b91c1c"

plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 12.5,
    "figure.facecolor": "white", "axes.facecolor": "white",
    "axes.edgecolor": "#9ca3af",
})


def style_bar_ax(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    ax.set_axisbelow(True)


# ---------------- 1. Structure ----------------
fig, ax = plt.subplots(figsize=(9.6, 3.3), dpi=150)
books = [("US sectors", 35, "#1e3a8a"), ("Global asset classes", 35, "#2f5aa8"),
         ("Thematics", 10, "#5b82c4"), ("European sectors", 20, "#8aa8d6")]
left = 0
for name, w, c in books:
    ax.barh(0.80, w, left=left, height=0.34, color=c)
    if w >= 25:
        ax.text(left + w / 2, 0.80, f"{name}   {w}%", ha="center",
                va="center", color="white", fontsize=12, fontweight="bold")
    else:
        ax.text(left + w / 2, 0.80, f"{w}%", ha="center", va="center",
                color="white", fontsize=12, fontweight="bold")
        ax.text(left + w / 2, 0.535, name, ha="center", va="center",
                color="#333333", fontsize=10.5)
    left += w
ax.text(0, 1.16, "One portfolio, four rotation books — each holds its strongest funds, reviewed weekly",
        fontsize=13.5, fontweight="bold", va="center")
for y, txt in ((0.30, "Market-breadth brake — moves half the portfolio to short-term Treasuries when most S&P 500 stocks break down"),
               (0.10, "Emerging-markets tilt — adds 10% emerging-market exposure while EM is trending ahead of the US")):
    ax.text(0.6, y, txt, fontsize=10.8, va="center", color="#333333",
            bbox=dict(boxstyle="round,pad=0.38", facecolor="#eef2f7",
                      edgecolor="#9ca3af", linewidth=0.7))
ax.text(0, -0.10, "All books use the same trend test: is the price above its 200-day (≈10-month) average?",
        fontsize=11, color="#555555", style="italic")
ax.set_xlim(0, 100)
ax.set_ylim(-0.18, 1.30)
ax.axis("off")
fig.tight_layout()
fig.savefig(OUT / "ws1_sum_structure.png", bbox_inches="tight")

# ---------------- 2. Re-tuning race ----------------
fig, ax = plt.subplots(figsize=(9.6, 2.9), dpi=150)
rows = [("Perfect hindsight  (not achievable)", 1.227, GREY, "//"),
        ("Re-tuned each book separately", 1.107, STEEL, None),
        ("Re-tuned every January", 1.170, STEEL, None),
        ("Left alone — the deployed setting", 1.183, NAVY, None)]
for i, (label, v, c, hatch) in enumerate(rows):
    ax.barh(i, v, height=0.62, color=c, hatch=hatch, edgecolor="white")
    ax.text(v + 0.012, i, f"{v:.2f}", va="center", fontsize=13,
            fontweight="bold", color="#222222")
    ax.text(0.015, i, label, va="center", fontsize=12.5,
            color="white" if c != GREY else "#444444", fontweight="bold")
ax.set_xlim(0, 1.36)
ax.set_yticks([])
ax.set_xlabel("Risk-adjusted return on the same 4.5 unseen years (2022–2026)", fontsize=11.5)
style_bar_ax(ax)
ax.set_title("Re-tuning the trend speed every year would have LOST to leaving it alone",
             fontsize=13.5, fontweight="bold", loc="left", pad=10)
fig.tight_layout()
fig.savefig(OUT / "ws1_sum_retuning.png", bbox_inches="tight")

# ---------------- 3. The volatility-adjusted upgrade ----------------
# The headline proposed upgrade was a volatility-adjusted trend signal (divide
# each holding's distance-to-average by its own volatility, so one setting fits
# bonds and crypto alike). Applied across all books it cut the blend Sharpe
# 1.196 -> 0.888 and hurt every book individually (ws1_vol_variants.json).
fig, ax = plt.subplots(figsize=(9.6, 2.4), dpi=150)
rows = [("Volatility-adjusted, multi-speed signal (all books)", 0.89, RED),
        ("Best of the 17 sophisticated variants tried", 1.20, STEEL),
        ("Deployed — the simple 10-month signal", 1.20, NAVY)]
for i, (label, v, c) in enumerate(rows):
    ax.barh(i, v, height=0.6, color=c, edgecolor="white")
    ax.text(v + 0.012, i, f"{v:.2f}", va="center", fontsize=13,
            fontweight="bold", color="#222222")
    ax.text(0.015, i, label, va="center", fontsize=12.5, color="white",
            fontweight="bold")
ax.set_xlim(0, 1.36)
ax.set_yticks([])
ax.set_xlabel("Risk-adjusted return, full 7.6-year test", fontsize=11.5)
style_bar_ax(ax)
ax.set_title("The main proposed upgrade — a volatility-adjusted signal — did not beat the simple one",
             fontsize=13.0, fontweight="bold", loc="left", pad=10)
fig.tight_layout()
fig.savefig(OUT / "ws1_sum_smarter.png", bbox_inches="tight")

# ---------------- 4. The brake ----------------
fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.6, 2.9), dpi=150)
for ax, vals, title, better in (
        (a1, [("Without", 1.20, GREY), ("With brake", 1.29, NAVY)],
         "Risk-adjusted return", "higher is better"),
        (a2, [("Without", 23.8, GREY), ("With brake", 16.4, NAVY)],
         "Worst peak-to-trough loss (%)", "lower is better")):
    for i, (label, v, c) in enumerate(vals):
        ax.bar(i, v, width=0.55, color=c)
        ax.text(i, v * 1.02, (f"{v:.2f}" if v < 5 else f"−{v:.0f}%"),
                ha="center", fontsize=13.5, fontweight="bold")
    ax.set_xticks([0, 1], [v[0] for v in vals], fontsize=12)
    ax.set_yticks([])
    ax.set_title(f"{title}\n({better})", fontsize=12)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.set_ylim(0, max(v[1] for v in vals) * 1.22)
fig.suptitle("The safety brake helps on both dimensions — at EVERY one of the 25 trigger settings tested",
             fontsize=13.5, fontweight="bold", x=0.02, ha="left")
fig.tight_layout(rect=(0, 0, 1, 0.86))
fig.savefig(OUT / "ws1_sum_brake.png", bbox_inches="tight")

# ---------------- 5. Thematic trade-off ----------------
fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.6, 2.9), dpi=150)
for ax, vals, title, sub, hi_col in (
        (a1, [("Hurdle kept\n(deployed)", 0.69), ("Hurdle removed", 0.83)],
         "Return score on unseen data", "removal looks attractive…", GREEN),
        (a2, [("Hurdle kept\n(deployed)", 36), ("Hurdle removed", 48)],
         "Worst loss of the thematic book (%)", "…until you price the risk", RED)):
    for i, (label, v) in enumerate(vals):
        c = NAVY if i == 0 else hi_col
        ax.bar(i, v, width=0.55, color=c)
        ax.text(i, v * 1.02, (f"{v:.2f}" if v < 5 else f"−{v:.0f}%"),
                ha="center", fontsize=13.5, fontweight="bold")
    ax.set_xticks([0, 1], [v[0] for v in vals], fontsize=11.5)
    ax.set_yticks([])
    ax.set_title(f"{title}\n({sub})", fontsize=12)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.set_ylim(0, max(v[1] for v in vals) * 1.25)
fig.suptitle("The one tempting change was declined: better return, unacceptably deeper losses",
             fontsize=13.5, fontweight="bold", x=0.02, ha="left")
fig.tight_layout(rect=(0, 0, 1, 0.86))
fig.savefig(OUT / "ws1_sum_thematic.png", bbox_inches="tight")

print("wrote 5 finding charts")
