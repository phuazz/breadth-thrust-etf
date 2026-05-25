"""Phase 2 of the strategy-improvement experiments — covers four follow-on
ideas raised after the original Test 1 / 2 / 3 batch:

  ITEM 1+4 — base × thrust GRID (combined since they are the same parameter)
    Sweep the base allocation (0 / 25 / 50 / 75 / 100 per cent) crossed
    with the thrust allocation (100 / 150 / 200 per cent). Maps the
    Pareto curve of "time in market" vs alpha per ETF. The 50/100 cell
    is the original Test 2 winner; this grid asks whether more leverage
    on the signal, or a different base, improves it.

  ITEM 2 — CONTINUOUS signal score
    Replace the binary "in trade / not" thrust state with a continuous
    score derived from where the composite z-score sits in its expanding
    [p10, p90] band. Allocation = base + (1 - base) * signal_strength,
    where signal_strength is clipped to [0, 1]. Smoother turnover, no
    discrete on/off transitions.

  ITEM 3 — MASTER REGIME FILTER from CSP1 (S&P 500) breadth
    Use the broad-market breadth panel as a "system off" mask. Whenever
    CSP1's composite_z < its expanding p10 OR ma_breadth < 0.40, force
    every sector allocation to zero regardless of the per-ETF signal.
    Applied as an overlay on the 50/100 Test 2 strategy.

Each test is reported per ETF on the 2019-2026 signal-eligible window
with Sharpe, CAGR, max DD, total return, and time-in-market.

Output: data/tuning.json (grid stats + per-ETF equity curves for the
continuous and master-filter variants).

Run:
    python scripts/run_tuning.py
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
from backtest import download_soxx_ohlc, download_spy_close  # noqa: E402
from etf_registry import get_etf  # noqa: E402
from run_improvements import (  # noqa: E402
    COST_BPS, ETFS, compute_stats, round_series, size_scaled_thrust,
    time_in_market, load_triple_trades,
)

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "tuning.json"


# (base, thrust) in PERCENTAGES — converted to fractions internally.
BASE_THRUST_GRID = [
    # thrust = 100 (no leverage on signal)
    (0, 100), (25, 100), (50, 100), (75, 100), (100, 100),
    # thrust = 150 (1.5x on signal)
    (0, 150), (50, 150), (100, 150),
    # thrust = 200 (2x on signal)
    (0, 200), (50, 200), (100, 200),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_breadth_full(etf: str) -> pd.DataFrame:
    """Read breadth_<etf>.json directly — we need composite_p90 in addition
    to the fields backtest.load_breadth pulls."""
    path = DATA_DIR / f"breadth_{etf.lower()}.json"
    blob = json.loads(path.read_text(encoding="utf-8"))
    ser = blob["series"]
    return pd.DataFrame({
        "composite_z": ser["composite_z"],
        "composite_p10": ser["composite_p10"],
        "composite_p90": ser["composite_p90"],
        "ma_breadth": ser["ma_breadth"],
    }, index=pd.to_datetime(ser["dates"]))


def load_close(yf_sym: str, start: str, end: str) -> pd.Series:
    ohlc = download_soxx_ohlc(start, end, etf=yf_sym, yf_symbol=yf_sym)
    ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
    return ohlc["Close"].astype(float)


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


# ---------------------------------------------------------------------------
# Item 2 — continuous signal allocation
# ---------------------------------------------------------------------------


def continuous_signal(
    breadth_df: pd.DataFrame,
    prices_close: pd.Series,
    base: float = 0.5,
    cost: float = COST_BPS / 10_000,
    window_start: pd.Timestamp | None = None,
) -> dict:
    """alloc = base + (1 - base) * clip((z - p10) / (p90 - p10), 0, 1).

    signal_strength is 0 when composite_z is at or below its expanding p10,
    1 when at or above expanding p90, linear in between. Allocations are
    bounded in [base, 1.0] — no leverage in this variant.
    """
    aligned = align_frame_to_index(breadth_df, prices_close.index)
    span = (aligned["composite_p90"] - aligned["composite_p10"]).replace(0, np.nan)
    score = ((aligned["composite_z"] - aligned["composite_p10"]) / span).clip(0, 1).fillna(0)
    alloc = base + (1.0 - base) * score
    if window_start is not None:
        alloc.loc[alloc.index < window_start] = 0.0
    daily_ret = prices_close.pct_change().fillna(0)
    strat_ret = alloc.shift(1).fillna(0.0) * daily_ret
    alloc_change = alloc.diff().fillna(0).abs()
    strat_ret = strat_ret - alloc_change * cost
    equity = (1.0 + strat_ret).cumprod()
    return {"equity": equity, "alloc": alloc, "score": score}


# ---------------------------------------------------------------------------
# Item 3 — master regime filter (CSP1 breadth as system-off mask)
# ---------------------------------------------------------------------------


def master_regime_mask(csp1_breadth: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    """True when SPY breadth regime is healthy (composite_z >= p10 AND ma_breadth >= 0.40).
    Forward-fills onto the requested index; missing -> False."""
    aligned = align_frame_to_index(csp1_breadth, index)
    ok = (
        (aligned["composite_z"].fillna(-1e9) >= aligned["composite_p10"].fillna(1e9))
        & (aligned["ma_breadth"] >= 0.40)
        & aligned["composite_z"].notna()
        & aligned["composite_p10"].notna()
    )
    return ok


def apply_overlay(
    base_alloc: pd.Series,
    overlay_mask: pd.Series,
    prices_close: pd.Series,
    cost: float = COST_BPS / 10_000,
) -> dict:
    """Multiply a strategy's allocation by a binary overlay mask, then
    recompute returns and turnover costs from scratch."""
    aligned_mask = overlay_mask.reindex(base_alloc.index).fillna(False).astype(float)
    final_alloc = base_alloc * aligned_mask
    daily_ret = prices_close.pct_change().fillna(0)
    strat_ret = final_alloc.shift(1).fillna(0.0) * daily_ret
    alloc_change = final_alloc.diff().fillna(0).abs()
    strat_ret = strat_ret - alloc_change * cost
    equity = (1.0 + strat_ret).cumprod()
    return {"equity": equity, "alloc": final_alloc}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("Loading breadth panels (with p90) + prices ...", flush=True)
    breadths: dict[str, pd.DataFrame] = {}
    closes: dict[str, pd.Series] = {}
    triple_trades: dict[str, list[dict]] = {}
    eligible_starts: dict[str, pd.Timestamp] = {}
    for etf in ETFS:
        cfg = get_etf(etf)
        proxy = cfg.get("yfinance_trading_proxy") or etf
        b = load_breadth_full(etf)
        eligible_start = b.index[252] if len(b) > 252 else b.index[0]
        eligible_starts[etf] = eligible_start
        dl_start = (b.index[0] - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end = (b.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        closes[etf] = load_close(proxy, dl_start, dl_end)
        breadths[etf] = b
        triple_trades[etf] = load_triple_trades(etf)
        print(f"  {etf:5} -> {proxy:4}  breadth {len(b)} rows  "
              f"trades {len(triple_trades[etf])}  eligible from {eligible_start.date()}")
    csp1_breadth = breadths["CSP1"]

    # ---------- Item 1 + 4: base x thrust grid ----------------------------
    print("\n=== Items 1+4: base x thrust GRID per ETF ===", flush=True)
    grid_by_etf: dict[str, list[dict]] = {}
    for etf in ETFS:
        grid_by_etf[etf] = []
        for base_pct, thrust_pct in BASE_THRUST_GRID:
            base = base_pct / 100.0
            on = thrust_pct / 100.0
            r = size_scaled_thrust(
                triple_trades[etf], closes[etf],
                base=base, on=on,
                window_start=eligible_starts[etf],
            )
            st = compute_stats(r["equity"], eligible_starts[etf])
            st["time_in_market"] = time_in_market(
                (r["alloc"] > 0).astype(float),
                eligible_starts[etf], r["equity"].index[-1],
            )
            grid_by_etf[etf].append({
                "base_pct": base_pct, "thrust_pct": thrust_pct,
                "total_return": _safe(st["total_return"]),
                "cagr": _safe(st["cagr"]),
                "sharpe": _safe(st["sharpe"]),
                "max_dd": _safe(st["max_dd"]),
                "time_in_market": _safe(st["time_in_market"]),
            })
        best = max(grid_by_etf[etf], key=lambda r: r["sharpe"] or -1e9)
        print(f"  {etf:5} best:  base={best['base_pct']:>3}/thrust={best['thrust_pct']:>3}  "
              f"Sharpe {best['sharpe']:+.2f}  totRet {(best['total_return'] or 0)*100:+.1f}%  "
              f"MaxDD {(best['max_dd'] or 0)*100:.1f}%")

    # ---------- Item 2: continuous signal allocation ---------------------
    print("\n=== Item 2: continuous signal (base + score*(1-base)) ===", flush=True)
    cont_by_etf: dict[str, dict] = {}
    for etf in ETFS:
        r = continuous_signal(
            breadths[etf], closes[etf],
            base=0.5,
            window_start=eligible_starts[etf],
        )
        st = compute_stats(r["equity"], eligible_starts[etf])
        st["time_in_market"] = time_in_market(
            (r["alloc"] > 0).astype(float),
            eligible_starts[etf], r["equity"].index[-1],
        )
        st["mean_alloc_when_in"] = float(
            r["alloc"].loc[
                (r["alloc"].index >= eligible_starts[etf]) & (r["alloc"] > 0)
            ].mean()
        )
        win = (r["equity"].index >= eligible_starts[etf])
        eq_w = r["equity"].loc[win]
        eq_w = eq_w / eq_w.iloc[0]
        cont_by_etf[etf] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq_w.index],
            "equity": round_series(eq_w.values),
            **{k: _safe(v) for k, v in st.items()},
        }
        print(f"  {etf:5}  totRet {st['total_return']*100:+7.1f}%  CAGR {st['cagr']*100:+6.1f}%  "
              f"Sharpe {st['sharpe']:+.2f}  MaxDD {st['max_dd']*100:>5.1f}%  "
              f"MeanAllocWhenIn {st['mean_alloc_when_in']*100:>5.1f}%")

    # ---------- Item 3: master regime filter from CSP1 -------------------
    print("\n=== Item 3: master CSP1-breadth regime filter applied to 50/100 ===", flush=True)
    master_by_etf: dict[str, dict] = {}
    csp1_mask_cache: dict | None = None
    for etf in ETFS:
        # Start from the 50/100 Test 2 result on this ETF, then overlay
        # the CSP1 regime mask.
        base_r = size_scaled_thrust(
            triple_trades[etf], closes[etf], base=0.5, on=1.0,
            window_start=eligible_starts[etf],
        )
        mask = master_regime_mask(csp1_breadth, base_r["alloc"].index)
        overlaid = apply_overlay(base_r["alloc"], mask, closes[etf])
        st_overlaid = compute_stats(overlaid["equity"], eligible_starts[etf])
        st_overlaid["time_in_market"] = time_in_market(
            (overlaid["alloc"] > 0).astype(float),
            eligible_starts[etf], overlaid["equity"].index[-1],
        )
        st_base = compute_stats(base_r["equity"], eligible_starts[etf])
        win = (overlaid["equity"].index >= eligible_starts[etf])
        eq_w = overlaid["equity"].loc[win]
        eq_w = eq_w / eq_w.iloc[0]
        master_by_etf[etf] = {
            "dates": [d.strftime("%Y-%m-%d") for d in eq_w.index],
            "equity": round_series(eq_w.values),
            "with_master_filter": {k: _safe(v) for k, v in st_overlaid.items()},
            "without_master_filter": {k: _safe(v) for k, v in st_base.items()},
        }
        delta_sh = st_overlaid["sharpe"] - st_base["sharpe"]
        delta_dd = st_overlaid["max_dd"] - st_base["max_dd"]
        print(f"  {etf:5}  Sharpe {st_base['sharpe']:+.2f} -> {st_overlaid['sharpe']:+.2f} "
              f"(d={delta_sh:+.2f})  "
              f"MaxDD {st_base['max_dd']*100:.1f}% -> {st_overlaid['max_dd']*100:.1f}% "
              f"(d={delta_dd*100:+.1f}pp)  "
              f"TimeInMkt {st_overlaid['time_in_market']*100:.1f}%")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_thrust_grid": grid_by_etf,
        "continuous_signal_base50": cont_by_etf,
        "master_csp1_overlay_on_50_100": master_by_etf,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ---------- Final comparison table per ETF ----------------------------
    print()
    print("=" * 110)
    print("PHASE 2 HEADLINES per ETF — best Sharpe in each category")
    print("=" * 110)
    print(f"{'ETF':<5} {'Grid best':<22} {'Cont. signal':<22} "
          f"{'50/100 + master filter':<28}")
    print("-" * 110)
    for etf in ETFS:
        best_grid = max(grid_by_etf[etf], key=lambda r: r["sharpe"] or -1e9)
        cont = cont_by_etf[etf]
        master = master_by_etf[etf]["with_master_filter"]
        g = (f"b={best_grid['base_pct']}/t={best_grid['thrust_pct']}: "
             f"Shp {best_grid['sharpe']:+.2f}")
        c = f"Shp {cont['sharpe']:+.2f}  DD {cont['max_dd']*100:.0f}%"
        m = (f"Shp {master['sharpe']:+.2f}  DD {master['max_dd']*100:.0f}%  "
             f"TIM {master['time_in_market']*100:.0f}%")
        print(f"{etf:<5} {g:<22} {c:<22} {m:<28}")
    print()
    print(f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
