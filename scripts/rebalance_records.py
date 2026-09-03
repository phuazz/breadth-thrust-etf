"""The last rebalance an engine actually ran, whether or not it traded.

WHY THIS EXISTS — 2026-09-03, found from the dashboard.

Every engine publishes ``trade_history``, and every ``build_trade_history``
appends an entry only when the weight vector CHANGES. That is the right record
of trades. It is the wrong record of rebalances, and four dashboard blocks, the
hero line and the factsheet vintage line were reading it as one: each labelled
``trade_history[-1].date`` "the rebalance" and printed that entry's signals as
"signal at rebalance".

Sleeves A, B and D weight by signal or breadth, so their weights drift every
week and they log every week — the two records coincide by accident. Sleeve C
is equal-weighted (1/K), so a week that holds the same names writes nothing. On
2026-09-03 the dashboard read "as of the 2026-08-24 rebalance" for a sleeve
whose 2026-08-31 rebalance had run and held; the same thing had happened in the
week of 2026-08-17. The holdings shown were right; the date and the signals
beside them were a week stale, and nothing said so.

This module gives each engine a second record: the LAST rebalance in the
weight panel, on the same conventions as the trade record (decision session is
the index entry before the rebalance date; the value recorded is the one that
decided the weight). The two records agree on a week that traded and differ by
exactly the held weeks otherwise, which is what a surface needs in order to say
"unchanged since the <date> trade" instead of misdating the rebalance.

Python datetime months are 1-indexed (January = 1). Dates are read from the
panel index, never typed.
"""

from __future__ import annotations

import pandas as pd


def latest_rebalance_record(
    weight_panel: pd.DataFrame,
    value_panel: pd.DataFrame,
    rebalance_dates,
    value_key: str,
    eligible_start: pd.Timestamp | None = None,
) -> dict | None:
    """The last rebalance date's book, on trade-record conventions.

    ``weight_panel`` is the engine's daily weight frame (rebalance rows carried
    forward), ``rebalance_dates`` the dates the engine rebalanced on, and
    ``value_panel`` the signal or breadth panel whose value on the DECISION
    session (the index entry before the rebalance date) is recorded under
    ``value_key`` — ``signal_pct`` for B and C, ``breadth_pct`` for A and D.

    Returns None when no rebalance date at or after ``eligible_start`` is in
    the panel. A rebalance that put the whole sleeve in cash returns an empty
    ``holdings`` list rather than None: "held nothing" is a rebalance outcome,
    "never rebalanced" is not.
    """
    if weight_panel is None or len(weight_panel) == 0:
        return None
    index = pd.DatetimeIndex(weight_panel.index)
    have = set(index)
    dates = [pd.Timestamp(d) for d in (rebalance_dates if rebalance_dates is not None else [])]
    dates = [d for d in dates if d in have]
    if eligible_start is not None:
        dates = [d for d in dates if d >= pd.Timestamp(eligible_start)]
    if not dates:
        return None
    rd = max(dates)
    pos = index.get_loc(rd)
    decision = index[pos - 1] if pos > 0 else rd

    values = (value_panel.reindex(index, method="ffill")
              if value_panel is not None else None)
    row = weight_panel.loc[rd]
    non_zero = row[row > 1e-6].sort_values(ascending=False)
    holdings = []
    for etf, w in non_zero.items():
        v = None
        if values is not None and etf in values.columns:
            raw = values.loc[decision, etf]
            v = round(float(raw) * 100, 1) if raw == raw else None
        holdings.append({"etf": str(etf), "weight": round(float(w), 4),
                         value_key: v})
    return {
        "date": rd.strftime("%Y-%m-%d"),
        "decision_date": pd.Timestamp(decision).strftime("%Y-%m-%d"),
        "holdings": holdings,
    }
