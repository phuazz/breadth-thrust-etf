"""159801.SZ vendor retraction and freshness handling (2026-10-13).

THE EVENT. yfinance served 159801.SZ's 2026-10-12 close (the probe saw the
bar at 14:17 UTC, after the 07:00 UTC Shenzhen close) and by 00:05 UTC on
10-13 had replaced it with a row carrying open and volume but a null close and
adjusted close. The last valid close was 10-09, about CNY 0.991.

WHAT THESE TESTS PIN.

  1. The engine's FX step may carry 159801.SZ across a CLOSED Shenzhen venue
     (Golden Week, New Year) but never across a Shenzhen session the vendor
     did not serve: that cell stays blank on the trailing edge, so the 10-09
     close is never relabelled 10-12 and live_targets' coverage floor HOLDs
     sleeve C. Interior gaps keep the historical carry (the record is not
     restated).
  2. The probe records settled bars only, with their close, and keeps an
     in-progress bar out of last_bar; the tripwire carries the value that was
     withdrawn.
  3. The freshness verdict carries its own expiry, which agrees with
     verdict_has_lapsed and reaches the reduced public page.

Every fixture is synthetic and isolated: no committed cache, log or output is
read for writing. Sessions come from pandas_market_calendars, never from a
weekday count. Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_simple_page as bsp  # noqa: E402
import check_vendor_probe as cv  # noqa: E402
import live_targets as lt  # noqa: E402
import probe_vendor_availability as pv  # noqa: E402
import run_thematic_rotation as th  # noqa: E402
import strategy_freshness as sf  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

TK = "159801.SZ"
FX = 7.0           # CNY per USD, flat so every expected value is native / 7


def _sessions(venue: str, start: str, end: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        get_calendar(venue).schedule(start_date=start, end_date=end).index
    ).normalize()


class _FakeYF:
    """Stands in for the module-level ``yf`` the FX step calls for CNY=X."""

    def __init__(self, index):
        self._fx = pd.DataFrame({"Close": pd.Series(FX, index=index)})

    def download(self, *_a, **_kw):
        return self._fx


def _frame(start: str, end: str, withheld=()):
    """The raw union frame a yfinance batch returns: NYSE rows priced for the
    cash proxy, the Shenzhen line priced on its own sessions, and a withheld
    session present as a placeholder row with a null close."""
    nyse = _sessions("NYSE", start, end)
    sse = _sessions("SSE", start, end)
    native = pd.Series(1.0 + 0.001 * np.arange(len(sse)), index=sse)
    withheld = pd.DatetimeIndex(pd.to_datetime(list(withheld)))
    idx = nyse.union(sse)
    df = pd.DataFrame({th.CASH_PROXY: pd.Series(80.0, index=nyse),
                       TK: native.drop(withheld)}).reindex(idx)
    return df, native


def _convert(monkeypatch, df):
    monkeypatch.setattr(th, "yf", _FakeYF(df.index.union(
        pd.date_range(df.index.min(), df.index.max(), freq="D"))))
    return th._fx_convert_to_usd(df.copy())


# ---------------------------------------------------------------------------
# 1. The engine's FX step
# ---------------------------------------------------------------------------

def test_null_placeholder_on_a_shenzhen_session_stays_blank(monkeypatch, capsys):
    """The 2026-10-12 shape: placeholder row, null close, Shenzhen open."""
    df, native = _frame("2026-09-01", "2026-10-12", withheld=["2026-10-12"])
    out = _convert(monkeypatch, df)
    d12, d09 = pd.Timestamp("2026-10-12"), pd.Timestamp("2026-10-09")
    assert d12 in _sessions("SSE", "2026-10-12", "2026-10-12")
    assert pd.isna(out.loc[d12, TK]), "10-09 was relabelled as 10-12"
    assert out.loc[d09, TK] == pytest.approx(native[d09] / FX)
    assert "left blank, not carried forward" in capsys.readouterr().out


def test_recovery_prices_the_restored_bar(monkeypatch):
    df, native = _frame("2026-09-01", "2026-10-12")
    out = _convert(monkeypatch, df)
    d12 = pd.Timestamp("2026-10-12")
    assert out.loc[d12, TK] == pytest.approx(native[d12] / FX)


def test_golden_week_carry_survives_across_the_month_boundary(monkeypatch):
    """Shenzhen closed 10-01..10-07: carrying 09-30 is a valuation, not a hole."""
    df, native = _frame("2026-09-01", "2026-10-07")
    out = _convert(monkeypatch, df)
    holiday = _sessions("NYSE", "2026-10-01", "2026-10-07")
    assert len(holiday) and not len(_sessions("SSE", "2026-10-01", "2026-10-07"))
    for d in holiday:
        assert out.loc[d, TK] == pytest.approx(native[pd.Timestamp("2026-09-30")] / FX)


def test_first_session_after_golden_week_unserved_blanks_only_that_row(monkeypatch):
    df, native = _frame("2026-09-01", "2026-10-08", withheld=["2026-10-08"])
    out = _convert(monkeypatch, df)
    assert pd.notna(out.loc[pd.Timestamp("2026-10-07"), TK])
    assert pd.isna(out.loc[pd.Timestamp("2026-10-08"), TK])


def test_year_boundary_trailing_hole_is_blank(monkeypatch):
    """Shenzhen and NYSE both shut on 2027-01-01; 01-04 is a Shenzhen session."""
    df, _ = _frame("2026-12-01", "2027-01-04", withheld=["2027-01-04"])
    out = _convert(monkeypatch, df)
    assert pd.notna(out.loc[pd.Timestamp("2026-12-31"), TK])
    assert pd.isna(out.loc[pd.Timestamp("2027-01-04"), TK])


def test_interior_gap_keeps_the_historical_carry(monkeypatch):
    """A gap a later served bar brackets is NOT restated (2025-10-24 is the
    one such Shenzhen session in the record since 2020-02). Pinned at the
    year boundary: 2027-01-04 withheld, 01-05 served."""
    df, native = _frame("2026-12-01", "2027-01-05", withheld=["2027-01-04"])
    out = _convert(monkeypatch, df)
    assert out.loc[pd.Timestamp("2027-01-04"), TK] == pytest.approx(
        native[pd.Timestamp("2026-12-31")] / FX)
    assert out.loc[pd.Timestamp("2027-01-05"), TK] == pytest.approx(
        native[pd.Timestamp("2027-01-05")] / FX)


def test_mixed_calendar_unserved_session_on_a_us_holiday(monkeypatch):
    """Thanksgiving: NYSE shut, Shenzhen open. A Shenzhen session missed on a
    day the NYSE frame never shows still blanks the next NYSE row."""
    nyse = _sessions("NYSE", "2026-11-26", "2026-11-26")
    sse = _sessions("SSE", "2026-11-26", "2026-11-26")
    assert not len(nyse) and len(sse), "fixture assumes a US-only holiday"
    df, native = _frame("2026-10-15", "2026-11-27",
                        withheld=["2026-11-26", "2026-11-27"])
    out = _convert(monkeypatch, df)
    assert pd.isna(out.loc[pd.Timestamp("2026-11-27"), TK])
    df, native = _frame("2026-10-15", "2026-11-27", withheld=["2026-11-26"])
    out = _convert(monkeypatch, df)
    assert out.loc[pd.Timestamp("2026-11-27"), TK] == pytest.approx(
        native[pd.Timestamp("2026-11-27")] / FX)


def test_the_blank_cell_holds_sleeve_c_at_the_coverage_floor(monkeypatch):
    """End to end on the engine's own functions: FX step -> signal -> rank.
    The retracted bar must produce HOLD naming 159801.SZ, never READY on the
    carried 10-09 close; the restored bar must produce READY."""
    now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)

    def ranked(withheld):
        df, _ = _frame("2025-08-01", "2026-10-12", withheld=withheld)
        df["ARKK"] = df[th.CASH_PROXY] * 0 + np.linspace(50, 60, len(df))
        closes = _convert(monkeypatch, df)
        closes = closes[closes[th.CASH_PROXY].notna()]
        signal = th.compute_signal(closes[[TK, "ARKK"]])
        return lt._rank(signal, lambda r: r.fillna(0) * 0 + 0.5, "NYSE",
                        now, "C")

    held = ranked(["2026-10-12"])
    assert held["status"] == "HOLD"
    assert held["missing_signal_inputs"] == [TK]
    assert held["decision_session"] == "2026-10-12"
    assert ranked([])["status"] == "READY"


# ---------------------------------------------------------------------------
# 2. The probe and the retraction tripwire
# ---------------------------------------------------------------------------

def _probe_download(bars: dict[str, dict[str, float]]):
    """Batch and single calls both answer from ``bars`` (ticker -> date ->
    close, NaN for a placeholder row). Unlisted probe tickers get SPY's."""
    def download(tickers, **_kw):
        cols = {}
        for t in tickers:
            series = bars.get(t, bars["SPY"])
            cols[("Close", t)] = pd.Series(
                list(series.values()), index=pd.to_datetime(list(series)))
        return pd.DataFrame(cols)
    return download


def _probe_rows(now, bars, monkeypatch):
    monkeypatch.setattr(pv, "REFETCH_PAUSE_SECONDS", 0.0)
    return {r["ticker"]: r
            for r in pv.probe(now_utc=now, download=_probe_download(bars))["rows"]}


BTC = {str(d.date()): 60000.0 for d in pd.date_range("2026-10-05", "2026-10-13")}


def test_probe_reads_a_timestamp_only_row_as_no_bar(monkeypatch):
    """00:05 UTC 10-13: the 10-12 row exists with a null close."""
    bars = {"SPY": {"2026-10-08": 600.0, "2026-10-09": 601.0},
            "BTC-USD": BTC,
            TK: {"2026-10-08": 0.995, "2026-10-09": 0.991,
                 "2026-10-12": float("nan")}}
    rows = _probe_rows(datetime(2026, 10, 13, 0, 5, tzinfo=timezone.utc),
                       bars, monkeypatch)
    assert rows[TK]["last_bar"] == "2026-10-09"
    assert rows[TK]["last_close"] == pytest.approx(0.991)
    assert rows[TK]["last_completed_session"] == "2026-10-12"
    assert rows[TK]["sessions_behind"] == 1
    assert "in_progress_bar" not in rows[TK]


def test_probe_keeps_an_incomplete_session_out_of_last_bar(monkeypatch):
    """04:05 UTC 10-12 is the Shenzhen lunch break, 14:17 UTC is mid-session
    in New York and Frankfurt: those bars were logged as last_bar, behind -1."""
    bars = {"SPY": {"2026-10-09": 601.0, "2026-10-12": 603.0},
            "BTC-USD": BTC,
            TK: {"2026-10-09": 0.991, "2026-10-12": 0.97}}
    lunch = _probe_rows(datetime(2026, 10, 12, 4, 5, tzinfo=timezone.utc),
                        bars, monkeypatch)
    assert lunch[TK]["last_bar"] == "2026-10-09"
    assert lunch[TK]["in_progress_bar"] == "2026-10-12"
    assert lunch[TK]["sessions_behind"] == 0
    mid = _probe_rows(datetime(2026, 10, 12, 14, 17, tzinfo=timezone.utc),
                      bars, monkeypatch)
    assert mid["SPY"]["last_bar"] == "2026-10-09"
    assert mid["SPY"]["in_progress_bar"] == "2026-10-12"
    assert mid["SPY"]["sessions_behind"] == 0
    # Shenzhen closed at 07:00 UTC, so by 14:17 its 10-12 bar IS settled.
    assert mid[TK]["last_bar"] == "2026-10-12"
    assert mid[TK]["last_close"] == pytest.approx(0.97)
    assert "in_progress_bar" not in mid[TK]


def _rec(stamp, last_bar, close, lcs):
    return {"probed_at_utc": stamp, "rows": [
        {"ticker": TK, "venue": "SSE", "last_bar": last_bar,
         "last_close": close, "last_completed_session": lcs,
         "sessions_behind": 0 if last_bar == lcs else 1}]}


def test_retraction_records_the_value_that_was_withdrawn():
    history = [_rec("2026-10-12T14:17:31+00:00", "2026-10-12", 0.966, "2026-10-12"),
               _rec("2026-10-13T00:05:10+00:00", "2026-10-09", 0.991, "2026-10-12")]
    (r,) = cv.detect_retractions(history)
    assert (r["was"], r["now"]) == ("2026-10-12", "2026-10-09")
    assert r["was_close"] == pytest.approx(0.966)
    assert r["now_close"] == pytest.approx(0.991)
    assert r["was_seen_at"] == "2026-10-12T14:17:31+00:00"
    assert r["routine"] is False      # SSE has no measured overnight cycle
    detail = cv._retraction_detail([r])
    assert "2026-10-12 close 0.966" in detail and "close 0.991" in detail


def test_restoration_clears_the_tripwire():
    history = [_rec("2026-10-12T14:17:31+00:00", "2026-10-12", 0.966, "2026-10-12"),
               _rec("2026-10-13T00:05:10+00:00", "2026-10-09", 0.991, "2026-10-12"),
               _rec("2026-10-13T06:05:00+00:00", "2026-10-12", 0.966, "2026-10-12")]
    assert cv.detect_retractions(history) == []


def test_rows_logged_before_values_were_recorded_still_compare():
    old = {"probed_at_utc": "2026-10-12T14:17:31+00:00", "rows": [
        {"ticker": TK, "venue": "SSE", "last_bar": "2026-10-12",
         "last_completed_session": "2026-10-12", "sessions_behind": 0}]}
    new = _rec("2026-10-13T00:05:10+00:00", "2026-10-09", 0.991, "2026-10-12")
    (r,) = cv.detect_retractions([old, new])
    assert r["was_close"] is None and r["now_close"] == pytest.approx(0.991)


# ---------------------------------------------------------------------------
# 3. Freshness verdict expiry and publication
# ---------------------------------------------------------------------------

def _fresh_rows(seen: str):
    return [{"sleeve": "B", "venue": "NYSE", "venue_last_session": seen,
             "status": "current"},
            {"sleeve": "D", "venue": "XETR", "venue_last_session": seen,
             "status": "current"}]


def test_verdict_expires_at_the_first_close_after_the_sessions_it_saw():
    """Computed Sun 2026-10-11 over Friday's closes: Xetra's Monday close
    (15:30 UTC, CEST) comes before New York's (20:00 UTC, EDT)."""
    until = pd.Timestamp(sf.verdict_valid_until(_fresh_rows("2026-10-09")))
    assert until == pd.Timestamp("2026-10-12 15:30", tz="UTC")
    report = {"strategies": _fresh_rows("2026-10-09")}
    before = until.to_pydatetime() - timedelta(seconds=1)
    assert sf.verdict_has_lapsed(report, now_utc=before) is False
    assert sf.verdict_has_lapsed(report, now_utc=until.to_pydatetime()) is True


def test_verdict_expiry_skips_holidays_across_the_year_boundary():
    """New York only, seen 2026-12-31: 2027-01-01 is a holiday, so the verdict
    holds until the 2027-01-04 close (21:00 UTC, EST)."""
    rows = [{"sleeve": "B", "venue": "NYSE", "venue_last_session": "2026-12-31"}]
    assert pd.Timestamp(sf.verdict_valid_until(rows)) == pd.Timestamp(
        "2027-01-04 21:00", tz="UTC")


def test_verdict_expiry_month_boundary_and_early_close():
    """Seen Wed 2026-11-25: Thanksgiving shut, Friday 11-27 closes early."""
    rows = [{"sleeve": "B", "venue": "NYSE", "venue_last_session": "2026-11-25"}]
    assert pd.Timestamp(sf.verdict_valid_until(rows)) == pd.Timestamp(
        "2026-11-27 18:00", tz="UTC")


def test_verdict_expiry_fails_closed_on_an_unknown_venue():
    rows = [{"sleeve": "X", "venue": "NOT-A-VENUE",
             "venue_last_session": "2026-10-09"}]
    assert sf.verdict_valid_until(rows) is None


def test_build_publishes_an_expiry_that_agrees_with_the_lapse_check():
    now = datetime(2026, 10, 11, 10, 59, tzinfo=timezone.utc)
    report = sf.build(now_utc=now)
    until = report["verdict_valid_until_utc"]
    assert until is not None
    t = datetime.fromisoformat(until)
    assert t > now
    assert sf.verdict_has_lapsed(report, now_utc=t - timedelta(seconds=1)) is False
    assert sf.verdict_has_lapsed(report, now_utc=t) is True


def test_reduced_page_carries_the_expiry_through_its_allow_list():
    blob = {"computed_at_utc": "2026-10-11T10:59:00+00:00",
            "verdict_valid_until_utc": "2026-10-12T15:30:00+00:00",
            "all_current": True,
            "strategies": [{"sleeve": "C", "status": "current",
                            "source": "price cache (thematic ETFs)"}]}
    out = bsp.filter_freshness(blob)
    assert out["verdict_valid_until_utc"] == "2026-10-12T15:30:00+00:00"
    assert "source" not in out["strategies"][0]


def test_both_pages_expire_the_current_claim_in_the_readers_clock():
    """The renderers are client-side; pin that both read the expiry and that
    the reduced page no longer calls unmeasured rows current."""
    dash = (ROOT / "template.html").read_text(encoding="utf-8")
    assert "function freshnessVerdictLapsed(f)" in dash
    assert "verdict_valid_until_utc" in dash
    simple = (ROOT / "simple_template.html").read_text(encoding="utf-8")
    assert "verdict_valid_until_utc" in simple
    assert "f.strategies.every(s => s.status === 'current')" in simple
