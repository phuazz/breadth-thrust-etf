"""Out-of-sample validation of the base x thrust grid winner on SOXX.

Train half : 2019-01-08 to 2022-09-08
Test  half : 2022-09-09 to 2026-05-15

Procedure:
  1. Restrict the triple-combo trades to those whose entry_date falls in
     the train half. Sweep the same (base, thrust) grid used in the
     Tuning tab on TRAIN data only.
  2. Pick the winner by train-half Sharpe.
  3. Apply that winner's (base, thrust) to TEST-half trades and TEST-half
     SOXX prices. Report test-half Sharpe / total return / max DD /
     time-in-market vs SOXX buy-and-hold on the same test half.

If the train-half winner's test-half stats are materially worse than
its train-half stats, that is overfitting evidence. If they hold up,
the parameter choice generalises within SOXX across regimes.

Output: data/oos_validation.json

Run:
    python scripts/run_oos_validation.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import download_soxx_ohlc  # noqa: E402
from etf_registry import get_etf  # noqa: E402
from run_improvements import (  # noqa: E402
    compute_stats, load_triple_trades, size_scaled_thrust,
)
from run_tuning import BASE_THRUST_GRID  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "oos_validation.json"

ETF = "SOXX"
TRAIN_START = pd.Timestamp("2019-01-08")
TRAIN_END = pd.Timestamp("2022-09-08")
TEST_START = pd.Timestamp("2022-09-09")
TEST_END = pd.Timestamp("2026-05-15")


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def evaluate_in_window(
    trades: list[dict],
    close: pd.Series,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    base_pct: int,
    thrust_pct: int,
) -> dict:
    """Run size_scaled_thrust then restrict the equity series to the period
    for stats computation."""
    r = size_scaled_thrust(
        trades, close, base=base_pct / 100.0, on=thrust_pct / 100.0,
        window_start=period_start,
    )
    period_mask = (r["equity"].index >= period_start) & (r["equity"].index <= period_end)
    eq = r["equity"].loc[period_mask].copy()
    if len(eq) < 2:
        return {"n_trades_in_period": 0}
    eq = eq / float(eq.iloc[0])
    alloc_period = r["alloc"].loc[period_mask]
    n_trades_in_period = sum(
        1 for t in trades
        if period_start <= pd.Timestamp(t["entry_date"]) <= period_end
    )
    st = compute_stats(eq, period_start)
    st["time_in_market"] = float((alloc_period > 0).mean())
    st["n_trades_in_period"] = n_trades_in_period
    st["base_pct"] = base_pct
    st["thrust_pct"] = thrust_pct
    return st


def main() -> int:
    cfg = get_etf(ETF)
    proxy = cfg.get("yfinance_trading_proxy") or ETF
    trades = load_triple_trades(ETF)
    print(f"Loaded {len(trades)} triple-combo trades for {ETF}")

    train_trades = [t for t in trades if TRAIN_START <= pd.Timestamp(t["entry_date"]) <= TRAIN_END]
    test_trades = [t for t in trades if TEST_START <= pd.Timestamp(t["entry_date"]) <= TEST_END]
    print(f"  Train trades: {len(train_trades)}  |  Test trades: {len(test_trades)}")

    # SOXX close
    dl_start = "2017-09-01"
    dl_end = "2026-05-20"
    close = download_soxx_ohlc(dl_start, dl_end, etf=proxy, yf_symbol=proxy)["Close"].astype(float)
    close = close[~close.index.duplicated(keep="first")]

    # ----- TRAIN HALF: full grid ----------------------------------------
    print("\n=== TRAIN half (2019-01-08 to 2022-09-08): base x thrust grid ===")
    train_grid = []
    for b, t in BASE_THRUST_GRID:
        st = evaluate_in_window(train_trades, close, TRAIN_START, TRAIN_END, b, t)
        if not st:
            continue
        train_grid.append({k: _safe(v) if isinstance(v, float) else v for k, v in st.items()})
        print(f"  b={b:>3}/t={t:>3}  Sharpe {st.get('sharpe',0):+.2f}  "
              f"totRet {(st.get('total_return') or 0)*100:+.1f}%  "
              f"DD {(st.get('max_dd') or 0)*100:.1f}%  TIM {(st.get('time_in_market') or 0)*100:.0f}%")

    winner = max(train_grid, key=lambda r: r.get("sharpe") or -1e9)
    print(f"\n  TRAIN winner: b={winner['base_pct']}/t={winner['thrust_pct']}  "
          f"Sharpe {winner['sharpe']:+.2f}")

    # ----- TEST HALF: apply train winner --------------------------------
    print(f"\n=== TEST half (2022-09-09 to 2026-05-15): apply train winner b={winner['base_pct']}/t={winner['thrust_pct']} ===")
    test_winner_stats = evaluate_in_window(
        test_trades, close, TEST_START, TEST_END,
        winner["base_pct"], winner["thrust_pct"],
    )
    test_winner_stats = {k: _safe(v) if isinstance(v, float) else v
                          for k, v in test_winner_stats.items()}
    print(f"  Sharpe {test_winner_stats['sharpe']:+.2f}  "
          f"totRet {test_winner_stats['total_return']*100:+.1f}%  "
          f"DD {test_winner_stats['max_dd']*100:.1f}%  "
          f"TIM {test_winner_stats['time_in_market']*100:.0f}%")

    # Also compute the FULL grid on test half — to see if the train winner
    # is also the test winner (sanity check).
    print("\n=== TEST half: full grid (for transparency) ===")
    test_grid = []
    for b, t in BASE_THRUST_GRID:
        st = evaluate_in_window(test_trades, close, TEST_START, TEST_END, b, t)
        if not st:
            continue
        test_grid.append({k: _safe(v) if isinstance(v, float) else v for k, v in st.items()})
        marker = "  <-- train winner" if (b == winner["base_pct"] and t == winner["thrust_pct"]) else ""
        print(f"  b={b:>3}/t={t:>3}  Sharpe {st.get('sharpe',0):+.2f}  "
              f"totRet {(st.get('total_return') or 0)*100:+.1f}%  "
              f"DD {(st.get('max_dd') or 0)*100:.1f}%{marker}")

    test_actual_winner = max(test_grid, key=lambda r: r.get("sharpe") or -1e9)
    rank_of_train_winner = sorted(
        test_grid, key=lambda r: r.get("sharpe") or -1e9, reverse=True
    ).index(
        next(r for r in test_grid
             if r["base_pct"] == winner["base_pct"] and r["thrust_pct"] == winner["thrust_pct"])
    ) + 1
    print(f"\n  Test-half actual winner: b={test_actual_winner['base_pct']}/t={test_actual_winner['thrust_pct']}  "
          f"Sharpe {test_actual_winner['sharpe']:+.2f}")
    print(f"  Train winner ranks #{rank_of_train_winner} out of {len(test_grid)} cells on test half")

    # ----- Buy-and-hold benchmarks --------------------------------------
    bh_train = compute_stats(close.loc[(close.index >= TRAIN_START) & (close.index <= TRAIN_END)], TRAIN_START)
    bh_train["time_in_market"] = 1.0
    bh_test = compute_stats(close.loc[(close.index >= TEST_START) & (close.index <= TEST_END)], TEST_START)
    bh_test["time_in_market"] = 1.0
    print(f"\n  BH train Sharpe {bh_train['sharpe']:+.2f}  "
          f"totRet {bh_train['total_return']*100:+.1f}%  DD {bh_train['max_dd']*100:.1f}%")
    print(f"  BH test  Sharpe {bh_test['sharpe']:+.2f}  "
          f"totRet {bh_test['total_return']*100:+.1f}%  DD {bh_test['max_dd']*100:.1f}%")

    # ----- Verdict -------------------------------------------------------
    delta_sh = test_winner_stats["sharpe"] - bh_test["sharpe"]
    print()
    print("=" * 80)
    print("OOS VERDICT")
    print("=" * 80)
    print(f"  Train winner config: b={winner['base_pct']}/t={winner['thrust_pct']}")
    print(f"  Train Sharpe (in-sample selection) : {winner['sharpe']:+.2f}")
    print(f"  Test  Sharpe (held-out validation) : {test_winner_stats['sharpe']:+.2f}")
    print(f"  Test  BH Sharpe                    : {bh_test['sharpe']:+.2f}")
    print(f"  Δ Test vs BH                       : {delta_sh:+.2f}")
    if delta_sh > 0.05:
        print("  -> Train winner OUTPERFORMS BH on the held-out test half.")
    elif delta_sh > -0.05:
        print("  -> Train winner roughly TIES BH on the held-out test half.")
    else:
        print("  -> Train winner UNDERPERFORMS BH on the held-out test half (likely overfit).")

    payload = {
        "etf": ETF,
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "train_start": TRAIN_START.strftime("%Y-%m-%d"),
        "train_end": TRAIN_END.strftime("%Y-%m-%d"),
        "test_start": TEST_START.strftime("%Y-%m-%d"),
        "test_end": TEST_END.strftime("%Y-%m-%d"),
        "n_train_trades": len(train_trades),
        "n_test_trades": len(test_trades),
        "train_grid": train_grid,
        "train_winner": {k: winner.get(k) for k in
                         ["base_pct", "thrust_pct", "sharpe", "total_return", "max_dd",
                          "cagr", "time_in_market", "n_trades_in_period"]},
        "test_winner_stats": test_winner_stats,
        "test_grid": test_grid,
        "test_actual_winner": {k: test_actual_winner.get(k) for k in
                                ["base_pct", "thrust_pct", "sharpe", "total_return", "max_dd"]},
        "rank_of_train_winner_on_test": rank_of_train_winner,
        "bh_train_stats": {k: _safe(v) if isinstance(v, float) else v for k, v in bh_train.items()},
        "bh_test_stats": {k: _safe(v) if isinstance(v, float) else v for k, v in bh_test.items()},
        "delta_test_winner_vs_bh_sharpe": _safe(delta_sh),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
