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

Before the first logged event the state is the payload's ``initial_state``
(S2-2 of the 2026-10-04 fill-placement red-team). ``run_risk_overlay``
takes the gate's history from its state file, which already read RISK_OFF
on the blend's first close (2018-10-31), so the first logged event (the
RISK_ON of 2018-11-28) is a real switch and the 19 sessions before it were
de-risked. The published ``days_risk_off`` (255) and ``n_switches`` (20)
reproduce only under that reading; "inactive before the first event"
gives 236 and 18. A payload without ``initial_state`` keeps the inactive
reading, which was always right for the tilt (OFF at inception) and was
wrong for the gate. ``assert_initial_state_reproduces`` is the engine's
publish-time guard that the two readings agree on every date. The seed
is bounded by the payload's ``history_start``, the first published close:
before it neither overlay existed and both read inactive (a payload
without ``history_start`` is not bounded).

Dates are compared as ISO YYYY-MM-DD strings (lexicographic order equals
chronological order); no day arithmetic happens here.
"""

from __future__ import annotations

BASE_SLEEVE_WEIGHTS = {"a": 0.35, "b": 0.35, "c": 0.10, "d": 0.20}
DEFAULT_TILT_WEIGHT = 0.10
DEFAULT_DERISK_FRACTION = 0.50


def initial_active(part: dict | None, on_direction: str) -> bool:
    """Whether the overlay in ``part`` (the gate payload, or its
    ``phase22_eem_tilt`` block) was already active on the first published
    close, read from ``initial_state``. Absent or unknown -> inactive, the
    reading every payload before 2026-10-04 implied."""
    return (part or {}).get("initial_state") == on_direction


def history_start(overlay: dict | None) -> str | None:
    """First close of the published history (``history_start``, shared by
    both overlays); None on a payload that predates the field."""
    return (overlay or {}).get("history_start") or None


def _seeded_state_on(part: dict | None, overlay: dict | None,
                     date_iso: str, on_direction: str) -> bool:
    """The payload-aware reading of one overlay: inactive before
    ``history_start`` (the overlay did not exist), the payload's
    ``initial_state`` from that close until the first logged event, the
    event log after. ``part`` is the block carrying the events and the
    seed (the gate payload itself, or its ``phase22_eem_tilt`` block);
    ``overlay`` is the payload carrying ``history_start``. A payload
    without ``history_start`` is not bounded, and one without
    ``initial_state`` reads inactive before its first event, so every
    archived vintage keeps its reading."""
    start = history_start(overlay)
    if start and (not date_iso or date_iso < start):
        return False
    return state_active_on((part or {}).get("events"), date_iso, on_direction,
                           initial_active=initial_active(part, on_direction))


def state_active_on(events: list[dict] | None, date_iso: str,
                    on_direction: str, *, initial_active: bool = False) -> bool:
    """True when the latest event dated on/before ``date_iso`` has
    ``direction == on_direction``. No event on/before the date ->
    ``initial_active`` (False unless the payload's ``initial_state`` says
    otherwise; see ``initial_active``)."""
    if not date_iso:
        return False
    active = bool(initial_active)
    for ev in sorted(events or [], key=lambda e: e.get("date") or ""):
        d = ev.get("date")
        if not d or d > date_iso:
            break
        active = ev.get("direction") == on_direction
    return active


def derive_daily_states(initial: bool, events: list[dict] | None,
                        dates_iso: list[str], on_direction: str) -> list[bool]:
    """``state_active_on`` on every date of ``dates_iso`` (ascending) in one
    pass: the reader's reading of the whole history, to be checked against
    the engine's own daily series and the counts it published."""
    if any(b < a for a, b in zip(dates_iso, dates_iso[1:])):
        raise ValueError("dates_iso must be ascending")
    ev = sorted((e for e in (events or []) if e.get("date")),
                key=lambda e: e["date"])
    out: list[bool] = []
    i = 0
    active = bool(initial)
    for d in dates_iso:
        while i < len(ev) and ev[i]["date"] <= d:
            active = ev[i].get("direction") == on_direction
            i += 1
        out.append(active)
    return out


def gate_counts(active: list[bool]) -> tuple[int, int]:
    """``(n_switches, days_risk_off)`` as ``run_risk_overlay`` counts them
    from a daily RISK_OFF series.

    The engine counts on the LAGGED book: the state that held the money on
    a close is the previous close's, seeded RISK_ON before the first close
    (``states.shift(1).fillna(1.0)``). So a history that opens RISK_OFF
    counts its inception de-risk as a switch (the money moved on the
    second close), every later change counts once, the last close's state
    has not yet been held and is not counted, and ``days_risk_off`` is the
    number of closes held de-risked. ``pct_days_risk_off`` is
    ``days_risk_off / len(active) * 100``.
    """
    held = list(active[:-1])
    if not held:
        return 0, 0
    n_switches = int(held[0]) + sum(
        1 for i in range(1, len(held)) if held[i] != held[i - 1])
    return n_switches, sum(1 for h in held if h)


def assert_initial_state_reproduces(part: dict | None, on_direction: str,
                                    dates_iso: list[str],
                                    engine_active: list[bool], *,
                                    n_switches: int | None = None,
                                    days_active: int | None = None,
                                    label: str = "overlay") -> None:
    """Hard-fail at publish time when a reader seeded from
    ``part['initial_state']`` and walking ``part['events']`` does not
    reproduce the engine's own daily series on every date and, when
    given, the headline counts under ``gate_counts``. The S2-2 counterpart
    of FM-2 (``regime_publish.assert_state_since_matches_events``): the
    payload must not carry a history its own readers read differently."""
    part = part or {}
    derived = derive_daily_states(initial_active(part, on_direction),
                                  part.get("events"), dates_iso, on_direction)
    if len(derived) != len(engine_active):
        raise ValueError(
            f"{label}: {len(derived)} dates against an engine series of "
            f"{len(engine_active)}")
    bad = [d for d, a, b in zip(dates_iso, derived, engine_active) if a != b]
    if bad:
        raise ValueError(
            f"{label}: initial_state={part.get('initial_state')!r} plus the "
            f"events list does not reproduce the engine's daily states on "
            f"{len(bad)} date(s), first {bad[:3]}; publish refused (S2-2).")
    if n_switches is None and days_active is None:
        return
    n_sw, days = gate_counts(derived)
    if ((n_switches is not None and n_sw != n_switches)
            or (days_active is not None and days != days_active)):
        raise ValueError(
            f"{label}: the derived history gives {n_sw} switches and {days} "
            f"active days against the published {n_switches} / "
            f"{days_active}; publish refused (S2-2).")


def tilt_signal_as_of(overlay: dict | None) -> str | None:
    """Last date the EEM/SPY feed was observed within the freshness cap."""
    p22 = (overlay or {}).get("phase22_eem_tilt") or {}
    return p22.get("signal_as_of")


def tilt_signal_stale(overlay: dict | None) -> bool:
    """True when run_risk_overlay flagged the EEM/SPY feed as stalled."""
    p22 = (overlay or {}).get("phase22_eem_tilt") or {}
    return bool(p22.get("signal_stale"))


def tilt_stale_on(overlay: dict | None, date_iso: str) -> bool:
    """True when ``date_iso`` sits in the stalled tail of the tilt feed,
    i.e. past ``signal_as_of`` while ``signal_stale`` is set. Dates on or
    before ``signal_as_of`` rest on a real observation and are not stale."""
    if not tilt_signal_stale(overlay) or not date_iso:
        return False
    as_of = tilt_signal_as_of(overlay)
    return bool(as_of) and date_iso > as_of


def tilt_active_on(overlay: dict | None, date_iso: str) -> bool:
    """Phase 22 EEM tilt state on ``date_iso`` from the overlay's own
    event log (NOT ``current_state``, which is only valid for the latest
    date).

    A stalled feed reads OFF (2026-07-29). ``run_risk_overlay``'s money
    path already forces the tilt flat on any day the EEM/SPY feed is stale
    beyond ``EEM_MAX_STALE_DAYS``, reverting to the baseline 35/35/10/20
    blend; the reporting surfaces read ``current_state``, which holds the
    LAST VALID reading for display continuity, and so kept publishing
    EM_TILT_ON. When em_regime_context.parquet froze on 2026-07-06 the two
    disagreed for three weeks: the blend ran untilted while the live mark,
    factsheet and weekly email all showed a 10% EEM leg funded out of
    sleeve B. Mirroring the money path here makes reporting agree with the
    engine by construction. Use ``tilt_stale_on`` to badge the reason.
    """
    p22 = (overlay or {}).get("phase22_eem_tilt") or {}
    if not p22.get("enabled"):
        return False
    if tilt_stale_on(overlay, date_iso):
        return False
    return _seeded_state_on(p22, overlay, date_iso, "EM_TILT_ON")


def tilt_display_state(overlay: dict | None, date_iso: str) -> dict:
    """Everything a reporting surface needs to render the tilt leg:

      - ``active``: whether the tilt applies on ``date_iso`` (stale -> False);
      - ``stale``: whether ``date_iso`` falls in the stalled tail;
      - ``signal_as_of``: last real observation behind the state;
      - ``label``: display string, badged when stale.
    """
    active = tilt_active_on(overlay, date_iso)
    stale = tilt_stale_on(overlay, date_iso)
    as_of = tilt_signal_as_of(overlay)
    if stale:
        label = f"EM_TILT_OFF (feed stale since {as_of})"
    else:
        label = "EM_TILT_ON" if active else "EM_TILT_OFF"
    return {"active": active, "stale": stale,
            "signal_as_of": as_of, "label": label}


def derisk_active_on(overlay: dict | None, date_iso: str) -> bool:
    """Phase 19 breadth gate state on ``date_iso``."""
    ov = overlay or {}
    return _seeded_state_on(ov, ov, date_iso, "RISK_OFF")


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
