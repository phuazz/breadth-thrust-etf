"""Single-indicator regime test: % of constituents above 200d MA.

Tests the question: would a simpler signal (just one breadth metric, with
LONG / NEUTRAL / SHORT thresholds) beat the multi-component composite +
50/150 sizing we landed on after two rounds of tuning?

For each ETF:
  1. Compute ma200_breadth daily from cached constituent prices.
  2. Sweep three strategy families:
       A. Long-only flat-cash : long 100% when ma200_b > L; flat else.
       B. Long-only base-50%  : long 100% when ma200_b > L; 50% else (always-on).
       C. Long-short         : long 100% when ma200_b > L; short 100% when
                                ma200_b < S; flat between thresholds.
     Long threshold L is swept over {50, 55, 60, 65, 70, 75, 80}.
     Short threshold S (family C only) over {10, 15, 20, 25, 30, 35, 40}.
  3. Report Sharpe / total return / max DD / time-long / time-short per cell.
  4. Compare the winners to:
       - Buy-and-hold
       - The OOS-validated 50/150 winner from the composite signal

Shorts assume zero borrow cost (the test is structural, not realistic).
A REAL inverse-ETF position would add ~4-6% per year drag.

Output: data/ma200_sweep.json

Run:
    python scripts/run_ma200_sweep.py
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

from backtest import download_soxx_ohlc  # noqa: E402
from etf_registry import get_etf, UNIVERSE_ETFS as ETFS  # noqa: E402
from run_improvements import COST_BPS, compute_stats  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "ma200_sweep.json"
LONG_THRESHOLDS = [50, 55, 60, 65, 70, 75, 80]
SHORT_THRESHOLDS = [10, 15, 20, 25, 30, 35, 40]
MA_PERIOD = 200


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def round_series(values, ndigits=4):
    out = []
    for v in values:
        try:
            f = float(v)
            out.append(round(f, ndigits) if not (math.isnan(f) or math.isinf(f)) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def load_constituent_prices(etf: str) -> pd.DataFrame:
    """Constituent close prices for `etf` from its cached parquet."""
    cache = DATA_DIR / f"prices_cache_{etf.lower()}.parquet"
    if not cache.exists():
        raise FileNotFoundError(f"No constituent price cache at {cache}")
    return pd.read_parquet(cache)


def compute_ma200_breadth(prices: pd.DataFrame, period: int = MA_PERIOD) -> pd.Series:
    """Per-day fraction of constituents above their `period`-day MA.

    Denominator is the count of constituents where BOTH the day's price and
    the day's MA are computable. A ticker without enough history yet, or
    one whose price is missing today, does not contribute to either the
    numerator or the denominator.

    `min_periods` is set to 90% of `period` (e.g. 180 for MA200) rather
    than the full window length. This tolerates the 1-2% sparse missingness
    typical of non-US constituents (UCITS sector funds with .L / .DE / .PA
    / .AS / .MI tickers — local holidays, ex-dividend gaps, intermittent
    prints). Under the strict full-window requirement, constituents with
    even 1% missingness silently lose their MA for every window touching
    a missing day; over a 200-day window that means *every* window, which
    for the Europe sector ETFs caused n_valid → 0 and froze breadth at
    a ffill'd stale value. With min_periods=180 a constituent only needs
    ~180 of the last 200 days to be valid — which matches what a human
    trader would consider "enough history to call the trend".

    For US constituents (S&P 500 via yfinance) with ~100% coverage, this
    change is a no-op: every window already has 200 valid observations.
    """
    min_p = max(1, int(period * 0.9))
    ma = prices.rolling(period, min_periods=min_p).mean()
    both_valid = prices.notna() & ma.notna()
    above = (prices > ma) & both_valid
    n_above = above.sum(axis=1)
    n_valid = both_valid.sum(axis=1)
    return (n_above / n_valid.replace(0, np.nan)).ffill().fillna(0)


def run_strategy(
    close: pd.Series,
    breadth: pd.Series,
    long_threshold: float,
    family: str,
    short_threshold: float | None = None,
    base_alloc: float = 0.0,
    cost: float = COST_BPS / 10_000,
    window_start: pd.Timestamp | None = None,
) -> dict:
    """Apply a regime-based allocation rule and return equity / alloc series.

    family in {"long_flat", "long_base50", "long_short"}.
    Allocations use yesterday's breadth reading (no look-ahead).
    """
    aligned = breadth.reindex(close.index, method="ffill").shift(1).fillna(0)
    alloc = pd.Series(base_alloc, index=close.index, dtype=float)
    long_mask = aligned >= long_threshold / 100.0
    alloc.loc[long_mask] = 1.0
    if family == "long_short" and short_threshold is not None:
        short_mask = aligned <= short_threshold / 100.0
        alloc.loc[short_mask] = -1.0
    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0
    daily = close.pct_change().fillna(0)
    strat_ret = alloc * daily
    turnover = alloc.diff().abs().fillna(0)
    strat_ret = strat_ret - turnover * cost
    equity = (1.0 + strat_ret).cumprod()
    return {"equity": equity, "alloc": alloc, "breadth_used": aligned}


def stats_for(equity: pd.Series, alloc: pd.Series, window_start: pd.Timestamp) -> dict:
    st = compute_stats(equity, window_start)
    a = alloc.loc[alloc.index >= window_start]
    st["time_long"] = float((a > 0).mean())
    st["time_short"] = float((a < 0).mean())
    st["time_neutral"] = float((a == 0).mean())
    return st


def main() -> int:
    print("Loading constituent prices + computing ma200 breadth per ETF ...", flush=True)
    per_etf: dict[str, dict] = {}
    eligible_starts: dict[str, pd.Timestamp] = {}

    for etf in ETFS:
        cfg = get_etf(etf)
        proxy = cfg.get("yfinance_trading_proxy") or etf
        try:
            cprices = load_constituent_prices(etf)
        except FileNotFoundError as e:
            print(f"  {etf:5}  SKIP -- {e}")
            continue
        ma200_b = compute_ma200_breadth(cprices, MA_PERIOD)
        # Eligible start = first date ma200 is defined for >= 50% of constituents
        # (avoid period where breadth is computed on a too-small universe).
        n_with = (cprices.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean().notna()).sum(axis=1)
        n_universe = cprices.shape[1]
        eligible_start = n_with[n_with >= 0.5 * n_universe].index.min()
        if pd.isna(eligible_start):
            eligible_start = ma200_b.index[MA_PERIOD]
        eligible_starts[etf] = eligible_start

        # Download the traded proxy ETF close for backtest
        dl_start = (cprices.index.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end = (cprices.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        ohlc = download_soxx_ohlc(dl_start, dl_end, etf=proxy, yf_symbol=proxy)
        ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
        close = ohlc["Close"].astype(float)
        per_etf[etf] = {"close": close, "ma200_b": ma200_b, "eligible": eligible_start}
        print(f"  {etf:5}  ma200_b defined from {eligible_start.date()}  "
              f"({len(close)} price rows, {cprices.shape[1]} constituents)")

    # Compute baselines (BH per ETF and 50/150 winner per ETF from existing OOS)
    print("\nLoading 50/150 baseline equities from backtest_<etf>_oos.json ...")
    baselines: dict[str, dict] = {}
    for etf in per_etf:
        eligible = eligible_starts[etf]
        close = per_etf[etf]["close"]
        bh_close = close.loc[close.index >= eligible]
        bh_eq = (bh_close / bh_close.iloc[0])
        bh_stats = compute_stats(bh_close, eligible)
        bh_stats["time_long"] = 1.0
        bh_stats["time_short"] = 0.0
        bh_stats["time_neutral"] = 0.0
        baselines[etf] = {"buy_and_hold": {**{k: _safe(v) for k, v in bh_stats.items()}}}

    print("\n=== Family A: LONG-only / flat cash else ===")
    family_a: dict[str, list[dict]] = {}
    for etf in per_etf:
        rows = []
        for L in LONG_THRESHOLDS:
            r = run_strategy(per_etf[etf]["close"], per_etf[etf]["ma200_b"],
                              long_threshold=L, family="long_flat",
                              base_alloc=0.0,
                              window_start=eligible_starts[etf])
            st = stats_for(r["equity"], r["alloc"], eligible_starts[etf])
            rows.append({"long_threshold": L,
                          **{k: _safe(v) if isinstance(v, float) else v for k, v in st.items()}})
        family_a[etf] = rows
        best = max(rows, key=lambda r: r["sharpe"] or -1e9)
        print(f"  {etf:5}  best L={best['long_threshold']}  "
              f"Shp {best['sharpe']:+.2f}  totRet {best['total_return']*100:+.0f}%  "
              f"DD {best['max_dd']*100:.0f}%  TimeLong {best['time_long']*100:.0f}%")

    print("\n=== Family B: LONG / base-50% else (always invested) ===")
    family_b: dict[str, list[dict]] = {}
    for etf in per_etf:
        rows = []
        for L in LONG_THRESHOLDS:
            r = run_strategy(per_etf[etf]["close"], per_etf[etf]["ma200_b"],
                              long_threshold=L, family="long_base50",
                              base_alloc=0.5,
                              window_start=eligible_starts[etf])
            st = stats_for(r["equity"], r["alloc"], eligible_starts[etf])
            rows.append({"long_threshold": L,
                          **{k: _safe(v) if isinstance(v, float) else v for k, v in st.items()}})
        family_b[etf] = rows
        best = max(rows, key=lambda r: r["sharpe"] or -1e9)
        print(f"  {etf:5}  best L={best['long_threshold']}  "
              f"Shp {best['sharpe']:+.2f}  totRet {best['total_return']*100:+.0f}%  "
              f"DD {best['max_dd']*100:.0f}%")

    print("\n=== Family D: LONG-leveraged-150% / base-50% else (apples-to-apples with composite 50/150) ===")
    family_d: dict[str, list[dict]] = {}
    for etf in per_etf:
        rows = []
        for L in LONG_THRESHOLDS:
            # Reuse run_strategy but bump the long alloc to 1.5 by post-processing
            aligned = per_etf[etf]["ma200_b"].reindex(
                per_etf[etf]["close"].index, method="ffill").shift(1).fillna(0)
            alloc = pd.Series(0.5, index=per_etf[etf]["close"].index, dtype=float)
            alloc.loc[aligned >= L / 100.0] = 1.5
            alloc.loc[alloc.index < eligible_starts[etf]] = 0.0
            daily = per_etf[etf]["close"].pct_change().fillna(0)
            strat_ret = alloc * daily
            turnover = alloc.diff().abs().fillna(0)
            strat_ret = strat_ret - turnover * (COST_BPS / 10_000)
            equity = (1.0 + strat_ret).cumprod()
            st = stats_for(equity, alloc, eligible_starts[etf])
            rows.append({"long_threshold": L,
                          **{k: _safe(v) if isinstance(v, float) else v for k, v in st.items()}})
        family_d[etf] = rows
        best = max(rows, key=lambda r: r["sharpe"] or -1e9)
        print(f"  {etf:5}  best L={best['long_threshold']}  "
              f"Shp {best['sharpe']:+.2f}  totRet {best['total_return']*100:+.0f}%  "
              f"DD {best['max_dd']*100:.0f}%")

    print("\n=== Family C: LONG/SHORT (long > L; short < S; flat between) ===")
    family_c: dict[str, list[dict]] = {}
    for etf in per_etf:
        rows = []
        for L in LONG_THRESHOLDS:
            for S in SHORT_THRESHOLDS:
                r = run_strategy(per_etf[etf]["close"], per_etf[etf]["ma200_b"],
                                  long_threshold=L, short_threshold=S,
                                  family="long_short",
                                  base_alloc=0.0,
                                  window_start=eligible_starts[etf])
                st = stats_for(r["equity"], r["alloc"], eligible_starts[etf])
                rows.append({"long_threshold": L, "short_threshold": S,
                              **{k: _safe(v) if isinstance(v, float) else v for k, v in st.items()}})
        family_c[etf] = rows
        best = max(rows, key=lambda r: r["sharpe"] or -1e9)
        print(f"  {etf:5}  best L={best['long_threshold']}/S={best['short_threshold']}  "
              f"Shp {best['sharpe']:+.2f}  totRet {best['total_return']*100:+.0f}%  "
              f"DD {best['max_dd']*100:.0f}%  TimeShort {best['time_short']*100:.0f}%")

    # Save winning equity curves (one per family per ETF) for the dashboard.
    winners_eq: dict[str, dict] = {}
    for etf in per_etf:
        winners_eq[etf] = {}
        # Family A winner
        a_best = max(family_a[etf], key=lambda r: r["sharpe"] or -1e9)
        r = run_strategy(per_etf[etf]["close"], per_etf[etf]["ma200_b"],
                          long_threshold=a_best["long_threshold"], family="long_flat",
                          base_alloc=0.0, window_start=eligible_starts[etf])
        eq = r["equity"].loc[r["equity"].index >= eligible_starts[etf]]
        eq = eq / eq.iloc[0]
        winners_eq[etf]["family_a"] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
            "equity": round_series(eq.values),
            "label": f"Long-flat L={a_best['long_threshold']}",
            **a_best,
        }
        # Family B winner
        b_best = max(family_b[etf], key=lambda r: r["sharpe"] or -1e9)
        r = run_strategy(per_etf[etf]["close"], per_etf[etf]["ma200_b"],
                          long_threshold=b_best["long_threshold"], family="long_base50",
                          base_alloc=0.5, window_start=eligible_starts[etf])
        eq = r["equity"].loc[r["equity"].index >= eligible_starts[etf]]
        eq = eq / eq.iloc[0]
        winners_eq[etf]["family_b"] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
            "equity": round_series(eq.values),
            "label": f"Long-base50 L={b_best['long_threshold']}",
            **b_best,
        }
        # Family D winner (long-leveraged)
        d_best = max(family_d[etf], key=lambda r: r["sharpe"] or -1e9)
        aligned = per_etf[etf]["ma200_b"].reindex(per_etf[etf]["close"].index, method="ffill").shift(1).fillna(0)
        alloc_d = pd.Series(0.5, index=per_etf[etf]["close"].index, dtype=float)
        alloc_d.loc[aligned >= d_best["long_threshold"] / 100.0] = 1.5
        alloc_d.loc[alloc_d.index < eligible_starts[etf]] = 0.0
        daily = per_etf[etf]["close"].pct_change().fillna(0)
        strat_ret = alloc_d * daily - alloc_d.diff().abs().fillna(0) * (COST_BPS / 10_000)
        eq = (1.0 + strat_ret).cumprod()
        eq = eq.loc[eq.index >= eligible_starts[etf]]
        eq = eq / eq.iloc[0]
        winners_eq[etf]["family_d"] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
            "equity": round_series(eq.values),
            "label": f"50/150 + MA200 L={d_best['long_threshold']}",
            **d_best,
        }
        # Family C winner
        c_best = max(family_c[etf], key=lambda r: r["sharpe"] or -1e9)
        r = run_strategy(per_etf[etf]["close"], per_etf[etf]["ma200_b"],
                          long_threshold=c_best["long_threshold"],
                          short_threshold=c_best["short_threshold"],
                          family="long_short",
                          base_alloc=0.0, window_start=eligible_starts[etf])
        eq = r["equity"].loc[r["equity"].index >= eligible_starts[etf]]
        eq = eq / eq.iloc[0]
        winners_eq[etf]["family_c"] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
            "equity": round_series(eq.values),
            "label": f"Long/short L={c_best['long_threshold']} S={c_best['short_threshold']}",
            **c_best,
        }
        # BH series for the chart
        close = per_etf[etf]["close"]
        bh_close = close.loc[close.index >= eligible_starts[etf]]
        bh_eq = (bh_close / bh_close.iloc[0])
        winners_eq[etf]["buy_and_hold"] = {
            "dates": [d.strftime("%Y-%m-%d") for d in bh_eq.index],
            "equity": round_series(bh_eq.values),
            "label": f"{etf} buy-and-hold",
            **baselines[etf]["buy_and_hold"],
        }

    # ---------- Per-ETF detail: breadth series, long-episodes, monitor state
    print("\n=== Building per-ETF detail blocks (breadth series, episodes, monitor) ===", flush=True)
    detail: dict[str, dict] = {}
    monitor: dict[str, dict] = {}
    for etf in per_etf:
        ma200_b = per_etf[etf]["ma200_b"]
        close = per_etf[etf]["close"]
        eligible = eligible_starts[etf]
        # Family D winner threshold for this ETF
        d_best = max(family_d[etf], key=lambda r: r["sharpe"] or -1e9)
        L = d_best["long_threshold"]
        # Align ma200_b to close trading days, restrict to eligible window
        aligned_b = ma200_b.reindex(close.index, method="ffill")
        win_mask = aligned_b.index >= eligible
        b_window = aligned_b.loc[win_mask]
        # Downsample to weekly for the chart (Friday close) to keep payload small
        weekly_b = b_window.resample("W-FRI").last().dropna()
        # Long-episodes: contiguous runs where lagged b >= L/100
        regime = (aligned_b.shift(1) >= L / 100.0).astype(int)
        regime.loc[regime.index < eligible] = 0
        # Find transitions
        transitions = regime.diff().fillna(0)
        entries = regime[(transitions == 1)].index
        exits = regime[(transitions == -1)].index
        # If still in long state at end, treat last date as exit
        episodes = []
        if len(entries) > 0:
            for entry in entries:
                # Find next exit after this entry
                later_exits = exits[exits > entry]
                exit_ = later_exits[0] if len(later_exits) > 0 else close.index[-1]
                # Return over the episode (close-to-close)
                if entry in close.index and exit_ in close.index:
                    entry_c = float(close.loc[entry])
                    exit_c = float(close.loc[exit_])
                    ret = exit_c / entry_c - 1.0
                    days = (exit_ - entry).days
                    episodes.append({
                        "entry": entry.strftime("%Y-%m-%d"),
                        "exit": exit_.strftime("%Y-%m-%d"),
                        "calendar_days": int(days),
                        "underlying_return": _safe(ret),
                        "ma200_breadth_at_entry": _safe(aligned_b.loc[entry]) if entry in aligned_b.index else None,
                    })
        # Current state for Monitor tab
        latest_date = close.index[-1]
        latest_b = float(aligned_b.iloc[-1]) if pd.notna(aligned_b.iloc[-1]) else None
        in_long = latest_b is not None and latest_b >= L / 100.0
        current_alloc = 150 if in_long else 50
        # Days in current state
        days_in_state = 0
        if len(regime) > 1:
            current_state = int(regime.iloc[-1])
            # Walk back through regime series to find where state last changed
            for i in range(len(regime) - 2, -1, -1):
                if int(regime.iloc[i]) != current_state:
                    days_in_state = (regime.index[-1] - regime.index[i + 1]).days
                    break
            else:
                days_in_state = (regime.index[-1] - regime.index[0]).days
        detail[etf] = {
            "breadth_dates": [d.strftime("%Y-%m-%d") for d in weekly_b.index],
            "breadth_pct": round_series(weekly_b.values * 100, 2),  # in percent
            "chosen_long_threshold": L,
            "episodes": episodes,
            "n_episodes": len(episodes),
        }
        monitor[etf] = {
            "etf": etf,
            "trading_proxy": get_etf(etf).get("yfinance_trading_proxy") or etf,
            "as_of": latest_date.strftime("%Y-%m-%d"),
            "ma200_breadth_pct": round(latest_b * 100, 1) if latest_b is not None else None,
            "long_threshold_pct": L,
            "in_long_state": bool(in_long),
            "current_allocation_pct": current_alloc,
            "days_in_state": int(days_in_state),
            "winner_sharpe": d_best["sharpe"],
            "winner_total_return": d_best["total_return"],
        }
        print(f"  {etf:5}  L={L:>3}  ma200_b now {latest_b*100:>5.1f}%  "
              f"-> {'LONG (150%)' if in_long else 'BASE (50%)'}  "
              f"{days_in_state}d in state  {len(episodes)} episodes total")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "ma_period": MA_PERIOD,
        "long_thresholds": LONG_THRESHOLDS,
        "short_thresholds": SHORT_THRESHOLDS,
        "eligible_starts": {e: s.strftime("%Y-%m-%d") for e, s in eligible_starts.items()},
        "family_a_long_flat": family_a,
        "family_b_long_base50": family_b,
        "family_c_long_short": family_c,
        "family_d_long_leveraged_base50": family_d,
        "winner_equity_curves": winners_eq,
        "baselines": baselines,
        "per_etf_detail": detail,
        "monitor": monitor,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Headline comparison vs the 50/150 composite winner
    print()
    print("=" * 115)
    print("MA200 SWEEP — best Sharpe per family per ETF, compared to BH and the 50/150 composite winner")
    print("=" * 115)
    print(f"{'ETF':<5} {'BH Shp':>7} {'A: long-flat':<20} {'B: base50/100':<20} "
          f"{'D: base50/150':<20} {'C: long/short':<20} {'50/150 composite'}")
    print("-" * 130)
    # Load tuning to get 50/150 winner numbers
    tuning_path = DATA_DIR / "tuning.json"
    tuning = json.loads(tuning_path.read_text(encoding="utf-8")) if tuning_path.exists() else {}
    for etf in per_etf:
        bh = baselines[etf]["buy_and_hold"]
        a = max(family_a[etf], key=lambda r: r["sharpe"] or -1e9)
        b = max(family_b[etf], key=lambda r: r["sharpe"] or -1e9)
        c = max(family_c[etf], key=lambda r: r["sharpe"] or -1e9)
        d = max(family_d[etf], key=lambda r: r["sharpe"] or -1e9)
        grid_rows = tuning.get("base_thrust_grid", {}).get(etf, [])
        composite = max(grid_rows, key=lambda r: r.get("sharpe") or -1e9) if grid_rows else None
        a_str = f"L={a['long_threshold']}: {a['sharpe']:+.2f}/{a['total_return']*100:+.0f}%"
        b_str = f"L={b['long_threshold']}: {b['sharpe']:+.2f}/{b['total_return']*100:+.0f}%"
        d_str = f"L={d['long_threshold']}: {d['sharpe']:+.2f}/{d['total_return']*100:+.0f}%"
        c_str = f"L{c['long_threshold']}/S{c['short_threshold']}: {c['sharpe']:+.2f}/{c['total_return']*100:+.0f}%"
        comp_str = (f"b{composite['base_pct']}/t{composite['thrust_pct']}: "
                    f"{composite['sharpe']:+.2f}/{composite['total_return']*100:+.0f}%"
                    if composite else "—")
        print(f"{etf:<5} {bh['sharpe']:>+7.2f} {a_str:<20} {b_str:<20} {d_str:<20} {c_str:<20} {comp_str}")
    print()
    print(f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
