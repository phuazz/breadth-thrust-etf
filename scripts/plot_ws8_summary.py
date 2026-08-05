"""WS8 record charts — reproducible from committed JSON.

Three figures for reviews/2026-08-05_ws8_reit-dual-coverage.docx:
  fig1  the ablation result — blend Sharpe change from dropping each REIT
        line, against the keep bar and against the sample's own noise.
  fig2  look-through — the REIT pair sized against the two dual-coverage
        pairs WS2 already quantified and accepted.
  fig3  the retrospective overlap audit — every incumbent pair above the
        0.90 rule, classified.

Inputs: data/ws8_reit_overlap.json, data/overlap_audit.json.

House chart conventions (research-review report_format.md): white theme,
sans-serif, navy #1e3a8a primary, teal #0891b2 / red #dc2626 secondary,
green #dcfce7 "same within noise" fill, every displayed number rounded.

Run: python scripts/plot_ws8_summary.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WS8 = ROOT / "data" / "ws8_reit_overlap.json"
AUDIT = ROOT / "data" / "overlap_audit.json"
ASSETS = ROOT / "reviews" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

NAVY = "#1e3a8a"
TEAL = "#0891b2"
RED = "#dc2626"
GREY = "#9ca3af"
GREEN = "#dcfce7"
INK = "#1f2937"

# Sharpe standard error on this sample: 7.7y of weekly data. README states
# ~+/-0.4 and the WS8 deltas must be read against it, not against zero.
SHARPE_SE = 0.40

# WS2's already-accepted dual-coverage pairs, for the size comparison in
# fig2. Source: data/ws2_correlation.json -> blend_lookthrough_overlap.
WS2_DUALS = {
    "SPY (A+B)": (0.0398, 0.1036, 0.432),
    "QQQ (A+B)": (0.0679, 0.2408, 0.427),
    "IJR (A+B)": (0.0211, 0.1369, 0.025),
}

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


def fig1_ablation(d: dict) -> Path:
    variants = [("V1_B_drop_VNQ", "Drop VNQ from B\n(keep IUSP in A)"),
                ("V2_A_drop_IUSP", "Drop IUSP from A\n(keep VNQ in B)")]
    full = [d[k]["blend_delta"]["full"] for k, _ in variants]
    test = [d[k]["blend_delta"]["test"] for k, _ in variants]
    labels = [lab for _, lab in variants]

    fig, axes = plt.subplots(1, 2, figsize=(9.4, 4.3),
                             gridspec_kw={"width_ratios": [1.25, 1]})
    x = [0, 1]
    w = 0.32

    for ax in axes:
        b1 = ax.bar([i - w / 2 for i in x], full, width=w, color=NAVY,
                    label="full window", zorder=3)
        b2 = ax.bar([i + w / 2 for i in x], test, width=w, color=TEAL,
                    label="out-of-sample half", zorder=3)
        ax.axhline(0, color=INK, lw=1.0, zorder=2)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=9)
        ax.grid(axis="y", color="#eef1f5", lw=0.8, zorder=0)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)

    # Left panel — the decision, on a scale where the numbers are legible.
    ax = axes[0]
    for i, (fv, tv) in enumerate(zip(full, test)):
        for xoff, v in ((-w / 2, fv), (w / 2, tv)):
            ax.text(i + xoff, v + (0.0007 if v >= 0 else -0.0007),
                    f"{v:+.3f}", ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=9,
                    color=INK, zorder=4)
    ax.set_ylabel("Change in blend Sharpe vs deployed", fontsize=10)
    ax.set_ylim(-0.017, 0.010)
    ax.set_title("The decision: both drops lose out of sample",
                 fontsize=10, color=INK, loc="left", pad=8)
    ax.text(-0.48, 0.0086, "keep bar: the out-of-sample half must not fall "
                           "below this line", fontsize=8.3, color=RED,
            ha="left", va="top")
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")

    # Right panel — the same four numbers against the sample's own error.
    ax = axes[1]
    ax.axhspan(-SHARPE_SE, SHARPE_SE, color=GREEN, zorder=0)
    ax.set_ylim(-0.48, 0.48)
    ax.set_title("The same four numbers, against the margin of error",
                 fontsize=10, color=INK, loc="left", pad=8)
    ax.text(-0.42, 0.30,
            f"green band = +/-{SHARPE_SE:.2f}, one standard\n"
            "error on 7.7 years of weekly data.\n"
            "Every bar is a hairline inside it.",
            fontsize=8.3, color="#4b5563", ha="left", va="top")

    fig.suptitle(
        "Neither REIT line can be dropped — and the differences are far "
        "inside what this sample can resolve",
        fontsize=10.5, color=INK, x=0.008, ha="left", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out = ASSETS / "ws8_fig1_ablation.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig2_lookthrough(d: dict) -> Path:
    lt = d["reit_lookthrough"]
    rows = [("REITs (A+B)", lt["combined"]["mean_lookthrough_w"],
             lt["combined"]["max_lookthrough_w"],
             lt["combined"]["share_weeks_held_by_both_A_and_B"], True)]
    for name, (m, mx, both) in WS2_DUALS.items():
        rows.append((name, m, mx, both, False))
    rows.sort(key=lambda r: -r[1])

    fig, axes = plt.subplots(1, 2, figsize=(9.4, 3.6))
    names = [r[0] for r in rows]
    y = range(len(rows))
    colours = [RED if r[4] else NAVY for r in rows]

    ax = axes[0]
    ax.barh(list(y), [r[1] * 100 for r in rows], color=colours, height=0.6,
            zorder=3)
    for i, r in enumerate(rows):
        ax.text(r[1] * 100 + 0.12, i, f"{r[1]*100:.2f}%  (peak {r[2]*100:.1f}%)",
                va="center", fontsize=8.5, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 11.5)
    ax.set_xlabel("mean share of NAV reached twice", fontsize=9)
    ax.set_title("How much overlap each pair carries", fontsize=10,
                 color=INK, loc="left", pad=8)

    ax = axes[1]
    ax.barh(list(y), [r[3] * 100 for r in rows], color=colours, height=0.6,
            zorder=3)
    for i, r in enumerate(rows):
        ax.text(r[3] * 100 + 0.8, i, f"{r[3]*100:.0f}%", va="center",
                fontsize=8.5, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels([])
    ax.invert_yaxis()
    ax.set_xlim(0, 58)
    ax.set_xlabel("share of weeks both sleeves held it", fontsize=9)
    ax.set_title("How often both sleeves held it at once", fontsize=10,
                 color=INK, loc="left", pad=8)

    for ax in axes:
        ax.grid(axis="x", color="#eef1f5", lw=0.8, zorder=0)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(left=False)

    fig.suptitle(
        "The REIT pair (red) is a SMALLER double-count than the SPY and QQQ "
        "pairs WS2 already accepted",
        fontsize=10.5, color=INK, x=0.008, ha="left", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = ASSETS / "ws8_fig2_lookthrough.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig3_audit(a: dict) -> Path:
    """Every incumbent pair above the rule, classified by what it means."""
    # Pairs already examined by a prior filed study. Two sources:
    #  - named pair decisions (WS2 prune tests P1/P2, the deliberate TLT/IEF
    #    duration ladder, this study's IUSP/VNQ);
    #  - the WS2 US-beta cluster, whose membership is quoted verbatim from
    #    ws2_correlation.json -> blend_lookthrough_overlap.largest_cluster.
    #    Any pair drawn from inside it is covered by that analysis (mean
    #    46.8% / peak 83.5% of NAV), including the several rows that are the
    #    same S&P-vs-Nasdaq relationship reappearing under a second ticker.
    named = {frozenset({"EFA", "VGK"}), frozenset({"IUIS", "PAVE"}),
             frozenset({"IEF", "TLT"}), frozenset({"ICLN", "TAN"}),
             frozenset({"IDP6", "PAVE"}), frozenset({"IJR", "PAVE"}),
             frozenset({"IUSP", "VNQ"})}
    us_beta_engine = {"CSP1", "CNDX", "IDP6", "IUCD", "IUIS", "SPY", "QQQ",
                      "IJR", "PAVE", "IUSP", "VNQ"}
    pairs = a["pairs"]
    labels, vals, colours = [], [], []
    for p in pairs:
        labels.append(f"{p['a']} ~ {p['b']}")
        vals.append(p["corr"])
        pair = frozenset({p["a"], p["b"]})
        if p["proxy_identity"]:
            colours.append(GREY)
        elif pair in named or pair <= us_beta_engine:
            colours.append(NAVY)
        else:
            colours.append(RED)

    fig, ax = plt.subplots(figsize=(9.0, 5.4))
    y = range(len(vals))
    ax.barh(list(y), vals, color=colours, height=0.68, zorder=3)
    for i, v in enumerate(vals):
        ax.text(v + 0.002, i, f"{v:.3f}", va="center", fontsize=8.3, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8.3)
    ax.invert_yaxis()
    ax.set_xlim(0.88, 1.03)
    ax.axvline(0.90, color=RED, lw=1.1, ls="--", zorder=2)
    ax.text(0.9015, -0.75, "WS2 rule: 0.90", fontsize=8.5, color=RED,
            va="center")
    ax.set_xlabel("weekly return correlation", fontsize=9.5)
    ax.set_title(
        f"{len(vals)} incumbent pairs sit above a rule that only ever "
        "screened new candidates — two of them unstudied",
        fontsize=10.5, color=INK, pad=16, loc="left", wrap=True)
    ax.legend(handles=[
        Patch(color=GREY, label="priced through the same ticker "
                                "(structural, not measured)"),
        Patch(color=NAVY, label="covered by a filed study (WS2 prune tests, "
                                "US-beta cluster, this study)"),
        Patch(color=RED, label="measured and not previously studied"),
    ], frameon=False, fontsize=8.5, loc="lower right")
    ax.grid(axis="x", color="#eef1f5", lw=0.8, zorder=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    fig.tight_layout()
    out = ASSETS / "ws8_fig3_audit.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main() -> None:
    d = json.loads(WS8.read_text(encoding="utf-8"))
    a = json.loads(AUDIT.read_text(encoding="utf-8"))
    outs = [fig1_ablation(d), fig2_lookthrough(d), fig3_audit(a)]
    # Weekday assertion for the record's date (Python months are 1-indexed).
    stamp = date(2026, 8, 5)
    assert stamp.strftime("%A") == "Wednesday", "record date weekday mismatch"
    print(f"record date {stamp.isoformat()} is a {stamp.strftime('%A')}")
    for o in outs:
        print("wrote", o.relative_to(ROOT))


if __name__ == "__main__":
    main()
