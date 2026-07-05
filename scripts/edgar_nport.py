"""SEC EDGAR N-PORT-P fallback for ETF constituent rosters (Phase 26.2).

Why this exists
---------------
SOXX is the only Strategy A member sourced from the iShares US holdings
endpoint, which is Akamai-blocked from automated requests (see
DATA_INTEGRITY_POLICY.md section 2.1). The carry-forward fallback in
``fetch_constituents.py`` keeps the pipeline running but ages the
roster indefinitely. This module provides a SECOND, fully public,
authoritative source: SEC Form N-PORT-P, filed quarterly by every
US-registered ETF including SOXX.

How it works
------------
1. Look up the ETF's parent CIK + series id (registered per-ETF in
   ``etf_registry.py`` under ``edgar_nport``).
2. List the parent CIK's recent N-PORT-P filings via the EDGAR JSON
   submissions API.
3. For each filing, fetch ``primary_doc.xml`` and check the
   ``<seriesId>`` matches the target series.
4. Parse the ``<invstOrSec>`` blocks — name, CUSIP, value, percent.
5. Resolve CUSIPs to US-listed tickers via the OpenFIGI free API
   (no auth needed for the ~30 SOXX constituents). Cache to disk.

Cadence
-------
N-PORT-P is QUARTERLY (60 days after quarter-end deadline). So the
roster from EDGAR is at most ~150 days stale (a new quarter just
ended but its filing is not yet due). For SOXX, with ~2-3 holdings
swaps per year, that is < 1.2 stocks of drift in 33 constituents
(3.6%), well within signal tolerance.

EDGAR access etiquette
----------------------
SEC fair-use policy: include an identifying User-Agent header, rate
limit to <=10 requests/second. We use 0.12s throttle between calls
(8/s), well under the cap.

References
----------
- N-PORT-P filing rules: https://www.sec.gov/rules/final/2016/33-10231.pdf
- EDGAR fair-use: https://www.sec.gov/os/accessing-edgar-data
- OpenFIGI free API: https://www.openfigi.com/api
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CUSIP_CACHE_PATH = DATA_DIR / "cusip_to_ticker_cache.json"

# SEC fair-use: identify ourselves. The SEC may rate-limit or block
# requests without a clear UA. The address is informational only.
SEC_USER_AGENT = (
    "Navigo Investment Management Pte. Ltd. (Singapore) research-pipeline "
    "Contact: research@navigo.sg"
)
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}

EDGAR_SUBMISSIONS_TEMPLATE = (
    "https://data.sec.gov/submissions/CIK{cik_padded}.json"
)
EDGAR_FILING_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/primary_doc.xml"
)
# Series-scoped filing view. browse-edgar accepts a fund seriesId (S...) in
# the CIK parameter and returns ONLY that series' filings. We use this rather
# than scanning the sponsor's CIK-wide submissions feed: iShares Trust
# (CIK 1100663) files >1400 N-PORT-P/yr across ~400 series, so any one
# series' most-recent filing is buried hundreds deep in the CIK-wide feed —
# which is exactly why the old scan silently returned None for SOXX.
BROWSE_EDGAR_SERIES_TEMPLATE = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    "&CIK={series_id}&type=NPORT-P&dateb=&owner=include&count={count}"
    "&output=atom"
)

# Throttle: 0.12 s between SEC requests = 8.3 req/s, under the 10 req/s cap.
SEC_THROTTLE_SECONDS = 0.12

# Upper safety bound on how many SERIES-scoped N-PORT-P filings to
# fetch+verify before giving up. The series view is already narrow (one
# fund's own filings, ~4/yr), so the newest candidate almost always matches
# on the first pass; this only bounds the belt-and-braces re-verification
# loop. (Before the series-scoped lookup this was a cap on the CIK-wide
# scan, where 200 was too small for a mega-filer and SOXX was never found.)
MAX_FILINGS_TO_SCAN = 40

# OpenFIGI free tier limits (no API key):
#   - 10 mappings per request (vs 100 with a free-registered API key)
#   - 25 requests per 6 seconds, soft rate cap
# We batch at 10 and sleep 0.3s between batches — 33 SOXX constituents
# take ~4 requests (≈1.2s). Way under any rate limit.
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_BATCH_SIZE = 10
OPENFIGI_THROTTLE_SECONDS = 0.3


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EdgarFiling:
    """One N-PORT-P filing matched to a target series."""
    cik: str
    accession_number: str
    filing_date: str          # ISO date the filing was submitted to SEC
    report_period_end: str    # ISO date of the holdings snapshot ("repPdEnd")
    series_id: str
    series_name: str
    primary_doc_url: str


@dataclass
class EdgarHolding:
    """One <invstOrSec> entry from the N-PORT-P XML."""
    name: str
    cusip: str
    ticker: str | None = None  # populated by cusip→ticker mapping
    value_usd: float | None = None
    percent_value: float | None = None
    country: str | None = None


@dataclass
class EdgarRoster:
    """The full result of a successful EDGAR roster fetch — what the
    consumer (fetch_constituents.py) writes into the constituents JSON."""
    filing: EdgarFiling
    holdings: list[EdgarHolding] = field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        """Return the resolved ticker list (drops any None values).

        A None ticker means the CUSIP did not resolve to a US-listed
        equity via OpenFIGI. For SOXX this should be empty in practice —
        all SOX members are US-listed or US-ADR-listed common stock."""
        return [h.ticker for h in self.holdings if h.ticker]


# ---------------------------------------------------------------------------
# SEC EDGAR client
# ---------------------------------------------------------------------------


def _pad_cik(cik: str | int) -> str:
    """SEC CIKs are zero-padded to 10 digits in API paths."""
    return str(cik).strip().lstrip("0").rjust(10, "0")


def _strip_acc(acc: str) -> str:
    """Accession number has hyphens in metadata; URLs need it without."""
    return acc.replace("-", "")


def list_recent_nport_filings(cik: str | int) -> list[dict]:
    """Return recent N-PORT-P filings for a CIK as a list of dicts.

    Each dict has keys: accessionNumber, filingDate, primaryDocument.
    """
    url = EDGAR_SUBMISSIONS_TEMPLATE.format(cik_padded=_pad_cik(cik))
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    body = r.json()
    recent = body.get("filings", {}).get("recent", {})
    if not recent:
        return []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    prims = recent.get("primaryDocument") or []
    out = []
    for i, form in enumerate(forms):
        if "NPORT-P" not in form:
            continue
        out.append({
            "accessionNumber": accs[i],
            "filingDate": dates[i],
            "primaryDocument": prims[i],
        })
    return out


def list_series_nport_filings(series_id: str, count: int = 40) -> list[dict]:
    """Return recent N-PORT-P filings for a SINGLE fund series, most recent
    first, as dicts with keys ``accession_nodash``, ``accession``,
    ``filing_date``.

    Uses EDGAR's browse-edgar series view (BROWSE_EDGAR_SERIES_TEMPLATE),
    which accepts the seriesId in its CIK parameter and returns only that
    series' filings. The accession is recovered from each atom entry's
    ``<filing-href>`` folder (the 18-digit no-dash accession).
    """
    url = BROWSE_EDGAR_SERIES_TEMPLATE.format(series_id=series_id, count=count)
    r = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=30)
    r.raise_for_status()
    out: list[dict] = []
    for entry in re.findall(r"<entry>(.*?)</entry>", r.text, re.DOTALL):
        href = re.search(r"<filing-href>([^<]+)</filing-href>", entry)
        if not href:
            continue
        m = re.search(r"/data/\d+/(\d{18})/", href.group(1))
        if not m:
            continue
        nodash = m.group(1)
        fdate = re.search(r"<filing-date>([^<]+)</filing-date>", entry)
        out.append({
            "accession_nodash": nodash,
            "accession": f"{nodash[:10]}-{nodash[10:12]}-{nodash[12:]}",
            "filing_date": fdate.group(1) if fdate else None,
        })
    return out


def find_filing_for_series(
    cik: str | int,
    series_id: str,
    max_scan: int = MAX_FILINGS_TO_SCAN,
) -> EdgarFiling | None:
    """Return the most recent N-PORT-P filing for ``series_id`` (filed under
    sponsor ``cik``), or None if none is found / verifiable.

    Filings are looked up SERIES-scoped via list_series_nport_filings, so
    this is correct even for a mega-filer whose CIK-wide feed would bury the
    series — the bug that left SOXX permanently on carry-forward. Each
    candidate's primary_doc.xml is fetched most-recent-first and its
    ``<seriesId>`` re-verified before returning: the series view is
    authoritative, the XML check is belt-and-braces. Throttles to
    ``SEC_THROTTLE_SECONDS`` between requests to respect SEC fair-use.
    """
    cik_padded = _pad_cik(cik)
    try:
        filings = list_series_nport_filings(series_id)
    except requests.RequestException:
        return None
    if not filings:
        return None
    for i, f in enumerate(filings[:max_scan]):
        if i > 0:
            time.sleep(SEC_THROTTLE_SECONDS)
        url = EDGAR_FILING_TEMPLATE.format(
            cik=int(cik_padded), acc_nodash=f["accession_nodash"],
        )
        try:
            r = requests.get(url, headers=SEC_HEADERS, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        body = r.text
        # Belt-and-braces: confirm the XML really is this series.
        if f"<seriesId>{series_id}</seriesId>" not in body:
            continue
        sname = re.search(r"<seriesName>([^<]+)</seriesName>", body)
        rep_end = re.search(r"<repPdEnd>([^<]+)</repPdEnd>", body)
        return EdgarFiling(
            cik=cik_padded,
            accession_number=f["accession"],
            filing_date=f["filing_date"],
            report_period_end=(rep_end.group(1) if rep_end
                                else f["filing_date"]),
            series_id=series_id,
            series_name=(sname.group(1).replace("&amp;", "&")
                          if sname else f"series-{series_id}"),
            primary_doc_url=url,
        )
    return None


def fetch_holdings_from_filing(filing: EdgarFiling) -> list[EdgarHolding]:
    """Download and parse the primary_doc.xml for ``filing``, returning
    its ``<invstOrSec>`` entries as EdgarHolding dataclasses.

    Tickers are NOT populated here — call resolve_tickers() afterwards
    to fill them via OpenFIGI.
    """
    r = requests.get(filing.primary_doc_url, headers=SEC_HEADERS, timeout=60)
    r.raise_for_status()
    body = r.text
    holdings: list[EdgarHolding] = []
    # Capture each <invstOrSec>...</invstOrSec> block. The XML is
    # well-formed but big; regex is sufficient for these tag-level pulls.
    for block in re.findall(r"<invstOrSec>(.*?)</invstOrSec>", body, re.DOTALL):
        def _grab(tag):
            m = re.search(rf"<{tag}>([^<]+)</{tag}>", block)
            return m.group(1) if m else None

        name = _grab("name")
        cusip = _grab("cusip")
        if not name or not cusip:
            continue
        value = _grab("valUSD")
        pct = _grab("pctVal")
        country = _grab("invCountry")
        try:
            value_f = float(value) if value else None
        except ValueError:
            value_f = None
        try:
            pct_f = float(pct) if pct else None
        except ValueError:
            pct_f = None
        holdings.append(EdgarHolding(
            name=name.strip().replace("&amp;", "&"),
            cusip=cusip.strip(),
            value_usd=value_f,
            percent_value=pct_f,
            country=country.strip() if country else None,
        ))
    return holdings


# ---------------------------------------------------------------------------
# CUSIP -> ticker mapping (OpenFIGI free tier + disk cache)
# ---------------------------------------------------------------------------


# Exchange codes considered "US primary listing" by OpenFIGI. Used to
# prefer the US-listed ticker (TSM) over the foreign primary (2330.TW)
# when an ADR + ordinary both resolve. UN=NYSE, UQ=NASDAQ, UA=AMEX,
# UF=NYSE Arca, US=US generic. Empty string is also OpenFIGI's "US".
US_EXCHANGE_CODES = {"UN", "UQ", "UA", "UF", "US", ""}


def _load_cusip_cache() -> dict[str, str | None]:
    """Load the on-disk CUSIP -> ticker cache. Values may be None to
    record "we asked, OpenFIGI returned nothing" — so we do not re-ask."""
    if not CUSIP_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CUSIP_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cusip_cache(cache: dict[str, str | None]) -> None:
    CUSIP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUSIP_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8",
    )


def _openfigi_batch(cusips: list[str]) -> dict[str, str | None]:
    """One OpenFIGI request for up to OPENFIGI_BATCH_SIZE CUSIPs.

    Returns a dict mapping each input CUSIP to the best US-listed
    ticker, or None if no match. OpenFIGI's free tier does not require
    auth at <=25 requests/minute and <=25 mappings/request.
    """
    if not cusips:
        return {}
    body = [{"idType": "ID_CUSIP", "idValue": c} for c in cusips]
    r = requests.post(
        OPENFIGI_URL, json=body,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    out: dict[str, str | None] = {}
    for c, entry in zip(cusips, payload):
        data = entry.get("data") or []
        # Prefer US-listed common stock; fall back to first result.
        equity = [x for x in data if x.get("marketSector") == "Equity"]
        us_listed = [x for x in equity if x.get("exchCode") in US_EXCHANGE_CODES]
        pick = (us_listed or equity or data)
        out[c] = pick[0].get("ticker") if pick else None
    return out


def resolve_tickers(holdings: list[EdgarHolding]) -> None:
    """Populate ``holding.ticker`` for each entry via OpenFIGI, with
    on-disk cache to avoid re-mapping CUSIPs we have already resolved.

    Mutates ``holdings`` in place. CUSIPs that fail to resolve are
    cached as None so subsequent calls do not re-fetch them; an
    operator can manually edit the cache to inject overrides if needed.
    """
    cache = _load_cusip_cache()
    unknown = sorted({h.cusip for h in holdings if h.cusip not in cache})
    if unknown:
        for i in range(0, len(unknown), OPENFIGI_BATCH_SIZE):
            batch = unknown[i:i + OPENFIGI_BATCH_SIZE]
            mapped = _openfigi_batch(batch)
            cache.update(mapped)
            if i + OPENFIGI_BATCH_SIZE < len(unknown):
                time.sleep(OPENFIGI_THROTTLE_SECONDS)
        _save_cusip_cache(cache)
    for h in holdings:
        h.ticker = cache.get(h.cusip)


# ---------------------------------------------------------------------------
# Public top-level convenience
# ---------------------------------------------------------------------------


def fetch_roster_via_edgar(
    cik: str | int, series_id: str,
) -> EdgarRoster | None:
    """One-call helper: find the most recent N-PORT-P for ``series_id``
    under ``cik``, fetch its holdings, resolve tickers, return the
    fully populated EdgarRoster (or None if no filing is available).

    Intended to be called by fetch_constituents.py when the primary
    iShares source returns blocked HTML or empty CSV.
    """
    filing = find_filing_for_series(cik, series_id)
    if filing is None:
        return None
    holdings = fetch_holdings_from_filing(filing)
    resolve_tickers(holdings)
    return EdgarRoster(filing=filing, holdings=holdings)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv: list[str]) -> int:
    """Simple CLI for ad-hoc lookups. Usage:
        python scripts/edgar_nport.py <cik> <series_id>
    """
    if len(argv) != 3:
        print(
            "Usage: python scripts/edgar_nport.py <cik> <series_id>\n"
            "Example: python scripts/edgar_nport.py 1100663 S000004354  "
            "# SOXX",
            file=sys.stderr,
        )
        return 1
    cik, series_id = argv[1], argv[2]
    print(f"Searching SEC EDGAR for {cik}/{series_id} ...")
    roster = fetch_roster_via_edgar(cik, series_id)
    if roster is None:
        print("No matching N-PORT-P filing found.", file=sys.stderr)
        return 2
    print(f"Found filing: {roster.filing.series_name}")
    print(f"  Accession: {roster.filing.accession_number}")
    print(f"  Filed:     {roster.filing.filing_date}")
    print(f"  Snapshot:  {roster.filing.report_period_end}")
    print(f"  Holdings:  {len(roster.holdings)} entries")
    resolved = sum(1 for h in roster.holdings if h.ticker)
    print(f"  Tickers resolved: {resolved}/{len(roster.holdings)}")
    print(f"  Tickers: {sorted(roster.tickers)}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
