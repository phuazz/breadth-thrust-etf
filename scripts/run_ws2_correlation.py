"""WS2 Experiment 1 — overlap control: correlation matrix + blend look-through.

Rolling weekly-return correlation over ALL sleeve members + WS2 candidates
(fixed window 2018-11-08 -> common end, plus trailing 1y), an explicit
cluster rule, and a quantification of intra-blend overlap (the SPY/QQQ/IJR
duplication between Sleeve A's trading proxies and Sleeve B, and A-sector
vs C-thematic pairs).

THE RULE (recorded for the memo): cluster names at pairwise weekly
correlation >= 0.80 on the full window (connected components — transparent,
no linkage parameter); within a cluster keep the most liquid/representative
line; any CANDIDATE with correlation > 0.90 to an incumbent is rejected
unless it adds exposure the incumbent cannot express (IUIT/CNDX 0.97 prune
is the precedent).

Three ways this analysis could be silently wrong, and the defences:
  1. FX MISMATCH — Sleeve D lines are EUR-priced on Xetra; correlating raw
     EUR series against USD series would manufacture fake decorrelation.
     Defence: D closes are converted EUR->USD with the same cached FX
     series the WS1 harness used (ws1_common.load_sleeve_d).
  2. ASYNCHRONOUS CLOSES — European (and A-share) closes print hours
     before New York, which depresses measured DAILY correlation.
     Defence: WEEKLY returns (W-FRI last available close) absorb most of
     the asynchronicity; the remaining bias is noted in the JSON.
  3. SHORT / STALE OVERLAP — late-inception names (PAVE 2020-03,
     159801.SZ 2019-12, IEMG 2012-10) would show noise correlations on
     few points. Defence: pairwise min_periods (52 weekly obs full window,
     40 for the trailing year); pairs below that report null. FM is
     excluded outright (fund liquidated 2025-01; dead tail is wind-down
     mechanics, not exposure).

Output: data/ws2_correlation.json + data/ws2_correlation.png
Run:    python scripts/run_ws2_correlation.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import ws2_common as W2  # noqa: E402
from etf_registry import get_etf, UNIVERSE_ETFS  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

OUT_JSON = W2.DATA / "ws2_correlation.json"
OUT_PNG = W2.DATA / "ws2_correlation.png"

CLUSTER_T = 0.80     # cluster edge threshold (full window)
FLAG_T = 0.90        # candidate-vs-incumbent rejection threshold
MINP_FULL = 52       # pairwise weekly obs required, full window
MINP_1Y = 40

# Buckets (display order). Duplicates resolved to the FIRST bucket that
# holds the ticker; the membership map records every role.
CAND_COUNTRIES = ["EWZ", "EWW", "EWY", "INDA", "EWT", "EWA", "EWS",
                  "EWG", "EWU", "IEMG"]          # EWJ/EEM/EFA already in B
CAND_COMMODITY = ["DBA", "DBB", "DBE", "GSG", "USO", "UNG", "SLV"]

# Approximate AUM tiers (data/ishares_catalogue.csv vintage 2026-05 where
# listed; SPDR/thematic lines assigned by judgement — APPROXIMATE, used
# only to rank keep-candidates inside a cluster, flagged as estimates).
TIER = {
    "SPY": 6, "QQQ": 6, "IJR": 6, "GLD": 6, "EEM": 6, "IEMG": 6, "EFA": 6,
    "TLT": 5, "IEF": 5, "TIP": 4, "SHY": 6, "VNQ": 5, "VGK": 5, "EWJ": 4,
    "SOXX": 4, "XLE": 5, "XLF": 5, "XLV": 5, "XLI": 5, "XLP": 5, "XLY": 5,
    "XLU": 5, "XLB": 5, "XLC": 5, "XLRE": 4, "DBC": 3,
    "EXV1": 4, "EXH1": 3, "EXV3": 3, "EXH3": 3, "EXH9": 3,
    "EWZ": 4, "EWW": 3, "EWY": 4, "INDA": 5, "EWT": 4, "EWA": 3,
    "EWS": 2, "EWG": 3, "EWU": 3,
    "ARKK": 4, "XBI": 4, "GDX": 4, "ITA": 5, "PAVE": 4, "IHI": 4,
    "CIBR": 4, "SKYY": 3, "BOTZ": 3, "BLOK": 2, "ICLN": 4, "TAN": 3,
    "LIT": 3, "URA": 4, "ARKG": 3, "JETS": 3, "COPX": 3, "MOO": 2,
    "XME": 3, "WOOD": 2, "REMX": 2, "CQQQ": 3, "159801.SZ": 2, "PHO": 3,
    "BTC-USD": 6, "DBA": 2, "DBB": 2, "DBE": 1, "GSG": 2, "USO": 3,
    "UNG": 3, "SLV": 4,
}


def build_panel() -> tuple[pd.DataFrame, dict, list]:
    """Daily USD close panel + membership map + display order."""
    membership: dict[str, list[str]] = {}
    order: list[str] = []
    frames: dict[str, pd.Series] = {}

    def add(ticker, series, role):
        membership.setdefault(ticker, []).append(role)
        if ticker not in frames:
            frames[ticker] = series
            order.append(ticker)

    # Sleeve A trading proxies
    for etf in UNIVERSE_ETFS:
        proxy = get_etf(etf).get("yfinance_trading_proxy") or etf
        add(proxy, W._proxy_close_from_cache(etf), f"A(proxy of {etf})")
    # Sleeve B (incl SHY cash)
    closes_b = B_engine.download_prices()
    for t in closes_b.columns:
        add(t, closes_b[t], "B")
    # Sleeve D (EUR->USD)
    closes_d, _ = W.load_sleeve_d()
    for t in closes_d.columns:
        add(t, closes_d[t], "D")
    # Sleeve C (deployed loader output: FX + drags applied)
    closes_c = C_engine.download_prices()
    for t in closes_c.columns:
        if t == C_engine.CASH_PROXY:
            membership.setdefault(t, []).append("C(cash)")
            continue
        add(t, closes_c[t], "C")
    # Candidates (countries + broad EM)
    ws2 = W2.load_ws2_prices()
    for t in CAND_COUNTRIES:
        add(t, ws2[t], "cand")
        if t in frames and "cand" not in membership[t]:
            membership[t].append("cand")
    for t in ("EWJ", "EEM", "EFA"):
        membership.setdefault(t, []).append("cand")
    # Commodity-spot candidates
    cm = pd.read_parquet(W2.DATA / "commodity_expansion_prices.parquet")
    cm.index = pd.to_datetime(cm.index).tz_localize(None)
    for t in CAND_COMMODITY:
        add(t, cm[t], "comm")
    for t in ("DBC", "GLD"):
        membership.setdefault(t, []).append("comm(base)")

    panel = pd.DataFrame(frames).sort_index()
    return panel[order], membership, order


def clusters_above(corr: pd.DataFrame, threshold: float) -> list[list[str]]:
    """Connected components of the corr>=threshold graph (numpy only)."""
    names = list(corr.index)
    adj = (corr.values >= threshold) & ~np.eye(len(names), dtype=bool)
    seen, out = set(), []
    for i in range(len(names)):
        if i in seen:
            continue
        stack, comp = [i], []
        while stack:
            j = stack.pop()
            if j in seen:
                continue
            seen.add(j)
            comp.append(j)
            stack.extend(np.flatnonzero(adj[j]).tolist())
        if len(comp) > 1:
            out.append(sorted((names[k] for k in comp),
                              key=lambda n: -TIER.get(n, 3)))
    return sorted(out, key=len, reverse=True)


def main() -> int:
    base = W2.build_baselines()
    start, end = base["common_start"], base["common_end"]
    panel, membership, order = build_panel()
    panel = panel.loc[:end]

    weekly = panel.resample("W-FRI").last()
    wret = weekly.pct_change(fill_method=None)
    wret_full = wret.loc[start:end]
    wret_1y = wret_full.iloc[-52:]
    print(f"Panel: {panel.shape[1]} lines; weekly obs full={len(wret_full)}, "
          f"1y={len(wret_1y)}")

    corr_full = wret_full.corr(min_periods=MINP_FULL)
    corr_1y = wret_1y.corr(min_periods=MINP_1Y)

    # ---- >0.9 pairs (full + 1y) ----
    def high_pairs(corr, t):
        out = []
        cols = list(corr.columns)
        for i, a in enumerate(cols):
            for b in cols[i + 1:]:
                v = corr.loc[a, b]
                if pd.notna(v) and v > t:
                    out.append({"a": a, "b": b, "corr": round(float(v), 3),
                                "roles_a": membership[a],
                                "roles_b": membership[b]})
        return sorted(out, key=lambda r: -r["corr"])

    pairs_full = high_pairs(corr_full, FLAG_T)
    pairs_1y = high_pairs(corr_1y, FLAG_T)
    print(f"\nPairs > {FLAG_T} (full window): {len(pairs_full)}")
    for p in pairs_full:
        print(f"  {p['corr']:+.3f}  {p['a']:>9s} ({'/'.join(p['roles_a'])})"
              f" — {p['b']} ({'/'.join(p['roles_b'])})")

    # ---- clusters at 0.8 ----
    clus = clusters_above(corr_full, CLUSTER_T)
    print(f"\nClusters at >= {CLUSTER_T} (full window): {len(clus)}")
    cluster_rows = []
    for c in clus:
        keep = c[0]
        cluster_rows.append({
            "members": c, "keep_most_liquid": keep,
            "roles": {m: membership[m] for m in c},
            "note": "tier ranking approximate (catalogue vintage 2026-05)",
        })
        print(f"  keep {keep:>9s} <- {c}")

    # ---- candidate flags vs incumbents ----
    incumbents = [t for t, r in membership.items()
                  if any(x[0] in "ABCD" for x in r)]
    cand_flags = []
    for t in CAND_COUNTRIES + CAND_COMMODITY:
        if t not in corr_full.columns:
            continue
        s = corr_full.loc[t, [i for i in incumbents
                              if i in corr_full.columns and i != t]].dropna()
        if s.empty:
            continue
        worst = s.idxmax()
        if s[worst] > FLAG_T:
            cand_flags.append({"candidate": t, "incumbent": worst,
                               "corr": round(float(s[worst]), 3)})
    print(f"\nCandidates flagged > {FLAG_T} vs an incumbent:")
    for f in cand_flags:
        print(f"  {f['candidate']:>6s} vs {f['incumbent']:<6s} {f['corr']:+.3f}")

    # ---- intra-blend look-through overlap ----
    wts = base["weights"]
    proxy_map = {etf: (get_etf(etf).get("yfinance_trading_proxy") or etf)
                 for etf in UNIVERSE_ETFS}
    wA = wts["A"].rename(columns=proxy_map)
    wB, wC = wts["B"], wts["C"]
    idx = wA.index.intersection(wB.index).intersection(wC.index)
    idx = idx[(idx >= start) & (idx <= end)]
    look = {}
    for t, frame, sw in (("A", wA, 0.35), ("B", wB, 0.35), ("C", wC, 0.10),
                         ("D", wts["D"], 0.20)):
        f = frame.reindex(idx).fillna(0.0) * sw
        for col in f.columns:
            look[col] = look.get(col, pd.Series(0.0, index=idx)) + f[col]
    look_df = pd.DataFrame(look)

    dup_lines = ["SPY", "QQQ", "IJR"]
    overlap = {}
    for t in dup_lines:
        a_col = t if t in wA.columns else None
        joint = ((wA[a_col].reindex(idx).fillna(0) > 1e-6)
                 & (wB[t].reindex(idx).fillna(0) > 1e-6)).mean() if a_col else None
        overlap[t] = {
            "mean_lookthrough_w": round(float(look_df[t].mean()), 4),
            "max_lookthrough_w": round(float(look_df[t].max()), 4),
            "share_weeks_held_by_both_A_and_B":
                round(float(joint), 3) if joint is not None else None,
        }
    # concentration into the biggest cluster (US large-cap beta)
    big = clus[0] if clus else []
    big_cols = [t for t in big if t in look_df.columns]
    us_beta = look_df[big_cols].sum(axis=1) if big_cols else pd.Series(dtype=float)
    overlap["largest_cluster"] = {
        "members_in_blend": big_cols,
        "mean_lookthrough_w": round(float(us_beta.mean()), 4) if len(us_beta) else None,
        "max_lookthrough_w": round(float(us_beta.max()), 4) if len(us_beta) else None,
    }
    print("\nBlend look-through (ungated 35/35/10/20, weekly drift ignored):")
    for t in dup_lines:
        o = overlap[t]
        print(f"  {t}: mean {o['mean_lookthrough_w']*100:.1f}% "
              f"max {o['max_lookthrough_w']*100:.1f}% "
              f"joint-held {o['share_weeks_held_by_both_A_and_B']}")
    if overlap["largest_cluster"]["mean_lookthrough_w"] is not None:
        print(f"  largest cluster ({len(big_cols)} lines): mean "
              f"{overlap['largest_cluster']['mean_lookthrough_w']*100:.1f}% "
              f"max {overlap['largest_cluster']['max_lookthrough_w']*100:.1f}%")

    # ---- heatmap ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(16, 14), dpi=160)
    m = corr_full.loc[order, order].values
    im = ax.imshow(m, vmin=-1, vmax=1, cmap="RdBu_r")
    ax.set_xticks(range(len(order)))
    ax.set_yticks(range(len(order)))
    bucket_colour = {"A": "#1d4ed8", "B": "#047857", "D": "#7c3aed",
                     "C": "#b45309", "c": "#dc2626"}
    ax.set_xticklabels(order, rotation=90, fontsize=4.5)
    ax.set_yticklabels(order, fontsize=4.5)
    for lbl in list(ax.get_xticklabels()) + list(ax.get_yticklabels()):
        role = membership[lbl.get_text()][0][0]
        lbl.set_color(bucket_colour.get(role, "#dc2626"))
    ax.set_title("WS2 weekly-return correlation, fixed window "
                 f"{start.date()} -> {end.date()}  "
                 "(blue=A proxies, green=B, purple=D, brown=C, red=candidates)",
                 fontsize=9)
    fig.colorbar(im, ax=ax, shrink=0.6)
    fig.tight_layout()
    fig.savefig(OUT_PNG)
    print(f"wrote {OUT_PNG.relative_to(ROOT)}")

    W.write_json(OUT_JSON, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "weekly_obs_full": int(len(wret_full)),
                   "weekly_obs_1y": int(len(wret_1y))},
        "rule": (f"cluster at pairwise weekly corr >= {CLUSTER_T} (full "
                 "window, connected components); keep most liquid per "
                 f"cluster; reject candidates > {FLAG_T} vs an incumbent "
                 "unless distinct exposure argued in writing"),
        "caveats": ("weekly W-FRI returns absorb most EU/Asia close "
                    "asynchronicity (residual bias noted); AUM tiers "
                    "approximate; FM excluded (liquidated 2025-01)"),
        "membership": membership,
        "pairs_gt_090_full": pairs_full,
        "pairs_gt_090_1y": pairs_1y,
        "clusters_080_full": cluster_rows,
        "candidate_flags_vs_incumbents": cand_flags,
        "blend_lookthrough_overlap": overlap,
        "corr_full": {a: {b: (None if pd.isna(corr_full.loc[a, b])
                              else round(float(corr_full.loc[a, b]), 2))
                          for b in order} for a in order},
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
