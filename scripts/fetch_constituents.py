"""Step 1 — pull point-in-time ETF constituent rosters from iShares.

Pulls one snapshot per Friday from the ETF's start_friday through the most
recent completed Friday and writes a structured JSON to
data/constituents_{etf_lower}.json.

The ETF symbol selects URL + start_friday + ticker overrides from
scripts/etf_registry.py. Pass --etf SYMBOL on the command line. Default
is SOXX for backward compatibility.

Per session decisions (2026-05-14):
  - Backtest window starts 2018; collection starts 2018-01-05 (first Friday).
  - Snapshot cadence: weekly on Fridays. If a Friday returns no data
    (US market holiday or iShares data gap) we walk back up to 5 calendar
    days. If still no data, we carry forward the most recent prior snapshot
    AND log a structured warning in the output JSON.
  - Equity-only filter (Asset Class == "Equity"); cash, currency, and futures
    placeholders (USD, XTSLA, WFFUT, RTYU4, IXTU4) are dropped.
  - Membership held static between weekly snapshots — explicit assumption,
    documented in the output JSON.
  - Raw CSVs cached to data/raw_ishares/ (gitignored) so re-runs are cheap.

Output layout:
{
  "etf": "SOXX",
  "source": "<URL>",
  "fetched_at_utc": "<ISO timestamp>",
  "start_friday": "2018-01-05",
  "end_friday": "...",
  "n_target_fridays": 437,
  "n_snapshots_written": 437,
  "membership_assumption": "...",
  "asset_class_filter": "Equity",
  "walkbacks":     [ { "target_friday": ..., "fallback_date": ..., "reason": ... }, ... ],
  "carry_forwards":[ { "target_friday": ..., "cause": ..., "carried_from_target": ..., "reason": ... }, ... ],
  "endpoint_unavailable": [ { "target_friday": ..., "cause": ..., "reason": ... }, ... ],
  "endpoint_health": { "status": "ok"|"unavailable", "detail": ..., ... },
  "snapshots": {
      "YYYY-MM-DD": { "actual_date": "...", "n_tickers": N, "tickers": [...] },
      ...
  }
}

Transport (Phase 27, 2026-08-07):
  Holdings come from the BlackRock product-data JSON API (PRODUCT_DATA_API
  below). The legacy `<ajax_id>.ajax?fileType=csv` route this module was
  built on stopped serving CSV when iShares re-platformed the product pages
  between the 2026-07-10 and 2026-07-17 refreshes; it now returns the SPA
  product page as HTTP 200 HTML for every date. The ~10,400 CSVs already in
  data/raw_ishares/ remain the source of truth for history and are read
  cache-first; only new dates go to the API.

  Failure taxonomy — the point of Phase 27 is that these three are no
  longer interchangeable:
    - walkback / carry_forward : this Friday has no holdings (holiday, data
                                 gap). Endpoint healthy. Exit 0.
    - endpoint_unavailable     : the transport is dead. The walk
                                 short-circuits on the first failure, no
                                 carry-forwards are emitted, exit 3.
    - staleness critical       : the roster has aged past policy. Exit 2.

Run:
    python scripts/fetch_constituents.py             # default: SOXX
    python scripts/fetch_constituents.py --etf CSP1  # S&P 500 via iShares UK
    python scripts/fetch_constituents.py --etf CSP1 --carry-forward-on-outage
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import get_etf  # noqa: E402
from stall_guard import EndpointDegraded, LatencyCircuit  # noqa: E402

# Force UTF-8 stdout so the BOM in iShares CSVs and any non-ASCII names do
# not crash on the Windows cp1252 console.
sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw_ishares"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Default selected by argparse — kept for backward compatibility with the
# original SOXX-only invocation.
DEFAULT_ETF = "SOXX"

MAX_WALKBACK_DAYS = 5  # how far back from a target Friday to search

# A "no holdings" answer is cached only once the date is this old. Old
# no-data dates are settled facts — a fund's pre-inception history never
# gains holdings, and neither does a past public holiday — so re-fetching
# them every run is pure cost. A fund that launched mid-sample would
# otherwise pay 6 uncached requests per pre-inception Friday, forever.
# Recent dates are deliberately NOT cached: an empty answer there usually
# means "holdings not published yet", and caching it would freeze the gap.
NEGATIVE_CACHE_MIN_AGE_DAYS = 30

THROTTLE_BASE_SECONDS = 1.5
THROTTLE_JITTER_SECONDS = 0.5

# Staleness policy (Phase 26.1, 2026-05-31, per-ETF override added in
# Phase 26.3). See DATA_INTEGRITY_POLICY.md for the rationale and
# escalation procedure.
#
# When the upstream holdings source (iShares US is the known offender)
# is blocked or returns warmup HTML, fetch_constituents.py carries the
# most recent known-good roster forward into subsequent Friday
# snapshots. This keeps the breadth pipeline running but ages the
# roster. Index turnover is ~2-3 holdings per year, so the default
# thresholds suit a daily-availability source:
#
#   - <= WARN_STALE_DAYS (14)     : OK, business as usual.
#   - WARN_STALE_DAYS .. MAX (30) : print a warning, exit 0. Carry-
#                                   forward continues. A human should
#                                   investigate.
#   - >  MAX_STALE_DAYS (30)      : print a critical alert, exit code 2.
#                                   CI must fail noisily. Operator
#                                   action required: either restore
#                                   the upstream source or refresh the
#                                   roster manually.
#
# Per-ETF overrides (Phase 26.3): when an ETF's registry entry carries
# a ``staleness`` block (see etf_registry.py for SOXX's), those values
# replace WARN_STALE_DAYS / MAX_STALE_DAYS for that ETF only. This is
# how SOXX uses 60/120-day thresholds matched to its EDGAR cadence
# rather than the global 14/30-day defaults.
WARN_STALE_DAYS = 14
MAX_STALE_DAYS = 30
EXIT_OK = 0
EXIT_STALENESS_CRITICAL = 2
# Phase 27 (2026-08-07) — the upstream transport is gone wholesale, as
# opposed to the roster merely ageing. Kept distinct from the staleness exit
# so the operator can tell "the roster aged out" from "the endpoint died",
# and takes precedence over it because it is the actionable root cause.
EXIT_ENDPOINT_UNAVAILABLE = 3
# 2026-08-14 — the endpoint answers, but each date pays a 30s timeout before
# succeeding. NDIA ran 228 minutes and exited 0 with a clean roster while the
# Friday it was feeding went unfilled. Distinct from UNAVAILABLE because the
# data is fine and the operator's action is different: wait for the network,
# then re-run. Nothing needs repairing.
EXIT_ENDPOINT_DEGRADED = 5


def resolve_staleness_thresholds(etf_cfg: dict) -> tuple[int, int]:
    """Return (warn_days, critical_days) for this ETF, applying any
    per-ETF override from the registry. Defaults to the module-level
    WARN_STALE_DAYS / MAX_STALE_DAYS if no override is registered."""
    override = etf_cfg.get("staleness") or {}
    warn = int(override.get("warn_days", WARN_STALE_DAYS))
    critical = int(override.get("critical_days", MAX_STALE_DAYS))
    if not (0 < warn < critical):
        raise ValueError(
            f"Invalid staleness thresholds for {etf_cfg.get('symbol')}: "
            f"warn={warn} critical={critical} (must satisfy 0 < warn < critical)"
        )
    return warn, critical
RETRY_BACKOFFS = [5, 10, 30]  # seconds; 3 retries on transport failure or 5xx

# Note: Python's datetime constructor is 1-indexed for months (Jan=1), unlike
# JavaScript's Date which is 0-indexed (Jan=0). We always use Python here.

# =============================================================================
# Phase 27 (2026-08-07) — product-data API transport
# =============================================================================
# iShares re-platformed the UK/EMEA product pages onto a new front end
# some time between the 2026-07-10 and 2026-07-17 refreshes. The legacy
# `<ajax_id>.ajax?fileType=csv&...` route no longer serves CSV: it falls
# through to the single-page product shell and returns HTTP 200 with ~2.7MB
# of HTML, for EVERY asOfDate including dates that previously worked.
#
# `looks_like_ishares_holdings_csv` correctly rejected that HTML and
# `fetch_with_retry` correctly raised, so no bad data was ever cached — but
# the carry-forward path upstream then reported a dead endpoint as an
# ordinary holiday data gap for four consecutive weeks. Hence the new
# failure taxonomy below.
#
# The replacement is the JSON component API that the new page itself calls.
# Verified 2026-08-07 against the cached CSV ground truth: rosters are
# identical for CSP1 (2026-07-10), SOXX (2026-05-08) and all six
# exchange-suffix ETFs, including suffix resolution.
PRODUCT_DATA_API = (
    "https://www.blackrock.com/varnish-api/uk-retail01-product-data"
    "/product-data/api/v2/get-product-data"
)

# The UK varnish host serves BOTH regions; only targetSite / locale differ.
# This is what unblocks SOXX: the US .ajax endpoint has been Akamai-blocked
# since ~2026-05-15, but the US fund's holdings are reachable here.
_REGION_TO_SITE: dict[str, tuple[str, str]] = {
    "uk": ("ishares-uk", "en_GB"),
    "us": ("ishares-us", "en_US"),
}

# Path to the holdings rows inside the API payload.
_HOLDINGS_PATH = ("componentsByNameMap", "holdings",
                  "containersByNameMap", "all", "dataPointsByNameMap")

# Column-major datapoints the parser consumes, mapped to the CSV column they
# replace. The API returns each as a parallel array under `.value`.
_JSON_TO_CSV_COLUMN = {
    "ticker": "Ticker",
    "assetClass": "Asset Class",
    "exchange": "Exchange",
    "countryOfRisk": "Location",
}


class EndpointUnavailable(RuntimeError):
    """The upstream transport is dead: no response, a non-200, a non-JSON
    body, or anti-bot HTML. Distinct from "this date legitimately has no
    holdings", which is an empty roster and NOT an error."""


class PayloadContractError(RuntimeError):
    """The endpoint answered but the payload no longer has the shape we
    parse. Treated as an outage rather than as an empty roster, because
    silently reading zero holdings out of a changed payload is exactly the
    failure this module exists to prevent."""


@dataclass
class EndpointCircuit:
    """Short-circuit for a dead upstream.

    The per-Friday walk costs ~48s per date against a dead endpoint (four
    attempts plus 45s of retry backoff), and the walk is ~448 Fridays per
    ETF across 24 ETFs. Before this existed, one outage burned hours
    re-confirming the same failure and emitted only carry-forwards.

    The breaker trips on the FIRST hard failure and every subsequent date
    short-circuits for free. Note this deliberately does not pre-probe the
    endpoint: a run whose Fridays are all served from cache must still
    succeed even when the endpoint is down.
    """

    dead: bool = False
    reason: str | None = None
    first_failure_target: date | None = None
    n_unavailable: int = 0

    def trip(self, target: date, reason: str) -> None:
        if not self.dead:
            self.dead = True
            self.reason = reason
            self.first_failure_target = target
            print(
                f"  ENDPOINT DOWN at {target.isoformat()}: {reason}",
                flush=True,
            )
            print(
                "  Short-circuiting the remaining Fridays — no carry-forwards "
                "will be emitted for them.",
                flush=True,
            )


def product_data_params(target: date, etf_cfg: dict) -> dict[str, str]:
    """Query parameters for one (ETF, asOfDate) holdings request."""
    region = etf_cfg.get("ishares_region", "uk")
    try:
        target_site, locale = _REGION_TO_SITE[region]
    except KeyError:
        raise ValueError(
            f"Unknown ishares_region {region!r} for {etf_cfg.get('symbol')}; "
            f"expected one of {sorted(_REGION_TO_SITE)}"
        ) from None
    return {
        "portfolioId": str(etf_cfg["product_id"]),
        "portfolioType": "ISHARES_FUND_DATA",
        "appType": "PRODUCT_PAGE",
        "appSubType": "ISHARES",
        "targetSite": target_site,
        "locale": locale,
        "userType": "individual",
        "component": "holdings",
        "asOfDate": target.strftime("%Y%m%d"),
    }


def _holdings_datapoints(payload: dict) -> dict:
    """Descend to the holdings datapoint map, or raise PayloadContractError."""
    node = payload
    for key in _HOLDINGS_PATH:
        if not isinstance(node, dict) or key not in node:
            raise PayloadContractError(
                f"payload missing {'.'.join(_HOLDINGS_PATH)} (stopped at "
                f"{key!r}); the product-data API contract has changed"
            )
        node = node[key]
    if not isinstance(node, dict):
        raise PayloadContractError(
            f"{'.'.join(_HOLDINGS_PATH)} is {type(node).__name__}, expected dict"
        )
    missing = [k for k in ("ticker", "assetClass", "asOfDate") if k not in node]
    if missing:
        raise PayloadContractError(
            f"payload holdings datapoints missing required keys {missing}; "
            f"present: {sorted(node)[:20]}"
        )
    return node


def parse_holdings_json(
    payload: dict,
    target: date,
    ticker_overrides: dict | None = None,
    apply_exchange_suffix: bool = False,
) -> list[str]:
    """Parse a product-data API payload into a yfinance-ready ticker list.

    Returns [] when the endpoint has no holdings for `target` — the JSON
    equivalent of the old empty-template CSV, and the input to the walkback.

    Two distinct "no data" signals, both of which MUST be honoured (verified
    against the live endpoint 2026-08-07):

      1. ``ticker.value`` is null.
      2. ``asOfDate.value`` does not echo the requested date. For a weekend,
         holiday, pre-inception or future date the API silently falls back to
         the LATEST available date rather than erroring. Accepting that would
         write today's roster into a historical Friday — a look-ahead bug in
         a point-in-time backtest. The date-parity check is the guard.

    ``hasData`` is deliberately NOT used: it is True even when the roster is
    null, so it discriminates nothing.
    """
    dps = _holdings_datapoints(payload)

    echoed = dps["asOfDate"].get("value")
    if echoed is None or str(echoed) != target.strftime("%Y%m%d"):
        return []

    columns: dict[str, list] = {}
    for json_key in _JSON_TO_CSV_COLUMN:
        dp = dps.get(json_key)
        columns[json_key] = (dp or {}).get("value")
    if columns["ticker"] is None:
        return []

    n = len(columns["ticker"])
    for key, values in columns.items():
        if values is not None and len(values) != n:
            raise PayloadContractError(
                f"holdings column {key!r} has {len(values)} rows, expected {n}"
            )

    def cell(key: str, i: int):
        values = columns[key]
        return values[i] if values is not None else None

    overrides = ticker_overrides or {}
    tickers: list[str] = []
    seen: set[str] = set()
    for i in range(n):
        if (cell("assetClass", i) or "").strip() != "Equity":
            continue
        raw = str(cell("ticker", i) or "").strip()
        # Mirrors the CSV parser: iShares emits a "-" placeholder row that
        # corresponds to no real holding.
        if raw in {"", "-"}:
            continue
        exchange = cell("exchange", i)
        location = cell("countryOfRisk", i)
        if apply_exchange_suffix:
            sym = _resolve_yf_symbol(
                raw, (exchange or "").strip() or None, overrides,
                location=(location or "").strip() or None,
            )
        else:
            sym = overrides.get(raw, raw.replace(".", "-"))
        if sym is None or sym in {"", "-"} or sym.startswith("-."):
            continue
        if sym in seen:
            continue
        seen.add(sym)
        tickers.append(sym)
    return tickers


def fetch_product_data(target: date, etf_cfg: dict) -> dict:
    """GET one holdings payload, with retries. Raises EndpointUnavailable."""
    url = PRODUCT_DATA_API
    params = product_data_params(target, etf_cfg)
    last_err: Exception | None = None
    for backoff in [0, *RETRY_BACKOFFS]:
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(
                url, params=params,
                headers={"User-Agent": UA,
                         "Accept": "application/json, text/plain, */*"},
                timeout=30,
            )
        except Exception as e:  # transport-level
            last_err = e
            continue
        if r.status_code != 200:
            last_err = RuntimeError(
                f"HTTP {r.status_code}, body {len(r.text)} bytes"
            )
            continue
        try:
            payload = r.json()
        except Exception:
            head = r.text.lstrip()[:80].replace("\n", " ")
            last_err = RuntimeError(
                f"HTTP 200 but body is not JSON ({len(r.text)} bytes) "
                f"— likely the SPA product page or anti-bot HTML: {head!r}"
            )
            continue
        time.sleep(THROTTLE_BASE_SECONDS
                   + random.uniform(0, THROTTLE_JITTER_SECONDS))
        return payload
    raise EndpointUnavailable(
        f"Failed to fetch {etf_cfg['symbol']} holdings for {target}: {last_err}"
    )


def looks_like_ishares_holdings_csv(body: str) -> bool:
    """Return True only for real iShares holdings CSV bodies.

    iShares bot protection occasionally returns a large HTML product page
    with HTTP 200. That must NOT be cached as a CSV — downstream parsing
    would treat it as "no holdings" and silently carry forward stale
    constituents for that date. The fix is a structural validator that
    discriminates real CSV bodies from anti-bot HTML stand-ins.

    Accepts:
      - Empty-template holdings (Fund Holdings as of "-") — these are
        legitimately empty for old dates / US holidays / data gaps.
      - Populated holdings CSVs that have both the "Fund Holdings as of"
        preamble and a Ticker / Asset Class column header row.
    Rejects:
      - HTML responses (anti-bot product pages)
      - Anything else lacking the iShares CSV markers
    """
    head = body.lstrip()[:500].lower()
    if head.startswith(("<!doctype html", "<html")) or "<html" in head:
        return False
    # Empty-template responses for old / no-data dates — legitimately empty
    if 'Fund Holdings as of,"-"' in body or 'Fund Holdings as of,-' in body:
        return True
    if "Fund Holdings as of" not in body:
        return False
    # Populated CSV — must have the column header row
    for ln in body.splitlines():
        if "Ticker" in ln[:20] and "Asset Class" in ln:
            return True
    return False


def fetch_with_retry(target: date, etf_cfg: dict) -> str:
    """Fetch the raw iShares CSV for `target` and return the body.

    Caches successful 200 responses to disk so reruns do not re-hit iShares.
    Empty-template responses (Fund Holdings as of "-") are also cached because
    they are stable over time for old dates (US holidays, data gaps)
    — re-fetching them would waste requests.

    Cached bodies are re-validated against `looks_like_ishares_holdings_csv`
    on read. If a poisoned HTML body got cached by an earlier run, it is
    discarded and the network fetch retried.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = RAW_DIR / f"{etf_cfg['symbol']}_{target.strftime('%Y%m%d')}.csv"
    if cache_path.exists():
        cached = cache_path.read_text(encoding="utf-8")
        if looks_like_ishares_holdings_csv(cached):
            return cached
        # Cached body is poisoned (HTML from anti-bot) — discard and re-fetch
        cache_path.unlink()

    template = etf_cfg.get("csv_url_template")
    if not template:
        raise EndpointUnavailable(
            f"{etf_cfg['symbol']} has no csv_url_template: it was onboarded "
            "after the legacy CSV route was retired (Phase 27) and has no "
            "cached CSV history. Use load_snapshot_tickers instead."
        )
    url = f"{template}&asOfDate={target.strftime('%Y%m%d')}"
    last_err: Exception | None = None
    for backoff in [0, *RETRY_BACKOFFS]:
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        except Exception as e:
            last_err = e
            continue
        if (
            r.status_code == 200
            and len(r.text) > 1000
            and looks_like_ishares_holdings_csv(r.text)
        ):
            cache_path.write_text(r.text, encoding="utf-8")
            time.sleep(THROTTLE_BASE_SECONDS + random.uniform(0, THROTTLE_JITTER_SECONDS))
            return r.text
        if r.status_code == 200:
            last_err = RuntimeError(
                f"HTTP 200 but response is not an iShares holdings CSV "
                f"({len(r.text)} bytes) — likely anti-bot HTML"
            )
        else:
            last_err = RuntimeError(f"HTTP {r.status_code}, body {len(r.text)} bytes")
    raise RuntimeError(
        f"Failed to fetch {etf_cfg['symbol']} holdings for {target}: {last_err}"
    )


# =============================================================================
# Non-US yfinance ticker resolution
# =============================================================================
# iShares constituent CSVs include an "Exchange" column identifying the listing
# venue for each holding. For non-US ETFs (Europe sectors, Asian / EM country
# funds), we map the Exchange name to the corresponding yfinance suffix so the
# downstream price fetch resolves correctly.
#
# yfinance ticker conventions:
#   - US stocks: no suffix (AAPL, MSFT, ...)
#   - Share-class dots: convert to dash (BRK.B -> BRK-B)
#   - European stocks: <local_ticker>.<exchange_suffix>
#     .L  London,  .DE Xetra,  .PA Paris,  .MI Milan,  .AS Amsterdam,
#     .MC Madrid,  .SW Switzerland,  .BR Brussels,  .ST Stockholm,
#     .CO Copenhagen, .HE Helsinki, .OL Oslo, .LS Lisbon, .IR Dublin,
#     .VI Vienna, .WA Warsaw, .PR Prague, .AT Athens
#   - Asian: .T Tokyo, .HK Hong Kong, .TW Taiwan, .KS Kospi, .NS NSE India
#   - Other: .AX Sydney, .SA São Paulo, .JO Johannesburg

_EXCHANGE_TO_YF_SUFFIX: dict[str, str] = {
    # United Kingdom
    "London Stock Exchange":          ".L",
    "London Stock Exchange-Sets":     ".L",
    # Continental Europe
    "Xetra":                          ".DE",
    "Deutsche Boerse Ag":             ".DE",
    "Deutsche Boerse Xetra":          ".DE",
    "Hanseatische Wertpapierboerse Hamburg": ".HM",
    "Frankfurt Stock Exchange":       ".F",
    "Nyse Euronext - Euronext Paris": ".PA",
    "Euronext Paris":                 ".PA",
    "Borsa Italiana":                 ".MI",
    "Euronext Amsterdam":             ".AS",
    "Nyse Euronext - Euronext Amsterdam": ".AS",
    "Bolsa De Madrid":                ".MC",
    "Bolsa Madrid":                   ".MC",
    "Bolsas Y Mercados Espanoles":    ".MC",
    "Six Swiss Exchange":             ".SW",
    "SIX Swiss Exchange":             ".SW",
    "Six Swiss Exchange Ag":          ".SW",
    "Swiss Exchange":                 ".SW",
    "Nyse Euronext - Euronext Brussels": ".BR",
    "Euronext Brussels":              ".BR",
    "Stockholm Stock Exchange":       ".ST",
    "Nasdaq Stockholm":               ".ST",
    "Nasdaq Helsinki":                ".HE",
    "Helsinki Stock Exchange":        ".HE",
    "Nasdaq Omx Helsinki Ltd.":       ".HE",
    "Copenhagen Stock Exchange":      ".CO",
    "Nasdaq Copenhagen":              ".CO",
    "Omx Nordic Exchange Copenhagen A/S": ".CO",
    "Oslo Stock Exchange":            ".OL",
    "Oslo Bors":                      ".OL",
    "Oslo Bors Asa":                  ".OL",
    "Nyse Euronext - Euronext Lisbon": ".LS",
    "Vienna Stock Exchange":          ".VI",
    "Wiener Boerse Ag":               ".VI",
    "Warsaw Stock Exchange":          ".WA",
    "Warsaw Stock Exchange/Equities/Main Market": ".WA",
    "Prague Stock Exchange":          ".PR",
    "Athens Stock Exchange":          ".AT",
    "Irish Stock Exchange":           ".IR",
    "Irish Stock Exchange - All Market": ".IR",
    # Asia
    "Tokyo Stock Exchange":           ".T",
    "Hong Kong Exchanges And Clearing Ltd": ".HK",
    "Hong Kong Exchanges":            ".HK",
    "Hong Kong Stock Exchange":       ".HK",
    "National Stock Exchange Of India": ".NS",
    "Bombay Stock Exchange":          ".BO",
    "Korea Exchange":                 ".KS",
    "Korea Stock Exchange":           ".KS",
    "Taiwan Stock Exchange":          ".TW",
    "Shanghai Stock Exchange":        ".SS",
    "Shenzhen Stock Exchange":        ".SZ",
    "Singapore Exchange":             ".SI",
    # Oceania / Africa / LatAm
    "Asx - All Markets":              ".AX",
    "Australian Securities Exchange": ".AX",
    "Johannesburg Stock Exchange":    ".JO",
    "Bm&Fbovespa":                    ".SA",
    "B3 - Brasil Bolsa Balcao":       ".SA",
    "Bolsa Mexicana De Valores":      ".MX",
    # US (no suffix)
    "Nasdaq":                         "",
    "Nasdaq Stock Market":            "",
    "Nasdaq/Ngs (Global Select Market)": "",
    "New York Stock Exchange Inc.":   "",
    "Nyse":                           "",
    "Nyse Arca":                      "",
    "Cboe Bzx Exchange":              "",
}

# Venues that name a market group rather than a single exchange. The listing
# venue (and hence the yfinance suffix) is disambiguated by the CSV's
# Location column. Observed in iShares Europe-sector CSVs 2018-2026:
# "Nasdaq Omx Nordic" rows are Stockholm listings (Location Sweden) in every
# one of the 8,486 sampled rows; the other locations are mapped defensively.
_AMBIGUOUS_EXCHANGE_BY_LOCATION: dict[str, dict[str, str | None]] = {
    "Nasdaq Omx Nordic": {
        "Sweden":  ".ST",
        "Denmark": ".CO",
        "Finland": ".HE",
        "Iceland": None,   # Nasdaq Iceland has no reliable yfinance data
        "_default": ".ST",
    },
}

# Placeholder venue for unlisted / expired lines in iShares CSVs — these rows
# have no tradable listing and no yfinance history by construction.
_UNLISTED_EXCHANGE_MARKERS = {
    "NO MARKET (E.G. UNLISTED)",
}


def _us_symbol(raw_ticker: str) -> str | None:
    """Normalise a ticker being treated as a US listing, or reject it.

    A US equity symbol never contains whitespace. iShares occasionally serves
    a Bloomberg-style composite instead of a plain ticker — "VSNTV UW", where
    UW is Bloomberg's Nasdaq code — and the US fall-through used to pass that
    straight through. It then resolved at no vendor, sat in the roster as a
    permanently unpriced name, and was counted in the denominator of nothing
    while cluttering the never-resolved list.

    Rejecting is right rather than salvaging the root: the composite tells us
    the upstream field is not the field we think it is, and guessing "VSNTV"
    would invent a security. Returning None drops the row exactly as an
    unlisted placeholder is dropped.
    """
    if not raw_ticker or any(c.isspace() for c in raw_ticker):
        return None
    return raw_ticker.rstrip(".").replace(".", "-")


def _resolve_yf_symbol(raw_ticker: str, exchange: str | None,
                         overrides: dict | None = None,
                         location: str | None = None) -> str | None:
    """Map (CSV ticker, Exchange name, Location) to a yfinance-ready symbol.

    Order of resolution:
      1. Explicit ticker_overrides (highest priority) — used for share-class
         quirks like BRK.B / BRKB → BRK-B.
      2. Exchange-based suffix mapping; market-group venues (e.g. "Nasdaq
         Omx Nordic") disambiguate the listing venue via `location`.
      3. If exchange unknown or empty → return raw ticker as-is (assume US).

    Returns None when the ticker is empty / unparseable, or when the row is
    an unlisted placeholder with no tradable listing.
    """
    if not raw_ticker:
        return None
    raw_ticker = raw_ticker.strip()
    overrides = overrides or {}
    if raw_ticker in overrides:
        return overrides[raw_ticker]

    def yf_base(symbol_root: str, suffix: str) -> str | None:
        """Normalise the local-listing root for a given yfinance suffix.

        Returns None when the row is a non-tradable entitlement (e.g. .RI
        rights) that has no stable yfinance history.
        """
        root = symbol_root.rstrip(".")
        # Rights / entitlement rows do not have stable yfinance histories.
        if root.endswith(".RI"):
            return None
        # Spain: iShares Europe files occasionally append .D entitlement
        # markers to the ordinary local ticker. yfinance uses the ordinary
        # listing (e.g. REP.D.MC → REP.MC).
        if suffix == ".MC" and root.endswith(".D"):
            root = root[:-2]
        # NSE: dashes for dot-separated local roots such as BAJAJ.AUTO →
        # BAJAJ-AUTO; ".RE" rows are rights entitlements that route to the
        # ordinary listing root (e.g. GRASIM.RE.NS → GRASIM.NS).
        if suffix == ".NS":
            if root.endswith(".RE"):
                root = root[:-3]
            return root.replace(".", "-")
        # Share-class spaces in local roots become dashes on yfinance:
        # Stockholm "SEB A" → SEB-A.ST, Helsinki "NDA FI" → NDA-FI.HE,
        # Copenhagen "MAERSK B" → MAERSK-B.CO.
        # LSE slash notation likewise: iShares prints "BA/" for BAE Systems
        # and "NG/" for National Grid (trailing slash marks a trailing dot in
        # the LSE code); an interior slash is a share class ("BT/A").
        # yfinance drops the trailing marker and uses dashes for classes.
        return root.replace(" ", "-").replace("/", "-").rstrip("-")

    if exchange:
        ex_key = exchange.strip()
        if ex_key in _UNLISTED_EXCHANGE_MARKERS:
            return None
        suffix: str | None = _EXCHANGE_TO_YF_SUFFIX.get(ex_key)
        if suffix is None and ex_key in _AMBIGUOUS_EXCHANGE_BY_LOCATION:
            by_loc = _AMBIGUOUS_EXCHANGE_BY_LOCATION[ex_key]
            loc_key = (location or "").strip()
            suffix = by_loc.get(loc_key, by_loc["_default"])
            if suffix is None:
                return None
        if suffix is not None:
            # If the suffix is empty (US listing), apply dot→dash share-class fix.
            if suffix == "":
                return _us_symbol(raw_ticker)
            # If the raw ticker already carries this exchange suffix (e.g.
            # iShares CSV ships "BP.L" with exchange "London Stock Exchange"),
            # do not double-glue — split and re-normalise the root only.
            existing_base, _, existing_suffix = raw_ticker.rpartition(".")
            if f".{existing_suffix}" == suffix and existing_base:
                base = yf_base(existing_base, suffix)
            else:
                base = yf_base(raw_ticker, suffix)
            return f"{base}{suffix}" if base else None
    # Fallback: assume US (no suffix). Apply share-class fix.
    return _us_symbol(raw_ticker)


def parse_holdings(body: str, ticker_overrides: dict | None = None,
                     apply_exchange_suffix: bool = False) -> list[str]:
    """Parse iShares CSV body and return Equity-only yfinance-ready ticker list,
    or [] if the file is empty.

    CSV layout: preamble of fund-level metadata (Fund name, "Fund Holdings as
    of <date>", inception date, totals), then a header row beginning
    'Ticker,Name,Sector,Asset Class,...', then one row per holding, then a
    blank line that terminates the holdings block. The file then continues
    with disclosures we do not need.

    An "empty template" file (no holdings) is detected by 'Fund Holdings as
    of,"-"' or 'Fund Holdings as of,-'.

    Parameters
    ----------
    body : str
        Raw CSV text.
    ticker_overrides : dict, optional
        Maps the raw ticker as it appears in the CSV (e.g. 'BRKB') to the form
        expected downstream by yfinance (e.g. 'BRK-B').
    apply_exchange_suffix : bool
        When True, the Exchange column is used to map each ticker to its
        yfinance symbol with the appropriate suffix (e.g. HSBA → HSBA.L for
        London-listed). Set this True for non-US iShares UCITS funds whose
        constituents trade outside the US. When False (default, US ETFs),
        only the dot→dash share-class conversion is applied.
    """
    if 'Fund Holdings as of,"-"' in body or 'Fund Holdings as of,-' in body:
        return []
    overrides = ticker_overrides or {}
    tickers: list[str] = []
    seen: set[str] = set()
    header: list[str] | None = None
    asset_class_idx: int | None = None
    exchange_idx: int | None = None
    location_idx: int | None = None
    for ln in body.splitlines():
        if header is None:
            if "Ticker" in ln[:20] and "Asset Class" in ln:
                header = next(csv.reader(io.StringIO(ln)))
                asset_class_idx = header.index("Asset Class")
                # Exchange / Location columns may or may not be present (US
                # iShares CSVs sometimes omit them). If missing, the indices
                # stay None and we fall back to the raw ticker / no location.
                try:
                    exchange_idx = header.index("Exchange")
                except ValueError:
                    exchange_idx = None
                try:
                    location_idx = header.index("Location")
                except ValueError:
                    location_idx = None
            continue
        if not ln.strip():
            break  # blank line terminates the holdings block
        row = next(csv.reader(io.StringIO(ln)))
        if not row or not row[0]:
            continue
        if asset_class_idx is not None and len(row) > asset_class_idx:
            if row[asset_class_idx].strip() != "Equity":
                continue
        raw = row[0].strip()
        # iShares CSVs occasionally include a row-level placeholder "-" that
        # corresponds to no real holding. Skip before the resolver, so it
        # cannot glue a dash to an exchange suffix (e.g. "-.PA").
        if raw in {"", "-"}:
            continue
        exchange = (row[exchange_idx].strip() if exchange_idx is not None
                                              and len(row) > exchange_idx
                                              else None)
        location = (row[location_idx].strip() if location_idx is not None
                                              and len(row) > location_idx
                                              else None)
        if apply_exchange_suffix:
            sym = _resolve_yf_symbol(raw, exchange, overrides,
                                     location=location)
        else:
            # Default US path: still apply dot → dash share-class normalisation
            # so the parser output is yfinance-ready (BRK.B → BRK-B).
            sym = overrides.get(raw, raw.replace(".", "-"))
        # Belt-and-braces: catch any "-." / "-" / empty that the resolver
        # could have produced from edge-case inputs.
        if sym is None or sym in {"", "-"} or sym.startswith("-."):
            continue
        if sym in seen:
            continue
        seen.add(sym)
        tickers.append(sym)
    return tickers


def fridays_between(start: date, end: date) -> list[date]:
    """All Fridays in the inclusive range [start, end].

    Uses pandas.date_range with the W-FRI frequency to ensure correct
    day-of-week handling across month, year, and leap-year boundaries.
    Never compute weekdays from memory.
    """
    rng = pd.date_range(start=start, end=end, freq="W-FRI")
    return [d.date() for d in rng]


def latest_completed_friday(today: date) -> date:
    """Return the most recent Friday strictly before `today`.

    Python's date.weekday() returns Monday=0 ... Sunday=6, so Friday=4.
    If today itself is a Friday, return last Friday — we want a settled file.
    """
    days_since_friday = (today.weekday() - 4) % 7
    if days_since_friday == 0:
        days_since_friday = 7
    return today - timedelta(days=days_since_friday)


def load_snapshot_tickers(target: date, etf_cfg: dict,
                          latency: LatencyCircuit | None = None) -> list[str]:
    """Return the Equity roster for one calendar date, cache-first.

    Resolution order:
      1. Legacy CSV cache (`SYM_YYYYMMDD.csv`) — the ~10,400 files captured
         before the 2026-07 re-platform. Still the source of truth for
         history; never re-fetched.
      2. JSON cache (`SYM_YYYYMMDD.json`) — product-data API responses.
      3. Network, via the product-data API.

    Only POSITIVE responses are cached. The old code also cached empty
    responses on the theory that they are stable for old dates, but an empty
    response for a RECENT date usually means "holdings are not published
    yet", and caching that freezes the gap permanently.

    Raises EndpointUnavailable / PayloadContractError when the transport is
    dead. Returns [] when the endpoint is healthy but has no data for
    `target` — the walkback's cue to try the previous day.

    `latency`, when supplied, is fed ONLY the network path below. Every return
    above it is a cache read of a few milliseconds, and mixing those into the
    mean is what would let a warm cache hide a stalled endpoint: an ETF whose
    dates are 95% cached would show a healthy average no matter how slow the
    remaining 5% ran.
    """
    overrides = etf_cfg.get("ticker_overrides", {})
    apply_suffix = etf_cfg.get("apply_exchange_suffix", False)
    symbol = etf_cfg["symbol"]
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = target.strftime("%Y%m%d")

    csv_path = RAW_DIR / f"{symbol}_{stamp}.csv"
    if csv_path.exists():
        cached = csv_path.read_text(encoding="utf-8")
        if looks_like_ishares_holdings_csv(cached):
            if latency is not None:
                latency.record_cache_hit()
            return parse_holdings(cached, ticker_overrides=overrides,
                                   apply_exchange_suffix=apply_suffix)
        # Poisoned by an earlier run's anti-bot HTML — drop and fall through.
        csv_path.unlink()

    json_path = RAW_DIR / f"{symbol}_{stamp}.json"
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            json_path.unlink()
        else:
            if latency is not None:
                latency.record_cache_hit()
            if payload.get("_no_holdings"):
                return []
            return parse_holdings_json(
                payload, target, ticker_overrides=overrides,
                apply_exchange_suffix=apply_suffix,
            )

    if latency is None:
        payload = fetch_product_data(target, etf_cfg)
    else:
        t0 = time.monotonic()
        try:
            payload = fetch_product_data(target, etf_cfg)
        finally:
            # Timed in a finally so a date that dies on the full retry ladder
            # still counts. A run where every date fails is EndpointCircuit's
            # job, but a run that mixes failures and slow successes is nobody
            # else's, and dropping the failures would flatter the mean.
            latency.record_served(time.monotonic() - t0, item=target)
    tickers = parse_holdings_json(
        payload, target, ticker_overrides=overrides,
        apply_exchange_suffix=apply_suffix,
    )
    if tickers:
        json_path.write_text(json.dumps(payload), encoding="utf-8")
    elif (date.today() - target).days > NEGATIVE_CACHE_MIN_AGE_DAYS:
        # Settled no-data date — record a marker so we never pay for it
        # again. Storing the full payload would be pure waste: it is the
        # latest-date fallback response, and all we need to remember is
        # that this date has nothing.
        json_path.write_text(
            json.dumps({
                "_no_holdings": True,
                "requested_as_of": target.strftime("%Y%m%d"),
                "captured_utc": datetime.now(timezone.utc).isoformat(),
                "note": ("Endpoint returned no holdings for this date "
                          "(pre-inception, holiday, or data gap)."),
            }),
            encoding="utf-8",
        )
    return tickers


def get_snapshot(
    target_friday: date, etf_cfg: dict,
    circuit: EndpointCircuit | None = None,
    latency: LatencyCircuit | None = None,
) -> tuple[list[str] | None, date | None, str]:
    """Walk back from `target_friday` looking for a populated holdings file.

    Returns (tickers, actual_date, status). `status` is one of:
      - "exact"     : Friday returned data
      - "walkback"  : an earlier weekday in the same week returned data
      - "not_found" : endpoint healthy, no data within MAX_WALKBACK_DAYS days
      - "endpoint_unavailable" : the transport is dead (see EndpointCircuit)

    Note on the walkback: the previous version let a transport exception
    propagate out of this loop, so a failed fetch on the target Friday
    aborted the walk on its first iteration and MAX_WALKBACK_DAYS never
    applied. Transport failures now trip the breaker explicitly, and a
    genuinely empty date continues the walk as intended.
    """
    if circuit is not None and circuit.dead:
        circuit.n_unavailable += 1
        return None, None, "endpoint_unavailable"

    for days_back in range(MAX_WALKBACK_DAYS + 1):
        try_date = target_friday - timedelta(days=days_back)
        try:
            tickers = load_snapshot_tickers(try_date, etf_cfg, latency=latency)
        except (EndpointUnavailable, PayloadContractError) as e:
            if circuit is None:
                raise
            circuit.trip(target_friday, str(e))
            circuit.n_unavailable += 1
            return None, None, "endpoint_unavailable"
        if tickers:
            status = "exact" if days_back == 0 else "walkback"
            return tickers, try_date, status
    return None, None, "not_found"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--etf", default=DEFAULT_ETF,
        help=f"ETF symbol to fetch (must be in etf_registry). Default: {DEFAULT_ETF}",
    )
    p.add_argument(
        "--carry-forward-on-outage", action="store_true",
        help="Carry the last known-good roster forward across Fridays that "
             "the endpoint could not serve. OFF by default: carrying forward "
             "through an outage is what let a dead endpoint look like a "
             "routine holiday gap for four weeks. Use only when a degraded "
             "but running pipeline is explicitly wanted; the run still exits "
             f"{EXIT_ENDPOINT_UNAVAILABLE}.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    etf_cfg = get_etf(args.etf)
    symbol = etf_cfg["symbol"]
    start_friday: date = etf_cfg["start_friday"]
    out_path = DATA_DIR / f"constituents_{symbol.lower()}.json"

    today = date.today()
    end_friday = latest_completed_friday(today)
    fridays = fridays_between(start_friday, end_friday)
    print(
        f"Fetching {symbol} point-in-time holdings for {len(fridays)} Fridays "
        f"({start_friday} -> {end_friday})",
        flush=True,
    )

    snapshots: dict[str, dict] = {}
    walkbacks: list[dict] = []
    carry_forwards: list[dict] = []
    edgar_used: list[dict] = []  # Phase 26.2 — audit trail
    unavailable: list[dict] = []  # Phase 27 — endpoint-outage audit trail
    circuit = EndpointCircuit()
    latency = LatencyCircuit(label=f"{etf_cfg['symbol']} holdings endpoint")
    prev_tickers: list[str] | None = None
    prev_actual: date | None = None
    prev_target: date | None = None

    # Phase 26.2 (2026-05-31) — lazy-loaded SEC EDGAR fallback. When the
    # primary iShares endpoint returns blocked HTML or empty CSV AND the
    # ETF has an edgar_nport entry in its registry, we drop down to the
    # most recent N-PORT-P filing for the series. Loaded once per run
    # to amortise the SEC scan cost. See scripts/edgar_nport.py and
    # DATA_INTEGRITY_POLICY.md section 2.1 for the full story.
    edgar_cfg = etf_cfg.get("edgar_nport")
    edgar_roster_cache: dict | None = None  # sentinel: None = not loaded yet
    edgar_roster_date: date | None = None

    def _try_edgar(target: date) -> tuple[list[str] | None, date | None]:
        """Return (tickers, snapshot_date) from EDGAR if a roster is
        available with repPdEnd <= target, else (None, None). Loads the
        EDGAR roster on first call. Always prefer the freshest source
        — caller decides whether EDGAR beats the carry-forward source."""
        nonlocal edgar_roster_cache, edgar_roster_date
        if not edgar_cfg:
            return None, None
        if edgar_roster_cache is None:
            # Sentinel: cache the entire (roster, date) tuple including
            # the "EDGAR returned nothing" outcome so we do not retry.
            try:
                from edgar_nport import fetch_roster_via_edgar
                roster = fetch_roster_via_edgar(
                    edgar_cfg["cik"], edgar_cfg["series_id"],
                )
            except Exception as e:
                print(f"  EDGAR lookup failed for {symbol}: {e}", flush=True)
                roster = None
            if roster is not None:
                edgar_roster_cache = {
                    "tickers": roster.tickers,
                    "rep_pd_end": roster.filing.report_period_end,
                    "filing_date": roster.filing.filing_date,
                    "accession": roster.filing.accession_number,
                }
                edgar_roster_date = date.fromisoformat(
                    roster.filing.report_period_end
                )
                print(
                    f"  EDGAR roster loaded for {symbol}: "
                    f"{len(roster.tickers)} tickers from "
                    f"N-PORT-P filed {roster.filing.filing_date} "
                    f"(repPdEnd {roster.filing.report_period_end})",
                    flush=True,
                )
            else:
                edgar_roster_cache = {}  # marker — "loaded, returned nothing"
        if not edgar_roster_cache:
            return None, None
        if edgar_roster_date and edgar_roster_date <= target:
            return list(edgar_roster_cache["tickers"]), edgar_roster_date
        return None, None

    for i, friday in enumerate(fridays, start=1):
        if i == 1 or i % 25 == 0 or i == len(fridays):
            print(f"  [{i}/{len(fridays)}] {friday.isoformat()}", flush=True)
        # Checked at the TOP of the date, not the bottom: tripping mid-walk
        # and then serving one more date would write a roster whose last entry
        # was fetched after the run had already decided it could not trust the
        # endpoint's timing. Abort before, not after.
        if latency.dead:
            raise EndpointDegraded(latency.reason or "endpoint degraded")
        try:
            tickers, actual, status = get_snapshot(friday, etf_cfg, circuit,
                                                   latency=latency)
        except Exception as e:
            print(f"  ERROR on {friday}: {e}", flush=True)
            tickers, actual, status = None, None, "not_found"

        # Phase 26.2 — when primary fails for this Friday and an EDGAR
        # source is registered, try EDGAR. Only USE EDGAR if its
        # roster is fresher than what carry-forward would produce —
        # else carry-forward is still the right choice.
        if tickers is None and edgar_cfg:
            edgar_tickers, edgar_date = _try_edgar(friday)
            if edgar_tickers and edgar_date:
                carry_date = prev_actual if prev_actual else None
                edgar_is_fresher = (
                    carry_date is None or edgar_date > carry_date
                )
                if edgar_is_fresher:
                    tickers = edgar_tickers
                    actual = edgar_date
                    status = "edgar_nport"
                    edgar_used.append({
                        "target_friday": friday.isoformat(),
                        "edgar_actual_date": edgar_date.isoformat(),
                        "accession": edgar_roster_cache["accession"],
                        "filing_date": edgar_roster_cache["filing_date"],
                        "n_tickers": len(tickers),
                        "carry_forward_alternative_date": (
                            carry_date.isoformat() if carry_date else None
                        ),
                    })

        if tickers is None or actual is None:
            # Phase 27 — separate "the endpoint is dead" from "this Friday
            # genuinely has no holdings". Conflating them is what made a
            # four-week outage read as a run of ordinary holiday gaps.
            outage = status == "endpoint_unavailable"
            if outage:
                unavailable.append({
                    "target_friday": friday.isoformat(),
                    "cause": "endpoint_unavailable",
                    "reason": circuit.reason,
                })
                if not args.carry_forward_on_outage:
                    # No snapshot and no carry-forward: the honest record of
                    # an outage is absence, not a fabricated roster.
                    continue
            cause = "endpoint_unavailable" if outage else "no_data_in_walkback"
            gap = (
                "upstream endpoint unavailable — see endpoint_health"
                if outage else
                f"no holdings data within {MAX_WALKBACK_DAYS} days back from "
                "target Friday"
            )
            if prev_tickers is None or prev_actual is None or prev_target is None:
                carry_forwards.append({
                    "target_friday": friday.isoformat(),
                    "outcome": "skipped",
                    "cause": cause,
                    "reason": f"{gap} and no prior snapshot to carry forward",
                })
                continue
            carry_forwards.append({
                "target_friday": friday.isoformat(),
                "outcome": "carried_forward",
                "cause": cause,
                "carried_from_target": prev_target.isoformat(),
                "carried_from_actual": prev_actual.isoformat(),
                "reason": f"{gap} — reused most recent prior snapshot",
            })
            snapshots[friday.isoformat()] = {
                "actual_date": prev_actual.isoformat(),
                "carried_forward_from": prev_target.isoformat(),
                "n_tickers": len(prev_tickers),
                "tickers": prev_tickers,
            }
        else:
            snap_entry: dict = {
                "actual_date": actual.isoformat(),
                "n_tickers": len(tickers),
                "tickers": tickers,
            }
            # Phase 26.2 — record the fallback source so the audit
            # trail distinguishes iShares-derived vs EDGAR-derived
            # snapshots. Absent field means primary (iShares) was used.
            if status == "edgar_nport":
                snap_entry["source"] = "edgar_nport"
            snapshots[friday.isoformat()] = snap_entry
            if status == "walkback":
                walkbacks.append({
                    "target_friday": friday.isoformat(),
                    "fallback_date": actual.isoformat(),
                    "days_back": (friday - actual).days,
                    "reason": (
                        "Friday holdings missing (likely US market holiday) — used "
                        "nearest prior trading day"
                    ),
                })
            prev_tickers, prev_actual, prev_target = tickers, actual, friday

    # Staleness check (Phase 26.1) — compute days since the most recent
    # REAL fetch (any snapshot that is not a carry-forward). The "today"
    # anchor uses calendar days from the latest target Friday so the test
    # is deterministic across local + CI clocks; using datetime.utcnow()
    # would make the alert flap across timezone boundaries.
    real_snapshot_dates: list[date] = []
    for snap in snapshots.values():
        if "carried_forward_from" in snap:
            continue
        try:
            real_snapshot_dates.append(date.fromisoformat(snap["actual_date"]))
        except (KeyError, ValueError):
            continue
    last_real_fetch_date = max(real_snapshot_dates) if real_snapshot_dates else None
    warn_days, critical_days = resolve_staleness_thresholds(etf_cfg)
    if last_real_fetch_date is not None:
        days_since_real = (end_friday - last_real_fetch_date).days
        if days_since_real > critical_days:
            staleness_status = "critical"
        elif days_since_real > warn_days:
            staleness_status = "warning"
        else:
            staleness_status = "fresh"
    else:
        days_since_real = None
        staleness_status = "no_real_fetches"
    staleness_override = etf_cfg.get("staleness") or {}

    payload = {
        "etf": symbol,
        "source": PRODUCT_DATA_API,
        # The pre-2026-07 history in data/raw_ishares/*.csv came from this
        # route. It stopped serving CSV when iShares re-platformed; retained
        # for provenance of the cached snapshots only.
        # None for funds onboarded after Phase 27 — they never had a CSV
        # route and their history comes entirely from the product-data API.
        "legacy_csv_source": etf_cfg.get("csv_url_template"),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_friday": start_friday.isoformat(),
        "end_friday": end_friday.isoformat(),
        "n_target_fridays": len(fridays),
        "n_snapshots_written": len(snapshots),
        "membership_assumption": (
            f"Constituents held static between weekly Friday snapshots. "
            f"Index typically rebalances quarterly; weekly oversamples "
            f"membership and protects against off-cycle add/drops."
        ),
        "asset_class_filter": "Equity",
        "ticker_overrides_applied": etf_cfg.get("ticker_overrides", {}),
        "walkbacks": walkbacks,
        "carry_forwards": carry_forwards,
        "edgar_used": edgar_used,
        # Phase 27 — every Friday the transport could not serve, and why.
        # An empty list with status "ok" is the only healthy reading.
        "endpoint_unavailable": unavailable,
        "endpoint_health": {
            "status": "unavailable" if circuit.dead else "ok",
            "transport": "product_data_api",
            "endpoint": PRODUCT_DATA_API,
            "detail": circuit.reason,
            "first_failure_target_friday": (
                circuit.first_failure_target.isoformat()
                if circuit.first_failure_target else None
            ),
            "n_fridays_unavailable": circuit.n_unavailable,
            "carry_forward_on_outage": args.carry_forward_on_outage,
            "policy_ref": "DATA_INTEGRITY_POLICY.md",
        },
        "staleness": {
            "last_real_fetch_date": (
                last_real_fetch_date.isoformat()
                if last_real_fetch_date else None
            ),
            "days_since_last_real_fetch": days_since_real,
            "status": staleness_status,
            "warn_threshold_days": warn_days,
            "critical_threshold_days": critical_days,
            "threshold_source": (
                "per_etf_override" if staleness_override else "global_default"
            ),
            "threshold_rationale": (
                staleness_override.get("rationale")
                if staleness_override else None
            ),
            "policy_ref": "DATA_INTEGRITY_POLICY.md",
        },
        "snapshots": snapshots,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    print(
        f"Wrote {out_path.relative_to(PROJECT_ROOT)} -- "
        f"{len(snapshots)} snapshots, "
        f"{len(walkbacks)} walkbacks, "
        f"{len(carry_forwards)} carry-forwards, "
        f"{len(edgar_used)} EDGAR fallbacks"
    )
    if edgar_used:
        print(
            f"  EDGAR (N-PORT-P) used for {len(edgar_used)} Friday "
            f"snapshot(s) — source snapshot date "
            f"{edgar_used[0]['edgar_actual_date']}, accession "
            f"{edgar_used[0]['accession']}"
        )
    if walkbacks:
        print(f"  First walkback: {walkbacks[0]}")
    if carry_forwards:
        print("  Carry-forwards in use:")
        for cf in carry_forwards:
            print(f"    {cf}")

    # Endpoint-outage alert (Phase 27). Raised BEFORE the staleness alert
    # because a dead transport is the cause and staleness is the symptom.
    if circuit.dead:
        bar = "!" * 72
        print(file=sys.stderr)
        print(bar, file=sys.stderr)
        print(
            f"ENDPOINT UNAVAILABLE: {symbol} — the holdings transport failed "
            f"at target Friday {circuit.first_failure_target}. "
            f"{circuit.n_unavailable} Friday(s) could not be served.",
            file=sys.stderr,
        )
        print(f"  Endpoint: {PRODUCT_DATA_API}", file=sys.stderr)
        print(f"  Detail:   {circuit.reason}", file=sys.stderr)
        if args.carry_forward_on_outage:
            print(
                "  Carry-forward was ENABLED for the outage: the affected "
                "Fridays hold a stale roster and are flagged with "
                "cause=endpoint_unavailable.",
                file=sys.stderr,
            )
        else:
            print(
                "  No carry-forwards were emitted for those Fridays — they "
                "are absent from `snapshots` by design.",
                file=sys.stderr,
            )
        print(
            "  Operator action required. See DATA_INTEGRITY_POLICY.md "
            "section 'Escalation procedure'.",
            file=sys.stderr,
        )
        print(bar, file=sys.stderr)

    # Staleness alert (Phase 26.1, per-ETF thresholds since 26.3).
    # Loud failure on critical so CI fails.
    threshold_label = (
        " (per-ETF override)" if staleness_override else " (global default)"
    )
    if staleness_status == "critical":
        bar = "!" * 72
        print(file=sys.stderr)
        print(bar, file=sys.stderr)
        print(
            f"CRITICAL: {symbol} roster is {days_since_real} days stale "
            f"(last real fetch {last_real_fetch_date}). "
            f"Threshold {critical_days} days{threshold_label} exceeded.",
            file=sys.stderr,
        )
        print(
            f"Operator action required. See DATA_INTEGRITY_POLICY.md "
            f"section 'Escalation procedure' for remediation.",
            file=sys.stderr,
        )
        print(bar, file=sys.stderr)
        # A dead endpoint outranks stale data: it is the cause, not the
        # symptom, and it is what the operator has to fix.
        return (EXIT_ENDPOINT_UNAVAILABLE if circuit.dead
                else EXIT_STALENESS_CRITICAL)
    if staleness_status == "warning":
        print(
            f"  WARNING: {symbol} roster is {days_since_real} days stale "
            f"(last real fetch {last_real_fetch_date}). "
            f"Threshold for critical alert is {critical_days} days"
            f"{threshold_label}."
        )
    elif staleness_status == "fresh" and last_real_fetch_date is not None:
        print(
            f"  Staleness OK: last real fetch {last_real_fetch_date} "
            f"({days_since_real} days ago, "
            f"under {warn_days}-day warning threshold{threshold_label})."
        )
    return EXIT_ENDPOINT_UNAVAILABLE if circuit.dead else EXIT_OK


def cli() -> int:
    """Entry point. Turns a degraded endpoint into an exit code, not a stack.

    EndpointDegraded is deliberately allowed to unwind out of the walk rather
    than being handled where it is raised: everything between the walk and
    here writes the roster, and unwinding past all of it is what guarantees
    the committed roster survives untouched. A handler at the raise site would
    have to remember not to write, and remembering is what failed here in the
    first place.
    """
    try:
        return main()
    except EndpointDegraded as e:
        bar = "!" * 72
        print(file=sys.stderr)
        print(bar, file=sys.stderr)
        print(f"ENDPOINT DEGRADED: {e}", file=sys.stderr)
        print("  No roster was written. The committed one is untouched and "
              "is strictly better than a partial walk.", file=sys.stderr)
        print(bar, file=sys.stderr)
        return EXIT_ENDPOINT_DEGRADED


if __name__ == "__main__":
    sys.exit(cli())
