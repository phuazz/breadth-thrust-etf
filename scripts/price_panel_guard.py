"""Refuse a backtest built on a price series that cannot support one.

WHY THIS EXISTS — 2026-08-15, caught by accident, after publication.

``scripts/refresh_all.py`` ran the four strategy engines at step 3 and only
refreshed the per-ETF OHLC caches at step 6. Strategy A ran at 16:17 local
against a broken SOXX series; ``export_holdings_prices.py`` repaired that
series at 16:36. The sleeve published Sharpe 0.76 / CAGR 11.2% / total return
+130% against committed values of 0.93 / 16.9% / +238%, and dragged the
deployed blend headline from 1.24 / +15.0% to 1.20 / +13.0%. Re-running
``run_topk_robustness.py`` afterwards, with no other change, restored +0.93
and +238.4%. The breadth panels were never at fault — 11 cells moved across
the 15 sleeve A panels. Every downstream artefact (multi_strategy,
portfolio_construction, phase7, phase8, docs/index.html, the factsheet PDF)
inherited the corrupted sleeve silently.

None of the four VERIFY steps saw it. The only thing that fired was
``tests/test_figure_bindings.py::test_committed_literals_match_the_data``,
and only because pinned literals moved. That is a tripwire on the
CONSEQUENCE, not a check on the INPUT: it would have stayed silent had the
damage landed on a figure nobody had pinned, and it says nothing about which
series was wrong.

THE TELL THIS ENCODES. An ETF whose close column is absent over the window
is still HELD, because the weights come from the breadth panel and the
breadth panel was healthy. Its daily returns then read as exactly zero, so
the attribution row carries a large ``days_held`` beside an
``ann_return_when_held`` of exactly 0.0. A liquid ETF does not produce a mean
daily return of exactly 0.0 over hundreds of sessions. That pairing is a data
fault every time it appears, and it must fail the run rather than be written.

WHAT THIS DOES NOT DO. It does not police how OLD the panel is. Absolute
staleness already belongs to ``check_freshness_headroom.py`` and
``check_capture_integrity.py``, and duplicating it here would give two guards
one job and let each assume the other was watching. What this owns is
degeneracy: a column that is missing, flat, holed, truncated or trailing its
own panel. It also never fails on thin data — a member with too few
observations to judge returns SKIP, because a guard that cries wolf on a
short history is a guard that gets switched off.

THRESHOLDS ARE MEASURED, NOT CHOSEN. Every floor below was set from the
committed panels of all four sleeves on 2026-08-15, and sits in the empty
space between healthy and broken rather than just outside the healthy range.
The measurements are recorded beside each constant so a later reader can tell
whether a breach is a real regression or a threshold that was always tight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# Below this many valid observations in the window a member is too thin to
# judge, so it returns SKIP. It is NOT a pass: a column with ZERO valid
# observations is the 2026-08-15 defect itself and always FAILs.
MIN_OBS_TO_JUDGE = 30

# A price series that never changes is not a price series. Two distinct
# values is the weakest possible statement of that and needs no calibration.
MIN_DISTINCT_VALUES = 2

# Valid fraction of the panel's sessions AFTER the member's own first bar.
# Measured after the first bar so a legitimately late listing is not
# penalised for its inception date — that is what MAX_LATE_START_SESSIONS is
# for, and conflating the two is how a late-inception member gets read as a
# broken one.
#
# MEASURED 2026-08-15 across all four sleeves: 0.9976 worst (159801.SZ, whose
# Shenzhen calendar loses a handful of NYSE sessions a year to Chinese New
# Year and Golden Week), 1.0000 for the other 57 members. A floor of 0.95
# leaves roughly twenty times the observed slack.
MIN_COVERAGE_AFTER_FIRST = 0.95

# Longest run of consecutive missing sessions after the member's first bar.
# Coverage alone does not catch this: a fifteen-session hole is 0.8% of
# sleeve C's window and would clear the floor above while destroying every
# return that spans it.
#
# MEASURED 2026-08-15: 4 sessions worst (159801.SZ), 0 for every other member.
MAX_INTERIOR_GAP_SESSIONS = 15

# How far a member's last bar may trail the newest bar in its own panel.
# Deliberately RELATIVE. An absolute staleness bound would fire on any
# legitimate research re-run against last Friday's caches, and staleness is
# already owned elsewhere; what this catches is the ragged tail — one line
# stopping while its peers carry on, which is the shape a vendor hole takes.
#
# MEASURED 2026-08-15: 1 session worst (BTC-USD, a crypto line reindexed onto
# the equity calendar), 0 for every other member.
MAX_MEMBER_LAG_SESSIONS = 3

# How many sessions into the window a member's first bar may fall before it
# is treated as absent rather than late. Members whose inception genuinely
# postdates the window are declared by the caller via ``allow_late``.
#
# This is the rule that catches a TRUNCATED fetch — the two-year yfinance
# fallback shape, 500 rows starting two years back, written over a full
# history. Such a column is dense, unflat and flush with the tail; only its
# start betrays it.
#
# MEASURED 2026-08-15: 0 sessions for every undeclared member of all four
# sleeves. Sleeve C's one late line (159801.SZ) is already declared
# late_inception in the engine.
MAX_LATE_START_SESSIONS = 5

# Sessions held before an exactly-zero return becomes evidence rather than
# coincidence. Five is far beyond what a real adjusted close can produce by
# chance and small enough that a brief but material holding is still caught.
MIN_DAYS_HELD_FOR_ZERO_TELL = 5


class DegeneratePriceError(RuntimeError):
    """A price series an engine must not backtest on."""


class AttributionTellError(RuntimeError):
    """An attribution row that reports holding an ETF that never moved."""


@dataclass(frozen=True)
class SeriesVerdict:
    """One member's fitness to be backtested over the window."""

    member: str
    n_obs: int
    n_distinct: int
    coverage: float
    max_gap: int
    late_start: int
    sessions_behind: int
    first_bar: pd.Timestamp | None
    last_bar: pd.Timestamp | None
    status: str
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def note(self) -> str:
        return "; ".join(self.reasons)


# --------------------------------------------------------------------------
# Pure logic — unit-tested offline in tests/test_price_panel_guard.py
# --------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Hole-tolerant trend signal (2026-08-22)
#
# THE DEFECT THIS REPLACES. Sleeves B and C computed their signal as
#
#     ma = closes.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
#
# and pandas counts NON-NaN observations against min_periods. With the two
# equal, ONE missing bar makes every window containing it short by one, so a
# single absent close blanks the moving average for the next MA_PERIOD
# sessions -- 200 of them, about ten months. Both engines then "drop NaN
# signal (insufficient history)", so the ticker is silently removed from
# candidacy for that whole period.
#
# Measured, not theorised: injecting one hole into BTC-USD a year back blanked
# exactly 200 subsequent signal values. BTC-USD is held in 95 of 212 sleeve-C
# rebalances at a 20% within-sleeve weight -- 2% of NAV -- so a one-bar vendor
# gap could have removed it from selection until roughly June 2027 with
# nothing failing and nothing logged. The vendor produced exactly such a gap
# on 2026-08-21.
#
# THE FIX IS ALREADY THE HOUSE CONVENTION, IN THE OTHER TWO SLEEVES.
# run_ma200_sweep.compute_ma200_breadth -- the constituent breadth behind
# sleeves A and D -- has always used min_periods = int(period * 0.9). B and C
# simply never got it. This is that convention, written once and shared,
# rather than a fourth copy of the same three lines.
#
# WHY THE WARM-UP GATE IS SEPARATE. One min_periods was doing two unrelated
# jobs: "is there enough history to compute a 200-day average at all" and "is
# this particular window complete". Loosening it alone would answer the first
# question wrongly and start every series ~20 sessions early, restating the
# record. So the warm-up is gated explicitly on cumulative observations and
# the tolerance applies only to holes INSIDE an already-warm window.
#
# Verified value-preserving on the committed caches with one price frame
# pinned across both runs: 112,360 cells where both definitions are defined,
# maximum absolute difference 0.000e+00, zero cells lost, seven gained (all
# 159801.SZ, 10-18 Nov 2020, none of them a rebalance decision or fill date).
#
# WHAT IT DELIBERATELY DOES NOT DO. If the CURRENT bar is missing, the signal
# is still NaN and the ticker is still excluded, because ranking a position on
# a close that does not exist is the partial-bar defect in another costume.
# The tolerance is for holes in the window's history, never for the point
# being measured.
# ---------------------------------------------------------------------------
MA_WINDOW_TOLERANCE = 0.9


def tolerant_moving_average(closes, period: int,
                            tolerance: float = MA_WINDOW_TOLERANCE):
    """Rolling mean that survives isolated missing bars.

    ``closes`` may be a Series or a DataFrame. Returns NaN until ``period``
    real observations exist (warm-up), then averages over whatever is present
    in the window provided at least ``tolerance`` of it is.
    """
    min_p = max(1, int(period * tolerance))
    ma = closes.rolling(period, min_periods=min_p).mean()
    # Warm-up on CUMULATIVE observations, so a loosened window cannot pull the
    # series start earlier than the old bound and restate history.
    warm = closes.notna().cumsum() >= period
    return ma.where(warm)


def ma_distance_signal(closes, period: int,
                       tolerance: float = MA_WINDOW_TOLERANCE):
    """Fractional distance of price from its own moving average.

    NaN wherever the current close is absent -- see the note above on why the
    tolerance never extends to the point being measured.
    """
    ma = tolerant_moving_average(closes, period, tolerance)
    return (closes - ma) / ma


def _numeric(series: pd.Series) -> pd.Series:
    """Coerce to float and treat infinities as missing.

    A parquet round-trip can carry an object column or an infinity through
    without complaint, and both read as "present" to ``notna``.
    """
    out = pd.to_numeric(series, errors="coerce").astype(float)
    return out.replace([np.inf, -np.inf], np.nan)


def longest_nan_run(series: pd.Series) -> int:
    """Longest run of consecutive missing values in ``series``."""
    isna = series.isna()
    if not isna.any():
        return 0
    # Group each NaN by the count of non-NaN values preceding it, so one
    # group is one uninterrupted run.
    return int(isna.groupby((~isna).cumsum()).sum().max())


def fetched_frame_is_worse(fetched: pd.DataFrame | None,
                           on_disk: pd.DataFrame | None) -> str | None:
    """Reason to REFUSE writing ``fetched`` over ``on_disk``, or None.

    The one rule for every ``{ticker}_ohlc_cache.parquet`` write site —
    ``backtest.download_soxx_ohlc``, ``export_holdings_prices.
    fetch_missing_from_yfinance`` and ``refresh_ohlc_caches``: a vendor never
    un-prints a close, so a response that is degenerate, ends earlier or
    STARTS LATER than the file it would replace is a sourcing fault and must
    not become the series the engines fall back on.

    The start half is the 2026-08-19 lesson. The daily two-year backfill
    (``period="2y"``) passes the end rule — it is perfectly fresh — and it
    overwrote the five sleeve-D Xetra caches' 2017 history on 2026-08-13/14;
    the next cold rebuild read the surviving stubs back as authoritative and
    collapsed a blend onto a two-year window (Sharpe +1.99 against a
    committed +1.29). Only the write sites can stop that: to every later
    reader the truncated file simply IS the past.
    """
    if fetched is None or len(fetched) == 0 or "Close" not in fetched.columns:
        return "the fetch returned nothing usable"
    close = pd.to_numeric(fetched["Close"], errors="coerce").dropna()
    if len(close) < 2:
        return f"the fetch returned {len(close)} usable close(s)"
    if close.nunique() < 2:
        return "the fetch returned a flat close series"
    if on_disk is None or "Close" not in on_disk.columns:
        return None
    prev = pd.to_numeric(on_disk["Close"], errors="coerce").dropna()
    if prev.empty:
        return None
    if close.index[-1] < prev.index[-1]:
        return (f"the fetch ends {close.index[-1].date()} but the cache "
                f"already ends {prev.index[-1].date()}")
    if close.index[0] > prev.index[0]:
        return (f"the fetch starts {close.index[0].date()} but the cache "
                f"already starts {prev.index[0].date()}")
    return None


def fetched_panel_is_worse(fetched: pd.DataFrame | None,
                           on_disk: pd.DataFrame | None) -> str | None:
    """Reason to REFUSE writing a WIDE close panel over ``on_disk``, or None.

    The sibling of ``fetched_frame_is_worse`` for the two engine-level price
    caches — ``asset_class_prices_cache.parquet`` and
    ``thematic_prices_cache.parquet`` — whose frames are one column per ticker
    rather than an OHLC block, so the Close-column rule cannot judge them.

    The rule is identical, and so is the reason for it: a vendor never
    un-prints a close, so a response that is empty, ends earlier, starts later
    or has LOST a ticker is a sourcing fault, not new information.

    Added 2026-08-26, after a dropped connection made yfinance return nothing
    mid-refresh and both cache files were overwritten with a ZERO-ROW frame.
    Nothing noticed at write time. The next run read the stubs back, took
    ``index.max()`` off an empty index and died on
    ``Cannot compare NaT with datetime.date`` — Strategies B and C both down,
    and the good caches gone, because the write site trusted the fetch. The
    SOXX path has refused degenerate writes since 2026-08-15; these two were
    simply never covered.
    """
    if fetched is None or len(fetched) == 0 or fetched.shape[1] == 0:
        return "the fetch returned nothing usable"
    usable = fetched.dropna(how="all")
    if len(usable) < 2:
        return f"the fetch returned {len(usable)} usable row(s)"
    if on_disk is None or len(on_disk) == 0:
        return None
    prev = on_disk.dropna(how="all")
    if prev.empty:
        return None
    new_idx, old_idx = pd.DatetimeIndex(usable.index), pd.DatetimeIndex(prev.index)
    if new_idx.max() < old_idx.max():
        return (f"the fetch ends {new_idx.max().date()} but the cache "
                f"already ends {old_idx.max().date()}")
    if new_idx.min() > old_idx.min():
        return (f"the fetch starts {new_idx.min().date()} but the cache "
                f"already starts {old_idx.min().date()}")
    lost = [c for c in on_disk.columns if c not in fetched.columns]
    if lost:
        return (f"the fetch dropped {len(lost)} ticker(s) the cache had: "
                f"{', '.join(map(str, sorted(lost)[:6]))}")
    return None


def assess_close_series(
    close: pd.Series | None,
    member: str,
    panel_index: pd.DatetimeIndex | None = None,
    window_start: pd.Timestamp | None = None,
    allow_late: bool = False,
    min_obs: int = MIN_OBS_TO_JUDGE,
    min_distinct: int = MIN_DISTINCT_VALUES,
    min_coverage: float = MIN_COVERAGE_AFTER_FIRST,
    max_gap: int = MAX_INTERIOR_GAP_SESSIONS,
    max_lag: int = MAX_MEMBER_LAG_SESSIONS,
    max_late_start: int = MAX_LATE_START_SESSIONS,
) -> SeriesVerdict:
    """Judge one member's close series over the window the engine uses.

    ``panel_index`` is the sessions the engine actually iterates. Passing it
    is what makes coverage and lag mean anything: a member is measured
    against the calendar its peers trade on, not against its own surviving
    rows, and a column that lost half its history therefore reads as holed
    rather than as short.

    ``allow_late`` exempts a member whose inception genuinely postdates the
    window (sleeve C declares these as ``late_inception``). It exempts the
    START only — a declared late member is still held to coverage, gaps and
    the tail.
    """
    empty = SeriesVerdict(member, 0, 0, 0.0, 0, 0, 0, None, None, FAIL,
                          ("no price series at all",))
    if close is None or len(close) == 0:
        return empty

    window = _numeric(close)
    if panel_index is not None:
        window = window.reindex(panel_index)
    if window_start is not None:
        window = window.loc[window.index >= pd.Timestamp(window_start)]
    if len(window) == 0:
        return empty

    valid = window.dropna()
    n_obs = int(len(valid))
    if n_obs == 0:
        return SeriesVerdict(
            member, 0, 0, 0.0, len(window), len(window), len(window),
            None, None, FAIL,
            ("no valid close in the backtest window — every session is NaN, "
             "so every day this member is held scores a zero return",),
        )

    first_bar = pd.Timestamp(valid.index[0])
    last_bar = pd.Timestamp(valid.index[-1])
    n_distinct = int(valid.nunique())
    after_first = window.loc[window.index >= first_bar]
    coverage = float(after_first.notna().mean()) if len(after_first) else 0.0
    gap = longest_nan_run(after_first)
    late_start = int((window.index < first_bar).sum())
    behind = int((window.index > last_bar).sum())

    if n_obs < min_obs:
        return SeriesVerdict(
            member, n_obs, n_distinct, coverage, gap, late_start, behind,
            first_bar, last_bar, SKIP,
            (f"only {n_obs} valid observation(s), below the {min_obs} needed "
             f"to judge",),
        )

    reasons: list[str] = []
    if n_distinct < min_distinct:
        reasons.append(
            f"flat: {n_distinct} distinct value(s) across {n_obs} sessions"
        )
    if coverage < min_coverage:
        reasons.append(
            f"coverage {coverage:.4f} after its first bar, below {min_coverage:.2f}"
        )
    if gap > max_gap:
        reasons.append(
            f"interior gap of {gap} consecutive sessions, above {max_gap}"
        )
    if behind > max_lag:
        reasons.append(
            f"last bar {last_bar.date()} trails the panel by {behind} "
            f"session(s), above {max_lag}"
        )
    if late_start > max_late_start and not allow_late:
        reasons.append(
            f"first bar {first_bar.date()} is {late_start} session(s) into "
            f"the window, above {max_late_start} — the series looks truncated, "
            f"not late-listed (declare it late_inception if it really is)"
        )

    status = FAIL if reasons else PASS
    return SeriesVerdict(member, n_obs, n_distinct, coverage, gap, late_start,
                         behind, first_bar, last_bar, status, tuple(reasons))


def assess_panel(
    closes: pd.DataFrame,
    window_start: pd.Timestamp | None = None,
    allow_late: set[str] | None = None,
    **kwargs,
) -> list[SeriesVerdict]:
    """``assess_close_series`` for every column, against the shared index."""
    late = allow_late or set()
    index = pd.DatetimeIndex(closes.index)
    return [
        assess_close_series(closes[c], str(c), panel_index=index,
                            window_start=window_start,
                            allow_late=str(c) in late, **kwargs)
        for c in closes.columns
    ]


def format_verdicts(verdicts: list[SeriesVerdict]) -> str:
    """Fixed-width report, worst first."""
    lines = [
        f"{'MEMBER':<12} {'OBS':>6} {'DISTINCT':>9} {'COVER':>7} {'GAP':>5} "
        f"{'LATE':>5} {'BEHIND':>7}  STATUS",
        "-" * 72,
    ]
    order = {FAIL: 0, SKIP: 1, PASS: 2}
    for v in sorted(verdicts, key=lambda x: (order.get(x.status, 3), x.member)):
        lines.append(
            f"{v.member:<12} {v.n_obs:>6} {v.n_distinct:>9} {v.coverage:>7.4f} "
            f"{v.max_gap:>5} {v.late_start:>5} {v.sessions_behind:>7}  {v.status}"
        )
        if v.note and v.status != PASS:
            lines.append(f"             {v.note}")
    return "\n".join(lines)


def assert_panel_usable(
    closes: pd.DataFrame,
    label: str,
    window_start: pd.Timestamp | None = None,
    allow_late: set[str] | None = None,
    **kwargs,
) -> list[SeriesVerdict]:
    """Raise ``DegeneratePriceError`` unless every member can be backtested.

    Called by each engine once its eligible start is known, because "the
    backtest window" is the only window in which the question means anything:
    a member may be missing for years before the window opens and still be
    perfectly sound inside it.
    """
    verdicts = assess_panel(closes, window_start=window_start,
                            allow_late=allow_late, **kwargs)
    failures = [v for v in verdicts if v.status == FAIL]
    skips = [v for v in verdicts if v.status == SKIP]
    window_note = (f" from {pd.Timestamp(window_start).date()}"
                   if window_start is not None else "")
    print(f"  [panel guard] {label}: {len(verdicts) - len(failures) - len(skips)}"
          f" pass, {len(failures)} FAIL, {len(skips)} skip"
          f" over {len(closes)} sessions{window_note}", flush=True)
    for v in skips:
        print(f"  [panel guard] {label}: {v.member} SKIP — {v.note}", flush=True)
    if failures:
        raise DegeneratePriceError(
            f"{label}: {len(failures)} member(s) cannot be backtested over "
            f"the window{window_note}.\n\n{format_verdicts(verdicts)}\n\n"
            "A backtest run on these prices would be plausible and wrong — "
            "the breadth panel still allocates to the affected member, and "
            "its returns read as zero. Repair the price cache (python "
            "scripts/export_holdings_prices.py --refresh-caches-only) and "
            "re-run the engine. Do NOT commit the artefacts from this run."
        )
    return verdicts


# --------------------------------------------------------------------------
# The attribution tell
# --------------------------------------------------------------------------
def zero_return_rows(
    attribution: dict,
    min_days: int = MIN_DAYS_HELD_FOR_ZERO_TELL,
) -> list[tuple[str, str]]:
    """Attribution rows that report holding an ETF which never moved.

    Returns [(member, reason)]. The comparison against zero is EXACT and
    deliberately so. ``ann_return_when_held`` is
    ``(1 + mean_daily_return) ** 252 - 1``; landing on exactly 0.0 requires
    every held-day return to be exactly 0.0, which happens when the close
    column is missing and ``fillna(0)`` turns absence into flatness. A
    tolerance-based comparison would instead fire on a genuinely quiet
    holding and would be tuned away within a month.
    """
    hits: list[tuple[str, str]] = []
    for member, row in sorted((attribution or {}).items()):
        if not isinstance(row, dict):
            continue
        try:
            days = int(row.get("days_held") or 0)
        except (TypeError, ValueError):
            continue
        if days < min_days:
            continue
        ann = row.get("ann_return_when_held")
        pnl = row.get("contribution_to_total_return")
        if isinstance(ann, (int, float)) and float(ann) == 0.0:
            hits.append((
                str(member),
                f"held {days} session(s) with ann_return_when_held exactly "
                f"0.0 — its close column is missing over the window, not flat",
            ))
        elif isinstance(pnl, (int, float)) and float(pnl) == 0.0:
            hits.append((
                str(member),
                f"held {days} session(s) contributing exactly 0.0 to total "
                f"return — its close column is missing over the window",
            ))
    return hits


def assert_attribution_sane(
    attribution: dict,
    label: str,
    min_days: int = MIN_DAYS_HELD_FOR_ZERO_TELL,
) -> None:
    """Raise ``AttributionTellError`` before an impossible sleeve is written.

    The last gate before ``OUT_PATH.write_text``. The panel guard should have
    caught the cause; this catches the symptom, and it catches it for any
    route into a dead return column that the panel guard has not been taught
    about yet.
    """
    hits = zero_return_rows(attribution, min_days=min_days)
    if not hits:
        return
    detail = "\n".join(f"  - {m}: {why}" for m, why in hits)
    raise AttributionTellError(
        f"{label}: {len(hits)} attribution row(s) report a held ETF that "
        f"never moved.\n{detail}\n\n"
        "This sleeve was NOT written. A large days_held beside an exactly "
        "zero return is a price-cache fault, never a market outcome — see the "
        "2026-08-15 SOXX incident in scripts/price_panel_guard.py. Repair the "
        "cache and re-run before committing anything downstream."
    )


# --------------------------------------------------------------------------
# The decision session (2026-09-03)
# --------------------------------------------------------------------------
# WHY THIS EXISTS. Every check above measures a member against the PANEL'S
# OWN INDEX, so a session the whole panel lacks is invisible to all of them.
# On Friday 2026-08-28 yfinance served no bar for ten of thirteen sleeve-B
# lines and for SHY. Sleeve B drops any row with a gap and sleeve C takes its
# calendar from SHY, so both panels lost the session outright; the engines
# then stamped the 2026-08-31 rebalance on THURSDAY's close (decision_date
# 2026-08-27), the refresh guard, this module and the cache-write refusal all
# passed, and the result was published. A rebalance decided on the wrong
# session is plausible and wrong — the same shape as the 2026-08-14 sleeve-D
# incident, which a vendor hole moved from Thursday to Wednesday.
#
# The VENUE CALENDAR is the reference this check has and the others do not.
# Two severities, deliberately:
#   FAIL  the session the most recent rebalance ranks on is absent from the
#         panel, or (for a price-signal engine) present but unpriced for a
#         member — the published decision is mis-decided either way.
#   WARN  any other scheduled session missing inside the trailing window. It
#         moves a 200-session mean by one day and drops a day from the equity
#         curve, and it should be repaired, but it decides nothing.
# A breadth engine (A, D) ranks on its breadth panel, so an unpriced member
# on the decision session mis-MARKS one day rather than mis-deciding; those
# callers pass hollow_is_fail=False and get the WARN.
#
# WHAT IT DOES NOT DO. It does not police the tail: a panel that stops short
# of the last completed session is live_targets' and the refresh guard's
# question (the "reaches" HOLD and G1). Here the rebalance grid is read off
# the panel that exists, and the question is whether THAT rebalance was
# decided on the session the venue actually closed before it.

DECISION_LOOKBACK_SESSIONS = 30


def venue_sessions_through(calendar: str, end, lookback: int = DECISION_LOOKBACK_SESSIONS
                           ) -> list[pd.Timestamp]:
    """The last ``lookback`` sessions of ``calendar`` at or before ``end``."""
    from rebalance_calendar import _exchange_sessions  # local: no import cycle

    end = pd.Timestamp(end).normalize()
    start = end - pd.Timedelta(days=lookback * 3 + 14)
    sessions = sorted(_exchange_sessions(
        calendar, start.date().isoformat(), end.date().isoformat()))
    return [pd.Timestamp(d) for d in sessions][-lookback:]


def expected_decision_session(rebalance_date, calendar: str) -> pd.Timestamp | None:
    """The venue's last session STRICTLY before the rebalance date — the one
    an engine ranking at rd-1 reads when its panel is complete."""
    rd = pd.Timestamp(rebalance_date).normalize()
    before = venue_sessions_through(calendar, rd - pd.Timedelta(days=1), lookback=10)
    return before[-1] if before else None


def missing_scheduled_sessions(index, calendar: str,
                               lookback: int = DECISION_LOOKBACK_SESSIONS
                               ) -> list[pd.Timestamp]:
    """Scheduled venue sessions inside the trailing window that the index lacks."""
    idx = pd.DatetimeIndex(index).normalize()
    if len(idx) == 0:
        return []
    have = set(idx)
    return [s for s in venue_sessions_through(calendar, idx.max(), lookback)
            if s not in have]


def decision_session_report(closes: pd.DataFrame, calendar: str, freq: str,
                            eligible_start, hollow_is_fail: bool = True,
                            lookback: int = DECISION_LOOKBACK_SESSIONS) -> dict:
    """Pure verdict on the most recent rebalance's decision session.

    Returns a dict with ``status`` (PASS / FAIL / SKIP), the rebalance date
    read off the panel, the venue session it should have ranked on, whether
    that session is present, the members unpriced on it, the other scheduled
    sessions missing from the trailing window, and the reasons and warnings
    in prose. SKIP means there is no rebalance to judge.
    """
    from rebalance_calendar import engine_rebalance_dates  # local: no cycle

    idx = pd.DatetimeIndex(closes.index).normalize()
    report = {
        "status": PASS, "calendar": calendar, "rebalance_date": None,
        "expected_decision": None, "present": None, "hollow_members": [],
        "missing_sessions": [], "reasons": [], "warnings": [],
    }
    if len(idx) == 0:
        report["status"] = SKIP
        report["reasons"].append("empty panel — nothing to judge")
        return report

    missing = missing_scheduled_sessions(idx, calendar, lookback)
    report["missing_sessions"] = [m.strftime("%Y-%m-%d") for m in missing]

    rds = engine_rebalance_dates(idx, pd.Timestamp(eligible_start), freq, calendar)
    if len(rds) == 0:
        report["status"] = SKIP
        report["reasons"].append("no rebalance date at or after eligible_start")
        return report
    rd = pd.Timestamp(rds[-1]).normalize()
    report["rebalance_date"] = rd.strftime("%Y-%m-%d")
    expected = expected_decision_session(rd, calendar)
    if expected is None:
        report["status"] = FAIL
        report["reasons"].append(
            f"the {calendar} calendar returned no session before the "
            f"{rd.date()} rebalance — calendar data is broken")
        return report
    report["expected_decision"] = expected.strftime("%Y-%m-%d")
    present = expected in set(idx)
    report["present"] = bool(present)
    if not present:
        earlier = idx[idx < expected]
        used = earlier.max().strftime("%Y-%m-%d") if len(earlier) else "nothing"
        report["status"] = FAIL
        report["reasons"].append(
            f"the {calendar} session {expected.date()} that the {rd.date()} "
            f"rebalance ranks on is ABSENT from the panel, so the engine "
            f"decides on {used} instead — a vendor gap on the decision "
            f"session, not a holiday")
    else:
        row = closes.loc[closes.index.normalize() == expected].iloc[0]
        hollow = [str(m) for m in row.index[row.isna()]]
        report["hollow_members"] = hollow
        if hollow:
            text = (f"the decision session {expected.date()} is present but "
                    f"unpriced for {len(hollow)} of {len(row)} members: "
                    f"{hollow}")
            if hollow_is_fail:
                report["status"] = FAIL
                report["reasons"].append(text + " — a partial row is a "
                                         "different signal, not a smaller one")
            else:
                report["warnings"].append(
                    text + " — this engine ranks on breadth, so the day is "
                    "mis-marked rather than mis-decided; repair the cache")
    others = [m for m in report["missing_sessions"]
              if m != report["expected_decision"]]
    if others:
        report["warnings"].append(
            f"{len(others)} scheduled {calendar} session(s) missing inside "
            f"the last {lookback}: {others} — each drops a day from the "
            f"equity curve and moves the 200-session mean; repair with "
            f"scripts/repair_price_gaps.py")
    return report


def assert_decision_session_present(closes: pd.DataFrame, calendar: str,
                                    freq: str, eligible_start, label: str,
                                    hollow_is_fail: bool = True,
                                    lookback: int = DECISION_LOOKBACK_SESSIONS
                                    ) -> dict:
    """Raise ``DegeneratePriceError`` unless the latest rebalance was decided
    on the session the venue actually closed before it. Prints its finding
    either way, so a passing run still names the session it checked."""
    rep = decision_session_report(closes, calendar, freq, eligible_start,
                                  hollow_is_fail=hollow_is_fail,
                                  lookback=lookback)
    if rep["status"] == SKIP:
        print(f"  [decision session] {label}: SKIP — {'; '.join(rep['reasons'])}",
              flush=True)
        return rep
    if rep["status"] == PASS:
        print(f"  [decision session] {label}: the {rep['rebalance_date']} "
              f"rebalance ranks on {calendar} {rep['expected_decision']} — "
              f"present and priced for every member", flush=True)
    for w in rep["warnings"]:
        print(f"  [decision session] {label}: WARN {w}", flush=True)
    if rep["status"] == FAIL:
        detail = "\n".join(f"  - {r}" for r in rep["reasons"])
        raise DegeneratePriceError(
            f"{label}: the latest rebalance was not decided on its venue "
            f"session.\n{detail}\n\n"
            "A backtest run on this panel would stamp the rebalance on the "
            "wrong close and publish it as plausible. This is the 2026-08-28 "
            "class: yfinance withheld a Friday, and the 2026-08-31 rebalance "
            "went out decided on Thursday. Repair the panel (python "
            "scripts/repair_price_gaps.py, then --apply) or source it from "
            "Norgate (BTE_PRICE_SOURCE=norgate), and re-run the engine. Do "
            "NOT commit the artefacts from this run."
        )
    return rep
