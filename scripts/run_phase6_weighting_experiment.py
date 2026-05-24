"""Phase 6 — weighting-scheme experiment for Strategy C (and B if C benefits).

Hypothesis: Strategy C's modest in-sample Sharpe (+0.71) and worse
walk-forward Sharpe (+0.36) are partly caused by the signal-share
weighting scheme, which overweights the most-overbought ETF.
Statistically the most-overbought ETF is the one most likely to
mean-revert, so weight-by-signal-share systematically overweights
the candidate with the worst forward expected return.

Test 4 weighting schemes side-by-side on the existing Strategy C
universe (no universe changes — pure parameter sweep):

  1. Current: signal-share with 35% per-ETF cap (the baseline)
  2. Equal-weight: 1/K across the top K (ignore signal magnitudes)
  3. Sqrt(signal): weight ∝ √signal (softens proportionality)
  4. Rank-weighted: top gets K weight units, 2nd K-1, ..., K-th 1
     (bounded dispersion regardless of signal magnitude)

For each: in-sample Sharpe, walk-forward Sharpe, max DD, turnover.
Walk-forward is the deciding metric.

Win condition: lift walk-forward Sharpe by ≥+0.10 with in-sample
Sharpe not dropping more than 0.05.

If C has a winner, apply the same scheme to Strategy B and re-test.

This script does NOT modify any deployed JSON. It only prints a
comparison table.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

# Import Strategy C config and helpers
from run_thematic_rotation import (  # noqa: E402
    UNIVERSE as C_UNIVERSE,
    CASH_PROXY as C_CASH,
    MA_PERIOD,
    SIGNAL_FLOOR as C_FLOOR,
    PER_ETF_CAP as C_CAP,
    COST_FRAC,
    HEADLINE_K as C_K,
    HEADLINE_FREQ as C_FREQ,
    download_prices as c_download,
    compute_signal,
    run_rotation,
    compute_stats,
    turnover_stats,
    top_k_by_signal_capped,  # current baseline (with cap)
)

# Import Strategy B config and helpers
from run_asset_class_rotation import (  # noqa: E402
    UNIVERSE as B_UNIVERSE,
    CASH_PROXY as B_CASH,
    HEADLINE_K as B_K,
    HEADLINE_FREQ as B_FREQ,
)


# =========================================================================
# Alternative weighters
# =========================================================================

def make_equal_weight(K: int, signal_floor: float, cash_proxy: str):
    """1/K across the top K candidates with signal >= floor.
    Below-floor slots go to cash."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        eligible = valid[valid > signal_floor]
        w = pd.Series(0.0, index=s_row.index)
        if len(eligible) == 0:
            if cash_proxy in w.index:
                w[cash_proxy] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested_frac = len(top) / K
        per_etf = invested_frac / len(top)
        w.loc[top.index] = per_etf
        cash = 1.0 - invested_frac
        if cash > 0 and cash_proxy in w.index:
            w[cash_proxy] = w.get(cash_proxy, 0.0) + cash
        return w
    return f


def make_sqrt_signal(K: int, signal_floor: float, cash_proxy: str):
    """Weight proportional to √signal among the top K eligible.
    Softens the signal-share proportionality."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        eligible = valid[valid > signal_floor]
        w = pd.Series(0.0, index=s_row.index)
        if len(eligible) == 0:
            if cash_proxy in w.index:
                w[cash_proxy] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested_frac = len(top) / K
        sqrt_top = np.sqrt(top)
        if sqrt_top.sum() <= 0:
            if cash_proxy in w.index:
                w[cash_proxy] = 1.0
            return w
        weights = (sqrt_top / sqrt_top.sum()) * invested_frac
        w.loc[top.index] = weights
        cash = 1.0 - invested_frac
        if cash > 0 and cash_proxy in w.index:
            w[cash_proxy] = w.get(cash_proxy, 0.0) + cash
        return w
    return f


def make_rank_weighted(K: int, signal_floor: float, cash_proxy: str):
    """Top gets K weight units, 2nd K-1, ..., K-th 1. Bounded dispersion
    regardless of signal magnitude — pure rank, signal magnitude ignored
    beyond ordering."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        eligible = valid[valid > signal_floor]
        w = pd.Series(0.0, index=s_row.index)
        if len(eligible) == 0:
            if cash_proxy in w.index:
                w[cash_proxy] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested_frac = len(top) / K
        # Rank weights: top gets K, 2nd K-1, ..., K-th gets 1
        n = len(top)
        ranks = pd.Series(np.arange(n, 0, -1, dtype=float), index=top.index)
        weights = (ranks / ranks.sum()) * invested_frac
        w.loc[top.index] = weights
        cash = 1.0 - invested_frac
        if cash > 0 and cash_proxy in w.index:
            w[cash_proxy] = w.get(cash_proxy, 0.0) + cash
        return w
    return f


# =========================================================================
# Walk-forward (refit K annually on expanding train window)
# =========================================================================

def walk_forward_sharpe(closes: pd.DataFrame, signal: pd.DataFrame,
                          eligible_start: pd.Timestamp,
                          initial_train_end: pd.Timestamp,
                          weighter_factory,  # callable(K) -> weight_fn
                          K_grid: list[int],
                          rebal_freq: str) -> dict:
    """Annual K refit on expanding train window, applying the chosen
    weighter at each refit. Returns concatenated test-segment Sharpe."""
    last_date = closes.index[-1]
    refit_ends = pd.date_range(initial_train_end, last_date, freq="YE")
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_ends]
    refit_ends = [r for r in refit_ends if r >= eligible_start]
    if not refit_ends:
        return {"walk_forward_sharpe": None, "n_segments": 0, "K_sequence": []}

    def _portfolio_equity(K, win_start):
        wf = weighter_factory(K)
        r = run_rotation(closes, signal, wf, win_start,
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

    test_eq_pieces = []
    K_sequence = []
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
        K_sequence.append(best_K)
        full_eq = _portfolio_equity(best_K, eligible_start)
        test_eq = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq / base_val
        last_val = test_eq_pieces[-1].iloc[-1] if test_eq_pieces else 1.0
        test_eq_pieces.append(test_eq * last_val / test_eq.iloc[0])
    if not test_eq_pieces:
        return {"walk_forward_sharpe": None, "n_segments": 0, "K_sequence": []}
    wf_equity = pd.concat(test_eq_pieces)
    wf_daily = wf_equity.pct_change().fillna(0)
    wf_sh = (wf_daily.mean() / wf_daily.std() * math.sqrt(252)
              if wf_daily.std() > 0 else 0.0)
    return {
        "walk_forward_sharpe": float(wf_sh),
        "n_segments": len(test_eq_pieces),
        "K_sequence": K_sequence,
    }


# =========================================================================
# Experiment runner
# =========================================================================

def run_experiment(label: str, closes: pd.DataFrame, signal: pd.DataFrame,
                    eligible_start: pd.Timestamp,
                    K_headline: int, rebal_freq: str,
                    K_grid: list[int],
                    initial_train_end: pd.Timestamp,
                    signal_floor: float, cash_proxy: str,
                    use_cap: bool, cap_value: float) -> None:
    """Run all 4 weighting schemes and print a comparison table."""
    print(f"\n{'=' * 95}")
    print(f"EXPERIMENT: {label}")
    print(f"{'=' * 95}")
    print(f"  universe: {len(closes.columns)} tickers (incl {cash_proxy} cash proxy)")
    print(f"  eligible_start: {eligible_start.date()}, initial train end: {initial_train_end.date()}")
    print(f"  K headline: {K_headline}, K grid (walk-fwd): {K_grid}, rebal: {rebal_freq}")
    print(f"  signal floor: +{signal_floor*100:.0f}% above MA200")

    schemes = []
    schemes.append(("current (signal-share + cap)" if use_cap
                     else "current (signal-share)",
                     lambda K: top_k_by_signal_capped(K) if use_cap
                                else _signal_share_uncapped(K, signal_floor, cash_proxy)))
    schemes.append(("equal-weight (1/K)",
                     lambda K: make_equal_weight(K, signal_floor, cash_proxy)))
    schemes.append(("sqrt(signal) share",
                     lambda K: make_sqrt_signal(K, signal_floor, cash_proxy)))
    schemes.append(("rank-weighted (K..1)",
                     lambda K: make_rank_weighted(K, signal_floor, cash_proxy)))

    results = []
    for scheme_name, factory in schemes:
        # In-sample at headline K
        wf_factory = factory  # closure capture
        weighter = wf_factory(K_headline)
        r = run_rotation(closes, signal, weighter, eligible_start,
                         rebalance_freq=rebal_freq)
        is_stats = compute_stats(r["equity"], eligible_start)
        is_turn = turnover_stats(r["weights"], eligible_start)
        # Walk-forward
        wf = walk_forward_sharpe(closes, signal, eligible_start,
                                  initial_train_end, wf_factory, K_grid,
                                  rebal_freq)
        results.append({
            "scheme": scheme_name,
            "is_sharpe": is_stats["sharpe"],
            "wf_sharpe": wf["walk_forward_sharpe"],
            "cagr": is_stats["cagr"],
            "max_dd": is_stats["max_dd"],
            "turnover": is_turn["annual_turnover"],
            "K_seq": wf["K_sequence"],
        })

    # Print comparison
    print(f"\n  {'Scheme':<32} {'IS Sharpe':>10} {'WF Sharpe':>10} {'CAGR':>8} {'Max DD':>8} {'Tov/yr':>8}")
    print(f"  {'-'*32} {'-'*10} {'-'*10} {'-'*8} {'-'*8} {'-'*8}")
    for r in results:
        is_sh = r["is_sharpe"] or 0
        wf_sh = r["wf_sharpe"] or 0
        cagr = r["cagr"] or 0
        dd = r["max_dd"] or 0
        tov = r["turnover"] or 0
        print(f"  {r['scheme']:<32} {is_sh:+10.3f} {wf_sh:+10.3f} "
              f"{cagr*100:+7.1f}% {dd*100:+7.1f}% {tov:>7.1f}x")
        print(f"    K sequence (walk-fwd): {r['K_seq']}")

    # Identify winner
    print(f"\n  WIN CONDITION: WF Sharpe ≥ baseline + 0.10, IS Sharpe drop ≤ 0.05")
    baseline = results[0]
    base_wf = baseline["wf_sharpe"] or 0
    base_is = baseline["is_sharpe"] or 0
    winners = []
    for r in results[1:]:
        wf = r["wf_sharpe"] or 0
        ins = r["is_sharpe"] or 0
        if wf >= base_wf + 0.10 and ins >= base_is - 0.05:
            winners.append(r)
    if winners:
        best = max(winners, key=lambda x: x["wf_sharpe"])
        print(f"  WINNERS: {[w['scheme'] for w in winners]}")
        print(f"  BEST:    {best['scheme']} (WF {best['wf_sharpe']:+.3f} vs baseline {base_wf:+.3f}, "
              f"delta +{(best['wf_sharpe'] - base_wf):.3f})")
    else:
        # Even if no winner by strict criterion, surface anyone with better WF
        better_wf = [r for r in results[1:] if (r["wf_sharpe"] or 0) > base_wf]
        if better_wf:
            best = max(better_wf, key=lambda x: x["wf_sharpe"])
            print(f"  NO STRICT WINNER. Closest: {best['scheme']} "
                  f"WF {best['wf_sharpe']:+.3f} vs baseline {base_wf:+.3f} "
                  f"(delta +{(best['wf_sharpe'] - base_wf):.3f})")
        else:
            print(f"  NO WINNER. All alternatives produce equal or lower WF Sharpe.")


def _signal_share_uncapped(K: int, signal_floor: float, cash_proxy: str):
    """Pure signal-share weighting with no per-ETF cap.
    Used as the Strategy B baseline (B has no cap)."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        eligible = valid[valid > signal_floor]
        w = pd.Series(0.0, index=s_row.index)
        if len(eligible) == 0:
            if cash_proxy in w.index:
                w[cash_proxy] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested_frac = len(top) / K
        if top.sum() <= 0:
            if cash_proxy in w.index:
                w[cash_proxy] = 1.0
            return w
        weights = (top / top.sum()) * invested_frac
        w.loc[top.index] = weights
        cash = 1.0 - invested_frac
        if cash > 0 and cash_proxy in w.index:
            w[cash_proxy] = w.get(cash_proxy, 0.0) + cash
        return w
    return f


# =========================================================================
# Main
# =========================================================================

def main() -> int:
    # ----- Strategy C -----
    print("Loading Strategy C universe ...", flush=True)
    c_closes = c_download()
    c_closes = c_closes.dropna(axis=1, how="all")
    c_signal = compute_signal(c_closes)
    # Eligible start: latest first-valid-MA200 date across universe
    starts = []
    for col in c_closes.columns:
        v = c_signal[col].dropna()
        if len(v):
            starts.append(v.index.min())
    c_eligible = max(starts)
    c_eligible = c_closes.index[c_closes.index >= c_eligible][0]
    # Initial train end: 2 years after eligible_start
    c_train_end = c_closes.index[c_closes.index >= c_eligible + pd.Timedelta(days=730)][0]
    print(f"  C eligible: {c_eligible.date()}, initial train end: {c_train_end.date()}")
    run_experiment(
        "Strategy C — thematic momentum (16 ETFs)", c_closes, c_signal,
        c_eligible, C_K, C_FREQ, [3, 4, 5],
        c_train_end, C_FLOOR, C_CASH, use_cap=True, cap_value=C_CAP,
    )

    # ----- Strategy B -----
    # Load Strategy B's universe via yfinance directly (run_asset_class_rotation
    # uses a different download path so we re-fetch here).
    print("\nLoading Strategy B universe ...", flush=True)
    b_tickers = list(B_UNIVERSE.keys())
    import yfinance as yf
    raw = yf.download(b_tickers, start="2008-01-01", end="2026-05-23",
                       auto_adjust=True, progress=False, threads=True,
                       group_by="ticker")
    closes = {}
    for t in b_tickers:
        if (t, "Close") in raw.columns:
            closes[t] = raw[(t, "Close")]
    b_closes = pd.DataFrame(closes).sort_index()
    b_closes.index = pd.to_datetime(b_closes.index).tz_localize(None)
    b_closes = b_closes.dropna(axis=1, how="all")
    b_signal = compute_signal(b_closes)
    starts = []
    for col in b_closes.columns:
        v = b_signal[col].dropna()
        if len(v):
            starts.append(v.index.min())
    b_eligible = max(starts)
    b_eligible = b_closes.index[b_closes.index >= b_eligible][0]
    b_train_end = b_closes.index[b_closes.index >= b_eligible + pd.Timedelta(days=730)][0]
    print(f"  B eligible: {b_eligible.date()}, initial train end: {b_train_end.date()}")
    # Strategy B uses signal_floor = 0 (any positive signal eligible) and NO cap
    B_FLOOR = 0.0  # Strategy B has no signal floor (only requires positive)
    run_experiment(
        "Strategy B — asset-class momentum (14 ETFs)", b_closes, b_signal,
        b_eligible, B_K, B_FREQ, [5, 6, 7, 8],
        b_train_end, B_FLOOR, B_CASH, use_cap=False, cap_value=1.0,
    )

    print("\n" + "=" * 95)
    print("DONE. Inspect table above for winners.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
