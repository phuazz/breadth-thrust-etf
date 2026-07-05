"""Tests for scripts/edgar_nport.py (SEC EDGAR N-PORT-P fallback).

These tests do NOT hit the live SEC EDGAR or OpenFIGI APIs — they use
monkeypatched HTTP clients and a fixed XML fixture so they run in
~50ms locally and never break in CI when SEC/OpenFIGI is down.

Coverage:
- _pad_cik / _strip_acc helpers (CIK and accession-number formatting)
- find_filing_for_series matches on <seriesId> and skips non-matches
- fetch_holdings_from_filing parses the XML correctly
- resolve_tickers respects the OpenFIGI cache and prefers US-listed
- US_EXCHANGE_CODES contains the codes we actually use
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from edgar_nport import (  # noqa: E402
    EdgarHolding,
    US_EXCHANGE_CODES,
    _openfigi_batch,
    _pad_cik,
    _strip_acc,
    fetch_holdings_from_filing,
    find_filing_for_series,
    list_series_nport_filings,
    resolve_tickers,
)


# ---------------------------------------------------------------------------
# Helper formatting
# ---------------------------------------------------------------------------


def test_pad_cik_zero_pads_to_ten_digits():
    assert _pad_cik("1100663") == "0001100663"
    assert _pad_cik(1100663) == "0001100663"
    assert _pad_cik("0001100663") == "0001100663"
    assert _pad_cik("0000930667") == "0000930667"


def test_strip_acc_removes_hyphens():
    assert _strip_acc("0002071691-26-012504") == "000207169126012504"
    assert _strip_acc("000207169126012504") == "000207169126012504"


# ---------------------------------------------------------------------------
# US exchange code set
# ---------------------------------------------------------------------------


def test_us_exchange_codes_includes_nyse_nasdaq_amex():
    """If anyone removes a code from US_EXCHANGE_CODES, this test fails
    and reminds them why each is included."""
    assert "UN" in US_EXCHANGE_CODES, "UN is NYSE primary listing"
    assert "UQ" in US_EXCHANGE_CODES, "UQ is NASDAQ primary listing"
    assert "UA" in US_EXCHANGE_CODES, "UA is AMEX primary listing"
    assert "UF" in US_EXCHANGE_CODES, "UF is NYSE Arca"
    assert "US" in US_EXCHANGE_CODES, "US is OpenFIGI's generic US tag"


# ---------------------------------------------------------------------------
# find_filing_for_series — series matching
# ---------------------------------------------------------------------------


def _make_response(status_code: int, text: str = "", json_payload=None):
    """Build a mock requests.Response with status + text + .json()."""
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.raise_for_status = MagicMock()
    if status_code != 200:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    if json_payload is not None:
        r.json = MagicMock(return_value=json_payload)
    return r


# browse-edgar series view (atom): one <entry> per filing, most recent
# first, each carrying a <filing-href> whose 18-digit folder is the
# accession without dashes.
BROWSE_ATOM = (
    '<?xml version="1.0" encoding="UTF-8"?><feed>'
    '<entry><filing-type>NPORT-P</filing-type>'
    '<filing-date>2026-05-28</filing-date>'
    '<filing-href>https://www.sec.gov/Archives/edgar/data/1100663/'
    '000207169126012504/0002071691-26-012504-index.htm</filing-href>'
    '</entry>'
    '<entry><filing-type>NPORT-P</filing-type>'
    '<filing-date>2026-02-25</filing-date>'
    '<filing-href>https://www.sec.gov/Archives/edgar/data/1100663/'
    '000207169126004253/0002071691-26-004253-index.htm</filing-href>'
    '</entry>'
    '</feed>'
)


def _series_xml(series_id: str, name: str, rep_end: str = "2026-03-31") -> str:
    """Minimal N-PORT-P primary_doc.xml with the tags find_filing reads."""
    return (
        '<?xml version="1.0"?><edgarSubmission>'
        '<headerData><seriesClassInfo>'
        f'<seriesId>{series_id}</seriesId>'
        '</seriesClassInfo></headerData>'
        '<formData><genInfo>'
        f'<seriesName>{name}</seriesName>'
        f'<repPdEnd>{rep_end}</repPdEnd>'
        '</genInfo></formData></edgarSubmission>'
    )


def test_list_series_nport_filings_parses_accession_and_date():
    """The series view returns one <entry> per filing; the accession is
    recovered from the <filing-href> folder (18-digit no-dash) and
    re-hyphenated, in most-recent-first order."""
    with patch("edgar_nport.requests.get") as mock_get:
        mock_get.return_value = _make_response(200, text=BROWSE_ATOM)
        filings = list_series_nport_filings("S000004354")
    assert len(filings) == 2
    assert filings[0]["accession_nodash"] == "000207169126012504"
    assert filings[0]["accession"] == "0002071691-26-012504"
    assert filings[0]["filing_date"] == "2026-05-28"
    assert filings[1]["accession"] == "0002071691-26-004253"


def test_find_filing_for_series_matches_target_series():
    """find_filing_for_series walks the SERIES-scoped list most-recent-first
    and returns the newest filing whose primary_doc.xml re-confirms the
    seriesId."""
    target = "S000004354"
    with patch("edgar_nport.requests.get") as mock_get:
        mock_get.side_effect = [
            _make_response(200, text=BROWSE_ATOM),                 # series list
            _make_response(200, text=_series_xml(
                target, "iShares Semiconductor ETF")),             # newest XML
        ]
        filing = find_filing_for_series("1100663", target)

    assert filing is not None
    assert filing.series_id == target
    assert filing.series_name == "iShares Semiconductor ETF"
    assert filing.report_period_end == "2026-03-31"
    assert filing.filing_date == "2026-05-28"
    assert filing.accession_number == "0002071691-26-012504"   # newest entry
    assert "1100663" in filing.primary_doc_url
    assert "000207169126012504" in filing.primary_doc_url


def test_find_filing_for_series_skips_xml_that_fails_reverification():
    """Belt-and-braces: if the newest filing's XML does not confirm the
    target seriesId, fall through to the next candidate."""
    target = "S000004354"
    with patch("edgar_nport.requests.get") as mock_get:
        mock_get.side_effect = [
            _make_response(200, text=BROWSE_ATOM),
            _make_response(200, text=_series_xml("S000000000", "Wrong Fund")),
            _make_response(200, text=_series_xml(target, "iShares Semiconductor ETF")),
        ]
        filing = find_filing_for_series("1100663", target)
    assert filing is not None
    assert filing.accession_number == "0002071691-26-004253"   # second entry


def test_find_filing_for_series_returns_none_when_no_match():
    """When no series-scoped filing's XML confirms the target series,
    return None."""
    target = "S999999999"
    with patch("edgar_nport.requests.get") as mock_get:
        mock_get.side_effect = [
            _make_response(200, text=BROWSE_ATOM),
            _make_response(200, text=_series_xml("S000000000", "Other")),
            _make_response(200, text=_series_xml("S000000000", "Other")),
        ]
        filing = find_filing_for_series("1100663", target)
    assert filing is None


def test_find_filing_for_series_returns_none_when_series_view_empty():
    """No filings for the series (empty atom feed) -> None."""
    with patch("edgar_nport.requests.get") as mock_get:
        mock_get.return_value = _make_response(
            200, text='<?xml version="1.0"?><feed></feed>')
        filing = find_filing_for_series("1100663", "S000004354")
    assert filing is None


# ---------------------------------------------------------------------------
# fetch_holdings_from_filing — XML parsing
# ---------------------------------------------------------------------------


FIXTURE_XML = """<edgarSubmission>
  <formData>
    <invstOrSecs>
      <invstOrSec>
        <name>Entegris, Inc.</name>
        <cusip>29362U104</cusip>
        <valUSD>15000000.50</valUSD>
        <pctVal>3.45</pctVal>
        <invCountry>US</invCountry>
      </invstOrSec>
      <invstOrSec>
        <name>Taiwan Semiconductor Manufacturing Co. Ltd.</name>
        <cusip>874039100</cusip>
        <valUSD>42000000.00</valUSD>
        <pctVal>9.65</pctVal>
        <invCountry>TW</invCountry>
      </invstOrSec>
      <invstOrSec>
        <name>Mystery name without cusip</name>
      </invstOrSec>
    </invstOrSecs>
  </formData>
</edgarSubmission>
"""


def test_fetch_holdings_parses_invstorsec_blocks():
    """Each <invstOrSec> with at least <name> and <cusip> becomes an
    EdgarHolding. Blocks missing either are silently dropped."""
    from edgar_nport import EdgarFiling
    filing = EdgarFiling(
        cik="0001100663", accession_number="0001-26-1",
        filing_date="2026-05-28", report_period_end="2026-03-31",
        series_id="S000004354", series_name="iShares Semiconductor ETF",
        primary_doc_url="https://example.com/primary_doc.xml",
    )
    with patch("edgar_nport.requests.get") as mock_get:
        mock_get.return_value = _make_response(200, text=FIXTURE_XML)
        holdings = fetch_holdings_from_filing(filing)
    assert len(holdings) == 2  # the "Mystery name" entry without cusip dropped
    names = [h.name for h in holdings]
    assert "Entegris, Inc." in names
    assert any("Taiwan" in n for n in names)
    entegris = next(h for h in holdings if h.name == "Entegris, Inc.")
    assert entegris.cusip == "29362U104"
    assert entegris.value_usd == pytest.approx(15000000.50)
    assert entegris.percent_value == pytest.approx(3.45)
    assert entegris.country == "US"


# ---------------------------------------------------------------------------
# OpenFIGI mapping — US-listed preference
# ---------------------------------------------------------------------------


def test_openfigi_batch_prefers_us_listed_when_multiple_match():
    """When OpenFIGI returns multiple instruments for a CUSIP (e.g. TSMC
    has both a Taiwan-listed primary 2330.TW and a US ADR TSM), we
    must return the US-listed ticker (TSM) for the breadth pipeline.

    This matches the actual SOXX deployment: yfinance can resolve US
    tickers but struggles with foreign primaries."""
    cusips = ["874039100"]  # TSMC
    payload = [
        {
            "data": [
                {"ticker": "TSM",  "exchCode": "UN", "marketSector": "Equity",
                 "name": "TAIWAN SEMICONDUCTOR-SP ADR"},
                {"ticker": "2330", "exchCode": "TT", "marketSector": "Equity",
                 "name": "TAIWAN SEMICONDUCTOR MFG"},
            ]
        }
    ]
    with patch("edgar_nport.requests.post") as mock_post:
        mock_post.return_value = _make_response(200, json_payload=payload)
        mapped = _openfigi_batch(cusips)
    assert mapped == {"874039100": "TSM"}


def test_openfigi_batch_returns_none_when_no_match():
    """A CUSIP with no data array → cached as None so we do not retry."""
    cusips = ["XXXNOMATCH"]
    payload = [{"warning": "No identifier found"}]
    with patch("edgar_nport.requests.post") as mock_post:
        mock_post.return_value = _make_response(200, json_payload=payload)
        mapped = _openfigi_batch(cusips)
    assert mapped == {"XXXNOMATCH": None}


# ---------------------------------------------------------------------------
# resolve_tickers — cache discipline
# ---------------------------------------------------------------------------


def test_resolve_tickers_uses_cache_and_skips_known(tmp_path, monkeypatch):
    """Already-cached CUSIPs are NOT re-fetched via OpenFIGI."""
    cache_path = tmp_path / "cusip_to_ticker_cache.json"
    cache_path.write_text(
        json.dumps({"29362U104": "ENTG", "OLDCACHED": None}),
        encoding="utf-8",
    )
    monkeypatch.setattr("edgar_nport.CUSIP_CACHE_PATH", cache_path)

    holdings = [
        EdgarHolding(name="Entegris", cusip="29362U104"),       # cached -> ENTG
        EdgarHolding(name="Unknown",  cusip="NEW_CUSIP_123"),   # new -> fetch
    ]
    with patch("edgar_nport._openfigi_batch") as mock_batch:
        mock_batch.return_value = {"NEW_CUSIP_123": "NEW"}
        resolve_tickers(holdings)
        # Only the NEW cusip should have been sent to OpenFIGI
        sent = mock_batch.call_args.args[0]
        assert sent == ["NEW_CUSIP_123"]
    assert holdings[0].ticker == "ENTG"
    assert holdings[1].ticker == "NEW"

    # Cache must have been persisted with the new mapping
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    assert persisted["NEW_CUSIP_123"] == "NEW"
    assert persisted["29362U104"] == "ENTG"  # preserved


def test_resolve_tickers_no_fetch_when_all_cached(tmp_path, monkeypatch):
    """If every CUSIP is cached, OpenFIGI is not called at all — zero
    network requests is the correct behaviour."""
    cache_path = tmp_path / "cusip_to_ticker_cache.json"
    cache_path.write_text(
        json.dumps({"A": "AA", "B": "BB"}), encoding="utf-8",
    )
    monkeypatch.setattr("edgar_nport.CUSIP_CACHE_PATH", cache_path)

    holdings = [
        EdgarHolding(name="A", cusip="A"),
        EdgarHolding(name="B", cusip="B"),
    ]
    with patch("edgar_nport._openfigi_batch") as mock_batch:
        resolve_tickers(holdings)
        mock_batch.assert_not_called()
    assert [h.ticker for h in holdings] == ["AA", "BB"]


# ---------------------------------------------------------------------------
# Phase 26.3 — per-ETF staleness override
# ---------------------------------------------------------------------------
# These tests live here (rather than in test_data_integrity.py) because
# they import directly from fetch_constituents.py and exercise pure
# Python logic with no I/O — same character as the other tests in this
# file. They guard the per-ETF threshold mechanism added in Phase 26.3.


def test_resolve_staleness_thresholds_uses_global_default_when_no_override():
    """An ETF registry entry without a 'staleness' block falls back to
    the module-level WARN_STALE_DAYS / MAX_STALE_DAYS (14 / 30)."""
    from fetch_constituents import (
        WARN_STALE_DAYS, MAX_STALE_DAYS, resolve_staleness_thresholds,
    )
    cfg = {"symbol": "IUES"}  # any ETF without per-ETF override
    warn, critical = resolve_staleness_thresholds(cfg)
    assert warn == WARN_STALE_DAYS == 14
    assert critical == MAX_STALE_DAYS == 30


def test_resolve_staleness_thresholds_applies_per_etf_override():
    """SOXX has a per-ETF override of warn=60 / critical=120 to match
    SEC EDGAR N-PORT-P cadence. resolve_staleness_thresholds must
    return those values, not the global default."""
    from fetch_constituents import resolve_staleness_thresholds
    cfg = {
        "symbol": "SOXX",
        "staleness": {"warn_days": 60, "critical_days": 120},
    }
    warn, critical = resolve_staleness_thresholds(cfg)
    assert warn == 60
    assert critical == 120


def test_resolve_staleness_thresholds_rejects_inverted_thresholds():
    """A registry entry with warn >= critical is a config bug — must
    raise rather than silently produce nonsensical staleness status."""
    from fetch_constituents import resolve_staleness_thresholds
    cfg = {
        "symbol": "BROKEN",
        "staleness": {"warn_days": 120, "critical_days": 60},
    }
    with pytest.raises(ValueError, match="must satisfy"):
        resolve_staleness_thresholds(cfg)


def test_soxx_registry_carries_phase_26_3_override():
    """SOXX must keep its per-ETF override in etf_registry.py. If
    anyone removes it, this test fails and points at the regression
    (without the override, SOXX would trip critical at 31 days even
    though EDGAR N-PORT-P is still authoritative)."""
    from etf_registry import get_etf
    cfg = get_etf("SOXX")
    stale = cfg.get("staleness")
    assert stale is not None, (
        "SOXX must carry a per-ETF staleness override; default 30-day "
        "critical threshold is incompatible with quarterly EDGAR cadence."
    )
    assert stale.get("warn_days") == 60
    assert stale.get("critical_days") == 120
    assert "EDGAR" in (stale.get("rationale") or ""), (
        "Rationale should reference EDGAR — that is the source of the "
        "relaxed threshold; future maintainers need that link."
    )
