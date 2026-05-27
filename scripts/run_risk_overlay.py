"""Phase 19 — Aggregate market-breadth regime overlay on the deployed blend.

This is the dedicated home for "overlays that modulate the deployed blend
based on a market signal". Currently houses Idea 1 from the breadth-
application survey (the CSP1 breadth gate). Future overlays (e.g., Idea
5 — breadth-divergence) would live alongside it here without bloating
``run_multi_strategy.py`` which is about blend construction not overlays.

Architecture:
  * Reads ``data/multi_strategy.json`` to get the underlying blend's
    daily equity curve.
  * Reads ``data/breadth_csp1.json`` for the S&P 500 constituent-breadth
    time series (CSP1 holdings, computed by scripts/compute_breadth.py).
  * Reads ``data/asset_class_prices_cache.parquet`` to source the IEF
    fallback (7-10y US Treasury — what the live strategy actually
    rotates into when the gate fires).
  * Emits ``data/risk_overlay.json`` with the gated equity curve,
    diagnostics (current state, last switch date, current breadth, etc),
    and full event log.

Output schema:
  {
    "computed_at_utc": "...",
    "underlying_blend_key": "blend_35_35_10_20",
    "gate_parameters": { off_threshold, on_threshold, derisk_fraction,
                          switch_cost_bps, fallback_ticker },
    "current_state": "RISK_ON" | "RISK_OFF",
    "current_state_since": "YYYY-MM-DD",
    "current_breadth": 0.4502,
    "n_switches": 16,
    "days_risk_off": 212,
    "pct_days_risk_off": 11.41,
    "events": [ {date, direction, breadth}, ... ],
    "ungated_reference": { sharpe, cagr, max_dd, total_return },
    "gated_variants": {
      "blend_35_35_10_20_gated": {
        label, dates, equity, sharpe, cagr, max_dd, total_return
      }
    }
  }

The dashboard's pipeline.py merges ``gated_variants`` into
``multi.strategies`` at injection time, and ``gate_parameters`` +
``current_state`` etc into ``multi.regime_gate`` for backward
compatibility with the existing dashboard render code. So the
template.html does not need to change — only the data flow upstream.

Usage:
    # After multi_strategy.json has been built
    python scripts/run_risk_overlay.py

Returns nonzero exit if upstream data is missing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "risk_overlay.json"

# ----------------------------------------------------------------------
# Gate parameters — chosen via the 12-variant sweep in
# scripts/run_regime_gate.py (Phase 19, variant #4). Pareto-improving on
# the ungated 4-way blend on both Sharpe AND max drawdown.
# ----------------------------------------------------------------------
UNDERLYING_BLEND_KEY = "blend_35_35_10_20"
OFF_THRESHOLD = 0.20    # de-risk when S&P 500 breadth falls below 20%
ON_THRESHOLD = 0.50     # re-engage when breadth crosses back above 50%
DERISK_FRACTION = 0.50  # 50% partial de-risk (not full move to cash)
SWITCH_COST_BPS = 5     # bps charged per regime flip
FALLBACK_TICKER = "IEF" # 7-10y Treasury — actual deployed execution


def _round(x, n=4):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), n)


def _round_series(arr, n=6):
    return [_round(v, n) for v in arr]


def _stats(daily_ret: pd.Series, eq: pd.Series) -> dict:
    """Match run_multi_strategy.compute_stats exactly so the displayed
    Sharpe / CAGR / DD on the gated variant uses the same methodology
    as every other entry in multi_strategy.json. Otherwise the gated
    variant would show a different Sharpe (mean(daily)*252 vs
    (1+mean)^252-1 etc) and reviewers would be unable to compare them
    apples-to-apples."""
    import math
    if len(eq) < 2:
        return {"sharpe": None, "cagr": None,
                 "total_return": None, "max_dd": None}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    total_ret = float(eq.iloc[-1] - 1.0)
    cagr = (float(eq.iloc[-1]) ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    return {"sharpe": _round(sharpe), "cagr": _round(cagr),
             "total_return": _round(total_ret), "max_dd": _round(float(dd.min()))}


def _compute_states(breadth: pd.Series, off: float, on: float) -> pd.Series:
    """Walk-forward regime detection with hysteresis."""
    states = []
    state = 1.0  # start RISK_ON
    for v in breadth.values:
        if pd.isna(v):
            states.append(state); continue
        if state == 1.0 and v < off:
            state = 0.0
        elif state == 0.0 and v > on:
            state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index, dtype=float)


def main() -> int:
    # ----- Load upstream data -----
    multi_path = DATA_DIR / "multi_strategy.json"
    csp1_path = DATA_DIR / "breadth_csp1.json"
    ief_cache_path = DATA_DIR / "asset_class_prices_cache.parquet"
    for required in (multi_path, csp1_path, ief_cache_path):
        if not required.exists():
            print(f"ERROR: required upstream missing: "
                  f"{required.relative_to(ROOT)}", file=sys.stderr)
            print(f"  Run the upstream pipeline first "
                  f"(run_multi_strategy.py / compute_breadth.py / "
                  f"run_asset_class_rotation.py).", file=sys.stderr)
            return 1

    multi = json.loads(multi_path.read_text(encoding="utf-8"))
    blend = multi.get("strategies", {}).get(UNDERLYING_BLEND_KEY)
    if not blend or "dates" not in blend or "equity" not in blend:
        print(f"ERROR: {UNDERLYING_BLEND_KEY} not present in "
              f"multi_strategy.json — run_multi_strategy.py needs to "
              f"emit it first.", file=sys.stderr)
        return 1
    blend_eq = pd.Series(blend["equity"],
                          index=pd.to_datetime(blend["dates"]),
                          name="blend")

    csp1 = json.loads(csp1_path.read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"]),
                         name="breadth").dropna()

    ief = pd.read_parquet(ief_cache_path)[FALLBACK_TICKER].dropna()

    # ----- Align on the blend's calendar -----
    common = blend_eq.index
    breadth = breadth.reindex(common, method="ffill")
    ief_aligned = ief.reindex(common, method="ffill")
    blend_ret = blend_eq.pct_change().fillna(0)
    fallback_ret = ief_aligned.pct_change().fillna(0)

    # ----- Compute gated equity -----
    states = _compute_states(breadth, OFF_THRESHOLD, ON_THRESHOLD)
    states_lagged = states.shift(1).fillna(1.0)
    state_changes = states_lagged.diff().fillna(0).abs()
    switch_cost = state_changes * (SWITCH_COST_BPS / 10_000.0)
    blend_w = states_lagged + (1.0 - states_lagged) * (1.0 - DERISK_FRACTION)
    fallback_w = (1.0 - states_lagged) * DERISK_FRACTION
    gated_ret = blend_w * blend_ret + fallback_w * fallback_ret - switch_cost
    gated_eq = (1.0 + gated_ret).cumprod()

    # ----- Diagnostics -----
    n_switches = int(state_changes.sum())
    days_off = int((states_lagged == 0).sum())
    pct_off = days_off / len(states_lagged) * 100
    transitions = states.diff().fillna(0)
    last_change_date = (states.index[transitions != 0][-1]
                         if (transitions != 0).any() else states.index[0])
    current_state = "RISK_ON" if states.iloc[-1] == 1.0 else "RISK_OFF"
    events = [
        {"date": d.strftime("%Y-%m-%d"),
         "direction": "RISK_OFF" if states.loc[d] == 0.0 else "RISK_ON",
         "breadth": _round(breadth.loc[d])}
        for d in states.index[transitions != 0]
    ]

    ungated_stats = _stats(blend_ret, blend_eq)
    gated_stats = _stats(gated_ret, gated_eq)

    label = (f"DEPLOYED · {UNDERLYING_BLEND_KEY} with CSP1 breadth gate "
              f"(Phase 19: off={int(OFF_THRESHOLD*100)}%, "
              f"on={int(ON_THRESHOLD*100)}%, "
              f"derisk={int(DERISK_FRACTION*100)}%)")
    gated_key = f"{UNDERLYING_BLEND_KEY}_gated"

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "underlying_blend_key": UNDERLYING_BLEND_KEY,
        "gate_parameters": {
            "off_threshold": OFF_THRESHOLD,
            "on_threshold": ON_THRESHOLD,
            "derisk_fraction": DERISK_FRACTION,
            "switch_cost_bps": SWITCH_COST_BPS,
            "fallback_ticker": FALLBACK_TICKER,
        },
        "current_state": current_state,
        "current_state_since": last_change_date.strftime("%Y-%m-%d"),
        "current_breadth": _round(breadth.iloc[-1]),
        "n_switches": n_switches,
        "days_risk_off": days_off,
        "pct_days_risk_off": _round(pct_off, 2),
        "events": events,
        "ungated_reference": {
            "key": UNDERLYING_BLEND_KEY, **ungated_stats,
        },
        "gated_variants": {
            gated_key: {
                "label": label,
                "dates": [d.strftime("%Y-%m-%d") for d in gated_eq.index],
                "equity": _round_series(gated_eq.values),
                **gated_stats,
            },
        },
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")),
                         encoding="utf-8")

    # Console summary
    delta_sh = (gated_stats["sharpe"] or 0) - (ungated_stats["sharpe"] or 0)
    delta_dd = (gated_stats["max_dd"] or 0) - (ungated_stats["max_dd"] or 0)
    print(f"Phase 19 risk overlay — built {OUT_PATH.relative_to(ROOT)}")
    print(f"  Underlying blend ({UNDERLYING_BLEND_KEY}):")
    print(f"    Sharpe {ungated_stats['sharpe']:+.4f}  "
          f"CAGR {ungated_stats['cagr']*100:+.1f}%  "
          f"DD {ungated_stats['max_dd']*100:+.2f}%")
    print(f"  Gated variant ({gated_key}):")
    print(f"    Sharpe {gated_stats['sharpe']:+.4f}  "
          f"CAGR {gated_stats['cagr']*100:+.1f}%  "
          f"DD {gated_stats['max_dd']*100:+.2f}%")
    print(f"    Delta vs ungated: Sharpe {delta_sh:+.4f}  "
          f"DD {delta_dd*100:+.2f}pp")
    print(f"  Current state: {current_state} since "
          f"{last_change_date.strftime('%Y-%m-%d')}  "
          f"(S&P 500 breadth {breadth.iloc[-1]*100:.1f}%)")
    print(f"  History: {n_switches} switches, "
          f"{pct_off:.1f}% of days RISK_OFF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
