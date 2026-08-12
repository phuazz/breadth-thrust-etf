"""WS14 record chart — reproducible from committed JSON.

One figure for reviews/2026-08-12_ws14_sleeve-a-lse-pricing.docx: how closely
each US trading proxy tracks the London UCITS line it stands in for, weekly
and daily, with the headline result stated on the chart.

Why this figure and not an equity-curve overlay: the two curves are visually
identical (Sharpe +0.8139 vs +0.8107), so a curve chart would carry no
information and would invite the reader to hunt for a difference that is not
there. The interesting quantity is the QUALITY of the proxy substitution,
which varies name by name and is the thing a reader cannot otherwise see.

Daily and weekly are both plotted because the gap between them IS the finding
about measurement: the LSE closes at 16:30 London, 11:30 New York, so daily
returns cover different windows and understate the tracking relationship for
every pair. Showing only the weekly figures would hide why the daily ones look
alarming.

Inputs: data_local/ws14_sleeve_a_lse.json (run scripts/run_ws14_sleeve_a_lse.py).

House chart conventions (research-review report_format.md): white theme,
sans-serif, navy #1e3a8a primary, teal #0891b2 / red #dc2626 secondary,
every displayed number rounded.

Run: python scripts/plot_ws14_summary.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data_local" / "ws14_sleeve_a_lse.json"
ASSETS = ROOT / "reviews" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

NAVY = "#1e3a8a"
TEAL = "#0891b2"
RED = "#dc2626"
INK = "#111827"
FAINT = "#6b7280"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "figure.facecolor": "white", "axes.facecolor": "white",
    "axes.edgecolor": "#d1d5db", "text.color": INK,
    "xtick.color": INK, "ytick.color": INK, "axes.labelcolor": INK,
    "axes.grid": True, "grid.color": "#e5e7eb", "grid.linewidth": 0.6,
})


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"missing {SRC} — run scripts/run_ws14_sleeve_a_lse.py")
    D = json.loads(SRC.read_text(encoding="utf-8"))
    keys = D["universe_priced"]
    cw = D["proxy_vs_london_corr_weekly"]
    cd = D["proxy_vs_london_corr_daily"]
    ccy = D["currencies"]
    pm = D["proxy_map"]
    floor = D["corr_floor_weekly"]

    order = sorted(keys, key=lambda k: cw[k])
    y = list(range(len(order)))
    fig, ax = plt.subplots(figsize=(9.2, 5.0))

    ax.barh([i + 0.19 for i in y], [cw[k] for k in order], height=0.36,
            color=NAVY, label="weekly returns (the guard's test)")
    ax.barh([i - 0.19 for i in y], [cd[k] for k in order], height=0.36,
            color=TEAL, label="daily returns (depressed by the close offset)")
    ax.axvline(floor, color=RED, lw=1.4, ls="--")
    # Below the lowest bar, not above the highest: at the top it crowds the
    # first row's value labels.
    ax.text(floor + 0.008, -0.72, f"guard floor {floor:.2f}",
            color=RED, fontsize=8.5, va="center")

    for i, k in enumerate(order):
        ax.text(cw[k] + 0.006, i + 0.19, f"{cw[k]:.3f}", va="center",
                fontsize=8, color=NAVY)
        ax.text(cd[k] + 0.006, i - 0.19, f"{cd[k]:.3f}", va="center",
                fontsize=8, color=TEAL)

    labels = [f"{k} / {pm[k]}" + ("  (GBp)" if ccy[k] in ("GBp", "GBX") else "")
              for k in order]
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlim(0, 1.14)
    ax.set_xlabel("correlation of returns, London UCITS line vs its US trading proxy")
    ax.grid(axis="y", visible=False)

    lg = D["legs"]
    dl = D["delta_lse_minus_proxy"]
    # Three short lines rather than two long ones: the single-line variant ran
    # past the figure edge and clipped "CAGR)".
    ax.set_title(
        "How good is the proxy substitution, name by name?\n"
        f"Sleeve A, 13 names — US proxies {lg['proxy_us']['sharpe']:+.4f} Sharpe, "
        f"London UCITS {lg['lse_ucits']['sharpe']:+.4f}\n"
        f"difference {dl['sharpe']:+.4f} Sharpe, {dl['cagr']*100:+.2f}pp CAGR",
        fontsize=10.5, color=INK, pad=10)
    handles, lab = ax.get_legend_handles_labels()
    handles.append(Patch(facecolor="white", edgecolor=RED, ls="--",
                         label="a wrong fund would sit near zero"))
    lab.append("a wrong fund would sit near zero")
    # Below the axes: inside the plot the legend sat on the two lowest bars and
    # hid their value labels, which are the two the reader most wants to read.
    ax.legend(handles, lab, frameon=False, fontsize=8.5, ncol=3,
              loc="upper center", bbox_to_anchor=(0.5, -0.11))
    fig.tight_layout()
    out = ASSETS / "ws14_fig1_proxy_tracking.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"wrote {out.relative_to(ROOT)}  ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
