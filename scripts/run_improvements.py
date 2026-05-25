"""Three follow-on strategy variants designed to address the "not invested
enough" problem identified in the time-in-market analysis.

  Test 1: LONG-BY-DEFAULT WITH REGIME OVERLAY
    Be invested whenever breadth regime is healthy (composite_z >= the
    expanding 10th percentile AND ma_breadth >= 0.40); flat otherwise.
    No thrust entry — this is a pure regime filter on buy-and-hold using
    the breadth panel as the regime indicator. Tested per-ETF.

  Test 2: SIZE-SCALED THRUST
    Always hold 50 per cent of capital in the underlying ETF (capture
    half of the unconditional drift). Step up to 100 per cent when the
    triple-combo thrust trade is active (capture the timing alpha).
    Step back to 50 per cent on exit. Tested per-ETF.

  Test 3: MULTI-ETF ROTATION
    At each trading day, identify which of the five ETFs have a live
    triple-combo thrust trade. Equal-weight across active ETFs. If none
    active, flat. Single portfolio across SOXX / SPY / QQQ / XLE / XLF.

For each variant, computes equity curve + headline metrics on the
2019-01-08 to 2026-05-08 signal-eligible window and compares to:
  - The relevant ETF's buy-and-hold (for tests 1 + 2)
  - SPY buy-and-hold (for test 3)
  - The current best strategy from regime+delay+trend (the headline)
  - A 50/50 ETF/cash portfolio (reference for test 2)

Output: data/improvements.json

Costs are 5 bps each side, applied on every allocation change
(turnover * cost). Daily-bar close-to-close throughout — simplification
versus the open-entry/close-exit convention used in the main backtest.

Run:
    python scripts/run_improvements.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alignment import align_frame_to_index  # noqa: E402
from backtest import (  # noqa: E402
    download_soxx_ohlc,
    download_spy_close,
    load_breadth,
    DEFAULT_CONFIG,
    run_strategy,
)
from etf_registry import get_etf  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "improvements.json"

ETFS = ["SOXX", "IUES", "IUFS", "CNDX", "CSP1"]
COST_BPS = 5
TRIPLE_CONFIG = {
    **DEFAULT_CONFIG,
    "trailing_stop_k": None,
    "entry_delay_bars": 5,
    "use_trend_filter": True,
}


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def compute_stats(equity: pd.Series, eligible_start: pd.Timestamp | None = None) -> dict:
    """Total return, CAGR, Sharpe (ann.), max DD on an equity curve."""
    if eligible_start is not None:
        equity = equity.loc[equity.index >= eligible_start].copy()
    if len(equity) < 2:
        return {"total_return": 0.0, "cagr": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    equity = equity / float(equity.iloc[0])  # renormalize
    daily = equity.pct_change().fillna(0)
    total = float(equity.iloc[-1]) - 1.0
    n_years = len(equity) / 252.0
    cagr = float(equity.iloc[-1]) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0
    std = float(daily.std())
    sharpe = float(daily.mean() / std * math.sqrt(252)) if std > 0 else 0.0
    peaks = equity.cummax()
    max_dd = float((1.0 - equity / peaks).max())
    return {
        "total_return": total, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
        "n_days": int(len(equity)),
    }


def round_series(values, ndigits=4):
    out = []
    for v in values:
        try:
            f = float(v)
            out.append(round(f, ndigits) if not (math.isnan(f) or math.isinf(f)) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# Test 1 — Long-by-default with regime overlay
# ---------------------------------------------------------------------------


def regime_overlay(breadth_df: pd.DataFrame, prices_close: pd.Series,
                   cost: float = COST_BPS / 10_000) -> dict:
    """Be invested when regime is OK; flat otherwise. Uses lagged regime
    signal (yesterday's regime determines today's allocation) so no
    look-ahead. Costs paid on every allocation transition.
    """
    aligned = align_frame_to_index(breadth_df, prices_close.index)
    regime_ok = (
        (aligned["composite_z"].fillna(-1e9) >= aligned["composite_p10"].fillna(1e9))
        & (aligned["ma_breadth"] >= 0.40)
        & aligned["composite_z"].notna()
        & aligned["composite_p10"].notna()
    )
    alloc = regime_ok.shift(1).fillna(False).astype(float)
    daily_ret = prices_close.pct_change().fillna(0)
    strat_ret = alloc * daily_ret
    alloc_change = alloc.diff().fillna(0).abs()
    strat_ret = strat_ret - alloc_change * cost
    equity = (1.0 + strat_ret).cumprod()
    return {"equity": equity, "alloc": alloc}


# ---------------------------------------------------------------------------
# Test 2 — Size-scaled thrust (base 50 per cent, step to 100 per cent)
# ---------------------------------------------------------------------------


def size_scaled_thrust(trades: list[dict], prices_close: pd.Series,
                       base: float = 0.5, on: float = 1.0,
                       cost: float = COST_BPS / 10_000,
                       window_start: pd.Timestamp | None = None) -> dict:
    in_trade = pd.Series(False, index=prices_close.index)
    for t in trades:
        entry = pd.Timestamp(t["entry_date"])
        exit_ = pd.Timestamp(t["exit_date"])
        in_trade[(in_trade.index >= entry) & (in_trade.index <= exit_)] = True
    alloc = base + (on - base) * in_trade.astype(float)
    # Constrain alloc to start at base only from eligible window onward; before
    # that, set alloc to 0 so we are not retroactively earning the BH drift
    # during pre-eligibility warmup.
    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0
    daily_ret = prices_close.pct_change().fillna(0)
    strat_ret = alloc.shift(1).fillna(0.0) * daily_ret
    alloc_change = alloc.diff().fillna(0).abs()
    strat_ret = strat_ret - alloc_change * cost
    equity = (1.0 + strat_ret).cumprod()
    return {"equity": equity, "alloc": alloc}


# ---------------------------------------------------------------------------
# Test 3 — Multi-ETF rotation (equal-weight across active ETFs)
# ---------------------------------------------------------------------------


def multi_etf_rotation(trades_by_etf: dict[str, list[dict]],
                       close_by_etf: dict[str, pd.Series],
                       cost: float = COST_BPS / 10_000,
                       window_start: pd.Timestamp | None = None) -> dict:
    # Common date index = intersection of all ETF date indices.
    common_dates = None
    for s in close_by_etf.values():
        common_dates = s.index if common_dates is None else common_dates.intersection(s.index)
    common_dates = common_dates.sort_values()

    # Active matrix
    active = pd.DataFrame(False, index=common_dates, columns=list(trades_by_etf.keys()))
    for etf, trades in trades_by_etf.items():
        for t in trades:
            entry = pd.Timestamp(t["entry_date"])
            exit_ = pd.Timestamp(t["exit_date"])
            mask = (active.index >= entry) & (active.index <= exit_)
            active.loc[mask, etf] = True
    n_active = active.sum(axis=1)

    # Equal-weight across active ETFs (sum to 1.0 if any active, else 0).
    alloc = active.astype(float).div(n_active.replace(0, 1).astype(float), axis=0)
    alloc.loc[n_active == 0] = 0.0
    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0
        n_active.loc[n_active.index < window_start] = 0

    # Daily returns per ETF on common dates
    rets = pd.DataFrame({
        etf: close_by_etf[etf].reindex(common_dates).pct_change()
        for etf in trades_by_etf.keys()
    }).fillna(0)

    # Portfolio return (yesterday's allocations earn today's returns)
    port_ret = (alloc.shift(1).fillna(0.0) * rets).sum(axis=1)
    # Turnover cost: sum of |alloc change| across all ETFs * cost
    turnover = alloc.diff().abs().sum(axis=1).fillna(0.0)
    port_ret = port_ret - turnover * cost
    equity = (1.0 + port_ret).cumprod()
    return {
        "equity": equity,
        "n_active": n_active,
        "alloc": alloc,
    }


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_breadth_panel(etf: str) -> pd.DataFrame:
    blob = load_breadth(etf=etf)[0]
    return blob


def load_close(yf_sym: str, start: str, end: str) -> pd.Series:
    """Load close-only series for a single ticker, using existing OHLC cache."""
    ohlc = download_soxx_ohlc(start, end, etf=yf_sym, yf_symbol=yf_sym)
    ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
    return ohlc["Close"].astype(float)


def load_triple_trades(etf: str) -> list[dict]:
    path = DATA_DIR / f"backtest_{etf.lower()}_oos.json"
    blob = json.loads(path.read_text(encoding="utf-8"))
    return blob["variants"].get("regime_time_only_delay5_trend", {}).get("trades", [])


def load_triple_equity(etf: str) -> dict:
    path = DATA_DIR / f"backtest_{etf.lower()}_oos.json"
    blob = json.loads(path.read_text(encoding="utf-8"))
    return blob["variants"].get("regime_time_only_delay5_trend", {}).get("equity_curve")


# ---------------------------------------------------------------------------
# Time-in-market for tests 1 / 2 / 3 needs a consistent denominator: the
# number of trading days in the signal-eligible window.
# ---------------------------------------------------------------------------


def time_in_market(alloc: pd.Series, window_start, window_end) -> float:
    a = alloc.loc[(alloc.index >= window_start) & (alloc.index <= window_end)]
    return float((a > 0).mean())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("Loading breadth panels + price series for 5 ETFs ...", flush=True)
    breadths: dict[str, pd.DataFrame] = {}
    closes: dict[str, pd.Series] = {}      # the TRADED ETF close (proxy)
    triple_trades: dict[str, list[dict]] = {}
    triple_eq: dict[str, dict] = {}
    eligible_starts: dict[str, pd.Timestamp] = {}

    for etf in ETFS:
        cfg = get_etf(etf)
        proxy = cfg.get("yfinance_trading_proxy") or etf
        breadth_df = load_breadth_panel(etf)
        # Window: signal-eligible window from this breadth panel
        eligible_start = breadth_df.index[252] if len(breadth_df) > 252 else breadth_df.index[0]
        eligible_starts[etf] = eligible_start
        dl_start = (breadth_df.index[0] - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end = (breadth_df.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        close = load_close(proxy, dl_start, dl_end)
        breadths[etf] = breadth_df
        closes[etf] = close
        triple_trades[etf] = load_triple_trades(etf)
        triple_eq[etf] = load_triple_equity(etf)
        print(f"  {etf:5} -> trade {proxy:4}  breadth {len(breadth_df):>4} rows  "
              f"close {len(close):>4} rows  triple-trades {len(triple_trades[etf])}")

    print("\n=== Test 1: Long-by-default regime overlay (per ETF) ===", flush=True)
    test1: dict[str, dict] = {}
    for etf in ETFS:
        r = regime_overlay(breadths[etf], closes[etf])
        win = (r["equity"].index >= eligible_starts[etf])
        st = compute_stats(r["equity"], eligible_starts[etf])
        st["time_in_market"] = time_in_market(r["alloc"], eligible_starts[etf], r["equity"].index[-1])
        # Equity restricted to eligible window for the dashboard
        eq_window = r["equity"].loc[win]
        eq_window = eq_window / eq_window.iloc[0]
        test1[etf] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq_window.index],
            "equity": round_series(eq_window.values),
            **st,
        }
        print(f"  {etf:5}  totRet {st['total_return']*100:+7.1f}%  CAGR {st['cagr']*100:+6.1f}%  "
              f"Sharpe {st['sharpe']:+.2f}  MaxDD {st['max_dd']*100:>5.1f}%  "
              f"TimeInMkt {st['time_in_market']*100:>5.1f}%")

    print("\n=== Test 2: Size-scaled thrust (50% base, 100% on signal) ===", flush=True)
    test2: dict[str, dict] = {}
    for etf in ETFS:
        r = size_scaled_thrust(
            triple_trades[etf], closes[etf],
            window_start=eligible_starts[etf],
        )
        st = compute_stats(r["equity"], eligible_starts[etf])
        st["time_in_market"] = time_in_market(r["alloc"], eligible_starts[etf], r["equity"].index[-1])
        win = (r["equity"].index >= eligible_starts[etf])
        eq_window = r["equity"].loc[win]
        eq_window = eq_window / eq_window.iloc[0]
        test2[etf] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq_window.index],
            "equity": round_series(eq_window.values),
            **st,
        }
        print(f"  {etf:5}  totRet {st['total_return']*100:+7.1f}%  CAGR {st['cagr']*100:+6.1f}%  "
              f"Sharpe {st['sharpe']:+.2f}  MaxDD {st['max_dd']*100:>5.1f}%  "
              f"TimeInMkt {st['time_in_market']*100:>5.1f}%")

    print("\n=== Test 3: Multi-ETF rotation across all 5 ETFs ===", flush=True)
    common_start = max(eligible_starts.values())
    r3 = multi_etf_rotation(triple_trades, closes, window_start=common_start)
    st3 = compute_stats(r3["equity"], common_start)
    st3["time_in_market"] = time_in_market(r3["n_active"].astype(bool).astype(float),
                                            common_start, r3["equity"].index[-1])
    # Mean number of active ETFs when in market
    win_mask = (r3["n_active"].index >= common_start) & (r3["n_active"] > 0)
    st3["mean_n_active_when_in"] = float(r3["n_active"].loc[win_mask].mean()) if win_mask.any() else 0.0
    win = (r3["equity"].index >= common_start)
    eq3_window = r3["equity"].loc[win]
    eq3_window = eq3_window / eq3_window.iloc[0]
    print(f"  totRet {st3['total_return']*100:+7.1f}%  CAGR {st3['cagr']*100:+6.1f}%  "
          f"Sharpe {st3['sharpe']:+.2f}  MaxDD {st3['max_dd']*100:>5.1f}%  "
          f"TimeInMkt {st3['time_in_market']*100:>5.1f}%  AvgActiveWhenIn {st3['mean_n_active_when_in']:.2f}")

    # Baselines for comparison: buy-and-hold (each ETF) and the existing
    # triple-combo equity curves.
    print("\n=== Baselines (for comparison) ===", flush=True)
    baselines = {}
    for etf in ETFS:
        eq_data = triple_eq.get(etf)
        if not eq_data:
            continue
        bh_close = closes[etf]
        win = (bh_close.index >= eligible_starts[etf])
        bh_eq_full = bh_close.loc[win]
        bh_eq_full = bh_eq_full / bh_eq_full.iloc[0]
        bh_stats = compute_stats(bh_close, eligible_starts[etf])
        bh_stats["time_in_market"] = 1.0

        # Triple combo: read from saved equity curve in OOS file
        tri_dates = pd.to_datetime(eq_data["dates"])
        tri_eq = pd.Series(eq_data["strategy"], index=tri_dates)
        tri_stats = compute_stats(tri_eq, eligible_starts[etf])
        # Time-in-market for triple from trade list
        in_days = sum(t["holding_days"] for t in triple_trades[etf])
        tri_stats["time_in_market"] = in_days / len(tri_eq)

        baselines[etf] = {
            "buy_and_hold": {
                "dates": [d.strftime("%Y-%m-%d") for d in bh_eq_full.index],
                "equity": round_series(bh_eq_full.values),
                **bh_stats,
            },
            "triple_combo": {
                # Equity dates and values are already in the OOS file
                "dates": eq_data["dates"],
                "equity": eq_data["strategy"],
                **tri_stats,
            },
        }
        print(f"  {etf:5}  BH totRet {bh_stats['total_return']*100:+7.1f}%  "
              f"CAGR {bh_stats['cagr']*100:+6.1f}%  Sharpe {bh_stats['sharpe']:+.2f}  "
              f"TripleCombo totRet {tri_stats['total_return']*100:+7.1f}%  "
              f"CAGR {tri_stats['cagr']*100:+6.1f}%  Sharpe {tri_stats['sharpe']:+.2f}")

    # SPY buy-and-hold as the cross-ETF benchmark for test 3
    spy_eq = closes["CSP1"]  # we already have SPY close via CSP1's trading proxy
    win = (spy_eq.index >= common_start)
    spy_bh_win = spy_eq.loc[win]
    spy_bh_win = spy_bh_win / spy_bh_win.iloc[0]
    spy_bh_stats = compute_stats(spy_eq, common_start)
    spy_bh_stats["time_in_market"] = 1.0
    print(f"  SPY (test 3 bench) totRet {spy_bh_stats['total_return']*100:+7.1f}%  "
          f"CAGR {spy_bh_stats['cagr']*100:+6.1f}%  Sharpe {spy_bh_stats['sharpe']:+.2f}")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_starts_per_etf": {etf: ts.strftime("%Y-%m-%d") for etf, ts in eligible_starts.items()},
        "test_1_regime_overlay": test1,
        "test_2_size_scaled": test2,
        "test_3_multi_etf_rotation": {
            "common_window_start": common_start.strftime("%Y-%m-%d"),
            "etfs_in_portfolio": list(triple_trades.keys()),
            "dates": [d.strftime("%Y-%m-%d") for d in eq3_window.index],
            "equity": round_series(eq3_window.values),
            "spy_buy_hold": {
                "dates": [d.strftime("%Y-%m-%d") for d in spy_bh_win.index],
                "equity": round_series(spy_bh_win.values),
                **spy_bh_stats,
            },
            **st3,
        },
        "baselines": baselines,
    }

    def clean(o):
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else o
        if isinstance(o, list):
            return [clean(x) for x in o]
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        return o

    OUT_PATH.write_text(json.dumps(clean(payload), indent=2), encoding="utf-8")

    # Final comparison table
    print()
    print("=" * 115)
    print("HEADLINE COMPARISON — total return / CAGR / Sharpe / max DD / time in market")
    print("=" * 115)
    cols = ("Strategy", "ETF", "TotRet%", "CAGR%", "Sharpe", "MaxDD%", "InMkt%")
    print(f"{cols[0]:<28} {cols[1]:<5} {cols[2]:>8} {cols[3]:>7} {cols[4]:>7} {cols[5]:>7} {cols[6]:>7}")
    print("-" * 115)
    for etf in ETFS:
        bs = baselines.get(etf, {})
        for label, src in [
            ("0. Buy-and-hold",            bs.get("buy_and_hold")),
            ("0. Triple combo (baseline)", bs.get("triple_combo")),
            ("1. Long-by-default regime",  test1.get(etf)),
            ("2. Size-scaled 50/100",      test2.get(etf)),
        ]:
            if not src: continue
            print(f"{label:<28} {etf:<5} "
                  f"{src.get('total_return',0)*100:>+7.1f} "
                  f"{src.get('cagr',0)*100:>+6.1f} "
                  f"{src.get('sharpe',0):>+7.2f} "
                  f"{src.get('max_dd',0)*100:>7.1f} "
                  f"{src.get('time_in_market',0)*100:>7.1f}")
        print()
    print(f"{'3. Multi-ETF rotation':<28} {'PORT':<5} "
          f"{st3.get('total_return',0)*100:>+7.1f} "
          f"{st3.get('cagr',0)*100:>+6.1f} "
          f"{st3.get('sharpe',0):>+7.2f} "
          f"{st3.get('max_dd',0)*100:>7.1f} "
          f"{st3.get('time_in_market',0)*100:>7.1f}")
    print(f"{'   SPY buy-and-hold (bench)':<28} {'SPY':<5} "
          f"{spy_bh_stats.get('total_return',0)*100:>+7.1f} "
          f"{spy_bh_stats.get('cagr',0)*100:>+6.1f} "
          f"{spy_bh_stats.get('sharpe',0):>+7.2f} "
          f"{spy_bh_stats.get('max_dd',0)*100:>7.1f} "
          f"{spy_bh_stats.get('time_in_market',0)*100:>7.1f}")
    print()
    print(f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
