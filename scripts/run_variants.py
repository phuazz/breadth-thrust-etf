"""Exit-logic variant sweep on the SOXX breadth-thrust signal.

Runs the same 2018-2026 signal stream through five different exit configurations
and writes a comparison to data/backtest_variants_soxx.json.

Variants:
  - baseline_2xATR        : original spec (2 x ATR stop, regime, time stop).
  - loose_3xATR           : looser stop multiple, all other knobs identical.
  - loose_4xATR           : even looser stop.
  - regime_time_only      : trailing stop DISABLED, only regime + time stop.
  - profit_anchored_3xATR : 3 x ATR stop, but only armed once the trade is
                            up 5 per cent from cost-adjusted entry.

WARNING about interpretation: picking the best variant from this sweep
is in-sample fitting. This script is a DIAGNOSTIC to confirm whether the
trailing-stop mechanic is the binding constraint on the strategy and to
quantify how much slack a different exit would create. Any chosen
parameter set must be validated out-of-sample (different ETF, different
window) before being treated as a deployable rule.

Run:
    python scripts/run_variants.py
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
    MC_PATHS,
    MC_SEED,
)

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = PROJECT_ROOT / "data" / "backtest_variants_soxx.json"


VARIANTS: dict[str, dict] = {
    "baseline_2xATR": {
        **DEFAULT_CONFIG,
        "trailing_stop_k": 2.0,
        "stop_active_after_profit_pct": None,
    },
    "loose_3xATR": {
        **DEFAULT_CONFIG,
        "trailing_stop_k": 3.0,
        "stop_active_after_profit_pct": None,
    },
    "loose_4xATR": {
        **DEFAULT_CONFIG,
        "trailing_stop_k": 4.0,
        "stop_active_after_profit_pct": None,
    },
    "regime_time_only": {
        **DEFAULT_CONFIG,
        "trailing_stop_k": None,
        "stop_active_after_profit_pct": None,
    },
    "profit_anchored_3xATR_arm_at_5pct": {
        **DEFAULT_CONFIG,
        "trailing_stop_k": 3.0,
        "stop_active_after_profit_pct": 0.05,
    },
}


def _summarise(stats: dict, mc: dict, diag: dict) -> dict:
    """Pull a comparable headline row out of the full per-variant result."""
    return {
        "n_trades": stats.get("n_trades"),
        "win_rate": stats.get("win_rate"),
        "mean_trade_return": stats.get("mean_trade_return"),
        "median_trade_return": stats.get("median_trade_return"),
        "mean_holding_days": stats.get("mean_holding_days"),
        "median_holding_days": stats.get("median_holding_days"),
        "equity_curve_total_return": stats.get("equity_curve_total_return"),
        "equity_curve_max_dd": stats.get("equity_curve_max_dd"),
        "sharpe_annualised": stats.get("sharpe_annualised"),
        "sortino_annualised": stats.get("sortino_annualised"),
        "exit_reason_counts": stats.get("exit_reason_counts"),
        "mc_strategy_total_return_percentile": mc.get("strategy_total_return_percentile"),
        "mc_strategy_win_rate_percentile": mc.get("strategy_win_rate_percentile"),
        "mc_strategy_mean_trade_return_percentile": mc.get("strategy_mean_trade_return_percentile"),
        "mc_null_total_return_p50": mc.get("null_total_return_p50"),
        "fwd_126d_pos_rate": diag.get("positive_rate", {}).get("126d"),
        "fwd_126d_mean": diag.get("mean_fwd_return", {}).get("126d"),
    }


def main() -> int:
    print("Loading breadth signal ...", flush=True)
    breadth, signal_records = load_breadth()
    signal_dates = [s["date"] for s in signal_records]
    print(f"  Breadth covers {breadth.index[0].date()} -> {breadth.index[-1].date()}, "
          f"{len(breadth)} trading days, {len(signal_dates)} signal-fire days")

    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print("Downloading SOXX OHLC (cached) ...", flush=True)
    soxx = download_soxx_ohlc(dl_start, dl_end)
    soxx = soxx[~soxx.index.duplicated(keep="first")]
    _ = download_spy_close(dl_start, dl_end)  # cache only

    eligible_start = (breadth.index[252] if len(breadth) > 252 else breadth.index[0])

    # Mechanism diagnostic depends only on signal dates + prices — identical
    # across variants — compute once and reuse.
    diag = mechanism_diagnostic(signal_dates, soxx, HORIZONS)

    results: dict[str, dict] = {}
    summary_table: list[dict] = []
    for label, cfg in VARIANTS.items():
        print(f"\n[{label}]")
        trades = run_strategy(signal_dates, soxx, breadth, config=cfg)
        daily_returns = build_daily_returns(trades, soxx)
        stats = aggregate_stats(trades, daily_returns)
        print(f"  Trades: {stats['n_trades']:>3}  "
              f"WinRate: {stats.get('win_rate', 0):.1%}  "
              f"Sharpe: {stats.get('sharpe_annualised', float('nan')):+.2f}  "
              f"TotalRet: {stats.get('equity_curve_total_return', float('nan')):+.1%}  "
              f"MaxDD: {stats.get('equity_curve_max_dd', float('nan')):.1%}")
        mc = monte_carlo_null(trades, soxx, eligible_start)
        print(f"  MC total-ret percentile: "
              f"{mc.get('strategy_total_return_percentile', float('nan')):.1f}")
        summary = _summarise(stats, mc, diag)
        summary_table.append({"variant": label, **summary})
        results[label] = {
            "config": {k: v for k, v in cfg.items()},
            "trades": [asdict(t) for t in trades],
            "primary": stats,
            "monte_carlo_null": mc,
            "summary_row": summary,
        }

    payload = {
        "etf": "SOXX",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "breadth_source": str((PROJECT_ROOT / "data" / "breadth_soxx.json")
                              .relative_to(PROJECT_ROOT)),
        "n_signal_fire_days": len(signal_dates),
        "mechanism_diagnostic_shared": diag,
        "variants": results,
        "summary_table": summary_table,
        "warning": (
            "Picking the best variant from this sweep is in-sample fitting. "
            "Treat this as diagnostic only — any chosen parameter set must be "
            "validated on a different ETF / out-of-sample window before being "
            "deployed."
        ),
    }

    # NaN/inf -> None for JSON
    def clean(o):
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else o
        if isinstance(o, list):
            return [clean(x) for x in o]
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        return o
    payload = clean(payload)

    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Comparison table to stdout
    print()
    print("=" * 110)
    print("Variant comparison (SOXX, 2019-01-08 to 2026-05-08, 10 bps round-trip cost)")
    print("=" * 110)
    headers = ["variant", "n_trd", "win%", "med_hold", "totRet%", "maxDD%", "Sharpe", "MC%ile"]
    fmt = "{:<36} {:>5} {:>6} {:>9} {:>8} {:>7} {:>7} {:>7}"
    print(fmt.format(*headers))
    print("-" * 110)
    for row in summary_table:
        print(fmt.format(
            row["variant"][:36],
            row["n_trades"] or 0,
            f"{(row['win_rate'] or 0)*100:.1f}",
            f"{row['median_holding_days'] or 0:.0f}",
            f"{(row['equity_curve_total_return'] or 0)*100:+.1f}",
            f"{(row['equity_curve_max_dd'] or 0)*100:.1f}",
            f"{row['sharpe_annualised'] or 0:+.2f}",
            f"{row['mc_strategy_total_return_percentile'] or 0:.1f}",
        ))
    print()
    print(f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
