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

import inspect
import json
import os
import sys
from datetime import date, timedelta
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


def test_corrupt_sidecar_refuses_and_is_quarantined(monkeypatch, raw_dir):
    """FINDING 2. A damaged sidecar used to be deleted and read as absence.

    The old behaviour unlinked it and fell through to the endpoint, so a
    truncated file plus a quiet Friday plus a parseable Thursday produced an
    ordinary "walkback" with the only record of the refusal destroyed.
    """
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("{not json", encoding="utf-8")
    called = []
    # The endpoint has nothing for this date, so recovery cannot resolve it.
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: called.append(t) or _payload_for(
                            date(1990, 1, 1), []))

    with pytest.raises(fc.RefusalEvidenceError) as exc:
        fc.load_snapshot_tickers(FRI_2, _cfg())

    # The vendor IS asked — damaged local evidence is a reason to distrust the
    # file, not to stop seeking a correction. What must never happen is the
    # date degrading into an ordinary walkback.
    assert called == [FRI_2], "damaged evidence must still reach recovery"
    assert not path.exists(), "the corrupt file should have been moved aside"
    quarantined = list(raw_dir.glob("EXV1_20260918.refused.json.corrupt.*"))
    assert len(quarantined) == 1, "evidence must be quarantined, not deleted"
    assert quarantined[0].read_text(encoding="utf-8") == "{not json"
    assert exc.value.evidence_unreadable is True
    assert exc.value.as_of == FRI_2


def test_non_object_sidecar_refuses_and_is_quarantined(raw_dir, monkeypatch):
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))
    with pytest.raises(fc.RefusalEvidenceError):
        fc.load_snapshot_tickers(FRI_2, _cfg())
    assert list(raw_dir.glob("EXV1_20260918.refused.json.corrupt.*"))
    assert fc.unresolved_marker_path("EXV1", FRI_2).exists()


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
# FINDING 1 — an unresolved refusal is authoritative until the date resolves
#
# END TO END: these drive the REAL loader, the REAL get_snapshot and the REAL
# main(). Only fetch_product_data is stubbed. A stubbed get_snapshot cannot
# prove any of this, because the defect lived inside the resolution order the
# stub replaces.
# ---------------------------------------------------------------------------
def run_live_walk(monkeypatch, tmp_path, responses, *, start=FRI_1, end=FRI_2,
                  symbol="EXV1"):
    """main() over a short Friday range with ONLY the transport stubbed.

    ``responses`` maps a date to a payload, or to an Exception to raise, or to
    None meaning "the endpoint has nothing for this date". Anything absent
    from the mapping is also "nothing".
    Returns (exit_code, payload_dict, list_of_dates_fetched).
    """
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw_ishares"
    raw_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fc, "DATA_DIR", data_dir)
    monkeypatch.setattr(fc, "RAW_DIR", raw_dir)
    monkeypatch.setattr(fc, "PROJECT_ROOT", tmp_path)

    cfg = {"symbol": symbol, "start_friday": start,
           "apply_exchange_suffix": True, "product_id": "x"}
    monkeypatch.setattr(fc, "get_etf", lambda _s: cfg)
    monkeypatch.setattr(fc, "latest_completed_friday", lambda _t: end)
    monkeypatch.setattr(fc, "parse_args",
                        lambda: type("A", (), {"etf": symbol,
                                               "carry_forward_on_outage": False})())

    fetched: list[date] = []

    def fake_fetch(target, etf_cfg):
        fetched.append(target)
        outcome = responses.get(target)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            # The endpoint's own "no holdings here" shape: a date echo that
            # does not match what was asked for.
            return _payload_for(date(1990, 1, 1), [])
        return outcome

    monkeypatch.setattr(fc, "fetch_product_data", fake_fetch)
    code = fc.main()
    payload = json.loads(
        (data_dir / f"constituents_{symbol.lower()}.json").read_text("utf-8"))
    return code, payload, fetched


def test_retry_with_vendor_absence_does_not_clear_the_refusal(monkeypatch,
                                                              tmp_path):
    """FINDING 1, the reported reproduction, end to end.

    Run 1: the newest Friday refuses, is retained, exit 6.
    Run 2: the vendor now serves nothing for that Friday, and Thursday has a
    perfectly good cached roster. The old code took the Thursday walkback,
    wrote roster_refusals=[] and exited 0 with the sidecar still unresolved.
    """
    good_thursday = _payload_for(date(2026, 9, 17),
                                 [("A", "Equity", MAPPED_VENUE)])
    refusing_friday = _payload_for(FRI_2, _rows_with_unmapped_venue())

    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
        FRI_2: refusing_friday,
        date(2026, 9, 17): good_thursday,
    })
    assert code == fc.EXIT_ROSTER_REFUSED
    sidecar = tmp_path / "data" / "raw_ishares" / "EXV1_20260918.refused.json"
    assert sidecar.exists()

    # Run 2: the vendor has nothing for Friday. Thursday is cached and good.
    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
        FRI_2: None,
        date(2026, 9, 17): good_thursday,
    })

    assert code == fc.EXIT_ROSTER_REFUSED, \
        "vendor absence cleared an unresolved refusal"
    assert payload["roster_refusals"], "the refusal vanished from the payload"
    assert payload["roster_refusals"][0]["source_date"] == "2026-09-18"
    assert sidecar.exists(), "unresolved evidence was dropped"
    # And it must NOT have been rebuilt as an ordinary Thursday walkback.
    assert "2026-09-18" not in payload["snapshots"] or \
        payload["snapshots"]["2026-09-18"].get("carried_forward_from")


def test_older_positive_cache_does_not_mask_a_later_refusal(monkeypatch,
                                                            tmp_path):
    """FINDING 1, second half: revalidation creates cache + sidecar together.

    Date D resolves and is cached. A later revalidation of D serves a REVISED
    response that refuses. The sidecar is written while the earlier good cache
    is still in place, and the old resolution order served that cache ahead of
    the sidecar for every subsequent run.
    """
    raw = tmp_path / "data" / "raw_ishares"
    raw.mkdir(parents=True)
    monkeypatch.setattr(fc, "RAW_DIR", raw)

    # The earlier good capture for this date.
    good = _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)])
    (raw / "EXV1_20260918.json").write_text(json.dumps(good), encoding="utf-8")
    # ...and a refusal retained for the SAME date by a later revalidation.
    revised = _payload_for(FRI_2, _rows_with_unmapped_venue())
    fc.retain_refused_payload(fc.refused_payload_path("EXV1", FRI_2), revised)

    # The vendor IS contacted, to attempt recovery (finding 2). What must not
    # happen is the stale positive cache being served as if it were current.
    seen = []

    def endpoint(target, cfg):
        seen.append(target)
        return _payload_for(FRI_2, _rows_with_unmapped_venue())

    monkeypatch.setattr(fc, "fetch_product_data", endpoint)

    with pytest.raises(fc.UnmappedExchangeError):
        fc.load_snapshot_tickers(FRI_2, _cfg())
    assert seen == [FRI_2], "recovery must attempt the endpoint"


def test_refresh_true_revalidates_but_cannot_be_cleared_by_absence(
        monkeypatch, raw_dir):
    """Revalidation is preserved; it just cannot clear an unresolved refusal."""
    refusing = _payload_for(FRI_2, _rows_with_unmapped_venue())
    fc.retain_refused_payload(fc.refused_payload_path("EXV1", FRI_2), refusing)

    seen = []

    def endpoint(target, cfg):
        seen.append(target)
        return _payload_for(date(1990, 1, 1), [])   # nothing for this date

    monkeypatch.setattr(fc, "fetch_product_data", endpoint)
    with pytest.raises(fc.UnmappedExchangeError):
        fc.load_snapshot_tickers(FRI_2, _cfg(), refresh=True)
    assert seen == [FRI_2], "refresh=True must still ask the endpoint"


def test_refresh_true_resolves_when_the_vendor_serves_a_clean_response(
        monkeypatch, raw_dir):
    """The legitimate clearing path: the date actually resolves."""
    refusing = _payload_for(FRI_2, _rows_with_unmapped_venue())
    sidecar = fc.refused_payload_path("EXV1", FRI_2)
    fc.retain_refused_payload(sidecar, refusing)

    clean = _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE),
                                 ("B", "Equity", MAPPED_VENUE)])
    monkeypatch.setattr(fc, "fetch_product_data", lambda t, c: clean)

    assert fc.load_snapshot_tickers(FRI_2, _cfg(), refresh=True) == ["A.L", "B.L"]
    assert not sidecar.exists(), "a resolved date must clear its refusal"
    assert (raw_dir / "EXV1_20260918.json").exists()


def test_unresolved_sidecar_the_walk_never_visits_is_still_recorded(
        monkeypatch, tmp_path):
    """FINDING 1: the walk path is not a guarantee.

    A sidecar for a THURSDAY the walkback reached only once. Every later walk
    finds a good Friday and never looks at that Thursday again, so no walk
    step can record it. The end-of-walk reconciliation is what keeps it
    authoritative.
    """
    data_dir = tmp_path / "data"
    raw = data_dir / "raw_ishares"
    raw.mkdir(parents=True)
    fc.retain_refused_payload(
        raw / "EXV1_20260917.refused.json",
        _payload_for(date(2026, 9, 17), _rows_with_unmapped_venue()))

    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
        FRI_2: _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)]),
    })

    assert code == fc.EXIT_ROSTER_REFUSED
    assert [r["source_date"] for r in payload["roster_refusals"]] == \
        ["2026-09-17"]
    # Both Fridays captured cleanly; the refusal is the Thursday's alone.
    assert payload["snapshots"]["2026-09-18"]["n_tickers"] == 1


def test_reconciliation_does_not_double_count_a_recorded_refusal(monkeypatch,
                                                                 tmp_path):
    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
        FRI_2: _payload_for(FRI_2, _rows_with_unmapped_venue()),
    })
    assert code == fc.EXIT_ROSTER_REFUSED
    sources = [r["source_date"] for r in payload["roster_refusals"]]
    assert sources == ["2026-09-18"], f"double counted: {sources}"


def test_mapping_repair_resolves_and_the_gate_clears_end_to_end(monkeypatch,
                                                                tmp_path):
    """The full recovery, with the vendor no longer serving the date."""
    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
        FRI_2: _payload_for(FRI_2, _rows_with_unmapped_venue()),
    })
    assert code == fc.EXIT_ROSTER_REFUSED

    # Venue mapped; endpoint now dead for every date.
    monkeypatch.setitem(fc._EXCHANGE_TO_YF_SUFFIX, UNMAPPED_VENUE, ".AT")
    dead = fc.EndpointUnavailable("history window closed")
    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: dead, FRI_2: dead,
    })

    assert payload["roster_refusals"] == [], "the repair did not clear it"
    # Exit 0 with the endpoint DEAD is the point: the refused Friday was
    # rebuilt from its retained response and the earlier Friday from its
    # positive cache, so the vendor was never needed at all. This is the
    # property the retention exists for — recovery after the history window
    # has closed.
    assert code == fc.EXIT_OK
    assert payload["snapshots"]["2026-09-18"]["n_tickers"] == 54
    assert not (tmp_path / "data" / "raw_ishares"
                / "EXV1_20260918.refused.json").exists()


# ---------------------------------------------------------------------------
# FINDING 2 — evidence integrity
# ---------------------------------------------------------------------------
def test_retention_is_atomic_no_partial_file_is_left_as_evidence(monkeypatch,
                                                                 raw_dir):
    """An interrupted write must leave no sidecar, not a truncated one."""
    path = fc.refused_payload_path("EXV1", FRI_2)
    real_dump = json.dump

    def exploding_dump(obj, fh, *a, **k):
        fh.write('{"partial": ')
        raise OSError("disk full")

    monkeypatch.setattr(fc.json, "dump", exploding_dump)
    with pytest.raises(OSError):
        fc.retain_refused_payload(path, {"x": 1})
    assert not path.exists(), "a truncated file was left as evidence"
    assert not list(raw_dir.glob("*.tmp")), "temporary file not cleaned up"

    monkeypatch.setattr(fc.json, "dump", real_dump)
    assert fc.retain_refused_payload(path, {"x": 1}) is True


def test_storage_failure_reports_but_preserves_the_original_refusal(
        monkeypatch, raw_dir, capsys):
    """The refusal must survive a failure to store its evidence."""
    monkeypatch.setattr(
        fc, "fetch_product_data",
        lambda t, c: _payload_for(FRI_2, _rows_with_unmapped_venue()))

    def failing_retain(path, payload):
        raise OSError("read-only file system")

    monkeypatch.setattr(fc, "retain_refused_payload", failing_retain)

    with pytest.raises(fc.UnmappedExchangeError) as exc:
        fc.load_snapshot_tickers(FRI_2, _cfg())

    assert exc.value.evidence_retained is False
    assert "read-only file system" in (exc.value.evidence_error or "")
    assert exc.value.exchanges == [UNMAPPED_VENUE], \
        "the original refusal detail was lost"
    assert "EVIDENCE NOT RETAINED" in capsys.readouterr().err
    rec = exc.value.as_record(FRI_2)
    assert rec["evidence_retained"] is False and rec["evidence_error"]


def test_concurrent_writer_cannot_overwrite_existing_evidence(monkeypatch,
                                                              raw_dir):
    """Write-once under a race: the loser must not replace the winner."""
    path = fc.refused_payload_path("EXV1", FRI_2)
    real_fsync = os.fsync

    def racing_fsync(fd):
        # Another process claims the sidecar while this one is mid-write.
        if not path.exists():
            path.write_text(json.dumps({"winner": True}), encoding="utf-8")
        return real_fsync(fd)

    monkeypatch.setattr(fc.os, "fsync", racing_fsync)
    assert fc.retain_refused_payload(path, {"loser": True}) is False
    assert json.loads(path.read_text("utf-8")) == {"winner": True}
    assert not list(raw_dir.glob("*.tmp"))


def test_promotion_failure_keeps_the_retained_response(monkeypatch, raw_dir):
    """If the positive cache cannot be written, the evidence stays put."""
    sidecar = fc.refused_payload_path("EXV1", FRI_2)
    fc.retain_refused_payload(
        sidecar, _payload_for(FRI_2, _rows_with_unmapped_venue()))
    monkeypatch.setitem(fc._EXCHANGE_TO_YF_SUFFIX, UNMAPPED_VENUE, ".AT")

    def failing_write(path, payload):
        raise OSError("cannot write the positive cache")

    monkeypatch.setattr(fc, "_write_json_atomic", failing_write)
    with pytest.raises(OSError):
        fc.load_snapshot_tickers(FRI_2, _cfg())
    assert sidecar.exists(), "evidence lost when promotion failed"


# ---------------------------------------------------------------------------
# JSON retention eligibility, pinned behaviourally
# ---------------------------------------------------------------------------
def test_malformed_columns_beat_the_refusal_and_nothing_is_retained(raw_dir,
                                                                    monkeypatch):
    """A payload can be BOTH column-malformed and full of unmapped venues.

    The contract check must win, so the refusal never fires and nothing is
    retained. This pins the ordering the retention safety argument rests on:
    report_unmapped_exchanges runs last, after the contract, date-parity and
    column-length checks.
    """
    rows = _rows_with_unmapped_venue()
    bad = _payload_for(FRI_2, rows)
    dps = bad["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
        "dataPointsByNameMap"]
    dps["exchange"]["value"] = dps["exchange"]["value"][:-2]   # length mismatch

    monkeypatch.setattr(fc, "fetch_product_data", lambda t, c: bad)
    with pytest.raises(fc.PayloadContractError):
        fc.load_snapshot_tickers(FRI_2, _cfg())
    assert not fc.refused_payload_path("EXV1", FRI_2).exists()


def test_wrong_date_beats_the_refusal_and_nothing_is_retained(raw_dir,
                                                              monkeypatch):
    """Unmapped venues AND a wrong date echo: parity wins, no retention."""
    monkeypatch.setattr(
        fc, "fetch_product_data",
        lambda t, c: _payload_for(FRI_3, _rows_with_unmapped_venue()))
    assert fc.load_snapshot_tickers(FRI_2, _cfg()) == []
    assert not fc.refused_payload_path("EXV1", FRI_2).exists()
    assert not (raw_dir / "EXV1_20260918.json").exists()


def test_refusal_is_raised_after_the_checks_so_retained_payloads_are_valid(
        raw_dir, monkeypatch):
    """The positive case: a valid, date-correct, unmapped payload IS kept."""
    payload = _payload_for(FRI_2, _rows_with_unmapped_venue())
    monkeypatch.setattr(fc, "fetch_product_data", lambda t, c: payload)
    with pytest.raises(fc.UnmappedExchangeError):
        fc.load_snapshot_tickers(FRI_2, _cfg())
    kept = json.loads(
        fc.refused_payload_path("EXV1", FRI_2).read_text("utf-8"))
    dps = kept["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
        "dataPointsByNameMap"]
    assert dps["asOfDate"]["value"] == "20260918", \
        "a retained payload must be for the date it was retained under"


# ---------------------------------------------------------------------------
# EndpointDegraded: no payload is written, so the log must carry the evidence
# ---------------------------------------------------------------------------
def test_degraded_endpoint_prints_refusals_before_unwinding(monkeypatch,
                                                            tmp_path, capsys):
    """The one path that reaches no exit ladder and writes no array."""
    from stall_guard import EndpointDegraded

    data_dir = tmp_path / "data"
    (data_dir / "raw_ishares").mkdir(parents=True)
    monkeypatch.setattr(fc, "DATA_DIR", data_dir)
    monkeypatch.setattr(fc, "RAW_DIR", data_dir / "raw_ishares")
    monkeypatch.setattr(fc, "PROJECT_ROOT", tmp_path)
    cfg = {"symbol": "EXV1", "start_friday": FRI_1,
           "apply_exchange_suffix": True, "product_id": "x"}
    monkeypatch.setattr(fc, "get_etf", lambda _s: cfg)
    monkeypatch.setattr(fc, "latest_completed_friday", lambda _t: FRI_3)
    monkeypatch.setattr(fc, "parse_args",
                        lambda: type("A", (), {"etf": "EXV1",
                                               "carry_forward_on_outage": False})())

    state = {"n": 0}

    def snapshots(friday, etf_cfg, circuit=None, latency=None, refresh=False):
        state["n"] += 1
        if friday == FRI_1:
            raise make_refusal(as_of=FRI_1)
        if latency is not None:          # degrade after the refusal is seen
            latency.dead = True
            latency.reason = "stalled"
        return None, None, "not_found"

    monkeypatch.setattr(fc, "get_snapshot", snapshots)

    with pytest.raises(EndpointDegraded):
        fc.main()

    err = capsys.readouterr().err
    assert "ROSTER REFUSALS already encountered" in err
    assert ATHENS in err, "the refusal detail did not reach the log"
    assert not (data_dir / "constituents_exv1.json").exists(), \
        "a degraded endpoint must still write no roster"


def test_complete_records_are_printed_not_only_the_truncated_summary(
        monkeypatch, tmp_path, capsys):
    """Thirteen symbols: the summary truncates, the record must not."""
    many = [f"SYM{i:02d}" for i in range(13)]
    code, payload = run_walk(monkeypatch, tmp_path, {
        FRI_1: ["A", "B", "C"],
        FRI_2: make_refusal(affected=many, n_equity=100),
    })
    assert code == fc.EXIT_ROSTER_REFUSED
    err = capsys.readouterr().err
    assert "+1 more" in err, "the readable summary should stay concise"
    assert "complete refusal records:" in err
    assert all(s in err for s in many), "the complete record was truncated"
    assert payload["roster_refusals"][0]["affected_symbols"] == sorted(many)


# ===========================================================================
# SECOND-ROUND REVIEW FINDINGS (against 9dc30b89)
# ===========================================================================

# --- R2-F1 (CRITICAL): quarantine cleared the refusal on the next run ------
def test_quarantine_keeps_the_date_refused_across_consecutive_runs(
        monkeypatch, tmp_path):
    """Two runs, not one. The first quarantined; the SECOND exited 0.

    Reproduced against 9dc30b89: run 1 quarantined a corrupt sidecar and
    exited 6, which moved the evidence out of the loader's sight as well as
    out of harm's way. Run 2 found no sidecar, took the cached Thursday
    walkback, wrote roster_refusals=[] and printed "Staleness OK" — the
    2026-09-18 failure shape rebuilt one layer down, with no source date ever
    resolved.
    """
    raw = tmp_path / "data" / "raw_ishares"
    raw.mkdir(parents=True)
    (raw / "EXV1_20260918.refused.json").write_text("{truncated",
                                                    encoding="utf-8")
    good = {
        FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
        date(2026, 9, 17): _payload_for(date(2026, 9, 17),
                                        [("A", "Equity", MAPPED_VENUE)]),
    }

    code1, payload1, _ = run_live_walk(monkeypatch, tmp_path, dict(good))
    assert code1 == fc.EXIT_ROSTER_REFUSED
    assert len(payload1["roster_refusals"]) == 1
    assert list(raw.glob("*corrupt*")), "evidence was not quarantined"

    marker = raw / "EXV1_20260918.unresolved.json"
    assert marker.exists(), "quarantine left no durable unresolved marker"

    # THE RUN THAT USED TO GO GREEN.
    code2, payload2, _ = run_live_walk(monkeypatch, tmp_path, dict(good))
    assert code2 == fc.EXIT_ROSTER_REFUSED, \
        "the quarantine cleared the refusal on the second run"
    assert payload2["roster_refusals"], "the refusal vanished on run 2"
    assert payload2["roster_refusals"][0]["source_date"] == "2026-09-18"
    assert marker.exists()

    # A third run, to be sure it is durable rather than one-shot.
    code3, payload3, _ = run_live_walk(monkeypatch, tmp_path, dict(good))
    assert code3 == fc.EXIT_ROSTER_REFUSED
    assert payload3["roster_refusals"]


def test_marker_survives_a_scheduled_rollback_by_being_gitignored():
    """data/raw_ishares/ is ignored, and `git clean -fd` omits -x."""
    import subprocess

    import scheduled_refresh

    source = inspect.getsource(scheduled_refresh.restore_tracked_outputs)
    assert '"-x"' not in source
    try:
        cp = subprocess.run(
            ["git", "check-ignore",
             "data/raw_ishares/EXV1_20260918.unresolved.json"],
            cwd=ROOT, capture_output=True, text=True)
    except OSError:  # pragma: no cover
        pytest.skip("git unavailable")
    if cp.returncode > 1:
        pytest.skip("not a git checkout")
    assert cp.returncode == 0, "the unresolved marker would not survive rollback"


def test_marker_alone_keeps_a_date_refused_without_any_payload(raw_dir,
                                                               monkeypatch):
    """No sidecar at all — only the marker — and the date stays refused."""
    fc.write_unresolved_marker(
        fc.unresolved_marker_path("EXV1", FRI_2), "EXV1", FRI_2,
        "evidence quarantined", "EXV1_20260918.refused.json.corrupt.X")
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))
    with pytest.raises(fc.RefusalEvidenceError):
        fc.load_snapshot_tickers(FRI_2, _cfg())


# --- R2-F2: historical dates could never reach the endpoint to recover -----
def test_historical_issuer_correction_resolves_through_a_normal_refresh(
        monkeypatch, tmp_path):
    """refresh=True is set only for the newest Friday.

    Against 9dc30b89 an unresolved HISTORICAL Friday re-raised before the
    endpoint was contacted, so an issuer correction could never be seen and a
    quarantined date had no route back at all — a permanent refusal.
    """
    raw = tmp_path / "data" / "raw_ishares"
    raw.mkdir(parents=True)
    # FRI_1 is historical once end_friday is FRI_2.
    fc.RAW_DIR = raw
    fc.retain_refused_payload(
        raw / "EXV1_20260911.refused.json",
        _payload_for(FRI_1, _rows_with_unmapped_venue()))

    # The issuer corrects FRI_1: same date, now on a venue we do map.
    corrected = _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE),
                                     ("B", "Equity", MAPPED_VENUE)])
    code, payload, fetched = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: corrected,
        FRI_2: _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)]),
    })

    assert FRI_1 in fetched, "the historical date was never re-fetched"
    assert code == fc.EXIT_OK, f"refusal not cleared: {payload['roster_refusals']}"
    assert payload["roster_refusals"] == []
    assert payload["snapshots"]["2026-09-11"]["n_tickers"] == 2
    assert not (raw / "EXV1_20260911.refused.json").exists()


def test_quarantined_historical_date_recovers_through_the_endpoint(
        monkeypatch, tmp_path):
    """The marker-only case: nothing to re-parse, so the endpoint is the
    ONLY route. Without it the date would be refused for ever."""
    raw = tmp_path / "data" / "raw_ishares"
    raw.mkdir(parents=True)
    fc.RAW_DIR = raw
    fc.write_unresolved_marker(raw / "EXV1_20260911.unresolved.json",
                               "EXV1", FRI_1, "evidence quarantined", None)

    code, payload, fetched = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
        FRI_2: _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)]),
    })
    assert FRI_1 in fetched
    assert code == fc.EXIT_OK
    assert payload["roster_refusals"] == []
    assert not (raw / "EXV1_20260911.unresolved.json").exists()


def test_recovery_cannot_be_cleared_by_absence_or_transport_failure(
        monkeypatch, tmp_path):
    """Recovery, not suppression: only a date-correct parse clears it."""
    raw = tmp_path / "data" / "raw_ishares"
    raw.mkdir(parents=True)
    fc.RAW_DIR = raw
    fc.retain_refused_payload(
        raw / "EXV1_20260911.refused.json",
        _payload_for(FRI_1, _rows_with_unmapped_venue()))

    # Vendor absence for the unresolved date.
    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: None,
        FRI_2: _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)]),
    })
    assert code == fc.EXIT_ROSTER_REFUSED
    assert [r["source_date"] for r in payload["roster_refusals"]] == \
        ["2026-09-11"]

    # Transport failure for the unresolved date. The dead transport outranks
    # the refusal on the exit code, but the refusal is still RECORDED.
    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: fc.EndpointUnavailable("connection reset"),
        FRI_2: _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)]),
    })
    assert code == fc.EXIT_ENDPOINT_UNAVAILABLE
    assert payload["roster_refusals"], \
        "a transport failure silently cleared the refusal"


def test_offline_recovery_after_a_mapping_repair_still_works(monkeypatch,
                                                             tmp_path):
    """The primary remedy must not have been traded away for the new one."""
    raw = tmp_path / "data" / "raw_ishares"
    raw.mkdir(parents=True)
    fc.RAW_DIR = raw
    fc.retain_refused_payload(
        raw / "EXV1_20260911.refused.json",
        _payload_for(FRI_1, _rows_with_unmapped_venue()))
    monkeypatch.setitem(fc._EXCHANGE_TO_YF_SUFFIX, UNMAPPED_VENUE, ".AT")

    dead = fc.EndpointUnavailable("history window closed")
    code, payload, _ = run_live_walk(monkeypatch, tmp_path, {
        FRI_1: dead,
        FRI_2: _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)]),
    })
    assert payload["roster_refusals"] == [], "offline recovery regressed"
    assert payload["snapshots"]["2026-09-11"]["n_tickers"] == 54


# --- R2-F3: quarantine overwrote evidence on a same-second collision -------
def test_quarantine_does_not_overwrite_on_a_same_second_collision(
        monkeypatch, raw_dir):
    """Fixed clock, two different contents, both must survive."""
    class _FrozenClock(fc.datetime):
        @classmethod
        def now(cls, tz=None):
            return fc.datetime(2026, 9, 22, 13, 8, 59, tzinfo=fc.timezone.utc)

    monkeypatch.setattr(fc, "datetime", _FrozenClock)

    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("FIRST", encoding="utf-8")
    first = fc.quarantine_retained_payload(path)
    path.write_text("SECOND", encoding="utf-8")
    second = fc.quarantine_retained_payload(path)

    assert first and second and first != second, \
        f"same-second quarantines collided: {first} vs {second}"
    kept = sorted(f.read_text(encoding="utf-8")
                  for f in raw_dir.glob("*corrupt*"))
    assert kept == ["FIRST", "SECOND"], f"evidence lost: {kept}"


# --- R2-F4: malformed record fields still crashed the verdict formatter ----
@pytest.mark.parametrize("rec", [
    {"target_friday": "2026-09-18", "exchanges": [{}]},
    {"target_friday": "2026-09-18", "exchanges": [["nested"]]},
    {"target_friday": "2026-09-18", "exchanges": "Athens"},
    {"target_friday": "2026-09-18", "exchanges": [None]},
    {"target_friday": "2026-09-18", "affected_symbols": [{}]},
    {"target_friday": "2026-09-18", "affected_symbols": 7},
])
def test_malformed_record_fields_are_rejected_not_crashed(rec):
    """The reported payload crashed with "unhashable type: dict"."""
    with pytest.raises(guard.RefusalStateError):
        guard.read_roster_refusals({"roster_refusals": [rec]})


def test_both_guard_paths_fail_explicitly_on_the_reported_payload(monkeypatch,
                                                                  tmp_path):
    """The exact payload from the review, through BOTH gates."""
    import refresh_all

    bad = {"roster_refusals": [{"target_friday": "2026-09-18",
                                "exchanges": [{}]}]}

    data = tmp_path / "data"
    data.mkdir()
    for etf in guard.ETFS_ALL:
        blob = dict(bad) if etf == "EXV1" else {"roster_refusals": []}
        blob.update({"end_friday": "2026-09-18", "snapshots": {},
                     "endpoint_health": {"status": "ok"},
                     "staleness": {"status": "fresh"}})
        (data / f"constituents_{etf.lower()}.json").write_text(
            json.dumps(blob), encoding="utf-8")
        (data / f"breadth_{etf.lower()}.json").write_text(
            json.dumps({"end_date": "2026-09-18"}), encoding="utf-8")

    monkeypatch.setattr(guard, "DATA_DIR", data)
    assert guard.main(["--end-friday", "2026-09-18"]) == 1, \
        "the final guard did not FAIL on malformed record fields"

    monkeypatch.setattr(refresh_all, "REPO_ROOT", tmp_path)
    failures = refresh_all._check_refusals_on_disk(list(guard.ETFS_ALL))
    assert failures and any("EXV1" in f for f in failures), \
        "the pre-calculation gate did not FAIL on malformed record fields"


def test_valid_records_with_absent_optional_fields_still_pass():
    """Compatibility: optional fields may be absent, and None is allowed."""
    recs = guard.read_roster_refusals({"roster_refusals": [
        {"target_friday": "2026-09-18"},
        {"target_friday": "2026-09-11", "exchanges": None,
         "affected_symbols": None},
        {"target_friday": "2026-09-04", "exchanges": ["A"],
         "affected_symbols": ["X", "Y"]},
    ]})
    assert len(recs) == 3
    results = guard.check_roster_refusals({"EXV1": recs})
    assert all(r["status"] == guard.FAIL for r in results)


# ===========================================================================
# THIRD-ROUND REVIEW FINDINGS (against fccdb98e)
# ===========================================================================

def _deny_unlink_of(monkeypatch, name: str):
    """Deterministic fault injection: deleting one file is always refused."""
    real = os.unlink

    def denied(p, *a, **k):
        if str(p).endswith(name):
            raise PermissionError(f"{name} is locked by another handle")
        return real(p, *a, **k)

    monkeypatch.setattr(fc.os, "unlink", denied)
    return real


# --- R3-F1: quarantine deletion failure blocked recovery indefinitely ------
def test_undeletable_evidence_is_filed_once_and_recovery_still_runs(
        monkeypatch, raw_dir):
    """os.link succeeds, the unlink is denied.

    Against fccdb98e this produced one fresh quarantine copy per run for ever,
    left the corrupt source in place, reported it as moved, and made ZERO
    endpoint calls — so a corrected issuer response could never be fetched.
    """
    sidecar = fc.refused_payload_path("EXV1", FRI_2)
    sidecar.write_text("{truncated", encoding="utf-8")
    _deny_unlink_of(monkeypatch, "EXV1_20260918.refused.json")

    calls = []
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: calls.append(t) or _payload_for(
                            date(1990, 1, 1), []))

    for _ in range(3):
        with pytest.raises(fc.RefusalEvidenceError) as exc:
            fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    copies = list(raw_dir.glob("EXV1_20260918.refused.json.corrupt.*"))
    assert len(copies) == 1, f"evidence filed {len(copies)} times, not once"
    assert sidecar.exists(), "the fixture should still be undeletable"
    assert len(calls) == 3, "recovery never reached the endpoint"
    assert "remains at" in str(exc.value), \
        "the refusal claims the original was moved when it was not"
    assert fc.unresolved_marker_path("EXV1", FRI_2).exists()


def test_undeletable_evidence_still_recovers_from_a_corrected_response(
        monkeypatch, raw_dir):
    """The point of the fix: a corrected issuer response must get through."""
    sidecar = fc.refused_payload_path("EXV1", FRI_2)
    sidecar.write_text("{truncated", encoding="utf-8")
    _deny_unlink_of(monkeypatch, "EXV1_20260918.refused.json")
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(
                            FRI_2, [("A", "Equity", MAPPED_VENUE)]))

    assert fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2) == ["A.L"]
    assert not fc.unresolved_marker_path("EXV1", FRI_2).exists(), \
        "a resolved date must clear its marker"


def test_marker_is_written_before_the_evidence_is_moved(monkeypatch, raw_dir):
    """Quarantine-then-marker left NO active refusal state when the marker
    write failed: the payload had been renamed out of sight and nothing had
    replaced it. The damaged file must stay put instead."""
    sidecar = fc.refused_payload_path("EXV1", FRI_2)
    sidecar.write_text("{truncated", encoding="utf-8")
    monkeypatch.setattr(fc, "write_unresolved_marker",
                        lambda *a, **k: False)
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))

    with pytest.raises(fc.RefusalEvidenceError) as exc:
        fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    assert sidecar.exists(), \
        "evidence was destroyed while no durable marker existed"
    assert not list(raw_dir.glob("*corrupt*")), \
        "evidence must not be filed when the marker could not be written"
    assert "could NOT be written" in str(exc.value)


def test_marker_write_failure_still_leaves_unresolved_state_for_the_next_run(
        monkeypatch, tmp_path):
    """Across runs: the next run must still see the date as unresolved."""
    raw = tmp_path / "data" / "raw_ishares"
    raw.mkdir(parents=True)
    (raw / "EXV1_20260918.refused.json").write_text("{truncated",
                                                    encoding="utf-8")
    monkeypatch.setattr(fc, "write_unresolved_marker", lambda *a, **k: False)
    good = {FRI_1: _payload_for(FRI_1, [("A", "Equity", MAPPED_VENUE)]),
            date(2026, 9, 17): _payload_for(date(2026, 9, 17),
                                            [("A", "Equity", MAPPED_VENUE)])}

    code1, payload1, _ = run_live_walk(monkeypatch, tmp_path, dict(good))
    assert code1 == fc.EXIT_ROSTER_REFUSED
    # The damaged file is the only record, so it must have survived.
    assert (raw / "EXV1_20260918.refused.json").exists()

    code2, payload2, _ = run_live_walk(monkeypatch, tmp_path, dict(good))
    assert code2 == fc.EXIT_ROSTER_REFUSED, \
        "a failed marker write let the refusal lapse on the next run"
    assert payload2["roster_refusals"]


# --- R3-F2: invalid retained contract never reached recovery ---------------
def test_invalid_retained_contract_is_local_damage_not_an_outage(
        monkeypatch, raw_dir):
    """{"broken_contract": true} passes the is-a-dict check.

    Against fccdb98e it then raised PayloadContractError — the ENDPOINT-outage
    class — before recovery, on every retry, while a valid issuer response was
    available.
    """
    sidecar = fc.refused_payload_path("EXV1", FRI_2)
    sidecar.write_text(json.dumps({"broken_contract": True}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: calls.append(t) or _payload_for(
                            date(1990, 1, 1), []))

    for _ in range(2):
        with pytest.raises(fc.RefusalEvidenceError):
            fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    assert len(calls) == 2, "local corruption was treated as an outage"
    assert fc.unresolved_marker_path("EXV1", FRI_2).exists()
    assert len(list(raw_dir.glob("*corrupt*"))) == 1


def test_invalid_retained_contract_resolves_on_a_valid_correction(
        monkeypatch, raw_dir):
    fc.refused_payload_path("EXV1", FRI_2).write_text(
        json.dumps({"broken_contract": True}), encoding="utf-8")
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(
                            FRI_2, [("A", "Equity", MAPPED_VENUE)]))
    assert fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2) == ["A.L"]
    assert not fc.has_unresolved_refusal("EXV1", FRI_2)


def test_a_network_contract_error_is_still_an_outage(monkeypatch, raw_dir):
    """The distinction must cut one way only: a bad response from the
    ENDPOINT is still a transport problem."""
    fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", FRI_2),
                               "EXV1", FRI_2, "quarantined", None)
    monkeypatch.setattr(fc, "fetch_product_data", lambda t, c: {"nope": 1})
    with pytest.raises(fc.PayloadContractError):
        fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)


# --- R3-F3: reconciliation bypassed latency and outage controls ------------
def test_reconciliation_stops_calling_the_endpoint_after_an_outage(
        monkeypatch, raw_dir):
    """Three unresolved dates against a dead endpoint made three full
    attempts, each able to exhaust the retry ladder."""
    dates = [date(2026, 9, 4), FRI_1, FRI_2]
    for d in dates:
        fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", d),
                                   "EXV1", d, "quarantined", None)
    calls = []

    def dead(t, c):
        calls.append(t)
        raise fc.EndpointUnavailable("connection reset")

    monkeypatch.setattr(fc, "fetch_product_data", dead)
    circuit = fc.EndpointCircuit()
    recs = fc.unresolved_refusal_records("EXV1", _cfg(), set(),
                                         circuit=circuit)

    assert len(calls) == 1, f"kept calling a dead endpoint: {len(calls)} calls"
    assert len(recs) == 3, "later dates were not recorded after the outage"
    assert circuit.dead, "the outage was not established on the shared circuit"


def test_reconciliation_requests_are_latency_accounted(monkeypatch, raw_dir):
    """Recovery traffic is real traffic. Excluding it would let a slow
    endpoint hide behind the reconciliation pass."""
    from stall_guard import LatencyCircuit

    fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", FRI_2),
                               "EXV1", FRI_2, "quarantined", None)
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))
    latency = LatencyCircuit(label="test")
    fc.unresolved_refusal_records("EXV1", _cfg(), set(), latency=latency)
    assert latency.n_served == 1, \
        "the recovery request bypassed latency accounting"


def test_loader_recovery_is_latency_accounted_too(monkeypatch, raw_dir):
    from stall_guard import LatencyCircuit

    fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", FRI_2),
                               "EXV1", FRI_2, "quarantined", None)
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))
    latency = LatencyCircuit(label="test")
    with pytest.raises(fc.RosterRefusal):
        fc.load_snapshot_tickers(FRI_2, _cfg(), latency=latency)
    assert latency.n_served == 1


def test_latency_guard_is_not_weakened_by_recovery(monkeypatch, raw_dir):
    """Slow-but-successful recoveries may legitimately trip the guard.

    The promotions persist, so that is not a permanent denial — it is the
    guard doing its job, and the next run starts from the resolved state.
    """
    from stall_guard import LatencyCircuit

    for i in range(3):
        d = date(2026, 9, 4) + timedelta(days=7 * i)
        fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", d),
                                   "EXV1", d, "quarantined", None)

    def slow(t, c):
        return _payload_for(t, [("A", "Equity", MAPPED_VENUE)])

    monkeypatch.setattr(fc, "fetch_product_data", slow)
    latency = LatencyCircuit(label="test")
    recs = fc.unresolved_refusal_records("EXV1", _cfg(), set(),
                                         latency=latency)
    assert recs == [], "successful recoveries should clear every refusal"
    assert latency.n_served == 3
    # The resolutions are durable regardless of what the circuit decides.
    for i in range(3):
        d = date(2026, 9, 4) + timedelta(days=7 * i)
        assert not fc.has_unresolved_refusal("EXV1", d)


# --- R3-F4: the top-level payload root was never validated -----------------
@pytest.mark.parametrize("root", ["[]", "null", '"text"', "7", "true"])
def test_invalid_payload_root_is_rejected(root):
    with pytest.raises(guard.RefusalStateError):
        guard.read_roster_refusals(json.loads(root))


@pytest.mark.parametrize("root", ["[]", "null"])
def test_invalid_root_fails_through_the_pre_calculation_gate(monkeypatch,
                                                              tmp_path, root):
    """`[]` returned no failures; `null` raised TypeError."""
    import refresh_all

    data = tmp_path / "data"
    data.mkdir()
    (data / "constituents_csp1.json").write_text(root, encoding="utf-8")
    monkeypatch.setattr(refresh_all, "REPO_ROOT", tmp_path)
    failures = refresh_all._check_refusals_on_disk(["CSP1"])
    assert failures, f"root {root} read as clean"
    assert any("CSP1" in f for f in failures)


@pytest.mark.parametrize("root", ["[]", "null"])
def test_invalid_root_fails_through_the_final_guard(monkeypatch, tmp_path,
                                                     root):
    data = tmp_path / "data"
    data.mkdir()
    for etf in guard.ETFS_ALL:
        blob = root if etf == "EXV1" else json.dumps({
            "end_friday": "2026-09-18", "snapshots": {},
            "endpoint_health": {"status": "ok"},
            "staleness": {"status": "fresh"}, "roster_refusals": []})
        (data / f"constituents_{etf.lower()}.json").write_text(
            blob, encoding="utf-8")
        (data / f"breadth_{etf.lower()}.json").write_text(
            json.dumps({"end_date": "2026-09-18"}), encoding="utf-8")
    monkeypatch.setattr(guard, "DATA_DIR", data)
    assert guard.main(["--end-friday", "2026-09-18"]) == 1


def test_a_legacy_object_without_the_field_is_still_clean():
    """The deliberate compatibility must survive the root check."""
    assert guard.read_roster_refusals({"etf": "EXV1", "snapshots": {}}) == []


# ===========================================================================
# FOURTH-ROUND REVIEW FINDINGS (against 61b09318)
# ===========================================================================

# --- R4-F1: a recorded latency breach was never enforced -------------------
def _slow_clock(monkeypatch, seconds_per_call=13.0):
    """Deterministic timing: every fetch appears to take `seconds_per_call`."""
    state = {"n": 0}
    monkeypatch.setattr(fc.time, "monotonic",
                        lambda: state["n"] * seconds_per_call)

    def tick():
        state["n"] += 1

    return tick


def test_reconciliation_latency_breach_aborts_the_run(monkeypatch, tmp_path):
    """Thirteen slow recoveries set latency.dead — and the run exited 0.

    The walk tests latency.dead at the top of each Friday, but reconciliation
    runs AFTER the walk, so a breach raised by recovery traffic was recorded
    on the circuit and then ignored: against 61b09318 the run wrote the
    constituents payload and exited 0 on a transport it had itself declared
    degraded.
    """
    data_dir = tmp_path / "data"
    raw = data_dir / "raw_ishares"
    raw.mkdir(parents=True)
    monkeypatch.setattr(fc, "DATA_DIR", data_dir)
    monkeypatch.setattr(fc, "RAW_DIR", raw)
    monkeypatch.setattr(fc, "PROJECT_ROOT", tmp_path)
    cfg = {"symbol": "EXV1", "start_friday": FRI_1,
           "apply_exchange_suffix": True, "product_id": "x"}
    monkeypatch.setattr(fc, "get_etf", lambda _s: cfg)
    monkeypatch.setattr(fc, "latest_completed_friday", lambda _t: FRI_2)
    monkeypatch.setattr(fc, "parse_args",
                        lambda: type("A", (), {"etf": "EXV1",
                                               "carry_forward_on_outage": False})())

    for i in range(13):
        d = FRI_1 - timedelta(days=7 * (i + 1))
        fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", d),
                                   "EXV1", d, "quarantined", None)

    tick = _slow_clock(monkeypatch)
    calls = []

    def slow(target, cfg_):
        calls.append(target)
        tick()
        return _payload_for(target, [("A", "Equity", MAPPED_VENUE)])

    monkeypatch.setattr(fc, "fetch_product_data", slow)

    rc = fc.cli()
    assert rc == fc.EXIT_ENDPOINT_DEGRADED, \
        "a declared latency breach did not fail the run"
    assert not (data_dir / "constituents_exv1.json").exists(), \
        "a roster was written on a transport declared degraded"

    before = len(calls)
    assert before < 15, "recovery kept calling after the breach"


def test_successful_promotions_persist_across_a_degraded_abort(monkeypatch,
                                                                tmp_path):
    """The abort must not undo work that already resolved."""
    data_dir = tmp_path / "data"
    raw = data_dir / "raw_ishares"
    raw.mkdir(parents=True)
    monkeypatch.setattr(fc, "DATA_DIR", data_dir)
    monkeypatch.setattr(fc, "RAW_DIR", raw)
    monkeypatch.setattr(fc, "PROJECT_ROOT", tmp_path)
    cfg = {"symbol": "EXV1", "start_friday": FRI_1,
           "apply_exchange_suffix": True, "product_id": "x"}
    monkeypatch.setattr(fc, "get_etf", lambda _s: cfg)
    monkeypatch.setattr(fc, "latest_completed_friday", lambda _t: FRI_2)
    monkeypatch.setattr(fc, "parse_args",
                        lambda: type("A", (), {"etf": "EXV1",
                                               "carry_forward_on_outage": False})())
    # Dates are recovered OLDEST FIRST, so the oldest marker is the one
    # certain to have resolved before the breach aborts the run.
    resolved_date = FRI_1 - timedelta(days=7 * 13)
    for i in range(13):
        d = FRI_1 - timedelta(days=7 * (i + 1))
        fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", d),
                                   "EXV1", d, "quarantined", None)

    tick = _slow_clock(monkeypatch)

    def slow(target, cfg_):
        tick()
        return _payload_for(target, [("A", "Equity", MAPPED_VENUE)])

    monkeypatch.setattr(fc, "fetch_product_data", slow)
    assert fc.cli() == fc.EXIT_ENDPOINT_DEGRADED
    assert not fc.has_unresolved_refusal("EXV1", resolved_date), \
        "a resolution completed before the abort was lost"


def test_degraded_abort_prints_the_refusals_gathered_so_far(monkeypatch,
                                                             tmp_path, capsys):
    """No payload is written on that path, so the log is the only record."""
    data_dir = tmp_path / "data"
    raw = data_dir / "raw_ishares"
    raw.mkdir(parents=True)
    monkeypatch.setattr(fc, "DATA_DIR", data_dir)
    monkeypatch.setattr(fc, "RAW_DIR", raw)
    monkeypatch.setattr(fc, "PROJECT_ROOT", tmp_path)
    cfg = {"symbol": "EXV1", "start_friday": FRI_1,
           "apply_exchange_suffix": True, "product_id": "x"}
    monkeypatch.setattr(fc, "get_etf", lambda _s: cfg)
    monkeypatch.setattr(fc, "latest_completed_friday", lambda _t: FRI_2)
    monkeypatch.setattr(fc, "parse_args",
                        lambda: type("A", (), {"etf": "EXV1",
                                               "carry_forward_on_outage": False})())
    for i in range(13):
        d = FRI_1 - timedelta(days=7 * (i + 1))
        fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", d),
                                   "EXV1", d, "quarantined", None)

    tick = _slow_clock(monkeypatch)

    def slow_and_absent(target, cfg_):
        tick()
        return _payload_for(date(1990, 1, 1), [])   # nothing resolves

    monkeypatch.setattr(fc, "fetch_product_data", slow_and_absent)
    assert fc.cli() == fc.EXIT_ENDPOINT_DEGRADED
    err = capsys.readouterr().err
    assert "ROSTER REFUSALS already encountered" in err
    assert "complete refusal records" in err or "source_date" in err


# --- R4-F2: nested malformed retained data blocked recovery ----------------
def _payload_with_broken_datapoint(kind):
    p = _payload_for(FRI_2, [("A", "Equity", MAPPED_VENUE)])
    dps = p["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
        "dataPointsByNameMap"]
    if kind == "asofdate_list":
        dps["asOfDate"] = []
    elif kind == "ticker_not_object":
        dps["ticker"] = "nope"
    elif kind == "column_not_list":
        dps["exchange"] = {"value": "not-a-list"}
    return p


@pytest.mark.parametrize("kind", ["asofdate_list", "ticker_not_object",
                                  "column_not_list"])
def test_nested_malformed_retained_data_enters_damaged_recovery(
        monkeypatch, raw_dir, kind):
    """asOfDate of [] raised AttributeError — a class nobody handles.

    Against 61b09318 that propagated out of recovery before the endpoint was
    contacted, on every retry, while a valid correction was available.
    """
    fc.refused_payload_path("EXV1", FRI_2).write_text(
        json.dumps(_payload_with_broken_datapoint(kind)), encoding="utf-8")
    calls = []
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: calls.append(t) or _payload_for(
                            date(1990, 1, 1), []))

    for _ in range(2):
        with pytest.raises(fc.RefusalEvidenceError):
            fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    assert len(calls) == 2, "malformed nested data blocked recovery"
    assert fc.unresolved_marker_path("EXV1", FRI_2).exists()


@pytest.mark.parametrize("kind", ["asofdate_list", "ticker_not_object",
                                  "column_not_list"])
def test_nested_malformed_retained_data_resolves_on_a_correction(
        monkeypatch, raw_dir, kind):
    fc.refused_payload_path("EXV1", FRI_2).write_text(
        json.dumps(_payload_with_broken_datapoint(kind)), encoding="utf-8")
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(
                            FRI_2, [("A", "Equity", MAPPED_VENUE)]))
    assert fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2) == ["A.L"]
    assert not fc.has_unresolved_refusal("EXV1", FRI_2)


def test_malformed_shape_is_a_contract_error_not_a_swallowed_exception():
    """Validated explicitly, so genuine programming errors are NOT swallowed."""
    with pytest.raises(fc.PayloadContractError):
        fc.parse_holdings_json(
            _payload_with_broken_datapoint("asofdate_list"), FRI_2,
            apply_exchange_suffix=True, symbol="EXV1")


# --- R4-F3: "already filed" did not establish evidence identity ------------
def test_a_different_response_at_the_same_path_is_archived_separately(
        monkeypatch, raw_dir):
    """Recovery legitimately retains a LATER refusing issuer response.

    If that later response is itself damaged, the marker already named a
    quarantine, so against 61b09318 the second file was deleted without ever
    being archived. Two different responses, one surviving copy.
    """
    path = fc.refused_payload_path("EXV1", FRI_2)
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))

    path.write_text("FIRST-DAMAGED", encoding="utf-8")
    with pytest.raises(fc.RefusalEvidenceError):
        fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    path.write_text("SECOND-DIFFERENT-DAMAGED", encoding="utf-8")
    with pytest.raises(fc.RefusalEvidenceError):
        fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    kept = sorted(f.read_text(encoding="utf-8")
                  for f in raw_dir.glob("*corrupt*"))
    assert kept == ["FIRST-DAMAGED", "SECOND-DIFFERENT-DAMAGED"], \
        f"distinct evidence was lost: {kept}"


def test_the_same_bytes_re_presented_are_not_archived_twice(monkeypatch,
                                                             raw_dir):
    """The other half: identical content must not multiply copies."""
    path = fc.refused_payload_path("EXV1", FRI_2)
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))
    for _ in range(3):
        path.write_text("SAME-DAMAGED", encoding="utf-8")
        with pytest.raises(fc.RefusalEvidenceError):
            fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)
    assert len(list(raw_dir.glob("*corrupt*"))) == 1


# --- R4-F4: failed outcome persistence defeated file-once ------------------
def test_file_once_holds_when_both_unlink_and_marker_rewrite_fail(
        monkeypatch, raw_dir):
    """Deletion denied AND the outcome rewrite denied.

    Against 61b09318 the marker never recorded the quarantine, so
    "already filed" was False every run and three attempts made three copies.
    Identity is now read from the archives on disk, which no failed write can
    erase.
    """
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("{truncated", encoding="utf-8")
    _deny_unlink_of(monkeypatch, "EXV1_20260918.refused.json")

    real_atomic = fc._write_json_atomic

    def deny_rewrite(p, payload):
        if p.name.endswith(".unresolved.json") and p.exists():
            raise OSError("outcome rewrite denied")
        return real_atomic(p, payload)

    monkeypatch.setattr(fc, "_write_json_atomic", deny_rewrite)
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))

    for _ in range(3):
        with pytest.raises(fc.RefusalEvidenceError):
            fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    copies = list(raw_dir.glob("*corrupt*"))
    assert len(copies) == 1, f"file-once broke: {len(copies)} copies"
    assert fc.unresolved_marker_path("EXV1", FRI_2).exists(), \
        "refusal persistence must still hold"


def test_archive_identity_is_recoverable_without_the_marker(raw_dir):
    """find_archived_evidence answers from disk, not from recorded state."""
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("EVIDENCE", encoding="utf-8")
    name, removed = fc.quarantine_retained_payload(path)
    assert name and removed
    path.write_text("EVIDENCE", encoding="utf-8")   # same bytes re-presented
    assert fc.find_archived_evidence(path, fc._file_sha256(path)) == name
    path.write_text("DIFFERENT", encoding="utf-8")
    assert fc.find_archived_evidence(path, fc._file_sha256(path)) is None


# --- Related small corrections ---------------------------------------------
def test_an_unreadable_marker_is_rebuilt_with_its_identifying_fields(
        monkeypatch, raw_dir):
    """It used to be reduced to two outcome flags."""
    marker = fc.unresolved_marker_path("EXV1", FRI_2)
    marker.write_text("{not json", encoding="utf-8")
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("{truncated", encoding="utf-8")
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: _payload_for(date(1990, 1, 1), []))

    with pytest.raises(fc.RefusalEvidenceError):
        fc.attempt_refusal_recovery("EXV1", _cfg(), FRI_2)

    rebuilt = json.loads(marker.read_text(encoding="utf-8"))
    assert rebuilt["symbol"] == "EXV1"
    assert rebuilt["source_date"] == "2026-09-18"
    assert rebuilt["reason"] and "2026-09-18" in rebuilt["reason"]
    assert rebuilt["first_seen_utc"]


def test_refusal_report_isolates_one_malformed_file_from_the_others(tmp_path):
    """A root of [] discarded the ENTIRE report through _safe."""
    import scheduled_refresh

    data = tmp_path / "data"
    data.mkdir()
    (data / "constituents_exv1.json").write_text("[]", encoding="utf-8")
    rec = make_refusal(symbol="CSP1").as_record(FRI_2)
    (data / "constituents_csp1.json").write_text(
        json.dumps({"etf": "CSP1", "roster_refusals": [rec]}),
        encoding="utf-8")

    report = scheduled_refresh.refusal_report(tmp_path)
    assert "CSP1" in report and ATHENS in report, \
        "a valid panel's refusals were discarded by a malformed neighbour"
    assert "MALFORMED" in report and "constituents_exv1.json" in report


def test_refusal_report_isolates_one_malformed_record(tmp_path):
    import scheduled_refresh

    data = tmp_path / "data"
    data.mkdir()
    good = make_refusal(symbol="EXV1").as_record(FRI_2)
    (data / "constituents_exv1.json").write_text(
        json.dumps({"etf": "EXV1", "roster_refusals": ["not a record", good]}),
        encoding="utf-8")
    report = scheduled_refresh.refusal_report(tmp_path)
    assert ATHENS in report, "the valid record was lost"
    assert "MALFORMED" in report


def test_reconciliation_outage_message_does_not_claim_to_stop_the_walk(
        monkeypatch, raw_dir, capsys):
    """The walk is over by then; saying it is short-circuited is false."""
    fc.write_unresolved_marker(fc.unresolved_marker_path("EXV1", FRI_2),
                               "EXV1", FRI_2, "quarantined", None)

    def dead(t, c):
        raise fc.EndpointUnavailable("connection reset")

    monkeypatch.setattr(fc, "fetch_product_data", dead)
    circuit = fc.EndpointCircuit()
    fc.unresolved_refusal_records("EXV1", _cfg(), set(), circuit=circuit)
    out = capsys.readouterr().out
    assert "Short-circuiting the remaining Fridays" not in out
    assert "no further endpoint calls" in out
    assert circuit.dead, "exit 3 and failed endpoint health remain correct"


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
    primary control and this is the secondary one. That compatibility is
    deliberate and is kept.
    """
    assert guard.read_roster_refusals({}) == []
    assert guard.read_roster_refusals({"etf": "EXV1"}) == []
    (r,) = guard.check_roster_refusals({"EXV1": []})
    assert r["status"] == guard.OK


# --- FINDING 4: malformed refusal state must not read as clean -------------
@pytest.mark.parametrize("value", [
    None,                                   # present but null
    {},                                     # empty dict — falsy, the trap
    {"2026-09-18": ["ALPHA"]},              # non-empty dict
    "none",                                 # a string
    0,
])
def test_malformed_refusal_field_is_rejected(value):
    """FINDING 4. `[] if not isinstance(list) else ...` read all of these clean.

    The non-empty dict is the one that matters: a plausible shape for
    hand-edited or half-migrated state, carrying real refusals, that produced
    a clean G8.
    """
    with pytest.raises(guard.RefusalStateError):
        guard.read_roster_refusals({"roster_refusals": value})


@pytest.mark.parametrize("records", [
    ["not a dict"],
    [{"exchanges": ["X"]}],                 # no target_friday
    [{"target_friday": None}],
    [{"target_friday": ""}],
    [{"target_friday": 20260918}],          # not a string
])
def test_malformed_refusal_records_are_rejected(records):
    with pytest.raises(guard.RefusalStateError):
        guard.read_roster_refusals({"roster_refusals": records})


def test_g8_fails_on_unreadable_refusal_state():
    """Malformed state FAILS; it is not silently absent and not a traceback."""
    results = guard.check_roster_refusals(
        {"CSP1": []}, {"EXV1": "roster_refusals is dict, expected a list"})
    assert any(r["status"] == guard.FAIL and "EXV1" in r["check"]
               for r in results)


def test_guard_main_fails_on_malformed_refusal_state(monkeypatch, tmp_path):
    """End to end through the final guard, not just the pure function."""
    data = tmp_path / "data"
    data.mkdir()
    for etf in guard.ETFS_ALL:
        blob = {"end_friday": "2026-09-18", "snapshots": {},
                "endpoint_health": {"status": "ok"},
                "staleness": {"status": "fresh"},
                "roster_refusals": ({"2026-09-18": ["ALPHA"]}
                                    if etf == "EXV1" else [])}
        (data / f"constituents_{etf.lower()}.json").write_text(
            json.dumps(blob), encoding="utf-8")
        (data / f"breadth_{etf.lower()}.json").write_text(
            json.dumps({"end_date": "2026-09-18"}), encoding="utf-8")
    monkeypatch.setattr(guard, "DATA_DIR", data)
    assert guard.main(["--end-friday", "2026-09-18"]) == 1


def test_refresh_all_gate_fails_on_malformed_refusal_state(monkeypatch,
                                                           tmp_path):
    """And through the pre-calculation path, which reads the same payloads."""
    import refresh_all

    data = tmp_path / "data"
    data.mkdir()
    for etf in refresh_all.ETFS_ALL:
        (data / f"constituents_{etf.lower()}.json").write_text(
            json.dumps({"roster_refusals": ({"2026-09-18": ["ALPHA"]}
                                            if etf == "SOXX" else [])}),
            encoding="utf-8")
    monkeypatch.setattr(refresh_all, "REPO_ROOT", tmp_path)
    failures = refresh_all._check_refusals_on_disk(refresh_all.ETFS_ALL)
    assert failures and any("SOXX" in f for f in failures)


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


# --- FINDING 3: the gate must run BEFORE that panel's compute_breadth ------
def _drive_refresh_all(monkeypatch, tmp_path, refused, argv):
    """Run refresh_all.main() with every subprocess step recorded, not run.

    Calling the helper directly cannot establish ORDERING, which is the whole
    of finding 3 — the gate existed, it simply ran a step too late.
    """
    import refresh_all

    data = tmp_path / "data"
    data.mkdir()
    rec = make_refusal(symbol="SOXX").as_record(FRI_2)
    for etf in refresh_all.ETFS_REFRESH:
        (data / f"constituents_{etf.lower()}.json").write_text(
            json.dumps({"roster_refusals": [rec] if etf in refused else []}),
            encoding="utf-8")
    monkeypatch.setattr(refresh_all, "REPO_ROOT", tmp_path)

    steps: list[str] = []

    def fake_run_step(label, cmd, cwd=None, timeout_s=None):
        steps.append(label)
        return True, 0.0

    monkeypatch.setattr(refresh_all, "run_step", fake_run_step)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", *argv])
    rc = refresh_all.main()
    return rc, steps


def test_compute_breadth_never_runs_for_a_refused_panel(monkeypatch, tmp_path):
    """The reported reproduction: --skip-soxx-fetch over a refused SOXX."""
    rc, steps = _drive_refresh_all(
        monkeypatch, tmp_path, refused={"SOXX"},
        argv=["--skip-soxx-fetch", "--no-tests", "--throttle", "0"])

    assert rc == 1
    soxx_breadth = [s for s in steps
                    if "compute_breadth" in s and "SOXX" in s]
    assert soxx_breadth == [], \
        f"compute_breadth ran on a refused roster: {soxx_breadth}"
    # The run stopped at capture; no engine or page step was reached.
    assert not any("Strategy" in s or "pipeline" in s for s in steps)


def test_a_clean_panel_still_computes_breadth(monkeypatch, tmp_path):
    """The control: the gate must not block panels that are fine."""
    rc, steps = _drive_refresh_all(
        monkeypatch, tmp_path, refused=set(),
        argv=["--skip-soxx-fetch", "--no-tests", "--throttle", "0"])
    assert any("compute_breadth" in s and "SOXX" in s for s in steps)
    assert rc == 0 or any("Strategy" in s for s in steps)


def test_one_refused_panel_does_not_block_the_others_computing(monkeypatch,
                                                               tmp_path):
    """Scope check: the refusal stops its own panel, not every panel."""
    _, steps = _drive_refresh_all(
        monkeypatch, tmp_path, refused={"SOXX"},
        argv=["--skip-soxx-fetch", "--no-tests", "--throttle", "0"])
    assert any("compute_breadth" in s and "CSP1" in s for s in steps)


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
    """A diagnostic must never replace the failure it is reporting on.

    It no longer goes silent either: a malformed payload is NAMED. Dropping
    it quietly is how a bad file could hide beside good ones.
    """
    import scheduled_refresh

    data = tmp_path / "data"
    data.mkdir()
    (data / "constituents_exv1.json").write_text("{broken", encoding="utf-8")
    report = scheduled_refresh.refusal_report(tmp_path)
    assert "MALFORMED" in report and "constituents_exv1.json" in report


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
