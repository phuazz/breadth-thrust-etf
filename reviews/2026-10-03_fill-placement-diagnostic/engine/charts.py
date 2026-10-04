#!/usr/bin/env python3
"""Charts for the fill-placement diagnostic, rendered from results/results.json
and results/coverage.json. Kept apart from fill_timing.py so that a change to a
title never changes the engine hash the registered run is bound to; written
and frozen before the run, so every title is a template filled from
results.json, not text written after reading a result.

Archetype: argument. Figure 1 carries the verdict (H-D2: the mean post leg of
the confirmatory fills against the blocked placebo, with the 10 bp floor);
figures 2 and 3 are the supporting exhibits (u of every confirmatory fill by
sleeve; the pre and post legs by side). Every figure prints its as-at line and
its source. The script verifies the rendered figures (marks drawn against
fills expected, footers present, no text outside the frame) and prints only
those structural counts."""
import json
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.text  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
CHART_DIR = HERE / "charts"
NAVY, RED, GREY = "#1e3a8a", "#dc2626", "#6b7280"
SLEEVE_ORDER = ["A", "B", "C", "D", "TILT", "GATE"]
SLEEVE_NAME = {"A": "A, US sectors", "B": "B, asset classes", "C": "C, thematics", "D": "D, Europe sectors",
               "TILT": "TILT leg, EEM", "GATE": "GATE leg, SHY"}


def load(name):
    with open(RESULTS_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


def fmt_p(p):
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def wrap_title(t, width=95):
    return "\n".join(textwrap.wrap(t, width))


def footer(fig, *lines, width=168):
    """Print the footer lines, each wrapped to `width` characters, stacked up
    from the bottom edge. Returns (the printed strings, the share of the
    figure height they take) so the layout leaves room and the check can find
    every printed line."""
    wrapped = [w for line in lines for w in textwrap.wrap(line, width)]
    step = 0.11 / fig.get_size_inches()[1]          # about 8 pt per line
    for i, w in enumerate(reversed(wrapped)):
        fig.text(0.01, 0.006 + i * step, w, fontsize=6.3, color=GREY)
    return wrapped, 0.012 + len(wrapped) * step


def verify(fig, footers, scatters=None, expected_marks=None):
    """The rendered artefact: count the marks, find the footer lines that
    printed, and measure every drawn text against the figure frame. Drawn
    texts are the figure's own, each axes' texts, title, axis labels and
    legend entries, and the tick labels of ticks inside the view (matplotlib
    keeps undrawn tick labels in its tick pool, which a scan of every Text
    object would count); the figure's tight bounding box must also sit inside
    the frame."""
    fig.canvas.draw()
    rdr = fig.canvas.get_renderer()
    fb = fig.bbox
    drawn = list(fig.texts)
    for ax in fig.axes:
        drawn += list(ax.texts) + [ax.title, ax.xaxis.label, ax.yaxis.label]
        leg = ax.get_legend()
        if leg is not None:
            drawn += list(leg.get_texts())
        for axis in (ax.xaxis, ax.yaxis):
            lo, hi = sorted(axis.get_view_interval())
            for tick in axis.get_major_ticks():
                if lo - 1e-12 <= tick.get_loc() <= hi + 1e-12:
                    drawn += [tick.label1, tick.label2]
    outside, printed, where = 0, set(), []
    for t in drawn:
        s = t.get_text()
        if not s or not t.get_visible():
            continue
        bb = t.get_window_extent(rdr)
        if bb.x0 < fb.x0 - 0.5 or bb.x1 > fb.x1 + 0.5 or bb.y0 < fb.y0 - 0.5 or bb.y1 > fb.y1 + 0.5:
            outside += 1
            where.append({"chars": len(s), "x0": round(bb.x0), "x1": round(bb.x1), "y0": round(bb.y0), "y1": round(bb.y1)})
        printed.add(s)
    tb = fig.get_tightbbox(rdr)          # inches
    w, h = fig.get_size_inches()
    tight_inside = bool(tb.x0 >= -0.01 and tb.y0 >= -0.01 and tb.x1 <= w + 0.01 and tb.y1 <= h + 0.01)
    marks = sum(len(c.get_offsets()) for c in scatters) if scatters else None
    return {"texts_outside_frame": outside, "outside_boxes": where, "tight_bbox_inside_frame": tight_inside,
            "frame": [round(fb.x1), round(fb.y1)],
            "footers_printed": sum(1 for f in footers if f in printed),
            "footers_expected": len(footers), "marks": marks, "marks_expected": expected_marks}


def main():
    res, cov = load("results.json"), load("coverage.json")
    CHART_DIR.mkdir(exist_ok=True)
    F = [f for f in res["fills"] if f["confirmatory"]]
    n_conf = len(F)
    first, last = min(f["session_date"] for f in F), max(f["session_date"] for f in F)
    vint = (res["provenance"].get("adapter_provenance") or {}).get("vintage") or {}
    vcommit = (vint.get("vintage_commit") or "")[:8]
    asof = (f"Confirmatory fills {first} to {last} (from the blend's inception, 2018-10-31, to the last fill with a full 60-session forward pool); "
            f"bars to {res['provenance']['history_last_session']}; n = {n_conf} fills in {cov['counts']['blocks']} rebalance-date clusters; "
            f"weights as published at {vcommit}.")
    src = ("Source: the engines' published weekly weight vectors and overlay events (data/*.json at the 2026-10-03 refresh); "
           "Yahoo daily bars via yfinance, unadjusted, windows rebased for dividends.")
    adp = res["provenance"].get("adapter_provenance") or {}
    par = adp.get("parity") or {}
    btc = (adp.get("excluded_btc_usd") or {}).get("fills", 0)
    n_sz = sum(b["fills"] for b in par.get("basis_mismatch_excluded", []))
    win = [x for x in adp.get("parity_excluded", []) if not str(x.get("reason", "")).startswith("basis mismatch")]
    win_conf = sum(1 for x in win if "2018-10-31" <= x["date"] <= last)
    g2 = len(adp.get("guard2_excluded", []))
    excl = (f"Modelled fills, not executed. Excluded before the engine: BTC-USD {btc} fills (seven-day calendar); 159801.SZ {n_sz} fills "
            f"(basis mismatch) and {g2} more on Chinese holidays (guard 2); {len(win)} fills failing the parity window test, {win_conf} of them "
            f"in the confirmatory window; the engine dropped {cov['counts']['dropped_window']} for incomplete windows. Disclosure cells, "
            f"not shown: {res['n_pre_blend']} pre-blend fills and {res['n_post_cutoff']} post-cutoff fills.")
    report = {}

    # 1. the verdict chart: the blocked null of the mean post leg, the actual and the floor
    H = res["H_D2"]
    d2 = res["verdict_inputs"]["floor_delta_pct"]
    verdict = res["verdict"]
    pw = fmt_p(H["p_one_sided_worse"])
    if verdict == "GIVE-BACK-AT-SIZE":
        title = f"The lines give back at least the 10 bp floor after the fill: mean post leg {H['actual']:+.3f}% against placebo {H['null_mean']:+.3f}% ({pw})"
    elif verdict == "GIVE-BACK-BELOW-SIZE":
        title = f"A give-back after the fill, below the 10 bp floor: mean post leg {H['actual']:+.3f}% against placebo {H['null_mean']:+.3f}% ({pw})"
    elif verdict == "NO-GIVE-BACK":
        title = f"No give-back after the fill beyond the placebo: mean post leg {H['actual']:+.3f}% against {H['null_mean']:+.3f}% ({pw})"
    else:
        title = f"Mean post leg {H['actual']:+.3f}% against placebo {H['null_mean']:+.3f}% ({pw}): {verdict}"
    draws = np.array(res["distribution"]["null_mean_post_pct_draws_confirmatory"])
    fig, ax = plt.subplots(figsize=(8.4, 4.1), dpi=150)
    ax.hist(draws, bins=60, color=GREY, alpha=0.85,
            label=f"{len(draws):,} placebo sets: mean post leg of the same {n_conf} fills at the close of a random session 4 to 60 bars after them")
    top = ax.get_ylim()[1]
    ax.axvline(H["actual"], color=NAVY, lw=2.2)
    right = H["actual"] > float(np.percentile(draws, 50))
    ax.text(H["actual"], top * 0.93, (f"actual {H['actual']:+.3f}%  " if right else f"  actual {H['actual']:+.3f}%"),
            color=NAVY, fontsize=8.5, va="top", ha="right" if right else "left")
    ax.axvline(H["null_mean"] + d2, color=RED, ls="--", lw=1.4)
    ax.text(H["null_mean"] + d2, top * 0.74, f"  floor {H['null_mean'] + d2:+.3f}%\n  (placebo + 10 bp)", color=RED, fontsize=8, va="top")
    ax.set_xlabel("mean post leg across the confirmatory fills, % of price\n"
                  "(positive: the price moved against the fill over the next three sessions)", fontsize=8)
    ax.set_ylabel("placebo sets")
    ax.set_title(wrap_title(title), fontsize=9.5)
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)
    pwr = cov["power"]["H_D2"]
    l1 = (asof + " Null: the close of a random session 4 to 60 bars after the fill, one offset per rebalance date per set, seed 20261003; "
          f"one-sided p, adverse direction. Floor 10 bp of price (the blend's modelled round trip); MDE {pwr['mde_at_target'] * 1e4:.1f} bp; "
          f"p-test power at the floor {pwr['power_at_delta']:.2f}; the point estimate clears the floor with probability 0.50 at a true "
          f"10 bp and 0.80 at {pwr['point_estimate_branch']['true_effect_for_0_80'] * 1e4:.1f} bp.")
    printed, bottom = footer(fig, l1, src, excl)
    fig.tight_layout(rect=(0, bottom, 0.975, 0.97))
    report["fig1_verdict_post_leg.png"] = verify(fig, printed)
    fig.savefig(CHART_DIR / "fig1_verdict_post_leg.png")
    plt.close(fig)

    # 2. every confirmatory fill: u by sleeve, the sleeve's placebo mean beside it
    rng_j = np.random.default_rng(7)
    sleeves = [s for s in SLEEVE_ORDER if any(f["theme"] == s for f in F)]
    fig, ax = plt.subplots(figsize=(8.4, 0.75 * len(sleeves) + 1.6), dpi=150)
    scatters = []
    for row, sv in enumerate(reversed(sleeves)):
        us = np.array([f["u"] for f in F if f["theme"] == sv])
        cellu = res["by_sleeve"][sv]["u"]
        q25, q50, q75 = np.percentile(us, [25, 50, 75])
        ax.barh(row, q75 - q25, left=q25, height=0.66, color="#e5e7eb", zorder=1)
        ax.plot([q50, q50], [row - 0.33, row + 0.33], color=GREY, lw=1.6, zorder=2)
        jit = rng_j.uniform(-0.26, 0.26, len(us))
        sc = ax.scatter(us, row + jit, s=3, color=NAVY, alpha=0.22, linewidths=0, zorder=3, rasterized=True)
        scatters.append(sc)
        ax.plot([cellu["null_mean"]] * 2, [row - 0.36, row + 0.36], color=RED, lw=1.6, zorder=4)
        ax.text(1.02, row, f"{SLEEVE_NAME[sv]}\nn = {len(us)}, mean u {cellu['actual']:.3f}\nplacebo {cellu['null_mean']:.3f}",
                fontsize=7, va="center", ha="left", color=NAVY)
    ax.set_xlim(-0.01, 1.01)
    ax.set_ylim(-0.6, len(sleeves) - 0.4)
    ax.set_yticks([])
    ax.set_xlabel("adverse rank u of each fill: the session close within its seven-session low-to-high range\n"
                  "(0 = the best price for the side, 1 = the worst)", fontsize=8)
    H1 = res["H_D1"]
    ax.set_title(wrap_title(f"Where the closing fills sit in their week (descriptive): mean u {H1['actual']:.3f} against placebo {H1['null_mean']:.3f} across {n_conf} fills", 80), fontsize=9.5)
    ax.spines[["top", "right", "left"]].set_visible(False)
    l1 = asof + " One mark per fill; grey band the interquartile range, grey line the median, red line the sleeve's placebo mean."
    printed, bottom = footer(fig, l1, src, excl)
    fig.tight_layout(rect=(0, bottom, 0.78, 0.97))
    report["fig2_u_by_sleeve.png"] = verify(fig, printed, scatters, n_conf)
    fig.savefig(CHART_DIR / "fig2_u_by_sleeve.png")
    plt.close(fig)

    # 3. the pre and post legs by side, actual against placebo
    bs = res["by_side"]
    keys = [("buys", "pre_pct"), ("buys", "post_pct"), ("sells", "pre_pct"), ("sells", "post_pct")]
    labels = ["buys: pre leg\n(bought after a rise)", "buys: post leg\n(fell after the buy)",
              "sells: pre leg\n(sold after a fall)", "sells: post leg\n(rose after the sale)"]
    a_vals = [bs[s][k]["actual"] for s, k in keys]
    n_vals = [bs[s][k]["null_mean"] for s, k in keys]
    ci = [bs[s][k]["effect_ci95_block_bootstrap"] for s, k in keys]
    xs = np.arange(4)
    fig, ax = plt.subplots(figsize=(8.4, 4.4), dpi=150)
    ax.bar(xs - 0.18, a_vals, width=0.36, color=NAVY, label="actual fills (equal-weighted mean)")
    ax.bar(xs + 0.18, n_vals, width=0.36, color=GREY, alpha=0.85, label="placebo, forward null (mean of the set means)")
    for i, c in enumerate(ci):
        ax.plot([xs[i] - 0.18] * 2, [n_vals[i] + c[0], n_vals[i] + c[1]], color="black", lw=1)
    ax.axhline(0, color="black", lw=0.6)
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("adverse move, % of price (positive = against the fill)", fontsize=8.5)
    pre = res["pre_leg"]
    ax.set_title(wrap_title(f"Pre leg {pre['actual']:+.2f}% against placebo {pre['null_mean']:+.2f}% ({fmt_p(pre['p_one_sided_worse'])}); "
                            f"post leg {H['actual']:+.3f}% against {H['null_mean']:+.3f}% ({pw})"), fontsize=9.5)
    ax.legend(fontsize=7.5, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    l1 = asof + " Pre: t-3 close to fill; post: fill to t+3 close; whiskers: 95% cluster-bootstrap interval of the actual mean."
    printed, bottom = footer(fig, l1, src, excl)
    fig.tight_layout(rect=(0, bottom, 0.975, 0.97))
    report["fig3_pre_post_by_side.png"] = verify(fig, printed)
    fig.savefig(CHART_DIR / "fig3_pre_post_by_side.png")
    plt.close(fig)

    with open(CHART_DIR / "render_check.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    ok = all(r["texts_outside_frame"] == 0 and r["tight_bbox_inside_frame"] and r["footers_printed"] == r["footers_expected"]
             and (r["marks"] is None or r["marks"] == r["marks_expected"]) for r in report.values())
    print("charts written to", CHART_DIR, "| render check", "PASSED" if ok else "FAILED", json.dumps(report))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
