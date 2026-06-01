"""Strategy C V6 — robustness checks against user's concerns.

Two legitimate concerns about V6 (sleeve-breadth gate at 30%):

  Concern A — REGIME OVERFITTING.
      V6's biggest in-sample win is during the 2021-Feb to 2022-Dec
      thematic-ETF blow-up. If we remove that episode, is V6 still a
      win, or is the +12pp DD reduction concentrated in one regime?
      Cannot test on pre-2018 data because the thematic universe
      did not exist with enough constituents. But we CAN check
      whether V6's contribution is uniform across the five distinct
      drawdown episodes in the backtest window.

  Concern B — TOP-DOWN vs BOTTOM-UP.
      V6 exits the ENTIRE sleeve when <30% of universe is above +5%.
      Concern: during a sleeve-wide rollover, some individual ETFs
      might still be in genuine uptrends. V6 dumps them all and
      misses recoveries that per-ETF rules would have kept.
      Quantification: at the moment V6 dumped the sleeve in early
      2022, what happened to the ETFs it held? Did any of them
      rally while V6 sat in cash?

This script runs both analyses:

  Phase A: episode-by-episode return attribution. Five hand-picked
  drawdown windows; compute baseline vs V6 return in each. If V6's
  win is uniform across episodes, it is regime-robust. If
  concentrated in one episode, it is regime-overfit.

  Phase B: case study of the V6 exit events during 2021-22. List
  the ETFs V6 was holding at the moment of exit; track each ETF's
  subsequent return until V6 re-entered (or until end of period).
  If most ETFs declined during the V6-cash period, V6's exit was
  correct. If many ETFs rallied during that period, V6 cost
  return without benefit.

Output: data/thematic_exit_robustness.json + printed analysis.
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
    UNIVERSE, TICKERS, CASH_PROXY, SIGNAL_FLOOR, COST_FRAC,
    download_prices, compute_signal, _safe,
)
from run_thematic_exit_bakeoff import (  # noqa: E402
    HEADLINE_K, _initial_state, _eligible_baseline,
    _eligible_v6_sleeve_breadth, compute_ema, compute_signal_slope,
    compute_rsi, compute_realised_vol,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


# Five distinct drawdown episodes in the backtest window. Each is a
# (peak, trough) date range chosen from inspection of the deployed
# blend's drawdown curve. Episodes are intentionally generous on the
# trough side to capture the full recovery question.
EPISODES = [
    ("2018-Q4 Powell pivot",          "2018-10-01", "2019-01-31"),
    ("2020-Q1 COVID crash",           "2020-02-15", "2020-05-31"),
    ("2021-2022 thematic blow-up",    "2021-02-15", "2022-12-31"),
    ("2022 rate-hike rotation",       "2022-04-01", "2022-12-31"),
    ("2025 mid-year correction",      "2025-08-01", "2025-12-31"),
]


def _run_rotation_full(closes, signal, K, eligible_start, eligible_fn,
                        features) -> dict:
    """Standalone copy of the variant runner from the bake-off so we can
    capture per-rebal weights + sleeve breadth without circular imports."""
    rebalance_target = pd.date_range(eligible_start, closes.index[-1],
                                      freq="W-FRI")
    rebalance_dates = closes.index[closes.index.isin(rebalance_target)]
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)
    state = _initial_state(list(closes.columns))
    # Capture sleeve breadth + holding set at each rebal for the case
    # study (Phase B).
    rebal_log = []

    for rd in rebalance_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0:
            continue
        s_row = signal.iloc[prev_idx]
        prev_close = closes.iloc[prev_idx]

        # Update state.peak_price for held ETFs
        for t in closes.columns:
            px = prev_close.get(t)
            if px is not None and px == px and state[t]["held"]:
                pk = state[t]["peak_price"]
                state[t]["peak_price"] = max(pk, px) if pk is not None else px

        # Sleeve breadth: % of universe (excluding cash) above +5%
        univ = s_row.drop(CASH_PROXY, errors="ignore").dropna()
        n_universe = len(univ)
        n_above = (univ > SIGNAL_FLOOR).sum() if n_universe else 0
        sleeve_breadth = (n_above / n_universe) if n_universe else 0.0

        eligible = eligible_fn(s_row, prev_close, prev_idx, state, features)

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

        new_held = set(w[w > 1e-6].index) - {CASH_PROXY}
        old_held = {t for t, st in state.items() if st["held"]}
        for t in new_held - old_held:
            px = prev_close.get(t)
            state[t]["held"] = True
            state[t]["peak_price"] = float(px) if px is not None and px == px else None
            state[t]["was_overbought"] = False
        for t in old_held - new_held:
            state[t]["held"] = False
            state[t]["peak_price"] = None
            state[t]["was_overbought"] = False

        rebal_log.append({
            "date": rd,
            "sleeve_breadth": sleeve_breadth,
            "held_etfs": sorted(new_held),
            "weight_in_cash": float(w.get(CASH_PROXY, 0.0)),
        })
        rb_weights.loc[rd] = w

    weight_panel = rb_weights.reindex(closes.index).ffill().fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel,
             "rebal_log": rebal_log}


# ---------------------------------------------------------------------------
# Phase A — per-episode attribution
# ---------------------------------------------------------------------------

def episode_return(equity: pd.Series, start: str, end: str) -> float | None:
    eq = equity.loc[start:end]
    if len(eq) < 2:
        return None
    return float(eq.iloc[-1] / eq.iloc[0] - 1)


def episode_max_dd(equity: pd.Series, start: str, end: str) -> float | None:
    eq = equity.loc[start:end]
    if len(eq) < 2:
        return None
    eq = eq / eq.iloc[0]
    pk = eq.cummax()
    dd = (eq - pk) / pk
    return float(dd.min())


def phase_a_episode_attribution(eq_baseline, eq_v6):
    print()
    print("=" * 78)
    print("PHASE A — per-episode attribution (concern A: regime overfitting)")
    print("=" * 78)
    print(f"{'Episode':<32} {'Baseline':>11} {'V6 30%':>11} "
          f"{'Δ return':>10} {'Δ MaxDD':>10}")
    print("-" * 78)
    rows = []
    for label, s, e in EPISODES:
        rb = episode_return(eq_baseline, s, e)
        rv = episode_return(eq_v6, s, e)
        dd_b = episode_max_dd(eq_baseline, s, e)
        dd_v = episode_max_dd(eq_v6, s, e)
        if rb is None or rv is None:
            print(f"  {label:<30} {'—':>11} {'—':>11} {'—':>10} {'—':>10}")
            continue
        d_ret = rv - rb
        d_dd = (dd_v or 0) - (dd_b or 0)
        print(f"  {label:<30} {rb*100:>+10.1f}% {rv*100:>+10.1f}% "
              f"{d_ret*100:>+9.1f}pp {d_dd*100:>+9.1f}pp")
        rows.append({
            "label": label,
            "start": s, "end": e,
            "baseline_return": rb,
            "v6_return": rv,
            "delta_return_pp": d_ret * 100,
            "baseline_max_dd": dd_b,
            "v6_max_dd": dd_v,
            "delta_max_dd_pp": d_dd * 100 if dd_b is not None and dd_v is not None else None,
        })
    return rows


# ---------------------------------------------------------------------------
# Phase B — case study of V6 exit events during 2021-22
# ---------------------------------------------------------------------------


def phase_b_case_study(closes, rebal_log_v6):
    """For each transition where V6 went from invested → all-cash during
    the 2021-22 episode, list the ETFs V6 was holding pre-exit and show
    each one's forward return until V6 re-entered (or end of episode)."""
    print()
    print("=" * 78)
    print("PHASE B — V6 exit case study (concern B: top-down vs bottom-up)")
    print("=" * 78)
    print("During the 2021-2022 episode, every time V6 switched from")
    print("invested -> all-cash, what happened to the ETFs it had been")
    print("holding? Forward return computed from V6-exit date to V6-")
    print("re-entry date (or end of episode if no re-entry).")
    print()

    ep_start = pd.Timestamp("2021-02-15")
    ep_end = pd.Timestamp("2022-12-31")
    relevant_log = [r for r in rebal_log_v6 if ep_start <= r["date"] <= ep_end]

    # Detect exit events: transition from invested (cash < 0.5) to
    # all-cash (cash > 0.99). Track the cash-period and what happens
    # to each previously-held ETF in that window.
    exit_events = []
    in_cash = False
    cash_started = None
    pre_exit_holdings = []
    for entry in relevant_log:
        was_invested = entry["weight_in_cash"] < 0.5
        now_cash = entry["weight_in_cash"] > 0.99
        if not in_cash and now_cash:
            # Just exited
            cash_started = entry["date"]
            in_cash = True
            # pre_exit_holdings was set on the PRIOR entry (we tracked
            # the previous invested entry's holdings below)
        elif in_cash and was_invested:
            # Re-entered
            in_cash = False
            cash_ended = entry["date"]
            # Compute forward return for each pre-exit holding
            forwards = []
            for etf in pre_exit_holdings:
                if etf not in closes.columns:
                    continue
                px_exit = closes.loc[cash_started, etf] if cash_started in closes.index else None
                px_reenter = closes.loc[cash_ended, etf] if cash_ended in closes.index else None
                if px_exit is None or px_reenter is None: continue
                if px_exit != px_exit or px_reenter != px_reenter: continue
                fwd = float(px_reenter / px_exit - 1)
                forwards.append({"etf": etf, "fwd_return": fwd})
            exit_events.append({
                "exit_date": cash_started.strftime("%Y-%m-%d"),
                "reentry_date": cash_ended.strftime("%Y-%m-%d"),
                "days_in_cash": (cash_ended - cash_started).days,
                "pre_exit_holdings": pre_exit_holdings,
                "forwards": forwards,
            })
        if was_invested:
            pre_exit_holdings = entry["held_etfs"]
    # Handle case where V6 was still in cash at episode end
    if in_cash and cash_started is not None:
        # Use episode end as the cash_ended timestamp
        cash_ended = ep_end
        # Find nearest trading day
        if cash_ended not in closes.index:
            cash_ended = closes.index[closes.index.searchsorted(cash_ended) - 1]
        forwards = []
        for etf in pre_exit_holdings:
            if etf not in closes.columns: continue
            px_exit = closes.loc[cash_started, etf] if cash_started in closes.index else None
            px_end = closes.loc[cash_ended, etf] if cash_ended in closes.index else None
            if px_exit is None or px_end is None: continue
            if px_exit != px_exit or px_end != px_end: continue
            fwd = float(px_end / px_exit - 1)
            forwards.append({"etf": etf, "fwd_return": fwd})
        exit_events.append({
            "exit_date": cash_started.strftime("%Y-%m-%d"),
            "reentry_date": cash_ended.strftime("%Y-%m-%d") + " (episode end)",
            "days_in_cash": (cash_ended - cash_started).days,
            "pre_exit_holdings": pre_exit_holdings,
            "forwards": forwards,
        })

    if not exit_events:
        print("  No V6 exit events found during 2021-2022. (V6 never went")
        print("  fully to cash in this episode — gate likely fired only")
        print("  partially or not at all.)")
        return {"exit_events": []}

    for i, ev in enumerate(exit_events, 1):
        print(f"  Exit event #{i}: {ev['exit_date']} -> "
              f"{ev['reentry_date']} ({ev['days_in_cash']} days in cash)")
        if not ev["forwards"]:
            print("    No price-data ETFs to report on")
            continue
        # Sort by forward return descending
        forwards_sorted = sorted(ev["forwards"],
                                   key=lambda x: -x["fwd_return"])
        n_positive = sum(1 for f in forwards_sorted if f["fwd_return"] > 0)
        n_total = len(forwards_sorted)
        avg = sum(f["fwd_return"] for f in forwards_sorted) / n_total
        print(f"    Pre-exit holdings: {len(ev['pre_exit_holdings'])} ETFs")
        for f in forwards_sorted:
            marker = "  (V6 missed upside)" if f["fwd_return"] > 0.05 else ""
            print(f"      {f['etf']:<8} {f['fwd_return']*100:>+8.1f}%{marker}")
        verdict = ("V6 EXIT WAS CORRECT" if n_positive < n_total / 2 else
                    "V6 MAY HAVE EXITED TOO EARLY")
        print(f"    Summary: {n_positive}/{n_total} positive forward returns, "
              f"average {avg*100:+.1f}% -- {verdict}")
        print()
    return {"exit_events": exit_events}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("STRATEGY C V6 — ROBUSTNESS CHECKS")
    print("=" * 78)
    print("Loading prices ...", flush=True)
    closes = download_prices()
    signal = compute_signal(closes)
    features = {
        "ema_fast": compute_ema(closes, 50),
        "ema_slow": compute_ema(closes, 100),
        "signal_slope": compute_signal_slope(signal, 20),
        "rsi": compute_rsi(closes, 14),
        "realised_vol": compute_realised_vol(closes, 20),
    }
    eligible_start = pd.Timestamp("2018-11-08")

    print("Running baseline ...", flush=True)
    r_base = _run_rotation_full(closes, signal, HEADLINE_K, eligible_start,
                                  _eligible_baseline, features)
    print("Running V6 @ 30% ...", flush=True)
    r_v6 = _run_rotation_full(closes, signal, HEADLINE_K, eligible_start,
                                _eligible_v6_sleeve_breadth(0.30), features)

    episode_rows = phase_a_episode_attribution(r_base["equity"], r_v6["equity"])
    case_study = phase_b_case_study(closes, r_v6["rebal_log"])

    # Headline interpretation
    print("=" * 78)
    print("INTERPRETATION")
    print("=" * 78)
    big_wins = [r for r in episode_rows
                  if r["delta_return_pp"] is not None
                  and r["delta_return_pp"] > 5]
    losers = [r for r in episode_rows
                if r["delta_return_pp"] is not None
                and r["delta_return_pp"] < -2]
    print(f"  Episodes where V6 beat baseline by >5pp return: {len(big_wins)}")
    print(f"  Episodes where V6 underperformed baseline by >2pp: {len(losers)}")
    if len(big_wins) == 1:
        print(f"  -> Concern A IS valid: V6's edge is concentrated in")
        print(f"     {big_wins[0]['label']}. Deployment-defensible only if")
        print(f"     you believe future drawdowns will resemble that episode.")
    elif len(big_wins) >= 2:
        print(f"  -> Concern A weakened: V6 helps across multiple episodes,")
        print(f"     not just one.")
    if losers:
        print(f"  -> V6 cost return in {len(losers)} episode(s): "
              f"{[l['label'] for l in losers]}")
    print()
    n_correct = sum(1 for ev in case_study.get('exit_events', [])
                    if ev['forwards']
                    and sum(1 for f in ev['forwards'] if f['fwd_return'] > 0)
                        < len(ev['forwards']) / 2)
    n_total = len(case_study.get('exit_events', []))
    if n_total:
        print(f"  V6 exit events during 2021-22: {n_total} total, "
              f"{n_correct} correct (most held names declined further)")
        print(f"  -> Concern B {'IS supported' if n_correct < n_total / 2 else 'NOT supported'} "
              f"by case study evidence.")

    # ---- Save JSON ------------------------------------------
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "v6_threshold": 0.30,
        "K_used": HEADLINE_K,
        "phase_a_episode_attribution": episode_rows,
        "phase_b_case_study": case_study,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "thematic_exit_robustness.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
