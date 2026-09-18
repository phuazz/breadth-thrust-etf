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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from venue_calendars import get_calendar as _venue_cal  # noqa: E402
import live_targets as lt  # noqa: E402
import probe_vendor_availability as probe  # noqa: E402

NYSE = _venue_cal("NYSE")
XETR = _venue_cal("XETR")


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


def test_partial_signal_names_missing_inputs_without_ranking():
    sig = _signal(["2026-09-09", "2026-09-10", "2026-09-11"])
    sig.loc[pd.Timestamp("2026-09-11"), "Y"] = float("nan")

    def must_not_rank(row):
        raise AssertionError("partial input must never reach the ranker")

    r = lt._rank(sig, must_not_rank, "NYSE", _utc(2026, 9, 12, 4), "C")
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["missing_signal_inputs"] == ["Y"]
    assert "missing signal inputs: Y" in r["reason"]
    assert "2 of 3" in r["reason"]


# ---------------------------------------------------------------------------
# The cash proxy — the destination, never a candidate
# ---------------------------------------------------------------------------

def _thematic_row(n_above: int, n_total: int = 25, cash: bool = True):
    """A sleeve C signal row with ``n_above`` names clear of the +5% floor."""
    import run_thematic_rotation as th
    vals = {f"T{i}": (0.20 if i < n_above else -0.10) for i in range(n_total)}
    if cash:
        vals[th.CASH_PROXY] = 0.001
    return pd.Series(vals)


def _nonzero(w):
    return dict(w[w > 0])


def test_the_thematic_gate_fires_on_the_count_not_on_what_is_held():
    """7 of 25 is 28% and gates; 8 of 25 is 32% and does not. The 2026-09-16
    book exited ARKG at rank 1 of 25 and improving, because the gate counts
    the universe, not the holdings."""
    import run_thematic_rotation as th
    f = th.top_k_equal_weight(th.HEADLINE_K)
    assert _nonzero(f(_thematic_row(7))) == {th.CASH_PROXY: 1.0}
    ungated = _nonzero(f(_thematic_row(8)))
    assert len(ungated) == th.HEADLINE_K and th.CASH_PROXY not in ungated


def test_the_gate_record_and_the_weights_cannot_disagree():
    """One definition: top_k_equal_weight calls sleeve_gate_state rather than
    counting for itself, so a surface quoting the record can never describe a
    book the weights did not produce."""
    import run_thematic_rotation as th
    f = th.top_k_equal_weight(th.HEADLINE_K)
    for n_above in range(0, 12):
        row = _thematic_row(n_above)
        assert th.sleeve_gate_state(row)["fired"] == \
            (_nonzero(f(row)) == {th.CASH_PROXY: 1.0}), n_above


def test_the_gate_record_carries_what_the_commentary_reads():
    """Cross-module contract: build_commentary._gate_sentence states the gate's
    own arithmetic, so a renamed field must break here rather than in a
    sentence nobody re-reads."""
    import run_thematic_rotation as th
    import build_commentary as bc
    gate = th.sleeve_gate_state(_thematic_row(6))
    assert set(gate) >= {"fired", "n_above", "n_universe", "floor", "threshold"}
    assert bc._gate_sentence(gate, [{"traded": "SHY"}]).startswith(
        "Sleeve-breadth gate on: 6 of 25 names above the +5% floor, under the "
        "30% threshold")


def test_a_gate_with_no_usable_names_does_not_divide_by_zero():
    import run_thematic_rotation as th
    gate = th.sleeve_gate_state(pd.Series({"X": float("nan")}))
    assert gate["fired"] is False and gate["n_universe"] == 0


def test_the_engines_place_their_cash_only_when_the_column_exists():
    """Both cash allocations are guarded by `if CASH_PROXY in w.index`, so a
    panel reindexed to TICKERS alone drops them silently — which is why
    live_targets has to carry the column. Pins the mechanism, not the bug."""
    import run_thematic_rotation as th
    f = th.top_k_equal_weight(th.HEADLINE_K)
    assert _nonzero(f(_thematic_row(6))) == {th.CASH_PROXY: 1.0}
    assert _nonzero(f(_thematic_row(6, cash=False))) == {}


def test_a_gated_sleeve_books_its_cash_proxy_rather_than_an_empty_rank():
    """2026-09-16: sleeve C's gate fired, the engine's own path books SHY at
    1.0, and the target book came back {} — five SELL ALLs, no buy, 90% of
    NAV."""
    def gate_to_cash(row):
        w = pd.Series(0.0, index=row.index)
        if "SHY" in w.index:
            w["SHY"] = 1.0
        return w

    sig = _signal(["2026-09-15", "2026-09-16"], cols=("X", "Y", "SHY"))
    r = lt._rank(sig, gate_to_cash, "NYSE", _utc(2026, 9, 17, 2), "C",
                 cash_proxy="SHY")
    assert r["status"] == "READY"
    assert r["weights"] == {"SHY": 1.0}


def test_the_cash_proxy_is_not_a_ranked_name():
    """It must not appear in the emitted signal, or every downstream "rank 4
    of 25" silently becomes "of 26" and the ranks below it shift."""
    sig = _signal(["2026-09-15", "2026-09-16"], cols=("X", "Y", "SHY"))
    r = lt._rank(sig, _top2, "NYSE", _utc(2026, 9, 17, 2), "C", cash_proxy="SHY")
    assert set(r["signals"]) == {"X", "Y"}


def test_the_cash_proxy_is_outside_the_coverage_floor():
    """SHY is the destination, not part of the signal: its absence cannot
    change the rank, so it must not be able to force a HOLD either."""
    sig = _signal(["2026-09-15", "2026-09-16"], cols=("X", "Y", "SHY"))
    sig.loc[pd.Timestamp("2026-09-16"), "SHY"] = float("nan")
    r = lt._rank(sig, _top2, "NYSE", _utc(2026, 9, 17, 2), "C", cash_proxy="SHY")
    assert r["status"] == "READY"

    sig.loc[pd.Timestamp("2026-09-16"), "Y"] = float("nan")
    r = lt._rank(sig, _top2, "NYSE", _utc(2026, 9, 17, 2), "C", cash_proxy="SHY")
    assert r["status"] == "HOLD" and "1 of 2" in r["reason"]


def test_a_ranked_cash_ticker_is_not_duplicated():
    """Sleeve B ranks IEF and holds SHY as cash; if an engine ever ranked its
    own proxy, a second column would be a second signal."""
    assert lt._with_cash(["SPY", "IEF"], "SHY") == ["SPY", "IEF", "SHY"]
    assert lt._with_cash(["SPY", "SHY"], "SHY") == ["SPY", "SHY"]


def test_the_intended_book_of_a_gated_sleeve_is_fully_invested():
    """The 90%-of-NAV regression: before the cash column, the five exits had
    no matching buy and a tenth of the book was unallocated and unlabelled,
    while mark_to_market_live booked the residual into SHY regardless."""
    sleeves = [{"sleeve": "C", "status": "READY", "weights": {"SHY": 1.0}}]
    held = {("C", t): 0.02 for t in ("CIBR", "SKYY", "XBI", "ARKG", "COPX")}
    nav = {"a": 0.35, "b": 0.25, "c": 0.10, "d": 0.20,
           "tilt_nav": 0.0, "shy_overlay": 0.0}
    lines = lt._intended_lines(sleeves, held, nav)
    buys = [ln for ln in lines if ln["held"] == 0 and ln["target"] > 0]
    assert [ln["etf"] for ln in buys] == ["SHY"]
    assert abs(buys[0]["target"] - 0.10) < 1e-9
    assert abs(sum(ln["target"] for ln in lines)
               - sum(ln["held"] for ln in lines)) < 1e-9


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
    # ADDED 2026-09-09. The US side has the same split and it went unmeasured:
    # SPY and XLF carried 2026-09-08 all evening while LIN, CRH, SW and AMCR
    # did not, which capped the IUMS panel at 09-04 and held sleeve A. A US
    # ETF proxy cannot answer for the constituents any more than EXV1.DE can.
    assert "US constituent (foreign domicile)" in roles


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
