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

Dates: pandas / dateutil only, never manual day arithmetic. Python ``datetime``
months are 1-indexed (stated where any month indexing occurs; this module does
none). The rebalance calendar is the deployed ``rebalance_calendar`` helper.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
# Git-ignored (licence guard); raw Norgate member series live here only.
DATA_LOCAL_WS6 = PROJECT_ROOT / "data_local" / "ws6"

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

# Explicit iShares -> Norgate ticker renames. Punctuation (dash -> dot for
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


# ---------------------------------------------------------------------------
# Ticker mapping (failure mode 1 — survivorship through the mapping)
# ---------------------------------------------------------------------------

def normalise_ticker(ishares_ticker: str) -> str:
    """Map an iShares constituent ticker to its Norgate symbol.

    Two deterministic steps, both explicit:
      1. Apply a KNOWN_RENAMES entry if one exists (genuine symbol change).
      2. Convert share-class punctuation from the iShares dash form to the
         Norgate dot form (``BRK-B`` -> ``BRK.B``, ``BF-B`` -> ``BF.B``).

    A plain alphabetic ticker maps to itself. This function ALWAYS returns a
    candidate symbol; whether Norgate actually carries that symbol is a separate
    coverage question resolved against the fetched price panel (a candidate with
    no Norgate data is counted against coverage in select_basket, never dropped
    silently).
    """
    t = ishares_ticker.strip().upper()
    if t in KNOWN_RENAMES:
        t = KNOWN_RENAMES[t]
    # Share-class punctuation: iShares uses a dash, Norgate uses a dot. Only the
    # separator changes; the class letter is preserved.
    if "-" in t:
        t = t.replace("-", ".")
    return t


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


def select_basket(spec: ArmSpec, eff_date: pd.Timestamp | None,
                  snapshots: dict, prices: pd.DataFrame, sig: dict
                  ) -> BasketResult:
    """Build one single-named line's basket for a rebalance whose effective
    (t-1) date is ``eff_date``, per the arm ``spec``.

    Steps (all as of ``eff_date`` — no look-ahead):
      1. Roster: ``snapshot_asof`` (membership on or before t-1), cap-rank order.
      2. Pool: first M for ``pool == "topM"``, else the full roster.
      3. Map iShares -> Norgate; a symbol Norgate does not carry is UNCOVERED
         (counted, excluded — never silently dropped).
      4. Present: covered names with a price as of t-1 (a gap / not-yet-listed /
         already-delisted name is missing_price — counted, excluded).
      5. Screen (if ``spec.screen``): keep names with trend state True. Fewer
         than MIN_PASS passing -> FALLBACK to the ETF (frequency reported).
      6. Rank (if ``spec.rank_key`` in {strength, momentum}): sort passing names
         by the key, drop NaN-key names (counted), keep the top ``select_n``.
      7. Equal weight inside the resulting basket.
    """
    if eff_date is None:
        return BasketResult(fallback=True, reason="no effective date (pre-window)")

    snap_date, roster = snapshot_asof(snapshots, eff_date)
    if not roster:
        return BasketResult(fallback=True, reason="no roster as of t-1")

    pool_ish = roster[:M_POOL] if spec.pool == "topM" else list(roster)

    # Map to Norgate; split covered vs uncovered against the price panel columns.
    price_cols = set(prices.columns)
    covered: list[str] = []
    uncovered: list[str] = []
    seen: set[str] = set()
    for ish in pool_ish:
        sym = normalise_ticker(ish)
        if sym in seen:
            continue          # a roster can list a name once; guard duplicates
        seen.add(sym)
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

    w = 1.0 / len(candidates)     # equal weight inside every basket (§2)
    weights = {s: w for s in candidates}
    return BasketResult(
        fallback=False, reason="",
        weights=weights, n_pool=len(pool_ish), n_covered=len(covered),
        n_present=len(present), n_pass=n_pass, n_selected=len(candidates),
        uncovered=uncovered, missing_price=missing_price,
        dropped_no_rank=dropped_no_rank)


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
    rebal_dates = weekly_rebalance_dates(closes.index, eligible, REBAL_FREQ)
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
    diagnostics (fallback frequency, coverage, basket sizes) for the record."""
    name_weights: pd.DataFrame
    fallback_weeks: dict[str, int]
    basket_sizes: dict[str, list[int]]
    uncovered_seen: dict[str, set]
    missing_seen: dict[str, set]
    weeks_evaluated: dict[str, int]


def _add(row: dict, name: str, w: float) -> None:
    row[name] = row.get(name, 0.0) + w


def build_arm_name_weights(spec: ArmSpec, sector_weights: pd.DataFrame,
                           closes: pd.DataFrame, rebal_dates: pd.DatetimeIndex,
                           eligible: pd.Timestamp,
                           membership: dict, member_signals: dict,
                           member_prices: dict) -> ArmBuild:
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
    (single-named lines only). They are dependency-injected so the selftests can
    drive the builder on synthetic panels and T3 on the Norgate caches.
    """
    lines = list(sector_weights.columns)
    fallback_weeks = {L: 0 for L in SINGLE_NAMED_LINES if L in lines}
    basket_sizes: dict[str, list[int]] = {L: [] for L in fallback_weeks}
    uncovered_seen: dict[str, set] = {L: set() for L in fallback_weeks}
    missing_seen: dict[str, set] = {L: set() for L in fallback_weeks}
    weeks_evaluated = {L: 0 for L in fallback_weeks}

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
            basket = select_basket(spec, eff_date, membership[L],
                                   member_prices[L], member_signals[L])
            uncovered_seen[L].update(basket.uncovered)
            missing_seen[L].update(basket.missing_price)
            if basket.fallback:
                fallback_weeks[L] += 1
                _add(row, L, w)          # revert this line to its ETF
                continue
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
                    missing_seen=missing_seen, weeks_evaluated=weeks_evaluated)


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
            cost_bps: float) -> dict:
    """Convenience: build an arm and simulate it at one cost. E0 ignores the
    member inputs. Provided for the T3 harness and the selftests; this module
    never invokes it across the register (that is T3's job)."""
    build = build_arm_name_weights(
        spec, sector["weights"], sector["closes"], sector["rebal_dates"],
        sector["eligible"], membership, member_signals, member_prices_by_line)
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


def line_member_universe(line: str, window_end: pd.Timestamp = WINDOW_END
                         ) -> tuple[list[str], dict]:
    """The union of Norgate symbols a line ever needs over the window: every
    ticker in any snapshot up to ``window_end``, mapped and de-duplicated in
    first-seen (roughly cap-rank) order. Returns (symbols, mapping_report) where
    the report records the iShares->Norgate mapping and any duplicates folded."""
    data = load_constituents(line)
    snapshots = data.get("snapshots", {})
    mapping: dict[str, str] = {}
    order: list[str] = []
    for key in sorted(snapshots.keys()):
        if pd.Timestamp(key) > window_end:
            continue
        for ish in snapshots[key].get("tickers", []):
            sym = normalise_ticker(ish)
            if sym not in mapping:
                mapping[sym] = ish
                order.append(sym)
    report = {"line": line, "n_ishares_unique": len(mapping),
              "mapping": mapping}
    return order, report


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
