"""Idea 3 quick-test — country ETF-level momentum K=3 rotation.

Fast gate before committing to multi-hour constituent-fetching work.
Tests whether the 11-country universe has signal at the cheapest possible
level — ETF-level momentum (% above own 200d MA), same paradigm as
Strategy B. Constituent breadth would be a strictly cleaner signal, so:

  - If ETF-level standalone Sharpe > 0.7 AND adding as 5th sleeve improves
    the deployed gated blend, escalate to constituent breadth.
  - If ETF-level standalone Sharpe < 0.5 OR adding as 5th sleeve degrades
    the blend, kill — constituent breadth unlikely to flip the verdict.

Universe (user-specified, US-listed for fastest fetch):
  INDA India, EWY Korea, EWZ Brazil, EWJ Japan, EWG Germany, EWQ France,
  EWU UK, MCHI China, EWT Taiwan, EWA Australia, EWC Canada

Usage: python scripts/test_idea3_etf_momentum_quicktest.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

COUNTRY_ETFS = {
    "INDA": "MSCI India",
    "EWY":  "MSCI Korea",
    "EWZ":  "MSCI Brazil",
    "EWJ":  "MSCI Japan",
    "EWG":  "MSCI Germany",
    "EWQ":  "MSCI France",
    "EWU":  "MSCI UK",
    "MCHI": "MSCI China",
    "EWT":  "MSCI Taiwan",
    "EWA":  "MSCI Australia",
    "EWC":  "MSCI Canada",
}

MA_PERIOD = 200
SIGNAL_FLOOR = 0.0   # require positive momentum (above MA200)
COST_BPS = 5         # country ETFs are less liquid than US sectors
COST_FRAC = COST_BPS / 10_000
K = 3
REBAL_FREQ = "W-FRI"
START = "2014-01-01"

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
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


def download_country_prices() -> pd.DataFrame:
    cache = DATA_DIR / "country_etf_prices_cache.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if set(COUNTRY_ETFS.keys()).issubset(df.columns):
            stale = (pd.Timestamp.utcnow().tz_localize(None) - df.index.max()).days
            if stale <= 7:
                print(f"  Using cached country prices ({stale}d stale)")
                return df[list(COUNTRY_ETFS.keys())]
    print(f"  Downloading {len(COUNTRY_ETFS)} country ETF prices from yfinance ...",
          flush=True)
    raw = yf.download(list(COUNTRY_ETFS.keys()), start=START,
                       end=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                       auto_adjust=True, progress=False, threads=True,
                       group_by="ticker")
    closes = {}
    for t in COUNTRY_ETFS:
        if (t, "Close") in raw.columns:
            closes[t] = raw[(t, "Close")]
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index().dropna(how="all")
    df.to_parquet(cache)
    print(f"  Downloaded {df.shape[0]} dates x {df.shape[1]} ETFs")
    return df


def compute_signal(closes: pd.DataFrame) -> pd.DataFrame:
    ma = closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    return (closes - ma) / ma


def top_k_eq_weight(K: int):
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        eligible = valid[valid > SIGNAL_FLOOR]
        w = pd.Series(0.0, index=s_row.index)
        if len(eligible) == 0:
            return w  # no cash proxy — stay flat (no SHY in this test)
        top = eligible.nlargest(min(K, len(eligible)))
        invested = len(top) / K
        per = invested / len(top)
        w.loc[top.index] = per
        return w
    return f


def run_rotation(closes, signal, weight_fn, eligible_start):
    rebal_target = pd.date_range(eligible_start, closes.index[-1], freq=REBAL_FREQ)
    rebal_dates = closes.index[closes.index.isin(rebal_target)]
    rb_w = pd.DataFrame(index=rebal_dates, columns=closes.columns, dtype=float)
    for rd in rebal_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0: continue
        s_row = signal.iloc[prev_idx]
        rb_w.loc[rd] = weight_fn(s_row).reindex(closes.columns).fillna(0.0)
    w_panel = rb_w.reindex(closes.index).ffill().fillna(0.0)
    w_panel.loc[w_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (w_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = w_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    return (1.0 + port_ret).cumprod()


def main():
    print("Loading country ETF prices ...")
    closes = download_country_prices()
    closes = closes.dropna()
    if len(closes) < MA_PERIOD + 100:
        print(f"  ERROR: only {len(closes)} dates after dropna — need more history")
        return 1
    eligible = closes.index[MA_PERIOD]
    print(f"  Eligible from {eligible.date()}  ({len(closes)} dates, "
          f"{closes.shape[1]} ETFs)")

    signal = compute_signal(closes)
    eq = run_rotation(closes, signal, top_k_eq_weight(K), eligible)
    eq = eq.loc[eq.index >= eligible]

    print(f"\n=== Standalone Strategy E quick-test: K={K} weekly, "
          f"ETF-level momentum (above 200d MA) ===")
    for w_name, start, end in WINDOWS:
        s = _ws(eq, start, end)
        if s["sharpe"] is None: continue
        print(f"  {w_name:<14s}  Sharpe {s['sharpe']:+.3f}  "
              f"CAGR {s['cagr']*100:+5.1f}%  Total {s['total']*100:+6.1f}%  "
              f"DD {s['dd']*100:.1f}%")

    # Test other Ks
    print(f"\n  K sensitivity:")
    for k_test in [2, 3, 4, 5]:
        eq_k = run_rotation(closes, signal, top_k_eq_weight(k_test), eligible)
        eq_k = eq_k.loc[eq_k.index >= eligible]
        s = _ws(eq_k, None, None)
        print(f"    K={k_test}  Sharpe {s['sharpe']:+.3f}  CAGR {s['cagr']*100:+.1f}%  "
              f"DD {s['dd']*100:.1f}%")

    # Test as 5th sleeve in the 4-way blend
    print(f"\n=== Blend impact: add Strategy E as 5th sleeve at various weights ===")
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    a = pd.Series(multi["strategies"]["strategy_a"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_a"]["dates"]))
    b = pd.Series(multi["strategies"]["strategy_b"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_b"]["dates"]))
    c = pd.Series(multi["strategies"]["strategy_c"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_c"]["dates"]))
    d = pd.Series(multi["strategies"]["strategy_d"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_d"]["dates"]))
    common = a.index.intersection(b.index).intersection(c.index).intersection(d.index).intersection(eq.index)
    print(f"  Common window: {common[0].date()} -> {common[-1].date()}")
    ar = a.reindex(common).pct_change().fillna(0)
    br = b.reindex(common).pct_change().fillna(0)
    cr = c.reindex(common).pct_change().fillna(0)
    dr = d.reindex(common).pct_change().fillna(0)
    er = eq.reindex(common).pct_change().fillna(0)
    # Baseline 4-way 35/35/10/20
    base_ret = 0.35*ar + 0.35*br + 0.10*cr + 0.20*dr
    base_blend = (1.0 + base_ret).cumprod()
    base_stats = {w[0]: _ws(base_blend, w[1], w[2]) for w in WINDOWS}
    print(f"  4-way baseline 35/35/10/20:")
    for w in WINDOWS:
        s = base_stats[w[0]]
        if s["sharpe"] is None: continue
        print(f"    {w[0]:<14s}  Sharpe {s['sharpe']:+.3f}  "
              f"Total {s['total']*100:+6.1f}%  DD {s['dd']*100:.1f}%")
    # 5-way blends with E at 5%, 10%, 15% (rebal from existing sleeves proportionally)
    for e_wt in [0.05, 0.10, 0.15]:
        scale = (1.0 - e_wt) / 1.0  # rescale 4-way weights
        new_w = (0.35*scale, 0.35*scale, 0.10*scale, 0.20*scale, e_wt)
        ret5 = new_w[0]*ar + new_w[1]*br + new_w[2]*cr + new_w[3]*dr + new_w[4]*er
        blend5 = (1.0 + ret5).cumprod()
        print(f"\n  5-way blend with E={int(e_wt*100)}% "
              f"(A/B/C/D/E = "
              f"{new_w[0]:.2f}/{new_w[1]:.2f}/{new_w[2]:.2f}/{new_w[3]:.2f}/{new_w[4]:.2f}):")
        for w in WINDOWS:
            s = _ws(blend5, w[1], w[2])
            b = base_stats[w[0]]
            if s["sharpe"] is None: continue
            d_sh = s["sharpe"] - b["sharpe"]; d_tot = (s["total"] - b["total"]) * 100
            print(f"    {w[0]:<14s}  Sharpe {s['sharpe']:+.3f} (d{d_sh:+.3f})  "
                  f"Total {s['total']*100:+6.1f}% (d{d_tot:+.1f}pp)  DD {s['dd']*100:.1f}%")


if __name__ == "__main__":
    sys.exit(main())
