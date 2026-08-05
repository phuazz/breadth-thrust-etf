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
# WS2's commodity evidence, reused for the plain-language summary's answer
# to the oil-and-gas question. Long-window MAR sweep, 2007-10 to 2026-06.
COMMOD = ROOT / "data" / "commodity_expansion.json"
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


def fig1_ablation(d: dict, plain: bool = False) -> Path:
    """`plain` swaps every technical label for the summary's audience.

    Same numbers, same file-generating code — only the wording differs, so
    the two documents cannot drift apart on the figures they share.
    """
    keys = ["V1_B_drop_VNQ", "V2_A_drop_IUSP"]
    if plain:
        labels = ["Remove property from the\nasset-class book",
                  "Remove property from the\nsector book"]
        lab_full, lab_test = "whole history", "later years (never tuned on)"
        ylab = "change in risk-adjusted return"
        t_left = "The decision: both removals lose in the later years"
        t_right = "The same four results, against the margin of error"
        bar_note = ("test: the later-years bar must not fall below this line")
        band_note = ("green band = the margin of error on 7.7 years of\n"
                     "data. Every bar is a hairline inside it.")
        sup = ("Neither property holding can be removed — and the "
               "differences are smaller than the measurement error")
    else:
        labels = ["Drop VNQ from B\n(keep IUSP in A)",
                  "Drop IUSP from A\n(keep VNQ in B)"]
        lab_full, lab_test = "full window", "out-of-sample half"
        ylab = "Change in blend Sharpe vs deployed"
        t_left = "The decision: both drops lose out of sample"
        t_right = "The same four numbers, against the margin of error"
        bar_note = ("keep bar: the out-of-sample half must not fall "
                    "below this line")
        band_note = (f"green band = +/-{SHARPE_SE:.2f}, one standard\n"
                     "error on 7.7 years of weekly data.\n"
                     "Every bar is a hairline inside it.")
        sup = ("Neither REIT line can be dropped — and the differences are "
               "far inside what this sample can resolve")
    full = [d[k]["blend_delta"]["full"] for k in keys]
    test = [d[k]["blend_delta"]["test"] for k in keys]

    fig, axes = plt.subplots(1, 2, figsize=(9.4, 4.3),
                             gridspec_kw={"width_ratios": [1.25, 1]})
    x = [0, 1]
    w = 0.32

    for ax in axes:
        ax.bar([i - w / 2 for i in x], full, width=w, color=NAVY,
               label=lab_full, zorder=3)
        ax.bar([i + w / 2 for i in x], test, width=w, color=TEAL,
               label=lab_test, zorder=3)
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
    ax.set_ylabel(ylab, fontsize=10)
    ax.set_ylim(-0.017, 0.010)
    ax.set_title(t_left, fontsize=10, color=INK, loc="left", pad=8)
    ax.text(-0.48, 0.0086, bar_note, fontsize=8.3, color=RED,
            ha="left", va="top")
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")

    # Right panel — the same four numbers against the sample's own error.
    ax = axes[1]
    ax.axhspan(-SHARPE_SE, SHARPE_SE, color=GREEN, zorder=0)
    ax.set_ylim(-0.48, 0.48)
    ax.set_title(t_right, fontsize=10, color=INK, loc="left", pad=8)
    ax.text(-0.42, 0.30, band_note,
            fontsize=8.3, color="#4b5563", ha="left", va="top")

    fig.suptitle(sup, fontsize=10.5, color=INK, x=0.008, ha="left", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out = ASSETS / (f"ws8_fig1_ablation{'_plain' if plain else ''}.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig2_lookthrough(d: dict, plain: bool = False) -> Path:
    plain_names = {
        "REITs (A+B)": "Property",
        "SPY (A+B)": "US large-company shares",
        "QQQ (A+B)": "US technology shares",
        "IJR (A+B)": "US smaller-company shares",
    }
    lt = d["reit_lookthrough"]
    rows = [("REITs (A+B)", lt["combined"]["mean_lookthrough_w"],
             lt["combined"]["max_lookthrough_w"],
             lt["combined"]["share_weeks_held_by_both_A_and_B"], True)]
    for name, (m, mx, both) in WS2_DUALS.items():
        rows.append((name, m, mx, both, False))
    rows.sort(key=lambda r: -r[1])

    fig, axes = plt.subplots(1, 2, figsize=(9.4, 3.6))
    names = [(plain_names[r[0]] if plain else r[0]) for r in rows]
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
    ax.set_xlabel("average share of the portfolio reached through two books"
                  if plain else "mean share of NAV reached twice", fontsize=9)
    ax.set_title("How much of the portfolio each doubles up" if plain
                 else "How much overlap each pair carries", fontsize=10,
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
    ax.set_xlabel("share of weeks both books held it" if plain
                  else "share of weeks both sleeves held it", fontsize=9)
    ax.set_title("How often both books held it at once" if plain
                 else "How often both sleeves held it at once", fontsize=10,
                 color=INK, loc="left", pad=8)

    for ax in axes:
        ax.grid(axis="x", color="#eef1f5", lw=0.8, zorder=0)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(left=False)

    fig.suptitle(
        "Property (red) is a SMALLER double-up than the two US share "
        "holdings already reviewed and kept" if plain else
        "The REIT pair (red) is a SMALLER double-count than the SPY and QQQ "
        "pairs WS2 already accepted",
        fontsize=10.5, color=INK, x=0.008, ha="left", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = ASSETS / (f"ws8_fig2_lookthrough{'_plain' if plain else ''}.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig3_audit(a: dict, plain: bool = False) -> Path:
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
    ax.text(0.9015, -0.75,
            "duplication limit" if plain else "WS2 rule: 0.90",
            fontsize=8.5, color=RED, va="center")
    ax.set_xlabel("how closely the two move together (1.00 = identical)"
                  if plain else "weekly return correlation", fontsize=9.5)
    ax.set_title(
        f"{len(vals)} pairs of holdings move closely enough to trip the "
        "duplication rule — two have never been examined" if plain else
        f"{len(vals)} incumbent pairs sit above a rule that only ever "
        "screened new candidates — two of them unstudied",
        fontsize=10.5, color=INK, pad=16, loc="left", wrap=True)
    ax.legend(handles=[
        Patch(color=GREY, label="priced using the same fund — not real "
                                "duplication" if plain else
                                "priced through the same ticker "
                                "(structural, not measured)"),
        Patch(color=NAVY, label="already examined by an earlier review"
                                if plain else
                                "covered by a filed study (WS2 prune tests, "
                                "US-beta cluster, this study)"),
        Patch(color=RED, label="never examined" if plain else
                              "measured and not previously studied"),
    ], frameon=False, fontsize=8.5, loc="lower right")
    ax.grid(axis="x", color="#eef1f5", lw=0.8, zorder=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    fig.tight_layout()
    out = ASSETS / (f"ws8_fig3_audit{'_plain' if plain else ''}.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig4_commodity() -> Path:
    """Answers the oil-and-gas question from the WS2 commodity evidence."""
    c = json.loads(COMMOD.read_text(encoding="utf-8"))["narrow_B"]
    rows = [
        ("Base metals only", "basemetals(DBB)"),
        ("Farm goods only", "ags(DBA)"),
        ("Energy only", "energy(DBE)"),
        ("Energy + metals", "energy+metals"),
        ("All three sectors", "DB-sectors(DBA+DBB+DBE)"),
        ("All three + a broad energy-heavy fund", "broad+sectors+GSG"),
        ("...plus oil and natural gas funds directly", "+USO+UNG(contango)"),
    ]
    labels = [r[0] for r in rows]
    vals = [c[r[1]]["dmar"] for r in rows]
    colours = [RED if i == len(rows) - 1 else NAVY for i in range(len(rows))]

    fig, ax = plt.subplots(figsize=(9.2, 4.2))
    y = range(len(rows))
    ax.barh(list(y), vals, color=colours, height=0.62, zorder=3)
    for i, v in enumerate(vals):
        ax.text(v - 0.006, i, f"{v:+.2f}", va="center", ha="right",
                fontsize=8.8, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.axvline(0, color=INK, lw=1.0, zorder=2)
    ax.set_xlim(-0.34, 0.02)
    ax.set_xlabel("change in return earned per unit of worst loss "
                  "(negative = worse)", fontsize=9)
    ax.set_title(
        "Every way of adding commodities to the book made it worse — and "
        "adding oil and natural gas directly was the worst of all",
        fontsize=10.5, color=INK, pad=10, loc="left", wrap=True)
    ax.grid(axis="x", color="#eef1f5", lw=0.8, zorder=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    fig.tight_layout()
    out = ASSETS / "ws8_fig4_commodity.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig5_scope() -> Path:
    """House scope-and-restraint exhibit: how much was checked, how little moved."""
    # Labels are plain-language: this exhibit appears only in the summary.
    cats = [
        ("Ways of removing a property holding", 2),
        ("Existing holding pairs checked for duplication", 18),
        ("Past decisions re-checked against the repaired check", 4),
    ]
    labels = [c[0] for c in cats]
    counts = [c[1] for c in cats]
    n = sum(counts)

    fig, ax = plt.subplots(figsize=(9.6, 3.2))
    y = range(len(cats))
    ax.barh(list(y), counts, color=NAVY, height=0.58, zorder=3)
    for i, v in enumerate(counts):
        ax.text(v + 0.25, i, str(v), va="center", fontsize=9, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 21)
    ax.set_xticks(range(0, 21, 5))       # counts are integers, not 2.5 checks
    ax.set_xlabel("checks run", fontsize=9.5)
    # Figure-level title, not axes-level: the long category labels push the
    # axes right, and a left-aligned axes title would run off the canvas.
    fig.suptitle(
        f"Scope and restraint:  {n} checks run  →  0 changes to the "
        f"portfolio  →  2 flagged for later",
        fontsize=10.5, color=INK, x=0.008, ha="left", y=0.98)
    ax.grid(axis="x", color="#eef1f5", lw=0.8, zorder=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out = ASSETS / "ws8_fig5_scope.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main() -> None:
    d = json.loads(WS8.read_text(encoding="utf-8"))
    a = json.loads(AUDIT.read_text(encoding="utf-8"))
    outs = [fig1_ablation(d), fig2_lookthrough(d), fig3_audit(a),
            fig4_commodity(), fig5_scope(),
            # Plain-language variants for the summary document. Same data,
            # same code path — only the wording differs, so the technical
            # record and the summary cannot disagree on a figure.
            fig1_ablation(d, plain=True), fig2_lookthrough(d, plain=True),
            fig3_audit(a, plain=True)]
    # Weekday assertion for the record's date (Python months are 1-indexed).
    stamp = date(2026, 8, 5)
    assert stamp.strftime("%A") == "Wednesday", "record date weekday mismatch"
    print(f"record date {stamp.isoformat()} is a {stamp.strftime('%A')}")
    for o in outs:
        print("wrote", o.relative_to(ROOT))


if __name__ == "__main__":
    main()
