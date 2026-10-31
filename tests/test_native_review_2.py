"""Adversarial regressions from the 2026-10-30 review of 451e5008.

Four findings were reproduced on 451e5008; each test marked below fails on
that commit and passes after the fix.

1. GENERATIONS COMBINED (P1). strategy_freshness read the thematic parquet
   twice (native_reach, then cache_reach) and the prior report twice
   (cache_reach_or_carried, then _prior_row). A cache replaced between the
   reads, or a report rewritten between them, combined two generations --
   neither current on its own -- into a current verdict. Each is now read
   once and every part of the verdict comes from that one object.
2. MALFORMED PROVENANCE (P2). A prior report whose ``native_provenance`` was
   a string, a list or a number raised AttributeError and aborted the whole
   report. The container and each record's fields are now validated; a bad
   record makes sleeve C unknown and the other sleeves are still reported.
3. CALENDAR BYPASS (P2). combined_book_risk.expected_last_session and the
   holdings exporter (interior_gaps, missing_completed_session, the
   completed-session trims) read the raw XSHG library calendar, so with a
   validated pin they expected a session sleeve C's window check calls
   closed. They now read the effective calendar; a pin conflict falls back
   to the library (more bars demanded, never fewer) and is reported, and a
   calendar fault is a reason, not a pass. NYSE and XETR are unchanged.
4. DIGEST PRECISION (P3). content_digest formatted the index with ``%f``,
   so a sub-microsecond change kept the digest. Stamps are now hashed as
   int64 nanoseconds.

The 2027 closures here are HYPOTHETICAL test input, never pinned in source.
Offline; nothing leaves tmp_path. Python datetime months are 1-indexed
(January = 1); sessions come from the venue calendars, never a weekday count.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import china_market_holidays as cmh  # noqa: E402
import native_sessions as ns  # noqa: E402
import price_source as ps  # noqa: E402
import strategy_freshness as sf  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
W = 200
HYPOTHETICAL_2027 = ("2027-01-01", "2027-01-29", "2027-02-01", "2027-02-08",
                     "2027-02-09", "2027-02-10", "2027-02-11", "2027-10-01")


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


def _weekdays(start, end):
    d = pd.date_range(start, end, freq="D")
    return d[d.weekday < 5]


# ---------------------------------------------------------------------------
# 1. One snapshot per verdict: the present cache
# ---------------------------------------------------------------------------

ROWS = _sessions("NYSE", "2025-03-03", "2026-04-17")
XSHG = _sessions("XSHG", "2025-03-03", "2026-04-17")
GOOD_FRIDAY = pd.Timestamp("2026-04-03")      # Shenzhen traded; NYSE shut


def _generations():
    """A: native line complete, US line X missing its final close (behind on
    X). B: X restored, Good Friday's Shenzhen bar withdrawn so the Qingming
    row carries Thursday (behind on 159801.SZ). Neither is current."""
    a = pd.DataFrame({"X": 1.0, CN: 0.5 + 0.001 * np.arange(len(ROWS))}, index=ROWS)
    a.iloc[-1, 0] = np.nan
    a_side = {ns.SIDECAR_KEY: ns.sidecar_record({CN: XSHG}, a)}
    b = a.copy()
    b.iloc[-1, 0] = 1.0
    b.at[pd.Timestamp("2026-04-06"), CN] = b.at[pd.Timestamp("2026-04-02"), CN]
    b_side = {ns.SIDECAR_KEY: ns.sidecar_record({CN: XSHG[XSHG != GOOD_FRIDAY]}, b)}
    return (a, a_side), (b, b_side)


def _write(cache, frame, side):
    frame.to_parquet(cache)
    ps.sidecar_path(cache).write_text(
        json.dumps({"source": "yfinance", **side}), encoding="utf-8")


def _measure(cache):
    return sf.native_cache_reach_or_carried(cache, ["X", CN], [CN], "C", "x",
                                            window_rows=W)


def _status(reach, last="2026-04-17"):
    return sf.classify(reach, last, "NYSE")[0]


def test_neither_generation_alone_is_current(tmp_path):
    cache = tmp_path / "th.parquet"
    (a, a_side), (b, b_side) = _generations()
    _write(cache, a, a_side)
    reach, lag, *_ = _measure(cache)
    assert (reach, lag, _status(reach)) == ("2026-04-16", ["X"], sf.BEHIND)
    _write(cache, b, b_side)
    reach, lag, *_ = _measure(cache)
    assert (reach, lag, _status(reach)) == ("2026-04-02", [CN], sf.BEHIND)


def test_a_cache_replaced_between_reads_is_not_combined(tmp_path, monkeypatch):
    """Fails on 451e5008: native_reach measured A, then cache_reach read B
    with A's native bound, and the verdict was current on 2026-04-17."""
    cache = tmp_path / "th.parquet"
    (a, a_side), (b, b_side) = _generations()
    _write(cache, a, a_side)
    real = pd.read_parquet
    calls = {"n": 0}

    def swapping(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2 and Path(path) == cache:
            _write(cache, b, b_side)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(pd, "read_parquet", swapping)
    reach, lag, *_ = _measure(cache)
    assert calls["n"] == 1
    assert _status(reach) != sf.CURRENT
    assert (reach, lag) == ("2026-04-16", ["X"])     # wholly generation A


def test_a_cache_replaced_after_the_frame_read_is_unknown(tmp_path, monkeypatch):
    """The sidecar is read after the frame; if both files are replaced in
    between, the record's digest does not match the frame in hand."""
    cache = tmp_path / "th.parquet"
    (a, a_side), (b, b_side) = _generations()
    _write(cache, a, a_side)
    real = pd.read_parquet

    def read_then_swap(path, *args, **kwargs):
        got = real(path, *args, **kwargs)
        if Path(path) == cache:
            _write(cache, b, b_side)
        return got
    monkeypatch.setattr(pd, "read_parquet", read_then_swap)
    reach, lag, _, _, native = _measure(cache)
    assert reach is None and CN in lag and _status(reach) == sf.UNKNOWN
    assert native[CN]["last_valid"] is None


def test_a_cache_removed_between_check_and_read_is_absent(tmp_path, monkeypatch):
    cache = tmp_path / "th.parquet"
    (a, a_side), _ = _generations()
    _write(cache, a, a_side)
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)

    def gone(path, *args, **kwargs):
        raise FileNotFoundError(path)
    monkeypatch.setattr(pd, "read_parquet", gone)
    reach, lag, _, carried, native = _measure(cache)
    assert reach is None and carried is False
    assert "not present" in native[CN]["error"]


# ---------------------------------------------------------------------------
# 1. One snapshot per verdict: the absent cache and the prior report
# ---------------------------------------------------------------------------

LEGACY = {"computed_at_utc": "2026-10-28T02:00:00+00:00",
          "strategies": [{"sleeve": "C", "data_through": "2026-10-28",
                          "laggards": []}]}
NEWER = {"computed_at_utc": "2026-10-29T02:00:00+00:00",
         "strategies": [{"sleeve": "C", "data_through": "2026-10-27",
                         "laggards": ["X"], "native_provenance": {CN: {
                             "venue": "XSHG", "last_valid": "2026-10-28",
                             "window_rows": W}}}]}


def _report(tmp_path, blob):
    (tmp_path / "strategy_freshness.json").write_text(json.dumps(blob),
                                                      encoding="utf-8")


def _carry(tmp_path):
    return sf.native_cache_reach_or_carried(
        tmp_path / "absent.parquet", ["X", CN], [CN], "C", "x", window_rows=W)


def test_neither_report_alone_carries_current(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _report(tmp_path, LEGACY)
    reach, _, _, carried, _ = _carry(tmp_path)
    assert reach is None and carried is False
    _report(tmp_path, NEWER)
    reach, _, _, carried, _ = _carry(tmp_path)
    assert (reach, carried) == ("2026-10-27", True)
    assert _status(reach, "2026-10-28") == sf.BEHIND


def test_a_report_rewritten_between_reads_is_not_combined(tmp_path, monkeypatch):
    """Fails on 451e5008: the bound came from the legacy report and the
    native records from the newer one, carried as current on 2026-10-28."""
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _report(tmp_path, LEGACY)
    real = sf._prior_row
    calls = {"n": 0}

    def swapping(sleeve):
        calls["n"] += 1
        got = real(sleeve)
        _report(tmp_path, NEWER)
        return got
    monkeypatch.setattr(sf, "_prior_row", swapping)
    reach, _, _, carried, _ = _carry(tmp_path)
    assert calls["n"] == 1
    assert reach is None and carried is False


def test_a_whole_carried_record_is_still_carried(tmp_path, monkeypatch):
    """The fix keeps the valid carry: every field in its documented shape."""
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    rec = {"venue": "XSHG", "last_valid": "2026-10-28", "window_rows": W,
           "last_observed_native_close": "2026-10-28",
           "required_native_session": "2026-10-28",
           "native_close_carried": "2026-10-28", "missing_native_sessions": [],
           "holiday_data_through": "2026-12-31", "error": None}
    _report(tmp_path, {"computed_at_utc": "2026-10-29T02:00:00+00:00",
                       "strategies": [{"sleeve": "C", "data_through": "2026-10-28",
                                       "laggards": [],
                                       "native_provenance": {CN: rec}}]})
    reach, lag, src, carried, native = _carry(tmp_path)
    assert (reach, lag, carried) == ("2026-10-28", [], True)
    assert native[CN] == rec and "carried" in src


# ---------------------------------------------------------------------------
# 2. Malformed provenance never aborts the report
# ---------------------------------------------------------------------------

GOOD = {"venue": "XSHG", "last_valid": "2026-10-28", "window_rows": W}


@pytest.mark.parametrize("prov", [
    "broken", ["x"], 42, True,                                     # the container
    {CN: "x"}, {CN: ["x"]}, {CN: None},                            # the record
    {CN: {**GOOD, "missing_native_sessions": 42}},
    {CN: {**GOOD, "missing_native_sessions": "2026-10-27"}},
    {CN: {**GOOD, "missing_native_sessions": ["2026-9-01"]}},
    {CN: {**GOOD, "missing_native_sessions": [None]}},
    {CN: {**GOOD, "native_close_carried": "garbled"}},
    {CN: {**GOOD, "required_native_session": 20261028}},
    {CN: {**GOOD, "last_observed_native_close": ["2026-10-28"]}},
    {CN: {**GOOD, "holiday_data_through": "2026-13-01"}},
    {CN: {**GOOD, "error": 7}},
], ids=repr)
def test_a_malformed_native_record_is_unknown_not_a_crash(tmp_path, monkeypatch, prov):
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _report(tmp_path, {"computed_at_utc": "2026-10-29T02:00:00+00:00",
                       "strategies": [{"sleeve": "C", "data_through": "2026-10-28",
                                       "laggards": [], "native_provenance": prov}]})
    reach, lag, _, carried, native = _carry(tmp_path)
    assert reach is None and lag == [CN] and carried is False
    assert native[CN]["last_valid"] is None and native[CN]["error"]
    sf._native_reason(native, lag)                  # renders without raising


@pytest.mark.parametrize("laggards", ["X", 42, [1], {"X": 1}], ids=repr)
def test_malformed_carried_laggards_are_unknown(tmp_path, monkeypatch, laggards):
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _report(tmp_path, {"strategies": [{"sleeve": "C", "data_through": "2026-10-28",
                                       "laggards": laggards,
                                       "native_provenance": {CN: GOOD}}]})
    reach, _, _, carried, _ = _carry(tmp_path)
    assert reach is None and carried is False


@pytest.mark.parametrize("blob", [
    [], "x", 42, {"strategies": "x"}, {"strategies": {"sleeve": "C"}},
    {"strategies": ["x", 42, None]},
    {"computed_at_utc": 42, "strategies": [
        {"sleeve": "C", "data_through": "2026-10-28", "laggards": [],
         "native_provenance": {CN: GOOD}}]},
], ids=repr)
def test_a_malformed_report_never_raises(tmp_path, monkeypatch, blob):
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _report(tmp_path, blob)
    reach, lag, src, carried, native = _carry(tmp_path)
    assert set(native) == {CN}
    if carried:                    # only the last case is well formed
        assert reach == "2026-10-28" and "report of" not in src
    sf.cache_reach_or_carried(tmp_path / "absent.parquet", ["X"], "C", "x")


def test_build_reports_every_sleeve_when_c_provenance_is_malformed(tmp_path, monkeypatch):
    """Fails on 451e5008: build() raised AttributeError and no sleeve was
    reported."""
    import run_thematic_rotation as th
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS

    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    for etf in list(UNIVERSE_ETFS) + list(UNIVERSE_EUROPE_SECTORS):
        (tmp_path / f"breadth_{etf.lower()}.json").write_text(
            json.dumps({"end_date": "2026-10-28"}), encoding="utf-8")
    ac_cache = tmp_path / "ac.parquet"
    pd.DataFrame({"SPY": 1.0}, index=_sessions("NYSE", "2026-01-02", "2026-10-28")
                 ).to_parquet(ac_cache)
    monkeypatch.setitem(sys.modules, "run_asset_class_rotation",
                        types.SimpleNamespace(PRICE_CACHE=ac_cache, TICKERS=["SPY"]))
    monkeypatch.setattr(th, "PRICE_CACHE", tmp_path / "absent-thematic.parquet")
    _report(tmp_path, {"computed_at_utc": "2026-10-29T02:00:00+00:00",
                       "strategies": [{"sleeve": "C", "data_through": "2026-10-28",
                                       "laggards": [], "native_provenance": "broken"}]})
    r = sf.build(now_utc=datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc))
    rows = {s["sleeve"]: s for s in r["strategies"]}
    assert set(rows) == {"A", "B", "C", "D"}
    assert rows["C"]["status"] == sf.UNKNOWN and rows["C"]["carried"] is False
    for key in ("A", "B", "D"):
        assert rows[key]["status"] == sf.CURRENT, rows[key]
    json.dumps(r)


# ---------------------------------------------------------------------------
# 3. One effective Shenzhen calendar for the downstream readers
# ---------------------------------------------------------------------------

@pytest.fixture
def pin_2027(monkeypatch):
    monkeypatch.setattr(cmh, "_PINNED", {2027: cmh.validate_year(2027, HYPOTHETICAL_2027)})


@pytest.fixture
def conflicting_2026(monkeypatch):
    """A 2026 pin that adds a traded day (30 September) to the library's
    complete year: checked_pins refuses it."""
    lib = get_calendar("XSHG")
    real = sorted(str(d.date()) for d in pd.DatetimeIndex(lib.adhoc_holidays)
                  if d.year == 2026 and d.weekday() < 5)
    monkeypatch.setattr(cmh, "_PINNED",
                        {2026: cmh.validate_year(2026, real + ["2026-09-30"])})


# 18 Jan - 5 Feb 2027 as Shenzhen would trade it under the hypothetical pin.
TRUE_SZ_2027 = [str(d.date()) for d in _weekdays("2027-01-18", "2027-02-05")
                if str(d.date()) not in HYPOTHETICAL_2027]


def test_no_2027_closure_is_pinned_in_source():
    assert 2027 not in cmh.PINNED_CLOSURES and 2027 not in cmh._PINNED


def test_risk_reader_expects_the_effective_last_session(pin_2027):
    """Fails on 451e5008: 2027-01-29 was expected from the raw library."""
    import combined_book_risk as cbr
    sched, _ = ns.native_schedule("XSHG", "2027-01-20", "2027-01-29")
    assert pd.Timestamp(sched.index[-1]).date() == date(2027, 1, 28)
    assert cbr.expected_last_session(CN, date(2027, 1, 29)) == date(2027, 1, 28)
    got = cbr.load_prices({CN: {"dates": TRUE_SZ_2027,
                                "prices": [1 + i / 1000 for i in range(len(TRUE_SZ_2027))]}},
                          [CN], date(2027, 1, 29))
    assert got[1] == []


def test_exporter_reads_the_effective_calendar(pin_2027):
    """Fails on 451e5008: interior_gaps named the pinned 29 Jan and 1 Feb, and
    a close for 29 Jan was requested from the vendor."""
    import export_holdings_prices as ehp
    assert ehp.interior_gaps({"dates": TRUE_SZ_2027}, CN) == []
    assert ehp.interior_gap_check({"dates": TRUE_SZ_2027}, CN) == ([], None)
    upto = [d for d in TRUE_SZ_2027 if d <= "2027-01-28"]
    now = datetime(2027, 1, 30, 3, 0, tzinfo=timezone.utc)
    assert ehp.missing_completed_session(CN, {"dates": upto}, now) is False
    # The completed-session trims use the same calendar: a bar dated on the
    # pinned closure lies after the last completed session and is cut.
    close = pd.Series(1.0, index=pd.DatetimeIndex(upto + ["2027-01-29"]))
    trimmed, dropped = ehp.completed_sessions_only(CN, close, now)
    assert dropped == ["2027-01-29"]
    assert str(trimmed.index[-1].date()) == "2027-01-28"
    # The shared library handle is not mutated.
    lib = get_calendar("XSHG").schedule(start_date="2027-01-25", end_date="2027-01-29")
    assert pd.Timestamp("2027-01-29") in pd.DatetimeIndex(lib.index).normalize()


def test_a_pin_conflict_fails_closed_in_every_reader(conflicting_2026):
    import combined_book_risk as cbr
    import export_holdings_prices as ehp
    with pytest.raises(cmh.HolidayPinError):
        ns.native_schedule("XSHG", "2026-09-20", "2026-10-02")
    cal, conflict = ns.completeness_calendar("XSHG")
    assert cal is get_calendar("XSHG") and conflict
    sz = [str(d.date()) for d in _sessions("XSHG", "2026-09-01", "2026-10-28")]
    sz_wo = [d for d in sz if d != "2026-09-30"]
    gaps, problem = ehp.interior_gap_check({"dates": sz_wo}, CN)
    assert gaps == ["2026-09-30"] and "pins refused" in problem
    _, reasons, _ = cbr.load_prices(
        {CN: {"dates": sz, "prices": [1 + i / 1000 for i in range(len(sz))]}},
        [CN], date(2026, 10, 28))
    assert any("expected sessions not established" in r for r in reasons), reasons


def test_a_calendar_fault_is_a_reason_not_a_pass(monkeypatch):
    """Before the fix interior_gaps swallowed any calendar exception as []."""
    import combined_book_risk as cbr
    import export_holdings_prices as ehp
    real = ns.get_calendar

    def broken(name):
        if name == "XSHG":
            raise RuntimeError("calendar library unavailable")
        return real(name)
    monkeypatch.setattr(ns, "get_calendar", broken)
    sz = [str(d.date()) for d in _sessions("XSHG", "2026-09-01", "2026-10-28")]
    gaps, problem = ehp.interior_gap_check({"dates": sz}, CN)
    assert gaps == [] and "unavailable" in problem
    _, reasons, _ = cbr.load_prices(
        {CN: {"dates": sz, "prices": [1 + i / 1000 for i in range(len(sz))]}},
        [CN], date(2026, 10, 28))
    assert any("not established" in r for r in reasons), reasons


@pytest.mark.parametrize("sym,venue", [("SPY", "NYSE"), ("EXH4.DE", "XETR")])
def test_nyse_and_xetra_are_the_library_calendars(conflicting_2026, sym, venue):
    import combined_book_risk as cbr
    import export_holdings_prices as ehp
    assert ehp._venue_calendar(venue) is get_calendar(venue)
    assert ns.effective_calendar(venue) is get_calendar(venue)
    assert ehp.calendar_problem(venue) is None
    lib = get_calendar(venue).schedule(start_date="2026-10-14", end_date="2026-10-28")
    assert cbr.expected_last_session(sym, date(2026, 10, 28)) == \
        pd.Timestamp(lib.index[-1]).date()


# ---------------------------------------------------------------------------
# 4. The digest at full timestamp precision
# ---------------------------------------------------------------------------

def _frame():
    rows = _sessions("NYSE", "2025-03-03", "2026-04-17")
    f = pd.DataFrame({"X": 2.0, CN: 0.5 + 0.001 * np.arange(len(rows))}, index=rows)
    f.iloc[5, 1] = np.nan
    return f


def _record(frame):
    return {ns.SIDECAR_KEY: ns.sidecar_record({CN: frame.index}, frame)}


def test_a_sub_microsecond_index_change_breaks_the_digest():
    """Fails on 451e5008: %f dropped the nanoseconds."""
    f = _frame()
    side = _record(f)
    for shift in (1, 999):
        g = f.copy()
        g.index = g.index.as_unit("ns") + pd.Timedelta(nanoseconds=shift)
        assert ns.content_digest(g, [CN]) != ns.content_digest(f, [CN])
        assert ns.parse_sidecar(side, g, [CN]) is None


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_the_digest_is_independent_of_resolution(unit):
    f = _frame()
    g = f.copy()
    g.index = g.index.as_unit(unit)
    assert ns.content_digest(g, [CN]) == ns.content_digest(f, [CN])


def test_the_digest_survives_a_parquet_round_trip_and_column_order(tmp_path):
    f = _frame()
    side = _record(f)                    # names f's record on f (2026-10-30)
    p = tmp_path / "f.parquet"
    f[[CN, "X"]].to_parquet(p)
    g = pd.read_parquet(p)
    assert ns.content_digest(g, [CN]) == ns.content_digest(f, [CN])
    assert ns.parse_sidecar(side, g, [CN]) is not None


def test_nan_forms_hash_alike():
    f = _frame()
    g = f.copy()
    g[CN] = g[CN].astype(object)
    g.iloc[5, 1] = None
    h = f.copy()
    h[CN] = h[CN].astype("Float64")
    h.iloc[5, 1] = pd.NA
    d = ns.content_digest(f, [CN])
    assert ns.content_digest(g, [CN]) == d == ns.content_digest(h, [CN])


def test_the_tz_is_part_of_the_digest():
    f = _frame()
    g = f.copy()
    g.index = g.index.tz_localize("UTC")
    assert ns.content_digest(g, [CN]) != ns.content_digest(f, [CN])


def test_scope_is_native_columns_plus_index():
    f = _frame()
    side = _record(f)
    us_repair = f.copy()
    us_repair.iloc[10, 0] = 2.5                      # a US column on an existing row
    assert ns.parse_sidecar(side, us_repair, [CN]) is not None
    native = f.copy()
    native.iloc[10, 1] += 1e-12                      # a native value
    assert ns.parse_sidecar(side, native, [CN]) is None
    dropped = f.drop(f.index[10])                    # a row removed
    assert ns.parse_sidecar(side, dropped, [CN]) is None
    added = pd.concat([f, f.iloc[[10]].set_axis(
        [f.index[10] + pd.Timedelta(hours=1)])]).sort_index()   # a row added
    assert ns.parse_sidecar(side, added, [CN]) is None
