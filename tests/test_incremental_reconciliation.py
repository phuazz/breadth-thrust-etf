"""Regression tests for the 2026-11-11 reconciliation failure.

Ten panels failed in ``incremental_prices.download`` on 2026-11-11. The replay
against captured vendor responses showed two separate things:

  * Yahoo's adjusted closes differ between two requests for the same history by
    float noise (largest observed 9.6e-07 relative, ratio 1.0, none in the most
    recent 25 sessions). The 1e-07 comparison read that as an adjustment change.
  * The cache deliberately keeps cells the vendor does not serve: an earlier
    occupant of a reused ticker (FB, LB, FOX), a bar on a venue closure the
    vendor later withdrew (five NSE holidays, one Taiwan closure). A column that
    "changed" and carries such a cell could never be covered by a full response.

The first is a defect and is fixed. The second is the guard working as written:
a real re-basing (smallest observed 6.9e-04) over cells the vendor cannot serve
still stops the panel, and still cannot renew audit evidence.

Offline. Python datetime months are 1-indexed (January = 1).
"""
import json
import sys
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import incremental_prices as ip  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

download = partial(ip.download, audit_required=False)
plan = partial(ip.plan, audit_required=False)
SIDE = {"source": "yfinance"}
START, END = "2025-11-03", "2026-02-05"
# The largest request-to-request difference measured on 2026-11-11 (IUSP).
OBSERVED_NOISE = 9.6e-7
# The smallest real adjustment measured the same day (SATS, a daily accrual).
OBSERVED_ADJUSTMENT = 6.9e-4


@pytest.fixture
def history():
    idx = get_calendar("NYSE").schedule(start_date=START, end_date="2026-02-04").index
    return pd.DataFrame({"A": np.arange(len(idx)) + 100., "B": np.arange(len(idx)) + 200.}, index=idx)


def jitter(frame, column, upto, scale=OBSERVED_NOISE):
    """Alternating-sign relative noise on ``column`` for rows before ``upto``.

    Deterministic, so the test does not depend on a seed, and confined to older
    rows, as measured: the most recent sessions compared equal bit for bit.
    """
    out = frame.copy()
    rows = out.index[:upto]
    signs = np.where(np.arange(len(rows)) % 2 == 0, 1.0, -1.0)
    out.loc[rows, column] = out.loc[rows, column] * (1.0 + signs * scale)
    return out


def fetcher(vendor, calls=None):
    def fetch(names, start, end):
        if calls is not None:
            calls.append((list(names), start, end))
        return vendor.loc[pd.Timestamp(start):pd.Timestamp(end) - pd.Timedelta(days=1), list(names)]
    return fetch


# ---------------------------------------------------------------- the defect

def test_tolerance_sits_between_measured_noise_and_measured_adjustment():
    assert OBSERVED_NOISE * 5 <= ip.RTOL <= OBSERVED_ADJUSTMENT / 5


def test_vendor_float_noise_is_not_an_adjustment_change(history):
    """ITWN/NDIA shape: every name jittered, one cached closure bar unserved."""
    prior = history.iloc[:-1].copy()
    vendor = jitter(history, "A", upto=len(history) - 25)
    vendor.iloc[30, 0] = np.nan            # the bar the vendor later withdrew
    out, _, rec = download(fetcher(vendor), list(history), START, END, prior, SIDE,
                           full_history=True)
    assert rec["reconciled_columns"] == []
    # The request helper returns what the vendor served and nothing else: the
    # withdrawn cell is NaN here, and it is the caller's cell-preservation
    # merge - unchanged - that keeps the cached value.
    assert np.isnan(out["A"].iloc[30])
    pd.testing.assert_series_equal(out["A"].drop(out.index[30]), vendor["A"].drop(vendor.index[30]),
                                   check_freq=False)


def test_noise_over_an_earlier_occupant_does_not_fail_the_panel(history):
    """FB/LB/FOX shape: the vendor serves only the ticker's current occupant."""
    prior = history.iloc[:-1].copy()
    vendor = jitter(history, "A", upto=len(history) - 25)
    vendor.iloc[:20, 0] = np.nan           # the earlier occupant: cache only
    out, _, rec = download(fetcher(vendor), list(history), START, END, prior, SIDE,
                           full_history=True)
    assert out["A"].iloc[:20].isna().all()
    assert rec["reconciled_columns"] == []


def test_noise_in_the_overlap_does_not_request_full_history(history):
    prior = history.iloc[:-1].copy()
    vendor = jitter(history, "A", upto=len(history))
    calls = []
    _, _, rec = download(fetcher(vendor, calls), list(history), START, END, prior, SIDE)
    assert len(calls) == 1 and rec["full_columns"] == 0 and rec["reconciled_columns"] == []


def test_noise_cannot_certify_history_the_vendor_did_not_serve(history):
    """A partial response is never recorded as a completed full request."""
    prior = history.iloc[:-1].copy()
    vendor = jitter(history, "A", upto=len(history) - 25)
    vendor.iloc[:20, 0] = np.nan
    _, _, rec = download(fetcher(vendor), list(history), START, END, prior, SIDE,
                         full_history=True)
    assert "A" not in rec["full_answered"] and "B" in rec["full_answered"]
    scan = ip.carry_audits(prior, None, rec, SIDE)
    assert "A" not in scan and "B" in scan


def test_download_prices_keeps_preserved_cells_through_vendor_noise(history, tmp_path, monkeypatch):
    """End to end through compute_breadth: the 2026-11-11 run, minus the failure."""
    import compute_breadth as cb
    import price_source
    cache = tmp_path / "data" / "prices_cache.parquet"
    cache.parent.mkdir()
    history["C"] = np.arange(len(history)) + 300.   # served whole: the control
    prior = history.iloc[:-1].copy()
    prior.to_parquet(cache)
    price_source.write_cache_source(cache, "yfinance")
    vendor = jitter(history, "A", upto=len(history) - 25)
    vendor.iloc[:20, 0] = np.nan           # earlier occupant, cache only
    vendor.iloc[30, 1] = np.nan            # withdrawn closure bar, cache only

    def yahoo(names, start, end, **kwargs):
        raw = vendor.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
        return pd.concat({"Close": raw}, axis=1)
    monkeypatch.setattr(cb.yf, "download", yahoo)
    monkeypatch.setattr(cb, "run_with_deadline", lambda fn, **kw: fn())
    monkeypatch.setattr(cb, "_revert_vendor_step_defects", lambda close, prior, tickers: (close, []))
    monkeypatch.setattr(cb.price_revisions, "record_vendor_observation", lambda raw, **kw: None)
    out = cb.download_prices(list(history), START, END, cache_path=cache, refresh=True,
                             tail_probe=False, required_through="2026-02-04")
    written = pd.read_parquet(cache)
    # Every cached cell the vendor did not serve is still there, unchanged...
    pd.testing.assert_series_equal(written["A"].iloc[:20], prior["A"].iloc[:20], check_freq=False)
    assert written["B"].iloc[30] == prior["B"].iloc[30]
    # ...every cell the vendor did serve is the vendor's, and the new session
    # arrived. Nothing was filled that neither side held.
    pd.testing.assert_series_equal(written["A"].iloc[20:], vendor["A"].iloc[20:], check_freq=False)
    assert written.index[-1] == history.index[-1]
    assert int(written.isna().sum().sum()) == 0 and int(out.isna().sum().sum()) == 0
    sidecar = json.loads(price_source.sidecar_path(cache).read_text(encoding="utf-8"))
    # Only the column the vendor covered whole is certified as audited; the
    # two with unserved cells are asked for full history again next run.
    assert sorted(sidecar["historical_scan"]) == ["C"]


# ------------------------------------------------- the guard, still in force

@pytest.mark.parametrize("factor", [1 - OBSERVED_ADJUSTMENT, 1 - 1.7e-3, 0.998, 0.5])
def test_real_rebasing_over_unserved_cells_still_stops_the_panel(history, factor):
    """SATS (reassigned symbol) and the three NDIA dividend payers of 2026-11-11."""
    prior = history.iloc[:-1].copy()
    vendor = history.copy()
    vendor.loc[vendor.index[:-1], "A"] *= factor
    vendor.iloc[:20, 0] = np.nan
    with pytest.raises(RuntimeError, match="reconciliation incomplete"):
        download(fetcher(vendor), list(history), START, END, prior, SIDE, full_history=True)


def test_one_revised_close_over_truncated_history_still_stops_the_panel(history):
    """MGPI shape: seven bars served after a rename, one of them restated."""
    prior = history.iloc[:-1].copy()
    vendor = history.copy()
    vendor.iloc[:-7, 0] = np.nan
    vendor.iloc[-2, 0] *= 1 + 1.08e-3
    with pytest.raises(RuntimeError, match="reconciliation incomplete"):
        download(fetcher(vendor), list(history), START, END, prior, SIDE)


def test_refusal_names_what_the_vendor_left_uncovered(history, capsys):
    prior = history.iloc[:-1].copy()
    vendor = history.copy()
    vendor.loc[vendor.index[:-1], "A"] *= 1 - OBSERVED_ADJUSTMENT
    vendor.iloc[:20, 0] = np.nan
    with pytest.raises(RuntimeError, match="reconciliation incomplete"):
        download(fetcher(vendor), list(history), START, END, prior, SIDE, full_history=True)
    line = next(l for l in capsys.readouterr().out.splitlines() if "Reconciliation incomplete A" in l)
    assert "20 cached cell(s) not served" in line
    assert str(history.index[0].date()) in line and str(history.index[19].date()) in line
    assert "before the vendor's first bar" in line


def test_describe_unserved_separates_leading_from_interior_cells(history):
    prior = history["A"].iloc[:-1]
    vendor = history["A"].copy() * 0.998
    vendor.iloc[:10] = np.nan
    vendor.iloc[30] = np.nan
    d = ip.describe_unserved(prior, vendor)
    assert d["unserved_cells"] == 11 and d["unserved_before_vendor_start"] == 10
    assert d["vendor_first"] == str(history.index[10].date())
    assert d["changed_cells"] == d["shared_cells"] == len(prior) - 11
    assert d["max_rel_change"] == pytest.approx(0.002, rel=1e-6)


# ----------------------------------- withdrawn closes, closures, trading days

def test_withdrawn_latest_close_is_neither_dropped_nor_invented(history):
    """The request helper passes on exactly what the vendor served."""
    prior = history.iloc[:-1].copy()
    vendor = history.copy()
    vendor.iloc[-2, 0] = np.nan            # yesterday's close for A, withdrawn
    seen = []
    out, _, _ = download(fetcher(vendor), list(history), START, END, prior, SIDE,
                         observe=seen.append)
    assert np.isnan(out["A"].iloc[-2])     # not carried forward from the day before
    assert np.isnan(seen[0]["A"].iloc[-2])  # the raw observation says withdrawn
    assert out["B"].iloc[-2] == history["B"].iloc[-2]


def test_missing_newest_session_is_left_missing(history):
    """A vendor that has not published the newest session yields no row for it."""
    prior = history.iloc[:-1].copy()
    out, _, _ = download(fetcher(history.iloc[:-1]), list(history), START, END, prior, SIDE)
    assert history.index[-1] not in out.index


def test_venue_holiday_is_not_a_missing_trading_day():
    """24 and 26 December 2025: NYSE traded, Xetra did not (month and year end)."""
    idx = get_calendar("XETR").schedule(start_date="2025-11-03", end_date="2026-02-27").index
    assert pd.Timestamp("2025-12-24") not in idx and pd.Timestamp("2025-12-31") not in idx
    assert pd.Timestamp("2026-01-02") in idx                      # year boundary
    assert pd.Timestamp("2026-01-30") in idx and pd.Timestamp("2026-02-02") in idx  # month boundary
    cache = pd.DataFrame({"A": np.arange(len(idx)) + 50.}, index=idx)
    # On its own venue calendar the cache is complete: only the overlap is asked.
    since, full = plan(cache, ["A"], "2025-11-03", "2026-02-28", SIDE, calendar="XETR")
    assert not full and since == idx[-ip.OVERLAP_BARS]
    # Judged on NYSE's calendar the same cache is missing real sessions, and
    # the request reaches back to the first of them instead of assuming a
    # holiday. Nothing is filled either way: plan() only widens the request.
    nyse = get_calendar("NYSE").schedule(start_date="2025-11-03", end_date="2026-02-27").index
    first_missing = nyse.difference(idx)[0]
    assert first_missing == pd.Timestamp("2025-12-24") < idx[-ip.OVERLAP_BARS]
    since_nyse, _ = plan(cache, ["A"], "2025-11-03", "2026-02-28", SIDE, calendar="NYSE")
    assert since_nyse == first_missing
