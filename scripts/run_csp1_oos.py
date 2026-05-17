"""Item 5 — true cross-ETF OOS validation on the S&P 500 (CSP1 / SPY).

Applies the configuration that won the SOXX in-sample split-half test
(regime_time_only + delay 5d + trend filter) to CSP1 (iShares Core S&P 500
UCITS) breadth signals WITHOUT any re-tuning. Trades the SPY ETF for OHLC
and stops (same constituents as CSP1, but US-listed in USD).

Also runs the baseline_2xATR config and the regime_time_only config for
comparison, so we can see whether the SOXX-tuned parameters transfer.

Output:
  - data/backtest_csp1_oos.json

Run:
    python scripts/run_csp1_oos.py
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import (  # noqa: E402
    DEFAULT_CONFIG,
    aggregate_stats,
    build_daily_returns,
    download_soxx_ohlc,
    download_spy_close,
    load_breadth,
    mechanism_diagnostic,
    monte_carlo_null,
    run_strategy,
    HORIZONS,
)

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = PROJECT_ROOT / "data" / "backtest_csp1_oos.json"


# Three configs to evaluate on CSP1:
#   1. baseline_2xATR — original specification
#   2. regime_time_only — SOXX exit-logic winner
#   3. regime_time_only + delay 5d + trend — SOXX split-half winner (true OOS test)
CONFIGS: dict[str, dict] = {
    "baseline_2xATR": {**DEFAULT_CONFIG},
    "regime_time_only": {**DEFAULT_CONFIG, "trailing_stop_k": None},
    "regime_time_only_delay5_trend": {
        **DEFAULT_CONFIG,
        "trailing_stop_k": None,
        "entry_delay_bars": 5,
        "use_trend_filter": True,
    },
}


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _summarise(label: str, stats: dict, mc: dict) -> dict:
    return {
        "variant": label,
        "n_trades": stats.get("n_trades"),
        "win_rate": _safe(stats.get("win_rate")),
        "mean_trade_return": _safe(stats.get("mean_trade_return")),
        "median_holding_days": _safe(stats.get("median_holding_days")),
        "equity_curve_total_return": _safe(stats.get("equity_curve_total_return")),
        "equity_curve_max_dd": _safe(stats.get("equity_curve_max_dd")),
        "sharpe_annualised": _safe(stats.get("sharpe_annualised")),
        "sortino_annualised": _safe(stats.get("sortino_annualised")),
        "mc_strategy_total_return_percentile": _safe(mc.get("strategy_total_return_percentile")),
        "mc_strategy_win_rate_percentile": _safe(mc.get("strategy_win_rate_percentile")),
        "mc_strategy_mean_trade_return_percentile": _safe(mc.get("strategy_mean_trade_return_percentile")),
        "mc_null_total_return_p50": _safe(mc.get("null_total_return_p50")),
    }


def _clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, list):
        return [_clean(x) for x in o]
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    return o


def main() -> int:
    print("Loading CSP1 breadth signal ...", flush=True)
    breadth, signal_records = load_breadth(etf="CSP1")
    signal_dates = [s["date"] for s in signal_records]
    print(f"  Breadth covers {breadth.index[0].date()} -> {breadth.index[-1].date()}, "
          f"{len(breadth)} trading days; {len(signal_dates)} signal-fire days")

    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    # Use SPY (US-listed, USD) as the tradable proxy for the S&P 500 — CSP1 is
    # UCITS GBP-denominated, less convenient for backtest comparison.
    print("Downloading SPY OHLC and SPY close ...", flush=True)
    spy_ohlc = download_soxx_ohlc(dl_start, dl_end, etf="SPY", yf_symbol="SPY")
    spy_close = download_spy_close(dl_start, dl_end)
    spy_ohlc = spy_ohlc[~spy_ohlc.index.duplicated(keep="first")]
    eligible_start = breadth.index[252] if len(breadth) > 252 else breadth.index[0]

    diag = mechanism_diagnostic(signal_dates, spy_ohlc, HORIZONS)

    rows = []
    full = {}
    for label, cfg in CONFIGS.items():
        print(f"\n[{label}]")
        trades = run_strategy(signal_dates, spy_ohlc, breadth, config=cfg)
        daily_returns = build_daily_returns(trades, spy_ohlc)
        stats = aggregate_stats(trades, daily_returns)
        mc = monte_carlo_null(trades, spy_ohlc, eligible_start)
        print(f"  n={stats.get('n_trades',0):>3}  "
              f"win={(stats.get('win_rate') or 0):.1%}  "
              f"medHold={(stats.get('median_holding_days') or 0):>3.0f}d  "
              f"totRet={(stats.get('equity_curve_total_return') or 0):+.1%}  "
              f"maxDD={(stats.get('equity_curve_max_dd') or 0):.1%}  "
              f"Shp={(stats.get('sharpe_annualised') or 0):+.2f}  "
              f"MC%={(mc.get('strategy_total_return_percentile') or 0):.1f}")
        rows.append(_summarise(label, stats, mc))
        full[label] = {
            "config": cfg,
            "trades": [asdict(t) for t in trades],
            "primary": stats,
            "monte_carlo_null": mc,
        }

    payload = {
        "etf_breadth_source": "CSP1",
        "etf_traded": "SPY",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "breadth_source_file": "data/breadth_csp1.json",
        "n_signal_fire_days": len(signal_dates),
        "mechanism_diagnostic": diag,
        "summary_table": rows,
        "variants": full,
        "note": (
            "TRUE CROSS-ETF OOS validation. The 'regime_time_only_delay5_trend' "
            "config was chosen on SOXX 2019-2022 train half and validated on "
            "SOXX 2022-2026 test half (see oos_split_half_soxx.json). Here it "
            "is applied to S&P 500 (CSP1 constituents, SPY OHLC) over the same "
            "2018-2026 window WITHOUT re-tuning. baseline_2xATR and "
            "regime_time_only are included for reference."
        ),
    }
    OUT_PATH.write_text(json.dumps(_clean(payload), indent=2), encoding="utf-8")

    print()
    print("=" * 110)
    print("CSP1 (S&P 500) CROSS-ETF OOS — SOXX-tuned config applied without re-tuning")
    print("=" * 110)
    print(f"{'variant':<38} {'n':>3} {'win%':>6} {'medH':>5} {'totRet%':>9} "
          f"{'maxDD%':>7} {'Sharpe':>7} {'MC%':>6}")
    print("-" * 110)
    for r in rows:
        print(f"{r['variant']:<38} "
              f"{r['n_trades'] or 0:>3} "
              f"{(r['win_rate'] or 0)*100:>6.1f} "
              f"{(r['median_holding_days'] or 0):>5.0f} "
              f"{(r['equity_curve_total_return'] or 0)*100:>+9.1f} "
              f"{(r['equity_curve_max_dd'] or 0)*100:>7.1f} "
              f"{(r['sharpe_annualised'] or 0):>+7.2f} "
              f"{(r['mc_strategy_total_return_percentile'] or 0):>6.1f}")
    print()
    print(f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
