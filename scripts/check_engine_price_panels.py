"""Did any sleeve backtest against a price series that could not support one?

The fifth VERIFY step, added 2026-08-15. The other four were all running on
the day sleeve A published Sharpe 0.76 / CAGR 11.2% / total return +130%
against committed values of 0.93 / 16.9% / +238%, and not one of them saw
it. Capture integrity asks whether the newest bar arrived; pair integrity
asks whether a fund tracks its own constituents; the refresh guard asks
whether the panels agree with each other; freshness headroom forecasts the
CI lag. None of them asks the question that mattered: does the close series
each engine actually priced its universe off exist across the window it
backtested?

The only thing that fired was
``tests/test_figure_bindings.py::test_committed_literals_match_the_data``,
because pinned literals moved. That is a tripwire on the consequence. It
would have said nothing had the damage landed on an unpinned figure, and it
could not name the series at fault.

TWO CHECKS, DIFFERENT REACH
---------------------------
1. THE ARTEFACT. Every committed sleeve JSON is read and its attribution
   scanned for the tell: a large ``days_held`` beside an
   ``ann_return_when_held`` of exactly 0.0. Offline, no caches needed, so it
   runs anywhere including a fresh CI runner. This is the check that would
   have caught 2026-08-15 from the published file alone.

2. THE PANEL. Where the price caches exist on disk — they are gitignored, so
   locally — each sleeve's close panel is rebuilt and judged over the
   ``eligible_start`` its own JSON records. Members with no cache are
   skipped rather than failed, because a missing local cache is the normal
   state on a runner and not evidence of anything.

Sleeve D is judged on its EUR panel rather than the USD one the engine
ranks on. Degeneracy survives an FX multiplication, so the approximation
only misses a defect introduced BY the FX leg — which the engine's own
in-run guard sees, because it checks the converted panel.

Exit codes: 0 all clear, 1 at least one breach. Intended for
scripts/refresh_all.py step 7 and the weekly workflow, not for pytest — the
pure logic is unit-tested in tests/test_price_panel_guard.py.

Usage:
    python scripts/check_engine_price_panels.py
    python scripts/check_engine_price_panels.py --sleeves A D --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
# Before any import that might reconfigure it for us halfway down the report.
sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import (  # noqa: E402
    ETF_REGISTRY, UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS,
)
from price_panel_guard import (  # noqa: E402
    FAIL, SKIP, assess_panel, format_verdicts, zero_return_rows,
)

DATA_DIR = ROOT / "data"

SLEEVES = {
    "A": ("Strategy A (US sectors)", "topk_robustness.json"),
    "B": ("Strategy B (asset class)", "asset_class_rotation.json"),
    "C": ("Strategy C (thematic)", "thematic_rotation.json"),
    "D": ("Strategy D (Europe sectors)", "europe_rotation.json"),
}


def _proxy(key: str) -> str:
    return (ETF_REGISTRY.get(key) or {}).get("yfinance_trading_proxy") or key


def _per_etf_panel(universe: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Close panel assembled from the per-ETF OHLC caches, plus the members
    whose cache is absent (skipped, not failed)."""
    closes, missing = {}, []
    for key in universe:
        path = DATA_DIR / f"{_proxy(key).lower()}_ohlc_cache.parquet"
        if not path.exists():
            missing.append(key)
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception:
            missing.append(key)
            continue
        if "Close" not in frame.columns:
            missing.append(key)
            continue
        col = frame["Close"]
        if isinstance(col, pd.DataFrame):
            col = col.iloc[:, 0]
        closes[key] = col.astype(float)
    return pd.DataFrame(closes).sort_index(), missing


def _multi_etf_panel(filename: str) -> tuple[pd.DataFrame, list[str]]:
    path = DATA_DIR / filename
    if not path.exists():
        return pd.DataFrame(), [filename]
    try:
        return pd.read_parquet(path).sort_index(), []
    except Exception:
        return pd.DataFrame(), [filename]


def _late_inception_tickers() -> set[str]:
    """Sleeve C's declared late-inception members, read from the engine so
    the two cannot drift apart."""
    try:
        from run_thematic_rotation import UNIVERSE  # noqa: PLC0415
    except Exception:
        return set()
    return {t for t, m in UNIVERSE.items() if m.get("late_inception")}


def sleeve_panel(sleeve: str) -> tuple[pd.DataFrame, list[str], set[str]]:
    """(close panel, members skipped for want of a cache, late-inception set)."""
    if sleeve == "A":
        panel, missing = _per_etf_panel(list(UNIVERSE_ETFS))
        return panel, missing, set()
    if sleeve == "D":
        panel, missing = _per_etf_panel(list(UNIVERSE_EUROPE_SECTORS))
        return panel, missing, set()
    if sleeve == "B":
        panel, missing = _multi_etf_panel("asset_class_prices_cache.parquet")
        return panel, missing, set()
    panel, missing = _multi_etf_panel("thematic_prices_cache.parquet")
    return panel, missing, _late_inception_tickers()


def check_sleeve(sleeve: str, verbose: bool = False) -> list[str]:
    """Breaches for one sleeve. Empty list means clear."""
    label, filename = SLEEVES[sleeve]
    path = DATA_DIR / filename
    breaches: list[str] = []
    print(f"\n{label} — {filename}")
    if not path.exists():
        print(f"  SKIP: {filename} not present")
        return breaches

    blob = json.loads(path.read_text(encoding="utf-8"))
    headline = blob.get("headline") or {}
    attribution = headline.get("attribution") or blob.get("attribution") or {}

    # 1. The artefact.
    hits = zero_return_rows(attribution)
    if hits:
        for member, why in hits:
            print(f"  FAIL {member}: {why}")
            breaches.append(f"{sleeve}/{member}: {why}")
    else:
        print(f"  attribution: {len(attribution)} row(s), none reporting a "
              f"held ETF that never moved")

    # 2. The panel.
    eligible = headline.get("eligible_start")
    panel, missing, late = sleeve_panel(sleeve)
    if missing:
        print(f"  no local cache for {', '.join(missing)} — panel check "
              f"skipped for those")
    if panel.empty:
        print("  panel: no cached prices on disk, panel check skipped")
        return breaches

    window_start = pd.Timestamp(eligible) if eligible else None
    verdicts = assess_panel(panel, window_start=window_start, allow_late=late)
    failed = [v for v in verdicts if v.status == FAIL]
    skipped = [v for v in verdicts if v.status == SKIP]
    print(f"  panel: {len(verdicts) - len(failed) - len(skipped)} pass, "
          f"{len(failed)} FAIL, {len(skipped)} skip"
          f"{f' from {window_start.date()}' if window_start is not None else ''}")
    if failed or verbose:
        print(format_verdicts(verdicts))
    for v in failed:
        breaches.append(f"{sleeve}/{v.member}: {v.note}")
    return breaches


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sleeves", nargs="*", default=sorted(SLEEVES),
                    choices=sorted(SLEEVES),
                    help="sleeves to check (default: all four)")
    ap.add_argument("--verbose", action="store_true",
                    help="print the per-member table even when it all passes")
    args = ap.parse_args(argv)

    print("Checking that every sleeve was backtested on a usable price panel ...")
    breaches: list[str] = []
    for sleeve in args.sleeves:
        breaches.extend(check_sleeve(sleeve, verbose=args.verbose))

    print(f"\n{'=' * 72}")
    if not breaches:
        print(f"All clear across {len(args.sleeves)} sleeve(s).")
        return 0
    print(f"{len(breaches)} BREACH(ES):")
    for b in breaches:
        print(f"  - {b}")
    print(
        "\n[ENGINE-PRICE-PANEL] A sleeve was backtested against a price "
        "series that cannot support one. Its headline Sharpe, CAGR and total "
        "return are wrong, and multi_strategy, portfolio_construction, "
        "phase7, phase8, docs/index.html and the factsheet inherit them. Do "
        "NOT commit or publish this state. Repair with `python "
        "scripts/export_holdings_prices.py --refresh-caches-only`, then "
        "re-run the affected engine."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
