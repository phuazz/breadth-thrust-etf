"""The engine caches settle their tail against the vendor (2026-09-06).

THE FAILURE THESE PIN. Sleeve C reported HOLD for the 7/8 September fill
with 25 of 26 names on the decision row: BTC-USD's Friday close was blank.
The failed Saturday run had fetched at 01:55 UTC, before yfinance served the
crypto bar, and written the Friday row with the 24 Norgate lines priced and
BTC-USD blank; the gitignored cache survived the clone's restore; on Sunday
the engine read ``cached.index.max()`` — Friday — called the cache current
and reused it. The bar was being served by 06:00 UTC. Sleeve B had learnt
the same lesson on 2026-08-31 (SPY) and measured per column; sleeve C had
not.

Two rules now, shared in vendor_tail: a cache is current only through its
LEAST current column, on values; and a blank tail cell of a name the batch
left behind is asked for single-ticker before the cache is written — filled
when served, left blank (row kept, HOLD downstream) when not. Every vendor
call here is stubbed.

Python datetime months are 1-indexed (January = 1).
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

import price_source as ps  # noqa: E402
import vendor_tail as vt  # noqa: E402

IDX = pd.bdate_range("2024-01-02", "2024-01-19")     # 14 sessions, ends a Friday
FRI = IDX[-1]
THU = IDX[-2]


def _frame(names, blank_on_last=(), idx=IDX):
    f = pd.DataFrame({t: np.linspace(100.0 + i, 110.0 + i, len(idx))
                      for i, t in enumerate(names)}, index=idx)
    for t in blank_on_last:
        f.loc[idx[-1], t] = np.nan
    return f


def _serving(frame, through=None):
    """Single-ticker stub: the column's own values, carried forward, up to
    ``through`` inclusive (None = every date)."""
    def fetch(t):
        s = frame[t].ffill().copy()
        return s if through is None else s.loc[:pd.Timestamp(through)]
    return fetch


# ---------------------------------------------------------------------------
# cache_current_through — the least current column, on values
# ---------------------------------------------------------------------------
def test_least_current_column_decides():
    f = _frame(["A", "B", "C"], blank_on_last=["B"])
    assert vt.cache_current_through(f) == THU.date()
    assert vt.cache_current_through(f, needed=["A", "C"]) == FRI.date()


def test_index_reaching_friday_is_not_enough():
    """The 2026-09-06 shape exactly: the index reaches Friday because 25
    names do; the 26th does not, and the cache is a session short."""
    f = _frame(["N1", "N2", "BTC-USD"], blank_on_last=["BTC-USD"])
    assert f.index.max() == FRI
    assert vt.cache_current_through(f, needed=["N1", "N2", "BTC-USD"]) == THU.date()


def test_empty_and_all_nan_caches_are_none():
    assert vt.cache_current_through(None) is None
    assert vt.cache_current_through(pd.DataFrame()) is None
    f = _frame(["A"])
    f[:] = np.nan
    assert vt.cache_current_through(f) is None


def test_a_dead_column_does_not_pin_the_cache_in_the_past():
    f = _frame(["A", "DEAD"])
    f["DEAD"] = np.nan
    assert vt.cache_current_through(f) == FRI.date()


# ---------------------------------------------------------------------------
# heal_hollow_tail — fill what the vendor serves, never drop a row
# ---------------------------------------------------------------------------
def test_served_cell_is_filled_and_recorded():
    f = _frame(["N1", "N2", "BTC-USD"], blank_on_last=["BTC-USD"])
    out, rec = vt.heal_hollow_tail(f, ["N1", "N2", "BTC-USD"], through=FRI.date(),
                                   fetch_single=_serving(f))
    assert pd.notna(out.loc[FRI, "BTC-USD"])
    assert rec["last_full_row"] == str(THU.date())
    assert rec["rows"][0]["filled"] == ["BTC-USD"]
    assert rec["rows"][0]["hollow"] == ["BTC-USD"]
    assert pd.isna(f.loc[FRI, "BTC-USD"]), "the caller's frame is not mutated"


def test_unserved_cell_stays_blank_and_the_row_stays():
    """A name the vendor still does not serve is left blank: the row is
    partial, and live_targets' 100% floor makes that a HOLD. Nothing is
    dropped and nothing is invented."""
    f = _frame(["N1", "N2", "BTC-USD"], blank_on_last=["BTC-USD"])
    out, rec = vt.heal_hollow_tail(f, ["N1", "N2", "BTC-USD"], through=FRI.date(),
                                   fetch_single=_serving(f, through=THU))
    assert pd.isna(out.loc[FRI, "BTC-USD"])
    assert FRI in out.index and len(out) == len(f)
    assert rec["rows"][0]["unserved"] == ["BTC-USD"] and rec["rows"][0]["filled"] == []


def test_no_answer_is_recorded_as_such():
    f = _frame(["N1", "BTC-USD"], blank_on_last=["BTC-USD"])
    out, rec = vt.heal_hollow_tail(f, ["N1", "BTC-USD"], through=FRI.date(),
                                   fetch_single=lambda t: None)
    assert pd.isna(out.loc[FRI, "BTC-USD"])
    assert rec["rows"][0]["no_answer"] == ["BTC-USD"]

    def boom(t):
        raise RuntimeError("rate limited")

    _, rec = vt.heal_hollow_tail(f, ["N1", "BTC-USD"], through=FRI.date(),
                                 fetch_single=boom)
    assert rec["rows"][0]["no_answer"] == ["BTC-USD"]


def test_excluded_columns_are_never_asked():
    """A Norgate-owned column must not receive a yfinance cell (WS19b)."""
    f = _frame(["N1", "N2"], blank_on_last=["N1", "N2"])
    asked = []

    def fetch(t):
        asked.append(t)
        return _serving(f)(t)

    out, rec = vt.heal_hollow_tail(f, ["N1", "N2"], through=FRI.date(),
                                   exclude=["N1"], fetch_single=fetch)
    assert asked == ["N2"]
    assert pd.isna(out.loc[FRI, "N1"]) and pd.notna(out.loc[FRI, "N2"])


def test_nothing_hollow_means_nothing_asked():
    f = _frame(["N1", "N2"])

    def must_not(t):
        raise AssertionError("a full tail must not be probed")

    out, rec = vt.heal_hollow_tail(f, ["N1", "N2"], through=FRI.date(),
                                   fetch_single=must_not)
    assert rec is None
    pd.testing.assert_frame_equal(out, f)


def test_rows_beyond_through_are_the_caps_business_not_the_vendors():
    """The crypto line carries weekend rows and an in-progress day past the
    last completed session; the heal must not chase those — they are for
    cap_to_last_completed_session to drop."""
    idx = pd.DatetimeIndex(list(IDX) + [pd.Timestamp("2024-01-20"),
                                        pd.Timestamp("2024-01-21")])
    f = pd.DataFrame({"N1": [100.0] * len(idx), "BTC-USD": [50.0] * len(idx)},
                     index=idx)
    f.loc[idx[-2:], "N1"] = np.nan            # equity line has no weekend
    f.loc[FRI, "BTC-USD"] = np.nan            # the blank that matters
    asked = []

    def fetch(t):
        asked.append(t)
        return _serving(f)(t)

    out, rec = vt.heal_hollow_tail(f, ["N1", "BTC-USD"], through=FRI.date(),
                                   fetch_single=fetch)
    assert [r["date"] for r in rec["rows"]] == [str(FRI.date())]
    assert pd.notna(out.loc[FRI, "BTC-USD"])
    assert pd.isna(out.loc[idx[-1], "N1"]), "weekend rows untouched"


def test_names_blank_on_the_last_full_row_cannot_be_healed_into_existence():
    """A delisted column is blank everywhere and must not be asked for."""
    f = _frame(["N1", "N2", "DEAD"], blank_on_last=["N1"])
    f["DEAD"] = np.nan
    asked = []

    def fetch(t):
        asked.append(t)
        return _serving(f)(t)

    vt.heal_hollow_tail(f, ["N1", "N2"], through=FRI.date(), fetch_single=fetch)
    assert asked == ["N1"]


def test_budget_exhaustion_stops_asking_and_says_so():
    f = _frame(["N1", "N2", "N3"], blank_on_last=["N1", "N2", "N3"])
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    out, rec = vt.heal_hollow_tail(f, ["N1", "N2", "N3"], through=FRI.date(),
                                   fetch_single=_serving(f), budget_s=10.0,
                                   clock=lambda: next(ticks))
    assert rec["budget_exhausted"] is True
    assert rec["rows"][0]["filled"] == ["N1"]
    assert pd.isna(out.loc[FRI, "N3"])


@pytest.mark.parametrize("last_full, blank", [
    ("2026-08-31", "2026-09-01"),   # month boundary: Mon 31 Aug -> Tue 1 Sep
    ("2026-12-31", "2027-01-04"),   # year boundary: Thu 31 Dec -> Mon 4 Jan
])
def test_heal_across_month_and_year_boundaries(last_full, blank):
    idx = pd.DatetimeIndex([pd.Timestamp(last_full) - pd.Timedelta(days=1),
                            pd.Timestamp(last_full), pd.Timestamp(blank)])
    f = pd.DataFrame({"N1": [1.0, 2.0, 3.0], "N2": [1.0, 2.0, np.nan]}, index=idx)
    out, rec = vt.heal_hollow_tail(f, ["N1", "N2"], through=date.fromisoformat(blank),
                                   fetch_single=_serving(f))
    assert rec["last_full_row"] == last_full
    assert rec["rows"][0]["date"] == blank and rec["rows"][0]["filled"] == ["N2"]
    assert pd.notna(out.loc[pd.Timestamp(blank), "N2"])


# ---------------------------------------------------------------------------
# The sidecar carries the record
# ---------------------------------------------------------------------------
def test_sidecar_records_the_heal_beside_the_source(tmp_path):
    cache = tmp_path / "thematic_prices_cache.parquet"
    heal = {"rows": [{"date": "2026-09-04", "hollow": ["BTC-USD"],
                      "filled": ["BTC-USD"], "unserved": [], "no_answer": []}]}
    ps.write_cache_source(cache, "norgate",
                          {"replaced": ["ARKK"], "kept": [], "unresolved": ["BTC-USD"],
                           "tail_heal": heal})
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert blob["columns_from_norgate"] == ["ARKK"]
    assert blob["tail_heal"] == heal
    # A yfinance run with only a heal to report does not claim Norgate keys.
    ps.write_cache_source(cache, "yfinance", {"tail_heal": heal})
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert "columns_from_norgate" not in blob and blob["tail_heal"] == heal
    assert ps.read_cache_source(cache) == "yfinance"


# ---------------------------------------------------------------------------
# Through the engines
# ---------------------------------------------------------------------------
def _yf(frame: pd.DataFrame, needed):
    """Shape yf.download returns under group_by='ticker'."""
    yf_frame = pd.concat({t: pd.DataFrame({"Close": frame[t]}) for t in needed}, axis=1)
    return type("Y", (), {"download": staticmethod(lambda *a, **k: yf_frame.copy())})()


@pytest.fixture
def thematic_site(monkeypatch, tmp_path):
    """Sleeve C on the Sunday shape: every line priced through Friday except
    BTC-USD, whose Friday cell the batch left blank."""
    import run_thematic_rotation as th
    import norgate_prices as npx
    needed = th.TICKERS + [th.CASH_PROXY]
    assert "BTC-USD" in needed
    frame = _frame(needed, blank_on_last=["BTC-USD"])
    cache = tmp_path / "thematic_prices_cache.parquet"
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    monkeypatch.setattr(th, "last_completed_session", lambda now: FRI.date())
    monkeypatch.setattr(th, "cap_to_last_completed_session",
                        lambda df, now_utc=None: df)
    monkeypatch.setattr(th, "_fx_convert_to_usd", lambda df: df)
    monkeypatch.setattr(th, "yf", _yf(frame, needed))
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(
        npx, "select_columns",
        lambda df, tickers, s, e, label="": (
            df, {"replaced": [t for t in tickers if ps.plain_us_listing(t)],
                 "kept": [],
                 "unresolved": [t for t in tickers if not ps.plain_us_listing(t)]}))
    return th, cache, frame, needed


def test_sleeve_c_refreshes_a_cache_whose_index_reaches_friday_but_btc_does_not(
        thematic_site, monkeypatch, capsys):
    """Saturday's cache on Sunday: index through Friday, BTC-USD blank there.
    It must be refreshed, not reused — and the refresh heals the cell."""
    th, cache, frame, needed = thematic_site
    frame.to_parquet(cache)
    ps.write_cache_source(cache, "norgate")
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setattr(vt, "single_ticker_closes", _serving(frame))
    out = th.download_prices()
    printed = capsys.readouterr().out
    assert "Using cached prices" not in printed
    assert f"least current column ends {THU.date()}" in printed
    assert pd.notna(out.loc[FRI, "BTC-USD"])
    assert pd.notna(pd.read_parquet(cache).loc[FRI, "BTC-USD"])
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert blob["source"] == "norgate"
    assert blob["tail_heal"]["rows"][0]["filled"] == ["BTC-USD"]


def test_sleeve_c_leaves_an_unserved_cell_blank_without_failing(thematic_site, monkeypatch):
    th, cache, frame, needed = thematic_site
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setattr(vt, "single_ticker_closes", _serving(frame, through=THU))
    out = th.download_prices()
    assert pd.isna(out.loc[FRI, "BTC-USD"])
    assert out.loc[FRI, [t for t in needed if t != "BTC-USD"]].notna().all()
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert blob["tail_heal"]["rows"][0]["unserved"] == ["BTC-USD"]


def test_sleeve_c_never_asks_yfinance_for_a_norgate_column(thematic_site, monkeypatch):
    th, cache, frame, needed = thematic_site
    frame.loc[FRI, "ARKK"] = np.nan             # a Norgate line blank in the batch
    monkeypatch.setattr(th, "yf", _yf(frame, needed))
    asked = []

    def fetch(t):
        asked.append(t)
        return _serving(frame)(t)

    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setattr(vt, "single_ticker_closes", fetch)
    th.download_prices()
    assert asked == ["BTC-USD"]


def test_sleeve_c_reuses_a_genuinely_current_cache(thematic_site, monkeypatch, capsys):
    """The rule must not over-refresh: a cache priced through Friday on
    every needed name is reused, and nothing is asked."""
    th, cache, frame, needed = thematic_site
    full = frame.copy()
    full.loc[FRI, "BTC-USD"] = 123.0
    full.to_parquet(cache)
    ps.write_cache_source(cache, "norgate")
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")

    def must_not(t):
        raise AssertionError("no probe on a reused cache")

    monkeypatch.setattr(vt, "single_ticker_closes", must_not)
    out = th.download_prices()
    assert "Using cached prices" in capsys.readouterr().out
    assert out.loc[FRI, "BTC-USD"] == 123.0


def test_sleeve_b_heals_a_blank_spy_on_a_yfinance_run(monkeypatch, tmp_path):
    """The 2026-08-31 shape on sleeve B, on the source every CI runner uses:
    SPY blank on the Friday row of the batch, served single-ticker."""
    import run_asset_class_rotation as ac
    needed = ac.TICKERS + ac.CASH_ONLY_TICKERS
    frame = _frame(needed, blank_on_last=["SPY"])
    cache = tmp_path / "asset_class_prices_cache.parquet"
    monkeypatch.setattr(ac, "PRICE_CACHE", cache)
    monkeypatch.setattr(ac, "last_completed_session", lambda now: FRI.date())
    monkeypatch.setattr(ac, "cap_to_last_completed_session",
                        lambda df, now_utc=None: df)
    monkeypatch.setattr(ac, "yf", _yf(frame, needed))
    monkeypatch.delenv("BTE_PRICE_SOURCE", raising=False)
    monkeypatch.setattr(vt, "single_ticker_closes", _serving(frame))
    out = ac.download_prices()
    assert pd.notna(out.loc[FRI, "SPY"])
    assert pd.notna(pd.read_parquet(cache).loc[FRI, "SPY"])
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert blob["source"] == "yfinance" and "columns_from_norgate" not in blob
    assert blob["tail_heal"]["rows"][0]["filled"] == ["SPY"]
