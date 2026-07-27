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
  "carry_forwards":[ { "target_friday": ..., "carried_from_target": ..., "reason": ... }, ... ],
  "snapshots": {
      "YYYY-MM-DD": { "actual_date": "...", "n_tickers": N, "tickers": [...] },
      ...
  }
}

Fetch modes (2026-07-27):
  - Incremental (default): Fridays that already have a REAL iShares-derived
    snapshot in the existing data/constituents_{etf}.json are reused without
    touching the network; Fridays known to be permanently missing (recorded
    in data/fetch_negative_cache.json) are re-attempted at most every
    NEGCACHE_RETRY_DAYS days rather than every run. Carried-forward and
    EDGAR-sourced snapshots are never reused — those Fridays re-resolve
    live each run, so carry-forward chains and the EDGAR freshness
    comparison behave exactly as in a full run.
  - Full (--full): the pre-2026-07-27 behaviour — walk every Friday from
    start_friday, using the raw CSV cache where present. Required after a
    registry parse-rule change (or run regenerate_constituents_from_cache.py
    for parser-only changes); incremental mode detects changed URL /
    ticker_overrides and falls back to full automatically.
  Parity between the two modes is guarded by
  scripts/verify_incremental_parity.py and tests/test_incremental_fetch.py.

Run:
    python scripts/fetch_constituents.py             # default: SOXX, incremental
    python scripts/fetch_constituents.py --etf CSP1  # S&P 500 via iShares UK
    python scripts/fetch_constituents.py --etf CSP1 --full   # full re-fetch
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

# ---------------------------------------------------------------------------
# Incremental mode + negative cache (2026-07-27)
# ---------------------------------------------------------------------------
# Anti-bot HTML responses are (correctly) never written to the raw cache, so
# every permanently-missing Friday — e.g. the 82 ICHN Fridays before/around
# the fund's 2019 launch — used to pay the full retry backoff ladder on every
# weekly run. That recurring cost, not cold caches, is where the measured
# ~4-hour Step 1 went. The negative cache records those Fridays and re-probes
# them at most every NEGCACHE_RETRY_DAYS days with a single no-backoff
# request. Fridays younger than NEGCACHE_RECENT_EXEMPT_DAYS are always
# attempted with the full ladder: a recently-missing Friday is usually
# iShares publication lag, not a permanent hole.
NEGCACHE_PATH = DATA_DIR / "fetch_negative_cache.json"
NEGCACHE_RETRY_DAYS = 30          # re-attempt known-missing Fridays at most this often
NEGCACHE_RECENT_EXEMPT_DAYS = 30  # Fridays younger than this always get a real attempt
NEGCACHE_MAX_RETRIES_PER_RUN = 16  # cap due probes per ETF-run to bound the worst case

# Note: Python's datetime constructor is 1-indexed for months (Jan=1), unlike
# JavaScript's Date which is 0-indexed (Jan=0). We always use Python here.


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


def fetch_with_retry(target: date, etf_cfg: dict, probe: bool = False) -> str:
    """Fetch the raw iShares CSV for `target` and return the body.

    Caches successful 200 responses to disk so reruns do not re-hit iShares.
    Empty-template responses (Fund Holdings as of "-") are also cached because
    they are stable over time for old dates (US holidays, data gaps)
    — re-fetching them would waste requests.

    Cached bodies are re-validated against `looks_like_ishares_holdings_csv`
    on read. If a poisoned HTML body got cached by an earlier run, it is
    discarded and the network fetch retried.

    probe=True makes a single attempt with no backoff ladder — used for the
    negative cache's monthly re-checks of known-missing Fridays, where a
    transient failure simply waits for the next monthly probe instead of
    burning ~45s of backoff on a date that has been dead for years.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = RAW_DIR / f"{etf_cfg['symbol']}_{target.strftime('%Y%m%d')}.csv"
    if cache_path.exists():
        cached = cache_path.read_text(encoding="utf-8")
        if looks_like_ishares_holdings_csv(cached):
            return cached
        # Cached body is poisoned (HTML from anti-bot) — discard and re-fetch
        cache_path.unlink()

    url = f"{etf_cfg['csv_url_template']}&asOfDate={target.strftime('%Y%m%d')}"
    last_err: Exception | None = None
    for backoff in ([0] if probe else [0, *RETRY_BACKOFFS]):
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
    "Swiss Exchange":                 ".SW",
    "Nyse Euronext - Euronext Brussels": ".BR",
    "Euronext Brussels":              ".BR",
    "Stockholm Stock Exchange":       ".ST",
    "Nasdaq Stockholm":               ".ST",
    "Nasdaq Helsinki":                ".HE",
    "Helsinki Stock Exchange":        ".HE",
    "Copenhagen Stock Exchange":      ".CO",
    "Nasdaq Copenhagen":              ".CO",
    "Oslo Stock Exchange":            ".OL",
    "Oslo Bors":                      ".OL",
    "Nyse Euronext - Euronext Lisbon": ".LS",
    "Vienna Stock Exchange":          ".VI",
    "Warsaw Stock Exchange":          ".WA",
    "Athens Stock Exchange":          ".AT",
    "Irish Stock Exchange":           ".IR",
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


def _resolve_yf_symbol(raw_ticker: str, exchange: str | None,
                         overrides: dict | None = None) -> str | None:
    """Map (CSV ticker, Exchange name) to a yfinance-ready symbol.

    Order of resolution:
      1. Explicit ticker_overrides (highest priority) — used for share-class
         quirks like BRK.B / BRKB → BRK-B.
      2. Exchange-based suffix mapping.
      3. If exchange unknown or empty → return raw ticker as-is (assume US).

    Returns None when the ticker is empty / unparseable.
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
        return root

    if exchange:
        ex_key = exchange.strip()
        if ex_key in _EXCHANGE_TO_YF_SUFFIX:
            suffix = _EXCHANGE_TO_YF_SUFFIX[ex_key]
            # If the suffix is empty (US listing), apply dot→dash share-class fix.
            if suffix == "":
                return raw_ticker.rstrip(".").replace(".", "-")
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
    return raw_ticker.replace(".", "-")


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
    for ln in body.splitlines():
        if header is None:
            if "Ticker" in ln[:20] and "Asset Class" in ln:
                header = next(csv.reader(io.StringIO(ln)))
                asset_class_idx = header.index("Asset Class")
                # Exchange column may or may not be present (US iShares CSVs
                # sometimes omit it). If missing, exchange_idx stays None and
                # we fall back to the raw ticker.
                try:
                    exchange_idx = header.index("Exchange")
                except ValueError:
                    exchange_idx = None
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
        if apply_exchange_suffix:
            sym = _resolve_yf_symbol(raw, exchange, overrides)
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


def _display_path(path: Path) -> str:
    """Return ``path`` relative to PROJECT_ROOT for display, falling back
    to the absolute path when ``path`` is outside the project tree (e.g. a
    tmp_path under test). Mirrors compute_breadth._display_path."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


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


def get_snapshot(
    target_friday: date, etf_cfg: dict, probe: bool = False
) -> tuple[list[str] | None, date | None, str]:
    """Walk back from `target_friday` looking for a populated holdings file.

    Returns (tickers, actual_date, status). `status` is one of:
      - "exact"     : Friday returned data
      - "walkback"  : an earlier weekday in the same week returned data
      - "not_found" : no data within MAX_WALKBACK_DAYS days

    probe=True passes single-attempt mode down to fetch_with_retry (negative
    cache monthly re-checks). The walkback logic itself is unchanged.
    """
    overrides = etf_cfg.get("ticker_overrides", {})
    apply_suffix = etf_cfg.get("apply_exchange_suffix", False)
    for days_back in range(MAX_WALKBACK_DAYS + 1):
        try_date = target_friday - timedelta(days=days_back)
        body = fetch_with_retry(try_date, etf_cfg, probe=probe)
        tickers = parse_holdings(body, ticker_overrides=overrides,
                                  apply_exchange_suffix=apply_suffix)
        if tickers:
            status = "exact" if days_back == 0 else "walkback"
            return tickers, try_date, status
    return None, None, "not_found"


# =============================================================================
# Incremental mode: negative cache + prior-output reuse
# =============================================================================


class NegativeCache:
    """Persistent record of Fridays that returned no holdings data.

    File layout (data/fetch_negative_cache.json, committed so the automation
    clone and interactive clones share one memory of the known holes):

        {
          "_meta":  { ...constants documented on every save... },
          "etfs": {
            "ICHN": {
              "2018-01-05": {
                "first_seen": "2026-07-26",
                "last_attempt": "2026-07-26",
                "attempts": 1,
                "seeded_from_store": true
              }, ...
            }, ...
          }
        }

    Decision rules (see `decide`):
      - Fridays within NEGCACHE_RECENT_EXEMPT_DAYS of today are NEVER
        skipped — always attempted with the full retry ladder.
      - A recorded Friday whose last attempt is younger than
        NEGCACHE_RETRY_DAYS is skipped.
      - A recorded Friday due for re-check gets a single-attempt probe,
        capped at NEGCACHE_MAX_RETRIES_PER_RUN grants per ETF-run; entries
        beyond the cap wait for a later run ("retry at most monthly"
        tolerates longer gaps, never shorter ones).

    Failures are recorded in BOTH incremental and full mode; entries are
    only ever USED to skip in incremental mode. A successful fetch clears
    the entry. Skipping the primary attempt does not touch the EDGAR
    fallback or carry-forward logic — a skipped Friday flows into exactly
    the same "no primary data" path a failed live attempt would.
    """

    def __init__(self, path: Path):
        self.path = path
        self._etfs: dict[str, dict[str, dict]] = {}
        self._dirty_etfs: set[str] = set()
        self._retries_granted = 0
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                loaded = doc.get("etfs", {})
                if isinstance(loaded, dict):
                    self._etfs = loaded
            except (OSError, ValueError) as e:
                print(f"  WARNING: negative cache unreadable ({e}) — "
                      f"starting empty", flush=True)

    def entry(self, etf: str, friday: date) -> dict | None:
        return self._etfs.get(etf, {}).get(friday.isoformat())

    def decide(self, etf: str, friday: date, today: date) -> str:
        """Return 'attempt' (full retry ladder), 'probe' (granted monthly
        single-attempt re-check), or 'skip'."""
        if (today - friday).days <= NEGCACHE_RECENT_EXEMPT_DAYS:
            return "attempt"
        e = self.entry(etf, friday)
        if e is None:
            return "attempt"
        try:
            last_attempt = date.fromisoformat(e["last_attempt"])
        except (KeyError, TypeError, ValueError):
            return "attempt"  # malformed entry — attempt, then rewrite it
        if (today - last_attempt).days < NEGCACHE_RETRY_DAYS:
            return "skip"
        if self._retries_granted >= NEGCACHE_MAX_RETRIES_PER_RUN:
            return "skip"
        self._retries_granted += 1
        return "probe"

    def record_failure(self, etf: str, friday: date, today: date) -> None:
        etf_map = self._etfs.setdefault(etf, {})
        key = friday.isoformat()
        e = etf_map.setdefault(key, {"first_seen": today.isoformat(),
                                     "attempts": 0})
        e["last_attempt"] = today.isoformat()
        e["attempts"] = int(e.get("attempts", 0)) + 1
        e.pop("seeded_from_store", None)  # now backed by a live attempt
        self._dirty_etfs.add(etf)

    def record_success(self, etf: str, friday: date) -> None:
        etf_map = self._etfs.get(etf, {})
        if etf_map.pop(friday.isoformat(), None) is not None:
            self._dirty_etfs.add(etf)

    def seed_from_store(self, etf: str, hole_fridays: list[date],
                        store_fetch_date: date) -> int:
        """Register store-known holes without a live attempt. Only fills
        absent entries. last_attempt is the prior run's fetch date — the
        run that genuinely attempted and failed those Fridays — so the
        first incremental run does not re-pay the whole retry backlog."""
        n_seeded = 0
        etf_map = self._etfs.setdefault(etf, {})
        for f in hole_fridays:
            key = f.isoformat()
            if key in etf_map:
                continue
            etf_map[key] = {
                "first_seen": store_fetch_date.isoformat(),
                "last_attempt": store_fetch_date.isoformat(),
                "attempts": 1,
                "seeded_from_store": True,
            }
            n_seeded += 1
        if n_seeded:
            self._dirty_etfs.add(etf)
        return n_seeded

    def save(self) -> None:
        """Merge-write: re-read the file and replace only the ETF sections
        this run touched, so sequential per-ETF runs (and an accidental
        concurrent run on a different ETF) do not clobber each other."""
        if not self._dirty_etfs:
            return
        current: dict = {}
        if self.path.exists():
            try:
                current = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
        etfs = current.get("etfs", {})
        if not isinstance(etfs, dict):
            etfs = {}
        for etf in self._dirty_etfs:
            section = self._etfs.get(etf, {})
            if section:
                etfs[etf] = {k: section[k] for k in sorted(section)}
            else:
                etfs.pop(etf, None)
        payload = {
            "_meta": {
                "description": (
                    "Fridays that persistently return no holdings data "
                    "(anti-bot HTML / permanent gaps). Incremental "
                    "fetch_constituents runs skip these and re-probe each "
                    "at most every retry_after_days days. Entries clear "
                    "automatically on a successful fetch. Delete an entry "
                    "(or the file) to force an immediate re-attempt."
                ),
                "retry_after_days": NEGCACHE_RETRY_DAYS,
                "recent_exempt_days": NEGCACHE_RECENT_EXEMPT_DAYS,
                "max_retries_per_run": NEGCACHE_MAX_RETRIES_PER_RUN,
            },
            "etfs": {k: etfs[k] for k in sorted(etfs)},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2) + "\n",
                             encoding="utf-8")


def load_reusable_snapshots(
    etf_cfg: dict, out_path: Path
) -> tuple[dict[str, dict], dict | None, str | None]:
    """Load the prior run's parsed output for incremental reuse.

    Returns (reusable, prior_payload, fallback_reason). `reusable` maps
    Friday ISO date -> snapshot dict for REAL iShares-derived snapshots
    only: carried-forward and EDGAR-sourced snapshots are excluded so those
    Fridays re-resolve live each run (carry-forward chains rebuild from
    what is fetchable today; the EDGAR roster is re-evaluated every run
    exactly as in a full run — SOXX fallback semantics unchanged).

    Falls back to a full re-fetch (empty dict + reason) when the store is
    missing, unreadable, or was built under a different registry definition
    (symbol, URL, or ticker_overrides changed) — parse-rule changes must
    not be silently frozen into reused snapshots.
    """
    if not out_path.exists():
        return {}, None, "no prior output — full fetch"
    try:
        prior = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {}, None, f"prior output unreadable ({e}) — full fetch"
    if prior.get("etf") != etf_cfg["symbol"]:
        return {}, None, (
            f"prior output is for {prior.get('etf')!r}, not "
            f"{etf_cfg['symbol']!r} — full fetch"
        )
    if prior.get("source") != etf_cfg["csv_url_template"]:
        return {}, None, "registry csv_url_template changed — full re-fetch"
    if prior.get("ticker_overrides_applied", {}) != etf_cfg.get("ticker_overrides", {}):
        return {}, None, "registry ticker_overrides changed — full re-fetch"
    snapshots = prior.get("snapshots", {})
    if not isinstance(snapshots, dict):
        return {}, None, "prior output has no snapshots dict — full fetch"
    reusable = {
        friday: snap for friday, snap in snapshots.items()
        if isinstance(snap, dict)
        and "carried_forward_from" not in snap
        and snap.get("source") != "edgar_nport"
        and snap.get("actual_date") and snap.get("tickers")
    }
    return reusable, prior, None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--etf", default=DEFAULT_ETF,
        help=f"ETF symbol to fetch (must be in etf_registry). Default: {DEFAULT_ETF}",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--incremental", dest="incremental", action="store_true", default=True,
        help="Reuse real iShares-derived snapshots from the prior parsed "
             "output and consult the negative cache for known-missing "
             "Fridays (default).",
    )
    mode.add_argument(
        "--full", dest="incremental", action="store_false",
        help="Re-fetch the full start_friday->present history (raw CSV cache "
             "still used). Required after registry parse-rule changes; see "
             "also scripts/regenerate_constituents_from_cache.py.",
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
        f"({start_friday} -> {end_friday}) "
        f"[{'incremental' if args.incremental else 'full'} mode]",
        flush=True,
    )

    # ----- Incremental setup: prior-output reuse + negative cache -----
    # The negative cache is loaded (and failures recorded) in both modes;
    # skip decisions apply only in incremental mode.
    negcache = NegativeCache(NEGCACHE_PATH)
    reusable: dict[str, dict] = {}
    if args.incremental:
        reusable, prior_payload, fallback_reason = load_reusable_snapshots(
            etf_cfg, out_path
        )
        if fallback_reason:
            print(f"  Incremental: {fallback_reason}", flush=True)
        elif prior_payload is not None:
            # Seed the negative cache: every Friday inside the stored range
            # without a reusable real snapshot was attempted — and failed —
            # by the run that produced the store.
            store_fetch_date: date | None = None
            try:
                store_fetch_date = date.fromisoformat(
                    str(prior_payload.get("fetched_at_utc", ""))[:10]
                )
            except ValueError:
                pass
            stored_end = str(prior_payload.get("end_friday", ""))
            if store_fetch_date and stored_end:
                holes = [
                    f for f in fridays
                    if f.isoformat() <= stored_end
                    and f.isoformat() not in reusable
                ]
                n_seeded = negcache.seed_from_store(
                    symbol, holes, store_fetch_date
                )
                if n_seeded:
                    print(
                        f"  Negative cache: seeded {n_seeded} known-missing "
                        f"Friday(s) from the prior output "
                        f"(last attempted {store_fetch_date})",
                        flush=True,
                    )
    n_reused = 0
    n_negcache_skipped = 0
    n_live_attempts = 0

    snapshots: dict[str, dict] = {}
    walkbacks: list[dict] = []
    carry_forwards: list[dict] = []
    edgar_used: list[dict] = []  # Phase 26.2 — audit trail
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
        reused = reusable.get(friday.isoformat())
        if reused is not None:
            # Real iShares-derived snapshot from the prior output — reuse
            # without any network traffic. Walkback status (and hence the
            # walkbacks audit list) is reconstructed from actual_date, so
            # the payload matches what a full run would produce from the
            # same underlying data.
            tickers = list(reused["tickers"])
            actual = date.fromisoformat(reused["actual_date"])
            status = "exact" if actual == friday else "walkback"
            n_reused += 1
        else:
            decision = (
                negcache.decide(symbol, friday, today)
                if args.incremental else "attempt"
            )
            if decision == "skip":
                # Known-missing Friday, not yet due for its monthly probe.
                # Flows into the same EDGAR / carry-forward path a failed
                # live attempt would.
                tickers, actual, status = None, None, "not_found"
                n_negcache_skipped += 1
            else:
                n_live_attempts += 1
                try:
                    tickers, actual, status = get_snapshot(
                        friday, etf_cfg, probe=(decision == "probe")
                    )
                except Exception as e:
                    print(f"  ERROR on {friday}: {e}", flush=True)
                    tickers, actual, status = None, None, "not_found"
                if tickers is None:
                    negcache.record_failure(symbol, friday, today)
                else:
                    negcache.record_success(symbol, friday)

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
            if prev_tickers is None or prev_actual is None or prev_target is None:
                carry_forwards.append({
                    "target_friday": friday.isoformat(),
                    "outcome": "skipped",
                    "reason": (
                        f"no holdings data within {MAX_WALKBACK_DAYS} days back "
                        "from target Friday and no prior snapshot to carry forward"
                    ),
                })
                continue
            carry_forwards.append({
                "target_friday": friday.isoformat(),
                "outcome": "carried_forward",
                "carried_from_target": prev_target.isoformat(),
                "carried_from_actual": prev_actual.isoformat(),
                "reason": (
                    f"no holdings data within {MAX_WALKBACK_DAYS} days back from "
                    "target Friday — reused most recent prior snapshot"
                ),
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

    # Persist any negative-cache changes (recorded in both modes) before
    # the staleness-driven early returns below.
    negcache.save()

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
        "source": etf_cfg["csv_url_template"],
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
        f"Wrote {_display_path(out_path)} -- "
        f"{len(snapshots)} snapshots, "
        f"{len(walkbacks)} walkbacks, "
        f"{len(carry_forwards)} carry-forwards, "
        f"{len(edgar_used)} EDGAR fallbacks"
    )
    print(
        f"  Mode: {'incremental' if args.incremental else 'full'} -- "
        f"{n_reused} Friday(s) reused from prior output, "
        f"{n_live_attempts} live-attempted, "
        f"{n_negcache_skipped} skipped via negative cache"
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
        return EXIT_STALENESS_CRITICAL
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
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
