"""Guards for the degraded-endpoint detector.

The failure being guarded against is not a crash — it is a step that runs 228
minutes and exits 0 with clean data. So the tests that matter most here are
the ones asserting the circuit does NOT trip: a guard that aborts healthy
refreshes would be worse than the four hours it saves.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from stall_guard import (  # noqa: E402
    EndpointDegraded,
    LatencyCircuit,
    StallTimeout,
    _env_float,
    run_with_deadline,
)


# ---------------------------------------------------------------------------
# LatencyCircuit — must not trip on healthy traffic
# ---------------------------------------------------------------------------

def test_healthy_endpoint_never_trips():
    """1.5-3s per date is the normal cost: throttle plus the request."""
    c = LatencyCircuit(threshold_s=12.0, window=12)
    for i in range(200):
        c.record_served(1.5 + (i % 4) * 0.5)   # 1.5-3.0s
    assert not c.dead
    assert c.reason is None
    assert c.n_served == 200


def test_one_slow_date_among_healthy_ones_does_not_trip():
    """A lone 30s timeout on an otherwise fine endpoint is normal.

    This is why the trip is on a rolling MEAN and not on any single item: a
    guard that fired on one slow request would abort refreshes weekly.
    """
    c = LatencyCircuit(threshold_s=12.0, window=12)
    for _ in range(11):
        c.record_served(2.0)
    c.record_served(36.0)          # mean = (11*2 + 36)/12 = 4.8s
    assert not c.dead


def test_sustained_slowness_trips():
    """The 2026-08-14 NDIA signature: ~30s every date, all succeeding."""
    c = LatencyCircuit(threshold_s=12.0, window=12)
    for _ in range(11):
        c.record_served(30.0)
    assert not c.dead, "must not trip before the window is full"
    c.record_served(30.0, item="2026-07-24")
    assert c.dead
    assert "degraded" in c.reason
    assert "2026-07-24" in c.reason
    assert "30.0s" in c.reason


def test_recovery_within_the_window_does_not_trip():
    """A slow patch that recovers must not trip once it has aged out."""
    c = LatencyCircuit(threshold_s=12.0, window=12)
    for _ in range(6):
        c.record_served(30.0)
    for _ in range(6):
        c.record_served(0.5)       # mean = 15.25 -> still hot
    assert c.dead is True
    # and a fresh circuit seeing the recovered half alone stays healthy
    c2 = LatencyCircuit(threshold_s=12.0, window=12)
    for _ in range(12):
        c2.record_served(0.5)
    assert not c2.dead


def test_trip_is_sticky():
    """Once degraded, later fast dates must not un-trip it mid-walk."""
    c = LatencyCircuit(threshold_s=12.0, window=4)
    for _ in range(4):
        c.record_served(50.0)
    assert c.dead
    reason = c.reason
    for _ in range(20):
        c.record_served(0.01)
    assert c.dead
    assert c.reason == reason


def test_cache_hits_are_excluded_from_the_mean():
    """A warm cache must not be able to hide a stalled endpoint.

    Feeding cache reads into the mean is the obvious wrong implementation:
    an ETF 95% served from disk averages milliseconds no matter how badly the
    remaining 5% behaves.
    """
    c = LatencyCircuit(threshold_s=12.0, window=12)
    for _ in range(400):
        c.record_cache_hit()
    for _ in range(12):
        c.record_served(30.0)
    assert c.dead
    assert c.n_cache_hits == 400
    assert c.n_served == 12


def test_window_must_fill_before_any_trip():
    c = LatencyCircuit(threshold_s=1.0, window=25)
    for _ in range(24):
        c.record_served(9999.0)
    assert not c.dead
    c.record_served(9999.0)
    assert c.dead


def test_raise_if_dead():
    c = LatencyCircuit(threshold_s=1.0, window=2)
    c.raise_if_dead()              # healthy: no raise
    c.record_served(50.0)
    c.record_served(50.0)
    with pytest.raises(EndpointDegraded):
        c.raise_if_dead()


def test_summary_reports_both_means_and_survives_an_empty_circuit():
    c = LatencyCircuit(threshold_s=12.0, window=3, label="TEST endpoint")
    s = c.summary()
    assert s["label"] == "TEST endpoint"
    assert s["degraded"] is False
    assert s["mean_served_s"] is None and s["mean_recent_s"] is None
    c.record_served(2.0)
    c.record_served(4.0)
    s = c.summary()
    assert s["mean_served_s"] == 3.0
    assert s["n_served"] == 2


def test_rejects_nonsense_configuration():
    with pytest.raises(ValueError):
        LatencyCircuit(window=0)
    with pytest.raises(ValueError):
        LatencyCircuit(threshold_s=0)


# ---------------------------------------------------------------------------
# run_with_deadline
# ---------------------------------------------------------------------------

def test_deadline_returns_the_value_when_fast_enough():
    assert run_with_deadline(lambda: 42, seconds=5, label="fast") == 42


def test_deadline_propagates_the_callee_error_unchanged():
    """A real vendor error must not be disguised as a timeout."""
    class Boom(RuntimeError):
        pass

    def explode():
        raise Boom("vendor said no")

    with pytest.raises(Boom, match="vendor said no"):
        run_with_deadline(explode, seconds=5, label="boom")


def test_deadline_trips_on_a_slow_call():
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(30)
        return "never"

    t0 = time.monotonic()
    with pytest.raises(StallTimeout, match="exceeded its"):
        run_with_deadline(slow, seconds=0.5, label="slow call")
    elapsed = time.monotonic() - t0
    assert started.is_set()
    assert elapsed < 5, f"should give up promptly, took {elapsed:.1f}s"


def test_deadline_message_names_the_env_override():
    """The operator needs to know the budget is raisable without reading code."""
    with pytest.raises(StallTimeout, match="BT_DOWNLOAD_DEADLINE_S"):
        run_with_deadline(lambda: time.sleep(10), seconds=0.2, label="x")


def test_deadline_rejects_a_nonsense_budget():
    with pytest.raises(ValueError):
        run_with_deadline(lambda: 1, seconds=0)


# ---------------------------------------------------------------------------
# env overrides
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, 12.0),
    ("30", 30.0),
    ("7.5", 7.5),
    ("banana", 12.0),      # junk must not take down a healthy refresh
    ("0", 12.0),           # nor must a value that would disable the guard
    ("-5", 12.0),
])
def test_env_float_is_forgiving(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("BT_TEST_STALL", raising=False)
    else:
        monkeypatch.setenv("BT_TEST_STALL", raw)
    assert _env_float("BT_TEST_STALL", 12.0) == expected


# ---------------------------------------------------------------------------
# Wiring — the guard has to be reachable from the modules that need it
# ---------------------------------------------------------------------------

def test_fetch_constituents_exposes_a_distinct_degraded_exit_code():
    import fetch_constituents as fc
    codes = {fc.EXIT_OK, fc.EXIT_STALENESS_CRITICAL,
             fc.EXIT_ENDPOINT_UNAVAILABLE, fc.EXIT_ENDPOINT_DEGRADED}
    assert len(codes) == 4, "exit codes must not collide"
    assert fc.EXIT_ENDPOINT_DEGRADED != fc.EXIT_ENDPOINT_UNAVAILABLE


def test_cli_maps_a_degraded_endpoint_to_its_own_exit_code(capsys, monkeypatch):
    """The operator must get an exit code, not a stack trace.

    refresh_all runs each step as its own subprocess, so the exit code IS the
    interface. A traceback exits 1 and reads as an ordinary bug.
    """
    import fetch_constituents as fc

    monkeypatch.setattr(
        fc, "main",
        lambda: (_ for _ in ()).throw(EndpointDegraded("too slow at X")))
    rc = fc.cli()
    assert rc == fc.EXIT_ENDPOINT_DEGRADED
    err = capsys.readouterr().err
    assert "ENDPOINT DEGRADED" in err
    assert "too slow at X" in err
    assert "No roster was written" in err


def test_cli_passes_a_healthy_run_through_untouched(monkeypatch):
    import fetch_constituents as fc
    monkeypatch.setattr(fc, "main", lambda: fc.EXIT_OK)
    assert fc.cli() == fc.EXIT_OK


def test_cli_does_not_swallow_ordinary_errors(monkeypatch):
    """Only degradation is converted. A real bug must still surface."""
    import fetch_constituents as fc
    monkeypatch.setattr(
        fc, "main", lambda: (_ for _ in ()).throw(KeyError("boom")))
    with pytest.raises(KeyError):
        fc.cli()


def test_load_snapshot_tickers_counts_a_cache_hit_and_skips_the_network(
        tmp_path, monkeypatch):
    """Serving from the JSON cache must register as a cache hit, not a sample."""
    import fetch_constituents as fc

    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    (tmp_path / "ZZZZ_20260807.json").write_text(
        '{"_no_holdings": true}', encoding="utf-8")

    def _never(*a, **k):
        raise AssertionError("network must not be touched on a cache hit")

    monkeypatch.setattr(fc, "fetch_product_data", _never)

    c = LatencyCircuit()
    out = fc.load_snapshot_tickers(
        __import__("datetime").date(2026, 8, 7),
        {"symbol": "ZZZZ"}, latency=c)
    assert out == []
    assert c.n_cache_hits == 1
    assert c.n_served == 0


def test_load_snapshot_tickers_times_the_network_path(tmp_path, monkeypatch):
    import datetime as _dt

    import fetch_constituents as fc

    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda *a, **k: {"payload": "x"})
    monkeypatch.setattr(fc, "parse_holdings_json", lambda *a, **k: ["AAA"])

    c = LatencyCircuit()
    out = fc.load_snapshot_tickers(_dt.date(2026, 8, 7),
                                   {"symbol": "ZZZZ"}, latency=c)
    assert out == ["AAA"]
    assert c.n_served == 1
    assert c.n_cache_hits == 0


def test_a_failing_network_date_is_still_timed(tmp_path, monkeypatch):
    """Failures count too — dropping them would flatter the mean.

    A run that alternates timeouts-then-failures between slow successes is
    precisely the mixed regime neither guard catches alone.
    """
    import datetime as _dt

    import fetch_constituents as fc

    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)

    def _boom(*a, **k):
        raise fc.EndpointUnavailable("transport dead")

    monkeypatch.setattr(fc, "fetch_product_data", _boom)

    c = LatencyCircuit()
    with pytest.raises(fc.EndpointUnavailable):
        fc.load_snapshot_tickers(_dt.date(2026, 8, 7),
                                 {"symbol": "ZZZZ"}, latency=c)
    assert c.n_served == 1


def _fake_yf_frame(syms):
    import pandas as pd
    idx = pd.date_range("2026-08-03", periods=5, freq="D")
    return pd.DataFrame(
        {("Close", s): [10.0, 11.0, 12.0, 13.0, 14.0] for s in syms},
        index=idx,
    )


def test_compute_breadth_routes_the_download_through_the_deadline(
        tmp_path, monkeypatch):
    """Behavioural, not textual: the download must actually go through it.

    An earlier version of this test compared source positions of
    "run_with_deadline" and "yf.download" and failed on the COMMENT above the
    call. Reading the source to prove behaviour is how you end up asserting
    about prose.
    """
    import compute_breadth as cb

    seen = {}

    def spy(fn, seconds=None, label=""):
        seen["label"] = label
        seen["called"] = True
        return fn()

    monkeypatch.setattr(cb, "run_with_deadline", spy)
    monkeypatch.setattr(cb.yf, "download",
                        lambda syms, **k: _fake_yf_frame(syms))

    out = cb.download_prices(["AAA", "BBB"], "2026-08-03", "2026-08-08",
                             cache_path=tmp_path / "px.parquet")
    assert seen.get("called"), "yf.download was not wrapped by the deadline"
    assert "yfinance download" in seen["label"]
    assert list(out.columns) == ["AAA", "BBB"]


def test_a_stalled_download_aborts_instead_of_hanging(tmp_path, monkeypatch):
    """The whole point: a download that never returns must not run forever."""
    import compute_breadth as cb

    monkeypatch.setattr(cb.yf, "download",
                        lambda syms, **k: time.sleep(60))
    monkeypatch.setenv("BT_DOWNLOAD_DEADLINE_S", "0.3")

    t0 = time.monotonic()
    with pytest.raises(StallTimeout):
        cb.download_prices(["AAA"], "2026-08-03", "2026-08-08",
                           cache_path=tmp_path / "px.parquet",
                           deadline_s=0.3)
    assert time.monotonic() - t0 < 5


def test_a_stalled_download_leaves_the_prior_cache_intact(
        tmp_path, monkeypatch):
    """Same judgement as n_with_any_data == 0: keep what is already good."""
    import pandas as pd

    import compute_breadth as cb

    cache = tmp_path / "px.parquet"
    prior = pd.DataFrame({"AAA": [1.0, 2.0]},
                         index=pd.date_range("2020-01-01", periods=2))
    prior.to_parquet(cache)
    before = cache.read_bytes()

    monkeypatch.setattr(cb.yf, "download", lambda syms, **k: time.sleep(60))
    with pytest.raises(StallTimeout):
        # Range the cache cannot cover, so it must attempt the download.
        cb.download_prices(["AAA"], "2026-08-03", "2026-08-08",
                           cache_path=cache, deadline_s=0.3)
    assert cache.read_bytes() == before
