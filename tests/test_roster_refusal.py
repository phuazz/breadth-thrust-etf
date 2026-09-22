"""A refused roster is its own failure class, not a vendor gap.

WHAT WENT WRONG. On 2026-09-18 (Friday) STOXX moved Greece from EM to DM and
four Greek banks entered SX7P. EXV1 — sleeve D's largest line — carried them on
a venue string the map did not hold, so report_unmapped_exchanges refused the
roster. The refusal propagated past get_snapshot into the walk's blanket
`except Exception`, became status "not_found", and was written as cause
"no_data_in_walkback" — the label a public holiday gets. The fetcher carried
the 2026-09-11 (Friday) roster forward, printed "Staleness OK", and exited 0.
Sleeve D ranked the fill week on it, and every guard was green.

These tests pin the four things that fix it:
  1. a refusal is classified, recorded and EXITS NON-ZERO;
  2. an ordinary vendor gap is unaffected and still soft;
  3. the refused vendor response is retained, so the date rebuilds from disk
     once the venue is mapped rather than from a vendor history window that
     may have closed;
  4. the state on disk is gated independently of the exit code.

Offline throughout: the transport is never touched, get_snapshot is stubbed,
and DATA_DIR / RAW_DIR are redirected into tmp_path.

Python datetime months are 1-indexed (January = 1). Calendar dates used below
were confirmed against the date library, not from memory:
  - 2026-09-11, 2026-09-18, 2026-09-25 are Fridays;
  - 2026-01-30 and 2026-02-27 are Fridays either side of a month boundary;
  - 2026-12-25 and 2027-01-01 are Fridays either side of a year boundary.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_refresh_guard as guard  # noqa: E402
import fetch_constituents as fc  # noqa: E402

FRI_1 = date(2026, 9, 11)
FRI_2 = date(2026, 9, 18)
FRI_3 = date(2026, 9, 25)

# The four Greek banks and the venue string that was not in the map on the
# day. ATHENS has since been mapped (commit 2b63dedb), so it is used only
# where the unmapped sink is supplied explicitly and the live map is not
# consulted. Tests that drive the real parser need a venue that is genuinely
# absent, which is what UNMAPPED_VENUE is for.
ATHENS = "Athens Exchange S.A. Cash Market"
GREEK = ["ALPHA", "ETE", "EUROB", "TPEIR"]

UNMAPPED_VENUE = "Nowhere Exchange PLC (test fixture, never mapped)"
MAPPED_VENUE = "London Stock Exchange"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_refusal(symbol="EXV1", as_of=FRI_2, venue=ATHENS, affected=GREEK,
                 n_equity=57):
    """Build the error the way production does, through the reporter.

    Raised via report_unmapped_exchanges rather than constructed directly, so
    these tests also pin that the raise path populates every attribute. A
    hand-built error would let the wiring rot while the tests stayed green.
    """
    try:
        fc.report_unmapped_exchanges({venue: list(affected)}, symbol,
                                     n_equity, as_of=as_of, strict=True)
    except fc.UnmappedExchangeError as exc:
        return exc
    raise AssertionError("report_unmapped_exchanges did not refuse")


def run_walk(monkeypatch, tmp_path, snapshots, *, start=FRI_1, end=FRI_2,
             etf_extra=None, symbol="EXV1"):
    """Drive fetch_constituents.main() offline over a short Friday range.

    ``snapshots`` maps a target Friday to what get_snapshot should do:
      - a list of tickers  -> a real capture on that Friday;
      - an Exception       -> raised out of get_snapshot;
      - None               -> status "not_found" (the healthy vendor gap).
    Returns (exit_code, payload_dict).
    """
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw_ishares"
    raw_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fc, "DATA_DIR", data_dir)
    monkeypatch.setattr(fc, "RAW_DIR", raw_dir)
    monkeypatch.setattr(fc, "PROJECT_ROOT", tmp_path)

    cfg = {"symbol": symbol, "start_friday": start,
           "apply_exchange_suffix": True, "product_id": "x",
           **(etf_extra or {})}
    monkeypatch.setattr(fc, "get_etf", lambda _sym: cfg)
    monkeypatch.setattr(fc, "latest_completed_friday", lambda _today: end)
    monkeypatch.setattr(fc, "parse_args",
                        lambda: type("A", (), {"etf": symbol,
                                               "carry_forward_on_outage": False})())

    def fake_get_snapshot(friday, etf_cfg, circuit=None, latency=None,
                          refresh=False):
        outcome = snapshots.get(friday, None)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return None, None, "not_found"
        return list(outcome), friday, "exact"

    monkeypatch.setattr(fc, "get_snapshot", fake_get_snapshot)

    code = fc.main()
    payload = json.loads(
        (data_dir / f"constituents_{symbol.lower()}.json").read_text("utf-8"))
    return code, payload


# ---------------------------------------------------------------------------
# The exception carries structured evidence, not a sentence to be re-parsed
# ---------------------------------------------------------------------------
def test_refusal_carries_structured_attributes():
    exc = make_refusal()
    assert exc.symbol == "EXV1"
    assert exc.as_of == FRI_2
    assert exc.exchanges == [ATHENS]
    assert exc.affected_symbols == sorted(GREEK)
    assert exc.n_affected == 4
    assert exc.n_equity_rows == 57
    assert exc.share == pytest.approx(4 / 57)


def test_refusal_record_names_both_dates():
    """The target Friday and the date actually attempted are both kept.

    They differ whenever the walkback had already stepped back, and a reader
    repairing the venue needs the source date while a reader auditing which
    week was affected needs the target.
    """
    rec = make_refusal(as_of=date(2026, 9, 17)).as_record(FRI_2)
    assert rec["target_friday"] == "2026-09-18"
    assert rec["source_date"] == "2026-09-17"
    assert rec["exchanges"] == [ATHENS]
    assert rec["affected_symbols"] == sorted(GREEK)
    assert rec["n_affected"] == 4
    assert rec["n_equity_rows"] == 57


def test_thresholds_are_unchanged():
    """Pinned: the fix must not have loosened the trigger it acts on."""
    assert fc.UNMAPPED_EXCHANGE_MAX_SHARE == 0.02
    assert fc.UNMAPPED_EXCHANGE_MIN_ROWS == 3


# ---------------------------------------------------------------------------
# Exit precedence
# ---------------------------------------------------------------------------
def test_exit_precedence_never_reports_success_with_a_refusal():
    for dead in (True, False):
        for stale in ("fresh", "warning", "critical"):
            for unexpected in (0, 1):
                code = fc.walk_exit_code(
                    endpoint_dead=dead, n_refusals=1,
                    n_unexpected=unexpected, staleness_status=stale)
                assert code != fc.EXIT_OK


def test_exit_precedence_order():
    w = fc.walk_exit_code
    # A dead transport outranks a refusal: it is why the refusal cannot even
    # be assessed. The refusal is still recorded and printed.
    assert w(endpoint_dead=True, n_refusals=1, n_unexpected=1,
             staleness_status="critical") == fc.EXIT_ENDPOINT_UNAVAILABLE
    # A refusal outranks an unclassified error and staleness, both of which
    # it can itself cause.
    assert w(endpoint_dead=False, n_refusals=1, n_unexpected=1,
             staleness_status="critical") == fc.EXIT_ROSTER_REFUSED
    assert w(endpoint_dead=False, n_refusals=0, n_unexpected=1,
             staleness_status="critical") == fc.EXIT_UNEXPECTED_WALK_ERROR
    assert w(endpoint_dead=False, n_refusals=0, n_unexpected=0,
             staleness_status="critical") == fc.EXIT_STALENESS_CRITICAL
    assert w(endpoint_dead=False, n_refusals=0, n_unexpected=0,
             staleness_status="fresh") == fc.EXIT_OK


def test_staleness_no_real_fetches_still_exits_zero_here():
    """Unchanged on purpose: widening that is a separate decision.

    check_refresh_guard's G3 fails "no_real_fetches"; the fetcher never has.
    This patch changes the refusal class, not the staleness class.
    """
    assert fc.walk_exit_code(endpoint_dead=False, n_refusals=0,
                             n_unexpected=0,
                             staleness_status="no_real_fetches") == fc.EXIT_OK


# ---------------------------------------------------------------------------
# The walk: refusal versus vendor gap
# ---------------------------------------------------------------------------
def test_newest_friday_refusal_fails_with_persisted_detail(monkeypatch,
                                                           tmp_path):
    """The 2026-09-18 EXV1 shape: good week, then a refused week."""
    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: [f"N{i}" for i in range(57)],
        FRI_2: make_refusal(),
    })

    assert code == fc.EXIT_ROSTER_REFUSED

    (rec,) = payload["roster_refusals"]
    assert rec["target_friday"] == "2026-09-18"
    assert rec["source_date"] == "2026-09-18"
    assert rec["exchanges"] == [ATHENS]
    assert rec["affected_symbols"] == sorted(GREEK)
    assert rec["n_affected"] == 4
    assert rec["n_equity_rows"] == 57

    # The carried-forward date is labelled by CAUSE, so a reader of the
    # payload can tell a refusal from a holiday without reading a log.
    (cf,) = payload["carry_forwards"]
    assert cf["target_friday"] == "2026-09-18"
    assert cf["cause"] == "roster_refused"
    assert cf["outcome"] == "carried_forward"

    # And the roster on disk really is the previous week's, which is the
    # thing that must never be published quietly.
    assert payload["snapshots"]["2026-09-18"]["carried_forward_from"] == \
        "2026-09-11"


def test_ordinary_vendor_gap_stays_soft(monkeypatch, tmp_path):
    """The control. A holiday still carries forward and still exits 0.

    If this ever fails, the fix has over-reached: the soft class is the whole
    reason the refusal class had to be separated out rather than the
    carry-forward simply being made fatal.
    """
    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: ["A", "B", "C"],
        FRI_2: None,
    })
    assert code == fc.EXIT_OK
    assert payload["roster_refusals"] == []
    assert payload["walk_errors"] == []
    (cf,) = payload["carry_forwards"]
    assert cf["cause"] == "no_data_in_walkback"


def test_historical_refusal_fails_even_though_a_later_friday_succeeds(
        monkeypatch, tmp_path):
    """A newer good snapshot must not clear an older refusal.

    The newest roster is healthy and fresh, so staleness, G1, G5 and W1 all
    read clean — exactly the state in which the refused middle week would
    otherwise disappear. That week's breadth was still computed on the wrong
    roster.
    """
    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: ["A", "B", "C"],
        FRI_2: make_refusal(as_of=FRI_2),
        FRI_3: ["A", "B", "C", "D"],
    }, end=FRI_3)

    assert code == fc.EXIT_ROSTER_REFUSED
    assert [r["target_friday"] for r in payload["roster_refusals"]] == \
        ["2026-09-18"]
    # The newest snapshot is a real capture and the roster reads fresh.
    assert "carried_forward_from" not in payload["snapshots"]["2026-09-25"]
    assert payload["staleness"]["status"] == "fresh"


def test_refusal_with_no_prior_snapshot_is_recorded_and_fails(monkeypatch,
                                                             tmp_path):
    """Nothing to carry forward, so no carry-forward record exists.

    This is the first of the three outcomes that write no carry_forwards
    entry, and the reason the refusal array is kept independently of it.
    """
    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: make_refusal(as_of=FRI_1),
        FRI_2: make_refusal(as_of=FRI_2),
    })
    assert code == fc.EXIT_ROSTER_REFUSED
    assert len(payload["roster_refusals"]) == 2
    assert payload["snapshots"] == {}
    # Recorded as skipped, never as a fabricated roster.
    assert all(cf["outcome"] == "skipped" for cf in payload["carry_forwards"])


def test_edgar_fallback_cannot_erase_the_refusal(monkeypatch, tmp_path):
    """EDGAR repairs the ROSTER; it does not repair the mapping.

    The second outcome that writes no carry-forward: EDGAR supplies a real
    snapshot over the refused Friday, so carry_forwards is silent and the
    snapshot looks like an ordinary capture. The venue is still unmapped and
    the run must still fail.
    """
    import edgar_nport

    class _Filing:
        report_period_end = "2026-09-18"
        filing_date = "2026-09-19"
        accession_number = "0001-23-456789"

    class _Roster:
        tickers = ["A", "B", "C", "D", "E"]
        filing = _Filing()

    monkeypatch.setattr(edgar_nport, "fetch_roster_via_edgar",
                        lambda *a, **k: _Roster(), raising=False)

    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: ["A", "B", "C"],
        FRI_2: make_refusal(as_of=FRI_2),
    }, etf_extra={"edgar_nport": {"cik": "1", "series_id": "S1"}})

    assert code == fc.EXIT_ROSTER_REFUSED
    assert len(payload["roster_refusals"]) == 1
    # EDGAR did serve the Friday — the refusal survived beside it.
    assert payload["snapshots"]["2026-09-18"]["source"] == "edgar_nport"
    assert payload["carry_forwards"] == []


def test_unexpected_exception_is_not_a_vendor_gap(monkeypatch, tmp_path):
    """The blanket handler used to call every unknown error a holiday."""
    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: ["A", "B", "C"],
        FRI_2: ValueError("something nobody predicted"),
    })
    assert code == fc.EXIT_UNEXPECTED_WALK_ERROR
    (err,) = payload["walk_errors"]
    assert err["target_friday"] == "2026-09-18"
    assert err["error_type"] == "ValueError"
    (cf,) = payload["carry_forwards"]
    assert cf["cause"] == "unexpected_error"
    assert cf["cause"] != "no_data_in_walkback"
    assert payload["roster_refusals"] == []


def test_endpoint_degraded_still_unwinds_and_writes_nothing(monkeypatch,
                                                           tmp_path):
    """cli() depends on this unwinding past the roster write."""
    from stall_guard import EndpointDegraded

    with pytest.raises(EndpointDegraded):
        run_walk(monkeypatch, tmp_path, {
            FRI_1: ["A", "B", "C"],
            FRI_2: EndpointDegraded("stalled"),
        })


# ---------------------------------------------------------------------------
# Retaining the refused vendor response
# ---------------------------------------------------------------------------
def _payload_for(as_of: date, rows):
    """A minimal product-data payload: (ticker, assetClass, exchange)."""
    return {"componentsByNameMap": {"holdings": {"containersByNameMap": {
        "all": {"dataPointsByNameMap": {
            "asOfDate": {"value": as_of.strftime("%Y%m%d")},
            "ticker": {"value": [r[0] for r in rows]},
            "assetClass": {"value": [r[1] for r in rows]},
            "exchange": {"value": [r[2] for r in rows]},
            "countryOfRisk": {"value": [None] * len(rows)},
        }}}}}}


def test_fixture_venues_are_what_the_tests_assume():
    """The fixture venue must be absent and the control venue present.

    Pinned because the tests below would otherwise fail OPEN: if
    UNMAPPED_VENUE were ever added to the map, the refusal would stop firing
    and every assertion about retention would silently test nothing. ATHENS
    itself was mapped on 2026-09-22, which is exactly how this was found.
    """
    assert UNMAPPED_VENUE not in fc._EXCHANGE_TO_YF_SUFFIX
    assert UNMAPPED_VENUE not in fc._EXCHANGE_ROUTE_UNAVAILABLE
    assert fc._EXCHANGE_TO_YF_SUFFIX.get(MAPPED_VENUE) == ".L"


def _rows_with_unmapped_venue(n_mapped=50, n_unmapped=4):
    rows = [(f"NAME{i}", "Equity", MAPPED_VENUE) for i in range(n_mapped)]
    rows += [(t, "Equity", UNMAPPED_VENUE) for t in GREEK[:n_unmapped]]
    return rows


@pytest.fixture
def raw_dir(monkeypatch, tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    monkeypatch.setattr(fc, "RAW_DIR", d)
    return d


def _cfg(symbol="EXV1"):
    return {"symbol": symbol, "apply_exchange_suffix": True, "product_id": "x"}


def test_refused_response_is_retained_and_rebuilds_after_a_mapping_repair(
        monkeypatch, raw_dir):
    """The core of the preservation fix, end to end.

    Run 1: the venue is unmapped, the fetch refuses, and the response is kept.
    Run 2: the venue is mapped and the transport is GONE — reconstruction must
    come off disk, because the vendor's history window closing is the exact
    scenario this exists for.
    """
    payload = _payload_for(FRI_2, _rows_with_unmapped_venue())
    calls = []

    def fake_fetch(target, etf_cfg):
        calls.append(target)
        return payload

    monkeypatch.setattr(fc, "fetch_product_data", fake_fetch)

    with pytest.raises(fc.UnmappedExchangeError):
        fc.load_snapshot_tickers(FRI_2, _cfg())

    sidecar = fc.refused_payload_path("EXV1", FRI_2)
    assert sidecar.exists(), "the refused response was discarded"
    assert not (raw_dir / "EXV1_20260918.json").exists(), \
        "a refused response must not become a positive holdings cache"

    # The venue is mapped, and the endpoint is now dead.
    monkeypatch.setitem(fc._EXCHANGE_TO_YF_SUFFIX, UNMAPPED_VENUE, ".AT")

    def dead_endpoint(target, etf_cfg):
        raise fc.EndpointUnavailable("history window closed")

    monkeypatch.setattr(fc, "fetch_product_data", dead_endpoint)

    rebuilt = fc.load_snapshot_tickers(FRI_2, _cfg())
    assert len(rebuilt) == 54
    assert "ALPHA.AT" in rebuilt

    # Promoted to the ordinary positive cache, and the sidecar retired.
    assert (raw_dir / "EXV1_20260918.json").exists()
    assert not sidecar.exists(), "a resolved refusal must not linger"


def test_successful_reconstruction_clears_the_active_refusal(monkeypatch,
                                                             tmp_path):
    """A repaired mapping empties roster_refusals, so the gate clears.

    The record must not be sticky: the payload is rewritten wholesale each
    run, and a guard that stayed red after the remedy is a guard nobody reads.
    """
    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: ["A", "B", "C"],
        FRI_2: make_refusal(),
    })
    assert code == fc.EXIT_ROSTER_REFUSED
    assert payload["roster_refusals"]

    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: ["A", "B", "C"],
        FRI_2: ["A", "B", "C", "ALPHA.AT"],
    })
    assert code == fc.EXIT_OK
    assert payload["roster_refusals"] == []
    assert payload["carry_forwards"] == []


def test_retained_response_is_not_overwritten_by_a_later_capture(raw_dir):
    """Evidence is written once: the capture contemporaneous with the record.

    A later DIFFERENT write would silently replace the response an existing
    refusal record was made against.
    """
    path = fc.refused_payload_path("EXV1", FRI_2)
    assert fc.retain_refused_payload(path, {"first": True}) is True
    assert fc.retain_refused_payload(path, {"second": True}) is False
    assert json.loads(path.read_text("utf-8")) == {"first": True}


def test_wrong_date_response_never_becomes_a_positive_cache(monkeypatch,
                                                            raw_dir):
    """Date parity holds, and no sidecar is written for a non-refusal.

    The API silently falls back to the LATEST available date for a weekend,
    holiday or future date. Accepting that would write today's roster into a
    historical Friday — a look-ahead bug.
    """
    monkeypatch.setattr(
        fc, "fetch_product_data",
        lambda t, c: _payload_for(FRI_3, _rows_with_unmapped_venue()))
    # Asked for FRI_2, served FRI_3: an empty result, not a roster.
    assert fc.load_snapshot_tickers(FRI_2, _cfg()) == []
    assert not (raw_dir / "EXV1_20260918.json").exists()
    assert not fc.refused_payload_path("EXV1", FRI_2).exists()


def test_empty_and_malformed_responses_are_unaffected(monkeypatch, raw_dir):
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(FRI_2, []))
    assert fc.load_snapshot_tickers(FRI_2, _cfg()) == []
    assert not fc.refused_payload_path("EXV1", FRI_2).exists()

    monkeypatch.setattr(fc, "fetch_product_data", lambda t, c: {"nope": 1})
    with pytest.raises(fc.PayloadContractError):
        fc.load_snapshot_tickers(FRI_2, _cfg())
    assert not fc.refused_payload_path("EXV1", FRI_2).exists(), \
        "a malformed payload must never be retained as a holdings response"


def test_negative_cache_behaviour_is_preserved(monkeypatch, raw_dir):
    """A settled no-data date still writes its marker; a recent one does not."""
    old = date(2020, 1, 3)  # a Friday, far older than the 30-day floor
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(old, []))
    assert fc.load_snapshot_tickers(old, _cfg()) == []
    marker = json.loads((raw_dir / "EXV1_20200103.json").read_text("utf-8"))
    assert marker["_no_holdings"] is True


def test_corrupt_sidecar_is_discarded_not_trusted(monkeypatch, raw_dir):
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        fc, "fetch_product_data",
        lambda t, c: _payload_for(FRI_2, [("A", "Equity", "Nasdaq")]))
    assert fc.load_snapshot_tickers(FRI_2, _cfg()) == ["A"]
    assert not path.exists()


@pytest.mark.parametrize("day,stamp", [
    # Month boundary: the last Friday of January and the last of February.
    (date(2026, 1, 30), "20260130"),
    (date(2026, 2, 27), "20260227"),
    # Year boundary: the last Friday of 2026 and the first of 2027.
    (date(2026, 12, 25), "20261225"),
    (date(2027, 1, 1), "20270101"),
])
def test_sidecar_path_stamps_across_month_and_year_boundaries(day, stamp,
                                                              raw_dir):
    """The only date formatting this patch introduces, pinned at both edges."""
    assert fc.refused_payload_path("EXV1", day).name == \
        f"EXV1_{stamp}.refused.json"
    rec = make_refusal(as_of=day).as_record(day)
    assert rec["source_date"] == day.isoformat()
    assert rec["target_friday"] == day.isoformat()


# ---------------------------------------------------------------------------
# G8 — the state-based gate
# ---------------------------------------------------------------------------
def test_g8_ok_when_no_panel_is_refused():
    (r,) = guard.check_roster_refusals({"CSP1": [], "EXV1": []})
    assert r["status"] == guard.OK


def test_g8_fails_and_names_the_venue():
    rec = make_refusal().as_record(FRI_2)
    (r,) = guard.check_roster_refusals({"EXV1": [rec]})
    assert r["status"] == guard.FAIL
    assert "EXV1" in r["check"]
    assert ATHENS in r["evidence"]
    assert "2026-09-18" in r["evidence"]


def test_g8_fails_a_historical_refusal_beside_a_healthy_newest_week():
    """The gate reads the array, not the newest snapshot."""
    rec = make_refusal(as_of=FRI_2).as_record(FRI_2)
    (r,) = guard.check_roster_refusals({"EXV1": [rec]})
    assert r["status"] == guard.FAIL


def test_g8_reads_an_absent_array_as_clean():
    """A payload written before 2026-09-22 has no array.

    Absence means "not recorded", which this gate cannot distinguish from
    "none happened" — which is exactly why the fetcher's exit code is the
    primary control and this is the secondary one.
    """
    assert guard.read_roster_refusals({}) == []
    assert guard.read_roster_refusals({"roster_refusals": None}) == []
    (r,) = guard.check_roster_refusals({"EXV1": []})
    assert r["status"] == guard.OK


def test_g8_is_wired_into_the_guard_run(monkeypatch, tmp_path):
    """The gate is reached by main(), not merely defined.

    A pure function nothing calls is the defect this whole class of bug is
    made of, so the wiring is pinned rather than assumed.
    """
    data = tmp_path / "data"
    data.mkdir()
    rec = make_refusal().as_record(FRI_2)
    for etf in guard.ETFS_ALL:
        blob = {"end_friday": "2026-09-18", "snapshots": {},
                "endpoint_health": {"status": "ok"},
                "staleness": {"status": "fresh"},
                "roster_refusals": [rec] if etf == "EXV1" else []}
        (data / f"constituents_{etf.lower()}.json").write_text(
            json.dumps(blob), encoding="utf-8")
        (data / f"breadth_{etf.lower()}.json").write_text(
            json.dumps({"end_date": "2026-09-18"}), encoding="utf-8")
    monkeypatch.setattr(guard, "DATA_DIR", data)

    rc = guard.main(["--end-friday", "2026-09-18"])
    assert rc == 1


# ---------------------------------------------------------------------------
# refresh_all's pre-calculation reuse of the same gate
# ---------------------------------------------------------------------------
def test_refresh_all_gate_catches_refused_state_on_disk(monkeypatch, tmp_path):
    """The skipped-fetch path: no fetch ran, so no exit code was produced.

    --skip-soxx-fetch, a cache-served re-run and a manual invocation all leave
    a refused roster on disk with nothing having failed in this process. The
    gate reads state, which is what survives the absence of an exit code.
    """
    import refresh_all

    data = tmp_path / "data"
    data.mkdir()
    rec = make_refusal(symbol="SOXX").as_record(FRI_2)
    for etf in refresh_all.ETFS_ALL:
        (data / f"constituents_{etf.lower()}.json").write_text(
            json.dumps({"roster_refusals": [rec] if etf == "SOXX" else []}),
            encoding="utf-8")
    monkeypatch.setattr(refresh_all, "REPO_ROOT", tmp_path)

    failures = refresh_all._check_refusals_on_disk(refresh_all.ETFS_ALL)
    assert failures and any("SOXX" in f for f in failures)

    # And it is silent when the rosters are clean.
    for etf in refresh_all.ETFS_ALL:
        (data / f"constituents_{etf.lower()}.json").write_text(
            json.dumps({"roster_refusals": []}), encoding="utf-8")
    assert refresh_all._check_refusals_on_disk(refresh_all.ETFS_ALL) == []


def test_refresh_all_gate_does_not_read_an_unreadable_panel_as_clean(
        monkeypatch, tmp_path):
    import refresh_all

    (tmp_path / "data").mkdir()
    monkeypatch.setattr(refresh_all, "REPO_ROOT", tmp_path)
    failures = refresh_all._check_refusals_on_disk(["CSP1"])
    assert failures == ["roster payload unreadable"]


# ---------------------------------------------------------------------------
# The scheduled run: evidence must outlive the rollback
# ---------------------------------------------------------------------------
def test_refusal_report_is_emitted_before_rollback(tmp_path):
    """data/constituents_*.json is TRACKED and is restored to HEAD on failure.

    The run log is under logs/, which is gitignored and survives, so the
    detail has to be copied into it while it still exists.
    """
    import scheduled_refresh

    data = tmp_path / "data"
    data.mkdir()
    rec = make_refusal().as_record(FRI_2)
    (data / "constituents_exv1.json").write_text(
        json.dumps({"etf": "EXV1", "roster_refusals": [rec]}), encoding="utf-8")
    (data / "constituents_csp1.json").write_text(
        json.dumps({"etf": "CSP1", "roster_refusals": []}), encoding="utf-8")

    report = scheduled_refresh.refusal_report(tmp_path)
    assert "EXV1" in report
    assert ATHENS in report
    assert "2026-09-18" in report
    assert "ALPHA" in report
    assert "raw_ishares" in report, \
        "the operator must be told the dates rebuild from disk"
    assert "CSP1" not in report


def test_refusal_report_is_empty_when_nothing_is_refused(tmp_path):
    import scheduled_refresh

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "constituents_csp1.json").write_text(
        json.dumps({"etf": "CSP1", "roster_refusals": []}), encoding="utf-8")
    assert scheduled_refresh.refusal_report(tmp_path) == ""


def test_refusal_report_survives_a_corrupt_payload(tmp_path):
    """A diagnostic must never replace the failure it is reporting on."""
    import scheduled_refresh

    data = tmp_path / "data"
    data.mkdir()
    (data / "constituents_exv1.json").write_text("{broken", encoding="utf-8")
    assert scheduled_refresh.refusal_report(tmp_path) == ""


def test_retained_evidence_is_gitignored_so_it_survives_the_rollback():
    """restore_tracked_outputs runs `git clean -fd` WITHOUT -x.

    The rollback therefore leaves ignored paths alone. If data/raw_ishares/
    ever stopped being ignored, the retained responses would be deleted by
    the cleanup that exists to make the next firing a retry — so the property
    is pinned here rather than assumed.
    """
    import inspect
    import subprocess

    import scheduled_refresh

    source = inspect.getsource(scheduled_refresh.restore_tracked_outputs)
    assert '"clean", "-fd"' in source, \
        "the rollback's clean invocation changed; re-check whether it now " \
        "removes ignored files"
    assert '"-x"' not in source, \
        "the rollback now cleans IGNORED files too, which would delete the " \
        "retained refused vendor responses under data/raw_ishares/"

    try:
        cp = subprocess.run(
            ["git", "check-ignore",
             "data/raw_ishares/EXV1_20260918.refused.json"],
            cwd=ROOT, capture_output=True, text=True)
    except OSError:  # pragma: no cover — git absent
        pytest.skip("git unavailable; the ignore rule cannot be checked here")
    if cp.returncode > 1:  # 0 = ignored, 1 = not ignored, >1 = no repo
        pytest.skip("not a git checkout; the ignore rule cannot be checked")
    assert cp.returncode == 0, (
        "data/raw_ishares/ is no longer gitignored; the rollback would now "
        "delete the retained refused responses")
