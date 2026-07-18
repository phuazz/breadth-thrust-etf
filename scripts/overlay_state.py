"""Point-in-time overlay state shared by every reporting surface.

The EEM tilt and the Phase 19 de-risk gate both flip mid-history, and any
table that compares TWO rebalance dates (activity cards, trades tables)
must price each column with the sleeve weights that applied ON THAT DATE.
Until 2026-07-18 the email, factsheet and dashboard all scaled BOTH the
prior and the new column by the CURRENT state's sleeve weight, so on a
flip week every Strategy B prior weight was misstated (up to 5.7pp NAV on
the 2025-04-11 rebalance) and the tilt's own 10% NAV ENTER/EXIT — the
largest trade of such a week — appeared in no table at all. The de-risk
gate was ignored outright by the holdings tables (a RISK_OFF week would
have printed the full-equity book while the live target was 50% + SHY).

This module is the single source of truth for "which sleeve weights and
overlay legs applied on date D". The monitor repo's adapter implements
the same convention (`multi-strategy-portfolio/scripts/adapter.py,
build_weight_history`): the state on a date is the direction of the
LATEST event dated ON OR BEFORE it — an event dated D takes effect on D.

Dates are compared as ISO YYYY-MM-DD strings (lexicographic order equals
chronological order); no day arithmetic happens here.
"""

from __future__ import annotations

BASE_SLEEVE_WEIGHTS = {"a": 0.35, "b": 0.35, "c": 0.10, "d": 0.20}
DEFAULT_TILT_WEIGHT = 0.10
DEFAULT_DERISK_FRACTION = 0.50


def state_active_on(events: list[dict] | None, date_iso: str,
                    on_direction: str) -> bool:
    """True when the latest event dated on/before ``date_iso`` has
    ``direction == on_direction``. No event on/before the date -> False
    (both overlays start inactive at inception)."""
    if not events or not date_iso:
        return False
    active = False
    for ev in sorted(events, key=lambda e: e.get("date") or ""):
        d = ev.get("date")
        if not d or d > date_iso:
            break
        active = ev.get("direction") == on_direction
    return active


def tilt_active_on(overlay: dict | None, date_iso: str) -> bool:
    """Phase 22 EEM tilt state on ``date_iso`` from the overlay's own
    event log (NOT ``current_state``, which is only valid for the latest
    date)."""
    p22 = (overlay or {}).get("phase22_eem_tilt") or {}
    return bool(p22.get("enabled")) and state_active_on(
        p22.get("events"), date_iso, "EM_TILT_ON")


def derisk_active_on(overlay: dict | None, date_iso: str) -> bool:
    """Phase 19 breadth gate state on ``date_iso``."""
    return state_active_on((overlay or {}).get("events"), date_iso,
                           "RISK_OFF")


def tilt_weight(overlay: dict | None) -> float:
    p22 = (overlay or {}).get("phase22_eem_tilt") or {}
    return float((p22.get("parameters") or {}).get(
        "tilt_weight", DEFAULT_TILT_WEIGHT))


def derisk_fraction(overlay: dict | None) -> float:
    gp = (overlay or {}).get("gate_parameters") or {}
    return float(gp.get("derisk_fraction", DEFAULT_DERISK_FRACTION))


def sleeve_nav_weights(overlay: dict | None, date_iso: str) -> dict:
    """Effective NAV multiplier per sleeve on ``date_iso``, plus the
    overlay legs, mirroring ``mark_to_market_live._build_effective_weights``:

      - EEM tilt ON  -> sleeve B funds the tilt (0.35 -> 0.25) and an EEM
        leg of ``tilt_weight`` exists;
      - RISK_OFF     -> every equity leg (sleeves AND tilt) is scaled by
        (1 - derisk_fraction) and the freed fraction sits in the fallback
        ticker (SHY).

    Returns ``{"a","b","c","d"}`` NAV multipliers already scaled for the
    gate, together with ``tilt_on``, ``derisk_on``, ``equity_scaler``,
    ``tilt_nav`` (scaled EEM leg, 0.0 when off) and ``shy_overlay``
    (the gate's SHY fraction, 0.0 when RISK_ON).
    """
    t_on = tilt_active_on(overlay, date_iso)
    d_on = derisk_active_on(overlay, date_iso)
    t_wt = tilt_weight(overlay)
    d_frac = derisk_fraction(overlay)
    scaler = (1.0 - d_frac) if d_on else 1.0
    weights = {
        "a": BASE_SLEEVE_WEIGHTS["a"] * scaler,
        "b": (BASE_SLEEVE_WEIGHTS["b"] - t_wt if t_on
              else BASE_SLEEVE_WEIGHTS["b"]) * scaler,
        "c": BASE_SLEEVE_WEIGHTS["c"] * scaler,
        "d": BASE_SLEEVE_WEIGHTS["d"] * scaler,
    }
    return {
        **weights,
        "tilt_on": t_on,
        "derisk_on": d_on,
        "equity_scaler": scaler,
        "tilt_nav": t_wt * scaler if t_on else 0.0,
        "shy_overlay": d_frac if d_on else 0.0,
    }
