"""Sub-industry vs sector vs thematic momentum bake-off.

Empirical answer to Eileen's question: "When forming the momentum
strategies, using themes/sub-industrial ETFs might be better than
sectors?"

This script runs an identical top-K-by-distance-above-200d-MA
rotation engine across three universes:

    (1) SECTORS — 14 US sector / broad-market proxy ETFs (mirrors
        the deployed Strategy A universe; uses US-listed equivalents
        of the iShares UCITS so we can use ETF momentum, not
        constituent breadth — apples-to-apples with the other two).
    (2) SUB-INDUSTRIES — 28 US sub-industry slicing ETFs (KIE,
        KRE, XME, XOP, OIH, ITA, JETS, XHB, XBI, IHI, XRT, ITB,
        IGV, FDN, IYW, REZ, REM, etc.). Designed to test the
        Moskowitz-Grinblatt (1999) "industry momentum > sector
        momentum" hypothesis at the ETF wrapper level.
    (3) THEMES — 23 thematic ETFs (mirrors Strategy C's universe,
        run on ETF momentum so the K and weighting can be set
        identically to the other two).

Sweep K ∈ {3, 5, 7, 10, 15, 20} on each universe, plus annual
walk-forward K refit (the same robustness check the deployed
strategies use). Output:

    data/subindustry_bakeoff.json (full results)
    Console summary table (one-page verdict)

Caveat on the apples-to-apples comparison: Strategy A as deployed
uses CONSTITUENT BREADTH (% of constituents above 200d MA), not
ETF-level momentum. This bake-off uses ETF-level momentum across
all three universes so the only thing varying is the universe
granularity, not the signal. That makes the comparison cleanly
interpretable but means universe (1) here is NOT identical to
the deployed Strategy A — it is a "Strategy A with ETF momentum
substituted" benchmark.

Run:
    python scripts/run_subindustry_bakeoff.py
"""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Universes — each list is what the rotation engine ranks over.
# Cash floor for all three is SHY (1-3y Treasury) to match the deployed
# convention; SHY is downloaded but never ranked as a momentum candidate.
# ---------------------------------------------------------------------------

# (1) Sectors — US-listed equivalents of Strategy A's iShares UCITS.
# Substitution map: CSP1->SPY, CNDX->QQQ, IDP6->IJR, IUES->XLE,
# IUFS->XLF, IUHC->XLV, IUIS->XLI, IUCS->XLP, IUCD->XLY, IUUS->XLU,
# IUMS->XLB, IUCM->XLC, IUSP->XLRE.
UNIVERSE_SECTORS = [
    "SPY", "QQQ", "IJR", "SOXX",
    "XLE", "XLF", "XLV", "XLI", "XLP", "XLY",
    "XLU", "XLB", "XLC", "XLRE",
]

# (2) Sub-industries — 28 US-listed sub-industry / industry ETFs.
# Deliberately chosen as GENUINE sub-industry slicings (KIE under XLF,
# KRE under XLF, XBI under XLV etc.) rather than secular themes.
# Note SOXX appears in both (1) and (2) because semis is both a major
# Strategy-A holding AND a tech sub-industry — kept in both for the
# direct comparison.
UNIVERSE_SUBINDUSTRIES = [
    # Financials sub-industries
    "KIE",   # SPDR Insurance
    "KRE",   # SPDR Regional Banks
    "KBE",   # SPDR Banks (broader)
    "IAI",   # iShares Broker-Dealers
    # Materials sub-industries
    "XME",   # SPDR Metals & Mining
    "GDX",   # VanEck Gold Miners
    "COPX",  # Global X Copper Miners
    "WOOD",  # iShares Timber & Forestry
    # Energy sub-industries
    "XOP",   # SPDR Oil & Gas E&P
    "OIH",   # VanEck Oil Services
    "AMLP",  # Alerian MLP (midstream pipelines)
    "TAN",   # Invesco Solar
    "ICLN",  # iShares Clean Energy
    "URA",   # Global X Uranium
    # Industrials sub-industries
    "ITA",   # iShares US Aerospace & Defense
    "JETS",  # Global X Airlines
    "IYT",   # iShares US Transportation
    "XHB",   # SPDR Homebuilders
    "PAVE",  # Global X US Infrastructure
    # Health Care sub-industries
    "XBI",   # SPDR S&P Biotech (equal-weight)
    "IHI",   # iShares US Medical Devices
    "IBB",   # iShares Biotech (cap-weighted)
    # Consumer sub-industries
    "XRT",   # SPDR Retail (equal-weight)
    "ITB",   # iShares US Home Construction
    # Tech sub-industries
    "SOXX",  # iShares Semiconductors
    "IGV",   # iShares Expanded Tech-Software
    "FDN",   # First Trust DJ Internet
    "IYW",   # iShares US Technology (broad)
    # REIT sub-industries
    "REZ",   # iShares Residential REIT
    "REM",   # iShares Mortgage REIT
]

# (3) Themes — mirrors Strategy C's universe, excluding 159801.SZ
# (CNY-denominated, would need FX handling not relevant to the
# core comparison). BTC-USD included since it is in the deployed C.
UNIVERSE_THEMES = [
    "ARKK", "CIBR", "SKYY", "BOTZ", "BLOK",
    "ICLN", "TAN", "LIT", "URA",
    "XBI", "ARKG", "JETS",
    "GDX", "COPX", "MOO", "XME", "WOOD", "REMX",
    "CQQQ",
    "PAVE", "ITA",
    "BTC-USD",
]

CASH_PROXY = "SHY"
MA_PERIOD = 200
COST_FRAC = 0.0005   # 5 bps per unit weight change
REBAL_FREQ = "W-FRI"
K_GRID = [3, 5, 7, 10, 15, 20]
START_DATE = "2011-01-01"   # leave 5y train room before 2016 eligible-start


# ---------------------------------------------------------------------------
# Engine — self-contained copies of the run_asset_class_rotation primitives
# so this script does not pick up Strategy B's module-level state.
# ---------------------------------------------------------------------------

def _safe(v):
    if v is None: return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
    return float(v)


def download_closes(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Daily adjusted-close DataFrame indexed by date, one column per ticker."""
    import yfinance as yf
    print(f"  Downloading {len(tickers)} tickers from {start} to {end} ...",
          flush=True)
    raw = yf.download(tickers, start=start, end=end,
                       auto_adjust=True, progress=False, threads=False,
                       group_by="ticker")
    out = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for t in tickers:
            if t in raw.columns.get_level_values(0):
                col = raw[t]["Close"] if "Close" in raw[t].columns else None
                if col is not None and not col.dropna().empty:
                    out[t] = col
    else:
        if "Close" in raw.columns:
            out[tickers[0]] = raw["Close"]
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    missing = [t for t in tickers if t not in df.columns
                or df[t].dropna().empty]
    if missing:
        print(f"  WARNING: no data for {missing}")
    return df


def compute_signal(closes: pd.DataFrame) -> pd.DataFrame:
    """Distance above own 200d MA per ETF: (close - MA200) / MA200."""
    ma = closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    return (closes - ma) / ma


def top_k_by_signal_factory(K: int):
    """Top-K-by-signal weight function. Drop negatives; cash floor in SHY."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        if len(valid) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index: w[CASH_PROXY] = 1.0
            return w
        candidates = valid[valid > 0]
        if CASH_PROXY in candidates.index:
            candidates = candidates.drop(CASH_PROXY)
        if len(candidates) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index: w[CASH_PROXY] = 1.0
            return w
        top = candidates.nlargest(min(K, len(candidates)))
        invested_share = len(top) / K
        normed = top / top.sum()
        w = pd.Series(0.0, index=s_row.index)
        w.loc[top.index] = normed * invested_share
        cash = 1.0 - invested_share
        if cash > 0 and CASH_PROXY in w.index:
            w[CASH_PROXY] = w.get(CASH_PROXY, 0.0) + cash
        return w
    return f


def run_rotation(closes: pd.DataFrame, signal: pd.DataFrame, K: int,
                  eligible_start: pd.Timestamp) -> dict:
    """Run weekly Friday rebalance, yesterday-signal -> today-trade."""
    weight_fn = top_k_by_signal_factory(K)
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
    eq = equity.loc[equity.index >= eligible_start].copy()
    if len(eq) < 5: return {}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    cagr = (eq.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    rolling_max = eq.cummax()
    max_dd = float(((eq - rolling_max) / rolling_max).min())
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(cagr),
        "max_dd": _safe(max_dd),
        "total_return": _safe(float(eq.iloc[-1]) - 1.0),
    }


def annual_turnover(weights: pd.DataFrame,
                     eligible_start: pd.Timestamp) -> float:
    wp = weights.loc[weights.index >= eligible_start]
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    return float(diff.sum() / n_years) if n_years > 0 else 0.0


def walk_forward_K(closes: pd.DataFrame, signal: pd.DataFrame,
                     eligible_start: pd.Timestamp,
                     initial_train_end: pd.Timestamp,
                     K_grid: list[int]) -> dict:
    """Annual K refit: each year, pick best K on expanding train window, apply
    forward. Same logic as run_asset_class_rotation.walk_forward_K."""
    last_date = closes.index[-1]
    refit_ends_target = pd.date_range(initial_train_end, last_date, freq="YE")
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_ends_target]
    refit_ends = [r for r in refit_ends if r >= eligible_start]
    if not refit_ends:
        return {}

    segments = []
    test_eq_pieces = []
    for i, train_end in enumerate(refit_ends):
        train_end_idx = closes.index.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(closes): break
        test_start = closes.index[test_start_idx]
        if test_start > test_end: continue
        best_K, best_sh = None, -1e9
        for K in K_grid:
            if K > len(closes.columns) - 1: continue   # -1 for cash proxy
            r = run_rotation(closes, signal, K, eligible_start)
            eq = r["equity"].loc[(r["equity"].index >= eligible_start)
                                  & (r["equity"].index <= train_end)]
            if len(eq) < 5: continue
            eq = eq / float(eq.iloc[0])
            d = eq.pct_change().fillna(0)
            sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0.0
            if sh > best_sh: best_sh, best_K = sh, K
        if best_K is None: continue
        r = run_rotation(closes, signal, best_K, eligible_start)
        full_eq = r["equity"]
        test_eq = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq / base_val
        d = test_eq.pct_change().fillna(0)
        test_sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0.0
        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_K": best_K,
            "train_sharpe": _safe(best_sh),
            "test_sharpe": _safe(test_sh),
            "n_test_days": int(len(test_eq)),
        })
        last_val = test_eq_pieces[-1].iloc[-1] if test_eq_pieces else 1.0
        test_eq_pieces.append(test_eq * last_val / test_eq.iloc[0])

    if not test_eq_pieces:
        return {}
    wf_equity = pd.concat(test_eq_pieces)
    d = wf_equity.pct_change().fillna(0)
    wf_sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0.0
    return {
        "segments": segments,
        "walk_forward_sharpe": _safe(wf_sh),
        "wf_first_date": wf_equity.index[0].strftime("%Y-%m-%d"),
        "wf_last_date": wf_equity.index[-1].strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# Bake-off
# ---------------------------------------------------------------------------

def effective_breadth(closes: pd.DataFrame) -> float:
    """Effective breadth via average pairwise correlation (Bera-Park style).

    Returns N_eff = N / (1 + (N-1) * rho_bar) where rho_bar is the mean
    off-diagonal pairwise correlation of daily returns. Approximates how
    many INDEPENDENT bets the universe really gives you, vs nominal N.
    """
    rets = closes.pct_change().dropna(how="all")
    if len(rets) < 30: return float("nan")
    corr = rets.corr()
    # Off-diagonal mean
    n = len(corr)
    mask = ~np.eye(n, dtype=bool)
    rho_bar = float(corr.where(mask).stack().mean())
    eff = n / (1.0 + (n - 1) * rho_bar)
    return eff


def run_universe(name: str, tickers: list[str], start: str, end: str) -> dict:
    print(f"\n=== {name.upper()} ===")
    closes = download_closes(tickers + [CASH_PROXY], start, end)
    print(f"  Loaded {len(closes.columns)} usable series, "
          f"date range {closes.index[0].date()} -> {closes.index[-1].date()}")
    # Eligible start = MA_PERIOD trading days after the LATEST first-valid
    # date among the universe (excluding cash). This is when ALL momentum
    # signals are computable.
    first_valid = []
    for c in closes.columns:
        if c == CASH_PROXY: continue
        fv = closes[c].first_valid_index()
        if fv is not None: first_valid.append(fv)
    if not first_valid:
        return {"error": "no valid series"}
    latest_first = max(first_valid)
    eligible_start = closes.index[
        closes.index.get_loc(latest_first) + MA_PERIOD]
    print(f"  Eligible start: {eligible_start.date()} "
          f"(latest first-valid {latest_first.date()} + {MA_PERIOD}d MA window)")

    # Effective breadth diagnostic
    eb = effective_breadth(closes[[c for c in closes.columns if c != CASH_PROXY]])
    print(f"  Effective breadth: {eb:.2f} (nominal {len(tickers)})")

    signal = compute_signal(closes)

    # In-sample sweep
    sweep = []
    for K in K_GRID:
        if K > len(tickers): continue
        r = run_rotation(closes, signal, K, eligible_start)
        st = compute_stats(r["equity"], eligible_start)
        st["K"] = K
        st["annual_turnover"] = annual_turnover(r["weights"], eligible_start)
        sweep.append(st)
        print(f"  K={K:>2}  Sharpe {st['sharpe']:+.2f}  "
              f"CAGR {st['cagr']*100:+5.1f}%  DD {st['max_dd']*100:5.1f}%  "
              f"Turn {st['annual_turnover']:5.1f}x")

    # Walk-forward K refit — initial train end = eligible_start + 5y
    initial_train_end = eligible_start + pd.DateOffset(years=5)
    if initial_train_end > closes.index[-1] - pd.DateOffset(years=1):
        print("  Insufficient history for walk-forward (need 5y train + 1y test)")
        wf = {}
    else:
        wf = walk_forward_K(closes, signal, eligible_start,
                             initial_train_end, K_GRID)
        if wf:
            print(f"  Walk-forward Sharpe: {wf['walk_forward_sharpe']:+.2f}  "
                  f"({wf['wf_first_date']} -> {wf['wf_last_date']}, "
                  f"{len(wf['segments'])} refit segments)")

    # Best-K stats (the IS best, for comparison with WF)
    best = max(sweep, key=lambda s: s["sharpe"] or -1e9) if sweep else None

    return {
        "universe_name": name,
        "n_tickers": len(tickers),
        "n_loaded": len([c for c in closes.columns if c != CASH_PROXY]),
        "effective_breadth": _safe(eb),
        "eligible_start": eligible_start.strftime("%Y-%m-%d"),
        "data_end": closes.index[-1].strftime("%Y-%m-%d"),
        "sweep": sweep,
        "best_K_in_sample": best,
        "walk_forward": wf,
    }


def main() -> int:
    today = pd.Timestamp.today().normalize()
    end = today.strftime("%Y-%m-%d")
    start = START_DATE

    print("=" * 72)
    print("SUB-INDUSTRY vs SECTOR vs THEME MOMENTUM BAKE-OFF")
    print("=" * 72)
    print(f"Engine:      top-K by (close - 200d MA) / MA200, drop negatives")
    print(f"Rebalance:   {REBAL_FREQ}  |  Cost: {COST_FRAC * 1e4:.0f} bps")
    print(f"Cash floor:  {CASH_PROXY}  |  K grid: {K_GRID}")
    print(f"Window:      {start} -> {end}")

    results = {
        "computed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "engine": {
            "signal": "(close - 200d MA) / MA200",
            "rebalance": REBAL_FREQ,
            "cost_bps_per_unit_weight": COST_FRAC * 1e4,
            "cash_proxy": CASH_PROXY,
            "K_grid": K_GRID,
            "start_date": start,
            "end_date": end,
        },
        "universes": {},
    }

    for name, tickers in [("sectors", UNIVERSE_SECTORS),
                           ("subindustries", UNIVERSE_SUBINDUSTRIES),
                           ("themes", UNIVERSE_THEMES)]:
        try:
            results["universes"][name] = run_universe(name, tickers, start, end)
        except Exception as exc:
            print(f"  ERROR running {name}: {exc}", file=sys.stderr)
            results["universes"][name] = {"error": str(exc)}

    # ----- Verdict --------------------------------------------------------
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)
    print(f"{'Universe':<18}{'N':>4}{'EffBr':>8}{'BestK':>7}"
          f"{'IS Sh':>8}{'WF Sh':>8}{'IS DD':>8}{'CAGR':>8}{'Turn':>7}")
    for name, r in results["universes"].items():
        if "error" in r:
            print(f"{name:<18}  ERROR: {r['error']}"); continue
        best = r["best_K_in_sample"] or {}
        wf = r["walk_forward"] or {}
        wf_sh = wf.get("walk_forward_sharpe")
        print(f"{name:<18}{r['n_loaded']:>4}"
              f"{r['effective_breadth']:>8.2f}"
              f"{best.get('K', '-'):>7}"
              f"{best.get('sharpe', 0):>8.2f}"
              f"{(wf_sh if wf_sh is not None else 0):>8.2f}"
              f"{best.get('max_dd', 0) * 100:>7.1f}%"
              f"{best.get('cagr', 0) * 100:>7.1f}%"
              f"{best.get('annual_turnover', 0):>6.1f}x")

    # Pairwise WF deltas — the answer to Eileen's question
    def _wf(name):
        r = results["universes"].get(name) or {}
        return (r.get("walk_forward") or {}).get("walk_forward_sharpe")
    sec, sub, thm = _wf("sectors"), _wf("subindustries"), _wf("themes")
    print()
    print("Pairwise walk-forward Sharpe deltas:")
    if sub is not None and sec is not None:
        d = sub - sec
        verdict = ("sub-industries WIN" if d > 0.10
                    else "sectors WIN" if d < -0.10
                    else "WITHIN NOISE (|delta| < 0.10)")
        print(f"  sub-industries vs sectors:  {d:+.2f}  ->  {verdict}")
    if thm is not None and sec is not None:
        d = thm - sec
        verdict = ("themes WIN" if d > 0.10
                    else "sectors WIN" if d < -0.10
                    else "WITHIN NOISE (|delta| < 0.10)")
        print(f"  themes vs sectors:          {d:+.2f}  ->  {verdict}")
    if thm is not None and sub is not None:
        d = thm - sub
        verdict = ("themes WIN" if d > 0.10
                    else "sub-industries WIN" if d < -0.10
                    else "WITHIN NOISE (|delta| < 0.10)")
        print(f"  themes vs sub-industries:   {d:+.2f}  ->  {verdict}")

    out_path = DATA_DIR / "subindustry_bakeoff.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(ROOT)} "
          f"({out_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
