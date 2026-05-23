"""Strategy C — thematic rotation (Phase 3).

Where Strategy A operates within US sectors (constituent breadth) and
Strategy B operates across asset classes (ETF-level momentum), Strategy C
runs the same ETF-level momentum signal on a curated set of THEMATIC ETFs
to catch secular trends that don't fit traditional sector/asset-class
boxes (AI, cybersecurity, clean energy, biotech, blockchain, etc).

Thematic ETFs are riskier than broad sectors:
  - Survivorship bias (failed thematics get delisted; only winners remain
    in the backtest universe)
  - Short history (most launched 2014+, some 2018+)
  - Fad-prone — a top-K rotation can chase last year's blowoff right at
    the peak (ARKK 2021, cannabis 2018)
  - Higher fees (40-75 bps vs 10 bps for broad sectors)

Therefore Strategy C has additional fad-resistance guardrails on top of
Strategy B's mechanics:

  1. Hard signal floor: signal must be >= 5% above 200d MA to be eligible
     (not just positive). Filters marginal "in an uptrend" cases.
  2. Per-ETF cap: max 35% of Strategy C in any single thematic.
  3. Cash floor in IEF when fewer than K candidates clear the floor.
  4. Smaller K (3-4) because the universe is more internally correlated.
  5. The combined portfolio sleeve cap is 10% (managed in run_multi_strategy).

Universe (16 thematic ETFs, all US-listed, all > $500M AUM):

  Technology / Innovation:
    ARKK  - ARK Innovation
    CIBR  - First Trust NASDAQ Cybersecurity
    SKYY  - First Trust Cloud Computing
    BOTZ  - Global X Robotics & AI
    BLOK  - Amplify Transformational Data Sharing (blockchain)
  Energy / Climate:
    ICLN  - iShares Global Clean Energy
    TAN   - Invesco Solar
    LIT   - Global X Lithium & Battery Tech
    URA   - Global X Uranium
  Health / Bio:
    XBI   - SPDR S&P Biotech (equal-weight, more theme-driven than IBB)
    ARKG  - ARK Genomic Revolution
  Cyclical thematics:
    JETS  - Global X Airlines
  Commodity-equity (distinct from broad commodity spot in B):
    GDX   - VanEck Gold Miners (operational leverage to gold)
    COPX  - Global X Copper Miners (industrial metals cycle)
    MOO   - VanEck Agribusiness
  Infrastructure:
    PAVE  - Global X US Infrastructure Development

Signal: distance above own 200-day MA per ETF.
Output: data/thematic_rotation.json
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
PRICE_CACHE = DATA_DIR / "thematic_prices_cache.parquet"
OUT_PATH = DATA_DIR / "thematic_rotation.json"

sys.stdout.reconfigure(encoding="utf-8")


UNIVERSE: dict[str, dict] = {
    "ARKK": {"label": "ARK Disruptive Innovation",          "theme": "Tech / Innovation"},
    "CIBR": {"label": "First Trust Cybersecurity",          "theme": "Tech / Innovation"},
    "SKYY": {"label": "First Trust Cloud Computing",        "theme": "Tech / Innovation"},
    "BOTZ": {"label": "Global X Robotics & AI",             "theme": "Tech / Innovation"},
    "BLOK": {"label": "Amplify Blockchain",                 "theme": "Tech / Innovation"},
    "ICLN": {"label": "iShares Global Clean Energy",        "theme": "Energy / Climate"},
    "TAN":  {"label": "Invesco Solar",                      "theme": "Energy / Climate"},
    "LIT":  {"label": "Global X Lithium & Battery Tech",    "theme": "Energy / Climate"},
    "URA":  {"label": "Global X Uranium",                   "theme": "Energy / Climate"},
    "XBI":  {"label": "SPDR S&P Biotech (eq-weight)",       "theme": "Health / Bio"},
    "ARKG": {"label": "ARK Genomic Revolution",             "theme": "Health / Bio"},
    "JETS": {"label": "Global X Airlines",                  "theme": "Cyclical thematic"},
    "GDX":  {"label": "VanEck Gold Miners",                 "theme": "Commodity equity"},
    "COPX": {"label": "Global X Copper Miners",             "theme": "Commodity equity"},
    "MOO":  {"label": "VanEck Agribusiness",                "theme": "Commodity equity"},
    "PAVE": {"label": "Global X US Infrastructure",         "theme": "Infrastructure"},
}
TICKERS = list(UNIVERSE.keys())

# Cash proxy when fewer than K candidates clear the signal floor.
# IEF is the same 7-10y Treasury used in Strategy B, so cash exposure is
# consistent across B and C.
CASH_PROXY = "IEF"

START_DATE = "2018-01-01"  # BLOK inception (Jan 2018) is the binding date
END_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

MA_PERIOD = 200
SIGNAL_FLOOR = 0.05       # require >= 5% above 200d MA (not just positive)
PER_ETF_CAP = 0.35        # no single thematic > 35% of Strategy C
COST_BPS = 5
COST_FRAC = COST_BPS / 10_000

K_GRID = [3, 4, 5]
REBAL_FREQS = [
    ("Daily",         "D"),
    ("Weekly Fri",    "W-FRI"),
    ("Bi-weekly Fri", "2W-FRI"),
    ("Month-end",     "BME"),
]
HEADLINE_K = 4
HEADLINE_FREQ_NAME = "Weekly Fri"
HEADLINE_FREQ = "W-FRI"


# Stable per-ETF colour palette for the dashboard's stacked allocation chart
THEMATIC_COLOURS = {
    "ARKK": "#dc2626", "CIBR": "#7c3aed", "SKYY": "#0891b2",
    "BOTZ": "#1351b4", "BLOK": "#374151",
    "ICLN": "#1d7a3a", "TAN":  "#ca8a04", "LIT":  "#0d9488", "URA":  "#92400e",
    "XBI":  "#be185d", "ARKG": "#e879f9",
    "JETS": "#0e7490",
    "GDX":  "#a16207", "COPX": "#b45309", "MOO":  "#65a30d",
    "PAVE": "#52525b",
    "IEF":  "#6b727a",  # cash proxy
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


def download_prices() -> pd.DataFrame:
    """Download adjusted-close prices for the thematic universe + IEF.

    Reuses the asset_class cache when available for IEF (avoid double-
    downloading). Refreshes if more than 7 days stale.
    """
    needed = TICKERS + [CASH_PROXY]
    if PRICE_CACHE.exists():
        cached = pd.read_parquet(PRICE_CACHE)
        stale_days = (pd.Timestamp.utcnow().tz_localize(None) - cached.index.max()).days
        if stale_days <= 7 and set(needed).issubset(set(cached.columns)):
            print(f"  Using cached prices ({cached.index.min().date()} -> "
                  f"{cached.index.max().date()}, {stale_days}d stale)")
            return cached[needed]

    print(f"  Downloading {len(needed)} tickers from yfinance "
          f"({START_DATE} -> {END_DATE}) ...", flush=True)
    raw = yf.download(needed, start=START_DATE, end=END_DATE, auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    closes = {}
    for t in needed:
        if (t, "Close") in raw.columns:
            closes[t] = raw[(t, "Close")]
        elif "Close" in raw.columns:
            closes[t] = raw["Close"]
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df.to_parquet(PRICE_CACHE)
    print(f"  Downloaded {df.shape[0]} rows x {df.shape[1]} tickers")
    return df


def compute_signal(closes: pd.DataFrame) -> pd.DataFrame:
    ma = closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    return (closes - ma) / ma


def top_k_by_signal_capped(K: int):
    """Strategy C weight function with the fad-resistance guardrails.

    - Drop NaN signal (insufficient history)
    - Drop signal < SIGNAL_FLOOR (require >= 5% above 200d MA)
    - Top K by signal value
    - Weight by signal share, then cap any single ETF at PER_ETF_CAP and
      redistribute the spilled weight proportionally to the others (iterate
      until no cap is breached)
    - If fewer than K candidates clear the floor, the deficit goes to
      the IEF cash proxy
    """
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        if len(valid) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        eligible = valid[valid > SIGNAL_FLOOR]
        if len(eligible) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested_frac = len(top) / K
        if top.sum() <= 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        # Raw weights from signal share, scaled to invested_frac
        raw = (top / top.sum()) * invested_frac
        # Iteratively apply the per-ETF cap. Cap is on within-strategy weight,
        # i.e. raw weight as a fraction of invested portion (not total).
        cap = PER_ETF_CAP * invested_frac
        for _ in range(8):  # converges in a few iterations
            over = raw > cap
            if not over.any():
                break
            excess = (raw[over] - cap).sum()
            raw[over] = cap
            under = raw < cap
            if under.sum() == 0 or raw[under].sum() == 0:
                break
            # Redistribute excess pro-rata to the under-cap names
            raw[under] += excess * (raw[under] / raw[under].sum())
        w = pd.Series(0.0, index=s_row.index)
        w.loc[top.index] = raw
        cash = 1.0 - invested_frac
        if cash > 0 and CASH_PROXY in w.index:
            w[CASH_PROXY] = w.get(CASH_PROXY, 0.0) + cash
        return w
    return f


def run_rotation(closes: pd.DataFrame, signal: pd.DataFrame, weight_fn,
                  eligible_start: pd.Timestamp,
                  rebalance_freq: str = "W-FRI",
                  cost: float = COST_FRAC) -> dict:
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
    weight_panel = rb_weights.reindex(closes.index).ffill().fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0

    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * cost
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel, "daily_ret": port_ret,
             "turnover": turnover}


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
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(cagr),
        "total_return": _safe(total_ret),
        "max_dd": _safe(float(dd.min())),
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
    """Per-rebalance holdings list. Records the PRIOR trading day's signal
    (the value that actually decided the weight) so the share-math
    reproduces the weight exactly."""
    wp = weight_panel.loc[weight_panel.index >= eligible_start]
    sp = signal.reindex(wp.index, method="ffill")
    full_idx = list(wp.index)
    out: list[dict] = []
    prev: pd.Series | None = None
    for i, (dt, row) in enumerate(wp.iterrows()):
        if prev is None or not np.allclose(row.values, prev.values, atol=1e-6):
            non_zero = row[row > 1e-6].sort_values(ascending=False)
            if len(non_zero) == 0:
                prev = row
                continue
            decision_date = full_idx[i - 1] if i > 0 else full_idx[i]
            holdings = []
            for etf, w in non_zero.items():
                s_val = sp.loc[decision_date, etf] if etf in sp.columns else None
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
                     K_grid: list[int] | None = None,
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
        r = run_rotation(closes, signal, top_k_by_signal_capped(K), win_start,
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
    wf_daily = wf_equity.pct_change().fillna(0)
    wf_sh = (wf_daily.mean() / wf_daily.std() * math.sqrt(252)
              if wf_daily.std() > 0 else 0.0)
    return {
        "segments": segments,
        "walk_forward_sharpe": _safe(wf_sh),
        "wf_dates": [d.strftime("%Y-%m-%d") for d in wf_equity.index],
        "wf_equity": round_series(wf_equity.values),
    }


def main() -> int:
    print("Loading thematic universe ...", flush=True)
    closes = download_prices()
    # Drop columns that are entirely NaN (rare — should not happen for our list)
    closes = closes.dropna(axis=1, how="all")
    print(f"  {len(closes.columns)} tickers, {closes.shape[0]} trading days")

    # Each ticker may start on a different date. Eligible start =
    # 200 trading days after the latest first-valid date in the universe.
    first_valid_per_etf = {c: closes[c].first_valid_index() for c in closes.columns}
    latest_start = max(d for d in first_valid_per_etf.values() if d is not None)
    eligible_idx = closes.index.searchsorted(latest_start) + MA_PERIOD
    if eligible_idx >= len(closes):
        print("ERROR: not enough data for warm-up", file=sys.stderr)
        return 1
    eligible = closes.index[eligible_idx]
    print(f"  Latest ticker start: {latest_start.date()} -> eligible from {eligible.date()}")

    print("\nComputing signal (distance above 200d MA) ...")
    signal = compute_signal(closes)

    print("\n=== Rebalance-frequency sensitivity: K x cadence ===")
    grid: dict[str, dict[str, dict]] = {}
    headline_payload: dict | None = None
    for K in K_GRID:
        grid[f"K={K}"] = {}
        print(f"\n  --- K = {K} (signal floor +{int(SIGNAL_FLOOR*100)}%, "
              f"per-ETF cap {int(PER_ETF_CAP*100)}%) ---")
        for freq_name, freq_code in REBAL_FREQS:
            r = run_rotation(closes, signal, top_k_by_signal_capped(K),
                              eligible, rebalance_freq=freq_code)
            st = compute_stats(r["equity"], eligible)
            to = turnover_stats(r["weights"], eligible)
            grid[f"K={K}"][freq_name] = {**st, **to}
            print(f"    {freq_name:<14}  Sharpe {st['sharpe']:+.2f}   "
                  f"CAGR {st['cagr']*100:+5.1f}%   "
                  f"DD {st['max_dd']*100:>5.1f}%   "
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
                        ann_ret, avg_w = None, 0.0
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

                # Weekly allocation snapshot (Fridays only) for stacked-area
                weekly_idx = r["weights"].index[r["weights"].index.dayofweek == 4]
                weekly_w = r["weights"].loc[weekly_idx]
                weekly_w = weekly_w.loc[(weekly_w.sum(axis=1) > 0.5)]

                headline_payload = {
                    "K": K,
                    "rebal_freq": freq_name,
                    "rebal_freq_code": freq_code,
                    "n_etfs": len(TICKERS),
                    "etfs_used": list(closes.columns),
                    "eligible_start": eligible.strftime("%Y-%m-%d"),
                    "signal_floor_pct": SIGNAL_FLOOR * 100,
                    "per_etf_cap_pct": PER_ETF_CAP * 100,
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

    print("\n=== Benchmarks ===")
    # SPY and 60/40 sit in the asset_class cache; just compare to QQQ which is
    # the most relevant single-thematic comparator (concentrated tech).
    spy_close = closes.get("IEF")  # placeholder; actually want SPY here
    # Pull SPY from yfinance if not in our cache.
    try:
        spy_raw = yf.download("SPY", start=START_DATE, end=END_DATE,
                               auto_adjust=True, progress=False, threads=False)
        if isinstance(spy_raw.columns, pd.MultiIndex):
            spy_raw.columns = spy_raw.columns.get_level_values(0)
        spy_close = spy_raw["Close"]
        spy_close.index = pd.to_datetime(spy_close.index).tz_localize(None)
        spy_close = spy_close.reindex(closes.index).ffill()
    except Exception as e:
        print(f"  WARNING: could not fetch SPY -- {e}")
        spy_close = pd.Series(index=closes.index, dtype=float)

    spy_window = spy_close.loc[spy_close.index >= eligible].dropna()
    if len(spy_window) > 0:
        spy_eq = spy_window / spy_window.iloc[0]
        spy_stats = compute_stats(spy_close.ffill(), eligible)
        print(f"  SPY                Sharpe {spy_stats['sharpe']:+.2f}   "
              f"CAGR {spy_stats['cagr']*100:+5.1f}%   DD {spy_stats['max_dd']*100:.1f}%")
    else:
        spy_eq = pd.Series(dtype=float)
        spy_stats = {"sharpe": None, "cagr": None, "total_return": None, "max_dd": None}

    benchmarks = {
        "spy_buy_hold": {
            "label": "SPY buy-and-hold",
            "dates": [d.strftime("%Y-%m-%d") for d in spy_eq.index],
            "equity": round_series(spy_eq.values),
            **spy_stats,
        },
    }

    print("\n=== Walk-forward K refit (annual, K in {3, 4, 5}) ===")
    # Train initial period: roughly half the available history
    initial_train_end = pd.Timestamp(eligible.year + 2, 12, 31)
    if initial_train_end > closes.index[-1]:
        initial_train_end = closes.index[len(closes) // 2]
    wf = walk_forward_K(closes, signal, eligible, initial_train_end,
                         K_grid=K_GRID, refit_freq="YE",
                         rebal_freq=HEADLINE_FREQ)
    if wf:
        print(f"  Walk-forward Sharpe: {wf['walk_forward_sharpe']:+.2f}")
        print(f"  K sequence: {[s['best_K'] for s in wf['segments']]}")

    # Per-ETF signal time series (weekly Fridays) for ETF Detail tab.
    # Includes IEF since it is C's cash proxy and users will want to inspect
    # its momentum when it shows up in their portfolio.
    signal_window = signal.loc[signal.index >= eligible]
    weekly_signal = signal_window.loc[signal_window.index.dayofweek == 4]
    per_etf_signals = {}
    for etf in list(TICKERS) + [CASH_PROXY]:
        if etf in weekly_signal.columns:
            ser = weekly_signal[etf].dropna()
            per_etf_signals[etf] = {
                "dates": [d.strftime("%Y-%m-%d") for d in ser.index],
                "signal_pct": [round(float(v) * 100, 2) for v in ser.values],
            }

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": [
            {"etf": t, "label": UNIVERSE[t]["label"], "theme": UNIVERSE[t]["theme"]}
            for t in TICKERS
        ],
        "ma_period": MA_PERIOD,
        "signal_floor_pct": SIGNAL_FLOOR * 100,
        "per_etf_cap_pct": PER_ETF_CAP * 100,
        "cost_bps": COST_BPS,
        "rebalance_freq_grid": grid,
        "headline": headline_payload,
        "benchmarks": benchmarks,
        "walk_forward": wf,
        "thematic_colours": THEMATIC_COLOURS,
        "per_etf_signal": per_etf_signals,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    print()
    print("=" * 90)
    print(f"STRATEGY C HEADLINE — K={HEADLINE_K}, {HEADLINE_FREQ_NAME}")
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
