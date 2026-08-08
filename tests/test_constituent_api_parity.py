"""Parity and failure-mode tests for the Phase 27 product-data transport.

Background. iShares re-platformed its product pages between the 2026-07-10
and 2026-07-17 refreshes. The legacy `<ajax_id>.ajax?fileType=csv` route
stopped serving CSV and began returning the single-page product shell as
HTTP 200 HTML for every date, including dates that had previously worked.
Every roster silently fell back to carry-forward for four weeks.

Two classes of test here:

1. PARITY — the new JSON transport must reproduce, ticker for ticker, what
   the old CSV transport produced for the same fund on the same date. This
   is the test that would have to fail before we trusted a roster change.
   It covers all six exchange-suffix ETFs, so it also pins the Exchange /
   Location to yfinance-suffix resolution, which is where a silent
   mis-resolution would otherwise hide.

2. FAILURE MODES — the taxonomy that Phase 27 introduced. A dead endpoint,
   a changed payload, and a date with genuinely no holdings must stay
   distinguishable. Conflating them is the original defect.

Fixtures live in tests/fixtures/constituents_parity/ — see the README there
for provenance and how to regenerate them.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_constituents as fc  # noqa: E402
from etf_registry import get_etf  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "constituents_parity"

# (symbol, asOfDate). SOXX is pinned to its last known-good date because the
# iShares US .ajax endpoint has been Akamai-blocked since ~2026-05-15; the
# product-data API reaches it via targetSite=ishares-us, which is what makes
# SOXX fetchable again at all.
PARITY_CASES = [
    ("CSP1", "20260710"),
    ("SOXX", "20260508"),
    ("EXV1", "20260710"),
    ("EXH1", "20260710"),
    ("IJPN", "20260710"),
    ("ITWN", "20260710"),
    ("NDIA", "20260710"),
    ("IDP6", "20260710"),
]

SUFFIX_CASES = [c for c in PARITY_CASES
                if get_etf(c[0]).get("apply_exchange_suffix")]


def _stamp_to_date(stamp: str) -> date:
    # Python months are 1-indexed (Jan=1), unlike JavaScript's 0-indexed Date.
    return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))


def _load(symbol: str, stamp: str):
    cfg = get_etf(symbol)
    csv_body = (FIXTURES / f"{symbol}_{stamp}.csv").read_text(encoding="utf-8")
    payload = json.loads(
        (FIXTURES / f"{symbol}_{stamp}_api.json").read_text(encoding="utf-8")
    )
    return cfg, csv_body, payload


# ---------------------------------------------------------------------------
# 1. Parity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,stamp", PARITY_CASES)
def test_json_transport_matches_csv_ground_truth(symbol, stamp):
    """The new transport must return the identical roster, in order."""
    cfg, csv_body, payload = _load(symbol, stamp)
    overrides = cfg.get("ticker_overrides", {})
    suffix = cfg.get("apply_exchange_suffix", False)

    from_csv = fc.parse_holdings(
        csv_body, ticker_overrides=overrides, apply_exchange_suffix=suffix)
    from_json = fc.parse_holdings_json(
        payload, _stamp_to_date(stamp), ticker_overrides=overrides,
        apply_exchange_suffix=suffix)

    assert from_csv, f"{symbol}: CSV fixture parsed to an empty roster"
    assert from_json == from_csv, (
        f"{symbol} {stamp}: transport parity broken. "
        f"json-only={sorted(set(from_json) - set(from_csv))[:10]} "
        f"csv-only={sorted(set(from_csv) - set(from_json))[:10]}"
    )


@pytest.mark.parametrize("symbol,stamp", SUFFIX_CASES)
def test_exchange_suffixes_resolve_identically(symbol, stamp):
    """Every non-US ticker must carry a yfinance suffix, from both paths.

    A bare ticker here means the Exchange column stopped resolving and the
    resolver fell through to its assume-US branch — the silent failure that
    would send the price fetch to the wrong listing.
    """
    cfg, csv_body, payload = _load(symbol, stamp)
    overrides = cfg.get("ticker_overrides", {})

    from_json = fc.parse_holdings_json(
        payload, _stamp_to_date(stamp), ticker_overrides=overrides,
        apply_exchange_suffix=True)
    from_csv = fc.parse_holdings(
        csv_body, ticker_overrides=overrides, apply_exchange_suffix=True)

    assert from_json == from_csv
    # Not every constituent is non-US (IDP6 holds US lines), but the two
    # paths must at minimum agree on which ones are suffixed.
    assert ({t for t in from_json if "." in t}
            == {t for t in from_csv if "." in t})


def test_every_registry_etf_builds_valid_request_params():
    """Params must be derivable from the registry alone, for both regions."""
    from etf_registry import ETF_REGISTRY

    for symbol, cfg in ETF_REGISTRY.items():
        params = fc.product_data_params(date(2026, 7, 10), cfg)
        assert params["portfolioId"] == str(cfg["product_id"])
        assert params["asOfDate"] == "20260710"
        assert params["component"] == "holdings"
        expected_site = "ishares-us" if cfg.get("ishares_region") == "us" \
            else "ishares-uk"
        assert params["targetSite"] == expected_site, symbol


def test_unknown_region_is_rejected_not_guessed():
    with pytest.raises(ValueError, match="Unknown ishares_region"):
        fc.product_data_params(
            date(2026, 7, 10),
            {"symbol": "FAKE", "product_id": "1", "ishares_region": "moon"},
        )


# ---------------------------------------------------------------------------
# 2. Failure modes
# ---------------------------------------------------------------------------

def _payload_with(**datapoints) -> dict:
    return {"componentsByNameMap": {"holdings": {"containersByNameMap": {
        "all": {"dataPointsByNameMap": datapoints}}}}}


def test_date_mismatch_returns_empty_not_the_latest_roster():
    """The look-ahead guard.

    For a weekend, holiday, pre-inception or future date the API does not
    error — it returns the LATEST roster with asOfDate silently rewritten to
    the latest available date. Verified against the live endpoint on
    2026-08-07: asOfDate=20260711 (a Saturday) echoed back 20260805.

    Accepting that payload would stamp today's roster onto a historical
    Friday, which is look-ahead bias in a point-in-time backtest. The parser
    must treat an echoed date that differs from the requested one as
    "no data for this date".
    """
    payload = _payload_with(
        asOfDate={"value": "20260805"},
        ticker={"value": ["NVDA", "AAPL"]},
        assetClass={"value": ["Equity", "Equity"]},
    )
    assert fc.parse_holdings_json(payload, date(2026, 7, 11)) == []
    # Sanity: the same payload IS accepted for the date it actually echoes.
    assert fc.parse_holdings_json(payload, date(2026, 8, 5)) == ["NVDA", "AAPL"]


def test_null_ticker_array_is_no_data_not_an_error():
    """The other no-data signal. `hasData` is True even here, so it cannot
    be used to discriminate — only the null roster and the date can."""
    payload = _payload_with(
        asOfDate={"value": "20260711"},
        ticker={"value": None},
        assetClass={"value": None},
    )
    assert fc.parse_holdings_json(payload, date(2026, 7, 11)) == []


@pytest.mark.parametrize("payload", [
    {},
    {"componentsByNameMap": {}},
    {"componentsByNameMap": {"holdings": {}}},
    _payload_with(asOfDate={"value": "20260710"}),          # no ticker column
    _payload_with(ticker={"value": []}, assetClass={"value": []}),  # no date
])
def test_changed_payload_raises_rather_than_reading_zero_holdings(payload):
    """A structural change must NOT look like an empty roster.

    This is the whole lesson of the outage: a payload we can no longer parse
    has to be loud. If this returned [] it would walk back, carry forward,
    and report a healthy run.
    """
    with pytest.raises(fc.PayloadContractError):
        fc.parse_holdings_json(payload, date(2026, 7, 10))


def test_mismatched_column_lengths_raise():
    payload = _payload_with(
        asOfDate={"value": "20260710"},
        ticker={"value": ["NVDA", "AAPL"]},
        assetClass={"value": ["Equity"]},
    )
    with pytest.raises(fc.PayloadContractError, match="expected 2"):
        fc.parse_holdings_json(payload, date(2026, 7, 10))


# ---------------------------------------------------------------------------
# 3. Circuit breaker
# ---------------------------------------------------------------------------

def test_circuit_trips_once_and_short_circuits_the_rest(monkeypatch, tmp_path):
    """One dead date must not cost 448 more.

    Against a dead endpoint each date costs ~48s (four attempts plus 45s of
    backoff). The breaker must convert an outage from O(Fridays) network
    round-trips into exactly one.
    """
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    calls: list[date] = []

    def dead(target, etf_cfg):
        calls.append(target)
        raise fc.EndpointUnavailable("HTTP 200 but body is not JSON")

    monkeypatch.setattr(fc, "fetch_product_data", dead)

    cfg = get_etf("CSP1")
    circuit = fc.EndpointCircuit()
    fridays = [date(2026, 7, 17), date(2026, 7, 24), date(2026, 7, 31)]
    statuses = [fc.get_snapshot(f, cfg, circuit)[2] for f in fridays]

    assert statuses == ["endpoint_unavailable"] * 3
    assert circuit.dead
    assert circuit.first_failure_target == date(2026, 7, 17)
    assert circuit.n_unavailable == 3
    # Exactly one network attempt in total, on the first Friday only.
    assert calls == [date(2026, 7, 17)]


def test_walkback_still_runs_when_a_date_is_merely_empty(monkeypatch, tmp_path):
    """Regression guard for the latent walkback bug.

    Previously a transport exception propagated straight out of get_snapshot,
    so the walk aborted on its first iteration and MAX_WALKBACK_DAYS never
    applied. An empty date must still walk back; only a dead transport
    short-circuits.
    """
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    seen: list[date] = []

    def empty_until(target, etf_cfg):
        seen.append(target)
        if target == date(2026, 7, 15):        # data appears 2 days back
            return _payload_with(
                asOfDate={"value": "20260715"},
                ticker={"value": ["NVDA"]},
                assetClass={"value": ["Equity"]},
            )
        return _payload_with(
            asOfDate={"value": target.strftime("%Y%m%d")},
            ticker={"value": None}, assetClass={"value": None},
        )

    monkeypatch.setattr(fc, "fetch_product_data", empty_until)

    circuit = fc.EndpointCircuit()
    tickers, actual, status = fc.get_snapshot(
        date(2026, 7, 17), get_etf("CSP1"), circuit)

    assert status == "walkback"
    assert actual == date(2026, 7, 15)
    assert tickers == ["NVDA"]
    assert not circuit.dead
    assert seen == [date(2026, 7, 17), date(2026, 7, 16), date(2026, 7, 15)]


def test_exhausted_walkback_is_not_found_not_an_outage(monkeypatch, tmp_path):
    """A genuine data gap must stay distinguishable from a dead endpoint."""
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fc, "fetch_product_data", lambda target, cfg: _payload_with(
        asOfDate={"value": target.strftime("%Y%m%d")},
        ticker={"value": None}, assetClass={"value": None},
    ))
    circuit = fc.EndpointCircuit()
    _, _, status = fc.get_snapshot(date(2026, 7, 17), get_etf("CSP1"), circuit)
    assert status == "not_found"
    assert not circuit.dead


def _no_data(target, cfg):
    return _payload_with(
        asOfDate={"value": target.strftime("%Y%m%d")},
        ticker={"value": None}, assetClass={"value": None},
    )


def test_recent_no_data_is_not_cached(monkeypatch, tmp_path):
    """An empty answer for a recent Friday usually means "not published
    yet". Caching it would freeze the gap permanently."""
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fc, "fetch_product_data", _no_data)
    recent = date.today() - timedelta(days=3)
    assert fc.load_snapshot_tickers(recent, get_etf("CSP1")) == []
    assert list(tmp_path.glob("*.json")) == []


def test_settled_no_data_is_cached_as_a_marker(monkeypatch, tmp_path):
    """Regression guard for a real cost bug.

    A fund that launched mid-sample has pre-inception Fridays with no
    holdings. Each costs 6 uncached requests (the Friday plus the full
    walkback). Without a negative cache that price is paid on EVERY run,
    forever — IUCM, which launched in September 2018, was re-fetching ~37
    Fridays x 6 dates on every refresh.

    The marker is stored instead of the payload: the response body for a
    no-data date is the latest-date fallback, so it carries nothing worth
    keeping.
    """
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    calls: list[date] = []

    def counted(target, cfg):
        calls.append(target)
        return _no_data(target, cfg)

    monkeypatch.setattr(fc, "fetch_product_data", counted)
    old = date(2018, 1, 5)

    assert fc.load_snapshot_tickers(old, get_etf("CSP1")) == []
    cached = list(tmp_path.glob("*.json"))
    assert [p.name for p in cached] == ["CSP1_20180105.json"]
    marker = json.loads(cached[0].read_text(encoding="utf-8"))
    assert marker["_no_holdings"] is True
    assert marker["requested_as_of"] == "20180105"

    # Second call must be served from the marker, with no network.
    assert fc.load_snapshot_tickers(old, get_etf("CSP1")) == []
    assert len(calls) == 1


def test_positive_response_is_cached_as_the_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fc, "fetch_product_data", lambda target, cfg: _payload_with(
        asOfDate={"value": target.strftime("%Y%m%d")},
        ticker={"value": ["NVDA"]}, assetClass={"value": ["Equity"]},
    ))
    assert fc.load_snapshot_tickers(date(2026, 7, 17), get_etf("CSP1")) == ["NVDA"]
    assert [p.name for p in tmp_path.glob("*.json")] == ["CSP1_20260717.json"]


def test_legacy_csv_cache_still_wins(monkeypatch, tmp_path):
    """The ~10,400 pre-re-platform CSVs remain the source of truth for
    history and must never trigger a network call."""
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path)

    def explode(target, etf_cfg):
        raise AssertionError("network hit despite a warm CSV cache")

    monkeypatch.setattr(fc, "fetch_product_data", explode)
    src = FIXTURES / "CSP1_20260710.csv"
    (tmp_path / "CSP1_20260710.csv").write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8")

    tickers = fc.load_snapshot_tickers(date(2026, 7, 10), get_etf("CSP1"))
    assert len(tickers) == 504
    assert "BRK-B" in tickers          # ticker_overrides still applied


# ---------------------------------------------------------------------------
# 4. End-to-end: what main() writes and what it exits with
# ---------------------------------------------------------------------------

def _stub_run(monkeypatch, tmp_path, *, n_weeks=2, carry_forward=False):
    """Point main() at a temp tree and a short, today-relative Friday range."""
    from datetime import timedelta

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(fc, "DATA_DIR", data_dir)
    monkeypatch.setattr(fc, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(fc, "PROJECT_ROOT", tmp_path)

    end = fc.latest_completed_friday(date.today())
    cfg = dict(get_etf("CSP1"))
    cfg["start_friday"] = end - timedelta(days=7 * n_weeks)
    monkeypatch.setattr(fc, "get_etf", lambda _sym: cfg)

    import argparse
    monkeypatch.setattr(fc, "parse_args", lambda: argparse.Namespace(
        etf="CSP1", carry_forward_on_outage=carry_forward))
    monkeypatch.setattr(fc.time, "sleep", lambda *_a, **_k: None)
    n_fridays = len(fc.fridays_between(cfg["start_friday"], end))
    return data_dir / "constituents_csp1.json", n_fridays


def test_healthy_run_writes_every_friday_and_exits_ok(monkeypatch, tmp_path):
    out, n_fridays = _stub_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fc, "fetch_product_data", lambda target, cfg: _payload_with(
        asOfDate={"value": target.strftime("%Y%m%d")},
        ticker={"value": ["NVDA", "AAPL"]},
        assetClass={"value": ["Equity", "Equity"]},
    ))

    assert fc.main() == fc.EXIT_OK

    written = json.loads(out.read_text(encoding="utf-8"))
    assert len(written["snapshots"]) == n_fridays
    assert written["endpoint_health"]["status"] == "ok"
    assert written["endpoint_unavailable"] == []
    assert written["carry_forwards"] == []
    assert written["source"] == fc.PRODUCT_DATA_API


def test_outage_run_emits_no_carry_forwards_and_exits_3(monkeypatch, tmp_path):
    """The core behavioural change.

    Before Phase 27 this run wrote a full set of snapshots, every one of them
    a carry-forward labelled "no holdings data within 5 days back from target
    Friday", and exited 0. A dead endpoint was indistinguishable from a run
    of public holidays.
    """
    out, n_fridays = _stub_run(monkeypatch, tmp_path)
    attempts: list[date] = []

    def dead(target, cfg):
        attempts.append(target)
        raise fc.EndpointUnavailable("HTTP 200 but body is not JSON")

    monkeypatch.setattr(fc, "fetch_product_data", dead)

    assert fc.main() == fc.EXIT_ENDPOINT_UNAVAILABLE

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["snapshots"] == {}
    assert written["carry_forwards"] == []          # nothing fabricated
    assert len(written["endpoint_unavailable"]) == n_fridays
    health = written["endpoint_health"]
    assert health["status"] == "unavailable"
    assert health["n_fridays_unavailable"] == n_fridays
    assert health["carry_forward_on_outage"] is False
    assert "not JSON" in health["detail"]
    # Short-circuit: one network attempt regardless of how many Fridays.
    assert len(attempts) == 1


def test_outage_with_opt_in_carry_forward_labels_the_cause(monkeypatch, tmp_path):
    """The escape hatch still runs, still exits 3, and still tells the truth
    about why each Friday is stale."""
    out, _ = _stub_run(monkeypatch, tmp_path, n_weeks=3, carry_forward=True)
    real = fc.latest_completed_friday(date.today())

    def flaky(target, cfg):
        # First Friday of the range succeeds, then the endpoint dies.
        if target <= real - __import__("datetime").timedelta(days=14):
            return _payload_with(
                asOfDate={"value": target.strftime("%Y%m%d")},
                ticker={"value": ["NVDA"]}, assetClass={"value": ["Equity"]})
        raise fc.EndpointUnavailable("connection reset")

    monkeypatch.setattr(fc, "fetch_product_data", flaky)

    assert fc.main() == fc.EXIT_ENDPOINT_UNAVAILABLE

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["carry_forwards"], "opt-in carry-forward produced nothing"
    causes = {cf["cause"] for cf in written["carry_forwards"]}
    assert causes == {"endpoint_unavailable"}
    for cf in written["carry_forwards"]:
        assert "endpoint unavailable" in cf["reason"]
        assert "within 5 days back" not in cf["reason"]


# ---------------------------------------------------------------------------
# 5. Live contract check — opt-in, guards against upstream drift
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("BREADTH_LIVE_API_TESTS") != "1",
    reason="live network test; set BREADTH_LIVE_API_TESTS=1 to run",
)
@pytest.mark.parametrize("symbol,stamp", [("CSP1", "20260710"),
                                           ("SOXX", "20260508")])
def test_live_endpoint_still_matches_fixture(symbol, stamp):
    """Run this when you suspect upstream changed again.

    The offline fixtures pin our parsing; this pins the endpoint itself.
    """
    cfg, csv_body, _ = _load(symbol, stamp)
    target = _stamp_to_date(stamp)
    payload = fc.fetch_product_data(target, cfg)
    live = fc.parse_holdings_json(
        payload, target, ticker_overrides=cfg.get("ticker_overrides", {}),
        apply_exchange_suffix=cfg.get("apply_exchange_suffix", False))
    expected = fc.parse_holdings(
        csv_body, ticker_overrides=cfg.get("ticker_overrides", {}),
        apply_exchange_suffix=cfg.get("apply_exchange_suffix", False))
    assert live == expected
