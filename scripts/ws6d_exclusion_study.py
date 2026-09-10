"""WS6d — does the overbought exclusion reduce drawdown, or does dropping names?

Registration: ``C:\\dev\\KICKOFF_ws6d-overbought-exclusion.md``, FROZEN at
vault-docs ``37022b7`` on 2026-09-10, before this module existed and before any
figure in its §5 was computed. That document governs; no gate, count, seed or
percentile below may be changed without a dated §9 amendment carrying its own
commit, and after the first scored run not even that.

--------------------------------------------------------------------------
WHAT THIS STUDY ASKS, AND WHY THE OBVIOUS COMPARISON IS NOT IT
--------------------------------------------------------------------------
The WS6 window is SEEN. I1's maximum drawdown of 28.0% against I0's 30.3% is a
filed number and is the whole reason this study exists. A headline of "I1X's
drawdown against I0's" would therefore be measuring a rule chosen after looking
at the answer, and would carry no evidence at all.

So the verdict rests on a different comparison, and the entire design follows
from it (§3 of the registration):

    I1X against P1X — a COUNT-MATCHED RANDOM exclusion drawn from I1's own
    survivor set.

Nobody has ever computed what dropping k random names from I1's survivors does
to drawdown on this window, so that null is unseen even though the window is
not. If the exclusion carries no information about WHICH names to drop, I1X
lands in the middle of the placebo distribution — and the fact that its drawdown
beats I0's then tells us only that dropping names lowers drawdown, which is not
a finding.

--------------------------------------------------------------------------
WAYS THIS COULD BE SILENTLY WRONG (registration §6), AND THE GUARD FOR EACH
--------------------------------------------------------------------------
6.1 **The verdict quietly reverts to the seen comparison.** Guard: ``verdict()``
    reads the placebo gates and nothing else; the I1X-versus-I0 figure is carried
    in the descriptive block with its caveat attached and is structurally unable
    to reach the verdict. Test-pinned.
6.2 **A two-episode gain reads as a persistent effect.** Guard: ``thinness()``,
    computed and reported whether or not the gates pass, with the rule fixed in
    the registration rather than chosen after seeing the decomposition. The
    register already holds one study — 2026-07-15-crypto-breadth-7 — where every
    arm's drawdown gain lived inside a single window.
6.3 **The result is concentration, not selection.** Guard: the placebo removes
    exactly ``k_t`` names per line-week, so concentration is held fixed across
    I1X and P1X by construction; effective N and largest weight are reported for
    both arms.
6.4 **The placebo draws from the wrong set.** Drawing from the pool rather than
    from I1's survivors would let it drop state-failing names I1X never held, and
    would flatter I1X. Guard: ``placebo_survivors`` takes I1's basket as its only
    candidate source, and ``LineWeekPlan`` is built so no wider set is reachable;
    a fatal assertion and a test pin it.
6.5 **A menu appears by accident.** Guard: one cell per arm. Every cell computed
    is written to the results file, so a later reader can count them.
6.6 **The two implementations of the exclusion drift.** Guard: I1X here IS
    ``ws6c_screened.select_screened_basket``, imported, and the decile is
    ``ws6c_screened.constants()`` read at runtime rather than restated.

--------------------------------------------------------------------------
IMPLEMENTATION NOTE (registration §7: a note, not an amendment)
--------------------------------------------------------------------------
§5.3 says to decompose drawdown into episodes and attribute the I1X-minus-P1X
difference across them, without fixing whose episode calendar to use. Resolved
here: episodes are defined on the UNSCREENED control I0's weekly index, so both
arms are measured over an identical, arm-independent set of windows. Defining
them on each arm separately would compare episodes that are not the same events,
and defining them on I1X would let the arm under test choose its own windows.
I0 is also the common ancestor — I1X and P1X are both subsets of I1, which is a
subset of I0's pool — so it is the natural reference. Fixed before the first run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from single_name_impl import BROAD_SLICES, MIN_PASS, SINGLE_NAMED_LINES
from ws6c_screened import constants as ws6c_constants
from ws6c_screened import downside_deviation, max_drawdown

# ---------------------------------------------------------------------------
# Frozen study constants (registration §4-§5). Written into the results file.
# ---------------------------------------------------------------------------
STUDY_ID = "WS6d"
REGISTRATION = "KICKOFF_ws6d-overbought-exclusion.md"
REGISTRATION_FROZEN_AT = "37022b7"

# §4.2 — the placebo
PLACEBO_PATHS = 1000
PLACEBO_SEED = 20260910

# §5.1-5.2 — the gates. BOTH required, owner-confirmed at sign-off; the
# alternative of a single 5th-percentile gate on downside deviation was put and
# declined. Lower is better for both statistics, so a pass is a LOW percentile.
PASS_PERCENTILE = 0.10

# §5.3 — the thinness gate, which OVERRIDES a pass. Taken from the cause of
# death of 2026-07-15-crypto-breadth-7.
THIN_SINGLE_EPISODE_SHARE = 0.60
THIN_MIN_CONTRIBUTING = 3
THIN_CONTRIBUTION_FLOOR = 0.10

# §4.3 — window, universes, costs
COST_SWEEP_BPS = (2, 5, 10, 20)
BINDING_COST_BPS = 10
WINDOW_START = pd.Timestamp("2018-10-12")
WINDOW_END = pd.Timestamp("2026-06-30")

ARMS = ("E0", "I0", "I1", "I1X")


def constants() -> dict:
    """The frozen study construction, as data, sealed into the results file.

    Carries ``ws6c_screened.constants()`` inside it rather than restating the
    decile, so guard 6.6 is structural: if the shadow's construction ever
    changed, this study's own record would show a different nested block.
    """
    return {
        "study": STUDY_ID,
        "registration": REGISTRATION,
        "registration_frozen_at": REGISTRATION_FROZEN_AT,
        "placebo_paths": PLACEBO_PATHS,
        "placebo_seed": PLACEBO_SEED,
        "pass_percentile": PASS_PERCENTILE,
        "gates_required": ["G1_max_drawdown", "G2_downside_deviation"],
        "thin_single_episode_share": THIN_SINGLE_EPISODE_SHARE,
        "thin_min_contributing": THIN_MIN_CONTRIBUTING,
        "thin_contribution_floor": THIN_CONTRIBUTION_FLOOR,
        "cost_sweep_bps": list(COST_SWEEP_BPS),
        "binding_cost_bps": BINDING_COST_BPS,
        "window_start": str(WINDOW_START.date()),
        "window_end": str(WINDOW_END.date()),
        "episode_reference_arm": "I0",     # implementation note, see docstring
        "exclusion_construction": ws6c_constants(),
    }


# ---------------------------------------------------------------------------
# The per-line-week plan: everything the panel builders need, computed once
# ---------------------------------------------------------------------------

class LineWeekPlan:
    """One line-week, as all four basket arms see it.

    Built once from a single I1 pass and a single I1X pass, so the 1,000 placebo
    paths never re-run selection — they resample only the exclusion step. That is
    what makes the placebo affordable, and it is also what makes it provably
    count-matched: every path drops exactly ``k_excluded`` names from exactly
    ``i1_weights``, the same set I1X excluded from (guard 6.4).
    """

    __slots__ = ("line", "rebal_date", "line_weight", "i1_weights",
                 "i1x_weights", "k_excluded", "i1_fallback", "i1x_fallback")

    def __init__(self, line: str, rebal_date, line_weight: float,
                 i1_weights: dict, i1x_weights: dict, k_excluded: int,
                 i1_fallback: bool, i1x_fallback: bool):
        self.line = line
        self.rebal_date = rebal_date
        self.line_weight = float(line_weight)
        self.i1_weights = dict(i1_weights)
        self.i1x_weights = dict(i1x_weights)
        self.k_excluded = int(k_excluded)
        self.i1_fallback = bool(i1_fallback)
        self.i1x_fallback = bool(i1x_fallback)
        # Guard 6.4, fatal and structural: I1X can only ever hold names I1 held.
        extra = set(self.i1x_weights) - set(self.i1_weights)
        assert not extra, (
            f"{line} {rebal_date}: I1X holds names outside I1's basket {sorted(extra)} "
            "— the placebo draw set would not be the set under test")
        assert 0 <= self.k_excluded <= len(self.i1_weights), (
            f"{line} {rebal_date}: k={self.k_excluded} outside I1's basket of "
            f"{len(self.i1_weights)}")


def placebo_survivors(i1_names: list[str], k: int,
                      rng: np.random.Generator) -> list[str]:
    """Drop ``k`` names uniformly at random from I1's basket (§4.2).

    The draw set is I1's basket and nothing wider: drawing from the pool would
    let the placebo remove state-failing names I1X never held, which is a
    different intervention and would flatter I1X (guard 6.4).

    Order is preserved so the renormalisation is deterministic given the draw.
    """
    if k <= 0:
        return list(i1_names)
    if k >= len(i1_names):
        return []
    dropped = set(rng.choice(np.asarray(i1_names, dtype=object), size=k,
                             replace=False).tolist())
    return [n for n in i1_names if n not in dropped]


def _renormalise(weights: dict[str, float], keep: list[str]) -> dict[str, float]:
    """Pro rata over ``keep`` (§3.4 of the WS6c registration, one weighting rule).

    Renormalising the already-normalised parent basket is exactly equivalent to
    renormalising the underlying true snapshot weights over the same subset,
    including in the equal-weight fallback case, because both are a common
    positive rescaling.
    """
    total = sum(weights[n] for n in keep)
    if total <= 0:
        return {}
    return {n: weights[n] / total for n in keep}


def placebo_rebalance_rows(plans: list[LineWeekPlan],
                           sector_weights: pd.DataFrame,
                           rebal_dates: pd.DatetimeIndex,
                           adopted: tuple[str, ...],
                           rng: np.random.Generator) -> dict:
    """One placebo path's rebalance-level name weights.

    Mirrors ``build_arm_name_weights``' book assembly exactly — every line that
    is not an adopted single-named line, and every line whose basket falls back,
    is expressed as its own ETF at its own weight — so the placebo book preserves
    E0's total weight identically to the other arms.
    """
    by_key = {(p.line, p.rebal_date): p for p in plans}
    lines = list(sector_weights.columns)
    rows: dict = {}
    for rd in rebal_dates:
        line_w = sector_weights.loc[rd]
        row: dict[str, float] = {}
        for L in lines:
            w = float(line_w.get(L, 0.0))
            if w <= 0.0:
                continue
            plan = by_key.get((L, rd))
            if (L in BROAD_SLICES or L not in adopted or plan is None
                    or plan.i1_fallback):
                row[L] = row.get(L, 0.0) + w
                continue
            keep = placebo_survivors(list(plan.i1_weights), plan.k_excluded, rng)
            # §3.3's valve, applied to the placebo identically to I1X.
            if len(keep) < MIN_PASS:
                row[L] = row.get(L, 0.0) + w
                continue
            for name, bw in _renormalise(plan.i1_weights, keep).items():
                row[name] = row.get(name, 0.0) + w * bw
        rows[rd] = row
    return rows


def rows_to_panel(rows: dict, closes_index: pd.DatetimeIndex,
                  rebal_dates: pd.DatetimeIndex,
                  eligible: pd.Timestamp) -> pd.DataFrame:
    """Rebalance rows to a daily forward-filled panel, exactly as the register's
    builder does — same reindex, same fill, same zeroing before ``eligible`` — so
    the placebo and the register arms are simulated on identical mechanics."""
    names = sorted({n for r in rows.values() for n in r})
    rb = pd.DataFrame(0.0, index=rebal_dates, columns=names)
    for rd, row in rows.items():
        for name, w in row.items():
            rb.at[rd, name] = w
    panel = rb.reindex(closes_index, method="ffill").fillna(0.0)
    panel.loc[panel.index < eligible] = 0.0
    return panel


def recording_basket_fn(sink: dict):
    """A ``basket_fn`` that runs the register's own selection and records it.

    Used to capture I1's basket per line-week in one pass, so the 1,000 placebo
    paths can resample the exclusion step without re-running selection. I1's
    weights are taken from the engine rather than reconstructed by renormalising
    I0's: the two agree except when the A3 weighting basis differs between the
    arms, and that exception is exactly the case a reconstruction would get
    silently wrong.
    """
    from single_name_impl import select_basket

    def _fn(spec, eff_date, snapshots, prices, sig, *, resolution=None,
            weights=None, line=None, rebal_date=None):
        res = select_basket(spec, eff_date, snapshots, prices, sig,
                            resolution=resolution, weights=weights)
        sink[(line, rebal_date)] = res
        return res
    return _fn


def weekly_returns_from_daily(daily: pd.Series,
                              rebal_dates: pd.DatetimeIndex) -> pd.Series:
    """Compound a daily return series into one return per rebalance interval.

    The interval is (previous rebalance, this rebalance] — a PARTITION of the
    timeline, which is what the cumulative weekly index of §4.1 requires. The
    single-week helper ``ws6b_shadow.weekly_gap_from_daily`` takes a fixed
    six-calendar-day window instead, which is right for one week in isolation and
    wrong here: the deployed calendar skips holiday-week rebalances, so a fixed
    window would drop seven days of return from the index whenever a fortnight
    falls between two rebalances.

    Consequence to state rather than bury: a skipped holiday week produces one
    fortnight-long "week", so the series carries slightly fewer than 52
    observations a year while §4.1 annualises the downside deviation by √52. The
    registration fixes that convention and it is applied identically to every
    arm and every placebo path, so no comparison is affected.
    """
    out, idx = [], []
    for i in range(1, len(rebal_dates)):
        seg = daily.loc[rebal_dates[i - 1]:rebal_dates[i]].iloc[1:]
        if not len(seg):
            continue
        out.append(float((1.0 + seg).prod() - 1.0))
        idx.append(rebal_dates[i])
    return pd.Series(out, index=pd.DatetimeIndex(idx))


def gross_and_turnover(panel: pd.DataFrame,
                       returns: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """``simulate_arm``'s two cost-independent legs, split out.

    Identical mechanics — yesterday's weights earn today's return, turnover is
    the full-vector one-way weight change — but returning the pieces lets one
    panel be scored at every point of the cost sweep without re-simulating, which
    is what makes 1,000 placebo paths across four cost points affordable.
    """
    rets = returns.reindex(columns=panel.columns).fillna(0.0)
    gross = (panel.shift(1).fillna(0.0) * rets).sum(axis=1)
    turnover = panel.diff().abs().sum(axis=1).fillna(0.0)
    return gross, turnover


def net_daily(gross: pd.Series, turnover: pd.Series, cost_bps: float) -> pd.Series:
    """Net daily return at one cost point. Matches ``simulate_arm`` exactly."""
    return gross - turnover * (cost_bps / 10_000.0)


def path_seeds(n_paths: int = PLACEBO_PATHS,
               seed: int = PLACEBO_SEED) -> list[np.random.Generator]:
    """One independent, reproducible generator per path.

    ``SeedSequence.spawn`` rather than ``seed + i``: spawned streams are
    guaranteed independent, where offset seeds are not, and the whole set is
    reproducible from the one frozen seed in the registration.
    """
    return [np.random.default_rng(s)
            for s in np.random.SeedSequence(seed).spawn(n_paths)]


# ---------------------------------------------------------------------------
# §5 — the measures, the gates and the verdict
# ---------------------------------------------------------------------------

def weekly_index(weekly_returns) -> np.ndarray:
    """Cumulative weekly return index, base 1.0 (§4.1 basis)."""
    return np.cumprod(1.0 + np.asarray(weekly_returns, dtype=float))


def percentile_rank(value: float, null_values) -> float:
    """Fraction of the null at or below ``value``.

    Both statistics are ones where LOWER is better, so a pass is a LOW rank: the
    arm sits in the favourable tail of the placebo distribution. Ties count as at
    or below, which is the conservative direction for a pass at the boundary.
    """
    arr = np.asarray(null_values, dtype=float)
    if arr.size == 0:
        return float("nan")
    return float(np.count_nonzero(arr <= value) / arr.size)


def gate(name: str, value: float, null_values) -> dict:
    """One pre-registered gate at ``PASS_PERCENTILE``. Lower is better."""
    arr = np.asarray(null_values, dtype=float)
    rank = percentile_rank(value, arr)
    return {
        "gate": name,
        "value": float(value),
        "null_n": int(arr.size),
        "null_p10": float(np.percentile(arr, 10)) if arr.size else None,
        "null_p50": float(np.percentile(arr, 50)) if arr.size else None,
        "null_p90": float(np.percentile(arr, 90)) if arr.size else None,
        "percentile_rank": rank,
        "pass_line": PASS_PERCENTILE,
        "passed": bool(rank <= PASS_PERCENTILE),
    }


def drawdown_episodes(weekly_returns) -> list[dict]:
    """Peak-to-trough-to-recovery episodes on the cumulative weekly index.

    An episode opens when the index first falls below a running peak, troughs at
    its lowest point, and closes when the index regains that peak. A drawdown
    still open at the end of the series is returned with ``recovered`` False —
    dropping it would discard whichever episode is most likely to be the deepest.
    """
    idx = weekly_index(weekly_returns)
    episodes: list[dict] = []
    peak = idx[0] if len(idx) else 1.0
    peak_i = 0
    open_ep = None
    for i, lvl in enumerate(idx):
        if lvl >= peak:
            if open_ep is not None:
                open_ep["end"] = i
                open_ep["recovered"] = True
                episodes.append(open_ep)
                open_ep = None
            peak, peak_i = lvl, i
            continue
        dd = lvl / peak - 1.0
        if open_ep is None:
            open_ep = {"start": peak_i, "trough": i, "end": None,
                       "depth": dd, "recovered": False}
        elif dd < open_ep["depth"]:
            open_ep["depth"] = dd
            open_ep["trough"] = i
    if open_ep is not None:
        open_ep["end"] = len(idx) - 1
        episodes.append(open_ep)
    return episodes


def episode_attribution(reference_returns, i1x_returns,
                        placebo_median_returns) -> dict:
    """§5.3. Where the I1X-minus-placebo advantage actually came from.

    Episodes are defined on the REFERENCE arm (I0 — see the implementation note
    in the module docstring), so both arms are scored over the same windows and
    neither chooses its own. Within each window, each arm's drawdown is measured
    on its own index, and the episode's contribution is the difference.

    ``share`` is the episode's share of the TOTAL ABSOLUTE difference, so an
    episode where the exclusion hurt counts towards concentration rather than
    silently cancelling one where it helped.
    """
    eps = drawdown_episodes(reference_returns)
    i1x_i = weekly_index(i1x_returns)
    pl_i = weekly_index(placebo_median_returns)

    def _depth(idx: np.ndarray, a: int, b: int) -> float:
        seg = idx[a:b + 1]
        if len(seg) == 0:
            return 0.0
        return float(seg.min() / seg[0] - 1.0)

    rows = []
    for e in eps:
        a, b = e["start"], e["end"]
        d_i1x, d_pl = _depth(i1x_i, a, b), _depth(pl_i, a, b)
        rows.append({
            "start_week": a, "trough_week": e["trough"], "end_week": b,
            "recovered": e["recovered"],
            "reference_depth": e["depth"],
            "i1x_depth": d_i1x, "placebo_median_depth": d_pl,
            # Positive = the exclusion was the shallower of the two here.
            "difference": d_i1x - d_pl,
        })
    total_abs = sum(abs(r["difference"]) for r in rows)
    for r in rows:
        r["share"] = (abs(r["difference"]) / total_abs) if total_abs > 0 else 0.0
    return {"n_episodes": len(rows), "total_abs_difference": total_abs,
            "episodes": rows}


def thinness(attribution: dict) -> dict:
    """§5.3's gate, which OVERRIDES a pass.

    An arm whose whole advantage is one window is not evidence of a persistent
    property. This is not a probabilistic test and it is not tuned: the numbers
    are fixed in the registration, and they exist because the register already
    contains a study whose every arm's drawdown gain lived inside a single window
    (2026-07-15-crypto-breadth-7).
    """
    rows = attribution.get("episodes", [])
    shares = sorted((r["share"] for r in rows), reverse=True)
    top = shares[0] if shares else 0.0
    contributing = sum(1 for s in shares if s > THIN_CONTRIBUTION_FLOOR)
    reasons = []
    if top > THIN_SINGLE_EPISODE_SHARE:
        reasons.append(
            f"one episode carries {top:.1%} of the total absolute difference, "
            f"above the {THIN_SINGLE_EPISODE_SHARE:.0%} line")
    if contributing < THIN_MIN_CONTRIBUTING:
        reasons.append(
            f"only {contributing} episode(s) contribute more than "
            f"{THIN_CONTRIBUTION_FLOOR:.0%}, below the required "
            f"{THIN_MIN_CONTRIBUTING}")
    return {"largest_episode_share": top,
            "n_contributing_episodes": contributing,
            "thin": bool(reasons), "reasons": reasons}


def verdict(g1: dict, g2: dict, thin: dict) -> dict:
    """The pre-registered verdict rule, and NOTHING else feeds it.

    Guard 6.1 is structural here: this function takes the two placebo gates and
    the thinness gate. The I1X-versus-I0 comparison is not a parameter, so it
    cannot reach the verdict however tempting the number turns out to be.
    """
    both = bool(g1["passed"] and g2["passed"])
    if thin["thin"]:
        outcome = "INCONCLUSIVE"
        why = ("thinness gate G3 overrides: " + "; ".join(thin["reasons"])
               + f" (gates: G1 {'pass' if g1['passed'] else 'fail'}, "
                 f"G2 {'pass' if g2['passed'] else 'fail'})")
    elif both:
        outcome = "CONFIRMED"
        why = (f"G1 {g1['percentile_rank']:.3f} and G2 "
               f"{g2['percentile_rank']:.3f}, both at or below the "
               f"{PASS_PERCENTILE:.2f} line, and the effect is not thin")
    elif g1["passed"] or g2["passed"]:
        outcome = "CONDITIONAL"
        failed = "G2 downside deviation" if g1["passed"] else "G1 max drawdown"
        why = (f"one of two gates passed; {failed} did not "
               f"(G1 {g1['percentile_rank']:.3f}, G2 {g2['percentile_rank']:.3f})")
    else:
        outcome = "REJECTED"
        why = (f"neither gate passed: G1 {g1['percentile_rank']:.3f}, "
               f"G2 {g2['percentile_rank']:.3f} against a "
               f"{PASS_PERCENTILE:.2f} line — the exclusion did not select "
               "better than a count-matched random drop")
    return {
        "outcome": outcome, "reason": why,
        "g1_passed": g1["passed"], "g2_passed": g2["passed"],
        "thin": thin["thin"],
        # Stated on every verdict, so it cannot be read off later.
        "scope": ("In-sample on the SEEN WS6 window. Supports a claim about "
                  "selection skill on 2018-2026 only, never about forward "
                  "drawdown. Authorises nothing: WS6 KEEP-ETF stands and any "
                  "adoption needs its own registration."),
    }


def concentration(weights: dict[str, float]) -> dict:
    """Effective number of names and largest single weight (§5.5, guard 6.3)."""
    if not weights:
        return {"effective_n": None, "max_weight": None, "n_names": 0}
    ssq = sum(w * w for w in weights.values())
    return {"effective_n": (1.0 / ssq) if ssq > 0 else None,
            "max_weight": max(weights.values()), "n_names": len(weights)}


def summarise_arm(weekly_returns) -> dict:
    """The descriptive block for one arm. Return and Sharpe carry NO bar (§5.5).

    Reported so the price of any drawdown benefit is visible and the study cannot
    be read as a free lunch — not so that anything turns on them.
    """
    r = np.asarray(weekly_returns, dtype=float)
    idx = weekly_index(r)
    sd = r.std(ddof=1) if r.size > 1 else float("nan")
    return {
        "n_weeks": int(r.size),
        "total_return": float(idx[-1] - 1.0) if r.size else None,
        "max_drawdown": max_drawdown(list(r)),
        "downside_deviation": downside_deviation(list(r)),
        "mean_weekly": float(r.mean()) if r.size else None,
        # Descriptive only, and named so: no bar attaches to it anywhere.
        "sharpe_annualised_no_bar": (float(r.mean() / sd * np.sqrt(52))
                                     if sd and np.isfinite(sd) and sd > 0
                                     else None),
    }


def seen_data_caveat() -> str:
    """Printed beside every I1X-versus-I0 figure, wherever one appears."""
    return ("SEEN-DATA CAVEAT: the WS6 window was already run and I1's 28.0% "
            "against I0's 30.3% is the observation that motivated this study. "
            "Any I1X-versus-I0 figure is therefore descriptive context, not "
            "evidence, and no gate reads it.")


def adopted_single_named(universe: tuple[str, ...]) -> tuple[str, ...]:
    """The single-named lines in a universe, in register order."""
    return tuple(L for L in SINGLE_NAMED_LINES if L in set(universe))
