"""Phase 23 — Regional relative-breadth rotation (Idea 2 at index level).

Generalises the Phase 20 sector-relative-breadth mechanism that worked
for Strategy A (US sectors) to the REGION/INDEX level.

Universe (7 regional indices with constituent breadth available):
  CSP1   — S&P 500 (US large)
  CNDX   — NASDAQ-100 (US tech)
  IDP6   — S&P SmallCap 600 (US small)
  IJPN   — MSCI Japan
  ITWN   — MSCI Taiwan
  NDIA   — MSCI India
  ICHN   — MSCI China A

Missing: Europe broad (would need IMEU or equivalent fetch). Scope A
test runs without Europe; if results encourage, escalate to Scope B
with proper Europe coverage.

Mechanism:
  1. For each index, compute % of constituents above own 200d MA
     (same metric as Strategy A's sector breadth)
  2. Each date: subtract cross-sectional mean from each index's breadth
     -> relative breadth (positive = above the global mean)
  3. Rank by relative breadth, take top K, weight by RELATIVE breadth
     (capped at zero — never pick a region with below-mean breadth)
  4. Rebalance weekly Friday

Compared against:
  Baseline (no Phase 23): current deployed gated + EEM-tilted blend
  Variant: Phase 23 as 5th sleeve at 10%, funded from B (same as EEM tilt)
           or alternative funding sources.

Decision: deploy if 22-on Total improves +1pp+ with no Full Sharpe drag
and DD impact within +/-0.5pp.

Usage: python scripts/test_phase23_regional_rotation.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from run_ma200_sweep import load_constituent_prices, compute_ma200_breadth, MA_PERIOD  # noqa: E402

# 7-index universe — all have constituent caches at data/prices_cache_{etf}.parquet
REGIONAL_INDICES = [
    ("CSP1", "S&P 500 (US large)"),
    ("CNDX", "NASDAQ-100 (US tech-heavy)"),
    ("IDP6", "S&P SmallCap 600 (US small)"),
    ("IJPN", "MSCI Japan"),
    ("ITWN", "MSCI Taiwan"),
    ("NDIA", "MSCI India"),
    ("ICHN", "MSCI China A"),
]

# yfinance trading proxies for price returns (we need ETF prices, not
# constituent prices, for the rotation return calculation)
TRADING_PROXIES = {
    "CSP1": "SPY",     # S&P 500 ETF (USD-listed)
    "CNDX": "QQQ",     # NASDAQ-100 ETF
    "IDP6": "IJR",     # S&P 600 (US small)
    "IJPN": "EWJ",     # MSCI Japan
    "ITWN": "EWT",     # MSCI Taiwan
    "NDIA": "INDA",    # MSCI India
    "ICHN": "MCHI",    # MSCI China
}

COST_BPS = 5
COST_FRAC = COST_BPS / 10_000
K = 3
REBAL_FREQ = "W-FRI"

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-onwards", "2022-01-01", None),
]


def _stats(eq):
    if len(eq) < 5: return {"sharpe": None}
    e = eq.dropna() / eq.dropna().iloc[0]
    d = e.pct_change().fillna(0)
    n = (e.index[-1] - e.index[0]).days / 365.25
    return {
        "sharpe": d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0,
        "cagr": e.iloc[-1] ** (1 / n) - 1 if n > 0 else 0,
        "total": e.iloc[-1] - 1,
        "dd": ((e - e.cummax()) / e.cummax()).min(),
    }


def _ws(eq, start, end):
    w = eq.loc[start:end] if (start or end) else eq
    return _stats(w)


def load_regional_breadths() -> pd.DataFrame:
    """Compute ma200 breadth for each regional index from cached
    constituent prices. Returns DataFrame indexed by date, columns =
    index tickers."""
    out = {}
    for etf, label in REGIONAL_INDICES:
        try:
            prices = load_constituent_prices(etf)
        except FileNotFoundError:
            print(f"  WARN: no constituent cache for {etf}; skipping")
            continue
        breadth = compute_ma200_breadth(prices, MA_PERIOD)
        out[etf] = breadth
        print(f"  {etf:6s} {label:35s}  {len(prices.columns):>5d} constituents  "
              f"breadth {breadth.first_valid_index().date()} -> "
              f"{breadth.last_valid_index().date()}")
    return pd.DataFrame(out).sort_index()


def load_proxy_prices() -> pd.DataFrame:
    """ETF-level price for each region (used for rotation returns).
    Uses yfinance via a small cache."""
    import yfinance as yf
    cache = DATA_DIR / "regional_etf_prices_cache.parquet"
    tickers = list(TRADING_PROXIES.values())
    if cache.exists():
        df = pd.read_parquet(cache)
        if set(tickers).issubset(df.columns):
            stale = (pd.Timestamp.utcnow().tz_localize(None) - df.index.max()).days
            if stale <= 7:
                print(f"  Using cached regional ETF prices ({stale}d stale)")
                return df[tickers]
    print(f"  Downloading {len(tickers)} regional ETF prices ...", flush=True)
    raw = yf.download(tickers, start="2007-01-01", auto_adjust=True,
                       progress=False, threads=True, group_by="ticker")
    closes = {t: raw[(t, "Close")] for t in tickers if (t, "Close") in raw.columns}
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index().dropna(how="all")
    df.to_parquet(cache)
    return df


def compute_relative_breadth(breadths: pd.DataFrame) -> pd.DataFrame:
    """Subtract row-mean from each entry — same mechanism as Phase 20."""
    row_mean = breadths.mean(axis=1, skipna=True)
    return breadths.sub(row_mean, axis=0)


def top_k_relative(K: int):
    """Pick top-K by relative breadth value, weight equally."""
    def f(b_row: pd.Series) -> pd.Series:
        valid = b_row.dropna()
        if len(valid) == 0:
            return pd.Series(0.0, index=b_row.index)
        # Drop indices with below-zero relative breadth (below mean)
        positives = valid[valid > 0]
        if len(positives) == 0:
            return pd.Series(0.0, index=b_row.index)
        top = positives.nlargest(min(K, len(positives)))
        per = 1.0 / len(top)  # equal weight within top-K
        w = pd.Series(0.0, index=b_row.index)
        w.loc[top.index] = per
        return w
    return f


def run_regional_rotation(closes: pd.DataFrame, signal: pd.DataFrame,
                            eligible: pd.Timestamp, K: int) -> pd.Series:
    """Run the rotation; closes columns are PROXY tickers, signal columns
    are INDEX tickers. We map index -> proxy when applying weights."""
    rebal = pd.date_range(eligible, closes.index[-1], freq=REBAL_FREQ)
    rebal = closes.index[closes.index.isin(rebal)]
    # Weights stored on PROXY tickers
    proxy_for_index = TRADING_PROXIES
    proxy_cols = list(closes.columns)
    rb_w = pd.DataFrame(0.0, index=rebal, columns=proxy_cols)
    weight_fn = top_k_relative(K)
    for rd in rebal:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0: continue
        # Map closes-index date to nearest signal date (signal indexed by
        # NYSE calendar; same here since regional ETFs use NYSE)
        if closes.index[prev_idx] not in signal.index:
            sig_at = signal.loc[:closes.index[prev_idx]].iloc[-1] if len(signal.loc[:closes.index[prev_idx]]) else None
        else:
            sig_at = signal.loc[closes.index[prev_idx]]
        if sig_at is None or sig_at.dropna().empty:
            continue
        w = weight_fn(sig_at)
        # Map index ticker weights to proxy ticker weights
        for idx_ticker, weight in w.items():
            if weight <= 0:
                continue
            proxy = proxy_for_index.get(idx_ticker)
            if proxy and proxy in proxy_cols:
                rb_w.loc[rd, proxy] = weight
    w_panel = rb_w.reindex(closes.index).ffill().fillna(0.0)
    w_panel.loc[w_panel.index < eligible] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (w_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = w_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    return (1.0 + port_ret).cumprod()


def main():
    print("Loading regional breadths from constituent caches ...")
    breadths = load_regional_breadths()
    print(f"\nBreadth panel: {breadths.shape[0]} dates x {breadths.shape[1]} indices")

    print("\nLoading regional proxy ETF prices ...")
    closes = load_proxy_prices()
    closes = closes.dropna()  # require all proxies available
    print(f"  Closes panel: {closes.shape[0]} dates x {closes.shape[1]} ETFs")

    # Re-index breadths onto closes' calendar (proxies use NYSE; constituents
    # may use various exchanges)
    breadths_aligned = breadths.reindex(closes.index, method="ffill")

    # Eligible start = 200d after the earliest common breadth date
    valid_per_idx = {col: breadths_aligned[col].first_valid_index()
                     for col in breadths_aligned.columns}
    latest_start = max(d for d in valid_per_idx.values() if d is not None)
    eligible_idx = closes.index.searchsorted(latest_start)
    eligible = closes.index[eligible_idx]
    print(f"  Eligible from {eligible.date()}")

    # Compute relative breadth signal
    rel = compute_relative_breadth(breadths_aligned)

    # Run rotation at K=2, K=3, K=4 to test sensitivity
    print(f"\n=== Standalone Phase 23 regional rotation (relative breadth) ===")
    print(f"  {'K':<3s}  " + "  ".join(f"{w[0]:<28s}" for w in WINDOWS))
    rotation_eqs = {}
    for k_test in [2, 3, 4]:
        eq = run_regional_rotation(closes, rel, eligible, k_test)
        eq = eq.loc[eq.index >= eligible]
        rotation_eqs[k_test] = eq
        cells = []
        for w in WINDOWS:
            s = _ws(eq, w[1], w[2])
            if s["sharpe"] is None: cells.append("n/a"); continue
            cells.append(f"Sh{s['sharpe']:+.3f} CAGR{s['cagr']*100:+5.1f}% "
                          f"Tot{s['total']*100:+5.1f}% DD{s['dd']*100:.1f}%")
        print(f"  K={k_test}  " + "  ".join(c.ljust(28) for c in cells))

    # Test blend impact: splice K=3 variant as 5th sleeve at various weights
    print(f"\n=== Blend impact (Phase 23 K=3 as 5th sleeve) ===")
    ov = json.loads((DATA_DIR / "risk_overlay.json").read_text(encoding="utf-8"))
    deployed = ov["gated_variants"]["blend_35_35_10_20_gated_eem_tilted"]
    base_eq = pd.Series(deployed["equity"], index=pd.to_datetime(deployed["dates"]))
    base_stats = {w[0]: _ws(base_eq, w[1], w[2]) for w in WINDOWS}
    print(f"  BASELINE (deployed gated + EEM-tilted blend):")
    for w in WINDOWS:
        s = base_stats[w[0]]
        if s["sharpe"] is None: continue
        print(f"    {w[0]:<14s}  Sharpe {s['sharpe']:+.3f}  Total {s['total']*100:+6.1f}%  DD {s['dd']*100:.1f}%")

    # Phase 23 K=3 spliced as 5th sleeve: w_new = (1-tilt)*existing + tilt*phase23
    # Funded proportionally (simplest baseline; matches Phase 22's first iteration)
    phase23_eq = rotation_eqs[3]
    common = base_eq.index.intersection(phase23_eq.index)
    if len(common) < 30:
        print(f"\n  WARN: Phase 23 eligible window starts {eligible.date()} "
              f"vs blend {base_eq.index[0].date()} — limited overlap")
    base_ret = base_eq.reindex(common).pct_change().fillna(0)
    p23_ret = phase23_eq.reindex(common).pct_change().fillna(0)
    print(f"\n  Phase 23 K=3 spliced at various weights (proportional funding):")
    print(f"  {'P23 wt':<8s}  " + "  ".join(f"{w[0]:<28s}" for w in WINDOWS))
    for wt in [0.05, 0.10, 0.15]:
        spliced_ret = (1.0 - wt) * base_ret + wt * p23_ret
        spliced_eq = (1.0 + spliced_ret).cumprod()
        cells = []
        for w in WINDOWS:
            s = _ws(spliced_eq, w[1], w[2]); b = base_stats[w[0]]
            if s["sharpe"] is None or b["sharpe"] is None:
                cells.append("n/a"); continue
            d_sh = s["sharpe"] - b["sharpe"]
            d_tot = (s["total"] - b["total"]) * 100
            cells.append(f"dSh{d_sh:+.3f} dTot{d_tot:+.1f}pp")
        print(f"  {int(wt*100):>4d}%  " + "  ".join(c.ljust(28) for c in cells))


if __name__ == "__main__":
    sys.exit(main())
