"""Re-parse cached iShares CSV snapshots under the current parser logic.

Use this after a parse_holdings / _resolve_yf_symbol fix to scrub
historical snapshots without re-fetching from iShares. Reads the raw
CSV cache at data/raw_ishares/, reparses each snapshot referenced by
the target JSON, and writes the cleaned snapshot list back.

Usage:
    python scripts/regenerate_constituents_from_cache.py ETF1 ETF2 ...

If no ETFs are passed, falls back to the 14 affected by the Phase 13
parser fix.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from etf_registry import get_etf  # noqa: E402
from fetch_constituents import RAW_DIR, parse_holdings  # noqa: E402

DATA_DIR = ROOT / "data"

# 14 files affected by the Phase 13 parser fix (Codex review #2).
DEFAULT_ETFS = [
    "CSP1", "EXH1", "EXH3", "EXH9", "EXV1", "EXV3",
    "ICHN", "IDP6", "ITWN", "IUCM", "IUES", "IUHC", "IUSP", "NDIA",
]


def regenerate(etf: str) -> dict:
    """Reparse cached CSVs for one ETF; return summary dict."""
    cfg = get_etf(etf)
    symbol = cfg["symbol"]
    overrides = cfg.get("ticker_overrides", {})
    apply_suffix = cfg.get("apply_exchange_suffix", False)

    json_path = DATA_DIR / f"constituents_{etf.lower()}.json"
    if not json_path.exists():
        return {"etf": etf, "status": "missing_json"}
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    snapshots = doc["snapshots"]

    n_changed = 0
    n_unchanged = 0
    n_no_cache = 0
    before_total = 0
    after_total = 0

    for friday, snap in snapshots.items():
        actual = snap.get("actual_date") or friday
        actual_dt = datetime.strptime(actual, "%Y-%m-%d").date()
        cache_path = RAW_DIR / f"{symbol}_{actual_dt.strftime('%Y%m%d')}.csv"
        if not cache_path.exists():
            n_no_cache += 1
            continue
        body = cache_path.read_text(encoding="utf-8", errors="replace")
        new_tickers = parse_holdings(body, ticker_overrides=overrides,
                                     apply_exchange_suffix=apply_suffix)
        old_tickers = snap.get("tickers", [])
        before_total += len(old_tickers)
        after_total += len(new_tickers)
        if new_tickers != old_tickers:
            snap["tickers"] = new_tickers
            snap["n_tickers"] = len(new_tickers)
            n_changed += 1
        else:
            n_unchanged += 1

    doc["fetched_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    json_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    return {
        "etf": etf,
        "snapshots": len(snapshots),
        "changed": n_changed,
        "unchanged": n_unchanged,
        "no_cache": n_no_cache,
        "tickers_before": before_total,
        "tickers_after": after_total,
        "delta": after_total - before_total,
    }


def main() -> int:
    etfs = sys.argv[1:] or DEFAULT_ETFS
    print(f"Regenerating {len(etfs)} constituent files from raw_ishares cache ...\n")
    rows = []
    for etf in etfs:
        try:
            row = regenerate(etf)
        except Exception as exc:  # noqa: BLE001
            row = {"etf": etf, "status": f"error: {exc}"}
        rows.append(row)
        print(f"  {row}")
    print(f"\nDone. {sum(1 for r in rows if r.get('changed', 0))} files had at least one snapshot changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
