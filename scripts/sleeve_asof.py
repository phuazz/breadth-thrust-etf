"""Two different dates a sleeve carries, and which one answers "is it stale".

WHY THIS EXISTS — 2026-08-15, caught on the deployed page.

The dashboard hero read "Last rebalance 2026-07-31" for Strategy C and the
amber banner aged it at 15 days, over the 14-day threshold, telling the
reader to re-run the pipeline before deploying. C was fine. It had
rebalanced on 2026-08-07 with every other sleeve and simply produced no
trades — the ranking did not change, so nothing needed to be bought or
sold. It was the only sleeve breaching the threshold; A, B and D sat at 8
days.

THE CONFLATION. Every engine's ``build_trade_history`` appends an entry
only when the weight vector MOVES::

    if prev is None or not np.allclose(row.values, prev.values, atol=1e-6):

so ``trade_history[-1]["date"]`` is the last date the holdings CHANGED.
``weekly_allocation_dates`` is ``weekly_w.index`` — every date the
rebalance grid ran, traded or not. The first is a subset of the second in
all four sleeve JSONs; on 2026-08-15 the two differed by 405 grid dates
against 200 trade dates for C.

Reading the trade date and printing it as "last rebalance" therefore makes
a sleeve that is behaving perfectly — rebalancing on schedule and choosing
to hold — look like a sleeve whose pipeline has stopped running. It is a
false positive in the one direction a freshness alarm must not fail: it
cries wolf on a healthy sleeve, and an alarm that does that gets ignored
on the day it is right.

WHICH TO USE. Freshness is a question about the PIPELINE — did the grid
run — so the staleness age comes from :func:`last_rebalance`. "When did
the book last change" is a question about the STRATEGY, still worth
showing, and comes from :func:`last_traded` under its own label. Any
surface printing one under the other's name is the bug this module exists
to prevent.

Dates are ISO ``YYYY-MM-DD`` strings throughout; the sleeve JSONs store
them that way and lexicographic order equals chronological order, so
nothing here needs day arithmetic except :func:`age_days`, which uses the
date library.
"""

from __future__ import annotations

from datetime import date

# Age at which a sleeve's rebalance grid is old enough to block deployment.
# Cadence is weekly, so two missed Fridays. The dashboard's JS port
# restates this literal and test_sleeve_asof pins the two together.
STALE_AFTER_DAYS = 14


def _headline(sleeve: dict | None) -> dict:
    return (sleeve or {}).get("headline") or {}


def last_rebalance(sleeve: dict | None) -> str | None:
    """Last date the sleeve's rebalance grid RAN, whether or not it traded.

    This is the freshness quantity: it advances every scheduled Friday for
    as long as the engine keeps running, and stops advancing exactly when
    the pipeline stops — which is the thing a staleness banner is asking
    about.
    """
    grid = _headline(sleeve).get("weekly_allocation_dates") or []
    if grid:
        return grid[-1]
    # Sleeve JSONs written before Phase 10.1 harmonisation carry no grid.
    # Falling back to the trade date restores the OLD behaviour for those
    # files, which errs towards declaring a sleeve stale — the safe
    # direction for a pre-deployment check, and visibly wrong rather than
    # silently missing.
    return last_traded(sleeve)


def last_traded(sleeve: dict | None) -> str | None:
    """Last date the sleeve's HOLDINGS changed.

    Legitimately older than :func:`last_rebalance` — a sleeve that reranks
    to the same book trades nothing. Never present this as a rebalance
    date.
    """
    th = _headline(sleeve).get("trade_history") or []
    return th[-1].get("date") if th else None


def age_days(iso: str | None, today: date | None = None) -> int | None:
    """Whole days from ``iso`` to ``today`` (default: today, UTC-naive).

    Returns None when the date is missing, so callers distinguish "no data"
    from "zero days old" rather than treating an absent sleeve as fresh.
    """
    if not iso:
        return None
    ref = today or date.today()
    return (ref - date.fromisoformat(iso)).days


def is_stale(sleeve: dict | None, today: date | None = None) -> bool:
    """True when the sleeve's rebalance grid has not run in
    :data:`STALE_AFTER_DAYS` days. A sleeve with no resolvable date is NOT
    reported stale here — an absent sleeve is a different failure, and the
    renderers omit it rather than warning on it."""
    age = age_days(last_rebalance(sleeve), today)
    return age is not None and age > STALE_AFTER_DAYS
