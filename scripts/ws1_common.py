"""Workstream 1 shared helpers — MA-robustness experiments (review session 1).

Read-only with respect to deployed scripts: imports the deployed engines and
loaders, never modifies them. Experiment runners: run_ws1_ma_surface.py,
run_ws1_vol_variants.py. Outputs: data/ws1_*.json.

Three ways these backtests could be silently wrong, and the defences:
  1. LOOK-AHEAD — every portfolio run goes through the deployed engines
     (run_portfolio.run_portfolio / run_*_rotation.run_rotation), which use the
     PRIOR trading day's signal row for each rebalance and apply
     weights.shift(1) * returns. All new signal panels here are built from
     trailing rolling windows only.
  2. WINDOW INCONSISTENCY — variants with different lookbacks have different
     warm-ups. All stats are computed on ONE fixed common window
     (COMMON_START -> common end), asserted per run; a variant that is not
     fully defined by COMMON_START raises rather than silently shortening.
  3. COST / DRAG MIS-MODELLING — deployed per-sleeve one-way costs (A 2 /
     B 2 / C 5 / D 9 bps) are charged on absolute weight change inside the
     deployed engines; every variant is also run at 2x cost; Sleeve C prices
     come from the deployed loader (CNY->USD FX + expense-ratio drags kept);
     Sleeve D closes are FX-converted EUR->USD with the deployed converter.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))

from etf_registry import (  # noqa: E402
    get_etf, UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS,
)
from run_ma200_sweep import (  # noqa: E402
    align_breadth_to_index, compute_ma200_breadth, load_constituent_prices,
)
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402
from run_multi_strategy import fixed_blend_4way  # noqa: E402

# ---------------------------------------------------------------------------
# Deployed sleeve configuration (verified in RESEARCH_MEMO.md, Workstream 0)
# ---------------------------------------------------------------------------
COST_A = 2 / 10_000   # run_topk_robustness.py:53
COST_B = 2 / 10_000   # run_asset_class_rotation.py:126
COST_C = 5 / 10_000   # run_thematic_rotation.py:295
COST_D = 9 / 10_000   # run_europe_rotation.py:55
K_A, K_B, K_C, K_D = 7, 7, 5, 3
BLEND_W = (0.35, 0.35, 0.10)          # A, B, C; D = 0.20 residual
REBAL = "W-FRI"

# Fixed common evaluation window. Start = deployed blend common start
# (multi_strategy.json common_start 2018-11-08); end = intersection of sleeve
# panels, computed at load time. Split date follows run_split_half.py.
COMMON_START = pd.Timestamp("2018-11-08")
SPLIT_DATE = pd.Timestamp("2022-09-08")

# Sub-period grid — copied from run_robustness.py:68-76 (single source cited;
# copied rather than imported to avoid that module's heavy import chain).
SUB_PERIODS = [
    ("2019_pre_covid",       "2019-01-01", "2020-02-19"),
    ("2020_covid_recovery",  "2020-02-19", "2021-01-01"),
    ("2021_rally",           "2021-01-01", "2022-01-01"),
    ("2022_inflation_shock", "2022-01-01", "2023-01-01"),
    ("2023_ai_rally",        "2023-01-01", "2024-01-01"),
    ("2024_25_recent",       "2024-01-01", "2026-01-01"),
    ("2026_ytd",             "2026-01-01", "2026-12-31"),
]
# The 6 "full" regimes used for the >=4-of-6 consistency bar (2026_ytd is a
# half-year stub, reported but not counted).
N_CONSISTENCY_PERIODS = 6

VOL_WIN = 63          # trailing realised-vol window (days), fixed ex-ante
VOL_MIN_PERIODS = 40  # tolerate sparse non-US prints
ENSEMBLE_WINDOWS = [50, 100, 150, 200]


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


# ---------------------------------------------------------------------------
# Panel loaders (offline: parquet caches written by the weekly refresh)
# ---------------------------------------------------------------------------

def _proxy_close_from_cache(etf: str) -> pd.Series:
    """Trading-proxy close from the committed *_ohlc_cache.parquet files.
    Deliberately does NOT call backtest.download_soxx_ohlc: its range check
    would trigger a needless network refetch for a 2-day gap. The caches are
    the exact series the deployed track was built from."""
    cfg = get_etf(etf)
    proxy = (cfg.get("yfinance_trading_proxy") or etf)
    cache = DATA / f"{proxy.lower()}_ohlc_cache.parquet"
    if not cache.exists():
        raise FileNotFoundError(f"missing OHLC cache for {etf} proxy {proxy}")
    ohlc = pd.read_parquet(cache)
    close = ohlc["Close"].astype(float)
    return close[~close.index.duplicated(keep="first")].sort_index()


def load_sleeve_a() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """(proxy closes panel, {etf: constituent price frame}) for Sleeve A."""
    closes, cons = {}, {}
    for etf in UNIVERSE_ETFS:
        cons[etf] = load_constituent_prices(etf)
        closes[etf] = _proxy_close_from_cache(etf)
    return pd.DataFrame(closes).sort_index(), cons


def load_sleeve_d() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """(USD closes panel, constituent frames) for Sleeve D. EUR->USD via the
    deployed converter (run_europe_rotation.py:128); FX series cached locally
    so reruns are offline."""
    closes_eur, cons = {}, {}
    for etf in UNIVERSE_EUROPE_SECTORS:
        cons[etf] = load_constituent_prices(etf)
        closes_eur[etf] = _proxy_close_from_cache(etf)
    closes_eur = pd.DataFrame(closes_eur).sort_index()
    fx_cache = DATA / "ws1_fx_eurusd_cache.parquet"
    if fx_cache.exists():
        fx = pd.read_parquet(fx_cache)["EURUSD"]
        fx = fx.reindex(closes_eur.index, method="ffill").bfill()
        closes = closes_eur.multiply(fx, axis=0)
    else:
        from run_europe_rotation import _fx_convert_eur_to_usd
        closes = _fx_convert_eur_to_usd(closes_eur)
        implied = (closes.iloc[:, 0] / closes_eur.iloc[:, 0]).rename("EURUSD")
        implied.to_frame().to_parquet(fx_cache)
    return closes, cons


def load_sleeve_b() -> pd.DataFrame:
    return B_engine.download_prices()


def load_sleeve_c() -> pd.DataFrame:
    return C_engine.download_prices()


# ---------------------------------------------------------------------------
# Signal builders
# ---------------------------------------------------------------------------

def breadth_panel(cons: dict[str, pd.DataFrame], index: pd.DatetimeIndex,
                  period: int) -> pd.DataFrame:
    """Share of constituents above their own `period`-day MA, per ETF,
    aligned to `index` with the deployed 7-day stale cap."""
    out = {}
    for etf, cp in cons.items():
        b = compute_ma200_breadth(cp, period=period)
        out[etf] = align_breadth_to_index(b, index)
    return pd.DataFrame(out)


def graded_breadth_panel(cons: dict[str, pd.DataFrame], index: pd.DatetimeIndex,
                         period: int, vol_normalised: bool) -> pd.DataFrame:
    """Graded breadth: cross-constituent MEAN of per-constituent distance to
    its `period`-day MA — vol-normalised z if requested — instead of the
    binary above/below share. Denominator = constituents with a valid value.
    """
    out = {}
    for etf, cp in cons.items():
        min_p = max(1, int(period * 0.9))
        ma = cp.rolling(period, min_periods=min_p).mean()
        dist = cp / ma - 1.0
        if vol_normalised:
            sigma = cp.pct_change().rolling(VOL_WIN,
                                            min_periods=VOL_MIN_PERIODS).std()
            z = dist / (sigma * math.sqrt(period))
            z = z.replace([np.inf, -np.inf], np.nan)
        else:
            z = dist
        g = z.mean(axis=1, skipna=True)
        g[z.notna().sum(axis=1) == 0] = np.nan
        out[etf] = align_breadth_to_index(g, index)
    return pd.DataFrame(out)


def distance_signal(closes: pd.DataFrame, period: int) -> pd.DataFrame:
    """(close - MA)/MA — the deployed B/C signal at an arbitrary period."""
    ma = closes.rolling(period, min_periods=period).mean()
    return (closes - ma) / ma


def vol_norm_signal(closes: pd.DataFrame, period: int) -> pd.DataFrame:
    """(close/MA - 1) / (sigma_63d * sqrt(period)). sigma in daily units.
    sqrt(period) puts different horizons on a comparable diffusion scale.
    Fixed ex-ante; deliberately NOT tuned (zero knobs)."""
    dist = distance_signal(closes, period)
    sigma = closes.pct_change().rolling(VOL_WIN,
                                        min_periods=VOL_MIN_PERIODS).std()
    z = dist / (sigma * math.sqrt(period))
    return z.replace([np.inf, -np.inf], np.nan)


def ensemble_mean(panels: list[pd.DataFrame]) -> pd.DataFrame:
    """Equal-weight mean across horizon panels. NaN until ALL members are
    defined, so short-lookback data cannot masquerade as an ensemble reading
    during the long-lookback warm-up (pandas + propagates NaN)."""
    out = panels[0].copy()
    for p in panels[1:]:
        out = out + p
    return out / len(panels)


def relative(panel: pd.DataFrame) -> pd.DataFrame:
    """Sleeve A cross-sectional demean (run_topk_robustness.py:82)."""
    return panel.sub(panel.mean(axis=1, skipna=True), axis=0)


# ---------------------------------------------------------------------------
# Sleeve C weight function with decoupled eligibility vs ranking
# ---------------------------------------------------------------------------

def c_rank_decoupled_weighter(K: int, raw200: pd.DataFrame,
                              floor: float, gate_threshold: float):
    """Sleeve C weighter for ranking-signal variants. Eligibility and the
    Phase 27 sleeve gate stay EXACTLY as deployed (raw 200d distance vs the
    +5% floor; gate = share of universe above floor < 30% -> all to SHY);
    only the RANKING among eligible names comes from the variant panel.
    Zero new knobs: floor/gate untouched, rank signal swapped.

    The engine passes the variant-signal row; the raw-200d row is looked up
    by the row's own timestamp (same PRIOR-day row the engine chose)."""
    cash = C_engine.CASH_PROXY

    def f(s_row: pd.Series) -> pd.Series:
        w = pd.Series(0.0, index=s_row.index)
        dt = s_row.name
        raw_row = raw200.loc[dt] if dt in raw200.index else None
        if raw_row is None:
            if cash in w.index:
                w[cash] = 1.0
            return w
        univ = raw_row.drop(cash, errors="ignore").dropna()
        if len(univ) == 0:
            if cash in w.index:
                w[cash] = 1.0
            return w
        sleeve_breadth = float((univ > floor).mean())
        if sleeve_breadth < gate_threshold:
            if cash in w.index:
                w[cash] = 1.0
            return w
        eligible_names = univ[univ > floor].index
        rank_vals = s_row.reindex(eligible_names).dropna()
        if len(rank_vals) == 0:
            if cash in w.index:
                w[cash] = 1.0
            return w
        top = rank_vals.nlargest(min(K, len(rank_vals)))
        invested = len(top) / K
        w.loc[top.index] = invested / len(top)
        if invested < 1.0 and cash in w.index:
            w[cash] = w.get(cash, 0.0) + (1.0 - invested)
        return w

    return f


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def window_stats(equity: pd.Series, start: pd.Timestamp,
                 end: pd.Timestamp | None = None) -> dict:
    eq = equity.loc[equity.index >= start]
    if end is not None:
        eq = eq.loc[eq.index <= end]
    if len(eq) < 10:
        return {"sharpe": None, "cagr": None, "total_return": None,
                "max_dd": None, "n_days": int(len(eq))}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    dd = (eq - eq.cummax()) / eq.cummax()
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(float(eq.iloc[-1]) ** (1.0 / n_years) - 1.0
                      if n_years > 0 else 0.0),
        "total_return": _safe(float(eq.iloc[-1] - 1.0)),
        "max_dd": _safe(float(dd.min())),
        "n_days": int(len(eq)),
    }


def sub_period_sharpes(equity: pd.Series) -> dict:
    out = {}
    for label, s, e in SUB_PERIODS:
        eq = equity.loc[(equity.index >= pd.Timestamp(s))
                        & (equity.index < pd.Timestamp(e))]
        if len(eq) < 20:
            out[label] = None
            continue
        daily = (eq / eq.iloc[0]).pct_change().fillna(0)
        out[label] = _safe(daily.mean() / daily.std() * math.sqrt(252)
                           if daily.std() > 0 else 0.0)
    return out


def annual_turnover(weights: pd.DataFrame, start: pd.Timestamp) -> float:
    wp = weights.loc[weights.index >= start]
    if len(wp) < 2:
        return 0.0
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    return float(diff.sum() / n_years) if n_years > 0 else 0.0


def full_report(equity: pd.Series, weights: pd.DataFrame | None,
                common_start: pd.Timestamp, common_end: pd.Timestamp) -> dict:
    """Full / train / test / sub-period stats on the FIXED window."""
    eq = equity.loc[(equity.index >= common_start)
                    & (equity.index <= common_end)]
    assert len(eq) > 250, "variant not defined on the common window"
    assert eq.index[0] <= common_start + pd.Timedelta(days=7), (
        f"variant starts late: {eq.index[0]} vs {common_start}")
    rep = {
        "full": window_stats(eq, common_start, common_end),
        "train": window_stats(eq, common_start, SPLIT_DATE),
        "test": window_stats(eq, SPLIT_DATE, common_end),
        "sub_period_sharpe": sub_period_sharpes(eq),
    }
    if weights is not None:
        rep["annual_turnover"] = _safe(annual_turnover(weights, common_start))
    return rep


def blend_equity(eq_a: pd.Series, eq_b: pd.Series, eq_c: pd.Series,
                 eq_d: pd.Series, common_start: pd.Timestamp,
                 common_end: pd.Timestamp) -> pd.Series:
    idx = (eq_a.index.intersection(eq_b.index)
           .intersection(eq_c.index).intersection(eq_d.index))
    idx = idx[(idx >= common_start) & (idx <= common_end)]
    norm = [s.loc[idx] / s.loc[idx].iloc[0] for s in (eq_a, eq_b, eq_c, eq_d)]
    return fixed_blend_4way(norm[0], norm[1], norm[2], norm[3], *BLEND_W)


def consistency_count(variant_sub: dict, baseline_sub: dict) -> int:
    """In how many of the 6 full regimes does the variant match or beat the
    baseline Sharpe? (2026_ytd stub excluded.)"""
    n = 0
    for label, _, _ in SUB_PERIODS[:N_CONSISTENCY_PERIODS]:
        v, b = variant_sub.get(label), baseline_sub.get(label)
        if v is not None and b is not None and v >= b:
            n += 1
    return n


def write_json(path: Path, payload: dict) -> None:
    def clean(o):
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else round(o, 6)
        if isinstance(o, (np.floating,)):
            return clean(float(o))
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(x) for x in o]
        return o
    path.write_text(json.dumps(clean(payload), indent=1), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
