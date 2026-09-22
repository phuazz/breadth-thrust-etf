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
import os
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


def test_corrupt_sidecar_refuses_and_is_quarantined(monkeypatch, raw_dir):
    """FINDING 2. A damaged sidecar used to be deleted and read as absence.

    The old behaviour unlinked it and fell through to the endpoint, so a
    truncated file plus a quiet Friday plus a parseable Thursday produced an
    ordinary "walkback" with the only record of the refusal destroyed.
    """
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("{not json", encoding="utf-8")
    called = []
    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: called.append(t) or _payload_for(
                            FRI_2, [("A", "Equity", "Nasdaq")]))

    with pytest.raises(fc.RefusalEvidenceError) as exc:
        fc.load_snapshot_tickers(FRI_2, _cfg())

    assert called == [], "a corrupt sidecar must not fall through to the vendor"
    assert not path.exists(), "the corrupt file should have been moved aside"
    quarantined = list(raw_dir.glob("EXV1_20260918.refused.json.corrupt.*"))
    assert len(quarantined) == 1, "evidence must be quarantined, not deleted"
    assert quarantined[0].read_text(encoding="utf-8") == "{not json"
    assert exc.value.evidence_unreadable is True
    assert exc.value.as_of == FRI_2


def test_non_object_sidecar_refuses_and_is_quarantined(raw_dir):
    path = fc.refused_payload_path("EXV1", FRI_2)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(fc.RefusalEvidenceError):
        fc.load_snapshot_tickers(FRI_2, _cfg())
    assert list(raw_dir.glob("EXV1_20260918.refused.json.corrupt.*"))


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

    monkeypatch.setattr(fc, "fetch_product_data",
                        lambda t, c: pytest.fail("must not reach the vendor"))

    with pytest.raises(fc.UnmappedExchangeError):
        fc.load_snapshot_tickers(FRI_2, _cfg())


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
