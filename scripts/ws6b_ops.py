"""WS6b T1 — ops assessment: corporate-action event rate and operator load.

Kickoff §2 item 2 (BINDING): "historical corporate-action event rate read off
the WS6 resolver tables (events per year the live book would have handled),
weekly operator-time estimate, broker mechanics (fractional shares, order
staging)". This module supplies the classifiers; the runner
(``run_ws6b_t1_ops.py``) feeds them the T1 mechanics caches and writes
``data_local/ws6b/t1_ops_assessment.json``.

Everything here is pure and dependency-injected so the tests can drive every
classifier on synthetic frames — the same discipline as ``ws6b_shadow``.

--------------------------------------------------------------------------
WHAT COUNTS AS AN EVENT THE LIVE BOOK WOULD HAVE HANDLED
--------------------------------------------------------------------------
The WS6 resolver architecture makes death detection exact: the book's name
columns are RESOLVED Norgate instruments, so a name that died carries its
``-YYYYMM`` delisting suffix in the column name itself, and its price series
terminates on its final print. Four classes are read off the caches:

1. **Deaths while held** (mergers, acquisitions, take-privates, bankruptcies):
   the price series ends before the panel does, and the book held the name in
   the sessions up to that final print. Live operator action: verify the cash
   or stock consideration landed, let the next W-FRI rebalance dispose of any
   received line. Deaths of names the book had ALREADY rotated out of are
   counted separately — the live book would have sold on rotation and never
   met the corporate action.

2. **Special distributions while held**: read as the daily return wedge
   between the CAPITALSPECIAL- and CAPITAL-adjusted closes. Norgate cannot
   split cash specials from stock spin-offs (documented in
   ``run_ws6b_t1_friction.dividend_panel``), so the class is reported with a
   size split instead: distributions at or above ``large_frac`` are
   spin-off-scale (a received line to check and possibly sell); the small
   remainder is dominated by the shale variable cash dividends, which land as
   cash and need no operator action.

3. **Capital-structure changes while held** (splits, consolidations,
   spin-off adjustments): jumps in the unadjusted/CAPITAL-adjusted price
   ratio. With fractional shares the broker adjusts positions automatically;
   the operator action is verification only.

4. **Resolver renames** whose continuing instrument the book ever held: an
   UPPER BOUND list, because the tables record the mapping but not a
   machine-readable event date (those live in the per-entry comments).
   Renames re-symbol in the account automatically; verification only.

The headline "operator-touch events per year" aggregates classes 1–3
(deaths + spin-off-scale specials + capital-structure changes) — the events
where the operator must at least look. Small cash specials and renames are
reported but not counted into the touch rate.

Every minutes figure downstream of these counts is an ESTIMATE and is marked
as such: bar (c) of the registration is judged on operator time MEASURED
during the shadow, never on this model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Median IBKR basket-trader staging effort per order line, plus fixed
# overheads, all in minutes. ESTIMATES for the T1 feasibility read — the
# shadow measures the real figure. Kept module-level so the record shows the
# knobs, and the tests pin the arithmetic rather than the guesses.
EST_FIXED_WEEKLY_MIN = 5.0        # pull weights, review shadow/guard output
EST_STAGE_FIXED_MIN = 2.0         # assemble + upload the basket CSV
EST_PER_ORDER_MIN = 0.15          # per-line review in the staging screen
EST_FILL_CHECK_MIN = 5.0          # post-close fill reconciliation
EST_CA_EVENT_MIN = 10.0           # verify one corporate action landed


def held_mask(book: pd.DataFrame, names: list[str],
              start: pd.Timestamp) -> pd.DataFrame:
    """Boolean held-per-day frame for ``names``, clipped to ``start`` onward.

    ``book`` is a daily name-weight panel whose pre-eligible rows are warmup;
    events before ``start`` are not events the live book would have handled.
    """
    cols = [c for c in names if c in book.columns]
    return book.loc[book.index >= start, cols] > 0


def held_on_or_before(held: pd.DataFrame, name: str, when: pd.Timestamp,
                      sessions: int = 5) -> bool:
    """Was ``name`` held on ``when`` or within the ``sessions`` sessions up to
    it? Positional, not calendar-day, so a death on a Monday still sees the
    prior week's holding."""
    if name not in held.columns:
        return False
    col = held[name].loc[:when]
    if col.empty:
        return False
    return bool(col.iloc[-sessions:].any())


def death_events(held: pd.DataFrame, prices: pd.DataFrame,
                 clip_sessions: int = 5,
                 lookback_sessions: int = 5) -> list[dict]:
    """Corporate deaths among the book's names, split held / not-held.

    A name whose series ends within ``clip_sessions`` sessions of the panel's
    own end is the panel clip, not a death. All date comparisons are on the
    panel's session index (pandas ``DatetimeIndex``), never manual day
    arithmetic.
    """
    idx = prices.index
    panel_end = idx.max()
    clip_floor = idx[max(len(idx) - clip_sessions, 0)]
    out: list[dict] = []
    for name in held.columns:
        if name not in prices.columns:
            continue
        last = prices[name].last_valid_index()
        if last is None or last >= clip_floor:
            continue
        was_held = held_on_or_before(held, name, last, lookback_sessions)
        weight_col = held[name].loc[:last]
        out.append({
            "name": name,
            "last_price_date": str(last.date()),
            "delist_suffix": name.rsplit("-", 1)[-1]
                             if "-" in name and name.rsplit("-", 1)[-1].isdigit()
                             else None,
            "held_at_death": was_held,
            "held_any_time_before": bool(weight_col.any()),
            "panel_end": str(panel_end.date()),
        })
    return out


def _daily_returns(panel: pd.DataFrame) -> pd.DataFrame:
    """Plain price relatives without pandas' implicit forward-fill."""
    return panel / panel.shift(1) - 1.0


def special_distribution_events(capital_close: pd.DataFrame,
                                capitalspecial_close: pd.DataFrame,
                                held: pd.DataFrame,
                                min_frac: float = 0.002,
                                large_frac: float = 0.02) -> list[dict]:
    """Special distributions (cash specials + stock spin-offs) on held days.

    The CAPITALSPECIAL series reinvests special distributions that the CAPITAL
    series does not, so on a special's ex-date the return wedge
    ``capitalspecial - capital`` equals the distribution as a fraction of
    price. Distributions below ``min_frac`` are noise-floored; at or above
    ``large_frac`` they are flagged spin-off-scale.
    """
    common = sorted(set(capital_close.columns)
                    & set(capitalspecial_close.columns) & set(held.columns))
    wedge = (_daily_returns(capitalspecial_close[common])
             - _daily_returns(capital_close[common]))
    wedge = wedge.where(np.isfinite(wedge))
    events: list[dict] = []
    for name in common:
        s = wedge[name]
        s = s[s.abs() >= min_frac]
        for when, frac in s.items():
            h = held[name].reindex(held.index)
            if when not in h.index or not bool(h.loc[when]):
                continue
            events.append({
                "name": name,
                "date": str(when.date()),
                "distribution_frac_of_price": round(float(frac), 5),
                "spin_off_scale": abs(float(frac)) >= large_frac,
            })
    return sorted(events, key=lambda e: (e["date"], e["name"]))


def capital_structure_events(unadjusted: pd.DataFrame,
                             capital_close: pd.DataFrame,
                             held: pd.DataFrame,
                             min_factor: float = 1.02) -> list[dict]:
    """Splits, consolidations and spin-off adjustments on held days.

    The unadjusted/CAPITAL-adjusted ratio is a step function that moves only
    on capital events; the step's factor is the event's size (a 10:1 split
    shows factor ≈ 10, a 5%-of-price spin-off ≈ 1.05). ``split_scale`` marks
    factors at or above 1.25 (or at or below 0.8 for consolidations).
    """
    common = sorted(set(unadjusted.columns) & set(capital_close.columns)
                    & set(held.columns))
    ratio = unadjusted[common] / capital_close[common]
    jump = np.log(ratio).diff()
    jump = jump.where(np.isfinite(jump))
    events: list[dict] = []
    for name in common:
        s = jump[name]
        s = s[s.abs() >= np.log(min_factor)]
        for when, j in s.items():
            h = held[name]
            if when not in h.index or not bool(h.loc[when]):
                continue
            factor = float(np.exp(-j))
            events.append({
                "name": name,
                "date": str(when.date()),
                "factor": round(factor, 4),
                "split_scale": factor >= 1.25 or factor <= 0.8,
            })
    return sorted(events, key=lambda e: (e["date"], e["name"]))


def rename_candidates(instrument_renames: dict[str, str],
                      known_renames: dict[str, str],
                      ever_held: set[str]) -> list[dict]:
    """Resolver rename entries whose continuing instrument the book ever held.

    UPPER BOUND: the tables map snapshot ticker -> continuing instrument but
    carry the event date only in comments, so whether the symbol change fell
    inside the window, on a held week, is not machine-checkable here. Renames
    re-symbol in the account automatically; operator action is verification.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for table, src_map in (("instrument_renames", instrument_renames),
                           ("known_renames", known_renames)):
        for src, dst in src_map.items():
            if dst == src or dst not in ever_held:
                continue
            key = (src, dst)
            if key in seen:
                continue
            seen.add(key)
            out.append({"snapshot_ticker": src, "instrument": dst,
                        "table": table})
    return sorted(out, key=lambda e: (e["instrument"], e["snapshot_ticker"]))


def weekly_order_stats(trades: pd.DataFrame) -> dict:
    """Orders and one-way turnover per rebalance date, inception separated.

    The first rebalance establishes the book from flat and is not a recurring
    weekly load, so it is reported on its own and excluded from the medians.
    """
    per = (trades.groupby("date")
           .agg(orders=("name", "size"), one_way_turnover=("abs_delta", "sum"))
           .sort_index())
    if per.empty:
        return {"n_rebalances": 0}
    inception = per.iloc[0]
    rest = per.iloc[1:]
    return {
        "n_rebalances": int(len(per)),
        "inception_orders": int(inception["orders"]),
        "inception_one_way_turnover": float(inception["one_way_turnover"]),
        "orders_mean": float(rest["orders"].mean()),
        "orders_median": float(rest["orders"].median()),
        "orders_p90": float(rest["orders"].quantile(0.9)),
        "orders_max": int(rest["orders"].max()),
        "turnover_weekly_mean": float(rest["one_way_turnover"].mean()),
        "turnover_weekly_median": float(rest["one_way_turnover"].median()),
    }


def operator_time_model(orders_median: float, orders_p90: float,
                        touch_events_per_year: float,
                        budget_min_per_week: float = 30.0) -> dict:
    """Weekly operator minutes, typical and p90 — ESTIMATES throughout.

    Bar (c) of the registration is judged on MEASURED operator time during the
    shadow and live running; this model exists so the T1 feasibility read is
    explicit about what it assumed rather than silent.
    """
    def _week(n_orders: float) -> float:
        return (EST_FIXED_WEEKLY_MIN + EST_STAGE_FIXED_MIN
                + EST_PER_ORDER_MIN * n_orders + EST_FILL_CHECK_MIN)

    typical = _week(orders_median)
    p90 = _week(orders_p90)
    ca_amortised = touch_events_per_year * EST_CA_EVENT_MIN / 52.0
    return {
        "_estimate": ("ALL FIGURES ESTIMATES — bar (c) is judged on operator "
                      "time measured during the shadow, not on this model."),
        "assumptions_min": {
            "fixed_weekly": EST_FIXED_WEEKLY_MIN,
            "stage_fixed": EST_STAGE_FIXED_MIN,
            "per_order": EST_PER_ORDER_MIN,
            "fill_check": EST_FILL_CHECK_MIN,
            "per_corporate_action": EST_CA_EVENT_MIN,
        },
        "typical_week_min": round(typical, 1),
        "p90_week_min": round(p90, 1),
        "corporate_action_amortised_min_per_week": round(ca_amortised, 2),
        "typical_plus_ca_min": round(typical + ca_amortised, 1),
        "p90_plus_ca_min": round(p90 + ca_amortised, 1),
        "budget_min_per_week": budget_min_per_week,
        "typical_within_budget": (typical + ca_amortised)
                                 <= budget_min_per_week,
        "p90_within_budget": (p90 + ca_amortised) <= budget_min_per_week,
        "shadow_note": ("The shadow itself is zero-touch (scheduled publisher "
                        "+ guard); its weekly operator load is reviewing the "
                        "published guard line, minutes not tens of minutes."),
    }
