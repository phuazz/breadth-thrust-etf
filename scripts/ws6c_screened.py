"""WS6c — screened arm (I1X): pure core and guard layer.

Registration: ``C:\\dev\\KICKOFF_ws6c-screened-arm.md``, FROZEN at vault-docs
commit ``cc84122`` (2026-09-09), §9 Amendments empty. That document governs; no
parameter of its §3 may be changed by this implementation.

What this arm is: a second, OBSERVATIONAL arm of the WS6b shadow. The screened
PARTIAL-5 basket is computed weekly from the same inputs as the signed
unscreened arm I0 and logged beside it. It accrues an out-of-sample record and
nothing else — no bar, no verdict, no adoption path, and per §4.3 no Sharpe, no
net return and no return gap read as anything but a tracking statistic.

This module is pure and dependency-injected in the manner of ``ws6b_shadow``: it
never fetches, never writes, and takes its inputs as arguments so the tests can
drive every guard branch on synthetic data. ``run_ws6c_screened_arm.py`` supplies
the live data.

--------------------------------------------------------------------------
THREE WAYS THIS COULD BE SILENTLY WRONG (kickoff §6), AND THE GUARD FOR EACH
--------------------------------------------------------------------------
6.1 **The decile threshold is a free parameter chosen without data.** 0.10, the
    ceil rule and the full-roster eligible set were fixed by judgement, not by
    fitting. Re-tuned on the shadow record, any of them becomes a fitted
    parameter with n equal to the record's length. Guard: they are module
    constants, returned as data by ``constants()``, sealed inside every hashed
    weekly record, pinned by test, and ``check_constants_unchanged`` refuses a
    record whose sealed constants differ from the module's. No other threshold,
    rounding rule or eligible set is computed anywhere in this module (§3.7).

6.2 **The excluded-weight rule changes the risk profile independently of the
    screen.** Pro rata (§3.4) removes the cash and ETF confounds, so equity
    exposure and line weight are identical across the arms; what remains is that
    the screened basket holds fewer names at larger weights. Guard: the §3.5
    identity is logged per line-week — w_x, r_S, r_x, the spread and the residual
    — together with the effective number of names and the largest single weight
    in BOTH arms, so the weight moved is read separately from the headline
    difference. The stated limit stands: this record cannot separate "the
    excluded names fell" from "moving that weight into fewer names changed the
    profile". That needs the count-and-weight-matched placebo §6.2 names, which
    is deliberately NOT computed here.

6.3 **The two arms share a holdings source, so a sourcing failure hits both and
    must not read as a screen effect.** Guard, in four parts: ``check_paired``
    (the recomputed I0 weekly return must equal the WS6b record's ``i0_return``
    to 1e-9, and that record's hash is sealed into the screened record — a miss
    marks the week UNPAIRED and it does not count); ``check_subset_invariant``
    (the I1X survivors are a subset of the I0 basket, per line-week, fatal);
    inert line-week logging (a line the WS6b week carries on its ETF is on its
    ETF here, logged "screen inert"; a week with every basketed line inert is
    logged "screen inert — no effect measurable", kept in the record and
    excluded from the §4.1 statistics); and the unresolved count split into its
    shared and screen-specific components.

--------------------------------------------------------------------------
THREE MORE, FOUND IN THE ENGINE RATHER THAN THE REGISTRATION
--------------------------------------------------------------------------
A. **The decile ROUNDING RULE is implemented as one of its near-misses.** Not a
   float-representation problem — ``math.ceil(0.10 * n)`` agrees with exact
   arithmetic for every n up to 2,000,000 (checked) — but a wrong-function
   problem, and each wrong function is silent: it just drops one name too many or
   one too few from every populated line, every week. ``int(0.10 * 22)`` is 2 and
   ``round(0.10 * 22)`` is 2 where §3.3 says 3; ``int(0.10 * 30) + 1`` is 4 where
   §3.3 says 3. ``overbought_k`` therefore takes the ceiling of an EXACT rational
   — correct by construction and independent of the representation question — and
   the document's four worked cases (15->2, 22->3, 30->3, 70->7) are pinned by
   test because between them they reject all three near-misses. The exact
   multiples of ten, 30 and 70, are in that list to catch "add one".

B. **A reimplemented screen drifts from ``select_basket``.** §3.3 puts the
   exclusion BETWEEN the state screen and the MIN_PASS test, so this cannot be a
   post-filter on ``select_basket``'s output — that function applies MIN_PASS
   first. Guard: the unscreened side is the engine's OWN ``select_basket(I0, …)``
   result rather than a transcription of it, so coverage, resolution, missing
   price and A3 weighting semantics cannot drift; and an equivalence test asserts
   that when no overbought name reaches the pool, ``select_screened_basket``
   returns exactly ``select_basket(I1, …)``'s weights.

C. **The §3.5 identity computed on the wrong week's weights.** ``simulate_arm``
   has yesterday's weights earn today's return, so a published week is earned by
   the basket selected at the PREVIOUS rebalance. Decomposing the week against
   the basket selected AT ``week_ending`` would explain a week that has not
   traded yet, and the §6.2 guard would read green while saying nothing. The
   attribution block is therefore keyed to ``weights_in_force_from`` — the last
   rebalance strictly before ``week_ending`` — while the selection block is keyed
   to ``week_ending``; both carry their date.

--------------------------------------------------------------------------
TWO IMPLEMENTATION NOTES (§7: logged as notes, NOT amendments)
--------------------------------------------------------------------------
1. **"Excluded names" in §3.5 means every I0 name the screened arm does not
   hold**, not only the overbought ones. The identity
   ``r_I1X - r_I0 = w_x * (r_S - r_x)`` holds only when S and X partition the I0
   basket, and §3.5's own gloss ("the survivor-minus-excluded spread scaled by
   the weight moved") is the whole weight moved. Both quantities are therefore
   logged under distinct names: ``overbought_excluded`` / ``w_overbought`` for
   §3.3's exclusion alone, and ``dropped_vs_i0`` / ``w_x`` for the identity's X,
   which is the state failures and the overbought exclusions together.

2. **``select_screened_basket`` returns a ``ScreenedSelection``, not a bare
   ``BasketResult``.** §3.2-3.4 require diagnostics (k, n_eligible, w_x, the
   survivor and excluded lists) that ``single_name_impl.BasketResult`` has no
   fields for, and that dataclass belongs to the frozen WS6 engine. The
   ``BasketResult`` the registration asks for is carried verbatim as
   ``ScreenedSelection.basket`` and is what the arm builder consumes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from fractions import Fraction

import pandas as pd

from single_name_impl import (
    ARM_BY_ID,
    M_POOL,
    MIN_PASS,
    BasketResult,
    _asof_pos,
    _true_basket_weights,
    normalise_ticker,
    select_basket,
    snapshot_asof,
)
from ws6b_shadow import (
    DIVERGENCE_BAR_ADOPTED_SET,
    DIVERGENCE_BAR_REGISTERED,
    WEEKLY_RETURN_BOUND,
    GuardResult,
    check_weight_integrity,
)

# ---------------------------------------------------------------------------
# Frozen construction constants (kickoff §3.3-3.4). See guard 6.1: these are
# written into every weekly record so a later change is visible in the log, and
# a different value is a NEW REGISTRATION with a new log, never an edit here.
# ---------------------------------------------------------------------------
ARM_ID = "I1X"
DECILE_FRACTION = 0.10
DECILE_ROUNDING = "ceil"
ELIGIBLE_SET = "full_roster"
EXCLUDED_WEIGHT_RULE = "pro_rata"
REGISTRATION = "KICKOFF_ws6c-screened-arm.md"
REGISTRATION_FROZEN_AT = "cc84122"

# The base screen is WS6 arm I1, reused verbatim (§3.2) — pool topM, trend-state
# screen, no ranking, MIN_PASS valve. Nothing about it is re-specified here.
BASE_ARM = "I1"
UNSCREENED_ARM = "I0"

# §6.3(i): the pairing tolerance. §3.5: the identity tolerance.
PAIRING_TOL = 1e-9
IDENTITY_TOL = 1e-12

WEEKS_PER_YEAR = 52          # §4.1 downside-deviation annualisation
CITABLE_AT_WEEKS = 52        # §5; descriptive, never a bar

# 0.10 as a binary float is 0.1000000000000000055511151231257827. It happens to
# ceil correctly at every roster size that can occur, but pinning the exact
# rational once here and doing all decile arithmetic in integers off it removes
# the question rather than relying on that.
_DECILE = Fraction(DECILE_FRACTION).limit_denominator(10_000)


def constants() -> dict:
    """The frozen §3 construction, as data (guard 6.1).

    Returned rather than read from module state at report time so that a record
    published under one construction still says so afterwards, and sealed inside
    the hashed payload so a later edit breaks the chain rather than passing.
    """
    return {
        "arm_id": ARM_ID,
        "decile_fraction": DECILE_FRACTION,
        "decile_rounding": DECILE_ROUNDING,
        "eligible_set": ELIGIBLE_SET,
        "excluded_weight_rule": EXCLUDED_WEIGHT_RULE,
        "min_pass": MIN_PASS,
        "m_pool": M_POOL,
        "base_arm": BASE_ARM,
        "registration": REGISTRATION,
        "registration_frozen_at": REGISTRATION_FROZEN_AT,
    }


def constants_line(c: dict | None = None) -> str:
    """One line naming the governing construction, for the weekly log line."""
    c = c or constants()
    return (f"construction ({c['registration_frozen_at']}): "
            f"arm {c['arm_id']} | decile {c['decile_fraction']:.2f} "
            f"{c['decile_rounding']} on {c['eligible_set']} | "
            f"MIN_PASS {c['min_pass']} | excluded weight {c['excluded_weight_rule']}")


def overbought_k(n_eligible: int) -> int:
    """k = ceil(DECILE_FRACTION * n) over the eligible set (§3.3).

    Exact-rational arithmetic, deliberately: a TRUE ceiling, not a rounding
    function that resembles one. The document's worked cases are n = 15 -> 2,
    22 -> 3, 30 -> 3, 70 -> 7, and between them they reject truncation (wrong at
    15 and 22), banker's rounding (wrong at 22) and "add one" (wrong at 30 and
    70). An off-by-one here changes the basket on every populated line, every
    week, and shows up nowhere but in the weights.

    ceil(a/b) for non-negative integers is ``-(-a // b)``.
    """
    if n_eligible <= 0:
        return 0
    return -(-int(n_eligible) * _DECILE.numerator // _DECILE.denominator)


# ---------------------------------------------------------------------------
# Selection (§3.2-3.4)
# ---------------------------------------------------------------------------

@dataclass
class ScreenedSelection:
    """One line-week's I1X selection, with everything §3.3-3.5 need logged.

    ``basket`` is the ``BasketResult`` the arm builder consumes — the object the
    registration asks ``select_screened_basket`` to return; the remaining fields
    are the diagnostics ``BasketResult`` has no room for (implementation note 2).

    ``i0_weights`` is the UNSCREENED basket for the same line-week, taken from
    the engine's own ``select_basket(I0, …)`` rather than rebuilt, so ``w_x`` is
    measured where §3.3 says to measure it and the subset invariant is checked
    against the arm it actually pairs with.
    """

    basket: BasketResult
    n_eligible: int = 0
    k: int = 0
    overbought: list[str] = field(default_factory=list)
    # §3.3's exclusion alone: pool names that passed the state screen and sat in
    # the overbought set.
    overbought_excluded: list[str] = field(default_factory=list)
    state_failed: list[str] = field(default_factory=list)
    survivors: list[str] = field(default_factory=list)
    # §3.5's X: every I0 name the screened arm does not hold (implementation
    # note 1). state_failed + overbought_excluded, in I0 basket order.
    dropped_vs_i0: list[str] = field(default_factory=list)
    i0_weights: dict[str, float] = field(default_factory=dict)
    i1x_weights: dict[str, float] = field(default_factory=dict)
    w_x: float = 0.0                 # weight of dropped_vs_i0 in the I0 basket
    w_overbought: float = 0.0        # weight of overbought_excluded alone
    i0_weight_source: str = ""
    i1x_weight_source: str = ""
    # Pool names whose strength was undefined at t-1 — the screened-arm-specific
    # half of the §4.2 unresolved split.
    undefined_strength: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def fallback(self) -> bool:
        return self.basket.fallback


def _concentration(weights: dict[str, float]) -> dict:
    """Effective number of names (1 / sum of squared weights) and the largest
    single weight — the §3.5 pair that lets a profile difference be read against
    concentration rather than against selection alone (guard 6.2)."""
    if not weights:
        return {"effective_n": None, "max_weight": None, "n_names": 0}
    ssq = sum(w * w for w in weights.values())
    return {"effective_n": (1.0 / ssq) if ssq > 0 else None,
            "max_weight": max(weights.values()),
            "n_names": len(weights)}


def select_screened_basket(spec, eff_date: pd.Timestamp | None,
                           snapshots: dict, prices: pd.DataFrame, sig: dict,
                           resolution: dict | None = None,
                           weights: dict | None = None) -> ScreenedSelection:
    """Build one line-week's arm-I1X basket. Same inputs as ``select_basket``.

    Composed from the engine's own pieces, never editing them (§3.2 defers to the
    engine's I1 semantics; §3.3-3.5 are this document's):

      1. The UNSCREENED basket is ``select_basket(I0, …)`` verbatim. It supplies
         the present pool, the coverage and missing-price diagnostics, and the
         A3 weights against which w_x is measured. Taking it from the engine
         rather than transcribing it is guard B: coverage, resolution and A3
         semantics cannot drift out of step with the arm this one pairs against.
      2. The state screen (§3.2) keeps present-pool names with trend state True
         at t-1.
      3. The overbought exclusion (§3.3) is applied AFTER the state screen and
         BEFORE MIN_PASS, over the FULL t-1 roster's eligible cross-section.
      4. MIN_PASS (§3.3) applies to the survivors of both filters; fewer than
         MIN_PASS reverts the line to its ETF for the week, logged.
      5. Weights (§3.4) are the true snapshot weights renormalised over the
         survivors via ``_true_basket_weights`` — the same pro-rata
         renormalisation the base screen already applies, so the arm has one
         weighting rule rather than two and the line stays fully invested.

    ``spec`` is accepted for signature parity with ``select_basket`` and is not
    read: the construction is fixed by the registration, not by a spec, and an
    arm register entry for I1X would be a WS6 register change this document does
    not authorise.
    """
    del spec  # the construction is frozen by the registration, not by a spec

    i0 = select_basket(ARM_BY_ID[UNSCREENED_ARM], eff_date, snapshots, prices,
                       sig, resolution=resolution, weights=weights)
    if i0.fallback:
        # No unscreened basket to screen. The line is its ETF in BOTH arms, which
        # is §6.3(iii)'s inert line-week; the reason is inherited verbatim so the
        # log says why rather than inventing a screened-arm explanation.
        return ScreenedSelection(basket=i0, reason=i0.reason,
                                 i0_weight_source=i0.weight_source)

    snap_date, roster = snapshot_asof(snapshots, eff_date)
    pos = _asof_pos(prices.index, eff_date)
    state_row = sig["state"].iloc[pos]
    strength_row = sig["strength"].iloc[pos]

    # --- the eligible cross-section (§3.3): the FULL roster, not the pool ----
    # "every name on the line's t-1 roster that resolves to a price column and
    # has a defined strength at the t-1 row". Strength is NaN wherever the price
    # or the SMA is undefined, so notna() covers presence and warm-up together.
    res_map = resolution.get(snap_date, {}) if resolution is not None else None
    price_cols = set(prices.columns)
    eligible: list[tuple[str, int]] = []      # (symbol, cap-rank position)
    seen: set[str] = set()
    sym_to_ish: dict[str, str] = {}
    for cap_rank, ish in enumerate(roster):
        if res_map is not None:
            sym = res_map.get(ish)
            if sym is None:
                continue                      # unresolved: counted by I0 already
        else:
            sym = normalise_ticker(ish)
        if sym in seen:
            continue                          # a roster may list a name twice
        seen.add(sym)
        sym_to_ish[sym] = ish
        if sym in price_cols and bool(pd.notna(strength_row.get(sym))):
            eligible.append((sym, cap_rank))

    n_eligible = len(eligible)
    k = overbought_k(n_eligible)
    # Rank on strength descending; an exact tie goes to the name EARLIER in the
    # snapshot's cap-rank order (§3.3), which the second key delivers because
    # Python sorts tuples left to right and cap_rank ascends with the roster.
    ranked = sorted(eligible, key=lambda sc: (-float(strength_row[sc[0]]), sc[1]))
    overbought = [s for s, _ in ranked[:k]]
    overbought_set = set(overbought)

    # --- screen, exclusion, MIN_PASS, weights -------------------------------
    present_pool = list(i0.weights)           # I0 holds exactly the present pool
    passing = [s for s in present_pool if bool(state_row.get(s, False))]
    state_failed = [s for s in present_pool if s not in set(passing)]
    overbought_excluded = [s for s in passing if s in overbought_set]
    survivors = [s for s in passing if s not in overbought_set]
    undefined_strength = [s for s in present_pool
                          if not bool(pd.notna(strength_row.get(s)))]

    dropped = [s for s in present_pool if s not in set(survivors)]
    w_x = sum(i0.weights[s] for s in dropped)
    w_ob = sum(i0.weights[s] for s in overbought_excluded)

    common = dict(
        n_eligible=n_eligible, k=k, overbought=overbought,
        overbought_excluded=overbought_excluded, state_failed=state_failed,
        survivors=survivors, dropped_vs_i0=dropped, i0_weights=dict(i0.weights),
        w_x=w_x, w_overbought=w_ob, i0_weight_source=i0.weight_source,
        undefined_strength=undefined_strength)

    if len(survivors) < MIN_PASS:
        # §3.3: the same valve as the base screen, applied to the survivors of
        # BOTH filters. The line reverts to its ETF for the week, logged.
        reason = (f"only {len(survivors)} survived screen+exclusion "
                  f"(< {MIN_PASS})")
        return ScreenedSelection(
            basket=BasketResult(
                fallback=True, reason=reason, n_pool=i0.n_pool,
                n_covered=i0.n_covered, n_present=i0.n_present,
                n_pass=len(passing), uncovered=list(i0.uncovered),
                missing_price=list(i0.missing_price)),
            reason=reason, **common)

    basket_weights, weight_source = _true_basket_weights(
        survivors, sym_to_ish, weights, snap_date)
    return ScreenedSelection(
        basket=BasketResult(
            fallback=False, reason="", weights=basket_weights,
            n_pool=i0.n_pool, n_covered=i0.n_covered, n_present=i0.n_present,
            n_pass=len(passing), n_selected=len(survivors),
            uncovered=list(i0.uncovered), missing_price=list(i0.missing_price),
            weight_source=weight_source),
        i1x_weights=dict(basket_weights), i1x_weight_source=weight_source,
        **common)


def screened_basket_fn(sink: dict | None = None):
    """A ``basket_fn`` for ``build_arm_name_weights``, recording its selections.

    The builder passes ``line`` and ``rebal_date`` so the sink can be keyed by
    line-week; every other argument matches ``select_basket``'s signature. The
    sink is how the publisher recovers the selection for the rebalance whose
    weights were IN FORCE during the measured week (guard C) as well as the one
    selected at ``week_ending``.
    """
    def _fn(spec, eff_date, snapshots, prices, sig, *, resolution=None,
            weights=None, line=None, rebal_date=None) -> BasketResult:
        sel = select_screened_basket(spec, eff_date, snapshots, prices, sig,
                                     resolution=resolution, weights=weights)
        if sink is not None:
            sink[(line, rebal_date)] = sel
        return sel.basket
    return _fn


# ---------------------------------------------------------------------------
# §3.5 — the identity the log records
# ---------------------------------------------------------------------------

def within_line_weights(panel_row: pd.Series, line_codes: set[str],
                        line_weight: float) -> dict[str, float]:
    """One line's within-line basket weights, read back off an ISOLATED book.

    ``panel_row`` is a name-weight row from a build restricted to a single line,
    so every member column belongs to that line; ``line_codes`` is EVERY line in
    the sector book and ``line_weight`` is the line's own weight that week. The
    member weights divided by the line weight sum to 1.0 for a basketed line, and
    to nothing for a line the builder reverted to its ETF.

    ``line_codes`` must be the WHOLE book, not just the single-named lines. Under
    a single-line restriction the three broad slices are still held as their own
    ETFs and still appear as columns in the panel; filtering only the
    single-named codes leaves CSP1's weight in the row, where it is then divided
    by this line's weight and counted into this line's basket. That is not a
    rounding matter — it made a basket sum to 1.37 on live data (2026-09-04,
    IUCS), which is a weight-integrity failure that would refuse the week.
    """
    if line_weight <= 0:
        return {}
    w = panel_row[[c for c in panel_row.index if c not in line_codes]]
    return {str(n): float(v) / line_weight for n, v in w.items() if v > 0}


def name_week_returns(returns: pd.DataFrame,
                      week_ending: pd.Timestamp) -> dict[str, float]:
    """Per-name COMPOUNDED return over the week ending ``week_ending``.

    Same window and convention as ``ws6b_shadow.weekly_gap_from_daily`` — the
    six calendar days back from the rebalance Friday — so the identity is
    decomposing the same week the headline returns measure. Compounded, not
    summed: with weights fixed for the week a basket's weekly return is the
    weight-average of its members' COMPOUNDED weekly returns, and summing daily
    returns would leave a residual that looks like a weighting error.

    All date arithmetic is by pandas offset, never a manual day count.
    """
    win = returns.loc[week_ending - pd.Timedelta(days=6):week_ending]
    return {str(k): float(v) for k, v in ((1.0 + win).prod() - 1.0).items()}


def line_identity(sel: ScreenedSelection,
                  name_returns: dict[str, float]) -> dict:
    """The §3.5 decomposition for one line-week, with its residual.

    With X the I0 names the screened arm does not hold, w_x their combined weight
    in the unscreened basket, r_S the weight-averaged return of the survivors and
    r_x that of X:

        r_I1X - r_I0 = w_x * (r_S - r_x)

    Line-level buy-and-hold arithmetic on the week's compounded member returns,
    which is what makes it an exact identity rather than an approximation: with
    weights fixed for the week, a basket's weekly return IS the weight-average of
    its members' weekly returns. The residual is therefore a live check that the
    screened weights are the unscreened weights renormalised over the survivors
    and nothing else — the arithmetic form of §3.4.

    A name absent from ``name_returns`` contributes 0.0 rather than raising: a
    member with no return series for the week is already an unresolved gap and is
    counted as one, and a KeyError here would refuse the week for a reason the
    log would not name.
    """
    def _r(sym: str) -> float:
        return float(name_returns.get(sym, 0.0))

    i0_w = sel.i0_weights
    surv, dropped = sel.survivors, sel.dropped_vs_i0
    w_x = sel.w_x
    w_s = sum(i0_w[s] for s in surv)

    r_i0 = sum(i0_w[s] * _r(s) for s in i0_w)
    r_s = (sum(i0_w[s] * _r(s) for s in surv) / w_s) if w_s > 0 else 0.0
    r_x = (sum(i0_w[s] * _r(s) for s in dropped) / w_x) if w_x > 0 else 0.0
    r_i1x = sum(w * _r(s) for s, w in sel.i1x_weights.items())

    lhs = r_i1x - r_i0
    rhs = w_x * (r_s - r_x)
    return {
        "r_i0_line": r_i0, "r_i1x_line": r_i1x,
        "w_x": w_x, "r_s": r_s, "r_x": r_x, "spread": r_s - r_x,
        "lhs": lhs, "rhs": rhs, "residual": lhs - rhs,
        "w_overbought": sel.w_overbought,
        "n_dropped": len(dropped), "n_survivors": len(surv),
        "dropped_vs_i0": list(dropped),
        "overbought_excluded": list(sel.overbought_excluded),
        "k": sel.k, "n_eligible": sel.n_eligible,
        "weight_source_i0": sel.i0_weight_source,
        "weight_source_i1x": sel.i1x_weight_source,
        "concentration_i0": _concentration(i0_w),
        "concentration_i1x": _concentration(sel.i1x_weights),
    }


def selection_record(sel: ScreenedSelection) -> dict:
    """The line's selection AT the week's rebalance — the book being formed,
    which trades next week. Kept separate from the attribution block so the two
    are never read as the same week's weights (guard C)."""
    return {
        "fallback": sel.fallback, "reason": sel.reason,
        "n_eligible": sel.n_eligible, "k": sel.k,
        "overbought": list(sel.overbought),
        "overbought_excluded": list(sel.overbought_excluded),
        "state_failed": list(sel.state_failed),
        "survivors": list(sel.survivors),
        "dropped_vs_i0": list(sel.dropped_vs_i0),
        "w_x": sel.w_x, "w_overbought": sel.w_overbought,
        "undefined_strength": list(sel.undefined_strength),
        "weight_source_i0": sel.i0_weight_source,
        "weight_source_i1x": sel.i1x_weight_source,
        "concentration_i0": _concentration(sel.i0_weights),
        "concentration_i1x": _concentration(sel.i1x_weights),
    }


# ---------------------------------------------------------------------------
# The weekly record
# ---------------------------------------------------------------------------

@dataclass
class ScreenedWeek:
    """One screened week. Serialised verbatim into the log."""

    week_ending: str                    # ISO date of the W-FRI rebalance
    i1x_return: float
    i0_return: float
    e0_return: float
    gap_i1x_e0: float                   # §4.2 tracking gap
    gap_i1x_i0: float                   # the §4.1 headline difference's input
    turnover_i1x: float
    turnover_i0: float
    lines_held: list[str]
    lines_basketed: list[str]           # from the WS6b record, never re-decided
    lines_inert: list[str]              # §6.3(iii): on the ETF in the WS6b week
    lines_screen_fallback: list[str]    # basketed in I0, on the ETF in I1X
    all_inert: bool
    # §6.3(i): the pairing. The WS6b record's own i0_return and hash, sealed.
    ws6b_i0_return: float
    ws6b_record_hash: str
    paired: bool
    pairing_error: float
    selection: dict = field(default_factory=dict)      # per line, at week_ending
    attribution: dict = field(default_factory=dict)    # per line, §3.5 in force
    weights_in_force_from: str = ""     # the rebalance the week was earned on
    unresolved_shared: list[str] = field(default_factory=list)
    unresolved_screen_specific: list[str] = field(default_factory=list)
    data_asof: str = ""
    engine_commit: str = ""
    params_sha: str = ""
    # The frozen §3 construction in force when this week was published. Inside
    # the hashed payload deliberately (guard 6.1): a later change to any of the
    # three must be visible in the log and must break the chain if backdated.
    constants: dict = field(default_factory=dict)
    prev_hash: str = ""
    record_hash: str = ""

    def payload(self) -> dict:
        d = asdict(self)
        d.pop("record_hash")
        return d

    def compute_hash(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def check_paired(recomputed_i0: float, ws6b_i0: float,
                 guard: GuardResult) -> bool:
    """§6.3(i). The screened arm recomputes I0 from the same inputs; if it does
    not reproduce the WS6b record's own ``i0_return`` the two arms are not on the
    same book and the week cannot be compared. Logged UNPAIRED, not counted.

    Fatal deliberately: an unpaired week that still published would enter the
    §4.1 statistics as though the comparison were like-for-like.
    """
    err = abs(recomputed_i0 - ws6b_i0)
    ok = err <= PAIRING_TOL
    guard.add("paired_to_ws6b", ok,
              f"recomputed I0 {recomputed_i0:+.10f} vs WS6b record "
              f"{ws6b_i0:+.10f} (error {err:.2e}, tol {PAIRING_TOL:.0e})"
              + ("" if ok else " — UNPAIRED, week does not count"))
    return ok


def check_subset_invariant(selections: dict, guard: GuardResult) -> None:
    """§6.3(ii). The I1X survivors must be a subset of the I0 basket names, per
    line-week. Fatal: a survivor outside the I0 basket means the screened arm
    holds something the unscreened arm never did, so any difference between them
    is no longer the screen."""
    bad = {}
    for line, sel in selections.items():
        extra = sorted(set(sel.survivors) - set(sel.i0_weights))
        if extra:
            bad[line] = extra
    guard.add("subset_invariant", not bad,
              f"survivors outside the I0 basket: {bad}" if bad
              else f"survivors are a subset of I0 on all "
                   f"{len(selections)} basketed line(s)")


def check_identity(attribution: dict, guard: GuardResult) -> None:
    """§3.5, asserted rather than merely logged.

    Fatal, and the reason is guard 6.2: the decomposition is what lets a drawdown
    difference be read against the weight moved. A residual means the screened
    weights are not the unscreened weights renormalised over the survivors —
    most plausibly because the two arms landed on different A3 weighting bases —
    and then w_x and the spread describe a book that was not held.
    """
    if not attribution:
        guard.add("identity_3_5", True, "no basketed line carried the identity "
                                        "this week (all inert or on the valve)")
        return
    worst_line, worst = max(attribution.items(),
                            key=lambda kv: abs(kv[1]["residual"]))
    bad = {L: a["residual"] for L, a in attribution.items()
           if abs(a["residual"]) > IDENTITY_TOL}
    guard.add("identity_3_5", not bad,
              (f"residual breach: {bad}" if bad else
               f"r_I1X - r_I0 == w_x*(r_S - r_x) on all {len(attribution)} "
               f"line(s); worst residual {worst['residual']:.2e} ({worst_line}) "
               f"vs tol {IDENTITY_TOL:.0e}"))


def check_weighting_basis(attribution: dict, guard: GuardResult) -> None:
    """Both arms must have reached their weights on the same A3 basis.

    ``_true_basket_weights`` drops a whole line-week to equal weight when any
    selected member lacks a usable snapshot weight. Because the arms select
    different members, one can land on "snapshot" while the other lands on "ew" —
    a difference in weighting basis masquerading as a difference in composition.
    Warned rather than fatal: the identity guard above already refuses the week
    numerically, and this names the cause instead of leaving it to be inferred.
    """
    bad = {L: (a["weight_source_i0"], a["weight_source_i1x"])
           for L, a in attribution.items()
           if a["weight_source_i0"] != a["weight_source_i1x"]}
    guard.add("weighting_basis_matched", not bad,
              f"A3 basis differs between arms: {bad}" if bad
              else "both arms on the same A3 basis on every line",
              fatal=False)


def check_inert(lines_basketed: list[str], lines_inert: list[str],
                guard: GuardResult) -> bool:
    """§6.3(iii). A line the WS6b week carries on its ETF is on its ETF here too;
    a week with EVERY basketed line inert is kept in the record and excluded from
    the §4.1 statistics. Neither is a failure — an inert week is the screen
    having nothing to act on, which is information, not a fault."""
    all_inert = not lines_basketed
    if lines_inert:
        guard.add("screen_inert_lines", False,
                  f"screen inert on {sorted(lines_inert)} (on the ETF in the "
                  "WS6b week, identically here)", fatal=False)
    if all_inert:
        guard.add("screen_inert_week", False,
                  "screen inert — no effect measurable; week kept in the record "
                  "and excluded from the §4.1 statistics", fatal=False)
    return all_inert


def check_return_sanity(returns: dict[str, float], guard: GuardResult) -> None:
    """An implausible weekly book return is a data error, not a market move.
    Same bound and reasoning as the WS6b guard, extended to the third arm."""
    bad = [n for n, r in returns.items() if abs(r) > WEEKLY_RETURN_BOUND]
    guard.add("weekly_return_within_bound", not bad,
              f"implausible weekly return: {bad}" if bad
              else " ".join(f"{n} {r:+.4f}" for n, r in returns.items()))


def check_constants_unchanged(sealed: dict, guard: GuardResult) -> None:
    """Guard 6.1. The record's sealed construction must equal this module's.

    A record whose constants differ was computed under a different construction
    and cannot join the same series: §6.1's whole point is that the shadow record
    is citable for what happened at 0.10 and for nothing else.
    """
    now = constants()
    diff = {k: (sealed.get(k), v) for k, v in now.items() if sealed.get(k) != v}
    guard.add("constants_unchanged", not diff,
              f"sealed construction differs from the module (sealed, module): "
              f"{diff}" if diff
              else f"decile {now['decile_fraction']:.2f} "
                   f"{now['decile_rounding']} on {now['eligible_set']}, "
                   f"MIN_PASS {now['min_pass']}")


def evaluate_week(week: ScreenedWeek, selections: dict,
                  line_weights: dict[str, float],
                  basket_weights: dict[str, dict[str, float]],
                  e0_total_weight: float) -> GuardResult:
    """Run the full guard layer over one screened week."""
    guard = GuardResult(publishable=True)
    check_constants_unchanged(week.constants, guard)
    check_paired(week.i0_return, week.ws6b_i0_return, guard)
    check_subset_invariant(selections, guard)
    check_weight_integrity(line_weights, basket_weights, e0_total_weight, guard)
    check_identity(week.attribution, guard)
    check_weighting_basis(week.attribution, guard)
    check_inert(week.lines_basketed, week.lines_inert, guard)
    check_return_sanity({"i1x": week.i1x_return, "i0": week.i0_return,
                         "e0": week.e0_return}, guard)
    return guard


# ---------------------------------------------------------------------------
# Append-only log with a hash chain (mirrors ws6b_shadow exactly)
# ---------------------------------------------------------------------------

def verify_log_chain(records: list[dict]) -> tuple[bool, str]:
    """Confirm no previously published week has been altered or reordered.

    Each record hashes its own payload plus its predecessor's hash, so editing
    week 3 invalidates every hash from 3 onward and the breach names the first
    bad link. The construction constants sit inside that payload (guard 6.1), so
    a backdated parameter change is a chain break, not a quiet edit.
    """
    prev = ""
    for i, r in enumerate(records):
        if r.get("prev_hash", "") != prev:
            return False, (f"chain break at record {i} "
                           f"({r.get('week_ending')}): prev_hash mismatch")
        w = ScreenedWeek(**{k: v for k, v in r.items()
                            if k in ScreenedWeek.__annotations__
                            and k != "record_hash"})
        if r.get("record_hash") != w.compute_hash():
            return False, (f"record {i} ({r.get('week_ending')}) has been "
                           "altered since it was published")
        prev = r["record_hash"]
    return True, f"chain intact over {len(records)} record(s)"


def append_week(records: list[dict], week: ScreenedWeek) -> list[dict]:
    """Append one week, sealing it into the chain. Refuses to rewrite history."""
    if records:
        if week.week_ending <= records[-1]["week_ending"]:
            raise ValueError(
                f"week {week.week_ending} is not after the last published week "
                f"{records[-1]['week_ending']} — the screened log is append-only")
        week.prev_hash = records[-1]["record_hash"]
    else:
        week.prev_hash = ""
    week.record_hash = week.compute_hash()
    return records + [asdict(week)]


# ---------------------------------------------------------------------------
# §4 measures. Descriptive only: no bar, no verdict, and no Sharpe (§4.3).
# ---------------------------------------------------------------------------

def max_drawdown(weekly_returns: list[float]) -> float | None:
    """Largest peak-to-trough fall of the cumulative weekly return index, as a
    POSITIVE fraction (§4.1). Weekly closes, so a within-week trough is invisible
    to both arms — the basis is the same on both sides, which is the point."""
    if not weekly_returns:
        return None
    level, peak, worst = 1.0, 1.0, 0.0
    for r in weekly_returns:
        level *= (1.0 + r)
        peak = max(peak, level)
        worst = min(worst, level / peak - 1.0)
    return -worst


def downside_deviation(weekly_returns: list[float]) -> float | None:
    """sqrt(mean over all published weeks of min(r, 0)^2), annualised by sqrt(52)
    (§4.1, MAR = 0). The mean is over ALL published weeks, not only the negative
    ones — a series with few losing weeks should score low, and dividing by the
    count of losers instead would erase exactly that."""
    if not weekly_returns:
        return None
    ms = sum(min(r, 0.0) ** 2 for r in weekly_returns) / len(weekly_returns)
    return (ms ** 0.5) * (WEEKS_PER_YEAR ** 0.5)


def _stat_weeks(records: list[dict]) -> list[dict]:
    """The weeks the §4.1 statistics are computed over: publishable, paired, and
    not wholly inert (§6.3(iii) keeps an all-inert week in the record but out of
    the statistics)."""
    return [r for r in records
            if r.get("publishable") and r.get("paired") and not r.get("all_inert")]


def status(records: list[dict]) -> dict:
    """The §4 measures over the record so far. DESCRIPTIVE — no bar, no verdict.

    §5 is explicit that eight weeks can conclude nothing about drawdown or
    downside deviation as a property of the screen: with 8 observations each
    arm's maximum drawdown is one path segment and the difference between them is
    one number with no sampling distribution. The figures below are reported with
    their basis and n and nothing is inferred from them here.
    """
    ok, detail = verify_log_chain(
        [{k: v for k, v in r.items() if k in ScreenedWeek.__annotations__}
         for r in records])
    stat = _stat_weeks(records)
    i1x = [r["i1x_return"] for r in stat]
    i0 = [r["i0_return"] for r in stat]

    dd_i1x, dd_i0 = max_drawdown(i1x), max_drawdown(i0)
    dsd_i1x, dsd_i0 = downside_deviation(i1x), downside_deviation(i0)
    gaps_bp = [r["gap_i1x_e0"] * 1e4 for r in stat]
    t_i1x = [r["turnover_i1x"] for r in stat]
    t_i0 = [r["turnover_i0"] for r in stat]

    return {
        "chain_intact": ok, "chain_detail": detail,
        "constants_in_force": constants(),
        "constructions_seen_in_log": sorted(
            {json.dumps(r.get("constants", {}), sort_keys=True) for r in records}),
        "weeks_in_log": len(records),
        "n_published": sum(1 for r in records if r.get("publishable")),
        "n_in_statistics": len(stat),
        "n_inert": sum(1 for r in records if r.get("all_inert")),
        "n_unpaired": sum(1 for r in records if not r.get("paired")),
        "citable_at_published_weeks": CITABLE_AT_WEEKS,
        # --- §4.1 primary, descriptive -------------------------------------
        "primary": {
            "basis": ("weekly closes, gross of trading costs, same published "
                      "weeks both arms; MAR = 0; annualised by sqrt(52)"),
            "max_drawdown_i1x": dd_i1x,
            "max_drawdown_i0": dd_i0,
            # Negative = the screened arm's drawdown was the shallower one.
            "max_drawdown_diff_pp": (None if dd_i1x is None or dd_i0 is None
                                     else (dd_i1x - dd_i0) * 100.0),
            "downside_deviation_i1x": dsd_i1x,
            "downside_deviation_i0": dsd_i0,
            "downside_deviation_diff_pp": (
                None if dsd_i1x is None or dsd_i0 is None
                else (dsd_i1x - dsd_i0) * 100.0),
            "n": len(stat),
        },
        # --- §4.2 secondary, logged and unbarred ---------------------------
        "secondary": {
            "gap_i1x_e0_bp": gaps_bp,
            "mean_gap_i1x_e0_bp": (sum(gaps_bp) / len(gaps_bp)
                                   if gaps_bp else None),
            "max_abs_gap_i1x_e0_bp": (max(abs(g) for g in gaps_bp)
                                      if gaps_bp else None),
            # REFERENCE LINES ONLY. Neither is a bar for this arm and a breach
            # changes nothing (§4.2) — they are printed so the tracking gap has
            # a scale, not so it has a verdict.
            "reference_line_registered_bp": DIVERGENCE_BAR_REGISTERED * 1e4,
            "reference_line_adopted_set_bp": DIVERGENCE_BAR_ADOPTED_SET * 1e4,
            "turnover_i1x_weekly": t_i1x,
            "turnover_i0_weekly": t_i0,
            "mean_turnover_i1x": (sum(t_i1x) / len(t_i1x)) if t_i1x else None,
            "mean_turnover_i0": (sum(t_i0) / len(t_i0)) if t_i0 else None,
            "unresolved_shared_total": sum(
                len(r.get("unresolved_shared", [])) for r in records),
            "unresolved_screen_specific_total": sum(
                len(r.get("unresolved_screen_specific", [])) for r in records),
            "screen_fallback_line_weeks": sum(
                len(r.get("lines_screen_fallback", [])) for r in records),
        },
        "not_measured": ["sharpe", "net_return", "return_gap_as_performance"],
    }
