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

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_vendor_probe as cvp  # noqa: E402


def _row(stamp: str, entries):
    """entries: (ticker, last_bar, last_completed_session[, venue]) tuples;
    the venue defaults to NYSE."""
    rows = []
    for e in entries:
        t, lb, lcs = e[:3]
        venue = e[3] if len(e) > 3 else "NYSE"
        rows.append({"ticker": t, "venue": venue, "last_bar": lb,
                     "last_completed_session": lcs})
    return {"probed_at_utc": stamp, "rows": rows}


def _xetr(stamp: str, ticker: str, last_bar, lcs: str):
    return _row(stamp, [(ticker, last_bar, lcs, "XETR")])


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


# ---------------------------------------------------------------------------
# The routine overnight cycle (2026-09-05): recorded, printed, not emailed.
#
# In its first week the tripwire emailed on twelve probe rows, nearly all of
# them the vendor's ordinary European cycle — the close withdrawn overnight
# and restored within two calendar days. The rule below carves that cycle out
# EXACTLY, and the 2026-08-28/30 outage weekend still alerts on both of its
# shapes. Dates are ISO strings; the calendar arithmetic is
# pandas_market_calendars' and timedelta's, never a hand count.
# ---------------------------------------------------------------------------
def test_previous_session_uses_the_venue_calendar():
    """Friday before a Monday, Thursday before a Friday — the calendar, not
    a weekday count; and an unknown venue is None, never a guess."""
    assert cvp.previous_session("XETR", date(2026, 8, 31)) == date(2026, 8, 28)
    assert cvp.previous_session("XETR", date(2026, 9, 4)) == date(2026, 9, 3)
    assert cvp.previous_session("NOT-A-VENUE", date(2026, 9, 4)) is None


def test_routine_overnight_xetr_withdrawal_is_flagged_routine():
    """The 2026-09-05 02:43 UTC shape that emailed: Friday's bar served at
    20:12 UTC, gone by the Saturday 02:43 probe, Thursday's in its place."""
    history = [
        _xetr("2026-09-04T20:12:45+00:00", "EXV1.DE", "2026-09-04", "2026-09-04"),
        _xetr("2026-09-05T02:43:11+00:00", "EXV1.DE", "2026-09-03", "2026-09-04"),
    ]
    out = cvp.detect_retractions(history)
    assert len(out) == 1
    assert out[0]["routine"] is True
    assert out[0]["was"] == "2026-09-04" and out[0]["now"] == "2026-09-03"


def test_saturday_evening_is_still_inside_the_window():
    """2026-08-22 18:23 UTC: Friday's bar absent all Saturday, restored by
    the Sunday 01:04 probe. Routine right up to 00:00 UTC on the Sunday."""
    history = [
        _xetr("2026-08-21T18:30:00+00:00", "EXV1.DE", "2026-08-21", "2026-08-21"),
        _xetr("2026-08-22T18:23:00+00:00", "EXV1.DE", "2026-08-20", "2026-08-21"),
    ]
    assert cvp.detect_retractions(history)[0]["routine"] is True


def test_bar_still_absent_on_sunday_is_anomalous():
    """2026-08-30 03:21 UTC: Friday 08-28's bar not back on the Sunday — the
    genuine outage weekend, and the window is set so it still alerts."""
    history = [
        _xetr("2026-08-28T21:41:00+00:00", "EXV1.DE", "2026-08-28", "2026-08-28"),
        _xetr("2026-08-30T03:21:04+00:00", "EXV1.DE", "2026-08-27", "2026-08-28"),
    ]
    assert cvp.detect_retractions(history)[0]["routine"] is False


def test_two_sessions_withdrawn_is_anomalous():
    """2026-08-29 05:25 UTC: last_bar 08-26 against a withdrawn 08-28 — two
    sessions gone is not the one-session cycle."""
    history = [
        _xetr("2026-08-28T21:41:00+00:00", "EXV1.DE", "2026-08-28", "2026-08-28"),
        _xetr("2026-08-29T05:25:20+00:00", "EXV1.DE", "2026-08-26", "2026-08-28"),
    ]
    assert cvp.detect_retractions(history)[0]["routine"] is False


def test_nyse_line_has_no_routine_window():
    """The cycle is measured on the European lines only; SPY going back one
    session overnight is still an alert."""
    history = [
        _row("2026-08-28T21:41:31+00:00", [("SPY", "2026-08-28", "2026-08-28")]),
        _row("2026-08-29T05:25:20+00:00", [("SPY", "2026-08-27", "2026-08-28")]),
    ]
    assert cvp.detect_retractions(history)[0]["routine"] is False


def test_total_withdrawal_is_never_routine():
    history = [
        _xetr("2026-09-04T20:12:45+00:00", "EXV1.DE", "2026-09-04", "2026-09-04"),
        _xetr("2026-09-05T02:43:11+00:00", "EXV1.DE", None, "2026-09-04"),
    ]
    out = cvp.detect_retractions(history)
    assert out[0]["now"] is None and out[0]["routine"] is False


def test_routine_window_across_a_month_boundary():
    """Edge case 1: Monday 2026-08-31 withdrawn. The window ends 00:00 UTC
    on 2026-09-02, so the Tuesday 02:40 probe is routine and a Wednesday
    00:30 probe is not."""
    served = _xetr("2026-08-31T20:00:00+00:00", "SAP.DE", "2026-08-31", "2026-08-31")
    inside = _xetr("2026-09-01T02:40:00+00:00", "SAP.DE", "2026-08-28", "2026-08-31")
    outside = _xetr("2026-09-02T00:30:00+00:00", "SAP.DE", "2026-08-28", "2026-08-31")
    assert cvp.detect_retractions([served, inside])[0]["routine"] is True
    assert cvp.detect_retractions([served, outside])[0]["routine"] is False
    assert cvp.routine_window_end(date(2026, 8, 31)) == datetime(
        2026, 9, 2, tzinfo=timezone.utc)


def test_routine_window_across_a_year_boundary():
    """Edge case 2: Wednesday 2026-12-30 withdrawn (Xetra is shut on the
    31st, so the session before it is the 29th). The window ends 00:00 UTC
    on 2027-01-01."""
    served = _xetr("2026-12-30T20:00:00+00:00", "SAP.DE", "2026-12-30", "2026-12-30")
    inside = _xetr("2026-12-31T06:00:00+00:00", "SAP.DE", "2026-12-29", "2026-12-30")
    outside = _xetr("2027-01-01T01:00:00+00:00", "SAP.DE", "2026-12-29", "2026-12-30")
    assert cvp.detect_retractions([served, inside])[0]["routine"] is True
    assert cvp.detect_retractions([served, outside])[0]["routine"] is False
    assert cvp.routine_window_end(date(2026, 12, 30)) == datetime(
        2027, 1, 1, tzinfo=timezone.utc)


def _write_log(path: Path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")


def _pin_clock(monkeypatch, now: datetime) -> None:
    """main() reads the wall clock through evaluate(); pin it so the fixture
    rows are 'fresh' on any day the suite runs."""
    real = cvp.evaluate
    monkeypatch.setattr(
        cvp, "evaluate",
        lambda p, max_age_minutes=90: real(p, now_utc=now,
                                           max_age_minutes=max_age_minutes))


def test_routine_only_probe_is_endorsed_and_does_not_email(tmp_path, monkeypatch, capsys):
    """The Saturday-morning shape end to end: the row is committed, the
    withdrawal is printed, and the workflow's email condition stays false."""
    log = tmp_path / "log.jsonl"
    gh_out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    _write_log(log, [
        _row("2026-09-04T20:12:45+00:00",
             [("SPY", "2026-09-04", "2026-09-04"),
              ("EXV1.DE", "2026-09-04", "2026-09-04", "XETR")]),
        _row("2026-09-05T02:43:11+00:00",
             [("SPY", "2026-09-04", "2026-09-04"),
              ("EXV1.DE", "2026-09-03", "2026-09-04", "XETR")]),
    ])
    _pin_clock(monkeypatch, datetime(2026, 9, 5, 2, 50, tzinfo=timezone.utc))
    assert cvp.main(["--log", str(log), "--fail-on-retraction"]) == 0
    printed = capsys.readouterr().out
    assert "Routine overnight withdrawal" in printed
    assert "RETRACTION TRIPWIRE" not in printed
    outputs = gh_out.read_text(encoding="utf-8")
    assert "retracted=false" in outputs
    assert "routine_count=1" in outputs
    assert "retracted_count=0" in outputs


def test_anomalous_retraction_still_emails_and_can_block(tmp_path, monkeypatch, capsys):
    """SPY going backwards beside the routine XETR withdrawal: the alert
    names SPY, lists the routine line separately, and --fail-on-retraction
    blocks on the anomalous one alone."""
    log = tmp_path / "log.jsonl"
    gh_out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    _write_log(log, [
        _row("2026-09-04T20:12:45+00:00",
             [("SPY", "2026-09-04", "2026-09-04"),
              ("EXV1.DE", "2026-09-04", "2026-09-04", "XETR")]),
        _row("2026-09-05T02:43:11+00:00",
             [("SPY", "2026-09-03", "2026-09-04"),
              ("EXV1.DE", "2026-09-03", "2026-09-04", "XETR")]),
    ])
    _pin_clock(monkeypatch, datetime(2026, 9, 5, 2, 50, tzinfo=timezone.utc))
    assert cvp.main(["--log", str(log)]) == 0            # endorsed: a true observation
    assert cvp.main(["--log", str(log), "--fail-on-retraction"]) == 1
    printed = capsys.readouterr().out
    assert "RETRACTION TRIPWIRE" in printed and "SPY" in printed
    outputs = gh_out.read_text(encoding="utf-8")
    assert "retracted=true" in outputs
    assert "retracted_summary=SPY 2026-09-04->2026-09-03" in outputs
    assert "routine_count=1" in outputs
    # The routine line is disclosed in the email body, not hidden.
    assert "Routine overnight withdrawal on the same probe" in outputs
    assert "EXV1.DE 2026-09-04->2026-09-03" in outputs
