"""The BTC fallback proves a completed day and retains earlier repairs.

Python and pandas months are 1-indexed. The dated cases cross a month and
a year boundary using the NYSE calendar rather than calendar-day arithmetic.
"""

import json

import pandas as pd
import pytest

from scripts.price_panel_guard import venue_sessions_through
from scripts.repair_price_gaps import (
    RepairError, recover_completed_btc_tail, restore_recorded_btc_gaps,
    verify_auto_btc_cache, record_auto_btc_repair,
)


def _tail(required):
    required = pd.Timestamp(required)
    sessions = pd.DatetimeIndex(venue_sessions_through("NYSE", required, 30))
    alternate = pd.Series({day: 200.0 + 2 * i
                           for i, day in enumerate(sessions[-7:])})
    btc = pd.Series({day: 100.0 + i for i, day in enumerate(sessions)},
                    dtype=float)
    for day in sessions[-7:-1]:
        btc.at[day] = alternate.at[day] * 0.99
    btc.at[required] = float("nan")
    frame = pd.DataFrame({"BTC-USD": btc,
                          "OTHER": pd.Series(1.0, index=sessions)})
    return frame, alternate


@pytest.mark.parametrize("required", ["2026-11-02", "2027-01-04"])
def test_auto_btc_fills_only_completed_current_session_at_calendar_edges(required):
    frame, alternate = _tail(required)
    out, rec = recover_completed_btc_tail(
        frame, required_session=required, completed_through=required,
        fetch=lambda symbol, start, end: alternate)
    day = pd.Timestamp(required)
    assert out.at[day, "BTC-USD"] == pytest.approx(alternate.at[day] * 0.99)
    assert rec["date"] == required
    assert rec["overlap_returns"] >= 3
    assert pd.isna(frame.at[day, "BTC-USD"])


def test_unfinished_utc_day_cannot_be_filled():
    frame, alternate = _tail("2026-11-02")
    called = []
    out, rec = recover_completed_btc_tail(
        frame, required_session="2026-11-02",
        completed_through="2026-11-01",
        fetch=lambda *args: called.append(args))
    assert rec is None and not called
    assert pd.isna(out.at[pd.Timestamp("2026-11-02"), "BTC-USD"])


def test_auto_btc_carries_registered_fee_across_weekend():
    frame, alternate = _tail("2027-01-04")
    out, rec = recover_completed_btc_tail(
        frame, required_session="2027-01-04",
        completed_through="2027-01-04", annual_fee_bps=25,
        fetch=lambda symbol, start, end: alternate)
    prior = pd.Timestamp(rec["prev_date"])
    day = pd.Timestamp(rec["date"])
    expected = (frame.at[prior, "BTC-USD"]
                * alternate.at[day] / alternate.at[prior]
                * (1 - 25 / 10_000) ** ((day - prior).days / 365))
    assert out.at[day, "BTC-USD"] == pytest.approx(expected)
    assert rec["implied_return"] == pytest.approx(
        expected / frame.at[prior, "BTC-USD"] - 1)
    assert rec["secondary_return"] == pytest.approx(
        alternate.at[day] / alternate.at[prior] - 1)


@pytest.mark.parametrize("fault", ["two_gaps", "no_secondary", "source_error",
                                    "basis_drift", "implausible_move",
                                    "zero_overlap", "infinite_overlap",
                                    "zero_previous"])
def test_auto_btc_refuses_unproved_tail(fault):
    frame, alternate = _tail("2026-11-02")
    if fault == "two_gaps":
        frame.at[pd.Timestamp("2026-10-30"), "BTC-USD"] = float("nan")
    elif fault == "no_secondary":
        alternate = alternate.drop(pd.Timestamp("2026-11-02"))
    elif fault == "basis_drift":
        alternate.at[pd.Timestamp("2026-10-30")] *= 1.1
    elif fault == "implausible_move":
        alternate.at[pd.Timestamp("2026-11-02")] *= 2
    elif fault == "zero_overlap":
        alternate.at[pd.Timestamp("2026-10-29")] = 0.0
    elif fault == "infinite_overlap":
        alternate.at[pd.Timestamp("2026-10-29")] = float("inf")
    elif fault == "zero_previous":
        frame.at[pd.Timestamp("2026-10-30"), "BTC-USD"] = 0.0

    def fetch(*_):
        if fault == "source_error":
            raise OSError("offline")
        return alternate

    with pytest.raises(RepairError):
        recover_completed_btc_tail(
            frame, required_session="2026-11-02",
            completed_through="2026-11-02", fetch=fetch)


def _recorded(tmp_path):
    previous, day = pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")
    cached = pd.DataFrame({"BTC-USD": [100.0, 105.0]},
                          index=[previous, day])
    fresh = cached.copy()
    fresh.at[day, "BTC-USD"] = float("nan")
    record = {"cache": "thematic", "ticker": "BTC-USD",
              "date": "2026-10-16", "prev_date": "2026-10-15",
              "prev_value": 100.0, "value": 105.0,
              "implied_return": 0.05, "source": "binance:BTCUSDT",
              "method": "return_splice"}
    ledger = tmp_path / "repairs.jsonl"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return fresh, cached, ledger


def test_recorded_btc_repair_survives_later_primary_omission(tmp_path):
    fresh, cached, ledger = _recorded(tmp_path)
    out, retained = restore_recorded_btc_gaps(
        fresh, cached, ledger=ledger, completed_through="2026-10-23",
        cache_proved_through="2026-10-16", decision_session="2026-10-16",
        same_source_and_basis=True)
    assert out.at[pd.Timestamp("2026-10-16"), "BTC-USD"] == 105.0
    assert len(retained) == 1
    assert pd.isna(fresh.at[pd.Timestamp("2026-10-16"), "BTC-USD"])


@pytest.mark.parametrize("fault", ["changed_prior", "changed_cache",
                                    "missing_proof", "backfill_conflict"])
def test_recorded_btc_repair_cannot_silently_restate(tmp_path, fault):
    fresh, cached, ledger = _recorded(tmp_path)
    proof = "2026-10-16"
    bound = "2026-10-23"
    if fault == "changed_prior":
        fresh.at[pd.Timestamp("2026-10-15"), "BTC-USD"] = 99.0
    elif fault == "changed_cache":
        cached.at[pd.Timestamp("2026-10-16"), "BTC-USD"] = 106.0
    elif fault == "missing_proof":
        proof = None
    elif fault == "backfill_conflict":
        fresh.at[pd.Timestamp("2026-10-16"), "BTC-USD"] = 102.0
    with pytest.raises(RepairError):
        restore_recorded_btc_gaps(
            fresh, cached, ledger=ledger, completed_through=bound,
            cache_proved_through=proof, decision_session="2026-10-16",
            same_source_and_basis=True)


def test_future_ledger_day_cannot_prove_an_unfinished_session(tmp_path):
    fresh, cached, ledger = _recorded(tmp_path)
    out, retained = restore_recorded_btc_gaps(
        fresh, cached, ledger=ledger, completed_through="2026-10-15",
        cache_proved_through="2026-10-16", decision_session="2026-10-16",
        same_source_and_basis=True)
    assert retained == []
    assert pd.isna(out.at[pd.Timestamp("2026-10-16"), "BTC-USD"])


def test_small_primary_backfill_does_not_restate_published_btc(tmp_path):
    fresh, cached, ledger = _recorded(tmp_path)
    fresh.at[pd.Timestamp("2026-10-16"), "BTC-USD"] = 105.25
    out, retained = restore_recorded_btc_gaps(
        fresh, cached, ledger=ledger, completed_through="2026-10-23",
        cache_proved_through="2026-10-16", decision_session="2026-10-16",
        same_source_and_basis=True)
    assert out.at[pd.Timestamp("2026-10-16"), "BTC-USD"] == 105.0
    assert len(retained) == 1


def test_older_ledger_value_superseded_by_published_cache_is_preserved(tmp_path):
    fresh, cached, ledger = _recorded(tmp_path)
    cached.at[pd.Timestamp("2026-10-16"), "BTC-USD"] = 105.25
    out, retained = restore_recorded_btc_gaps(
        fresh, cached, ledger=ledger, completed_through="2026-10-23",
        cache_proved_through="2026-10-23", decision_session="2026-10-23",
        same_source_and_basis=True)
    assert out.at[pd.Timestamp("2026-10-16"), "BTC-USD"] == 105.25
    assert retained[0]["source"] == "cache:completed"
    assert retained[0]["method"] == "preserve_published_history"
    record = json.loads(ledger.read_text(encoding="utf-8"))
    record["applied_at_utc"] = "2026-10-17T04:03:54+00:00"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    verify_auto_btc_cache(cached, {"retained_gap_repairs": retained}, ledger)
    cached.at[pd.Timestamp("2026-10-16"), "BTC-USD"] = 105.5
    with pytest.raises(RepairError):
        verify_auto_btc_cache(cached, {"retained_gap_repairs": retained}, ledger)


def test_interrupted_auto_repair_cannot_be_reused_without_ledger(tmp_path):
    _, cached, ledger = _recorded(tmp_path)
    rec = json.loads(ledger.read_text(encoding="utf-8"))
    sidecar = {"auto_btc_repair": rec}
    with pytest.raises(RepairError):
        verify_auto_btc_cache(cached, sidecar, ledger)
    rec["applied_at_utc"] = "2026-10-17T04:03:54+00:00"
    ledger.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    verify_auto_btc_cache(cached, sidecar, ledger)
    cached.at[pd.Timestamp("2026-10-16"), "BTC-USD"] = 106.0
    with pytest.raises(RepairError):
        verify_auto_btc_cache(cached, sidecar, ledger)


def test_existing_applied_btc_repair_is_not_duplicated(tmp_path):
    _, _, ledger = _recorded(tmp_path)
    record = json.loads(ledger.read_text(encoding="utf-8"))
    record["applied_at_utc"] = "2026-10-17T04:03:54+00:00"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    before = ledger.read_bytes()
    assert record_auto_btc_repair(record, ledger, check_only=True) is False
    assert record_auto_btc_repair(record, ledger) is False
    assert ledger.read_bytes() == before
    with pytest.raises(RepairError):
        record_auto_btc_repair({**record, "value": 106.0}, ledger,
                               check_only=True)
    with pytest.raises(RepairError):
        record_auto_btc_repair({**record, "value": 106.0}, ledger)
    assert ledger.read_bytes() == before


def test_new_btc_repair_record_is_durable_once(tmp_path):
    _, _, ledger = _recorded(tmp_path)
    record = json.loads(ledger.read_text(encoding="utf-8"))
    ledger.unlink()
    assert record_auto_btc_repair(record, ledger) is True
    written = json.loads(ledger.read_text(encoding="utf-8"))
    assert written["value"] == record["value"]
    assert written["applied_at_utc"]
    assert record_auto_btc_repair(record, ledger) is False


def test_different_declared_basis_never_replays_a_btc_spot_repair(tmp_path):
    fresh, cached, ledger = _recorded(tmp_path)
    with pytest.raises(RepairError):
        restore_recorded_btc_gaps(
            fresh, cached, ledger=ledger, completed_through="2026-10-23",
            cache_proved_through="2026-10-16", decision_session="2026-10-23",
            same_source_and_basis=False)
def test_current_gap_can_be_fetched_again_without_ignored_cache(tmp_path):
    fresh, _, ledger = _recorded(tmp_path)
    out, retained = restore_recorded_btc_gaps(
        fresh, None, ledger=ledger, completed_through="2026-10-16",
        cache_proved_through=None, decision_session="2026-10-16",
        same_source_and_basis=False)
    assert retained == []
    assert pd.isna(out.at[pd.Timestamp("2026-10-16"), "BTC-USD"])


def test_historical_repair_without_cache_cannot_be_rebased_by_new_vendor(tmp_path):
    fresh, _, ledger = _recorded(tmp_path)
    fresh.at[pd.Timestamp("2026-10-16"), "BTC-USD"] = 105.25
    with pytest.raises(RepairError, match="published history"):
        restore_recorded_btc_gaps(
            fresh, None, ledger=ledger, completed_through="2026-10-23",
            cache_proved_through=None, decision_session="2026-10-23",
            same_source_and_basis=False)
