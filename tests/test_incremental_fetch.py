"""Offline tests for fetch_constituents incremental mode + negative cache.

The central invariant: an incremental run must produce EXACTLY the same
payload (excluding the fetched_at_utc wall-clock stamp) as a full-history
run over the same underlying data. These tests prove it against a fake
fetch layer; scripts/verify_incremental_parity.py proves the same thing
against the live iShares endpoints.

Python date months are 1-indexed (January = 1). All Friday literals below
were verified as Fridays with pandas (W-FRI); the test window deliberately
crosses two month boundaries (Jan->Feb->Mar 2026).
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_constituents as fc  # noqa: E402
import refresh_all as ra  # noqa: E402

# ---------------------------------------------------------------------------
# Scenario fixtures
# ---------------------------------------------------------------------------

WINDOW_START = date(2026, 1, 2)   # Friday (verified via pandas W-FRI)
WINDOW_END = date(2026, 3, 27)    # Friday
FRIDAYS = [d.date() for d in pd.date_range(WINDOW_START, WINDOW_END, freq="W-FRI")]

CSV_HEADER = (
    "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),"
    "Notional Value,Shares,Price,Location,Exchange,Currency"
)
EMPTY_BODY = 'Fund Holdings as of,"-"\nno holdings for this date\n'


def csv_body(tickers: list[str]) -> str:
    lines = ['Fund Holdings as of,"Jan 02, 2026"', "", CSV_HEADER]
    for t in tickers:
        lines.append(
            f'{t},{t} Corp,Technology,Equity,"1,000",1.0,"1,000",10,'
            f"100.0,United States,Nasdaq,USD"
        )
    lines.append("")
    lines.append("Disclosures follow")
    return "\n".join(lines)


class FakeFetcher:
    """Stands in for fetch_with_retry: serves a date->body corpus; dates
    absent from the corpus raise like an exhausted anti-bot retry ladder.
    Records every call as (date, probe)."""

    def __init__(self, corpus: dict[date, str]):
        self.corpus = corpus
        self.calls: list[tuple[date, bool]] = []

    def __call__(self, target: date, etf_cfg: dict, probe: bool = False) -> str:
        self.calls.append((target, probe))
        body = self.corpus.get(target)
        if body is None:
            raise RuntimeError(f"simulated anti-bot HTML for {target}")
        return body


def make_cfg(**overrides) -> dict:
    cfg = {
        "symbol": "TEST",
        "csv_url_template": "https://example.test/holdings.ajax?fileType=csv",
        "start_friday": WINDOW_START,
        "ticker_overrides": {},
        "csv_date_format": "uk",
    }
    cfg.update(overrides)
    return cfg


def base_corpus() -> dict[date, str]:
    """13 Fridays: two leading misses (skips), a mid-window empty-Friday
    with a populated Thursday (walkback), one mid-window miss
    (carry-forward), the rest populated."""
    c: dict[date, str] = {}
    roster = {
        date(2026, 1, 16): ["AAA", "BBB"],
        date(2026, 1, 23): ["AAA", "BBB"],
        date(2026, 1, 30): ["AAA", "CCC"],
        # 2026-02-06 handled below (walkback)
        date(2026, 2, 13): ["AAA", "CCC"],
        # 2026-02-20 missing (carry-forward)
        date(2026, 2, 27): ["AAA", "CCC", "DDD"],
        date(2026, 3, 6): ["AAA", "CCC", "DDD"],
        date(2026, 3, 13): ["AAA", "DDD"],
        date(2026, 3, 20): ["AAA", "DDD"],
        date(2026, 3, 27): ["AAA", "DDD"],
    }
    for d, tickers in roster.items():
        c[d] = csv_body(tickers)
    # Friday 2026-02-06 returns the empty template; Thursday 2026-02-05 has
    # data -> the walkback path.
    c[date(2026, 2, 6)] = EMPTY_BODY
    c[date(2026, 2, 5)] = csv_body(["AAA", "CCC"])
    return c


def run_main(monkeypatch, tmp_path: Path, fetcher: FakeFetcher, cfg: dict,
             argv: list[str]) -> tuple[int, dict]:
    monkeypatch.setattr(fc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fc, "NEGCACHE_PATH", tmp_path / "fetch_negative_cache.json")
    monkeypatch.setattr(fc, "get_etf", lambda s: cfg)
    monkeypatch.setattr(fc, "fetch_with_retry", fetcher)
    monkeypatch.setattr(fc, "latest_completed_friday", lambda today: WINDOW_END)
    monkeypatch.setattr(sys, "argv", ["fetch_constituents.py", *argv])
    rc = fc.main()
    out = tmp_path / f"constituents_{cfg['symbol'].lower()}.json"
    payload = json.loads(out.read_text(encoding="utf-8"))
    return rc, payload


def payloads_equal_ex_stamp(a: dict, b: dict) -> bool:
    a2 = {k: v for k, v in a.items() if k != "fetched_at_utc"}
    b2 = {k: v for k, v in b.items() if k != "fetched_at_utc"}
    return a2 == b2


# ---------------------------------------------------------------------------
# Full-vs-incremental parity (the central invariant)
# ---------------------------------------------------------------------------


def test_incremental_matches_full_and_uses_no_network(monkeypatch, tmp_path):
    fetcher = FakeFetcher(base_corpus())
    cfg = make_cfg()

    rc, full_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                                ["--etf", "TEST", "--full"])
    assert rc == 0
    # Scenario sanity: 11 snapshots (13 Fridays minus 2 leading skips),
    # one walkback (Thu 2026-02-05 for Fri 2026-02-06), skips + one
    # mid-window carry-forward on 2026-02-20.
    assert len(full_payload["snapshots"]) == 11
    assert [w["fallback_date"] for w in full_payload["walkbacks"]] == ["2026-02-05"]
    assert full_payload["walkbacks"][0]["days_back"] == 1
    outcomes = [(c["target_friday"], c["outcome"])
                for c in full_payload["carry_forwards"]]
    assert outcomes == [("2026-01-02", "skipped"), ("2026-01-09", "skipped"),
                        ("2026-02-20", "carried_forward")]
    n_full_calls = len(fetcher.calls)
    assert n_full_calls == 14  # 13 Fridays + 1 walkback Thursday

    # Negative cache recorded the three misses with a live-attempt stamp.
    neg = json.loads((tmp_path / "fetch_negative_cache.json").read_text(encoding="utf-8"))
    assert sorted(neg["etfs"]["TEST"]) == ["2026-01-02", "2026-01-09", "2026-02-20"]
    assert all(not e.get("seeded_from_store")
               for e in neg["etfs"]["TEST"].values())

    # Incremental over the same store: identical payload, ZERO fetch calls
    # (reals reused, known misses skipped via the negative cache).
    fetcher.calls.clear()
    rc, inc_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                               ["--etf", "TEST", "--incremental"])
    assert rc == 0
    assert fetcher.calls == []
    assert payloads_equal_ex_stamp(full_payload, inc_payload)


def test_incremental_seeds_negcache_from_prior_store(monkeypatch, tmp_path):
    fetcher = FakeFetcher(base_corpus())
    cfg = make_cfg()
    _, full_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                               ["--etf", "TEST", "--full"])
    # Fresh clone situation: prior parsed output exists but no negative
    # cache. The holes must be seeded from the store, not re-attempted.
    (tmp_path / "fetch_negative_cache.json").unlink()
    fetcher.calls.clear()
    rc, inc_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                               ["--etf", "TEST", "--incremental"])
    assert rc == 0
    assert fetcher.calls == []
    assert payloads_equal_ex_stamp(full_payload, inc_payload)
    neg = json.loads((tmp_path / "fetch_negative_cache.json").read_text(encoding="utf-8"))
    assert sorted(neg["etfs"]["TEST"]) == ["2026-01-02", "2026-01-09", "2026-02-20"]
    assert all(e["seeded_from_store"] for e in neg["etfs"]["TEST"].values())
    assert all(e["last_attempt"] == full_payload["fetched_at_utc"][:10]
               for e in neg["etfs"]["TEST"].values())


def test_due_hole_is_probed_and_clears_on_success(monkeypatch, tmp_path):
    fetcher = FakeFetcher(base_corpus())
    cfg = make_cfg()
    run_main(monkeypatch, tmp_path, fetcher, cfg, ["--etf", "TEST", "--full"])

    # Age the 2026-02-20 hole past the retry window and let the upstream
    # source now have data for it.
    neg_path = tmp_path / "fetch_negative_cache.json"
    neg = json.loads(neg_path.read_text(encoding="utf-8"))
    aged = (date.today() - timedelta(days=fc.NEGCACHE_RETRY_DAYS + 10)).isoformat()
    neg["etfs"]["TEST"]["2026-02-20"]["last_attempt"] = aged
    neg_path.write_text(json.dumps(neg), encoding="utf-8")
    fetcher.corpus[date(2026, 2, 20)] = csv_body(["AAA", "CCC", "EEE"])

    fetcher.calls.clear()
    rc, inc_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                               ["--etf", "TEST", "--incremental"])
    assert rc == 0
    # Exactly one network touch: the due hole, in single-attempt probe mode.
    assert fetcher.calls == [(date(2026, 2, 20), True)]
    snap = inc_payload["snapshots"]["2026-02-20"]
    assert "carried_forward_from" not in snap
    assert snap["tickers"] == ["AAA", "CCC", "EEE"]
    # Entry cleared on success.
    neg = json.loads(neg_path.read_text(encoding="utf-8"))
    assert "2026-02-20" not in neg["etfs"]["TEST"]
    # A fresh full run over the updated corpus agrees exactly.
    fetcher.calls.clear()
    _, full_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                               ["--etf", "TEST", "--full"])
    assert payloads_equal_ex_stamp(full_payload, inc_payload)


def test_registry_override_change_falls_back_to_full(monkeypatch, tmp_path):
    fetcher = FakeFetcher(base_corpus())
    run_main(monkeypatch, tmp_path, fetcher, make_cfg(),
             ["--etf", "TEST", "--full"])

    drifted = make_cfg(ticker_overrides={"ZZZ": "Z-Z"})
    fetcher.calls.clear()
    _, inc_payload = run_main(monkeypatch, tmp_path, fetcher, drifted,
                              ["--etf", "TEST", "--incremental"])
    # Prior store was built under different overrides -> nothing reused;
    # negative-cache skips for known misses still apply, so the re-fetch
    # touches the 11 datable Fridays + the walkback Thursday, not the
    # 3 recorded misses.
    assert len(fetcher.calls) == 11
    fetcher.calls.clear()
    _, full_payload = run_main(monkeypatch, tmp_path, fetcher, drifted,
                               ["--etf", "TEST", "--full"])
    assert payloads_equal_ex_stamp(full_payload, inc_payload)


# ---------------------------------------------------------------------------
# EDGAR fallback semantics under negative-cache skips (the SOXX guarantee)
# ---------------------------------------------------------------------------


def edgar_corpus() -> dict[date, str]:
    """Five populated Fridays then a persistent primary outage (the SOXX
    Akamai shape)."""
    c: dict[date, str] = {}
    for d, tickers in {
        date(2026, 1, 2): ["AAA", "BBB"],
        date(2026, 1, 9): ["AAA", "BBB"],
        date(2026, 1, 16): ["AAA", "BBB"],
        date(2026, 1, 23): ["AAA", "BBB"],
        date(2026, 1, 30): ["AAA", "CCC"],
    }.items():
        c[d] = csv_body(tickers)
    return c


def test_negcache_skip_still_consults_edgar(monkeypatch, tmp_path):
    import edgar_nport

    edgar_calls = {"n": 0}

    def fake_roster(cik, series_id):
        edgar_calls["n"] += 1
        return SimpleNamespace(
            tickers=["EE1", "EE2"],
            filing=SimpleNamespace(
                report_period_end="2026-02-27",
                filing_date="2026-03-02",
                accession_number="ACC-1",
            ),
        )

    monkeypatch.setattr(edgar_nport, "fetch_roster_via_edgar", fake_roster)
    cfg = make_cfg(edgar_nport={"cik": "999", "series_id": "S000000001"})
    fetcher = FakeFetcher(edgar_corpus())

    rc, full_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                                ["--etf", "TEST", "--full"])
    assert rc == 0
    assert edgar_calls["n"] == 1  # lazy-loaded once per run
    # 2026-02-27 is the first outage Friday the filing (repPdEnd
    # 2026-02-27) can serve; earlier outage Fridays carry forward.
    assert full_payload["snapshots"]["2026-02-27"]["source"] == "edgar_nport"
    assert [e["target_friday"] for e in full_payload["edgar_used"]] == ["2026-02-27"]
    assert full_payload["snapshots"]["2026-02-20"]["carried_forward_from"] == "2026-01-30"
    # Post-EDGAR outage Fridays carry the EDGAR roster forward (the filing
    # is no fresher than the EDGAR snapshot itself).
    assert full_payload["snapshots"]["2026-03-27"]["carried_forward_from"] == "2026-02-27"

    # Incremental: every outage Friday is negative-cache skipped on the
    # primary, yet EDGAR is still loaded and applied exactly as before.
    edgar_calls["n"] = 0
    fetcher.calls.clear()
    rc, inc_payload = run_main(monkeypatch, tmp_path, fetcher, cfg,
                               ["--etf", "TEST", "--incremental"])
    assert rc == 0
    assert fetcher.calls == []          # primary never touched
    assert edgar_calls["n"] == 1        # EDGAR consulted regardless
    assert payloads_equal_ex_stamp(full_payload, inc_payload)


# ---------------------------------------------------------------------------
# NegativeCache unit behaviour
# ---------------------------------------------------------------------------


def _cache(tmp_path: Path) -> fc.NegativeCache:
    return fc.NegativeCache(tmp_path / "neg.json")


def test_decide_recent_friday_always_attempted(tmp_path):
    nc = _cache(tmp_path)
    today = date(2026, 7, 27)
    recent = today - timedelta(days=7)
    nc.record_failure("X", recent, today)
    assert nc.decide("X", recent, today) == "attempt"


def test_decide_fresh_entry_skips_and_due_entry_probes(tmp_path):
    nc = _cache(tmp_path)
    today = date(2026, 7, 27)
    old_friday = today - timedelta(days=400)
    assert nc.decide("X", old_friday, today) == "attempt"  # no entry yet
    nc.record_failure("X", old_friday, today - timedelta(days=5))
    assert nc.decide("X", old_friday, today) == "skip"     # attempted 5d ago
    nc.record_failure("X", old_friday, today - timedelta(days=fc.NEGCACHE_RETRY_DAYS))
    assert nc.decide("X", old_friday, today) == "probe"    # due for re-check


def test_decide_probe_cap_defers_excess_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "NEGCACHE_MAX_RETRIES_PER_RUN", 3)
    nc = _cache(tmp_path)
    today = date(2026, 7, 27)
    stale = today - timedelta(days=90)
    fridays = [today - timedelta(days=365 + 7 * i) for i in range(5)]
    for f in fridays:
        nc.record_failure("X", f, stale)
    decisions = [nc.decide("X", f, today) for f in fridays]
    assert decisions == ["probe", "probe", "probe", "skip", "skip"]


def test_record_success_clears_entry(tmp_path):
    nc = _cache(tmp_path)
    today = date(2026, 7, 27)
    f = today - timedelta(days=200)
    nc.record_failure("X", f, today)
    nc.record_success("X", f)
    nc.save()
    reloaded = _cache(tmp_path)
    assert reloaded.entry("X", f) is None


def test_record_failure_increments_and_unsets_seeded(tmp_path):
    nc = _cache(tmp_path)
    today = date(2026, 7, 27)
    f = today - timedelta(days=200)
    nc.seed_from_store("X", [f], today - timedelta(days=40))
    e = nc.entry("X", f)
    assert e["seeded_from_store"] and e["attempts"] == 1
    nc.record_failure("X", f, today)
    e = nc.entry("X", f)
    assert e["attempts"] == 2
    assert "seeded_from_store" not in e
    assert e["first_seen"] == (today - timedelta(days=40)).isoformat()


def test_seed_never_overwrites_existing_entries(tmp_path):
    nc = _cache(tmp_path)
    today = date(2026, 7, 27)
    f = today - timedelta(days=200)
    nc.record_failure("X", f, today)
    assert nc.seed_from_store("X", [f], today - timedelta(days=90)) == 0
    assert nc.entry("X", f)["last_attempt"] == today.isoformat()


def test_save_merges_sections_across_instances(tmp_path):
    path = tmp_path / "neg.json"
    today = date(2026, 7, 27)
    f = today - timedelta(days=200)
    a = fc.NegativeCache(path)
    a.record_failure("AAA", f, today)
    a.save()
    b = fc.NegativeCache(path)
    b.record_failure("BBB", f, today)
    b.save()
    # Instance a saves again from stale memory: must not clobber BBB.
    a.record_failure("AAA", f - timedelta(days=7), today)
    a.save()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(doc["etfs"]) == ["AAA", "BBB"]
    assert len(doc["etfs"]["AAA"]) == 2


# ---------------------------------------------------------------------------
# Reuse loader validation
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "constituents_test.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_loader_excludes_carried_and_edgar_snapshots(tmp_path):
    cfg = make_cfg()
    payload = {
        "etf": "TEST",
        "source": cfg["csv_url_template"],
        "ticker_overrides_applied": {},
        "end_friday": "2026-03-27",
        "fetched_at_utc": "2026-07-26T20:00:00+00:00",
        "snapshots": {
            "2026-01-16": {"actual_date": "2026-01-16", "n_tickers": 1,
                           "tickers": ["AAA"]},
            "2026-01-23": {"actual_date": "2026-01-16", "n_tickers": 1,
                           "carried_forward_from": "2026-01-16",
                           "tickers": ["AAA"]},
            "2026-01-30": {"actual_date": "2026-01-28", "n_tickers": 1,
                           "source": "edgar_nport", "tickers": ["AAA"]},
        },
    }
    reusable, prior, reason = fc.load_reusable_snapshots(
        cfg, _store(tmp_path, payload))
    assert reason is None and prior is not None
    assert list(reusable) == ["2026-01-16"]


def test_loader_rejects_registry_drift(tmp_path):
    cfg = make_cfg()
    good = {
        "etf": "TEST", "source": cfg["csv_url_template"],
        "ticker_overrides_applied": {}, "end_friday": "2026-03-27",
        "fetched_at_utc": "2026-07-26T20:00:00+00:00", "snapshots": {},
    }
    for mutation, expect in [
        ({"etf": "OTHER"}, "not"),
        ({"source": "https://example.test/changed.ajax"}, "csv_url_template"),
        ({"ticker_overrides_applied": {"BRKB": "BRK-B"}}, "ticker_overrides"),
    ]:
        payload = dict(good, **mutation)
        reusable, prior, reason = fc.load_reusable_snapshots(
            cfg, _store(tmp_path, payload))
        assert reusable == {} and prior is None
        assert expect in reason
    missing = fc.load_reusable_snapshots(cfg, tmp_path / "absent.json")
    assert missing[0] == {} and "no prior output" in missing[2]


# ---------------------------------------------------------------------------
# Probe ladder + orchestrator wiring
# ---------------------------------------------------------------------------


def test_probe_makes_single_attempt(monkeypatch, tmp_path):
    calls = {"n": 0}

    def failing_get(url, headers=None, timeout=None):
        calls["n"] += 1
        raise ConnectionError("simulated transport failure")

    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fc.requests, "get", failing_get)
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    cfg = make_cfg()
    with pytest.raises(RuntimeError):
        fc.fetch_with_retry(date(2026, 1, 2), cfg, probe=True)
    assert calls["n"] == 1
    calls["n"] = 0
    with pytest.raises(RuntimeError):
        fc.fetch_with_retry(date(2026, 1, 2), cfg)
    assert calls["n"] == 1 + len(fc.RETRY_BACKOFFS)


def test_refresh_all_fetch_cmd_modes():
    inc = ra.constituent_fetch_cmd("py", "CSP1", full=False)
    full = ra.constituent_fetch_cmd("py", "CSP1", full=True)
    assert inc[-1] == "--incremental" and full[-1] == "--full"
    assert inc[:-1] == full[:-1] == ["py", "scripts/fetch_constituents.py",
                                     "--etf", "CSP1"]


def test_refresh_all_defaults_to_incremental():
    args = ra.make_parser().parse_args([])
    assert args.full is False
    assert ra.make_parser().parse_args(["--full"]).full is True


def test_fetch_constituents_cli_default_is_incremental(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["fetch_constituents.py", "--etf", "TEST"])
    assert fc.parse_args().incremental is True
    monkeypatch.setattr(sys, "argv",
                        ["fetch_constituents.py", "--etf", "TEST", "--full"])
    assert fc.parse_args().incremental is False


# ---------------------------------------------------------------------------
# compute_breadth --reuse-price-cache
# ---------------------------------------------------------------------------


def test_download_prices_reuse_cache_dates(monkeypatch, tmp_path):
    import compute_breadth as cb

    idx = pd.date_range("2026-01-02", "2026-03-27", freq="B")
    frame = pd.DataFrame({"A": 1.0, "B": 2.0}, index=idx)
    cache = tmp_path / "prices.parquet"
    frame.to_parquet(cache)

    def no_network(*args, **kwargs):
        raise AssertionError("yfinance download attempted")

    monkeypatch.setattr(cb.yf, "download", no_network)
    out = cb.download_prices(["A", "B"], "2026-01-02", "2026-12-31",
                             cache_path=cache, reuse_cache_dates=True)
    assert list(out.columns) == ["A", "B"]
    assert out.index.max() == idx.max()
    # Default policy must NOT silently reuse a date-short cache.
    with pytest.raises(AssertionError, match="yfinance download attempted"):
        cb.download_prices(["A", "B"], "2026-01-02", "2026-12-31",
                           cache_path=cache)
