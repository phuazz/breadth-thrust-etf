"""WS18 step 1 — reconcile rebalance DATES before reading any performance number.

This is a GATE, and it runs first by pre-registration
(reviews/2026-08-22_prereg_ws18_monday-cadence.md §5.3, §8).

WHY IT GATES. NYSE Mondays are closed 39 of 406 in the sample (9.6%) against
Friday's 15 of 407 (3.7%) — 2.6x as many holiday rebalances for the 70% of NAV
that trades there. WS10 adopted `holiday_aware` precisely because a holiday
rebalance was silently skipping a whole week. Under W-MON that path is
exercised 2.6x more, so a cadence comparison could differ simply because one
leg TRADED FEWER WEEKS. That is not a like-for-like comparison, and a Sharpe
read before checking it would be measuring the calendar, not the cadence.

WHAT IT CHECKS, per sleeve and venue, for W-FRI against W-MON:
  1. how many rebalances each leg resolves;
  2. how many scheduled days were market holidays, and what the mode did;
  3. that EVERY rebalance still has a strictly earlier decision session —
     the look-ahead invariant, re-checked because the roll direction changes.

Exit 0 = reconciled, proceed to the performance leg. Exit 1 = discrepancy the
holiday table does not explain; HALT per §8.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rebalance_calendar import DEFAULT_MODE, engine_rebalance_dates  # noqa: E402

# Sleeve -> the venue it TRADES on. A/B/C are US-listed; D is Xetra.
SLEEVES = {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"}
START, END = "2018-11-08", "2026-08-21"


def _sessions(cal_name: str) -> pd.DatetimeIndex:
    sched = mcal.get_calendar(cal_name).schedule(start_date=START, end_date=END)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


def _scheduled_days(freq: str) -> pd.DatetimeIndex:
    return pd.date_range(START, END, freq=freq)


def reconcile() -> dict:
    print(f"WS18 date reconciliation — calendar mode {DEFAULT_MODE!r}, "
          f"{START} to {END}\n")
    out, ok = {}, True

    for sleeve, venue in SLEEVES.items():
        idx = _sessions(venue)
        eligible = idx[0]
        row = {}
        for freq, label in (("W-FRI", "Fri"), ("W-MON", "Mon")):
            rd = engine_rebalance_dates(idx, eligible, freq, venue)
            sched = _scheduled_days(freq)
            closed = [d for d in sched if d.normalize() not in set(idx)]
            # Look-ahead invariant: the decision session is the index entry
            # BEFORE the fill, so every rebalance needs a predecessor.
            bad = [d for d in rd if idx.get_loc(d) - 1 < 0]
            row[label] = {
                "rebalances": len(rd),
                "scheduled": len(sched),
                "holidays_on_scheduled_day": len(closed),
                "first": str(pd.Timestamp(rd[0]).date()) if len(rd) else None,
                "last": str(pd.Timestamp(rd[-1]).date()) if len(rd) else None,
                "no_prior_session": len(bad),
            }
            if bad:
                ok = False
        row["delta_rebalances"] = row["Mon"]["rebalances"] - row["Fri"]["rebalances"]
        out[f"{sleeve} ({venue})"] = row

    print(f"{'sleeve':12s} {'leg':4s} {'sched':>6s} {'holiday':>8s} "
          f"{'rebals':>7s} {'first':>11s} {'last':>11s}")
    for name, row in out.items():
        for leg in ("Fri", "Mon"):
            r = row[leg]
            print(f"{name:12s} {leg:4s} {r['scheduled']:6d} "
                  f"{r['holidays_on_scheduled_day']:8d} {r['rebalances']:7d} "
                  f"{str(r['first']):>11s} {str(r['last']):>11s}")
        print(f"{'':12s}      Monday minus Friday rebalances: "
              f"{row['delta_rebalances']:+d}")
    print()

    # THE GATE. A leg that resolves materially fewer rebalances is trading a
    # different number of weeks, and any Sharpe difference then partly measures
    # that rather than the cadence.
    for name, row in out.items():
        f, m = row["Fri"], row["Mon"]
        explained = f["holidays_on_scheduled_day"] - m["holidays_on_scheduled_day"]
        actual = row["delta_rebalances"]
        drop = m["scheduled"] - m["rebalances"]
        fdrop = f["scheduled"] - f["rebalances"]
        print(f"  {name}: Friday drops {fdrop} of {f['scheduled']} scheduled, "
              f"Monday drops {drop} of {m['scheduled']}")
        if m["no_prior_session"] or f["no_prior_session"]:
            print("     FAIL look-ahead invariant: a rebalance has no prior session")
            ok = False
        # Under holiday_aware a closed scheduled day is SKIPPED, so the drop
        # should equal the holiday count. Anything else means the mode did
        # something the holiday table cannot account for.
        for leg, r, d in (("Fri", f, fdrop), ("Mon", m, drop)):
            if d != r["holidays_on_scheduled_day"]:
                print(f"     NOTE {leg}: {d} dropped vs "
                      f"{r['holidays_on_scheduled_day']} holidays — "
                      f"mode did more than skip holidays")
    print()
    verdict = "RECONCILED — proceed to the performance leg" if ok else \
              "HALT — a discrepancy the holiday table does not explain"
    print(f"VERDICT: {verdict}")
    return {"ok": ok, "mode": DEFAULT_MODE, "sleeves": out}


if __name__ == "__main__":
    raise SystemExit(0 if reconcile()["ok"] else 1)
