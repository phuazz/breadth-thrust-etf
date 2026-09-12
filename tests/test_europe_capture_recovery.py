"""Offline recovery tests. Python datetime months are 1-indexed."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scheduled_refresh as sr
import refresh_all as refresh
import export_holdings_prices as prices
import component_release as release


@pytest.mark.parametrize("codes,expected,result", [
    ([0, 0], [("core", False), ("europe", False)], 0),
    ([3, 0, 0, 0], [("core", False), ("europe", True), ("core", False), ("europe", False)], 0),
    ([4, 0, 3], [("core", False), ("europe", True), ("core", False)], 3),
    ([3, 3, 0, 0], [("core", False), ("europe", True), ("core", False), ("europe", False)], 0),
    ([3, 2], [("core", False), ("europe", True)], 2),
    ([2], [("core", False)], 2),
    ([5], [("core", False)], 5),
])
def test_bounded_recovery(codes, expected, result):
    calls = []
    results = iter(codes)
    def child(component, *, capture_only):
        calls.append((component, capture_only))
        return next(results)
    assert sr.component_sequence(child, armed=True) == result
    assert calls == expected


def test_explicit_recovery_starts_with_d_and_never_repeats():
    calls = []
    def child(component, *, capture_only):
        calls.append((component, capture_only))
        return 0 if capture_only else 3
    assert sr.component_sequence(child, armed=True, recover_first=True) == 3
    assert calls == [("europe", True), ("core", False)]


def test_soak_does_not_start_another_child_on_dirty_outputs():
    calls = []
    def child(component, *, capture_only):
        calls.append(component)
        return 0
    assert sr.component_sequence(child, armed=False) == 0
    assert calls == ["core"]


@pytest.mark.parametrize("capture_fails", [False, True])
def test_capture_only_never_prepares_book_or_runs_engines(monkeypatch, capture_fails):
    calls = []
    monkeypatch.setenv("BTE_COMPONENT_REFRESH", "core")
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--component", "europe",
                                     "--capture-only", "--throttle", "0"])
    monkeypatch.setattr(refresh, "ETFS_REFRESH", ["CSP1", "EXV1"])
    monkeypatch.setattr(refresh, "ETFS_ALL", ["CSP1", "EXV1"])
    import component_basis
    monkeypatch.setattr(component_basis, "prepare", lambda *a: pytest.fail("prepared held book"))
    def step(label, cmd):
        calls.append(cmd)
        return (not capture_fails or "fetch_constituents.py" not in cmd[1]), 0
    monkeypatch.setattr(refresh, "run_step", step)
    assert refresh.main() == (1 if capture_fails else 0)
    assert calls[0][-1] == "EXV1"
    assert calls[-1][1:] == ["scripts/export_holdings_prices.py", "--refresh-caches-only",
                            "--component", "europe"]
    assert all(c[1] in {"scripts/fetch_constituents.py", "scripts/compute_breadth.py",
                       "scripts/export_holdings_prices.py"} for c in calls)


def test_europe_cache_scope_does_not_refresh_core(monkeypatch):
    monkeypatch.setattr(prices, "engine_ohlc_tickers", lambda: {"SPY": "CSP1", "EXV1.DE": "EXV1"})
    seen = []
    monkeypatch.setattr(prices, "refresh_ohlc_caches", lambda mapping: seen.append(mapping) or 0)
    assert prices.main(["--refresh-caches-only", "--component", "europe"]) == 0
    assert seen == [{"EXV1.DE": "EXV1"}]


@pytest.mark.parametrize("now,required", [
    (datetime(2026, 9, 12, 15, tzinfo=timezone.utc), "2026-09-11"),
    (datetime(2026, 8, 1, 6, tzinfo=timezone.utc), "2026-07-31"),
    (datetime(2027, 1, 2, 6, tzinfo=timezone.utc), "2026-12-30"),
])
def test_current_venue_price_not_seven_day_age(now, required):
    assert not prices.missing_completed_session("EXV1.DE", {"dates": [required]}, now)
    assert prices.missing_completed_session("EXV1.DE", {"dates": []}, now)


def test_thursday_quote_is_recent_but_not_ready_for_friday_release():
    now = datetime(2026, 9, 12, 15, tzinfo=timezone.utc)
    entry = {"dates": ["2026-09-10"], "prices": [100]}
    assert not prices.entry_is_stale(entry, now)
    assert prices.missing_completed_session("EXV1.DE", entry, now)


def test_shipped_exporter_refetches_recent_but_incomplete_component_quote(monkeypatch, tmp_path):
    import pandas as pd
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 12, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(prices, "datetime", Clock)
    monkeypatch.setenv("BTE_COMPONENT_REFRESH", "core")
    monkeypatch.setattr(prices, "DATA_DIR", tmp_path)
    monkeypatch.setattr(prices, "OUT_PATH", tmp_path / "prices.json")
    monkeypatch.setattr(prices, "collect_all_tickers", lambda: {"EXV1.DE"})
    monkeypatch.setattr(prices, "collect_book_symbols", lambda: {"EXV1.DE"})
    monkeypatch.setattr(prices, "NETWORK_FALLBACK_TICKERS", ["EXV1.DE"])
    series = pd.Series(100.0, index=pd.bdate_range(end="2026-09-10", periods=400))
    monkeypatch.setattr(prices, "load_close_series", lambda ticker: series)
    fetched = []
    monkeypatch.setattr(prices, "fetch_missing_from_yfinance",
                        lambda tickers, **kw: fetched.extend(tickers) or {})
    assert prices.main([]) == 0
    assert fetched == ["EXV1.DE"]
    # An unavailable response must not manufacture the missing Friday close.
    assert release.read(tmp_path / "prices.json")["prices"]["EXV1.DE"]["dates"][-1] == "2026-09-10"


def test_d_risk_resize_still_requires_actual_price(tmp_path):
    release.write(tmp_path / "data/holdings_prices_1y.json", {
        "prices": {"EXV1.DE": {"dates": ["2026-09-10"], "prices": [100]}}})
    book = {"as_of": "2026-09-11", "sleeves": [
        {"sleeve": "D", "status": "HOLD", "last_completed_session": "2026-09-11"}],
        "lines": [{"sleeve": "D", "etf": "EXV1", "delta": -.02}]}
    with pytest.raises(ValueError, match="missing price observation"):
        release.price_evidence(tmp_path, book)
    release.write(tmp_path / "data/holdings_prices_1y.json", {
        "prices": {"EXV1.DE": {"dates": ["2026-09-11"], "prices": [101]}}})
    assert release.price_evidence(tmp_path, book)["EXV1"]["price"] == 101


@pytest.mark.parametrize("rc,clean,expected", [(0, True, 0), (1, True, 3), (0, False, 2)])
def test_scheduler_collection_restores_unsealed_outputs_without_publication(monkeypatch, tmp_path, rc, clean, expected):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sr, "log_path_for", lambda now: tmp_path / "capture.log")
    monkeypatch.setattr(sr, "price_source_preflight", lambda source: (True, "fixture"))
    git_calls = []
    def git(args, log, **kwargs):
        git_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(sr, "_git", git)
    commands = []
    monkeypatch.setattr(sr.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or SimpleNamespace(returncode=rc))
    restored = []
    monkeypatch.setattr(sr, "restore_tracked_outputs", lambda log: restored.append(True) or clean)
    monkeypatch.setattr(sr, "_email", lambda *a: pytest.fail("collection sent email"))
    monkeypatch.setattr(sr, "record_green_run", lambda *a: pytest.fail("collection marked publication green"))
    assert sr.main(["--component", "europe", "--capture-only"]) == expected
    assert restored == [True]
    assert len(commands) == 1 and "--capture-only" in commands[0]
    assert all(c[0] in {"status", "pull"} for c in git_calls)


def test_collection_rejects_push():
    with pytest.raises(SystemExit):
        sr.main(["--component", "europe", "--capture-only", "--push"])
