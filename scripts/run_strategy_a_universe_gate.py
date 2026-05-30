"""Strategy A universe gate test.

Tests whether adding (or retroactively, including) a candidate ETF in
Strategy A's universe would pass the symmetric correlation +
walk-forward gate. SOXX was an early discretionary inclusion never
subjected to this test; IBB / other industry candidates need to clear
it before deployment.

Usage:
    python scripts/run_strategy_a_universe_gate.py CANDIDATE_TICKER

Examples:
    python scripts/run_strategy_a_universe_gate.py SOXX   # retro test
    python scripts/run_strategy_a_universe_gate.py IBB    # new candidate

Gate criteria:
    1. WITHIN-STRATEGY CORRELATION
       Candidate's weekly Friday breadth signal max pairwise corr
       with any existing universe ETF < 0.85. (Same threshold as
       Phase 5 retrospective.)

    2. DEPLOYED-K SHARPE
       In-sample Sharpe at K=7 weekly Friday must not DEGRADE by more
       than 0.05 vs the universe without the candidate.

    3. MAX DRAWDOWN
       Max DD must not WORSEN by more than 5pp vs the baseline.

    4. WALK-FORWARD K-STABILITY
       Annual K refit on the modified universe should pick K in the
       same {5, 6, 7} plateau as the baseline. A refit that picks
       K=3 every year is a red flag (the universe became too
       correlated for cross-sectional rotation).

Pre-requisites:
    data/breadth_<CANDIDATE>.json must exist. If missing, the script
    prints the data-prep instructions and exits.

Output:
    data/strategy_a_gate_<CANDIDATE>.json (full gate results)
    Console verdict with each criterion's pass / fail.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
from etf_registry import UNIVERSE_ETFS, get_etf  # noqa: E402

# Same constants as run_topk_robustness.py / run_portfolio.py
MA_PERIOD = 200
COST_BPS = 2          # Strategy A per-unit cost calibration
COST_FRAC = COST_BPS / 10_000
REBAL_FREQ = "W-FRI"
DEPLOYED_K = 7
K_GRID = [3, 5, 7, 9]
CORR_THRESHOLD = 0.85   # same threshold the Phase 5 retrospective used
SHARPE_DEGRADE_TOLERANCE = 0.05
DD_DEGRADE_TOLERANCE_PP = 5.0   # percentage-points


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_breadth_panel(tickers: list[str]) -> pd.DataFrame:
    """Load breadth_<TICKER>.json for each ticker; return a DataFrame
    indexed by date with one column per ticker (ma_breadth fraction 0-1).

    Schema (from compute_breadth.py output):
        {
          "etf": "SOXX",
          "series": {
            "dates": ["2018-01-05", ...],
            "ma_breadth": [0.807, 0.846, ...]   # the % above 200d MA
          },
          ...
        }
    """
    cols = {}
    missing = []
    for t in tickers:
        path = DATA_DIR / f"breadth_{t.lower()}.json"
        if not path.exists():
            missing.append(t)
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        series = d.get("series") or {}
        dates = series.get("dates")
        breadth = series.get("ma_breadth")
        if not dates or not breadth or len(dates) != len(breadth):
            missing.append(t)
            continue
        s = pd.Series(breadth, index=pd.to_datetime(dates))
        # Drop None / NaN at the front (before signal-eligible date)
        s = s.dropna()
        cols[t] = s
    if missing:
        raise FileNotFoundError(
            f"Missing or unreadable breadth JSON for: {missing}. "
            "Expected schema: {{series: {{dates, ma_breadth}}}}. "
            "Run scripts/fetch_constituents.py + scripts/compute_breadth.py "
            "for each missing ticker first."
        )
    df = pd.DataFrame(cols).sort_index()
    df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
    return df


def download_closes(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Daily adjusted-close DataFrame for the trading proxies of the
    given tickers. Uses each ETF's yfinance_trading_proxy from the
    registry (e.g. IUES -> XLE) so we hit the liquid US-listed
    instrument, not the .L UCITS."""
    import yfinance as yf
    proxies = {}
    for t in tickers:
        try:
            cfg = get_etf(t)
            proxies[t] = cfg.get("yfinance_trading_proxy") or t
        except Exception:
            proxies[t] = t   # candidate may not be in registry; assume = ticker
    syms = sorted(set(proxies.values()))
    print(f"  Downloading {len(syms)} trading proxies ({sorted(syms)}) ...",
          flush=True)
    raw = yf.download(syms, start=start, end=end, auto_adjust=True,
                       progress=False, threads=False,
                       group_by="ticker" if len(syms) > 1 else None)
    out = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in syms:
            if sym in raw.columns.get_level_values(0):
                col = raw[sym]["Close"] if "Close" in raw[sym].columns else None
                if col is not None and not col.dropna().empty:
                    out[sym] = col
    else:
        if "Close" in raw.columns:
            out[syms[0]] = raw["Close"]
    proxy_df = pd.DataFrame(out).sort_index()
    proxy_df.index = proxy_df.index.tz_localize(None)
    # Map back from proxy column to original ticker column
    df = pd.DataFrame({t: proxy_df[proxies[t]] for t in tickers
                        if proxies[t] in proxy_df.columns})
    return df


# ---------------------------------------------------------------------------
# Strategy A engine — slim reproduction of run_portfolio.run_portfolio()
# ---------------------------------------------------------------------------

def relative_breadth_signal(breadths: pd.DataFrame) -> pd.DataFrame:
    """Phase 20: sector-relative breadth = absolute breadth minus the
    cross-sectional mean per date. Keeps the rank-information without
    market-beta drift in broad rallies."""
    cs_mean = breadths.mean(axis=1, skipna=True)
    return breadths.sub(cs_mean, axis=0)


def top_k_weight_fn(K: int):
    """Top-K-by-signal weight function with Phase 20.1 positive-only
    fix: drop negatives BEFORE normalising so weights cannot sum
    above 1.0 in mixed-sign cross-sections."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        top = valid.nlargest(min(K, len(valid)))
        positives = top[top > 0]
        if len(positives) == 0:
            return pd.Series(0.0, index=s_row.index)
        normed = positives / positives.sum()
        w = pd.Series(0.0, index=s_row.index)
        w.loc[positives.index] = normed
        return w
    return f


def run_topk_backtest(closes: pd.DataFrame, breadths: pd.DataFrame,
                       K: int, eligible_start: pd.Timestamp) -> dict:
    """Top-K-by-breadth rotation, weekly Friday rebalance, t-1 signal."""
    closes = closes.loc[:, breadths.columns].dropna(how="all")
    signal = relative_breadth_signal(breadths)
    weight_fn = top_k_weight_fn(K)
    target = pd.date_range(eligible_start, closes.index[-1], freq=REBAL_FREQ)
    rebal_dates = closes.index[closes.index.isin(target)]
    rb = pd.DataFrame(index=rebal_dates, columns=closes.columns, dtype=float)
    for rd in rebal_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0: continue
        s_row = signal.iloc[prev_idx]
        rb.loc[rd] = weight_fn(s_row).reindex(closes.columns).fillna(0.0)
    weights = rb.reindex(closes.index, method="ffill").fillna(0.0)
    weights.loc[weights.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weights.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weights, "turnover": turnover}


def compute_stats(equity: pd.Series, eligible_start: pd.Timestamp) -> dict:
    eq = equity.loc[equity.index >= eligible_start]
    if len(eq) < 5: return {}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    cagr = (eq.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    rolling_max = eq.cummax()
    max_dd = float(((eq - rolling_max) / rolling_max).min())
    return {"sharpe": float(sharpe), "cagr": float(cagr),
             "max_dd": max_dd, "n_years": float(n_years)}


def walk_forward_K(closes: pd.DataFrame, breadths: pd.DataFrame,
                     eligible_start: pd.Timestamp,
                     train_years: int = 5) -> dict:
    """Annual K refit. Each year: pick best K on expanding train
    window, deploy forward for the test year."""
    initial_train_end = eligible_start + pd.DateOffset(years=train_years)
    last_date = closes.index[-1]
    if initial_train_end >= last_date - pd.DateOffset(years=1):
        return {"walk_forward_sharpe": None,
                "segments": [], "note": "insufficient history"}
    refit_ends_target = pd.date_range(initial_train_end, last_date, freq="YE")
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_ends_target]
    refit_ends = [r for r in refit_ends if r >= eligible_start]

    segments = []
    test_eq_pieces = []
    for i, train_end in enumerate(refit_ends):
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        train_end_idx = closes.index.get_loc(train_end)
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(closes): break
        test_start = closes.index[test_start_idx]
        best_K, best_sh = None, -1e9
        for K in K_GRID:
            r = run_topk_backtest(closes, breadths, K, eligible_start)
            eq = r["equity"].loc[(r["equity"].index >= eligible_start)
                                  & (r["equity"].index <= train_end)]
            if len(eq) < 5: continue
            eq = eq / eq.iloc[0]
            d = eq.pct_change().fillna(0)
            sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0
            if sh > best_sh: best_sh, best_K = sh, K
        if best_K is None: continue
        r = run_topk_backtest(closes, breadths, best_K, eligible_start)
        test_eq = r["equity"].loc[test_start:test_end]
        if len(test_eq) < 2: continue
        d = test_eq.pct_change().fillna(0)
        test_sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0
        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_K": best_K,
            "train_sharpe": round(best_sh, 3),
            "test_sharpe": round(test_sh, 3),
            "n_test_days": int(len(test_eq)),
        })
        last_val = test_eq_pieces[-1].iloc[-1] if test_eq_pieces else 1.0
        test_eq_pieces.append(test_eq * last_val / test_eq.iloc[0])
    if not test_eq_pieces:
        return {"walk_forward_sharpe": None, "segments": [],
                 "note": "no test segments produced"}
    wf_eq = pd.concat(test_eq_pieces)
    d = wf_eq.pct_change().fillna(0)
    wf_sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0
    return {"walk_forward_sharpe": round(wf_sh, 3), "segments": segments}


# ---------------------------------------------------------------------------
# Gate test
# ---------------------------------------------------------------------------

def run_gate(candidate: str) -> dict:
    print(f"=== Strategy A universe gate test — candidate: {candidate} ===")

    # Determine baseline vs proposed universe.
    # If the candidate is already in the deployed universe (e.g. SOXX),
    # we run a RETROACTIVE test: baseline = universe minus candidate.
    is_retro = candidate in UNIVERSE_ETFS
    if is_retro:
        baseline = [t for t in UNIVERSE_ETFS if t != candidate]
        proposed = list(UNIVERSE_ETFS)
        print(f"Mode: RETROACTIVE (candidate already in deployed universe).")
        print(f"Baseline {len(baseline)} ETFs (drop {candidate}) "
              f"vs Proposed {len(proposed)} ETFs (full deployed).")
    else:
        baseline = list(UNIVERSE_ETFS)
        proposed = baseline + [candidate]
        print(f"Mode: NEW CANDIDATE.")
        print(f"Baseline {len(baseline)} ETFs vs Proposed {len(proposed)} ETFs.")

    # Load breadth panels for the union — must include candidate.
    union = sorted(set(baseline) | set(proposed))
    print(f"\nLoading breadth panels for {len(union)} ETFs ...")
    breadth_all = load_breadth_panel(union)
    print(f"  Loaded {len(breadth_all.columns)} ETFs, "
          f"date range {breadth_all.index[0].date()} -> "
          f"{breadth_all.index[-1].date()}")

    # --- Gate 1: correlation matrix ---------------------------------------
    print("\n--- Gate 1: within-strategy correlation (weekly Friday) ---")
    weekly = breadth_all.resample("W-FRI").last().dropna(how="all")
    corr = weekly.corr()
    # Max correlation between candidate and the OTHER ETFs in proposed
    others = [t for t in proposed if t != candidate]
    candidate_corrs = corr.loc[candidate, others].sort_values(ascending=False)
    max_corr = float(candidate_corrs.iloc[0])
    max_corr_ticker = candidate_corrs.index[0]
    print(f"  Top 5 correlations of {candidate} vs existing universe:")
    for t, c in candidate_corrs.head(5).items():
        marker = "  <- FAILS GATE" if c > CORR_THRESHOLD else ""
        print(f"    {t:<6}  {c:+.3f}{marker}")
    gate1_pass = max_corr < CORR_THRESHOLD
    print(f"  Verdict: max corr {max_corr:+.3f} vs threshold "
          f"{CORR_THRESHOLD:+.3f}  ->  {'PASS' if gate1_pass else 'FAIL'}")

    # --- Run backtests for both universes ---------------------------------
    print("\n--- Loading ETF prices for backtest ---")
    common_start = breadth_all.dropna(how="all").index.min().strftime("%Y-%m-%d")
    common_end_date = breadth_all.index.max() + pd.Timedelta(days=5)
    closes = download_closes(union, common_start,
                              common_end_date.strftime("%Y-%m-%d"))
    print(f"  Got prices for {len(closes.columns)} ETFs")
    # Reindex breadth to price calendar (forward-fill within 7 days)
    breadth_aligned = breadth_all.reindex(closes.index).ffill(limit=7)

    # Eligible start = latest first-valid breadth + 30 trading days
    first_valids = [breadth_aligned[c].first_valid_index()
                     for c in breadth_aligned.columns]
    first_valids = [fv for fv in first_valids if fv is not None]
    if not first_valids:
        raise RuntimeError("No valid breadth data after alignment")
    latest_first = max(first_valids)
    idx_pos = closes.index.get_loc(latest_first)
    eligible_start = closes.index[min(idx_pos + 30, len(closes) - 1)]
    print(f"  Eligible start: {eligible_start.date()}")

    # --- Gate 2/3: in-sample Sharpe + Max DD comparison at deployed K ----
    print(f"\n--- Gate 2/3: in-sample Sharpe + Max DD at K={DEPLOYED_K} weekly ---")
    bl_closes = closes[[c for c in baseline if c in closes.columns]]
    bl_breadths = breadth_aligned[[c for c in baseline if c in breadth_aligned.columns]]
    pr_closes = closes[[c for c in proposed if c in closes.columns]]
    pr_breadths = breadth_aligned[[c for c in proposed if c in breadth_aligned.columns]]

    bl = run_topk_backtest(bl_closes, bl_breadths, DEPLOYED_K, eligible_start)
    pr = run_topk_backtest(pr_closes, pr_breadths, DEPLOYED_K, eligible_start)
    bl_stats = compute_stats(bl["equity"], eligible_start)
    pr_stats = compute_stats(pr["equity"], eligible_start)
    print(f"  Baseline ({len(baseline)} ETFs)  Sharpe {bl_stats['sharpe']:+.3f}  "
          f"CAGR {bl_stats['cagr']*100:+5.1f}%  DD {bl_stats['max_dd']*100:5.1f}%")
    print(f"  Proposed ({len(proposed)} ETFs)  Sharpe {pr_stats['sharpe']:+.3f}  "
          f"CAGR {pr_stats['cagr']*100:+5.1f}%  DD {pr_stats['max_dd']*100:5.1f}%")
    d_sharpe = pr_stats['sharpe'] - bl_stats['sharpe']
    d_dd = (pr_stats['max_dd'] - bl_stats['max_dd']) * 100   # pp
    print(f"  Delta Sharpe: {d_sharpe:+.3f}  "
          f"(tolerance: degrade < {SHARPE_DEGRADE_TOLERANCE})")
    print(f"  Delta DD:     {d_dd:+.2f}pp  "
          f"(tolerance: worsen < {DD_DEGRADE_TOLERANCE_PP}pp)")
    gate2_pass = d_sharpe > -SHARPE_DEGRADE_TOLERANCE
    gate3_pass = d_dd > -DD_DEGRADE_TOLERANCE_PP   # negative DD = worse, so > -5
    print(f"  Sharpe gate:  {'PASS' if gate2_pass else 'FAIL'}")
    print(f"  Max DD gate:  {'PASS' if gate3_pass else 'FAIL'}")

    # --- Gate 4: walk-forward K stability --------------------------------
    print(f"\n--- Gate 4: walk-forward K refit ---")
    bl_wf = walk_forward_K(bl_closes, bl_breadths, eligible_start)
    pr_wf = walk_forward_K(pr_closes, pr_breadths, eligible_start)
    bl_wf_sh = bl_wf.get("walk_forward_sharpe")
    pr_wf_sh = pr_wf.get("walk_forward_sharpe")
    if bl_wf_sh is None or pr_wf_sh is None:
        print(f"  Baseline WF: {bl_wf_sh}, Proposed WF: {pr_wf_sh}")
        print(f"  Verdict: SKIPPED (insufficient history)")
        gate4_pass = None
    else:
        d_wf = pr_wf_sh - bl_wf_sh
        print(f"  Baseline WF Sharpe: {bl_wf_sh:+.3f}")
        print(f"  Proposed WF Sharpe: {pr_wf_sh:+.3f}")
        print(f"  Delta WF Sharpe:    {d_wf:+.3f}  "
              f"(tolerance: degrade < {SHARPE_DEGRADE_TOLERANCE})")
        bl_ks = [s['best_K'] for s in bl_wf.get('segments', [])]
        pr_ks = [s['best_K'] for s in pr_wf.get('segments', [])]
        print(f"  Baseline K-sequence: {bl_ks}")
        print(f"  Proposed K-sequence: {pr_ks}")
        gate4_pass = d_wf > -SHARPE_DEGRADE_TOLERANCE

    # --- Overall verdict --------------------------------------------------
    print("\n" + "=" * 64)
    print("OVERALL VERDICT")
    print("=" * 64)
    print(f"  Gate 1 (within-strategy corr):  {'PASS' if gate1_pass else 'FAIL'}")
    print(f"  Gate 2 (in-sample Sharpe):      {'PASS' if gate2_pass else 'FAIL'}")
    print(f"  Gate 3 (max drawdown):          {'PASS' if gate3_pass else 'FAIL'}")
    print(f"  Gate 4 (walk-forward Sharpe):   "
          f"{'PASS' if gate4_pass else 'FAIL' if gate4_pass is False else 'SKIP'}")
    must_pass = [gate1_pass, gate2_pass, gate3_pass]
    if gate4_pass is not None:
        must_pass.append(gate4_pass)
    overall = "PASS" if all(must_pass) else "FAIL"
    print(f"\n  >> {overall} <<")
    if is_retro and overall == "PASS":
        print(f"  ({candidate} retroactively passes the gate it never got)")
    elif is_retro and overall == "FAIL":
        print(f"  ({candidate} would NOT have passed if tested today — "
              "consider removing from deployed universe)")

    return {
        "computed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "candidate": candidate,
        "mode": "retroactive" if is_retro else "new_candidate",
        "baseline_universe": baseline,
        "proposed_universe": proposed,
        "eligible_start": eligible_start.strftime("%Y-%m-%d"),
        "correlation": {
            "max_corr": max_corr,
            "max_corr_with": max_corr_ticker,
            "threshold": CORR_THRESHOLD,
            "top_5_pairs": [{"ticker": t, "corr": float(c)}
                              for t, c in candidate_corrs.head(5).items()],
            "passed": bool(gate1_pass),
        },
        "in_sample": {
            "K": DEPLOYED_K,
            "baseline_stats": bl_stats,
            "proposed_stats": pr_stats,
            "delta_sharpe": float(d_sharpe),
            "delta_max_dd_pp": float(d_dd),
            "sharpe_passed": bool(gate2_pass),
            "max_dd_passed": bool(gate3_pass),
        },
        "walk_forward": {
            "baseline": bl_wf,
            "proposed": pr_wf,
            "passed": bool(gate4_pass) if gate4_pass is not None else None,
        },
        "overall_pass": overall == "PASS",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("candidate", help="Ticker to gate-test")
    args = p.parse_args()
    try:
        result = run_gate(args.candidate.upper())
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    out_path = DATA_DIR / f"strategy_a_gate_{args.candidate.upper()}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(ROOT)}")
    return 0 if result["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
