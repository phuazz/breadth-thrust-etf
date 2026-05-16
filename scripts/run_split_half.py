"""Split-half out-of-sample validation on SOXX.

Items 1, 2, 5 from the proposed next-session list all require fresh
iShares constituent data (IVV for items 1+5, pre-2018 SOXX for item 2).
On 2026-05-16 iShares' Akamai bot defence began returning a 10 MB HTML
product page in place of the CSV regardless of headers, session cookies,
or referrer. The fetch is blocked.

This script provides the best-available substitute: a within-SOXX
out-of-sample test. The 2019-01-08 to 2026-05-08 signal-eligible
window is split into two roughly equal halves. For each candidate
exit / entry / filter configuration we report:

  - TRAIN HALF stats : 2019-01-08 to mid-2022
  - TEST HALF stats  : mid-2022 to 2026-05-08

The variant that wins on the train half by Sharpe is then explicitly
called out, with its test-half performance reported as the OOS result.
Same-distribution Monte Carlo nulls are run separately for each half.

This is NOT a full OOS test (same ETF, same constituent universe),
but it does test whether the parameter choices generalise across two
materially different SOXX regimes:

  - Train half spans the COVID crash + recovery + Inflation-Shock-2022
  - Test half spans the AI rally (NVDA, AVGO) + Apr-2026 sell-off

If a config wins train and bombs test, that is overfit. If it
generalises, that is at least some evidence the parameter choice is
not pure curve fit.

Output:
  - data/oos_split_half_soxx.json

Run:
    python scripts/run_split_half.py
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
    monte_carlo_null,
    run_strategy,
)

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = PROJECT_ROOT / "data" / "oos_split_half_soxx.json"


# Candidate configs to evaluate on each half. Subset of the prior sweeps,
# selected for diagnostic clarity rather than exhaustive search.
CANDIDATES: dict[str, dict] = {
    "baseline_2xATR": {**DEFAULT_CONFIG},
    "regime_time_only": {**DEFAULT_CONFIG, "trailing_stop_k": None},
    "baseline_2xATR_delay5": {**DEFAULT_CONFIG, "entry_delay_bars": 5},
    "regime_time_only_delay5": {
        **DEFAULT_CONFIG, "trailing_stop_k": None, "entry_delay_bars": 5,
    },
    "regime_time_only_trend": {
        **DEFAULT_CONFIG, "trailing_stop_k": None, "use_trend_filter": True,
    },
    "regime_time_only_delay5_trend": {
        **DEFAULT_CONFIG, "trailing_stop_k": None,
        "entry_delay_bars": 5, "use_trend_filter": True,
    },
}


def slice_signals(signal_dates: list[str], lo: pd.Timestamp, hi: pd.Timestamp) -> list[str]:
    return [d for d in signal_dates if lo <= pd.Timestamp(d) <= hi]


def evaluate_in_window(
    label: str,
    cfg: dict,
    signals_in_window: list[str],
    soxx_full: pd.DataFrame,
    breadth: pd.DataFrame,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> dict:
    """Run the strategy on signals inside [window_start, window_end] and
    compute aggregate stats over that same window's daily returns. MC null
    is restricted to the same window.
    """
    trades = run_strategy(signals_in_window, soxx_full, breadth, config=cfg)
    daily_full = build_daily_returns(trades, soxx_full)
    window_mask = (daily_full.index >= window_start) & (daily_full.index <= window_end)
    daily_window = daily_full[window_mask]
    stats = aggregate_stats(trades, daily_window)
    mc = monte_carlo_null(trades, soxx_full, window_start,
                          eligible_end=window_end)
    return {
        "label": label,
        "n_trades": stats.get("n_trades"),
        "win_rate": stats.get("win_rate"),
        "mean_trade_return": stats.get("mean_trade_return"),
        "median_holding_days": stats.get("median_holding_days"),
        "equity_curve_total_return": stats.get("equity_curve_total_return"),
        "equity_curve_max_dd": stats.get("equity_curve_max_dd"),
        "sharpe_annualised": stats.get("sharpe_annualised"),
        "sortino_annualised": stats.get("sortino_annualised"),
        "mc_strategy_total_return_percentile": mc.get("strategy_total_return_percentile"),
        "mc_strategy_win_rate_percentile": mc.get("strategy_win_rate_percentile"),
        "mc_null_total_return_p50": mc.get("null_total_return_p50"),
        "trades": [asdict(t) for t in trades],
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
    print("Loading breadth signal ...", flush=True)
    breadth, signal_records = load_breadth()
    signal_dates = [s["date"] for s in signal_records]

    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    soxx = download_soxx_ohlc(dl_start, dl_end)
    _ = download_spy_close(dl_start, dl_end)
    soxx = soxx[~soxx.index.duplicated(keep="first")]

    # Eligible signal-fire window starts at index 252 of breadth (the
    # signal_eligible flag in Step 2). Split at the midpoint of that
    # eligible window so train and test are roughly equal in length.
    eligible_start = breadth.index[252]
    eligible_end = breadth.index[-1]
    midpoint = eligible_start + (eligible_end - eligible_start) / 2
    # Snap midpoint to a real trading day.
    mid_idx = soxx.index.searchsorted(midpoint, side="left")
    midpoint = soxx.index[mid_idx]

    train_start, train_end = eligible_start, midpoint
    test_start, test_end = midpoint + pd.Timedelta(days=1), eligible_end
    print(f"  Train half: {train_start.date()} to {train_end.date()} "
          f"({(train_end - train_start).days} calendar days)")
    print(f"  Test  half: {test_start.date()} to {test_end.date()} "
          f"({(test_end - test_start).days} calendar days)")

    train_sigs = slice_signals(signal_dates, train_start, train_end)
    test_sigs = slice_signals(signal_dates, test_start, test_end)
    print(f"  Signal-fire days: train={len(train_sigs)}  test={len(test_sigs)}")

    rows = []
    for label, cfg in CANDIDATES.items():
        print(f"\n[{label}]")
        train_res = evaluate_in_window(
            f"{label}|train", cfg, train_sigs, soxx, breadth, train_start, train_end
        )
        print(f"  TRAIN: n={train_res['n_trades']:>2}  "
              f"win={(train_res['win_rate'] or 0):.1%}  "
              f"Sharpe={(train_res['sharpe_annualised'] or 0):+.2f}  "
              f"totRet={(train_res['equity_curve_total_return'] or 0):+.1%}  "
              f"MC%={(train_res['mc_strategy_total_return_percentile'] or 0):.1f}")
        test_res = evaluate_in_window(
            f"{label}|test", cfg, test_sigs, soxx, breadth, test_start, test_end
        )
        print(f"  TEST : n={test_res['n_trades']:>2}  "
              f"win={(test_res['win_rate'] or 0):.1%}  "
              f"Sharpe={(test_res['sharpe_annualised'] or 0):+.2f}  "
              f"totRet={(test_res['equity_curve_total_return'] or 0):+.1%}  "
              f"MC%={(test_res['mc_strategy_total_return_percentile'] or 0):.1f}")
        rows.append({"variant": label, "config": cfg, "train": train_res, "test": test_res})

    # Pick the variant with the best TRAIN Sharpe and report its TEST result.
    train_sharpes = [(r["variant"],
                      r["train"]["sharpe_annualised"] or float("-inf"))
                     for r in rows]
    train_sharpes.sort(key=lambda x: x[1], reverse=True)
    winner_label, winner_sharpe = train_sharpes[0]
    winner_row = next(r for r in rows if r["variant"] == winner_label)
    print()
    print("=" * 80)
    print(f"WINNER BY TRAIN SHARPE: {winner_label} (train Sharpe {winner_sharpe:+.2f})")
    print(f"  Test-half result for that variant:")
    tr = winner_row["test"]
    print(f"    n_trades                    {tr['n_trades']}")
    print(f"    win_rate                    {(tr['win_rate'] or 0):.1%}")
    print(f"    equity_curve_total_return   {(tr['equity_curve_total_return'] or 0):+.1%}")
    print(f"    equity_curve_max_dd         {(tr['equity_curve_max_dd'] or 0):.1%}")
    print(f"    Sharpe                      {(tr['sharpe_annualised'] or 0):+.2f}")
    print(f"    MC %ile (total return)      {(tr['mc_strategy_total_return_percentile'] or 0):.1f}")

    # Console comparison table
    print()
    print("=" * 110)
    print("SPLIT-HALF COMPARISON (TRAIN | TEST)")
    print("=" * 110)
    print(f"{'variant':<34} | "
          f"{'n':>3} {'win%':>5} {'totRet%':>8} {'Shp':>5} {'MC%':>5} | "
          f"{'n':>3} {'win%':>5} {'totRet%':>8} {'Shp':>5} {'MC%':>5}")
    print("-" * 110)
    for r in rows:
        t = r["train"]; e = r["test"]
        print(f"{r['variant']:<34} | "
              f"{t['n_trades'] or 0:>3} "
              f"{(t['win_rate'] or 0)*100:>5.1f} "
              f"{(t['equity_curve_total_return'] or 0)*100:>+8.1f} "
              f"{(t['sharpe_annualised'] or 0):>+5.2f} "
              f"{(t['mc_strategy_total_return_percentile'] or 0):>5.1f} | "
              f"{e['n_trades'] or 0:>3} "
              f"{(e['win_rate'] or 0)*100:>5.1f} "
              f"{(e['equity_curve_total_return'] or 0)*100:>+8.1f} "
              f"{(e['sharpe_annualised'] or 0):>+5.2f} "
              f"{(e['mc_strategy_total_return_percentile'] or 0):>5.1f}")

    payload = {
        "etf": "SOXX",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "split": {
            "train_start": train_start.strftime("%Y-%m-%d"),
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "n_train_signal_fire_days": len(train_sigs),
            "n_test_signal_fire_days": len(test_sigs),
        },
        "selection_criterion": "best Sharpe on TRAIN half, report TEST half stats",
        "winner_by_train_sharpe": {
            "variant": winner_label,
            "train_sharpe": winner_sharpe,
            "test_stats": {
                k: v for k, v in winner_row["test"].items() if k != "trades"
            },
        },
        "rows": [
            {
                "variant": r["variant"],
                "config": r["config"],
                "train": {k: v for k, v in r["train"].items() if k != "trades"},
                "test":  {k: v for k, v in r["test"].items()  if k != "trades"},
                "train_trades": r["train"]["trades"],
                "test_trades":  r["test"]["trades"],
            } for r in rows
        ],
        "caveats": (
            "WITHIN-ETF, WITHIN-INDEX OOS. Not a true cross-ETF OOS — iShares "
            "fetch was blocked by Akamai bot defence on the day this was run, "
            "preventing IVV / SPY-equivalent constituent download. Train and "
            "test halves use the SAME breadth file and SAME constituent universe "
            "(so the breadth thresholds are computed on the full window, not "
            "re-estimated per half). What is held out is purely the choice of "
            "exit / entry / filter parameters."
        ),
    }
    OUT_PATH.write_text(json.dumps(_clean(payload), indent=2), encoding="utf-8")
    print()
    print(f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
