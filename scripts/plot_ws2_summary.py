"""Allocator-facing charts for the WS2 plain-language summary (reproducible).

Reads  data/ws2_eem_coherence.json, data/ws2_country_sleeve.json,
       data/ws2_commodity_fixed.json, data/ws2_correlation.json,
       data/ws2_baselines_meta.json, data/ws2_baseline_weights_*.parquet,
       data/ws2_prices_cache.parquet
Writes data/ws2_sum_eem_lookthrough.png   (summary finding 1)
       data/ws2_sum_eem_ablation.png      (summary finding 2)
       data/ws2_sum_country_regimes.png   (summary finding 3)
       data/ws2_sum_commodity.png         (summary finding 4)
       data/ws2_sum_scope.png             (appendix A1)
       data/ws2_sum_usbeta.png            (appendix A3)

Every plotted number is read from the committed WS2 artefacts or recomputed
from the committed caches with the same arithmetic as the WS2 experiment
scripts; where a series is recomputed (EEM look-through, US-beta
concentration) the script ASSERTS its summary statistics against the values
stored in the corresponding JSON, so a silent divergence between chart and
record fails the build instead of shipping.

Run: python scripts/plot_ws2_summary.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

NAVY, RED, TEAL, GREEN_FILL = "#1e3a8a", "#dc2626", "#0891b2", "#dcfce7"
GREY = "#6b7280"

plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 11,
    "axes.grid": True, "grid.color": "#e5e7eb", "grid.linewidth": 0.7,
    "axes.edgecolor": "#9ca3af", "figure.facecolor": "white",
    "axes.facecolor": "white",
})

J = lambda f: json.loads((DATA / f).read_text(encoding="utf-8"))  # noqa: E731

TILT_FAST, TILT_SLOW, TILT_W = 50, 200, 0.10   # run_risk_overlay.py:123-125


def _lookthrough_series():
    """Combined EM weight under the OLD architecture (EEM inside book B plus
    the overlay) and the NEW one (overlay only) — same arithmetic as
    run_ws2_eem_coherence.py, asserted against its stored statistics."""
    meta = J("ws2_baselines_meta.json")
    start = pd.Timestamp(meta["common_start"])
    end = pd.Timestamp(meta["common_end"])
    wB = pd.read_parquet(DATA / "ws2_baseline_weights_B.parquet")
    ws2 = pd.read_parquet(DATA / "ws2_prices_cache.parquet")
    idx = wB.index[(wB.index >= start) & (wB.index <= end)]
    ratio = (ws2["EEM"] / ws2["SPY"]).dropna()
    fast = ratio.rolling(TILT_FAST, min_periods=TILT_FAST).mean()
    slow = ratio.rolling(TILT_SLOW, min_periods=TILT_SLOW).mean()
    sig = ((fast > slow).astype(float)
           .reindex(idx, method="ffill").fillna(0).shift(1).fillna(0))
    b_eem = wB["EEM"].reindex(idx).fillna(0.0)
    old = b_eem * (0.35 - TILT_W * sig) + TILT_W * sig
    new = TILT_W * sig
    dc = J("ws2_eem_coherence.json")["double_count_quantification"]
    assert abs(float(old.max()) - dc["max_lookthrough_eem_w"]) < 0.005, \
        f"look-through peak drifted: {old.max():.4f} vs {dc['max_lookthrough_eem_w']}"
    assert abs(float(old.mean()) - dc["mean_lookthrough_eem_w"]) < 0.005, \
        f"look-through mean drifted: {old.mean():.4f} vs {dc['mean_lookthrough_eem_w']}"
    return old, new


def eem_lookthrough():
    old, new = _lookthrough_series()
    fig, ax = plt.subplots(figsize=(9.2, 4.3), dpi=150)
    ax.axhline(0.10, color=GREY, lw=1.1, ls=(0, (5, 3)))
    ax.text(pd.Timestamp("2023-06-01"), 0.108,
            "intended maximum: the 10% overlay", fontsize=9.5, color=GREY,
            bbox=dict(facecolor="white", edgecolor="none", pad=1.2))
    ax.fill_between(old.index, 0.10, old.where(old > 0.10, 0.10),
                    color="#fecaca", zorder=1)
    ax.plot(old.index, old.values, color=NAVY, lw=1.8,
            label="old set-up: fund inside the global book + overlay on top")
    ax.plot(new.index, new.values, color=TEAL, lw=1.6, ls="--",
            label="new set-up (from 2 Jul 2026): overlay only, capped at 10%")
    peak_dt = old.idxmax()
    ax.annotate(f"peak {old.max()*100:.0f}% of the whole portfolio —\n"
                "half as much again as intended",
                (peak_dt, float(old.max())),
                xytext=(pd.Timestamp("2021-08-01"), 0.155),
                textcoords="data", fontsize=9.6, color="#991b1b",
                arrowprops=dict(arrowstyle="->", color="#991b1b", lw=0.9))
    ax.set_ylim(-0.005, 0.175)
    ax.set_yticks([0, 0.05, 0.10, 0.15], ["0%", "5%", "10%", "15%"])
    ax.set_ylabel("Share of the portfolio in emerging markets", fontsize=10.5)
    ax.set_title("Emerging markets was held two ways at once — the true position "
                 "ran half as large again as intended", fontsize=11.8,
                 fontweight="bold", pad=12)
    ax.legend(fontsize=9.3, loc="lower center", bbox_to_anchor=(0.5, 0.42),
              framealpha=0.95)
    fig.tight_layout()
    out = DATA / "ws2_sum_eem_lookthrough.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


def eem_ablation():
    eem = J("ws2_eem_coherence.json")
    cells = [
        ("Old set-up\n(in the book + overlay)", "V0_status_quo_EEM_in_B_plus_tilt"),
        ("Overlay only\n(CHOSEN — now live)", "V1_overlay_only_B_without_EEM"),
        ("In the book only\n(no overlay)", "V2_B_member_only_no_tilt"),
        ("No EM position\nat all", "V3_neither"),
    ]
    vals = [eem[k]["full"]["sharpe"] for _, k in cells]
    fig, ax = plt.subplots(figsize=(9.2, 4.0), dpi=150)
    ax.axhspan(min(vals) - 0.02, max(vals) + 0.02, color=GREEN_FILL, zorder=0)
    ax.text(1.5, max(vals) + 0.028,
            "all four arrangements are the same within the margin of error\n"
            f"(spread {max(vals)-min(vals):.3f}; noise on 7.6 years is about ±0.4)",
            ha="center", fontsize=9.8, color="#166534")
    for i, ((label, _), v) in enumerate(zip(cells, vals)):
        chosen = i == 1
        ax.plot([i], [v], "o", ms=13 if chosen else 10,
                color=NAVY if chosen else GREY, zorder=3)
        ax.annotate(f"{v:+.2f}", (i, v), textcoords="offset points",
                    xytext=(0, 11), ha="center", fontsize=10.5,
                    fontweight="bold" if chosen else "normal",
                    color=NAVY if chosen else "#374151")
    ax.set_xticks(range(len(cells)), [c[0] for c in cells], fontsize=9.6)
    ax.set_xlim(-0.5, 3.5)
    ax.set_ylim(min(vals) - 0.12, max(vals) + 0.12)
    ax.set_ylabel("Whole-portfolio risk-adjusted return", fontsize=10.5)
    ax.set_title("All four ways of arranging the EM position perform identically — "
                 "the fix is housekeeping, not a return bet",
                 fontsize=11.8, fontweight="bold", pad=12)
    fig.tight_layout()
    out = DATA / "ws2_sum_eem_ablation.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


SUBP = [
    ("2019_pre_covid", "2019\npre-Covid"),
    ("2020_covid_recovery", "2020 Covid\nrecovery"),
    ("2021_rally", "2021\nrally"),
    ("2022_inflation_shock", "2022 inflation\nshock"),
    ("2023_ai_rally", "2023 AI\nrally"),
    ("2024_25_recent", "2024-25\nrecent"),
]


def country_regimes():
    c = J("ws2_country_sleeve.json")
    sl = c["variants"]["U10_K3"]
    bm = c["benchmarks"]["EEM_EFA_5050"]
    labels = [lab for _, lab in SUBP] + ["FIRST HALF\n(2018-2022)", "SECOND HALF\n(2022-2026)"]
    s_vals = [sl["sub_period_sharpe"][k] for k, _ in SUBP] + \
             [sl["train"]["sharpe"], sl["test"]["sharpe"]]
    b_vals = [bm["sub_period_sharpe"][k] for k, _ in SUBP] + \
             [bm["train"]["sharpe"], bm["test"]["sharpe"]]
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(10.2, 4.4), dpi=150)
    ax.axhline(0, color="#9ca3af", lw=0.8)
    ax.bar(x - 0.19, s_vals, 0.36, color=NAVY, label="country-fund rotation")
    ax.bar(x + 0.19, b_vals, 0.36, color="#9ca3af",
           label="simple benchmark: half EM fund, half developed-markets fund")
    for i, (s, b) in enumerate(zip(s_vals, b_vals)):
        if s < b:
            ax.annotate("loses", (i - 0.19, min(s, 0)), ha="center",
                        textcoords="offset points", xytext=(0, -14),
                        fontsize=8.6, color="#991b1b", fontweight="bold")
    ax.axvline(5.5, color="#d1d5db", lw=1.0)
    ax.set_xticks(x, labels, fontsize=8.6)
    ax.set_ylabel("Risk-adjusted return", fontsize=10.5)
    ax.set_title("Country funds beat a simple benchmark only when EM is already "
                 "winning — and lost the whole first half of the sample",
                 fontsize=11.6, fontweight="bold", pad=12)
    ax.legend(fontsize=9.3, loc="upper left", framealpha=0.95)
    fig.tight_layout()
    out = DATA / "ws2_sum_country_regimes.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


def commodity():
    cm = J("ws2_commodity_fixed.json")
    groups = [
        ("Global book\n+ 3 commodity funds",
         cm["B_plus_DBA_DBB_DBE"], cm["sleeve_baselines"]["B"]),
        ("Thematic book\n+ 4 commodity funds",
         cm["C_plus_DBC_DBA_DBB_DBE"], cm["sleeve_baselines"]["C"]),
        ("Whole portfolio\n(both books widened)",
         cm["blend_both_widened"], cm["blend_baseline"]),
    ]
    train_d = [g[1]["train"]["sharpe"] - g[2]["train"]["sharpe"] for g in groups]
    test_d = [g[1]["test"]["sharpe"] - g[2]["test"]["sharpe"] for g in groups]
    x = np.arange(len(groups))
    fig, ax = plt.subplots(figsize=(9.2, 4.2), dpi=150)
    ax.axhline(0, color="#9ca3af", lw=0.9)
    ax.bar(x - 0.17, train_d, 0.32, color=TEAL,
           label="first half (2018-2022, includes the commodity boom)")
    ax.bar(x + 0.17, test_d, 0.32, color=RED,
           label="second half (2022-2026, the honest exam)")
    for i, (tr, te) in enumerate(zip(train_d, test_d)):
        ax.annotate(f"{tr:+.2f}", (i - 0.17, tr), ha="center",
                    textcoords="offset points",
                    xytext=(0, 5 if tr >= 0 else -13), fontsize=9.6)
        ax.annotate(f"{te:+.2f}", (i + 0.17, te), ha="center",
                    textcoords="offset points",
                    xytext=(0, 5 if te >= 0 else -13), fontsize=9.6,
                    fontweight="bold", color="#991b1b")
    ax.set_xticks(x, [g[0] for g in groups], fontsize=9.6)
    ax.set_ylabel("Change in risk-adjusted return vs current portfolio",
                  fontsize=10)
    ax.set_ylim(min(test_d) - 0.12, max(train_d) + 0.12)
    ax.set_title("Commodity funds: the improvement lives entirely in the "
                 "rear-view mirror — the honest exam gets worse",
                 fontsize=11.8, fontweight="bold", pad=12)
    ax.legend(fontsize=9.3, loc="lower left", framealpha=0.95)
    fig.tight_layout()
    out = DATA / "ws2_sum_commodity.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


def scope():
    # Counts per the RESEARCH_MEMO.md WS2 trial register (session total 32:
    # 21 new + 11 retro-logged from the 2026-07-01 commodity thread).
    cats = [
        ("Country-fund rotation set-ups  (2 universes × 3 depths + benchmark)", 7),
        ("Commodity-fund additions  (11 original probes + 5 re-tests)", 16),
        ("Removal tests of possibly-duplicate holdings", 4),
        ("Ways of arranging the EM position", 5),
    ][::-1]
    labels = [c[0] for c in cats]
    vals = [c[1] for c in cats]
    fig, ax = plt.subplots(figsize=(9.6, 3.1), dpi=150)
    ax.barh(range(len(vals)), vals, height=0.62, color=NAVY)
    for i, v in enumerate(vals):
        ax.text(v + 0.3, i, str(v), va="center", fontsize=13,
                fontweight="bold", color="#222222")
        if v >= 16:
            ax.text(0.35, i, labels[i], va="center", ha="left", fontsize=11,
                    color="white", fontweight="bold")
        else:
            ax.text(v + 1.1, i, labels[i], va="center", ha="left",
                    fontsize=11, color="#333333")
    ax.set_xlim(0, 19.5)
    ax.set_ylim(-0.6, len(vals) - 0.4)
    ax.set_yticks([])
    ax.set_xlabel("Number of configurations tested", fontsize=11)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(left=False)
    ax.set_axisbelow(True)
    ax.set_title("The extent of the review: 32 configurations tested — "
                 "no additions, one housekeeping fix", fontsize=13.5,
                 fontweight="bold", loc="left", pad=12)
    fig.text(0.5, -0.04, "32 tested   →   31 no change   →   1 structural fix "
             "(the EM double position — not a performance claim)",
             ha="center", fontsize=12.2, color=NAVY, fontweight="bold")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    out = DATA / "ws2_sum_scope.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


def usbeta():
    """US-equity-cluster look-through weight — same arithmetic as
    run_ws2_correlation.py, asserted against its stored statistics."""
    from etf_registry import get_etf, UNIVERSE_ETFS  # noqa: E402
    corr = J("ws2_correlation.json")
    members = corr["clusters_080_full"][0]["members"]
    ov = corr["blend_lookthrough_overlap"]["largest_cluster"]
    meta = J("ws2_baselines_meta.json")
    start = pd.Timestamp(meta["common_start"])
    end = pd.Timestamp(meta["common_end"])
    proxy_map = {etf: (get_etf(etf).get("yfinance_trading_proxy") or etf)
                 for etf in UNIVERSE_ETFS}
    wts = {s: pd.read_parquet(DATA / f"ws2_baseline_weights_{s}.parquet")
           for s in "ABCD"}
    wts["A"] = wts["A"].rename(columns=proxy_map)
    idx = wts["A"].index
    for s in "BCD":
        idx = idx.intersection(wts[s].index)
    idx = idx[(idx >= start) & (idx <= end)]
    look = {}
    for s, sw in (("A", 0.35), ("B", 0.35), ("C", 0.10), ("D", 0.20)):
        f = wts[s].reindex(idx).fillna(0.0) * sw
        for col in f.columns:
            look[col] = look.get(col, pd.Series(0.0, index=idx)) + f[col]
    look_df = pd.DataFrame(look)
    cols = [t for t in members if t in look_df.columns]
    series = look_df[cols].sum(axis=1)
    assert abs(float(series.mean()) - ov["mean_lookthrough_w"]) < 0.005, \
        f"US-beta mean drifted: {series.mean():.4f} vs {ov['mean_lookthrough_w']}"
    assert abs(float(series.max()) - ov["max_lookthrough_w"]) < 0.005, \
        f"US-beta max drifted: {series.max():.4f} vs {ov['max_lookthrough_w']}"

    fig, ax = plt.subplots(figsize=(9.2, 4.0), dpi=150)
    ax.plot(series.index, series.values, color=NAVY, lw=1.6)
    ax.axhline(series.mean(), color=GREY, lw=1.0, ls=(0, (5, 3)))
    ax.text(series.index[10], series.mean() + 0.045,
            f"average {series.mean()*100:.0f}%", fontsize=9.6, color=GREY,
            bbox=dict(facecolor="white", edgecolor="none", pad=1.2))
    pk = series.idxmax()
    ax.annotate(f"peak {series.max()*100:.0f}%", (pk, float(series.max())),
                textcoords="offset points", xytext=(-60, 6), fontsize=9.6,
                color="#991b1b")
    ax.set_ylim(0, 1.0)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0],
                  ["0%", "25%", "50%", "75%", "100%"])
    ax.set_ylabel("Share of the portfolio in that one cluster", fontsize=10.5)
    ax.set_title("The honest concentration number: the US-equity cluster "
                 "averages about half the portfolio, and peaks far higher",
                 fontsize=11.6, fontweight="bold", pad=12)
    fig.tight_layout()
    out = DATA / "ws2_sum_usbeta.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out.relative_to(ROOT))


def main():
    eem_lookthrough()
    eem_ablation()
    country_regimes()
    commodity()
    scope()
    usbeta()


if __name__ == "__main__":
    main()
