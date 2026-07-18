"""Regression wiring test for the 2026-07-18 exclusive-end fencepost fix.

The strategy engines fetch with yf.download(end=END_DATE); yfinance's
``end`` is EXCLUSIVE, so END_DATE must be strictly LATER than today or a
run on a trading day silently drops that day's completed close — the
2026-07-17 weekly factsheet shipped without the Friday rebalance exactly
this way. Guards the module constants so the bug cannot be reintroduced
by reverting to ``datetime.now().strftime(...)``.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

from datetime import datetime, timezone


def test_engine_fetch_windows_end_strictly_after_today():
    from scripts.run_asset_class_rotation import END_DATE as b_end
    from scripts.run_thematic_rotation import END_DATE as c_end

    today = datetime.now(timezone.utc).date().isoformat()
    # ISO dates compare lexically in chronological order.
    assert b_end > today, (
        f"Strategy B fetch window ends {b_end} — an exclusive yfinance "
        f"end must be strictly after today ({today})"
    )
    assert c_end > today, (
        f"Strategy C fetch window ends {c_end} — an exclusive yfinance "
        f"end must be strictly after today ({today})"
    )
