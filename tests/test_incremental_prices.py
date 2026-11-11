"""Offline incremental request safety tests. Python months are 1-indexed."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import incremental_prices as ip
from functools import partial
plan_without_audit = partial(ip.plan, audit_required=False)
download_without_audit = partial(ip.download, audit_required=False)
from venue_calendars import get_calendar


@pytest.fixture
def history():
    idx = get_calendar("NYSE").schedule(start_date="2025-11-03", end_date="2026-02-04").index
    return pd.DataFrame({"A": np.arange(len(idx)) + 100., "B": np.arange(len(idx)) + 200.}, index=idx)


SIDE = {"source": "yfinance"}


def assert_held(out, rec, prior, column, appended=()):
    """The column is the cached one cell for cell; nothing re-based came in."""
    kept = prior[column].dropna()
    got = out[column].dropna()
    pd.testing.assert_series_equal(got.loc[kept.index], kept, check_freq=False)
    assert list(got.index.difference(kept.index)) == list(appended)
    assert column in rec["held_columns"]
    assert column not in rec["full_answered"] and column not in rec["reconciled_columns"]


def run(history, prior=None, sidecar=SIDE):
    calls = []
    def fetch(names, start, end):
        calls.append((list(names), start, end))
        return history.loc[pd.Timestamp(start):pd.Timestamp(end) - pd.Timedelta(days=1), names]
    out, since, rec = download_without_audit(fetch, list(history), str(history.index[0].date()),
        "2026-02-05", history.iloc[:-1] if prior is None else prior, sidecar)
    return out, since, rec, calls


def test_unchanged_requests_only_overlap(history):
    out, since, rec, calls = run(history)
    assert len(calls) == 1
    assert since == history.index[-21]
    pd.testing.assert_frame_equal(out.combine_first(history.iloc[:-1]), history, check_freq=False)
    assert rec["full_columns"] == 0


def test_new_symbol_gets_full_history_only_for_it(history):
    _, _, rec, calls = run(history, history.iloc[:-1][["A"]])
    assert calls[0][0] == ["A"] and calls[1][0] == ["B"]
    assert calls[1][1] == "2025-11-03"


@pytest.mark.parametrize("factor", [0.5, 0.998])
def test_split_or_dividend_reconciles_whole_column(history, factor):
    prior = history.iloc[:-1].copy()
    changed = history.copy()
    changed.loc[changed.index[:-1], "A"] *= factor
    out, _, rec, calls = run(changed, prior)
    assert rec["reconciled_columns"] == ["A"]
    assert calls[-1][0] == ["A"]
    pd.testing.assert_series_equal(out["A"], changed["A"], check_freq=False)


def test_incomplete_reconciliation_holds_the_column_before_splice(history):
    prior = history.iloc[:-1].copy()
    history["A"] *= .5
    history.iloc[0, 0] = np.nan
    out, _, rec, _ = run(history, prior)
    assert_held(out, rec, prior, "A")
    # The panel's other column is not held back with it.
    pd.testing.assert_series_equal(out["B"].dropna().iloc[-21:], history["B"].iloc[-21:], check_freq=False)
    assert "B" not in rec["held_columns"]


def test_interior_cell_gap_only_retries_affected_symbol(history):
    prior = history.iloc[:-1].copy()
    prior.iloc[4, 0] = np.nan
    _, _, _, calls = run(history, prior)
    assert calls[-1][0] == ["A"]
    assert calls[-1][1] == "2025-11-03"


def test_entire_missing_row_is_requested(history):
    prior = history.iloc[:-1].drop(history.index[5])
    _, since, _, calls = run(history, prior)
    assert since == history.index[5]
    assert calls[0][1] == str(history.index[5].date())


@pytest.mark.parametrize("sidecar", [None, {"source": "norgate"},
    {"source": "yfinance", "column_basis": {"A": "custom", "B": "custom"}}])
def test_uncertain_or_different_basis_gets_full_history(history, sidecar):
    _, _, rec, calls = run(history, sidecar=sidecar)
    assert rec["full_columns"] == 2
    assert calls[0][1] == "2025-11-03"


def test_open_withdrawal_keeps_old_observation_in_scope(history, tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"cells": {"2025-11-10|A": {"status": "withheld"}}}))
    since, _ = plan_without_audit(history, list(history), "2025-11-03", "2026-02-05", SIDE, path)
    assert since == pd.Timestamp("2025-11-10")


@pytest.mark.parametrize("start", ["2025-11-01", "2026-01-01"])
def test_closed_start_and_month_year_boundaries(history, start):
    prior = history.loc[start:].iloc[:-1]
    since, full = plan_without_audit(prior, list(prior), start, "2026-02-05", SIDE)
    assert not full
    assert since >= pd.Timestamp(start)


def test_invalid_cache_and_force_equivalent_request_full(history):
    _, full = plan_without_audit(None, list(history), "2025-11-03", "2026-02-05", SIDE)
    assert full == list(history)
    _, full = plan_without_audit(history.iloc[::-1], list(history), "2025-11-03", "2026-02-05", SIDE)
    assert full == list(history)


def test_mixed_source_column_is_not_spliced(history):
    side = {"source": "auto", "tail_from_norgate": {"filled": {"A": {"dates": ["2026-02-03"]}}}}
    _, full = plan_without_audit(history, list(history), "2025-11-03", "2026-02-05", side)
    assert full == ["A"]


def test_initially_full_holed_column_cannot_mix_adjustment_bases(history):
    prior = history.iloc[:-1].copy()
    prior.iloc[5, 0] = np.nan
    history["A"] *= .998
    history.iloc[0, 0] = np.nan
    out, _, rec, _ = run(history, prior)
    assert_held(out, rec, prior, "A")


def test_download_prices_retains_history_and_raw_observation_scope(history, tmp_path, monkeypatch):
    import compute_breadth as cb
    import price_source
    cache = tmp_path / "data" / "prices_cache.parquet"
    cache.parent.mkdir()
    prior = history.iloc[:-1]
    prior.to_parquet(cache)
    scan = ip.carry_audits(prior, None,
        {"full_requested": list(prior), "full_answered": list(prior), "incremental_requested": [], "full_start": "2025-11-03"}, SIDE)
    price_source.write_cache_source(cache, "yfinance", {"historical_scan": scan})
    calls, observed = [], []
    def vendor(names, start, end, **kwargs):
        calls.append(start)
        raw = history.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
        return pd.concat({"Close": raw}, axis=1)
    monkeypatch.setattr(cb.yf, "download", vendor)
    monkeypatch.setattr(cb, "run_with_deadline", lambda fn, **kw: fn())
    monkeypatch.setattr(cb, "_revert_vendor_step_defects", lambda close, prior, tickers: (close, []))
    monkeypatch.setattr(cb.price_revisions, "record_vendor_observation", lambda raw, **kw: observed.append(raw.copy()))
    out = cb.download_prices(list(history), "2025-11-03", "2026-02-05", cache_path=cache,
                             refresh=True, tail_probe=False, required_through="2026-02-04")
    pd.testing.assert_frame_equal(out, history, check_freq=False)
    assert len(calls) == 1 and calls[0] > "2025-11-03"
    assert observed[0].index.min() == pd.Timestamp(calls[0])
    pd.testing.assert_frame_equal(pd.read_parquet(cache), history, check_freq=False)


def test_held_reconciliation_writes_no_rebased_cell_to_the_cache(history, tmp_path, monkeypatch):
    import compute_breadth as cb
    import price_source
    cache = tmp_path / "data" / "prices_cache.parquet"
    cache.parent.mkdir()
    prior = history.iloc[:-1]
    prior.to_parquet(cache)
    price_source.write_cache_source(cache, "yfinance")
    changed = history.copy()
    changed["A"] *= .998                   # A re-based, and one cached cell unserved
    changed.iloc[0, 0] = np.nan
    def vendor(names, start, end, **kwargs):
        raw = changed.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
        return pd.concat({"Close": raw}, axis=1)
    monkeypatch.setattr(cb.yf, "download", vendor)
    monkeypatch.setattr(cb, "run_with_deadline", lambda fn, **kw: fn())
    monkeypatch.setattr(cb.price_revisions, "record_vendor_observation", lambda raw, **kw: None)
    cb.download_prices(list(history), "2025-11-03", "2026-02-05", cache_path=cache,
                       refresh=True, tail_probe=False)
    written = pd.read_parquet(cache)
    # A: every cached cell exactly as it was, and no cell on the other basis.
    pd.testing.assert_series_equal(written["A"].dropna(), prior["A"], check_freq=False)
    # B: refreshed as usual, new session included.
    pd.testing.assert_series_equal(written["B"], history["B"], check_freq=False)
    sidecar = json.loads(price_source.sidecar_path(cache).read_text(encoding="utf-8"))
    held = sidecar["reconciliation_held"]["A"]
    assert held["unserved_cells"] == 1 and held["seam"]["matched"] is False
    assert "A" not in sidecar.get("historical_scan", {})


def test_leading_unpriced_prefix_is_retried(history):
    prior = history.iloc[:-1].copy()
    prior.iloc[:4, 0] = np.nan
    _, full = plan_without_audit(prior, list(history), "2025-11-03", "2026-02-05", SIDE)
    assert full == ["A"]


def test_reconciliation_fetches_preserved_history_before_requested_start(history):
    prior = history.iloc[:-1].copy()
    changed = history * .998
    calls = []
    def fetch(names, start, end):
        calls.append(start)
        return changed.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
    out, _, _ = download_without_audit(fetch, list(history), "2025-12-01", "2026-02-05", prior, SIDE)
    assert calls[-1] == "2025-11-03"
    pd.testing.assert_frame_equal(out, changed, check_freq=False)


def test_held_reconciliation_still_records_raw_vendor_observation(history):
    prior = history.iloc[:-1].copy()
    changed = history * .998
    changed.iloc[0, 0] = np.nan
    seen = []
    def fetch(names, start, end):
        return changed.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
    out, _, rec = download_without_audit(fetch, list(history), "2025-11-03", "2026-02-05", prior, SIDE,
                                         observe=seen.append)
    assert_held(out, rec, prior, "A")
    assert len(seen) == 1
    assert seen[0].index.min() == history.index[-21]
    # The observation is the vendor's answer, not the held column.
    pd.testing.assert_series_equal(seen[0]["A"], changed["A"].loc[seen[0].index], check_freq=False)


def test_changed_prefix_cannot_meet_preserved_unfetched_suffix(history):
    changed = history * .998
    def fetch(names, start, end):
        return changed.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
    out, _, rec = download_without_audit(fetch, list(history), "2025-11-03", "2026-02-03", history, SIDE)
    for column in history:
        assert_held(out, rec, history, column)


def test_full_history_audit_keeps_reconciliation_hold(history):
    changed = history * .998
    changed.iloc[0, 0] = np.nan
    def fetch(names, start, end):
        assert start == "2025-11-03"
        return changed.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
    out, _, rec = download_without_audit(fetch, list(history), "2025-11-03", "2026-02-05", history, SIDE,
                                         full_history=True)
    assert_held(out, rec, history, "A")
    assert rec["full_answered"] == ["B"]


from datetime import datetime, timezone, timedelta
NOW = datetime(2026, 2, 5, tzinfo=timezone.utc)


def audited_side(history, now=NOW):
    request = {"full_requested": list(history), "full_answered": list(history),
               "incremental_requested": [], "full_start": "2025-11-03"}
    return {**SIDE, "historical_scan": ip.carry_audits(history, None, request, SIDE, now)}


def test_first_run_bootstraps_full_audit(history):
    _, full = ip.plan(history, list(history), "2025-11-03", "2026-02-05", SIDE, now=NOW)
    assert full == list(history)


def test_recent_audit_allows_sparse_history_without_repeated_full_fetch(history):
    history.loc[history.index[:4], "A"] = np.nan
    history.loc[history.index[10], "B"] = np.nan
    history = history.drop(history.index[15])
    since, full = ip.plan(history, list(history), "2025-11-03", "2026-02-05",
                          audited_side(history), now=NOW + timedelta(days=1))
    assert not full
    assert since == history.index[-20]


@pytest.mark.parametrize("offset", [timedelta(days=7), timedelta(days=-1)])
def test_expired_or_future_audit_forces_full(history, offset):
    _, full = ip.plan(history, list(history), "2025-11-03", "2026-02-05",
                      audited_side(history), now=NOW + offset)
    assert full == list(history)


def test_changed_cache_invalidates_audit_for_affected_name(history):
    side = audited_side(history)
    history.iloc[2, 0] += 1
    _, full = ip.plan(history, list(history), "2025-11-03", "2026-02-05", side, now=NOW)
    assert full == ["A"]


def test_weekly_audit_detects_historical_only_correction(history):
    side = audited_side(history)
    changed = history.copy()
    changed.iloc[2, 0] += 1
    calls = []
    def fetch(names, start, end):
        calls.append(start)
        return changed.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
    out, _, rec = ip.download(fetch, list(history), "2025-11-03", "2026-02-05", history,
                             side, now=NOW + timedelta(days=7))
    pd.testing.assert_frame_equal(out, changed, check_freq=False)
    assert calls == ["2025-11-03"]
    assert rec["full_columns"] == len(history.columns)


def test_incremental_write_does_not_reset_audit_clock(history):
    prior = history.iloc[:-1]
    side = audited_side(prior)
    rec = {"full_requested": [], "full_answered": [], "incremental_requested": list(history),
           "full_start": "2025-11-03"}
    scans = ip.carry_audits(history, side, rec, SIDE, NOW + timedelta(days=1))
    assert scans["A"]["scanned_at_utc"] == side["historical_scan"]["A"]["scanned_at_utc"]
    assert scans["A"]["fingerprint"] == ip.fingerprint(history["A"])


def test_unanswered_full_request_cannot_certify_audit(history):
    rec = {"full_requested": list(history), "full_answered": [], "incremental_requested": [],
           "full_start": "2025-11-03"}
    assert ip.carry_audits(history, None, rec, SIDE, NOW) == {}


def test_full_audit_matches_full_refresh_even_with_old_only_adjustment(history):
    side = audited_side(history)
    changed = history.copy()
    changed.loc[changed.index[:10], "A"] *= .998
    def fetch(names, start, end):
        return changed.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
    periodic, _, _ = ip.download(fetch, list(history), "2025-11-03", "2026-02-05", history,
                                side, now=NOW + timedelta(days=7))
    explicit, _, _ = ip.download(fetch, list(history), "2025-11-03", "2026-02-05", history,
                                side, full_history=True, now=NOW)
    pd.testing.assert_frame_equal(periodic, explicit)


def test_truncated_full_response_cannot_renew_audit(history):
    def fetch(names, start, end):
        return history.iloc[-20:][names]
    _, _, rec = ip.download(fetch, list(history), "2025-11-03", "2026-02-05", history, SIDE, now=NOW)
    assert rec["full_answered"] == []
    assert ip.carry_audits(history, None, rec, SIDE, NOW) == {}


def test_historical_end_cannot_certify_unrequested_suffix(history):
    def fetch(names, start, end):
        return history.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]
    _, _, rec = ip.download(fetch, list(history), "2025-11-03", "2026-02-03", history, SIDE, now=NOW)
    assert rec["full_answered"] == []
    assert ip.carry_audits(history, None, rec, SIDE, NOW) == {}


def test_production_bootstrap_incremental_and_weekly_historical_correction(history, tmp_path, monkeypatch):
    import compute_breadth as cb
    import price_source
    cache = tmp_path / "data" / "prices_cache.parquet"
    cache.parent.mkdir()
    history.loc[history.index[:3], "A"] = np.nan
    history.loc[history.index[5], "B"] = np.nan
    history.to_parquet(cache)
    price_source.write_cache_source(cache, "yfinance")
    calls = []
    def vendor(names, start, end, **kwargs):
        calls.append(start)
        return pd.concat({"Close": history.loc[start:pd.Timestamp(end) - pd.Timedelta(days=1), names]}, axis=1)
    monkeypatch.setattr(cb.yf, "download", vendor)
    monkeypatch.setattr(cb, "run_with_deadline", lambda fn, **kw: fn())
    monkeypatch.setattr(cb, "_revert_vendor_step_defects", lambda close, prior, tickers: (close, []))
    def refresh():
        return cb.download_prices(list(history), "2025-11-03", "2026-02-05", cache_path=cache,
                                  refresh=True, tail_probe=False, required_through="2026-02-04")
    pd.testing.assert_frame_equal(refresh(), history, check_freq=False)
    assert calls == ["2025-11-03"]
    pd.testing.assert_frame_equal(refresh(), history, check_freq=False)
    assert calls[-1] > "2025-11-03"
    # Old-only correction does not alter the overlap. Expired audit catches it.
    history.iloc[8, 0] *= .998
    side = price_source.read_cache_sidecar(cache)
    expired = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    for item in side["historical_scan"].values():
        item["scanned_at_utc"] = expired
    price_source.sidecar_path(cache).write_text(json.dumps(side))
    pd.testing.assert_frame_equal(refresh(), history, check_freq=False)
    assert calls[-1] == "2025-11-03"
