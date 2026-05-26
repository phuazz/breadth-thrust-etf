"""One-off gate check for 588200.SS (Harvest SSE STAR Chip Index ETF).

Special-case helper because 588200.SS:
  1. Returns CNY prices from yfinance — must FX-adjust to USD to compare
     against the USD-denominated Strategy C incumbents.
  2. Launched 2022, so the FX-adjusted weekly return history is short.

Methodology: download both 588200.SS (CNY) and USDCNY=X (FX), align them,
compute USD_price = CNY_price * (1 / USDCNY), then run the standard
within-strategy correlation gate + history check.
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
MIN_YEARS_HISTORY = 5
DEFAULT_START = "2018-01-01"
DEFAULT_END = date.today().isoformat()


def main() -> int:
    cand_ticker = "588200.SS"
    incumbents = list(C_UNIVERSE.keys())

    print(f"Step 1: download {cand_ticker} (CNY) + USDCNY=X (FX) ...")
    try:
        raw_cny = yf.download(cand_ticker, start=DEFAULT_START, end=DEFAULT_END,
                               auto_adjust=True, progress=False)
        if raw_cny.empty:
            print(f"  ERROR: no yfinance data for {cand_ticker}")
            return 1
        cny_close = raw_cny["Close"]
        if isinstance(cny_close, pd.DataFrame):
            cny_close = cny_close.iloc[:, 0]
        cny_close.name = cand_ticker
        print(f"  {cand_ticker} CNY close: {cny_close.index.min().date()} → "
              f"{cny_close.index.max().date()}  ({len(cny_close.dropna())} obs)")
    except Exception as exc:
        print(f"  ERROR fetching {cand_ticker}: {exc}")
        return 1

    try:
        fx_raw = yf.download("CNY=X", start=DEFAULT_START, end=DEFAULT_END,
                              auto_adjust=True, progress=False)
        fx_close = fx_raw["Close"]
        if isinstance(fx_close, pd.DataFrame):
            fx_close = fx_close.iloc[:, 0]
        fx_close.name = "USDCNY"
        # USDCNY is "how many CNY per 1 USD" → to convert CNY price to USD: divide.
        print(f"  USDCNY=X: {fx_close.index.min().date()} → "
              f"{fx_close.index.max().date()}  ({len(fx_close.dropna())} obs, "
              f"latest rate {fx_close.dropna().iloc[-1]:.4f} CNY per USD)")
    except Exception as exc:
        print(f"  ERROR fetching USDCNY=X: {exc}")
        return 1

    print("\nStep 2: align and convert to USD-denominated price series ...")
    # Convert CNY → USD: USD = CNY / (CNY per USD)
    aligned = pd.concat([cny_close, fx_close], axis=1).dropna()
    usd_price = aligned[cand_ticker] / aligned["USDCNY"]
    usd_price.name = cand_ticker
    print(f"  USD-converted: {usd_price.index.min().date()} → "
          f"{usd_price.index.max().date()}  ({len(usd_price)} obs)")
    years = (usd_price.index.max() - usd_price.index.min()).days / 365.25
    print(f"  History: {years:.2f} years (gate min = {MIN_YEARS_HISTORY}y)")
    history_passes = years >= MIN_YEARS_HISTORY
    print(f"  History gate: {'PASS' if history_passes else 'FAIL (DEFER)'}")

    print("\nStep 3: weekly returns + correlation vs current Strategy C "
          f"incumbents ({len(incumbents)}) ...")
    incumbent_raw = yf.download(incumbents, start=DEFAULT_START, end=DEFAULT_END,
                                auto_adjust=True, progress=False,
                                group_by="ticker", threads=True)
    incumbent_close = pd.DataFrame()
    for inc in incumbents:
        try:
            incumbent_close[inc] = incumbent_raw[inc]["Close"]
        except Exception:
            pass

    panel = pd.concat([usd_price, incumbent_close], axis=1)
    weekly = panel.resample("W-FRI").last()
    rets = weekly.pct_change().dropna(how="all")
    valid = rets[cand_ticker].dropna()
    print(f"  Weekly USD obs for candidate: {len(valid)}")

    corrs = []
    for inc in incumbents:
        if inc not in rets.columns:
            continue
        paired = pd.concat([rets[cand_ticker], rets[inc]], axis=1).dropna()
        if len(paired) < 26:  # need at least 6 months of overlap
            continue
        corr = paired.corr().iloc[0, 1]
        corrs.append((inc, corr))
    corrs.sort(key=lambda x: -x[1])
    if not corrs:
        print("  ERROR: no overlapping incumbent data")
        return 1
    max_corr_inc, max_corr = corrs[0]
    print(f"  Max correlation: {max_corr:+.3f} vs {max_corr_inc}")
    corr_passes = max_corr < GATE_MAX_CORR
    print(f"  Correlation gate: {'PASS' if corr_passes else f'FAIL (>= {GATE_MAX_CORR})'}")
    print(f"\n  Top-10 correlations vs incumbents:")
    for inc, corr in corrs[:10]:
        print(f"    {inc:8s}  {corr:+.3f}")

    print("\nVerdict:")
    if history_passes and corr_passes:
        print(f"  PASS — {cand_ticker} (USD-adjusted) is gate-eligible for "
              f"Strategy C deployment.")
    elif corr_passes and not history_passes:
        print(f"  DEFER — corr gate passes but history only {years:.2f}y < "
              f"{MIN_YEARS_HISTORY}y. Would need a longer-history proxy "
              f"(like BTC-USD/IBIT pattern) or wait until ~{2024 + MIN_YEARS_HISTORY}.")
    else:
        print(f"  FAIL — corr gate failed (max-corr {max_corr:.2f} vs "
              f"{max_corr_inc}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
