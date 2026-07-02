"""WS2 Experiment 6 — trend-opportunity map (descriptive; no backtest).

Lays the exposure space out as a grid and plots current coverage, the
session's add/drop verdicts, and the named gaps. Purely descriptive —
every cell verdict is backed by a specific artefact from this session
(ws2_correlation.json, ws2_country_sleeve.json, ws2_commodity_fixed.json,
ws2_prune_tests.json, ws2_eem_coherence.json) or a documented deployed
decision (HYG removal, IUIT prune, SLV revert).

Output: data/ws2_trend_map.png
Run:    python scripts/run_ws2_trend_map.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ws2_trend_map.png"
sys.stdout.reconfigure(encoding="utf-8")

# (bucket, coverage colour key, instruments today, WS2 verdict)
ROWS = [
    ("US broad equity",        "dual",    "CSP1/CNDX (A, breadth) + SPY/QQQ (B, momentum)",
     "Deliberate dual-signal coverage; look-through quantified (US-beta cluster mean 46.8%, max 83.5% of NAV)"),
    ("US sectors (11 GICS)",   "full",    "Sleeve A: 11 iShares UCITS slices + SOXX, SPDR proxies",
     "Covered; IUIT stays pruned (0.97 vs CNDX)"),
    ("US small-cap",           "dual",    "IDP6 (A) + IJR (B)",
     "Deliberate dual-signal coverage"),
    ("Ex-US DM broad",         "full",    "EFA, VGK, EWJ (B)",
     "Covered as aggregates; EFA/VGK 0.984 prune tested -> wash, keep both (incumbent wins ties)"),
    ("Ex-US DM sectors",       "partial", "Sleeve D: 5 of ~19 Stoxx 600 supersectors",
     "PARTIAL — widening needs point-in-time EU constituents (expensive bucket); defer"),
    ("EM broad",               "full",    "EEM (Phase 22 overlay)",
     "PROPOSED: overlay-only role — EEM leaves B's universe (2x2 ablation, all cells inside noise, coherence decides)"),
    ("Single countries DM/EM", "killed",  "none",
     "KILLED this session: momentum sleeve fails 3/6 sub-periods, train half negative — regime bet, not edge"),
    ("Frontier",               "gap",     "none",
     "GAP but UNINVESTABLE: FM liquidated 2025-01 (caught by ticker verification); no clean US-listed replacement"),
    ("US rates / duration",    "full",    "TLT, IEF, TIP + SHY floor (B)",
     "Covered; TLT/IEF 0.918 = deliberate duration ladder"),
    ("Non-US rates",           "gap",     "none",
     "GAP acknowledged: no USD-listed liquid vehicle set fits the momentum bucket cleanly (BWX thin); low priority"),
    ("Credit",                 "delib",   "none (HYG removed Phase 24)",
     "DELIBERATE gap: HYG was an equity-correlated fake diversifier; documented decision"),
    ("Commodities",            "full",    "GLD + DBC (B)",
     "Sector granularity KILLED: DBA/DBB/DBE/DBC adds fail everywhere (blend dTest -0.124, 2/6); DBE/GSG 0.94-0.96 to DBC"),
    ("REITs",                  "dual",    "IUSP->XLRE (A) + VNQ (B), corr 0.990",
     "Deliberate dual-signal coverage, US-only; global REIT gap minor"),
    ("Crypto",                 "full",    "BTC-USD in C (25 bps IBIT drag)",
     "Covered, capacity-flagged; ETH deferred (would deepen C's survivorship bias)"),
    ("Styles / factors",       "rule",    "none",
     "GAP, marginal by construction: QUAL 0.985 to SPY (auto-reject); USMV/MTUM/VLUE 0.889-0.896 — ON the 0.9 flag boundary; defer (factor-timing knob, weak prior)"),
    ("Thematics",              "full",    "25 names in C (+5% floor, 30% gate)",
     "Covered, survivorship-flagged; prune bundle {TAN,SKYY,PAVE} tested -> REJECTED (carried the train half)"),
]

COLOURS = {
    "full":    "#bbf7d0",   # green — covered
    "dual":    "#a5d8ff",   # blue — deliberate dual-signal coverage
    "partial": "#fde68a",   # amber — partial
    "gap":     "#fecaca",   # red — open gap
    "delib":   "#e5e7eb",   # grey — deliberate gap
    "killed":  "#fca5a5",   # dark red — evaluated and killed this session
    "rule":    "#ddd6fe",   # violet — blocked by the overlap rule
}
LEGEND = [("full", "covered"), ("dual", "dual-signal (deliberate)"),
          ("partial", "partial"), ("gap", "open gap"),
          ("delib", "deliberate gap"),
          ("killed", "killed this session"),
          ("rule", "overlap-rule marginal")]


def main() -> int:
    fig, ax = plt.subplots(figsize=(15, 9.5), dpi=160)
    ax.set_xlim(0, 15)
    ax.set_ylim(0, len(ROWS))
    ax.axis("off")
    ax.set_title(
        "WS2 trend-opportunity map — exposure space vs coverage "
        "(fixed window 2018-11-08 -> 2026-06-16; verdicts from ws2_* artefacts)",
        fontsize=11, loc="left")
    for i, (bucket, key, instruments, verdict) in enumerate(ROWS):
        y = len(ROWS) - 1 - i
        ax.add_patch(Rectangle((0, y), 15, 0.96, color=COLOURS[key],
                               ec="white", lw=1.5))
        ax.text(0.15, y + 0.68, bucket, fontsize=9.5, fontweight="bold",
                va="center")
        ax.text(0.15, y + 0.28, instruments, fontsize=8, va="center",
                color="#1f2937")
        ax.text(5.6, y + 0.48, verdict, fontsize=8, va="center",
                color="#111827", wrap=True)
    for j, (key, label) in enumerate(LEGEND):
        x = 0.15 + j * 2.15
        ax.add_patch(Rectangle((x, -0.85), 0.28, 0.28,
                               color=COLOURS[key], clip_on=False))
        ax.text(x + 0.36, -0.71, label, fontsize=7.5, va="center",
                clip_on=False)
    ax.set_ylim(-1.1, len(ROWS))
    fig.tight_layout()
    fig.savefig(OUT, bbox_inches="tight")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
