"""Phase 5 candidate correlation diagnostic.

Tests 11 US sub-sector / industry ETF candidates against the existing
Strategy C (thematic) universe and against the Strategy A sector slate
(via SPDR proxies) to identify which candidates are genuinely orthogonal.

Methodology (matches dashboard Test 12):
  - For each ETF, compute the Strategy C signal: distance above 200d MA,
    i.e. (close - MA200) / MA200
  - Resample to weekly Friday close (matches the rotation cadence)
  - Compute Pearson correlation matrix on the weekly signal series
  - For each candidate, find its MAX correlation with any existing
    holding (either Strategy C member or Strategy A sector cousin)
  - Survivors = candidates with max correlation < 0.85 (the threshold
    used for the IUIT pruning in Phase 1)

Output: scripts/data/phase5_correlation.json + console summary table.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 11 Phase 5 candidates from the user's screenshot
CANDIDATES = {
    "AMLP": "MLPs (energy infrastructure)",
    "PHO":  "Water Resources",
    "KRE":  "Regional Banks",
    "ITB":  "Homebuilders",
    "XOP":  "Oil & Gas E&P",
    "OIH":  "Oil Services",
    "XME":  "Metals & Mining",
    "IGV":  "Software",
    "KIE":  "Insurance",
    "XRT":  "Retail",
    "FDN":  "Internet",
}

# Current Strategy C universe (from run_thematic_rotation.py)
STRATEGY_C = [
    "ARKK", "CIBR", "SKYY", "BOTZ", "BLOK",   # Tech / Innovation
    "ICLN", "TAN", "LIT", "URA",               # Energy / Climate
    "XBI", "ARKG",                              # Health / Bio
    "JETS",                                     # Cyclical thematic
    "GDX", "COPX", "MOO",                       # Commodity equity
    "PAVE",                                     # Infrastructure
]

# Strategy A sector cousins (SPDR proxies for the iShares UK UCITS)
# Used to detect cases like IGV ~ CNDX (already covered in Strategy A)
STRATEGY_A_PROXIES = {
    "XLE": "Energy (Strategy A: IUES)",
    "XLF": "Financials (Strategy A: IUFS)",
    "XLV": "Health Care (Strategy A: IUHC)",
    "XLI": "Industrials (Strategy A: IUIS)",
    "XLP": "Cons Staples (Strategy A: IUCS)",
    "XLY": "Cons Disc (Strategy A: IUCD)",
    "XLU": "Utilities (Strategy A: IUUS)",
    "XLB": "Materials (Strategy A: IUMS)",
    "XLC": "Comm Svcs (Strategy A: IUCM)",
    "XLRE": "Real Estate (Strategy A: IUSP)",
    "QQQ": "Nasdaq-100 (Strategy A: CNDX)",
    "SPY": "S&P 500 (Strategy A: CSP1)",
    "SOXX": "Semis (Strategy A: SOXX)",
    "IJR": "Small Cap (Strategy A: IDP6)",
}

MA_PERIOD = 200
CORR_THRESHOLD = 0.85
START_DATE = "2018-01-01"
END_DATE = "2026-05-23"


def fetch_close(ticker: str) -> pd.Series:
    """Fetch daily adjusted close from yfinance. Cached parquet per ticker."""
    cache = PROJECT_ROOT / "data" / f"phase5_close_{ticker.lower().replace('.', '_')}.parquet"
    if cache.exists():
        s = pd.read_parquet(cache).iloc[:, 0]
        s.index = pd.to_datetime(s.index)
        return s
    try:
        df = yf.download(ticker, start=START_DATE, end=END_DATE,
                          auto_adjust=True, progress=False)
        if df.empty:
            return pd.Series(dtype=float, name=ticker)
        s = df["Close"]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        s.name = ticker
        s.to_frame(ticker).to_parquet(cache)
        return s
    except Exception as e:
        print(f"  ! {ticker}: fetch error {e}", file=sys.stderr)
        return pd.Series(dtype=float, name=ticker)


def compute_signal(closes: pd.Series, period: int = MA_PERIOD) -> pd.Series:
    """Strategy C signal: distance above 200d MA, (close - ma) / ma."""
    ma = closes.rolling(period, min_periods=period).mean()
    return (closes - ma) / ma


def main() -> int:
    all_tickers = list(CANDIDATES.keys()) + STRATEGY_C + list(STRATEGY_A_PROXIES.keys())
    print(f"Fetching {len(all_tickers)} tickers ...", flush=True)
    panel = {}
    for t in all_tickers:
        s = fetch_close(t)
        if len(s):
            panel[t] = s
        else:
            print(f"  ! {t}: no data", file=sys.stderr)
    closes = pd.DataFrame(panel).sort_index()
    print(f"  closes panel: {closes.shape}, "
          f"range {closes.index.min().date()} -> {closes.index.max().date()}")

    print("\nComputing signal (distance above 200d MA) ...")
    signals = closes.apply(compute_signal)

    # Resample to weekly Friday close
    weekly = signals.resample("W-FRI").last().dropna(how="all")
    weekly = weekly.dropna(axis=1, thresh=int(len(weekly) * 0.5))
    print(f"  weekly signal panel: {weekly.shape}, "
          f"range {weekly.index.min().date()} -> {weekly.index.max().date()}")

    # Drop NaN rows for fair correlation
    weekly_full = weekly.dropna()
    print(f"  full-coverage rows: {len(weekly_full)} "
          f"({weekly_full.index.min().date()} -> {weekly_full.index.max().date()})")

    print("\n" + "=" * 90)
    print("CORRELATION DIAGNOSTIC — each candidate vs existing universe")
    print("=" * 90)

    existing = [c for c in STRATEGY_C if c in weekly_full.columns] + \
               [c for c in STRATEGY_A_PROXIES if c in weekly_full.columns]

    results = []
    for cand in CANDIDATES:
        if cand not in weekly_full.columns:
            print(f"\n  {cand}: NO DATA — skipped")
            results.append({
                "candidate": cand,
                "label": CANDIDATES[cand],
                "max_corr": None,
                "max_corr_with": None,
                "verdict": "NO DATA",
            })
            continue
        cs = weekly_full[cand]
        max_corr = -np.inf
        max_corr_with = None
        top5 = []
        for ex in existing:
            if ex == cand:
                continue
            corr = cs.corr(weekly_full[ex])
            top5.append((ex, corr))
            if corr > max_corr:
                max_corr = corr
                max_corr_with = ex
        top5 = sorted(top5, key=lambda x: -x[1])[:5]
        passes = max_corr < CORR_THRESHOLD
        verdict = "PASS — orthogonal" if passes else f"FAIL — too correlated with {max_corr_with}"
        results.append({
            "candidate": cand,
            "label": CANDIDATES[cand],
            "max_corr": float(max_corr),
            "max_corr_with": max_corr_with,
            "max_corr_with_label": (
                "Strategy C: " + max_corr_with if max_corr_with in STRATEGY_C
                else STRATEGY_A_PROXIES.get(max_corr_with, max_corr_with)
            ),
            "top5": [(t, float(c)) for t, c in top5],
            "verdict": verdict,
        })
        print(f"\n  {cand} ({CANDIDATES[cand]})")
        print(f"    max corr: {max_corr:+.3f} with {max_corr_with} "
              f"({'Strategy C' if max_corr_with in STRATEGY_C else STRATEGY_A_PROXIES.get(max_corr_with, max_corr_with)})")
        print(f"    top 5 correlations:")
        for t, c in top5:
            cls = "Strategy C" if t in STRATEGY_C else STRATEGY_A_PROXIES.get(t, t)
            print(f"      {t:>5} ({cls:<35}): {c:+.3f}")
        print(f"    {verdict}")

    print("\n" + "=" * 90)
    print(f"SUMMARY — threshold = {CORR_THRESHOLD}")
    print("=" * 90)
    passes = [r for r in results if r["verdict"].startswith("PASS")]
    fails = [r for r in results if r["verdict"].startswith("FAIL")]
    nodata = [r for r in results if r["verdict"] == "NO DATA"]
    print(f"  PASS ({len(passes)}): {[r['candidate'] for r in passes]}")
    print(f"  FAIL ({len(fails)}): {[r['candidate'] for r in fails]}")
    if nodata:
        print(f"  NO DATA ({len(nodata)}): {[r['candidate'] for r in nodata]}")

    print(f"\n  Survivor list for Phase 5 Strategy C expansion:")
    for r in passes:
        print(f"    {r['candidate']:>5}  {r['label']:<35}  (max corr {r['max_corr']:+.3f} with {r['max_corr_with']})")

    import json
    out = {
        "computed_at": pd.Timestamp.utcnow().isoformat(),
        "ma_period": MA_PERIOD,
        "corr_threshold": CORR_THRESHOLD,
        "weekly_panel_window": f"{weekly_full.index.min().date()} -> {weekly_full.index.max().date()}",
        "n_weeks": len(weekly_full),
        "candidates_tested": len(CANDIDATES),
        "passes": [r['candidate'] for r in passes],
        "fails": [r['candidate'] for r in fails],
        "no_data": [r['candidate'] for r in nodata],
        "detail": results,
    }
    out_path = PROJECT_ROOT / "data" / "phase5_correlation.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
