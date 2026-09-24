"""Norgate tail re-source for kept US columns (2026-09-24).

THE FAILURE THESE PIN. The 2026-09-23 06:43 UTC post-fill refresh ran IUMS on
'auto'. Norgate resolved all 38 names, but WS19b kept five on yfinance because
their yfinance series reach back past the US listing (LIN, CRH, SW, AMCR, DD),
so Norgate's dates can never be a superset. Yahoo's overnight retraction left
LIN/CRH/SW/AMCR without the 2026-09-22 bar; the tail check found 21 of 25
roster names priced, all four answered without the bar, the row was dropped
as a placeholder, the panel capped at 2026-09-21 and sleeve A went to HOLD.
Norgate carried all four for 2026-09-22, level-identical to yfinance on the
last ten shared closes.

The fix fills the TRAILING cells of those kept columns from Norgate, under the
fb5122d6 agreement test, for plain US listings on the roster only. A
European-suffixed name, an interior gap, a long outage, a moved adjustment
vintage and a yfinance run are all left exactly as they were. Nothing here
hits the network. The coverage floor in _rank() is not touched.

Python datetime months are 1-indexed (January = 1); every index is built with
pandas (bdate_range with start= and periods=, which the local pandas 3.0.0
bdate_range(end=weekend) bug does not affect), never a hand-computed offset.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compute_breadth as cb  # noqa: E402
import export_holdings_prices as ehp  # noqa: E402

NG_ROSTER = ["ALB", "APD", "ECL"]          # taken whole from Norgate
KEPT = ["LIN", "CRH", "SW", "AMCR"]        # served by Norgate, kept on yfinance
ROSTER = NG_ROSTER + KEPT


def _frames(tail_date: str = "2026-09-22", n_days: int = 40,
            ng_start_offset: int = 15):
    """``n_days`` sessions ending on ``tail_date``. Incumbent (yfinance) columns
    carry every session except the tail for the kept names; Norgate carries the
    kept names from ``ng_start_offset`` sessions in (their US listing) through
    the tail, at the incumbent's level. Returns (incumbent, norgate, idx)."""
    tail = pd.Timestamp(tail_date)
    # Build forwards so the tail session is the last element, then check it.
    idx = pd.bdate_range(start=tail - pd.offsets.BDay(n_days - 1), periods=n_days)
    assert idx[-1] == tail
    inc, ng = {}, {}
    for i, t in enumerate(ROSTER):
        s = pd.Series(100.0 + 3 * i + np.arange(n_days, dtype=float), index=idx)
        if t in KEPT:
            inc[t] = s.copy()
            inc[t].iloc[-1] = np.nan          # Yahoo withdrew the tail bar
            ng[t] = s.iloc[ng_start_offset:]  # Norgate from the US listing on
        else:
            inc[t] = s                        # Norgate column, already whole
    return pd.DataFrame(inc), pd.DataFrame(ng), idx


# ---------------------------------------------------------------------------
# The pure function
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tail_date, prev", [
    ("2026-09-22", "2026-09-21"),   # the live instance (Tue after Mon)
    ("2026-10-01", "2026-09-30"),   # month boundary
    ("2027-01-01", "2026-12-31"),   # year boundary (bdate_range is calendar-free)
])
def test_the_2026_09_23_shape_fills_the_tail_from_norgate(tail_date, prev):
    inc, ng, idx = _frames(tail_date)
    out, rec = cb.resource_tail_from_norgate(inc, ng, KEPT, ROSTER)
    tail = pd.Timestamp(tail_date)
    assert out.loc[tail, ROSTER].notna().all()
    for t in KEPT:
        assert out.at[tail, t] == ng.at[tail, t]
        assert rec["filled"][t]["dates"] == [tail_date]
        assert rec["filled"][t]["max_rel_diff"] == 0.0
    assert rec["declined"] == {}
    # The history before the listing is still the incumbent's, untouched.
    pd.testing.assert_frame_equal(out.iloc[:-1], inc.iloc[:-1])
    assert out.index.equals(inc.index), "no row may be added"
    assert str(idx[-2].date()) == prev
    assert str(inc["LIN"].dropna().index.max().date()) == prev


def test_the_input_frame_is_not_mutated():
    inc, ng, _ = _frames()
    before = inc.copy()
    cb.resource_tail_from_norgate(inc, ng, KEPT, ROSTER)
    pd.testing.assert_frame_equal(inc, before)


def test_a_european_listing_is_never_touched():
    """Even handed a Norgate column under a suffixed ticker - a wrong-match -
    the name is not a plain US listing and is left blank."""
    inc, ng, idx = _frames()
    inc["SIKA.SW"] = inc["LIN"] * 2
    ng["SIKA.SW"] = ng["LIN"] * 2
    out, rec = cb.resource_tail_from_norgate(
        inc, ng, KEPT + ["SIKA.SW"], ROSTER + ["SIKA.SW"])
    assert pd.isna(out.at[idx[-1], "SIKA.SW"])
    assert "SIKA.SW" not in rec["filled"] and "SIKA.SW" not in rec["declined"]


@pytest.mark.parametrize("ticker", ["RIO.L", "AIR.PA", "SIE.DE", "BTC-USD", "EURUSD=X"])
def test_non_us_tickers_are_filtered_by_the_shared_rule(ticker):
    inc, ng, idx = _frames()
    inc[ticker] = inc["LIN"]
    ng[ticker] = ng["LIN"]
    out, rec = cb.resource_tail_from_norgate(inc, ng, [ticker], ROSTER + [ticker])
    assert rec is None
    assert pd.isna(out.at[idx[-1], ticker])


def test_a_name_off_the_roster_is_not_filled():
    inc, ng, idx = _frames()
    out, rec = cb.resource_tail_from_norgate(inc, ng, KEPT, NG_ROSTER + ["LIN"])
    assert list(rec["filled"]) == ["LIN"]
    assert out.loc[idx[-1], ["CRH", "SW", "AMCR"]].isna().all()


def test_a_moved_adjustment_vintage_is_declined():
    """A dividend going ex on the tail session re-scales Norgate's TOTALRETURN
    history by the yield; 0.3% is six times the tolerance. Two bases: not
    spliced, the cell stays blank and the tail check judges the row."""
    inc, ng, idx = _frames()
    ng["LIN"] = ng["LIN"] * 0.997
    out, rec = cb.resource_tail_from_norgate(inc, ng, KEPT, ROSTER)
    assert pd.isna(out.at[idx[-1], "LIN"])
    assert "disagree" in rec["declined"]["LIN"]
    assert set(rec["filled"]) == {"CRH", "SW", "AMCR"}


def test_rounding_inside_the_tolerance_is_accepted():
    inc, ng, idx = _frames()
    ng["LIN"] = ng["LIN"] * (1 + 4e-4)
    out, rec = cb.resource_tail_from_norgate(inc, ng, KEPT, ROSTER)
    assert out.at[idx[-1], "LIN"] == ng.at[idx[-1], "LIN"]


def test_a_long_outage_is_not_a_vendor_lag():
    inc, ng, idx = _frames()
    inc.loc[idx[-7]:, "LIN"] = np.nan              # seven trailing sessions
    out, rec = cb.resource_tail_from_norgate(inc, ng, ["LIN"], ROSTER)
    assert out.loc[idx[-7]:, "LIN"].isna().all()
    assert "not a vendor lag" in rec["declined"]["LIN"]


def test_norgate_missing_the_incumbent_last_bar_is_declined():
    inc, ng, idx = _frames()
    ng.loc[idx[-2], "LIN"] = np.nan
    out, rec = cb.resource_tail_from_norgate(inc, ng, ["LIN"], ROSTER)
    assert pd.isna(out.at[idx[-1], "LIN"])
    assert "scale unverified" in rec["declined"]["LIN"]


def test_too_little_overlap_is_declined():
    """A listing younger than the overlap window cannot verify the scale."""
    inc, ng, idx = _frames(ng_start_offset=35)     # 4 shared bars before the tail
    out, rec = cb.resource_tail_from_norgate(inc, ng, ["LIN"], ROSTER)
    assert pd.isna(out.at[idx[-1], "LIN"])
    assert "required to verify the scale" in rec["declined"]["LIN"]


def test_only_the_contiguous_run_is_filled_never_a_hole_behind_it():
    inc, ng, idx = _frames()
    inc.loc[idx[-2]:, "LIN"] = np.nan              # two trailing sessions
    ng.loc[idx[-2], "LIN"] = np.nan                # Norgate lacks the first
    inc.loc[idx[-2]:, "CRH"] = np.nan
    out, rec = cb.resource_tail_from_norgate(inc, ng, ["LIN", "CRH"], ROSTER)
    # LIN: Norgate lacks the first trailing session, so nothing is filled
    assert out.loc[idx[-2]:, "LIN"].isna().all()
    assert "first trailing session" in rec["declined"]["LIN"]
    # CRH: both sessions carried, both filled, in order
    assert rec["filled"]["CRH"]["dates"] == [str(d.date()) for d in idx[-2:]]


def test_an_interior_gap_is_never_filled():
    inc, ng, idx = _frames()
    inc.at[idx[-5], "LIN"] = np.nan                 # interior hole, tail present
    inc.at[idx[-1], "LIN"] = 999.0
    out, rec = cb.resource_tail_from_norgate(inc, ng, ["LIN"], ROSTER)
    assert pd.isna(out.at[idx[-5], "LIN"])
    assert rec is None


def test_the_through_cap_bounds_the_fill():
    inc, ng, idx = _frames()
    out, rec = cb.resource_tail_from_norgate(inc, ng, KEPT, ROSTER,
                                             through=idx[-2])
    assert out.loc[idx[-1], KEPT].isna().all()
    assert rec is None


def test_nothing_to_do_returns_the_frame_and_no_record():
    inc, ng, _ = _frames()
    assert cb.resource_tail_from_norgate(inc, None, KEPT, ROSTER)[1] is None
    assert cb.resource_tail_from_norgate(inc, ng, [], ROSTER)[1] is None
    assert cb.resource_tail_from_norgate(inc, ng, KEPT, [])[1] is None


def test_the_tolerances_are_the_export_guard_s():
    """One agreement rule across the codebase: the export's regression
    re-source (fb5122d6) and this one may not drift apart."""
    assert cb.NORGATE_TAIL_RTOL == ehp.VENDOR_GAP_RTOL
    assert cb.NORGATE_TAIL_OVERLAP_BARS == ehp.VENDOR_GAP_OVERLAP_BARS


# ---------------------------------------------------------------------------
# Through download_prices: the seam, the sidecar, and the yfinance run
# ---------------------------------------------------------------------------
def _yf(frame: pd.DataFrame):
    return type("Y", (), {
        "download": staticmethod(
            lambda *a, **k: pd.concat({"Close": frame.copy()}, axis=1))})()


def _span(idx) -> tuple[str, str]:
    return str(idx[0].date()), str((idx[-1] + pd.Timedelta(days=3)).date())


def _stub_norgate(monkeypatch, inc, ng):
    import norgate_prices
    ng_full = ng.reindex(ng.index.union(inc.index))
    for t in NG_ROSTER:
        ng_full[t] = inc[t]                         # superset: taken whole
    monkeypatch.setattr(norgate_prices, "available", lambda: True)
    monkeypatch.setattr(
        norgate_prices, "fetch_closes",
        lambda tickers, start, end, verbose=True: (
            ng_full[[t for t in tickers if t in ng_full.columns]],
            sorted(t for t in tickers if t in ng_full.columns), []))


def _unserving(inc):
    """Single-ticker yfinance: answers, without the tail bar (the retraction)."""
    def fetch(t):
        return inc[t].dropna() if t in inc.columns else None
    return fetch


def test_download_prices_auto_completes_the_row_and_records_it(tmp_path, monkeypatch):
    inc, ng, idx = _frames()
    cache = tmp_path / "prices_cache_iums.parquet"
    monkeypatch.setattr(cb, "yf", _yf(inc))
    _stub_norgate(monkeypatch, inc, ng)
    monkeypatch.setattr(cb, "_single_ticker_closes", _unserving(inc))
    start, end = _span(idx)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache,
                             roster=ROSTER, price_source="auto")
    tail = idx[-1]
    assert out.index.max() == tail, "the 2026-09-22 row must survive"
    assert out.loc[tail, ROSTER].notna().all()
    assert out.attrs["tail_verification"] is None, "no tail left to settle"
    assert set(out.attrs["tail_from_norgate"]["filled"]) == set(KEPT)

    import price_source
    side = json.loads(price_source.sidecar_path(cache).read_text(encoding="utf-8"))
    assert sorted(side["columns_from_norgate"]) == sorted(NG_ROSTER)
    assert sorted(side["columns_kept_on_incumbent"]) == sorted(KEPT), \
        "the sidecar must record the kept columns the log reports"
    assert set(side["tail_from_norgate"]["filled"]) == set(KEPT)

    # A cache hit carries the record forward rather than erasing it.
    again = cb.download_prices(ROSTER, start, str(tail.date()), cache_path=cache,
                               roster=ROSTER, price_source="auto")
    assert again.attrs["tail_from_norgate"]["from_cache"] is True


def test_download_prices_yfinance_run_is_unchanged(tmp_path, monkeypatch):
    """A yfinance run never consults Norgate: the retraction still drops the
    row as a placeholder, exactly as on 2026-09-23."""
    inc, ng, idx = _frames()
    cache = tmp_path / "px.parquet"
    monkeypatch.setattr(cb, "yf", _yf(inc))
    import norgate_prices

    def must_not_call(*a, **k):
        raise AssertionError("a yfinance run must not reach Norgate")

    monkeypatch.setattr(norgate_prices, "fetch_closes", must_not_call)
    monkeypatch.setattr(cb, "_single_ticker_closes", _unserving(inc))
    start, end = _span(idx)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache,
                             roster=ROSTER, price_source="yfinance")
    assert out.index.max() == idx[-2]
    assert out.attrs["tail_verification"]["rows"][0]["verdict"] == "unserved_placeholder"
    assert out.attrs["tail_from_norgate"] is None


def test_download_prices_auto_declined_falls_through_to_the_placeholder_drop(
        tmp_path, monkeypatch):
    """Norgate disagreeing on every kept name leaves the pre-fix behaviour
    intact: the tail check drops the row. Fail closed, never a partial splice."""
    inc, ng, idx = _frames()
    cache = tmp_path / "px.parquet"
    monkeypatch.setattr(cb, "yf", _yf(inc))
    _stub_norgate(monkeypatch, inc, ng * 1.01)
    monkeypatch.setattr(cb, "_single_ticker_closes", _unserving(inc))
    start, end = _span(idx)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache,
                             roster=ROSTER, price_source="auto")
    assert out.index.max() == idx[-2]
    assert set(out.attrs["tail_from_norgate"]["declined"]) == set(KEPT)
    assert out.attrs["tail_verification"]["rows"][0]["verdict"] == "unserved_placeholder"
