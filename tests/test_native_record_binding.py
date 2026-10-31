"""Adversarial regressions from the 2026-10-29 review of fix/native-window-hold.

Four defects were reproduced against 30ac1ed + 99039858; each test below fails
on that code and passes after the fix.

1. GENERATIONS. The parquet is replaced before its sidecar is written. If the
   sidecar write fails, the old record (same last row) sat beside a new frame
   whose Qingming row was forward-filled, and the restart reused the pair:
   READY, no missing session, freshness current. A record now carries the
   digest of the frame's rows and native values (native_sessions.
   content_digest) and is refused for any other frame.
2. CARRIED BOUNDS were compared as strings: "2026-9-01" and "garbled" both
   sort after "2026-10-28". They are now parsed, and anything malformed is
   unknown.
3. A PIN THAT ADDS A TRADED DAY to the library's complete year passed the
   validator and turned a real withdrawal into READY. A pin that disagrees
   with the library is now refused at use (checked_pins), and the source must
   be a complete review record; neither proves the dates are the notice's.
4. CONSTRUCTION. A record written at 30ac1ed (conversion after the NYSE-only
   reindex) omits Shenzhen sessions on US holidays and was reused at
   99039858. Records now carry SIDECAR_CONSTRUCTION and older ones are no
   record.

Plus the shared calendar the sibling branch fix/159801-retraction-freshness
needs (native_schedule). Offline: the vendor and the session clock are
stubbed, nothing leaves tmp_path. Python datetime months are 1-indexed
(January = 1); weekdays come from datetime.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import china_market_holidays as cmh  # noqa: E402
import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
import price_source as ps  # noqa: E402
import strategy_freshness as sf  # noqa: E402
import test_native_us_holiday_load_path as lp  # noqa: E402
import vendor_tail as vt  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
W = 200


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


def _weekdays(start, end):
    d = pd.date_range(start, end, freq="D")
    return d[d.weekday < 5]


# ---------------------------------------------------------------------------
# The real load path, with the vendor, clock and Norgate stubbed
# ---------------------------------------------------------------------------

def _vendor(th, withdraw_good_friday=False, btc_last_missing=False):
    """lp._vendor, optionally with the Bitcoin line's final bar not served."""
    yf = lp._vendor(th, withdraw_good_friday)
    if not btc_last_missing:
        return yf
    inner = yf.download

    def download(tickers, *a, **k):
        out = inner(tickers, *a, **k)
        if tickers != "CNY=X":
            for t in th.TICKERS:
                if th.UNIVERSE.get(t, {}).get("trading_calendar") == "crypto_24x7":
                    out.loc[out.index >= lp.LAST, (t, "Close")] = np.nan
        return out
    return type("Y", (), {"download": staticmethod(download)})()


@pytest.fixture(params=["norgate", "yfinance"])
def site(request, monkeypatch, tmp_path):
    import norgate_prices as npx
    import run_thematic_rotation as th
    cache = tmp_path / "thematic_prices_cache.parquet"
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    monkeypatch.setattr(th, "last_completed_session", lambda now: lp.LAST.date())
    monkeypatch.setattr(th, "cap_to_last_completed_session",
                        lambda df, now_utc=None: df.loc[:lp.LAST])
    monkeypatch.setattr(vt, "single_ticker_closes", lambda t: None)
    monkeypatch.setenv("BTE_PRICE_SOURCE", request.param)
    monkeypatch.delenv("BTE_C_BTC_BASIS", raising=False)
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(
        npx, "select_columns",
        lambda df, tickers, s, e, label="": (
            df, {"replaced": [t for t in tickers if ps.plain_us_listing(t)],
                 "kept": [],
                 "unresolved": [t for t in tickers if not ps.plain_us_listing(t)]}))

    def run(**vendor_kw):
        monkeypatch.setattr(th, "yf", _vendor(th, **vendor_kw))
        return th.download_prices()
    return th, cache, run


def _rank(th, frame, weight_fn=lp._top2):
    return lt._rank(th.compute_signal(frame), weight_fn, "NYSE", lp.NOW, "C",
                    signal_kind="ma_distance", top_k=2, cash_proxy=th.CASH_PROXY,
                    native_inputs=th.native_inputs())


def _must_not_rank(_row):
    raise AssertionError("an uncertified native window must never reach the ranker")


def _freshness(th):
    return sf.native_cache_reach_or_carried(
        th.PRICE_CACHE, list(th.TICKERS), th._native_tickers(), "C", "x",
        window_rows=th.MA_PERIOD)


def _interrupted_write(th, run, monkeypatch):
    """Codex's sequence: a first download with every native observation but
    the final Bitcoin bar missing; a retry that restores Bitcoin but has lost
    the Good Friday Shenzhen bar, and fails after the parquet is replaced."""
    first = run(btc_last_missing=True)
    assert np.isnan(first["BTC-USD"].iloc[-1])
    assert lp.GOOD_FRIDAY in th.NATIVE_OBSERVATIONS[CN]
    old_side = ps.read_cache_sidecar(th.PRICE_CACHE)

    def fail(*a, **k):
        raise OSError("simulated: sidecar write failed after the parquet")
    monkeypatch.setattr(ps, "write_cache_source", fail)
    with pytest.raises(OSError, match="simulated"):
        run(withdraw_good_friday=True)
    monkeypatch.setattr(ps, "write_cache_source", _REAL_WRITE)
    on_disk = pd.read_parquet(th.PRICE_CACHE)
    # The pair on disk: the NEW frame (Qingming carries Thursday) beside the
    # OLD record (same last row, Good Friday listed).
    assert ps.read_cache_sidecar(th.PRICE_CACHE) == old_side
    assert old_side[ns.SIDECAR_KEY]["through"] == str(on_disk.index.max().date())
    assert "2026-04-03" in old_side[ns.SIDECAR_KEY]["lines"][CN]["dates"]
    assert on_disk.at[lp.QINGMING_MONDAY, CN] == pytest.approx(
        on_disk.at[lp.THURSDAY, CN], rel=1e-3)
    return on_disk


_REAL_WRITE = ps.write_cache_source


# ---------------------------------------------------------------------------
# 1. A record belongs to one generation of the parquet
# ---------------------------------------------------------------------------

def test_an_interrupted_write_is_never_reused_as_current(site, monkeypatch, capsys):
    th, _, run = site
    _interrupted_write(th, run, monkeypatch)
    # Publication: the freshness report reads the pair on disk.
    reach, lag, _, carried, native = _freshness(th)
    assert reach is None and lag == [CN] and carried is False
    assert "matching this cache" in native[CN]["error"]
    # Restart: refreshed, not reused; the vendor still lacks Good Friday.
    capsys.readouterr()
    again = run(withdraw_good_friday=True)
    log = capsys.readouterr().out
    assert "Using cached prices" not in log and "refreshing" in log
    r = _rank(th, again, _must_not_rank)
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["native_provenance"][CN]["missing_native_sessions"] == ["2026-04-03"]


def test_the_refused_write_fallback_rejects_a_mismatched_pair(site, monkeypatch):
    """Recovery: a fetch refused as degenerate falls back to the cache on
    disk, and must not adopt a record from another generation."""
    th, _, run = site
    _interrupted_write(th, run, monkeypatch)
    monkeypatch.setattr(th, "fetched_panel_is_worse",
                        lambda df, cached: "simulated degenerate fetch")
    back = run()
    assert th.NATIVE_OBSERVATIONS is None
    r = _rank(th, back, _must_not_rank)
    assert r["status"] == "HOLD" and "no native observation record" in r["reason"]


def test_a_matching_pair_is_still_reused(site, capsys):
    th, cache, run = site
    first = run()
    capsys.readouterr()
    again = run()
    assert "Using cached prices" in capsys.readouterr().out
    assert lp.GOOD_FRIDAY in th.NATIVE_OBSERVATIONS[CN]
    assert again.at[lp.QINGMING_MONDAY, CN] == pytest.approx(first.at[lp.QINGMING_MONDAY, CN])
    assert _rank(th, again)["status"] == "READY"


def _frame(rows=None):
    rows = _sessions("NYSE", "2025-09-01", "2026-10-28") if rows is None else rows
    return pd.DataFrame({"X": 1.0 + 0.01 * np.arange(len(rows)),
                         CN: 0.5 + 0.001 * np.arange(len(rows))}, index=rows)


def _record(frame, observed=None):
    observed = _sessions("XSHG", "2025-09-01", "2026-10-28") if observed is None else observed
    return {ns.SIDECAR_KEY: ns.sidecar_record({CN: observed}, frame)}


def test_the_record_is_bound_to_native_values_and_rows_not_other_columns():
    frame = _frame()
    side = _record(frame)
    assert ns.parse_sidecar(side, frame, [CN]) is not None
    # A native value changed, even far inside the window: another generation.
    changed = frame.copy()
    changed.iloc[-60, changed.columns.get_loc(CN)] = changed[CN].iloc[-61]
    assert ns.parse_sidecar(side, changed, [CN]) is None
    # A native cell blanked: another generation.
    blank = frame.copy()
    blank.iloc[-3, blank.columns.get_loc(CN)] = np.nan
    assert ns.parse_sidecar(side, blank, [CN]) is None
    # A row inserted or dropped, last row unchanged: another generation.
    assert ns.parse_sidecar(side, frame.drop(frame.index[-10]), [CN]) is None
    # A US line repaired (repair_price_gaps rewrites the parquet without the
    # sidecar): the native record still describes the native values.
    repaired = frame.copy()
    repaired.iloc[-5, repaired.columns.get_loc("X")] = 99.0
    assert ns.parse_sidecar(side, repaired, [CN]) is not None


def test_the_digest_is_independent_of_encoding(tmp_path):
    frame = _frame()
    p = tmp_path / "f.parquet"
    frame.to_parquet(p)
    back = pd.read_parquet(p)
    assert ns.content_digest(back, [CN]) == ns.content_digest(frame, [CN])
    as_us = frame.copy()
    as_us.index = as_us.index.astype("datetime64[us]")
    assert ns.content_digest(as_us, [CN]) == ns.content_digest(frame, [CN])
    nan_a, nan_b = frame.copy(), frame.copy()
    nan_a.iloc[0, 1] = float("nan")
    nan_b.iloc[0, 1] = np.float64(np.nan) * -1.0
    assert ns.content_digest(nan_a, [CN]) == ns.content_digest(nan_b, [CN])
    assert ns.content_digest(frame.drop(columns=[CN]), [CN]) != \
        ns.content_digest(frame, [CN])


def test_a_record_written_for_the_next_frame_does_not_fit_the_old_one():
    """Either write order: a new sidecar beside the old parquet is refused
    exactly as an old sidecar beside the new parquet is."""
    old = _frame()
    new = old.copy()
    new.iloc[-1, new.columns.get_loc(CN)] *= 1.01
    assert ns.parse_sidecar(_record(new), old, [CN]) is None
    assert ns.parse_sidecar(_record(old), new, [CN]) is None


# ---------------------------------------------------------------------------
# 4. A record built under the earlier conversion order is no record
# ---------------------------------------------------------------------------

def test_a_record_without_the_current_construction_is_refused():
    frame = _frame()
    side = _record(frame)
    rec = side[ns.SIDECAR_KEY]
    assert rec["construction"] == ns.SIDECAR_CONSTRUCTION
    for value in (None, "", "2026-10-28", ns.SIDECAR_CONSTRUCTION + "x", 1):
        rec["construction"] = value
        assert ns.parse_sidecar(side, frame, [CN]) is None, value
    del rec["construction"]
    assert ns.parse_sidecar(side, frame, [CN]) is None


def test_a_cache_written_at_30ac1ed_is_rebuilt_not_reused(site, capsys):
    """The 30ac1ed pair, reconstructed: the frame and record that order
    produced for these vendor inputs (Good Friday never recorded, Qingming
    carrying Thursday), with that version's record fields (no construction,
    no digest). Reused, it HOLDs on a session the vendor served; it must be
    rebuilt, after which the same inputs rank READY."""
    th, cache, run = site
    run(withdraw_good_friday=True)              # Good Friday absent, as at 30ac1ed
    side = ps.read_cache_sidecar(cache)
    rec = side[ns.SIDECAR_KEY]
    assert "2026-04-03" not in rec["lines"][CN]["dates"]
    for key in ("construction", "content_sha256"):
        del rec[key]
    ps.sidecar_path(cache).write_text(json.dumps(side), encoding="utf-8")
    capsys.readouterr()
    out = run()                                  # identical vendor inputs, served
    assert "Using cached prices" not in capsys.readouterr().out
    assert lp.GOOD_FRIDAY in th.NATIVE_OBSERVATIONS[CN]
    r = _rank(th, out)
    assert r["status"] == "READY", r.get("reason")
    assert r["native_provenance"][CN]["missing_native_sessions"] == []


# ---------------------------------------------------------------------------
# 2. Carried bounds are parsed, never compared as strings
# ---------------------------------------------------------------------------

def _prior(tmp_path, monkeypatch, data_through, record):
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    (tmp_path / "strategy_freshness.json").write_text(json.dumps(
        {"computed_at_utc": "2026-10-29T02:00:00+00:00",
         "strategies": [{"sleeve": "C", "data_through": data_through,
                         "laggards": [],
                         "native_provenance": {CN: record}}]}), encoding="utf-8")
    return sf.native_cache_reach_or_carried(
        tmp_path / "absent.parquet", ["X", CN], [CN], "C", "x", window_rows=W)


@pytest.mark.parametrize("last_valid", [
    "2026-9-01", "garbled", "20261028", "2026-W44-3", "2026-02-30",
    "2026-10-28T00:00", " 2026-10-28", "", None, 20261028, ["2026-10-28"]])
def test_a_malformed_carried_bound_is_unknown(tmp_path, monkeypatch, last_valid):
    reach, lag, src, carried, native = _prior(
        tmp_path, monkeypatch, "2026-10-28",
        {"venue": "XSHG", "last_valid": last_valid, "window_rows": W})
    assert reach is None and lag == [CN] and carried is False
    assert "carried" not in src
    assert sf.classify(reach, "2026-10-28", "NYSE") == (sf.UNKNOWN, None)
    assert native[CN]["last_valid"] is None


@pytest.mark.parametrize("data_through", ["2026-9-01", "garbled", "20261028"])
def test_a_malformed_carried_data_through_is_unknown(tmp_path, monkeypatch,
                                                     data_through):
    reach, _, _, carried, _ = _prior(
        tmp_path, monkeypatch, data_through,
        {"venue": "XSHG", "last_valid": "2026-10-28", "window_rows": W})
    assert reach is None and carried is False


def test_a_carried_bound_for_another_venue_is_unknown(tmp_path, monkeypatch):
    reach, _, _, carried, _ = _prior(
        tmp_path, monkeypatch, "2026-10-28",
        {"venue": "NYSE", "last_valid": "2026-10-28", "window_rows": W})
    assert reach is None and carried is False


def test_a_well_formed_carried_bound_still_carries(tmp_path, monkeypatch):
    """Year boundary: a bound measured on 4 January carries a 31 December
    report; one measured on 31 December does not carry a 4 January one."""
    reach, _, _, carried, _ = _prior(
        tmp_path, monkeypatch, "2026-12-31",
        {"venue": "XSHG", "last_valid": "2027-01-04", "window_rows": W})
    assert reach == "2026-12-31" and carried is True
    reach, _, _, carried, _ = _prior(
        tmp_path, monkeypatch, "2027-01-04",
        {"venue": "XSHG", "last_valid": "2026-12-31", "window_rows": W})
    assert reach is None and carried is False


# ---------------------------------------------------------------------------
# 3. A pin cannot excuse a session the library knows was traded
# ---------------------------------------------------------------------------

GOOD_SOURCE = {"title": "HYPOTHETICAL test record, not a real notice",
               "issuer": "test", "issued_on": "2026-11-04",
               "url": "https://www.gov.cn/test", "reviewed_by": "test",
               "reviewed_on": "2026-11-05"}
HYPOTHETICAL_2027 = ("2027-01-01", "2027-01-29", "2027-02-01",
                     "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11",
                     "2027-10-01")


def _library_2026():
    return sorted(str(pd.Timestamp(h).date())
                  for h in get_calendar("XSHG").adhoc_holidays
                  if pd.Timestamp(h).year == 2026 and pd.Timestamp(h).weekday() < 5)


def _pin_via_validator(monkeypatch, year, closures, source=None):
    monkeypatch.setattr(cmh, "PINNED_CLOSURES", {year: tuple(closures)})
    monkeypatch.setattr(cmh, "PINNED_SOURCES",
                        {year: dict(GOOD_SOURCE, issued_on=f"{year - 1}-11-04",
                                    reviewed_on=f"{year - 1}-11-05")
                         if source is None else source})
    monkeypatch.setattr(cmh, "_PINNED", cmh._validated())


ROWS_2026 = _sessions("NYSE", "2025-11-03", "2026-10-28")
TRUE_2026 = _sessions("XSHG", "2025-06-01", "2026-10-28")
NOW_2026 = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
GAP = pd.Timestamp("2026-09-30")


def _rank_2026(observed, weight_fn=_must_not_rank):
    panel = pd.DataFrame({"X": 0.2, CN: 0.3 + 0.001 * np.arange(len(ROWS_2026)),
                          "SHY": 0.0}, index=ROWS_2026)
    return lt._rank(panel, weight_fn, "NYSE", NOW_2026, "C", top_k=1,
                    cash_proxy="SHY",
                    native_inputs={CN: {"venue": "XSHG", "observed": observed,
                                        "window_rows": W}})


def test_a_pin_adding_a_traded_day_cannot_excuse_its_withdrawal(monkeypatch):
    """Codex's case: the library's complete 2026 list plus 30 September (a
    Wednesday both venues traded), with a well-formed source record. The form
    check passes -- it cannot know -- and the use-time check refuses."""
    assert GAP.weekday() == 2 and GAP in ROWS_2026 and GAP in TRUE_2026
    _pin_via_validator(monkeypatch, 2026, _library_2026() + ["2026-09-30"])
    with pytest.raises(cmh.HolidayPinError, match="2026-09-30"):
        cmh.checked_pins(get_calendar("XSHG"), "XSHG")
    with pytest.raises(cmh.HolidayPinError):
        ns.native_schedule("XSHG", "2026-09-01", "2026-10-28")
    r = _rank_2026(TRUE_2026[TRUE_2026 != GAP])
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert "disagree with the calendar library" in r["reason"]
    assert "2026-09-30" in r["reason"]


def test_a_pin_equal_to_the_library_changes_nothing(monkeypatch):
    _pin_via_validator(monkeypatch, 2026, _library_2026())
    assert cmh.checked_pins(get_calendar("XSHG"), "XSHG").size == len(_library_2026())
    r = _rank_2026(TRUE_2026[TRUE_2026 != GAP])
    assert r["status"] == "HOLD"
    assert r["native_provenance"][CN]["missing_native_sessions"] == ["2026-09-30"]
    assert r["native_provenance"][CN]["error"] is None
    assert _rank_2026(TRUE_2026, lp._top2)["status"] == "READY"


def test_a_pin_missing_a_library_closure_is_refused_too(monkeypatch):
    _pin_via_validator(monkeypatch, 2026,
                       [d for d in _library_2026() if d != "2026-09-25"])
    with pytest.raises(cmh.HolidayPinError, match="2026-09-25"):
        cmh.checked_pins(get_calendar("XSHG"), "XSHG")


@pytest.mark.parametrize("source, needle", [
    ("State Council notice, 2026-11-04", "not free text"),
    ({**GOOD_SOURCE, "title": " "}, "'title'"),
    ({**GOOD_SOURCE, "reviewed_by": None}, "'reviewed_by'"),
    ({k: v for k, v in GOOD_SOURCE.items() if k != "issuer"}, "'issuer'"),
    ({**GOOD_SOURCE, "issued_on": "2026-11-4"}, "YYYY-MM-DD"),
    ({**GOOD_SOURCE, "reviewed_on": "garbled"}, "YYYY-MM-DD"),
    ({**GOOD_SOURCE, "issued_on": "2025-11-04", "reviewed_on": "2025-11-05"},
     "not that year's notice"),
    ({**GOOD_SOURCE, "reviewed_on": "2026-11-03"}, "before the notice"),
    ({**GOOD_SOURCE, "url": "https://gov.cn.example.com/x"}, "is not on"),
    ({**GOOD_SOURCE, "url": "https://example.com/www.gov.cn"}, "is not on"),
    ({**GOOD_SOURCE, "url": "ftp://www.sse.com.cn/x"}, "is not on"),
    ({**GOOD_SOURCE, "url": None}, "is not on"),
])
def test_an_incomplete_source_record_is_refused(monkeypatch, source, needle):
    monkeypatch.setattr(cmh, "PINNED_CLOSURES", {2027: HYPOTHETICAL_2027})
    monkeypatch.setattr(cmh, "PINNED_SOURCES", {2027: source})
    with pytest.raises(cmh.HolidayPinError, match=needle):
        cmh._validated()


@pytest.mark.parametrize("url", ["https://www.gov.cn/zhengce/x.htm",
                                 "http://www.sse.com.cn/x", "https://www.szse.cn/x"])
def test_a_complete_source_record_passes_the_form_check(monkeypatch, url):
    monkeypatch.setattr(cmh, "PINNED_CLOSURES", {2027: HYPOTHETICAL_2027})
    monkeypatch.setattr(cmh, "PINNED_SOURCES", {2027: {**GOOD_SOURCE, "url": url}})
    assert [str(d) for d in cmh._validated()[2027]] == list(HYPOTHETICAL_2027)


def test_more_closures_than_any_listed_year_is_refused():
    extra = [str(d.date()) for d in _weekdays("2027-03-01", "2027-03-31")]
    closures = list(HYPOTHETICAL_2027) + extra
    assert len(closures) > cmh.MAX_WEEKDAY_CLOSURES
    with pytest.raises(cmh.HolidayPinError, match="exceeds"):
        cmh.validate_year(2027, closures)
    real_max = max(sum(1 for h in get_calendar("XSHG").adhoc_holidays
                       if pd.Timestamp(h).year == y and pd.Timestamp(h).weekday() < 5)
                   for y in range(1991, 2027))
    assert real_max <= cmh.MAX_WEEKDAY_CLOSURES


def test_nothing_is_pinned_and_nothing_conflicts_today():
    assert cmh.PINNED_CLOSURES == {} and cmh.PINNED_SOURCES == {}
    assert not len(cmh.checked_pins(get_calendar("XSHG"), "XSHG"))


# ---------------------------------------------------------------------------
# One effective calendar for every reader of the Shenzhen schedule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("venue", ["XSHG", "SSE"])
def test_native_schedule_applies_the_pin_under_either_name(monkeypatch, venue):
    """fix/159801-retraction-freshness reads the schedule as "SSE"; the pin
    must reach it under that name as well (month boundary: 29 Jan, 1 Feb)."""
    monkeypatch.setattr(cmh, "_PINNED",
                        {2027: cmh.validate_year(2027, HYPOTHETICAL_2027)})
    assert date(2027, 1, 29).weekday() == 4 and date(2027, 2, 1).weekday() == 0
    raw = pd.DatetimeIndex(get_calendar(venue).schedule(
        start_date="2027-01-25", end_date="2027-02-05").index).normalize()
    sched, bound = ns.native_schedule(venue, "2027-01-25", "2027-02-05")
    got = pd.DatetimeIndex(sched.index).normalize()
    for d in ("2027-01-29", "2027-02-01"):
        assert pd.Timestamp(d) in raw and pd.Timestamp(d) not in got
    assert pd.Timestamp("2027-01-28") in got
    assert bound == pd.Timestamp("2027-12-31")
    # The shared handle is untouched.
    again = pd.DatetimeIndex(get_calendar(venue).schedule(
        start_date="2027-01-25", end_date="2027-02-05").index).normalize()
    assert again.equals(raw)


def test_native_schedule_refuses_an_unmapped_venue():
    with pytest.raises(ValueError):
        ns.native_schedule(None, "2026-01-01", "2026-01-31")
