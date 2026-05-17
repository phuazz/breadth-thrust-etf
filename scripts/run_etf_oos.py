"""Cross-ETF OOS validation — apply SOXX-tuned configs to any other ETF.

Loads the chosen ETF's breadth file, then runs three configs against it:

  1. baseline_2xATR                  — original specification
  2. regime_time_only                — SOXX exit-logic winner
  3. regime_time_only_delay5_trend   — SOXX split-half winner

No re-tuning. The point is to see whether the SOXX-tuned parameter
choices generalise to other universes (S&P 500, sector slices, NDX-100).

The OHLC for the trading vehicle defaults to the ETF's `yfinance_trading_proxy`
in scripts/etf_registry.py (e.g. CSP1 -> SPY, IUES -> XLE, IUFS -> XLF,
CNDX -> QQQ) so we always trade a liquid US-listed ETF in USD even when
the breadth comes from a UK UCITS file.

Output:
  - data/backtest_<etf_lower>_oos.json

Run:
    python scripts/run_etf_oos.py --etf CSP1
    python scripts/run_etf_oos.py --etf IUES
    python scripts/run_etf_oos.py --etf IUFS
    python scripts/run_etf_oos.py --etf CNDX
"""

from __future__ import annotations

import argparse
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
from etf_registry import get_etf  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf", required=True,
                   help="ETF symbol whose breadth file to load (must be in etf_registry).")
    p.add_argument("--yf-symbol", default=None,
                   help="Override yfinance ticker for OHLC trading proxy. "
                        "Defaults to the registry's yfinance_trading_proxy or the ETF itself.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    etf = args.etf
    cfg = get_etf(etf)
    yf_sym = args.yf_symbol or cfg.get("yfinance_trading_proxy") or etf
    out_path = PROJECT_ROOT / "data" / f"backtest_{etf.lower()}_oos.json"

    print(f"Loading {etf} breadth signal ...", flush=True)
    breadth, signal_records = load_breadth(etf=etf)
    signal_dates = [s["date"] for s in signal_records]
    print(f"  Breadth covers {breadth.index[0].date()} -> {breadth.index[-1].date()}, "
          f"{len(breadth)} trading days; {len(signal_dates)} signal-fire days")

    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"Downloading {yf_sym} OHLC (trading proxy) and SPY close ...", flush=True)
    ohlc = download_soxx_ohlc(dl_start, dl_end, etf=yf_sym, yf_symbol=yf_sym)
    spy_close = download_spy_close(dl_start, dl_end)
    ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
    eligible_start = breadth.index[252] if len(breadth) > 252 else breadth.index[0]

    diag = mechanism_diagnostic(signal_dates, ohlc, HORIZONS)

    rows = []
    full = {}
    for label, conf in CONFIGS.items():
        print(f"\n[{label}]")
        trades = run_strategy(signal_dates, ohlc, breadth, config=conf)
        daily_returns = build_daily_returns(trades, ohlc)
        stats = aggregate_stats(trades, daily_returns)
        mc = monte_carlo_null(trades, ohlc, eligible_start)
        print(f"  n={stats.get('n_trades',0):>3}  "
              f"win={(stats.get('win_rate') or 0):.1%}  "
              f"medHold={(stats.get('median_holding_days') or 0):>3.0f}d  "
              f"totRet={(stats.get('equity_curve_total_return') or 0):+.1%}  "
              f"maxDD={(stats.get('equity_curve_max_dd') or 0):.1%}  "
              f"Shp={(stats.get('sharpe_annualised') or 0):+.2f}  "
              f"MC%={(mc.get('strategy_total_return_percentile') or 0):.1f}")
        rows.append(_summarise(label, stats, mc))
        full[label] = {
            "config": conf,
            "trades": [asdict(t) for t in trades],
            "primary": stats,
            "monte_carlo_null": mc,
        }

    payload = {
        "etf_breadth_source": etf,
        "etf_traded_yf_symbol": yf_sym,
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_signal_fire_days": len(signal_dates),
        "mechanism_diagnostic": diag,
        "summary_table": rows,
        "variants": full,
        "note": (
            f"Cross-ETF OOS: configs tuned on SOXX 2019-2026 applied to {etf} "
            f"(traded via {yf_sym}) without re-tuning. baseline_2xATR is the "
            "original spec; regime_time_only is the SOXX exit-logic winner; "
            "regime_time_only_delay5_trend is the SOXX split-half winner."
        ),
    }
    out_path.write_text(json.dumps(_clean(payload), indent=2), encoding="utf-8")

    print()
    print("=" * 110)
    print(f"{etf} (breadth) traded via {yf_sym} -- SOXX-tuned configs, no re-tuning")
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
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
