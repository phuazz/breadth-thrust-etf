"""Which bars may an engine rank on, and which session is it ranking for.

WHY THIS EXISTS — 2026-08-14, caught in prep, not in production.

Strategy D emitted a rebalance dated Friday 14 August that switched EXH3 out
for EXV3. It was wrong, and it was wrong for two compounding reasons that no
guard in the repo could see.

1. A PARTIAL BAR. Xetra closes 15:30 UTC. At 13:15 UTC, with the session still
   two hours from finishing, yfinance served a bar stamped 2026-08-14 for the
   .DE lines. It was a live quote. The engine took it as a close.

2. A HOLE ON THE DECISION SESSION. Thursday 13 August is a Xetra session and
   the vendor does not serve it for those lines — verified against the
   calendar, and still absent when today's bar is excluded, so it is a genuine
   gap and not displacement. Rare: 2 sessions missing out of 516 since August
   2024. It happened on the one that mattered.

   Because build_trade_history takes ``decision_date = full_idx[i - 1]``, the
   hole silently moved the decision from Thursday to WEDNESDAY. Nothing said
   so. On Wednesday EXV3 breadth (73.6) beat EXH3 (71.6); by Thursday that had
   reversed, EXH3 73.0 against EXV3 71.7. A 1.3pp call decided by the wrong
   session.

THE DISTINCTION THAT MATTERS. The SIGNAL was never the problem: constituent
prices for EXV1/EXH3/EXV3 all carry 13 August. Only the ETF EXECUTION line
lacked it. The engine derives its decision date from the price index rather
than the signal index, so a gap in the line it trades on quietly redated the
decision it was ranking. Those are different series answering different
questions and the engine conflated them — the same shape of error as bounding
breadth by the roster's last Friday, and as an anchor guard that asks about a
panel rather than a roster.

WHAT THIS DOES NOT DO. It does not fail on historical gaps. 2025-10-24 has
been missing for ten months and demanding zero would fail every run forever,
which is how a guard gets switched off. It reports the tail honestly and
refuses only what is genuinely unusable: a bar from a session that has not
closed.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd


class PartialBarError(RuntimeError):
    """A frame carries a bar from a session that has not closed."""


def last_completed_session_on(cal, now_utc: datetime,
                              horizon_days: int = 14) -> pd.Timestamp | None:
    """Most recent session on `cal` whose CLOSE has already passed.

    Deliberately not "today", and deliberately not "the last row the vendor
    returned". Both admit a partial bar.

    Venue-aware because the answer differs by venue and the difference is not
    cosmetic: on a US-holiday Friday, Xetra has closed for the day and NYSE
    never opened, so a single NYSE-derived cap would either truncate the
    European funds by a session or admit a partial US one.

    Returns None when the calendar yields nothing in the horizon, which every
    caller treats as "keep the previous bound" rather than as an error.
    """
    now = pd.Timestamp(now_utc)
    if now.tz is None:
        now = now.tz_localize("UTC")
    end = now.tz_convert("UTC").normalize() + pd.Timedelta(days=1)
    sched = cal.schedule(start_date=end - pd.Timedelta(days=horizon_days),
                         end_date=end)
    if sched.empty or "market_close" not in sched.columns:
        return None
    closed = sched[sched["market_close"] <= now]
    if closed.empty:
        return None
    last = pd.Timestamp(closed.index[-1])
    if last.tz is not None:
        last = last.tz_convert("UTC").tz_localize(None)
    return last.normalize()


def trim_to_completed(df: pd.DataFrame, cal, now_utc: datetime,
                      label: str = "panel") -> tuple[pd.DataFrame, list]:
    """Drop rows stamped after the last completed session on `cal`.

    Returns (trimmed, dropped_dates). Dropping rather than raising is
    deliberate: an intraday quote is a normal thing for a vendor to serve
    while a market is open, and a refresh run during market hours must still
    produce a usable panel from the sessions that HAVE closed. What must never
    happen is that quote reaching a ranking decision.

    Historically a no-op — every bar in a finished session is complete — so
    this cannot move a backtest. It only ever removes a tail.
    """
    if df is None or len(df) == 0:
        return df, []
    cap = last_completed_session_on(cal, now_utc)
    if cap is None:
        return df, []
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    keep = idx.normalize() <= cap
    dropped = [pd.Timestamp(d).date() for d in idx[~keep]]
    if dropped:
        print(f"  [session guard] {label}: dropped {len(dropped)} bar(s) from "
              f"a session that has not closed: {dropped} "
              f"(last completed session {cap.date()})", flush=True)
    return df.loc[keep], dropped


def decision_session_report(df: pd.DataFrame, cal, now_utc: datetime,
                            label: str = "panel",
                            lookback_sessions: int = 10) -> dict:
    """Does this frame actually carry the session a decision would rank on?

    The engine ranks on the session before the rebalance. If the vendor holed
    that session, the engine does not fail — it silently ranks on whatever
    came before, which is what redated the 14 August Strategy D decision to
    Wednesday. This names the gap instead.
    """
    cap = last_completed_session_on(cal, now_utc)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    idx = idx.normalize()
    last_bar = pd.Timestamp(idx.max()) if len(idx) else None

    missing: list = []
    if cap is not None:
        sched = cal.schedule(
            start_date=cap - pd.Timedelta(days=lookback_sessions * 3),
            end_date=cap)
        sessions = [pd.Timestamp(d).normalize()
                    for d in sched.index][-lookback_sessions:]
        have = set(idx)
        missing = [s.date() for s in sessions if s not in have]

    ok = cap is not None and last_bar is not None and last_bar >= cap
    rep = {
        "label": label,
        "expected_decision_session": cap.date() if cap is not None else None,
        "last_bar": last_bar.date() if last_bar is not None else None,
        "reaches_decision_session": ok,
        "missing_recent_sessions": missing,
    }
    if not ok:
        print(f"  [session guard] {label}: last bar {rep['last_bar']} does NOT "
              f"reach the decision session {rep['expected_decision_session']}. "
              f"A ranking taken now uses an EARLIER session than intended.",
              flush=True)
    elif missing:
        print(f"  [session guard] {label}: reaches {rep['last_bar']}, but the "
              f"vendor omitted {missing} from the recent window.", flush=True)
    return rep
