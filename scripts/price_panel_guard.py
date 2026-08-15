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
