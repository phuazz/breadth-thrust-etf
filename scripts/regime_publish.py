"""Regime-publish freshness + reconciliation + near-threshold guards.

Phase 28.5 (2026-06-20) — silent-staleness audit response. The breadth panel
that drives the de-risk gate went stale for ~11 weeks (2026-03-27 to
2026-06-18). The 2026-06-13 weekly publish read ``risk_overlay.json``'s
``current_state`` / ``current_state_since`` / ``current_breadth`` fields and
rendered them with no awareness that the panel feeding those fields had
stopped advancing on 2026-05-29 — so the factsheet watchlist printed
"breadth 55%, ARMED, +35pp buffer" while the actual market reading on
2026-03-27 had been 19.4% (a true de-risk trigger).

This module centralises three guards so any future stale-panel situation
trips immediately and visibly:

  1. ``regime_publish_status(panel_end_date, breadth, off, on, today, budget)``
     - Returns ``status='stale'`` when the breadth panel lags today by more
       than ``budget`` trading days. Renderers must replace the confident
       state block with the returned stale banner.
     - Returns ``status='near'`` when current_breadth is within ``near_band``
       of either gate threshold. Renderers must replace 'ARMED' with 'NEAR'.

  2. ``assert_state_since_matches_events(state, since, events)``
     - Hard-fails when the published ``current_state_since`` does not equal
       the most recent event date matching ``current_state``.

  3. ``detect_historical_revision(prior_events, new_events)``
     - Returns the list of dates where a previously-committed event has been
       rewritten by the current run (e.g. added 2026-03-27 RISK_OFF that was
       previously absent on 2026-06-13). Renderers surface this so the user
       sees that the historical regime ledger changed in this build.

All date arithmetic uses ``datetime.date`` + ``numpy.busday_count`` for
trading-day math — never compute weekdays from memory (vault rule). US
market holidays are NOT subtracted from the count: the 5-trading-day
default budget is conservative enough that the ~9 US holidays per year
do not move any verdict from ok to stale.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable

import numpy as np


# Default chosen on the basis of the 2026-03/06 incident:
# breadth panel updates daily on trading days; the weekly refresh cadence
# means days 0-4 of the trading week are within "this cycle's panel" if
# the refresh ran on day 0 (Saturday morning). Day 5+ = missed the cycle
# entirely → alarm. The incident's panel was 11 trading days stale on the
# 2026-06-13 publish, comfortably over 5.
DEFAULT_BUDGET_TRADING_DAYS = 5

# ±2pp around either threshold. The 2026-03-27 trigger was at 19.39%
# breadth — 0.61pp below the 20% off_threshold. Anything within 2pp is
# operationally indistinguishable from the boundary because the breadth
# series quantum is ~0.5pp per constituent moving above/below its MA.
DEFAULT_NEAR_BAND = 0.02


@dataclass(frozen=True)
class RegimePublishStatus:
    publishable: bool
    status: str                  # 'ok' | 'stale' | 'near'
    lag_trading_days: int
    near_threshold: bool
    proximity_band: str | None   # 'below_off'|'above_off_close'|'below_on_close'|'above_on'|None
    message: str
    panel_end_date: str          # ISO YYYY-MM-DD
    today: str                   # ISO YYYY-MM-DD

    def as_dict(self) -> dict:
        return asdict(self)


def _trading_days_between(start: date, end: date) -> int:
    """Trading days strictly between ``start`` and ``end`` (exclusive of
    start, inclusive of end) under the US Mon-Fri convention.

    Uses ``numpy.busday_count``. Returns 0 when ``end <= start``.
    """
    if end <= start:
        return 0
    return int(np.busday_count(start.isoformat(), end.isoformat()))


def regime_publish_status(
    panel_end_date: date,
    current_breadth: float,
    off_threshold: float,
    on_threshold: float,
    today: date,
    budget_trading_days: int = DEFAULT_BUDGET_TRADING_DAYS,
    near_band: float = DEFAULT_NEAR_BAND,
) -> RegimePublishStatus:
    """Compute the publishability verdict for the current regime headline.

    Args:
        panel_end_date: latest date in ``breadth_csp1.json`` (the panel
            feeding the regime gate).
        current_breadth: latest breadth value rendered in the headline.
        off_threshold: the OFF gate (default 0.20).
        on_threshold: the ON gate (default 0.50).
        today: the build's reference date.
        budget_trading_days: max acceptable lag of panel_end_date behind
            today. Above this → status='stale', publishable=False.
        near_band: ± width around either threshold inside which the state
            is labelled 'near' rather than confident.

    Status precedence: stale takes priority over near; a stale panel near
    a threshold is still 'stale' (a publishable=False verdict).
    """
    lag = _trading_days_between(panel_end_date, today)

    proximity: str | None = None
    near = False
    if abs(current_breadth - off_threshold) <= near_band:
        near = True
        proximity = "below_off" if current_breadth < off_threshold else "above_off_close"
    elif abs(current_breadth - on_threshold) <= near_band:
        near = True
        proximity = "below_on_close" if current_breadth < on_threshold else "above_on"

    if lag > budget_trading_days:
        # The 'REGIME STALE' headline is the renderers' responsibility — every
        # call site (factsheet banner, email banner, dashboard badge) already
        # prepends its own 'REGIME STALE — DO NOT TRADE OFF THIS PANEL'
        # header before this message. Including the prefix again here
        # produced a doubled-up "REGIME STALE ... REGIME STALE — breadth
        # panel as of ..." rendering in the 2026-06-20 factsheet, which the
        # user flagged. The message is now the SUPPORTING SENTENCE only:
        # facts + remediation, no redundant header.
        return RegimePublishStatus(
            publishable=False,
            status="stale",
            lag_trading_days=lag,
            near_threshold=near,
            proximity_band=proximity,
            message=(
                f"Breadth panel as of {panel_end_date.isoformat()}, "
                f"{lag} trading days behind today ({today.isoformat()}). "
                f"Budget is {budget_trading_days} trading days. "
                "Do not trade on this regime state until the panel is "
                "refreshed via `python scripts/refresh_all.py`."
            ),
            panel_end_date=panel_end_date.isoformat(),
            today=today.isoformat(),
        )

    if near:
        return RegimePublishStatus(
            publishable=True,
            status="near",
            lag_trading_days=lag,
            near_threshold=True,
            proximity_band=proximity,
            message=(
                f"NEAR THRESHOLD — current breadth {current_breadth*100:.1f}% "
                f"is within {near_band*100:.0f}pp of a gate boundary "
                f"(off {off_threshold*100:.0f}% / on {on_threshold*100:.0f}%). "
                "A small data revision could flip the state. Hold position "
                "pending confirmation at the next rebalance."
            ),
            panel_end_date=panel_end_date.isoformat(),
            today=today.isoformat(),
        )

    return RegimePublishStatus(
        publishable=True,
        status="ok",
        lag_trading_days=lag,
        near_threshold=False,
        proximity_band=None,
        message="",
        panel_end_date=panel_end_date.isoformat(),
        today=today.isoformat(),
    )


def assert_state_since_matches_events(
    current_state: str,
    current_state_since: str,
    events: Iterable[dict],
    series_start_date: str | None = None,
) -> None:
    """Hard-fail when ``current_state_since`` does not equal the most recent
    event date matching ``current_state``.

    Args:
        current_state: 'RISK_ON' or 'RISK_OFF'.
        current_state_since: ISO date string as published.
        events: iterable of ``{'date': 'YYYY-MM-DD', 'direction': 'RISK_ON'
            | 'RISK_OFF', 'breadth': float}`` records.
        series_start_date: when no event of ``current_state``'s kind exists,
            the state must trace back to this date (the series origin).

    Raises:
        ValueError: when the headline date and events disagree (FM-2).
    """
    matching = [e for e in events if e.get("direction") == current_state]
    if matching:
        expected = max(e["date"] for e in matching)
        if current_state_since != expected:
            raise ValueError(
                f"Regime publish failed reconciliation: "
                f"current_state_since={current_state_since!r} does not equal "
                f"the most recent {current_state} event date ({expected!r}). "
                "This is FM-2 — the headline date was not updated when the "
                "events list was. Re-run scripts/run_risk_overlay.py or "
                "check the events computation."
            )
        return
    if series_start_date is not None and current_state_since != series_start_date:
        raise ValueError(
            f"Regime publish failed reconciliation: no {current_state} "
            f"event in history but current_state_since="
            f"{current_state_since!r} does not equal "
            f"series_start_date={series_start_date!r}."
        )


def detect_historical_revision(
    prior_events: list[dict], new_events: list[dict],
) -> list[dict]:
    """Compare two events lists and report dates where the regime history
    changed between runs.

    Catches the silent-rewrite scenario where a roster catch-up adds events
    at past dates (the 2026-03-27 RISK_OFF was absent in the 2026-06-13
    publish, appeared in the 2026-06-18 publish — a regime history that
    silently became 11 weeks more bearish in retrospect).

    Returns a list of revision records, each a dict with at least ``date``
    and ``change``. Change values:

      'added'   — the new run has an event at this date, prior did not.
                  Most common when a roster fix recomputes historical
                  breadth and a sub-threshold reading appears.
      'changed' — both have an event at this date but ``direction`` differs.
      'removed' — prior had an event at this date, new does not. Rare —
                  would mean a recomputation eliminated a past trigger.

    Events later than the prior run's last event date are excluded — those
    are normal new tail-end entries, not revisions of history.
    """
    if not prior_events or not new_events:
        return []
    prior_by_date = {e["date"]: e for e in prior_events}
    new_by_date = {e["date"]: e for e in new_events}
    prior_last = max(prior_by_date)
    revisions: list[dict] = []
    for d, e in new_by_date.items():
        if d > prior_last:
            continue
        if d not in prior_by_date:
            revisions.append({
                "date": d, "change": "added",
                "to": e.get("direction"), "breadth": e.get("breadth"),
            })
        elif prior_by_date[d].get("direction") != e.get("direction"):
            revisions.append({
                "date": d, "change": "changed",
                "from": prior_by_date[d].get("direction"),
                "to": e.get("direction"), "breadth": e.get("breadth"),
            })
    for d, e in prior_by_date.items():
        if d <= prior_last and d not in new_by_date:
            revisions.append({
                "date": d, "change": "removed",
                "from": e.get("direction"),
            })
    return sorted(revisions, key=lambda r: r["date"])
