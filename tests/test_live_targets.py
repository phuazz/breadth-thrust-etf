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


# ---------------------------------------------------------------------------
# Europe-splice visibility (2026-10-11) — the discard is still the rule,
# and it now says what it discarded.
#
# The weekend that motivated this: Saturday's core run held sleeve C because
# the vendor served no BTC-USD Friday close (single-ticker re-probe answered
# without the bar); the vendor backfilled overnight; Sunday's Europe run
# recomputed a READY C from the backfilled data and silently replaced it
# with the spliced HOLD. Component isolation is correct — the silence was
# the defect. Python months are 1-indexed.
# ---------------------------------------------------------------------------

def _sleeve(name, status, missing=None, decided="2026-10-09", marker=0.0):
    s = {"sleeve": name, "venue": "NYSE", "status": status,
         "decision_session": decided, "weights": {"W": marker or 0.5}}
    if missing is not None:
        s["missing_signal_inputs"] = list(missing)
        s["weights"] = {}
    return s


def test_splice_surfaces_a_repairable_hold():
    previous = [_sleeve("A", "READY", marker=0.11),
                _sleeve("B", "READY", marker=0.12),
                _sleeve("C", "HOLD", missing=["BTC-USD"]),
                _sleeve("D", "READY", marker=0.14)]
    fresh = [_sleeve("A", "READY", marker=0.21),
             _sleeve("B", "READY", marker=0.22),
             _sleeve("C", "READY", marker=0.23),
             _sleeve("D", "READY", marker=0.24)]
    spliced, repairable = lt._splice_previous_core(fresh, previous, "2026-10-09")
    assert [r["sleeve"] for r in repairable] == ["C"]
    assert repairable[0]["missing_then"] == ["BTC-USD"]
    assert repairable[0]["fresh_recompute_status"] == "READY"
    assert repairable[0]["fresh_decision_session"] == "2026-10-09"


def test_splice_is_quiet_while_the_vendor_still_does_not_serve():
    """A hold the fresh recompute CANNOT clear is not repairable — flagging
    it would train the operator to ignore the flag."""
    previous = [_sleeve("C", "HOLD", missing=["BTC-USD"])]
    fresh = [_sleeve("C", "HOLD", missing=["BTC-USD"])]
    _, repairable = lt._splice_previous_core(fresh, previous, "2026-10-09")
    assert repairable == []


def test_splice_is_quiet_when_nothing_was_held():
    previous = [_sleeve("A", "READY", marker=0.11)]
    fresh = [_sleeve("A", "READY", marker=0.21)]
    _, repairable = lt._splice_previous_core(fresh, previous, "2026-10-09")
    assert repairable == []


def test_splice_never_alters_the_previous_release_content():
    """The spliced sleeves are the previous release's, byte for byte: the
    flag changes visibility, never the decision. D stays the fresh run's
    business and is excluded from the splice."""
    previous = [_sleeve("A", "READY", marker=0.11),
                _sleeve("C", "HOLD", missing=["BTC-USD"]),
                _sleeve("D", "READY", marker=0.99)]
    fresh = [_sleeve("A", "READY", marker=0.21),
             _sleeve("C", "READY", marker=0.23),
             _sleeve("D", "READY", marker=0.98)]
    spliced, _ = lt._splice_previous_core(fresh, previous, "2026-10-09")
    assert spliced == [previous[0], previous[1]]
    assert all(s["sleeve"] != "D" for s in spliced)
    # Identity, not equality by accident: the exact dicts are passed through.
    assert spliced[0] is previous[0] and spliced[1] is previous[1]


# ---------------------------------------------------------------------------
# Probe roster: the two C lines Norgate cannot reach (2026-10-11)
# ---------------------------------------------------------------------------

def test_probe_covers_the_c_lines_norgate_cannot_reach():
    """BTC-USD and 159801.SZ are the two sleeve C names outside Norgate's
    reach, and the two the availability log was blind on when the 2026-10-10
    core run held sleeve C. Both are sampled now."""
    tickers = {t for t, _, _ in probe.PROBES}
    assert "BTC-USD" in tickers
    assert "159801.SZ" in tickers
    venues = {t: v for t, v, _ in probe.PROBES}
    assert venues["BTC-USD"] == "CRYPTO"
    assert venues["159801.SZ"] == "SSE"


def test_crypto_days_behind_boundaries():
    """Calendar-day lateness for a 7-day instrument, with the month and year
    boundaries the house date rules require. Python months are 1-indexed."""
    assert probe._days_behind("2026-10-09", "2026-10-10") == 1   # weekend Sat
    assert probe._days_behind("2026-10-10", "2026-10-10") == 0
    assert probe._days_behind("2026-10-11", "2026-10-10") == -1  # in-progress bar
    assert probe._days_behind("2026-10-31", "2026-11-01") == 1   # month boundary
    assert probe._days_behind("2026-12-31", "2027-01-01") == 1   # year boundary
    assert probe._days_behind(None, "2026-10-10") is None
    assert probe._days_behind("2026-10-09", None) is None


def _stub_download(bars_through_by_ticker):
    """A yfinance.download stand-in serving daily closes through the given
    date per ticker, on a ('Close', ticker) MultiIndex like group_by='column'."""
    def download(tickers, start=None, end=None, **_kw):
        tickers = list(tickers)
        frames = {}
        for tk in tickers:
            end_date = pd.Timestamp(bars_through_by_ticker[tk])
            idx = pd.date_range(end=end_date, periods=8, freq="D")
            frames[("Close", tk)] = pd.Series(100.0, index=idx)
        return pd.DataFrame(frames)
    return download


def test_probe_crypto_row_reads_utc_days_not_nyse_sessions():
    """At Sunday 2026-10-11 01:16 UTC — the actual weekend run clock — a
    BTC-USD series ending Friday is one completed UTC DAY behind (Saturday's
    bar is owed), while SPY ending Friday is current on the NYSE calendar.
    Conflating those two frames is exactly what a per-venue probe exists to
    prevent."""
    now = _utc(2026, 10, 11, 1, 16)
    through = {t: "2026-10-09" for t, _, _ in probe.PROBES}
    r = probe.probe(now_utc=now, download=_stub_download(through))
    rows = {x["ticker"]: x for x in r["rows"]}
    assert rows["SPY"]["sessions_behind"] == 0
    assert rows["SPY"]["last_completed_session"] == "2026-10-09"
    assert rows["BTC-USD"]["last_completed_session"] == "2026-10-10"
    assert rows["BTC-USD"]["sessions_behind"] == 1
    assert rows["159801.SZ"]["sessions_behind"] == 0  # SSE last session 10-09

    through["BTC-USD"] = "2026-10-10"
    r2 = probe.probe(now_utc=now, download=_stub_download(through))
    rows2 = {x["ticker"]: x for x in r2["rows"]}
    assert rows2["BTC-USD"]["sessions_behind"] == 0


def test_splice_enforces_the_same_anchor_on_both_sides():
    """Adversarial review P2 (2026-10-11): a delayed Europe run must not
    recommend repairing a Friday instruction from a Monday decision, and an
    old HOLD from an earlier week is not repaired by this week's READY."""
    # Fresh recompute decided on a LATER session than the release anchor.
    previous = [_sleeve("C", "HOLD", missing=["BTC-USD"], decided="2026-10-09")]
    fresh = [_sleeve("C", "READY", decided="2026-10-12", marker=0.23)]
    _, repairable = lt._splice_previous_core(fresh, previous, "2026-10-09")
    assert repairable == []
    # Prior HOLD from an EARLIER week than the release anchor.
    previous = [_sleeve("C", "HOLD", missing=["BTC-USD"], decided="2026-10-02")]
    fresh = [_sleeve("C", "READY", decided="2026-10-09", marker=0.23)]
    _, repairable = lt._splice_previous_core(fresh, previous, "2026-10-09")
    assert repairable == []
    # Both sides on the anchor: flagged, and the anchor is recorded.
    previous = [_sleeve("C", "HOLD", missing=["BTC-USD"], decided="2026-10-09")]
    fresh = [_sleeve("C", "READY", decided="2026-10-09", marker=0.23)]
    _, repairable = lt._splice_previous_core(fresh, previous, "2026-10-09")
    assert [r["sleeve"] for r in repairable] == ["C"]
    assert repairable[0]["anchor"] == "2026-10-09"


def _stub_download_days(days_by_ticker):
    """Like _stub_download, but with explicit bar dates per ticker, so a
    response can carry internal holes and in-progress rows. Records every
    call's ticker list so a test can assert the single-ticker re-ask."""
    calls = []

    def download(tickers, start=None, end=None, **_kw):
        tickers = list(tickers)
        calls.append(tuple(tickers))
        frames = {}
        for tk in tickers:
            idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days_by_ticker[tk]])
            frames[("Close", tk)] = pd.Series(100.0, index=idx)
        return pd.DataFrame(frames)

    download.calls = calls
    return download


def test_crypto_settled_bar_ignores_intraday_and_sees_holes():
    """The reviewer's reproduction: 8 and 11 October present, 9 and 10
    October absent, probed Sunday 01:16 UTC. The old logic read the
    in-progress 11 October row as sessions_behind=-1, current. The settled
    logic must report two days behind, name both holes, flag the bound bar
    absent — and must have re-asked the vendor single-ticker."""
    now = _utc(2026, 10, 11, 1, 16)
    days = {t: ["2026-10-07", "2026-10-08", "2026-10-09"] for t, _, _ in probe.PROBES}
    days["BTC-USD"] = ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-11"]
    dl = _stub_download_days(days)
    r = probe.probe(now_utc=now, download=dl)
    row = {x["ticker"]: x for x in r["rows"]}["BTC-USD"]
    assert row["last_bar"] == "2026-10-08"
    assert row["sessions_behind"] == 2
    assert row["required_day_bar_present"] is False
    assert row["missing_completed_days"] == ["2026-10-09", "2026-10-10"]
    assert row.get("refetched") is True
    assert ("BTC-USD",) in dl.calls  # the single-ticker re-ask happened


def test_crypto_full_tail_reports_current_and_quiet():
    now = _utc(2026, 10, 11, 1, 16)
    days = {t: ["2026-10-08", "2026-10-09"] for t, _, _ in probe.PROBES}
    days["BTC-USD"] = ["2026-10-06", "2026-10-07", "2026-10-08",
                       "2026-10-09", "2026-10-10"]
    r = probe.probe(now_utc=now, download=_stub_download_days(days))
    row = {x["ticker"]: x for x in r["rows"]}["BTC-USD"]
    assert row["sessions_behind"] == 0
    assert row["required_day_bar_present"] is True
    assert row["missing_completed_days"] == []
    assert "refetched" not in row


def test_crypto_non_finite_values_are_holes_not_bars():
    """A zero, negative, infinite or NaN print on a completed day is a hole.
    Checked directly on the settled helper. Python months are 1-indexed."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in
                            ["2026-10-06", "2026-10-07", "2026-10-08",
                             "2026-10-09", "2026-10-10"]])
    s = pd.Series([100.0, 0.0, float("inf"), float("nan"), -5.0], index=idx)
    last_bar, missing, required = probe._settled_crypto(s, pd.Timestamp("2026-10-10"))
    assert str(last_bar.date()) == "2026-10-06"
    assert missing == ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"]
    assert required is False


def test_crypto_settled_window_crosses_month_and_year_boundaries():
    """Bound 2027-01-01: the five-day hole window reaches back into December
    2026. Python months are 1-indexed."""
    idx = pd.DatetimeIndex([pd.Timestamp("2026-12-29"), pd.Timestamp("2026-12-30")])
    s = pd.Series([100.0, 101.0], index=idx)
    last_bar, missing, required = probe._settled_crypto(s, pd.Timestamp("2027-01-01"))
    assert str(last_bar.date()) == "2026-12-30"
    assert missing == ["2026-12-28", "2026-12-31", "2027-01-01"]
    assert required is False
