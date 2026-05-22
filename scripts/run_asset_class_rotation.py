"""Strategy B — asset-class rotation (Phase 2).

Where Strategy A (top-K-by-breadth, see run_topk_robustness.py) operates
within US equities by ranking the SECTOR ETFs on constituent-level breadth,
Strategy B operates ACROSS asset classes by ranking BROAD asset-class ETFs
on their own price-level momentum.

Universe (14 US-listed broad-asset ETFs — clean yfinance pricing, all with
long histories back to at least 2007-2010):

  US equity      :  SPY (large), IJR (small), QQQ (NASDAQ-100 tech)
  Intl developed :  EFA (MSCI EAFE), VGK (Europe), EWJ (Japan)
  Emerging Mkts  :  EEM (MSCI EM)
  Real estate    :  VNQ (US REITs broad)
  Commodities    :  GLD (gold), DBC (broad commodities)
  Bonds          :  TLT (20+y Treasury), IEF (7-10y Treasury), TIP (TIPS),
                    HYG (high-yield credit)

Signal: distance above own 200-day moving average per ETF
        signal_i = (close_i - MA200_i) / MA200_i
The signal is well-defined for any time series (equity, bond, commodity)
and has a clean regime-following interpretation: positive means the asset
is above its long-term trend, negative means below.

Trading rules:
  - Rank the 14 ETFs each Friday close by current signal.
  - Hold the top K with positive signal. ETFs with negative signal (below
    their 200d MA) are EXCLUDED — unlike Strategy A which is always 100%
    invested, this strategy has a built-in cash floor when broadly weak.
  - Weight each held ETF by its signal share among the survivors.
  - Idle (non-held) capital sits in IEF (7-10y Treasury) as cash proxy —
    earns a small carry instead of zero.
  - 5 bps per unit weight change (matches Strategy A cost assumption).

Benchmarks:
  - SPY buy-and-hold (single passive equity)
  - Equal-weight across all 14 ETFs (no signal)
  - 60/40 (SPY/AGG): rebalance weekly to 60% SPY, 40% AGG — the
    conventional balanced portfolio.

Output: data/asset_class_rotation.json
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PRICE_CACHE = DATA_DIR / "asset_class_prices_cache.parquet"
OUT_PATH = DATA_DIR / "asset_class_rotation.json"

sys.stdout.reconfigure(encoding="utf-8")


# =========================================================================
# Asset-class universe
# =========================================================================
UNIVERSE: dict[str, dict] = {
    # US equity
    "SPY":  {"label": "S&P 500 (US large)",         "asset_class": "US Equity"},
    "IJR":  {"label": "S&P 600 (US small)",         "asset_class": "US Equity"},
    "QQQ":  {"label": "NASDAQ-100 (US tech-heavy)", "asset_class": "US Equity"},
    # International developed
    "EFA":  {"label": "MSCI EAFE (Intl developed)", "asset_class": "Intl Developed"},
    "VGK":  {"label": "FTSE Europe",                "asset_class": "Intl Developed"},
    "EWJ":  {"label": "MSCI Japan",                 "asset_class": "Intl Developed"},
    # Emerging markets
    "EEM":  {"label": "MSCI EM (Emerging Mkts)",    "asset_class": "Emerging Mkts"},
    # Real estate
    "VNQ":  {"label": "US Real Estate (REITs)",     "asset_class": "Real Estate"},
    # Commodities
    "GLD":  {"label": "Gold",                       "asset_class": "Commodities"},
    "DBC":  {"label": "Broad Commodities",          "asset_class": "Commodities"},
    # Bonds
    "TLT":  {"label": "20+y Treasury (long dur)",   "asset_class": "Bonds"},
    "IEF":  {"label": "7-10y Treasury (interm)",    "asset_class": "Bonds"},
    "TIP":  {"label": "TIPS (inflation-linked)",    "asset_class": "Bonds"},
    "HYG":  {"label": "High-Yield Credit",          "asset_class": "Bonds"},
}
TICKERS = list(UNIVERSE.keys())

START_DATE = "2007-01-01"  # earliest common start across the universe
END_DATE   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

MA_PERIOD = 200
COST_BPS = 5
COST_FRAC = COST_BPS / 10_000

K_GRID = [3, 4, 5, 6, 7]
REBAL_FREQS = [
    ("Daily",         "D"),
    ("Weekly Fri",    "W-FRI"),
    ("Bi-weekly Fri", "2W-FRI"),
    ("Month-end",     "BME"),
]
HEADLINE_K = 7
HEADLINE_FREQ_NAME = "Weekly Fri"
HEADLINE_FREQ = "W-FRI"

# ETF used as a cash proxy when fewer than K ETFs have positive signal.
# IEF earns ~3-4% Treasury carry vs 0% for cash — small carry on idle capital.
CASH_PROXY = "IEF"


# =========================================================================
# Stable per-ETF colour palette (used by the dashboard's stacked-area chart)
# =========================================================================
ASSET_CLASS_COLOURS = {
    "SPY":  "#374151",  "IJR":  "#1e3a8a",  "QQQ":  "#7c3aed",
    "EFA":  "#0e7490",  "VGK":  "#0891b2",  "EWJ":  "#be185d",
    "EEM":  "#dc2626",
    "VNQ":  "#0d9488",
    "GLD":  "#ca8a04",  "DBC":  "#92400e",
    "TLT":  "#1d7a3a",  "IEF":  "#65a30d",  "TIP":  "#a16207",  "HYG":  "#52525b",
}


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


# =========================================================================
# Data loading
# =========================================================================
def download_prices() -> pd.DataFrame:
    """Download adjusted-close prices for the asset-class universe.

    Parquet cache at data/asset_class_prices_cache.parquet. Refreshes if the
    cache is more than 7 days stale relative to today (so weekly Friday
    rebalances stay current without re-downloading every run).
    """
    if PRICE_CACHE.exists():
        cached = pd.read_parquet(PRICE_CACHE)
        stale_days = (pd.Timestamp.utcnow().tz_localize(None) - cached.index.max()).days
        cached_universe = set(cached.columns)
        if stale_days <= 7 and set(TICKERS).issubset(cached_universe):
            print(f"  Using cached prices ({cached.index.min().date()} -> "
                  f"{cached.index.max().date()}, {stale_days}d stale)")
            return cached[TICKERS]
        print(f"  Cache stale ({stale_days}d) or universe expanded — refreshing")

    print(f"  Downloading {len(TICKERS)} tickers from yfinance "
          f"({START_DATE} -> {END_DATE}) ...", flush=True)
    raw = yf.download(TICKERS, start=START_DATE, end=END_DATE, auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    # Result has MultiIndex columns (ticker, field). Extract Close per ticker.
    closes = {}
    for t in TICKERS:
        if (t, "Close") in raw.columns:
            closes[t] = raw[(t, "Close")]
        elif "Close" in raw.columns:
            closes[t] = raw["Close"]
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index().dropna(how="all")
    df.to_parquet(PRICE_CACHE)
    print(f"  Downloaded {df.shape[0]} rows x {df.shape[1]} tickers")
    return df


# =========================================================================
# Signal + portfolio engine
# =========================================================================
def compute_signal(closes: pd.DataFrame) -> pd.DataFrame:
    """Distance above 200d MA per ETF: (close - MA200) / MA200.

    Positive = uptrend (above MA200). Negative = downtrend.
    """
    ma = closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    return (closes - ma) / ma


def top_k_by_signal(K: int, exclude_negative: bool = True):
    """Weight function for top-K-by-signal portfolio construction.

    Returns a callable that takes a row of signal values (Series indexed by
    ETF) and returns a Series of weights summing to <= 1.
    - Drop NaN signal (insufficient history)
    - Optionally drop negative-signal candidates (below 200d MA)
    - Pick top K by signal value, weight by signal share among them
    - If fewer than K positive-signal candidates exist, the deficit goes to
      cash (CASH_PROXY) — modelled as 1.0 weight on the cash ETF for the
      missing slots.
    """
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        if len(valid) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        candidates = valid[valid > 0] if exclude_negative else valid
        if len(candidates) == 0:
            # Everything in downtrend — sit in cash proxy
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        top = candidates.nlargest(min(K, len(candidates)))
        total_invested_weight = len(top) / K  # so 3 positives in K=5 -> 60% invested
        normed = top / top.sum()
        w = pd.Series(0.0, index=s_row.index)
        w.loc[top.index] = normed * total_invested_weight
        cash_weight = 1.0 - total_invested_weight
        if cash_weight > 0 and CASH_PROXY in w.index:
            w[CASH_PROXY] = w.get(CASH_PROXY, 0.0) + cash_weight
        return w
    return f


def run_rotation(closes: pd.DataFrame, signal: pd.DataFrame, weight_fn,
                  eligible_start: pd.Timestamp,
                  rebalance_freq: str = "W-FRI",
                  cost: float = COST_FRAC) -> dict:
    """Run the rotation portfolio. Same mechanics as run_portfolio: yesterday's
    signal -> today's rebalance, yesterday's weights * today's returns."""
    rebalance_dates_target = pd.date_range(eligible_start, closes.index[-1],
                                             freq=rebalance_freq)
    rebalance_dates = closes.index[closes.index.isin(rebalance_dates_target)]
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)
    for rd in rebalance_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0:
            continue
        s_row = signal.iloc[prev_idx]
        rb_weights.loc[rd] = weight_fn(s_row).reindex(closes.columns).fillna(0.0)
    weight_panel = rb_weights.reindex(closes.index, method="ffill").fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0

    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * cost
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel, "daily_ret": port_ret,
             "turnover": turnover}


def sixty_forty(closes: pd.DataFrame, eligible_start: pd.Timestamp,
                  rebalance_freq: str = "W-FRI") -> dict:
    """60% SPY / 40% IEF, rebalanced same cadence. The classical benchmark."""
    target = pd.Series({"SPY": 0.6, "IEF": 0.4})
    rebalance_dates_target = pd.date_range(eligible_start, closes.index[-1],
                                             freq=rebalance_freq)
    rebalance_dates = closes.index[closes.index.isin(rebalance_dates_target)]
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)
    for rd in rebalance_dates:
        rb_weights.loc[rd] = target.reindex(closes.columns).fillna(0.0)
    weight_panel = rb_weights.reindex(closes.index, method="ffill").fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel}


def equal_weight_all(closes: pd.DataFrame, eligible_start: pd.Timestamp,
                      rebalance_freq: str = "W-FRI") -> dict:
    """Equal weight across all N tickers in the universe."""
    n = len(closes.columns)
    target_w = 1.0 / n
    rebalance_dates_target = pd.date_range(eligible_start, closes.index[-1],
                                             freq=rebalance_freq)
    rebalance_dates = closes.index[closes.index.isin(rebalance_dates_target)]
    rb_weights = pd.DataFrame(target_w, index=rebalance_dates,
                                columns=closes.columns, dtype=float)
    weight_panel = rb_weights.reindex(closes.index, method="ffill").fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity}


# =========================================================================
# Stats + reporting
# =========================================================================
def compute_stats(equity: pd.Series, eligible_start: pd.Timestamp) -> dict:
    eq = equity.loc[equity.index >= eligible_start].copy()
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    total_ret = float(eq.iloc[-1] - 1.0)
    cagr = (eq.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(cagr),
        "total_return": _safe(total_ret),
        "max_dd": _safe(max_dd),
    }


def turnover_stats(weight_panel: pd.DataFrame,
                     eligible_start: pd.Timestamp) -> dict:
    wp = weight_panel.loc[weight_panel.index >= eligible_start].copy()
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    return {
        "annual_turnover": float(diff.sum() / n_years) if n_years > 0 else 0.0,
        "n_flips": int((diff > 1e-6).sum()),
    }


def build_trade_history(weight_panel: pd.DataFrame, signal: pd.DataFrame,
                          eligible_start: pd.Timestamp) -> list[dict]:
    wp = weight_panel.loc[weight_panel.index >= eligible_start]
    sp = signal.reindex(wp.index, method="ffill")
    out: list[dict] = []
    prev: pd.Series | None = None
    for dt, row in wp.iterrows():
        if prev is None or not np.allclose(row.values, prev.values, atol=1e-6):
            non_zero = row[row > 1e-6].sort_values(ascending=False)
            if len(non_zero) == 0:
                prev = row
                continue
            holdings = []
            for etf, w in non_zero.items():
                s_val = sp.loc[dt, etf] if etf in sp.columns else None
                holdings.append({
                    "etf": etf,
                    "weight": round(float(w), 4),
                    "signal_pct": (round(float(s_val) * 100, 1)
                                    if s_val == s_val else None),
                })
            out.append({"date": dt.strftime("%Y-%m-%d"), "holdings": holdings})
            prev = row
    return out


def walk_forward_K(closes: pd.DataFrame, signal: pd.DataFrame,
                     eligible_start: pd.Timestamp,
                     initial_train_end: pd.Timestamp,
                     K_grid: list[int] = None,
                     refit_freq: str = "YE",
                     rebal_freq: str = "W-FRI") -> dict:
    if K_grid is None:
        K_grid = K_GRID
    last_date = closes.index[-1]
    refit_ends = pd.date_range(initial_train_end, last_date, freq=refit_freq)
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_ends]
    refit_ends = [r for r in refit_ends if r >= eligible_start]
    if not refit_ends:
        return {}

    def _portfolio_equity(K, win_start):
        r = run_rotation(closes, signal, top_k_by_signal(K), win_start,
                         rebalance_freq=rebal_freq)
        return r["equity"]

    def _sharpe(equity, win_start, win_end):
        eq = equity.loc[(equity.index >= win_start) & (equity.index <= win_end)]
        if len(eq) < 5:
            return float("nan")
        eq = eq / float(eq.iloc[0])
        daily = eq.pct_change().fillna(0)
        if daily.std() == 0:
            return 0.0
        return float(daily.mean() / daily.std() * math.sqrt(252))

    segments = []
    test_eq_pieces = []
    for i, train_end in enumerate(refit_ends):
        train_end_idx = closes.index.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(closes):
            break
        test_start = closes.index[test_start_idx]
        if test_start > test_end:
            continue
        best_K, best_sh = None, -1e9
        for K in K_grid:
            full_eq = _portfolio_equity(K, eligible_start)
            sh = _sharpe(full_eq, eligible_start, train_end)
            if not np.isnan(sh) and sh > best_sh:
                best_sh, best_K = sh, K
        if best_K is None:
            continue
        full_eq = _portfolio_equity(best_K, eligible_start)
        test_eq = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq / base_val
        test_sh = _sharpe(test_eq, test_start, test_end)
        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_K": best_K,
            "train_sharpe": _safe(best_sh),
            "test_sharpe": _safe(test_sh),
            "n_test_days": int(len(test_eq)),
        })
        last_val = test_eq_pieces[-1].iloc[-1] if test_eq_pieces else 1.0
        test_eq_pieces.append(test_eq * last_val / test_eq.iloc[0])
    if not test_eq_pieces:
        return {}
    wf_equity = pd.concat(test_eq_pieces)
    wf_sh = (wf_equity.pct_change().fillna(0).mean()
              / wf_equity.pct_change().fillna(0).std() * math.sqrt(252)
              if wf_equity.pct_change().fillna(0).std() > 0 else 0.0)
    return {
        "segments": segments,
        "walk_forward_sharpe": _safe(wf_sh),
        "wf_dates": [d.strftime("%Y-%m-%d") for d in wf_equity.index],
        "wf_equity": round_series(wf_equity.values),
    }


# =========================================================================
# Main
# =========================================================================
def main() -> int:
    print("Loading asset-class universe ...", flush=True)
    closes = download_prices()
    print(f"  {len(closes.columns)} ETFs, {closes.shape[0]} trading days")

    # Eligible start: 200d after first day of full panel
    closes = closes.dropna()  # drop early dates where any ticker is missing
    if len(closes) == 0:
        print("ERROR: no common dates across the universe", file=sys.stderr)
        return 1
    eligible = closes.index[MA_PERIOD]
    print(f"  Eligible start: {eligible.date()} (200d warm-up)")

    print("\nComputing signal (distance above 200d MA) ...")
    signal = compute_signal(closes)

    # ===== Rebalance-frequency sensitivity grid =====
    print("\n=== Rebalance-frequency sensitivity: K x cadence ===")
    grid: dict[str, dict[str, dict]] = {}
    headline_payload: dict | None = None
    for K in K_GRID:
        grid[f"K={K}"] = {}
        print(f"\n  --- K = {K} ---")
        for freq_name, freq_code in REBAL_FREQS:
            r = run_rotation(closes, signal, top_k_by_signal(K), eligible,
                             rebalance_freq=freq_code)
            st = compute_stats(r["equity"], eligible)
            to = turnover_stats(r["weights"], eligible)
            grid[f"K={K}"][freq_name] = {**st, **to}
            print(f"    {freq_name:<14}  Sharpe {st['sharpe']:+.2f}   "
                  f"CAGR {st['cagr']*100:+5.1f}%   "
                  f"DD {st['max_dd']*100:>4.1f}%   "
                  f"turnover/yr {to['annual_turnover']:>4.2f}   "
                  f"flips {to['n_flips']:>3d}")
            if K == HEADLINE_K and freq_name == HEADLINE_FREQ_NAME:
                eq_window = r["equity"].loc[r["equity"].index >= eligible]
                eq_window = eq_window / eq_window.iloc[0]
                trades = build_trade_history(r["weights"], signal, eligible)

                # Per-ETF attribution
                rets = closes.pct_change().fillna(0).loc[r["weights"].index]
                rets = rets.loc[rets.index >= eligible]
                used_w = r["weights"].loc[rets.index].shift(1).fillna(0)
                daily_contrib = used_w * rets
                total_contrib = daily_contrib.sum()
                total_all = float(total_contrib.sum())
                attribution = {}
                for etf in closes.columns:
                    if etf not in daily_contrib.columns:
                        continue
                    held_mask = used_w[etf] > 1e-6
                    n_held = int(held_mask.sum())
                    total_days = len(used_w)
                    if n_held == 0:
                        ann_ret = None
                        avg_w = 0.0
                    else:
                        mean_daily = float(rets.loc[held_mask, etf].mean())
                        ann_ret = (1.0 + mean_daily) ** 252 - 1.0
                        avg_w = float(used_w[etf][held_mask].mean())
                    pnl = float(total_contrib.get(etf, 0.0))
                    attribution[etf] = {
                        "days_held": n_held,
                        "pct_of_days": round(n_held / total_days * 100, 1)
                                          if total_days else 0.0,
                        "avg_weight_when_held": round(avg_w, 4),
                        "ann_return_when_held": _safe(ann_ret),
                        "contribution_to_total_return": _safe(pnl),
                        "pct_of_total_contribution": (
                            round(pnl / total_all * 100, 1)
                            if total_all != 0 else 0.0
                        ),
                    }

                # Weekly allocation snapshot for stacked-area chart
                weekly_idx = r["weights"].index[r["weights"].index.dayofweek == 4]
                weekly_w = r["weights"].loc[weekly_idx]
                weekly_w = weekly_w.loc[(weekly_w.sum(axis=1) > 0.5)]

                headline_payload = {
                    "K": K,
                    "rebal_freq": freq_name,
                    "rebal_freq_code": freq_code,
                    "n_etfs": len(TICKERS),
                    "etfs_used": TICKERS,
                    "eligible_start": eligible.strftime("%Y-%m-%d"),
                    "headline_stats": {**st, **to},
                    "headline_equity_dates": [d.strftime("%Y-%m-%d")
                                                for d in eq_window.index],
                    "headline_equity": round_series(eq_window.values),
                    "n_rebalances": len(trades),
                    "trade_history": trades,
                    "attribution": attribution,
                    "weekly_allocation_dates": [d.strftime("%Y-%m-%d")
                                                  for d in weekly_w.index],
                    "weekly_allocation": {
                        etf: round_series(weekly_w[etf].values)
                        for etf in weekly_w.columns
                    },
                }

    # ===== Benchmarks =====
    print("\n=== Benchmarks ===")
    spy_eq_window = closes["SPY"].loc[closes["SPY"].index >= eligible]
    spy_eq = spy_eq_window / spy_eq_window.iloc[0]
    spy_stats = compute_stats(closes["SPY"], eligible)
    print(f"  SPY                Sharpe {spy_stats['sharpe']:+.2f}   "
          f"CAGR {spy_stats['cagr']*100:+5.1f}%   DD {spy_stats['max_dd']*100:.1f}%")

    ew_run = equal_weight_all(closes, eligible, HEADLINE_FREQ)
    ew_eq_window = ew_run["equity"].loc[ew_run["equity"].index >= eligible]
    ew_eq = ew_eq_window / ew_eq_window.iloc[0]
    ew_stats = compute_stats(ew_run["equity"], eligible)
    print(f"  Equal-weight 14    Sharpe {ew_stats['sharpe']:+.2f}   "
          f"CAGR {ew_stats['cagr']*100:+5.1f}%   DD {ew_stats['max_dd']*100:.1f}%")

    sf_run = sixty_forty(closes, eligible, HEADLINE_FREQ)
    sf_eq_window = sf_run["equity"].loc[sf_run["equity"].index >= eligible]
    sf_eq = sf_eq_window / sf_eq_window.iloc[0]
    sf_stats = compute_stats(sf_run["equity"], eligible)
    print(f"  60/40 SPY/IEF      Sharpe {sf_stats['sharpe']:+.2f}   "
          f"CAGR {sf_stats['cagr']*100:+5.1f}%   DD {sf_stats['max_dd']*100:.1f}%")

    benchmarks = {
        "spy_buy_hold": {
            "label": "SPY buy-and-hold",
            "dates": [d.strftime("%Y-%m-%d") for d in spy_eq.index],
            "equity": round_series(spy_eq.values),
            **spy_stats,
        },
        "equal_weight_14": {
            "label": "Equal-weight 14 asset-class ETFs (no signal)",
            "dates": [d.strftime("%Y-%m-%d") for d in ew_eq.index],
            "equity": round_series(ew_eq.values),
            **ew_stats,
        },
        "sixty_forty": {
            "label": "60/40 SPY/IEF rebalanced weekly",
            "dates": [d.strftime("%Y-%m-%d") for d in sf_eq.index],
            "equity": round_series(sf_eq.values),
            **sf_stats,
        },
    }

    # ===== Walk-forward K selection =====
    print("\n=== Walk-forward K refit (annual, K in {3,4,5,6,7}) ===")
    wf = walk_forward_K(closes, signal, eligible,
                         pd.Timestamp("2014-12-31"), K_grid=K_GRID,
                         refit_freq="YE", rebal_freq=HEADLINE_FREQ)
    if wf:
        print(f"  Walk-forward Sharpe: {wf['walk_forward_sharpe']:+.2f}")
        print(f"  K sequence: {[s['best_K'] for s in wf['segments']]}")

    # ===== Output =====
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": [
            {"etf": t, "label": UNIVERSE[t]["label"],
             "asset_class": UNIVERSE[t]["asset_class"]}
            for t in TICKERS
        ],
        "ma_period": MA_PERIOD,
        "cost_bps": COST_BPS,
        "rebalance_freq_grid": grid,
        "headline": headline_payload,
        "benchmarks": benchmarks,
        "walk_forward": wf,
        "asset_class_colours": ASSET_CLASS_COLOURS,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    print()
    print("=" * 90)
    print(f"STRATEGY B HEADLINE — K={HEADLINE_K}, {HEADLINE_FREQ_NAME}")
    print("=" * 90)
    h = headline_payload
    s = h["headline_stats"]
    print(f"  Sharpe          : {s['sharpe']:+.2f}")
    print(f"  CAGR            : {s['cagr']*100:+.1f}%")
    print(f"  Total return    : {s['total_return']*100:+.1f}%")
    print(f"  Max drawdown    : {s['max_dd']*100:.1f}%")
    print(f"  Annual turnover : {s['annual_turnover']:.2f}")
    print(f"  Number of rebals: {h['n_rebalances']}")
    if wf:
        print(f"  Walk-forward Sharpe: {wf['walk_forward_sharpe']:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
