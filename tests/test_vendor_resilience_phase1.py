"""Vendor-resilience Phase 1 acceptance matrix (2026-10-15).

Offline: frozen clocks, an injected provider adapter, an injected mail
outcome, temporary state only. Nothing here touches the network, SMTP, a
production cache or a committed artefact; the module fixture hashes the
protected artefacts before and after and fails if any moved.

Sections follow the acceptance matrix in handoffs/vendor-resilience-phase1-
review.md. Python datetime months are 1-indexed (January = 1). Every session
is the venue calendar's answer, never a weekday count.

Three silent failures this file exists to catch:
  1. an intraday (unfinished) bar read as a completed session;
  2. a preserved cache or published export read as fresh provider evidence;
  3. an attempted notification read as a delivered one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import alert_policy as ap  # noqa: E402
import check_vendor_probe as cvp  # noqa: E402
import probe_vendor_availability as pv  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

PROTECTED = [
    "data/vendor_availability_log.jsonl",
    "data/ops_alert_log.jsonl",
    "data/live_targets.json",
    "docs/component_delivery.json",
    "docs/factsheet_published.json",
]


def _hashes():
    out = {}
    for rel in PROTECTED:
        p = ROOT / rel
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    return out


@pytest.fixture(scope="module", autouse=True)
def protected_artefacts_unchanged():
    before = _hashes()
    yield
    assert _hashes() == before, "a protected artefact changed during the tests"


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(pv, "REFETCH_PAUSE_SECONDS", 0.0)
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return [pd.Timestamp(d).date().isoformat() for d in sched.index]


def series(bars):
    """bars: {iso: value} or [iso,...] (value 100)."""
    if isinstance(bars, (list, tuple)):
        bars = {d: 100.0 for d in bars}
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in bars])
    return pd.Series(list(bars.values()), index=idx, dtype=float)


def frame(data):
    cols = {("Close", tk): series(b) for tk, b in data.items()}
    return pd.DataFrame(cols)


class Provider:
    """An injected yfinance.download: ``batch`` answers multi-ticker calls,
    ``single`` answers the one-ticker re-ask (defaults to batch)."""

    def __init__(self, batch, single=None):
        self.batch = batch
        self.single = batch if single is None else single
        self.calls = []

    def __call__(self, tickers, **_kw):
        tickers = list(tickers)
        self.calls.append(tuple(tickers))
        src = self.batch if len(tickers) > 1 else self.single
        if callable(src):
            return src(tickers)
        return frame({t: src.get(t, {}) for t in tickers})

    def singles(self, tk):
        return sum(1 for c in self.calls if c == (tk,))


# ===========================================================================
# 1. The required session comes from the venue's own close schedule
# ===========================================================================
@pytest.mark.parametrize("now, venue, expected", [
    # NYSE, Wednesday 2026-10-14, close 16:00 EDT = 20:00 UTC.
    (utc(2026, 10, 14, 19, 59, 59), "NYSE", "2026-10-13"),   # before close
    (utc(2026, 10, 14, 20, 0, 0), "NYSE", "2026-10-14"),     # exactly at close
    (utc(2026, 10, 14, 20, 0, 1), "NYSE", "2026-10-14"),     # after close
    # Weekend: Saturday and Sunday read Friday.
    (utc(2026, 10, 17, 12), "NYSE", "2026-10-16"),
    (utc(2026, 10, 18, 23), "NYSE", "2026-10-16"),
    # Holiday: Thanksgiving Thursday 2026-11-26 is closed.
    (utc(2026, 11, 26, 23), "NYSE", "2026-11-25"),
    # Early close: Friday 2026-11-27 closes 13:00 EST = 18:00 UTC.
    (utc(2026, 11, 27, 17, 59), "NYSE", "2026-11-25"),
    (utc(2026, 11, 27, 18, 0), "NYSE", "2026-11-27"),
    # DST, Europe: Xetra closes 15:30 UTC under CEST, 16:30 UTC from 26 Oct.
    (utc(2026, 10, 23, 15, 30), "XETR", "2026-10-23"),
    (utc(2026, 10, 26, 16, 0), "XETR", "2026-10-23"),
    (utc(2026, 10, 26, 16, 30), "XETR", "2026-10-26"),
    # DST, US: from Monday 2 November NYSE closes 21:00 UTC.
    (utc(2026, 11, 2, 20, 30), "NYSE", "2026-10-30"),
    (utc(2026, 11, 2, 21, 0), "NYSE", "2026-11-02"),
    # Month boundary: Sunday 1 November reads Friday 30 October.
    (utc(2026, 11, 1, 12), "NYSE", "2026-10-30"),
    # Year boundary: Xetra shut 31 Dec and 1 Jan; 30 Dec closes early 13:00 UTC.
    (utc(2026, 12, 30, 12, 59), "XETR", "2026-12-29"),
    (utc(2026, 12, 30, 13, 0), "XETR", "2026-12-30"),
    (utc(2027, 1, 1, 12), "XETR", "2026-12-30"),
    # Golden Week: SSE shut 1-7 October 2026 while NYSE trades.
    (utc(2026, 10, 7, 12), "SSE", "2026-09-30"),
    (utc(2026, 10, 7, 12), "NYSE", "2026-10-06"),
])
def test_required_session_is_the_venue_close_schedule(now, venue, expected):
    assert pv.required_session(venue, now).date().isoformat() == expected


def test_shenzhen_line_rides_the_sse_calendar_and_it_matches_xshg():
    """159801.SZ is probed on SSE. The repository's other contract for .SZ
    (export_holdings_prices.VENUE_BY_SUFFIX) names XSHG; both calendars must
    agree on every 2026 session or one of them is wrong. Never NYSE."""
    venues = {t: v for t, v, _ in pv.PROBES}
    assert venues["159801.SZ"] == "SSE"
    from export_holdings_prices import VENUE_BY_SUFFIX
    assert VENUE_BY_SUFFIX[".SZ"] == "XSHG"
    assert sessions("SSE", "2026-01-01", "2026-12-31") == \
        sessions("XSHG", "2026-01-01", "2026-12-31")
    assert sessions("SSE", "2026-10-01", "2026-10-07") == []
    assert sessions("NYSE", "2026-10-01", "2026-10-07") != []


def test_sse_beyond_its_published_holidays_is_unknown_not_current():
    """pandas_market_calendars lists announced SSE/XSHG holidays only through
    2026 (last: 2026-10-07). Beyond that XSHG invents sessions (2027-01-01)
    and SSE's closures are rule-derived, not announced, so a required
    session there is UNKNOWN."""
    with pytest.raises(pv.CalendarUnknown):
        pv.required_session("SSE", utc(2027, 1, 4, 12))
    ev = pv.venue_evidence(series(["2026-12-31", "2027-01-04"]), "SSE",
                           utc(2027, 1, 4, 12))
    assert ev["calendar_error"] and ev["required_day_bar_present"] is None
    assert ev["last_bar"] is None and ev["sessions_behind"] is None
    # The rule-based venues are unaffected across the same year boundary.
    assert pv.required_session("XETR", utc(2027, 1, 4, 17)).date().isoformat() == "2027-01-04"


def test_calendar_failure_produces_no_current_verdict(monkeypatch):
    def boom(_venue):
        raise RuntimeError("calendar library missing")
    monkeypatch.setattr(pv, "_venue_cal", boom)
    ev = pv.venue_evidence(series(["2026-10-14"]), "NYSE", utc(2026, 10, 15, 3))
    assert ev["calendar_error"]
    assert ev["required_day_bar_present"] is None
    row = {"ticker": "SPY", "venue": "NYSE", **ev}
    assert pv.decision_input_complete(row) is None
    assert pv.session_evidence(row, "2026-10-14") is None
    with pytest.raises(pv.CalendarUnknown):
        pv.required_session("NOT-A-VENUE", utc(2026, 10, 15, 3))


def test_a_calendar_failure_row_is_neither_a_withdrawal_nor_a_high_water_mark():
    history = [
        {"probed_at_utc": "2026-10-14T21:00:00+00:00", "rows": [
            {"ticker": "SPY", "venue": "NYSE", "last_bar": "2026-10-14",
             "last_completed_session": "2026-10-14"}]},
        {"schema": 2, "probed_at_utc": "2026-10-15T03:00:00+00:00", "rows": [
            {"ticker": "SPY", "venue": "NYSE", "last_bar": None,
             "calendar_error": "x", "last_completed_session": None}]},
    ]
    assert cvp.detect_retractions(history) == []


def test_the_guard_names_undetermined_lines(tmp_path):
    log = tmp_path / "log.jsonl"
    rec = {"schema": 2, "probed_at_utc": "2026-10-15T03:00:00+00:00", "rows": [
        {"ticker": "SPY", "venue": "NYSE", "last_bar": "2026-10-14",
         "last_completed_session": "2026-10-14", "required_day_bar_present": True,
         "missing_completed_days": [], "checked_days": ["2026-10-14"]},
        {"ticker": "159801.SZ", "venue": "SSE", "last_bar": None,
         "calendar_error": "x", "required_day_bar_present": None,
         "missing_completed_days": None, "checked_days": []}]}
    log.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    verdict = cvp.evaluate(log, utc(2026, 10, 15, 3, 10))
    assert verdict["undetermined"] == ["159801.SZ"]
    assert verdict["decision_inputs_complete"] is False
    assert "verdict unknown" in verdict["summary"]


# ===========================================================================
# 2. The required completed bar decides; an unfinished row never does
# ===========================================================================
NOW_FIXTURE = utc(2026, 10, 15, 2, 58, 47)      # fixture 8cadbb2's probe clock


def test_required_bar_with_a_later_intraday_bar_is_current_on_the_required_bar():
    days = sessions("SSE", "2026-09-25", "2026-10-15")      # includes 10-15
    ev = pv.venue_evidence(series(days), "SSE", NOW_FIXTURE)
    assert ev["last_completed_session"] == "2026-10-14"
    assert ev["last_bar"] == "2026-10-14"
    assert ev["required_day_bar_present"] is True
    assert ev["sessions_behind"] == 0
    assert ev["unsettled_bar"] == "2026-10-15"
    assert ev["missing_completed_days"] == []


@pytest.mark.parametrize("bad", [None, float("nan"), 0.0, -1.0,
                                 float("inf"), float("-inf")])
def test_absent_or_invalid_required_bar_stays_unavailable_despite_the_same_intraday_bar(bad):
    days = sessions("SSE", "2026-09-25", "2026-10-13")
    bars = {d: 100.0 for d in days}
    if bad is not None:
        bars["2026-10-14"] = bad
    bars["2026-10-15"] = 101.0                                # unfinished row
    ev = pv.venue_evidence(series(bars), "SSE", NOW_FIXTURE)
    assert ev["required_day_bar_present"] is False
    assert ev["last_bar"] == "2026-10-13"
    assert ev["sessions_behind"] == 1
    assert ev["missing_completed_days"] == ["2026-10-14"]
    assert ev["unsettled_bar"] == "2026-10-15"
    if bad is not None and bad == bad:          # not NaN: a rejected row
        assert ev["invalid_days"] == ["2026-10-14"]


def test_missing_required_bar_with_older_rows_is_explicit_missing_evidence():
    ev = pv.venue_evidence(series(["2026-10-08", "2026-10-09"]), "SSE", NOW_FIXTURE)
    assert ev["required_day_bar_present"] is False
    assert ev["missing_completed_days"] == ["2026-10-12", "2026-10-13", "2026-10-14"]
    assert ev["checked_days"] == ["2026-10-08", "2026-10-09", "2026-10-12",
                                  "2026-10-13", "2026-10-14"]
    assert ev["sessions_behind"] == 3


def test_no_row_ever_reports_negative_lag():
    """The 8cadbb2 row read 159801.SZ as -1 behind (an unfinished 10-15 bar)."""
    for bars in (["2026-10-15"], ["2026-10-14", "2026-10-15", "2026-10-16"],
                 sessions("SSE", "2026-10-01", "2026-10-15")):
        ev = pv.venue_evidence(series(bars), "SSE", NOW_FIXTURE)
        assert ev["sessions_behind"] is None or ev["sessions_behind"] >= 0
        assert ev["last_bar"] is None or ev["last_bar"] <= "2026-10-14"


# ===========================================================================
# 3. Value validity, frame shapes, duplicates, refetch
# ===========================================================================
def test_empty_frame_and_missing_close_column_are_unserved_not_a_crash():
    assert pv._close_frame(pd.DataFrame(), ["SPY"]).empty
    no_close = pd.DataFrame({("Open", "SPY"): series(["2026-10-14"])})
    assert pv._close_frame(no_close, ["SPY"]).empty
    flat = pd.DataFrame({"Open": series(["2026-10-14"])})
    assert pv._close_frame(flat, ["SPY"]).empty
    ev = pv.venue_evidence(pd.Series(dtype=float), "NYSE", utc(2026, 10, 15, 3))
    assert ev["last_bar"] is None and ev["required_day_bar_present"] is False


def test_unsorted_dates_read_the_same_as_sorted():
    days = sessions("NYSE", "2026-10-05", "2026-10-14")
    a = pv.venue_evidence(series(days), "NYSE", utc(2026, 10, 15, 3))
    b = pv.venue_evidence(series(list(reversed(days))), "NYSE", utc(2026, 10, 15, 3))
    assert a == b and a["required_day_bar_present"] is True


@pytest.mark.parametrize("values, verdict", [
    ([100.0, 100.0], "present"),                 # identical duplicates
    ([100.0, float("nan")], "present"),          # NaN is absence, not a value
    ([100.0, 101.0], "ambiguous"),               # two valid values disagree
    ([100.0, 0.0], "ambiguous"),                 # a rejected value beside a valid one
    ([100.0, float("inf")], "ambiguous"),
    ([0.0, -1.0], "invalid"),
    ([float("nan"), float("nan")], "absent"),
])
def test_duplicate_labels_are_judged_conservatively(values, verdict):
    days = sessions("NYSE", "2026-10-05", "2026-10-13")
    idx = [pd.Timestamp(d) for d in days] + [pd.Timestamp("2026-10-14")] * len(values)
    s = pd.Series([100.0] * len(days) + values, index=pd.DatetimeIndex(idx))
    ev = pv.venue_evidence(s, "NYSE", utc(2026, 10, 15, 3))
    assert ev["required_day_bar_present"] is (verdict == "present")
    assert ("2026-10-14" in ev["ambiguous_days"]) is (verdict == "ambiguous")
    assert ("2026-10-14" in ev["invalid_days"]) is (verdict == "invalid")


def test_timezone_aware_daily_labels_keep_their_own_date():
    """A Shanghai label 2026-10-14 00:00+08:00 is the 14 October session;
    converting to UTC first would call it 13 October."""
    days = sessions("SSE", "2026-10-08", "2026-10-14")
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days]).tz_localize("Asia/Shanghai")
    ev = pv.venue_evidence(pd.Series(100.0, index=idx), "SSE", NOW_FIXTURE)
    assert ev["last_bar"] == "2026-10-14" and ev["required_day_bar_present"] is True
    idx_ny = pd.DatetimeIndex([pd.Timestamp(d) for d in
                               sessions("NYSE", "2026-10-08", "2026-10-14")]
                              ).tz_localize("America/New_York")
    ev = pv.venue_evidence(pd.Series(100.0, index=idx_ny), "NYSE", NOW_FIXTURE)
    assert ev["last_bar"] == "2026-10-14"


def _all_lines(now_days):
    """Every probe line served through its own calendar's sessions."""
    out = {}
    for tk, venue, _ in pv.PROBES:
        if venue == "CRYPTO":
            out[tk] = [d.date().isoformat() for d in
                       pd.date_range("2026-10-01", now_days["CRYPTO"], freq="D")]
        else:
            out[tk] = sessions(venue, "2026-09-28", now_days[venue])
    return out


def test_refetch_fires_when_the_required_bar_is_missing_not_only_when_empty():
    batch = _all_lines({"NYSE": "2026-10-14", "XETR": "2026-10-14",
                        "SSE": "2026-10-14", "CRYPTO": "2026-10-14"})
    batch["SPY"] = sessions("NYSE", "2026-09-28", "2026-10-13")   # older rows only
    single = dict(batch)
    single["SPY"] = sessions("NYSE", "2026-09-28", "2026-10-14")  # re-ask serves it
    prov = Provider(batch, single)
    rows = {r["ticker"]: r for r in pv.probe(now_utc=NOW_FIXTURE, download=prov)["rows"]}
    assert prov.singles("SPY") == 1
    assert rows["SPY"]["required_day_bar_present"] is True
    assert rows["SPY"]["evidence"] == "refetch" and rows["SPY"]["refetched"] is True
    assert rows["SPY"]["refetch"] == {"attempts": 1, "result": "proved"}
    assert rows["XLF"]["evidence"] == "batch" and "refetch" not in rows["XLF"]


@pytest.mark.parametrize("bad", [float("nan"), 0.0, -2.0, float("inf"), float("-inf")])
def test_refetch_is_bounded_and_invalid_fallback_values_prove_nothing(bad):
    base = _all_lines({"NYSE": "2026-10-14", "XETR": "2026-10-14",
                       "SSE": "2026-10-14", "CRYPTO": "2026-10-14"})
    batch = {tk: {d: 100.0 for d in ds} for tk, ds in base.items()}
    batch["SPY"]["2026-10-14"] = bad
    prov = Provider(batch)                     # the re-ask returns the same answer
    rows = {r["ticker"]: r for r in pv.probe(now_utc=NOW_FIXTURE, download=prov)["rows"]}
    assert prov.singles("SPY") == pv.REFETCH_ATTEMPTS
    assert rows["SPY"]["required_day_bar_present"] is False
    assert rows["SPY"]["evidence"] == "batch"            # the batch answer, kept apart
    assert rows["SPY"]["refetch"]["result"] == "not_proved"
    assert "refetched" not in rows["SPY"]


def test_refetch_errors_are_recorded_as_errors():
    batch = _all_lines({"NYSE": "2026-10-14", "XETR": "2026-10-14",
                        "SSE": "2026-10-14", "CRYPTO": "2026-10-14"})
    batch["SPY"] = []

    def single(_t):
        raise RuntimeError("vendor down")
    prov = Provider(batch, single)
    rows = {r["ticker"]: r for r in pv.probe(now_utc=NOW_FIXTURE, download=prov)["rows"]}
    assert rows["SPY"]["last_bar"] is None and rows["SPY"]["evidence"] == "none"
    assert rows["SPY"]["refetch"]["result"] == "error"


def test_the_probe_writes_only_its_own_log_and_never_a_price_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "out" / "probe.jsonl"
    prov = Provider(_all_lines({"NYSE": "2026-10-14", "XETR": "2026-10-14",
                                "SSE": "2026-10-14", "CRYPTO": "2026-10-14"}))
    before = {p for p in tmp_path.rglob("*")}
    assert pv.main([], download=prov, now_utc=NOW_FIXTURE, log=log) == 0
    created = {p for p in tmp_path.rglob("*")} - before
    assert created == {log, log.parent}
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["schema"] == pv.PROBE_SCHEMA
    source = Path(pv.__file__).read_text(encoding="utf-8")
    for forbidden in ("to_parquet", "_ohlc_cache", "prices_cache", "to_csv"):
        assert forbidden not in source


# ===========================================================================
# 4. BTC unchanged; replay of fixture 8cadbb2 with synthetic provider frames
# ===========================================================================
def _fixture_row():
    for line in (ROOT / "data" / "vendor_availability_log.jsonl").read_text(
            encoding="utf-8").splitlines():
        if '"2026-10-15T02:58:47+00:00"' in line:
            return {r["ticker"]: r for r in json.loads(line)["rows"]}
    raise AssertionError("fixture row 8cadbb2 not in the committed log")


def _replay_provider(china_required_present: bool):
    data = {}
    for tk, venue, _ in pv.PROBES:
        if venue == "NYSE":
            data[tk] = sessions("NYSE", "2026-09-28", "2026-10-14")
        elif venue == "XETR":
            data[tk] = sessions("XETR", "2026-09-28", "2026-10-13")   # 10-14 withheld
        elif venue == "CRYPTO":
            days = [d.date().isoformat() for d in pd.date_range("2026-10-01", "2026-10-13")]
            data[tk] = days + ["2026-10-15"]                         # 10-14 absent
        else:
            days = sessions("SSE", "2026-09-28", "2026-10-13")
            data[tk] = days + (["2026-10-14"] if china_required_present else []) \
                + ["2026-10-15"]                                     # unfinished row
    return Provider(data)


@pytest.mark.parametrize("china_present", [True, False])
def test_replay_8cadbb2(china_present):
    fixture = _fixture_row()
    prov = _replay_provider(china_present)
    rec = pv.probe(now_utc=NOW_FIXTURE, download=prov)
    rows = {r["ticker"]: r for r in rec["rows"]}

    # Five Xetra lines one session stale, and the fresh evidence agrees with
    # the recorded lag while now proving the absence explicitly.
    xetra = [t for t, v, _ in pv.PROBES if v == "XETR"]
    assert len(xetra) == 5
    for tk in xetra:
        assert fixture[tk]["last_bar"] == rows[tk]["last_bar"] == "2026-10-13"
        assert fixture[tk]["sessions_behind"] == rows[tk]["sessions_behind"] == 1
        assert rows[tk]["required_day_bar_present"] is False
        assert rows[tk]["missing_completed_days"] == ["2026-10-14"]
        assert prov.singles(tk) == pv.REFETCH_ATTEMPTS

    # BTC: completed-UTC-day behaviour unchanged; same fields as recorded.
    for field in ("last_bar", "last_completed_session", "sessions_behind",
                  "required_day_bar_present", "missing_completed_days"):
        assert rows["BTC-USD"][field] == fixture["BTC-USD"][field], field
    assert rows["BTC-USD"]["refetched"] is True

    # China: the recorded row said last_bar 10-15, lag -1. Neither state of
    # the required 10-14 bar may reproduce that.
    assert fixture["159801.SZ"]["sessions_behind"] == -1
    cn = rows["159801.SZ"]
    assert cn["last_completed_session"] == "2026-10-14"
    assert cn["unsettled_bar"] == "2026-10-15"
    if china_present:
        assert (cn["last_bar"], cn["sessions_behind"], cn["required_day_bar_present"]) \
            == ("2026-10-14", 0, True)
    else:
        assert (cn["last_bar"], cn["sessions_behind"], cn["required_day_bar_present"]) \
            == ("2026-10-13", 1, False)
        assert cn["missing_completed_days"] == ["2026-10-14"]


def test_preserved_caches_and_published_exports_are_not_provider_evidence(tmp_path, monkeypatch):
    """A preserved cache and a published export that both hold the withheld
    10-14 Xetra bar change nothing: the probe reads only the provider adapter,
    and the policy only the probe row."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "EXV1.DE_ohlc_cache.json").write_text(
        json.dumps({"2026-10-14": 101.0}), encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "holdings_prices.json").write_text(
        json.dumps({"EXV1.DE": {"2026-10-14": 101.0}}), encoding="utf-8")
    rows = {r["ticker"]: r for r in pv.probe(
        now_utc=NOW_FIXTURE, download=_replay_provider(True))["rows"]}
    assert rows["EXV1.DE"]["required_day_bar_present"] is False
    assert pv.session_evidence(rows["EXV1.DE"], "2026-10-14") is False


# ===========================================================================
# 5. Legacy rows, mixed schemas, malformed history
# ===========================================================================
def test_legacy_rows_are_readable_but_prove_no_session():
    legacy_venue = {"ticker": "SPY", "venue": "NYSE", "last_bar": "2026-10-14",
                    "last_completed_session": "2026-10-14", "sessions_behind": 0}
    legacy_crypto = {"ticker": "BTC-USD", "venue": "CRYPTO", "last_bar": "2026-10-13",
                     "last_completed_session": "2026-10-13", "sessions_behind": 0,
                     "required_day_bar_present": True, "missing_completed_days": []}
    assert pv.session_evidence(legacy_venue, "2026-10-14") is None
    assert pv.session_evidence(legacy_crypto, "2026-10-13") is None
    assert pv.decision_input_complete(legacy_venue) is None
    assert pv.session_evidence(None, "2026-10-14") is None


def test_mixed_schema_history_and_unfinished_legacy_rows_are_not_withdrawals():
    """A legacy row whose last_bar was an UNFINISHED session (159801.SZ
    10-15 against completed 10-14) followed by a schema-2 row reading the
    completed 10-14 bar is not a retraction."""
    history = [
        {"probed_at_utc": "2026-10-15T02:58:47+00:00", "rows": [
            {"ticker": "159801.SZ", "venue": "SSE", "last_bar": "2026-10-15",
             "last_completed_session": "2026-10-14", "sessions_behind": -1}]},
        {"schema": 2, "probed_at_utc": "2026-10-15T09:00:00+00:00", "rows": [
            {"ticker": "159801.SZ", "venue": "SSE", "last_bar": "2026-10-15",
             "last_completed_session": "2026-10-15",
             "checked_days": ["2026-10-15"], "missing_completed_days": [],
             "required_day_bar_present": True}]},
        {"schema": 2, "probed_at_utc": "2026-10-16T02:58:00+00:00", "rows": [
            {"ticker": "159801.SZ", "venue": "SSE", "last_bar": "2026-10-15",
             "last_completed_session": "2026-10-15", "unsettled_bar": "2026-10-16",
             "checked_days": ["2026-10-15"], "missing_completed_days": [],
             "required_day_bar_present": True}]},
    ]
    assert cvp.detect_retractions(history) == []


def test_malformed_truncated_and_untimestamped_history_is_survivable(tmp_path):
    log = tmp_path / "vendor.jsonl"
    good1 = {"probed_at_utc": "2026-10-14T21:00:00+00:00", "rows": [
        {"ticker": "SPY", "venue": "NYSE", "last_bar": "2026-10-14",
         "last_completed_session": "2026-10-14"}]}
    no_stamp = {"rows": [{"ticker": "SPY", "venue": "NYSE", "last_bar": "2026-10-14",
                          "last_completed_session": "2026-10-14"}]}
    newest = {"schema": 2, "probed_at_utc": "2026-10-15T03:00:00+00:00", "rows": [
        {"ticker": "SPY", "venue": "NYSE", "last_bar": "2026-10-13",
         "last_completed_session": "2026-10-14", "checked_days": ["2026-10-13", "2026-10-14"],
         "missing_completed_days": ["2026-10-14"], "required_day_bar_present": False}]}
    log.write_text(json.dumps(good1) + "\n{ not json\n" + json.dumps(no_stamp) + "\n"
                   + '{"probed_at_utc": "2026-10-15T0' + "\n" + json.dumps(newest) + "\n",
                   encoding="utf-8")
    history = ap.load_history(log, cvp.RETRACTION_LOOKBACK_ROWS)
    assert len(history) == 3
    found = cvp.detect_retractions(history)
    assert [(r["ticker"], r["was"], r["now"]) for r in found] == [("SPY", "2026-10-14", "2026-10-13")]


def test_an_absent_symbol_or_partial_row_is_not_recovery(tmp_path):
    opened = _open_incident(tmp_path, delivered_ok=True)
    for newest in (
        {"schema": 2, "probed_at_utc": "2026-10-15T09:00:00+00:00", "rows": []},
        {"schema": 2, "probed_at_utc": "2026-10-15T09:00:00+00:00", "rows": [
            {"ticker": "SPY", "venue": "NYSE", "last_bar": None, "calendar_error": "x",
             "checked_days": [], "missing_completed_days": None}]},
        {"schema": 2, "probed_at_utc": "2026-10-15T09:00:00+00:00", "rows": [
            {"ticker": "SPY", "venue": "NYSE", "last_bar": None,
             "last_completed_session": "2026-10-14", "checked_days": ["2026-10-14"],
             "missing_completed_days": ["2026-10-14"], "required_day_bar_present": False}]},
    ):
        verdict = route([newest], opened, utc(2026, 10, 15, 9, 5), tmp_path)
        assert not [e for e in verdict["events"] if e["kind"] == ap.RECOVERY]
        assert ap.incidents_from(opened + verdict["events"])["SPY@2026-10-14"].open


# ===========================================================================
# Incident helpers (tripwire routing, the deployed default)
# ===========================================================================
def vrow(stamp, ticker, last_bar, lcs, venue="NYSE", checked=None, missing=None):
    row = {"ticker": ticker, "venue": venue, "last_bar": last_bar,
           "last_completed_session": lcs}
    rec = {"probed_at_utc": stamp, "rows": [row]}
    if checked is not None:
        row.update({"checked_days": checked, "missing_completed_days": missing or [],
                    "required_day_bar_present": lcs not in (missing or []),
                    "evidence": "batch"})
        rec["schema"] = 2
    return rec


def merge(*records):
    out = {"schema": 2, "probed_at_utc": records[0]["probed_at_utc"], "rows": []}
    for r in records:
        out["rows"] += r["rows"]
    return out


WINDOW = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]
SEEN = vrow("2026-10-14T21:00:00+00:00", "SPY", "2026-10-14", "2026-10-14")
GONE = vrow("2026-10-15T03:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
            checked=WINDOW, missing=["2026-10-14"])
BACK = vrow("2026-10-15T15:00:00+00:00", "SPY", "2026-10-14", "2026-10-14",
            checked=WINDOW, missing=[])


def route(history, ops_rows, now, tmp_path, routing=ap.DEFAULT_ROUTING,
          book=None, delivery=None):
    return ap.evaluate(vendor_rows=history,
                       retractions=cvp.detect_retractions(history),
                       ops_rows=list(ops_rows), book=book, delivery=delivery,
                       now=now, data_dir=tmp_path, routing=routing)


def _open_incident(tmp_path, delivered_ok, outcome=None):
    verdict = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)
    assert [e["disposition"] for e in verdict["events"]] == [ap.NOTIFY]
    return ap.stamp_delivery(verdict["events"],
                             outcome or ("success" if delivered_ok else "failure"))


# ===========================================================================
# 6. One alert per episode, after confirmed delivery
# ===========================================================================
def test_default_routing_is_the_tripwire_mailing_set(tmp_path):
    """No new suppression: an anomalous withdrawal on a line nothing ranks
    still mails under the default; the consequence is recorded beside it."""
    lone = [vrow("2026-10-14T21:00:00+00:00", "ZZZQ", "2026-10-14", "2026-10-14"),
            vrow("2026-10-15T03:00:00+00:00", "ZZZQ", "2026-10-13", "2026-10-14")]
    verdict = route(lone, [], utc(2026, 10, 15, 3, 5), tmp_path)
    event = verdict["events"][0]
    assert ap.DEFAULT_ROUTING == ap.ROUTING_TRIPWIRE
    assert event["disposition"] == ap.NOTIFY
    assert event["consequence"] == ap.NO_DEPENDENCY
    assert event["consequence_severity"] == ap.LOG_ONLY
    assert ap.subject(verdict["events"]).startswith("[WARN] Vendor RETRACTED a settled bar")


def test_same_incident_alerts_once_after_confirmed_delivery(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    for hour in (9, 15, 21):
        later = vrow(f"2026-10-15T{hour:02d}:00:00+00:00", "SPY", "2026-10-13",
                     "2026-10-14", checked=WINDOW, missing=["2026-10-14"])
        verdict = route([SEEN, GONE, later], log, utc(2026, 10, 15, hour, 5), tmp_path)
        assert [e["disposition"] for e in verdict["events"]] == [ap.REPEAT]
        log += ap.stamp_delivery(verdict["events"], "skipped")
    assert len([r for r in log if r["kind"] == ap.RETRACTION]) == 4    # all recorded


def test_independent_incidents_stay_independent(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    seen2 = merge(SEEN, vrow("x", "XLF", "2026-10-14", "2026-10-14"))
    gone2 = merge(GONE, vrow("x", "XLF", "2026-10-13", "2026-10-14",
                             checked=WINDOW, missing=["2026-10-14"]))
    gone2["probed_at_utc"] = "2026-10-15T09:00:00+00:00"
    verdict = route([seen2, gone2], log, utc(2026, 10, 15, 9, 5), tmp_path)
    by = {e["incident"]: e["disposition"] for e in verdict["events"]}
    assert by == {"SPY@2026-10-14": ap.REPEAT, "XLF@2026-10-14": ap.NOTIFY}


def test_quiet_to_action_escalation_alerts_once(tmp_path):
    """Under consequence routing (owner-gated) the same withdrawal is quiet
    before distribution and action-required after it."""
    from test_alert_policy import _ranked_withdrawal, book, delivered, undelivered
    quiet = route(_ranked_withdrawal("2026-09-19T20:00:00+00:00"), [],
                  utc(2026, 9, 19, 20, 5), tmp_path, ap.ROUTING_CONSEQUENCE,
                  book(), undelivered())
    log = ap.stamp_delivery(quiet["events"], "skipped")
    assert log[0]["disposition"] == ap.QUIET
    up = route(_ranked_withdrawal("2026-09-20T03:08:00+00:00"), log,
               utc(2026, 9, 20, 3, 10), tmp_path, ap.ROUTING_CONSEQUENCE, book(), delivered())
    assert up["events"][0]["disposition"] == ap.NOTIFY
    log += ap.stamp_delivery(up["events"], "success")
    again = route(_ranked_withdrawal("2026-09-20T09:08:00+00:00"), log,
                  utc(2026, 9, 20, 9, 10), tmp_path, ap.ROUTING_CONSEQUENCE, book(), delivered())
    assert again["events"][0]["disposition"] == ap.REPEAT


def test_recovery_then_recurrence_is_a_fresh_episode(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    rec = route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5), tmp_path)
    assert [e["kind"] for e in rec["events"]] == [ap.RECOVERY]
    log += ap.stamp_delivery(rec["events"], "success")
    again = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
                 checked=WINDOW, missing=["2026-10-14"])
    verdict = route([SEEN, BACK, again], log, utc(2026, 10, 16, 3, 5), tmp_path)
    assert [e["disposition"] for e in verdict["events"]] == [ap.NOTIFY]
    inc = ap.incidents_from(log + ap.stamp_delivery(verdict["events"], "success"))["SPY@2026-10-14"]
    assert inc.episode == 2 and inc.notified


def test_an_open_incident_outlives_the_detector_lookback(tmp_path):
    """Thirteen later probes, none re-observing the withdrawal and none
    proving the session: the incident stays open, quiet, and remembered."""
    log = _open_incident(tmp_path, delivered_ok=True)
    for i in range(cvp.RETRACTION_LOOKBACK_ROWS + 1):
        newest = vrow(f"2026-10-16T{i:02d}:00:00+00:00", "XLF", "2026-10-15", "2026-10-15")
        verdict = route([newest], log, utc(2026, 10, 16, i, 5), tmp_path)
        assert verdict["events"] == []
    assert ap.incidents_from(log)["SPY@2026-10-14"].open


def test_trimming_keeps_open_and_delivery_pending_incidents(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 50)
    path = tmp_path / "ops.jsonl"
    pending = _open_incident(tmp_path, delivered_ok=False)        # alert owed
    ap.record(path, pending)
    filler = [{"schema": 2, "event_id": f"f{i}", "recorded_at_utc": "2026-10-01T00:00:00+00:00",
               "kind": ap.RECOVERY, "incident": f"OLD{i}@2026-10-01",
               "disposition": ap.RECOVERY_QUIET, "delivery": ap.DELIVERY_NOT_REQUIRED}
              for i in range(120)]
    ap.record(path, filler)
    rows = ap.read_ops_log(path)
    assert len(rows) <= 50
    assert ap.incidents_from(rows)["SPY@2026-10-14"].alert_pending


# ===========================================================================
# 7. Unconfirmed delivery stays retryable; acknowledgement is batch-bound
# ===========================================================================
@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", "", None, "maybe"])
def test_unconfirmed_initial_mail_is_routed_again_even_unobserved(tmp_path, outcome):
    log = _open_incident(tmp_path, delivered_ok=False, outcome=outcome or "unknown")
    assert ap.incidents_from(log)["SPY@2026-10-14"].alert_pending
    # The detector no longer sees the withdrawal (lookback passed), and the
    # session is still not proven: the alert is retried, not forgotten.
    unrelated = vrow("2026-10-18T03:00:00+00:00", "XLF", "2026-10-16", "2026-10-16")
    verdict = route([unrelated], log, utc(2026, 10, 18, 3, 5), tmp_path)
    assert [(e["kind"], e["disposition"]) for e in verdict["events"]] == [(ap.RETRY, ap.NOTIFY)]
    log += ap.stamp_delivery(verdict["events"], "success")
    assert ap.incidents_from(log)["SPY@2026-10-14"].notified


def test_a_pending_alert_is_never_closed_as_stale(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=False)
    late = vrow("2026-11-20T03:00:00+00:00", "XLF", "2026-11-19", "2026-11-19")
    verdict = route([late], log, utc(2026, 11, 20, 3, 5), tmp_path)
    assert [e["kind"] for e in verdict["events"]] == [ap.RETRY]


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled"])
def test_unconfirmed_recovery_mail_is_routed_again(tmp_path, outcome):
    book = {"sleeves": [{"sleeve": "B", "signals": {"SPY": 1.0}, "weights": {},
                         "decision_session": "2026-10-14", "fill_date": "2026-10-19"}]}
    log = _open_incident(tmp_path, delivered_ok=True)
    rec = route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5), tmp_path, book=book)
    assert rec["events"][0]["disposition"] == ap.RECOVERY_NOTIFY
    log += ap.stamp_delivery(rec["events"], outcome)
    inc = ap.incidents_from(log)["SPY@2026-10-14"]
    assert inc.open is False and inc.recovery_pending      # data recovered, mail owed
    retry = route([BACK], log, utc(2026, 10, 15, 21, 5), tmp_path, book=book)
    assert [(e["kind"], e["retry_of"], e["disposition"]) for e in retry["events"]] \
        == [(ap.RETRY, "recovery", ap.RECOVERY_NOTIFY)]
    log += ap.stamp_delivery(retry["events"], "success")
    assert ap.incidents_from(log)["SPY@2026-10-14"].recovery == ap.RECOVERY_DELIVERED
    assert route([BACK], log, utc(2026, 10, 16, 3, 5), tmp_path, book=book)["events"] == []


def test_success_then_a_failed_write_mails_again_rather_than_never(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    verdict = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)
    batch = ap.freeze_batch(verdict)
    real = os.replace

    def fail(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(ap.os, "replace", fail)
    with pytest.raises(OSError):
        ap.record_batch(path, batch, "success")
    monkeypatch.setattr(ap.os, "replace", real)
    assert not path.exists()
    assert not list(tmp_path.glob("ops.jsonl.tmp-*"))
    again = route([SEEN, GONE], ap.read_ops_log(path), utc(2026, 10, 15, 9, 5), tmp_path)
    assert again["events"][0]["disposition"] == ap.NOTIFY       # a duplicate, not a silence


def test_acknowledgement_is_bound_to_the_exact_routed_batch(tmp_path):
    path = tmp_path / "ops.jsonl"
    first = ap.freeze_batch(route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path))
    # A second, different evaluation happens before the first is recorded.
    gone2 = merge(GONE, vrow("x", "XLF", "2026-10-13", "2026-10-14",
                             checked=WINDOW, missing=["2026-10-14"]))
    seen2 = merge(SEEN, vrow("x", "XLF", "2026-10-14", "2026-10-14"))
    second = ap.freeze_batch(route([seen2, gone2], [], utc(2026, 10, 15, 3, 6), tmp_path))
    assert first["batch_id"] != second["batch_id"]
    ap.record_batch(path, first, "success")
    incidents = ap.incidents_from(ap.read_ops_log(path))
    assert incidents["SPY@2026-10-14"].notified
    assert "XLF@2026-10-14" not in incidents                      # never acknowledged
    # Re-recording the same batch is a no-op; a tampered batch is refused.
    assert ap.record(path, ap.stamp_delivery(first["events"], "success")) == 0
    bad = tmp_path / "bad.json"
    tampered = json.loads(json.dumps(first))
    tampered["events"][0]["disposition"] = ap.QUIET
    bad.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ap.BatchIntegrityError):
        ap.read_batch(bad)


def test_the_record_cli_fails_closed_and_writes_nothing(tmp_path):
    batch_path = tmp_path / "batch.json"
    batch_path.write_text("{ truncated", encoding="utf-8")
    ops = tmp_path / "ops.jsonl"
    assert ap.main(["--ops-log", str(ops), "--record-batch", str(batch_path),
                    "--mail-outcome", "success"]) == 1
    assert not ops.exists()


# ===========================================================================
# 8. Recovery needs explicit provider evidence for the affected session
# ===========================================================================
def test_a_later_tail_is_not_recovery(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    later_legacy = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-15", "2026-10-15")
    verdict = route([later_legacy], log, utc(2026, 10, 16, 3, 5), tmp_path)
    assert verdict["events"] == []
    window2 = WINDOW[1:] + ["2026-10-15"]
    later_missing = vrow("2026-10-16T09:00:00+00:00", "SPY", "2026-10-15", "2026-10-15",
                         checked=window2, missing=["2026-10-14"])
    verdict = route([later_missing], log, utc(2026, 10, 16, 9, 5), tmp_path)
    assert verdict["events"] == []
    assert ap.incidents_from(log)["SPY@2026-10-14"].open


def test_a_session_outside_the_checked_window_is_unknown(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    far = vrow("2026-10-23T03:00:00+00:00", "SPY", "2026-10-22", "2026-10-22",
               checked=["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21",
                        "2026-10-22"], missing=[])
    assert route([far], log, utc(2026, 10, 23, 3, 5), tmp_path)["events"] == []


def test_quiet_incidents_recover_quietly_and_pending_alerts_are_withdrawn(tmp_path):
    routine = [vrow("2026-10-14T20:00:00+00:00", "EXV1.DE", "2026-10-14", "2026-10-14", "XETR"),
               vrow("2026-10-15T02:00:00+00:00", "EXV1.DE", "2026-10-13", "2026-10-14", "XETR",
                    checked=WINDOW, missing=["2026-10-14"])]
    q = route(routine, [], utc(2026, 10, 15, 2, 5), tmp_path)
    assert q["events"][0]["disposition"] == ap.QUIET
    log = ap.stamp_delivery(q["events"], "skipped")
    back = vrow("2026-10-16T02:00:00+00:00", "EXV1.DE", "2026-10-15", "2026-10-15", "XETR",
                checked=WINDOW[1:] + ["2026-10-15"], missing=[])
    r = route([back], log, utc(2026, 10, 16, 2, 5), tmp_path)
    assert [e["disposition"] for e in r["events"]] == [ap.RECOVERY_QUIET]
    # A pending (undelivered) alert whose data recovers: closed, alert withdrawn.
    log = _open_incident(tmp_path, delivered_ok=False)
    r = route([BACK], log, utc(2026, 10, 15, 15, 5), tmp_path)
    assert [e["disposition"] for e in r["events"]] == [ap.RECOVERY_QUIET]
    inc = ap.incidents_from(log + ap.stamp_delivery(r["events"], "skipped"))["SPY@2026-10-14"]
    assert inc.open is False and inc.alert == ap.ALERT_WITHDRAWN and not inc.owes_delivery


# ===========================================================================
# 9. Configured dependencies survive a HOLD
# ===========================================================================
HOLD_BOOK = {"as_of": "2026-10-13", "sleeves": [
    {"sleeve": "A", "status": "READY", "signals": {"CSP1": 0.5}, "weights": {"CSP1": 1.0},
     "decision_session": "2026-10-13", "fill_date": "2026-10-19"},
    {"sleeve": "C", "status": "HOLD", "signals": {}, "weights": {},
     "missing_signal_inputs": ["BTC-USD"],
     "decision_session": "2026-10-13", "fill_date": "2026-10-19"},
    {"sleeve": "D", "status": "HOLD", "signals": {}, "weights": {},
     "decision_session": "2026-10-12", "fill_date": "2026-10-19"},
]}


def test_a_missing_signal_input_of_a_hold_is_a_dependency(tmp_path):
    dep = ap.resolve_dependency("BTC-USD", "CRYPTO", HOLD_BOOK, tmp_path)
    assert dep.sleeves == ("C",) and dep.hold_input and dep.ranked
    assert "missing_signal_input" in dep.roles
    assert "missing signal input of a sleeve on HOLD" in dep.describe()


def test_configured_but_unheld_names_keep_their_sleeve(tmp_path):
    cn = ap.resolve_dependency("159801.SZ", "SSE", HOLD_BOOK, tmp_path)
    assert cn.sleeves == ("C",) and not cn.ranked and "universe" in cn.roles
    exh4 = ap.resolve_dependency("EXH4.DE", "XETR", HOLD_BOOK, tmp_path)
    assert exh4.sleeves == ("D",) and exh4.panels == ("EXH3",)
    assert "trading_proxy" in exh4.roles
    exv1 = ap.resolve_dependency("EXV1.DE", "XETR", HOLD_BOOK, tmp_path)
    assert exv1.sleeves == ("D",) and "universe" in exv1.roles


def test_unrelated_proxies_and_missing_provenance(tmp_path):
    dep = ap.resolve_dependency("ZZZQ", "NYSE", HOLD_BOOK, tmp_path)
    assert not dep.known
    prov = ap.read_provenance(HOLD_BOOK, None, ("C",))
    assert prov.distributed is False and prov.fill_date.isoformat() == "2026-10-19"
    # Default routing does not silence a withdrawal for missing provenance.
    hist = [vrow("2026-10-14T00:00:00+00:00", "BTC-USD", "2026-10-13", "2026-10-13", "CRYPTO"),
            vrow("2026-10-15T03:00:00+00:00", "BTC-USD", "2026-10-12", "2026-10-14", "CRYPTO")]
    verdict = route(hist, [], utc(2026, 10, 15, 3, 5), tmp_path, book=HOLD_BOOK)
    event = verdict["events"][0]
    assert event["disposition"] == ap.NOTIFY
    assert event["dependency"]["hold_input"] is True
    assert event["consequence"] == ap.PRE_RELEASE_INPUT          # recorded, not routed on


def test_an_incomplete_dependency_lookup_cannot_buy_silence(tmp_path):
    dep = ap.resolve_dependency("ZZZQ", "NYSE", None, tmp_path,
                                configured=({}, ("run_thematic_rotation: ImportError()",)))
    consequence, severity, _ = ap.assess_retraction(
        {"was": "2026-10-14"}, dep, ap.Provenance(), utc(2026, 10, 15).date())
    assert severity == ap.ACTION_REQUIRED


def test_readiness_routing_is_not_changed_by_phase_1():
    source = (ROOT / "scripts" / "check_pretrade_ready.py").read_text(encoding="utf-8")
    assert "alert_policy" not in source
    workflow = (ROOT / ".github" / "workflows" / "vendor_probe.yml").read_text(encoding="utf-8")
    assert "--routing consequence" not in workflow


# ===========================================================================
# 10. Concurrency, interleaving, interrupted writes, deterministic replay
# ===========================================================================
def _events(tag, n):
    out = []
    for i in range(n):
        e = {"schema": 2, "recorded_at_utc": "2026-10-15T00:00:00+00:00",
             "kind": ap.RETRACTION, "incident": f"{tag}{i}@2026-10-14",
             "ticker": f"{tag}{i}", "session": "2026-10-14",
             "disposition": ap.QUIET, "delivery": ap.DELIVERY_NOT_REQUIRED}
        e["event_id"] = ap._event_id(e)
        out.append(e)
    return out


def test_two_concurrent_writers_lose_nothing(tmp_path):
    path = tmp_path / "ops.jsonl"
    batches = {"A": _events("A", 60), "B": _events("B", 60)}
    errors = []

    def writer(tag):
        try:
            for e in batches[tag]:
                ap.record(path, [e])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(t,)) for t in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ids = [r["event_id"] for r in ap.read_ops_log(path)]
    assert sorted(ids) == sorted(e["event_id"] for b in batches.values() for e in b)
    assert len(ids) == len(set(ids))


def test_concurrent_writers_while_trimming_keep_open_incidents(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 40)
    path = tmp_path / "ops.jsonl"
    keep = _open_incident(tmp_path, delivered_ok=False)
    ap.record(path, keep)
    closed = []
    for i in range(80):
        e = {"schema": 2, "recorded_at_utc": "2026-10-15T00:00:00+00:00",
             "kind": ap.RECOVERY, "incident": f"C{i}@2026-10-01",
             "disposition": ap.RECOVERY_QUIET, "delivery": ap.DELIVERY_NOT_REQUIRED}
        e["event_id"] = ap._event_id(e)
        closed.append(e)
    halves = [closed[::2], closed[1::2]]
    threads = [threading.Thread(target=lambda h=h: [ap.record(path, [e]) for e in h])
               for h in halves]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = ap.read_ops_log(path)
    assert len(rows) <= 40
    assert ap.incidents_from(rows)["SPY@2026-10-14"].alert_pending


def test_stale_snapshots_from_two_evaluators_converge(tmp_path):
    """Two evaluators read the same empty log and both route the alert. One
    send is confirmed and one fails; the fold is delivered either way round,
    and the next observation is a repeat."""
    path = tmp_path / "ops.jsonl"
    a = ap.freeze_batch(route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path))
    b = ap.freeze_batch(route([SEEN, GONE], [], utc(2026, 10, 15, 3, 6), tmp_path))
    ap.record_batch(path, b, "failure")
    ap.record_batch(path, a, "success")
    rows = ap.read_ops_log(path)
    assert len(rows) == 2
    later = vrow("2026-10-15T09:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
                 checked=WINDOW, missing=["2026-10-14"])
    assert route([SEEN, later], rows, utc(2026, 10, 15, 9, 5), tmp_path)["events"][0][
        "disposition"] == ap.REPEAT


def test_replay_is_deterministic(tmp_path):
    one = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)
    two = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)
    assert one["batch_id"] == two["batch_id"]
    assert [e["event_id"] for e in one["events"]] == [e["event_id"] for e in two["events"]]
    log = ap.stamp_delivery(one["events"], "success")
    s1 = {k: (v.open, v.alert, v.recovery, v.episode) for k, v in ap.incidents_from(log).items()}
    s2 = {k: (v.open, v.alert, v.recovery, v.episode) for k, v in ap.incidents_from(
        json.loads(json.dumps(log))).items()}
    assert s1 == s2


def test_a_held_lock_fails_closed_and_the_events_are_routed_again(tmp_path):
    path = tmp_path / "ops.jsonl"
    Path(str(path) + ".lock").write_text("other writer", encoding="utf-8")
    events = _open_incident(tmp_path, delivered_ok=True)
    with pytest.raises(ap.LockTimeout):
        ap.record(path, events, lock_timeout=0.1)
    assert not path.exists()
    again = route([SEEN, GONE], ap.read_ops_log(path), utc(2026, 10, 15, 9, 5), tmp_path)
    assert again["events"][0]["disposition"] == ap.NOTIFY


def test_an_interrupted_write_leaves_the_previous_log_intact(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    ap.record(path, _events("P", 3))
    before = path.read_bytes()

    def crash(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(ap.os, "replace", crash)
    with pytest.raises(KeyboardInterrupt):
        ap.record(path, _events("Q", 3))
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("ops.jsonl.tmp-*"))
    assert not Path(str(path) + ".lock").exists()


def test_unreadable_lines_survive_a_rewrite(tmp_path):
    path = tmp_path / "ops.jsonl"
    path.write_text('{"incident": "X@2026-10-01", "kind": "recovery"}\n{ truncated\n',
                    encoding="utf-8")
    ap.record(path, _events("R", 1))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "{ truncated" in lines and len(lines) == 3


def test_the_workflow_serialises_runs_and_persists_through_git():
    workflow = (ROOT / ".github" / "workflows" / "vendor_probe.yml").read_text(encoding="utf-8")
    assert "group: vendor-availability-probe" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "git pull --rebase --autostash origin main || exit 1" in workflow
    assert "git add data/vendor_availability_log.jsonl data/ops_alert_log.jsonl" in workflow
    # Recording failure must not stop the probe row being committed.
    record = workflow[workflow.index("Record the routed batch"):workflow.index("Commit the observation")]
    assert "continue-on-error: true" in record


# ===========================================================================
# Independent review fixes F1-F6 (2026-10-15)
# ===========================================================================
def _pending_log(tmp_path):
    return _open_incident(tmp_path, delivered_ok=False)


def _deny_read(monkeypatch, target):
    real = Path.read_text

    def fail(self, *a, **k):
        if Path(self) == Path(target):
            raise PermissionError("simulated read denial")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", fail)


def test_f1_unreadable_existing_log_is_never_replaced(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    ap.record(path, _pending_log(tmp_path))
    original = path.read_bytes()
    _deny_read(monkeypatch, path)
    with pytest.raises(PermissionError):
        ap.record(path, _events("Z", 1))
    with pytest.raises(PermissionError):
        ap.read_ops_log(path)
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert not Path(str(path) + ".lock").exists()


def test_f1_undecodable_log_fails_closed_and_keeps_its_bytes(tmp_path):
    path = tmp_path / "ops.jsonl"
    path.write_bytes(b'{"incident": "A@2026-10-14"}\n\xff\xfe garbage\n')
    original = path.read_bytes()
    with pytest.raises(ValueError):
        ap.record(path, _events("Z", 1))
    assert path.read_bytes() == original


def test_f1_missing_log_is_the_only_empty_history(tmp_path):
    assert ap.read_ops_log(tmp_path / "absent.jsonl") == []
    assert ap.record(tmp_path / "new.jsonl", _events("N", 2)) == 2


def test_f1_route_cli_refuses_unreadable_state_and_writes_no_batch(tmp_path, monkeypatch):
    ops = tmp_path / "ops.jsonl"
    ap.record(ops, _pending_log(tmp_path))
    vendor = tmp_path / "vendor.jsonl"
    vendor.write_text(json.dumps(SEEN) + "\n" + json.dumps(GONE) + "\n", encoding="utf-8")
    batch = tmp_path / "batch.json"
    _deny_read(monkeypatch, ops)
    code = ap.main(["--vendor-log", str(vendor), "--ops-log", str(ops),
                    "--book", str(tmp_path / "none.json"),
                    "--delivery", str(tmp_path / "none.json"),
                    "--data-dir", str(tmp_path), "--now", "2026-10-15T09:05:00Z",
                    "--batch-out", str(batch)])
    assert code == 1 and not batch.exists()


def test_f2_events_carry_their_episode(tmp_path):
    first = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)
    assert first["events"][0]["episode"] == 1
    log = ap.stamp_delivery(first["events"], "success")
    rec = route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5), tmp_path)
    assert rec["events"][0]["episode"] == 1
    log += ap.stamp_delivery(rec["events"], "success")
    again = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
                 checked=WINDOW, missing=["2026-10-14"])
    assert route([SEEN, BACK, again], log, utc(2026, 10, 16, 3, 5),
                 tmp_path)["events"][0]["episode"] == 2


def test_f2_stale_batches_from_episode_one_change_nothing_in_episode_two(tmp_path):
    path = tmp_path / "ops.jsonl"
    ep1_alert_late = ap.freeze_batch(route([SEEN, GONE], [], utc(2026, 10, 15, 3, 6), tmp_path))
    log = _open_incident(tmp_path, delivered_ok=True)
    ep1_recovery_late = ap.freeze_batch(route([SEEN, GONE, BACK], log,
                                              utc(2026, 10, 15, 15, 6), tmp_path))
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "failure")      # recovery mail owed
    ep1_recovery_retry_late = ap.freeze_batch(route([BACK], log, utc(2026, 10, 15, 18, 5),
                                                    tmp_path))
    again = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
                 checked=WINDOW, missing=["2026-10-14"])
    log += ap.stamp_delivery(route([SEEN, BACK, again], log, utc(2026, 10, 16, 3, 5),
                                   tmp_path)["events"], "failure")      # episode 2, alert owed
    ap.record(path, log)
    before = ap.incidents_from(ap.read_ops_log(path))["SPY@2026-10-14"]
    # RESOLVED was never delivered, so the confirmed warning carries into
    # the successor under the owner-approved REOPEN rule.
    assert (before.episode, before.open, before.alert) == (2, True, ap.ALERT_DELIVERED)
    for late in (ep1_alert_late, ep1_recovery_late, ep1_recovery_retry_late):
        ap.record_batch(path, late, "success")
    rows = ap.read_ops_log(path)
    after = ap.incidents_from(rows)["SPY@2026-10-14"]
    assert (after.episode, after.open, after.alert) == (2, True, ap.ALERT_DELIVERED)
    assert after.unapplied >= 3                 # kept as history, not applied
    assert len(rows) == len(log) + 3


def test_f3_explicit_missing_evidence_reopens_instead_of_resolving(tmp_path):
    book = {"sleeves": [{"sleeve": "B", "signals": {"SPY": 1.0}, "weights": {},
                         "decision_session": "2026-10-14", "fill_date": "2026-10-19"}]}
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path, book=book)["events"], "failure")
    hole = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-15", "2026-10-15",
                checked=WINDOW[1:] + ["2026-10-15"], missing=["2026-10-14"])
    assert cvp.detect_retractions([BACK, hole]) == []          # the tail hides it
    verdict = route([BACK, hole], log, utc(2026, 10, 16, 3, 5), tmp_path, book=book)
    assert [(e["kind"], e["disposition"]) for e in verdict["events"]] == [(ap.REOPEN, ap.QUIET)]
    assert not verdict["notifying"]
    log += ap.stamp_delivery(verdict["events"], "skipped")
    inc = ap.incidents_from(log)["SPY@2026-10-14"]
    assert inc.open and inc.episode == 2 and inc.notified
    assert inc.recovery == ap.RECOVERY_SUPERSEDED and not inc.owes_delivery
    # Proven served again later: the delivered alert earns its recovery notice.
    back2 = vrow("2026-10-16T15:00:00+00:00", "SPY", "2026-10-15", "2026-10-15",
                 checked=WINDOW[1:] + ["2026-10-15"], missing=[])
    rec = route([hole, back2], log, utc(2026, 10, 16, 15, 5), tmp_path, book=book)
    assert [(e["kind"], e["disposition"], e["episode"]) for e in rec["events"]] \
        == [(ap.RECOVERY, ap.RECOVERY_NOTIFY, 2)]


def test_f3_unknown_evidence_never_claims_the_newest_probe(tmp_path):
    book = {"sleeves": [{"sleeve": "B", "signals": {"SPY": 1.0}, "weights": {},
                         "decision_session": "2026-10-14", "fill_date": "2026-10-19"}]}
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path, book=book)["events"], "failure")
    unrelated = vrow("2026-10-16T03:00:00+00:00", "XLF", "2026-10-15", "2026-10-15")
    verdict = route([unrelated], log, utc(2026, 10, 16, 3, 5), tmp_path, book=book)
    event = verdict["events"][0]
    assert (event["kind"], event["retry_of"], event["evidence_basis"]) == \
        (ap.RETRY, "recovery", "earlier_probe")
    assert event["evidence_probed_at"] == BACK["probed_at_utc"]
    text = ap.body(verdict["events"])
    assert "newest probe neither confirms nor contradicts" in text
    assert "proven by the newest probe" not in text


def test_f3_two_routers_reopening_the_same_episode_converge(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "failure")
    hole = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-15", "2026-10-15",
                checked=WINDOW[1:] + ["2026-10-15"], missing=["2026-10-14"])
    a = route([BACK, hole], log, utc(2026, 10, 16, 3, 5), tmp_path)["events"]
    b = route([BACK, hole], log, utc(2026, 10, 16, 3, 6), tmp_path)["events"]
    inc = ap.incidents_from(log + ap.stamp_delivery(a, "skipped")
                            + ap.stamp_delivery(b, "skipped"))["SPY@2026-10-14"]
    assert inc.open and inc.episode == 2 and inc.unapplied == 1


_GOOD = {"last_bar": "2026-10-14", "last_completed_session": "2026-10-14",
         "checked_days": ["2026-10-13", "2026-10-14"], "missing_completed_days": [],
         "required_day_bar_present": True, "invalid_days": [], "ambiguous_days": [],
         "evidence": "batch"}


@pytest.mark.parametrize("patch", [
    {"last_completed_session": None},
    {"last_completed_session": "14/10/2026"},
    {"last_completed_session": "2026-10-15"},                  # window not ending on bound
    {"checked_days": ["2026-10-13", "2026-10-14", "2026-10-15"]},
    {"missing_completed_days": ["2026-10-09"]},                # missing outside scope
    {"required_day_bar_present": False},                       # contradicts missing list
    {"required_day_bar_present": None},
    {"invalid_days": ["2026-10-13"]},                          # rejected yet not missing
    {"ambiguous_days": ["2026-10-13"]},
    {"evidence": None},
    {"evidence": "cache"},
    {"evidence": "none"},                                      # nothing served, yet present
    {"last_bar": "2026-10-12"},                                # earlier than a present day
    {"last_bar": None},
    {"checked_days": ["2026-10-13", "20261014"]},
])
def test_f4_inconsistent_or_incomplete_proof_is_unknown(patch):
    assert pv.session_evidence(_GOOD, "2026-10-13") is True
    assert pv.session_evidence({**_GOOD, **patch}, "2026-10-13") is None


def test_f4_a_valid_absence_and_a_generated_row_still_answer():
    absent = {**_GOOD, "missing_completed_days": ["2026-10-14"],
              "required_day_bar_present": False, "last_bar": "2026-10-13"}
    assert pv.session_evidence(absent, "2026-10-14") is False
    assert pv.session_evidence(absent, "2026-10-13") is True
    rows = {r["ticker"]: r for r in pv.probe(now_utc=NOW_FIXTURE,
                                              download=_replay_provider(True))["rows"]}
    assert pv.session_evidence(rows["EXV1.DE"], "2026-10-13") is True
    assert pv.session_evidence(rows["EXV1.DE"], "2026-10-14") is False
    assert pv.session_evidence(rows["BTC-USD"], "2026-10-13") is True
    assert pv.session_evidence(rows["159801.SZ"], "2026-10-14") is True


def test_f4_the_enclosing_record_must_declare_schema_2(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    unmarked = json.loads(json.dumps(BACK))
    del unmarked["schema"]
    assert route([unmarked], log, utc(2026, 10, 15, 15, 5), tmp_path)["events"] == []
    wrong = {**json.loads(json.dumps(BACK)), "schema": 3}
    assert route([wrong], log, utc(2026, 10, 15, 15, 5), tmp_path)["events"] == []


@pytest.mark.parametrize("disposition", [ap.RECOVERY_QUIET, ap.RECOVERY_NOTIFY])
def test_f5_schema_1_recovery_rows_are_history_not_closure(tmp_path, disposition):
    log = _open_incident(tmp_path, delivered_ok=True)
    log.append({"schema": 1, "kind": ap.RECOVERY, "incident": "SPY@2026-10-14",
                "ticker": "SPY", "session": "2026-10-14", "disposition": disposition,
                "recorded_at_utc": "2026-10-15T09:00:00+00:00"})
    inc = ap.incidents_from(log)["SPY@2026-10-14"]
    assert inc.open and inc.notified and not inc.owes_delivery and inc.unapplied == 1
    rec = route([BACK], log, utc(2026, 10, 15, 15, 5), tmp_path)
    assert [e["kind"] for e in rec["events"]] == [ap.RECOVERY]


def test_f5_schema_1_stale_rows_still_close():
    rows = [{"schema": 1, "kind": ap.RETRACTION, "incident": "X@2026-09-01",
             "ticker": "X", "session": "2026-09-01", "disposition": ap.QUIET,
             "recorded_at_utc": "2026-09-01T03:00:00+00:00"},
            {"schema": 1, "kind": ap.STALE, "incident": "X@2026-09-01",
             "recorded_at_utc": "2026-09-20T03:00:00+00:00", "disposition": ap.QUIET}]
    assert ap.incidents_from(rows)["X@2026-09-01"].open is False


def test_f6_lock_metadata_failure_closes_the_handle_and_frees_the_lock(tmp_path, monkeypatch):
    opened = []
    real_open, real_write = os.open, os.write

    def capture(*a, **k):
        fd = real_open(*a, **k)
        opened.append(fd)
        return fd

    def fail(*_a, **_k):
        raise OSError("metadata write failure")
    monkeypatch.setattr(ap.os, "open", capture)
    monkeypatch.setattr(ap.os, "write", fail)
    with pytest.raises(OSError):
        with ap._locked(tmp_path / "ops.jsonl", timeout=0):
            pass
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert not (tmp_path / "ops.jsonl.lock").exists()
    monkeypatch.setattr(ap.os, "write", real_write)
    with ap._locked(tmp_path / "ops.jsonl", timeout=0):
        pass
    assert ap.record(tmp_path / "ops.jsonl", _events("L", 1), lock_timeout=0) == 1


# ===========================================================================
# Re-review fixes R1-R6 (2026-10-15): the episode, transition, retention and
# migration contracts in alert_policy.fold_log and _trim.
# ===========================================================================
KEY = "SPY@2026-10-14"


def _cycle(tmp_path):
    """Episode 1 alerted (confirmed) and recovered (confirmed)."""
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "success")
    return log


AGAIN = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
             checked=WINDOW, missing=["2026-10-14"])


def test_r1_identity_chains_from_the_predecessor(tmp_path):
    log = _cycle(tmp_path)
    e1 = ap.incidents_from(log)[KEY].episode_id
    assert e1 == ap.episode_identity(KEY, None)
    opener = route([SEEN, BACK, AGAIN], log, utc(2026, 10, 16, 3, 5), tmp_path)["events"][0]
    assert opener["prev_episode_id"] == e1
    assert opener["episode_id"] == ap.episode_identity(KEY, e1) != e1


def test_r1_compaction_keeps_identity_and_the_chain_continues(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    ap.record(path, _cycle(tmp_path))
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    ap.record(path, _events("Q", 1))            # forces compaction of the settled incident
    rows = ap.read_ops_log(path)
    tombs = [r for r in rows if r.get("kind") == ap.TOMBSTONE]
    assert len(tombs) == 1 and tombs[0]["tombstone_of"] == KEY
    assert tombs[0]["episode_id"] == ap.episode_identity(KEY, None)
    assert KEY not in ap.incidents_from(rows) and KEY in ap.fold_log(rows)
    opener = route([SEEN, BACK, AGAIN], rows, utc(2026, 10, 16, 3, 5), tmp_path)["events"][0]
    assert opener["prev_episode_id"] == tombs[0]["episode_id"] and opener["episode"] == 2


def test_r1_never_opened_incidents_are_dropped_not_tombstoned(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 3)
    never_opened = [{"schema": 1, "kind": ap.RECOVERY, "incident": f"N{i}@2026-10-14",
                     "event_id": f"n{i}", "disposition": ap.RECOVERY_QUIET,
                     "recorded_at_utc": "2026-10-15T00:00:00+00:00"} for i in range(6)]
    ap.record(path, never_opened)               # history only: no episode, no identity
    rows = ap.read_ops_log(path)
    assert len(rows) == 3 and not [r for r in rows if r.get("kind") == ap.TOMBSTONE]


def test_r1_tombstones_expire_only_past_the_horizon_and_late_batches_are_refused(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    old_batch = ap.freeze_batch(route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path))
    ap.record(path, _cycle(tmp_path))
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    young = {**_events("Y", 1)[0], "recorded_at_utc": "2026-11-01T00:00:00+00:00"}
    young["event_id"] = ap._event_id(young)
    ap.record(path, [young], now=utc(2026, 11, 1, 0, 0))
    assert [r for r in ap.read_ops_log(path) if r.get("kind") == ap.TOMBSTONE]
    late = {**_events("Z", 1)[0], "recorded_at_utc": "2026-11-20T00:00:00+00:00"}
    late["event_id"] = ap._event_id(late)
    ap.record(path, [late], now=utc(2026, 11, 20, 0, 0))
    rows = ap.read_ops_log(path)
    assert not [r for r in rows if r.get("kind") == ap.TOMBSTONE]
    before = path.read_bytes()
    with pytest.raises(ap.StaleBatch):          # its identity may now be reused
        ap.record_batch(path, old_batch, "success", now=utc(2026, 11, 20, 0, 0))
    assert path.read_bytes() == before


def test_r1_a_tombstone_with_later_rows_is_never_dropped(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    ap.record(path, _cycle(tmp_path))
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    ap.record(path, _events("Q", 1))
    rows = ap.read_ops_log(path)
    reopened = route([SEEN, BACK, AGAIN], rows, utc(2026, 10, 16, 3, 5), tmp_path)
    ap.record_batch(path, ap.freeze_batch(reopened), "failure")
    far = {**_events("F", 1)[0], "recorded_at_utc": "2026-12-31T00:00:00+00:00"}
    far["event_id"] = ap._event_id(far)
    ap.record(path, [far], now=utc(2026, 12, 31, 0, 0))
    inc = ap.incidents_from(ap.read_ops_log(path))[KEY]
    assert inc.alert_pending and inc.episode == 2


def test_r2_stale_closure_is_revalidated_against_pending_delivery(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    stale = route([SEEN], log, utc(2026, 10, 30, 3, 5), tmp_path)["events"]
    assert [e["kind"] for e in stale] == [ap.STALE]
    assert not ap.incidents_from(log + ap.stamp_delivery(stale, "skipped"))[KEY].open
    pending = _open_incident(tmp_path, delivered_ok=False)
    inc = ap.incidents_from(pending + ap.stamp_delivery(stale, "skipped"))[KEY]
    assert inc.open and inc.alert_pending


def test_r3_reopen_after_a_delivered_recovery_starts_unannounced(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "failure")
    hole = vrow("2026-10-16T03:00:00+00:00", "SPY", "2026-10-15", "2026-10-15",
                checked=WINDOW[1:] + ["2026-10-15"], missing=["2026-10-14"])
    reopen = route([BACK, hole], log, utc(2026, 10, 16, 3, 5), tmp_path)["events"]
    retry = route([BACK], log, utc(2026, 10, 15, 21, 5), tmp_path)["events"]
    log += ap.stamp_delivery(retry, "success") + ap.stamp_delivery(reopen, "skipped")
    inc = ap.incidents_from(log)[KEY]
    assert inc.open and inc.episode == 2 and inc.alert == ap.ALERT_NONE
    # A later withdrawal the detector does see is a fresh alert, not a repeat.
    seen2 = vrow("2026-10-16T09:00:00+00:00", "SPY", "2026-10-15", "2026-10-15")
    gone2 = vrow("2026-10-16T15:00:00+00:00", "SPY", "2026-10-13", "2026-10-15",
                 checked=WINDOW[1:] + ["2026-10-15"], missing=["2026-10-14", "2026-10-15"])
    nxt = route([seen2, gone2], log, utc(2026, 10, 16, 15, 5), tmp_path)["events"]
    assert [e["disposition"] for e in nxt if e["incident"] == "SPY@2026-10-15"] == [ap.NOTIFY]


def test_r4_late_confirmed_alert_after_quiet_recovery_owes_a_notice(tmp_path):
    first = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)
    second = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 7), tmp_path)
    log = ap.stamp_delivery(first["events"], "failure")
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "skipped")
    assert ap.incidents_from(log)[KEY].recovery == ap.RECOVERY_NOT_REQUIRED
    unconfirmed = ap.incidents_from(log + ap.stamp_delivery(second["events"], "failure"))[KEY]
    assert not unconfirmed.recovery_pending                 # a failed late send owes nothing
    log += ap.stamp_delivery(second["events"], "success")
    inc = ap.incidents_from(log)[KEY]
    assert inc.notified and inc.recovery_pending and not inc.open
    nxt = route([BACK], log, utc(2026, 10, 15, 21, 5), tmp_path)["events"]
    assert [(e["kind"], e["disposition"], e["evidence_basis"]) for e in nxt] == \
        [(ap.RETRY, ap.RECOVERY_NOTIFY, "newest_probe")]


def test_r4_a_late_alert_for_an_older_episode_changes_nothing(tmp_path):
    early = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 7), tmp_path)
    log = _cycle(tmp_path)
    log += ap.stamp_delivery(route([SEEN, BACK, AGAIN], log, utc(2026, 10, 16, 3, 5),
                                   tmp_path)["events"], "failure")
    before = ap.incidents_from(log)[KEY]
    after = ap.incidents_from(log + ap.stamp_delivery(early["events"], "success"))[KEY]
    assert (after.episode, after.alert, after.recovery) == \
        (before.episode, before.alert, before.recovery) == (2, ap.ALERT_PENDING, ap.RECOVERY_NONE)


@pytest.mark.parametrize("observed", [None, "2026-10-15T03:00:00", "not-a-time"])
def test_t4_an_unusable_observation_is_ordered_by_probe_record_not_by_clock(tmp_path, observed):
    """Supersedes the S4 routing-time fallback (review T4). With the source
    time unusable, the episode is ordered by the probe RECORD its router saw:
    the same record never proves recovery; a different record now newest in
    the append-only log was written after it, whatever its clock says. The
    router's clock is never compared."""
    events = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)["events"]
    events[0]["observed_at"] = observed           # a persisted, damaged record
    events[0]["observed_record"] = ap.record_identity(BACK)   # the router saw BACK
    log = ap.stamp_delivery(events, "failure")
    inc = ap.incidents_from(log)[KEY]
    assert inc.withdrawn_observed_at is None and inc.withdrawn_record == ap.record_identity(BACK)
    # The withdrawal's own record cannot prove the session served.
    assert [e["kind"] for e in route([BACK], log, utc(2026, 10, 15, 15, 5),
                                     tmp_path)["events"]] == [ap.RETRY]
    # A later record proves it, even stamped before the router's clock.
    early_clock = json.loads(json.dumps(BACK))
    early_clock["probed_at_utc"] = "2026-10-15T03:04:00+00:00"
    rec = route([BACK, early_clock], log, utc(2026, 10, 15, 15, 6), tmp_path)["events"]
    assert [(e["kind"], e["disposition"]) for e in rec] == [(ap.RECOVERY, ap.RECOVERY_QUIET)]
    assert not ap.incidents_from(log + ap.stamp_delivery(rec, "skipped"))[KEY].owes_delivery


def test_t4_an_episode_with_no_ordering_evidence_fails_closed(tmp_path):
    """Neither a usable source time nor a record identity (a row no released
    build writes): nothing can be ordered, so no proof is accepted and the
    owed alert keeps retrying. Flagged in the handoff as an owner decision."""
    events = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)["events"]
    events[0]["observed_at"] = None
    del events[0]["observed_record"]
    log = ap.stamp_delivery(events, "failure")
    assert [e["kind"] for e in route([BACK], log, utc(2026, 10, 15, 15, 5),
                                     tmp_path)["events"]] == [ap.RETRY]


def test_r5_proof_must_be_observed_strictly_after_the_withdrawal(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    same = json.loads(json.dumps(BACK))
    same["probed_at_utc"] = GONE["probed_at_utc"]
    assert route([same], log, utc(2026, 10, 15, 15, 5), tmp_path)["events"] == []


def test_r5_an_absence_older_than_the_recovery_proof_cannot_reopen(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "failure")
    old_hole = vrow("2026-10-15T09:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
                    checked=WINDOW, missing=["2026-10-14"])
    verdict = route([old_hole], log, utc(2026, 10, 15, 21, 5), tmp_path)
    assert [(e["kind"], e["evidence_basis"]) for e in verdict["events"]] == \
        [(ap.RETRY, "earlier_probe")]


@pytest.mark.parametrize("patch", [
    {"unsettled_bar": "2026-10-14"},
    {"unsettled_bar": "2026-10-13"},
    {"unsettled_bar": "soon"},
    {"off_calendar_days": ["2026-10-13"]},
    {"off_calendar_days": "2026-10-13"},
    {"last_bar": "2026-10-15"},
])
def test_r5_completion_and_calendar_contradictions_are_unknown(patch):
    assert pv.session_evidence({**_GOOD, "unsettled_bar": "2026-10-15",
                                "off_calendar_days": ["2026-10-11"]}, "2026-10-13") is True
    assert pv.session_evidence({**_GOOD, **patch}, "2026-10-13") is None


def test_r6_a_legacy_episode_continues_into_schema_2_binding(tmp_path):
    legacy = {"schema": 1, "kind": ap.RETRACTION, "incident": KEY, "ticker": "SPY",
              "venue": "NYSE", "session": "2026-10-14", "disposition": ap.NOTIFY,
              "recorded_at_utc": "2026-10-15T03:00:00+00:00"}
    inc = ap.incidents_from([legacy])[KEY]
    assert inc.open and inc.notified and inc.episode_id == ap.episode_identity(KEY, None)
    rec = route([BACK], [legacy], utc(2026, 10, 15, 15, 5), tmp_path)["events"]
    assert [(e["kind"], e["episode_id"]) for e in rec] == [(ap.RECOVERY, inc.episode_id)]
    after = ap.incidents_from([legacy] + ap.stamp_delivery(rec, "success"))[KEY]
    assert not after.open and after.recovery == ap.RECOVERY_DELIVERED


def test_r6_unbound_rows_after_the_boundary_are_history(tmp_path):
    log = _open_incident(tmp_path, delivered_ok=False)
    late_stale = {"schema": 1, "kind": ap.STALE, "incident": KEY,
                  "recorded_at_utc": "2026-10-15T04:00:00+00:00", "disposition": ap.QUIET}
    inc = ap.incidents_from(log + [late_stale])[KEY]
    assert inc.open and inc.alert_pending and inc.unapplied == 1


# ===========================================================================
# Third review fixes S1-S5 (2026-10-16): composed interleavings, retention
# boundaries and the legacy-ingestion boundary.
# ===========================================================================
GONE_LATER = vrow("2026-10-15T09:00:00+00:00", "SPY", "2026-10-13", "2026-10-14",
                  checked=WINDOW, missing=["2026-10-14"])
BACK_EARLY = vrow("2026-10-15T06:00:00+00:00", "SPY", "2026-10-14", "2026-10-14",
                  checked=WINDOW, missing=[])


def _hole(stamp):
    return vrow(stamp, "SPY", "2026-10-15", "2026-10-15",
                checked=WINDOW[1:] + ["2026-10-15"], missing=["2026-10-14"])


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_s1_a_frozen_recovery_older_than_a_folded_withdrawal_is_history(tmp_path, outcome):
    log = _open_incident(tmp_path, delivered_ok=True)
    frozen = route([SEEN, BACK_EARLY], log, utc(2026, 10, 15, 6, 5), tmp_path)["events"]
    assert [e["kind"] for e in frozen] == [ap.RECOVERY]
    newer = route([SEEN, GONE_LATER], log, utc(2026, 10, 15, 9, 5), tmp_path)["events"]
    log += ap.stamp_delivery(newer, "skipped") + ap.stamp_delivery(frozen, outcome)
    inc = ap.incidents_from(log)[KEY]
    assert inc.open and inc.unapplied == 1
    assert inc.contradicted is (outcome == "success")     # a delivered RESOLVED stays visible
    if outcome == "success":
        # Owner-approved correction (2026-10-16): mailed, retried until
        # confirmed, then never again for this episode.
        first = route([GONE_LATER], log, utc(2026, 10, 15, 12, 5), tmp_path)
        marks = [e for e in first["events"] if e["kind"] == ap.CONTRADICTION]
        assert [m["disposition"] for m in marks] == [ap.CORRECTION_NOTIFY]
        assert first["correction"] and ap.subject(first["events"]).startswith("[CORRECTION]")
        log += ap.stamp_delivery(first["events"], "failure")
        assert ap.incidents_from(log)[KEY].correction_owed
        retry = route([GONE_LATER], log, utc(2026, 10, 15, 18, 5), tmp_path)
        assert [e["disposition"] for e in retry["events"]
                if e["kind"] == ap.CONTRADICTION] == [ap.CORRECTION_NOTIFY]
        log += ap.stamp_delivery(retry["events"], "success")
        assert not ap.incidents_from(log)[KEY].owes_delivery
        again = route([GONE_LATER], log, utc(2026, 10, 15, 21, 5), tmp_path)
        assert not [e for e in again["events"] if e["kind"] == ap.CONTRADICTION]


@pytest.mark.parametrize("order", ["earlier_first", "later_first"])
def test_s1_duplicate_reopens_keep_the_newest_absence_in_either_order(tmp_path, order):
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "failure")
    early = route([_hole("2026-10-16T03:00:00+00:00")], log, utc(2026, 10, 16, 3, 5),
                  tmp_path)["events"]
    late = route([_hole("2026-10-16T09:00:00+00:00")], log, utc(2026, 10, 16, 9, 5),
                 tmp_path)["events"]
    pair = (early, late) if order == "earlier_first" else (late, early)
    for batch in pair:
        log += ap.stamp_delivery(batch, "skipped")
    inc = ap.incidents_from(log)[KEY]
    assert inc.episode == 2 and inc.withdrawn_observed_at == "2026-10-16T09:00:00+00:00"
    stale_proof = vrow("2026-10-16T06:00:00+00:00", "SPY", "2026-10-15", "2026-10-15",
                       checked=WINDOW[1:] + ["2026-10-15"], missing=[])
    assert not route([stale_proof], log, utc(2026, 10, 16, 12, 5), tmp_path)["recovery"]


def test_s1_a_reopen_not_observed_after_the_recovery_proof_is_refused():
    log = [{"schema": 2, "kind": ap.RETRACTION, "incident": KEY, "episode_id": "e1",
            "prev_episode_id": None, "episode": 1, "disposition": ap.NOTIFY,
            "delivery": ap.DELIVERY_CONFIRMED, "observed_at": "2026-10-15T03:00:00+00:00",
            "recorded_at_utc": "2026-10-15T03:05:00+00:00"}]
    log[0]["episode_id"] = ap.episode_identity(KEY, None)
    e1 = log[0]["episode_id"]
    log.append({"schema": 2, "kind": ap.RECOVERY, "incident": KEY, "episode_id": e1,
                "disposition": ap.RECOVERY_NOTIFY, "delivery": ap.DELIVERY_FAILED,
                "evidence_probed_at": "2026-10-15T15:00:00+00:00",
                "recorded_at_utc": "2026-10-15T15:05:00+00:00"})
    reopen = {"schema": 2, "kind": ap.REOPEN, "incident": KEY, "prev_episode_id": e1,
              "episode_id": ap.episode_identity(KEY, e1), "episode": 2,
              "disposition": ap.QUIET, "observed_at": "2026-10-15T15:00:00+00:00",
              "recorded_at_utc": "2026-10-15T15:06:00+00:00"}
    inc = ap.incidents_from(log + [reopen])[KEY]
    assert not inc.open and inc.recovery_pending and inc.unapplied == 1


@pytest.mark.parametrize("first", ["late_ack", "duplicate_quiet"])
def test_s2_a_duplicate_quiet_recovery_never_cancels_an_owed_notice(tmp_path, first):
    a = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 5), tmp_path)
    b = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 7), tmp_path)
    log = ap.stamp_delivery(a["events"], "failure")
    q1 = route([BACK], log, utc(2026, 10, 15, 15, 5), tmp_path)["events"]
    q2 = route([BACK], log, utc(2026, 10, 15, 15, 6), tmp_path)["events"]
    log += ap.stamp_delivery(q1, "skipped")
    rest = [ap.stamp_delivery(b["events"], "success"), ap.stamp_delivery(q2, "skipped")]
    for batch in (rest if first == "late_ack" else rest[::-1]):
        log += batch
    assert ap.incidents_from(log)[KEY].recovery_pending


def test_s2_a_quiet_recovery_retry_discharges_only_a_pending_notice(tmp_path):
    log = _cycle(tmp_path)                              # recovery notice delivered
    quiet_retry = {"schema": 2, "kind": ap.RETRY, "retry_of": "recovery",
                   "incident": KEY, "episode_id": ap.incidents_from(log)[KEY].episode_id,
                   "disposition": ap.RECOVERY_QUIET, "delivery": ap.DELIVERY_NOT_REQUIRED,
                   "recorded_at_utc": "2026-10-15T21:05:00+00:00"}
    inc = ap.incidents_from(log + [quiet_retry])[KEY]
    assert inc.recovery == ap.RECOVERY_DELIVERED and inc.unapplied == 1


def test_s3_last_activity_is_monotonic_and_tombstones_carry_it(tmp_path, monkeypatch):
    late = route([SEEN, GONE], [], utc(2026, 10, 15, 3, 7), tmp_path)
    path = tmp_path / "ops.jsonl"
    ap.record(path, _cycle(tmp_path))
    ap.record_batch(path, ap.freeze_batch(late), "success")     # routed 03:07, after 15:05
    assert ap.incidents_from(ap.read_ops_log(path))[KEY].last_seen == "2026-10-15T15:05:00+00:00"
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    ap.record(path, _events("Q", 1))
    tomb = [r for r in ap.read_ops_log(path) if r.get("kind") == ap.TOMBSTONE][0]
    assert tomb["recorded_at_utc"] == "2026-10-15T15:05:00+00:00"


@pytest.mark.parametrize("delta, kept", [(timedelta(days=30), True),
                                         (timedelta(days=30, minutes=1), False)])
def test_s3_tombstone_expiry_boundary(tmp_path, monkeypatch, delta, kept):
    path = tmp_path / "ops.jsonl"
    ap.record(path, _cycle(tmp_path))                  # last activity 15:05
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    ap.record(path, _events("Q", 1))
    later = {**_events("L", 1)[0],
             "recorded_at_utc": (utc(2026, 10, 15, 15, 5) + delta).isoformat()}
    later["event_id"] = ap._event_id(later)
    ap.record(path, [later], now=utc(2026, 10, 15, 15, 5) + delta)
    assert (KEY in ap.fold_log(ap.read_ops_log(path))) is kept


def test_s3_an_expired_predecessor_refuses_the_batch_and_reconciles_next_probe(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    ap.record(path, _cycle(tmp_path))
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    ap.record(path, _events("Q", 1))
    rows = ap.read_ops_log(path)
    fresh = route([BACK, AGAIN], rows, utc(2026, 11, 13, 3, 5), tmp_path)
    assert fresh["events"][0]["prev_episode_id"] is not None
    expire = {**_events("X", 1)[0], "recorded_at_utc": "2026-11-15T00:00:00+00:00"}
    expire["event_id"] = ap._event_id(expire)
    ap.record(path, [expire], now=utc(2026, 11, 15, 0, 0))
    assert KEY not in ap.fold_log(ap.read_ops_log(path))
    before = path.read_bytes()
    with pytest.raises(ap.PredecessorExpired):
        ap.record_batch(path, ap.freeze_batch(fresh), "failure",
                        now=utc(2026, 11, 15, 0, 0))
    assert path.read_bytes() == before and not Path(str(path) + ".lock").exists()
    # The next probe routes from current state: a genesis opener, admitted.
    again = route([BACK, AGAIN], ap.read_ops_log(path), utc(2026, 11, 15, 3, 5), tmp_path)
    assert again["events"][0]["prev_episode_id"] is None
    ap.record_batch(path, ap.freeze_batch(again), "failure",
                    now=utc(2026, 11, 15, 3, 5))
    assert ap.incidents_from(ap.read_ops_log(path))[KEY].alert_pending


@pytest.mark.parametrize("routed, opens", [("2026-10-15T10:00:00+00:00", False),
                                           ("2026-10-15T15:05:00+00:00", True),
                                           ("2026-10-15T16:00:00+00:00", True)])
def test_s3_an_opener_routed_before_its_predecessor_closed_is_history(tmp_path, routed, opens):
    """Such an opener cannot come from a snapshot in which the predecessor was
    closed; it belongs to another lineage, for example a chain restarted after
    expiry, and must not open the successor."""
    log = _cycle(tmp_path)                  # episode 1 closed by recovery routed 15:05
    e1 = ap.incidents_from(log)[KEY].episode_id
    opener = {"schema": 2, "kind": ap.RETRACTION, "incident": KEY, "ticker": "SPY",
              "session": "2026-10-14", "prev_episode_id": e1,
              "episode_id": ap.episode_identity(KEY, e1), "episode": 2,
              "disposition": ap.NOTIFY, "delivery": ap.DELIVERY_FAILED,
              "observed_at": routed, "recorded_at_utc": routed}
    inc = ap.incidents_from(log + [opener])[KEY]
    assert inc.open is opens and (inc.episode == 2) is opens


def test_s4_an_unorderable_opener_from_an_empty_history_still_resolves(tmp_path):
    v = route([], [], utc(2026, 10, 15, 3, 5), tmp_path)
    assert v["events"] == []                       # no history, no retraction
    forced = ap.evaluate(vendor_rows=[], retractions=[{"ticker": "SPY", "venue": "NYSE",
                         "was": "2026-10-14", "now": "2026-10-13",
                         "routine": False, "measuring": False}],
                         ops_rows=[], book=None, delivery=None,
                         now=utc(2026, 10, 15, 3, 5), data_dir=tmp_path)
    log = ap.stamp_delivery(forced["events"], "failure")
    assert forced["events"][0]["observed_at"] is None
    rec = route([BACK], log, utc(2026, 10, 15, 15, 5), tmp_path)["events"]
    assert [e["kind"] for e in rec] == [ap.RECOVERY]


def test_s5_legacy_rows_past_the_horizon_are_refused_whole(tmp_path):
    path = tmp_path / "ops.jsonl"
    ap.record(path, _cycle(tmp_path))
    recent = {**_events("R", 1)[0], "recorded_at_utc": "2026-11-20T00:00:00+00:00"}
    recent["event_id"] = ap._event_id(recent)
    ap.record(path, [recent], now=utc(2026, 11, 20, 0, 0))
    before = path.read_bytes()
    old_legacy = {"schema": 1, "kind": ap.RETRACTION, "incident": "OLD@2026-10-14",
                  "ticker": "OLD", "session": "2026-10-14", "disposition": ap.NOTIFY,
                  "recorded_at_utc": "2026-10-15T04:00:00+00:00"}
    undated = {k: v for k, v in old_legacy.items() if k != "recorded_at_utc"}
    for row in (old_legacy, undated, {**old_legacy, "recorded_at_utc": "garbage"}):
        with pytest.raises(ap.StaleBatch):
            ap.record(path, [row], now=utc(2026, 11, 20, 0, 0))
        assert path.read_bytes() == before
    fresh_legacy = {**old_legacy, "recorded_at_utc": "2026-11-19T00:00:00+00:00"}
    assert ap.record(path, [fresh_legacy], now=utc(2026, 11, 20, 0, 0)) == 1
    assert ap.incidents_from(ap.read_ops_log(path))["OLD@2026-10-14"].notified


# ===========================================================================
# Correction notice (owner approval 2026-10-16): one operator-only notice per
# contradicted episode when this system's own delivered RESOLVED is
# contradicted by newer exact-session absence; confirmed-delivery and retry
# rules as for alerts; no wider historical-hole detection.
# ===========================================================================
HOLE = _hole("2026-10-16T03:00:00+00:00")
BACK_AGAIN = vrow("2026-10-16T15:00:00+00:00", "SPY", "2026-10-15", "2026-10-15",
                  checked=WINDOW[1:] + ["2026-10-15"], missing=[])


def _contradicted(tmp_path):
    """RESOLVED delivered for episode 1, then a reopen on newer absence."""
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "failure")
    reopen = route([BACK, HOLE], log, utc(2026, 10, 16, 3, 5), tmp_path)["events"]
    retry = route([BACK], log, utc(2026, 10, 15, 21, 5), tmp_path)["events"]
    log += ap.stamp_delivery(retry, "success") + ap.stamp_delivery(reopen, "skipped")
    inc = ap.incidents_from(log)[KEY]
    assert inc.open and inc.contradicted and inc.correction_owed
    return log


def test_correction_is_mailed_with_its_evidence_and_operator_wording(tmp_path):
    log = _contradicted(tmp_path)
    v = route([HOLE], log, utc(2026, 10, 16, 9, 5), tmp_path)
    assert [e["disposition"] for e in v["notifying"]] == [ap.CORRECTION_NOTIFY]
    assert ap.subject(v["events"]) == "[CORRECTION] Withdrawn bar is missing again - SPY 2026-10-14"
    text = ap.body(v["events"])
    assert "CORRECTION" in text and "2026-10-16T03:00:00+00:00" in text
    assert "That RESOLVED no longer holds" in text


def test_no_correction_for_a_resolved_this_system_never_delivered(tmp_path):
    # Recovery notice still pending when the reopen lands: the reader never
    # received RESOLVED, so the delivered alert carries and nothing is corrected.
    log = _open_incident(tmp_path, delivered_ok=True)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "failure")
    log += ap.stamp_delivery(route([BACK, HOLE], log, utc(2026, 10, 16, 3, 5),
                                   tmp_path)["events"], "skipped")
    inc = ap.incidents_from(log)[KEY]
    assert inc.open and not inc.contradicted and inc.notified
    assert not route([HOLE], log, utc(2026, 10, 16, 9, 5), tmp_path)["notifying"]


def test_no_correction_for_a_hole_after_a_quiet_recovery(tmp_path):
    # Never reported resolved: wider historical-hole detection stays out.
    log = _open_incident(tmp_path, delivered_ok=False)
    log += ap.stamp_delivery(route([SEEN, GONE, BACK], log, utc(2026, 10, 15, 15, 5),
                                   tmp_path)["events"], "skipped")
    assert ap.incidents_from(log)[KEY].recovery == ap.RECOVERY_NOT_REQUIRED
    assert route([BACK, HOLE], log, utc(2026, 10, 16, 3, 5), tmp_path)["events"] == []


def test_recovery_after_a_confirmed_correction_sends_resolved_again(tmp_path):
    book = {"sleeves": [{"sleeve": "B", "signals": {"SPY": 1.0}, "weights": {},
                         "decision_session": "2026-10-14", "fill_date": "2026-10-19"}]}
    log = _contradicted(tmp_path)
    log += ap.stamp_delivery(route([HOLE], log, utc(2026, 10, 16, 9, 5), tmp_path,
                                   book=book)["events"], "success")
    inc = ap.incidents_from(log)[KEY]
    assert inc.correction == "delivered" and inc.notified and not inc.owes_delivery
    rec = route([HOLE, BACK_AGAIN], log, utc(2026, 10, 16, 15, 5), tmp_path, book=book)
    assert [(e["kind"], e["disposition"]) for e in rec["events"]] == \
        [(ap.RECOVERY, ap.RECOVERY_NOTIFY)]


def test_recovery_before_the_correction_arrives_withdraws_it_quietly(tmp_path):
    log = _contradicted(tmp_path)
    log += ap.stamp_delivery(route([HOLE], log, utc(2026, 10, 16, 9, 5), tmp_path)["events"],
                             "failure")
    rec = route([HOLE, BACK_AGAIN], log, utc(2026, 10, 16, 15, 5), tmp_path)
    assert [(e["kind"], e["disposition"]) for e in rec["events"]] == \
        [(ap.RECOVERY, ap.RECOVERY_QUIET)]
    inc = ap.incidents_from(log + ap.stamp_delivery(rec["events"], "skipped"))[KEY]
    assert not inc.open and inc.correction == "withdrawn" and not inc.owes_delivery


def test_an_owed_correction_blocks_stale_closure_and_survives_trimming(tmp_path, monkeypatch):
    log = _contradicted(tmp_path)
    stale = {"schema": 2, "kind": ap.STALE, "incident": KEY,
             "episode_id": ap.incidents_from(log)[KEY].episode_id,
             "disposition": ap.QUIET, "delivery": ap.DELIVERY_NOT_REQUIRED,
             "recorded_at_utc": "2026-11-01T00:00:00+00:00"}
    assert ap.incidents_from(log + [stale])[KEY].correction_owed
    path = tmp_path / "ops.jsonl"
    ap.record(path, log)
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    ap.record(path, _events("Q", 1))
    assert ap.incidents_from(ap.read_ops_log(path))[KEY].correction_owed


def test_a_late_failed_duplicate_cannot_reopen_a_delivered_correction(tmp_path):
    log = _contradicted(tmp_path)
    a = route([HOLE], log, utc(2026, 10, 16, 9, 5), tmp_path)["events"]
    b = route([HOLE], log, utc(2026, 10, 16, 9, 6), tmp_path)["events"]
    log += ap.stamp_delivery(a, "success") + ap.stamp_delivery(b, "failure")
    assert ap.incidents_from(log)[KEY].correction == "delivered"


def test_correction_mail_goes_to_the_operator_only():
    workflow = (ROOT / ".github" / "workflows" / "vendor_probe.yml").read_text(encoding="utf-8")
    mail = workflow[workflow.index("id: mail"):workflow.index("Record the routed batch")]
    assert "to: ${{ secrets.GMAIL_USER }}" in mail
    assert "secrets.RECIPIENT_EMAIL" not in mail


# ===========================================================================
# Fourth review fixes T1-T4 (2026-10-16): lineage after identity reuse,
# quiet-recovery ordering, undated legacy on an empty log, clock-free order.
# ===========================================================================
def _genesis(when, observed_rows, ops=()):
    """A genesis-opening retraction for SPY routed at ``when``."""
    return ap.evaluate(vendor_rows=observed_rows, retractions=[
        {"ticker": "SPY", "venue": "NYSE", "was": "2026-10-14", "now": "2026-10-13",
         "routine": False, "measuring": False}], ops_rows=list(ops), book=None,
        delivery=None, now=when, data_dir=Path("."), configured=({}, ()))


def _absent_at(when):
    return vrow(when.isoformat(), "SPY", "2026-10-13", "2026-10-14",
                checked=WINDOW, missing=["2026-10-14"])


@pytest.mark.parametrize("opened, retry_at, applies", [
    # Month boundary: 2026-10-31 -> 2026-11-01 (Python months are 1-indexed).
    (utc(2026, 11, 1, 0, 1), utc(2026, 10, 31, 23, 59), False),
    (utc(2026, 11, 1, 0, 1), utc(2026, 11, 1, 0, 1), True),
    # Year boundary: 2026-12-31 -> 2027-01-01.
    (utc(2027, 1, 1, 0, 1), utc(2026, 12, 31, 23, 59), False),
    (utc(2027, 1, 1, 0, 1), utc(2027, 1, 1, 0, 2), True),
])
def test_t1_a_non_opening_event_routed_before_its_episode_opened_is_history(
        opened, retry_at, applies):
    log = ap.stamp_delivery(_genesis(opened, [_absent_at(opened)])["events"], "failure")
    inc = ap.incidents_from(log)[KEY]
    retry = {"schema": 2, "kind": ap.RETRY, "retry_of": "alert", "incident": KEY,
             "episode_id": inc.episode_id, "disposition": ap.NOTIFY,
             "opener_event_id": inc.opener_event_id,
             "delivery": ap.DELIVERY_CONFIRMED, "recorded_at_utc": retry_at.isoformat()}
    after = ap.incidents_from(log + [retry])[KEY]
    assert after.notified is applies and after.alert_pending is (not applies)


def test_t1_a_stale_genesis_opener_cannot_confirm_a_reused_identity():
    """Identity reuse after expiry: the fresh genesis episode was opened from
    a non-empty log snapshot; a stale opener of the old lineage carries an
    older (here empty) snapshot and is history, even though it is younger
    than the horizon and shares the identity."""
    other = {"schema": 2, "kind": ap.RETRACTION, "incident": "OTHER@2026-10-14",
             "episode_id": ap.episode_identity("OTHER@2026-10-14", None),
             "prev_episode_id": None, "disposition": ap.QUIET,
             "recorded_at_utc": "2026-11-24T00:00:00+00:00"}
    fresh_at = utc(2026, 11, 24, 0, 1)
    fresh = _genesis(fresh_at, [_absent_at(fresh_at)], ops=[other])["events"]
    assert fresh[0]["snapshot_through"] == "2026-11-24T00:00:00+00:00"
    stale = _genesis(utc(2026, 11, 13, 3, 5), [_absent_at(utc(2026, 11, 13, 3, 5))])["events"]
    assert stale[0]["episode_id"] == fresh[0]["episode_id"]          # reused identity
    log = [other] + ap.stamp_delivery(fresh, "failure") + ap.stamp_delivery(stale, "success")
    inc = ap.incidents_from(log)[KEY]
    assert inc.alert_pending and inc.unapplied == 1


def test_t1_concurrent_openers_from_one_snapshot_still_converge():
    a = _genesis(utc(2026, 10, 15, 3, 5), [_absent_at(utc(2026, 10, 15, 3, 0))])["events"]
    b = _genesis(utc(2026, 10, 15, 3, 7), [_absent_at(utc(2026, 10, 15, 3, 0))])["events"]
    for first, second in ((a, b), (b, a)):
        log = ap.stamp_delivery(first, "failure") + ap.stamp_delivery(second, "success")
        inc = ap.incidents_from(log)[KEY]
        assert inc.notified and inc.opened_at == "2026-10-15T03:05:00+00:00"


@pytest.mark.parametrize("order", ["alert_first", "quiet_first"])
@pytest.mark.parametrize("dup", [None, "quiet", "notify_failed"])
def test_t2_a_quiet_recovery_never_erases_a_notice_owed_after_delivery(tmp_path, order, dup):
    first = _genesis(utc(2026, 10, 15, 3, 5), [GONE])
    late = _genesis(utc(2026, 10, 15, 4, 5), [GONE])
    log = ap.stamp_delivery(first["events"], "failure")
    quiet = route([BACK], log, utc(2026, 10, 15, 15, 5), tmp_path)["events"]
    assert quiet[0]["disposition"] == ap.RECOVERY_QUIET
    pair = [ap.stamp_delivery(late["events"], "success"), ap.stamp_delivery(quiet, "skipped")]
    for batch in (pair if order == "alert_first" else pair[::-1]):
        log += batch
    if dup == "quiet":
        log += ap.stamp_delivery(route([BACK], ap.stamp_delivery(first["events"], "failure"),
                                       utc(2026, 10, 15, 15, 6), tmp_path)["events"], "skipped")
    elif dup == "notify_failed":
        notify = [{**quiet[0], "disposition": ap.RECOVERY_NOTIFY,
                   "recorded_at_utc": "2026-10-15T15:07:00+00:00"}]
        log += ap.stamp_delivery(notify, "failure")
    inc = ap.incidents_from(log)[KEY]
    assert not inc.open and inc.notified and inc.recovery_pending
    nxt = route([BACK], log, utc(2026, 10, 15, 21, 5), tmp_path)["events"]
    assert [(e["kind"], e["disposition"]) for e in nxt] == [(ap.RETRY, ap.RECOVERY_NOTIFY)]


@pytest.mark.parametrize("stamp", [None, "", "garbage", "2026-10-15"])
def test_t3_undated_legacy_rows_are_refused_even_on_an_empty_log(tmp_path, stamp):
    path = tmp_path / "ops.jsonl"
    legacy = {"schema": 1, "kind": ap.RETRACTION, "incident": KEY, "ticker": "SPY",
              "session": "2026-10-14", "disposition": ap.NOTIFY}
    if stamp is not None:
        legacy["recorded_at_utc"] = stamp
    with pytest.raises(ap.StaleBatch):
        ap.record(path, [legacy])
    assert not path.exists() and not Path(str(path) + ".lock").exists()
    dated = {**legacy, "recorded_at_utc": "2026-10-15T03:00:00+00:00"}
    assert ap.record(path, [dated]) == 1


# The following cases reproduce the independent T-fixes review findings.
def test_a_retry_from_an_expired_snapshot_cannot_confirm_fresh_genesis(tmp_path):
    first_at = utc(2026, 10, 15, 3, 5)
    old = ap.stamp_delivery(_genesis(first_at, [_absent_at(first_at)])["events"],
                            "failure")
    # The old operations history has expired. The stale router still holds it
    # and routes after a new genesis episode opens with the same derived ID.
    fresh_at = first_at + timedelta(days=31)
    fresh = ap.stamp_delivery(_genesis(fresh_at, [_absent_at(fresh_at)])["events"],
                              "failure")
    retry_at = fresh_at + timedelta(minutes=1)
    stale = route([_absent_at(retry_at)], old, retry_at, tmp_path)["events"]
    assert stale[0]["episode_id"] == fresh[0]["episode_id"]
    assert stale[0]["opener_event_id"] == old[0]["event_id"]
    assert stale[0]["opener_event_id"] != fresh[0]["event_id"]
    inc = ap.incidents_from(fresh + ap.stamp_delivery(stale, "success"))[KEY]
    assert inc.alert_pending and not inc.notified


def test_later_reopen_absence_survives_unrelated_ops_snapshot_skew(tmp_path):
    first = _open_incident(tmp_path, delivered_ok=True)
    back_at_nine = vrow(utc(2026, 10, 15, 9, 5).isoformat(), "SPY",
                        "2026-10-14", "2026-10-14", checked=WINDOW)
    closed = first + ap.stamp_delivery(
        route([back_at_nine], first, utc(2026, 10, 15, 9, 5), tmp_path)["events"],
        "failure")
    unrelated = {**_events("OTHER", 1)[0],
                 "recorded_at_utc": utc(2026, 10, 15, 10, 5).isoformat()}
    unrelated["event_id"] = ap._event_id(unrelated)
    with_unrelated = closed + [unrelated]
    early_at = utc(2026, 10, 15, 11, 5)
    late_at = utc(2026, 10, 15, 15, 5)
    early = route([_absent_at(early_at)], with_unrelated, early_at, tmp_path)
    late = route([_absent_at(late_at)], closed, late_at, tmp_path)
    assert early["events"][0]["kind"] == late["events"][0]["kind"] == ap.REOPEN
    assert early["events"][0]["snapshot_through"] != late["events"][0]["snapshot_through"]
    log = (with_unrelated + ap.stamp_delivery(early["events"], "skipped")
           + ap.stamp_delivery(late["events"], "skipped"))
    inc = ap.incidents_from(log)[KEY]
    assert inc.withdrawn_observed_at == late_at.isoformat()
    earlier_proof = vrow(utc(2026, 10, 15, 12, 5).isoformat(), "SPY",
                         "2026-10-14", "2026-10-14", checked=WINDOW)
    verdict = route([earlier_proof], log, utc(2026, 10, 15, 16, 5), tmp_path)
    assert not verdict["recovery"]


def test_older_ops_snapshot_preserves_later_anomalous_escalation(tmp_path):
    unrelated = {**_events("OTHER", 1)[0],
                 "recorded_at_utc": utc(2026, 10, 15, 10, 5).isoformat()}
    unrelated["event_id"] = ap._event_id(unrelated)
    quiet_at = utc(2026, 10, 15, 11, 5)
    routine = {"ticker": "SPY", "venue": "NYSE", "was": "2026-10-14",
               "now": "2026-10-13", "routine": True, "measuring": False}
    first = ap.evaluate(vendor_rows=[_absent_at(quiet_at)],
                        retractions=[routine], ops_rows=[unrelated], book=None,
                        delivery=None, now=quiet_at, data_dir=tmp_path,
                        configured=({}, ()))
    assert first["events"][0]["disposition"] == ap.QUIET
    later_at = utc(2026, 10, 15, 15, 5)
    later = _genesis(later_at, [_absent_at(later_at)])
    assert later["events"][0]["disposition"] == ap.NOTIFY
    log = ([unrelated] + ap.stamp_delivery(first["events"], "skipped")
           + ap.stamp_delivery(later["events"], "success"))
    inc = ap.incidents_from(log)[KEY]
    assert inc.alert_pending and not inc.notified
    retry = route([_absent_at(utc(2026, 10, 15, 16, 5))], log,
                  utc(2026, 10, 15, 16, 5), tmp_path)
    assert [(e["kind"], e["disposition"]) for e in retry["events"]] == [
        (ap.RETRY, ap.NOTIFY)]


def test_an_earlier_record_cannot_prove_an_untimed_later_withdrawal(tmp_path):
    earlier = vrow(utc(2026, 10, 15, 3, 5).isoformat(), "SPY",
                   "2026-10-14", "2026-10-14", checked=WINDOW)
    withdrawal = _absent_at(utc(2026, 10, 15, 4, 5))
    withdrawal["probed_at_utc"] = "unreadable"
    opened = ap.stamp_delivery(_genesis(utc(2026, 10, 15, 4, 5),
                                        [earlier, withdrawal])["events"], "failure")
    stale = route([earlier], opened, utc(2026, 10, 15, 5, 5), tmp_path)
    assert not any(e["kind"] == ap.RECOVERY for e in stale["events"])
    later = vrow(utc(2026, 10, 15, 6, 5).isoformat(), "SPY",
                 "2026-10-14", "2026-10-14", checked=WINDOW)
    valid = route([withdrawal, later], opened, utc(2026, 10, 15, 6, 5), tmp_path)
    assert any(e["kind"] == ap.RECOVERY for e in valid["events"])


def test_an_isolated_proof_cannot_order_an_untimed_withdrawal(tmp_path):
    withdrawal = _absent_at(utc(2026, 10, 15, 4, 5))
    withdrawal["probed_at_utc"] = "unreadable"
    opened = ap.stamp_delivery(_genesis(utc(2026, 10, 15, 3, 5),
                                        [withdrawal])["events"], "failure")
    proof = vrow(utc(2026, 10, 15, 3, 35).isoformat(), "SPY",
                 "2026-10-14", "2026-10-14", checked=WINDOW)
    verdict = route([proof], opened, utc(2026, 10, 15, 3, 35), tmp_path)
    assert not any(e["kind"] == ap.RECOVERY for e in verdict["events"])


def test_absolute_probe_line_proves_order_after_withdrawal_leaves_lookback(tmp_path):
    withdrawal = _absent_at(utc(2026, 10, 15, 4, 5))
    withdrawal["probed_at_utc"] = "unreadable"
    unrelated = vrow(utc(2026, 10, 15, 5, 5).isoformat(), "OTHER",
                     "2026-10-14", "2026-10-14", checked=WINDOW)
    later = vrow(utc(2026, 10, 15, 6, 5).isoformat(), "SPY",
                 "2026-10-14", "2026-10-14", checked=WINDOW)
    vendor_log = tmp_path / "vendor.jsonl"
    vendor_log.write_text("".join(json.dumps(r) + "\n" for r in
                                  (withdrawal, unrelated, later)), encoding="utf-8")
    opened = ap.stamp_delivery(_genesis(
        utc(2026, 10, 15, 4, 5), ap.load_history(vendor_log, 2)[:1]
    )["events"], "failure")
    inc = ap.incidents_from(opened)[KEY]
    assert inc.withdrawn_record_position == 0
    newest_only = ap.load_history(vendor_log, 0)
    assert len(newest_only) == 1 and newest_only[0]["_log_line"] == 2
    assert ap.record_identity(newest_only[0]) == ap.record_identity(later)
    verdict = route(newest_only, opened, utc(2026, 10, 15, 6, 5), tmp_path)
    assert any(e["kind"] == ap.RECOVERY for e in verdict["events"])


@pytest.mark.parametrize("seeded", [False, True])
def test_a_legacy_stale_row_does_not_introduce_an_episode_in_a_batch(tmp_path, seeded):
    path = tmp_path / "ops.jsonl"
    legacy = {"schema": 1, "kind": ap.STALE, "incident": KEY,
              "ticker": "SPY", "session": "2026-10-14",
              "recorded_at_utc": utc(2026, 10, 15, 3, 5).isoformat(),
              "disposition": ap.QUIET}
    pending = ap.stamp_delivery(_genesis(utc(2026, 10, 15, 3, 5),
                                         [_absent_at(utc(2026, 10, 15, 3, 5))])["events"],
                                "failure")
    retry = route([_absent_at(utc(2026, 10, 15, 4, 5))], pending,
                  utc(2026, 10, 15, 4, 5), tmp_path)["events"]
    if seeded:
        ap.record(path, _events("OTHER", 1))
    before = path.read_bytes() if path.exists() else b""
    with pytest.raises(ap.PredecessorExpired):
        ap.record(path, [legacy, ap.stamp_delivery(retry, "success")[0]])
    assert (path.read_bytes() if path.exists() else b"") == before


# F1/F2 follow-up: source-ordered stale withdrawals and the recording clock.
@pytest.mark.parametrize("order", ["stale_first", "fresh_first"])
def test_f1_old_snapshot_withdrawal_and_fresh_opener_converge(tmp_path, order):
    recovered = _cycle(tmp_path)
    old_id = ap.incidents_from(recovered)[KEY].episode_id
    stale = ap.stamp_delivery(route([SEEN, BACK, AGAIN], [],
                                    utc(2026, 10, 16, 3, 5), tmp_path)["events"],
                              "success")
    fresh = ap.stamp_delivery(route([SEEN, BACK, AGAIN], recovered,
                                    utc(2026, 10, 16, 3, 6), tmp_path)["events"],
                              "success")
    assert stale[0]["episode_id"] == old_id
    assert fresh[0]["episode_id"] == ap.episode_identity(KEY, old_id)
    only_stale = ap.incidents_from(recovered + stale)[KEY]
    assert only_stale.open and only_stale.alert == ap.ALERT_PENDING
    assert only_stale.contradicted and only_stale.correction_owed
    rows = recovered + (stale + fresh if order == "stale_first" else fresh + stale)
    inc = ap.incidents_from(rows)[KEY]
    assert inc.open and inc.episode_id == fresh[0]["episode_id"]
    # The fresh opener's confirmed WARN discharges the stale successor's
    # correction obligation in either persistence order.
    assert inc.alert == ap.ALERT_DELIVERED and not inc.correction_owed
    assert inc.correction == "delivered"


@pytest.mark.parametrize("classification", ["routine", "measuring"])
def test_g1_quiet_successor_never_owes_correction(tmp_path, classification):
    recovered = _cycle(tmp_path)
    withdrawal = {"ticker": "SPY", "venue": "NYSE", "was": "2026-10-14",
                  "now": "2026-10-13", "routine": classification == "routine",
                  "measuring": classification == "measuring"}
    opened = ap.evaluate(vendor_rows=[AGAIN], retractions=[withdrawal],
                         ops_rows=recovered, book=None, delivery=None,
                         now=utc(2026, 10, 16, 3, 5), data_dir=tmp_path)
    assert [e["disposition"] for e in opened["events"]] == [ap.QUIET]
    log = recovered + ap.stamp_delivery(opened["events"], "skipped")
    inc = ap.incidents_from(log)[KEY]
    assert inc.open and not inc.correction_owed
    later = ap.evaluate(vendor_rows=[AGAIN], retractions=[], ops_rows=log,
                        book=None, delivery=None,
                        now=utc(2026, 10, 16, 9, 5), data_dir=tmp_path)
    assert not later["notifying"]


def test_g1_pending_warn_suppresses_separate_correction(tmp_path):
    recovered = _cycle(tmp_path)
    stale = route([SEEN, BACK, AGAIN], [], utc(2026, 10, 16, 3, 5),
                  tmp_path)["events"]
    log = recovered + ap.stamp_delivery(stale, "failure")
    inc = ap.incidents_from(log)[KEY]
    assert inc.alert_pending and inc.correction_owed
    later = route([AGAIN], log, utc(2026, 10, 16, 9, 5), tmp_path)
    assert [e["disposition"] for e in later["notifying"]] == [ap.NOTIFY]


def test_f1_untimed_stale_withdrawal_needs_source_record_order(tmp_path):
    recovered = _cycle(tmp_path)
    recovered[-1] = {**recovered[-1], "evidence_record": "proof",
                     "evidence_record_position": 10}
    stale = ap.stamp_delivery(route([SEEN, BACK, AGAIN], [],
                                    utc(2026, 10, 16, 3, 5), tmp_path)["events"],
                              "success")[0]
    stale = {**stale, "observed_at": None, "observed_record": "later",
             "observed_record_position": 11}
    assert ap.incidents_from(recovered + [stale])[KEY].correction_owed
    unordered = {**stale, "observed_record_position": 10}
    inc = ap.incidents_from(recovered + [unordered])[KEY]
    assert not inc.open and not inc.owes_delivery and inc.unapplied >= 1


def test_f1_stale_closed_episode_requires_fresh_opener(tmp_path):
    opened = ap.stamp_delivery(route([SEEN, GONE], [],
                                     utc(2026, 10, 15, 3, 5), tmp_path)["events"],
                               "skipped")
    opened[0]["disposition"] = ap.QUIET
    opened[0]["delivery"] = ap.DELIVERY_NOT_REQUIRED
    stale_close = {"schema": 2, "kind": ap.STALE, "incident": KEY,
                   "episode_id": opened[0]["episode_id"],
                   "opener_event_id": opened[0]["event_id"],
                   "recorded_at_utc": utc(2026, 10, 30, 3, 5).isoformat()}
    closed = opened + [stale_close]
    assert ap.incidents_from(closed)[KEY].closed_by == ap.STALE
    delayed = {**opened[0], "observed_at": utc(2026, 10, 16, 3).isoformat(),
               "recorded_at_utc": utc(2026, 10, 31, 3, 5).isoformat(),
               "delivery": ap.DELIVERY_FAILED}
    inc = ap.incidents_from(closed + [delayed])[KEY]
    assert not inc.open and inc.closed_by == ap.STALE and inc.unapplied >= 1


@pytest.mark.parametrize("clock", [utc(2026, 10, 31, 23, 58),
                                     utc(2026, 12, 31, 23, 58)])
def test_f2_future_tolerance_crosses_month_and_year_atomically(tmp_path, clock):
    # Python datetime months are 1-indexed; timedelta crosses the boundary.
    path = tmp_path / "ops.jsonl"
    allowed = {**_events("OK", 1)[0],
               "recorded_at_utc": (clock + ap.MAX_FUTURE_SKEW).isoformat()}
    allowed["event_id"] = ap._event_id(allowed)
    assert ap.record(path, [allowed], now=clock) == 1
    before = path.read_bytes()
    future = {**_events("BAD", 1)[0],
              "recorded_at_utc": (clock + ap.MAX_FUTURE_SKEW
                                  + timedelta(seconds=1)).isoformat()}
    future["event_id"] = ap._event_id(future)
    with pytest.raises(ap.StaleBatch):
        ap.record(path, [_events("OTHER", 1)[0], future], now=clock)
    assert path.read_bytes() == before


def test_f2_future_existing_row_cannot_advance_retention_horizon(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    monkeypatch.setattr(ap, "MAX_OPS_ROWS", 1)
    clock = utc(2026, 10, 16, 3, 5)
    tomb = {"schema": 2, "kind": ap.TOMBSTONE, "tombstone_of": KEY,
            "episode_id": ap.episode_identity(KEY, None),
            "recorded_at_utc": utc(2026, 10, 15, 3, 5).isoformat()}
    poison = {**_events("FUTURE", 1)[0],
              "recorded_at_utc": utc(2026, 12, 15, 3, 5).isoformat()}
    path.write_text("".join(json.dumps(r) + "\n" for r in (tomb, poison)),
                    encoding="utf-8")
    current = {**_events("CURRENT", 1)[0],
               "recorded_at_utc": clock.isoformat()}
    current["event_id"] = ap._event_id(current)
    assert ap.record(path, [current], now=clock) == 1
    rows = ap.read_ops_log(path)
    assert any(r.get("tombstone_of") == KEY for r in rows)
    assert any(r.get("incident") == current["incident"] for r in rows)
