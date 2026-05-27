"""Idea 3 regime-conditional re-test — is country rotation a victim of
the 2014-2026 US-dominance regime?

User's hypothesis: the 2014-2026 backtest window coincides with a
decade of unprecedented US-equity / USD dominance. EM was crushed.
Country rotation may have failed not because the SIGNAL is broken but
because the UNIVERSE was deeply out of favour. If regime turns
(EM/SPY ratio mean-reverts from its 2024 multi-decade low), country
rotation may become valuable.

This test re-runs the country momentum K=3 rotation on:
  (1) Full available history per country (varies)
  (2) Pre-2014 sub-window (BRICs / EM-favoured decade)
  (3) Rolling 3-year Sharpe to see if there are ANY windows where
      country rotation works

If 2003-2014 shows Sharpe > 0.7 and 2014-2026 shows Sharpe < 0.5, that
confirms regime-conditional behaviour and supports DEFERRING (not
killing) Idea 3 with an EM-regime wake-up trigger.

Usage: python scripts/test_idea3_regime_conditional.py
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.stdout.reconfigure(encoding="utf-8")

# 9-country universe excluding INDA (2012) and MCHI (2011) — allows
# extending the backtest back to early 2000s when EM outperformed.
COUNTRY_ETFS_LONG = ["EWY", "EWZ", "EWJ", "EWG", "EWQ", "EWU", "EWT", "EWA", "EWC"]
# Full 11-country universe (matches earlier quick-test)
COUNTRY_ETFS_FULL = ["INDA", "EWY", "EWZ", "EWJ", "EWG", "EWQ", "EWU",
                      "MCHI", "EWT", "EWA", "EWC"]

MA_PERIOD = 200
COST_FRAC = 5 / 10_000
K = 3


def _stats(eq):
    if len(eq) < 5: return {"sharpe": None}
    e = eq.dropna() / eq.dropna().iloc[0]
    d = e.pct_change().fillna(0)
    n = (e.index[-1] - e.index[0]).days / 365.25
    return {
        "sharpe": d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0,
        "cagr": e.iloc[-1] ** (1/n) - 1 if n > 0 else 0,
        "total": e.iloc[-1] - 1,
        "dd": ((e - e.cummax()) / e.cummax()).min(),
    }


def fetch(tickers, start, cache_name):
    cache = DATA_DIR / cache_name
    if cache.exists():
        df = pd.read_parquet(cache)
        if set(tickers).issubset(df.columns):
            stale = (pd.Timestamp.utcnow().tz_localize(None) - df.index.max()).days
            if stale <= 7:
                print(f"  Using cached {cache_name} ({stale}d stale)")
                return df[tickers]
    print(f"  Downloading {len(tickers)} tickers from {start} ...", flush=True)
    raw = yf.download(tickers, start=start, auto_adjust=True, progress=False,
                       threads=True, group_by="ticker")
    closes = {t: raw[(t, "Close")] for t in tickers if (t, "Close") in raw.columns}
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index().dropna(how="all")
    df.to_parquet(cache)
    return df


def run_rotation_full(closes, K=3, ma=MA_PERIOD):
    """Run K=3 weekly Fri rotation with simple top-K positive-momentum picker."""
    closes = closes.dropna()  # require all tickers have data
    if len(closes) < ma + 50:
        return None
    eligible = closes.index[ma]
    ma_panel = closes.rolling(ma, min_periods=ma).mean()
    signal = (closes - ma_panel) / ma_panel
    rebal = pd.date_range(eligible, closes.index[-1], freq="W-FRI")
    rebal = closes.index[closes.index.isin(rebal)]
    w_panel = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for rd in rebal:
        pi = closes.index.get_loc(rd) - 1
        if pi < 0: continue
        s = signal.iloc[pi].dropna()
        elig = s[s > 0]
        if len(elig) == 0: continue
        top = elig.nlargest(min(K, len(elig)))
        invested = len(top) / K
        per = invested / len(top)
        w_panel.loc[rd, top.index] = per
    w_panel = w_panel.reindex(closes.index).ffill().fillna(0.0)
    w_panel.loc[w_panel.index < eligible] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (w_panel.shift(1).fillna(0) * rets).sum(axis=1)
    to = w_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - to * COST_FRAC
    return (1.0 + port_ret).cumprod()


def main():
    print("=== Test 1: 9-country universe (excl INDA, MCHI), 2003-2026 ===")
    closes_long = fetch(COUNTRY_ETFS_LONG, "2003-01-01",
                          "country_etf_prices_9_cache.parquet")
    closes_long = closes_long.dropna()
    print(f"  Common dates: {closes_long.index[0].date()} -> "
          f"{closes_long.index[-1].date()}  ({len(closes_long)} days)")
    eq_long = run_rotation_full(closes_long, K=3)
    if eq_long is None:
        print("  ERROR: insufficient data")
        return 1
    eligible = closes_long.index[MA_PERIOD]
    eq_long = eq_long.loc[eq_long.index >= eligible]

    # Sub-windows
    windows = [
        ("Full backtest",     None,         None),
        ("EM-favoured 03-10", "2003-01-01", "2010-12-31"),
        ("Transition 11-13",  "2011-01-01", "2013-12-31"),
        ("US-dominant 14-21", "2014-01-01", "2021-12-31"),
        ("Recent 22-now",     "2022-01-01", None),
    ]
    print(f"\n  Window stats (9 countries, K=3 weekly):")
    print(f"  {'Window':<22s}  {'Sharpe':>7s}  {'CAGR':>7s}  {'Total':>8s}  {'MaxDD':>7s}")
    for name, start, end in windows:
        s = _stats(eq_long.loc[start:end] if (start or end) else eq_long)
        if s["sharpe"] is None: continue
        print(f"  {name:<22s}  {s['sharpe']:>+6.3f}  {s['cagr']*100:>+5.1f}%  "
              f"{s['total']*100:>+7.1f}%  {s['dd']*100:>+6.1f}%")

    # Rolling 3y Sharpe to see if any window worked
    print(f"\n  Rolling 3-year Sharpe (every 6 months, K=3 weekly):")
    print(f"  {'window':<26s}  {'Sharpe':>7s}  {'CAGR':>7s}")
    starts = pd.date_range("2003-06-01", "2023-06-01", freq="6MS")
    for start_dt in starts:
        end_dt = start_dt + pd.DateOffset(years=3)
        if end_dt > eq_long.index[-1]: break
        seg = eq_long.loc[start_dt:end_dt]
        s = _stats(seg)
        if s["sharpe"] is None: continue
        print(f"  {start_dt.date()} -> {end_dt.date()}  {s['sharpe']:>+6.3f}  "
              f"{s['cagr']*100:>+5.1f}%")

    # EEM/SPY relative-strength context
    print(f"\n=== EM regime context (EEM/SPY ratio) ===")
    rel = fetch(["EEM", "SPY"], "2003-01-01", "em_regime_context.parquet")
    rel = rel.dropna()
    ratio = rel["EEM"] / rel["SPY"]
    ratio = ratio / ratio.iloc[0] * 100  # rebase to 100 at start
    print(f"  EEM/SPY ratio (rebased to 100 at 2003-04):")
    for yr in [2003, 2007, 2010, 2014, 2018, 2020, 2022, 2024, 2026]:
        try:
            v = ratio[ratio.index.year == yr].iloc[0]
            print(f"    {yr}: {v:6.1f}")
        except IndexError:
            pass
    print(f"  Current vs peak: {ratio.iloc[-1] / ratio.max() * 100:.1f}% of peak")
    print(f"  Current vs trough: {ratio.iloc[-1] / ratio.min() * 100:.1f}% of trough")

    return 0


if __name__ == "__main__":
    sys.exit(main())
