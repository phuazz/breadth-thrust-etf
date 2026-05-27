"""Compare alternative fallback tickers for the Phase 19 risk overlay.

The original gate uses IEF (7-10y Treasury) as the de-risk vehicle.
User flagged that IEF's ~7y duration means it can sell off alongside
equities in inflationary regimes (2022 was the canonical case). This
script tests SHY (1-3y Treasury, ~1.8y duration) as an alternative
"cleaner cash" choice and reports the resulting Sharpe / max DD trade-off
against the same gate parameters.

Reports stats for COVID Mar 2020 (deflationary stress — IEF should win)
and the 2022 inflation episode (inflationary stress — SHY should win)
separately so the regime-dependent behaviour is visible.

Usage:
    python scripts/compare_fallback_ticker.py [TICKER1 TICKER2 ...]

Default: tests IEF (incumbent), SHY, BIL.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Same gate parameters as the deployed overlay
OFF_THRESHOLD = 0.20
ON_THRESHOLD = 0.50
DERISK_FRACTION = 0.50
SWITCH_COST_BPS = 5

STRESS_WINDOWS = [
    ("COVID 2020 (deflationary stress)", "2020-02-15", "2020-05-15"),
    ("2022 inflation crash (inflationary stress)", "2022-08-01", "2022-12-31"),
    ("Full backtest", None, None),
]


def fetch_close(ticker: str, start: str, end: str) -> pd.Series:
    """Fetch close prices via yfinance. Tries asset_class cache first."""
    ac_path = DATA_DIR / "asset_class_prices_cache.parquet"
    if ac_path.exists():
        cached = pd.read_parquet(ac_path)
        if ticker in cached.columns:
            return cached[ticker].dropna()
    print(f"  Fetching {ticker} from yfinance (not in cache)...", flush=True)
    raw = yf.download(ticker, start=start, end=end, auto_adjust=True,
                       progress=False)
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return close.dropna()


def _stats(daily_ret: pd.Series, eq: pd.Series) -> dict:
    import math
    if len(eq) < 2:
        return {"sharpe": None, "cagr": None, "max_dd": None, "total": None}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    total_ret = float(eq.iloc[-1] - 1.0)
    cagr = (float(eq.iloc[-1]) ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    return {"sharpe": sharpe, "cagr": cagr,
             "max_dd": float(dd.min()), "total": total_ret}


def run_gate(blend_eq, fallback_eq, breadth, off=OFF_THRESHOLD,
              on=ON_THRESHOLD, derisk=DERISK_FRACTION,
              switch_cost_bps=SWITCH_COST_BPS):
    common = blend_eq.index
    breadth = breadth.reindex(common, method="ffill")
    fallback = fallback_eq.reindex(common, method="ffill")
    blend_ret = blend_eq.pct_change().fillna(0)
    fallback_ret = fallback.pct_change().fillna(0)
    states = []
    state = 1.0
    for v in breadth.values:
        if pd.isna(v):
            states.append(state); continue
        if state == 1.0 and v < off:
            state = 0.0
        elif state == 0.0 and v > on:
            state = 1.0
        states.append(state)
    states_lagged = pd.Series(states, index=common).shift(1).fillna(1.0)
    state_changes = states_lagged.diff().fillna(0).abs()
    switch_cost = state_changes * (switch_cost_bps / 10_000.0)
    blend_w = states_lagged + (1.0 - states_lagged) * (1.0 - derisk)
    fallback_w = (1.0 - states_lagged) * derisk
    gated_ret = blend_w * blend_ret + fallback_w * fallback_ret - switch_cost
    gated_eq = (1.0 + gated_ret).cumprod()
    return gated_eq, gated_ret


def main() -> int:
    tickers = sys.argv[1:] or ["IEF", "SHY", "BIL"]

    print("Loading deployed blend equity + CSP1 breadth ...")
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    blend = multi["strategies"]["blend_35_35_10_20"]
    blend_eq = pd.Series(blend["equity"],
                          index=pd.to_datetime(blend["dates"]))
    csp1 = json.loads((DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"])
                         ).dropna()

    # Fetch each candidate's price series with enough history
    fallback_eqs = {}
    for t in tickers:
        print(f"  Loading {t} ...")
        try:
            fallback_eqs[t] = fetch_close(t, "2018-01-01",
                                            datetime.now().strftime("%Y-%m-%d"))
        except Exception as exc:
            print(f"  ERROR fetching {t}: {exc}")

    # Stats with no gate (baseline)
    ungated_stats = _stats(blend_eq.pct_change().fillna(0), blend_eq)
    print(f"\nUNGATED BASELINE: Sharpe {ungated_stats['sharpe']:+.4f}  "
          f"CAGR {ungated_stats['cagr']*100:+.1f}%  "
          f"DD {ungated_stats['max_dd']*100:.2f}%")

    print(f"\n{'=' * 90}")
    print(f"FULL BACKTEST — gate (20%/50%/50%) with each fallback")
    print(f"{'=' * 90}")
    print(f"  {'Ticker':<6s}  {'Sharpe':>9s}  {'vs ungated':>11s}  "
          f"{'CAGR':>7s}  {'Max DD':>8s}  {'vs ungated':>11s}")
    print(f"  {'------':<6s}  {'------':>9s}  {'----------':>11s}  "
          f"{'------':>7s}  {'------':>8s}  {'----------':>11s}")
    full_stats_by_ticker = {}
    for t in tickers:
        if t not in fallback_eqs:
            continue
        gated_eq, gated_ret = run_gate(blend_eq, fallback_eqs[t], breadth)
        s = _stats(gated_ret, gated_eq)
        full_stats_by_ticker[t] = s
        d_sh = s["sharpe"] - ungated_stats["sharpe"]
        d_dd = s["max_dd"] - ungated_stats["max_dd"]
        print(f"  {t:<6s}  {s['sharpe']:>+8.4f}  {d_sh:>+10.4f}  "
              f"{s['cagr']*100:>+6.1f}%  {s['max_dd']*100:>+7.2f}%  "
              f"{d_dd*100:>+9.2f}pp")

    # Per-stress-window analysis
    for label, start, end in STRESS_WINDOWS[:2]:  # COVID + 2022
        print(f"\n{'=' * 90}")
        print(f"{label.upper()}  ({start} to {end})")
        print(f"{'=' * 90}")
        # Slice the blend + breadth + each fallback to the window
        win_blend = blend_eq.loc[start:end]
        win_breadth = breadth.loc[start:end]
        if len(win_blend) < 5:
            print(f"  Insufficient data in window")
            continue
        # Ungated baseline for this window
        win_un_stats = _stats(win_blend.pct_change().fillna(0), win_blend)
        print(f"  UNGATED          Sharpe {win_un_stats['sharpe']:+.3f}  "
              f"return {win_un_stats['total']*100:+.1f}%  "
              f"DD {win_un_stats['max_dd']*100:.2f}%")
        print(f"  {'Ticker':<8s}  {'Window Sharpe':>14s}  "
              f"{'Window return':>14s}  {'Window DD':>10s}  "
              f"{'Fallback return in window':>27s}")
        for t in tickers:
            if t not in fallback_eqs:
                continue
            # For each fallback, slice + re-run gate
            win_fallback = fallback_eqs[t].loc[start:end]
            if len(win_fallback) < 5:
                continue
            # Compute fallback's own return in window for context
            fb_total = win_fallback.iloc[-1] / win_fallback.iloc[0] - 1
            # Run gate on the FULL series then slice the gated result
            gated_eq, _ = run_gate(blend_eq, fallback_eqs[t], breadth)
            win_gated = gated_eq.loc[start:end]
            if len(win_gated) < 5:
                continue
            win_gated_ret = win_gated.pct_change().fillna(0)
            s_w = _stats(win_gated_ret, win_gated)
            print(f"  {t:<8s}  {s_w['sharpe']:>+13.3f}  "
                  f"{s_w['total']*100:>+13.1f}%  "
                  f"{s_w['max_dd']*100:>+9.2f}%  "
                  f"{fb_total*100:>+26.2f}%")

    print(f"\nDONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
