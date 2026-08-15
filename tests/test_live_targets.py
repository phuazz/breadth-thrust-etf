"""Live targets rank on the signal, or they refuse — never a session early.

Python months are 1-indexed (January = 1). Every literal below is 1-indexed.

The engines emit a rebalance only where an execution BAR exists, which is right
for a backtest and useless on a Friday morning. This step ranks each sleeve on
its own signal at the last completed session on its own venue, and reports HOLD
rather than ranking on whatever came before — because ranking a session early is
how EXH3/EXV3 flipped on a 1.3pp margin on 2026-08-14.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import live_targets as lt  # noqa: E402
import probe_vendor_availability as probe  # noqa: E402

NYSE = mcal.get_calendar("NYSE")
XETR = mcal.get_calendar("XETR")


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _signal(dates, cols=("X", "Y", "Z")):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame(
        {c: [0.9 - 0.1 * i - 0.01 * j for j in range(len(idx))]
         for i, c in enumerate(cols)}, index=idx)


def _top2(row):
    top = row.sort_values(ascending=False).head(2)
    return top / top.sum()


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------

def test_holds_when_the_signal_is_a_session_short():
    """Sat 15 Aug 02:41 UTC: Xetra's last completed session is Fri 14 Aug, and
    the .DE complex had only published through Thu 13 Aug. Measured, not
    hypothetical."""
    sig = _signal(["2026-08-11", "2026-08-12", "2026-08-13"])
    r = lt._rank(sig, _top2, "XETR", _utc(2026, 8, 15, 2, 41), "D")
    assert r["status"] == "HOLD"
    assert r["decision_session"] == "2026-08-13"
    assert r["last_completed_session"] == "2026-08-14"
    assert "earlier session" in r["reason"]


def test_ready_when_the_signal_reaches_the_last_close():
    sig = _signal(["2026-08-12", "2026-08-13", "2026-08-14"])
    r = lt._rank(sig, _top2, "NYSE", _utc(2026, 8, 15, 2, 41), "B")
    assert r["status"] == "READY"
    assert r["decision_session"] == "2026-08-14"
    assert r["reason"] is None
    assert abs(sum(r["weights"].values()) - 1.0) < 1e-9


def test_a_signal_ahead_of_the_last_close_is_not_used():
    """A bar for a session that has not closed must not decide anything, even
    if the vendor serves one — that is the 2026-08-14 partial bar."""
    sig = _signal(["2026-08-13", "2026-08-14", "2026-08-17"])
    r = lt._rank(sig, _top2, "NYSE", _utc(2026, 8, 14, 13, 15), "A")
    assert r["decision_session"] == "2026-08-13", \
        "must rank on the last COMPLETED session, not the freshest row"
    assert r["status"] == "READY"


def test_status_is_per_venue_not_global():
    """US Independence Day, Fri 3 Jul 2026 16:00 UTC: Xetra closed, NYSE never
    opened. A signal through 2 July is short for Xetra and current for NYSE."""
    sig = _signal(["2026-07-01", "2026-07-02"])
    assert lt._rank(sig, _top2, "XETR", _utc(2026, 7, 3, 16, 0), "D")["status"] == "HOLD"
    assert lt._rank(sig, _top2, "NYSE", _utc(2026, 7, 3, 16, 0), "A")["status"] == "READY"


def test_empty_signal_holds_rather_than_raising():
    sig = _signal([]) if False else pd.DataFrame(columns=["X"], index=pd.DatetimeIndex([]))
    r = lt._rank(sig, _top2, "NYSE", _utc(2026, 8, 15, 2, 41), "A")
    assert r["status"] == "HOLD" and r["weights"] == {}


def test_breadth_panel_is_not_collapsed_onto_the_execution_calendar():
    """The whole point of the module. _build_panels_for aligns breadth onto
    closes.index, which deletes a signal the vendor did publish whenever the
    ETF wrapper's own bar is missing; this must read the constituent series
    directly instead."""
    import ast
    import inspect
    fn = ast.parse(inspect.getsource(lt._breadth_panel)).body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]          # the docstring NAMES it, to explain why
    body = ast.unparse(fn)
    assert "load_constituent_prices" in body
    assert "_build_panels_for" not in body


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

def test_sessions_behind_counts_sessions_not_days():
    """Fri 3 Jul 2026 is a US holiday, so 2 -> 6 July spans one session."""
    assert probe._sessions_behind(NYSE, "2026-07-02", "2026-07-06") == 1
    assert probe._sessions_behind(NYSE, "2026-08-13", "2026-08-14") == 1
    assert probe._sessions_behind(NYSE, "2026-08-14", "2026-08-14") == 0
    assert probe._sessions_behind(NYSE, None, "2026-08-14") is None


def test_probe_covers_both_sides_of_the_europe_question():
    """The ETF line and the constituents are different series with different
    lags, and conflating them is what made the 2026-08-14 diagnosis wrong the
    first time. Both must be sampled."""
    roles = {r for _, _, r in probe.PROBES}
    assert "Europe ETF line" in roles
    assert "Europe constituent" in roles
    assert "US ETF proxy" in roles


def test_probe_is_not_a_guard():
    """It must never fail a pipeline. A probe that can break a refresh gets
    switched off before it has collected anything."""
    import inspect
    src = inspect.getsource(probe)
    assert "raise SystemExit(1)" not in src
    assert "sys.exit(1)" not in src


# ---------------------------------------------------------------------------
# The probe's guard — the failure mode is a green run that measured nothing
# ---------------------------------------------------------------------------

def _write_log(tmp_path, rows, stamp):
    import json
    p = tmp_path / "log.jsonl"
    p.write_text(json.dumps({"probed_at_utc": stamp, "rows": rows}) + "\n",
                 encoding="utf-8")
    return p


def test_guard_passes_when_lines_were_served(tmp_path):
    import check_vendor_probe as g
    now = _utc(2026, 8, 15, 2, 45)
    p = _write_log(tmp_path, [{"ticker": "SPY", "last_bar": "2026-08-14"}],
                   "2026-08-15T02:41:07+00:00")
    r = g.evaluate(p, now_utc=now)
    assert r["ok"] is True


def test_guard_fails_when_every_line_came_back_empty(tmp_path):
    """A green run that measured nothing is the whole reason this exists."""
    import check_vendor_probe as g
    p = _write_log(tmp_path, [{"ticker": "SPY", "last_bar": None},
                              {"ticker": "EXV1.DE", "last_bar": None}],
                   "2026-08-15T02:41:07+00:00")
    r = g.evaluate(p, now_utc=_utc(2026, 8, 15, 2, 45))
    assert r["ok"] is False and "empty" in r["summary"]


def test_a_partial_result_passes_because_it_is_the_measurement(tmp_path):
    """One venue answering while the other does not IS the asymmetry the probe
    is for. Refusing it would discard the finding."""
    import check_vendor_probe as g
    p = _write_log(tmp_path, [{"ticker": "SPY", "last_bar": "2026-08-14"},
                              {"ticker": "EXV1.DE", "last_bar": None}],
                   "2026-08-15T02:41:07+00:00")
    r = g.evaluate(p, now_utc=_utc(2026, 8, 15, 2, 45))
    assert r["ok"] is True
    assert "EXV1.DE" in r["empty"]


def test_guard_fails_when_this_run_appended_nothing(tmp_path):
    """A stale file re-read looks identical to a fresh observation unless the
    timestamp is checked."""
    import check_vendor_probe as g
    p = _write_log(tmp_path, [{"ticker": "SPY", "last_bar": "2026-08-14"}],
                   "2026-08-14T02:41:07+00:00")          # a day old
    r = g.evaluate(p, now_utc=_utc(2026, 8, 15, 2, 45))
    assert r["ok"] is False and "appended nothing" in r["summary"]


def test_missing_or_unreadable_log_is_undetermined_not_pass(tmp_path):
    import check_vendor_probe as g
    assert g.evaluate(tmp_path / "nope.jsonl")["undetermined"] is True
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    assert g.evaluate(bad)["undetermined"] is True
