"""Strategy B — asset-class rotation (Phase 2).

Where Strategy A (top-K-by-breadth, see run_topk_robustness.py) operates
within US equities by ranking the SECTOR ETFs on constituent-level breadth,
Strategy B operates ACROSS asset classes by ranking BROAD asset-class ETFs
on their own price-level momentum.

Universe (12 US-listed broad-asset ETFs — clean yfinance pricing, all with
long histories back to at least 2007-2010):

  US equity      :  SPY (large), IJR (small), QQQ (NASDAQ-100 tech)
  Intl developed :  EFA (MSCI EAFE), VGK (Europe), EWJ (Japan)
  Real estate    :  VNQ (US REITs broad)
  Commodities    :  GLD (gold), DBC (broad commodities)
  Bonds (Tsy)    :  TLT (20+y Treasury), IEF (7-10y Treasury), TIP (TIPS)

  (Phase 24 2026-05-28: HYG removed — behaviourally an equity-correlated
  credit instrument, never a real defensive diversifier. Pareto-clean
  improvement on Sharpe + Total + DD at blend level.)
  (Phase 29 2026-07-02: EEM moved to overlay-only — the WS2 review found
  EEM double-counted between B's rotation and the Phase 22 tilt; EM
  exposure is now expressed solely by the tilt. See the UNIVERSE comment
  below and reviews/2026-07-02_ws2_universe.docx.)

Signal: distance above own 200-day moving average per ETF
        signal_i = (close_i - MA200_i) / MA200_i
The signal is well-defined for any time series (equity, bond, commodity)
and has a clean regime-following interpretation: positive means the asset
is above its long-term trend, negative means below.

Trading rules:
  - Rank the 12 ETFs each Friday close by current signal.
  - Hold the top K with positive signal. ETFs with negative signal (below
    their 200d MA) are EXCLUDED — unlike Strategy A which is always 100%
    invested, this strategy has a built-in cash floor when broadly weak.
  - Weight each held ETF by its signal share among the survivors.
  - Idle (non-held) capital sits in SHY (1-3y Treasury) as cash proxy —
    earns t-bill carry without the duration risk of IEF. SHY is OUT of
    the rotation candidate set (cash-only) so its low signal magnitude
    cannot accidentally crowd out a momentum pick.
    (Phase 19.1, 2026-05-27: switched from IEF to SHY after attribution
    showed B held IEF 92% of 2022 days as cash floor with IEF at -15%.)
  - 2 bps one-way per unit weight change (Phase 12 calibration; COST_BPS
    below is the number that runs — this line said 5 bps until 2026-09-03).

Benchmarks:
  - SPY buy-and-hold (single passive equity)
  - Equal-weight across the rotation universe + cash line (no signal)
  - 60/40 (SPY/AGG): rebalance weekly to 60% SPY, 40% AGG — the
    conventional balanced portfolio.

Output: data/asset_class_rotation.json
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PRICE_CACHE = DATA_DIR / "asset_class_prices_cache.parquet"
OUT_PATH = DATA_DIR / "asset_class_rotation.json"
# The source download_prices() actually priced this run on; written into the
# payload so the artefact states its own basis (2026-09-03).
EFFECTIVE_PRICE_SOURCE: str | None = None

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from rebalance_calendar import engine_rebalance_dates  # noqa: E402
from rebalance_records import latest_rebalance_record  # noqa: E402
import price_source as price_source_mod  # noqa: E402
import vendor_tail  # noqa: E402
from nyse_sessions import (  # noqa: E402
    cap_to_last_completed_session,
    last_completed_session,
    yf_fetch_end,
)
from price_panel_guard import (
    ma_distance_signal,  # noqa: E402
    assert_attribution_sane, assert_decision_session_present,
    assert_panel_usable, fetched_panel_is_worse,
)

sys.stdout.reconfigure(encoding="utf-8")


# =========================================================================
# Asset-class universe
# =========================================================================
UNIVERSE: dict[str, dict] = {
    # US equity
    "SPY":  {"label": "S&P 500 (US large)",         "asset_class": "US Equity"},
    "IJR":  {"label": "S&P 600 (US small)",         "asset_class": "US Equity"},
    "QQQ":  {"label": "NASDAQ-100 (US tech-heavy)", "asset_class": "US Equity"},
    # International developed
    "EFA":  {"label": "MSCI EAFE (Intl developed)", "asset_class": "Intl Developed"},
    "VGK":  {"label": "FTSE Europe",                "asset_class": "Intl Developed"},
    "EWJ":  {"label": "MSCI Japan",                 "asset_class": "Intl Developed"},
    # Emerging markets — EEM REMOVED in Phase 29 (2026-07-02, approved).
    # WS2 review finding: EEM was double-counted — a Strategy B rotation
    # member AND the Phase 22 overlay instrument. Look-through EEM peaked
    # at 15.0% of NAV with both roles holding it simultaneously on 26% of
    # days (11 tilt switches ever). The 2x2 role ablation on the fixed
    # window put all four cells within 0.009 full-window Sharpe —
    # statistically indistinguishable — so the decision is architectural:
    # ONE role, the Phase 22 overlay. Strategy B standalone is no worse
    # without EEM (+1.02 vs +1.01). The overlap was NOT rare (corrected
    # 2026-07-03): B held EEM on 45% of days (mean 12% of the book when
    # held), and on 88% of tilt-ON days BOTH routes held it together —
    # exactly the stacking the ablation shows added nothing. EEM exposure
    # is now expressed ONLY via the Phase 22 EEM/SPY golden-cross tilt in
    # run_risk_overlay.py.
    # Evidence: data/ws2_eem_coherence.json; record
    # reviews/2026-07-02_ws2_universe.docx sections 3.5 and 4.1.
    # Real estate
    "VNQ":  {"label": "US Real Estate (REITs)",     "asset_class": "Real Estate"},
    # Commodities
    "GLD":  {"label": "Gold",                       "asset_class": "Commodities"},
    "DBC":  {"label": "Broad Commodities",          "asset_class": "Commodities"},
    # Phase 16 (2026-05-26) — SLV was tested as an addition but REVERTED
    # before commit. Empirical result: dragged Strategy B common-window
    # Sharpe from +0.99 to +0.81 (-0.18) AND widened max DD from -14% to
    # -27% (+13pp worse). The correlation gate passed (0.78 vs GLD) but
    # silver's chop-then-reverse momentum profile poisons a top-K signal
    # the way gold's smoother trend behaviour does not. Documented as a
    # lesson: corr gate is necessary but not sufficient for a momentum
    # universe — the asset's signal-to-noise character matters too.
    # Bonds — Treasuries only after Phase 24 (2026-05-28).
    # HYG (high-yield credit) was REMOVED on 2026-05-28 because it is a
    # nominally-classified bond but behaviourally an equity-correlated
    # credit instrument: it crashed -25% in 2008 alongside equities and
    # -22% in 2020. It was a "fake" diversifier — never provided
    # defensive value when it was actually needed.
    # Empirical (HYG-only drop, blend-level, Phase 19-gated):
    #   Full Sharpe +0.003, Total +0.7pp, DD +0.1pp BETTER
    #   2022 single year: Sharpe +0.013, Total +0.2pp
    #   2022-onwards: Sharpe +0.004, Total +0.4pp, DD flat
    # Pareto-clean — every metric improves or stays flat. Full
    # Treasury defensive coverage (TLT 20+y, IEF 7-10y, TIP) retained
    # for deflationary regime insurance.
    "TLT":  {"label": "20+y Treasury (long dur)",   "asset_class": "Bonds"},
    "IEF":  {"label": "7-10y Treasury (interm)",    "asset_class": "Bonds"},
    "TIP":  {"label": "TIPS (inflation-linked)",    "asset_class": "Bonds"},
}
TICKERS = list(UNIVERSE.keys())

START_DATE = "2007-01-01"  # earliest common start across the universe
# yfinance's `end` is EXCLUSIVE — end=today drops today's completed
# close. The Friday 2026-07-17 22:00 UTC CI run captured this sleeve
# only through Thursday that way, and the factsheet shipped without the
# Friday rebalance. Fetch padded 2 days ahead; download_prices() then
# caps the panel at the last completed NYSE session so a mid-session
# run cannot ingest a partial bar either.
END_DATE   = yf_fetch_end()

MA_PERIOD = 200
# Phase 12 cost calibration: Strategy B trades 12 broad-asset ETFs
# (SPY, IJR, QQQ, EFA, VGK, EWJ, VNQ, GLD, DBC, TLT, IEF, TIP) after
# the Phase 24 HYG removal and the Phase 29 EEM move to overlay-only.
# These are among the most liquid ETFs in the world — bid-ask typically
# 0.5-2 bps. Realistic blended one-way cost: ~2 bps (was uniform 5).
COST_BPS = 2
# Venue this sleeve trades on, for the holiday-aware rebalance rule.
CALENDAR = "NYSE"
COST_FRAC = COST_BPS / 10_000

K_GRID = [3, 4, 5, 6, 7]
# WS18 (2026-08-22): "Weekly Mon" is the DEPLOYED cell and must be present,
# because main() captures headline_payload by matching HEADLINE_FREQ_NAME
# against this grid. Changing the constant without adding the cell left the
# payload None and every engine died on it — loudly, which was the right
# failure, but the grid is the other half of the same decision.
# "Weekly Fri" is KEPT as a comparison rather than replaced: after a cadence
# move the incumbent is the single most useful row in this table.
REBAL_FREQS = [
    ("Daily",         "D"),
    ("Weekly Mon",    "W-MON"),
    ("Weekly Fri",    "W-FRI"),
    ("Bi-weekly Fri", "2W-FRI"),
    ("Month-end",     "BME"),
]
HEADLINE_K = 7
HEADLINE_FREQ_NAME = "Weekly Mon"
# WS18 (2026-08-22): the whole book moved to a Monday rebalance so
# every sleeve ranks at rd-1. Under the Friday cadence sleeve D could
# only reach rd-2 - the European data is a session late at every hour
# of the decision window - so the live book could not implement what
# this engine backtests, for 20% of NAV, weekly.
HEADLINE_FREQ = "W-MON"

# ETF used as a cash proxy when fewer than K ETFs have positive signal.
# Phase 19.1 (2026-05-27): switched from IEF (7-10y, ~7y duration) to SHY
# (1-3y, ~1.8y duration). IEF stays in TICKERS as a rotation candidate
# (legitimate duration play); SHY is added separately as a cash-only
# vehicle (downloaded but excluded from the candidate set). The 2022
# inflation crash showed IEF's duration risk turns the cash floor into a
# correlated drawdown — SHY is duration-neutral cash.
CASH_PROXY = "SHY"
# Tickers downloaded purely for use as cash floor; NOT eligible to be
# picked by the rotation. Excluded from the candidate set in the weight
# function and from per-ETF iteration where appropriate.
CASH_ONLY_TICKERS = ["SHY"]


# =========================================================================
# Stable per-ETF colour palette (used by the dashboard's stacked-area chart)
# =========================================================================
ASSET_CLASS_COLOURS = {
    "SPY":  "#374151",  "IJR":  "#1e3a8a",  "QQQ":  "#7c3aed",
    "EFA":  "#0e7490",  "VGK":  "#0891b2",  "EWJ":  "#be185d",
    "EEM":  "#dc2626",  # retained: old payloads + the Phase 22 tilt row
                        # on the dashboard still render EEM (Phase 29
                        # removed it from the rotation universe only)
    "VNQ":  "#0d9488",
    "GLD":  "#ca8a04",  "DBC":  "#92400e",
    "TLT":  "#1d7a3a",  "IEF":  "#65a30d",  "TIP":  "#a16207",
    "SHY":  "#6b727a",  # cash proxy (Phase 19.1 — 1-3y Treasury)
    "HYG":  "#52525b",  # retained for backward compatibility (old payloads;
                        # Phase 24 removed HYG from active universe)
}


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def round_series(values, ndigits=4):
    out = []
    for v in values:
        try:
            f = float(v)
            out.append(round(f, ndigits) if not (math.isnan(f) or math.isinf(f)) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


# =========================================================================
# Data loading
# =========================================================================
def download_prices() -> pd.DataFrame:
    """Download adjusted-close prices for the asset-class universe.

    Parquet cache at data/asset_class_prices_cache.parquet. Reused only
    when current through the last COMPLETED NYSE session — the previous
    "<= 7 calendar days stale" rule could serve a days-old panel to an
    ad-hoc midweek run, silently rebalancing on stale closes.
    """
    needed = TICKERS + CASH_ONLY_TICKERS
    current_through = last_completed_session(datetime.now(timezone.utc))
    # Resolved FIRST, so a request for Norgate that cannot be met fails here
    # rather than after a silent fallback (price_source.py, 2026-09-03).
    price_source, why = price_source_mod.resolve_source(
        price_source_mod.requested_source())
    print(f"  price source: {price_source} ({why})", flush=True)
    global EFFECTIVE_PRICE_SOURCE
    EFFECTIVE_PRICE_SOURCE = price_source
    cached = None
    cache_source = None
    if PRICE_CACHE.exists():
        cached = pd.read_parquet(PRICE_CACHE)
        # An EMPTY cache must read as "no usable cache", not explode. A
        # zero-row frame gives index.max() = NaT, and comparing NaT to a date
        # raises TypeError — which is how a dropped connection on 2026-08-26
        # took down Strategies B and C on the NEXT run rather than on the one
        # that broke the file.
        # The cache is only as current as its LEAST current column, and that
        # has to be measured on values rather than on the index (2026-08-31).
        # cached.index.max() reads the union of every column's dates, so one
        # line lagging behind twelve others is invisible: on 2026-08-31 the
        # index reached Friday because 12 of 13 tickers did, while SPY was NaN
        # there, and this branch handed back the short frame instead of
        # refetching. The sleeve then published a session behind and capture
        # integrity failed the run — twice, because the second attempt hit the
        # same short-circuit. Index-versus-values is the same confusion that
        # cost this repo the 2026-08-29 weekend; it is worth being explicit
        # about wherever a date is read.
        # One definition since 2026-09-06, shared with sleeve C, which had
        # kept the index-based read and reused a cache with BTC-USD blank on
        # the Friday row (vendor_tail.cache_current_through). Measured over
        # the names this run needs: a column left behind by a universe change
        # cannot force a refresh forever.
        cache_end = vendor_tail.cache_current_through(cached, needed)
        cached_universe = set(cached.columns)
        cache_source = price_source_mod.read_cache_source(PRICE_CACHE)
        if cache_end is None:
            print(f"  Cache at {PRICE_CACHE.name} is EMPTY — re-downloading")
        elif cache_end >= current_through and set(needed).issubset(cached_universe):
            # A current cache built from the OTHER source is not this run's
            # cache. WS19 found the Norgate switch vacuous for exactly this
            # reason: the reuse branch returned before the selection ran.
            if price_source_mod.cache_matches(cache_source, price_source):
                print(f"  Using cached prices ({cached.index.min().date()} -> "
                      f"{cache_end}, current through {current_through}, "
                      f"built from {cache_source or 'yfinance (unrecorded)'})")
                return cached[needed]
            print(f"  Cache is current but was built from "
                  f"{cache_source or 'yfinance (unrecorded)'}; this run is on "
                  f"{price_source} — refreshing")
        else:
            print(f"  Cache ends {cache_end} < last completed session "
                  f"{current_through}, or universe expanded — refreshing")

    print(f"  Downloading {len(needed)} tickers from yfinance "
          f"({START_DATE} -> {END_DATE})"
          + (" as the base frame; Norgate column selection follows"
             if price_source == "norgate" else "") + " ...", flush=True)
    raw = yf.download(needed, start=START_DATE, end=END_DATE, auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    # Result has MultiIndex columns (ticker, field). Extract Close per ticker.
    closes = {}
    for t in needed:
        if (t, "Close") in raw.columns:
            closes[t] = raw[(t, "Close")]
        elif "Close" in raw.columns:
            closes[t] = raw["Close"]
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index().dropna(how="all")

    # ----- Norgate price source, opt-in (2026-08-30) -----
    # Set BTE_PRICE_SOURCE=norgate to prefer the locally licensed feed for the
    # US-listed lines this sleeve trades. Default is yfinance, i.e. deployed
    # behaviour unchanged, and every CI runner stays on it because no runner
    # has the feed.
    #
    # WHY IT EXISTS: over 2026-08-28/30 yfinance withheld Friday's closes on
    # every probed line for more than 43 hours, having served them once and
    # retracted them, and no fetch shape recovered them. Norgate carried
    # Friday throughout. This sleeve is entirely US-listed ETFs, which Norgate
    # covers completely.
    #
    # The selection is the WS19b superset rule: a column is taken whole or not
    # at all, never spliced, because filling one source's gaps from the other
    # fabricates returns at each junction. Placed here, BEFORE the partial-bar
    # cap and the degenerate-write guard, so both still vet the final frame —
    # the rule's contract allows row-level and whole-frame steps afterwards
    # and forbids only per-cell fills.
    #
    # BASIS: measured 2026-08-30 on five of this sleeve's own ETFs, each over
    # 125 sessions spanning a dividend ex-date, yfinance-adjusted against
    # Norgate: worst deviation 6.3e-5 (TLT, XLF, IJR, XLU, EEM). At ETF level
    # the two feeds are interchangeable, which is NOT true at constituent
    # level — see WS19 on AZN.
    _ngrep = None
    if price_source == "norgate":
        import norgate_prices
        df, _ngrep = norgate_prices.select_columns(
            df, list(df.columns), START_DATE, END_DATE, label="Strategy B ")
        # REACHABLE IS NOT SERVING (2026-09-03). The preflight only proved
        # the service answers; a strict run must also have TAKEN every US
        # line, or it would record a yfinance frame as Norgate-built.
        price_source_mod.assert_norgate_complete(_ngrep, needed, "Strategy B")

    # ----- Blank tail cells (2026-09-06) -----
    # Same step as Strategy C, same reasoning (vendor_tail): a name's newest
    # cell left empty by the batch is asked for single-ticker before the cache
    # is written, Norgate-owned columns excluded. On a Norgate run this sleeve
    # is wholly Norgate's and the step has nothing to ask; on a yfinance run
    # (every CI runner) it is the 2026-08-31 SPY blank, healed at the source.
    df, _heal = vendor_tail.heal_hollow_tail(
        df, needed, through=current_through,
        exclude=(_ngrep or {}).get("replaced", []))
    vendor_tail.report_heal(_heal, label="Strategy B")

    # Partial-bar guard: the padded fetch window may include today's
    # in-progress session when run during US market hours.
    df = cap_to_last_completed_session(df)
    # REFUSE A DEGENERATE WRITE. Same rule as the SOXX OHLC path: a vendor
    # never un-prints a close, so an empty or shrunken fetch is a sourcing
    # fault and must not become the series the engine falls back on.
    worse = fetched_panel_is_worse(df, cached)
    if worse is not None:
        if cached is not None and len(cached) and set(needed).issubset(set(cached.columns)):
            # The frame handed back is the CACHE'S basis, not this run's
            # (2026-09-03): a strict Norgate run may not fall back onto a
            # yfinance-built cache, and any run must label what it returns.
            cache_src = cache_source or "yfinance"
            if price_source == "norgate" and cache_src != "norgate":
                raise RuntimeError(
                    f"price fetch refused ({worse}) and the cache on disk was "
                    f"built from {cache_src}; a Norgate-basis run cannot fall "
                    f"back onto it. Re-run once the fetch is healthy, or set "
                    f"BTE_PRICE_SOURCE=yfinance to accept that basis explicitly.")
            print(f"  REFUSED cache write: {worse}. Falling back to the "
                  f"cache on disk (built from {cache_src}).")
            EFFECTIVE_PRICE_SOURCE = cache_src
            return cached[needed]
        raise RuntimeError(
            f"price fetch unusable and no cache to fall back on: {worse}")
    df.to_parquet(PRICE_CACHE)
    _sidecar = dict(_ngrep or {})
    if _heal:
        _sidecar["tail_heal"] = _heal
    price_source_mod.write_cache_source(PRICE_CACHE, price_source,
                                        _sidecar or None)
    print(f"  Downloaded {df.shape[0]} rows x {df.shape[1]} tickers "
          f"(source recorded: {price_source})")
    return df


# =========================================================================
# Signal + portfolio engine
# =========================================================================
def compute_signal(closes: pd.DataFrame) -> pd.DataFrame:
    """Distance above 200d MA per ETF: (close - MA200) / MA200.

    Positive = uptrend (above MA200). Negative = downtrend.

    Tolerant of isolated missing bars since 2026-08-22. This was
    `closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()`; with window and
    min_periods equal, ONE absent close blanks the average for the next 200
    sessions and the weight function then drops the ticker as having
    insufficient history -- a ten-month silent exclusion from a single vendor
    gap. Sleeves A and D never had this, their breadth having always used
    int(period * 0.9); the shared helper is that same convention.
    Value-preserving on the committed cache: bit-identical on every cell both
    definitions define, nothing gained or lost. See price_panel_guard.
    """
    return ma_distance_signal(closes, MA_PERIOD)


def top_k_by_signal(K: int, exclude_negative: bool = True):
    """Weight function for top-K-by-signal portfolio construction.

    Returns a callable that takes a row of signal values (Series indexed by
    ETF) and returns a Series of weights summing to <= 1.
    - Drop NaN signal (insufficient history)
    - Optionally drop negative-signal candidates (below 200d MA)
    - Pick top K by signal value, weight by signal share among them
    - If fewer than K positive-signal candidates exist, the deficit goes to
      cash (CASH_PROXY) — modelled as 1.0 weight on the cash ETF for the
      missing slots.
    """
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        if len(valid) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        candidates = valid[valid > 0] if exclude_negative else valid
        # Cash proxy is downloaded for the floor only — never let it appear
        # as a momentum pick. (Phase 19.1: SHY is cash, not a rotation ETF.)
        if CASH_PROXY in candidates.index:
            candidates = candidates.drop(CASH_PROXY)
        if len(candidates) == 0:
            # Everything in downtrend — sit in cash proxy
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        top = candidates.nlargest(min(K, len(candidates)))
        total_invested_weight = len(top) / K  # so 3 positives in K=5 -> 60% invested
        normed = top / top.sum()
        w = pd.Series(0.0, index=s_row.index)
        w.loc[top.index] = normed * total_invested_weight
        cash_weight = 1.0 - total_invested_weight
        if cash_weight > 0 and CASH_PROXY in w.index:
            w[CASH_PROXY] = w.get(CASH_PROXY, 0.0) + cash_weight
        return w
    return f


def run_rotation(closes: pd.DataFrame, signal: pd.DataFrame, weight_fn,
                  eligible_start: pd.Timestamp,
                  rebalance_freq: str = "W-MON",
                  cost: float = COST_FRAC) -> dict:
    """Run the rotation portfolio. Same mechanics as run_portfolio: yesterday's
    signal -> today's rebalance, yesterday's weights * today's returns."""
    rebalance_dates = engine_rebalance_dates(closes.index, eligible_start,
                                             rebalance_freq, CALENDAR)
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)
    for rd in rebalance_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0:
            continue
        s_row = signal.iloc[prev_idx]
        rb_weights.loc[rd] = weight_fn(s_row).reindex(closes.columns).fillna(0.0)
    weight_panel = rb_weights.reindex(closes.index, method="ffill").fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0

    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * cost
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel, "daily_ret": port_ret,
             "turnover": turnover, "rebalance_dates": rebalance_dates}


def sixty_forty(closes: pd.DataFrame, eligible_start: pd.Timestamp,
                  rebalance_freq: str = "W-MON") -> dict:
    """60% SPY / 40% IEF, rebalanced same cadence. The classical benchmark."""
    target = pd.Series({"SPY": 0.6, "IEF": 0.4})
    rebalance_dates = engine_rebalance_dates(closes.index, eligible_start,
                                             rebalance_freq, CALENDAR)
    rb_weights = pd.DataFrame(index=rebalance_dates, columns=closes.columns,
                               dtype=float)
    for rd in rebalance_dates:
        rb_weights.loc[rd] = target.reindex(closes.columns).fillna(0.0)
    weight_panel = rb_weights.reindex(closes.index, method="ffill").fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel}


def equal_weight_all(closes: pd.DataFrame, eligible_start: pd.Timestamp,
                      rebalance_freq: str = "W-MON") -> dict:
    """Equal weight across all N tickers in the universe."""
    n = len(closes.columns)
    target_w = 1.0 / n
    rebalance_dates = engine_rebalance_dates(closes.index, eligible_start,
                                             rebalance_freq, CALENDAR)
    rb_weights = pd.DataFrame(target_w, index=rebalance_dates,
                                columns=closes.columns, dtype=float)
    weight_panel = rb_weights.reindex(closes.index, method="ffill").fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity}


# =========================================================================
# Stats + reporting
# =========================================================================
def compute_stats(equity: pd.Series, eligible_start: pd.Timestamp) -> dict:
    eq = equity.loc[equity.index >= eligible_start].copy()
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    total_ret = float(eq.iloc[-1] - 1.0)
    cagr = (eq.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(cagr),
        "total_return": _safe(total_ret),
        "max_dd": _safe(max_dd),
    }


def turnover_stats(weight_panel: pd.DataFrame,
                     eligible_start: pd.Timestamp) -> dict:
    wp = weight_panel.loc[weight_panel.index >= eligible_start].copy()
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    return {
        "annual_turnover": float(diff.sum() / n_years) if n_years > 0 else 0.0,
        "n_flips": int((diff > 1e-6).sum()),
    }


def build_trade_history(weight_panel: pd.DataFrame, signal: pd.DataFrame,
                          eligible_start: pd.Timestamp) -> list[dict]:
    """Per-rebalance holdings list. The signal value recorded for each ETF
    is the value the engine actually USED to decide the weight — i.e. the
    PRIOR trading day's signal — not the signal at the rebalance date
    itself. This way the displayed share-math (signal / sum × n/K) exactly
    reproduces the weight, with no day-shift discrepancy."""
    wp = weight_panel.loc[weight_panel.index >= eligible_start]
    sp = signal.reindex(wp.index, method="ffill")
    full_idx = list(wp.index)
    out: list[dict] = []
    prev: pd.Series | None = None
    for i, (dt, row) in enumerate(wp.iterrows()):
        if prev is None or not np.allclose(row.values, prev.values, atol=1e-6):
            non_zero = row[row > 1e-6].sort_values(ascending=False)
            if len(non_zero) == 0:
                prev = row
                continue
            # Signal that was actually used for the decision = signal one
            # trading day BEFORE this rebalance date (no look-ahead).
            decision_date = full_idx[i - 1] if i > 0 else full_idx[i]
            holdings = []
            for etf, w in non_zero.items():
                s_val = sp.loc[decision_date, etf] if etf in sp.columns else None
                holdings.append({
                    "etf": etf,
                    "weight": round(float(w), 4),
                    "signal_pct": (round(float(s_val) * 100, 1)
                                    if s_val == s_val else None),
                })
  # decision_date is the session this rebalance actually RANKED on.
            # All four engines computed it and threw it away, so a
            # rebalance could not say which session decided it. On
            # 2026-08-14 a vendor hole at Thu 13 Aug in the .DE lines
            # moved Strategy D's decision to Wed 12 Aug and flipped
            # EXH3/EXV3 on a 1.3pp margin, invisibly. Recorded now so a
            # stale or divergent decision session is readable, not
            # inferred.
            out.append({"date": dt.strftime("%Y-%m-%d"),
                        "decision_date": decision_date.strftime("%Y-%m-%d"),
                        "holdings": holdings})
            prev = row
    return out


def walk_forward_K(closes: pd.DataFrame, signal: pd.DataFrame,
                     eligible_start: pd.Timestamp,
                     initial_train_end: pd.Timestamp,
                     K_grid: list[int] = None,
                     refit_freq: str = "YE",
                     rebal_freq: str = "W-FRI") -> dict:
    if K_grid is None:
        K_grid = K_GRID
    last_date = closes.index[-1]
    refit_ends = pd.date_range(initial_train_end, last_date, freq=refit_freq)
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_ends]
    refit_ends = [r for r in refit_ends if r >= eligible_start]
    if not refit_ends:
        return {}

    def _portfolio_equity(K, win_start):
        r = run_rotation(closes, signal, top_k_by_signal(K), win_start,
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

    segments = []
    test_eq_pieces = []
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
        full_eq = _portfolio_equity(best_K, eligible_start)
        test_eq = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq / base_val
        test_sh = _sharpe(test_eq, test_start, test_end)
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
    wf_sh = (wf_equity.pct_change().fillna(0).mean()
              / wf_equity.pct_change().fillna(0).std() * math.sqrt(252)
              if wf_equity.pct_change().fillna(0).std() > 0 else 0.0)
    return {
        "segments": segments,
        "walk_forward_sharpe": _safe(wf_sh),
        "wf_dates": [d.strftime("%Y-%m-%d") for d in wf_equity.index],
        "wf_equity": round_series(wf_equity.values),
    }


# =========================================================================
# Main
# =========================================================================
def main() -> int:
    print("Loading asset-class universe ...", flush=True)
    closes = download_prices()
    print(f"  {len(closes.columns)} ETFs, {closes.shape[0]} trading days")

    # Eligible start: 200d after first day of full panel
    closes = closes.dropna()  # drop early dates where any ticker is missing
    if len(closes) == 0:
        print("ERROR: no common dates across the universe", file=sys.stderr)
        return 1
    eligible = closes.index[MA_PERIOD]
    print(f"  Eligible start: {eligible.date()} (200d warm-up)")

    # The dropna() above makes coverage trivially complete, so what this
    # catches for B is the other shapes: a flat column, a member trailing the
    # panel tail, or a series whose history was truncated to a vendor
    # fallback window. See the 2026-08-15 note in price_panel_guard.py.
    assert_panel_usable(closes, "Strategy B closes", window_start=eligible)
    # The dropna() above is also how a withheld Friday LEAVES the panel: on
    # 2026-08-28 ten of thirteen lines came back NaN, the row went, and the
    # 2026-08-31 rebalance was decided on Thursday. The venue calendar is the
    # only reference that can see a session the whole panel lacks.
    assert_decision_session_present(closes, CALENDAR, HEADLINE_FREQ, eligible,
                                    "Strategy B closes")

    print("\nComputing signal (distance above 200d MA) ...")
    signal = compute_signal(closes)

    # ===== Rebalance-frequency sensitivity grid =====
    print("\n=== Rebalance-frequency sensitivity: K x cadence ===")
    grid: dict[str, dict[str, dict]] = {}
    headline_payload: dict | None = None
    for K in K_GRID:
        grid[f"K={K}"] = {}
        print(f"\n  --- K = {K} ---")
        for freq_name, freq_code in REBAL_FREQS:
            r = run_rotation(closes, signal, top_k_by_signal(K), eligible,
                             rebalance_freq=freq_code)
            st = compute_stats(r["equity"], eligible)
            to = turnover_stats(r["weights"], eligible)
            grid[f"K={K}"][freq_name] = {**st, **to}
            print(f"    {freq_name:<14}  Sharpe {st['sharpe']:+.2f}   "
                  f"CAGR {st['cagr']*100:+5.1f}%   "
                  f"DD {st['max_dd']*100:>4.1f}%   "
                  f"turnover/yr {to['annual_turnover']:>4.2f}   "
                  f"flips {to['n_flips']:>3d}")
            if K == HEADLINE_K and freq_name == HEADLINE_FREQ_NAME:
                eq_window = r["equity"].loc[r["equity"].index >= eligible]
                eq_window = eq_window / eq_window.iloc[0]
                trades = build_trade_history(r["weights"], signal, eligible)
                # The last rebalance RUN, traded or held (2026-09-03). The
                # trade record logs weight changes only; this sleeve's
                # signal-proportional weights drift every week so the two
                # coincide here, but the surfaces read one field for all
                # four sleeves. See rebalance_records.py.
                latest_rebalance = latest_rebalance_record(
                    r["weights"], signal, r["rebalance_dates"], "signal_pct",
                    eligible)

                # Per-ETF attribution
                rets = closes.pct_change().fillna(0).loc[r["weights"].index]
                rets = rets.loc[rets.index >= eligible]
                used_w = r["weights"].loc[rets.index].shift(1).fillna(0)
                daily_contrib = used_w * rets
                total_contrib = daily_contrib.sum()
                total_all = float(total_contrib.sum())
                attribution = {}
                for etf in closes.columns:
                    if etf not in daily_contrib.columns:
                        continue
                    held_mask = used_w[etf] > 1e-6
                    n_held = int(held_mask.sum())
                    total_days = len(used_w)
                    if n_held == 0:
                        ann_ret = None
                        avg_w = 0.0
                    else:
                        mean_daily = float(rets.loc[held_mask, etf].mean())
                        ann_ret = (1.0 + mean_daily) ** 252 - 1.0
                        avg_w = float(used_w[etf][held_mask].mean())
                    pnl = float(total_contrib.get(etf, 0.0))
                    attribution[etf] = {
                        "days_held": n_held,
                        "pct_of_days": round(n_held / total_days * 100, 1)
                                          if total_days else 0.0,
                        "avg_weight_when_held": round(avg_w, 4),
                        "ann_return_when_held": _safe(ann_ret),
                        "contribution_to_total_return": _safe(pnl),
                        "pct_of_total_contribution": (
                            round(pnl / total_all * 100, 1)
                            if total_all != 0 else 0.0
                        ),
                    }

                # Last gate before anything is written — a large days_held
                # beside an exactly zero return is a price-cache fault, never
                # a market outcome.
                assert_attribution_sane(attribution,
                                        "Strategy B attribution")

                # Weekly allocation snapshot for stacked-area chart
                # Sample at the ACTUAL rebalance grid, not every Friday:
                # under a holiday-aware cadence a decision can land on a
                # Thursday, and a dayofweek filter would silently drop it.
                weekly_idx = r["rebalance_dates"]
                weekly_w = r["weights"].loc[weekly_idx]
                weekly_w = weekly_w.loc[(weekly_w.sum(axis=1) > 0.5)]

                headline_payload = {
                    "K": K,
                    "rebal_freq": freq_name,
                    "rebal_freq_code": freq_code,
                    "n_etfs": len(TICKERS),
                    "etfs_used": TICKERS,
                    "eligible_start": eligible.strftime("%Y-%m-%d"),
                    "headline_stats": {**st, **to},
                    "headline_equity_dates": [d.strftime("%Y-%m-%d")
                                                for d in eq_window.index],
                    "headline_equity": round_series(eq_window.values),
                    # n_rebalances counted TRADES until 2026-09-03; it now
                    # counts the rebalance grid, and trades have their own key.
                    "n_rebalances": int(len(weekly_w)),
                    "n_trades": len(trades),
                    "trade_history": trades,
                    "latest_rebalance": latest_rebalance,
                    "attribution": attribution,
                    "weekly_allocation_dates": [d.strftime("%Y-%m-%d")
                                                  for d in weekly_w.index],
                    "weekly_allocation": {
                        etf: round_series(weekly_w[etf].values)
                        for etf in weekly_w.columns
                    },
                }

    # ===== Benchmarks =====
    print("\n=== Benchmarks ===")
    spy_eq_window = closes["SPY"].loc[closes["SPY"].index >= eligible]
    spy_eq = spy_eq_window / spy_eq_window.iloc[0]
    spy_stats = compute_stats(closes["SPY"], eligible)
    print(f"  SPY                Sharpe {spy_stats['sharpe']:+.2f}   "
          f"CAGR {spy_stats['cagr']*100:+5.1f}%   DD {spy_stats['max_dd']*100:.1f}%")

    ew_run = equal_weight_all(closes, eligible, HEADLINE_FREQ)
    ew_eq_window = ew_run["equity"].loc[ew_run["equity"].index >= eligible]
    ew_eq = ew_eq_window / ew_eq_window.iloc[0]
    ew_stats = compute_stats(ew_run["equity"], eligible)
    print(f"  Equal-weight univ  Sharpe {ew_stats['sharpe']:+.2f}   "
          f"CAGR {ew_stats['cagr']*100:+5.1f}%   DD {ew_stats['max_dd']*100:.1f}%")

    sf_run = sixty_forty(closes, eligible, HEADLINE_FREQ)
    sf_eq_window = sf_run["equity"].loc[sf_run["equity"].index >= eligible]
    sf_eq = sf_eq_window / sf_eq_window.iloc[0]
    sf_stats = compute_stats(sf_run["equity"], eligible)
    print(f"  60/40 SPY/IEF      Sharpe {sf_stats['sharpe']:+.2f}   "
          f"CAGR {sf_stats['cagr']*100:+5.1f}%   DD {sf_stats['max_dd']*100:.1f}%")

    benchmarks = {
        "spy_buy_hold": {
            "label": "SPY buy-and-hold",
            "dates": [d.strftime("%Y-%m-%d") for d in spy_eq.index],
            "equity": round_series(spy_eq.values),
            **spy_stats,
        },
        "equal_weight_14": {
            "label": "Equal-weight asset-class universe (no signal)",
            # JSON key `equal_weight_14` kept for dashboard compatibility
            # (historical name; the count is no longer 14 post-Phase 29).
            "dates": [d.strftime("%Y-%m-%d") for d in ew_eq.index],
            "equity": round_series(ew_eq.values),
            **ew_stats,
        },
        "sixty_forty": {
            "label": "60/40 SPY/IEF rebalanced weekly",
            "dates": [d.strftime("%Y-%m-%d") for d in sf_eq.index],
            "equity": round_series(sf_eq.values),
            **sf_stats,
        },
    }

    # ===== Walk-forward K selection =====
    print("\n=== Walk-forward K refit (annual, K in {3,4,5,6,7}) ===")
    wf = walk_forward_K(closes, signal, eligible,
                         pd.Timestamp("2014-12-31"), K_grid=K_GRID,
                         refit_freq="YE", rebal_freq=HEADLINE_FREQ)
    if wf:
        print(f"  Walk-forward Sharpe: {wf['walk_forward_sharpe']:+.2f}")
        print(f"  K sequence: {[s['best_K'] for s in wf['segments']]}")

    # ===== Per-ETF signal time series (weekly samples) for ETF Detail tab =====
    # Sample on Fridays only to keep JSON size down; that matches the chart
    # cadence elsewhere and ETF Detail's weekly granularity. Include the
    # CASH_PROXY (SHY) so users who see SHY in their attribution can
    # inspect its signal too.
    signal_window = signal.loc[signal.index >= eligible]
    weekly_signal = signal_window.loc[signal_window.index.dayofweek == 4]
    per_etf_signals = {}
    for etf in TICKERS + CASH_ONLY_TICKERS:
        if etf in weekly_signal.columns:
            ser = weekly_signal[etf].dropna()
            per_etf_signals[etf] = {
                "dates": [d.strftime("%Y-%m-%d") for d in ser.index],
                "signal_pct": [round(float(v) * 100, 2) for v in ser.values],
            }

    # ===== Output =====
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        # The basis this run priced on (2026-09-03): a reader of the artefact
        # can tell a Norgate-basis book from a yfinance one without the log.
        "price_source": EFFECTIVE_PRICE_SOURCE,
        "universe": [
            {"etf": t, "label": UNIVERSE[t]["label"],
             "asset_class": UNIVERSE[t]["asset_class"]}
            for t in TICKERS
        ] + [
            # CASH_ONLY_TICKERS (SHY, Phase 19.1) are not rotation candidates
            # but appear in attribution / weights whenever the cash floor is
            # active. Without an asset_class entry the dashboard renders an
            # empty Asset Class cell — same bug Phase 21 fixed for IEF in C.
            {"etf": "SHY",
             "label": "iShares 1-3y US Treasury (Strategy B cash floor)",
             "asset_class": "Cash / Treasury"},
        ],
        "ma_period": MA_PERIOD,
        "cost_bps": COST_BPS,
        "rebalance_freq_grid": grid,
        "headline": headline_payload,
        "benchmarks": benchmarks,
        "walk_forward": wf,
        "asset_class_colours": ASSET_CLASS_COLOURS,
        "per_etf_signal": per_etf_signals,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    print()
    print("=" * 90)
    print(f"STRATEGY B HEADLINE — K={HEADLINE_K}, {HEADLINE_FREQ_NAME}")
    print("=" * 90)
    h = headline_payload
    s = h["headline_stats"]
    print(f"  Sharpe          : {s['sharpe']:+.2f}")
    print(f"  CAGR            : {s['cagr']*100:+.1f}%")
    print(f"  Total return    : {s['total_return']*100:+.1f}%")
    print(f"  Max drawdown    : {s['max_dd']*100:.1f}%")
    print(f"  Annual turnover : {s['annual_turnover']:.2f}")
    print(f"  Number of rebals: {h['n_rebalances']}")
    if wf:
        print(f"  Walk-forward Sharpe: {wf['walk_forward_sharpe']:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
