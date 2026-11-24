"""The SPY benchmark's withheld tail is re-sourced from Norgate (2026-11-24).

THE FAILURE THESE PIN. The post-fill refresh failed its first attempt at
``portfolio_construction (variants table)`` on 10, 17, 18 and 24 November
2026 with "SPY benchmark unavailable: 1 session(s) lack a finite positive
close". On 24 November the cache the failing fetch wrote at 02:22:17 UTC
held the 2026-11-23 row with a NaN close; Yahoo served 722.2899780273438 for
it by 03:36 UTC and Norgate carried the same figure throughout.

The fix fills only the trailing sessions Yahoo withheld, from Norgate, under
compute_breadth.resource_tail_from_norgate's guards, and only on a run that
asked for Norgate. Everything else reaches run_portfolio.spy_benchmark_problem
as before, and that guard still refuses to write the diagnostics.

Nothing here hits the network or the Norgate feed: every vendor call is
stubbed. Sessions come from the NYSE calendar (pandas_market_calendars via
venue_calendars), never from a hand-computed offset or weekday count. Python
datetime months are 1-indexed (January = 1).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backtest as bt  # noqa: E402
import compute_breadth as cb  # noqa: E402
import norgate_prices as npx  # noqa: E402
import run_portfolio as rp  # noqa: E402
import venue_calendars as vc  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

NYSE = get_calendar("NYSE")

# The live instance: Monday's session withheld on Tuesday morning, 02:22 UTC.
LIVE_SESSION = "2026-11-23"
LIVE_NOW = datetime(2026, 11, 24, 2, 22, tzinfo=timezone.utc)


def _sessions(start: str, end: str) -> pd.DatetimeIndex:
    """NYSE sessions from ``start`` to ``end`` inclusive, tz-naive."""
    days = NYSE.valid_days(start_date=start, end_date=end)
    return pd.DatetimeIndex(days.tz_localize(None)).normalize()


def _series(idx: pd.DatetimeIndex, base: float = 500.0) -> pd.Series:
    """A rising close on ``idx``; daily steps well under any step guard."""
    return pd.Series(base + 0.5 * np.arange(len(idx), dtype=float), index=idx,
                     name="Close")


def _pair(newest: str, n: int = 60) -> tuple[pd.Series, pd.Series]:
    """(truth, norgate): ``n`` sessions ending on ``newest``, same levels."""
    idx = _sessions("2026-06-01", newest)[-n:]
    assert idx[-1] == pd.Timestamp(newest)
    truth = _series(idx)
    return truth, truth.copy()


# ---------------------------------------------------------------------------
# spy_tail_cap: the newest session a re-source may fill
# ---------------------------------------------------------------------------
def test_the_fixture_dates_are_the_weekdays_the_incident_had():
    # weekday(): Monday = 0. Read from the date library, not from memory.
    assert date(2026, 11, 23).weekday() == 0     # Monday, the withheld session
    assert date(2026, 11, 24).weekday() == 1     # Tuesday, the post-fill run
    assert date(2026, 11, 30).weekday() == 0     # Monday, month-boundary case
    assert date(2026, 12, 31).weekday() == 3     # Thursday, year-boundary case


@pytest.mark.parametrize("now, end, expected", [
    # The live instance: Tuesday 02:22 UTC, Monday's close has passed.
    (datetime(2026, 11, 24, 2, 22, tzinfo=timezone.utc), "2026-11-28", "2026-11-23"),
    # One minute before Monday's 21:00 UTC close: Monday is not complete.
    (datetime(2026, 11, 23, 20, 59, tzinfo=timezone.utc), "2026-11-28", "2026-11-20"),
    (datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc), "2026-11-28", "2026-11-23"),
    # Month boundary: Tuesday 1 December, newest session Monday 30 November.
    (datetime(2026, 12, 1, 2, 22, tzinfo=timezone.utc), "2026-12-05", "2026-11-30"),
    # Year boundary: New Year's Day morning, newest session 31 December.
    (datetime(2027, 1, 1, 2, 22, tzinfo=timezone.utc), "2027-01-05", "2026-12-31"),
    # Still 31 December on the Monday morning after the holiday weekend.
    (datetime(2027, 1, 4, 2, 22, tzinfo=timezone.utc), "2027-01-08", "2026-12-31"),
    # Thanksgiving (Thursday 26 November 2026) is not a session.
    (datetime(2026, 11, 27, 2, 22, tzinfo=timezone.utc), "2026-12-01", "2026-11-25"),
    # ``end`` is exclusive, as yfinance reads it: a session dated ``end`` was
    # never requested, so it is not filled.
    (datetime(2026, 11, 24, 2, 22, tzinfo=timezone.utc), "2026-11-23", "2026-11-20"),
    (datetime(2027, 1, 5, 2, 22, tzinfo=timezone.utc), "2027-01-01", "2026-12-31"),
])
def test_cap_is_the_last_completed_session_inside_the_window(now, end, expected):
    assert bt.spy_tail_cap(end, now) == pd.Timestamp(expected)


def test_cap_accepts_a_naive_clock_as_utc():
    naive = datetime(2026, 11, 24, 2, 22)
    assert bt.spy_tail_cap("2026-11-28", naive) == pd.Timestamp(LIVE_SESSION)


# ---------------------------------------------------------------------------
# resource_spy_tail: the pure rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("newest, now", [
    (LIVE_SESSION, LIVE_NOW),                                         # the live instance
    ("2026-11-30", datetime(2026, 12, 1, 2, 22, tzinfo=timezone.utc)),  # month boundary
    ("2026-12-31", datetime(2027, 1, 1, 2, 22, tzinfo=timezone.utc)),   # year boundary
    ("2027-01-04", datetime(2027, 1, 5, 2, 22, tzinfo=timezone.utc)),   # overlap spans the year end
])
@pytest.mark.parametrize("shape", ["nan_row", "row_omitted"])
def test_a_withheld_newest_session_is_filled_from_norgate(newest, now, shape):
    truth, norgate = _pair(newest)
    yahoo = truth.copy()
    if shape == "nan_row":
        yahoo.iloc[-1] = np.nan            # the 2026-11-24 shape
    else:
        yahoo = yahoo.iloc[:-1]            # the row is not in the response
    cap = bt.spy_tail_cap((truth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d"), now)
    assert cap == pd.Timestamp(newest)

    out, record = bt.resource_spy_tail(yahoo, norgate, cap)

    assert out.index.equals(truth.index)
    assert out.loc[pd.Timestamp(newest)] == norgate.loc[pd.Timestamp(newest)]
    # The Yahoo history is not touched: no published benchmark figure moves.
    pd.testing.assert_series_equal(out.iloc[:-1], truth.iloc[:-1])
    fill = record["filled"]["SPY"]
    assert fill["dates"] == [newest]
    assert fill["overlap_bars"] == cb.NORGATE_TAIL_OVERLAP_BARS
    assert fill["max_rel_diff"] == 0.0


def test_two_withheld_sessions_are_filled_in_order():
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.iloc[:-2]
    out, record = bt.resource_spy_tail(yahoo, norgate, pd.Timestamp(LIVE_SESSION))
    assert record["filled"]["SPY"]["dates"] == ["2026-11-20", "2026-11-23"]
    pd.testing.assert_series_equal(out, truth)


def test_a_complete_series_is_returned_as_the_same_object():
    truth, norgate = _pair(LIVE_SESSION)
    out, record = bt.resource_spy_tail(truth, norgate, pd.Timestamp(LIVE_SESSION))
    assert out is truth and record is None


def test_an_interior_hole_is_not_a_tail_and_is_left_alone():
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-5] = np.nan
    out, record = bt.resource_spy_tail(yahoo, norgate, pd.Timestamp(LIVE_SESSION))
    assert out is yahoo and record is None
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


@pytest.mark.parametrize("bad", [0.0, np.inf])
def test_a_zero_or_infinite_newest_close_is_not_treated_as_withheld(bad):
    """Only a blank is the withdrawal shape. Any other unusable close is a
    different fault and stays in front of the guard."""
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = bad
    out, record = bt.resource_spy_tail(yahoo, norgate, pd.Timestamp(LIVE_SESSION))
    assert out is yahoo and record is None
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


def test_a_session_norgate_does_not_carry_stays_missing():
    """The benchmark is missing for real: neither vendor has the session."""
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    out, record = bt.resource_spy_tail(yahoo, norgate.iloc[:-1], pd.Timestamp(LIVE_SESSION))
    assert out is yahoo
    assert "does not carry" in record["declined"]["SPY"]
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


def test_two_adjustment_bases_are_never_spliced():
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    rebased = norgate * (1.0 + 2 * cb.NORGATE_TAIL_RTOL)
    out, record = bt.resource_spy_tail(yahoo, rebased, pd.Timestamp(LIVE_SESSION))
    assert out is yahoo
    assert "two bases" in record["declined"]["SPY"]


def test_a_long_outage_is_not_a_vendor_lag():
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.iloc[:-(cb.NORGATE_TAIL_MAX_SESSIONS + 1)]
    out, record = bt.resource_spy_tail(yahoo, norgate, pd.Timestamp(LIVE_SESSION))
    assert out is yahoo
    assert "not a vendor lag" in record["declined"]["SPY"]


def test_a_blank_row_after_the_cap_is_not_filled():
    """A session that has not closed is not a withheld bar, whatever Norgate
    is handed: the cap is frozen before acquisition."""
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    cap = truth.index[-2]                  # Friday 20 November
    out, record = bt.resource_spy_tail(yahoo, norgate, cap)
    assert out is yahoo and record is None


@pytest.mark.parametrize("case, reason", [
    ("no_norgate", "no Norgate series was supplied"),
    ("no_cap", "no completed session bounds the fill"),
    ("yahoo_blank", "the Yahoo series has no finite close"),
    ("norgate_blank", "the Norgate close column has no finite close"),
    # Thursday 19 November 2026 appears twice in the Yahoo index.
    ("duplicated_date", "the Yahoo index carries 2026-11-19 more than once, "
                        "so its tail cannot be read"),
    # Yahoo sent no row for Monday and Norgate has none either.
    ("neither_has_a_row", "Norgate carries no session after 2026-11-20 either"),
])
def test_every_return_that_fills_nothing_names_its_reason(case, reason):
    """Review of pull request 17: one fallback text used to cover a
    duplicated Yahoo date and an entirely blank Norgate column as well as the
    case it described."""
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    cap = pd.Timestamp(LIVE_SESSION)
    if case == "no_norgate":
        norgate = None
    elif case == "no_cap":
        cap = None
    elif case == "yahoo_blank":
        yahoo = yahoo * np.nan
    elif case == "norgate_blank":
        norgate = norgate * np.nan
    elif case == "duplicated_date":
        yahoo = pd.concat([yahoo.iloc[:-2], yahoo.iloc[-3:-2], yahoo.iloc[-2:]])
        assert list(yahoo.index[yahoo.index.duplicated()]) == [pd.Timestamp("2026-11-19")]
    else:
        yahoo, norgate = truth.iloc[:-1], norgate.iloc[:-1]

    out, record = bt.resource_spy_tail(yahoo, norgate, cap)

    assert out is yahoo
    assert record == {"filled": {}, "declined": {"SPY": reason}}
    assert json.loads(json.dumps(record)) == record


# ---------------------------------------------------------------------------
# download_spy_close: the fetch, with every vendor stubbed
# ---------------------------------------------------------------------------
def _yahoo_frame(close: pd.Series) -> pd.DataFrame:
    frame = close.to_frame("Close")
    frame.index.name = "Date"
    return frame


def _norgate_ohlc(close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({c: close for c in ("Open", "High", "Low", "Close")})


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A cold SPY cache in tmp_path and a Norgate feed that must not be
    touched unless a test installs one."""
    cache = tmp_path / "spy_close_cache.parquet"
    monkeypatch.setattr(bt, "SPY_CACHE", cache)
    monkeypatch.delenv("BTE_PRICE_SOURCE", raising=False)
    monkeypatch.setattr(npx, "fetch_ohlc", lambda *a, **k: pytest.fail(
        "Norgate must not be asked on this run"))
    return cache


def _serve(monkeypatch, yahoo: pd.Series, norgate: pd.Series | None = None,
           source: str | None = None):
    monkeypatch.setattr(bt.yf, "download", lambda *a, **k: _yahoo_frame(yahoo),
                        raising=False)
    if source is not None:
        monkeypatch.setenv("BTE_PRICE_SOURCE", source)
    if norgate is not None:
        monkeypatch.setattr(npx, "fetch_ohlc",
                            lambda *a, **k: _norgate_ohlc(norgate))


@pytest.mark.parametrize("source", ["norgate", "auto"])
@pytest.mark.parametrize("shape", ["nan_row", "row_omitted"])
def test_a_norgate_run_returns_and_caches_the_withheld_close(site, monkeypatch,
                                                            capsys, source, shape):
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    if shape == "nan_row":
        yahoo.iloc[-1] = np.nan
    else:
        yahoo = yahoo.iloc[:-1]
    _serve(monkeypatch, yahoo, norgate, source)

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    pd.testing.assert_series_equal(out, truth, check_names=False, check_freq=False)
    assert out.attrs["tail_from_norgate"]["filled"]["SPY"]["dates"] == [LIVE_SESSION]
    on_disk = pd.read_parquet(site)["Close"]
    assert on_disk.loc[pd.Timestamp(LIVE_SESSION)] == truth.iloc[-1]
    said = capsys.readouterr().out
    assert "Yahoo's last finite close is 2026-11-20" in said
    assert f"Norgate tail re-source SPY (benchmark): {LIVE_SESSION}" in said
    # The guard the step applies now passes, on real closes.
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is None


def test_a_yfinance_run_is_left_exactly_as_fetched(site, monkeypatch, capsys):
    """No Norgate on a default run: the hole reaches the guard and the step
    fails as it did before."""
    truth, _ = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    _serve(monkeypatch, yahoo)                    # fetch_ohlc still fails the test

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    assert np.isnan(out.iloc[-1]) and "tail_from_norgate" not in out.attrs
    said = capsys.readouterr().out
    assert "row(s) present with a NaN close: 2026-11-23" in said
    assert "NOT re-sourced: the run did not ask for Norgate" in said
    problem = rp.spy_benchmark_problem(out, truth.index, truth.index[0])
    assert problem.startswith("1 session(s) lack a finite positive close")


def test_a_norgate_run_with_the_feed_down_is_left_as_fetched(site, monkeypatch,
                                                            capsys):
    truth, _ = _pair(LIVE_SESSION)
    yahoo = truth.iloc[:-1]
    _serve(monkeypatch, yahoo, source="norgate")
    monkeypatch.setattr(npx, "fetch_ohlc", lambda *a, **k: None)

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    pd.testing.assert_series_equal(out, yahoo, check_names=False, check_freq=False)
    said = capsys.readouterr().out
    assert "no row after it in the response" in said
    assert "Norgate returned nothing for SPY" in said
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


def test_a_fault_in_the_re_source_leaves_the_series_as_fetched(site, monkeypatch,
                                                             capsys):
    """The callers that forward-fill must not crash where they used to run;
    the hole still reaches the guard."""
    truth, _ = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    _serve(monkeypatch, yahoo, source="norgate")

    def broken(*a, **k):
        raise RuntimeError("feed dropped mid-call")

    monkeypatch.setattr(npx, "fetch_ohlc", broken)

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    assert np.isnan(out.iloc[-1]) and "tail_from_norgate" not in out.attrs
    assert "NOT re-sourced: RuntimeError: feed dropped mid-call" in capsys.readouterr().out
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


def test_a_benchmark_missing_at_both_vendors_is_declined_and_refused(
        site, monkeypatch, capsys):
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    _serve(monkeypatch, yahoo, norgate.iloc[:-1], "norgate")

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    assert np.isnan(out.iloc[-1])
    assert "filled" in out.attrs["tail_from_norgate"]
    assert out.attrs["tail_from_norgate"]["filled"] == {}
    assert "DECLINED" in capsys.readouterr().out
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


def test_a_complete_fetch_never_asks_norgate(site, monkeypatch, capsys):
    truth, _ = _pair(LIVE_SESSION)
    _serve(monkeypatch, truth, source="norgate")  # fetch_ohlc still fails the test

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    pd.testing.assert_series_equal(out, truth, check_names=False, check_freq=False)
    assert "tail_from_norgate" not in out.attrs
    assert "SPY benchmark" not in capsys.readouterr().out


def test_a_session_dated_end_is_outside_the_request_and_not_added(site, monkeypatch):
    """Year boundary on the exclusive ``end``: a window ending 2027-01-04
    stops at 31 December even though Norgate carries 4 January."""
    truth, norgate = _pair("2027-01-04")
    yahoo = truth.iloc[:-1]                       # through 2026-12-31, complete
    _serve(monkeypatch, yahoo, source="norgate")  # fetch_ohlc still fails the test

    out = bt.download_spy_close("2026-06-01", "2027-01-04",
                                now_utc=datetime(2027, 1, 5, 2, 22, tzinfo=timezone.utc))

    assert out.index[-1] == pd.Timestamp("2026-12-31")
    assert len(out) == len(yahoo)


def _break_calendar(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("calendar unavailable")

    # spy_tail_cap imports get_calendar from the module at call time.
    monkeypatch.setattr(vc, "get_calendar", broken)


def test_a_calendar_fault_leaves_a_complete_fetch_as_fetched(site, monkeypatch,
                                                           capsys):
    """The calendar is read on every fresh fetch, a complete one on a
    yfinance run included, so a fault in it must not raise for the caller
    (review of pull request 17)."""
    truth, _ = _pair(LIVE_SESSION)
    _serve(monkeypatch, truth)                    # fetch_ohlc still fails the test
    _break_calendar(monkeypatch)

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    pd.testing.assert_series_equal(out, truth, check_names=False, check_freq=False)
    assert "tail_from_norgate" not in out.attrs
    assert ("NYSE calendar lookup failed (RuntimeError: calendar unavailable)"
            in capsys.readouterr().out)


def test_a_calendar_fault_on_a_norgate_run_fills_nothing_and_is_refused(
        site, monkeypatch, capsys):
    """Without a session bound nothing is filled and Norgate is not asked;
    the blank row is still named and still reaches the guard."""
    truth, _ = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    _serve(monkeypatch, yahoo, source="norgate")  # fetch_ohlc still fails the test
    _break_calendar(monkeypatch)

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    assert np.isnan(out.iloc[-1]) and "tail_from_norgate" not in out.attrs
    said = capsys.readouterr().out
    assert "row(s) present with a NaN close: 2026-11-23" in said
    assert "NOT re-sourced: the NYSE calendar lookup failed." in said
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


def test_a_duplicated_yahoo_date_prints_its_own_reason(site, monkeypatch, capsys):
    """Not "Norgate carries no session after it either": Norgate carries the
    session, and the fault is in the Yahoo index."""
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    yahoo = pd.concat([yahoo.iloc[:-2], yahoo.iloc[-3:-2], yahoo.iloc[-2:]])
    _serve(monkeypatch, yahoo, norgate, "norgate")

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    assert np.isnan(out.iloc[-1]) and len(out) == len(yahoo)
    assert out.attrs["tail_from_norgate"]["filled"] == {}
    said = capsys.readouterr().out
    assert ("DECLINED: the Yahoo index carries 2026-11-19 more than once, "
            "so its tail cannot be read") in said
    assert "Norgate carries no session" not in said


def test_a_row_neither_vendor_sent_prints_that_reason(site, monkeypatch, capsys):
    """The one case the old fallback text described."""
    truth, norgate = _pair(LIVE_SESSION)
    yahoo = truth.iloc[:-1]
    _serve(monkeypatch, yahoo, norgate.iloc[:-1], "norgate")

    out = bt.download_spy_close("2026-06-01", "2026-11-28", now_utc=LIVE_NOW)

    pd.testing.assert_series_equal(out, yahoo, check_names=False, check_freq=False)
    assert ("DECLINED: Norgate carries no session after 2026-11-20 either"
            in capsys.readouterr().out)
    assert rp.spy_benchmark_problem(out, truth.index, truth.index[0]) is not None


# ---------------------------------------------------------------------------
# run_portfolio: the guard is kept, and now says which session
# ---------------------------------------------------------------------------
def test_the_refusal_names_the_session_and_what_the_fetch_held():
    truth, _ = _pair(LIVE_SESSION)
    blank = truth.copy()
    blank.iloc[-1] = np.nan
    assert rp.spy_benchmark_problem(blank, truth.index, truth.index[0]) == (
        "1 session(s) lack a finite positive close: "
        "2026-11-23 (close nan in the fetch)")
    assert rp.spy_benchmark_problem(truth.iloc[:-1], truth.index, truth.index[0]) == (
        "1 session(s) lack a finite positive close: "
        "2026-11-23 (absent from the fetch)")


def test_the_refusal_lists_a_bounded_number_of_sessions():
    truth, _ = _pair(LIVE_SESSION)
    short = truth.iloc[:-(rp.SPY_PROBLEM_DATES_SHOWN + 3)]
    problem = rp.spy_benchmark_problem(short, truth.index, truth.index[0])
    assert problem.startswith(
        f"{rp.SPY_PROBLEM_DATES_SHOWN + 3} session(s) lack a finite positive close: ")
    assert problem.count("absent from the fetch") == rp.SPY_PROBLEM_DATES_SHOWN
    assert problem.endswith(", and 3 more")


class _VariantsReached(Exception):
    """The benchmark guard passed and the variants were about to run."""


@pytest.fixture
def step(site, tmp_path, monkeypatch):
    """run_portfolio.main() on a one-line panel ending Monday 23 November
    2026, with the real download_spy_close on a fixed clock."""
    idx = _sessions("2025-11-03", LIVE_SESSION)
    closes = pd.DataFrame({"SOXX": np.linspace(100.0, 120.0, len(idx))}, index=idx)
    breadths = pd.DataFrame({"SOXX": 0.5}, index=idx)
    report = tmp_path / "portfolio_construction.json"
    report.write_text("previous verified report", encoding="utf-8")
    monkeypatch.setattr(rp, "OUT_PATH", report)
    monkeypatch.setattr(rp, "build_panels", lambda: (closes, breadths, ["SOXX"]))
    monkeypatch.setattr(rp, "download_spy_close",
                        lambda s, e: bt.download_spy_close(s, e, now_utc=LIVE_NOW))

    def reached(*a, **k):
        raise _VariantsReached

    monkeypatch.setattr(rp, "run_portfolio", reached)
    return _series(idx), report


def test_the_step_proceeds_when_norgate_carries_the_withheld_session(step, monkeypatch):
    truth, report = step
    yahoo = truth.copy()
    yahoo.iloc[-1] = np.nan
    _serve(monkeypatch, yahoo, truth.copy(), "norgate")
    with pytest.raises(_VariantsReached):
        rp.main()
    assert report.read_text(encoding="utf-8") == "previous verified report"


@pytest.mark.parametrize("shape", ["nan_row", "row_omitted"])
def test_the_step_still_refuses_a_benchmark_missing_for_real(step, monkeypatch,
                                                            capsys, shape):
    truth, report = step
    yahoo = truth.copy()
    if shape == "nan_row":
        yahoo.iloc[-1] = np.nan
    else:
        yahoo = yahoo.iloc[:-1]
    _serve(monkeypatch, yahoo, truth.iloc[:-1], "norgate")   # Norgate lacks it too

    assert rp.main() == 1

    err = capsys.readouterr().err
    assert "SPY benchmark unavailable: 1 session(s) lack a finite positive close" in err
    assert "2026-11-23" in err
    assert report.read_text(encoding="utf-8") == "previous verified report"


# ---------------------------------------------------------------------------
# run_portfolio: the provenance key describes the SPY line that is written
# (review of pull request 17, 2026-11-24)
# ---------------------------------------------------------------------------
def _fill_record(dates: list[str]) -> dict:
    return {"filled": {"SPY": {"dates": list(dates), "overlap_bars": 10,
                               "max_rel_diff": 0.0}},
            "declined": {}, "rule": "the rule, as text"}


@pytest.mark.parametrize("panel_end, inside, outside", [
    ("2026-11-20", "2026-11-20", "2026-11-23"),   # the live week
    ("2026-11-30", "2026-11-30", "2026-12-01"),   # month boundary
    ("2026-12-31", "2026-12-31", "2027-01-04"),   # year boundary
])
def test_only_filled_sessions_in_the_panel_are_published(panel_end, inside, outside):
    panel = _sessions("2026-11-02", panel_end)
    assert pd.Timestamp(inside) in panel and pd.Timestamp(outside) not in panel

    # A fill wholly inside the panel is published as recorded.
    whole = _fill_record([inside])
    assert rp.spy_tail_in_panel(whole, panel) == whole
    # A fill after the panel's last date is not in the SPY line.
    assert rp.spy_tail_in_panel(_fill_record([outside]), panel) is None
    # A fill that straddles the end keeps the session the line carries.
    kept = rp.spy_tail_in_panel(_fill_record([inside, outside]), panel)
    assert kept["filled"]["SPY"] == {"dates": [inside], "overlap_bars": 10,
                                     "max_rel_diff": 0.0}
    assert kept["rule"] == "the rule, as text"


def test_a_decline_or_no_record_publishes_nothing():
    panel = _sessions("2026-11-02", LIVE_SESSION)
    assert rp.spy_tail_in_panel(None, panel) is None
    declined = {"filled": {}, "declined": {"SPY": "two bases, not spliced"}}
    assert rp.spy_tail_in_panel(declined, panel) is None


@pytest.fixture
def book(site, tmp_path, monkeypatch):
    """run_portfolio.main() end to end on a small synthetic panel: the real
    run_portfolio, every variant, and the real download_spy_close on a fixed
    clock. Returns ``install(panel_end) -> (spy_truth, report)``; the SPY
    truth always runs through Monday 23 November 2026."""
    report = tmp_path / "portfolio_construction.json"
    monkeypatch.setattr(rp, "OUT_PATH", report)
    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)       # no ma200_sweep.json here
    monkeypatch.setattr(rp, "PROJECT_ROOT", tmp_path)   # main() prints OUT_PATH relative to it
    monkeypatch.setattr(rp, "download_spy_close",
                        lambda s, e: bt.download_spy_close(s, e, now_utc=LIVE_NOW))

    def install(panel_end: str) -> tuple[pd.Series, Path]:
        idx = _sessions("2025-11-03", panel_end)
        names = ["AAA", "BBB", "CCC", "DDD"]
        closes = pd.DataFrame(
            {n: np.linspace(100.0, 110.0 + 5.0 * i, len(idx))
             for i, n in enumerate(names)}, index=idx)
        breadths = pd.DataFrame(
            {n: 0.35 + 0.10 * i for i, n in enumerate(names)}, index=idx)
        monkeypatch.setattr(rp, "build_panels", lambda: (closes, breadths, names))
        return _series(_sessions("2025-11-03", LIVE_SESSION)), report

    return install


@pytest.mark.parametrize("shape", ["nan_row", "row_omitted"])
def test_a_filled_session_is_written_and_reads_back_as_json(book, monkeypatch,
                                                           shape):
    """The write of a non-null key first runs in production on a morning
    Yahoo withholds the close. json.dumps is called with no ``default``, so a
    numpy scalar in the record would fail the step on exactly that morning."""
    truth, report = book(LIVE_SESSION)
    yahoo = truth.copy()
    if shape == "nan_row":
        yahoo.iloc[-1] = np.nan
    else:
        yahoo = yahoo.iloc[:-1]
    _serve(monkeypatch, yahoo, truth.copy(), "norgate")

    assert rp.main() == 0

    payload = json.loads(report.read_text(encoding="utf-8"))
    tail = payload["spy_benchmark_tail_from_norgate"]
    assert tail["filled"] == {"SPY": {"dates": [LIVE_SESSION],
                                      "overlap_bars": cb.NORGATE_TAIL_OVERLAP_BARS,
                                      "max_rel_diff": 0.0}}
    assert tail["declined"] == {} and isinstance(tail["rule"], str)
    spy = payload["results"]["spy_buy_hold"]
    assert spy["dates"][-1] == LIVE_SESSION
    # The line's last point is the filled close, not a carried one.
    assert spy["equity"][-1] == round(truth.iloc[-1] / truth.loc[spy["dates"][0]], 4)


def test_an_ordinary_run_writes_a_null_key(book, monkeypatch):
    truth, report = book(LIVE_SESSION)
    _serve(monkeypatch, truth, source="norgate")  # fetch_ohlc still fails the test

    assert rp.main() == 0

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["spy_benchmark_tail_from_norgate"] is None
    assert payload["results"]["spy_buy_hold"]["dates"][-1] == LIVE_SESSION


@pytest.mark.parametrize("norgate_has_monday", [True, False])
def test_a_session_after_the_panel_is_not_reported_in_the_spy_line(
        book, monkeypatch, capsys, norgate_has_monday):
    """The panel is capped at Friday 20 November and Yahoo's SPY frame ends
    there too. The fetch window runs five days on, so the fetch fills (or
    declines) Monday 23 November, and the reindex onto the panel drops it.
    The report must not say a Norgate close is in the SPY line."""
    truth, report = book("2026-11-20")
    yahoo = truth.iloc[:-1]
    norgate = truth.copy() if norgate_has_monday else truth.iloc[:-1]
    _serve(monkeypatch, yahoo, norgate, "norgate")

    assert rp.main() == 0

    said = capsys.readouterr().out
    if norgate_has_monday:
        assert f"Norgate tail re-source SPY (benchmark): {LIVE_SESSION}" in said
    else:
        assert "DECLINED: Norgate carries no session after 2026-11-20 either" in said
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["spy_benchmark_tail_from_norgate"] is None
    assert payload["results"]["spy_buy_hold"]["dates"][-1] == "2026-11-20"
