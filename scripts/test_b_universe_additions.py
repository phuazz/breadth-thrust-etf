"""Empirical test: add each candidate ETF to Strategy B and measure impact.

For each candidate, re-run B's K=7 weekly headline + walk-forward Sharpe,
then report the delta vs the current 14-ETF baseline on:
  - Full backtest Sharpe / CAGR / max DD
  - Walk-forward Sharpe (annual K-refit)
  - 2022 single-year window (the user's pain point)
  - 2022-2024 multi-year window

A candidate is worth deploying only if it improves the WF Sharpe (the OOS
honest measure) without materially widening DD or hurting 2022-2024.

Method: monkey-patch run_asset_class_rotation.TICKERS / UNIVERSE for each
test, run the existing engine, restore baseline.

Usage:
    python scripts/test_b_universe_additions.py [TICKER1 TICKER2 ...]

Default: tests INDA, EMB, IGOV, EWZ, FXI (the gate-passers worth testing).
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_asset_class_rotation as B  # noqa: E402

DATA_DIR = ROOT / "data"
START_DATE = "2007-01-01"
END_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Hard-coded asset_class metadata for new candidates (so the universe-export
# entry has a sane label). Only used inside this test script.
CANDIDATE_META = {
    "INDA": {"label": "MSCI India",                      "asset_class": "Emerging Mkts"},
    "EMB":  {"label": "EM USD-denominated Bonds",        "asset_class": "Bonds"},
    "IGOV": {"label": "Intl Developed Gov Bonds (ex-US)","asset_class": "Bonds"},
    "EWZ":  {"label": "MSCI Brazil",                     "asset_class": "Emerging Mkts"},
    "FXI":  {"label": "FTSE China 50",                   "asset_class": "Emerging Mkts"},
    "BIL":  {"label": "1-3 month US T-bills",            "asset_class": "Bonds"},
    "BNDX": {"label": "Vanguard Intl Bond (hedged)",     "asset_class": "Bonds"},
}


def _window_stats(dates, eq, start, end):
    s = pd.Series(eq, index=pd.to_datetime(dates)).loc[start:end].dropna()
    if len(s) < 5:
        return None
    s = s / s.iloc[0]
    d = s.pct_change().fillna(0)
    n_years = (s.index[-1] - s.index[0]).days / 365.25
    cagr = s.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else 0
    sh = d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0
    dd = ((s - s.cummax()) / s.cummax()).min()
    return {"sharpe": sh, "cagr": cagr, "total": s.iloc[-1] - 1, "dd": dd}


def _download_one(ticker: str) -> pd.Series:
    """Fetch single-ticker close; reuse cache columns if already present."""
    cache = DATA_DIR / "asset_class_prices_cache.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if ticker in df.columns:
            return df[ticker].dropna()
    print(f"  Fetching {ticker} from yfinance ...", flush=True)
    raw = yf.download(ticker, start=START_DATE, end=END_DATE,
                       auto_adjust=True, progress=False, threads=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna()


def _run_b_with_universe(closes: pd.DataFrame) -> dict:
    """Replicate B's K=7 weekly + walk-forward on a given closes panel."""
    # Drop early dates with any NaN (mimics B's behavior)
    closes = closes.dropna().sort_index()
    if len(closes) == 0:
        return {}
    eligible = closes.index[B.MA_PERIOD]
    signal = B.compute_signal(closes)
    # K=7 weekly Friday headline
    r = B.run_rotation(closes, signal, B.top_k_by_signal(7), eligible,
                        rebalance_freq="W-FRI")
    st = B.compute_stats(r["equity"], eligible)
    to = B.turnover_stats(r["weights"], eligible)
    eq = r["equity"].loc[r["equity"].index >= eligible]
    eq = eq / eq.iloc[0]
    # Walk-forward
    wf = B.walk_forward_K(closes, signal, eligible,
                            pd.Timestamp("2014-12-31"),
                            K_grid=B.K_GRID, refit_freq="YE",
                            rebal_freq="W-FRI")
    out = {
        "stats_full": {**st, **to},
        "wf_sharpe": wf.get("walk_forward_sharpe") if wf else None,
        "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
        "equity": list(eq.values),
    }
    return out


def main() -> int:
    candidates = sys.argv[1:] or ["INDA", "EMB", "IGOV", "EWZ", "FXI"]
    print(f"Testing candidates: {candidates}")
    print(f"Baseline: 14 ETFs (current B universe) + SHY cash floor")
    print()

    # Load baseline B panel (current cache)
    cache_path = DATA_DIR / "asset_class_prices_cache.parquet"
    baseline_panel = pd.read_parquet(cache_path)
    # Use only the 14 TICKERS + SHY (matches current deployed config)
    baseline_panel = baseline_panel[B.TICKERS + B.CASH_ONLY_TICKERS]

    print("Computing baseline ...")
    baseline = _run_b_with_universe(baseline_panel)
    if not baseline:
        print("ERROR: baseline run failed")
        return 1
    b_full = baseline["stats_full"]
    b_2022 = _window_stats(baseline["dates"], baseline["equity"],
                            "2022-01-01", "2022-12-31")
    b_2224 = _window_stats(baseline["dates"], baseline["equity"],
                            "2022-01-01", "2024-12-31")
    print(f"  BASELINE Sharpe {b_full['sharpe']:+.3f}  WF {baseline['wf_sharpe']:+.3f}  "
          f"CAGR {b_full['cagr']*100:+.1f}%  DD {b_full['max_dd']*100:.1f}%")
    print(f"           2022   Sharpe {b_2022['sharpe']:+.3f}  Total {b_2022['total']*100:+.1f}%  DD {b_2022['dd']*100:.1f}%")
    print(f"           22-24  Sharpe {b_2224['sharpe']:+.3f}  Total {b_2224['total']*100:+.1f}%  DD {b_2224['dd']*100:.1f}%")
    print()

    # Test each candidate individually
    rows = []
    for c in candidates:
        print(f"--- Adding {c} ({CANDIDATE_META.get(c,{}).get('label','?')}) ---")
        try:
            cand_series = _download_one(c)
        except Exception as exc:
            print(f"  ERROR fetching {c}: {exc}")
            continue
        cand_series.name = c
        panel = baseline_panel.copy()
        panel[c] = cand_series
        # Monkey-patch B.TICKERS so the cash-proxy exclusion and other
        # logic that references TICKERS sees the new ticker. UNIVERSE
        # isn't read by run_rotation directly so we can skip patching it.
        original_tickers = B.TICKERS
        B.TICKERS = original_tickers + [c]
        try:
            r = _run_b_with_universe(panel)
        finally:
            B.TICKERS = original_tickers
        if not r:
            print(f"  Run failed; skipping")
            continue
        f = r["stats_full"]
        s2022 = _window_stats(r["dates"], r["equity"], "2022-01-01", "2022-12-31")
        s2224 = _window_stats(r["dates"], r["equity"], "2022-01-01", "2024-12-31")
        d_sh   = f["sharpe"] - b_full["sharpe"]
        d_wf   = r["wf_sharpe"] - baseline["wf_sharpe"] if r["wf_sharpe"] and baseline["wf_sharpe"] else None
        d_dd   = (f["max_dd"] - b_full["max_dd"]) * 100
        d_cagr = (f["cagr"] - b_full["cagr"]) * 100
        d_22_tot = (s2022["total"] - b_2022["total"]) * 100 if s2022 and b_2022 else None
        d_24_tot = (s2224["total"] - b_2224["total"]) * 100 if s2224 and b_2224 else None
        d_22_dd  = (s2022["dd"]   - b_2022["dd"])   * 100 if s2022 and b_2022 else None
        rows.append({
            "ticker": c,
            "sharpe": f["sharpe"], "d_sh": d_sh,
            "wf": r["wf_sharpe"],  "d_wf": d_wf,
            "cagr": f["cagr"],     "d_cagr": d_cagr,
            "dd": f["max_dd"],     "d_dd": d_dd,
            "tot22": s2022["total"] if s2022 else None,  "d_22_tot": d_22_tot,
            "dd22":  s2022["dd"]   if s2022 else None,   "d_22_dd": d_22_dd,
            "tot24": s2224["total"] if s2224 else None,  "d_24_tot": d_24_tot,
        })
        wf_str = f"{r['wf_sharpe']:+.3f}" if r['wf_sharpe'] is not None else "n/a"
        d_wf_str = f" (d {d_wf:+.3f})" if d_wf is not None else ""
        print(f"  Sharpe {f['sharpe']:+.3f} (d {d_sh:+.3f})  "
              f"WF {wf_str}{d_wf_str}  "
              f"CAGR {f['cagr']*100:+.1f}% (d {d_cagr:+.2f}pp)  "
              f"DD {f['max_dd']*100:.1f}% (d {d_dd:+.2f}pp)")
        if s2022:
            print(f"  2022:  total {s2022['total']*100:+.1f}% (d {d_22_tot:+.2f}pp)  "
                  f"DD {s2022['dd']*100:.1f}% (d {d_22_dd:+.2f}pp)")
        if s2224:
            print(f"  22-24: total {s2224['total']*100:+.1f}% (d {d_24_tot:+.2f}pp)")
        print()

    # Summary table
    print("=" * 110)
    print("SUMMARY — Strategy B candidate addition impact (vs 14-ETF baseline)")
    print("=" * 110)
    print(f"  {'Ticker':<6s}  {'Sharpe':>8s} {'dSh':>7s}   {'WF':>6s} {'dWF':>7s}   "
          f"{'2022 tot':>9s} {'d22':>7s}   {'22-24 tot':>10s} {'d24':>7s}   "
          f"{'DD':>7s} {'dDD':>7s}")
    for r in rows:
        d_wf_str = f"{r['d_wf']:+.3f}" if r['d_wf'] is not None else "  n/a"
        print(f"  {r['ticker']:<6s}  {r['sharpe']:>+8.3f} {r['d_sh']:>+7.3f}   "
              f"{r['wf']:>+6.3f} {d_wf_str:>7s}   "
              f"{r['tot22']*100:>+8.1f}% {r['d_22_tot']:>+6.2f}pp   "
              f"{r['tot24']*100:>+9.1f}% {r['d_24_tot']:>+6.2f}pp   "
              f"{r['dd']*100:>+6.1f}% {r['d_dd']:>+6.2f}pp")
    print()
    print(f"Baseline reference: Sharpe {b_full['sharpe']:+.3f}  WF {baseline['wf_sharpe']:+.3f}  "
          f"CAGR {b_full['cagr']*100:+.1f}%  DD {b_full['max_dd']*100:.1f}%")
    print(f"                    2022   total {b_2022['total']*100:+.1f}%  DD {b_2022['dd']*100:.1f}%")
    print(f"                    22-24  total {b_2224['total']*100:+.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
