"""Relative-strength portfolio construction across the full ETF universe.

Builds long-only portfolios that tilt toward ETFs with the strongest
current breadth health. Evaluates a small grid of construction rules
and benchmarks against equal-weight all-ETFs and SPY buy-and-hold.

The single-signal MA200 sweep result (Family D: 50% base, 150% on signal)
showed that being long-and-leveraged when % above 200d MA crosses a
mid-to-high threshold beats buy-and-hold per ETF. Portfolio version asks:
across N tradeable ETFs, does CONCENTRATING into the K with the strongest
current breadth produce a better portfolio than equal-weight all of them?

Variants tested per (K, weighting, rebalance_freq):
  - top_k_eq_weight    : pick top K by current ma200_breadth, equal weight
  - top_k_rank_weight  : pick top K, weight inversely by rank (rank-1 gets
                          most, rank-K gets least)
  - top_k_breadth_weight: pick top K, weight by breadth fraction
  - equal_weight_all   : 1/N across every ETF (benchmark)
  - leveraged variants : as above but with the 50/150 sizing overlay
                          (50% base in the chosen basket, 150% when
                          basket-avg breadth > 60 per cent)

All portfolios are rebalanced weekly (Fridays) with 5 bps round-trip cost
per unit of turnover.

Output: data/portfolio_construction.json
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import download_soxx_ohlc, download_spy_close  # noqa: E402
from etf_registry import get_etf, UNIVERSE_ETFS as ETFS  # noqa: E402
from run_improvements import compute_stats  # noqa: E402
from run_ma200_sweep import (  # noqa: E402
    align_breadth_to_index, compute_ma200_breadth, load_constituent_prices,
    MA_PERIOD, COST_BPS,
)
from rebalance_calendar import engine_rebalance_dates  # noqa: E402
from session_bounds import (  # noqa: E402
    decision_session_report,
    trim_to_completed,
)

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "portfolio_construction.json"

TOP_K_VALUES = [3, 5, 7]
LEVERAGE_THRESHOLD = 0.60  # If basket-average ma200_breadth > 0.6, lever to 150%


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


def build_panels() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Returns (closes_panel, ma200_b_panel, etfs_used).

    closes_panel    : DataFrame indexed by date, columns = ETFs, values = trading
                      proxy close prices.
    ma200_b_panel   : DataFrame indexed by date, columns = ETFs, values = ma200
                      breadth fraction (0-1).
    """
    return _build_panels_for(ETFS)


def _build_panels_for(universe: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Build (closes, breadths, etfs_used) panels for an arbitrary ETF list.
    Used by Phase 4 to construct Europe-sector-only or Country-only panels
    without modifying the global UNIVERSE_ETFS."""
    closes = {}
    breadths = {}
    used = []
    for etf in universe:
        cfg = get_etf(etf)
        proxy = cfg.get("yfinance_trading_proxy") or etf
        try:
            cp = load_constituent_prices(etf)
        except FileNotFoundError:
            continue
        ma200_b = compute_ma200_breadth(cp, MA_PERIOD)
        dl_start = (cp.index.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end = (cp.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        ohlc = download_soxx_ohlc(dl_start, dl_end, etf=proxy, yf_symbol=proxy)
        ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
        # Drop any bar from a session that has not closed. On 2026-08-14 at
        # 13:15 UTC yfinance served a bar stamped that day for the .DE lines
        # while Xetra still had two hours to run, and Strategy D ranked on it.
        # Historically a no-op — every bar in a finished session is complete —
        # so this removes a tail and cannot move a backtest.
        ohlc, _dropped = trim_to_completed(
            ohlc, mcal.get_calendar(cfg.get("trading_calendar", "NYSE")),
            datetime.now(timezone.utc), label=f"{etf} ({proxy}) closes")
        closes[etf] = ohlc["Close"].astype(float)
        breadths[etf] = ma200_b
        used.append(etf)
    closes_df = pd.DataFrame(closes).sort_index()
    # Phase 10.2: route each ETF's breadth through align_breadth_to_index,
    # which forward-fills only up to MAX_BREADTH_STALE_DAYS (=7) of staleness
    # then returns NaN. Previously the unbounded .ffill() would carry stale
    # breadth forward forever, silently masking source-data freshness bugs.
    # The strategy engines treat NaN breadth as "no signal → no allocation"
    # (the eligibility check inside top_k_breadth_weight already handles
    # this), so a sleeve goes flat rather than trading on stale signal.
    breadths_df = pd.DataFrame({
        etf: align_breadth_to_index(b, closes_df.index)
        for etf, b in breadths.items()
    })
    # Say out loud whether the panel reaches the session a decision would rank
    # on. It is reindexed onto the EXECUTION calendar above, so a hole in the
    # traded line deletes a signal that exists: on 2026-08-14 the constituent
    # prices for EXV1/EXH3/EXV3 all carried Thursday 13 Aug while the .DE
    # lines did not, and the ranking silently fell back to Wednesday. This
    # does not repair the hole — that would change which session historical
    # rebalances ranked on — it refuses to let the fallback happen quietly.
    if used:
        cal_names = {get_etf(e).get("trading_calendar", "NYSE") for e in used}
        for cn in sorted(cal_names):
            decision_session_report(
                closes_df, mcal.get_calendar(cn), datetime.now(timezone.utc),
                label=f"{'/'.join(used[:3])}{'...' if len(used) > 3 else ''} "
                      f"closes [{cn}]")
    return closes_df, breadths_df, used


def run_portfolio(
    closes: pd.DataFrame,
    breadths: pd.DataFrame,
    weight_fn,
    eligible_start: pd.Timestamp,
    cost: float = COST_BPS / 10_000,
    rebalance_freq: str = "W-FRI",
    calendar: str = "NYSE",
) -> dict:
    """Simulate a weekly-rebalanced portfolio.

    `weight_fn(b_row)` takes a row of ma200_breadth (Series indexed by ETF)
    and returns a Series of weights (sums to <= 1).

    `calendar` is the venue this sleeve trades on. It defaults to NYSE for
    Strategy A, but this function is ALSO the engine behind Strategy D
    (run_europe_rotation imports it), which trades XETR — so D must pass its
    own calendar or a holiday-aware run would judge German sessions against
    the US calendar.
    """
    # Rebalance grid: every Friday close, build weights from yesterday's
    # breadth. Cadence rule centralised in rebalance_calendar.
    rebalance_dates = engine_rebalance_dates(
        closes.index, eligible_start, rebalance_freq, calendar
    )
    # Build a sparse rebalance-weights frame (rows = rebalance days),
    # then reindex to daily with ffill so the WHOLE row carries forward
    # (a dropped position correctly goes to 0 on the next rebalance, not
    # the previous-rebalance leftover).
    rb_weights = pd.DataFrame(
        index=rebalance_dates, columns=closes.columns, dtype=float
    )
    for rd in rebalance_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0:
            continue
        b_row = breadths.iloc[prev_idx]
        rb_weights.loc[rd] = weight_fn(b_row).reindex(closes.columns).fillna(0.0)
    weight_panel = rb_weights.reindex(closes.index, method="ffill").fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0

    # Daily returns per ETF
    rets = closes.pct_change().fillna(0)
    # Portfolio return = yesterday's weights * today's returns
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    # Turnover cost
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * cost
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel,
            "rebalance_dates": rebalance_dates}


def top_k_eq_weight(K: int):
    def f(b_row):
        valid = b_row.dropna()
        if len(valid) == 0:
            return pd.Series(0.0, index=b_row.index)
        top = valid.nlargest(min(K, len(valid))).index
        w = pd.Series(0.0, index=b_row.index)
        w.loc[top] = 1.0 / len(top)
        return w
    return f


def top_k_rank_weight(K: int):
    def f(b_row):
        valid = b_row.dropna()
        if len(valid) == 0:
            return pd.Series(0.0, index=b_row.index)
        top = valid.nlargest(min(K, len(valid)))
        ranks = pd.Series(range(len(top), 0, -1), index=top.index, dtype=float)
        ranks = ranks / ranks.sum()
        w = pd.Series(0.0, index=b_row.index)
        w.loc[top.index] = ranks
        return w
    return f


def top_k_breadth_weight(K: int):
    """Pick top K by signal value, weight by positive signal share only.

    Phase 20.1 (2026-05-28) — bug fix for sector-relative-breadth signal.

    Pre Phase 20: absolute breadth was always in [0,1] so every top-K
    pick was positive, and the original `normed = top / top.sum()` line
    produced clean weights that summed to 1.0.

    Phase 20 introduced sector-RELATIVE breadth (sector - cross-sectional
    mean). After mean-subtraction the top-K can contain NEGATIVE values
    (below-mean sectors). The original normaliser used
    `top.sum() = positives + negatives` which is SMALLER than
    `positives.sum()`, so each positive weight was amplified above its
    true share, and the negative weights implicitly shorted those
    below-mean sectors.

    The dashboard filter only displays positive weights, so post-Phase 20
    Strategy A's within-sleeve weights summed to >100% (114% in one
    observed snapshot), and the deployed-blend total ran ~105% even
    with the EEM tilt math fixed elsewhere.

    Beyond the display issue, the implicit shorting also meant the
    Phase 20 backtest was effectively LONG-SHORT, not long-only as
    advertised. Some of the Phase 20 lift (+5.3pp Full Total, +0.27
    Sharpe in 2022) came from those implicit shorts which are not
    actually implementable in a long-only portfolio.

    The fix: drop below-zero values from the top-K and weight only the
    positives. For absolute-breadth callers this is a no-op (all top-K
    were already positive). For relative-breadth callers it makes the
    strategy genuinely long-only — the picker holds only sectors
    above the cross-sectional mean, weighted by their excess.

    Net effect:
      - Strategy A under relative breadth: now holds N <= K positions
        (the positive-relative sectors), weights renormalised to 1.0.
      - Strategy D under absolute breadth: identical to pre-fix.
    """
    def f(b_row):
        valid = b_row.dropna()
        if len(valid) == 0:
            return pd.Series(0.0, index=b_row.index)
        top = valid.nlargest(min(K, len(valid)))
        # Drop below-zero values so the relative-breadth case stays
        # long-only. For absolute breadth (values in [0,1]) this is a
        # no-op.
        positives = top[top > 0]
        if len(positives) == 0:
            return pd.Series(0.0, index=b_row.index)
        normed = positives / positives.sum()
        w = pd.Series(0.0, index=b_row.index)
        w.loc[positives.index] = normed
        return w
    return f


def equal_weight_all_fn(b_row):
    valid = b_row.dropna()
    if len(valid) == 0:
        return pd.Series(0.0, index=b_row.index)
    w = pd.Series(0.0, index=b_row.index)
    w.loc[valid.index] = 1.0 / len(valid)
    return w


def family_d_eq_weight_ensemble(
    closes: pd.DataFrame,
    breadths: pd.DataFrame,
    etf_thresholds: dict[str, float],
    base: float = 0.5,
    on: float = 1.5,
    cost: float = COST_BPS / 10_000,
    window_start: pd.Timestamp | None = None,
) -> dict:
    """Equal-weight ensemble of N single-ETF MA200 50/150 (Family D) strategies.

    For each ETF in the universe, allocate `base` (50%) of THAT ETF's share
    of capital when its breadth is below its chosen L, `on` (150%) when at
    or above L. Then equal-weight across the N ETF strategies (1/N each).

    This is the FAIR no-selection baseline for "what does the MA200 signal
    earn if you run it on the whole universe with zero relative-strength
    tilt and zero ex-ante ETF selection." Comparing the top-K tilted
    portfolios against this isolates the value-add of the tilt itself.
    """
    n = len(closes.columns)
    rets = closes.pct_change().fillna(0)
    alloc = pd.DataFrame(base, index=closes.index, columns=closes.columns, dtype=float)
    for etf in closes.columns:
        L = etf_thresholds.get(etf)
        if L is None:
            continue
        b_shifted = breadths[etf].shift(1).fillna(0)
        alloc.loc[b_shifted >= L / 100.0, etf] = on
    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0
    weight = 1.0 / n
    daily_ret = (alloc * rets * weight).sum(axis=1)
    turnover = (alloc.diff().abs() * weight).sum(axis=1).fillna(0)
    daily_ret = daily_ret - turnover * cost
    equity = (1.0 + daily_ret).cumprod()
    return {"equity": equity, "alloc": alloc}


def apply_leverage_overlay(equity: pd.Series, weights: pd.DataFrame,
                            breadths: pd.DataFrame, threshold: float = LEVERAGE_THRESHOLD,
                            cost: float = COST_BPS / 10_000) -> pd.Series:
    """Apply a 50%/150% leverage overlay based on average basket breadth.

    Equivalent to: rebuild the portfolio with allocation = 0.5 + (alloc_signal * 1.0)
    where alloc_signal = 1 if basket avg breadth > threshold, else 0.
    """
    # Weighted-average breadth across the basket. Since weights sum to 1.0
    # on rebalance days, the weighted SUM is already the weighted average —
    # do NOT divide by n_active (would double-normalise to basket_avg / K).
    basket_avg_b = (weights * breadths.reindex(weights.index).fillna(0)).sum(axis=1)
    # Multiplier: 0.5 base or 1.5 when basket healthy
    mult = pd.Series(0.5, index=weights.index)
    mult.loc[basket_avg_b.shift(1).fillna(0) >= threshold] = 1.5
    # Recompute equity: daily strategy return = (mult * weights).shift(1) * rets
    # Easier: scale the daily returns
    daily_ret = equity.pct_change().fillna(0)
    scaled_ret = mult.shift(1).fillna(0) * daily_ret
    # Cost on multiplier changes (rough — assumes basket itself doesn't move)
    mult_change = mult.diff().abs().fillna(0)
    scaled_ret = scaled_ret - mult_change * cost
    return (1.0 + scaled_ret).cumprod()


def main() -> int:
    print("Loading panels (closes + ma200 breadth) for all ETFs ...", flush=True)
    closes, breadths, etfs_used = build_panels()
    print(f"  {len(etfs_used)} ETFs used: {etfs_used}")

    # Eligible start: latest of all ETFs' ma200-defined dates
    starts = []
    for etf in etfs_used:
        b = breadths[etf].dropna()
        if len(b):
            starts.append(b.index.min())
    eligible = max(starts)
    eligible = pd.Timestamp(eligible.date()) + pd.Timedelta(days=MA_PERIOD)
    eligible = closes.index[closes.index >= eligible][0] if (closes.index >= eligible).any() else closes.index[MA_PERIOD]
    print(f"  Eligible start: {eligible.date()}")

    # SPY benchmark
    spy_close = download_spy_close(closes.index.min().strftime("%Y-%m-%d"),
                                    (closes.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d"))
    spy_close = spy_close.reindex(closes.index).ffill()

    results: dict[str, dict] = {}
    print("\n=== Running portfolio variants ===")

    # Equal-weight all (benchmark)
    r = run_portfolio(closes, breadths, equal_weight_all_fn, eligible)
    eq_window = r["equity"].loc[r["equity"].index >= eligible]
    eq_window = eq_window / eq_window.iloc[0]
    st = compute_stats(r["equity"], eligible)
    results["equal_weight_all"] = {
        "label": f"Equal weight all {len(etfs_used)} ETFs",
        "dates": [d.strftime("%Y-%m-%d") for d in eq_window.index],
        "equity": round_series(eq_window.values),
        **{k: _safe(v) if isinstance(v, float) else v for k, v in st.items()},
    }
    print(f"  equal_weight_all          Shp {st['sharpe']:+.2f}  totRet {st['total_return']*100:+.0f}%  DD {st['max_dd']*100:.0f}%")

    # Top-K variants
    for K in TOP_K_VALUES:
        for weight_name, weight_fn in [
            (f"top{K}_eq_weight", top_k_eq_weight(K)),
            (f"top{K}_rank_weight", top_k_rank_weight(K)),
            (f"top{K}_breadth_weight", top_k_breadth_weight(K)),
        ]:
            r = run_portfolio(closes, breadths, weight_fn, eligible)
            eq_window = r["equity"].loc[r["equity"].index >= eligible]
            eq_window = eq_window / eq_window.iloc[0]
            st = compute_stats(r["equity"], eligible)
            results[weight_name] = {
                "label": f"Top {K}, {weight_name.split('_', 1)[1]}",
                "dates": [d.strftime("%Y-%m-%d") for d in eq_window.index],
                "equity": round_series(eq_window.values),
                **{k: _safe(v) if isinstance(v, float) else v for k, v in st.items()},
            }
            print(f"  {weight_name:<25} Shp {st['sharpe']:+.2f}  totRet {st['total_return']*100:+.0f}%  DD {st['max_dd']*100:.0f}%")

            # Leveraged variant
            leveraged_eq = apply_leverage_overlay(r["equity"], r["weights"], breadths)
            lev_window = leveraged_eq.loc[leveraged_eq.index >= eligible]
            lev_window = lev_window / lev_window.iloc[0]
            st_l = compute_stats(leveraged_eq, eligible)
            results[weight_name + "_leveraged"] = {
                "label": f"Top {K} (leveraged 50/150 on basket breadth > {int(LEVERAGE_THRESHOLD*100)}%), "
                          f"{weight_name.split('_', 1)[1]}",
                "dates": [d.strftime("%Y-%m-%d") for d in lev_window.index],
                "equity": round_series(lev_window.values),
                **{k: _safe(v) if isinstance(v, float) else v for k, v in st_l.items()},
            }
            print(f"  {weight_name + '_leveraged':<25} Shp {st_l['sharpe']:+.2f}  totRet {st_l['total_return']*100:+.0f}%  DD {st_l['max_dd']*100:.0f}%")

    # Eq-weight ensemble of 11 single-ETF MA200 50/150 strategies — fair
    # baseline for "MA200 signal everywhere, no relative-strength tilt"
    print()
    ma200_path = DATA_DIR / "ma200_sweep.json"
    if ma200_path.exists():
        ma200 = json.loads(ma200_path.read_text(encoding="utf-8"))
        etf_thresholds: dict[str, float] = {}
        for etf, info in ma200.get("monitor", {}).items():
            L = info.get("long_threshold_pct")
            if L is not None and etf in closes.columns:
                etf_thresholds[etf] = float(L)
        if etf_thresholds:
            r = family_d_eq_weight_ensemble(closes, breadths, etf_thresholds,
                                              base=0.5, on=1.5,
                                              window_start=eligible)
            eq_window = r["equity"].loc[r["equity"].index >= eligible]
            eq_window = eq_window / eq_window.iloc[0]
            st = compute_stats(r["equity"], eligible)
            results["eq_weight_11_family_d"] = {
                "label": "Eq-weight ensemble of 11 single-ETF MA200 50/150 strategies",
                "dates": [d.strftime("%Y-%m-%d") for d in eq_window.index],
                "equity": round_series(eq_window.values),
                **{k: _safe(v) if isinstance(v, float) else v for k, v in st.items()},
            }
            print(f"  eq_weight_11_family_d     Shp {st['sharpe']:+.2f}  "
                  f"totRet {st['total_return']*100:+.0f}%  DD {st['max_dd']*100:.0f}%")

    # SPY benchmark
    spy_window = spy_close.loc[spy_close.index >= eligible]
    spy_eq = (spy_window / spy_window.iloc[0])
    spy_stats = compute_stats(spy_close, eligible)
    results["spy_buy_hold"] = {
        "label": "SPY buy-and-hold",
        "dates": [d.strftime("%Y-%m-%d") for d in spy_eq.index],
        "equity": round_series(spy_eq.values),
        **{k: _safe(v) if isinstance(v, float) else v for k, v in spy_stats.items()},
    }
    print(f"  spy_buy_hold              Shp {spy_stats['sharpe']:+.2f}  totRet {spy_stats['total_return']*100:+.0f}%  DD {spy_stats['max_dd']*100:.0f}%")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "etfs_used": etfs_used,
        "eligible_start": eligible.strftime("%Y-%m-%d"),
        "leverage_threshold_pct": int(LEVERAGE_THRESHOLD * 100),
        "rebalance_freq": "W-FRI",
        "top_k_values": TOP_K_VALUES,
        "results": results,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Print headline winner
    print()
    print("=" * 90)
    print("HEADLINE — best Sharpe across all portfolio variants")
    print("=" * 90)
    best = max(results.items(), key=lambda kv: kv[1].get("sharpe") or -1e9)
    print(f"  Winner: {best[0]}")
    print(f"    Sharpe: {best[1]['sharpe']:+.2f}")
    print(f"    Total return: {best[1]['total_return']*100:+.1f}%")
    print(f"    Max DD: {best[1]['max_dd']*100:.1f}%")
    spy = results["spy_buy_hold"]
    print(f"\n  vs SPY buy-and-hold: Sharpe {spy['sharpe']:+.2f}, totRet {spy['total_return']*100:+.1f}%, DD {spy['max_dd']*100:.1f}%")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
