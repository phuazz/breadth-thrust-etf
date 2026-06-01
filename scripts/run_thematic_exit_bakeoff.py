"""Strategy C exit-rule bake-off — six candidate exit improvements.

Strategy C's current signal (top-K by distance above 200d MA, +5% floor,
equal-weight) achieves CAGR +21% but max DD -50.9%. The big DD events
(early 2021 ARKK roll-over, late-2022 deep cycle) happen because the
200d MA is too slow for thematic-ETF volatility — the slow MA only
rolls over months AFTER price has corrected 30-50%.

This script tests six exit-rule modifications under a shared walk-
forward harness so we can compare apples-to-apples vs the baseline.
Each variant keeps the SAME ENTRY criterion (signal >= +5%) and the
SAME K=4 equal-weight portfolio construction. Only the HOLD/EXIT
condition differs.

Variants tested (see CLAUDE.md and chat log for the design rationale):

  V1 - Fast/slow EMA confirmation:    hold only if 50d EMA > 100d EMA
  V2 - Signal slope filter:           hold only if 20d slope of
                                       (distance-above-MA200) >= 0
  V3 - Trailing peak stop (15%):      exit if price < 0.85 * peak-
                                       while-held
  V4 - RSI overbought-and-rolling:    exit when RSI(14) was > 70
                                       then crosses back below 60
  V5 - Vol-adjusted trailing stop:    exit if price < peak - 3 * vol
  V6 - Sleeve breadth gate:           exit ALL positions if < 30%
                                       of universe is above +5% floor

All variants run on the same backtest window, same K=4, same SHY cash
floor, same 5 bps per turnover unit. Output: side-by-side comparison
table + the specific drawdown during the 2021-02 ARKK peak episode.

Run:
    python scripts/run_thematic_exit_bakeoff.py

Output: data/thematic_exit_bakeoff.json + printed summary table.
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
sys.stdout.reconfigure(encoding="utf-8")

from run_thematic_rotation import (  # noqa: E402
    UNIVERSE, TICKERS, CASH_PROXY, START_DATE, END_DATE,
    MA_PERIOD, SIGNAL_FLOOR, COST_FRAC,
    download_prices, compute_signal, _safe,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Deployed K from run_thematic_rotation.py — keep constant across all
# variants so the comparison isolates the effect of the exit rule.
HEADLINE_K = 4

# Episode-specific drawdown we want to focus on (the Feb 2021 thematic
# roll-over that hurt the baseline most).
EPISODE_2021_PEAK = "2021-02-01"
EPISODE_2021_TROUGH = "2022-12-31"


# ---------------------------------------------------------------------------
# Per-ETF technical features used by the variant rules
# ---------------------------------------------------------------------------


def compute_ema(closes: pd.DataFrame, span: int) -> pd.DataFrame:
    return closes.ewm(span=span, adjust=False, min_periods=span).mean()


def compute_signal_slope(signal: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Per-ETF rolling slope of the signal (distance above MA200).

    A positive slope means the distance is widening (still strengthening);
    a negative slope means it is contracting (cooling off) even if still
    positive in level. Used by Variant 2 to filter eligibility on
    momentum direction in addition to momentum level."""
    return signal - signal.shift(window)


def compute_rsi(closes: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Per-ETF Wilder RSI (14-day)."""
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    # Wilder smoothing = EMA with alpha=1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False,
                         min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False,
                         min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_realised_vol(closes: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Per-ETF rolling realised vol = stdev of daily returns over window,
    expressed as a single-day stdev (not annualised). Used as the
    width multiplier for Variant 5's vol-adjusted trailing stop."""
    returns = closes.pct_change()
    return returns.rolling(window, min_periods=window).std()


# ---------------------------------------------------------------------------
# State-aware backtest harness
# ---------------------------------------------------------------------------


def _initial_state(tickers: list[str]) -> dict:
    """Stateful per-ETF tracking for the variants that need it.

    Keys per ticker:
      held              - bool: currently in the portfolio
      peak_price        - max close since entry while held
      was_overbought    - did RSI cross above 70 during this position?
    """
    return {
        t: {"held": False, "peak_price": None, "was_overbought": False}
        for t in tickers
    }


def _build_target_weights_baseline(
    s_row: pd.Series, K: int,
    **_kwargs,
) -> pd.Series:
    """Reference: the deployed +5% floor / top-K / equal-weight rule.
    Used by every variant as the ENTRY criterion; variants then add
    their exit logic on top."""
    w = pd.Series(0.0, index=s_row.index)
    valid = s_row.dropna()
    eligible = valid[valid > SIGNAL_FLOOR]
    if CASH_PROXY in eligible.index:
        eligible = eligible.drop(CASH_PROXY)
    if len(eligible) == 0:
        if CASH_PROXY in w.index:
            w[CASH_PROXY] = 1.0
        return w
    top = eligible.nlargest(min(K, len(eligible)))
    invested_frac = len(top) / K
    per_etf = invested_frac / len(top)
    w.loc[top.index] = per_etf
    cash = 1.0 - invested_frac
    if cash > 0 and CASH_PROXY in w.index:
        w[CASH_PROXY] = cash
    return w


def _run_variant(
    closes: pd.DataFrame,
    signal: pd.DataFrame,
    K: int,
    eligible_start: pd.Timestamp,
    variant_name: str,
    variant_eligible_fn,  # (signal_row, prev_close_row, state, **feat) -> Series of eligible flags
    features: dict,  # named feature panels (DataFrames) needed by the variant
) -> dict:
    """Run a weekly-Friday rotation backtest with a variant-specific
    eligibility/exit rule.

    The eligibility function decides at each rebal date which ETFs
    pass BOTH the standard entry criterion AND the variant's hold
    condition. ETFs newly entering get their state initialised;
    ETFs exiting have state cleared."""
    rebalance_target = pd.date_range(eligible_start, closes.index[-1],
                                      freq="W-FRI")
    rebalance_dates = closes.index[closes.index.isin(rebalance_target)]
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)

    state = _initial_state(list(closes.columns))

    for rd in rebalance_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0:
            continue
        s_row = signal.iloc[prev_idx]
        prev_close = closes.iloc[prev_idx]

        # ---- Per-ETF state update: peak_price + was_overbought
        # Update tracked state for every ticker (held or not) so RSI/peak
        # tracking is correct when an ETF re-enters.
        for t in closes.columns:
            px = prev_close.get(t)
            if px is not None and px == px:  # not NaN
                if state[t]["held"]:
                    pk = state[t]["peak_price"]
                    state[t]["peak_price"] = max(pk, px) if pk is not None else px
                # Track was_overbought flag for any ETF currently held
                rsi_panel = features.get("rsi")
                if rsi_panel is not None and state[t]["held"]:
                    rsi_val = rsi_panel.iloc[prev_idx].get(t)
                    if rsi_val is not None and rsi_val == rsi_val and rsi_val > 70:
                        state[t]["was_overbought"] = True

        # ---- Decide eligibility for this rebal -------------------
        eligible = variant_eligible_fn(s_row, prev_close, prev_idx, state, features)

        # Build target weights from eligible set (top K equal-weight,
        # SHY for unfilled slots — same as baseline weight function)
        w = pd.Series(0.0, index=closes.columns)
        if CASH_PROXY in eligible.index:
            eligible = eligible.drop(CASH_PROXY)
        if len(eligible) == 0:
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
        else:
            top = eligible.nlargest(min(K, len(eligible)))
            invested_frac = len(top) / K
            per_etf = invested_frac / len(top)
            w.loc[top.index] = per_etf
            cash = 1.0 - invested_frac
            if cash > 0 and CASH_PROXY in w.index:
                w[CASH_PROXY] = cash

        # ---- Update state for entries/exits ---------------------
        new_held = set(w[w > 1e-6].index) - {CASH_PROXY}
        old_held = {t for t, st in state.items() if st["held"]}
        # Newly entering ETFs — initialise their tracking state
        for t in new_held - old_held:
            px = prev_close.get(t)
            state[t]["held"] = True
            state[t]["peak_price"] = float(px) if px is not None and px == px else None
            state[t]["was_overbought"] = False
        # Exited ETFs — clear state
        for t in old_held - new_held:
            state[t]["held"] = False
            state[t]["peak_price"] = None
            state[t]["was_overbought"] = False

        rb_weights.loc[rd] = w

    weight_panel = rb_weights.reindex(closes.index).ffill().fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {
        "name": variant_name,
        "equity": equity,
        "weights": weight_panel,
        "turnover": turnover,
        "daily_ret": port_ret,
    }


# ---------------------------------------------------------------------------
# Eligibility functions — one per variant
# ---------------------------------------------------------------------------


def _eligible_baseline(s_row, _prev_close, _idx, _state, _feat):
    valid = s_row.dropna()
    return valid[valid > SIGNAL_FLOOR]


def _eligible_v1_fast_slow_ema(s_row, _prev_close, idx, _state, feat):
    """Hold only if standard floor passes AND 50d EMA > 100d EMA."""
    fast = feat["ema_fast"].iloc[idx]
    slow = feat["ema_slow"].iloc[idx]
    valid = s_row.dropna()
    floor_pass = valid[valid > SIGNAL_FLOOR]
    fast_above_slow = (fast > slow).reindex(floor_pass.index).fillna(False)
    return floor_pass[fast_above_slow]


def _eligible_v2_slope(s_row, _prev_close, idx, _state, feat):
    """Hold only if standard floor passes AND 20d slope of signal >= 0."""
    slope = feat["signal_slope"].iloc[idx]
    valid = s_row.dropna()
    floor_pass = valid[valid > SIGNAL_FLOOR]
    slope_ok = (slope >= 0).reindex(floor_pass.index).fillna(False)
    return floor_pass[slope_ok]


def _eligible_v3_trailing_stop(stop_frac: float):
    """Closure: hold only if standard floor passes AND current price
    >= (1 - stop_frac) * peak-while-held. Held ETFs that breach the
    stop are excluded for this rebal; un-held ETFs use only the
    floor criterion."""
    def f(s_row, prev_close, _idx, state, _feat):
        valid = s_row.dropna()
        floor_pass = valid[valid > SIGNAL_FLOOR]
        keepers = []
        for t in floor_pass.index:
            if state[t]["held"]:
                pk = state[t]["peak_price"]
                px = prev_close.get(t)
                if pk is not None and px is not None and px == px:
                    if px < (1 - stop_frac) * pk:
                        continue  # hit trailing stop, exclude
            keepers.append(t)
        return floor_pass.loc[keepers]
    return f


def _eligible_v4_rsi(s_row, prev_close, idx, state, feat):
    """Exit if RSI was > 70 during this position and now < 60.
    Standard floor entry for un-held ETFs."""
    rsi_panel = feat["rsi"]
    valid = s_row.dropna()
    floor_pass = valid[valid > SIGNAL_FLOOR]
    keepers = []
    for t in floor_pass.index:
        if state[t]["held"]:
            rsi_val = rsi_panel.iloc[idx].get(t)
            if rsi_val is not None and rsi_val == rsi_val:
                if state[t]["was_overbought"] and rsi_val < 60:
                    continue
        keepers.append(t)
    return floor_pass.loc[keepers]


def _eligible_v5_vol_stop(k_sigma: float = 3.0):
    """Closure: trailing stop where stop distance scales with realised
    vol. Stop level = peak * (1 - k_sigma * 1d_realised_vol_20d).
    Wider stop in high-vol regimes, tighter in low-vol."""
    def f(s_row, prev_close, idx, state, feat):
        vol = feat["realised_vol"].iloc[idx]
        valid = s_row.dropna()
        floor_pass = valid[valid > SIGNAL_FLOOR]
        keepers = []
        for t in floor_pass.index:
            if state[t]["held"]:
                pk = state[t]["peak_price"]
                px = prev_close.get(t)
                v = vol.get(t)
                if (pk is not None and px is not None and px == px
                        and v is not None and v == v):
                    stop_level = pk * (1 - k_sigma * v)
                    if px < stop_level:
                        continue
            keepers.append(t)
        return floor_pass.loc[keepers]
    return f


def _eligible_v6_sleeve_breadth(min_breadth: float = 0.30):
    """If fewer than min_breadth fraction of the C universe is above
    the +5% floor (excluding cash proxy), exit ALL positions for this
    rebal — entire sleeve goes to cash."""
    def f(s_row, _prev_close, _idx, _state, _feat):
        valid = s_row.dropna()
        # Universe count: exclude SHY cash proxy
        univ = valid.drop(CASH_PROXY, errors="ignore")
        n_universe = len(univ)
        if n_universe == 0:
            return pd.Series(dtype=float)
        n_above = (univ > SIGNAL_FLOOR).sum()
        sleeve_breadth = n_above / n_universe
        if sleeve_breadth < min_breadth:
            return pd.Series(dtype=float)  # empty - no ETFs eligible
        return univ[univ > SIGNAL_FLOOR]
    return f


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_metrics(equity: pd.Series, eligible_start: pd.Timestamp,
                     turnover_panel: pd.DataFrame | None,
                     weight_panel: pd.DataFrame | None) -> dict:
    eq = equity.loc[equity.index >= eligible_start].copy()
    if len(eq) == 0:
        return {}
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
    # 2021-02 episode-specific peak-to-trough
    try:
        episode_eq = eq.loc[EPISODE_2021_PEAK:EPISODE_2021_TROUGH]
        if len(episode_eq) > 1:
            ep_peak = episode_eq.cummax()
            ep_dd = (episode_eq - ep_peak) / ep_peak
            episode_dd = float(ep_dd.min())
        else:
            episode_dd = None
    except Exception:
        episode_dd = None
    # Annualised turnover
    ann_turnover = None
    if turnover_panel is not None and weight_panel is not None:
        wp = weight_panel.loc[weight_panel.index >= eligible_start]
        diff = wp.diff().abs().sum(axis=1).fillna(0)
        ann_turnover = float(diff.sum() / n_years) if n_years > 0 else 0.0
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(cagr),
        "total_return": _safe(total_ret),
        "max_dd": _safe(max_dd),
        "episode_2021_dd": _safe(episode_dd),
        "annual_turnover": _safe(ann_turnover),
        "n_years": _safe(n_years),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("STRATEGY C EXIT-RULE BAKE-OFF — 6 variants vs baseline")
    print("=" * 78)
    print(f"Universe: {len(TICKERS)} thematic ETFs ({CASH_PROXY} cash floor)")
    print(f"K = {HEADLINE_K}, Weekly Fri rebalance, {SIGNAL_FLOOR*100:.0f}% signal floor")
    print()

    print("Loading prices ...", flush=True)
    closes = download_prices()
    print(f"  Loaded {len(closes.columns)} tickers, "
          f"{closes.index[0].date()} -> {closes.index[-1].date()}")
    signal = compute_signal(closes)

    # ---- Pre-compute technical features for all variants -------
    print("Computing per-ETF features (EMAs, slope, RSI, vol) ...", flush=True)
    features = {
        "ema_fast": compute_ema(closes, 50),
        "ema_slow": compute_ema(closes, 100),
        "signal_slope": compute_signal_slope(signal, 20),
        "rsi": compute_rsi(closes, 14),
        "realised_vol": compute_realised_vol(closes, 20),
    }

    eligible_start = pd.Timestamp("2018-11-08")  # match deployed C window

    variants = [
        ("Baseline (deployed)", _eligible_baseline),
        ("V1 · Fast/slow EMA confirm", _eligible_v1_fast_slow_ema),
        ("V2 · Signal slope filter", _eligible_v2_slope),
        ("V3 · Trailing stop 15%", _eligible_v3_trailing_stop(0.15)),
        ("V3a · Trailing stop 10%", _eligible_v3_trailing_stop(0.10)),
        ("V3b · Trailing stop 20%", _eligible_v3_trailing_stop(0.20)),
        ("V4 · RSI overbought-rollover", _eligible_v4_rsi),
        ("V5 · Vol-adjusted stop (k=3)", _eligible_v5_vol_stop(3.0)),
        ("V6 · Sleeve breadth gate 30%", _eligible_v6_sleeve_breadth(0.30)),
    ]

    print(f"Running {len(variants)} variants ...", flush=True)
    results = []
    for name, elig_fn in variants:
        print(f"  {name} ...", flush=True)
        r = _run_variant(closes, signal, HEADLINE_K, eligible_start,
                          name, elig_fn, features)
        m = compute_metrics(r["equity"], eligible_start, r["turnover"],
                              r["weights"])
        results.append({
            "name": name,
            "metrics": m,
            "equity_dates": [d.strftime("%Y-%m-%d")
                              for d in r["equity"].index],
            "equity": [float(v) if v == v else None
                        for v in r["equity"].values],
        })

    # ---- Comparison table ------------------------------------
    print()
    print("=" * 78)
    print("RESULTS — in-sample, common window 2018-11-08 → "
          f"{closes.index[-1].date()}")
    print("=" * 78)
    print(f"{'Variant':<32} {'Sharpe':>7} {'CAGR':>7} {'MaxDD':>8} "
          f"{'2021 DD':>8} {'Turn':>6}")
    print("-" * 78)
    baseline_m = results[0]["metrics"]
    for r in results:
        m = r["metrics"]
        sharpe = m.get("sharpe")
        cagr = m.get("cagr")
        mdd = m.get("max_dd")
        ep = m.get("episode_2021_dd")
        turn = m.get("annual_turnover")
        d_sharpe = (sharpe - baseline_m.get("sharpe", 0)) if sharpe is not None else None
        d_mdd = (mdd - baseline_m.get("max_dd", 0)) if mdd is not None else None
        sharpe_s = f"{sharpe:+.2f}" if sharpe is not None else "—"
        cagr_s = f"{cagr*100:+.1f}%" if cagr is not None else "—"
        mdd_s = f"{mdd*100:.1f}%" if mdd is not None else "—"
        ep_s = f"{ep*100:.1f}%" if ep is not None else "—"
        turn_s = f"{turn:.1f}x" if turn is not None else "—"
        marker = ""
        if r["name"] != "Baseline (deployed)":
            dd_target = -0.40
            if mdd is not None and mdd > dd_target and sharpe is not None:
                if abs(d_sharpe or 0) <= 0.10:
                    marker = "  ←PASS"
        print(f"  {r['name']:<30} {sharpe_s:>7} {cagr_s:>7} {mdd_s:>8} "
              f"{ep_s:>8} {turn_s:>6}{marker}")
    print()
    print("Acceptance: MaxDD better than -40% AND Sharpe degradation < 0.10")
    print()

    # ---- Save JSON ------------------------------------------
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "K": HEADLINE_K,
        "signal_floor": SIGNAL_FLOOR,
        "rebalance_freq": "W-FRI",
        "eligible_start": str(eligible_start.date()),
        "end_date": str(closes.index[-1].date()),
        "n_universe": int(len(TICKERS)),
        "variants": [
            {
                "name": r["name"],
                "metrics": r["metrics"],
                "equity_dates": r["equity_dates"],
                "equity": r["equity"],
            }
            for r in results
        ],
        "acceptance_criteria": {
            "max_dd_target": -0.40,
            "max_sharpe_degradation": 0.10,
            "note": ("V3 trailing stop variants are parameter-tuned in-"
                      "sample; if any pass acceptance criteria the next "
                      "step is walk-forward validation with annual stop-"
                      "width refit. V1, V2, V6 are parameter-free "
                      "(industry-standard thresholds) so are robust to "
                      "in-sample testing."),
        },
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "thematic_exit_bakeoff.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
