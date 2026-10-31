"""Native provenance covers the WHOLE 200-row signal window (2026-10-29).

The first cut of the native-session HOLD (2026-10-28) certified ten rows while
sleeve C's signal averages two hundred. A Shenzhen session withdrawn eleven or
more rows back was still forward-filled into the average and the row was
ranked READY. These pin that every native session inside the window counts:
older and interior withdrawals HOLD, the exact window edge is respected, real
exchange holidays on either side stay READY, restoration returns READY, and
valid inputs rank exactly as they did without the check.

Offline: no network, nothing outside tmp_path. Python datetime months are
1-indexed (January = 1). Sessions come from the venue calendars, never from a
weekday count.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
import price_source  # noqa: E402
import strategy_freshness as sf  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
W = 200
HIST = "2025-09-01"


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


ROWS = _sessions("NYSE", HIST, "2026-10-28")
XSHG = _sessions("XSHG", HIST, "2026-10-28")
NOW = _utc(2026, 10, 29, 3)                      # NYSE last completed: 10-28
DECIDED = pd.Timestamp("2026-10-28")


def _panel(rows):
    n = len(rows)
    return pd.DataFrame({"X": [0.20] * n, "Y": [0.10] * n,
                         CN: [0.30 + 0.001 * i for i in range(n)],
                         "SHY": [0.0] * n}, index=rows)


def _top2(row):
    top = row.drop("SHY", errors="ignore").sort_values(ascending=False).head(2)
    return top / top.sum()


def _must_not_rank(_row):
    raise AssertionError("an uncertified native window must never reach the ranker")


def _rank(observed, rows=ROWS, now=NOW, weight_fn=_top2, spec=None):
    spec = {"venue": "XSHG", "observed": observed, "window_rows": W,
            **(spec or {})}
    return lt._rank(_panel(rows), weight_fn, "NYSE", now, "C",
                    signal_kind="ma_distance", top_k=2, cash_proxy="SHY",
                    native_inputs={CN: spec})


def _back(k, rows=ROWS):
    """The NYSE row k rows before the last (k = 0 is the decision row),
    asserted to be a Shenzhen session too, so its absence is a withdrawal."""
    d = rows[-1 - k]
    assert d in XSHG, f"{d.date()} is not a Shenzhen session; pick another"
    return d


def _without(*gaps):
    return XSHG[~XSHG.isin(pd.DatetimeIndex(list(gaps)))]


def _us_only_split():
    """Shenzhen sessions on NYSE holidays, split into those no NYSE row needs
    and those a later NYSE row does need (Shenzhen shut on that row's date)."""
    us_only = XSHG[~XSHG.isin(ROWS)]
    req = ns.provenance(ROWS, XSHG, "XSHG", window_rows=1)["required_native_session"]
    needed = us_only[us_only.isin(pd.DatetimeIndex([d for d in req if d is not None]))]
    return us_only[~us_only.isin(needed)], needed


# ---------------------------------------------------------------------------
# The window is the signal's real dependency
# ---------------------------------------------------------------------------

def test_window_rows_is_the_signal_s_real_dependency(monkeypatch):
    """compute_signal at the last row moves when a close 199 rows back moves
    and does not move for one 200 rows back -- so the 200-row certificate is
    exactly the rows the signal reads, and native_inputs states that number."""
    import run_thematic_rotation as th
    assert th.MA_PERIOD == W
    monkeypatch.setattr(th, "NATIVE_OBSERVATIONS", {CN: XSHG})
    assert th.native_inputs()[CN]["window_rows"] == th.MA_PERIOD

    rng = np.random.default_rng(7)
    idx = ROWS[-260:]
    closes = pd.DataFrame({CN: 100 + rng.normal(0, 1, len(idx)).cumsum()},
                          index=idx)
    base = th.compute_signal(closes)[CN].iloc[-1]
    inside, outside = closes.copy(), closes.copy()
    inside.iloc[-W, 0] *= 1.10                  # oldest row of the window
    outside.iloc[-W - 1, 0] *= 1.10             # first row before it
    assert th.compute_signal(inside)[CN].iloc[-1] != pytest.approx(base, abs=1e-12)
    assert th.compute_signal(outside)[CN].iloc[-1] == pytest.approx(base, abs=1e-15)


# ---------------------------------------------------------------------------
# Older and interior withdrawals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("k", [11, 60, 120, W - 1])
def test_an_older_withdrawal_inside_the_window_holds(k):
    """k rows back: beyond the first cut's ten-row tail, inside the 200-row
    window. HOLD, the session named, nothing ranked."""
    gap = _back(k)
    r = _rank(_without(gap), weight_fn=_must_not_rank)
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["decision_session"] == "2026-10-28"
    assert r["missing_signal_inputs"] == [CN]
    here = r["native_provenance"][CN]
    assert here["required_native_session"] == "2026-10-28"
    assert here["native_close_carried"] == "2026-10-28"   # the row itself is fine
    assert here["current"] is False and here["window_complete"] is True
    assert here["missing_native_sessions"] == [str(gap.date())]
    assert str(gap.date()) in r["reason"] and "200-row" in r["reason"]
    # last_valid: the newest judged row whose own window excludes the gap.
    lookback = ROWS[-lt.NATIVE_LOOKBACK_ROWS:]
    clear = [d for d in lookback[:-1] if d < gap]
    if clear:
        assert r["last_valid"]["decision_session"] == str(clear[-1].date())
    else:
        assert r["last_valid"] is None
    json.dumps(r)


def test_the_first_cut_s_ten_row_tail_would_have_called_it_current():
    """The defect, pinned: a gap 60 rows back is invisible to a 10-row check
    and visible to the 200-row one, on the same evidence."""
    gap = _back(60)
    p10 = ns.provenance(ROWS, _without(gap), "XSHG", window_rows=10)
    p200 = ns.provenance(ROWS, _without(gap), "XSHG", window_rows=W)
    assert bool(p10.at[DECIDED, "current"]) is True
    assert bool(p200.at[DECIDED, "current"]) is False


def test_the_window_edge_is_exact():
    """The oldest row of the window counts; the row before it does not."""
    edge, beyond = _back(W - 1), _back(W)
    assert _rank(_without(edge), weight_fn=_must_not_rank)["status"] == "HOLD"
    r = _rank(_without(beyond))
    assert r["status"] == "READY"
    assert r["native_provenance"][CN]["missing_native_sessions"] == []


def test_several_interior_gaps_are_all_named():
    gaps = [_back(3), _back(47), _back(150)]
    r = _rank(_without(*gaps), weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    assert r["native_provenance"][CN]["missing_native_sessions"] == \
        sorted(str(g.date()) for g in gaps)
    for g in gaps:
        assert str(g.date()) in r["reason"]


def test_a_stale_row_with_an_older_gap_names_both():
    """The decision row's own bar withdrawn AND one 90 rows back."""
    old = _back(90)
    observed = _without(old, DECIDED)
    r = _rank(observed, weight_fn=_must_not_rank)
    here = r["native_provenance"][CN]
    assert here["native_close_carried"] == "2026-10-27"
    assert here["missing_native_sessions"] == sorted(
        [str(old.date()), "2026-10-28"])
    assert str(old.date()) in r["reason"] and "2026-10-28" in r["reason"]


def test_month_boundary_gap_holds_and_golden_week_rows_require_it():
    """30 Sep 2026 is the last Shenzhen session before Golden Week and a month
    end. Withdrawn, it is the required close of every NYSE row 30 Sep - 7 Oct,
    and the decision row of 28 Oct still averages it."""
    gap = pd.Timestamp("2026-09-30")
    assert gap in XSHG and gap in ROWS
    p = ns.provenance(ROWS, _without(gap), "XSHG", window_rows=W)
    for d in _sessions("NYSE", "2026-10-01", "2026-10-07"):
        assert p.at[d, "required_native_session"] == gap
        assert not bool(p.at[d, "row_current"])
    r = _rank(_without(gap), weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    assert r["native_provenance"][CN]["missing_native_sessions"] == ["2026-09-30"]


def test_year_boundary_gap_holds():
    """31 Dec 2025 is a Shenzhen session; 2 Jan 2026 NYSE trades while
    Shenzhen is shut, so that row requires the 31 Dec close too. Judged from
    31 Mar 2026, whose window still reaches back across the year end (from
    28 Oct 2026 it does not: about 207 rows)."""
    gap = pd.Timestamp("2025-12-31")
    jan2 = pd.Timestamp("2026-01-02")
    rows = ROWS[ROWS <= "2026-03-31"]
    assert gap in XSHG and jan2 in rows and jan2 not in XSHG
    assert rows.get_loc(gap) > len(rows) - W               # inside the window
    assert ROWS.get_loc(gap) <= len(ROWS) - W - 1          # outside from 28 Oct
    p = ns.provenance(rows, _without(gap), "XSHG", window_rows=W)
    assert p.at[jan2, "required_native_session"] == gap
    assert p.at[rows[-1], "missing_native_sessions"] == [gap]
    r = _rank(_without(gap), rows=rows, now=_utc(2026, 4, 1, 3),
              weight_fn=_must_not_rank)
    assert r["status"] == "HOLD" and r["decision_session"] == "2026-03-31"
    assert "2025-12-31" in r["reason"]
    assert _rank(_without(gap))["status"] == "READY"


# ---------------------------------------------------------------------------
# Genuine holidays, restoration, valid inputs
# ---------------------------------------------------------------------------

def test_genuine_holidays_on_both_venues_inside_the_window_stay_ready():
    """The 200-row window ending 28 Oct 2026 spans Chinese closures (Lunar New
    Year, Labour Day, Golden Week) on which NYSE trades -- filled rows, but
    legitimate -- and US-only holidays on which Shenzhen trades but no NYSE
    row needs the bar. Neither is a missing session."""
    window = ROWS[-W:]
    cn_closed = window[~window.isin(XSHG)]
    for d in ("2026-02-17", "2026-05-04", "2026-10-05"):
        assert pd.Timestamp(d) in cn_closed
    unneeded, _ = _us_only_split()
    for d in ("2026-05-25", "2026-09-07"):              # Memorial, Labor Day
        assert pd.Timestamp(d) in unneeded
    observed = XSHG[~XSHG.isin(unneeded)]                 # those bars absent too
    r = _rank(observed)
    assert r["status"] == "READY" and r["reason"] is None
    assert r["native_provenance"][CN]["missing_native_sessions"] == []
    assert r["native_provenance"][CN]["current"] is True


def test_a_us_holiday_bar_is_required_when_shenzhen_shuts_on_the_next_row():
    """Good Friday 3 Apr 2026: NYSE shut, Shenzhen trades. Mon 6 Apr: NYSE
    trades, Shenzhen shut (Qingming). The 6 April NYSE row therefore carries
    the 3 April Shenzhen close, so that US-holiday bar IS required: withdrawn,
    the window ending 28 Oct holds. Found while writing these tests."""
    good_friday, qingming = pd.Timestamp("2026-04-03"), pd.Timestamp("2026-04-06")
    assert good_friday in XSHG and good_friday not in ROWS
    assert qingming in ROWS and qingming not in XSHG
    _, needed = _us_only_split()
    assert good_friday in needed
    p = ns.provenance(ROWS, XSHG, "XSHG", window_rows=1)
    assert p.at[qingming, "required_native_session"] == good_friday
    r = _rank(_without(good_friday), weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    assert r["native_provenance"][CN]["missing_native_sessions"] == ["2026-04-03"]


def test_a_restored_older_observation_ranks_ready_again():
    gap = _back(100)
    held = _rank(_without(gap), weight_fn=_must_not_rank)
    assert held["status"] == "HOLD"
    restored = _rank(XSHG)
    assert restored["status"] == "READY"
    assert restored["native_provenance"][CN]["missing_native_sessions"] == []


def test_valid_inputs_rank_exactly_as_without_the_check():
    """The check adds evidence, never changes the decision it lets through."""
    plain = lt._rank(_panel(ROWS), _top2, "NYSE", NOW, "C",
                     signal_kind="ma_distance", top_k=2, cash_proxy="SHY")
    checked = _rank(XSHG)
    assert checked["status"] == plain["status"] == "READY"
    extra = {"native_provenance"}
    assert {k: v for k, v in checked.items() if k not in extra} == plain


# ---------------------------------------------------------------------------
# Fails closed when the window cannot be judged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, 0, -5, True, 200.0, "200"])
def test_a_spec_that_does_not_state_its_window_holds(bad):
    r = _rank(XSHG, weight_fn=_must_not_rank, spec={"window_rows": bad})
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert "signal window not stated" in r["reason"]
    assert r["last_valid"] is None


def test_history_shorter_than_the_window_holds():
    rows = ROWS[-150:]
    r = _rank(XSHG, rows=rows, weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    here = r["native_provenance"][CN]
    assert here["window_complete"] is False and here["missing_native_sessions"] == []
    assert "does not reach back over the 200-row signal window" in r["reason"]
    assert r["last_valid"] is None


def test_a_record_starting_inside_the_window_holds():
    """Evidence that begins 150 rows back cannot vouch for the 50 before."""
    start = ROWS[-150]
    r = _rank(XSHG[XSHG >= start], weight_fn=_must_not_rank)
    assert r["status"] == "HOLD"
    missing = r["native_provenance"][CN]["missing_native_sessions"]
    assert missing and max(missing) < str(start.date())


# ---------------------------------------------------------------------------
# End to end: the fill itself, and the cache record
# ---------------------------------------------------------------------------

def test_a_forward_filled_price_is_not_an_observation_end_to_end(monkeypatch):
    """A Shenzhen bar 80 rows back withdrawn: _fx_convert_to_usd fills it (the
    backtest path is unchanged), compute_signal produces a finite, apparently
    current value on the decision row, and the rank is HOLD because the
    observation record does not contain the filled date."""
    import run_thematic_rotation as th
    gap = _back(80)
    # The download frame's index is the union of every line's dates; the
    # native line has values on Shenzhen sessions, the US lines on NYSE ones.
    union = ROWS.union(XSHG)
    native = pd.Series(np.linspace(1.0, 1.5, len(union)), index=union)
    native[~native.index.isin(XSHG)] = np.nan            # Shenzhen shut
    native.loc[gap] = np.nan                             # withdrawn
    us = pd.Series(np.where(union.isin(ROWS), 1.0, np.nan), index=union)
    df = pd.DataFrame({CN: native, th.CASH_PROXY: 80.0 * us, "X": 50.0 * us},
                      index=union)
    fx = pd.Series(7.0, index=union)
    monkeypatch.setattr(th.yf, "download",
                        lambda *a, **k: pd.DataFrame({"Close": fx}))
    monkeypatch.setattr(th, "NATIVE_OBSERVATIONS", None)
    out = th._fx_convert_to_usd(df.copy()).loc[ROWS]
    prev = ROWS[ROWS.get_loc(gap) - 1]
    assert out.at[gap, CN] == out.at[prev, CN]           # filled, not observed
    assert gap not in th.NATIVE_OBSERVATIONS[CN]
    sig = th.compute_signal(out[[CN, "X", th.CASH_PROXY]])
    assert np.isfinite(sig.at[DECIDED, CN])              # looks current
    r = lt._rank(sig, _must_not_rank, "NYSE", NOW, "C",
                 cash_proxy=th.CASH_PROXY, native_inputs=th.native_inputs())
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["native_provenance"][CN]["missing_native_sessions"] == [str(gap.date())]


def test_cache_record_round_trip_carries_an_older_gap(tmp_path, monkeypatch):
    """Written by sidecar_record, read back by the cache-hit path: the older
    gap survives the round trip and HOLDs; a clean record ranks READY."""
    import run_thematic_rotation as th
    cache = tmp_path / "thematic_prices_cache.parquet"
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    frame = pd.DataFrame({CN: 1.0}, index=ROWS)
    full = _sessions("XSHG", "2024-01-02", "2026-10-28")
    gap = _back(130)

    def _hit(observed):
        rec = ns.sidecar_record({CN: observed}, frame)
        price_source.write_cache_source(cache, "yfinance", {ns.SIDECAR_KEY: rec})
        monkeypatch.setattr(th, "NATIVE_OBSERVATIONS",
                            th._cached_native_observations(frame))
        return lt._rank(_panel(ROWS), _top2, "NYSE", NOW, "C", cash_proxy="SHY",
                        native_inputs=th.native_inputs())

    assert _hit(full[full != gap])["status"] == "HOLD"
    assert _hit(full)["status"] == "READY"


def test_a_sixty_day_record_from_the_first_cut_is_not_provenance(tmp_path,
                                                                 monkeypatch):
    """A cache written by the ten-row first cut kept sixty days. It cannot
    reach over a 200-row window, so it reads as absent: refresh, or HOLD."""
    import run_thematic_rotation as th
    frame = pd.DataFrame({CN: 1.0}, index=ROWS)
    rec = ns.sidecar_record({CN: XSHG}, frame)
    rec["window_days"] = 60
    rec["lines"][CN]["dates"] = [d for d in rec["lines"][CN]["dates"]
                                 if d >= "2026-08-29"]
    side = {ns.SIDECAR_KEY: rec}
    assert ns.parse_sidecar(side, frame, [CN]) is None
    for bad in (None, "400", True):
        rec["window_days"] = bad
        assert ns.parse_sidecar(side, frame, [CN]) is None
    cache = tmp_path / "thematic_prices_cache.parquet"
    rec["window_days"] = 60
    price_source.write_cache_source(cache, "yfinance", side)
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    assert th._cached_native_observations(frame) is None
    monkeypatch.setattr(th, "NATIVE_OBSERVATIONS", None)
    r = lt._rank(_panel(ROWS), _must_not_rank, "NYSE", NOW, "C",
                 cash_proxy="SHY", native_inputs=th.native_inputs())
    assert r["status"] == "HOLD" and "no native observation record" in r["reason"]


def test_the_record_window_covers_every_judged_row_on_the_calendar():
    """For every month-end NYSE session 2016-2026, the oldest row live_targets
    judges (NATIVE_LOOKBACK_ROWS + 200 - 1 rows back), less the longest Chinese
    closure the native lookback allows for, still lies inside the record."""
    nyse = _sessions("NYSE", "2015-01-01", "2026-12-31")
    span = lt.NATIVE_LOOKBACK_ROWS + W - 1
    ends = nyse.to_series().groupby(nyse.to_period("M")).max()
    ends = ends[ends >= pd.Timestamp("2016-01-01")]
    assert len(ends) == 132
    worst = 0
    for end in ends:
        pos = nyse.get_loc(end)
        oldest = nyse[pos - span + 1]
        need = (end - oldest).days + 15             # Chinese closure + margin
        worst = max(worst, need)
        assert need <= ns.SIDECAR_WINDOW_DAYS, end
    assert worst < ns.SIDECAR_WINDOW_DAYS


def test_a_clipped_record_just_after_lunar_new_year_still_ranks_ready():
    """The record the cache keeps (400 days, clipped by sidecar_record) is
    enough for a decision right after a long closure."""
    rows = _sessions("NYSE", "2024-06-03", "2026-03-02")
    full = _sessions("XSHG", "2024-06-03", "2026-03-02")
    frame = pd.DataFrame({CN: 1.0}, index=rows)
    rec = ns.sidecar_record({CN: full}, frame)
    obs = ns.parse_sidecar({ns.SIDECAR_KEY: rec}, frame, [CN])[CN]
    assert obs.min() > full.min()                        # clipped
    r = _rank(obs, rows=rows, now=_utc(2026, 3, 3, 3))
    assert r["status"] == "READY"
    assert r["decision_session"] == "2026-03-02"


# ---------------------------------------------------------------------------
# The freshness report reads the same window
# ---------------------------------------------------------------------------

def _cache(tmp_path, rows=ROWS):
    p = tmp_path / "th.parquet"
    pd.DataFrame({"X": 1.0, CN: 1.0}, index=rows).to_parquet(p)
    return p


def _measure(tmp_path, observed, rows=ROWS):
    cache = _cache(tmp_path, rows)
    frame = pd.read_parquet(cache)
    rec = ns.sidecar_record({CN: observed}, frame)
    frame.to_parquet(cache)              # the run writes the frame that names rec
    price_source.write_cache_source(cache, "yfinance", {ns.SIDECAR_KEY: rec})
    return sf.native_cache_reach_or_carried(
        cache, ["X", CN], [CN], "C", "price cache (thematic ETFs)",
        window_rows=W)


def test_freshness_older_gap_is_behind_and_names_it(tmp_path):
    gap = _back(60)
    reach, lag, _, carried, native = _measure(tmp_path, _without(gap))
    before = ROWS[ROWS.get_loc(gap) - 1]
    assert reach == str(before.date()) and lag == [CN] and carried is False
    status, behind = sf.classify(reach, "2026-10-28", "NYSE")
    assert status == sf.BEHIND and behind == 61
    rec = native[CN]
    assert rec["missing_native_sessions"] == [str(gap.date())]
    why, plain = sf._native_reason(native, lag)
    assert str(gap.date()) in why and "200-row" in why
    assert str(before.date()) in plain
    for jargon in ("venue", "vendor", "nyse", "xshg", "constituent", "panel"):
        assert jargon not in plain.lower(), jargon


def test_freshness_gap_beyond_the_window_and_restoration_are_current(tmp_path):
    for observed in (_without(_back(W)), XSHG):
        reach, lag, _, _, native = _measure(tmp_path, observed)
        assert (reach, lag) == ("2026-10-28", [])
        assert sf.classify(reach, "2026-10-28", "NYSE") == (sf.CURRENT, 0)
        assert sf._native_reason(native, lag) == (None, None)


def test_freshness_genuine_holidays_are_current(tmp_path):
    unneeded, _ = _us_only_split()
    reach, lag, _, _, _ = _measure(tmp_path, XSHG[~XSHG.isin(unneeded)])
    assert (reach, lag) == ("2026-10-28", [])


def test_freshness_cache_shorter_than_the_window_is_unknown(tmp_path):
    rows = ROWS[-150:]
    reach, lag, _, _, native = _measure(tmp_path, XSHG, rows=rows)
    assert reach is None and lag == [CN]
    assert sf.classify(reach, "2026-10-28", "NYSE") == (sf.UNKNOWN, None)
    assert "200-row signal window" in native[CN]["error"]


def test_freshness_build_holds_c_behind_on_an_older_gap(tmp_path, monkeypatch):
    import types
    import run_thematic_rotation as th
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS

    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    for etf in list(UNIVERSE_ETFS) + list(UNIVERSE_EUROPE_SECTORS):
        (tmp_path / f"breadth_{etf.lower()}.json").write_text(
            json.dumps({"end_date": "2026-10-28"}), encoding="utf-8")
    ac_cache = tmp_path / "ac.parquet"
    pd.DataFrame({"SPY": 1.0}, index=ROWS).to_parquet(ac_cache)
    monkeypatch.setitem(sys.modules, "run_asset_class_rotation",
                        types.SimpleNamespace(PRICE_CACHE=ac_cache,
                                              TICKERS=["SPY"]))
    th_cache = tmp_path / "th.parquet"
    pd.DataFrame({t: 1.0 for t in th.TICKERS}, index=ROWS).to_parquet(th_cache)
    gap = _back(25)
    th_frame = pd.read_parquet(th_cache)
    rec = ns.sidecar_record({CN: _without(gap)}, th_frame)
    th_frame.to_parquet(th_cache)
    price_source.write_cache_source(th_cache, "yfinance", {ns.SIDECAR_KEY: rec})
    monkeypatch.setattr(th, "PRICE_CACHE", th_cache)

    r = sf.build(now_utc=NOW)
    c = {s["sleeve"]: s for s in r["strategies"]}["C"]
    assert c["status"] == sf.BEHIND and c["status"] != sf.CURRENT
    assert c["data_through"] == str(ROWS[ROWS.get_loc(gap) - 1].date())
    assert c["laggards"] == [CN]
    assert c["native_provenance"][CN]["window_rows"] == th.MA_PERIOD
    assert str(gap.date()) in c["why"]
    assert r["all_current"] is False
    json.dumps(r)
