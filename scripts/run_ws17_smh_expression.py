"""WS17 H2b — SMH perp expression economics under the frozen funding band.

Reads the SMH-vehicle OOS run (data/ws17_soxx_smh.json — the filed SOXX
composite config traded on SMH), then applies the pre-registered perp-vs-ETF
per-trade delta: (funding_band + measured SMH trailing-12m dividend yield)
x in-trade days / 365. Fees are already charged in-engine at 10bp round trip
and held EQUAL across vehicles (conservative: growth-mode HIP-3 fees are
lower and are not credited). Bands frozen at {0, +3, +6} %/yr in
reviews/2026-08-16_ws17_hl-perp-expression.md, with the verdict rule:
KEEP-for-shadow if net Sharpe >= +0.40 AND net total return > 0 across the
ENTIRE band; INCONCLUSIVE if it clears at 0-3% only; REJECT otherwise.

The MC percentile is invariant to a uniform per-day drag (strategy and
cost-matched null shift together), so this script reports absolute economics
only and quotes the gross MC percentile from the underlying run.

Run: python scripts/run_ws17_smh_expression.py -> data/ws17_smh_expression.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

FOCUS_VARIANT = "regime_time_only_delay5_trend"
FUNDING_BANDS = [0.0, 0.03, 0.06]      # frozen; annualised long drag
KEEP_SHARPE_BAR = 0.40                 # frozen
TRADING_DAYS = 252


def measure_div_yield() -> tuple[float, str]:
    """SMH trailing-12m cash dividends / last close, from yfinance history."""
    t = yf.Ticker("SMH")
    divs = t.dividends
    hist = t.history(period="1y", auto_adjust=False)
    if divs is None or len(divs) == 0 or hist is None or len(hist) == 0:
        return 0.006, "FALLBACK 0.6%/yr (dividend history unavailable)"
    cutoff = pd.Timestamp.now(tz=divs.index.tz) - pd.Timedelta(days=365)
    ttm = float(divs[divs.index >= cutoff].sum())
    last = float(hist["Close"].iloc[-1])
    y = ttm / last
    return y, f"measured: TTM dividends {ttm:.4f} / close {last:.2f}"


def stats_from_daily(daily: pd.Series) -> dict:
    eq = (1.0 + daily).cumprod()
    total = float(eq.iloc[-1] - 1.0)
    dd = float((eq / eq.cummax() - 1.0).min())
    mu, sd = daily.mean(), daily.std()
    sharpe = float(mu / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    return {"total_return": round(total, 4), "max_dd": round(dd, 4),
            "sharpe": round(sharpe, 3)}


def main() -> int:
    src = json.loads((DATA_DIR / "ws17_soxx_smh.json").read_text(encoding="utf-8"))
    var = src["variants"][FOCUS_VARIANT]
    trades = var["trades"]
    ec = var["equity_curve"]
    dates = pd.to_datetime(ec["dates"])
    strat_eq = pd.Series(ec["strategy"], index=dates, dtype=float)
    daily = strat_eq.pct_change().fillna(0.0)

    # In-trade mask from the trade log (entry day excluded — the engine's entry
    # day return runs from the cost-adjusted entry price to the close, and the
    # drag applies per day HELD; one-day precision is immaterial at 44d median
    # holds and is applied identically across bands).
    in_trade = pd.Series(False, index=dates)
    for t in trades:
        in_trade[(dates > pd.Timestamp(t["entry_date"])) &
                 (dates <= pd.Timestamp(t["exit_date"]))] = True
    n_in_trade = int(in_trade.sum())

    div_yield, div_note = measure_div_yield()

    bands_out = {}
    for band in FUNDING_BANDS:
        drag_daily = (band + div_yield) / 365.0
        adj = daily.copy()
        adj[in_trade] = adj[in_trade] - drag_daily
        s = stats_from_daily(adj)
        s["annual_drag_pct"] = round((band + div_yield) * 100, 2)
        bands_out[f"{band:.0%}"] = s

    gross = stats_from_daily(daily)
    worst = bands_out[f"{FUNDING_BANDS[-1]:.0%}"]
    mid = bands_out[f"{FUNDING_BANDS[1]:.0%}"]
    zero = bands_out[f"{FUNDING_BANDS[0]:.0%}"]

    def clears(s):
        return s["sharpe"] >= KEEP_SHARPE_BAR and s["total_return"] > 0

    if clears(worst) and clears(mid) and clears(zero):
        verdict = "KEEP-for-shadow"
    elif clears(zero) and clears(mid):
        verdict = "INCONCLUSIVE (clears at 0-3% band only)"
    else:
        verdict = "REJECT"

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_run": "data/ws17_soxx_smh.json",
        "focus_variant": FOCUS_VARIANT,
        "n_trades": len(trades),
        "n_in_trade_days": n_in_trade,
        "n_total_days": int(len(dates)),
        "smh_div_yield_ttm": round(div_yield, 5),
        "smh_div_yield_note": div_note,
        "gross_mc_percentile": var["monte_carlo_null"].get("strategy_total_return_percentile"),
        "gross": gross,
        "bands": bands_out,
        "verdict_rule": (f"KEEP-for-shadow if net Sharpe >= {KEEP_SHARPE_BAR} and net total "
                         "return > 0 across the ENTIRE band; INCONCLUSIVE if 0-3% only; else REJECT"),
        "verdict": verdict,
    }
    out = DATA_DIR / "ws17_smh_expression.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"SMH vehicle, {FOCUS_VARIANT}: {len(trades)} trades, "
          f"{n_in_trade}/{len(dates)} days in trade ({n_in_trade/len(dates):.0%})")
    print(f"Dividend yield (TTM): {div_yield:.2%}  [{div_note}]")
    print(f"Gross:   Sharpe {gross['sharpe']:+.2f}  total {gross['total_return']:+.1%}  "
          f"MaxDD {gross['max_dd']:.1%}  (MC pct {payload['gross_mc_percentile']})")
    for k, s in bands_out.items():
        print(f"Band {k:>3} funding (total drag {s['annual_drag_pct']:.2f}%/yr): "
              f"Sharpe {s['sharpe']:+.2f}  total {s['total_return']:+.1%}  MaxDD {s['max_dd']:.1%}")
    print(f"VERDICT: {verdict}")
    print(f"Wrote {out.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
