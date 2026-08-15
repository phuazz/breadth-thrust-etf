"""One-off fetch of ETF long names for the scanner's Name column.

Run manually, commit the result, and do not wire this into the daily job.
``Ticker.info`` is slow (a second or more per ticker) and flaky, and fund
long names effectively never change — paying that cost every morning
would add a failure mode to the daily build in exchange for nothing.

Usage:
    python scripts/fetch_etf_names.py              # fill gaps only
    python scripts/fetch_etf_names.py --refresh    # re-fetch everything

Existing entries are never overwritten with a blank: a ticker whose
``info`` call comes back empty keeps whatever name is already committed.
Anything still missing at the end is reported for hand-filling, which is
the expected outcome for the Xetra lines and the Shenzhen listing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scanner_universe import resolve_universe  # noqa: E402

NAMES_PATH = ROOT / "data" / "etf_names.json"
RISK_OVERLAY_PATH = ROOT / "data" / "risk_overlay.json"
RETRIES = 3
RETRY_PAUSE_SECONDS = 2.0


def overlay_fallback_ticker() -> str | None:
    """The risk overlay's de-risk instrument, read from its own config.

    It is not a scanner instrument, so resolve_universe() does not return it,
    but it CAN appear in the book: when the regime gate de-risks, NAV is
    parked in it. build_simple_page refuses to print a bare ticker to a
    non-specialist reader, so a name missing here fails the public page —
    which is exactly what happened on 2026-08-15, when SHY turned up holding
    one basis point and the page would not build.

    Read from config rather than hardcoded so that changing the parameter
    cannot silently reintroduce the gap.
    """
    if not RISK_OVERLAY_PATH.exists():
        return None
    try:
        overlay = json.loads(RISK_OVERLAY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ticker = (overlay.get("gate_parameters") or {}).get("fallback_ticker")
    return str(ticker).strip() or None if ticker else None

# Fields yfinance may carry the fund name under, in preference order.
NAME_FIELDS = ("longName", "shortName")


def _fetch_one(ticker: str) -> str | None:
    import yfinance as yf

    for attempt in range(1, RETRIES + 1):
        try:
            info = yf.Ticker(ticker).info or {}
            for field in NAME_FIELDS:
                value = info.get(field)
                if value and str(value).strip():
                    return str(value).strip()
            return None
        except Exception as exc:  # noqa: BLE001 — one-off tool, report and move on
            if attempt == RETRIES:
                print(f"  {ticker}: failed after {RETRIES} attempts ({exc})")
                return None
            time.sleep(RETRY_PAUSE_SECONDS)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch names that are already committed",
    )
    args = parser.parse_args(argv)

    existing: dict[str, str] = {}
    if NAMES_PATH.exists():
        existing = json.loads(NAMES_PATH.read_text(encoding="utf-8"))

    tickers = [row.scan_ticker for row in resolve_universe()]
    fallback = overlay_fallback_ticker()
    if fallback and fallback not in tickers:
        tickers.append(fallback)
    todo = tickers if args.refresh else [t for t in tickers if not existing.get(t)]
    print(f"{len(tickers)} tickers in the universe, {len(todo)} to fetch")

    for i, ticker in enumerate(todo, start=1):
        name = _fetch_one(ticker)
        if name:
            existing[ticker] = name
            print(f"  [{i}/{len(todo)}] {ticker}: {name}")
        else:
            print(f"  [{i}/{len(todo)}] {ticker}: no name returned "
                  f"(keeping {existing.get(ticker) or 'nothing'})")

    ordered = {t: existing[t] for t in tickers if existing.get(t)}
    # Preserve any committed name for a ticker that has since left the
    # universe rather than silently dropping it — cheap, and it keeps the
    # file usable if a sleeve member returns.
    for ticker in sorted(set(existing) - set(ordered)):
        ordered[ticker] = existing[ticker]

    NAMES_PATH.write_text(
        json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nwritten: {NAMES_PATH.relative_to(ROOT)} ({len(ordered)} names)")

    missing = [t for t in tickers if not ordered.get(t)]
    if missing:
        print(f"STILL MISSING ({len(missing)}) — hand-fill these in the JSON:")
        for t in missing:
            print(f"  - {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
