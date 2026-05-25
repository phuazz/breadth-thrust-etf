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

Run:
    python scripts/fetch_constituents.py             # default: SOXX
    python scripts/fetch_constituents.py --etf CSP1  # S&P 500 via iShares UK
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
RETRY_BACKOFFS = [5, 10, 30]  # seconds; 3 retries on transport failure or 5xx

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

    url = f"{etf_cfg['csv_url_template']}&asOfDate={target.strftime('%Y%m%d')}"
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
    overrides = overrides or {}
    if raw_ticker in overrides:
        return overrides[raw_ticker]
    # Standard dot → dash conversion for share-class US tickers
    base = raw_ticker.replace(".", "-") if exchange and "United States" not in (exchange or "") and exchange.lower().startswith(("nasdaq", "nyse", "cboe")) else raw_ticker
    # Note: only convert dots-to-dashes for US tickers; for non-US tickers,
    # dots may have meaning we don't want to touch.
    if exchange:
        ex_key = exchange.strip()
        if ex_key in _EXCHANGE_TO_YF_SUFFIX:
            suffix = _EXCHANGE_TO_YF_SUFFIX[ex_key]
            # If the suffix is empty (US listing), apply dot→dash share-class fix
            if suffix == "":
                return base.replace(".", "-")
            return f"{raw_ticker}{suffix}"
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
        exchange = (row[exchange_idx].strip() if exchange_idx is not None
                                              and len(row) > exchange_idx
                                              else None)
        if apply_exchange_suffix:
            sym = _resolve_yf_symbol(raw, exchange, overrides)
            if sym is None:
                continue
            tickers.append(sym)
        else:
            tickers.append(overrides.get(raw, raw))
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


def get_snapshot(
    target_friday: date, etf_cfg: dict
) -> tuple[list[str] | None, date | None, str]:
    """Walk back from `target_friday` looking for a populated holdings file.

    Returns (tickers, actual_date, status). `status` is one of:
      - "exact"     : Friday returned data
      - "walkback"  : an earlier weekday in the same week returned data
      - "not_found" : no data within MAX_WALKBACK_DAYS days
    """
    overrides = etf_cfg.get("ticker_overrides", {})
    apply_suffix = etf_cfg.get("apply_exchange_suffix", False)
    for days_back in range(MAX_WALKBACK_DAYS + 1):
        try_date = target_friday - timedelta(days=days_back)
        body = fetch_with_retry(try_date, etf_cfg)
        tickers = parse_holdings(body, ticker_overrides=overrides,
                                  apply_exchange_suffix=apply_suffix)
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
    prev_tickers: list[str] | None = None
    prev_actual: date | None = None
    prev_target: date | None = None

    for i, friday in enumerate(fridays, start=1):
        if i == 1 or i % 25 == 0 or i == len(fridays):
            print(f"  [{i}/{len(fridays)}] {friday.isoformat()}", flush=True)
        try:
            tickers, actual, status = get_snapshot(friday, etf_cfg)
        except Exception as e:
            print(f"  ERROR on {friday}: {e}", flush=True)
            tickers, actual, status = None, None, "not_found"

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
            snapshots[friday.isoformat()] = {
                "actual_date": actual.isoformat(),
                "n_tickers": len(tickers),
                "tickers": tickers,
            }
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
        "snapshots": snapshots,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    print(
        f"Wrote {out_path.relative_to(PROJECT_ROOT)} -- "
        f"{len(snapshots)} snapshots, "
        f"{len(walkbacks)} walkbacks, "
        f"{len(carry_forwards)} carry-forwards"
    )
    if walkbacks:
        print(f"  First walkback: {walkbacks[0]}")
    if carry_forwards:
        print("  WARNING -- carry-forwards required:")
        for cf in carry_forwards:
            print(f"    {cf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
