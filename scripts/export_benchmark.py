"""The benchmark series the email and the factsheet compare against, committed.

WHY (2026-09-06). Both builders read SPY from data/asset_class_prices_cache
.parquet, the sleeve B engine cache. That file is gitignored, so on the CI
runner that builds and sends the factsheet it exists only when CI has just
re-run Strategy B — and CI keeps the committed engine whenever a re-run
cannot improve on it, which is every normal week. The first automatic-era
send (2026-09-06) therefore went out with "—" under every KPI on the PDF's
first page and no SPY line on the email's tiles, while the same builders
run locally showed "SPY +12.7% · vs +5.0%". The comparison was designed
in; the input was simply not there.

So the local refresh exports the series here, as a small committed JSON,
and the builders read the parquet when present (identical numbers, one
source) and this file otherwise. Adjusted closes, so the benchmark is total
return like the strategy's own equity — the basis is written into the file
so a reader of the number can see it.

Python datetime months are 1-indexed (January = 1).

Usage:
    python scripts/export_benchmark.py            # writes data/benchmark_spy.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE = DATA_DIR / "asset_class_prices_cache.parquet"
OUT = DATA_DIR / "benchmark_spy.json"
TICKER = "SPY"


def spy_from_cache(cache: Path = CACHE) -> pd.Series | None:
    """SPY adjusted closes from the engine cache, or None."""
    if not cache.exists():
        return None
    try:
        df = pd.read_parquet(cache)
    except Exception:  # noqa: BLE001
        return None
    if TICKER not in df.columns:
        return None
    s = df[TICKER].dropna()
    if not len(s):
        return None
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def spy_from_json(path: Path = OUT) -> pd.Series | None:
    """The committed export, or None when absent or unreadable."""
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        s = pd.Series(blob["closes"], index=pd.to_datetime(blob["dates"]),
                      dtype=float).dropna()
    except Exception:  # noqa: BLE001
        return None
    return s.sort_index() if len(s) else None


def load_spy_series(cache: Path = CACHE, path: Path = OUT) -> pd.Series | None:
    """The parquet when it is here (the local machine), else the committed
    export (CI), else None — every caller degrades to bare figures."""
    s = spy_from_cache(cache)
    return s if s is not None else spy_from_json(path)


def export(cache: Path = CACHE, out: Path = OUT,
           now_utc: datetime | None = None) -> Path | None:
    """Write the JSON from the cache. Refuses to overwrite a longer or newer
    export with a shorter one — a vendor never un-prints a close."""
    s = spy_from_cache(cache)
    if s is None:
        print(f"  no {TICKER} series in {cache.name}; export unchanged", flush=True)
        return None
    prior = spy_from_json(out)
    if prior is not None and (prior.index.max() > s.index.max()
                              or prior.index.min() < s.index.min()):
        print(f"  REFUSED: the fresh {TICKER} series ({s.index.min().date()} -> "
              f"{s.index.max().date()}) would shrink the export "
              f"({prior.index.min().date()} -> {prior.index.max().date()})", flush=True)
        return None
    payload = {
        "ticker": TICKER,
        "basis": "adjusted close (dividends reinvested) from the sleeve B "
                 "engine cache — the same series the local email and factsheet "
                 "builds read",
        "source_file": cache.name,
        "written_at_utc": (now_utc or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "first": str(s.index.min().date()),
        "last": str(s.index.max().date()),
        "dates": [d.strftime("%Y-%m-%d") for d in s.index],
        "closes": [round(float(v), 4) for v in s.values],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"  Wrote {out.name}: {TICKER} {payload['first']} -> {payload['last']}, "
          f"{len(s)} closes, {out.stat().st_size / 1024:.0f} KB", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    return 0 if export(Path(args.cache), Path(args.out)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
