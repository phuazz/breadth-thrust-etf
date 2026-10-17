"""A repair source appears only beside the same ready C decision."""

import json
from datetime import datetime

import pytest

from scripts.pipeline import _btc_repair_for_live_decision


@pytest.mark.parametrize("status,decision,required,expected", [
    ("READY", "2026-10-16", "2026-10-16", True),
    ("HOLD", "2026-10-16", "2026-10-16", False),
    ("READY", "2026-10-15", "2026-10-16", False),
    ("READY", "2026-10-23", "2026-10-23", False),
])
def test_health_provenance_requires_same_ready_decision(
        tmp_path, status, decision, required, expected):
    record = {"cache": "thematic", "ticker": "BTC-USD",
              "date": "2026-10-16", "prev_date": "2026-10-15",
              "prev_value": 100.0, "value": 105.0,
              "implied_return": 0.05, "source": "binance:BTCUSDT",
              "method": "return_splice",
              "applied_at_utc": "2026-10-17T04:03:54+00:00"}
    ledger = tmp_path / "repairs.jsonl"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    targets = {"sleeves": [{"sleeve": "C", "status": status,
                            "decision_session": decision,
                            "last_completed_session": required}]}
    result = _btc_repair_for_live_decision(targets, ledger)
    assert bool(result) is expected


@pytest.mark.parametrize("fault", [
    "duplicate", "different_source", "missing_value", "nan_value",
    "wrong_return", "naive_stamp", "non_object", "duplicate_sleeve",
])
def test_health_provenance_refuses_ambiguous_or_incomplete_record(tmp_path, fault):
    record = {"cache": "thematic", "ticker": "BTC-USD",
              "date": "2026-10-16", "prev_date": "2026-10-15",
              "prev_value": 100.0, "value": 105.0,
              "implied_return": 0.05, "source": "binance:BTCUSDT",
              "method": "return_splice",
              "applied_at_utc": "2026-10-17T04:03:54+00:00"}
    ledger = tmp_path / "repairs.jsonl"
    rows = [record]
    targets = {"sleeves": [{"sleeve": "C", "status": "READY",
                            "decision_session": "2026-10-16",
                            "last_completed_session": "2026-10-16"}]}
    if fault == "duplicate":
        rows.append(dict(record))
    elif fault == "different_source":
        record["source"] = "primary:yfinance"
    elif fault == "missing_value":
        del record["value"]
    elif fault == "nan_value":
        record["value"] = float("nan")
    elif fault == "wrong_return":
        record["implied_return"] = 0.06
    elif fault == "naive_stamp":
        record["applied_at_utc"] = "2026-10-17T04:03:54"
    elif fault == "non_object":
        rows.append([])
    elif fault == "duplicate_sleeve":
        targets["sleeves"].append(dict(targets["sleeves"][0]))
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows),
                      encoding="utf-8")
    assert _btc_repair_for_live_decision(targets, ledger) is None


@pytest.mark.parametrize("previous,decision", [
    ("2026-09-30", "2026-10-01"),
    ("2025-12-31", "2026-01-01"),
])
def test_health_provenance_accepts_completed_boundary_session(
        tmp_path, previous, decision):
    from datetime import date, timedelta

    # Python calendar months are 1-indexed; the date library crosses both
    # month and year boundaries before the BTC UTC day can count as complete.
    applied_day = (date.fromisoformat(decision) + timedelta(days=1)).isoformat()
    record = {"cache": "thematic", "ticker": "BTC-USD",
              "date": decision, "prev_date": previous,
              "prev_value": 100.0, "value": 105.0,
              "implied_return": 0.05, "source": "binance:BTCUSDT",
              "method": "return_splice",
              "applied_at_utc": applied_day + "T04:03:54+00:00"}
    ledger = tmp_path / "repairs.jsonl"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    targets = {"sleeves": [{"sleeve": "C", "status": "READY",
                            "decision_session": decision,
                            "last_completed_session": decision}]}
    now = datetime.fromisoformat(applied_day + "T05:00:00+00:00")
    assert _btc_repair_for_live_decision(targets, ledger, now_utc=now)["date"] == decision


@pytest.mark.parametrize("decision,applied,now", [
    ("2026-10-17", "2026-10-17T05:00:00+00:00", "2026-10-17T06:00:00+00:00"),
    ("2026-10-16", "2026-10-16T23:59:00+00:00", "2026-10-17T06:00:00+00:00"),
    ("2026-10-16", "2026-10-18T05:00:00+00:00", "2026-10-17T06:00:00+00:00"),
])
def test_health_provenance_rejects_unfinished_or_future_proof(
        tmp_path, decision, applied, now):
    record = {"cache": "thematic", "ticker": "BTC-USD",
              "date": decision, "prev_date": "2026-10-15",
              "prev_value": 100.0, "value": 105.0,
              "implied_return": 0.05, "source": "binance:BTCUSDT",
              "method": "return_splice", "applied_at_utc": applied}
    ledger = tmp_path / "repairs.jsonl"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    targets = {"sleeves": [{"sleeve": "C", "status": "READY",
                            "decision_session": decision,
                            "last_completed_session": decision}]}
    assert _btc_repair_for_live_decision(
        targets, ledger, now_utc=datetime.fromisoformat(now)) is None
