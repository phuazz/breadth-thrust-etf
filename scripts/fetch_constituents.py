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

  Failure taxonomy — the point of Phase 27 is that these are no longer
  interchangeable (extended 2026-09-22; see EXIT_PRECEDENCE for which code
  wins when several fire at once):
    - walkback / carry_forward : this Friday has no holdings (holiday, data
                                 gap). Endpoint healthy. Exit 0. The ONLY
                                 soft class, because it is the only one that
                                 heals without anyone doing anything.
    - endpoint_unavailable     : the transport is dead. The walk
                                 short-circuits on the first failure, no
                                 carry-forwards are emitted, exit 3.
    - roster_refused           : the transport is healthy and the issuer
                                 published, but too much of the roster
                                 resolves at no vendor. Recorded in
                                 roster_refusals, exit 6. Does not heal on
                                 its own: map the venue.
    - unexpected_error         : the walk raised something none of the above
                                 describes. Recorded in walk_errors, exit 7.
                                 Never relabelled as a vendor gap.
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
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import get_etf, roster_rules  # noqa: E402
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
# 2026-09-22 — the transport is healthy, the issuer published, the payload is
# well-formed and FOR the date requested, and we declined to use it because
# too much of it resolves at no vendor (UnmappedExchangeError). A fourth
# class, because the operator's action differs from all three above and is
# small and definite: map the venue in _EXCHANGE_TO_YF_SUFFIX.
#
# WHY THIS IS NOT SOFT, WHICH IS THE WHOLE POINT OF THE CLASS. A vendor gap
# heals on its own; a refusal never does. Carry-forward is the right answer
# to a transient absence that will resolve itself, which is exactly why
# no_data_in_walkback keeps exiting 0. A venue string will not map itself, so
# every further day of carry-forward drifts the roster with no prospect of
# self-repair, and the only backstop is a 14-day staleness warning. On
# 2026-09-18 that asymmetry left EXV1 — sleeve D's largest line — ranking a
# fill week on the previous week's roster, green throughout.
EXIT_ROSTER_REFUSED = 6
# 2026-09-22 — the walk raised something none of the classes above describes.
# Previously every such exception was caught by a blanket handler and written
# as cause "no_data_in_walkback", i.e. an unknown failure was recorded as a
# routine vendor absence and exited 0. An unclassified failure is not evidence
# that the issuer published nothing; it is evidence that we do not know what
# happened, and it must not wear a healthy label.
EXIT_UNEXPECTED_WALK_ERROR = 7

# EXIT PRECEDENCE (2026-09-22), highest first. Stated once, applied once, in
# ``walk_exit_code`` below.
#
#   EXIT_ENDPOINT_UNAVAILABLE (3)  transport dead
#   EXIT_ROSTER_REFUSED       (6)  roster refused
#   EXIT_UNEXPECTED_WALK_ERROR(7)  unclassified failure
#   EXIT_STALENESS_CRITICAL   (2)  roster aged past policy
#   EXIT_OK                   (0)
#
# EXIT_ENDPOINT_DEGRADED (5) sits outside this ladder: it unwinds as an
# exception from the walk and is handled in cli(), before any roster is
# written.
#
# Causes outrank symptoms, which is the rule the pre-existing "a dead endpoint
# outranks stale data" comment already applied: a dead transport is why a
# refusal cannot even be assessed, and a refusal is one of the things that
# produces staleness in the first place.
#
# THE GUARANTEE THIS LADDER MUST NOT BREAK. The exit code names ONE class.
# On every path that REACHES it, each class that fired prints its own alert
# block on stderr and writes its own array into the roster payload, so a
# lower-precedence class is never hidden by a higher one — in particular, a
# refusal's venue strings and affected symbols are recorded and printed
# whether or not the refusal wins the exit code.
#
# ONE PATH DOES NOT REACH IT, and the claim is bounded accordingly.
# EndpointDegraded unwinds out of the walk to cli() without writing a roster
# at all — deliberately, so a stalled endpoint cannot leave a partial one — so
# on that path NO array is written, including roster_refusals. Refusals
# already encountered are printed to stderr before the raise instead, which is
# what the scheduled run's retained log keeps. Do not describe the payload
# arrays as unconditional; they are not.
#
# Nothing downstream may infer "no refusal" from an exit code that is not 6;
# that question is answered by ``roster_refusals``, and on a degraded-endpoint
# abort only by the log.
EXIT_PRECEDENCE = (
    EXIT_ENDPOINT_UNAVAILABLE,
    EXIT_ROSTER_REFUSED,
    EXIT_UNEXPECTED_WALK_ERROR,
    EXIT_STALENESS_CRITICAL,
)


def walk_exit_code(*, endpoint_dead: bool, n_refusals: int,
                   n_unexpected: int, staleness_status: str) -> int:
    """The single exit code for a completed walk, by EXIT_PRECEDENCE.

    Pure, so the precedence is unit-testable without a walk. Returns EXIT_OK
    only when no failure class fired at all — a run with a refusal can never
    report success, whatever else is or is not wrong with it.
    """
    # Only "critical" is a failing staleness status HERE, deliberately.
    # check_refresh_guard's G3 additionally fails "no_real_fetches", but that
    # is a cross-panel commit gate reading committed state; the fetcher has
    # always exited 0 on it, and widening that is a separate decision from
    # this one. Leave it where it is rather than changing two things at once.
    fired = {
        EXIT_ENDPOINT_UNAVAILABLE: endpoint_dead,
        EXIT_ROSTER_REFUSED: n_refusals > 0,
        EXIT_UNEXPECTED_WALK_ERROR: n_unexpected > 0,
        EXIT_STALENESS_CRITICAL: staleness_status == "critical",
    }
    for code in EXIT_PRECEDENCE:
        if fired[code]:
            return code
    return EXIT_OK


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
    symbol: str | None = None,
    strict_exchanges: bool = True,
    exclude_symbols: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Parse a product-data API payload into a yfinance-ready ticker list.

    ``exclude_symbols`` drops RESOLVED symbols after the resolver has run —
    rights, paid/nil-paid and tendered lines that are transient by
    construction and inflate the breadth denominator for a week or two each
    (see etf_registry.roster_rules). None or empty means deployed behaviour.

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
    excluded = exclude_symbols or frozenset()
    tickers: list[str] = []
    seen: set[str] = set()
    unmapped: dict[str, list[str]] = {}
    n_equity = 0
    for i in range(n):
        if (cell("assetClass", i) or "").strip() != "Equity":
            continue
        raw = str(cell("ticker", i) or "").strip()
        # Mirrors the CSV parser: iShares emits a "-" placeholder row that
        # corresponds to no real holding.
        if raw in {"", "-"}:
            continue
        n_equity += 1
        exchange = cell("exchange", i)
        location = cell("countryOfRisk", i)
        if apply_exchange_suffix:
            sym = _resolve_yf_symbol(
                raw, (exchange or "").strip() or None, overrides,
                location=(location or "").strip() or None,
                unmapped=unmapped,
            )
        else:
            sym = overrides.get(raw, raw.replace(".", "-"))
        if sym is None or sym in {"", "-"} or sym.startswith("-."):
            continue
        if sym in excluded or sym in seen:
            continue
        seen.add(sym)
        tickers.append(sym)
    report_unmapped_exchanges(unmapped, symbol or "?", n_equity,
                              as_of=target, strict=strict_exchanges)
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
    "Boerse Duesseldorf":             ".DU",
    "Boerse Muenchen":                ".MU",
    "Frankfurt Stock Exchange":       ".F",
    "Nyse Euronext - Euronext Paris": ".PA",
    "Euronext Paris":                 ".PA",
    "Borsa Italiana":                 ".MI",
    "Euronext Amsterdam":             ".AS",
    "Nyse Euronext - Euronext Amsterdam": ".AS",
    "Bolsa De Madrid":                ".MC",
    "Bolsa Madrid":                   ".MC",
    "Bolsas Y Mercados Espanoles":    ".MC",
    "Bme Bolsas Y Mercados Espanoles": ".MC",
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
    # The venue's current name in the iShares feed. It arrived on the
    # 2026-09-18 roster, when STOXX reclassified Greece from EM to DM and
    # four Greek banks entered SX7P: EXV1 carried ALPHA, ETE, EUROB and
    # TPEIR on an unrecognised venue, 4 of 61 equity rows (6.6%), which is
    # past the bound, so the roster was refused and the fetcher carried the
    # 2026-09-11 snapshot forward instead. MOH (EXH1) and MTLN / PPC (EXH9)
    # took the same venue below the bound and only warned.
    # Suffix verified against yfinance 1.1.0 on 2026-09-22: all seven names
    # resolve under .AT with a full year of daily closes to that day's bar,
    # priced in EUR, exchange "Athens", and longName matching the issuer in
    # every case (Alpha Bank, National Bank of Greece, Eurobank, Piraeus
    # Bank, Motor Oil Hellas, Metlen Energy & Metals, Public Power).
    "Athens Exchange S.A. Cash Market": ".AT",
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
    # Taipei Exchange, Taiwan's second board — iShares still prints its
    # pre-2015 name. yfinance uses .TWO, NOT .TW: every one of the 12
    # currently-listed ITWN names carrying this venue resolves under .TWO
    # with full history and 404s under .TW (probed 2026-08-15).
    "Gretai Securities Market":       ".TWO",
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
    "NASDAQ":                         "",
    "Nasdaq Stock Market":            "",
    "Nasdaq/Ngs (Global Select Market)": "",
    "New York Stock Exchange Inc.":   "",
    "Nyse":                           "",
    "Nyse Arca":                      "",
    "Cboe Bzx Exchange":              "",
    # US OTC. ICHN routes a handful of China ADRs here when they drop off a
    # listed venue; the bare ticker is the right symbol form even though a
    # given pink-sheet name may not be carried by the vendor.
    "Non-Nms Quotation Service (Nnqs)": "",
}

# Venues we recognise but deliberately do NOT route by suffix, because the
# vendor's symbol space for them is unusable. Rows keep the raw local code
# (the historical behaviour) so a per-name override downstream can repair
# them; the point of naming them here is that they are a KNOWN gap and are
# excluded from the unmapped-exchange alarm rather than re-reported weekly.
#
# "Bse Ltd" (India, BSE). Appending .BO is not a fix — probed 2026-08-15
# against yfinance 1.1.0 and Yahoo's chart endpoint directly:
#   - Yahoo 404s outright on 4 of 10 BSE scrip codes tested, including TCS
#     (532540), HDFC Bank (500180), ICICI Bank (532174) and Hindustan
#     Aeronautics (541154). Coverage is arbitrary, not systematic.
#   - Where Yahoo does serve the line the payload is malformed —
#     exchangeName "YHD", instrumentType "MUTUALFUND", currency null —
#     and yfinance raises TypeError parsing it at every horizon of three
#     months or more for most names (532483, 500325, 500790). Even the
#     best-behaved code, 534091, breaks at two years. A 200-day breadth
#     panel needs far more history than the route survives.
# The prices themselves are correct when they do come through, so this is a
# broken vendor path and not an absent security: the right resolution is the
# NSE line of the same issuer, which is what YF_TICKER_OVERRIDES in
# compute_breadth.py does for the two names NDIA actually holds.
_EXCHANGE_ROUTE_UNAVAILABLE: dict[str, str] = {
    "Bse Ltd": (
        "yfinance/Yahoo .BO coverage is partial and its metadata breaks the "
        "client at the history lengths breadth needs; route the issuer's NSE "
        "line via YF_TICKER_OVERRIDES instead"
    ),
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


class RosterRefusal(RuntimeError):
    """This date's roster is REFUSED and must never read as vendor absence.

    The base of the refusal class. Two things raise it: a roster whose venues
    we cannot resolve (UnmappedExchangeError), and evidence of an earlier such
    refusal that we can no longer read (RefusalEvidenceError). They differ in
    what the operator does about them and are recorded separately, but they
    share one property that the walk depends on — an unresolved refusal is
    AUTHORITATIVE for its source date. Nothing clears it except resolving that
    date: not an older positive cache, not a vendor gap, not an EDGAR
    fallback, not a later successful Friday.

    Subclasses populate the attributes below; the defaults let a partially
    described refusal still produce a well-formed record rather than a
    KeyError in the reporting path.
    """

    def __init__(self, message: str, *, symbol: str | None = None,
                 as_of: date | None = None) -> None:
        super().__init__(message)
        self.symbol = symbol
        self.as_of = as_of
        self.exchanges: list[str] = []
        self.affected_symbols: list[str] = []
        self.n_affected = 0
        self.n_equity_rows = 0
        self.share: float | None = None
        # Set by the retention path when keeping the vendor response failed;
        # the ORIGINAL refusal still propagates, with the storage failure
        # recorded beside it rather than replacing it.
        self.evidence_retained: bool | None = None
        self.evidence_error: str | None = None
        self.evidence_unreadable = False
        self.evidence_quarantined: str | None = None

    def as_record(self, target_friday: date) -> dict:
        """The refusal as it is written into the roster payload.

        ``target_friday`` is the Friday the walk was serving; ``as_of`` is the
        date actually attempted, which differs whenever the walkback had
        already stepped back from the Friday. Both are recorded: a reader
        repairing the venue needs the second, and a reader auditing which week
        was affected needs the first.
        """
        return {
            "target_friday": target_friday.isoformat(),
            "source_date": self.as_of.isoformat() if self.as_of else None,
            "kind": type(self).__name__,
            "exchanges": sorted(self.exchanges),
            "affected_symbols": sorted(self.affected_symbols),
            "n_affected": self.n_affected,
            "n_equity_rows": self.n_equity_rows,
            # Row share, not portfolio weight. See UnmappedExchangeError.
            "share_of_equity_rows": self.share,
            "evidence_retained": self.evidence_retained,
            "evidence_error": self.evidence_error,
            "evidence_unreadable": self.evidence_unreadable,
            "evidence_quarantined": self.evidence_quarantined,
            "detail": str(self),
        }


class RefusalEvidenceError(RosterRefusal):
    """A retained refusal exists for this date but cannot be read back.

    Raised rather than swallowed because the alternative is the defect this
    class was written to close: the reader used to unlink a corrupt sidecar
    and fall through to the endpoint, so a truncated file plus a quiet vendor
    day plus a parseable Thursday produced status "walkback" — a refused date
    silently rebuilt as an ordinary capture.

    A refusal we cannot read is still a refusal. The evidence is quarantined
    rather than deleted, because it is the only record of what was refused.
    """

    def __init__(self, message: str, *, symbol: str | None = None,
                 as_of: date | None = None,
                 quarantined: str | None = None) -> None:
        super().__init__(message, symbol=symbol, as_of=as_of)
        self.evidence_unreadable = True
        self.evidence_quarantined = quarantined


class UnmappedExchangeError(RosterRefusal):
    """Too much of a roster resolved through the assume-US fall-through.

    Raised rather than warned once the share crosses
    ``UNMAPPED_EXCHANGE_MAX_SHARE``, on the same reasoning as
    PayloadContractError: a roster that silently lost a tenth of its names
    is worse than a roster that failed to build, because breadth is a ratio
    and a dropped name leaves BOTH the numerator and the denominator, so
    the figure stays plausible while measuring a different universe.

    CARRIES ITS OWN EVIDENCE (2026-09-22). The attributes below exist so
    that every consumer reads STRUCTURED fields rather than re-deriving the
    facts by parsing ``str(exc)``. A message-text parser is a second,
    unversioned copy of the schema: it breaks the moment the sentence is
    reworded, and it breaks silently, which is the failure class this
    module exists to prevent.

    ``share`` is the share of the roster's EQUITY ROWS that fell through,
    which is the trigger the threshold is written against. It is NOT the
    share of the fund by weight, and it is NOT a bound on how much of the
    fund the missing names represent — a 2% row share can carry far more
    or far less than 2% of NAV. Nothing downstream may read it as a
    portfolio weight.
    """

    def __init__(self, message: str, *, symbol: str | None = None,
                 as_of: date | None = None,
                 exchanges: list[str] | None = None,
                 affected_symbols: list[str] | None = None,
                 n_affected: int = 0, n_equity_rows: int = 0,
                 share: float | None = None) -> None:
        super().__init__(message, symbol=symbol, as_of=as_of)
        self.exchanges = list(exchanges or [])
        self.affected_symbols = list(affected_symbols or [])
        self.n_affected = n_affected
        self.n_equity_rows = n_equity_rows
        self.share = share


# An unrecognised exchange is only visible as a coverage figure someone
# happens to audit, so the fall-through is bounded. The threshold is a
# share of the fund's own equity roster, not an absolute count, because the
# rosters run from ~30 to ~600 names.
#
# Calibrated against the full 2018-2026 cache (10,927 non-US roster-days):
# the Taipei Exchange gap would have tripped it on day one at 9.0% of ITWN
# (7 of 78 names, 3.05% by weight), while every genuine one-off — the
# Bloomberg placeholder rows and corporate-action artefacts that appear for
# one or two Fridays and vanish — sits at or below 0.3% of its roster and
# only warns. MIN_ROWS keeps a small roster from tripping on a single name.
UNMAPPED_EXCHANGE_MAX_SHARE = 0.02
UNMAPPED_EXCHANGE_MIN_ROWS = 3


def report_unmapped_exchanges(
    sink: dict[str, list[str]],
    symbol: str,
    n_equity_rows: int,
    as_of: date | None = None,
    strict: bool = True,
) -> None:
    """Announce every exchange string that fell through to the US branch.

    `sink` is the mapping filled by `_resolve_yf_symbol`: exchange name →
    tickers that carried it. Always prints; raises UnmappedExchangeError
    when the affected share crosses the threshold and `strict` is set.

    A caller that legitimately wants the roster anyway (a historical
    re-parse, an audit) passes strict=False and reads the printed report.
    """
    if not sink:
        return
    n_affected = sum(len(v) for v in sink.values())
    share = n_affected / n_equity_rows if n_equity_rows else 1.0
    stamp = f" {as_of.isoformat()}" if as_of else ""
    print(
        f"  UNMAPPED EXCHANGE in {symbol}{stamp}: {n_affected} of "
        f"{n_equity_rows} equity rows ({share:.1%}) fell through to the "
        f"assume-US branch and will resolve at no vendor.",
        flush=True,
    )
    for ex, tickers in sorted(sink.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(sorted(tickers)[:12])
        more = f" (+{len(tickers) - 12} more)" if len(tickers) > 12 else ""
        print(f"      {ex!r}: {len(tickers)} — {shown}{more}", flush=True)
    print(
        "      Add the venue to _EXCHANGE_TO_YF_SUFFIX once its yfinance "
        "suffix is verified, or to _EXCHANGE_ROUTE_UNAVAILABLE if there "
        "is no usable vendor route.",
        flush=True,
    )
    if strict and n_affected >= UNMAPPED_EXCHANGE_MIN_ROWS and (
            share > UNMAPPED_EXCHANGE_MAX_SHARE):
        raise UnmappedExchangeError(
            f"{symbol}{stamp}: {n_affected} of {n_equity_rows} equity rows "
            f"({share:.1%}) carry an unrecognised exchange "
            f"({', '.join(sorted(sink))}), above the "
            f"{UNMAPPED_EXCHANGE_MAX_SHARE:.0%} bound. Breadth computed on "
            f"this roster would silently measure a smaller universe.",
            symbol=symbol,
            as_of=as_of,
            exchanges=sorted(sink),
            affected_symbols=sorted(t for v in sink.values() for t in v),
            n_affected=n_affected,
            n_equity_rows=n_equity_rows,
            share=share,
        )


def _resolve_yf_symbol(raw_ticker: str, exchange: str | None,
                         overrides: dict | None = None,
                         location: str | None = None,
                         unmapped: dict[str, list[str]] | None = None) -> str | None:
    """Map (CSV ticker, Exchange name, Location) to a yfinance-ready symbol.

    Order of resolution:
      1. Explicit ticker_overrides (highest priority) — used for share-class
         quirks like BRK.B / BRKB → BRK-B.
      2. Exchange-based suffix mapping; market-group venues (e.g. "Nasdaq
         Omx Nordic") disambiguate the listing venue via `location`.
      3. If exchange unknown or empty → return raw ticker as-is (assume US).

    Step 3 is the hazard this signature exists to expose. A non-US holding
    whose venue is not in the map keeps its bare local code, is treated as a
    US ticker, resolves at no vendor, and drops out of both the numerator
    and the denominator of breadth — a silent coverage loss rather than an
    error. Pass `unmapped` to collect exchange → [tickers] for every row
    that takes it, then hand the result to `report_unmapped_exchanges`.

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
        if ex_key in _EXCHANGE_ROUTE_UNAVAILABLE:
            # Known venue, known-bad vendor route. Fall through to the raw
            # local code exactly as before so a downstream per-name override
            # can still repair it, but do not report it as a discovery.
            return _us_symbol(raw_ticker)
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
        # Non-empty exchange we do not recognise. This is the silent-loss
        # path: record it so the caller can announce it.
        if unmapped is not None:
            unmapped.setdefault(ex_key, []).append(raw_ticker)
    # Fallback: assume US (no suffix). Apply share-class fix.
    return _us_symbol(raw_ticker)


def parse_holdings(body: str, ticker_overrides: dict | None = None,
                     apply_exchange_suffix: bool = False,
                     symbol: str | None = None,
                     strict_exchanges: bool = True,
                     exclude_symbols: frozenset[str] | set[str] | None = None,
                     as_of: date | None = None,
                     ) -> list[str]:
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
    as_of : date, optional
        The date the CALLER asked for, carried through only so a refusal can
        name the date it happened on. It is NOT validated against the date
        embedded in the CSV body, and this function performs no date-parity
        check of any kind — unlike parse_holdings_json, whose asOfDate echo
        check is what makes the JSON path's retained responses safe to keep.
        Cached CSVs are the pre-2026-07 archive, keyed by filename; auditing
        their embedded dates would be historical repair and is out of scope
        here.
    """
    if 'Fund Holdings as of,"-"' in body or 'Fund Holdings as of,-' in body:
        return []
    overrides = ticker_overrides or {}
    excluded = exclude_symbols or frozenset()
    tickers: list[str] = []
    seen: set[str] = set()
    unmapped: dict[str, list[str]] = {}
    n_equity = 0
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
        n_equity += 1
        exchange = (row[exchange_idx].strip() if exchange_idx is not None
                                              and len(row) > exchange_idx
                                              else None)
        location = (row[location_idx].strip() if location_idx is not None
                                              and len(row) > location_idx
                                              else None)
        if apply_exchange_suffix:
            sym = _resolve_yf_symbol(raw, exchange, overrides,
                                     location=location, unmapped=unmapped)
        else:
            # Default US path: still apply dot → dash share-class normalisation
            # so the parser output is yfinance-ready (BRK.B → BRK-B).
            sym = overrides.get(raw, raw.replace(".", "-"))
        # Belt-and-braces: catch any "-." / "-" / empty that the resolver
        # could have produced from edge-case inputs.
        if sym is None or sym in {"", "-"} or sym.startswith("-."):
            continue
        if sym in excluded or sym in seen:
            continue
        seen.add(sym)
        tickers.append(sym)
    report_unmapped_exchanges(unmapped, symbol or "?", n_equity,
                              as_of=as_of, strict=strict_exchanges)
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


def refused_payload_path(symbol: str, target: date) -> Path:
    """Where a REFUSED vendor response for one date is retained.

    A sidecar beside the positive caches rather than the positive cache
    itself, because the two are read on different terms: ``SYM_YYYYMMDD.json``
    is trusted unconditionally by every later run, and a response we could
    not fully resolve must never acquire that standing.
    """
    return RAW_DIR / f"{symbol}_{target.strftime('%Y%m%d')}.refused.json"


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write `payload` to `path` so a reader never sees a half-written file.

    Into a uniquely named temporary in the same directory, flushed and
    fsynced, then moved into place by os.replace — atomic on both POSIX and
    Windows. A crash leaves the temporary behind and the target untouched,
    rather than a truncated file that parses as neither JSON nor absence.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def retain_refused_payload(path: Path, payload: dict) -> bool:
    """Keep a refused vendor response so the date can be rebuilt later.

    WHY THIS EXISTS. ``load_snapshot_tickers`` parsed before it cached, so a
    payload that raised UnmappedExchangeError was discarded: every later run
    re-fetched the date, re-raised and threw the response away again. iShares
    serves a bounded history window, so once that window closes the date is
    unrecoverable even after the venue is mapped — which would remove the
    remedy the whole refusal class depends on. The 2026-09-18 EXV1 roster was
    recovered on 2026-09-22 only because the window was still open.

    WHY RETAINING IT IS SAFE, AND WHY ONLY HERE. This is called from the
    UnmappedExchangeError handler on the NETWORK path and nowhere else. That
    raise happens at the END of ``parse_holdings_json``, after the payload has
    already cleared the contract check, the asOfDate parity check and the
    column-length check. A payload that reaches this function is therefore
    well-formed, non-empty and FOR the date requested, so a wrong-date, empty
    or malformed response cannot be laundered into a holdings cache by this
    path. That eligibility is a property of WHERE the call sits, not of
    anything asserted here, so it is pinned by behavioural tests — see
    tests/test_roster_refusal.py.

    WRITE-ONCE, AND ATOMIC. The first capture is the one contemporaneous with
    the refusal recorded against it; a later identical write buys nothing, and
    a later DIFFERENT write would quietly replace the evidence under an
    unchanged record. Returns False without writing when a sidecar already
    exists — including when a concurrent writer created it while this one was
    still writing its temporary, which os.link detects atomically where the
    filesystem supports it.

    Raises OSError if the evidence cannot be stored. The caller must report
    that and still propagate the ORIGINAL refusal: failing to keep the
    evidence is a second problem, not a reason to forget the first.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            # Atomic claim: fails outright if another process won the race,
            # which os.replace would not — it would overwrite their evidence.
            os.link(tmp, path)
        except FileExistsError:
            return False
        except (OSError, AttributeError, NotImplementedError):
            # No hard-link support on this filesystem. Fall back to the
            # ordinary atomic move, re-checking first so the common race is
            # still lost safely rather than silently overwriting.
            if path.exists():
                return False
            os.replace(tmp, path)
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def quarantine_retained_payload(path: Path) -> str | None:
    """Move unreadable evidence aside instead of deleting it.

    NEVER OVERWRITES. The name was a one-second UTC stamp and the move was
    os.replace, which clobbers its destination: two quarantines for the same
    source date inside one second destroyed the first file — evidence lost by
    the very routine that exists to preserve it. The name now carries a random
    suffix as well as the stamp, and the move claims the name exclusively.

    Returns the new name, or None when the move failed entirely. Never raises:
    the caller is already raising a refusal, and a failure to file the
    evidence must not replace it.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for _ in range(64):
        target = path.with_name(
            f"{path.name}.corrupt.{stamp}.{uuid.uuid4().hex[:8]}")
        if target.exists():
            continue
        try:
            # os.link claims the destination atomically and fails outright if
            # it exists; os.rename/os.replace would overwrite on POSIX.
            os.link(path, target)
        except FileExistsError:
            continue
        except (OSError, AttributeError, NotImplementedError):
            try:
                if target.exists():
                    continue
                os.replace(path, target)
                return target.name
            except OSError:
                return None
        else:
            try:
                os.unlink(path)
            except OSError:
                pass
            return target.name
    return None


def unresolved_marker_path(symbol: str, target: date) -> Path:
    """Where "this date is refused and has no usable evidence" is recorded."""
    return RAW_DIR / f"{symbol}_{target.strftime('%Y%m%d')}.unresolved.json"


def write_unresolved_marker(path: Path, symbol: str, source_date: date,
                            reason: str, quarantined: str | None) -> bool:
    """Record an unresolved refusal that has no re-parseable payload left.

    WHY THIS EXISTS. Quarantining a corrupt sidecar moved it out of the way of
    the loader — and out of the way of the guard with it. The refusal was
    raised once, and the NEXT run found no sidecar, walked back to a cached
    Thursday, wrote roster_refusals=[] and exited 0. The quarantine was
    clearing the refusal without anything ever resolving the date, which is
    the 2026-09-18 failure shape rebuilt one layer down.

    The marker is the durable stand-in: no payload to re-parse, but a
    statement that this source date is unresolved. It lives beside the
    retained payloads under data/raw_ishares/, which is gitignored, so the
    scheduled run's rollback (`git checkout -- data/` plus `git clean -fd`,
    no -x) leaves it alone.

    Write-once: the first reason is the one contemporaneous with the refusal
    that was recorded against it.
    """
    if path.exists():
        return False
    try:
        _write_json_atomic(path, {
            "symbol": symbol,
            "source_date": source_date.isoformat(),
            "reason": reason,
            "quarantined": quarantined,
            "first_seen_utc": datetime.now(timezone.utc).isoformat(),
            "note": ("Unresolved roster refusal with no re-parseable vendor "
                     "response. Cleared ONLY by resolving this source date."),
        })
    except OSError:
        return False
    return True


def read_unresolved_marker(path: Path) -> dict:
    """The marker's contents, or {} when it is absent or unreadable.

    An unreadable marker still means "unresolved" — the caller checks
    existence, not contents — so this never raises.
    """
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def clear_unresolved_state(symbol: str, target: date) -> None:
    """Drop every unresolved marker for a source date that has RESOLVED.

    The only routine permitted to do this, and only ever called after a
    date-correct, non-empty parse. Quarantined files are deliberately left:
    they are the record of what went wrong, not live state.
    """
    for path in (refused_payload_path(symbol, target),
                 unresolved_marker_path(symbol, target)):
        try:
            path.unlink()
        except OSError:
            pass


def load_retained_payload(path: Path, symbol: str, target: date) -> dict:
    """Read a retained refusal back, or refuse the date explicitly.

    The previous version unlinked a corrupt sidecar and fell through to the
    endpoint. That turned damaged evidence into an ordinary vendor gap: a
    truncated file, a quiet Friday and a parseable Thursday produced status
    "walkback" with no refusal raised and the only record destroyed.

    A refusal we cannot read is still a refusal.

    QUARANTINING LEAVES A MARKER BEHIND. Moving the corrupt file aside also
    moved it out of the loader's sight, so the NEXT run found nothing, took a
    Thursday walkback and exited 0 with an empty refusal list. The quarantine
    was clearing the refusal on its own. write_unresolved_marker is what keeps
    the date refused until it is actually resolved.
    """

    def _refuse(message: str, cause: Exception | None = None):
        quarantined = quarantine_retained_payload(path)
        detail = message + (
            f" Evidence quarantined as {quarantined}." if quarantined
            else " Evidence could NOT be quarantined; inspect it by hand.")
        marker = unresolved_marker_path(symbol, target)
        if not write_unresolved_marker(marker, symbol, target, detail,
                                       quarantined) and not marker.exists():
            detail += (" WARNING: the unresolved marker could not be written "
                       "either; this date may read as clean on the next run.")
        err = RefusalEvidenceError(detail, symbol=symbol, as_of=target,
                                   quarantined=quarantined)
        raise err from cause

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _refuse(
            f"{symbol} {target.isoformat()}: a retained refusal exists for "
            f"this date but could not be read ({type(exc).__name__}: {exc}). "
            f"The date stays REFUSED — unreadable evidence is not evidence of "
            f"absence.", exc)
    if not isinstance(payload, dict):
        _refuse(
            f"{symbol} {target.isoformat()}: the retained refusal is "
            f"{type(payload).__name__}, not a vendor payload. The date stays "
            f"REFUSED.")
    return payload


def has_unresolved_refusal(symbol: str, target: date) -> bool:
    """Is this source date refused and not yet resolved?"""
    return (refused_payload_path(symbol, target).exists()
            or unresolved_marker_path(symbol, target).exists())


def unresolved_source_dates(symbol: str) -> list[date]:
    """Every source date carrying unresolved refusal state, oldest first.

    Matches only ``<symbol>_<8 digits>.<kind>.json``, so a quarantined file
    (which carries a further ``.corrupt.<stamp>.<rand>`` tail) is never
    mistaken for live state, and a symbol that is a prefix of another cannot
    pick up its neighbour's dates.
    """
    out: set[date] = set()
    for kind in ("refused", "unresolved"):
        try:
            found = RAW_DIR.glob(f"{symbol}_*.{kind}.json")
        except OSError:
            continue
        for path in found:
            stamp = path.name[len(symbol) + 1:-len(f".{kind}.json")]
            if len(stamp) != 8 or not stamp.isdigit():
                continue
            try:
                out.add(datetime.strptime(stamp, "%Y%m%d").date())
            except ValueError:
                continue
    return sorted(out)


def attempt_refusal_recovery(symbol: str, etf_cfg: dict, source_date: date,
                             *, latency=None,
                             allow_network: bool = True) -> list[str]:
    """Try to RESOLVE one unresolved source date. The only route out.

    Returns the roster when the date resolves, and clears every unresolved
    marker for it. Raises RosterRefusal when it stays unresolved.
    EndpointUnavailable / PayloadContractError propagate: a dead transport is
    the root cause and outranks the refusal, and the reconciliation pass still
    records the refusal afterwards, so nothing is cleared by it.

    TWO ROUTES, IN ORDER.

    1. OFFLINE, from the retained response. This is the ordinary remedy — map
       the venue, re-run — and it works with the vendor gone, which is the
       whole reason responses are retained.

    2. THE ENDPOINT, when offline recovery cannot resolve it. Needed because
       ``refresh=True`` is set only for the newest Friday, so an unresolved
       HISTORICAL Friday or walkback Thursday used to re-raise before ever
       contacting the vendor: an issuer correction could never be seen, and a
       date whose evidence had been quarantined had no route back at all. That
       is a permanent refusal, which is no better than a silent one.

    This is recovery, not suppression. It cannot clear anything except by a
    date-correct, non-empty parse. Vendor absence, a refusing response, a
    transport failure and any older cache all leave the date refused.
    """
    rules = roster_rules(etf_cfg)
    apply_suffix = etf_cfg.get("apply_exchange_suffix", False)
    refused_path = refused_payload_path(symbol, source_date)
    marker_path = unresolved_marker_path(symbol, source_date)
    json_path = RAW_DIR / f"{symbol}_{source_date.strftime('%Y%m%d')}.json"

    def _parse(payload: dict) -> list[str]:
        return parse_holdings_json(
            payload, source_date, ticker_overrides=rules["ticker_overrides"],
            apply_exchange_suffix=apply_suffix, symbol=symbol,
            exclude_symbols=rules["exclude_symbols"],
        )

    def _resolved(payload: dict, names: list[str], how: str) -> list[str]:
        # Positive cache FIRST, so a failure in between loses neither the
        # roster nor the evidence.
        _write_json_atomic(json_path, payload)
        clear_unresolved_state(symbol, source_date)
        print(f"  RESOLVED {symbol} {source_date.isoformat()} ({how}): "
              f"{len(names)} names. Refusal cleared.", flush=True)
        return names

    pending: RosterRefusal | None = None

    if refused_path.exists():
        retained = load_retained_payload(refused_path, symbol, source_date)
        try:
            names = _parse(retained)
        except UnmappedExchangeError as exc:
            pending = exc
        else:
            if names:
                return _resolved(retained, names,
                                 "re-parsed under the current map")
            pending = RefusalEvidenceError(
                f"{symbol} {source_date.isoformat()}: the retained refusal no "
                f"longer parses to a roster for its own date.",
                symbol=symbol, as_of=source_date)

    if pending is None:
        marker = read_unresolved_marker(marker_path)
        pending = RefusalEvidenceError(
            marker.get("reason")
            or (f"{symbol} {source_date.isoformat()}: refused, with no "
                f"re-parseable vendor response retained."),
            symbol=symbol, as_of=source_date,
            quarantined=marker.get("quarantined"))

    if not allow_network:
        raise pending

    fresh = _fetch_payload(source_date, etf_cfg, latency)
    try:
        names = _parse(fresh)
    except UnmappedExchangeError as exc:
        # Still refused. Retain this response if nothing is retained yet —
        # write-once, so an existing capture is never replaced.
        try:
            exc.evidence_retained = retain_refused_payload(refused_path, fresh)
        except OSError as io_exc:
            exc.evidence_retained = False
            exc.evidence_error = f"{type(io_exc).__name__}: {io_exc}"
        raise
    if names:
        return _resolved(fresh, names, "revalidated against the endpoint")
    print(f"  {symbol} {source_date.isoformat()}: the endpoint served no "
          f"holdings for this date; the refusal is UNRESOLVED and stands.",
          flush=True)
    raise pending


def unresolved_refusal_records(symbol: str, etf_cfg: dict,
                               already_recorded: set[str],
                               *, allow_network: bool = True) -> list[dict]:
    """Every retained refusal for `symbol` that the walk did not resolve.

    THE WALK PATH IS NOT A GUARANTEE. A refusal is recorded when a walk step
    hits its source date, and the walk does not always hit it: the sidecar may
    be for a Thursday the walkback reached only once, and a later run whose
    Friday resolves never looks at that Thursday again. The refusal would then
    sit unresolved on disk with nothing reporting it — the same silence the
    whole class exists to end, one layer further down.

    So the payload's refusal list is reconciled against the evidence on disk
    before it is written. Anything still refusing under the current mapping is
    recorded whether or not this walk happened to visit it.

    `already_recorded` holds the source_date strings the walk recorded, so a
    date is never counted twice.

    It RECOVERS as well as reports, through the one shared routine. A date the
    walk never visits would otherwise have no route to resolution at all:
    reporting it forever is a permanent refusal, which is no better than a
    silent one. `allow_network` is passed False when the transport is already
    known dead, so this cannot hammer an endpoint the walk has given up on.
    """
    out: list[dict] = []
    for source in unresolved_source_dates(symbol):
        if source.isoformat() in already_recorded:
            continue
        try:
            attempt_refusal_recovery(symbol, etf_cfg, source,
                                     allow_network=allow_network)
        except RosterRefusal as exc:
            out.append(exc.as_record(source))
        except (EndpointUnavailable, PayloadContractError) as exc:
            # The transport is the root cause and outranks the refusal, but
            # the date is still unresolved and must still be recorded.
            rec = RefusalEvidenceError(
                f"{symbol} {source.isoformat()}: unresolved, and the endpoint "
                f"could not be reached to resolve it ({type(exc).__name__}: "
                f"{exc}).", symbol=symbol, as_of=source).as_record(source)
            out.append(rec)
    return out


def _fetch_payload(target: date, etf_cfg: dict, latency) -> dict:
    """One holdings payload from the endpoint, timed when a circuit is given.

    Timed in a finally so a date that dies on the full retry ladder still
    counts. A run where every date fails is EndpointCircuit's job, but a run
    that mixes failures and slow successes is nobody else's, and dropping the
    failures would flatter the mean.
    """
    if latency is None:
        return fetch_product_data(target, etf_cfg)
    t0 = time.monotonic()
    try:
        return fetch_product_data(target, etf_cfg)
    finally:
        latency.record_served(time.monotonic() - t0, item=target)


def load_snapshot_tickers(target: date, etf_cfg: dict,
                          latency: LatencyCircuit | None = None,
                          refresh: bool = False) -> list[str]:
    """Return the Equity roster for one calendar date, cache-first.

    ``refresh=True`` revalidates this date against the endpoint. The main
    weekly run uses it for the newest Friday; historical captures stay cached.

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
    rules = roster_rules(etf_cfg)
    overrides = rules["ticker_overrides"]
    excluded = rules["exclude_symbols"]
    apply_suffix = etf_cfg.get("apply_exchange_suffix", False)
    symbol = etf_cfg["symbol"]
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = target.strftime("%Y%m%d")

    json_path = RAW_DIR / f"{symbol}_{stamp}.json"
    refused_path = refused_payload_path(symbol, target)

    def _parse(payload: dict) -> list[str]:
        return parse_holdings_json(
            payload, target, ticker_overrides=overrides,
            apply_exchange_suffix=apply_suffix, symbol=symbol,
            exclude_symbols=excluded,
        )

    # ---- AN UNRESOLVED REFUSAL OUTRANKS EVERY CACHE AND THE ENDPOINT ----
    #
    # Checked FIRST, before the CSV cache, the JSON cache and the network.
    # It used to sit after both caches and be skipped entirely under
    # refresh=True, and both of those let a refusal vanish without ever being
    # resolved:
    #
    #   - refresh=True bypassed it, so a retry that found no data for the
    #     newest Friday walked back to a cached Thursday, wrote an ordinary
    #     snapshot, recorded no refusal and exited 0 while the sidecar sat
    #     unresolved on disk;
    #   - a positive cache for the SAME date won ahead of it, which
    #     revalidation creates naturally — a revised response that refuses
    #     leaves the earlier good cache in place, and every later run then
    #     served the superseded roster.
    #
    # The rule now has no exceptions: the ONLY thing that clears a refusal is
    # successfully resolving that source date. Not a vendor gap, not an older
    # positive cache, not an EDGAR fallback, not a later good Friday.
    # UNRESOLVED STATE IS EITHER a retained payload or a marker left behind
    # when that payload had to be quarantined. Both mean the same thing here,
    # and both are handled by the ONE recovery routine, which is also what the
    # end-of-walk reconciliation uses — so the loader and the reconciler
    # cannot drift apart on what "resolved" means.
    #
    # allow_network is unconditional, NOT gated on `refresh`. refresh=True is
    # set only for the newest Friday, so gating on it left every unresolved
    # historical Friday and walkback Thursday re-raising before the endpoint
    # was ever asked: an issuer correction could not be seen, and a date whose
    # evidence had been quarantined had no route back at all. A dead endpoint
    # is already short-circuited by the caller's EndpointCircuit, so this
    # cannot hammer a transport the walk has given up on.
    if has_unresolved_refusal(symbol, target):
        return attempt_refusal_recovery(symbol, etf_cfg, target,
                                        latency=latency, allow_network=True)

    csv_path = RAW_DIR / f"{symbol}_{stamp}.csv"
    if csv_path.exists() and not refresh:
        cached = csv_path.read_text(encoding="utf-8")
        if looks_like_ishares_holdings_csv(cached):
            if latency is not None:
                latency.record_cache_hit()
            return parse_holdings(cached, ticker_overrides=overrides,
                                   apply_exchange_suffix=apply_suffix,
                                   symbol=symbol, exclude_symbols=excluded,
                                   as_of=target)
        # Poisoned by an earlier run's anti-bot HTML — drop and fall through.
        csv_path.unlink()

    if json_path.exists() and not refresh:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            json_path.unlink()
        else:
            if latency is not None:
                latency.record_cache_hit()
            if payload.get("_no_holdings"):
                if (date.today() - target).days > NEGATIVE_CACHE_MIN_AGE_DAYS:
                    return []
            else:
                cached_tickers = _parse(payload)
                if cached_tickers:
                    return cached_tickers
            # An empty or wrong-date response is not a permanent positive cache.

    payload = _fetch_payload(target, etf_cfg, latency)
    try:
        tickers = _parse(payload)
    except UnmappedExchangeError as refusal:
        # Keep the vendor's response before the refusal unwinds past it. See
        # retain_refused_payload for why a payload that reaches here is
        # already proven well-formed and date-correct.
        #
        # A storage failure is reported and recorded ON the refusal, never
        # substituted for it: losing the evidence is a second problem, and
        # raising OSError here would drop the first one entirely and land in
        # the walk's unclassified-error handler as though nothing had been
        # refused.
        try:
            stored = retain_refused_payload(refused_path, payload)
            refusal.evidence_retained = stored
            if stored:
                print(f"  Retained the refused {symbol} {target.isoformat()} "
                      f"response at {refused_path.name} for rebuild once the "
                      f"venue is mapped.", flush=True)
        except OSError as exc:
            refusal.evidence_retained = False
            refusal.evidence_error = f"{type(exc).__name__}: {exc}"
            print(f"  EVIDENCE NOT RETAINED for {symbol} "
                  f"{target.isoformat()}: {exc}. The refusal stands; the date "
                  f"will need the endpoint to serve it again.",
                  file=sys.stderr, flush=True)
        raise
    if tickers:
        _write_json_atomic(json_path, payload)
    elif (date.today() - target).days > NEGATIVE_CACHE_MIN_AGE_DAYS:
        # Settled no-data date — record a marker so we never pay for it
        # again. Storing the full payload would be pure waste: it is the
        # latest-date fallback response, and all we need to remember is
        # that this date has nothing.
        _write_json_atomic(json_path, {
            "_no_holdings": True,
            "requested_as_of": target.strftime("%Y%m%d"),
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "note": ("Endpoint returned no holdings for this date "
                     "(pre-inception, holiday, or data gap)."),
        })
    return tickers


def get_snapshot(
    target_friday: date, etf_cfg: dict,
    circuit: EndpointCircuit | None = None,
    latency: LatencyCircuit | None = None,
    refresh: bool = False,
) -> tuple[list[str] | None, date | None, str]:
    """Walk back from `target_friday` looking for a populated holdings file.

    Returns (tickers, actual_date, status). `status` is one of:
      - "exact"     : Friday returned data
      - "walkback"  : an earlier weekday in the same week returned data
      - "not_found" : endpoint healthy, no data within MAX_WALKBACK_DAYS days
      - "endpoint_unavailable" : the transport is dead (see EndpointCircuit)

    UnmappedExchangeError is deliberately NOT caught here and aborts the
    walkback for this Friday rather than stepping back a day. A venue the map
    does not hold on Friday is not held on Thursday either, and a walkback
    that happened to find a parseable older date would return an OLDER roster
    under a "walkback" label — a carry-forward wearing a capture's clothes,
    which is the thing this module exists to prevent. The caller classifies
    it as a refusal.

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
            kwargs = {"refresh": True} if refresh and days_back == 0 else {}
            tickers = load_snapshot_tickers(try_date, etf_cfg, latency=latency, **kwargs)
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
    # 2026-09-22 — kept INDEPENDENT of carry_forwards, because a refusal can
    # be followed by an outcome that writes no carry-forward at all (no prior
    # snapshot, an EDGAR fallback, or a later successful Friday).
    refusals: list[dict] = []
    walk_errors: list[dict] = []
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

    def _print_refusals_so_far(why: str) -> None:
        """Dump refusals to the log on an abort that writes no payload.

        EndpointDegraded unwinds past every write by design (see cli()), so on
        that path the roster_refusals array is never written and the ONLY
        surviving record of a refusal already encountered is what was printed.
        The scheduled run's log is retained across the rollback, so printing
        here is what keeps the evidence.
        """
        if not refusals:
            return
        print(file=sys.stderr)
        print(f"ROSTER REFUSALS already encountered before {why} — no roster "
              f"payload will be written, so this log is the only record:",
              file=sys.stderr)
        for rec in refusals:
            print(f"  {json.dumps(rec, sort_keys=True)}", file=sys.stderr)

    for i, friday in enumerate(fridays, start=1):
        if i == 1 or i % 25 == 0 or i == len(fridays):
            print(f"  [{i}/{len(fridays)}] {friday.isoformat()}", flush=True)
        # Checked at the TOP of the date, not the bottom: tripping mid-walk
        # and then serving one more date would write a roster whose last entry
        # was fetched after the run had already decided it could not trust the
        # endpoint's timing. Abort before, not after.
        if latency.dead:
            _print_refusals_so_far("the endpoint was declared degraded")
            raise EndpointDegraded(latency.reason or "endpoint degraded")
        try:
            tickers, actual, status = get_snapshot(friday, etf_cfg, circuit,
                                                   latency=latency,
                                                   refresh=friday == end_friday)
        except RosterRefusal as e:
            # A REFUSAL, not an absence (2026-09-22). This used to fall into
            # the blanket handler below and be written as "no_data_in_walkback"
            # — the same label a public holiday gets — so nothing downstream
            # could tell a roster we declined from one the issuer never
            # published.
            #
            # Recorded here and NOT inside the carry-forward block, because
            # three of the outcomes that follow a refusal write no
            # carry-forward at all: a refusal with no prior snapshot is
            # skipped, an EDGAR fallback writes a real snapshot over it, and a
            # later successful Friday leaves the newest snapshot healthy. In
            # each of those the carry-forward array is silent and the refusal
            # would vanish with it.
            refusals.append(e.as_record(friday))
            print(f"  ROSTER REFUSED on {friday}: {e}", flush=True)
            tickers, actual, status = None, None, "roster_refused"
        except EndpointDegraded:
            # Never classified here. cli() documents that this unwinds past
            # everything between the walk and the entry point, which is what
            # guarantees no roster is written on a stalled endpoint. Today it
            # can only be raised at the top of the loop, outside this try;
            # re-raising keeps that contract if it ever moves deeper. The
            # refusals collected so far go to the log first, since no payload
            # will be written to carry them.
            _print_refusals_so_far("the endpoint was declared degraded")
            raise
        except Exception as e:  # noqa: BLE001 — classified, then re-raised as a failure
            # UNCLASSIFIED, and it must not wear a healthy label. The previous
            # handler turned every unexpected exception into "not_found",
            # which the record then called a vendor gap and the run exited 0
            # on. An exception we cannot name is not evidence that the issuer
            # published nothing.
            walk_errors.append({
                "target_friday": friday.isoformat(),
                "error_type": type(e).__name__,
                "detail": str(e),
            })
            print(f"  UNEXPECTED ERROR on {friday}: "
                  f"{type(e).__name__}: {e}", flush=True)
            tickers, actual, status = None, None, "unexpected_error"

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
            # FOUR CLASSES, NOT TWO (2026-09-22). "no_data_in_walkback" is
            # reserved for the one case it actually describes: the endpoint was
            # healthy and had nothing for this date. A refusal and an
            # unclassified error each keep their own label so that a reader of
            # the payload — and the guard in check_refresh_guard — can tell
            # which of them produced a given carry-forward.
            cause = {
                "endpoint_unavailable": "endpoint_unavailable",
                "roster_refused": "roster_refused",
                "unexpected_error": "unexpected_error",
            }.get(status, "no_data_in_walkback")
            gap = {
                "endpoint_unavailable": (
                    "upstream endpoint unavailable — see endpoint_health"),
                "roster_refused": (
                    "roster refused: unrecognised exchange above the bound — "
                    "see roster_refusals"),
                "unexpected_error": (
                    "unclassified failure during the walk — see walk_errors"),
            }.get(
                status,
                f"no holdings data within {MAX_WALKBACK_DAYS} days back from "
                "target Friday",
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

    # RECONCILE THE REFUSAL LIST AGAINST THE EVIDENCE ON DISK. The walk only
    # records a refusal for a date it actually visited, and a retained refusal
    # can outlive every route to its own source date — see
    # unresolved_refusal_records. Anything still refusing is added here, so
    # "no refusal recorded" means "none is unresolved", not "the walk did not
    # happen to look".
    reconciled = unresolved_refusal_records(
        symbol, etf_cfg,
        {r["source_date"] for r in refusals if r.get("source_date")},
        # No point asking a transport the walk has already given up on; the
        # dates are still recorded as unresolved either way.
        allow_network=not circuit.dead,
    )
    if reconciled:
        print(f"  {len(reconciled)} retained refusal(s) for {symbol} are "
              f"still unresolved on disk and were not reached by this walk; "
              f"recorded.", flush=True)
        refusals.extend(reconciled)

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
        "ticker_overrides_applied": roster_rules(etf_cfg)["ticker_overrides"],
        # Resolved symbols dropped from every roster (rights / temporary
        # lines), and whether STAGED registry changes were merged in — a
        # roster built with staged rules must never be mistaken for a
        # deployed one (etf_registry.roster_rules, 2026-09-02).
        "exclude_symbols_applied": sorted(roster_rules(etf_cfg)["exclude_symbols"]),
        "staged_roster_changes_applied": roster_rules(etf_cfg)["staged_applied"],
        "walkbacks": walkbacks,
        "carry_forwards": carry_forwards,
        # Every Friday whose roster was REFUSED, with the venue strings and
        # the symbols that carried them. Written whatever the walk did next,
        # so an EDGAR fallback or a later good Friday cannot erase it. An
        # empty list is the only healthy reading; check_refresh_guard's G8
        # reads exactly this. Note share_of_equity_rows is a ROW share and
        # never a portfolio weight.
        "roster_refusals": refusals,
        # Failures the walk could not classify. Never folded into
        # carry_forwards' "no_data_in_walkback", which means something
        # specific and healthy.
        "walk_errors": walk_errors,
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
    # Roster-refusal alert (2026-09-22). Printed in full whether or not the
    # refusal wins the exit code — see EXIT_PRECEDENCE. The venue strings and
    # the affected symbols are the whole remedy, so they go to the operator
    # rather than only into the payload.
    if refusals:
        bar = "!" * 72
        print(file=sys.stderr)
        print(bar, file=sys.stderr)
        print(
            f"ROSTER REFUSED: {symbol} — {len(refusals)} target Friday(s) "
            f"carry an unrecognised exchange above the "
            f"{UNMAPPED_EXCHANGE_MAX_SHARE:.0%} bound.",
            file=sys.stderr,
        )
        for rec in refusals:
            print(
                f"  {rec['target_friday']} (source {rec['source_date']}): "
                f"{rec['n_affected']} of {rec['n_equity_rows']} equity rows"
                + (f" ({rec['share_of_equity_rows']:.1%} of rows)"
                   if rec["share_of_equity_rows"] is not None else ""),
                file=sys.stderr,
            )
            print(f"      venues:  {', '.join(rec['exchanges'])}",
                  file=sys.stderr)
            shown = ", ".join(rec["affected_symbols"][:12])
            more = (f" (+{len(rec['affected_symbols']) - 12} more)"
                    if len(rec["affected_symbols"]) > 12 else "")
            print(f"      symbols: {shown}{more}", file=sys.stderr)
            if rec.get("evidence_retained") is False:
                print(f"      EVIDENCE NOT RETAINED: "
                      f"{rec.get('evidence_error')}", file=sys.stderr)
            if rec.get("evidence_unreadable"):
                print(f"      EVIDENCE UNREADABLE, quarantined as "
                      f"{rec.get('evidence_quarantined') or '<rename failed>'}",
                      file=sys.stderr)
        # The summary above truncates the symbol list so it stays readable.
        # The COMPLETE records follow, one JSON object each: this log is what
        # survives the scheduled run's rollback, and a truncated symbol list
        # is not a record of what was refused.
        print("  complete refusal records:", file=sys.stderr)
        for rec in refusals:
            print(f"    {json.dumps(rec, sort_keys=True)}", file=sys.stderr)
        print(
            "  The row share above is NOT a portfolio weight; the affected "
            "names may be a larger or smaller share of NAV.",
            file=sys.stderr,
        )
        print(
            "  Operator action: map the venue in _EXCHANGE_TO_YF_SUFFIX once "
            "its yfinance suffix is verified, then re-run. The refused "
            "responses are retained under data/raw_ishares/*.refused.json, so "
            "the dates rebuild from disk.",
            file=sys.stderr,
        )
        print(bar, file=sys.stderr)

    if walk_errors:
        bar = "!" * 72
        print(file=sys.stderr)
        print(bar, file=sys.stderr)
        print(
            f"UNCLASSIFIED WALK FAILURE: {symbol} — {len(walk_errors)} "
            f"target Friday(s) raised an error the walk could not classify.",
            file=sys.stderr,
        )
        for rec in walk_errors:
            print(f"  {rec['target_friday']}: {rec['error_type']}: "
                  f"{rec['detail']}", file=sys.stderr)
        print(
            "  Not recorded as a vendor gap. Diagnose before trusting any "
            "roster this run produced.",
            file=sys.stderr,
        )
        print(bar, file=sys.stderr)

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
    elif staleness_status == "warning":
        print(
            f"  WARNING: {symbol} roster is {days_since_real} days stale "
            f"(last real fetch {last_real_fetch_date}). "
            f"Threshold for critical alert is {critical_days} days"
            f"{threshold_label}."
        )
    elif staleness_status == "fresh" and last_real_fetch_date is not None:
        # Deliberately NOT printed when a refusal fired. "Staleness OK" beside
        # a refused roster is the exact sentence that made 2026-09-18 read as
        # a healthy run: the carried roster was indeed only days old, and
        # saying so was true and entirely beside the point.
        if not refusals:
            print(
                f"  Staleness OK: last real fetch {last_real_fetch_date} "
                f"({days_since_real} days ago, "
                f"under {warn_days}-day warning threshold{threshold_label})."
            )
    return walk_exit_code(
        endpoint_dead=circuit.dead,
        n_refusals=len(refusals),
        n_unexpected=len(walk_errors),
        staleness_status=staleness_status,
    )


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
