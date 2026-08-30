"""The vendor-retraction tripwire — and the cases it must stay quiet on.

On 2026-08-28/29 yfinance served Friday's closes on all seven probed lines
AFTER their sessions had closed, then withdrew every one of them overnight,
replacing each with a row carrying the right date and a NaN close. Every
downstream guard refused the short data correctly and none of them said why,
so two full local refresh_all.py runs were spent rebuilding against bars the
vendor had taken back.

The tripwire reads the probe log the repo was already keeping. These tests pin
what it must catch, and — the harder half — what it must not, because the
probe fires at 00/06/12/18 UTC and half of those land mid-session, when a bar
dated today is legitimately still being shaped.

Dates here are ISO strings parsed with date.fromisoformat; Python months are
1-indexed (January = 1). The month- and year-boundary cases are the two
edge cases CLAUDE.md requires of any date logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_vendor_probe as cvp  # noqa: E402


def _row(stamp: str, entries):
    """entries: (ticker, last_bar, last_completed_session) triples."""
    return {
        "probed_at_utc": stamp,
        "rows": [{"ticker": t, "venue": "NYSE", "last_bar": lb,
                  "last_completed_session": lcs} for t, lb, lcs in entries],
    }


def test_withdrawn_settled_bar_is_detected():
    """The SPY shape: served after the close, gone by morning."""
    history = [
        _row("2026-08-28T21:41:31+00:00", [("SPY", "2026-08-28", "2026-08-28")]),
        _row("2026-08-29T05:25:20+00:00", [("SPY", "2026-08-27", "2026-08-28")]),
    ]
    out = cvp.detect_retractions(history)
    assert len(out) == 1
    assert out[0]["ticker"] == "SPY"
    assert out[0]["was"] == "2026-08-28"
    assert out[0]["now"] == "2026-08-27"
    assert out[0]["was_seen_at"] == "2026-08-28T21:41:31+00:00"


def test_in_progress_bar_disappearing_is_not_a_retraction():
    """THE FALSE POSITIVE THAT MATTERS.

    The 12:00 and 18:00 UTC probes fire while Xetra and the NYSE are open. A
    bar dated today, recorded while today is still trading, is not a promise
    the vendor has made — reshaping or removing it is correct behaviour. Judged
    without the completed-session filter this fired on all five Xetra lines
    from a single Friday-morning probe, which would have made the tripwire
    noise from its first day.
    """
    history = [
        # 08:48 UTC on the 28th: Xetra is open, so the 28th is NOT complete.
        _row("2026-08-28T08:48:29+00:00", [("EXV1.DE", "2026-08-28", "2026-08-27")]),
        _row("2026-08-28T12:00:00+00:00", [("EXV1.DE", "2026-08-27", "2026-08-27")]),
    ]
    assert cvp.detect_retractions(history) == []


def test_forward_only_log_is_quiet():
    history = [
        _row("2026-08-26T18:00:00+00:00", [("SPY", "2026-08-25", "2026-08-25")]),
        _row("2026-08-27T18:00:00+00:00", [("SPY", "2026-08-26", "2026-08-26")]),
        _row("2026-08-28T18:00:00+00:00", [("SPY", "2026-08-27", "2026-08-27")]),
    ]
    assert cvp.detect_retractions(history) == []


def test_line_going_completely_blank_counts():
    """A total withdrawal is the shape a whole-venue outage takes."""
    history = [
        _row("2026-08-28T21:41:31+00:00", [("XLF", "2026-08-28", "2026-08-28")]),
        _row("2026-08-29T05:25:20+00:00", [("XLF", None, "2026-08-28")]),
    ]
    out = cvp.detect_retractions(history)
    assert len(out) == 1 and out[0]["now"] is None
    assert out[0]["was"] == "2026-08-28"


def test_a_partial_row_between_does_not_mask_a_retraction():
    """High-water mark, not previous-row comparison.

    A partial result is a legitimate probe outcome, so a line can be absent
    from a row without that being a withdrawal. Comparing only against the
    immediately preceding row would let a retraction hide behind one.
    """
    history = [
        _row("2026-08-28T21:41:31+00:00", [("SPY", "2026-08-28", "2026-08-28")]),
        _row("2026-08-29T00:00:00+00:00", [("SPY", None, "2026-08-28")]),
        _row("2026-08-29T05:25:20+00:00", [("SPY", "2026-08-27", "2026-08-28")]),
    ]
    out = cvp.detect_retractions(history)
    assert len(out) == 1 and out[0]["was"] == "2026-08-28"


def test_month_boundary_retraction():
    """Edge case 1: the withdrawn bar is the last session of a month."""
    history = [
        _row("2026-07-31T21:41:00+00:00", [("SPY", "2026-07-31", "2026-07-31")]),
        _row("2026-08-01T05:25:00+00:00", [("SPY", "2026-07-30", "2026-07-31")]),
    ]
    out = cvp.detect_retractions(history)
    assert len(out) == 1
    assert out[0]["was"] == "2026-07-31" and out[0]["now"] == "2026-07-30"


def test_year_boundary_retraction():
    """Edge case 2: the withdrawn bar is the last session of a year."""
    history = [
        _row("2025-12-31T21:41:00+00:00", [("SPY", "2025-12-31", "2025-12-31")]),
        _row("2026-01-01T05:25:00+00:00", [("SPY", "2025-12-30", "2025-12-31")]),
    ]
    out = cvp.detect_retractions(history)
    assert len(out) == 1
    assert out[0]["was"] == "2025-12-31" and out[0]["now"] == "2025-12-30"


def test_single_row_history_cannot_retract():
    history = [_row("2026-08-29T05:25:20+00:00",
                    [("SPY", "2026-08-27", "2026-08-28")])]
    assert cvp.detect_retractions(history) == []


def test_lookback_window_forgets_an_old_high_water_mark():
    """A bar absent for long enough stops re-alerting.

    Without a bounded window the tripwire would keep firing on the same
    withdrawal every six hours indefinitely, which is how an alert stops being
    read.
    """
    history = [_row("2026-08-01T21:41:00+00:00",
                    [("SPY", "2026-08-01", "2026-08-01")])]
    history += [_row(f"2026-08-{d:02d}T18:00:00+00:00",
                     [("SPY", "2026-07-31", "2026-08-01")])
                for d in range(2, 2 + cvp.RETRACTION_LOOKBACK_ROWS + 2)]
    assert cvp.detect_retractions(history) == []
