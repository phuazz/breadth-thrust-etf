"""WS3 Item 4 — cost and execution stress at 1x/2x/3x with PER-LINE spread
overrides, plus the break-even cost multiple per sleeve and for the blend.

The deployed cost model is a per-sleeve scalar (A 2 / B 2 / C 5 / D 9 bps
one-way). This stress replaces it with a per-line vector priced off each
TRADED line's typical quoted spread + slippage (estimates, flagged below),
then scales that vector 1x/2x/3x. Holding drags already embedded in the
price series by the deployed loaders (BTC-USD 25 bps p.a. wrapper drag,
159801.SZ FX + expense) are NOT re-charged — the vector prices trading
costs only.

Per-line one-way bps (ESTIMATES stated for review, not tuned):
  A trades US proxies (SPY/QQQ/IJR/SOXX + SPDR sectors): 2
  B: broad lines 2, DBC 5, TIP 3, SHY 1
  C: liquid thematics (ARKK XBI GDX JETS ICLN TAN LIT CIBR SKYY) 8,
     thinner thematics (BOTZ BLOK URA ARKG COPX MOO PAVE ITA XME WOOD
     REMX CQQQ PHO IHI) 12, BTC-USD 25 (IBIT-style spread + premium
     volatility), 159801.SZ 25 (A-share access + CNY conversion), SHY 1
  D UCITS lines (EXV1 EXH1 EXV3 EXH3 EXH9): 15 (LSE/Xetra book + FX)
  Overlay switch costs (gate + tilt flips): deployed 5 bps, scaled with m.

BREAK-EVEN DEFINITION: the smallest multiple m at which the strategy's
full-window Sharpe falls to its no-skill comparator — per sleeve, the
equal-weight weekly-rebalanced basket of the SAME universe (charged the
FIXED 1x vector; stressing the benchmark too would flatter the rotation);
for the blend, the 35/35/10/20 blend of the four equal-weight baskets.

Three ways this could be silently wrong, and the defences:
  1. RECONSTRUCTION MISMATCH — stressed returns are rebuilt as
     gross(weights, closes) - m x drag(vector). Before any stress, the
     rebuild at the deployed scalar cost must reproduce each cached sleeve
     equity curve's Sharpe to 1e-6, or the script raises.
  2. DOUBLE-CHARGED DRAGS — expense/FX drags live in the loader prices;
     the vector prices trading only (stated above); no line is charged
     both ways.
  3. BENCHMARK CONTAMINATION — the comparator's cost is FIXED at 1x while
     the strategy is stressed; the break-even is therefore conservative
     (the real-world benchmark would degrade too).

Output: data/ws3_cost_stress.json
Run:    python scripts/run_ws3_cost_stress.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import ws3_common as W3  # noqa: E402
from etf_registry import get_etf, UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402

OUT = W.DATA / "ws3_cost_stress.json"
MULTIPLES = [1.0, 2.0, 3.0]
BREAKEVEN_GRID = np.arange(1.0, 30.25, 0.25)

B_LINE_BPS = {"DBC": 5.0, "TIP": 3.0, "SHY": 1.0}          # default 2
C_LIQUID = {"ARKK", "XBI", "GDX", "JETS", "ICLN", "TAN", "LIT", "CIBR",
            "SKYY"}
C_LINE_BPS = {"BTC-USD": 25.0, "159801.SZ": 25.0, "SHY": 1.0}
D_UCITS_BPS = 15.0
A_PROXY_BPS = 2.0


def sleeve_cost_vector(sleeve: str, cols) -> pd.Series:
    bps = pd.Series(index=list(cols), dtype=float)
    if sleeve == "A":
        bps[:] = A_PROXY_BPS
    elif sleeve == "B":
        bps[:] = 2.0
        for k, v in B_LINE_BPS.items():
            if k in bps.index:
                bps[k] = v
    elif sleeve == "C":
        for c in cols:
            if c in C_LINE_BPS:
                bps[c] = C_LINE_BPS[c]
            elif c in C_LIQUID:
                bps[c] = 8.0
            else:
                bps[c] = 12.0
    elif sleeve == "D":
        bps[:] = D_UCITS_BPS
    return bps / 1e4


def load_closes() -> dict[str, pd.DataFrame]:
    closes = {}
    closes["A"] = pd.DataFrame(
        {etf: W._proxy_close_from_cache(etf) for etf in UNIVERSE_ETFS}
    ).sort_index()
    d_eur = pd.DataFrame(
        {etf: W._proxy_close_from_cache(etf)
         for etf in UNIVERSE_EUROPE_SECTORS}).sort_index()
    fx = pd.read_parquet(W.DATA / "ws1_fx_eurusd_cache.parquet")["EURUSD"]
    fx = fx.reindex(d_eur.index, method="ffill").bfill()
    closes["D"] = d_eur.multiply(fx, axis=0)
    closes["B"] = B_engine.download_prices()
    closes["C"] = W.load_sleeve_c()
    return closes


def gross_and_drag(weights: pd.DataFrame, closes: pd.DataFrame,
                   cost_vec: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(gross daily return, per-line 1x drag, scalar-equivalent turnover)."""
    cols = [c for c in weights.columns if c in closes.columns]
    wp = weights[cols].fillna(0.0)
    rets = closes[cols].reindex(wp.index).pct_change().fillna(0)
    gross = (wp.shift(1).fillna(0) * rets).sum(axis=1)
    dw = wp.diff().abs().fillna(0)
    drag = (dw * cost_vec.reindex(cols)).sum(axis=1)
    turnover = dw.sum(axis=1)
    return gross, drag, turnover


def ew_benchmark(closes: pd.DataFrame, cost_vec: pd.Series, cash: str | None,
                 idx: pd.DatetimeIndex, start: pd.Timestamp) -> pd.Series:
    """Equal-weight weekly-rebalanced basket of the risk lines, charged the
    FIXED 1x vector."""
    cols = [c for c in closes.columns if c != cash]
    rebal_target = pd.date_range(start, closes.index[-1], freq=W.REBAL)
    rebal = closes.index[closes.index.isin(rebal_target)]
    rb = pd.DataFrame(np.nan, index=rebal, columns=cols, dtype=float)
    rb[:] = 1.0 / len(cols)
    wp = rb.reindex(closes.index, method="ffill").fillna(0.0)
    wp.loc[wp.index < start] = 0.0
    rets = closes[cols].pct_change().fillna(0)
    gross = (wp.shift(1).fillna(0) * rets).sum(axis=1)
    drag = (wp.diff().abs().fillna(0) * cost_vec.reindex(cols)).sum(axis=1)
    return (gross - drag).reindex(idx).fillna(0)


def ann_sharpe(x: pd.Series) -> float:
    sd = x.std()
    return float(x.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0


def main() -> int:
    base = W3.build_ws3_baselines()
    idx, start, end = base["idx"], base["common_start"], base["common_end"]
    closes = load_closes()
    scalar_cost = {"A": W.COST_A, "B": W.COST_B, "C": W.COST_C, "D": W.COST_D}

    sleeves = {}
    for s in ["A", "B", "C", "D", "C_S1", "B_S2"]:
        panel = closes[s[0]]
        wts = base["weights"][s].reindex(panel.index).fillna(0.0)
        vec = sleeve_cost_vector(s[0], wts.columns)
        gross, drag1x, turn = gross_and_drag(wts, panel, vec)
        # defence 1: rebuild at deployed scalar must match the cached curve
        # (compared on the sleeve's OWN calendar; the blend calendar drops
        # the rare non-common days and would compound across them)
        net_dep = gross - turn * scalar_cost[s[0]]
        own = base["equities"][s].dropna()
        own = own.loc[(own.index >= start) & (own.index <= end)]
        got = ann_sharpe(net_dep.loc[own.index[0]:own.index[-1]]
                         .reindex(own.index).fillna(0))
        ref = ann_sharpe(own.pct_change().fillna(0))
        assert abs(got - ref) < 1e-6, (
            f"reconstruction mismatch for {s}: {got:+.6f} vs {ref:+.6f}")
        sleeves[s] = {"gross": gross.reindex(idx).fillna(0),
                      "drag": drag1x.reindex(idx).fillna(0),
                      "turnover_annual": float(turn.loc[turn.index >= start]
                                               .sum()
                                               / ((end - start).days / 365.25)),
                      "vector_bps_mean": float(vec.mean() * 1e4)}

    benchmarks = {s: ew_benchmark(closes[s], sleeve_cost_vector(
        s, [c for c in closes[s].columns]),
        "SHY" if s in ("B", "C") else None, idx, start)
        for s in "ABCD"}
    bench_blend = (0.35 * benchmarks["A"] + 0.35 * benchmarks["B"]
                   + 0.10 * benchmarks["C"] + 0.20 * benchmarks["D"])
    bench_sh = {s: ann_sharpe(benchmarks[s]) for s in "ABCD"}
    bench_sh["blend"] = ann_sharpe(bench_blend)

    def max_dd(r: pd.Series) -> float:
        eq = (1 + r).cumprod()
        return float((eq / eq.cummax() - 1).min())

    bench_dd = {s: max_dd(benchmarks[s]) for s in "ABCD"}
    bench_dd["blend"] = max_dd(bench_blend)
    print("EW benchmarks:", {k: round(v, 3) for k, v in bench_sh.items()})
    print("EW benchmark max DD:", {k: f"{v * 100:.0f}%"
                                   for k, v in bench_dd.items()})

    def sleeve_net(s: str, m: float) -> pd.Series:
        return sleeves[s]["gross"] - m * sleeves[s]["drag"]

    def blend_net(m: float, b_key: str = "B", c_key: str = "C",
                  overlays: bool = True) -> pd.Series:
        r = {"A": sleeve_net("A", m), "B": sleeve_net(b_key, m),
             "C": sleeve_net(c_key, m), "D": sleeve_net("D", m)}
        blend = (0.35 * r["A"] + 0.35 * r["B"] + 0.10 * r["C"]
                 + 0.20 * r["D"])
        if not overlays:
            return blend
        sig = base["tilt_sig_lagged"]
        on = blend - W3.TILT_W * r["B"] + W3.TILT_W * base["eem_ret"]
        sw_t = sig.diff().fillna(0).abs() * W3.SWITCH_COST * m
        tilted = sig * on + (1 - sig) * blend - sw_t
        st = base["gate_state_lagged"]
        derisked = (1 - W3.DERISK) * tilted + W3.DERISK * base["shy_ret"]
        sw_g = st.diff().fillna(0).abs() * W3.SWITCH_COST * m
        return st * tilted + (1 - st) * derisked - sw_g

    results = {"benchmark_sharpe": bench_sh, "benchmark_max_dd": bench_dd,
               "per_line_vectors_bps": {
                   s: {c: round(float(v * 1e4), 1) for c, v in
                       sleeve_cost_vector(s, closes[s].columns).items()}
                   for s in "ABCD"},
               "sleeves": {}, "blend": {}, "final_track": {},
               "shortlist_2x_leg": {}}

    for s in ["A", "B", "C", "D", "C_S1", "B_S2"]:
        row = {"annual_turnover_x": round(sleeves[s]["turnover_annual"], 1),
               "sharpe_at_multiple": {},
               "max_dd_1x": round(max_dd(sleeve_net(s, 1.0)), 4)}
        for m in MULTIPLES:
            row["sharpe_at_multiple"][f"{m:.0f}x"] = round(
                ann_sharpe(sleeve_net(s, m)), 4)
        bench = bench_sh[s[0]]
        be = None
        for m in BREAKEVEN_GRID:
            if ann_sharpe(sleeve_net(s, m)) <= bench:
                be = float(m)
                break
        row["breakeven_multiple_vs_ew"] = be if be is not None else ">30"
        results["sleeves"][s] = row
        print(f"{s:5s} turn {row['annual_turnover_x']:5.1f}x | "
              + " ".join(f"{k} {v:+.3f}" for k, v in
                         row["sharpe_at_multiple"].items())
              + f" | break-even {row['breakeven_multiple_vs_ew']}x "
              f"(EW {bench:+.3f})")

    for label, kwargs in (("ungated", {"overlays": False}),
                          ("final_gated_tilted", {"overlays": True})):
        row = {}
        for m in MULTIPLES:
            row[f"{m:.0f}x"] = round(ann_sharpe(blend_net(m, **kwargs)), 4)
        be = None
        for m in BREAKEVEN_GRID:
            if ann_sharpe(blend_net(m, **kwargs)) <= bench_sh["blend"]:
                be = float(m)
                break
        row["breakeven_multiple_vs_ew_blend"] = be if be is not None else ">30"
        target = "blend" if label == "ungated" else "final_track"
        results[target] = row
        print(f"blend[{label}]: " + " ".join(
            f"{k} {v:+.3f}" for k, v in row.items() if k.endswith("x"))
            + f" | break-even {row['breakeven_multiple_vs_ew_blend']}x "
            f"(EW blend {bench_sh['blend']:+.3f})")

    # shortlist 2x leg: variant final tracks vs deployed final track at 2x
    dep2 = ann_sharpe(blend_net(2.0))
    for name, kw in (("S1", {"c_key": "C_S1"}), ("S2", {"b_key": "B_S2"})):
        v2 = ann_sharpe(blend_net(2.0, **kw))
        results["shortlist_2x_leg"][name] = {
            "final_track_sharpe_2x": round(v2, 4),
            "deployed_final_2x": round(dep2, 4),
            "passes": bool(v2 >= dep2 - 1e-9)}
        print(f"{name} final track @2x per-line: {v2:+.4f} vs deployed "
              f"{dep2:+.4f} -> {'PASS' if v2 >= dep2 else 'FAIL'}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "description": ("Per-line spread-vector cost stress at 1x/2x/3x, "
                        "break-even cost multiples vs equal-weight "
                        "same-universe baskets (benchmark cost fixed at "
                        "1x), and the shortlist 2x-cost decision leg."),
        "window": {"start": str(start.date()), "end": str(end.date())},
        "assumption_note": ("per-line bps are stated estimates (quoted "
                            "spread + slippage, one-way); holding drags "
                            "(BTC wrapper 25 bps p.a., 159801.SZ FX/expense)"
                            " remain embedded in loader prices and are not"
                            " re-charged"),
        **results,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
