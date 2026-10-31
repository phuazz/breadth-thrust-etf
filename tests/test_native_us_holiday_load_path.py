"""Sleeve C's Shenzhen line through the real load path (2026-10-29).

THE FAILURE THESE PIN. The supervised refresh of 2026-10-29 held sleeve C on
the 2026-10-28 decision row because the 2026-04-03 Shenzhen close was "not
observed". Yahoo had served it (0.803 CNY, Good Friday, NYSE shut). The load
path dropped it: _reindex_crypto_to_equity_calendar keeps NYSE rows only and
ran BEFORE _fx_convert_to_usd, so a Shenzhen session on a US holiday was never
converted or recorded, and the 2026-04-06 NYSE row (Qingming in Shenzhen)
carried Thursday's close. Every earlier test either stubbed _fx_convert_to_usd
or called it directly on a hand-built frame, so none saw the order.

These run download_prices() itself, with only the vendor and the session clock
stubbed, then the real compute_signal and the real ranking with the real
native_inputs(). Offline. Python datetime months are 1-indexed (January = 1).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
import price_source as ps  # noqa: E402
import vendor_tail as vt  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
GOOD_FRIDAY = pd.Timestamp("2026-04-03")
QINGMING_MONDAY = pd.Timestamp("2026-04-06")
THURSDAY = pd.Timestamp("2026-04-02")
LAST = pd.Timestamp("2026-04-17")
NOW = pd.Timestamp("2026-04-18 03:00", tz="UTC").to_pydatetime()
SPIKE = 3.0      # the Good Friday native close, far from its neighbours


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


NYSE = _sessions("NYSE", "2025-05-01", LAST)          # > 200 rows before LAST
XSHG = _sessions("XSHG", "2025-05-01", LAST)


def test_the_calendars_have_the_shape_this_file_is_about():
    assert GOOD_FRIDAY.weekday() == 4 and QINGMING_MONDAY.weekday() == 0
    assert GOOD_FRIDAY in XSHG and GOOD_FRIDAY not in NYSE
    assert QINGMING_MONDAY in NYSE and QINGMING_MONDAY not in XSHG
    assert (NYSE <= LAST).sum() > 200


def _vendor(th, withdraw_good_friday=False):
    """What yf.download returns: the batch under group_by='ticker' on the
    union of the venues' dates, and CNY=X on every weekday."""
    names = th.TICKERS + [th.CASH_PROXY]
    union = NYSE.union(XSHG)
    cols = {}
    for i, t in enumerate(names):
        if t == CN:
            s = pd.Series(1.0 + 0.001 * np.arange(len(XSHG)), index=XSHG)
            s.loc[GOOD_FRIDAY] = SPIKE
            if withdraw_good_friday:
                s = s.drop(GOOD_FRIDAY)
            cols[t] = s.reindex(union)
        elif th.UNIVERSE.get(t, {}).get("trading_calendar") == "crypto_24x7":
            cols[t] = pd.Series(100.0 + i + 0.05 * np.arange(len(union)), index=union)
        else:
            cols[t] = pd.Series(100.0 + i + 0.05 * np.arange(len(NYSE)),
                                index=NYSE).reindex(union)
    batch = pd.concat({t: pd.DataFrame({"Close": cols[t]}) for t in names}, axis=1)
    weekdays = pd.bdate_range(union.min(), union.max())
    fx = pd.DataFrame({"Close": 7.0}, index=weekdays)

    def download(tickers, *a, **k):
        return fx.copy() if tickers == "CNY=X" else batch.copy()
    return type("Y", (), {"download": staticmethod(download)})()


@pytest.fixture(params=["norgate", "yfinance"])
def load(request, monkeypatch, tmp_path):
    import norgate_prices as npx
    import run_thematic_rotation as th
    cache = tmp_path / "thematic_prices_cache.parquet"
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    monkeypatch.setattr(th, "last_completed_session", lambda now: LAST.date())
    monkeypatch.setattr(th, "cap_to_last_completed_session",
                        lambda df, now_utc=None: df.loc[:LAST])
    monkeypatch.setattr(vt, "single_ticker_closes", lambda t: None)
    monkeypatch.setenv("BTE_PRICE_SOURCE", request.param)
    monkeypatch.delenv("BTE_C_BTC_BASIS", raising=False)
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(          # Norgate keeps the base's union index
        npx, "select_columns",
        lambda df, tickers, s, e, label="": (
            df, {"replaced": [t for t in tickers if ps.plain_us_listing(t)],
                 "kept": [],
                 "unresolved": [t for t in tickers if not ps.plain_us_listing(t)]}))

    def run(withdraw_good_friday=False):
        monkeypatch.setattr(th, "yf", _vendor(th, withdraw_good_friday))
        out = th.download_prices()
        return th, out, cache
    return run


def test_the_good_friday_close_reaches_the_qingming_row(load):
    th, out, _ = load()
    assert GOOD_FRIDAY not in out.index                    # the frame stays NYSE
    assert out.index.equals(NYSE)
    ratio = out.at[QINGMING_MONDAY, CN] / out.at[THURSDAY, CN]
    # Before the fix this was about 1: Thursday's close carried forward.
    assert ratio == pytest.approx(SPIKE / (1.0 + 0.001 * XSHG.get_loc(THURSDAY)), rel=1e-3)
    assert GOOD_FRIDAY in th.NATIVE_OBSERVATIONS[CN]


def test_the_record_written_beside_the_cache_carries_the_us_holiday_session(load, capsys):
    """Written to the sidecar, and read back by the next run's cache reuse."""
    th, first, cache = load()
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert "2026-04-03" in blob[ns.SIDECAR_KEY]["lines"][CN]["dates"]
    capsys.readouterr()
    again = th.download_prices()
    assert "Using cached prices" in capsys.readouterr().out
    assert GOOD_FRIDAY in th.NATIVE_OBSERVATIONS[CN]
    assert again.at[QINGMING_MONDAY, CN] == pytest.approx(first.at[QINGMING_MONDAY, CN])


def test_the_real_signal_ranks_ready_with_the_window_over_good_friday(load):
    th, out, _ = load()
    spec = th.native_inputs()[CN]
    assert spec["window_rows"] == th.MA_PERIOD == 200
    p = ns.provenance(out.index, spec["observed"], spec["venue"], window_rows=1)
    assert p.at[QINGMING_MONDAY, "required_native_session"] == GOOD_FRIDAY
    assert bool(p.at[QINGMING_MONDAY, "current"])
    assert NYSE.get_loc(LAST) - NYSE.get_loc(QINGMING_MONDAY) < 200   # in the window
    r = lt._rank(th.compute_signal(out), _top2, "NYSE", NOW, "C",
                 signal_kind="ma_distance", top_k=2, cash_proxy=th.CASH_PROXY,
                 native_inputs=th.native_inputs())
    assert r["status"] == "READY", r.get("reason")
    assert r["native_provenance"][CN]["missing_native_sessions"] == []


def test_a_good_friday_bar_the_vendor_withdrew_still_holds(load):
    """The fix converts what was served; it does not excuse what was not."""
    th, out, _ = load(withdraw_good_friday=True)
    assert out.at[QINGMING_MONDAY, CN] == pytest.approx(out.at[THURSDAY, CN], rel=1e-3)
    assert GOOD_FRIDAY not in th.NATIVE_OBSERVATIONS[CN]

    def must_not_rank(_row):
        raise AssertionError("an uncertified native window must never reach the ranker")

    r = lt._rank(th.compute_signal(out), must_not_rank, "NYSE", NOW, "C",
                 signal_kind="ma_distance", top_k=2, cash_proxy=th.CASH_PROXY,
                 native_inputs=th.native_inputs())
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["native_provenance"][CN]["missing_native_sessions"] == ["2026-04-03"]
    assert "2026-04-03" in r["reason"]


def test_us_only_rows_no_nyse_row_needs_change_nothing(load):
    """Thanksgiving, Christmas and the other US-only sessions: converting them
    moves no NYSE row's value, because the next NYSE row has its own close."""
    th, out, _ = load()
    for d in ("2025-11-27", "2025-12-25", "2026-01-19"):
        d = pd.Timestamp(d)
        assert d in XSHG and d not in NYSE and d in th.NATIVE_OBSERVATIONS[CN]
        nxt = NYSE[NYSE > d][0]
        assert nxt in XSHG
        k = XSHG.get_loc(nxt)
        assert out.at[nxt, CN] == pytest.approx((1.0 + 0.001 * k) / 7.0, rel=5e-3)


def _top2(row):
    top = row.drop("SHY", errors="ignore").dropna().sort_values(ascending=False).head(2)
    return top / top.sum()
