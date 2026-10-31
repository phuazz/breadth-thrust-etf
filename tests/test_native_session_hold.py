"""A forward-filled native bar is not a current one (2026-10-28).

Sleeve C's 159801.SZ is aligned onto NYSE with a ten-day fill. A withdrawn or
withheld Shenzhen session fills exactly like Golden Week, so the decision row
looks complete and live_targets used to rank it READY. These pin the HOLD, its
provenance, the last valid information it keeps, and READY once the bar is
restored — on the native venue's own calendar.

Offline: no network, no cache or artefact outside tmp_path. Python datetime
months are 1-indexed (January = 1). Sessions come from the venue calendars.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import alignment  # noqa: E402
import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
import price_source  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


def _panel(rows):
    """A complete signal panel on ``rows``: CN ranks first, X second."""
    n = len(rows)
    return pd.DataFrame({"X": [0.20] * n, "Y": [0.10] * n,
                         CN: [0.30 + 0.001 * i for i in range(n)],
                         "SHY": [0.0] * n}, index=rows)


def _top2(row):
    top = row.drop("SHY", errors="ignore").sort_values(ascending=False).head(2)
    return top / top.sum()


def _must_not_rank(_row):
    raise AssertionError("a filled native row must never reach the ranker")


# Sleeve C's signal window: compute_signal's 200-row moving average. Pinned
# against the engine in test_window_rows_is_the_signal_s_real_dependency.
W = 200
# Far enough back that every row judged (the last NATIVE_LOOKBACK_ROWS) has
# its whole 200-row window inside the fixture: 214 NYSE rows before late
# October 2026 reach December 2025.
HIST = "2025-09-01"


def _rank(rows, observed, now, weight_fn=_top2, venue="XSHG", window_rows=W):
    return lt._rank(_panel(rows), weight_fn, "NYSE", now, "C",
                    signal_kind="ma_distance", top_k=2, cash_proxy="SHY",
                    native_inputs={CN: {"venue": venue, "observed": observed,
                                        "window_rows": window_rows}})


OCT_ROWS = _sessions("NYSE", HIST, "2026-10-28")
OCT_NOW = _utc(2026, 10, 29, 3)                  # NYSE last completed: 10-28


# ---------------------------------------------------------------------------
# The false-current path itself
# ---------------------------------------------------------------------------

def test_the_ten_day_alignment_fills_a_withdrawn_session():
    """The mechanism, pinned: a native close ending 2026-09-30 still fills the
    NYSE row of 8 October, the day Shenzhen reopened after Golden Week."""
    native = pd.Series(1.0, index=_sessions("XSHG", "2026-09-21", "2026-09-30"))
    nyse = _sessions("NYSE", "2026-09-21", "2026-10-08")
    aligned = alignment.align_series_to_index(native, nyse, max_stale_days=10)
    assert aligned.loc["2026-10-08"] == 1.0


def test_fx_conversion_records_native_dates_before_alignment(monkeypatch):
    """_fx_convert_to_usd still fills the NYSE row (backtest unchanged) but
    records that the native close stopped a session earlier — and that filled
    frame, ranked end to end, HOLDs."""
    import run_thematic_rotation as th
    rows = OCT_ROWS
    native = pd.Series([70.0 + i for i in range(len(rows))], index=rows)
    native.loc["2026-10-28"] = float("nan")                  # withdrawn bar
    df = pd.DataFrame({CN: native, th.CASH_PROXY: 80.0, "X": 50.0}, index=rows)
    fx = pd.Series(7.0, index=rows)
    monkeypatch.setattr(th.yf, "download",
                        lambda *a, **k: pd.DataFrame({"Close": fx}))
    monkeypatch.setattr(th, "NATIVE_OBSERVATIONS", None)
    out = th._fx_convert_to_usd(df.copy())
    assert out.at[pd.Timestamp("2026-10-28"), CN] == out.at[pd.Timestamp("2026-10-27"), CN]
    obs = th.NATIVE_OBSERVATIONS[CN]
    assert str(obs[-1].date()) == "2026-10-27"
    inputs = th.native_inputs()
    assert inputs[CN]["venue"] == "XSHG"
    r = lt._rank(out[[CN, "X", th.CASH_PROXY]], _must_not_rank, "NYSE", OCT_NOW,
                 "C", cash_proxy=th.CASH_PROXY, native_inputs=inputs)
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["missing_signal_inputs"] == [CN]


def test_unknown_provenance_reads_as_none_not_as_current(monkeypatch):
    import run_thematic_rotation as th
    monkeypatch.setattr(th, "NATIVE_OBSERVATIONS", None)
    assert th.native_inputs() == {CN: {"venue": "XSHG", "observed": None,
                                       "window_rows": th.MA_PERIOD}}


# ---------------------------------------------------------------------------
# Missing, withdrawn, stale, restored
# ---------------------------------------------------------------------------

def test_withdrawn_required_bar_holds_with_provenance_and_last_valid():
    observed = _sessions("XSHG", HIST, "2026-10-27")   # 10-28 withdrawn
    r = _rank(OCT_ROWS, observed, OCT_NOW, weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    assert r["weights"] == {}
    assert r["missing_signal_inputs"] == [CN]
    assert r["decision_session"] == "2026-10-28"
    assert r["last_completed_session"] == "2026-10-28"
    p = r["native_provenance"][CN]
    assert p == {"venue": "XSHG", "required_native_session": "2026-10-28",
                 "native_close_carried": "2026-10-27", "current": False,
                 "window_rows": W, "window_complete": True,
                 "missing_native_sessions": ["2026-10-28"],
                 "holiday_data_through": "2026-12-31", "error": None}
    assert "forward-filled" in r["reason"] and "2026-10-27" in r["reason"]
    # Last valid information stays visible, with its own provenance.
    lv = r["last_valid"]
    assert lv["decision_session"] == "2026-10-27"
    assert lv["native_provenance"][CN]["current"] is True
    assert lv["signals"][CN] == round(_panel(OCT_ROWS).loc["2026-10-27", CN], 4)
    assert set(lv["signals"]) == {"X", "Y", CN}           # cash proxy not ranked
    json.dumps(r)                                          # artefact-serialisable


def test_withdrawn_interior_bar_in_the_recent_tail_holds_then_restores():
    """27 October withdrawn, 28 October observed. The 28 October row carries
    its own close, but its 200-day signal averages in 26 October's close
    filled onto the 27th — so the row is not current, and neither is any
    later row whose tail reaches the 27th."""
    full = _sessions("XSHG", HIST, "2026-10-28")
    gap = pd.Timestamp("2026-10-27")
    assert gap in full and gap in OCT_ROWS                  # both venues traded
    observed = full[full != gap]

    p = ns.provenance(OCT_ROWS, observed, "XSHG", window_rows=W)
    last = pd.Timestamp("2026-10-28")
    assert bool(p.at[last, "row_current"])                  # latest row alone: fine
    assert not bool(p.at[last, "current"])
    assert p.at[last, "missing_native_sessions"] == [gap]

    r = _rank(OCT_ROWS, observed, OCT_NOW, weight_fn=_must_not_rank)
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["missing_signal_inputs"] == [CN]
    assert r["decision_session"] == "2026-10-28"
    here = r["native_provenance"][CN]
    assert here["required_native_session"] == "2026-10-28"
    assert here["native_close_carried"] == "2026-10-28"
    assert here["current"] is False
    assert here["missing_native_sessions"] == ["2026-10-27"]
    assert "forward-filled" in r["reason"] and "2026-10-27" in r["reason"]
    lv = r["last_valid"]
    assert lv["decision_session"] == "2026-10-26"
    assert lv["decision_session"] <= "2026-10-26"
    assert lv["native_provenance"][CN]["current"] is True
    assert lv["native_provenance"][CN]["missing_native_sessions"] == []
    json.dumps(r)

    restored = _rank(OCT_ROWS, full, OCT_NOW)
    assert restored["status"] == "READY" and restored["reason"] is None
    assert restored["decision_session"] == "2026-10-28"
    assert set(restored["weights"]) == {CN, "X"}
    assert restored["native_provenance"][CN]["current"] is True
    assert restored["native_provenance"][CN]["missing_native_sessions"] == []


def test_a_record_too_short_to_cover_the_window_fails_closed():
    """Bounded evidence that starts inside the window cannot certify the rows
    before it: HOLD, and no last valid row is invented."""
    observed = _sessions("XSHG", "2026-10-27", "2026-10-28")
    r = _rank(OCT_ROWS, observed, OCT_NOW, weight_fn=_must_not_rank)
    assert r["status"] == "HOLD" and r["weights"] == {}
    missing = r["native_provenance"][CN]["missing_native_sessions"]
    assert "2026-10-26" in missing and "2026-10-28" not in missing
    assert r["last_valid"] is None


def test_us_only_holiday_inside_the_tail_stays_ready():
    """Thanksgiving 26 Nov: Shenzhen trades, NYSE shuts. That native bar sits
    on no NYSE row, so its absence from the record leaves the tail current."""
    rows = _sessions("NYSE", HIST, "2026-11-30")
    full = _sessions("XSHG", HIST, "2026-11-30")
    thanksgiving = pd.Timestamp("2026-11-26")
    assert thanksgiving in full and thanksgiving not in rows
    r = _rank(rows, full[full != thanksgiving], _utc(2026, 12, 1, 3))
    assert r["status"] == "READY"
    assert r["native_provenance"][CN]["missing_native_sessions"] == []


def test_stale_by_several_sessions_holds_and_points_at_the_true_last_row():
    observed = _sessions("XSHG", HIST, "2026-10-21")
    r = _rank(OCT_ROWS, observed, OCT_NOW, weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    assert r["native_provenance"][CN]["native_close_carried"] == "2026-10-21"
    assert r["last_valid"]["decision_session"] == "2026-10-21"


def test_missing_observation_record_holds_without_inventing_a_last_row():
    r = _rank(OCT_ROWS, None, OCT_NOW, weight_fn=_must_not_rank)
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["native_provenance"][CN]["native_close_carried"] is None
    assert "no native observation record" in r["reason"]
    assert r["last_valid"] is None


def test_empty_observation_record_holds():
    r = _rank(OCT_ROWS, pd.DatetimeIndex([]), OCT_NOW, weight_fn=_must_not_rank)
    assert r["status"] == "HOLD" and r["last_valid"] is None


def test_restored_bar_ranks_ready_as_normal():
    observed = _sessions("XSHG", HIST, "2026-10-28")
    r = _rank(OCT_ROWS, observed, OCT_NOW)
    assert r["status"] == "READY" and r["reason"] is None
    assert r["decision_session"] == "2026-10-28"
    assert set(r["weights"]) == {CN, "X"}
    assert abs(sum(r["weights"].values()) - 1.0) < 1e-9
    assert r["native_provenance"][CN]["current"] is True
    assert "last_valid" not in r


def test_without_native_inputs_rank_is_unchanged():
    """Sleeves A, B and D pass no native_inputs; their records gain nothing."""
    r = lt._rank(_panel(OCT_ROWS), _top2, "NYSE", OCT_NOW, "B", cash_proxy="SHY")
    assert r["status"] == "READY" and "native_provenance" not in r


def test_a_native_hold_leaves_the_held_book_untouched():
    """No new allocation: the HOLD's intended book is its held book, and the
    last valid signals are never read as weights."""
    # _intended_lines imports this on its HOLD path; it is not among the
    # files copied into the isolated tree.
    pytest.importorskip("component_contract")
    observed = _sessions("XSHG", HIST, "2026-10-27")
    hold = _rank(OCT_ROWS, observed, OCT_NOW, weight_fn=_must_not_rank)
    held = {("C", "ARKK"): 0.04, ("C", CN): 0.03, ("C", "SHY"): 0.03}
    nav = {"a": 0.35, "b": 0.25, "c": 0.10, "d": 0.20,
           "tilt_nav": 0.0, "shy_overlay": 0.0}
    lines = lt._intended_lines([hold], held, nav)
    assert {ln["etf"] for ln in lines} == {"ARKK", CN, "SHY"}
    assert all(ln["delta"] == 0 and ln["status"] == "HOLD" for ln in lines)
    assert lt._targets_final([hold]) is False


# ---------------------------------------------------------------------------
# China / NYSE calendars and time zones
# ---------------------------------------------------------------------------

def test_golden_week_fill_is_legitimate_and_stays_ready():
    """SSE/SZSE shut 1-7 October 2026 while NYSE trades. On 6 October the
    required native session is 30 September, so the fill is a holiday fill."""
    rows = _sessions("NYSE", HIST, "2026-10-06")
    observed = _sessions("XSHG", HIST, "2026-09-30")
    r = _rank(rows, observed, _utc(2026, 10, 7, 3))
    assert r["status"] == "READY"
    assert r["native_provenance"][CN]["required_native_session"] == "2026-09-30"


def test_golden_week_with_the_pre_holiday_bar_withdrawn_holds():
    rows = _sessions("NYSE", HIST, "2026-10-06")
    observed = _sessions("XSHG", HIST, "2026-09-29")
    r = _rank(rows, observed, _utc(2026, 10, 7, 3), weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    assert r["native_provenance"][CN]["required_native_session"] == "2026-09-30"
    assert r["last_valid"]["decision_session"] == "2026-09-29"


def test_reopening_session_missing_after_golden_week_holds():
    """The ten-day cap still fills 8 October from 30 September — exactly the
    false-current case. Shenzhen traded the 8th, so that close is required."""
    rows = _sessions("NYSE", HIST, "2026-10-08")
    observed = _sessions("XSHG", HIST, "2026-09-30")
    r = _rank(rows, observed, _utc(2026, 10, 9, 3), weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    p = r["native_provenance"][CN]
    assert p["required_native_session"] == "2026-10-08"
    assert p["native_close_carried"] == "2026-09-30"
    assert r["last_valid"]["decision_session"] == "2026-10-07"

    restored = observed.append(pd.DatetimeIndex([pd.Timestamp("2026-10-08")]))
    assert _rank(rows, restored, _utc(2026, 10, 9, 3))["status"] == "READY"


def test_native_close_precedes_the_nyse_close_on_the_same_date():
    """Shenzhen closes 15:00 Asia/Shanghai = 07:00 UTC, NYSE 20:00 UTC (EDT),
    so a NYSE row requires the same-date native session when both trade."""
    xshg = get_calendar("XSHG").schedule(start_date="2026-10-28", end_date="2026-10-28")
    nyse = get_calendar("NYSE").schedule(start_date="2026-10-28", end_date="2026-10-28")
    assert xshg["market_close"].iloc[0] == pd.Timestamp("2026-10-28 07:00", tz="UTC")
    assert nyse["market_close"].iloc[0] == pd.Timestamp("2026-10-28 20:00", tz="UTC")
    p = ns.provenance(pd.DatetimeIndex(["2026-10-28"]),
                      pd.DatetimeIndex(["2026-10-28"]), "XSHG",
                      window_rows=1)
    assert p.at[pd.Timestamp("2026-10-28"), "required_native_session"] == \
        pd.Timestamp("2026-10-28")
    assert bool(p.at[pd.Timestamp("2026-10-28"), "current"])


def test_weekend_and_month_boundary_after_us_dst_end():
    """Mon 2 Nov 2026 (NYSE closes 21:00 UTC from today): the required native
    session is Mon 2 Nov, not Fri 30 Oct across the weekend and month end."""
    rows = pd.DatetimeIndex(["2026-10-30", "2026-11-02"])
    p = ns.provenance(rows, _sessions("XSHG", "2026-10-26", "2026-10-30"), "XSHG",
                      window_rows=1)
    assert bool(p.at[pd.Timestamp("2026-10-30"), "current"])
    assert p.at[pd.Timestamp("2026-11-02"), "required_native_session"] == \
        pd.Timestamp("2026-11-02")
    assert not bool(p.at[pd.Timestamp("2026-11-02"), "current"])


def test_us_holiday_and_early_close_use_the_native_calendar():
    """Thanksgiving 26 Nov: NYSE shut, Shenzhen trades. Fri 27 Nov NYSE closes
    early (18:00 UTC), still after Shenzhen's 07:00 UTC, so 27 Nov is required;
    the 26 Nov native bar never sits on a NYSE row and is not needed."""
    rows = pd.DatetimeIndex(["2026-11-25", "2026-11-27"])
    observed = pd.DatetimeIndex(["2026-11-24", "2026-11-25", "2026-11-27"])
    p = ns.provenance(rows, observed, "XSHG", window_rows=1)
    assert p.at[pd.Timestamp("2026-11-27"), "required_native_session"] == \
        pd.Timestamp("2026-11-27")
    assert bool(p["current"].all())


def test_year_boundary_beyond_announced_holidays_fails_closed():
    """XSHG's announced holidays end in 2026. Whatever it says about early
    January 2027, a native record ending 31 Dec cannot vouch for 4 January."""
    p = ns.provenance(pd.DatetimeIndex(["2027-01-04"]),
                      _sessions("XSHG", "2026-12-21", "2026-12-31"), "XSHG",
                      window_rows=1)
    assert not bool(p.at[pd.Timestamp("2027-01-04"), "current"])


def test_tz_aware_native_stamps_keep_their_own_session_date():
    """A Shenzhen bar stamped at local midnight is that local session, not the
    previous UTC day."""
    stamp = pd.Timestamp("2026-10-28 00:00", tz="Asia/Shanghai")
    assert ns.session_dates([stamp])[0] == pd.Timestamp("2026-10-28")
    p = ns.provenance(pd.DatetimeIndex(["2026-10-28"]), [stamp], "XSHG", window_rows=1)
    assert bool(p.at[pd.Timestamp("2026-10-28"), "current"])


def test_non_finite_and_non_positive_values_are_not_observations():
    idx = pd.DatetimeIndex(["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"])
    s = pd.Series([10.0, float("inf"), 0.0, float("nan")], index=idx)
    assert list(ns.observed_dates(s)) == [pd.Timestamp("2026-10-26")]


def test_calendar_failure_fails_closed(monkeypatch):
    def boom(_name):
        raise RuntimeError("calendar library missing")
    monkeypatch.setattr(ns, "get_calendar", boom)
    r = _rank(OCT_ROWS, _sessions("XSHG", HIST, "2026-10-28"), OCT_NOW,
              weight_fn=_must_not_rank)
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert "unknown" in r["reason"] and "calendar" in r["reason"]


def test_unmapped_venue_fails_closed():
    r = _rank(OCT_ROWS, _sessions("XSHG", HIST, "2026-10-28"), OCT_NOW,
              weight_fn=_must_not_rank, venue=None)
    assert r["status"] == "HOLD"


# ---------------------------------------------------------------------------
# The cache sidecar: bounded, truthful, and tied to its own cache
# ---------------------------------------------------------------------------

def _frame(through):
    """A cache frame ending on ``through``: the native line filled on every
    NYSE row, as aligned."""
    return pd.DataFrame({CN: 1.0}, index=_sessions("NYSE", "2025-01-02", through))


def test_sidecar_record_lists_only_observed_dates_within_its_window():
    observed = _sessions("XSHG", "2025-01-02", "2026-10-27")
    rec = ns.sidecar_record({CN: observed}, _frame("2026-10-28"))
    line = rec["lines"][CN]
    assert rec["through"] == "2026-10-28"
    assert rec["window_days"] == ns.SIDECAR_WINDOW_DAYS == 400
    assert line["venue"] == "XSHG" and line["last"] == "2026-10-27"
    assert "2026-10-28" not in line["dates"]               # a fill is not listed
    floor = pd.Timestamp("2026-10-28") - pd.Timedelta(days=ns.SIDECAR_WINDOW_DAYS)
    assert str(floor.date()) == "2025-09-23"
    assert min(line["dates"]) >= "2025-09-23"
    assert "NOT native observations" in rec["meaning"]


def test_sidecar_round_trip_and_mismatch_is_absent(tmp_path):
    observed = _sessions("XSHG", "2025-10-09", "2026-10-27")
    frame = _frame("2026-10-28")
    rec = ns.sidecar_record({CN: observed}, frame)
    cache = tmp_path / "thematic_prices_cache.parquet"
    price_source.write_cache_source(cache, "yfinance",
                                    {ns.SIDECAR_KEY: rec})
    side = price_source.read_cache_sidecar(cache)
    assert side[ns.SIDECAR_KEY] == rec
    got = ns.parse_sidecar(side, frame, [CN])
    assert list(got[CN]) == list(observed)
    # Belongs to another cache, missing a line, or malformed: absent.
    assert ns.parse_sidecar(side, _frame("2026-10-29"), [CN]) is None
    assert ns.parse_sidecar(side, frame, [CN, "OTHER.SS"]) is None
    assert ns.parse_sidecar({ns.SIDECAR_KEY: {"lines": "x"}}, frame, [CN]) is None
    assert ns.parse_sidecar(None, frame, [CN]) is None
    assert ns.parse_sidecar(side, None, [CN]) is None


def test_cached_frame_reads_its_own_native_record(tmp_path, monkeypatch):
    import run_thematic_rotation as th
    cache = tmp_path / "thematic_prices_cache.parquet"
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    frame = pd.DataFrame({CN: 1.0}, index=OCT_ROWS)
    assert th._cached_native_observations(frame) is None   # no sidecar yet
    rec = ns.sidecar_record({CN: _sessions("XSHG", HIST, "2026-10-27")}, frame)
    price_source.write_cache_source(cache, "yfinance", {ns.SIDECAR_KEY: rec})
    got = th._cached_native_observations(frame)
    assert str(got[CN][-1].date()) == "2026-10-27"


def test_a_cache_hit_requires_a_matching_native_record():
    """Source contract: the reuse branch refuses a cache whose native record
    is absent, and the fallback hands back the CACHE's record, not the
    refused fetch's."""
    import run_thematic_rotation as th
    src = inspect.getsource(th.download_prices)
    assert src.index("_cached_native_observations(cached)) is None") < \
        src.index("Using cached prices")
    # One read: the record checked is the record adopted (2026-10-30).
    hit = src[src.index("Using cached prices"):src.index("return cached[needed]")]
    assert "NATIVE_OBSERVATIONS = cached_native" in hit
    assert "_cached_native_observations" not in hit
    fallback = src[src.index("REFUSED cache write"):]
    assert fallback.index("NATIVE_OBSERVATIONS = _cached_native_observations") < \
        fallback.index("return cached[needed]")


def test_build_passes_native_inputs_for_sleeve_c_only():
    src = inspect.getsource(lt.build)
    assert src.count("native_inputs=") == 1
    assert "native_inputs=th.native_inputs()" in src
