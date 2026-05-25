"""Integrity sweeps over checked-in generated data artefacts.

These tests do not run any logic — they walk the data directory and
assert that all generated JSONs satisfy basic shape invariants. The
intent is to catch the next instance of the Phase 13 corruption (literal
"-" tickers, duplicates, length mismatches) automatically in CI, rather
than waiting for a reviewer to spot it.

If a future regeneration introduces a regression, these tests fail loud
and name the exact file + date + violation.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def test_constituent_snapshots_are_clean_sets():
    """Every snapshot in every constituents_*.json must be a clean set:
    no duplicates, no placeholder rows ("", "-", "-.PA"-style, .RI rights),
    and n_tickers must equal the actual tickers list length."""
    for path in DATA_DIR.glob("constituents_*.json"):
        blob = json.loads(path.read_text(encoding="utf-8"))
        for date, snap in blob.get("snapshots", {}).items():
            tickers = snap.get("tickers", [])
            counts = Counter(tickers)
            duplicates = sorted(t for t, n in counts.items() if n > 1)
            placeholders = [
                t for t in tickers
                if not t or t == "-" or t.startswith("-.") or ".RI." in t
            ]

            assert not duplicates, f"{path.name} {date} duplicates: {duplicates[:5]}"
            assert not placeholders, f"{path.name} {date} placeholders: {placeholders[:5]}"
            assert snap.get("n_tickers") == len(tickers), (
                f"{path.name} {date} n_tickers does not match ticker count"
            )


def test_breadth_series_columns_match_dates():
    """Every series column in every breadth_*.json must have the same
    length as the dates array. Every referenced signal date must exist
    in the series dates."""
    for path in DATA_DIR.glob("breadth_*.json"):
        blob = json.loads(path.read_text(encoding="utf-8"))
        series = blob.get("series", {})
        dates = series.get("dates", [])
        assert dates, f"{path.name} has no dates"
        for key, values in series.items():
            if isinstance(values, list):
                assert len(values) == len(dates), (
                    f"{path.name} series.{key} length {len(values)} != dates {len(dates)}"
                )
        date_set = set(dates)
        for signal in blob.get("signals", []):
            assert signal.get("date") in date_set, (
                f"{path.name} signal date {signal.get('date')} not in series dates"
            )


def test_backtest_equity_curve_shapes_are_consistent():
    """Every equity curve in every backtest*.json must have all its
    list-valued columns aligned with its dates array."""
    for path in DATA_DIR.glob("backtest*.json"):
        blob = json.loads(path.read_text(encoding="utf-8"))
        curves = []
        if "equity_curves" in blob:
            curves.append(blob["equity_curves"])
        for variant in blob.get("variants", {}).values():
            if isinstance(variant, dict) and "equity_curve" in variant:
                curves.append(variant["equity_curve"])
        for curve in curves:
            dates = curve.get("dates", [])
            assert dates, f"{path.name} has an equity curve with no dates"
            for key, values in curve.items():
                if key == "dates" or not isinstance(values, list):
                    continue
                assert len(values) == len(dates), (
                    f"{path.name} equity_curve.{key} length {len(values)} != dates {len(dates)}"
                )
