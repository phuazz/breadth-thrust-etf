"""WS6b T2 — shadow publisher core and guard layer.

Registration: ``C:\\dev\\KICKOFF_ws6b-unscreened-replication.md`` SS2, bar (b).
A weekly shadow publisher computes I0-PARTIAL5 weights and returns alongside the
live E0 book, zero-touch, and ships WITH its guard layer per the house
unattended-agent rule: no scheduled run may be trusted without something that
can catch a silently-wrong step. Shadow weeks count only after this module and
its tests are committed.

This module is pure and dependency-injected: it never fetches, never writes to
any deployed artefact, and takes its inputs as arguments so the tests can drive
every guard branch on synthetic data. The CLI wrapper
(``run_ws6b_shadow.py``) supplies the live data.

--------------------------------------------------------------------------
TWO PROBLEMS WITH THE SIGNED BARS, SURFACED NOT SILENTLY RESOLVED
--------------------------------------------------------------------------
1. **The divergence bar is slack for the adopted set.** The kickoff's bar is
   "3x the backtest weekly tracking error (approx 66 bp)". The 66 bp
   parenthetical was computed from WS6's FULL-11 I0, whose annualised TE is
   1.60%. The SIGNED adoption set is PARTIAL-5, whose own weekly TE is 14.3 bp
   (1.03% annualised), so the same rule applied to the actual set gives 43 bp.
   Over the backtest window, 0 of 404 weeks breach 66 bp and 5 breach 43 bp — a
   gate nothing ever trips is not a test. Both thresholds are evaluated and
   logged every week; ``BINDING_DIVERGENCE_BAR`` records which one governs, and
   it is a ZH ruling because the document is binding, not a modelling choice.

2. **The turnover bar breaks under a per-week reading.** "Realised shadow
   turnover <= 1.5x the backtest average" exceeds its bar in 15.2% of backtest
   weeks if applied week by week, so a per-week reading would fail the shadow
   routinely on entirely normal behaviour. It is therefore evaluated on the
   RUNNING AVERAGE across shadow weeks, which is the only reading under which
   the bar discriminates. Stated here rather than buried.

All constants below are frozen from the T1 mechanics and must not be re-derived
from shadow data — a bar that moves with the evidence it judges is not a bar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd

# --- Frozen bars (T1 mechanics, window 2018-10-12..2026-06-30) -------------
BACKTEST_WEEKLY_TE_PARTIAL5 = 0.001430      # 14.3 bp
BACKTEST_WEEKLY_TE_FULL11 = 0.002219        # 22.2 bp, the source of "approx 66 bp"
DIVERGENCE_BAR_REGISTERED = 0.0066          # the kickoff's parenthetical
DIVERGENCE_BAR_ADOPTED_SET = 3 * BACKTEST_WEEKLY_TE_PARTIAL5   # 42.9 bp
# Which bar governs the verdict. "registered" until ZH rules otherwise; the
# stricter figure is logged either way so no shadow week has to be re-run.
BINDING_DIVERGENCE_BAR = "registered"

BACKTEST_MEAN_WEEKLY_TURNOVER = 0.3391
TURNOVER_BAR_MULTIPLE = 1.5
TURNOVER_BAR = TURNOVER_BAR_MULTIPLE * BACKTEST_MEAN_WEEKLY_TURNOVER   # 0.5086

REQUIRED_CONSECUTIVE_WEEKS = 8
ADOPTED_SET = ("IUES", "IUUS", "IUCS", "SOXX", "IUFS")

# A weekly book-level return outside this is a data error, not a market move —
# same reasoning as check_capture_integrity.RETURN_BOUND, widened for a weekly
# rather than daily bar.
WEEKLY_RETURN_BOUND = 0.25


@dataclass
class ShadowWeek:
    """One published shadow week. Serialised verbatim into the log."""

    week_ending: str                 # ISO date of the W-FRI rebalance
    i0_return: float
    e0_return: float
    gap: float                       # i0 - e0
    turnover_i0: float               # one-way, this week
    lines_held: list[str]
    lines_basketed: list[str]
    fallback_lines: list[str]        # fired fallback = RESOLVED, not a gap
    unresolved_gaps: list[str]       # anything the resolver could not settle
    corporate_actions: list[str]     # logged explanations for a wide gap
    snapshot_dates: dict             # per line, the membership snapshot used
    data_asof: str                   # last session the price data reaches
    engine_commit: str
    params_sha: str
    prev_hash: str = ""
    record_hash: str = ""

    def payload(self) -> dict:
        d = asdict(self)
        d.pop("record_hash")
        return d

    def compute_hash(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class GuardResult:
    """Verdict for one week. ``publishable`` gates whether the week COUNTS."""

    publishable: bool
    checks: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, fatal: bool = True) -> None:
        self.checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            (self.failures if fatal else self.warnings).append(f"{name}: {detail}")
            if fatal:
                self.publishable = False


def check_capture_integrity(data_asof: date, expected_session: date,
                            guard: GuardResult) -> None:
    """Did this run capture data reaching the session it believes it did?

    The silent-failure class the house rule exists for: the job succeeds while
    the fetched series quietly stopped at an older session, so the shadow logs a
    stale week as if it were current. Mirrors
    ``check_capture_integrity``'s anchoring to the true NYSE calendar.
    """
    behind = (expected_session - data_asof).days
    guard.add("capture_integrity", data_asof >= expected_session,
              f"data reaches {data_asof}, expected {expected_session} "
              f"({behind} day(s) behind)")


def check_weight_integrity(line_weights: dict[str, float],
                           basket_weights: dict[str, dict[str, float]],
                           e0_total_weight: float, guard: GuardResult,
                           tol: float = 1e-9) -> None:
    """Every basket must sum to 1, and the book must preserve E0's total weight.

    This is the shadow's version of the T1 weight-preservation guard. Without
    it a basket that silently dropped a name would still publish, understating
    both the line's exposure and its divergence.
    """
    bad = {L: s for L, s in
           ((L, sum(w.values())) for L, w in basket_weights.items())
           if abs(s - 1.0) > 1e-6}
    guard.add("basket_weights_sum_to_one", not bad,
              f"off-sum baskets: {bad}" if bad else "all baskets sum to 1.0")

    total = sum(line_weights.values())
    guard.add("book_preserves_e0_weight", abs(total - e0_total_weight) <= tol,
              f"book weight {total:.12f} vs E0 {e0_total_weight:.12f}")


def check_data_gaps(unresolved: list[str], fallbacks: list[str],
                    guard: GuardResult) -> None:
    """Bar (b): zero UNRESOLVED data gaps. A fired fallback counts as RESOLVED.

    The fallback is the registered safety valve, so its firing is a logged,
    intended outcome — not a gap. It is surfaced as a warning so a line quietly
    living on the valve cannot hide inside a run of green weeks.
    """
    guard.add("no_unresolved_gaps", not unresolved,
              f"unresolved: {unresolved}" if unresolved else "none")
    if fallbacks:
        guard.add("fallback_fired", False,
                  f"lines on the ETF fallback this week: {fallbacks} "
                  "(resolved, not a gap)", fatal=False)


def check_return_sanity(i0_return: float, e0_return: float,
                        guard: GuardResult) -> None:
    """An implausible weekly book return is a data error, not a market move."""
    bad = [n for n, r in (("i0", i0_return), ("e0", e0_return))
           if abs(r) > WEEKLY_RETURN_BOUND]
    guard.add("weekly_return_within_bound", not bad,
              f"implausible weekly return: {bad}" if bad
              else f"i0 {i0_return:+.4f}, e0 {e0_return:+.4f}")


def check_divergence(gap: float, corporate_actions: list[str],
                     guard: GuardResult) -> None:
    """Bar (b): weekly |I0 - E0| within the divergence bar, OR carrying a
    logged corporate-action explanation.

    Both thresholds are recorded every week regardless of which governs, so a
    later ruling on BINDING_DIVERGENCE_BAR never requires re-running the shadow.
    A breach with a logged explanation is a WARNING, exactly as registered.
    """
    bar = (DIVERGENCE_BAR_REGISTERED if BINDING_DIVERGENCE_BAR == "registered"
           else DIVERGENCE_BAR_ADOPTED_SET)
    within = abs(gap) <= bar
    excused = bool(corporate_actions)
    guard.checks["divergence_detail"] = {
        "gap_bp": round(gap * 1e4, 2),
        "bar_registered_bp": round(DIVERGENCE_BAR_REGISTERED * 1e4, 2),
        "bar_adopted_set_bp": round(DIVERGENCE_BAR_ADOPTED_SET * 1e4, 2),
        "binding": BINDING_DIVERGENCE_BAR,
        "within_registered": abs(gap) <= DIVERGENCE_BAR_REGISTERED,
        "within_adopted_set": abs(gap) <= DIVERGENCE_BAR_ADOPTED_SET,
    }
    guard.add("divergence_within_bar", within,
              f"|gap| {abs(gap)*1e4:.1f}bp vs {bar*1e4:.1f}bp bar"
              + (f", excused by {corporate_actions}" if excused and not within
                 else ""),
              # An EXCUSED breach must still be recorded as a warning, never as
              # silent green: otherwise a run of eight "clean" weeks could
              # conceal several wide weeks, each individually explained away,
              # and bar (b) would be met by a book that never actually tracked.
              fatal=not excused)


def check_turnover(weekly_turnovers: list[float], guard: GuardResult) -> None:
    """Bar (b): realised shadow turnover <= 1.5x the backtest average.

    Evaluated on the RUNNING AVERAGE, not week by week. A per-week reading
    exceeds this bar in 15.2% of backtest weeks, so it would fail the shadow on
    behaviour the backtest itself shows is normal — see the module docstring.
    """
    if not weekly_turnovers:
        return
    mean = sum(weekly_turnovers) / len(weekly_turnovers)
    guard.add("turnover_within_bar", mean <= TURNOVER_BAR,
              f"running mean {mean:.4f} vs bar {TURNOVER_BAR:.4f} "
              f"({TURNOVER_BAR_MULTIPLE}x backtest {BACKTEST_MEAN_WEEKLY_TURNOVER:.4f})"
              f" over {len(weekly_turnovers)} week(s)")


def evaluate_week(week: ShadowWeek, expected_session: date,
                  line_weights: dict[str, float],
                  basket_weights: dict[str, dict[str, float]],
                  e0_total_weight: float,
                  prior_turnovers: list[float]) -> GuardResult:
    """Run the full guard layer over one shadow week."""
    guard = GuardResult(publishable=True)
    check_capture_integrity(date.fromisoformat(week.data_asof),
                            expected_session, guard)
    check_weight_integrity(line_weights, basket_weights, e0_total_weight, guard)
    check_data_gaps(week.unresolved_gaps, week.fallback_lines, guard)
    check_return_sanity(week.i0_return, week.e0_return, guard)
    check_divergence(week.gap, week.corporate_actions, guard)
    check_turnover(prior_turnovers + [week.turnover_i0], guard)
    return guard


# --- Append-only log with a hash chain -------------------------------------

def verify_log_chain(records: list[dict]) -> tuple[bool, str]:
    """Confirm no previously published week has been altered or reordered.

    The 8-consecutive-week bar is only meaningful if the record of those weeks
    is tamper-evident. Each record hashes its own payload plus its predecessor's
    hash, so editing week 3 invalidates every hash from 3 onward and the breach
    names the first bad link rather than failing vaguely.
    """
    prev = ""
    for i, r in enumerate(records):
        if r.get("prev_hash", "") != prev:
            return False, (f"chain break at record {i} "
                           f"({r.get('week_ending')}): prev_hash mismatch")
        w = ShadowWeek(**{k: v for k, v in r.items() if k != "record_hash"})
        expect = w.compute_hash()
        if r.get("record_hash") != expect:
            return False, (f"record {i} ({r.get('week_ending')}) has been "
                           "altered since it was published")
        prev = r["record_hash"]
    return True, f"chain intact over {len(records)} record(s)"


def append_week(records: list[dict], week: ShadowWeek) -> list[dict]:
    """Append one week, sealing it into the chain. Refuses to rewrite history."""
    if records:
        if week.week_ending <= records[-1]["week_ending"]:
            raise ValueError(
                f"week {week.week_ending} is not after the last published week "
                f"{records[-1]['week_ending']} — the shadow log is append-only")
        week.prev_hash = records[-1]["record_hash"]
    else:
        week.prev_hash = ""
    week.record_hash = week.compute_hash()
    return records + [asdict(week)]


def consecutive_publishable_weeks(records: list[dict]) -> int:
    """Length of the CURRENT run of publishable weeks, counting back from the
    most recent. Bar (b) requires 8 CONSECUTIVE — a failure resets the run to
    zero rather than merely not incrementing it."""
    n = 0
    for r in reversed(records):
        if not r.get("publishable", False):
            break
        n += 1
    return n


def shadow_status(records: list[dict]) -> dict:
    """Where the shadow stands against bar (b), for the T4 verdict."""
    ok, detail = verify_log_chain(
        [{k: v for k, v in r.items() if k in ShadowWeek.__annotations__}
         for r in records])
    run = consecutive_publishable_weeks(records)
    turnovers = [r["turnover_i0"] for r in records if r.get("publishable")]
    gaps = [abs(r["gap"]) for r in records if r.get("publishable")]
    return {
        "chain_intact": ok, "chain_detail": detail,
        "weeks_published": len(records),
        "consecutive_publishable": run,
        "required": REQUIRED_CONSECUTIVE_WEEKS,
        "bar_b_met": ok and run >= REQUIRED_CONSECUTIVE_WEEKS,
        "mean_weekly_turnover": (sum(turnovers) / len(turnovers)
                                 if turnovers else None),
        "turnover_bar": TURNOVER_BAR,
        "max_abs_gap_bp": (max(gaps) * 1e4 if gaps else None),
        "binding_divergence_bar": BINDING_DIVERGENCE_BAR,
        # Bar (b) can be met with excused wide weeks inside the run. Count them
        # so the T4 verdict sees a book that tracked, not one that was
        # explained. Also count against the stricter adopted-set bar, so the
        # ruling on which bar binds can be made on evidence already collected.
        "weeks_excused_by_corporate_action": sum(
            1 for r in records if r.get("corporate_actions")),
        "weeks_breaching_registered_bar": sum(
            1 for r in records if abs(r.get("gap", 0)) > DIVERGENCE_BAR_REGISTERED),
        "weeks_breaching_adopted_set_bar": sum(
            1 for r in records if abs(r.get("gap", 0)) > DIVERGENCE_BAR_ADOPTED_SET),
        "weeks_on_fallback": sum(1 for r in records if r.get("fallback_lines")),
    }


def weekly_gap_from_daily(i0_daily: pd.Series, e0_daily: pd.Series,
                          week_ending: pd.Timestamp) -> tuple[float, float, float]:
    """Compound both arms' daily returns over the week ending ``week_ending``.

    Weekly returns COMPOUND; summing daily returns would understate the gap in
    exactly the volatile weeks the divergence bar exists to catch.
    """
    start = week_ending - pd.Timedelta(days=6)
    i0 = float((1 + i0_daily.loc[start:week_ending]).prod() - 1)
    e0 = float((1 + e0_daily.loc[start:week_ending]).prod() - 1)
    return i0, e0, i0 - e0
