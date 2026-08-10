"""WS6 (2026-07-17) — single-name implementation of Sleeve A.

Pre-registration: KICKOFF_ws6-single-name-implementation.md (frozen, signed
2026-07-17). This module is T2 — the engine and its selftests, committed
BEFORE any register results exist (em-rotation-lab precedent; the same
discipline WS5 followed). It deliberately carries NO ``__main__`` that runs the
eight-arm register: the registered run is T3 (a separate harness), which may
execute only once this module and tests/test_single_name_impl.py are committed.

The one decision the study must produce: can Sleeve A's ETF positions be
expressed as constituent baskets without degrading the sleeve's evidence base
(Design 1 — replication with a trend screen), and does within-sector selection
add anything beyond that (Design 2 — top-N strongest selection)?

Architecture — the sector layer is REUSED, not rebuilt
------------------------------------------------------
Every arm shares ONE sector book, produced by the deployed Phase 20.1 Sleeve A
path exactly: per-ETF constituent breadth (the deployed cached member-price
path), cross-sectional demeaning, top K=7 of the 14-line universe, positive-
relative-share weighting, W-FRI rebalance with the deployed holiday-week skip,
and the deployed ``shift(1)`` at each rebalance. ``deployed_sector_layer``
below assembles this from ``run_portfolio.build_panels`` +
``run_portfolio.run_portfolio`` + ``run_portfolio.top_k_breadth_weight`` — the
same objects ``run_strategy_a_universe_gate.run_topk_backtest`` (the deployed
engine) and ``run_ws5_relative_trend`` reuse. The sector picks and the per-line
weights are therefore IDENTICAL across all arms by construction; arms differ
ONLY in how a held single-named line's weight is expressed. Norgate prices are
used ONLY on the basket side (member screening, ranking, member returns) — they
NEVER re-touch the breadth signal.

Arms differ only in the basket. For each of the 11 single-named lines (SOXX,
IUES, IUFS, IUHC, IUIS, IUCS, IUCD, IUUS, IUMS, IUCM, IUSP) the line weight is
distributed across constituent names per the arm's rule. The three broad slices
(CSP1, CNDX, IDP6) stay ETFs in EVERY arm — single-naming a 500-name index is
replication theatre, and the fund-of-funds optic concerns sector funds. E0 is
the degenerate arm where every line is expressed as its own ETF, so E0 must
reproduce the deployed sleeve to 0.0 (the parity anchor; see
tests/test_single_name_impl.py).

Register (frozen §2): #0 E0 · #1 I0 · #2 I1 · #3 I2 · #4 P2 · #5 I2-N15 ·
#6 P2-N15 · #7 I1-all-members. Exposed here as callable arm builders; the T3
harness runs them.

Licence guard (Norgate: personal use, no redistribution): raw vendor series are
cached ONLY under the git-ignored ``data_local/ws6/`` tree and must NEVER be
committed. Committed files may carry DERIVED statistics only. This module writes
no vendor values to any committed path; the selftests run on synthetic panels.

No look-ahead (failure mode 2): both the per-name state/rank AND the membership
snapshot are taken as of the prior trading day (t-1), identical to the deployed
sector signal's ``shift(1)``. Membership therefore never comes from the same
week's forward-dated file. ``select_basket`` reads only member data at or before
the effective date; tests/test_single_name_impl.py pins the invariance.

Amendment A1 (2026-07-18, kickoff §5b — logged pre-results after the first G1
FAIL_STOP): ticker resolution is completed to (ticker, membership date) ->
Norgate INSTRUMENT. Norgate stores delisted instruments under delisting-dated
suffixes (``XLNX-202202`` style, suffix = delisting YYYYMM), so the resolver
enumerates the plain live symbol plus every delisted-suffixed variant of the
base, disambiguates recycled base tickers by the instrument life interval
(first quoted date to the suffix month end) containing the membership date, and
falls back to a small VERIFIED rename table only when no native candidate
contains the date. A membership date contained by no candidate — or by more
than one (never guess) — leaves the name unresolved, counted against coverage.
Panels and screens are keyed by INSTRUMENT symbol, so a recycled ticker never
blends two companies' price histories in one column. G1 re-tests at the
unchanged 97% bar; arm definitions, constants, costs, window and verdict rule
are all unchanged by A1.

Amendment A3 (2026-07-19, kickoff §5b, signed ZH — logged after the post-A2 G2
FAIL_STOP: I0-vs-E0 weekly correlation IUCD 0.9193 / IUCM 0.9468 against the
0.95 bar; equal-weight top-15 misprices the mega-cap-concentrated
heterogeneous lines): TRUE-WEIGHT baskets, §6 item 4's pre-registered
alternative. The pool rule and M=15 are UNCHANGED; inside the pool every
non-fallback basket weights its selected members by the TRUE snapshot
Weight (%) renormalised over the selected set (screened arms renormalise the
survivors' weights). Weights come from the A3 Step-0 stage
(scripts/fetch_ws6_weights.py -> data_local/ws6/weights/, basis-tagged;
load_member_weights validates the basis so a stale equal-weight artefact can
never be silently consumed). Weight lookup is by snapshot ticker and the
basket key stays the RESOLVED INSTRUMENT, so recycled tickers do not blend
eras in the weight panel either. A snapshot without weights carries the
line's last known weights forward (counted per line); a line-week whose
selected members lack a usable weight — including the no-weights-at-all
degenerate case — falls back to equal weight (counted per line; expected
zero). G1/G2 definitions and bars, the register, costs, window and verdict
rule are unchanged.

Amendment A2 (2026-07-18, kickoff §5b — logged pre-results after the post-A1 G1
re-test narrowed to 6 failing cells): BASE-TICKER TENURE disambiguation. Where
more than one candidate's LIFE interval contains the membership date (a dead
instrument whose base was later recycled by a live acquirer carrying
pre-recycle history under the same base), the week belongs to the instrument
that actually traded under the base ticker on that date. A dead instrument's
tenure ends at its day-granular ``last_quoted_date`` (the one tenure signal the
NDU API exposes — there is no former-symbol/rename-date metadata; the
deprecated ``base_symbol`` helper is futures-only); a live successor's tenure
over a recycled base begins at its verified rename date, carried in
``TICKER_TENURE_OVERRIDES`` (NDU-verified per entry). Tenure refines ONLY the
multi-candidate case; single-candidate resolution keeps the A1 month-end slack,
and rename-table targets are still validated on the LIFE interval (the
snapshot ticker in those eras was the source name, not the target's base). A2
also completes the rename table (rename-at-death and relist gaps: FOX/FOXA
pre-2019, LB, PCLN, CBL, OPI, RVI). Bar and design otherwise unchanged.

Dates: pandas / dateutil only, never manual day arithmetic. Python ``datetime``
months are 1-indexed (stated where any month indexing occurs; this module does
none). The rebalance calendar is the deployed ``rebalance_calendar`` helper.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
# Git-ignored (licence guard); raw Norgate member series live here only.
DATA_LOCAL_WS6 = PROJECT_ROOT / "data_local" / "ws6"
# Amendment A3: per-line constituent Weight (%) tables built by the Step-0
# stage (scripts/fetch_ws6_weights.py). Git-ignored with the rest of
# data_local/. The basis tag is the cache-invalidation key: any consumer
# validates it via load_member_weights, so panels built under a different
# weighting basis can never be silently reused.
WS6_WEIGHTS_DIR = DATA_LOCAL_WS6 / "weights"
WEIGHTING_BASIS = "true_weight_a3"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from run_portfolio import (  # noqa: E402  (deployed sector engine, reused verbatim)
    build_panels,
    run_portfolio,
    top_k_breadth_weight,
)
from run_ma200_sweep import MA_PERIOD  # noqa: E402  (deployed 200d convention)
from rebalance_calendar import weekly_rebalance_dates  # noqa: E402


# ---------------------------------------------------------------------------
# Frozen constants (KICKOFF §2 — no tunable knobs inside an arm)
# ---------------------------------------------------------------------------

# The 11 sector/industry lines expressed as constituent baskets.
SINGLE_NAMED_LINES: tuple[str, ...] = (
    "SOXX", "IUES", "IUFS", "IUHC", "IUIS", "IUCS",
    "IUCD", "IUUS", "IUMS", "IUCM", "IUSP",
)
# The three broad slices stay ETFs in every arm (§6 item 3).
BROAD_SLICES: tuple[str, ...] = ("CSP1", "CNDX", "IDP6")

M_POOL = 15                 # top-M cap-rank pool per line
N_SELECT = 10               # top-N selection (Design 2)
N_NEIGHBOUR = 15            # neighbour arms I2-N15 / P2-N15
MIN_PASS = 3                # < 3 names passing the screen -> revert to ETF
K_DEPLOYED = 7              # canonical sector top-K (not refit)

# Per-name trend on the same 200d state that generates the sleeve's breadth
# signal, with the deployed 90%-populated-window minimum (identical to
# run_ma200_sweep.compute_ma200_breadth / relative_trend.MA_PERIOD).
TREND_MA = MA_PERIOD
MIN_PERIODS_FRACTION = 0.9
PLACEBO_MOM_DAYS = 126      # momentum-placebo rank window (matches WS5's placebo)

# Cost model. E0 keeps the deployed 2 bps; constituent trades pay the swept
# one-way bps on the FULL name-level weight vector so sector-rotation churn,
# screen churn and membership churn all pay (failure mode 3).
DEPLOYED_COST_BPS = 2
CONSTITUENT_COST_BPS = 5    # base estimate for large-cap US names (uncertain)
COST_SWEEP_BPS = (2, 5, 10, 20)
BINDING_COST_BPS = 10       # 2x, binding in the verdict

# Registered window: 2018-Q4 -> 2026-Q2 (deployed evidence window). The eligible
# start is derived from the data (200d warm-up) exactly as the deployed engine.
WINDOW_END = pd.Timestamp("2026-06-30")

REBAL_FREQ = "W-FRI"

# Explicit iShares -> Norgate BASE-ticker renames. Punctuation (dash -> dot for
# share classes) is handled algorithmically in normalise_ticker; this table is
# for genuine symbol RENAMES that punctuation cannot recover. Unmapped names are
# never silently dropped — they are counted against coverage (failure mode 1).
# The caches already normalise Berkshire/Brown-Forman to the dash form
# (constituents_*.json "ticker_overrides_applied"), so those arrive here as
# BRK-B / BF-B and only need the dash->dot punctuation step.
KNOWN_RENAMES: dict[str, str] = {
    "FB": "META",       # Facebook -> Meta (2022-06)
    "GOOGL": "GOOGL",   # class-A retained; identity, listed for explicitness
    "GOOG": "GOOG",
}

# Amendment A1 — verified snapshot-ticker -> exact Norgate INSTRUMENT renames.
# Fired ONLY when the base has no native instrument whose life interval
# contains the membership date (resolve_instrument step 2), and accepted only
# if the target instrument's own interval contains the date. Values are exact
# Norgate symbols: a live symbol for a rename-continuation, a delisted
# "-YYYYMM" symbol where the continuing instrument itself later delisted (the
# suffix is then part of the verified target, e.g. CHKAQ-202102).
#
# EVERY entry was verified against the local NDU on 2026-07-18 by
# security_name + first_quoted_date: the target's name matches the known
# corporate action and its first quoted date matches the SOURCE company's
# lineage (Norgate keys an instrument's full history to its final symbol), so
# the interval check passes exactly for the source-era membership dates. The
# per-entry comment records that evidence.
INSTRUMENT_RENAMES: dict[str, str] = {
    # --- SOXX (semiconductors) ---
    "CREE": "WOLF",          # Wolfspeed Inc Common, fqd 1993-02-09 (Cree lineage)
    "BRKS": "AZTA",          # Azenta Inc Common, fqd 1995-02-02 (Brooks Automation lineage)
    "IIVI": "COHR",          # Coherent Corp Common, fqd 1990-01-02 (II-VI lineage;
                             # delisted COHR-202206 is the acquired old Coherent Inc)
    "SGH": "PENG",           # Penguin Solutions Inc Common, fqd 2017-05-24 (SMART Global lineage)
    # --- IUFS (financials) ---
    "FI": "FISV",            # Fiserv Inc Common, fqd 1990-01-02 (Norgate keeps FISV;
                             # FI-199808 is Fina Inc — wrong era, excluded natively)
    "WLTW": "WTW",           # Willis Towers Watson plc Common, fqd 2001-06-12
    "TMK": "GL",             # Globe Life Inc Common, fqd 1990-01-02 (Torchmark lineage)
    "RE": "EG",              # Everest Group Ltd Common, fqd 1995-10-03 (Everest Re lineage)
    "LUK": "JEF",            # Jefferies Financial Group Common, fqd 1990-01-02 (Leucadia lineage)
    "FLT": "CPAY",           # Corpay Inc Common, fqd 2010-12-15 (Fleetcor lineage)
    "SIVB": "SIVBQ-202411",  # SVB Financial Group Common, fqd 1990-01-02 (ch.11 OTC form)
    "FRC": "FRCB",           # First Republic Bank Common (live), fqd 2010-12-09
    "BK": "BNY",             # Bank of New York Mellon Corp Common, fqd 1990-01-02
    "MMC": "MRSH",           # Marsh & McLennan Companies Inc Common, fqd 1990-01-02
    # --- IUHC (health care) ---
    "ANTM": "ELV",           # Elevance Health Inc Common, fqd 2001-10-30 (Anthem lineage)
    "ABC": "COR",            # Cencora Inc Common, fqd 1995-04-04 (AmerisourceBergen lineage)
    "MYL": "VTRS",           # Viatris Inc Common, fqd 1990-01-02 (Mylan lineage)
    "PKI": "RVTY",           # Revvity Inc Common, fqd 1990-01-02 (PerkinElmer lineage)
    # --- IUIS (industrials) ---
    "UTX": "RTX",            # RTX Corp Common, fqd 1990-01-02 (United Technologies lineage)
    "HRS": "LHX",            # L3Harris Technologies Common, fqd 1990-01-02 (Harris lineage)
    "JEC": "J",              # Jacobs Solutions Inc Common, fqd 1990-01-02
    "FBHS": "FBIN",          # Fortune Brands Innovations Common, fqd 2011-09-16
    "ARNC": "HWM",           # Howmet Aerospace Inc Common, fqd 1990-01-02 — covers the
                             # pre-2020-04 Arconic Inc era; the 2020 spinoff Arconic
                             # Corp is ARNC-202308 (fqd 2020-04-01) and wins natively
                             # for its own era, so the chain is era-consistent
    "APY": "CHX-202507",     # ChampionX Corp Common, fqd 2018-04-27 (Apergy lineage)
    "CDAY": "DAY-202602",    # Dayforce Inc Common, fqd 2018-04-26 (Ceridian lineage)
    # --- IUES (energy) ---
    "COG": "CTRA-202605",    # Coterra Energy Inc Common, fqd 1990-02-08 (Cabot lineage)
    "HFC": "DINO",           # HF Sinclair Corp Common, fqd 1990-01-02 (HollyFrontier lineage)
    "BHGE": "BKR",           # Baker Hughes Company Class A Common, fqd 1990-01-02
    "CHK": "CHKAQ-202102",   # Chesapeake Energy Corp Common, fqd 1993-02-05 (ch.11 OTC
                             # form; covers the old-equity era through 2021-02 — the
                             # post-bankruptcy Chesapeake/Expand is a different equity,
                             # so later CHK dates correctly stay unresolved)
    # --- IUCS / IUCD (consumer) ---
    "KORS": "CPRI",          # Capri Holdings Ltd Common, fqd 2011-12-15 (Michael Kors lineage)
    "GPS": "GAP",            # Gap Inc Common, fqd 1990-01-02
    "LB": "BBWI",            # Bath & Body Works Inc Common, fqd 1990-01-02 (Limited /
                             # L Brands lineage; the live LB is LandBridge Company,
                             # fqd 2024-06-28 — wrong era, excluded natively)
    "PCLN": "BKNG",          # Booking Holdings Inc Common, fqd 1999-03-30 (Priceline
                             # lineage; the live PCLN is a 2025 Pictet ETF)
    "WYN": "TNL",            # Travel + Leisure Co Common, fqd 2006-07-19 (Wyndham
                             # Worldwide lineage; WH is the 2018 hotel spinoff, not the parent)
    "DPS": "KDP",            # Keurig Dr Pepper Inc Common, fqd 2008-04-28 (Dr Pepper
                             # Snapple lineage)
    "BFB": "BF.B",           # Brown-Forman Class B Common, fqd 1990-01-02 (dash-less
                             # snapshot form of BF-B seen in a few IUCS rows)
    "VSCO": "VSXY",          # Victoria's Secret & Co Common, fqd 2021-07-21
    # --- IUCM (communications) ---
    "CTL": "LUMN",           # Lumen Technologies Inc Common, fqd 1990-01-02 (CenturyLink lineage)
    "CBS": "PSKY",           # Paramount Skydance Corp Class B Common, fqd 1990-06-14 —
                             # CBS Corp was the surviving entity of the 2019 merger;
                             # CBS-199511 / CBS-200005 are earlier CBS incarnations
                             # whose intervals end pre-window
    "VIAC": "PSKY",          # same continuing instrument for the 2019-12 -> 2022-02 era
    "PARA": "PSKY",          # same continuing instrument for the 2022 -> 2025 era
    "DISCA": "WBD",          # Warner Bros. Discovery Series A, fqd 2005-07-06 (Discovery-A
                             # lineage; DISCK resolves natively to DISCK-202204)
    "SATS": "ECHO",          # EchoStar Corporation Class A Common, fqd 2007-12-31
    "FOX": "TFCF-201903",    # Twenty-First Century Fox Class B Common, fqd 1990-01-02,
                             # lqd 2019-03-19 (renamed TFCF at the Disney close; the
                             # NEW Fox Corporation Class B, fqd 2019-03-13, wins
                             # natively from the recycle date — class-consistent B->B)
    "FOXA": "TFCFA-201903",  # Twenty-First Century Fox Class A Common, fqd 1994-11-03,
                             # lqd 2019-03-19 (class-consistent A->A; Fox Corporation
                             # Class A, fqd 2019-03-12, wins natively post-recycle)
    # --- IUMS (materials) ---
    "DWDP": "DD",            # DuPont de Nemours Inc Common, fqd 2017-09-01 (DowDuPont
                             # lineage; DD-201708 is the pre-merger E I du Pont)
    "BLL": "BALL",           # Ball Corp Common, fqd 1990-01-02
    # --- IUSP (real estate) ---
    "HCN": "WELL",           # Welltower Inc Common, fqd 1990-01-02 (Health Care REIT lineage)
    "HCP": "DOC",            # Healthpeak Properties Common, fqd 1990-01-02 (HCP lineage;
                             # HashiCorp's HCP-202502 has fqd 2021-12 and never claims
                             # the 2018-2019 HCP-REIT dates natively)
    "PEAK": "DOC",           # same continuing instrument for the 2019 -> 2023 era
    "HPT": "SVC",            # Service Properties Trust Common, fqd 1995-08-17 (HPT lineage)
    "SNH": "DHC",            # Diversified Healthcare Trust Common, fqd 1999-10-07
                             # (Senior Housing lineage)
    "OFC": "CDP",            # COPT Defense Properties Common, fqd 1991-12-31 (COPT lineage)
    "DDR": "SITC",           # SITE Centers Corp Common, fqd 1993-02-02 (DDR lineage)
    "WRE": "ELME",           # Elme Communities Common, fqd 1990-01-02 (Washington REIT lineage)
    "CLI": "VRE-202605",     # Veris Residential Inc Common, fqd 1994-08-25 (Mack-Cali lineage)
    "WPG": "WPGGQ-202110",   # Washington Prime Group Common, fqd 2014-05-14 (ch.11 OTC form)
    "PEI": "PRETQ-202404",   # Pennsylvania REIT Common, fqd 1990-01-02 (ch.11 OTC form)
    "FVE": "ALR-202303",     # AlerisLife Inc Common, fqd 2001-12-17 (Five Star Senior lineage)
    "FCEA": "FCE.A-201812",  # Forest City Realty Class A Common, fqd 1990-01-02
                             # (dash-less snapshot form of FCE.A)
    "BPR": "BPYU-202107",    # Brookfield Property REIT Class A, fqd 2018-08-28 (BPR lineage)
    "AFIN": "RTL-202309",    # Necessity Retail REIT Class A Common, fqd 2018-07-19
                             # (American Finance Trust lineage; bare RTL snapshot rows
                             # resolve natively to the same instrument)
    "GOV": "OPITQ-202606",   # Office Properties Income Trust Common, fqd 2009-06-03
                             # (GOV lineage; ch.11 OTC form)
    "HTA": "HR",             # Healthcare Realty Class A Common (live), fqd 2012-06-06 —
                             # the surviving ex-HTA entity; delisted HR-202207 is the
                             # old Healthcare Realty absorbed in the 2022 merger
    "CLNY": "DBRG",          # DigitalBridge Group Class A Common, fqd 2014-06-27
                             # (Colony lineage post-2017 merger; CLNY-201701 is the
                             # pre-merger Colony, interval ends pre-window)
    "IRET": "CSR",           # Centerspace Common, fqd 1997-10-17 (IRET lineage)
    "MPW": "MPT",            # Medical Properties Trust Inc Common, fqd 2005-07-08
    "AHH": "AHRT",           # AH Realty Trust Inc Common, fqd 2013-05-08 (Armada
                             # Hoffler lineage)
    "CBL": "CBLAQ-202111",   # CBL And Associates Properties Common, fqd 1993-10-28,
                             # lqd 2021-11-01 (renamed CBLAQ at ch.11; the live CBL,
                             # fqd 2021-11-02, is the post-reorg re-registration and
                             # wins natively for any post-relist rows)
    "OPI": "OPITQ-202606",   # Office Properties Income Trust Common, fqd 2009-06-03,
                             # lqd 2026-06-17 (renamed OPITQ at death; the live OPI,
                             # fqd 2026-06-22, is a DISTINCT post-reorg re-registration
                             # — assetid 4090874 vs 455627 — whose first quote
                             # postdates every membership row; diagnosed 2026-07-18)
    "RVI": "RVIC-202304",    # Retail Value Inc Common, fqd 2018-06-26, lqd 2023-04-04
                             # (renamed RVIC at its OTC move; the live RVI is a 2026
                             # Robinhood CEF, wrong era)
}

# Amendment A2 — verified BASE-TICKER TENURE starts for live instruments that
# RECYCLED a dead namesake's base ticker while carrying pre-recycle history
# under the same base (Norgate keys full lineage history to the final symbol,
# so the life interval alone cannot separate the claimants). The date is the
# first session the LIVE instrument traded under this base — the day after the
# predecessor's day-granular last_quoted_date (NDU metadata; there is no
# former-symbol API). Dead instruments need no entry: their tenure end is read
# directly from last_quoted_date by the live directory. Every entry
# NDU-verified 2026-07-18 (security names + dates in the comment).
TICKER_TENURE_OVERRIDES: dict[str, str] = {
    "HR": "2022-07-21",      # predecessor HR-202207 'Healthcare Realty Trust Common'
                             # (fqd 1993-05-27) lqd 2022-07-20; successor = ex-HTA
                             # 'Healthcare Realty Trust Inc Class A Common'
                             # (fqd 2012-06-06) under HR from the next session
    "DOC": "2024-03-01",     # predecessor DOC-202402 'Physicians Realty Trust Common'
                             # lqd 2024-02-29; successor = Healthpeak Properties
                             # (fqd 1990-01-02), PEAK -> DOC at the merger
    "COR": "2021-12-28",     # predecessor COR-202112 'CoreSite Realty Common'
                             # lqd 2021-12-27; successor = Cencora (fqd 1995-04-04),
                             # ABC -> COR rename 2023-08 — the override is the
                             # earliest-possible floor anchored to the predecessor's
                             # last quote; no snapshot rows fall in the dark gap
    "RPT": "2023-12-30",     # predecessor RPT-202312 'RPT Realty Common'
                             # (fqd 1990-01-02) lqd 2023-12-29; successor = 'Rithm
                             # Property Trust Inc Common' (ex-Great Ajax lineage,
                             # fqd 2015-02-13) recycled the base in 2024 — floor
                             # bound as for COR; snapshot RPT rows end 2023-12-29
}


# ---------------------------------------------------------------------------
# Ticker mapping (failure mode 1 — survivorship through the mapping)
# ---------------------------------------------------------------------------

def normalise_ticker(ishares_ticker: str) -> str:
    """Map an iShares constituent ticker to its Norgate BASE symbol.

    Three deterministic steps, all explicit:
      1. Strip a single trailing " US" token (Bloomberg country qualifier seen
         in a handful of snapshot rows, e.g. ``HOLX US``, ``RTX US`` — the same
         instrument as the plain ticker).
      2. Apply a KNOWN_RENAMES entry if one exists (genuine symbol change).
      3. Convert share-class punctuation from the iShares dash form to the
         Norgate dot form (``BRK-B`` -> ``BRK.B``, ``BF-B`` -> ``BF.B``).

    A plain alphabetic ticker maps to itself. This function returns a BASE
    symbol only; instrument-level resolution (delisted suffixes, recycled
    tickers, verified renames — amendment A1) is ``resolve_instrument``. A base
    with no Norgate instrument is counted against coverage downstream, never
    dropped silently.
    """
    t = ishares_ticker.strip().upper()
    if t.endswith(" US"):
        t = t[:-3].strip()
    if t in KNOWN_RENAMES:
        t = KNOWN_RENAMES[t]
    # Share-class punctuation: iShares uses a dash, Norgate uses a dot. Only the
    # separator changes; the class letter is preserved.
    if "-" in t:
        t = t.replace("-", ".")
    return t


# ---------------------------------------------------------------------------
# Instrument resolution (amendment A1, kickoff §5b) — (ticker, date) -> the
# Norgate INSTRUMENT that was quoted under that ticker on that date
# ---------------------------------------------------------------------------

# Norgate marks a delisted instrument with a "-YYYYMM" suffix on its base
# ticker (delisting year-month). All 21,030 symbols in the "US Equities
# Delisted" database carry it (verified against NDU, 2026-07-18).
DELISTED_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<yyyymm>\d{6})$")


def suffix_month_end(yyyymm: str) -> pd.Timestamp:
    """Last calendar day of a delisted suffix's YYYYMM month, via pandas Period
    arithmetic (vault date rule: no manual day offsets; Python datetime months
    are 1-indexed and the suffix encodes the month as 01-12)."""
    period = pd.Period(f"{yyyymm[:4]}-{yyyymm[4:6]}", freq="M")
    return period.end_time.normalize()


@dataclass(frozen=True)
class Instrument:
    """One Norgate instrument: an exact symbol plus its life interval.

    ``first_quoted`` is Norgate's first quoted date for the SECURITY (the full
    lineage under any earlier ticker — Norgate keys an instrument's whole
    history to its final symbol). ``last_valid`` is the delisting suffix month
    end for a delisted instrument, or None for a live one (open-ended). A None
    ``first_quoted`` means the metadata was unavailable; such an instrument can
    never be confirmed to contain a date and is excluded from candidacy (never
    guess).

    Amendment A2 tenure fields — the interval the instrument actually traded
    UNDER ITS BASE TICKER, used only to separate multiple claimants of one
    base (see resolve_instrument): ``tenure_end`` is the dead instrument's
    day-granular last quoted date (NDU metadata); ``tenure_start`` is a live
    successor's verified rename date (TICKER_TENURE_OVERRIDES). None falls
    back to the corresponding life bound."""
    symbol: str
    base: str
    first_quoted: pd.Timestamp | None
    last_valid: pd.Timestamp | None
    tenure_start: pd.Timestamp | None = None
    tenure_end: pd.Timestamp | None = None

    def contains(self, date: pd.Timestamp) -> bool:
        """True iff ``date`` lies within this instrument's life interval."""
        if self.first_quoted is None or date < self.first_quoted:
            return False
        return self.last_valid is None or date <= self.last_valid

    def tenure_contains(self, date: pd.Timestamp) -> bool:
        """True iff this instrument traded under its base ticker on ``date``
        (tenure bounds where known, life bounds otherwise)."""
        start = self.tenure_start if self.tenure_start is not None else self.first_quoted
        end = self.tenure_end if self.tenure_end is not None else self.last_valid
        if start is None or date < start:
            return False
        return end is None or date <= end


class InstrumentDirectory:
    """Base-ticker -> instrument-candidates lookup, built from an explicit
    instrument list. Used directly by the selftests (synthetic instruments);
    the live path subclasses it with lazy Norgate enumeration."""

    def __init__(self, instruments: list[Instrument] | None = None):
        self._by_symbol: dict[str, Instrument] = {}
        self._by_base: dict[str, list[Instrument]] = {}
        for inst in instruments or []:
            self._by_symbol[inst.symbol] = inst
            self._by_base.setdefault(inst.base, []).append(inst)

    def candidates(self, base: str) -> list[Instrument]:
        """Every instrument that ever traded under ``base`` (the plain live
        symbol plus all delisted-suffixed variants)."""
        return list(self._by_base.get(base, []))

    def lookup(self, symbol: str) -> Instrument | None:
        """The instrument with this exact symbol, or None."""
        return self._by_symbol.get(symbol)


class NorgateInstrumentDirectory(InstrumentDirectory):
    """Live directory over the local NDU databases.

    Enumeration is two calls (``database_symbols`` for "US Equities" and
    "US Equities Delisted" — 14,537 + 21,030 symbols, held as in-memory maps);
    ``first_quoted_date`` is then fetched lazily per candidate actually needed
    and memoised, so resolving a line's membership costs a handful of metadata
    calls, not a database sweep. Pattern per the solved reference
    implementation (event-studies scripts/norgate_universe.py)."""

    _LIVE_DB = "US Equities"
    _DELISTED_DB = "US Equities Delisted"

    def __init__(self):
        super().__init__()
        nd = _norgate()
        self._nd = nd
        self._live: set[str] = set(nd.database_symbols(self._LIVE_DB))
        self._suffixed_by_base: dict[str, list[str]] = {}
        for sym in nd.database_symbols(self._DELISTED_DB):
            m = DELISTED_SUFFIX_RE.match(sym)
            if m:
                self._suffixed_by_base.setdefault(m.group("base"), []).append(sym)
        self._fqd_memo: dict[str, pd.Timestamp | None] = {}
        self._lqd_memo: dict[str, pd.Timestamp | None] = {}

    def _first_quoted(self, symbol: str) -> pd.Timestamp | None:
        if symbol not in self._fqd_memo:
            try:
                raw = self._nd.first_quoted_date(symbol)
                self._fqd_memo[symbol] = pd.Timestamp(str(raw))
            except Exception:  # noqa: BLE001 — unavailable metadata -> None
                self._fqd_memo[symbol] = None
        return self._fqd_memo[symbol]

    def _last_quoted(self, symbol: str) -> pd.Timestamp | None:
        """Day-granular last quoted date (A2 tenure end for dead instruments).
        Populated by NDU for delisted symbols; None (or an error) simply leaves
        the month-end life bound in charge."""
        if symbol not in self._lqd_memo:
            try:
                raw = self._nd.last_quoted_date(symbol)
                self._lqd_memo[symbol] = (pd.Timestamp(str(raw))
                                          if raw is not None else None)
            except Exception:  # noqa: BLE001 — unavailable metadata -> None
                self._lqd_memo[symbol] = None
        return self._lqd_memo[symbol]

    def _instrument(self, symbol: str) -> Instrument | None:
        m = DELISTED_SUFFIX_RE.match(symbol)
        if m:
            if symbol not in self._suffixed_by_base.get(m.group("base"), []):
                return None
            return Instrument(symbol=symbol, base=m.group("base"),
                              first_quoted=self._first_quoted(symbol),
                              last_valid=suffix_month_end(m.group("yyyymm")),
                              tenure_end=self._last_quoted(symbol))
        if symbol in self._live:
            override = TICKER_TENURE_OVERRIDES.get(symbol)
            return Instrument(symbol=symbol, base=symbol,
                              first_quoted=self._first_quoted(symbol),
                              last_valid=None,
                              tenure_start=(pd.Timestamp(override)
                                            if override is not None else None))
        return None

    def candidates(self, base: str) -> list[Instrument]:
        out: list[Instrument] = []
        if base in self._live:
            inst = self._instrument(base)
            if inst is not None:
                out.append(inst)
        for sym in self._suffixed_by_base.get(base, []):
            inst = self._instrument(sym)
            if inst is not None:
                out.append(inst)
        return out

    def lookup(self, symbol: str) -> Instrument | None:
        return self._instrument(symbol)


_DEFAULT_DIRECTORY: NorgateInstrumentDirectory | None = None


def default_instrument_directory() -> NorgateInstrumentDirectory:
    """Process-wide memoised live directory (one NDU enumeration per run)."""
    global _DEFAULT_DIRECTORY
    if _DEFAULT_DIRECTORY is None:
        _DEFAULT_DIRECTORY = NorgateInstrumentDirectory()
    return _DEFAULT_DIRECTORY


def resolve_instrument(ishares_ticker: str, membership_date: pd.Timestamp,
                       directory: InstrumentDirectory,
                       renames: dict[str, str] | None = None
                       ) -> tuple[str | None, str]:
    """Resolve (ticker, membership date) to the exact Norgate instrument symbol.

    Deterministic chain (amendments A1 + A2; never guess):
      1. Native candidates of the base — the plain live symbol plus every
         delisted-suffixed variant. Exactly one whose life interval contains
         the membership date -> resolved.
      1b. More than one life-interval claimant -> BASE-TICKER TENURE
         refinement (A2): keep candidates that actually traded under the base
         ticker on the date (dead instruments up to their day-granular last
         quoted date; a recycling live successor from its verified rename
         date). Exactly one survivor -> resolved as "tenure"; zero or several
         (no tenure information, or a genuine overlap) -> AMBIGUOUS ->
         unresolved, counted, never guessed.
      2. Zero native matches -> the verified rename table: the entry names one
         exact target instrument (live or suffixed), accepted only if ITS
         LIFE interval contains the date (the snapshot ticker in that era was
         the SOURCE name, so the target's tenure over its own base is not the
         test). Renames fire only when the native symbol has no instrument
         claiming the date, so a recycled base resolves to its own era's
         instrument first (e.g. old-era CHK to the delisted instrument,
         post-rename dates through the table).
      3. Otherwise unresolved.

    Returns (symbol | None, status) with status in {"native", "tenure",
    "renamed", "unresolved", "ambiguous"}; a None symbol always counts against
    coverage.
    """
    base = normalise_ticker(ishares_ticker)
    date = pd.Timestamp(membership_date)
    native = [c for c in directory.candidates(base) if c.contains(date)]
    if len(native) == 1:
        return native[0].symbol, "native"
    if len(native) > 1:
        tenured = [c for c in native if c.tenure_contains(date)]
        if len(tenured) == 1:
            return tenured[0].symbol, "tenure"
        return None, "ambiguous"
    table = INSTRUMENT_RENAMES if renames is None else renames
    target = table.get(base)
    if target is not None:
        inst = directory.lookup(target)
        if inst is not None and inst.contains(date):
            return inst.symbol, "renamed"
    return None, "unresolved"


def resolve_membership(snapshots: dict, directory: InstrumentDirectory,
                       window_end: pd.Timestamp = WINDOW_END,
                       renames: dict[str, str] | None = None) -> dict:
    """Resolve every in-window snapshot roster to instrument symbols.

    The membership date of a snapshot is its KEY (the target Friday the roster
    is asserted for); the suffix month-end convention gives the resolution
    slack for an intra-week delisting. Returns::

        {"by_snapshot": {Timestamp(key): {ishares_ticker: symbol | None}},
         "instruments": sorted union of resolved symbols,
         "unresolved":  {ishares_ticker: {"status": ..., "n_weeks": int}},
         "n_member_weeks": int, "n_resolved_weeks": int}

    Unresolved and ambiguous names are counted per ticker (never silently
    dropped); the per-week None entries flow into select_basket / G1 as
    uncovered member-weeks.
    """
    by_snapshot: dict[pd.Timestamp, dict[str, str | None]] = {}
    instruments: set[str] = set()
    unresolved: dict[str, dict] = {}
    n_weeks = 0
    n_resolved = 0
    for key in sorted(snapshots.keys()):
        ts = pd.Timestamp(key)
        if ts > window_end:
            continue
        row: dict[str, str | None] = {}
        for ish in snapshots[key].get("tickers", []):
            sym, status = resolve_instrument(ish, ts, directory, renames=renames)
            row[ish] = sym
            n_weeks += 1
            if sym is None:
                rec = unresolved.setdefault(ish, {"status": status, "n_weeks": 0})
                rec["n_weeks"] += 1
            else:
                instruments.add(sym)
                n_resolved += 1
        by_snapshot[ts] = row
    return {"by_snapshot": by_snapshot,
            "instruments": sorted(instruments),
            "unresolved": unresolved,
            "n_member_weeks": n_weeks,
            "n_resolved_weeks": n_resolved}


# ---------------------------------------------------------------------------
# Membership snapshots (failure mode 2 — as-of alignment)
# ---------------------------------------------------------------------------

def load_constituents(line: str) -> dict:
    """Load the committed point-in-time membership cache for a line.

    Schema (per KICKOFF §2): ``{etf, source, ..., snapshots: {"YYYY-MM-DD"
    (target Friday): {actual_date, n_tickers, tickers: [...]}}}``. The tickers
    list is weight-sorted (cap-rank order); this loader preserves that order.
    """
    import json
    path = DATA_DIR / f"constituents_{line.lower()}.json"
    if not path.exists():
        raise FileNotFoundError(f"No constituents cache at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_member_weights(line: str) -> dict[pd.Timestamp, dict[str, float]]:
    """Load a line's A3 Step-0 weight table, validating the weighting basis.

    Returns {snapshot Timestamp: {snapshot ticker: Weight (%)}} for the line,
    or raises: a missing file means the Step-0 stage has not been run, and a
    basis mismatch means the cached table was built under a different
    weighting regime — both must FAIL FAST rather than let a stale basis leak
    into the panels (the A3 cache-invalidation guarantee)."""
    path = WS6_WEIGHTS_DIR / f"{line.lower()}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No A3 weights table at {path} — run scripts/fetch_ws6_weights.py")
    import json
    doc = json.loads(path.read_text(encoding="utf-8"))
    basis = doc.get("basis")
    if basis != WEIGHTING_BASIS:
        raise ValueError(
            f"{path} carries basis {basis!r}, expected {WEIGHTING_BASIS!r} — "
            "rebuild the Step-0 weights stage")
    return {pd.Timestamp(k): {t: float(v) for t, v in row.items()}
            for k, row in doc.get("weights", {}).items()}


def snapshot_asof(snapshots: dict, asof_date: pd.Timestamp
                  ) -> tuple[pd.Timestamp | None, list[str]]:
    """Return the (snapshot_date, tickers) whose target-Friday key is the latest
    on or before ``asof_date`` — the membership KNOWN as of that date.

    ``asof_date`` is passed as the prior trading day (t-1) of the rebalance, so a
    rebalance on Friday D reads the previous Friday's roster and NEVER the same
    week's forward-dated snapshot (failure mode 2). Returns (None, []) when
    ``asof_date`` precedes the first snapshot — the caller treats an empty roster
    as "no basket this week" and reverts the line to its ETF.
    """
    keys = sorted(snapshots.keys())
    chosen = None
    for k in keys:
        if pd.Timestamp(k) <= asof_date:
            chosen = k
        else:
            break
    if chosen is None:
        return None, []
    return pd.Timestamp(chosen), list(snapshots[chosen].get("tickers", []))


# ---------------------------------------------------------------------------
# Per-name signals (computed on the member calendar; read as-of t-1)
# ---------------------------------------------------------------------------

def _min_periods(period: int) -> int:
    return max(1, int(period * MIN_PERIODS_FRACTION))


def precompute_member_signals(prices: pd.DataFrame,
                              ma_period: int = TREND_MA,
                              mom_days: int = PLACEBO_MOM_DAYS) -> dict:
    """Trailing per-name signal frames for a line's member price panel.

    Every quantity is a trailing rolling window up to and including each date;
    the CALLER selects the row as of t-1 (``select_basket``), so there is no
    look-ahead here. Returns a dict of boolean/float DataFrames aligned to
    ``prices``:

      state    : close > SMA200(close), valid only where both are defined — the
                 same binary state as the sleeve's breadth (a member "trending").
      strength : close / SMA200 - 1 (Design 2 rank key); NaN where SMA invalid.
      momentum : mom_days total return (the placebo rank key; NaN before warm-up).

    ``prices`` NaNs (a name not yet listed, a gap, or a delisting) propagate to
    NaN signals for that name/day — such a name cannot pass the screen and is
    reported, never silently counted.
    """
    sma = prices.rolling(ma_period, min_periods=_min_periods(ma_period)).mean()
    valid = prices.notna() & sma.notna()
    state = (prices > sma) & valid
    strength = (prices / sma - 1.0).where(valid)
    momentum = prices.pct_change(mom_days)
    return {"state": state, "strength": strength, "momentum": momentum,
            "valid": valid, "prices": prices}


def _asof_pos(index: pd.DatetimeIndex, asof_date: pd.Timestamp) -> int:
    """Positional index of the last calendar entry on or before ``asof_date``;
    -1 when ``asof_date`` precedes the whole index. Robust to a member calendar
    that differs from the deployed trade calendar (US members vs a London ETF
    proxy) — the as-of row is always <= t-1, so no look-ahead."""
    return int(index.searchsorted(asof_date, side="right")) - 1


# ---------------------------------------------------------------------------
# Arm register (frozen §2) and basket selection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArmSpec:
    """One register arm. ``is_etf_baseline`` marks E0 (every line expressed as
    its own ETF); the remaining fields drive basket construction for the 11
    single-named lines. Broad slices stay ETFs regardless of the spec."""
    arm_id: str
    label: str
    is_etf_baseline: bool = False
    pool: str = "topM"        # "topM" (first M cap-rank) or "full" (all members)
    screen: bool = True       # apply the trend-state screen
    rank_key: str = "none"    # "none" | "strength" | "momentum"
    select_n: int | None = None   # top-N cap after ranking; None = take all


# Register #0-#7, frozen. Order is the register order.
ARM_REGISTER: tuple[ArmSpec, ...] = (
    ArmSpec("E0", "deployed ETF baseline", is_etf_baseline=True),
    ArmSpec("I0", "unscreened top-M EW basket",
            pool="topM", screen=False, rank_key="none", select_n=None),
    ArmSpec("I1", "screened top-M EW basket (Design 1)",
            pool="topM", screen=True, rank_key="none", select_n=None),
    ArmSpec("I2", "top-N by strength, screened (Design 2)",
            pool="full", screen=True, rank_key="strength", select_n=N_SELECT),
    ArmSpec("P2", "top-N by 126d momentum, screened (placebo)",
            pool="full", screen=True, rank_key="momentum", select_n=N_SELECT),
    ArmSpec("I2-N15", "top-N=15 by strength (neighbour)",
            pool="full", screen=True, rank_key="strength", select_n=N_NEIGHBOUR),
    ArmSpec("P2-N15", "top-N=15 by momentum (neighbour)",
            pool="full", screen=True, rank_key="momentum", select_n=N_NEIGHBOUR),
    ArmSpec("I1-all", "screened EW over all members (neighbour)",
            pool="full", screen=True, rank_key="none", select_n=None),
)

ARM_BY_ID: dict[str, ArmSpec] = {a.arm_id: a for a in ARM_REGISTER}


@dataclass
class BasketResult:
    """Outcome of one line-week basket selection.

    ``fallback`` True means the line reverts to its ETF this week (too few names
    passed the screen, or the roster/prices were unavailable); the caller then
    holds the line's ETF and increments the fallback counter. Otherwise
    ``weights`` maps Norgate symbols to within-line weights summing to 1.0.
    All diagnostics are counted, never dropped silently (failure mode 1)."""
    fallback: bool
    reason: str = ""
    weights: dict[str, float] = field(default_factory=dict)
    n_pool: int = 0            # names considered (post top-M truncation)
    n_covered: int = 0         # pool names with a Norgate price column
    n_present: int = 0         # covered names with a price as of t-1
    n_pass: int = 0            # names passing the screen
    n_selected: int = 0        # names actually held
    uncovered: list[str] = field(default_factory=list)   # no Norgate data
    missing_price: list[str] = field(default_factory=list)  # gap/not-listed at t-1
    dropped_no_rank: list[str] = field(default_factory=list)  # NaN rank key
    # A3 weighting provenance for this line-week: "snapshot" (true weights from
    # the week's own snapshot), "carried" (last known weights carried forward),
    # "ew" (equal-weight fallback — no usable weights), or "" on fallback.
    weight_source: str = ""


def _true_basket_weights(candidates: list[str], sym_to_ish: dict[str, str],
                         weights_by_key: dict[pd.Timestamp, dict[str, float]] | None,
                         snap_date: pd.Timestamp
                         ) -> tuple[dict[str, float], str]:
    """A3 within-line weights for the selected members.

    True snapshot Weight (%) renormalised over ``candidates`` (the selected
    set — the whole present pool for I0, the screen survivors for I1, the
    top-N for the selection arms). Weight lookup is by SNAPSHOT ticker
    (``sym_to_ish``) against the week's own snapshot; a snapshot absent from
    the weight table carries the line's latest EARLIER weights forward
    ("carried"). A selected member without a positive weight under the
    effective map — or no weights at all — drops the whole line-week to equal
    weight ("ew"), never a silently mixed basis. Returns (weights, source)."""
    n = len(candidates)
    ew = {s: 1.0 / n for s in candidates}
    if not weights_by_key:
        return ew, "ew"
    if snap_date in weights_by_key:
        w_map, source = weights_by_key[snap_date], "snapshot"
    else:
        earlier = [k for k in weights_by_key if k < snap_date]
        if not earlier:
            return ew, "ew"
        w_map, source = weights_by_key[max(earlier)], "carried"
    vals = {}
    for s in candidates:
        v = w_map.get(sym_to_ish.get(s, s))
        if v is None or v <= 0.0:
            return ew, "ew"    # unusable weight -> uniform basis, counted
        vals[s] = float(v)
    total = sum(vals.values())
    return {s: v / total for s, v in vals.items()}, source


def select_basket(spec: ArmSpec, eff_date: pd.Timestamp | None,
                  snapshots: dict, prices: pd.DataFrame, sig: dict,
                  resolution: dict[pd.Timestamp, dict[str, str | None]] | None = None,
                  weights: dict[pd.Timestamp, dict[str, float]] | None = None
                  ) -> BasketResult:
    """Build one single-named line's basket for a rebalance whose effective
    (t-1) date is ``eff_date``, per the arm ``spec``.

    Steps (all as of ``eff_date`` — no look-ahead):
      1. Roster: ``snapshot_asof`` (membership on or before t-1), cap-rank order.
      2. Pool: first M for ``pool == "topM"``, else the full roster.
      3. Map iShares -> Norgate INSTRUMENT via ``resolution`` (amendment A1:
         the per-snapshot output of ``resolve_membership``, so a delisted or
         recycled ticker keys the era-correct instrument column). A name whose
         resolution is None, or whose instrument has no price column, is
         UNCOVERED (counted, excluded — never silently dropped). ``resolution``
         None falls back to base-symbol identity — the degenerate case for
         synthetic panels keyed by plain tickers; the live path always passes
         the resolved mapping, and a delisted member under identity mapping has
         no price column, so misuse surfaces as uncovered weeks in G1 rather
         than silent survivorship.
      4. Present: covered names with a price as of t-1 (a gap / not-yet-listed /
         already-delisted name is missing_price — counted, excluded).
      5. Screen (if ``spec.screen``): keep names with trend state True. Fewer
         than MIN_PASS passing -> FALLBACK to the ETF (frequency reported).
      6. Rank (if ``spec.rank_key`` in {strength, momentum}): sort passing names
         by the key, drop NaN-key names (counted), keep the top ``select_n``.
      7. Weights inside the resulting basket (amendment A3): TRUE snapshot
         Weight (%) renormalised over the selected members via
         ``_true_basket_weights`` — the week's own snapshot when present, the
         line's last known weights carried forward otherwise, and an
         equal-weight fallback (counted) when no usable weight exists.
         ``weights`` None (the synthetic/degenerate path) is the equal-weight
         case throughout.
    """
    if eff_date is None:
        return BasketResult(fallback=True, reason="no effective date (pre-window)")

    snap_date, roster = snapshot_asof(snapshots, eff_date)
    if not roster:
        return BasketResult(fallback=True, reason="no roster as of t-1")

    pool_ish = roster[:M_POOL] if spec.pool == "topM" else list(roster)

    # Map to the era-correct Norgate instrument; split covered vs uncovered
    # against the price panel columns.
    res_map = resolution.get(snap_date, {}) if resolution is not None else None
    price_cols = set(prices.columns)
    covered: list[str] = []
    uncovered: list[str] = []
    seen: set[str] = set()
    sym_to_ish: dict[str, str] = {}   # instrument -> snapshot ticker (A3 weights)
    for ish in pool_ish:
        if res_map is not None:
            sym = res_map.get(ish)
            if sym is None:
                uncovered.append(normalise_ticker(ish))   # unresolved name
                continue
        else:
            sym = normalise_ticker(ish)
        if sym in seen:
            continue          # a roster can list a name once; guard duplicates
        seen.add(sym)
        sym_to_ish[sym] = ish
        if sym in price_cols:
            covered.append(sym)
        else:
            uncovered.append(sym)

    # As-of row on the member calendar (<= t-1).
    pos = _asof_pos(prices.index, eff_date)
    if pos < 0:
        return BasketResult(fallback=True, reason="no member prices as of t-1",
                            n_pool=len(pool_ish), n_covered=len(covered),
                            uncovered=uncovered)
    price_row = prices.iloc[pos]
    state_row = sig["state"].iloc[pos]
    strength_row = sig["strength"].iloc[pos]
    momentum_row = sig["momentum"].iloc[pos]

    present: list[str] = []
    missing_price: list[str] = []
    for sym in covered:
        if bool(pd.notna(price_row.get(sym))):
            present.append(sym)
        else:
            missing_price.append(sym)

    if spec.screen:
        passing = [s for s in present if bool(state_row.get(s, False))]
    else:
        passing = list(present)
    n_pass = len(passing)

    # Fallback rule (§2): a screened line with fewer than MIN_PASS names passing
    # reverts to its ETF. An unscreened line (I0) only falls back if it has no
    # holdable name at all.
    if spec.screen and n_pass < MIN_PASS:
        return BasketResult(
            fallback=True, reason=f"only {n_pass} passed screen (< {MIN_PASS})",
            n_pool=len(pool_ish), n_covered=len(covered), n_present=len(present),
            n_pass=n_pass, uncovered=uncovered, missing_price=missing_price)

    candidates = passing
    dropped_no_rank: list[str] = []
    if spec.rank_key in ("strength", "momentum"):
        key_row = strength_row if spec.rank_key == "strength" else momentum_row
        ranked = []
        for s in candidates:
            v = key_row.get(s)
            if pd.isna(v):
                dropped_no_rank.append(s)   # e.g. < mom_days history for momentum
            else:
                ranked.append((s, float(v)))
        ranked.sort(key=lambda kv: kv[1], reverse=True)
        if spec.select_n is not None:
            ranked = ranked[:spec.select_n]
        candidates = [s for s, _ in ranked]

    if not candidates:
        return BasketResult(
            fallback=True, reason="empty basket after screen/rank",
            n_pool=len(pool_ish), n_covered=len(covered), n_present=len(present),
            n_pass=n_pass, uncovered=uncovered, missing_price=missing_price,
            dropped_no_rank=dropped_no_rank)

    basket_weights, weight_source = _true_basket_weights(
        candidates, sym_to_ish, weights, snap_date)
    return BasketResult(
        fallback=False, reason="",
        weights=basket_weights, n_pool=len(pool_ish), n_covered=len(covered),
        n_present=len(present), n_pass=n_pass, n_selected=len(candidates),
        uncovered=uncovered, missing_price=missing_price,
        dropped_no_rank=dropped_no_rank, weight_source=weight_source)


# ---------------------------------------------------------------------------
# Sector layer — deployed Phase 20.1 Sleeve A, reused verbatim
# ---------------------------------------------------------------------------

def demean(panel: pd.DataFrame) -> pd.DataFrame:
    """Phase 20 cross-sectional demeaning — sector-relative breadth = absolute
    breadth minus the per-date cross-sectional mean. Identical to
    run_strategy_a_universe_gate.relative_breadth_signal and
    run_ws5_relative_trend.demean (kept local so the engine does not import the
    WS5 run harness)."""
    return panel.sub(panel.mean(axis=1, skipna=True), axis=0)


def deployed_eligible_start(closes: pd.DataFrame, breadths: pd.DataFrame,
                            used: list[str]) -> pd.Timestamp:
    """Deployed eligible-start rule (run_portfolio.main / run_topk_robustness):
    the latest per-ETF first breadth date, plus the 200d warm-up, snapped to the
    first trading day on or after."""
    starts = [breadths[e].dropna().index.min() for e in used
              if breadths[e].notna().any()]
    eligible = max(starts)
    eligible = pd.Timestamp(eligible.date()) + pd.Timedelta(days=MA_PERIOD)
    if (closes.index >= eligible).any():
        return closes.index[closes.index >= eligible][0]
    return closes.index[MA_PERIOD]


def deployed_sector_layer(window_end: pd.Timestamp = WINDOW_END,
                          k: int = K_DEPLOYED,
                          cost_bps: int = DEPLOYED_COST_BPS) -> dict:
    """Assemble the shared sector book from the deployed engine.

    Returns a dict with ``closes`` (ETF proxy closes), ``breadths`` (deployed
    absolute breadth panel), ``used`` (the 14 lines), ``eligible`` (start), the
    demeaned ``signal``, the ``rebal_dates`` and the deployed E0 ``weights`` /
    ``equity``. Every arm consumes ``weights`` as its per-line book; E0 IS this
    equity. This function performs live data loads (build_panels reads the
    committed membership + local price/OHLC caches, with a yfinance fallback),
    so it belongs to T3 and to the offline-guarded parity test — never to the
    frozen synthetic selftests.
    """
    closes, breadths, used = build_panels()
    closes = closes.loc[:window_end]
    breadths = breadths.loc[:window_end]
    eligible = deployed_eligible_start(closes, breadths, used)
    signal = demean(breadths)
    res = run_portfolio(closes, signal, top_k_breadth_weight(k), eligible,
                        cost=cost_bps / 10_000, rebalance_freq=REBAL_FREQ)
    # Take the grid from the run that produced these weights. Deriving it
    # independently let the two drift apart the moment the cadence rule
    # changed (the holiday-aware adoption, 2026-08-10): run_portfolio moved to
    # the new rule while a separate weekly_rebalance_dates call kept the old
    # default, and the arm builder then rebuilt weights on a grid the sector
    # book had never used.
    rebal_dates = res["rebalance_dates"]
    return {"closes": closes, "breadths": breadths, "used": used,
            "eligible": eligible, "signal": signal,
            "rebal_dates": rebal_dates,
            "weights": res["weights"], "equity": res["equity"]}


# ---------------------------------------------------------------------------
# Name-level weight assembly (the arm builder)
# ---------------------------------------------------------------------------

@dataclass
class ArmBuild:
    """Assembled arm: the daily name-level weight panel plus per-line
    diagnostics (fallback frequency, coverage, basket sizes, A3 weighting
    provenance) for the record."""
    name_weights: pd.DataFrame
    fallback_weeks: dict[str, int]
    basket_sizes: dict[str, list[int]]
    uncovered_seen: dict[str, set]
    missing_seen: dict[str, set]
    weeks_evaluated: dict[str, int]
    # A3: line-weeks whose basket used carried-forward weights, and line-weeks
    # that fell back to equal weight (no usable true weights; expected zero on
    # the real data).
    weight_carry_weeks: dict[str, int] = field(default_factory=dict)
    weight_ew_weeks: dict[str, int] = field(default_factory=dict)


def _add(row: dict, name: str, w: float) -> None:
    row[name] = row.get(name, 0.0) + w


def build_arm_name_weights(spec: ArmSpec, sector_weights: pd.DataFrame,
                           closes: pd.DataFrame, rebal_dates: pd.DatetimeIndex,
                           eligible: pd.Timestamp,
                           membership: dict, member_signals: dict,
                           member_prices: dict,
                           member_resolution: dict | None = None,
                           member_weights: dict | None = None) -> ArmBuild:
    """Distribute the shared per-line book into a daily name-level weight panel.

    ``sector_weights`` is the deployed E0 weight panel (columns = the 14 lines,
    daily, already ``shift(1)``-clean). For each rebalance, each held line's
    weight is expressed either as its ETF (E0, broad slices, or a fallback week)
    or as its arm-specific member basket; the within-line basket weights sum to
    1.0, so the name-level book preserves the sector book exactly (no weight
    leakage — pinned by a selftest). The rebalance-level rows are then reindexed
    to the daily calendar with forward fill and zeroed before ``eligible``,
    identical to run_portfolio's own weight-panel construction — which is why E0
    reproduces the deployed weights to 0.0.

    ``membership`` / ``member_signals`` / ``member_prices`` are keyed by line
    (single-named lines only). ``member_resolution`` (amendment A1) carries the
    per-line ``resolve_membership(...)['by_snapshot']`` maps so baskets are
    keyed by instrument; a line absent from it uses the identity mapping (the
    synthetic degenerate case — see select_basket). ``member_weights``
    (amendment A3) carries the per-line ``load_member_weights(...)`` tables;
    a line absent from it weights its baskets equally (the pre-A3 degenerate
    case, counted in ``weight_ew_weeks``). All are dependency-injected so the
    selftests can drive the builder on synthetic panels and T3 on the Norgate
    caches.
    """
    lines = list(sector_weights.columns)
    fallback_weeks = {L: 0 for L in SINGLE_NAMED_LINES if L in lines}
    basket_sizes: dict[str, list[int]] = {L: [] for L in fallback_weeks}
    uncovered_seen: dict[str, set] = {L: set() for L in fallback_weeks}
    missing_seen: dict[str, set] = {L: set() for L in fallback_weeks}
    weeks_evaluated = {L: 0 for L in fallback_weeks}
    weight_carry_weeks = {L: 0 for L in fallback_weeks}
    weight_ew_weeks = {L: 0 for L in fallback_weeks}

    rb_rows: dict[pd.Timestamp, dict] = {}
    for rd in rebal_dates:
        pos = closes.index.get_loc(rd)
        eff_date = closes.index[pos - 1] if pos > 0 else None
        line_w = sector_weights.loc[rd]
        row: dict[str, float] = {}
        for L in lines:
            w = float(line_w.get(L, 0.0))
            if w <= 0.0:
                continue
            # E0 and the broad slices are always their own ETF.
            if spec.is_etf_baseline or L in BROAD_SLICES:
                _add(row, L, w)
                continue
            # Single-named line under a basket arm.
            weeks_evaluated[L] += 1
            resolution = (member_resolution or {}).get(L)
            line_weights = (member_weights or {}).get(L)
            basket = select_basket(spec, eff_date, membership[L],
                                   member_prices[L], member_signals[L],
                                   resolution=resolution, weights=line_weights)
            uncovered_seen[L].update(basket.uncovered)
            missing_seen[L].update(basket.missing_price)
            if basket.fallback:
                fallback_weeks[L] += 1
                _add(row, L, w)          # revert this line to its ETF
                continue
            if basket.weight_source == "carried":
                weight_carry_weeks[L] += 1
            elif basket.weight_source == "ew":
                weight_ew_weeks[L] += 1
            basket_sizes[L].append(basket.n_selected)
            for sym, bw in basket.weights.items():
                _add(row, sym, w * bw)
        rb_rows[rd] = row

    all_names = sorted({n for r in rb_rows.values() for n in r})
    rb_df = pd.DataFrame(0.0, index=rebal_dates, columns=all_names)
    for rd, row in rb_rows.items():
        for name, w in row.items():
            rb_df.at[rd, name] = w
    panel = rb_df.reindex(closes.index, method="ffill").fillna(0.0)
    panel.loc[panel.index < eligible] = 0.0
    return ArmBuild(name_weights=panel, fallback_weeks=fallback_weeks,
                    basket_sizes=basket_sizes, uncovered_seen=uncovered_seen,
                    missing_seen=missing_seen, weeks_evaluated=weeks_evaluated,
                    weight_carry_weeks=weight_carry_weeks,
                    weight_ew_weeks=weight_ew_weeks)


# ---------------------------------------------------------------------------
# Returns and simulation (mirror run_portfolio's mechanics exactly)
# ---------------------------------------------------------------------------

def build_name_return_panel(closes: pd.DataFrame,
                            member_prices: pd.DataFrame | None) -> pd.DataFrame:
    """Daily returns for every holdable name on the deployed trade calendar.

    Line-code columns (ETF / broad-slice / fallback holdings) take the deployed
    ETF proxy return. Member columns take the Norgate member return: the native
    price panel is reindexed to the trade calendar and forward-filled BEFORE
    differencing, so a member delisting mid-hold earns its final print then sits
    flat until the next rebalance drops it (exits at its final print, §2), and a
    US member day absent from a London-proxy calendar carries rather than
    fabricates a return. A member symbol must never collide with a line code."""
    line_rets = closes.pct_change().fillna(0.0)
    if member_prices is None or member_prices.shape[1] == 0:
        return line_rets
    # A name can sit in two lines' rosters (e.g. a semiconductor in both SOXX
    # and IUIS), so the combined panel may carry the same Norgate symbol twice.
    # The series are identical regardless of which line requested them, so keep
    # the first — a duplicate column would otherwise break the reindex in
    # simulate_arm and silently double-count the name.
    member_prices = member_prices.loc[:, ~member_prices.columns.duplicated(keep="first")]
    clash = set(member_prices.columns) & set(closes.columns)
    assert not clash, f"member/line ticker collision: {sorted(clash)}"
    mem = member_prices.reindex(closes.index).ffill()
    mem_rets = mem.pct_change().fillna(0.0)
    return pd.concat([line_rets, mem_rets], axis=1)


def simulate_arm(name_weights: pd.DataFrame, name_returns: pd.DataFrame,
                 cost_bps: float) -> dict:
    """Simulate an arm from its name-level weights and returns.

    Identical mechanics to run_portfolio.run_portfolio: yesterday's weights earn
    today's return; turnover is the full-vector one-way weight change and pays
    ``cost_bps`` (so sector-rotation, screen and membership churn all cost).
    With E0's weights, the deployed ETF returns and 2 bps this reproduces the
    deployed sleeve equity to 0.0."""
    rets = name_returns.reindex(columns=name_weights.columns).fillna(0.0)
    w = name_weights
    port_ret = (w.shift(1).fillna(0.0) * rets).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(0.0)
    port_ret = port_ret - turnover * (cost_bps / 10_000)
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "turnover": turnover, "daily": port_ret}


def run_arm(spec: ArmSpec, sector: dict, membership: dict,
            member_signals: dict, member_prices_by_line: dict,
            combined_member_prices: pd.DataFrame | None,
            cost_bps: float, member_resolution: dict | None = None,
            member_weights: dict | None = None) -> dict:
    """Convenience: build an arm and simulate it at one cost. E0 ignores the
    member inputs. Provided for the T3 harness and the selftests; this module
    never invokes it across the register (that is T3's job)."""
    build = build_arm_name_weights(
        spec, sector["weights"], sector["closes"], sector["rebal_dates"],
        sector["eligible"], membership, member_signals, member_prices_by_line,
        member_resolution=member_resolution, member_weights=member_weights)
    returns = build_name_return_panel(sector["closes"], combined_member_prices)
    sim = simulate_arm(build.name_weights, returns, cost_bps)
    return {"build": build, **sim}


# ---------------------------------------------------------------------------
# Norgate member-price fetch and cache (basket side only; licence-guarded)
# ---------------------------------------------------------------------------

def _norgate():
    """Import norgatedata lazily and assert the local updater is running. Kept
    out of module import so the synthetic selftests need neither the package nor
    NDU. STOP-and-report contract (§ kickoff): callers do not work around a
    down feed."""
    import norgatedata as nd
    if not nd.status():
        raise RuntimeError("NDU (Norgate Data Updater) is not running")
    return nd


def fetch_member_prices(symbols: list[str], start: str, end: str,
                        report: dict | None = None) -> pd.DataFrame:
    """Fetch Norgate TOTALRETURN-adjusted closes for ``symbols`` (delisted DB
    included). Padding NONE, following the in-repo pattern
    (run_norgate_feed_reconciliation / publish_norgate_breadth). A symbol with
    no Norgate data is recorded in ``report['uncovered']`` and omitted — never
    silently dropped. Returns a price panel (columns = resolved symbols)."""
    nd = _norgate()
    cols: dict[str, pd.Series] = {}
    uncovered: list[str] = []
    for sym in symbols:
        try:
            df = nd.price_timeseries(
                sym,
                stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
                padding_setting=nd.PaddingType.NONE,
                start_date=start, end_date=end,
                timeseriesformat="pandas-dataframe",
            )
        except Exception:  # noqa: BLE001 — any resolution failure is "uncovered"
            df = None
        if df is None or "Close" not in getattr(df, "columns", []) or len(df) == 0:
            uncovered.append(sym)
            continue
        s = df["Close"].astype(float)
        s.index = pd.to_datetime(s.index).tz_localize(None)
        cols[sym] = s[~s.index.duplicated(keep="first")]
    if report is not None:
        report["uncovered"] = uncovered
        report["resolved"] = list(cols.keys())
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index()


def member_cache_path(line: str) -> Path:
    """Git-ignored per-line raw-price cache (licence guard)."""
    return DATA_LOCAL_WS6 / f"prices_{line.lower()}.parquet"


def line_member_universe(line: str, window_end: pd.Timestamp = WINDOW_END,
                         directory: InstrumentDirectory | None = None
                         ) -> tuple[list[str], dict]:
    """The union of Norgate INSTRUMENTS a line ever needs over the window
    (amendment A1): every in-window snapshot roster resolved through
    ``resolve_instrument`` at its snapshot date, so delisted members appear
    under their suffixed instrument symbols and recycled tickers under the
    era-correct instrument. Returns (instrument_symbols, report); the report
    carries the per-snapshot resolution (``resolution``, keyed by snapshot
    Timestamp — feed this to select_basket / build_arm_name_weights), the
    per-ticker ``unresolved`` counts (these member-weeks count against G1
    coverage), and the week totals. ``directory`` defaults to the live NDU
    directory; the selftests inject a synthetic one."""
    data = load_constituents(line)
    snapshots = data.get("snapshots", {})
    if directory is None:
        directory = default_instrument_directory()
    res = resolve_membership(snapshots, directory, window_end=window_end)
    n_unique = len({ish for row in res["by_snapshot"].values() for ish in row})
    report = {"line": line,
              "n_ishares_unique": n_unique,
              "n_instruments": len(res["instruments"]),
              "resolution": res["by_snapshot"],
              "unresolved": res["unresolved"],
              "n_member_weeks": res["n_member_weeks"],
              "n_resolved_weeks": res["n_resolved_weeks"]}
    return res["instruments"], report


def smoke_test_iufs(window_end: pd.Timestamp = WINDOW_END) -> dict:
    """Prove the Norgate fetch/cache path on ONE line (IUFS) only — the full-
    universe fetch belongs to T3. Fetches the latest in-window snapshot's top-M
    pool, caches the raw panel under the git-ignored data_local/ws6/ tree, and
    returns a report (mapping size, unmapped/uncovered symbols, panel shape).
    Raises via _norgate() if NDU is down (STOP-and-report; no work-around)."""
    line = "IUFS"
    data = load_constituents(line)
    snapshots = data.get("snapshots", {})
    in_window = [k for k in sorted(snapshots.keys())
                 if pd.Timestamp(k) <= window_end]
    latest_key = in_window[-1]
    roster = list(snapshots[latest_key].get("tickers", []))
    pool_ish = roster[:M_POOL]
    pool_syms = [normalise_ticker(t) for t in pool_ish]

    # Warm-up from pre-2018 prices for the 200d SMA; fetch a generous lead-in.
    start = "2017-01-01"
    end = window_end.strftime("%Y-%m-%d")
    report: dict = {"line": line, "latest_snapshot": latest_key,
                    "pool_ishares": pool_ish, "pool_norgate": pool_syms,
                    "n_pool": len(pool_syms)}
    prices = fetch_member_prices(pool_syms, start, end, report=report)

    DATA_LOCAL_WS6.mkdir(parents=True, exist_ok=True)
    if not prices.empty:
        prices.to_parquet(member_cache_path(line))
        report["cache_path"] = str(member_cache_path(line))
        report["panel_shape"] = list(prices.shape)
        report["panel_start"] = str(prices.index.min().date())
        report["panel_end"] = str(prices.index.max().date())
    else:
        report["panel_shape"] = [0, 0]
    report["n_resolved"] = len(report.get("resolved", []))
    report["n_uncovered"] = len(report.get("uncovered", []))
    return report
