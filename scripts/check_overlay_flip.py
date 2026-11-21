"""Daily check: has the risk-overlay gate or the EEM tilt flipped since the live book was decided?

WHY THIS EXISTS (2026-11-20).

The backtest trades the breadth gate and the EEM tilt on the session after the
signal (run_risk_overlay.py: ``states.shift(1)`` and ``sig.shift(1)``). The
live process does not: ``overlay_decision.py`` runs only inside the core
WEEKLY refresh and is pinned to the Friday decision anchor
(live_targets.py refuses any other date), so a flip on a Monday is not traded
until the following Monday's fill. Nothing in the pipeline said so when it
happened. The daily Norgate gate feed (data/gate_states_norgate.json, pushed
about 07:15 SGT) already carried the answer every morning; no consumer
compared it with the book.

This script makes that comparison and nothing more. It changes no rule, no
weight and no file. Whether to trade a flip before the weekly process acts on
it is the owner's decision, logged as a discretionary override; the message
says so.

WHAT IT COMPARES, AND ON WHICH BASIS.

  * Gate: the current state in data/gate_states_norgate.json (Norgate
    #SPX%MA50, deployed hysteresis off 0.20 / on 0.50) against
    ``gate_on`` in data/overlay_decision.json. Same feed, same state machine:
    overlay_decision.py reads its gate from the same file.
  * Tilt: the V2 golden cross (EEM/SPY 50-day MA above 200-day MA), computed
    with run_risk_overlay's own ``_load_eem_data`` and
    ``_compute_eem_tilt_signal`` to the last completed NYSE session, against
    ``tilt_on`` in data/overlay_decision.json. Same functions the weekly
    decision uses.

Note the sign: ``gate_on`` in overlay_decision.json means the DE-RISK is on,
i.e. the gate state is RISK_OFF.

GUARD (vault rule: no unattended agent without one). A check that cannot see
current inputs must not report "no flip". If the gate feed is two or more
NYSE sessions behind the last completed session, or the tilt input does not
reach it, the status is STALE and it mails a warning instead of an all-clear.
One session behind is tolerated: the gate feed is published once a morning
and a run before the publish would otherwise false-alarm.

Python datetime months are 1-indexed. Session counting goes through the NYSE
exchange calendar, never through weekday arithmetic.

Exit code: 0 whenever the assessment completed (FLIP, STALE or OK all exit 0;
the workflow routes on the ``status`` output). A crash exits non-zero, which
fails the workflow, which the failure email and the fleet-watch ``run`` row
both see.

Usage:
    python scripts/check_overlay_flip.py
    python scripts/check_overlay_flip.py --now 2026-11-20T01:00:00Z   # rehearsal
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))

GATE_PATH = DATA / "gate_states_norgate.json"
DECISION_PATH = DATA / "overlay_decision.json"

# Gate feed may lag the last completed NYSE session by this many sessions
# before the check reports STALE rather than assessing.
GATE_MAX_SESSIONS_BEHIND = 1


def sessions_between(cal, after: pd.Timestamp, upto: pd.Timestamp) -> int:
    """NYSE sessions strictly after ``after`` and up to and including ``upto``."""
    after, upto = pd.Timestamp(after).normalize(), pd.Timestamp(upto).normalize()
    if upto <= after:
        return 0
    sched = cal.schedule(start_date=after + pd.Timedelta(days=1), end_date=upto)
    return int(len(sched))


def run_start(dates: list, values: list) -> str | None:
    """First date of the final run of equal values (the date the current state began)."""
    if not dates:
        return None
    last = values[-1]
    i = len(values) - 1
    while i > 0 and values[i - 1] == last:
        i -= 1
    return str(pd.Timestamp(dates[i]).date())


def assess(gate: dict, decision: dict, tilt: pd.Series | None,
           required: pd.Timestamp, cal) -> dict:
    """Pure assessment. ``tilt`` is the daily 0/1 tilt signal indexed by date."""
    required = pd.Timestamp(required).normalize()
    problems: list[str] = []
    flips: list[str] = []
    lines: list[str] = []

    # ---- gate ----
    last_bar = pd.Timestamp(gate["last_bar"]).normalize()
    behind = sessions_between(cal, last_bar, required)
    daily_state = str(gate["current_state"]).upper()
    if daily_state not in ("RISK_ON", "RISK_OFF"):
        raise ValueError(f"unexpected gate state {daily_state!r}")
    book_state = "RISK_OFF" if bool(decision["gate_on"]) else "RISK_ON"
    since = run_start(gate["series"]["dates"], gate["series"]["state"])
    breadth = float(gate["current_breadth"])
    lines.append(
        f"Gate: daily feed {daily_state} since {since} (breadth {breadth:.1%} "
        f"as at {last_bar.date()}; de-risk below 20%, re-engage above 50%). "
        f"Live book decided {decision['as_of']} holds {book_state}.")
    if behind > GATE_MAX_SESSIONS_BEHIND:
        problems.append(
            f"gate feed last bar {last_bar.date()} is {behind} NYSE sessions "
            f"behind the last completed session {required.date()}")
    elif daily_state != book_state:
        flips.append(f"GATE {book_state} -> {daily_state} on {since}")

    # ---- tilt ----
    book_tilt = "EM_TILT_ON" if bool(decision["tilt_on"]) else "EM_TILT_OFF"
    if tilt is None or tilt.empty:
        problems.append("EEM/SPY tilt input unavailable")
        lines.append(f"Tilt: NOT ASSESSED (input unavailable). Live book holds {book_tilt}.")
    else:
        tilt = tilt.loc[:required]
        tilt_last = pd.Timestamp(tilt.index[-1]).normalize()
        daily_tilt = "EM_TILT_ON" if float(tilt.iloc[-1]) >= 0.5 else "EM_TILT_OFF"
        tilt_since = run_start(list(tilt.index), list(tilt.values))
        lines.append(
            f"Tilt: daily signal {daily_tilt} since {tilt_since} "
            f"(as at {tilt_last.date()}). Live book holds {book_tilt}.")
        if tilt_last != required:
            problems.append(
                f"tilt input ends {tilt_last.date()}, not the last completed "
                f"session {required.date()}")
        elif daily_tilt != book_tilt:
            flips.append(f"TILT {book_tilt} -> {daily_tilt} on {tilt_since}")

    if flips:
        status = "FLIP"
        subject = "[OVERLAY] " + "; ".join(flips) + " - live book not yet changed"
    elif problems:
        status = "STALE"
        subject = "[WARN] Overlay flip check cannot assess - " + "; ".join(problems)
    else:
        status = "OK"
        subject = "Overlay flip check: no flip, live book matches the daily signals"

    body = [subject, ""] + lines
    if problems and flips:
        body += ["", "Also: " + "; ".join(problems)]
    if status == "FLIP":
        body += [
            "",
            "What the process does: the weekly refresh picks this up at the next "
            "Friday-close decision and it is traded at the following Monday fill. "
            "The backtest assumes the trade on the session after the signal.",
            "",
            "Trading it before then is a discretionary override: log it. "
            "This message changes no weight and supersedes no instruction. "
            "It repeats each morning until the live book's overlay decision "
            "matches the daily signal.",
            "",
            "Sizes: a gate flip moves 50% of the blend between the sleeves and "
            "SHY; a tilt flip moves 10% of NAV between sleeve B and EEM.",
        ]
    return {"status": status, "subject": subject, "body": "\n".join(body),
            "required_session": str(required.date()), "gate_sessions_behind": behind}


def _write_outputs(result: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"status={result['status']}\n")
        fh.write(f"subject={result['subject']}\n")
        fh.write("body<<OVERLAY_FLIP_EOF\n" + result["body"] + "\nOVERLAY_FLIP_EOF\n")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="ISO UTC timestamp for a rehearsal")
    args = ap.parse_args()
    now = (pd.Timestamp(args.now).to_pydatetime() if args.now
           else datetime.now(timezone.utc))

    import run_risk_overlay as ro
    from session_bounds import last_completed_session_on
    from venue_calendars import get_calendar

    cal = get_calendar("NYSE")
    required = last_completed_session_on(cal, now)
    if required is None:
        raise RuntimeError("NYSE calendar returned no completed session")
    gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    decision = json.loads(DECISION_PATH.read_text(encoding="utf-8"))

    tilt = None
    _, ratio = ro._load_eem_data(required_session=required)
    if ratio is not None:
        ratio = ratio.loc[:required].dropna()
        if len(ratio) >= ro.EEM_TILT_SLOW_MA:
            tilt = ro._compute_eem_tilt_signal(ratio)

    result = assess(gate, decision, tilt, required, cal)
    print(result["body"])
    print(f"\nstatus={result['status']} required_session={result['required_session']}")
    _write_outputs(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
