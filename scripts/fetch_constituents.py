"""Step 1 — pull point-in-time SOXX constituent rosters from iShares.

Pulls one snapshot per Friday from START_FRIDAY through the most recent
completed Friday and writes a structured JSON to data/constituents_soxx.json.

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
    python scripts/fetch_constituents.py
"""

from __future__ import annotations

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

# Force UTF-8 stdout so the BOM in iShares CSVs and any non-ASCII names do
# not crash on the Windows cp1252 console.
sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw_ishares"
OUT_PATH = DATA_DIR / "constituents_soxx.json"

ETF = "SOXX"
ISHARES_URL = (
    "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/"
    "1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Per session decision 2026-05-14: backtest window starts 2018.
# Note: Python's datetime constructor is 1-indexed for months (Jan=1), unlike
# JavaScript's Date which is 0-indexed (Jan=0). We always use Python here.
START_FRIDAY = date(2018, 1, 5)  # first Friday of 2018
MAX_WALKBACK_DAYS = 5  # how far back from a target Friday to search

THROTTLE_BASE_SECONDS = 1.5
THROTTLE_JITTER_SECONDS = 0.5
RETRY_BACKOFFS = [5, 10, 30]  # seconds; 3 retries on transport failure or 5xx


def fetch_with_retry(target: date) -> str:
    """Fetch the raw iShares CSV for `target` and return the body.

    Caches successful 200 responses to disk so reruns do not re-hit iShares.
    Empty-template responses (Fund Holdings as of "-") are also cached because
    they are stable over time for old dates (US holidays, the 2017 data gap)
    — re-fetching them would waste requests.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = RAW_DIR / f"{ETF}_{target.strftime('%Y%m%d')}.csv"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    url = f"{ISHARES_URL}&asOfDate={target.strftime('%Y%m%d')}"
    last_err: Exception | None = None
    for backoff in [0, *RETRY_BACKOFFS]:
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        except Exception as e:
            last_err = e
            continue
        if r.status_code == 200 and len(r.text) > 1000:
            cache_path.write_text(r.text, encoding="utf-8")
            time.sleep(THROTTLE_BASE_SECONDS + random.uniform(0, THROTTLE_JITTER_SECONDS))
            return r.text
        last_err = RuntimeError(f"HTTP {r.status_code}, body {len(r.text)} bytes")
    raise RuntimeError(f"Failed to fetch SOXX holdings for {target}: {last_err}")


def parse_holdings(body: str) -> list[str]:
    """Parse iShares CSV body and return Equity-only ticker list, or [] if empty.

    CSV layout: preamble of fund-level metadata (Fund name, "Fund Holdings as
    of <date>", inception date, totals), then a header row beginning
    'Ticker,Name,Sector,Asset Class,...', then one row per holding, then a
    blank line that terminates the holdings block. The file then continues
    with disclosures we do not need.

    An "empty template" file (no holdings) is detected by the literal token
    'Fund Holdings as of,"-"' in the preamble.
    """
    if 'Fund Holdings as of,"-"' in body:
        return []
    tickers: list[str] = []
    header: list[str] | None = None
    asset_class_idx: int | None = None
    for ln in body.splitlines():
        if header is None:
            if "Ticker" in ln[:20] and "Asset Class" in ln:
                header = next(csv.reader(io.StringIO(ln)))
                asset_class_idx = header.index("Asset Class")
            continue
        if not ln.strip():
            break  # blank line terminates the holdings block
        row = next(csv.reader(io.StringIO(ln)))
        if not row or not row[0]:
            continue
        if asset_class_idx is not None and len(row) > asset_class_idx:
            if row[asset_class_idx].strip() != "Equity":
                continue
        tickers.append(row[0].strip())
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


def get_snapshot(target_friday: date) -> tuple[list[str] | None, date | None, str]:
    """Walk back from `target_friday` looking for a populated holdings file.

    Returns (tickers, actual_date, status). `status` is one of:
      - "exact"     : Friday returned data
      - "walkback"  : an earlier weekday in the same week returned data
      - "not_found" : no data within MAX_WALKBACK_DAYS days
    """
    for days_back in range(MAX_WALKBACK_DAYS + 1):
        try_date = target_friday - timedelta(days=days_back)
        body = fetch_with_retry(try_date)
        tickers = parse_holdings(body)
        if tickers:
            status = "exact" if days_back == 0 else "walkback"
            return tickers, try_date, status
    return None, None, "not_found"


def main() -> int:
    today = date.today()
    end_friday = latest_completed_friday(today)
    fridays = fridays_between(START_FRIDAY, end_friday)
    print(
        f"Fetching SOXX point-in-time holdings for {len(fridays)} Fridays "
        f"({START_FRIDAY} -> {end_friday})",
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
            tickers, actual, status = get_snapshot(friday)
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
        "etf": ETF,
        "source": ISHARES_URL,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_friday": START_FRIDAY.isoformat(),
        "end_friday": end_friday.isoformat(),
        "n_target_fridays": len(fridays),
        "n_snapshots_written": len(snapshots),
        "membership_assumption": (
            "Constituents held static between weekly Friday snapshots. SOXX "
            "rebalances quarterly; weekly oversamples membership and protects "
            "against off-cycle add/drops."
        ),
        "asset_class_filter": "Equity",
        "walkbacks": walkbacks,
        "carry_forwards": carry_forwards,
        "snapshots": snapshots,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    print(
        f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)} -- "
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
