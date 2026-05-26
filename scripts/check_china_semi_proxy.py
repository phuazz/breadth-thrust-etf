"""Test 512760.SS as a longer-history proxy for 588200.SS.

The goal: validate whether 512760.SS (Guotai CES Semiconductor Industry
ETF, launched 2020-01) is a faithful enough proxy for 588200.SS
(Harvest SSE STAR Chip Index ETF, launched 2022-09) to serve as the
pre-2022 backtest source, with live execution rolling into 588200.SS
once it exists. Same pattern as BTC-USD / IBIT.

Tests:
  1. 512760.SS USD-adjusted history length and gate vs current C universe
  2. Correlation between 512760.SS and 588200.SS in their overlap
     window — needs to be > 0.85 for the splice to be honest
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from run_thematic_rotation import UNIVERSE as C_UNIVERSE  # noqa: E402

GATE_MAX_CORR = 0.85
MIN_PROXY_CORR = 0.85
MIN_YEARS_HISTORY = 5
DEFAULT_START = "2018-01-01"
DEFAULT_END = date.today().isoformat()


def fetch_usd_close(ticker: str, start: str, end: str) -> pd.Series:
    raw = yf.download(ticker, start=start, end=end, auto_adjust=True,
                       progress=False)
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    fx = yf.download("CNY=X", start=start, end=end, auto_adjust=True,
                      progress=False)["Close"]
    if isinstance(fx, pd.DataFrame):
        fx = fx.iloc[:, 0]
    aligned = pd.concat([close, fx], axis=1, sort=True).dropna()
    aligned.columns = ["cny_price", "usdcny"]
    usd_price = aligned["cny_price"] / aligned["usdcny"]
    usd_price.name = ticker
    return usd_price


def main() -> int:
    proxy = sys.argv[1] if len(sys.argv) > 1 else "512760.SS"
    live = "588200.SS"
    incumbents = list(C_UNIVERSE.keys())

    print(f"Step 1: fetch {proxy} and {live} both in USD ...")
    proxy_usd = fetch_usd_close(proxy, DEFAULT_START, DEFAULT_END)
    live_usd = fetch_usd_close(live, DEFAULT_START, DEFAULT_END)
    print(f"  {proxy}: {proxy_usd.index.min().date()} → "
          f"{proxy_usd.index.max().date()}  ({len(proxy_usd)} obs)")
    print(f"  {live}:  {live_usd.index.min().date()} → "
          f"{live_usd.index.max().date()}  ({len(live_usd)} obs)")
    years_proxy = (proxy_usd.index.max() - proxy_usd.index.min()).days / 365.25
    print(f"  {proxy} history: {years_proxy:.2f} years "
          f"(gate min = {MIN_YEARS_HISTORY}y)")

    print(f"\nStep 2: proxy-fidelity check — corr({proxy}, {live}) in overlap ...")
    proxy_weekly = proxy_usd.resample("W-FRI").last()
    live_weekly = live_usd.resample("W-FRI").last()
    proxy_ret = proxy_weekly.pct_change().dropna()
    live_ret = live_weekly.pct_change().dropna()
    overlap = pd.concat([proxy_ret, live_ret], axis=1, sort=True).dropna()
    overlap.columns = [proxy, live]
    if len(overlap) < 26:
        print(f"  ERROR: only {len(overlap)} weekly overlap obs")
        return 1
    proxy_corr = overlap.corr().iloc[0, 1]
    print(f"  Overlap window: {overlap.index.min().date()} → "
          f"{overlap.index.max().date()}  ({len(overlap)} weekly obs)")
    print(f"  Correlation: {proxy_corr:+.3f}  "
          f"(need >= {MIN_PROXY_CORR} for an honest splice)")
    proxy_fidelity_ok = proxy_corr >= MIN_PROXY_CORR
    print(f"  Proxy fidelity: "
          f"{'PASS' if proxy_fidelity_ok else f'FAIL (< {MIN_PROXY_CORR})'}")

    print(f"\nStep 3: incumbent gate — corr({proxy}, each C incumbent) ...")
    incumbent_raw = yf.download(incumbents, start=DEFAULT_START, end=DEFAULT_END,
                                 auto_adjust=True, progress=False,
                                 group_by="ticker", threads=True)
    incumbent_close = pd.DataFrame()
    for inc in incumbents:
        try:
            incumbent_close[inc] = incumbent_raw[inc]["Close"]
        except Exception:
            pass
    panel = pd.concat([proxy_usd, incumbent_close], axis=1, sort=True)
    weekly = panel.resample("W-FRI").last()
    rets = weekly.pct_change().dropna(how="all")
    corrs = []
    for inc in incumbents:
        if inc not in rets.columns:
            continue
        paired = pd.concat([rets[proxy], rets[inc]], axis=1, sort=True).dropna()
        if len(paired) < 26:
            continue
        corrs.append((inc, paired.corr().iloc[0, 1]))
    corrs.sort(key=lambda x: -x[1])
    max_inc, max_corr = corrs[0]
    incumbent_gate_ok = max_corr < GATE_MAX_CORR
    print(f"  Max corr: {max_corr:+.3f} vs {max_inc}")
    print(f"  Incumbent gate: "
          f"{'PASS' if incumbent_gate_ok else f'FAIL (>= {GATE_MAX_CORR})'}")
    print(f"  Top-10:")
    for inc, c in corrs[:10]:
        print(f"    {inc:8s}  {c:+.3f}")

    print("\nVerdict:")
    history_ok = years_proxy >= MIN_YEARS_HISTORY
    if history_ok and incumbent_gate_ok and proxy_fidelity_ok:
        print(f"  PASS — {proxy} (USD-adjusted) is a usable BACKTEST proxy "
              f"for {live}. Splice methodology like BTC-USD/IBIT viable.")
    else:
        reasons = []
        if not history_ok:
            reasons.append(f"history {years_proxy:.2f}y < {MIN_YEARS_HISTORY}y")
        if not incumbent_gate_ok:
            reasons.append(f"max-corr {max_corr:.2f} vs {max_inc} >= "
                          f"{GATE_MAX_CORR}")
        if not proxy_fidelity_ok:
            reasons.append(f"proxy corr to {live} = {proxy_corr:.2f} < "
                          f"{MIN_PROXY_CORR}")
        print(f"  FAIL — {', '.join(reasons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
