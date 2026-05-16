"""Entry-delay (item 3) and trend-filter (item 4) sensitivity sweeps on SOXX.

Each sweep is run twice: once against the original `baseline_2xATR` exit
config, and once against the `regime_time_only` config (the winner from the
exit-logic sweep). That lets us see whether the entry timing or trend gate
matters independently of the exit logic.

Outputs:
  - data/sensitivity_entry_delay_soxx.json
  - data/sensitivity_trend_filter_soxx.json

WARNING: as with the exit-logic sweep, this is in-sample fitting on a
single ETF over a single window. Use as diagnostic, not deployment.

Run:
    python scripts/run_sensitivity.py
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
DATA_DIR = PROJECT_ROOT / "data"
DELAY_OUT = DATA_DIR / "sensitivity_entry_delay_soxx.json"
TREND_OUT = DATA_DIR / "sensitivity_trend_filter_soxx.json"


# Two "reference" exit configurations we apply each sensitivity to.
REFERENCE_EXITS = {
    "baseline_2xATR": {**DEFAULT_CONFIG, "trailing_stop_k": 2.0},
    "regime_time_only": {**DEFAULT_CONFIG, "trailing_stop_k": None},
}

ENTRY_DELAYS = [0, 3, 5, 10]
TREND_OPTIONS = [False, True]


def _summarise(label: str, stats: dict, mc: dict) -> dict:
    return {
        "variant": label,
        "n_trades": stats.get("n_trades"),
        "win_rate": stats.get("win_rate"),
        "mean_trade_return": stats.get("mean_trade_return"),
        "median_trade_return": stats.get("median_trade_return"),
        "median_holding_days": stats.get("median_holding_days"),
        "equity_curve_total_return": stats.get("equity_curve_total_return"),
        "equity_curve_max_dd": stats.get("equity_curve_max_dd"),
        "sharpe_annualised": stats.get("sharpe_annualised"),
        "sortino_annualised": stats.get("sortino_annualised"),
        "mc_strategy_total_return_percentile": mc.get("strategy_total_return_percentile"),
        "mc_strategy_win_rate_percentile": mc.get("strategy_win_rate_percentile"),
        "mc_strategy_mean_trade_return_percentile": mc.get("strategy_mean_trade_return_percentile"),
        "mc_null_total_return_p50": mc.get("null_total_return_p50"),
    }


def _clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, list):
        return [_clean(x) for x in o]
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    return o


def run_sweep(label_template: str, base_label: str, base_cfg: dict, overlay_list: list[dict],
              signal_dates: list[str], soxx: pd.DataFrame, breadth: pd.DataFrame,
              eligible_start: pd.Timestamp) -> tuple[list[dict], dict]:
    """Run one cell of the sweep: one base config × many overlays."""
    rows = []
    full = {}
    for overlay in overlay_list:
        label = label_template.format(base=base_label, **overlay)
        cfg = {**base_cfg, **overlay}
        trades = run_strategy(signal_dates, soxx, breadth, config=cfg)
        daily_returns = build_daily_returns(trades, soxx)
        stats = aggregate_stats(trades, daily_returns)
        mc = monte_carlo_null(trades, soxx, eligible_start)
        print(f"  {label:<48}  n={stats.get('n_trades',0):>3}  "
              f"win={(stats.get('win_rate',0) or 0):.1%}  "
              f"Sharpe={(stats.get('sharpe_annualised') or 0):+.2f}  "
              f"totRet={(stats.get('equity_curve_total_return') or 0):+.1%}  "
              f"MC%={(mc.get('strategy_total_return_percentile') or 0):.1f}")
        rows.append(_summarise(label, stats, mc))
        full[label] = {
            "config": cfg,
            "trades": [asdict(t) for t in trades],
            "primary": stats,
            "monte_carlo_null": mc,
        }
    return rows, full


def main() -> int:
    print("Loading breadth signal ...", flush=True)
    breadth, signal_records = load_breadth()
    signal_dates = [s["date"] for s in signal_records]
    print(f"  {len(breadth)} trading days, {len(signal_dates)} signal-fire days")

    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    soxx = download_soxx_ohlc(dl_start, dl_end)
    _ = download_spy_close(dl_start, dl_end)
    soxx = soxx[~soxx.index.duplicated(keep="first")]
    eligible_start = breadth.index[252] if len(breadth) > 252 else breadth.index[0]
    diag = mechanism_diagnostic(signal_dates, soxx, HORIZONS)

    # --- Entry delay sweep --------------------------------------------------
    print("\n=== Entry-delay sweep (item 3) ===")
    delay_rows = []
    delay_full = {}
    for ref_name, ref_cfg in REFERENCE_EXITS.items():
        print(f"\n  Reference exit config: {ref_name}")
        overlays = [{"entry_delay_bars": k} for k in ENTRY_DELAYS]
        rows, full = run_sweep(
            "{base}+delay{entry_delay_bars}d",
            ref_name, ref_cfg, overlays,
            signal_dates, soxx, breadth, eligible_start,
        )
        delay_rows.extend(rows)
        delay_full.update(full)
    payload = {
        "etf": "SOXX",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "sweep_kind": "entry_delay",
        "n_signal_fire_days": len(signal_dates),
        "mechanism_diagnostic_shared": diag,
        "summary_table": delay_rows,
        "variants": delay_full,
        "warning": "In-sample fitting — diagnostic only. Pick OOS before deploying.",
    }
    DELAY_OUT.write_text(json.dumps(_clean(payload), indent=2), encoding="utf-8")

    # --- Trend filter sweep -------------------------------------------------
    print("\n=== Trend-filter sweep (item 4) ===")
    trend_rows = []
    trend_full = {}
    for ref_name, ref_cfg in REFERENCE_EXITS.items():
        print(f"\n  Reference exit config: {ref_name}")
        overlays = [{"use_trend_filter": v} for v in TREND_OPTIONS]
        rows, full = run_sweep(
            "{base}+trend{use_trend_filter}",
            ref_name, ref_cfg, overlays,
            signal_dates, soxx, breadth, eligible_start,
        )
        trend_rows.extend(rows)
        trend_full.update(full)
    payload = {
        "etf": "SOXX",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "sweep_kind": "trend_filter",
        "trend_filter_period": 200,
        "n_signal_fire_days": len(signal_dates),
        "mechanism_diagnostic_shared": diag,
        "summary_table": trend_rows,
        "variants": trend_full,
        "warning": "In-sample fitting — diagnostic only. Pick OOS before deploying.",
    }
    TREND_OUT.write_text(json.dumps(_clean(payload), indent=2), encoding="utf-8")

    # --- Console comparison tables -----------------------------------------
    print()
    print("=" * 110)
    print("ENTRY-DELAY sweep (SOXX)")
    print("=" * 110)
    print(f"{'variant':<48} {'n':>4} {'win%':>6} {'med_h':>6} {'totRet%':>9} "
          f"{'maxDD%':>7} {'Sharpe':>7} {'MC%':>6}")
    print("-" * 110)
    for r in delay_rows:
        print(f"{r['variant']:<48} "
              f"{r['n_trades'] or 0:>4} "
              f"{(r['win_rate'] or 0)*100:>6.1f} "
              f"{(r['median_holding_days'] or 0):>6.0f} "
              f"{(r['equity_curve_total_return'] or 0)*100:>+9.1f} "
              f"{(r['equity_curve_max_dd'] or 0)*100:>7.1f} "
              f"{(r['sharpe_annualised'] or 0):>+7.2f} "
              f"{(r['mc_strategy_total_return_percentile'] or 0):>6.1f}")
    print()
    print("=" * 110)
    print("TREND-FILTER sweep (SOXX, parent ETF > 200d MA at signal date)")
    print("=" * 110)
    print(f"{'variant':<48} {'n':>4} {'win%':>6} {'med_h':>6} {'totRet%':>9} "
          f"{'maxDD%':>7} {'Sharpe':>7} {'MC%':>6}")
    print("-" * 110)
    for r in trend_rows:
        print(f"{r['variant']:<48} "
              f"{r['n_trades'] or 0:>4} "
              f"{(r['win_rate'] or 0)*100:>6.1f} "
              f"{(r['median_holding_days'] or 0):>6.0f} "
              f"{(r['equity_curve_total_return'] or 0)*100:>+9.1f} "
              f"{(r['equity_curve_max_dd'] or 0)*100:>7.1f} "
              f"{(r['sharpe_annualised'] or 0):>+7.2f} "
              f"{(r['mc_strategy_total_return_percentile'] or 0):>6.1f}")
    print()
    print(f"Wrote {DELAY_OUT.relative_to(PROJECT_ROOT)}")
    print(f"Wrote {TREND_OUT.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
