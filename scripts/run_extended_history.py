"""Extended-history test: does the MA200 + 50/100 regime overlay add value
through bear markets (2008 GFC, 2000-2002 dot-com)?

The 2018-2026 backtest window contains essentially no extended bear
market — even 2022 was a slow grind, not a -50% crash. Buy-and-hold has
a structural advantage there. The regime overlay is designed to save
capital during severe drawdowns; we can only test that on data that
includes such drawdowns.

DATA APPROACH (caveat-aware):

  iShares only provides point-in-time constituents from ~2014. For 2000-
  2014 we substitute the LATEST CSP1 / SOXX constituent rosters (today's
  members) and pull yfinance prices back to 2000-01-01. This is
  survivor-biased: only stocks that survived to today are in the universe.

  Bias direction: SLIGHTLY UPWARD on breadth (today's S&P 500 members
  tend to be stronger names that survived). But the broad pattern — that
  MA200 breadth collapses to near zero during 2008-2009 and 2000-2002 —
  is robust to this bias and is the signal the regime overlay relies on.

  This is therefore a useful but not perfect test of the strategy in
  extended bear markets. The verdict is "directional, not precise".

WHAT WE TEST:

  - Fixed L threshold sweep ∈ {40, 45, 50, 55, 60, 65, 70} for both:
      a) 50/100 (50% base + 100% on signal, unleveraged)
      b)  0/100 (flat-cash off signal + 100% on signal, REGIME OVERLAY)
  - Apply to today's S&P 500 (proxy: SPY) and today's SOXX (proxy: SOXX)
  - Window: 2000-01-01 to 2026-05-19
  - Sub-period decomposition: pre-dot-com, dot-com bust, recovery, GFC,
    post-GFC, 2018, COVID, 2022, AI rally, recent.

Output: data/extended_history.json

Run:
    python scripts/run_extended_history.py
"""

from __future__ import annotations

import json
import math
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_improvements import COST_BPS, compute_stats  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "extended_history.json"
PRICE_CACHE = DATA_DIR / "extended_history_prices_cache.parquet"

START_DATE = "2000-01-01"
END_DATE = "2026-05-19"
MA_PERIOD = 200
L_GRID = [40, 45, 50, 55, 60, 65, 70]

SUB_PERIODS = [
    ("2000_pre_dotcom_bust",   "2000-01-01", "2000-03-24"),
    ("2000_2002_dotcom_bust",  "2000-03-24", "2002-10-09"),
    ("2002_2007_recovery",     "2002-10-09", "2007-10-09"),
    ("2007_2009_gfc",          "2007-10-09", "2009-03-09"),
    ("2009_2018_bull",         "2009-03-09", "2018-09-20"),
    ("2018q4_correction",      "2018-09-20", "2018-12-26"),
    ("2019_pre_covid",         "2018-12-26", "2020-02-19"),
    ("2020_covid",             "2020-02-19", "2020-12-31"),
    ("2021_2022_recovery",     "2020-12-31", "2022-01-03"),
    ("2022_inflation",         "2022-01-03", "2023-01-01"),
    ("2023_ai_rally",          "2023-01-01", "2024-01-01"),
    ("2024_2026_recent",       "2024-01-01", "2026-12-31"),
]


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


def load_latest_tickers(etf: str) -> list[str]:
    path = DATA_DIR / f"constituents_{etf.lower()}.json"
    blob = json.loads(path.read_text(encoding="utf-8"))
    snapshots = blob["snapshots"]
    latest = sorted(snapshots.keys())[-1]
    return snapshots[latest]["tickers"]


def download_prices(tickers: list[str], start: str, end: str,
                     cache_path: Path = PRICE_CACHE,
                     batch_size: int = 50) -> pd.DataFrame:
    """yfinance download with parquet cache. Batched to avoid request bloat."""
    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if (cached.index.min() <= pd.Timestamp(start) and
                cached.index.max() >= pd.Timestamp(end) and
                set(tickers).issubset(set(cached.columns))):
            print(f"  Cache hit ({len(cached)} rows × {len(cached.columns)} tickers)")
            return cached.loc[start:end, tickers]
    print(f"  Downloading {len(tickers)} tickers from yfinance ({start} → {end}) ...")
    all_close: dict[str, pd.Series] = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            raw = yf.download(batch, start=start, end=end, auto_adjust=True,
                               progress=False, threads=True, group_by="column")
        except Exception as e:
            print(f"  Batch {i}-{i+batch_size}: ERR {e}")
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            if "Close" in raw.columns.get_level_values(0):
                close = raw["Close"]
                for t in close.columns:
                    all_close[t] = close[t]
            else:
                print(f"  Batch {i}: no Close column")
        else:
            for col in raw.columns:
                if col == "Close":
                    all_close[batch[0]] = raw["Close"]
        print(f"    [{i + len(batch):>3}/{len(tickers)}] done")
    if not all_close:
        raise RuntimeError("No price data fetched")
    df = pd.DataFrame(all_close).sort_index()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    return df


def compute_ma_breadth(prices: pd.DataFrame, period: int = 200) -> pd.Series:
    ma = prices.rolling(period, min_periods=period).mean()
    above = (prices > ma) & ma.notna()
    n_above = above.sum(axis=1)
    n_valid = ma.notna().sum(axis=1)
    return (n_above / n_valid.replace(0, np.nan)).ffill().fillna(0)


def run_strategy(close: pd.Series, breadth: pd.Series, L_pct: float,
                  base: float, on: float, cost: float = COST_BPS / 10_000,
                  window_start: pd.Timestamp | None = None) -> dict:
    aligned = breadth.reindex(close.index, method="ffill").shift(1).fillna(0)
    alloc = pd.Series(base, index=close.index, dtype=float)
    alloc.loc[aligned >= L_pct / 100.0] = on
    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0
    daily = close.pct_change().fillna(0)
    strat = alloc * daily
    turnover = alloc.diff().abs().fillna(0)
    strat = strat - turnover * cost
    equity = (1.0 + strat).cumprod()
    return {"equity": equity, "alloc": alloc}


def sub_period_stats(equity: pd.Series, periods: list[tuple]) -> list[dict]:
    out = []
    for label, s, e in periods:
        eq = equity.loc[(equity.index >= pd.Timestamp(s)) &
                        (equity.index < pd.Timestamp(e))].copy()
        if len(eq) < 5:
            out.append({"label": label, "start": s, "end": e,
                        "sharpe": None, "total_return": None, "max_dd": None,
                        "n_days": int(len(eq))})
            continue
        eq = eq / eq.iloc[0]
        daily = eq.pct_change().fillna(0)
        sh = float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else 0.0
        peaks = eq.cummax()
        dd = float((1.0 - eq / peaks).max())
        out.append({"label": label, "start": s, "end": e,
                    "sharpe": _safe(sh), "total_return": _safe(float(eq.iloc[-1] - 1)),
                    "max_dd": _safe(dd), "n_days": int(len(eq))})
    return out


def main() -> int:
    print("Loading latest constituent rosters ...")
    spy_tickers = load_latest_tickers("CSP1")
    soxx_tickers = load_latest_tickers("SOXX")
    print(f"  SPY universe (current CSP1): {len(spy_tickers)} tickers")
    print(f"  SOXX universe (current SOXX): {len(soxx_tickers)} tickers")

    # Also need SPY and SOXX prices themselves for BH and trading proxy
    all_tickers = list(set(spy_tickers + soxx_tickers + ["SPY", "SOXX"]))

    print(f"\nFetching yfinance prices for {len(all_tickers)} tickers, 2000-2026 ...")
    prices = download_prices(all_tickers, START_DATE, END_DATE)
    print(f"  Loaded {len(prices)} rows × {prices.shape[1]} tickers")

    # SPY and SOXX prices (the things we trade)
    spy_close = prices["SPY"].dropna()
    soxx_close = prices["SOXX"].dropna()

    # Universe panels
    spy_universe = prices[[t for t in spy_tickers if t in prices.columns]].dropna(how="all")
    soxx_universe = prices[[t for t in soxx_tickers if t in prices.columns]].dropna(how="all")

    print("\nComputing MA200 breadth on full universe ...")
    spy_breadth = compute_ma_breadth(spy_universe, MA_PERIOD)
    soxx_breadth = compute_ma_breadth(soxx_universe, MA_PERIOD)
    # Eligible start: when at least ~50% of universe has 200d MA defined
    def eligible_date(universe):
        n_with = universe.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean().notna().sum(axis=1)
        return n_with[n_with >= universe.shape[1] * 0.5].index.min()
    spy_eligible = eligible_date(spy_universe)
    soxx_eligible = eligible_date(soxx_universe)
    print(f"  SPY eligible from {spy_eligible.date()}  "
          f"(50% of {spy_universe.shape[1]} tickers have 200d MA)")
    print(f"  SOXX eligible from {soxx_eligible.date()}")

    # ----- Run strategies -----
    print("\nRunning strategies ...")
    results = {}
    for traded_label, close, breadth, eligible in [
        ("SPY", spy_close, spy_breadth, spy_eligible),
        ("SOXX", soxx_close, soxx_breadth, soxx_eligible),
    ]:
        # Strategy variants
        variants = {}
        for L in L_GRID:
            # 50/100
            r = run_strategy(close, breadth, L_pct=L, base=0.5, on=1.0,
                              window_start=eligible)
            st = compute_stats(r["equity"], eligible)
            variants[f"50/100_L={L}"] = {**{k: _safe(v) if isinstance(v, float) else v
                                              for k, v in st.items()},
                                          "L": L, "base": 0.5, "on": 1.0}
            # 0/100 (pure regime overlay)
            r2 = run_strategy(close, breadth, L_pct=L, base=0.0, on=1.0,
                                window_start=eligible)
            st2 = compute_stats(r2["equity"], eligible)
            variants[f"0/100_L={L}"] = {**{k: _safe(v) if isinstance(v, float) else v
                                              for k, v in st2.items()},
                                          "L": L, "base": 0.0, "on": 1.0}
        # BH for comparison
        bh_close = close.loc[close.index >= eligible]
        bh_stats = compute_stats(bh_close, eligible)
        variants["buy_and_hold"] = {**{k: _safe(v) if isinstance(v, float) else v
                                          for k, v in bh_stats.items()},
                                      "L": None, "base": 1.0, "on": 1.0}
        # Equity curve for the headline L=60 50/100 variant
        r = run_strategy(close, breadth, L_pct=60, base=0.5, on=1.0,
                          window_start=eligible)
        eq_window = r["equity"].loc[r["equity"].index >= eligible]
        eq_window = eq_window / eq_window.iloc[0]
        # Equity curve for 0/100 L=60 (pure regime)
        r2 = run_strategy(close, breadth, L_pct=60, base=0.0, on=1.0,
                          window_start=eligible)
        eq_overlay = r2["equity"].loc[r2["equity"].index >= eligible]
        eq_overlay = eq_overlay / eq_overlay.iloc[0]
        # BH curve
        bh_norm = bh_close / bh_close.iloc[0]
        # Breadth curve (sample weekly for size)
        breadth_w = breadth.reindex(close.index, method="ffill").loc[eligible:].resample("W-FRI").last()
        # Sub-period stats for the two headline variants and BH
        sp_5050 = sub_period_stats(r["equity"], SUB_PERIODS)
        sp_0100 = sub_period_stats(r2["equity"], SUB_PERIODS)
        sp_bh = sub_period_stats(close, SUB_PERIODS)
        results[traded_label] = {
            "eligible_start": eligible.strftime("%Y-%m-%d"),
            "universe_size": spy_universe.shape[1] if traded_label == "SPY" else soxx_universe.shape[1],
            "variants": variants,
            "headline_equity_50_100_L60": {
                "dates": [d.strftime("%Y-%m-%d") for d in eq_window.index],
                "equity": round_series(eq_window.values),
            },
            "headline_equity_0_100_L60": {
                "dates": [d.strftime("%Y-%m-%d") for d in eq_overlay.index],
                "equity": round_series(eq_overlay.values),
            },
            "buy_and_hold_equity": {
                "dates": [d.strftime("%Y-%m-%d") for d in bh_norm.index],
                "equity": round_series(bh_norm.values),
            },
            "breadth_weekly": {
                "dates": [d.strftime("%Y-%m-%d") for d in breadth_w.index],
                "ma200_breadth_pct": round_series([v * 100 for v in breadth_w.values], 2),
            },
            "sub_periods": {
                "50_100_L60": sp_5050,
                "0_100_L60": sp_0100,
                "buy_and_hold": sp_bh,
            },
        }
        # Print headline table
        print(f"\n  {traded_label} ({results[traded_label]['eligible_start']} → present):")
        print(f"    {'Variant':<22} {'Sharpe':>7} {'TotRet':>9} {'MaxDD':>7}")
        for L in L_GRID:
            v = variants[f"50/100_L={L}"]
            print(f"    50/100  L={L:<3}             {v['sharpe']:>+7.2f} "
                  f"{v['total_return']*100:>+8.0f}% {v['max_dd']*100:>6.1f}%")
        for L in L_GRID:
            v = variants[f"0/100_L={L}"]
            print(f"    0/100   L={L:<3} (regime ovly){v['sharpe']:>+7.2f} "
                  f"{v['total_return']*100:>+8.0f}% {v['max_dd']*100:>6.1f}%")
        bh = variants["buy_and_hold"]
        print(f"    Buy-and-hold              {bh['sharpe']:>+7.2f} "
              f"{bh['total_return']*100:>+8.0f}% {bh['max_dd']*100:>6.1f}%")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_date": START_DATE,
        "end_date": END_DATE,
        "ma_period": MA_PERIOD,
        "L_grid": L_GRID,
        "sub_periods": [{"label": l, "start": s, "end": e} for l, s, e in SUB_PERIODS],
        "data_caveat": (
            "Survivor-biased universe: uses TODAY's CSP1 and SOXX constituent "
            "rosters back to 2000. Breadth metric is slightly upward-biased "
            "(more stocks above MA200 than the true historical reading) but "
            "the broad pattern of breadth collapsing during 2000-2002 and "
            "2007-2009 is robust. This is a useful but not perfect test."
        ),
        "results": results,
    }

    def clean(o):
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else o
        if isinstance(o, list): return [clean(x) for x in o]
        if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
        return o
    payload = clean(payload)

    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    # Sub-period summary for SPY
    print()
    print("=" * 95)
    print("SUB-PERIOD SHARPE — SPY: 50/100 L=60  vs  0/100 L=60 (regime)  vs  buy-and-hold")
    print("=" * 95)
    print(f"  {'Period':<26} {'50/100':>8} {'0/100':>8} {'BH':>8}   "
          f"{'Δ 50/100−BH':>11} {'Δ 0/100−BH':>11}")
    sp = results["SPY"]["sub_periods"]
    for i in range(len(SUB_PERIODS)):
        a = sp["50_100_L60"][i]
        b = sp["0_100_L60"][i]
        c = sp["buy_and_hold"][i]
        if a.get("sharpe") is None or c.get("sharpe") is None:
            continue
        d_a = a["sharpe"] - c["sharpe"]
        d_b = b["sharpe"] - c["sharpe"]
        print(f"  {a['label']:<26} {a['sharpe']:>+8.2f} {b['sharpe']:>+8.2f} "
              f"{c['sharpe']:>+8.2f}   {d_a:>+11.2f} {d_b:>+11.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
