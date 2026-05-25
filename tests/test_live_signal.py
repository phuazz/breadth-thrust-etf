"""Live-signal exit-day semantics.

Exits execute AT the close of exit_date. A notification generated after
that close should treat the trade as closed and report next-session
allocation as OUT/base. Previously the loop used `entry <= latest <= exit_`
which kept the alert in IN-TRADE state on exit_date and then flipped
without warning the next session.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from live_signal import _current_trade_at  # noqa: E402


def test_current_trade_is_closed_on_exit_date_after_close():
    trades = [{"entry_date": "2024-01-02", "exit_date": "2024-01-05"}]

    # Mid-trade: still open.
    assert _current_trade_at(trades, pd.Timestamp("2024-01-04")) == trades[0]
    # On exit_date itself: closed at the close, so notification reports OUT.
    assert _current_trade_at(trades, pd.Timestamp("2024-01-05")) is None
