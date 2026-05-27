"""Idea 4 — Constituent breadth confirmation filter on Strategy C.

Strategy C's documented weakness: fad-chasing degrades walk-forward
Sharpe (+0.71 IS -> +0.39 WF — the largest IS-vs-OOS gap of any sleeve).
Mechanism: the ETF-level momentum signal can stay positive while the
underlying theme is internally rolling over (insiders / sophisticated
holders exit), then the fad collapses (e.g. ARKK 2021-22 unwind).

Idea 4 adds a BREADTH CONFIRMATION FILTER: only buy a thematic if
BOTH (ETF momentum > SIGNAL_FLOOR) AND (proxy sector breadth >
BREADTH_FLOOR). If breadth is collapsing in the proxy sector, skip
the thematic even if its own ETF price still shows momentum.

Implementation uses SECTOR-PROXY breadth (using existing per-sector
constituent breadth from Strategy A) rather than true constituent
breadth for each thematic. The mapping:

  Tech / Innovation     -> IUIT  (US Tech sector breadth)
  Energy / Climate      -> IUES  (US Energy sector breadth) — or IUMS
                                  for battery/mining (LIT, URA)
  Health / Bio          -> IUHC  (US Healthcare sector breadth)
  Cyclical thematic     -> IUCD  (US Consumer Discretionary) for JETS;
                            IUIS for PAVE, ITA
  Commodity equity      -> IUMS  (US Materials sector breadth)
  China / EM Tech       -> CSP1  (broad market — no clean sector match)
  Crypto                -> CSP1  (broad market — risk-on regime check)

If the sector-proxy result has signal, escalate to true constituent
breadth in a follow-up. If it doesn't, kill the idea.

Usage:
    python scripts/test_idea4_breadth_filter.py [--floor PCT]

Sweeps the breadth floor across {30, 40, 50, 60, 70}% and reports the
A/B comparison.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import run_thematic_rotation as C  # noqa: E402
from run_ma200_sweep import (  # noqa: E402
    load_constituent_prices, compute_ma200_breadth, MA_PERIOD,
)

# Map each thematic ETF to its breadth proxy. The mapping uses Strategy
# A's existing per-sector constituent breadth where there's a clean match
# (most cases) and falls back to broad CSP1 where the theme spans
# sectors or has no clean US-sector parent (China, crypto).
THEME_TO_PROXY = {
    # Tech / Innovation (mostly IT exposure)
    "ARKK": "CNDX",   # ARK Innovation — broad NASDAQ-100 (innovation tilt)
    "CIBR": "IUIT",   # Cybersecurity — pure tech
    "SKYY": "IUIT",   # Cloud — pure tech
    "BOTZ": "IUIT",   # Robotics + AI — tech
    "BLOK": "IUIT",   # Blockchain — tech-adjacent
    # Energy / Climate
    "ICLN": "IUES",   # Clean energy — US energy sector proxy
    "TAN":  "IUES",   # Solar
    "LIT":  "IUMS",   # Lithium/batteries — materials (mining-heavy)
    "URA":  "IUMS",   # Uranium miners — materials
    # Health / Bio
    "XBI":  "IUHC",   # Biotech
    "ARKG": "IUHC",   # Genomics
    # Cyclical thematic
    "JETS": "IUCD",   # Airlines — consumer discretionary
    "PAVE": "IUIS",   # US infrastructure — industrials
    "ITA":  "IUIS",   # Aerospace & defence — industrials
    # Commodity equity
    "GDX":  "IUMS",   # Gold miners — materials
    "COPX": "IUMS",   # Copper miners
    "MOO":  "IUCS",   # Agribusiness — consumer staples (food)
    "XME":  "IUMS",   # Metals & mining
    "WOOD": "IUMS",   # Timber
    "REMX": "IUMS",   # Rare earth
    # China / EM Tech — no clean US-sector parent; use CSP1 broad as
    # "is the market risk-on" filter
    "CQQQ":      "CSP1",
    "159801.SZ": "CSP1",
    # Crypto — same logic
    "BTC-USD":   "CSP1",
}

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-2024",    "2022-01-01", "2024-12-31"),
    ("2022-onwards", "2022-01-01", None),
]


def _stats(eq: pd.Series) -> dict:
    if len(eq) < 5:
        return {"sharpe": None, "cagr": None, "total": None, "dd": None}
    e = eq.dropna() / eq.dropna().iloc[0]
    d = e.pct_change().fillna(0)
    n = (e.index[-1] - e.index[0]).days / 365.25
    return {
        "sharpe": d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0,
        "cagr": e.iloc[-1] ** (1 / n) - 1 if n > 0 else 0,
        "total": e.iloc[-1] - 1,
        "dd": ((e - e.cummax()) / e.cummax()).min(),
    }


def _ws(eq, start, end):
    w = eq.loc[start:end] if (start or end) else eq
    return _stats(w)


def _load_all_proxy_breadths(proxies: set[str]) -> pd.DataFrame:
    """For each unique proxy ETF, load its constituent panel and compute
    MA200 breadth. Returns a DataFrame indexed by date, columns = proxy
    ETF tickers, values = breadth fraction (0-1)."""
    out = {}
    for p in proxies:
        try:
            constituents = load_constituent_prices(p)
        except FileNotFoundError:
            print(f"  WARN: no constituent cache for proxy {p}")
            continue
        breadth = compute_ma200_breadth(constituents, MA_PERIOD)
        out[p] = breadth
    return pd.DataFrame(out).sort_index()


def make_filtered_weight_fn(K: int, proxy_breadth_panel: pd.DataFrame,
                              breadth_floor: float):
    """Wrap C's top_k_equal_weight to filter by proxy breadth at each
    decision point. proxy_breadth_panel is reindexed by date externally
    so that, given a row index, we can look up the right proxy values.
    """
    base_fn = C.top_k_equal_weight(K)
    def f(s_row: pd.Series, decision_date) -> pd.Series:
        # First check which ETFs have proxy breadth >= floor at decision_date
        # Use ffill to handle date misalignment between thematic and proxy
        ok_etfs = []
        for etf in s_row.index:
            proxy = THEME_TO_PROXY.get(etf)
            if proxy is None or proxy not in proxy_breadth_panel.columns:
                # No proxy mapped — let the ETF pass through unchanged
                ok_etfs.append(etf)
                continue
            # Look up proxy breadth at decision_date (or last known before)
            proxy_series = proxy_breadth_panel[proxy].loc[:decision_date]
            if len(proxy_series) == 0 or pd.isna(proxy_series.iloc[-1]):
                # No proxy data available yet — let pass to avoid losing
                # signal during warm-up. Could also be more conservative
                # and reject; tested both, leaving permissive for now.
                ok_etfs.append(etf)
                continue
            if proxy_series.iloc[-1] >= breadth_floor:
                ok_etfs.append(etf)
        # Mask out the ETFs that fail the proxy filter — set their signal
        # to NaN so the eligibility filter drops them
        filtered_signal = s_row.copy()
        for etf in s_row.index:
            if etf not in ok_etfs:
                filtered_signal[etf] = np.nan
        return base_fn(filtered_signal)
    return f


def run_filtered_rotation(closes, signal, proxy_breadth_panel,
                            breadth_floor, K, eligible_start,
                            rebalance_freq="W-FRI"):
    """Replicate C.run_rotation but with the proxy-breadth filter applied
    at each rebalance decision point."""
    rebalance_dates_target = pd.date_range(eligible_start, closes.index[-1],
                                             freq=rebalance_freq)
    rebalance_dates = closes.index[closes.index.isin(rebalance_dates_target)]
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)
    weight_fn = make_filtered_weight_fn(K, proxy_breadth_panel, breadth_floor)
    for rd in rebalance_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0:
            continue
        s_row = signal.iloc[prev_idx]
        decision_date = closes.index[prev_idx]
        rb_weights.loc[rd] = weight_fn(s_row, decision_date).reindex(closes.columns).fillna(0.0)
    weight_panel = rb_weights.reindex(closes.index).ffill().fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    cost = C.COST_FRAC
    port_ret = port_ret - turnover * cost
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel, "turnover": turnover}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--floors", default="30,40,50,60,70",
                          help="Comma-separated breadth floor pct values to sweep")
    args = parser.parse_args()
    floors = [int(f) / 100.0 for f in args.floors.split(",")]

    print("Loading C panel + signal ...")
    closes = C.download_prices()
    closes = closes.dropna(axis=1, how="all")
    # Eligible start logic from C.main
    late = {t for t, m in C.UNIVERSE.items()
            if m.get("late_inception") and t in closes.columns}
    core_first = {col: closes[col].first_valid_index()
                   for col in closes.columns if col not in late}
    latest = max(d for d in core_first.values() if d is not None)
    eligible_idx = closes.index.searchsorted(latest) + C.MA_PERIOD
    eligible = closes.index[eligible_idx]
    print(f"  Eligible start: {eligible.date()}")
    signal = C.compute_signal(closes)

    # Load proxy breadths
    proxies_needed = set(THEME_TO_PROXY[t] for t in closes.columns
                          if t in THEME_TO_PROXY)
    print(f"\nLoading proxy breadths for {len(proxies_needed)} sector ETFs: "
          f"{sorted(proxies_needed)}")
    proxy_breadths = _load_all_proxy_breadths(proxies_needed)
    print(f"  Proxy panel: {proxy_breadths.shape[0]} dates x "
          f"{proxy_breadths.shape[1]} sectors")
    # Print mean breadth per proxy (sanity check)
    print(f"  Mean breadth per proxy:")
    for col in proxy_breadths.columns:
        v = proxy_breadths[col].mean()
        print(f"    {col}  mean {v*100:.1f}%")

    # Baseline: no filter
    print(f"\nBASELINE (no breadth filter, K=4 weekly Fri):")
    base_r = C.run_rotation(closes, signal, C.WEIGHTER_FACTORY(4),
                              eligible, rebalance_freq="W-FRI")
    base_eq = base_r["equity"].loc[base_r["equity"].index >= eligible]
    for w, start, end in WINDOWS:
        s = _ws(base_eq, start, end)
        if s["sharpe"] is None: continue
        print(f"  {w:<14s}  Sharpe {s['sharpe']:+.3f}  "
              f"CAGR {s['cagr']*100:+5.1f}%  Total {s['total']*100:+6.1f}%  "
              f"DD {s['dd']*100:.1f}%")

    # Sweep filter floor
    print(f"\n=== Floor sweep ===")
    print(f"  {'floor':<6s}  " + "  ".join(f"{w[0]:<26s}" for w in WINDOWS))
    print(f"  {'-----':<6s}  " + "  ".join('-' * 26 for _ in WINDOWS))
    base_stats = {w[0]: _ws(base_eq, w[1], w[2]) for w in WINDOWS}
    print(f"  {'NONE':<6s}  " + "  ".join(
        f"Sh{base_stats[w[0]]['sharpe']:+.2f} Tot{base_stats[w[0]]['total']*100:+5.1f}%"
        for w in WINDOWS))
    rows = []
    for floor in floors:
        try:
            r = run_filtered_rotation(closes, signal, proxy_breadths,
                                       floor, 4, eligible)
            eq = r["equity"].loc[r["equity"].index >= eligible]
            stats_per_w = {w[0]: _ws(eq, w[1], w[2]) for w in WINDOWS}
            cells = []
            for w in WINDOWS:
                s = stats_per_w[w[0]]
                b = base_stats[w[0]]
                if s["sharpe"] is None or b["sharpe"] is None:
                    cells.append("n/a")
                    continue
                d_sh = s["sharpe"] - b["sharpe"]
                d_tot = (s["total"] - b["total"]) * 100
                cells.append(f"Sh{s['sharpe']:+.2f}({d_sh:+.2f}) "
                              f"Tot{s['total']*100:+5.1f}%({d_tot:+.1f}pp)")
            print(f"  {int(floor*100):>4d}%  " + "  ".join(c.ljust(26) for c in cells))
            rows.append({"floor": floor, "stats": stats_per_w, "equity": eq})
        except Exception as exc:
            print(f"  {int(floor*100):>4d}%  ERROR: {exc}")

    print(f"\nDONE — interpret 2022-onwards Total Δ as the headline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
