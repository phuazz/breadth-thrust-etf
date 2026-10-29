"""The mainland holiday pin and its effect on sleeve C's native check (2026-10-29).

pandas_market_calendars' XSHG lists no 2027 closures, so on a 2027 Chinese
holiday the NYSE rows would require Shenzhen closes that were never printed.
These pin: the validator against the library's real 2025 and 2026 closures;
its refusals; that nothing is pinned for 2027 yet; that a missing session past
the holiday data is labelled as possibly unannounced; and that pinning the
closure clears the HOLD on the next run.

The 2027 closures used below are HYPOTHETICAL test inputs, not the official
arrangement, which had not been published on 2026-10-29. Offline. Python
datetime months are 1-indexed (January = 1); weekdays come from datetime.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import china_market_holidays as cmh  # noqa: E402
import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
W = 200

# HYPOTHETICAL 2027 weekday closures, shaped like a notice: 1 Jan, a Lunar New
# Year block (the first lunar day of 2027 is 6 Feb), and 1 Oct. Two of them
# straddle the January/February month end so the month boundary is exercised.
HYPOTHETICAL_2027 = ("2027-01-01", "2027-01-29", "2027-02-01",
                     "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11",
                     "2027-10-01")


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


def _weekdays(start, end):
    """Every Monday-Friday date: what Shenzhen would trade with no holiday."""
    d = pd.date_range(start, end, freq="D")
    return d[d.weekday < 5]


def _library_weekday_closures(year):
    lib = get_calendar("XSHG").adhoc_holidays
    return [pd.Timestamp(h).date() for h in lib
            if pd.Timestamp(h).year == year and pd.Timestamp(h).weekday() < 5]


def _panel(rows):
    n = len(rows)
    return pd.DataFrame({"X": [0.20] * n, CN: [0.30 + 0.001 * i for i in range(n)],
                         "SHY": [0.0] * n}, index=rows)


def _top1(row):
    top = row.drop("SHY", errors="ignore").sort_values(ascending=False).head(1)
    return top / top.sum()


def _rank(rows, observed, now):
    return lt._rank(_panel(rows), _top1, "NYSE", now, "C", top_k=1,
                    cash_proxy="SHY",
                    native_inputs={CN: {"venue": "XSHG", "observed": observed,
                                        "window_rows": W}})


def _pin(monkeypatch, year, closures):
    monkeypatch.setattr(cmh, "_PINNED", {year: cmh.validate_year(year, closures)})


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("year", [2025, 2026])
def test_the_validator_accepts_the_library_s_real_closures(year):
    """The statutory fixed-date rule matches the two years published under
    the 2025 regulations, as the library lists them."""
    real = _library_weekday_closures(year)
    assert len(real) >= 15
    assert cmh.validate_year(year, [str(d) for d in real]) == sorted(real)


def test_the_validator_refuses_malformed_years():
    good = list(HYPOTHETICAL_2027)
    assert cmh.validate_year(2027, good)[0] == date(2027, 1, 1)
    # Weekdays read from datetime (Monday = 0), not from memory.
    assert date(2027, 1, 1).weekday() == 4 and date(2027, 10, 1).weekday() == 4
    assert date(2027, 1, 2).weekday() == 5
    with pytest.raises(cmh.HolidayPinError, match="weekend"):
        cmh.validate_year(2027, good + ["2027-01-02"])
    with pytest.raises(cmh.HolidayPinError, match="not in 2027"):
        cmh.validate_year(2027, good + ["2026-12-31"])
    with pytest.raises(cmh.HolidayPinError, match="duplicate"):
        cmh.validate_year(2027, good + ["2027-02-08"])
    with pytest.raises(cmh.HolidayPinError, match="2027-01-01"):
        cmh.validate_year(2027, [d for d in good if d != "2027-01-01"])
    with pytest.raises(cmh.HolidayPinError, match="2027-10-01"):
        cmh.validate_year(2027, [d for d in good if d != "2027-10-01"])


def test_a_pin_without_a_source_is_refused(monkeypatch):
    monkeypatch.setattr(cmh, "PINNED_CLOSURES", {2027: HYPOTHETICAL_2027})
    monkeypatch.setattr(cmh, "PINNED_SOURCES", {})
    with pytest.raises(cmh.HolidayPinError, match="without a source"):
        cmh._validated()


# ---------------------------------------------------------------------------
# What is pinned today, and the bound
# ---------------------------------------------------------------------------

def test_nothing_is_pinned_for_2027_until_the_notice_is_published():
    assert 2027 not in cmh.PINNED_CLOSURES
    assert not len(cmh.pinned_closures("XSHG"))
    assert _library_weekday_closures(2027) == []      # the gap this file exists for
    xshg = get_calendar("XSHG")
    assert cmh.holiday_data_through(xshg, "XSHG") == pd.Timestamp("2026-12-31")
    assert cmh.holiday_data_through(get_calendar("NYSE"), "NYSE") is None
    assert cmh.library_conflicts(xshg, "XSHG") == {}


def test_pinning_a_year_moves_the_bound(monkeypatch):
    _pin(monkeypatch, 2027, HYPOTHETICAL_2027)
    xshg = get_calendar("XSHG")
    assert cmh.holiday_data_through(xshg, "XSHG") == pd.Timestamp("2027-12-31")
    assert list(cmh.pinned_closures("XSHG").strftime("%Y-%m-%d")) == \
        list(HYPOTHETICAL_2027)
    assert not len(cmh.pinned_closures("NYSE"))


def test_a_pin_that_disagrees_with_a_later_library_is_reported(monkeypatch):
    real = [str(d) for d in _library_weekday_closures(2026)]
    _pin(monkeypatch, 2026, real)
    assert cmh.library_conflicts(get_calendar("XSHG"), "XSHG") == {}
    _pin(monkeypatch, 2026, [d for d in real if d != "2026-09-25"])
    assert cmh.library_conflicts(get_calendar("XSHG"), "XSHG") == {
        2026: {"pinned_only": [], "library_only": ["2026-09-25"]}}


def test_the_pin_never_mutates_the_shared_calendar(monkeypatch):
    _pin(monkeypatch, 2027, HYPOTHETICAL_2027)
    xshg = get_calendar("XSHG")
    before = len(xshg.schedule("2027-01-01", "2027-02-28"))
    ns.provenance(_sessions("NYSE", "2027-01-04", "2027-02-26"),
                  _weekdays("2026-12-01", "2027-02-26"), "XSHG", window_rows=1)
    assert len(xshg.schedule("2027-01-01", "2027-02-28")) == before
    assert pd.Timestamp("2027-02-08") in xshg.schedule("2027-02-01", "2027-02-12").index


# ---------------------------------------------------------------------------
# The effect on sleeve C
# ---------------------------------------------------------------------------

ROWS = _sessions("NYSE", "2026-03-02", "2027-02-26")
NOW = datetime(2027, 2, 27, 3, 0, tzinfo=timezone.utc)   # NYSE last: 2027-02-26
# What Shenzhen would really have printed under the hypothetical closures.
TRUE_SZ = _sessions("XSHG", "2026-03-02", "2026-12-31").append(
    _weekdays("2027-01-01", "2027-02-26").difference(pd.DatetimeIndex(HYPOTHETICAL_2027)))


def test_an_unannounced_holiday_holds_and_says_so_then_the_pin_clears_it(monkeypatch):
    """Unpinned, XSHG demands the hypothetical Lunar New Year closes: HOLD,
    labelled as possibly unannounced. Pinned on a later run: READY at once,
    with no 200-row wait, because the requirement is recomputed each run."""
    r = _rank(ROWS, TRUE_SZ, NOW)
    assert r["status"] == "HOLD" and r["weights"] == {}
    here = r["native_provenance"][CN]
    assert here["holiday_data_through"] == "2026-12-31"
    on_nyse = [d for d in HYPOTHETICAL_2027 if pd.Timestamp(d) in ROWS]
    assert here["missing_native_sessions"] == on_nyse
    assert "2027-01-01" not in on_nyse                  # NYSE shut too: never required
    assert "past the XSHG holiday data (through 2026-12-31)" in r["reason"]
    assert "china_market_holidays.py" in r["reason"]

    _pin(monkeypatch, 2027, HYPOTHETICAL_2027)
    r = _rank(ROWS, TRUE_SZ, NOW)
    assert r["status"] == "READY", r.get("reason")
    assert r["native_provenance"][CN]["holiday_data_through"] == "2027-12-31"
    assert r["native_provenance"][CN]["missing_native_sessions"] == []


def test_a_pinned_closure_moves_the_requirement_to_the_last_open_session(monkeypatch):
    """Month boundary: with 29 Jan and 1 Feb 2027 pinned, the NYSE rows of
    both days require Thursday 28 January's close."""
    _pin(monkeypatch, 2027, HYPOTHETICAL_2027)
    p = ns.provenance(_sessions("NYSE", "2027-01-25", "2027-02-12"), TRUE_SZ,
                      "XSHG", window_rows=1)
    assert date(2027, 1, 28).weekday() == 3
    for d in ("2027-01-29", "2027-02-01"):
        assert p.at[pd.Timestamp(d), "required_native_session"] == \
            pd.Timestamp("2027-01-28")
    for d in ("2027-02-08", "2027-02-11"):
        assert p.at[pd.Timestamp(d), "required_native_session"] == \
            pd.Timestamp("2027-02-05")
    assert bool(p["current"].all())


def test_year_boundary_runs_unpinned_until_the_first_real_closure():
    """No false HOLD from the bound alone: through January 2027, before any
    hypothetical closure on a NYSE session, an unpinned run is READY. XSHG
    calls 1 Jan 2027 a session, but NYSE is shut that day, so no row needs it;
    4 Jan requires its own close."""
    rows = ROWS[ROWS <= "2027-01-28"]
    p = ns.provenance(rows, TRUE_SZ, "XSHG", window_rows=W)
    assert p.at[pd.Timestamp("2027-01-04"), "required_native_session"] == \
        pd.Timestamp("2027-01-04")
    assert pd.Timestamp("2027-01-01") not in rows
    r = _rank(rows, TRUE_SZ, datetime(2027, 1, 29, 3, 0, tzinfo=timezone.utc))
    assert r["status"] == "READY", r.get("reason")
    assert r["native_provenance"][CN]["holiday_data_through"] == "2026-12-31"


def test_a_pin_cannot_excuse_a_real_withdrawal(monkeypatch):
    """The pin removes only the closures it lists: a withdrawn close on an
    open 2027 session still HOLDs, and is not labelled as unannounced once
    the year is pinned."""
    _pin(monkeypatch, 2027, HYPOTHETICAL_2027)
    gap = pd.Timestamp("2027-02-17")
    assert gap in ROWS and gap in TRUE_SZ
    r = _rank(ROWS, TRUE_SZ[TRUE_SZ != gap], NOW)
    assert r["status"] == "HOLD"
    assert r["native_provenance"][CN]["missing_native_sessions"] == ["2027-02-17"]
    assert "past the XSHG holiday data" not in r["reason"]


def test_freshness_labels_the_unannounced_session(tmp_path):
    import price_source
    import strategy_freshness as sf
    cache = tmp_path / "th.parquet"
    pd.DataFrame({"X": 1.0, CN: 1.0}, index=ROWS).to_parquet(cache)
    rec = ns.sidecar_record({CN: TRUE_SZ}, pd.read_parquet(cache))
    price_source.write_cache_source(cache, "yfinance", {ns.SIDECAR_KEY: rec})
    reach, lag, _, _, native = sf.native_cache_reach_or_carried(
        cache, ["X", CN], [CN], "C", "price cache (thematic ETFs)", window_rows=W)
    assert reach is not None and reach < "2027-01-29" and lag == [CN]
    assert native[CN]["holiday_data_through"] == "2026-12-31"
    why, _ = sf._native_reason(native, lag)
    assert "past the XSHG holiday data (through 2026-12-31)" in why
