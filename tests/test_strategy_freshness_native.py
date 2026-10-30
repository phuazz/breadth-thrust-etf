"""Sleeve C's freshness row must not call a filled native row current (2026-10-28).

159801.SZ's cached column is a Shenzhen close forward-filled onto NYSE, so it
reaches the last NYSE row whether or not Shenzhen printed that session. The
live_targets gate HOLDs on that; these pin the freshness report to the same
evidence -- the cache sidecar's native-observation record -- and pin unknown
when that record is missing or belongs to another cache.

Offline: no network, nothing outside tmp_path. Python datetime months are
1-indexed (January = 1). Sessions come from the venue calendars.
"""

from __future__ import annotations

import inspect
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import native_sessions as ns  # noqa: E402
import price_source  # noqa: E402
import strategy_freshness as sf  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
TICKERS = ["X", CN]
SOURCE = "price cache (thematic ETFs)"


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


# Sleeve C's 200-row signal window, and a history long enough to hold it (the
# sidecar keeps 400 days, so later rows than this are trimmed off the record
# rather than the fixture).
W = 200
HIST = "2025-09-01"
OCT_ROWS = _sessions("NYSE", HIST, "2026-10-28")
# Thu 29 Oct 03:00 UTC: NYSE last completed Wed 28 Oct.
OCT_NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)


def _cache(tmp_path, rows=OCT_ROWS, tickers=TICKERS, name="th.parquet"):
    """A cache whose native column is FILLED on every row, as aligned."""
    p = tmp_path / name
    pd.DataFrame({t: 1.0 for t in tickers}, index=rows).to_parquet(p)
    return p


def _sidecar(cache, observed, through=None, lines=None):
    """Write the record a run would have written for ``cache``, or, with
    ``through``, for the same cache cut at that row (another generation)."""
    frame = pd.read_parquet(cache)
    if through is not None:
        frame = frame.loc[:pd.Timestamp(through)]
    rec = ns.sidecar_record(lines if lines is not None else {CN: observed}, frame)
    if through is None:
        frame.to_parquet(cache)          # the run writes the frame that names rec
    price_source.write_cache_source(cache, "yfinance", {ns.SIDECAR_KEY: rec})


def _measure(cache, window_rows=W):
    return sf.native_cache_reach_or_carried(cache, TICKERS, [CN], "C", SOURCE,
                                            window_rows=window_rows)


# ---------------------------------------------------------------------------
# The false-current, measured from the cache
# ---------------------------------------------------------------------------

def test_the_filled_column_alone_would_claim_the_missing_session(tmp_path):
    """The defect, pinned: read as an ordinary column, the filled native line
    reaches 28 October although Shenzhen's 28 October close was never seen."""
    cache = _cache(tmp_path)
    assert sf.cache_reach(cache, TICKERS) == ("2026-10-28", [])


def test_missing_native_bar_is_behind_and_names_the_line(tmp_path):
    cache = _cache(tmp_path)
    _sidecar(cache, _sessions("XSHG", HIST, "2026-10-27"))
    reach, lag, source, carried, native = _measure(cache)
    assert reach == "2026-10-27" and lag == [CN]
    assert source == SOURCE and carried is False
    assert native[CN] == {"venue": "XSHG", "last_valid": "2026-10-27",
                          "last_observed_native_close": "2026-10-27",
                          "required_native_session": "2026-10-28",
                          "native_close_carried": "2026-10-27",
                          "missing_native_sessions": ["2026-10-28"],
                          "window_rows": W,
                          "holiday_data_through": "2026-12-31", "error": None}
    assert sf.classify(reach, "2026-10-28", "NYSE") == (sf.BEHIND, 1)
    why, plain = sf._native_reason(native, lag)
    assert CN in why and "2026-10-27" in why and "2026-10-28" in why
    assert "2026-10-27" in plain
    for jargon in ("venue", "vendor", "nyse", "xshg", "constituent", "panel"):
        assert jargon not in plain.lower(), jargon
    json.dumps(native)


def test_withdrawn_then_restored_bar(tmp_path):
    """Observed, withdrawn on a later rebuild, restored on the next: the
    verdict follows the record each time, never the unchanged filled column."""
    cache = _cache(tmp_path)
    full = _sessions("XSHG", HIST, "2026-10-28")
    _sidecar(cache, full)
    assert _measure(cache)[:2] == ("2026-10-28", [])
    _sidecar(cache, full[full != pd.Timestamp("2026-10-28")])     # withdrawn
    assert _measure(cache)[:2] == ("2026-10-27", [CN])
    _sidecar(cache, full)                                          # restored
    reach, lag, _, _, native = _measure(cache)
    assert (reach, lag) == ("2026-10-28", [])
    assert native[CN]["last_valid"] == "2026-10-28"
    assert sf.classify(reach, "2026-10-28", "NYSE") == (sf.CURRENT, 0)
    assert sf._native_reason(native, lag) == (None, None)


def test_withdrawn_interior_bar_is_behind_never_current(tmp_path):
    """27 October withdrawn, 28 October observed: the newest row carries its
    own close, yet the rows its signal draws on include a filled 27th. Behind
    through 26 October; restoring the 27th makes it current again."""
    cache = _cache(tmp_path)
    full = _sessions("XSHG", HIST, "2026-10-28")
    gap = pd.Timestamp("2026-10-27")
    assert gap in full
    _sidecar(cache, full[full != gap])
    reach, lag, _, carried, native = _measure(cache)
    assert reach is not None and reach <= "2026-10-26"
    assert reach == "2026-10-26" and lag == [CN] and carried is False
    rec = native[CN]
    assert rec["last_valid"] == "2026-10-26"
    assert rec["native_close_carried"] == "2026-10-28"
    assert rec["required_native_session"] == "2026-10-28"
    assert rec["missing_native_sessions"] == ["2026-10-27"]
    status, gap_n = sf.classify(reach, "2026-10-28", "NYSE")
    assert status in (sf.BEHIND, sf.UNKNOWN) and status != sf.CURRENT
    assert (status, gap_n) == (sf.BEHIND, 2)
    why, plain = sf._native_reason(native, lag)
    assert CN in why and "2026-10-27" in why and "2026-10-26" in why
    assert "2026-10-26" in plain
    for jargon in ("venue", "vendor", "nyse", "xshg", "constituent", "panel"):
        assert jargon not in plain.lower(), jargon
    json.dumps(native)

    _sidecar(cache, full)                                          # restored
    reach, lag, _, _, native = _measure(cache)
    assert (reach, lag) == ("2026-10-28", [])
    assert native[CN]["missing_native_sessions"] == []
    assert sf.classify(reach, "2026-10-28", "NYSE") == (sf.CURRENT, 0)


def test_stale_by_several_sessions_keeps_the_true_last_valid_date(tmp_path):
    cache = _cache(tmp_path)
    _sidecar(cache, _sessions("XSHG", HIST, "2026-10-21"))
    reach, lag, _, _, native = _measure(cache)
    assert reach == "2026-10-21" and lag == [CN]
    assert native[CN]["native_close_carried"] == "2026-10-21"
    assert sf.classify(reach, "2026-10-28", "NYSE")[1] == 5


def test_golden_week_fill_stays_current(tmp_path):
    """A holiday fill IS legitimate: on 6 October the required Shenzhen
    session is 30 September."""
    rows = _sessions("NYSE", HIST, "2026-10-06")
    cache = _cache(tmp_path, rows=rows)
    _sidecar(cache, _sessions("XSHG", HIST, "2026-09-30"))
    reach, lag, _, _, native = _measure(cache)
    assert (reach, lag) == ("2026-10-06", [])
    assert native[CN]["required_native_session"] == "2026-09-30"


# ---------------------------------------------------------------------------
# Missing or mismatched sidecar: unknown, never current
# ---------------------------------------------------------------------------

def _assert_unknown(cache, needle):
    reach, lag, _, carried, native = _measure(cache)
    assert reach is None and lag == [CN] and carried is False
    assert native[CN]["last_valid"] is None
    assert needle in native[CN]["error"]
    assert sf.classify(reach, "2026-10-28", "NYSE") == (sf.UNKNOWN, None)
    why, plain = sf._native_reason(native, lag)
    assert CN in why and plain


def test_no_sidecar_is_unknown(tmp_path):
    _assert_unknown(_cache(tmp_path), "no native-observation record")


def test_sidecar_without_a_native_record_is_unknown(tmp_path):
    """A cache written before 2026-10-28: the sidecar exists, the record not."""
    cache = _cache(tmp_path)
    price_source.write_cache_source(cache, "yfinance")
    _assert_unknown(cache, "no native-observation record")


def test_sidecar_for_another_cache_is_unknown(tmp_path):
    cache = _cache(tmp_path)
    _sidecar(cache, _sessions("XSHG", HIST, "2026-10-27"),
             through="2026-10-27")
    _assert_unknown(cache, "matching this cache")


def test_sidecar_missing_the_native_line_is_unknown(tmp_path):
    cache = _cache(tmp_path)
    _sidecar(cache, None, lines={"OTHER.SS": OCT_ROWS})
    _assert_unknown(cache, "matching this cache")


def test_calendar_failure_is_unknown(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    _sidecar(cache, _sessions("XSHG", HIST, "2026-10-28"))

    def boom(_name):
        raise RuntimeError("calendar library missing")
    monkeypatch.setattr(ns, "get_calendar", boom)
    _assert_unknown(cache, "calendar")


# ---------------------------------------------------------------------------
# An absent cache: carry only a bound that was measured on native provenance
# ---------------------------------------------------------------------------

def _prior(tmp_path, monkeypatch, row):
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    (tmp_path / "strategy_freshness.json").write_text(json.dumps(
        {"computed_at_utc": "2026-10-28T02:00:00+00:00",
         "strategies": [{"sleeve": "C", "data_through": "2026-10-27",
                         "laggards": [], **row}]}), encoding="utf-8")


def test_a_prior_bound_without_native_provenance_is_not_carried(tmp_path,
                                                                monkeypatch):
    """A report written before this fix measured C on the filled column."""
    _prior(tmp_path, monkeypatch, {})
    reach, lag, source, carried, native = _measure(tmp_path / "absent.parquet")
    assert reach is None and lag == [CN] and carried is False
    assert "carried" not in source
    assert "no native-observation provenance" in native[CN]["error"]


def test_a_prior_bound_measured_on_native_provenance_is_carried(tmp_path,
                                                                monkeypatch):
    _prior(tmp_path, monkeypatch, {"native_provenance": {
        CN: {"venue": "XSHG", "last_valid": "2026-10-27", "window_rows": W}}})
    reach, _, source, carried, native = _measure(tmp_path / "absent.parquet")
    assert reach == "2026-10-27" and carried is True and "carried" in source
    assert native[CN]["last_valid"] == "2026-10-27"


def test_a_prior_bound_past_its_native_reach_is_not_carried(tmp_path,
                                                           monkeypatch):
    _prior(tmp_path, monkeypatch, {"native_provenance": {
        CN: {"venue": "XSHG", "last_valid": "2026-10-26", "window_rows": W}}})
    reach, _, _, carried, _ = _measure(tmp_path / "absent.parquet")
    assert reach is None and carried is False


def test_a_prior_bound_measured_over_a_shorter_window_is_not_carried(
        tmp_path, monkeypatch):
    """The 2026-10-28 first cut certified ten rows. A bound it wrote, or one
    with no stated window, must not be repeated as a 200-row certificate."""
    for short in ({"window_rows": 10}, {}):
        _prior(tmp_path, monkeypatch, {"native_provenance": {
            CN: {"venue": "XSHG", "last_valid": "2026-10-27", **short}}})
        reach, lag, source, carried, native = _measure(tmp_path / "absent.parquet")
        assert reach is None and lag == [CN] and carried is False
        assert "carried" not in source
        assert "200-row signal window" in native[CN]["error"]


# ---------------------------------------------------------------------------
# build(): sleeve C alone changes
# ---------------------------------------------------------------------------

def test_build_reports_c_behind_on_a_withdrawn_bar(tmp_path, monkeypatch):
    import run_thematic_rotation as th
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS

    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    for etf in list(UNIVERSE_ETFS) + list(UNIVERSE_EUROPE_SECTORS):
        (tmp_path / f"breadth_{etf.lower()}.json").write_text(
            json.dumps({"end_date": "2026-10-28"}), encoding="utf-8")
    ac_cache = _cache(tmp_path, tickers=["SPY"], name="ac.parquet")
    monkeypatch.setitem(sys.modules, "run_asset_class_rotation",
                        types.SimpleNamespace(PRICE_CACHE=ac_cache,
                                              TICKERS=["SPY"]))
    th_cache = _cache(tmp_path, tickers=list(th.TICKERS))
    _sidecar(th_cache, _sessions("XSHG", HIST, "2026-10-27"))
    monkeypatch.setattr(th, "PRICE_CACHE", th_cache)

    r = sf.build(now_utc=OCT_NOW)
    rows = {s["sleeve"]: s for s in r["strategies"]}
    c = rows["C"]
    assert c["status"] == sf.BEHIND and c["data_through"] == "2026-10-27"
    assert c["venue_last_session"] == "2026-10-28" and c["sessions_behind"] == 1
    assert c["laggards"] == [CN] and c["carried"] is False
    assert CN in c["why"] and c["why_plain"]
    assert c["native_provenance"][CN]["last_valid"] == "2026-10-27"
    for key in ("A", "B", "D"):
        assert rows[key]["status"] == sf.CURRENT, rows[key]
        assert "native_provenance" not in rows[key]
    assert r["all_current"] is False and r["n_behind"] == 1
    json.dumps(r)


def test_only_sleeve_c_reads_native_provenance():
    src = inspect.getsource(sf.build)
    assert src.count("native_cache_reach_or_carried(") == 1
    assert "th.PRICE_CACHE, list(th.TICKERS), th._native_tickers()" in src
    assert src.count("cache_reach_or_carried(\n        ac.PRICE_CACHE") == 1
