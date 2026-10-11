"""Sleeve A universe gate: 50-day JSON field vs deployed 200-day breadth.

Read-only diagnostic. Imports the gate's own engine, correlation and
walk-forward code unchanged; only the breadth panel differs between arms.

Arms (all on one pinned data snapshot and one price frame):
  J50  series.ma_breadth from breadth_<etf>.json (what the gate reads today)
  C50  compute_ma200_breadth(cp, 50), validated-end capped  (construction control)
  C200 compute_ma200_breadth(cp, 200), validated-end capped (deployed signal)

Windows: each arm on its native eligible_start, and all arms on the common
(latest) eligible_start.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

WT = Path(__file__).resolve().parents[2]
# GATE_SNAP: a directory holding breadth_/constituents_<etf>.json and
# prices_cache_<etf>.parquet for the 14 sleeve A lines. The 2026-10-11 run used a
# copy taken from the -sched clone; checksums in snapshot_SHA256SUMS.
SNAP = Path(os.environ.get("GATE_SNAP", WT / "data"))
OUT = Path(__file__).resolve().parent / "gate_ma_basis_out.json"
PRICES = SNAP.parent / "proxy_closes.parquet"
sys.path.insert(0, str(WT / "scripts"))

import run_strategy_a_universe_gate as g  # noqa: E402
import run_ma200_sweep as m  # noqa: E402
from validated_breadth import validated_end, cap_signal  # noqa: E402

g.DATA_DIR = SNAP
m.DATA_DIR = SNAP
CAND = "SOXX"
UNIV = list(g.UNIVERSE_ETFS)
assert CAND in UNIV and len(UNIV) == 14, UNIV


def json_50d_panel() -> pd.DataFrame:
    """series.ma_breadth from breadth_<etf>.json — the 50-day field the gate
    script read until 2026-10-11 (its loader now builds 200-day breadth)."""
    cols = {}
    for t in UNIV:
        s = json.loads((SNAP / f"breadth_{t.lower()}.json").read_text(encoding="utf-8"))["series"]
        cols[t] = pd.Series(s["ma_breadth"], index=pd.to_datetime(s["dates"]), dtype=float).dropna()
    return pd.DataFrame(cols).sort_index()


def computed_panel(period: int) -> pd.DataFrame:
    cols = {}
    for t in UNIV:
        cp = m.load_constituent_prices(t)
        cols[t] = cap_signal(m.compute_ma200_breadth(cp, period), validated_end(SNAP, t))
    df = pd.DataFrame(cols).sort_index()
    return df.dropna(how="all")


def eligible(breadth_aligned: pd.DataFrame, closes: pd.DataFrame) -> pd.Timestamp:
    # Same rule as run_gate: latest first-valid date + 30 trading days.
    latest_first = max(breadth_aligned[c].first_valid_index() for c in breadth_aligned.columns)
    pos = closes.index.get_loc(latest_first)
    return closes.index[min(pos + 30, len(closes) - 1)]


def score(breadth_all: pd.DataFrame, closes: pd.DataFrame, es: pd.Timestamp) -> dict:
    baseline = [t for t in UNIV if t != CAND]
    weekly = breadth_all.resample("W-FRI").last().dropna(how="all")
    corr = weekly.corr().loc[CAND, baseline].sort_values(ascending=False)
    ba = breadth_all.reindex(closes.index).ffill(limit=7)
    bl = g.run_topk_backtest(closes[baseline], ba[baseline], g.DEPLOYED_K, es)
    pr = g.run_topk_backtest(closes[UNIV], ba[UNIV], g.DEPLOYED_K, es)
    bs, ps = g.compute_stats(bl["equity"], es), g.compute_stats(pr["equity"], es)
    bw = g.walk_forward_K(closes[baseline], ba[baseline], es)
    pw = g.walk_forward_K(closes[UNIV], ba[UNIV], es)
    d_sh = ps["sharpe"] - bs["sharpe"]
    d_dd = (ps["max_dd"] - bs["max_dd"]) * 100
    d_wf = (pw["walk_forward_sharpe"] - bw["walk_forward_sharpe"]
            if bw.get("walk_forward_sharpe") is not None and pw.get("walk_forward_sharpe") is not None
            else None)
    pr_k = [s["best_K"] for s in pw.get("segments", [])]
    bl_k = [s["best_K"] for s in bw.get("segments", [])]
    gates = {
        "g1_corr": float(corr.iloc[0]) < g.CORR_THRESHOLD,
        "g2_sharpe": d_sh > -g.SHARPE_DEGRADE_TOLERANCE,
        "g3_dd": d_dd > -g.DD_DEGRADE_TOLERANCE_PP,
        "g4_wf_sharpe": None if d_wf is None else d_wf > -g.SHARPE_DEGRADE_TOLERANCE,
    }
    return {
        "eligible_start": es.strftime("%Y-%m-%d"),
        "end": closes.index[-1].strftime("%Y-%m-%d"),
        "max_corr": round(float(corr.iloc[0]), 4), "max_corr_with": corr.index[0],
        "top5": {k: round(float(v), 4) for k, v in corr.head(5).items()},
        "n_pairs_above_085_in_universe": int(
            (weekly[UNIV].corr().where(lambda x: x < 0.9999) > g.CORR_THRESHOLD).sum().sum() // 2),
        "baseline": {k: round(v, 4) for k, v in bs.items()},
        "proposed": {k: round(v, 4) for k, v in ps.items()},
        "d_sharpe": round(d_sh, 4), "d_dd_pp": round(d_dd, 2),
        "wf_baseline": bw.get("walk_forward_sharpe"), "wf_proposed": pw.get("walk_forward_sharpe"),
        "d_wf": None if d_wf is None else round(d_wf, 4),
        "k_seq_baseline": bl_k, "k_seq_proposed": pr_k,
        "k_in_567_plateau_proposed": all(k in (5, 6, 7) for k in pr_k) if pr_k else None,
        "wf_segments_proposed": pw.get("segments"),
        "wf_segments_baseline": bw.get("segments"),
        "gates": gates,
        "overall_pass": all(v for v in gates.values() if v is not None),
    }


def main() -> None:
    arms = {
        "J50": json_50d_panel(),
        "C50": computed_panel(50),
        "C200": computed_panel(200),
    }
    start = min(a.index.min() for a in arms.values()).strftime("%Y-%m-%d")
    end = (max(a.index.max() for a in arms.values()) + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    if PRICES.exists():
        closes = pd.read_parquet(PRICES)
    else:
        closes = g.download_closes(UNIV, start, end)
        closes.to_parquet(PRICES)
    # One price frame for every arm; drop a trailing partial row if any column is missing.
    closes = closes.loc[:, UNIV]
    closes = closes.loc[: closes.dropna().index.max()]
    print("price frame", closes.index[0].date(), "->", closes.index[-1].date(), closes.shape)

    native = {k: eligible(a.reindex(closes.index).ffill(limit=7), closes) for k, a in arms.items()}
    common = max(native.values())
    res = {"snapshot": str(SNAP), "price_frame": [str(closes.index[0].date()), str(closes.index[-1].date())],
           "native": {}, "common": {}, "panel_diagnostics": {}}
    for k, a in arms.items():
        res["native"][k] = score(a, closes, native[k])
        res["common"][k] = score(a, closes, common)
        print(k, "native", native[k].date(), res["native"][k]["gates"], "common", common.date(),
              res["common"][k]["gates"])
    # How different are the panels themselves (weekly, common dates)?
    for t in UNIV:
        w = pd.DataFrame({k: a[t] for k, a in arms.items()}).resample("W-FRI").last().dropna()
        w = w.loc[w.index >= common]
        res["panel_diagnostics"][t] = {
            "corr_J50_C50": round(float(w["J50"].corr(w["C50"])), 4),
            "corr_C50_C200": round(float(w["C50"].corr(w["C200"])), 4),
            "medabs_J50_C50_pp": round(float((w["J50"] - w["C50"]).abs().median() * 100), 2),
        }
    # Rank agreement of the relative signal at the top 7, weekly
    rel = {k: g.relative_breadth_signal(a.reindex(closes.index).ffill(limit=7)) for k, a in arms.items()}
    fr = closes.index[(closes.index >= common) & (closes.index.weekday == 3)]
    def overlap(x, y):
        o = []
        for d in fr:
            a, b = rel[x].loc[d].dropna(), rel[y].loc[d].dropna()
            if len(a) < 8 or len(b) < 8:
                continue
            o.append(len(set(a.nlargest(7).index) & set(b.nlargest(7).index)) / 7)
        return round(float(pd.Series(o).mean()), 4), len(o)
    res["top7_overlap_thursday"] = {"J50_vs_C200": overlap("J50", "C200"),
                                    "C50_vs_C200": overlap("C50", "C200"),
                                    "J50_vs_C50": overlap("J50", "C50")}
    OUT.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
