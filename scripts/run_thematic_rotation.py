"""Strategy C — thematic rotation (Phase 3).

Where Strategy A operates within US sectors (constituent breadth) and
Strategy B operates across asset classes (ETF-level momentum), Strategy C
runs the same ETF-level momentum signal on a curated set of THEMATIC ETFs
to catch secular trends that don't fit traditional sector/asset-class
boxes (AI, cybersecurity, clean energy, biotech, blockchain, etc).

Thematic ETFs are riskier than broad sectors:
  - Survivorship bias (failed thematics get delisted; only winners remain
    in the backtest universe)
  - Short history (most launched 2014+, some 2018+)
  - Fad-prone — a top-K rotation can chase last year's blowoff right at
    the peak (ARKK 2021, cannabis 2018)
  - Higher fees (40-75 bps vs 10 bps for broad sectors)

Therefore Strategy C has additional fad-resistance guardrails on top of
Strategy B's mechanics:

  1. Hard signal floor: signal must be >= 5% above 200d MA to be eligible
     (not just positive). Filters marginal "in an uptrend" cases.
  2. Per-ETF cap: max 35% of Strategy C in any single thematic.
  3. Cash floor in SHY when fewer than K candidates clear the floor.
     (Phase 19.1, 2026-05-27: switched from IEF after the 2022 inflation
     episode showed IEF's 7y duration co-moves with equities in rate-hike
     regimes. SHY's 1-3y duration is duration-neutral cash. The same logic
     drove the overlay-fallback switch.)
  4. Smaller K (3-4) because the universe is more internally correlated.
  5. The combined portfolio sleeve cap is 10% (managed in run_multi_strategy).

Universe (16 thematic ETFs, all US-listed, all > $500M AUM):

  Technology / Innovation:
    ARKK  - ARK Innovation
    CIBR  - First Trust NASDAQ Cybersecurity
    SKYY  - First Trust Cloud Computing
    BOTZ  - Global X Robotics & AI
    BLOK  - Amplify Transformational Data Sharing (blockchain)
  Energy / Climate:
    ICLN  - iShares Global Clean Energy
    TAN   - Invesco Solar
    LIT   - Global X Lithium & Battery Tech
    URA   - Global X Uranium
  Health / Bio:
    XBI   - SPDR S&P Biotech (equal-weight, more theme-driven than IBB)
    ARKG  - ARK Genomic Revolution
  Cyclical thematics:
    JETS  - Global X Airlines
  Commodity-equity (distinct from broad commodity spot in B):
    GDX   - VanEck Gold Miners (operational leverage to gold)
    COPX  - Global X Copper Miners (industrial metals cycle)
    MOO   - VanEck Agribusiness
  Infrastructure:
    PAVE  - Global X US Infrastructure Development

Signal: distance above own 200-day MA per ETF.
Output: data/thematic_rotation.json
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PRICE_CACHE = DATA_DIR / "thematic_prices_cache.parquet"
OUT_PATH = DATA_DIR / "thematic_rotation.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from rebalance_calendar import engine_rebalance_dates  # noqa: E402
from nyse_sessions import (  # noqa: E402
    cap_to_last_completed_session,
    last_completed_session,
    yf_fetch_end,
)
from price_panel_guard import (  # noqa: E402
    assert_attribution_sane, assert_panel_usable,
)

sys.stdout.reconfigure(encoding="utf-8")


UNIVERSE: dict[str, dict] = {
    "ARKK": {"label": "ARK Disruptive Innovation",          "theme": "Tech / Innovation"},
    "CIBR": {"label": "First Trust Cybersecurity",          "theme": "Tech / Innovation"},
    "SKYY": {"label": "First Trust Cloud Computing",        "theme": "Tech / Innovation"},
    "BOTZ": {"label": "Global X Robotics & AI",             "theme": "Tech / Innovation"},
    "BLOK": {"label": "Amplify Blockchain",                 "theme": "Tech / Innovation"},
    "ICLN": {"label": "iShares Global Clean Energy",        "theme": "Energy / Climate"},
    "TAN":  {"label": "Invesco Solar",                      "theme": "Energy / Climate"},
    "LIT":  {"label": "Global X Lithium & Battery Tech",    "theme": "Energy / Climate"},
    "URA":  {"label": "Global X Uranium",                   "theme": "Energy / Climate"},
    "XBI":  {"label": "SPDR S&P Biotech (eq-weight)",       "theme": "Health / Bio"},
    "ARKG": {"label": "ARK Genomic Revolution",             "theme": "Health / Bio"},
    "JETS": {"label": "Global X Airlines",                  "theme": "Cyclical thematic"},
    "GDX":  {"label": "VanEck Gold Miners",                 "theme": "Commodity equity"},
    "COPX": {"label": "Global X Copper Miners",             "theme": "Commodity equity"},
    "MOO":  {"label": "VanEck Agribusiness",                "theme": "Commodity equity"},
    # Phase 16 (2026-05-26) commodity-equity additions. All three pass the
    # <0.85 correlation gate against incumbents and have 8.4y history.
    # XME is the broad metals-and-mining basket (steel, aluminum, iron-ore
    # plus precious + base metal miners) — broader than GDX/COPX. WOOD is
    # the timber + forestry theme (real-asset adjacent). REMX captures the
    # rare-earth + strategic-metals supply-chain decoupling theme.
    "XME":  {"label": "SPDR S&P Metals & Mining",           "theme": "Commodity equity"},
    "WOOD": {"label": "iShares Global Timber & Forestry",   "theme": "Commodity equity"},
    "REMX": {"label": "VanEck Rare Earth / Strategic Metals", "theme": "Commodity equity"},
    # ---------------------------------------------------------------------
    # Phase 17 (2026-05-26) — China-tech proxy. The Phase 4 retrospective
    # rejected a separate EM/countries sleeve, so China-tech exposure was
    # missing from the deployed architecture. CQQQ captures the broad
    # China-technology basket (Tencent, Alibaba, SMIC, BYD, Xiaomi) — the
    # closest USD-listed proxy for Shanghai-listed China-semi ETFs like
    # 588200.SS that are operationally hard to access for a Singapore CMS
    # fund. Passes the gate with a comfortable margin (max-corr 0.65 vs
    # LIT), far below the 0.85 cap.
    #
    # KWEB (KraneShares China Internet) was tested alongside CQQQ but
    # REVERTED before commit. KWEB passed the corr gate (0.57 max vs LIT)
    # but the 2021-2023 China internet crackdown (-70% over two years)
    # created a unique drawdown profile that the K=4 momentum signal
    # repeatedly bounce-traded and got chopped on. With both CQQQ and
    # KWEB, Strategy C WF Sharpe dropped +0.50 -> +0.37. With CQQQ alone,
    # the drag is smaller because semi-China was less crackdown-exposed
    # than internet-platform-China.
    "CQQQ": {"label": "Invesco China Technology",           "theme": "China / EM Tech"},
    # ---------------------------------------------------------------------
    # Phase 17.1 (2026-05-26) — China A-share semiconductor exposure.
    # CQQQ captures broad China-tech with platform tilt (Tencent, Alibaba)
    # plus some semis (SMIC). 159801.SZ captures the pure A-share semi
    # hardware basket (Cambricon, AMEC, NAURA, Will Semiconductor, SMIC)
    # — a meaningfully different exposure profile from CQQQ.
    #
    # IBKR access to Chinese A-share ETFs via Stock Connect is operationally
    # smooth (confirmed with Eileen, Navigo CEO, 2026-05-26) — the earlier
    # rejection of 588200.SS on operational grounds was incorrect. 159801.SZ
    # is chosen as the deployed ticker because:
    #   (a) 7.0 years of history (launched 2019-08-26) — clears the 5y
    #       walk-forward minimum, unlike 588200.SS which only has 3.65y.
    #   (b) 0.958 weekly-return correlation with 588200.SS in their
    #       overlap window — they track essentially the same A-share
    #       semiconductor universe (Bosera tracks CSI Chip, Harvest tracks
    #       SSE STAR Chip; both are dominated by the same names).
    #   (c) Gate vs C incumbents: max-corr 0.49 vs CQQQ — far below 0.85
    #       cap. Adds materially different exposure to CQQQ (pure semi
    #       hardware vs broader tech platforms).
    #
    # Implementation: CNY-denominated, so download pipeline applies a
    # daily FX conversion (CNY -> USD via USDCNY=X) and aligns to the
    # NYSE trading calendar with a 10-day stale-fill cap (handles Chinese
    # New Year + Oct Golden Week SSE closures). 50bps annual expense ratio
    # applied as per-calendar-day compounded drag, same pattern as IBIT's
    # 25bps on BTC-USD.
    "159801.SZ": {
        "label": "Bosera CSI Chip (China A-share semis — USD-adj from CNY)",
        "theme": "China / EM Tech",
        "currency": "CNY",
        "expense_ratio_bps": 50,
        "trading_calendar": "sse_szse",  # Shanghai / Shenzhen — needs alignment
        # Inception 2019-08-26 is after BLOK's 2018-01 binding date, so
        # this ticker must NOT constrain the core eligibility window or
        # the backtest collapses ~1.5y. The K=4 picker excludes NaN
        # signals automatically (signal is NaN for any date pre-200d-MA),
        # so this is safe and lossless.
        "late_inception": True,
    },
    "PAVE": {"label": "Global X US Infrastructure",         "theme": "Infrastructure"},
    # ---------------------------------------------------------------------
    # Phase 15 additions (2026-05-26). Passes the within-Strategy-C
    # correlation gate (<0.85 vs every existing member) with sufficient
    # history (>= 5 years) for walk-forward.
    "ITA":  {"label": "iShares US Aerospace & Defense",     "theme": "Defence / Aerospace"},
    # ---------------------------------------------------------------------
    # Phase 15.2 (2026-05-26) — Bitcoin via spot proxy + ETF execution.
    # IBIT (iShares Bitcoin Trust) only launched 2024-01-11, so it cannot
    # support the 5-year walk-forward methodology directly. But IBIT
    # tracks the CoinDesk Bitcoin Reference Price (BTC-USD) with very
    # tight error post-launch — it is a pure spot ETF. We therefore
    # backtest using BTC-USD (8.4y history, gate-passed max-corr 0.610
    # vs BLOK, 5y-history-clear) and apply IBIT's 25bps expense ratio
    # as a per-calendar-day compounded price drag so the historical
    # return path matches what an IBIT holder would have experienced.
    #
    # Live execution remains in IBIT. GBTC was considered as an alternative
    # but rejected: its market price traded at large premium / discount to
    # NAV from 2015-2024, so GBTC-price momentum would have captured
    # GBTC-discount narrative rather than BTC momentum. BTC-USD has none
    # of that fund-structure noise.
    "BTC-USD": {
        "label": "Bitcoin (CoinDesk spot — deployed via IBIT, 25bps ER)",
        "theme": "Crypto / Digital Assets",
        "expense_ratio_bps": 25,
        "trading_calendar": "crypto_24x7",  # reindex to equity calendar at load time
    },
    # ---------------------------------------------------------------------
    # Phase 25 additions (2026-05-31). Bulk universe screen of 27 liquid
    # US thematic ETFs (run_thematic_universe_screen.py) against the then-
    # current 23-theme universe. Two-stage gate (per-pair correlation <
    # 0.85 then walk-forward Sharpe degradation < 0.03).
    #
    # 5 candidates passed Stage 1; 4 passed Stage 2. PHO and IHI selected
    # for deployment as the two structurally meaningful adds — BETZ and
    # PRNT are too small / niche to consistently make the K=4 cut.
    #
    # PHO note — this is a RE-DEPLOYMENT. PHO was originally tested in
    # Phase 5 (2026-05-24) as part of a 4-ETF cohort (ITB, AMLP, PHO,
    # KRE) added simultaneously to the then-16-theme universe. That
    # cohort dropped the standalone walk-forward Sharpe ~0.10 and the
    # blended Sharpe moved within noise, so all 4 were reverted. The
    # current screen re-tested PHO ALONE against the 23-theme universe
    # (which now includes XME / WOOD / REMX / CQQQ / 159801.SZ / PAVE /
    # ITA / BTC-USD — none of which existed in Phase 5's baseline) and
    # got a marginal +0.001 WF lift. The Phase 5 negative was driven by
    # the cohort effect (4 correlated cyclicals fighting each other for
    # K=4 slots), not by PHO's standalone contribution. Honest framing
    # for Eileen: PHO is now structurally additive (water infra fills a
    # gap next to PAVE), but the WF Sharpe impact is essentially zero;
    # we are not expecting a return uplift, only better diversification.
    "PHO": {"label": "Invesco Water Resources",          "theme": "Infrastructure"},
    "IHI": {"label": "iShares US Medical Devices",       "theme": "Healthcare"},
    # ---------------------------------------------------------------------
    # Phase 25 rejected on Stage 1 (correlation gate, 2026-05-31):
    #   ROBO ~ BOTZ +0.93, KOMP ~ BLOK +0.87, WCLD ~ SKYY +0.92,
    #   FINX ~ SKYY +0.91, HACK ~ CIBR +0.96, ARKW ~ ARKK +0.97,
    #   ARKF ~ ARKK +0.95, ARKQ ~ ARKK +0.92, ARKX ~ ARKK +0.89,
    #   IBB ~ XBI +0.92, IDNA ~ XBI +0.90, KARS ~ LIT +0.93,
    #   DRIV ~ LIT +0.86, QCLN ~ ICLN +0.94, HYDR ~ LIT +0.88,
    #   GRID ~ PAVE +0.87, PPA ~ ITA +0.97, ESPO ~ ARKK +0.86,
    #   AWAY ~ BLOK +0.89, ETHA ~ ARKK +0.88, BITQ ~ BLOK +0.96,
    #   PBW ~ ICLN +0.93.
    # Phase 25 rejected on Stage 2 (walk-forward gate):
    #   KRBN — WF Sharpe -0.064 vs baseline.
    # PRNT (+0.003) and BETZ (+0.001) passed both gates but were not
    # deployed: AUM < 200M, niche exposure, unlikely to consistently
    # make K=4 cut in a 25-theme universe. Retained here for any future
    # re-test:
    #   "PRNT": {"label": "ARK 3D Printing & Tech",       "theme": "Manufacturing tech"},
    #   "BETZ": {"label": "Roundhill Sports Betting",     "theme": "Gaming / digital leisure"},
    # ---------------------------------------------------------------------
    # Phase 5 candidates (tested 2026-05-24, NOT deployed). The following
    # 4 ETFs passed the within-Strategy-C correlation gate (<0.85 vs any
    # existing C member) and were experimentally added to the universe.
    # Empirical result: Strategy C standalone Sharpe lifted +0.71 -> +0.74,
    # but walk-forward Sharpe DROPPED +0.36 -> +0.26 (more fad-chasing
    # OOS), and at the 10% sleeve weight the deployed 4-way blend Sharpe
    # changed by +0.0003 (within noise). Reverted; see Method tab for the
    # full retrospective. Retained as commented references for any future
    # re-test:
    #   "ITB":  {"label": "iShares US Home Construction",       "theme": "Rate-sensitive cyclical"},
    #   "AMLP": {"label": "Alerian MLP (energy infrastructure)", "theme": "Yield / infrastructure"},
    #   "KRE":  {"label": "SPDR S&P Regional Banking",          "theme": "Rate-sensitive cyclical"},
    # NOTE: PHO from this cohort was successfully re-deployed in Phase 25
    # (2026-05-31) — see the Phase 25 block above. The Phase 5 negative
    # was a cohort effect (4 correlated cyclicals added together), not
    # PHO-specific.
    # ---------------------------------------------------------------------
    # Phase 15 rejected on the correlation gate (2026-05-26):
    #   "AIQ":  Global X AI & Tech — max-corr 0.891 vs SKYY, also 0.881 vs
    #           BOTZ, 0.848 vs CIBR, 0.848 vs ARKK. Structural overlap with
    #           the Tech / Innovation bucket because the underlying holdings
    #           (NVDA, MSFT, GOOGL, META) are the top holdings of the cloud /
    #           cybersecurity / innovation ETFs already in the universe.
    #           Same problem applies to ROBT and IRBO.
}
TICKERS = list(UNIVERSE.keys())


# Deferred universe — registered for visibility but NOT in TICKERS,
# so they are excluded from downloads / signals / backtests. Move into
# UNIVERSE once each candidate clears both gates (correlation < 0.85
# AND history >= 5 years).
#
# IBIT was previously listed here pending 5y history. Phase 15.2
# supersedes that approach: BTC-USD (spot) is now the backtest source
# with IBIT-equivalent 25bps expense-ratio drag, and IBIT itself is the
# live execution vehicle. See the UNIVERSE entry for BTC-USD above.
DEFERRED_UNIVERSE: dict[str, dict] = {}

# Cash proxy when fewer than K candidates clear the signal floor.
# Phase 19.1 (2026-05-27): switched from IEF (7-10y, ~7y duration) to
# SHY (1-3y, ~1.8y duration). The 2022 inflation episode showed IEF can
# sell off alongside equities in rate-hike regimes — Strategy C held IEF
# 74% of the 2022 trading days as cash floor, and IEF was -15% that year,
# turning the cash floor into a second drawdown. SHY is duration-neutral
# cash and tracks the t-bill carry without the rate-hike risk.
CASH_PROXY = "SHY"

START_DATE = "2018-01-01"  # BLOK inception (Jan 2018) is the binding date
# yfinance's `end` is EXCLUSIVE — end=today drops today's completed
# close (this is how the 2026-07-17 weekly CI run captured the sleeve
# only through Thursday and the factsheet missed the Friday rebalance).
# Fetch padded 2 days ahead; download_prices() caps the panel at the
# last completed NYSE session so a mid-session run cannot ingest a
# partial bar either.
END_DATE = yf_fetch_end()

MA_PERIOD = 200
SIGNAL_FLOOR = 0.05       # require >= 5% above 200d MA (not just positive)
PER_ETF_CAP = 0.35        # no single thematic > 35% of Strategy C
# Phase 12 cost calibration: Strategy C trades 16 thematic ETFs with mixed
# liquidity — ARKK/XBI/GDX are 1-3 bps, but BLOK/MOO/PAVE/BOTZ are 5-10 bps.
# Blended one-way cost ~5 bps, kept at the prior uniform default since this
# is genuinely where the 5 bps anchor comes from in the first place.
COST_BPS = 5
# Venue this sleeve trades on, for the holiday-aware rebalance rule.
CALENDAR = "NYSE"
COST_FRAC = COST_BPS / 10_000

K_GRID = [3, 4, 5]
REBAL_FREQS = [
    ("Daily",         "D"),
    ("Weekly Fri",    "W-FRI"),
    ("Bi-weekly Fri", "2W-FRI"),
    ("Month-end",     "BME"),
]
# Phase 27 (2026-06-01): K moves from 4 -> 5 to match the walk-forward
# pick under the new sleeve-breadth gate. The bake-off + validation
# (scripts/run_thematic_exit_*.py + data/thematic_exit_*.json) showed
# WF picks K=5 every refit segment when the sleeve gate is active.
# Mechanism: the gate handles the regime risk that previously forced
# K=3-4, so the in-sample concentration choice (K=5) becomes safe to
# deploy.
HEADLINE_K = 5
HEADLINE_FREQ_NAME = "Weekly Fri"
HEADLINE_FREQ = "W-FRI"

# Phase 27 (2026-06-01) — sleeve-breadth gate ("V6"). If fewer than
# SLEEVE_GATE_THRESHOLD of the thematic universe is above SIGNAL_FLOOR
# at any rebal, exit all positions to SHY. Catches sleeve-wide
# regime changes that the per-ETF +5% signal misses until significant
# damage is done. See data/thematic_exit_robustness.json for the
# empirical justification:
#   - Walk-forward Sharpe +1.12 (vs baseline +1.01, +0.11 lift)
#   - Max DD -38.5% in-sample (vs baseline -50.9%)
#   - 2021-22 thematic blow-up DD -24.4% (vs baseline -43.2%)
#   - Threshold of 30% chosen for robustness — 50% looks better in-
#     sample but fails OOS (joint refit picks 50% and degrades to
#     +0.88 WF Sharpe). 30% is the conservative, generalising choice.
# Known downside (documented in robustness report):
#   - V-shape risk: gate exits AFTER thematic breadth crashes (locking
#     in the bottom) and re-enters AFTER recovery; COVID 2020-Q1 cost
#     -12.9pp vs baseline. Acceptable trade for the larger DD wins on
#     2021-22 and 2022-rate-hike episodes.
SLEEVE_GATE_ENABLED = True
SLEEVE_GATE_THRESHOLD = 0.30

# Phase 6 (2026-05-24): the within-strategy weight function. Equal-weight
# was empirically dominant for Strategy C across IS Sharpe, WF Sharpe,
# CAGR, max DD, and turnover. Revert to top_k_by_signal_capped here if
# the experiment needs to be re-run. See run_phase6_weighting_experiment.py
# for the full A/B comparison.
_WEIGHTER_NAME = "top_k_equal_weight"  # see top_k_equal_weight() docstring


# Stable per-ETF colour palette for the dashboard's stacked allocation chart
THEMATIC_COLOURS = {
    "ARKK": "#dc2626", "CIBR": "#7c3aed", "SKYY": "#0891b2",
    "BOTZ": "#1351b4", "BLOK": "#374151",
    "ICLN": "#1d7a3a", "TAN":  "#ca8a04", "LIT":  "#0d9488", "URA":  "#92400e",
    "XBI":  "#be185d", "ARKG": "#e879f9",
    "JETS": "#0e7490",
    "GDX":  "#a16207", "COPX": "#b45309", "MOO":  "#65a30d",
    "PAVE": "#52525b",
    # Phase 5 additions
    "ITB":  "#9d174d",  # magenta — homebuilders
    "AMLP": "#854d0e",  # ochre — MLPs / energy infra
    "PHO":  "#155e75",  # deep teal — water (deployed in Phase 25)
    "KRE":  "#1e40af",  # deep blue — regional banks
    # Phase 25 addition
    "IHI":  "#a21caf",  # purple — medical devices
    "SHY":  "#6b727a",  # cash proxy (1-3y Treasury — Phase 19.1)
    "IEF":  "#9ca3af",  # retained for backward compatibility (old payloads)
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


def _apply_expense_ratio_drag(df: pd.DataFrame) -> pd.DataFrame:
    """For any UNIVERSE entry with an ``expense_ratio_bps`` field, compound
    a per-calendar-day drag onto its price series so the backtest reflects
    the cost an actual ETF holder would have paid. Used for synthetic /
    index proxies like BTC-USD that yfinance returns gross of any ETF
    wrapper fee. Existing fund tickers (SOXX, IUSP, etc) already have
    their expense ratio embedded in the auto-adjusted close, so they do
    not carry this metadata."""
    for ticker, meta in UNIVERSE.items():
        er_bps = meta.get("expense_ratio_bps")
        if not er_bps or ticker not in df.columns:
            continue
        col = df[ticker].dropna()
        if not len(col):
            continue
        first_date = col.index[0]
        elapsed_days = (df.index - first_date).days.to_numpy()
        # Daily drag: (1 - ER)^(1/365). Over 365 calendar days, cumulative
        # drag == 1 - ER. Pre-inception days get exponent 0 → multiplier 1.
        daily_drag = (1 - er_bps / 10_000) ** (1 / 365)
        df[ticker] = df[ticker] * (daily_drag ** np.maximum(elapsed_days, 0))
    return df


def _reindex_crypto_to_equity_calendar(df: pd.DataFrame) -> pd.DataFrame:
    """BTC-USD trades 7 days/week; the rest of the universe trades on the
    NYSE calendar. Reindex any ``trading_calendar=='crypto_24x7'`` column
    to the equity trading calendar (Mon-Fri) using the CASH_PROXY series
    as the reference index. Without this, the weekend BTC prints would
    confuse downstream alignment and rebalance scheduling.

    For Friday rebalance decisions: the BTC price on the Friday close
    is the relevant value; we drop Saturday-Sunday prints entirely.
    """
    equity_cal = df[CASH_PROXY].dropna().index
    for ticker, meta in UNIVERSE.items():
        if meta.get("trading_calendar") != "crypto_24x7":
            continue
        if ticker not in df.columns:
            continue
        df[ticker] = df[ticker].reindex(equity_cal)
    # Drop any remaining non-equity-calendar rows (would have only
    # crypto-tagged tickers populated otherwise).
    return df.loc[df.index.isin(equity_cal) | df.index.isin(df[CASH_PROXY].dropna().index)]


def _fx_convert_to_usd(df: pd.DataFrame) -> pd.DataFrame:
    """For any UNIVERSE entry with currency != 'USD', FX-convert the
    native-currency price series to USD and reindex onto the equity
    (NYSE) trading calendar with a 10-day stale-fill cap. Handles
    Chinese exchange holidays (e.g., Chinese New Year, Oct Golden Week)
    which can leave SSE/SZSE closed for up to 7 calendar days.

    Currently supports CNY (via USDCNY=X). Extend to other currencies
    by adding the FX-pair mapping below.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from alignment import align_series_to_index  # noqa: E402

    fx_pair_for_currency = {
        "CNY": "CNY=X",  # yfinance returns CNY per 1 USD
    }
    fx_cache: dict[str, pd.Series] = {}
    equity_cal = df[CASH_PROXY].dropna().index

    for ticker, meta in UNIVERSE.items():
        currency = meta.get("currency", "USD")
        if currency == "USD" or ticker not in df.columns:
            continue
        fx_pair = fx_pair_for_currency.get(currency)
        if fx_pair is None:
            print(f"  WARN: no FX mapping for currency={currency} ({ticker})")
            continue
        if fx_pair not in fx_cache:
            print(f"  Downloading FX series {fx_pair} for {currency} -> USD ...",
                  flush=True)
            fx_raw = yf.download(fx_pair, start=START_DATE, end=END_DATE,
                                  auto_adjust=True, progress=False)
            fx_close = fx_raw["Close"]
            if isinstance(fx_close, pd.DataFrame):
                fx_close = fx_close.iloc[:, 0]
            fx_cache[fx_pair] = fx_close
        fx = fx_cache[fx_pair]
        # Compute USD-denominated price on the intersection of native-
        # currency dates + FX dates: USD_price = native_price / FX_rate
        # (FX_rate is "native per 1 USD", so divide to convert to USD).
        native_prices = df[ticker].dropna()
        merged = pd.concat([native_prices, fx], axis=1, sort=True).dropna()
        merged.columns = ["native", "fx"]
        usd_native = merged["native"] / merged["fx"]
        # Reindex to NYSE equity calendar with a 10-day stale-fill cap.
        # 10 days covers the Chinese National Day week (Oct 1-7) plus a
        # buffer; Chinese New Year (typically 7 trading days off) also fits.
        df[ticker] = align_series_to_index(usd_native, equity_cal,
                                            max_stale_days=10)
    return df


def download_prices() -> pd.DataFrame:
    """Download adjusted-close prices for the thematic universe + cash proxy.

    Reuses the asset_class cache when available for the cash proxy (avoid
    double-downloading). Reused only when current through the last
    COMPLETED NYSE session — the previous "<= 7 calendar days stale"
    rule could serve a days-old panel to an ad-hoc midweek run.
    """
    needed = TICKERS + [CASH_PROXY]
    current_through = last_completed_session(datetime.now(timezone.utc))
    if PRICE_CACHE.exists():
        cached = pd.read_parquet(PRICE_CACHE)
        cache_end = cached.index.max().date()
        if cache_end >= current_through and set(needed).issubset(set(cached.columns)):
            print(f"  Using cached prices ({cached.index.min().date()} -> "
                  f"{cache_end}, current through {current_through})")
            return cached[needed]

    print(f"  Downloading {len(needed)} tickers from yfinance "
          f"({START_DATE} -> {END_DATE}) ...", flush=True)
    raw = yf.download(needed, start=START_DATE, end=END_DATE, auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    closes = {}
    for t in needed:
        if (t, "Close") in raw.columns:
            closes[t] = raw[(t, "Close")]
        elif "Close" in raw.columns:
            closes[t] = raw["Close"]
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    # Partial-bar guard: the padded fetch window may include today's
    # in-progress session (and crypto's current partial day) when run
    # during market hours. Cap BEFORE the crypto/FX calendar work so
    # equity_cal is bounded too.
    df = cap_to_last_completed_session(df)
    # Phase 15.2: reindex crypto to equity calendar BEFORE applying
    # expense-ratio drag, so the drag's elapsed-days arithmetic uses the
    # right base index.
    df = _reindex_crypto_to_equity_calendar(df)
    # Phase 17.1: FX-convert any non-USD-denominated tickers (currently
    # CNY-denominated A-share ETFs) onto the NYSE equity calendar before
    # the expense-ratio drag, so the drag compounds on USD prices.
    df = _fx_convert_to_usd(df)
    df = _apply_expense_ratio_drag(df)
    df.to_parquet(PRICE_CACHE)
    print(f"  Downloaded {df.shape[0]} rows x {df.shape[1]} tickers")
    return df


def compute_signal(closes: pd.DataFrame) -> pd.DataFrame:
    ma = closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    return (closes - ma) / ma


def top_k_equal_weight(K: int):
    """Strategy C weight function — Phase 6 (equal-weight) + Phase 27
    sleeve-breadth gate.

    - Phase 27 GATE: if fewer than SLEEVE_GATE_THRESHOLD (30%) of the
      universe is above SIGNAL_FLOOR at this rebal, exit all positions
      to SHY. Catches sleeve-wide regime changes (e.g. early 2022
      thematic-complex rollover) before per-ETF +5% breaches trigger.
    - Drop NaN signal (insufficient history)
    - Drop signal < SIGNAL_FLOOR (require >= 5% above 200d MA)
    - Top K by signal value
    - Weight equally: 1/K per holding (so the most-overbought is not
      overweighted relative to peers that also cleared the floor)
    - If fewer than K candidates clear the floor, the deficit goes to
      the SHY cash proxy

    Phase 6 retrospective (2026-05-24): replaced top_k_by_signal_capped
    after the weighting-scheme experiment showed equal-weight dominates
    on every metric for Strategy C (IS Sharpe +0.708 -> +0.781, WF Sharpe
    +0.364 -> +0.388, CAGR +16.5% -> +18.3%, DD -43.7% -> -42.8%,
    turnover 16.9x -> 15.7x). The mechanistic reason: C's +5% signal
    floor already filters out modest trends, so signal magnitude beyond
    eligibility carries little extra information — every eligible
    candidate is "well into an uptrend". Weighting heavily toward the
    strongest just overweights the candidate most likely to mean-revert.

    Phase 27 binding (2026-06-01): added sleeve-breadth gate after a
    six-variant exit-rule bake-off (run_thematic_exit_*.py). Walk-
    forward Sharpe lifts from baseline +1.01 -> +1.12, max DD
    improves -50.9% -> -38.5% in-sample, with the 2021-22 thematic
    blow-up specifically halved -43.2% -> -24.4%. Documented downside:
    V-shape risk on fast recoveries (COVID 2020-Q1 cost -12.9pp).
    """
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        w = pd.Series(0.0, index=s_row.index)
        # Phase 27 sleeve-breadth gate. Compute universe breadth from
        # the same signal panel — fraction of non-cash universe above
        # SIGNAL_FLOOR. Below threshold = sleeve in regime change,
        # exit all positions to cash.
        if SLEEVE_GATE_ENABLED:
            univ = valid.drop(CASH_PROXY, errors="ignore")
            n_universe = len(univ)
            if n_universe > 0:
                n_above = (univ > SIGNAL_FLOOR).sum()
                sleeve_breadth = n_above / n_universe
                if sleeve_breadth < SLEEVE_GATE_THRESHOLD:
                    if CASH_PROXY in w.index:
                        w[CASH_PROXY] = 1.0
                    return w

        eligible = valid[valid > SIGNAL_FLOOR]
        # Phase 19.1: CASH_PROXY (SHY) is downloaded for the cash floor
        # only — never a momentum pick. Exclude it from the eligible set so
        # it cannot be promoted to a rotation candidate even if its tiny
        # signal somehow clears the +5% floor in a rate-shock regime.
        if CASH_PROXY in eligible.index:
            eligible = eligible.drop(CASH_PROXY)
        if len(eligible) == 0:
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested_frac = len(top) / K
        per_etf = invested_frac / len(top)  # = 1/K when K slots filled
        w.loc[top.index] = per_etf
        cash = 1.0 - invested_frac
        if cash > 0 and CASH_PROXY in w.index:
            w[CASH_PROXY] = w.get(CASH_PROXY, 0.0) + cash
        return w
    return f


def top_k_by_signal_capped(K: int):
    """Strategy C original weight function (PRE-PHASE-6 baseline).

    Retained for reference / reversibility. The deployed weighter is
    top_k_equal_weight; see WEIGHTER_FACTORY below.

    - Drop NaN signal (insufficient history)
    - Drop signal < SIGNAL_FLOOR (require >= 5% above 200d MA)
    - Top K by signal value
    - Weight by signal share, then cap any single ETF at PER_ETF_CAP and
      redistribute the spilled weight proportionally to the others (iterate
      until no cap is breached)
    - If fewer than K candidates clear the floor, the deficit goes to
      the IEF cash proxy
    """
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        if len(valid) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        eligible = valid[valid > SIGNAL_FLOOR]
        if len(eligible) == 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        top = eligible.nlargest(min(K, len(eligible)))
        invested_frac = len(top) / K
        if top.sum() <= 0:
            w = pd.Series(0.0, index=s_row.index)
            if CASH_PROXY in w.index:
                w[CASH_PROXY] = 1.0
            return w
        # Raw weights from signal share, scaled to invested_frac
        raw = (top / top.sum()) * invested_frac
        # Iteratively apply the per-ETF cap. Cap is on within-strategy weight,
        # i.e. raw weight as a fraction of invested portion (not total).
        cap = PER_ETF_CAP * invested_frac
        for _ in range(8):  # converges in a few iterations
            over = raw > cap
            if not over.any():
                break
            excess = (raw[over] - cap).sum()
            raw[over] = cap
            under = raw < cap
            if under.sum() == 0 or raw[under].sum() == 0:
                break
            # Redistribute excess pro-rata to the under-cap names
            raw[under] += excess * (raw[under] / raw[under].sum())
        w = pd.Series(0.0, index=s_row.index)
        w.loc[top.index] = raw
        cash = 1.0 - invested_frac
        if cash > 0 and CASH_PROXY in w.index:
            w[CASH_PROXY] = w.get(CASH_PROXY, 0.0) + cash
        return w
    return f


# Phase 6 binding: bound here after both weighters are defined. Change
# the right-hand side to `top_k_by_signal_capped` to revert to the
# pre-Phase-6 baseline weighting scheme.
WEIGHTER_FACTORY = top_k_equal_weight


def run_rotation(closes: pd.DataFrame, signal: pd.DataFrame, weight_fn,
                  eligible_start: pd.Timestamp,
                  rebalance_freq: str = "W-FRI",
                  cost: float = COST_FRAC) -> dict:
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
    weight_panel = rb_weights.reindex(closes.index).ffill().fillna(0.0)
    weight_panel.loc[weight_panel.index < eligible_start] = 0.0

    rets = closes.pct_change().fillna(0)
    port_ret = (weight_panel.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weight_panel.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * cost
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weight_panel, "daily_ret": port_ret,
             "turnover": turnover, "rebalance_dates": rebalance_dates}


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
    return {
        "sharpe": _safe(sharpe),
        "cagr": _safe(cagr),
        "total_return": _safe(total_ret),
        "max_dd": _safe(float(dd.min())),
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
    """Per-rebalance holdings list. Records the PRIOR trading day's signal
    (the value that actually decided the weight) so the share-math
    reproduces the weight exactly."""
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
                     K_grid: list[int] | None = None,
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
        r = run_rotation(closes, signal, WEIGHTER_FACTORY(K), win_start,
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
    wf_daily = wf_equity.pct_change().fillna(0)
    wf_sh = (wf_daily.mean() / wf_daily.std() * math.sqrt(252)
              if wf_daily.std() > 0 else 0.0)
    return {
        "segments": segments,
        "walk_forward_sharpe": _safe(wf_sh),
        "wf_dates": [d.strftime("%Y-%m-%d") for d in wf_equity.index],
        "wf_equity": round_series(wf_equity.values),
    }


def main() -> int:
    print("Loading thematic universe ...", flush=True)
    closes = download_prices()
    # Drop columns that are entirely NaN (rare — should not happen for our list)
    closes = closes.dropna(axis=1, how="all")
    print(f"  {len(closes.columns)} tickers, {closes.shape[0]} trading days")

    # Each ticker may start on a different date. Eligible start =
    # 200 trading days after the latest first-valid date in the CORE
    # universe (i.e. excluding late_inception tickers).
    #
    # Late-inception tickers (UNIVERSE metadata flag) do NOT constrain
    # the backtest window — they join the candidate pool only on days
    # when their signal exists (NaN signals are excluded by the K-pick
    # filter, so this is safe and lossless for the core universe).
    # Phase 17.1 introduced this for 159801.SZ (China A-share semi,
    # inception 2019-08, post-BLOK 2018-01 binding date). Without this
    # decoupling, adding 159801.SZ would have collapsed the backtest
    # window by ~1.5 years.
    late_inception_tickers = {
        t for t, m in UNIVERSE.items()
        if m.get("late_inception") and t in closes.columns
    }
    core_first_valid = {
        c: closes[c].first_valid_index()
        for c in closes.columns
        if c not in late_inception_tickers
    }
    latest_start = max(d for d in core_first_valid.values() if d is not None)
    eligible_idx = closes.index.searchsorted(latest_start) + MA_PERIOD
    if eligible_idx >= len(closes):
        print("ERROR: not enough data for warm-up", file=sys.stderr)
        return 1
    eligible = closes.index[eligible_idx]
    print(f"  Latest CORE ticker start: {latest_start.date()} -> "
          f"eligible from {eligible.date()}")
    if late_inception_tickers:
        late_starts = {
            t: closes[t].first_valid_index() for t in late_inception_tickers
        }
        print(f"  Late-inception tickers (not constraining window): "
              f"{ {t: d.date() for t, d in late_starts.items() if d} }")

    # The declared late-inception members are exempted from the "starts too
    # far into the window" rule and from nothing else — they are still held
    # to coverage, interior gaps and the panel tail. Exempting them wholesale
    # would blind the guard to exactly the sleeve member most likely to have
    # a thin vendor history. See the 2026-08-15 note in price_panel_guard.py.
    assert_panel_usable(closes, "Strategy C closes", window_start=eligible,
                        allow_late=set(late_inception_tickers))

    print("\nComputing signal (distance above 200d MA) ...")
    signal = compute_signal(closes)

    print("\n=== Rebalance-frequency sensitivity: K x cadence ===")
    grid: dict[str, dict[str, dict]] = {}
    headline_payload: dict | None = None
    for K in K_GRID:
        grid[f"K={K}"] = {}
        print(f"\n  --- K = {K} (signal floor +{int(SIGNAL_FLOOR*100)}%, "
              f"weighter {_WEIGHTER_NAME}) ---")
        for freq_name, freq_code in REBAL_FREQS:
            r = run_rotation(closes, signal, WEIGHTER_FACTORY(K),
                              eligible, rebalance_freq=freq_code)
            st = compute_stats(r["equity"], eligible)
            to = turnover_stats(r["weights"], eligible)
            grid[f"K={K}"][freq_name] = {**st, **to}
            print(f"    {freq_name:<14}  Sharpe {st['sharpe']:+.2f}   "
                  f"CAGR {st['cagr']*100:+5.1f}%   "
                  f"DD {st['max_dd']*100:>5.1f}%   "
                  f"turnover/yr {to['annual_turnover']:>4.2f}   "
                  f"flips {to['n_flips']:>3d}")
            if K == HEADLINE_K and freq_name == HEADLINE_FREQ_NAME:
                eq_window = r["equity"].loc[r["equity"].index >= eligible]
                eq_window = eq_window / eq_window.iloc[0]
                trades = build_trade_history(r["weights"], signal, eligible)

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
                        ann_ret, avg_w = None, 0.0
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
                                        "Strategy C attribution")

                # Weekly allocation snapshot (Fridays only) for stacked-area
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
                    "etfs_used": list(closes.columns),
                    "eligible_start": eligible.strftime("%Y-%m-%d"),
                    "signal_floor_pct": SIGNAL_FLOOR * 100,
                    "per_etf_cap_pct": PER_ETF_CAP * 100,
                    "headline_stats": {**st, **to},
                    "headline_equity_dates": [d.strftime("%Y-%m-%d")
                                                for d in eq_window.index],
                    "headline_equity": round_series(eq_window.values),
                    "n_rebalances": len(trades),
                    "trade_history": trades,
                    "attribution": attribution,
                    "weekly_allocation_dates": [d.strftime("%Y-%m-%d")
                                                  for d in weekly_w.index],
                    "weekly_allocation": {
                        etf: round_series(weekly_w[etf].values)
                        for etf in weekly_w.columns
                    },
                }

    print("\n=== Benchmarks ===")
    # SPY and 60/40 sit in the asset_class cache; just compare to QQQ which is
    # the most relevant single-thematic comparator (concentrated tech).
    spy_close = closes.get("IEF")  # placeholder; actually want SPY here
    # Pull SPY from yfinance if not in our cache.
    try:
        spy_raw = yf.download("SPY", start=START_DATE, end=END_DATE,
                               auto_adjust=True, progress=False, threads=False)
        if isinstance(spy_raw.columns, pd.MultiIndex):
            spy_raw.columns = spy_raw.columns.get_level_values(0)
        spy_close = spy_raw["Close"]
        spy_close.index = pd.to_datetime(spy_close.index).tz_localize(None)
        spy_close = spy_close.reindex(closes.index).ffill()
    except Exception as e:
        print(f"  WARNING: could not fetch SPY -- {e}")
        spy_close = pd.Series(index=closes.index, dtype=float)

    spy_window = spy_close.loc[spy_close.index >= eligible].dropna()
    if len(spy_window) > 0:
        spy_eq = spy_window / spy_window.iloc[0]
        spy_stats = compute_stats(spy_close.ffill(), eligible)
        print(f"  SPY                Sharpe {spy_stats['sharpe']:+.2f}   "
              f"CAGR {spy_stats['cagr']*100:+5.1f}%   DD {spy_stats['max_dd']*100:.1f}%")
    else:
        spy_eq = pd.Series(dtype=float)
        spy_stats = {"sharpe": None, "cagr": None, "total_return": None, "max_dd": None}

    benchmarks = {
        "spy_buy_hold": {
            "label": "SPY buy-and-hold",
            "dates": [d.strftime("%Y-%m-%d") for d in spy_eq.index],
            "equity": round_series(spy_eq.values),
            **spy_stats,
        },
    }

    print("\n=== Walk-forward K refit (annual, K in {3, 4, 5}) ===")
    # Train initial period: roughly half the available history
    initial_train_end = pd.Timestamp(eligible.year + 2, 12, 31)
    if initial_train_end > closes.index[-1]:
        initial_train_end = closes.index[len(closes) // 2]
    wf = walk_forward_K(closes, signal, eligible, initial_train_end,
                         K_grid=K_GRID, refit_freq="YE",
                         rebal_freq=HEADLINE_FREQ)
    if wf:
        print(f"  Walk-forward Sharpe: {wf['walk_forward_sharpe']:+.2f}")
        print(f"  K sequence: {[s['best_K'] for s in wf['segments']]}")

    # Per-ETF signal time series (weekly Fridays) for ETF Detail tab.
    # Includes IEF since it is C's cash proxy and users will want to inspect
    # its momentum when it shows up in their portfolio.
    signal_window = signal.loc[signal.index >= eligible]
    weekly_signal = signal_window.loc[signal_window.index.dayofweek == 4]
    per_etf_signals = {}
    for etf in list(TICKERS) + [CASH_PROXY]:
        if etf in weekly_signal.columns:
            ser = weekly_signal[etf].dropna()
            per_etf_signals[etf] = {
                "dates": [d.strftime("%Y-%m-%d") for d in ser.index],
                "signal_pct": [round(float(v) * 100, 2) for v in ser.values],
            }

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": [
            {"etf": t, "label": UNIVERSE[t]["label"], "theme": UNIVERSE[t]["theme"]}
            for t in TICKERS
        ] + [
            # CASH_PROXY (IEF) is not in TICKERS but appears in the attribution
            # table whenever fewer than K candidates clear the signal floor.
            # Without a theme entry, the dashboard renders an empty Theme cell.
            {"etf": CASH_PROXY,
             "label": "iShares 1-3y US Treasury (Strategy C cash floor)",
             "theme": "Cash / Treasury"}
        ],
        "ma_period": MA_PERIOD,
        "signal_floor_pct": SIGNAL_FLOOR * 100,
        "per_etf_cap_pct": PER_ETF_CAP * 100,
        "cost_bps": COST_BPS,
        "rebalance_freq_grid": grid,
        "headline": headline_payload,
        "benchmarks": benchmarks,
        "walk_forward": wf,
        "thematic_colours": THEMATIC_COLOURS,
        "per_etf_signal": per_etf_signals,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    print()
    print("=" * 90)
    print(f"STRATEGY C HEADLINE — K={HEADLINE_K}, {HEADLINE_FREQ_NAME}")
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
