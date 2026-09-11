"""Live capture smoke test in logs/, preserving deployed artefacts.

Refreshes one current weekly roster and its full constituent price history
using the production code. Raw vendor data stays in the ignored logs folder.
Python datetime months are 1-indexed.
"""
import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import compute_breadth as cb
import fetch_constituents as fc
from etf_registry import get_etf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--etf", default="EXH1")
    parser.add_argument("--price-source", choices=("auto", "yfinance"), default="auto")
    args = parser.parse_args()
    etf = args.etf.upper()
    cfg = get_etf(etf)
    out = ROOT / "logs" / "capture-smoke" / etf.lower()
    out.mkdir(parents=True, exist_ok=True)
    target = fc.latest_completed_friday(date.today())
    with patch.object(fc, "RAW_DIR", out / "raw"):
        tickers, actual, status = fc.get_snapshot(target, cfg, refresh=True)
    if not tickers:
        raise RuntimeError(f"Current roster unavailable: {status}")
    roster = json.loads((ROOT / "data" / f"constituents_{etf.lower()}.json").read_text(encoding="utf-8"))
    roster["snapshots"][str(target)] = {"actual_date": str(actual), "tickers": tickers,
                                         "n_tickers": len(tickers)}
    roster["end_friday"] = str(target)
    roster["endpoint_health"] = {"status": "ok"}
    (out / f"constituents_{etf.lower()}.json").write_text(json.dumps(roster), encoding="utf-8")
    # Preserve historical backfills exactly as the production merge does.
    for name in (f"prices_cache_{etf.lower()}.parquet", f"breadth_{etf.lower()}.json"):
        shutil.copy2(ROOT / "data" / name, out / name)
    with patch.object(cb, "DATA_DIR", out), patch.object(sys, "argv", [
            "compute_breadth.py", "--etf", etf, "--price-source", args.price_source]):
        rc = cb.main()
    if rc:
        return rc
    panel = json.loads((out / f"breadth_{etf.lower()}.json").read_text(encoding="utf-8"))
    print("\nLIVE CAPTURE RESULT")
    print(json.dumps(panel["current_capture"], indent=2))
    print(f"Deployed files untouched; evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
