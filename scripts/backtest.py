"""Step 3 — apply the breadth-thrust signal + exits to SOXX and benchmark.

Inputs:
  - data/breadth_soxx.json   (Step 2 output: signal_fires, composite_z, p10, ma_breadth)
  - yfinance for SOXX OHLC and SPY adjusted close

Outputs:
  - data/backtest_soxx.json

Trade mechanics (set in this session):
  - Entry: at OPEN of the next trading day after a signal-fire date.
  - No re-entry while a trade is open — signal_fire days inside an active
    trade are skipped; the next eligible entry is the first signal_fire
    strictly AFTER the prior trade's exit date.
  - Costs: 5 bps each side (10 bps round-trip) applied to entry open
    and exit close prices.
  - Exits (whichever fires first):
      * Trailing stop  : at any close, exit if close <= max_close_so_far
        - 2 * ATR(20, Wilder), with ATR computed through the same close.
      * Regime exit    : composite_z < composite_p10 (expanding bottom
        decile) OR ma_breadth < 0.40.
      * Time stop      : 252 trading days after entry.
    Each exit fires AT THE CLOSE of the day the condition is met (signal
    and decision use only data through that close, no look-ahead).

Outputs are split into:
  - primary           : per-trade records + aggregate stats including
                        win rate, avg win/loss, profit factor, Sharpe,
                        Sortino, max drawdown of the equity curve.
  - mechanism_diagnostic : fixed-horizon forward returns at 21/63/126/252
                        trading days from the signal date, no exits
                        applied. The clean test of whether the signal
                        contains information.
  - benchmarks        : SOXX unconditional same-window base rate, SPY
                        same window, and a 1,000-path Monte Carlo null
                        with bootstrapped holding periods (random entry
                        dates, same trade count, same holding-period
                        distribution). Strategy percentile reported.

Three ways this backtest could be silently wrong (and our defences):
  - Re-entry inside cluster -> hard skip until prior trade closes.
  - Look-ahead at the entry bar -> entry at OPEN of T+1 after signal
    fires at CLOSE of T. Exits use only same-day-close information.
  - Benchmark survivorship -> benchmarks use SOXX itself and SPY (single
    tradeable instruments), so the constituent-side survivorship in
    breadth does not contaminate the comparator.

Run:
    python scripts/backtest.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SPY_CACHE = DATA_DIR / "spy_close_cache.parquet"

DEFAULT_ETF = "SOXX"


def paths_for(etf: str) -> dict:
    """Per-ETF input / output file paths used by backtest.py and its sweeps."""
    e = etf.lower()
    return {
        "breadth": DATA_DIR / f"breadth_{e}.json",
        "ohlc_cache": DATA_DIR / f"{e}_ohlc_cache.parquet",
        "out": DATA_DIR / f"backtest_{e}.json",
    }


# Back-compat module-level constants pointing at the default ETF (SOXX).
# Existing callers (run_variants, run_sensitivity, run_split_half) keep
# working unchanged.
_default_paths = paths_for(DEFAULT_ETF)
BREADTH_PATH = _default_paths["breadth"]
SOXX_CACHE = _default_paths["ohlc_cache"]
OUT_PATH = _default_paths["out"]

# --- Trade mechanics (session-confirmed defaults) -------------------------
COST_BPS_ONE_SIDE = 5
ATR_PERIOD = 20
TRAILING_STOP_K = 2.0           # stop = max_close - K * ATR
REGIME_MA_FLOOR = 0.40          # ma_breadth < 0.40 forces regime exit
TIME_STOP_DAYS = 252            # ~1 year time stop
HORIZONS = [21, 63, 126, 252]   # mechanism-diagnostic forward windows

# --- Monte Carlo ---------------------------------------------------------
MC_PATHS = 1000
MC_SEED = 20260516


# --- Default config dict (consumed by simulate_trade / run_strategy) -----
# Any caller can pass a custom config dict to test exit-logic, entry-timing,
# or signal-filtering variants without touching this module. See
# scripts/run_variants.py and scripts/run_sensitivity.py for examples.
DEFAULT_CONFIG: dict = {
    "trailing_stop_k": TRAILING_STOP_K,              # None disables trailing stop
    "stop_active_after_profit_pct": None,            # None: stop active from entry
    "use_regime_composite_p10": True,
    "use_regime_ma_floor": True,
    "regime_ma_floor": REGIME_MA_FLOOR,
    "use_time_stop": True,
    "time_stop_days": TIME_STOP_DAYS,
    "atr_period": ATR_PERIOD,
    "cost_bps_one_side": COST_BPS_ONE_SIDE,
    # Item 3 — entry timing. 0 = next trading day open (default); k > 0 = enter
    # k trading days LATER (still at the open). Tests the "signal fires after
    # short-term overbought, entry is too early" hypothesis.
    "entry_delay_bars": 0,
    # Item 4 — trend filter. If True, only enter signals where the parent ETF's
    # close at the signal date is above its `trend_filter_period` SMA. Tests
    # whether the signal works better when paired with a regime filter.
    "use_trend_filter": False,
    "trend_filter_period": 200,
}


# ---------------------------------------------------------------------------
# Helper / price functions
# ---------------------------------------------------------------------------


def load_breadth(etf: str = DEFAULT_ETF) -> tuple[pd.DataFrame, list[dict]]:
    """Load the breadth JSON into (per-day DataFrame, signals list)."""
    breadth_path = paths_for(etf)["breadth"]
    blob = json.loads(breadth_path.read_text(encoding="utf-8"))
    ser = blob["series"]
    df = pd.DataFrame({
        "composite_z": ser["composite_z"],
        "composite_p10": ser["composite_p10"],
        "ma_breadth": ser["ma_breadth"],
        "signal_fires": ser["signal_fires"],
    }, index=pd.to_datetime(ser["dates"]))
    return df, blob["signals"]


def _degenerate_ohlc_reason(frame: "pd.DataFrame | None",
                            require_ohlc: bool = False) -> str | None:
    """Why ``frame`` cannot be priced off, or None when it can.

    Deliberately weak: it asks only whether the frame is a price series at
    all, not whether it is the RIGHT one. Window-relative questions —
    coverage, holes, a truncated start — belong to price_panel_guard, which
    sees the whole panel and knows the backtest window.

    ``require_ohlc`` is set for a fresh FETCH, which is about to be sliced to
    all four columns and would otherwise raise KeyError on a Close-only
    response. It is NOT set for a cached frame: the yfinance backfill in
    export_holdings_prices wrote Close-only caches for the non-engine
    tickers, and those are perfectly usable as a fallback.
    """
    if frame is None or len(frame) == 0:
        return "empty response"
    columns = list(getattr(frame, "columns", []))
    if "Close" not in columns:
        return f"no Close column (got {columns})"
    if require_ohlc:
        missing = [c for c in ("Open", "High", "Low", "Close")
                   if c not in columns]
        if missing:
            return f"missing {', '.join(missing)} (got {columns})"
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if len(close) < 2:
        return f"{len(close)} usable close(s)"
    if close.nunique() < 2:
        return f"flat across {len(close)} sessions"
    return None


def download_soxx_ohlc(start: str, end: str, etf: str = DEFAULT_ETF,
                       yf_symbol: str | None = None) -> pd.DataFrame:
    """Download the parent ETF's OHLC + Close, with parquet cache.

    Despite the legacy name, this function is ETF-parameterised. Callers that
    want SOXX get the original behaviour (cache at data/soxx_ohlc_cache.parquet).
    Pass etf="CSP1" to fetch S&P 500 (cache at data/csp1_ohlc_cache.parquet).
    yf_symbol overrides the yfinance ticker; useful when the parent ETF and
    the yfinance ticker differ (e.g. CSP1 trades in London as CSP1.L).

    A DEGENERATE RESPONSE IS NEVER WRITTEN AND NEVER RETURNED (2026-08-15).
    A SPAN-SHRINKING RESPONSE IS NEVER WRITTEN, ONLY RETURNED (2026-08-19):
    a healthy fetch over a SHORT window — the WS17 shadow evaluator asks from
    its breadth start, the two-year backfill asks ``period="2y"`` — used to
    land on top of nine years of history, and a cold rebuild then read the
    stub back as authoritative (the sleeve-D Xetra caches lost 2017-2024 and
    a blend rebuild collapsed onto the surviving two years). The caller still
    gets exactly the window it fetched; the cache keeps its longer span.
    Note first that the cache-reuse branch below almost never fires for the
    sleeve panels: ``run_portfolio._build_panels_for`` asks for
    ``[constituent_start - 10d, constituent_end + 5d]``, and no cache can
    reach five days past the last session, so the engines re-fetch every run
    and the old code wrote whatever came back straight over a good file. That
    is how SOXX's cache came to be broken at 16:17 on 2026-08-15 and repaired
    only at 16:36, after sleeve A had already published Sharpe 0.76 against a
    true 0.93. Now an empty, one-bar or flat response leaves the cache alone
    and the cached series is used instead; if there is no usable cache either,
    this raises rather than handing an engine something it will silently
    backtest.
    """
    from price_panel_guard import (  # noqa: PLC0415
        DegeneratePriceError, fetched_frame_is_worse,
    )

    cache_path = paths_for(etf)["ohlc_cache"]
    cached = None
    if cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
        except Exception:
            cached = None
        if cached is not None and len(cached):
            if (cached.index.min() <= pd.Timestamp(start)
                    and cached.index.max() >= pd.Timestamp(end)):
                return cached.loc[start:end]
    sym = yf_symbol or etf
    raw = yf.download(sym, start=start, end=end, auto_adjust=True,
                      progress=False, threads=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    why = _degenerate_ohlc_reason(raw, require_ohlc=True)
    if why is None:
        raw = raw[["Open", "High", "Low", "Close"]].copy()
        raw.index = pd.to_datetime(raw.index).tz_localize(None)

        # ----- Norgate price source, opt-in (2026-08-30) -----
        # BTE_PRICE_SOURCE=norgate prefers the locally licensed feed for this
        # security. Default, and every CI runner, is yfinance unchanged.
        #
        # This one site serves sleeves A and D, and that is deliberate rather
        # than convenient: sleeve D's five Xetra lines and the Shenzhen
        # holding resolve to None at Norgate — there is no European or Chinese
        # product at any tier — so they keep their yfinance series with no
        # sleeve-specific branch anywhere. D gets isolated treatment for free.
        #
        # WHOLE FRAME OR NOTHING, on the same superset test the column rule
        # uses (WS19b): take Norgate only when its dates cover every date this
        # response has. Splicing two vendors' bars into one series fabricates
        # a return at the join, and here it would also mix two adjustment
        # bases across Open/High/Low/Close within a bar.
        #
        # Placed BEFORE fetched_frame_is_worse and the cache write, so the
        # degenerate-write guard still vets whatever is returned.
        import os  # noqa: PLC0415
        if os.environ.get("BTE_PRICE_SOURCE", "").strip().lower() == "norgate":
            import norgate_prices  # noqa: PLC0415
            ng = norgate_prices.fetch_ohlc(yf_symbol or etf, start, end)
            if ng is not None and raw.index.difference(ng.index).empty:
                print(f"  Norgate: {sym} taken whole "
                      f"({ng.index.min().date()} -> {ng.index.max().date()}, "
                      f"{len(ng)} bars vs {len(raw)} from yfinance)", flush=True)
                raw = ng[["Open", "High", "Low", "Close"]].copy()
            elif ng is not None:
                print(f"  Norgate: {sym} NOT taken — "
                      f"{len(raw.index.difference(ng.index))} date(s) the "
                      f"incumbent has are missing there", flush=True)

        shrink = fetched_frame_is_worse(raw, cached)
        if shrink is None:
            raw.to_parquet(cache_path)
        else:
            print(f"  REFUSED cache write for {sym}: {shrink}. The fetched "
                  f"window is returned to the caller; {cache_path.name} "
                  f"keeps its longer span.", flush=True)
        return raw

    print(f"  REFUSED cache write for {sym}: the fetch is unusable ({why}). "
          f"{cache_path.name} is left as it was.", flush=True)
    if cached is not None:
        window = cached.loc[start:end]
        if _degenerate_ohlc_reason(window) is None:
            print(f"  Falling back to the cached {sym} series "
                  f"({window.index.min().date()} -> {window.index.max().date()}, "
                  f"{len(window)} bars).", flush=True)
            return window
    raise DegeneratePriceError(
        f"{sym}: the vendor returned an unusable series ({why}) and "
        f"{cache_path.name} has nothing usable for {start} -> {end}. Refusing to "
        f"return a price series an engine would backtest on — that is the "
        f"2026-08-15 SOXX defect. Run `python scripts/export_holdings_prices."
        f"py --refresh-caches-only` and retry."
    )


def download_spy_close(start: str, end: str) -> pd.Series:
    """Download SPY adjusted close with parquet cache.

    Same span rule as ``download_soxx_ohlc`` (2026-08-19): a healthy fetch
    over a short window is returned but never written over a longer cache.
    """
    from price_panel_guard import fetched_frame_is_worse  # noqa: PLC0415

    cached = None
    if SPY_CACHE.exists():
        try:
            cached = pd.read_parquet(SPY_CACHE)
        except Exception:
            cached = None
        if cached is not None and len(cached):
            if (cached.index.min() <= pd.Timestamp(start)
                    and cached.index.max() >= pd.Timestamp(end)):
                return cached["Close"].loc[start:end]
    raw = yf.download("SPY", start=start, end=end, auto_adjust=True,
                      progress=False, threads=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    out = raw[["Close"]].copy()
    out.index = pd.to_datetime(out.index).tz_localize(None)
    shrink = fetched_frame_is_worse(out, cached)
    if shrink is None:
        out.to_parquet(SPY_CACHE)
    else:
        print(f"  REFUSED cache write for SPY: {shrink}. The fetched window "
              f"is returned to the caller; {SPY_CACHE.name} keeps its longer "
              f"span.", flush=True)
    return out["Close"]


def compute_atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series,
                       period: int = ATR_PERIOD) -> pd.Series:
    """Average True Range with Wilder smoothing (alpha = 1/period)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    signal_date: str
    entry_date: str
    entry_open: float            # raw market open
    entry_price: float           # cost-adjusted
    exit_date: str
    exit_close: float            # raw market close
    exit_price: float            # cost-adjusted
    exit_reason: str             # "trailing_stop" | "regime" | "time_stop" | "data_end"
    holding_days: int
    trade_return: float
    max_drawdown: float


def simulate_trade(
    entry_idx: int,
    soxx: pd.DataFrame,
    atr: pd.Series,
    breadth: pd.DataFrame,
    entry_price: float | None = None,
    config: dict | None = None,
) -> tuple[int, str]:
    """Walk forward from entry_idx and return (exit_idx, exit_reason).

    Exit checks are evaluated at the CLOSE of each subsequent day, using
    only same-day-or-earlier information. Knobs come from `config` (see
    DEFAULT_CONFIG); when None, the default settings are used.

    Supported knobs:
      - trailing_stop_k                : float or None (None disables stop)
      - stop_active_after_profit_pct   : float (e.g. 0.05 for "arm at +5%")
                                          or None (stop active from entry)
      - use_regime_composite_p10       : bool
      - use_regime_ma_floor            : bool
      - regime_ma_floor                : float (default 0.40)
      - use_time_stop                  : bool
      - time_stop_days                 : int (default 252)
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    stop_k = cfg["trailing_stop_k"]
    profit_threshold = cfg["stop_active_after_profit_pct"]
    use_regime_comp = cfg["use_regime_composite_p10"]
    use_regime_ma = cfg["use_regime_ma_floor"]
    ma_floor = cfg["regime_ma_floor"]
    use_time_stop = cfg["use_time_stop"]
    time_stop_days = cfg["time_stop_days"]

    max_close = float(soxx["Close"].iloc[entry_idx])
    last_idx = (min(entry_idx + time_stop_days, len(soxx) - 1)
                if use_time_stop else len(soxx) - 1)

    for i in range(entry_idx + 1, last_idx + 1):
        close_i = float(soxx["Close"].iloc[i])
        max_close = max(max_close, close_i)
        atr_i = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else None

        # Trailing stop, optionally gated by a profit-armed condition.
        if stop_k is not None and atr_i is not None and atr_i > 0.0:
            stop_armed = (
                profit_threshold is None
                or (entry_price is not None
                    and max_close >= entry_price * (1.0 + profit_threshold))
            )
            if stop_armed:
                stop_level = max_close - stop_k * atr_i
                # Strict inequality: stop fires when close BREAKS the level.
                if close_i < stop_level:
                    return i, "trailing_stop"

        # Regime exits (close-aligned, using same-day breadth).
        soxx_date = soxx.index[i]
        if soxx_date in breadth.index:
            if use_regime_comp:
                comp_z = breadth.loc[soxx_date, "composite_z"]
                comp_p10 = breadth.loc[soxx_date, "composite_p10"]
                if (pd.notna(comp_z) and pd.notna(comp_p10)
                        and comp_z < comp_p10):
                    return i, "regime_exit_composite"
            if use_regime_ma:
                ma_b = breadth.loc[soxx_date, "ma_breadth"]
                if pd.notna(ma_b) and ma_b < ma_floor:
                    return i, "regime_exit_ma_floor"

        if use_time_stop and (i - entry_idx >= time_stop_days):
            return i, "time_stop"

    return last_idx, "data_end"


def run_strategy(
    signal_dates: list[str],
    soxx: pd.DataFrame,
    breadth: pd.DataFrame,
    config: dict | None = None,
) -> list[Trade]:
    """Iterate signal-fire days; pick first eligible (no overlap with prior
    trade) and simulate to exit. Returns a list of completed Trade records.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    atr = compute_atr_wilder(
        soxx["High"], soxx["Low"], soxx["Close"], period=cfg["atr_period"]
    )
    cost_factor = cfg["cost_bps_one_side"] / 10_000.0
    entry_delay = int(cfg.get("entry_delay_bars", 0))
    # Optional trend filter — apply at SIGNAL DATE on the parent ETF's own price.
    if cfg.get("use_trend_filter", False):
        tf_period = int(cfg.get("trend_filter_period", 200))
        trend_ma = soxx["Close"].rolling(tf_period, min_periods=tf_period).mean()
        above_trend = (soxx["Close"] > trend_ma) & trend_ma.notna()
    else:
        above_trend = None
    trades: list[Trade] = []
    last_exit_idx = -1

    for sd_str in signal_dates:
        sd = pd.Timestamp(sd_str)
        if sd not in soxx.index:
            pos = soxx.index.searchsorted(sd, side="left")
        else:
            pos = soxx.index.get_loc(sd)
        # Apply entry-delay: still entering at OPEN, just k trading days later.
        entry_idx = pos + 1 + entry_delay if soxx.index[pos] <= sd else pos + entry_delay
        if entry_idx >= len(soxx):
            break
        if entry_idx <= last_exit_idx:
            continue  # signal lands inside or before prior trade exit — skip

        # Trend filter is evaluated at the SIGNAL date (close of), not entry day,
        # so the decision uses only information available when the signal fires.
        if above_trend is not None and sd in above_trend.index:
            if not bool(above_trend.loc[sd]):
                continue  # below trend filter — skip this signal

        entry_open = float(soxx["Open"].iloc[entry_idx])
        if not np.isfinite(entry_open):
            continue
        entry_price = entry_open * (1.0 + cost_factor)

        exit_idx, exit_reason = simulate_trade(
            entry_idx, soxx, atr, breadth,
            entry_price=entry_price, config=cfg,
        )
        exit_close = float(soxx["Close"].iloc[exit_idx])
        exit_price = exit_close * (1.0 - cost_factor)

        # Per-trade drawdown over the close path (entry close .. exit close).
        path = soxx["Close"].iloc[entry_idx:exit_idx + 1].astype(float).values
        if len(path) > 1:
            normed = path / path[0]
            peaks = np.maximum.accumulate(normed)
            dds = (peaks - normed) / peaks
            max_dd = float(dds.max())
        else:
            max_dd = 0.0

        trades.append(Trade(
            signal_date=sd_str,
            entry_date=soxx.index[entry_idx].strftime("%Y-%m-%d"),
            entry_open=entry_open,
            entry_price=entry_price,
            exit_date=soxx.index[exit_idx].strftime("%Y-%m-%d"),
            exit_close=exit_close,
            exit_price=exit_price,
            exit_reason=exit_reason,
            holding_days=int(exit_idx - entry_idx),
            trade_return=exit_price / entry_price - 1.0,
            max_drawdown=max_dd,
        ))
        last_exit_idx = exit_idx

    return trades


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------


def build_daily_returns(trades: list[Trade], soxx: pd.DataFrame) -> pd.Series:
    """Build a daily P&L series: per-day return when in a trade, else 0.

    On entry day: from cost-adjusted entry price to close.
    On exit day : from prior close to cost-adjusted exit price.
    Between     : standard close-to-close.

    Per-trade entry_price and exit_price already include the cost
    adjustment, so we read them off the Trade record directly — this
    means daily returns honour whatever cost config produced the trade.
    """
    out = pd.Series(0.0, index=soxx.index)
    closes = soxx["Close"].astype(float)
    for t in trades:
        entry_idx = soxx.index.get_loc(pd.Timestamp(t.entry_date))
        exit_idx = soxx.index.get_loc(pd.Timestamp(t.exit_date))
        # Same-day entry+exit: write the full cost-adjusted round trip into
        # the single bar. Otherwise the daily series only honours the entry
        # cost and silently disagrees with t.trade_return by one round trip.
        if exit_idx == entry_idx:
            out.iloc[entry_idx] = t.exit_price / t.entry_price - 1.0
            continue
        out.iloc[entry_idx] = closes.iloc[entry_idx] / t.entry_price - 1.0
        for j in range(entry_idx + 1, exit_idx):
            out.iloc[j] = closes.iloc[j] / closes.iloc[j - 1] - 1.0
        if exit_idx > entry_idx:
            out.iloc[exit_idx] = t.exit_price / closes.iloc[exit_idx - 1] - 1.0
    return out


def aggregate_stats(trades: list[Trade], daily_returns: pd.Series) -> dict:
    """Win rate, avg win, avg loss, profit factor, Sharpe, Sortino, max DD."""
    rets = np.array([t.trade_return for t in trades])
    if len(rets) == 0:
        return {"n_trades": 0}
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    win_rate = len(wins) / len(rets)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    profit_factor = (float(wins.sum() / -losses.sum())
                     if len(losses) and losses.sum() != 0 else float("inf"))
    # Annualised Sharpe / Sortino on the daily series (rf=0)
    dr = daily_returns.values
    if dr.std() > 0:
        sharpe = float(dr.mean() / dr.std() * math.sqrt(252))
    else:
        sharpe = float("nan")
    downside = dr[dr < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = float(dr.mean() / downside.std() * math.sqrt(252))
    else:
        sortino = float("nan")
    # Equity-curve max drawdown
    eq = (1.0 + daily_returns).cumprod()
    peaks = eq.cummax()
    max_dd = float((1.0 - eq / peaks).max())
    total_return = float(eq.iloc[-1] - 1.0)
    return {
        "n_trades": len(trades),
        "win_rate": float(win_rate),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "mean_trade_return": float(rets.mean()),
        "median_trade_return": float(np.median(rets)),
        "best_trade_return": float(rets.max()),
        "worst_trade_return": float(rets.min()),
        "mean_holding_days": float(np.mean([t.holding_days for t in trades])),
        "median_holding_days": float(np.median([t.holding_days for t in trades])),
        "mean_per_trade_max_dd": float(np.mean([t.max_drawdown for t in trades])),
        "equity_curve_total_return": total_return,
        "equity_curve_max_dd": max_dd,
        "sharpe_annualised": sharpe,
        "sortino_annualised": sortino,
        "exit_reason_counts": {
            r: int(sum(1 for t in trades if t.exit_reason == r))
            for r in sorted({t.exit_reason for t in trades})
        },
    }


# ---------------------------------------------------------------------------
# Mechanism diagnostic (signal-anchored fixed-horizon returns, no exits)
# ---------------------------------------------------------------------------


def mechanism_diagnostic(
    signal_dates: list[str], soxx: pd.DataFrame, horizons: list[int]
) -> dict:
    closes = soxx["Close"].astype(float)
    rows = []
    for sd_str in signal_dates:
        sd = pd.Timestamp(sd_str)
        if sd not in closes.index:
            continue
        sd_idx = closes.index.get_loc(sd)
        row = {"signal_date": sd_str}
        for h in horizons:
            if sd_idx + h < len(closes):
                row[f"fwd_{h}d"] = float(closes.iloc[sd_idx + h] / closes.iloc[sd_idx] - 1.0)
            else:
                row[f"fwd_{h}d"] = None
        rows.append(row)
    means = {}
    medians = {}
    pos_rates = {}
    for h in horizons:
        vals = [r[f"fwd_{h}d"] for r in rows if r[f"fwd_{h}d"] is not None]
        if vals:
            arr = np.array(vals)
            means[f"{h}d"] = float(arr.mean())
            medians[f"{h}d"] = float(np.median(arr))
            pos_rates[f"{h}d"] = float((arr > 0).mean())
    return {
        "horizons_days": horizons,
        "per_signal": rows,
        "mean_fwd_return": means,
        "median_fwd_return": medians,
        "positive_rate": pos_rates,
        "n_signals": len(rows),
    }


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def base_rate_returns(closes: pd.Series, horizons: list[int]) -> dict:
    """Unconditional rolling H-day return distribution stats."""
    out = {}
    for h in horizons:
        ret = closes / closes.shift(h) - 1.0
        ret = ret.dropna()
        if len(ret):
            out[f"{h}d"] = {
                "mean": float(ret.mean()),
                "median": float(np.median(ret)),
                "positive_rate": float((ret > 0).mean()),
                "n": int(len(ret)),
            }
    return out


def _sample_non_overlapping_random_trades(
    rng: np.random.Generator,
    holdings: np.ndarray,
    eligible_indices: np.ndarray,
    end_idx: int,
    n_trades: int,
) -> list[tuple[int, int]]:
    """Sample a sequential random-entry path with no overlapping trades.

    Phase 10.2 fix. The previous monte_carlo_null sampled entries with
    replace=False but did not enforce sequential non-overlap, meaning
    the 'null' was effectively allowed multiple concurrent positions
    while the actual strategy is one-position-at-a-time. That was an
    unfair comparison — the null had more 'shots on goal' than the
    strategy, biasing the percentile ranking.

    This sampler enforces: trade k+1 entry index > trade k exit index.
    WS15 fix (2026-08-13). The original placed each entry uniformly over
    ALL remaining feasible positions, reserving room for later trades at
    only the MINIMUM holding (1 session here). Early entries therefore
    scattered deep into the window and stranded the rest: on the CNDX OOS
    re-run (13 trades, 596 total holding sessions, a ~1,750-session
    window) every one of 1,000 restart-bounded paths came back partial,
    monte_carlo_null discarded them all, and every percentile field was
    None. No committed artefact carries the damage — nothing was
    regenerated between the Phase 10.2 commit and this fix — but any
    regeneration would have shipped an empty null.

    Construction: bootstrap the n holding lengths first, then place all n
    entries at once through the classic gap transform — draw n iid
    integers on the exact feasible box, sort ascending, and shift the
    k-th by the space the first k-1 trades occupy. Every draw satisfies
    entry order, the one-session separation, the entry ceiling and the
    exit ceiling by construction, so a feasible configuration is never
    dead-ended; [] is returned only when the window genuinely cannot hold
    the drawn holdings, and a redraw of holdings is attempted first.
    """
    if n_trades == 0 or len(eligible_indices) == 0:
        return []
    e0 = int(eligible_indices[0])
    e1 = int(eligible_indices[-1])
    if len(eligible_indices) != e1 - e0 + 1:
        raise ValueError(
            "the gap-transform placement assumes a contiguous eligible "
            "window; got a gapped eligible_indices")
    for _attempt in range(100):
        hs = rng.choice(holdings, size=n_trades, replace=True).astype(int)
        # S_k = sessions occupied by trades 1..k plus one separator each.
        s_cum = np.cumsum(hs + 1)
        # u_k = entry_k - S_{k-1} must be non-decreasing on [e0, hi]:
        #   entry ceiling  e_k <= e1      -> u_n <= e1 - S_{n-1}
        #   exit ceiling   exit_n <= end  -> u_n <= end_idx - S_n + 1
        hi = min(e1 - int(s_cum[-2]) if n_trades > 1 else e1,
                 end_idx - int(s_cum[-1]) + 1)
        if hi < e0:
            continue  # this holding draw does not fit; redraw
        u = np.sort(rng.integers(e0, hi + 1, size=n_trades))
        entries = u + np.concatenate(([0], s_cum[:-1]))
        return [(int(e), int(e + h)) for e, h in zip(entries, hs)]
    return []


def monte_carlo_null(
    trades: list[Trade],
    soxx: pd.DataFrame,
    eligible_start: pd.Timestamp,
    n_paths: int = MC_PATHS,
    seed: int = MC_SEED,
    cost_bps_one_side: int = COST_BPS_ONE_SIDE,
    eligible_end: pd.Timestamp | None = None,
) -> dict:
    """Random-entry null with bootstrapped holding periods, costs applied
    identically to the strategy. Random trades are sequential and non-
    overlapping, matching the strategy's one-position-at-a-time
    constraint. Returns percentile rank of strategy aggregate return
    and Sharpe vs the null distribution.
    """
    rng = np.random.default_rng(seed)
    n_trades = len(trades)
    if n_trades == 0:
        return {"n_paths": n_paths, "n_trades": 0}
    holdings = np.array([t.holding_days for t in trades])
    cost = cost_bps_one_side / 10_000.0

    # Eligible entry-bar pool: any trading day from eligible_start to
    # eligible_end (default: end of data). Trades that would exit after
    # eligible_end are filtered out so every random trade fully runs
    # within the eligible window.
    max_h = int(holdings.max())
    eligible_mask = (soxx.index >= eligible_start)
    if eligible_end is not None:
        eligible_mask &= (soxx.index <= eligible_end)
    eligible_indices = np.where(eligible_mask)[0]
    # Find the last index whose exit (entry + max_h) still falls inside the
    # eligible window — bound by eligible_end if set, else end of data.
    if eligible_end is not None:
        end_idx = soxx.index.searchsorted(eligible_end, side="right") - 1
    else:
        end_idx = len(soxx) - 1
    eligible_indices = eligible_indices[eligible_indices + max_h <= end_idx]
    if len(eligible_indices) < n_trades:
        return {"n_paths": n_paths, "n_trades": n_trades, "note": "insufficient sample"}

    opens = soxx["Open"].astype(float).values
    closes = soxx["Close"].astype(float).values

    # Phase 10.2: NaN-init instead of zero-init so unsuccessful paths
    # (where the non-overlap sampler couldn't fit n_trades) don't
    # pollute the average with synthetic zeros. NaN aggregators below
    # exclude them properly.
    null_totals = np.full(n_paths, np.nan)
    null_means = np.full(n_paths, np.nan)
    null_sharpes = np.full(n_paths, np.nan)
    null_win_rates = np.full(n_paths, np.nan)

    for p in range(n_paths):
        trade_rets = []
        daily = np.zeros(len(soxx))
        random_path = _sample_non_overlapping_random_trades(
            rng, holdings, eligible_indices, end_idx, n_trades
        )
        for ei, exit_i in random_path:
            entry_eff = opens[ei] * (1.0 + cost)
            exit_eff = closes[exit_i] * (1.0 - cost)
            r = exit_eff / entry_eff - 1.0
            trade_rets.append(r)
            # populate daily series for Sharpe
            daily[ei] = closes[ei] / entry_eff - 1.0
            for j in range(ei + 1, exit_i):
                daily[j] = closes[j] / closes[j - 1] - 1.0
            if exit_i > ei:
                daily[exit_i] = exit_eff / closes[exit_i - 1] - 1.0
        # Phase 10.2: require ALL n_trades to have been placed, otherwise
        # the path is invalid (sampler couldn't fit them all). Previously
        # any non-empty list contributed to the null distribution which
        # biased it toward fewer-trade paths.
        if len(trade_rets) != n_trades:
            continue
        arr = np.array(trade_rets)
        # Geometric total return as if compounded across all trades (matches
        # the strategy convention).
        null_totals[p] = float(np.prod(1.0 + arr) - 1.0)
        null_means[p] = float(arr.mean())
        null_win_rates[p] = float((arr > 0).mean())
        if daily.std() > 0:
            null_sharpes[p] = float(daily.mean() / daily.std() * math.sqrt(252))
        else:
            null_sharpes[p] = float("nan")

    # Strategy aggregate (for direct comparison)
    strat_rets = np.array([t.trade_return for t in trades])
    strat_total = float(np.prod(1.0 + strat_rets) - 1.0)
    strat_mean = float(strat_rets.mean())
    strat_win_rate = float((strat_rets > 0).mean())

    def percentile_rank(value, array):
        valid = array[~np.isnan(array)]
        if len(valid) == 0:
            return None
        return float((valid <= value).mean() * 100.0)

    def nan_mean(array):
        valid = array[~np.isnan(array)]
        return float(valid.mean()) if len(valid) else None

    def nan_percentile(array, q):
        valid = array[~np.isnan(array)]
        return float(np.percentile(valid, q)) if len(valid) else None

    return {
        "n_paths": n_paths,
        "n_valid_paths": int((~np.isnan(null_totals)).sum()),
        "n_trades": n_trades,
        "strategy_total_return": strat_total,
        "strategy_mean_trade_return": strat_mean,
        "strategy_win_rate": strat_win_rate,
        "null_total_return_mean": nan_mean(null_totals),
        "null_total_return_p5": nan_percentile(null_totals, 5),
        "null_total_return_p50": nan_percentile(null_totals, 50),
        "null_total_return_p95": nan_percentile(null_totals, 95),
        "strategy_total_return_percentile": percentile_rank(strat_total, null_totals),
        "null_mean_trade_return_mean": nan_mean(null_means),
        "strategy_mean_trade_return_percentile": percentile_rank(strat_mean, null_means),
        "null_win_rate_mean": nan_mean(null_win_rates),
        "strategy_win_rate_percentile": percentile_rank(strat_win_rate, null_win_rates),
        "null_sharpe_p50": nan_percentile(null_sharpes, 50),
        "null_sharpe_p5": nan_percentile(null_sharpes, 5),
        "null_sharpe_p95": nan_percentile(null_sharpes, 95),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf", default=DEFAULT_ETF,
                   help=f"ETF symbol (must be in etf_registry). Default: {DEFAULT_ETF}.")
    p.add_argument("--yf-symbol", default=None,
                   help="Override yfinance ticker for OHLC fetch — useful when "
                        "the iShares ETF and the US-listed equivalent differ "
                        "(e.g. --etf CSP1 --yf-symbol SPY).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    etf = args.etf
    yf_sym = args.yf_symbol or etf
    paths = paths_for(etf)
    breadth_path = paths["breadth"]
    out_path = paths["out"]

    print(f"Loading breadth signal ({breadth_path.name}) ...", flush=True)
    breadth, signal_records = load_breadth(etf=etf)
    signal_dates = [s["date"] for s in signal_records]
    print(f"  Breadth covers {breadth.index[0].date()} -> {breadth.index[-1].date()}, "
          f"{len(breadth)} trading days")
    print(f"  Signal-fire days: {len(signal_dates)}")

    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"Downloading {yf_sym} OHLC and SPY close ...", flush=True)
    soxx = download_soxx_ohlc(dl_start, dl_end, etf=etf, yf_symbol=yf_sym)
    spy = download_spy_close(dl_start, dl_end)
    soxx = soxx[~soxx.index.duplicated(keep="first")]
    print(f"  {yf_sym} rows: {len(soxx)}; SPY rows: {len(spy)}")

    print("Running strategy ...", flush=True)
    trades = run_strategy(signal_dates, soxx, breadth)
    print(f"  Trades opened: {len(trades)}")

    daily_returns = build_daily_returns(trades, soxx)
    primary = aggregate_stats(trades, daily_returns)

    print("Mechanism diagnostic ...", flush=True)
    diag = mechanism_diagnostic(signal_dates, soxx, HORIZONS)

    print("Benchmarks ...", flush=True)
    soxx_base = base_rate_returns(soxx["Close"].astype(float), HORIZONS)
    spy_base = base_rate_returns(spy.astype(float), HORIZONS)

    print("Monte Carlo null ...", flush=True)
    eligible_start = breadth.index[252] if len(breadth) > 252 else breadth.index[0]
    mc = monte_carlo_null(trades, soxx, eligible_start)

    # Equity curves for plotting / sanity
    strat_eq = (1.0 + daily_returns).cumprod()
    aligned_close = soxx["Close"].astype(float).reindex(daily_returns.index)
    soxx_eq = aligned_close / aligned_close.iloc[0]
    aligned_spy = spy.astype(float).reindex(daily_returns.index)
    spy_eq = aligned_spy / aligned_spy.iloc[0]

    payload = {
        "etf": etf,
        "yf_symbol_for_ohlc": yf_sym,
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "breadth_source": str(breadth_path.relative_to(PROJECT_ROOT)),
        "config": {
            "cost_bps_one_side": COST_BPS_ONE_SIDE,
            "atr_period": ATR_PERIOD,
            "trailing_stop_atr_multiple": TRAILING_STOP_K,
            "regime_ma_floor": REGIME_MA_FLOOR,
            "time_stop_days": TIME_STOP_DAYS,
            "horizons_days": HORIZONS,
            "monte_carlo_paths": MC_PATHS,
            "monte_carlo_seed": MC_SEED,
            "entry_bar": "next_trading_day_open",
            "exit_bar": "same_day_close_on_trigger",
        },
        "trades": [asdict(t) for t in trades],
        "primary": primary,
        "mechanism_diagnostic": diag,
        "benchmarks": {
            f"{yf_sym.lower()}_unconditional_base_rate": soxx_base,
            "spy_unconditional_base_rate": spy_base,
            "monte_carlo_null": mc,
        },
        "equity_curves": {
            "dates": [d.strftime("%Y-%m-%d") for d in daily_returns.index],
            "strategy": [round(float(x), 6) for x in strat_eq.ffill().values],
            f"{yf_sym.lower()}_buy_hold": [round(float(x), 6) for x in soxx_eq.ffill().values],
            "spy_buy_hold": [round(float(x), 6) for x in spy_eq.ffill().values],
        },
    }

    # Clean NaN/inf for JSON
    def clean(o):
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else o
        if isinstance(o, list):
            return [clean(x) for x in o]
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        return o
    payload = clean(payload)

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print()
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)}")
    print("=" * 60)
    print("PRIMARY (per-trade with exits + costs)")
    print("=" * 60)
    for k, v in primary.items():
        if isinstance(v, float):
            print(f"  {k:32} {v:+.4f}")
        else:
            print(f"  {k:32} {v}")
    print()
    print("=" * 60)
    print("MECHANISM DIAGNOSTIC (fixed-horizon forward, no exits)")
    print("=" * 60)
    for h in HORIZONS:
        m = diag["mean_fwd_return"].get(f"{h}d")
        pr = diag["positive_rate"].get(f"{h}d")
        base = soxx_base.get(f"{h}d", {})
        bm = base.get("mean")
        bp = base.get("positive_rate")
        print(f"  {h:>3}d  signal mean = {m:+.4f}  pos rate = {pr:.2%}   "
              f"|  {yf_sym} base mean = {bm:+.4f}  pos = {bp:.2%}")
    print()
    print("=" * 60)
    print("MONTE CARLO NULL (1,000 random-entry paths, bootstrapped holds)")
    print("=" * 60)
    print(f"  Strategy total return       : {mc['strategy_total_return']:+.4f}")
    print(f"  Null total return p5/50/95  : "
          f"{mc['null_total_return_p5']:+.4f} / "
          f"{mc['null_total_return_p50']:+.4f} / "
          f"{mc['null_total_return_p95']:+.4f}")
    print(f"  Strategy total %ile in null : {mc['strategy_total_return_percentile']:.1f}")
    print(f"  Strategy win rate %ile      : {mc['strategy_win_rate_percentile']:.1f}")
    print(f"  Strategy mean-ret %ile      : {mc['strategy_mean_trade_return_percentile']:.1f}")
    print(f"  Null Sharpe p5/50/95        : "
          f"{mc['null_sharpe_p5']:+.2f} / "
          f"{mc['null_sharpe_p50']:+.2f} / "
          f"{mc['null_sharpe_p95']:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
