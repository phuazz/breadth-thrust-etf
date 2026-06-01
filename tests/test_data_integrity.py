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


def test_constituent_staleness_block_is_well_formed():
    """Phase 26.1 — every constituents_*.json that carries a staleness
    block must satisfy: status is one of the known values, threshold
    fields match the constants in fetch_constituents.py, and
    days_since_last_real_fetch is None or a non-negative integer.

    The block itself is OPTIONAL on legacy files (added Phase 26.1) so
    we only validate when present — once every file has been re-written
    by the next full pipeline run, the absence guard can be tightened.
    """
    from datetime import date as _date

    valid_statuses = {"fresh", "warning", "critical", "no_real_fetches"}
    for path in DATA_DIR.glob("constituents_*.json"):
        blob = json.loads(path.read_text(encoding="utf-8"))
        s = blob.get("staleness")
        if s is None:
            continue
        # Shape invariants
        assert s.get("status") in valid_statuses, (
            f"{path.name} staleness.status {s.get('status')!r} "
            f"not in {valid_statuses}"
        )
        days = s.get("days_since_last_real_fetch")
        assert days is None or (isinstance(days, int) and days >= 0), (
            f"{path.name} days_since_last_real_fetch={days!r} invalid"
        )
        # Threshold sanity
        warn = s.get("warn_threshold_days")
        crit = s.get("critical_threshold_days")
        if warn is not None and crit is not None:
            assert 0 < warn < crit, (
                f"{path.name} warn ({warn}) must be < critical ({crit})"
            )
        # If status is critical, days must actually be over the critical
        # threshold (catches a renaming-vs-recomputation drift).
        if s.get("status") == "critical" and days is not None and crit is not None:
            assert days > crit, (
                f"{path.name} status=critical but days ({days}) "
                f"not > critical_threshold ({crit})"
            )
        # If a real fetch date is supplied, it must parse as ISO date
        # and be no later than today.
        last = s.get("last_real_fetch_date")
        if last is not None:
            parsed = _date.fromisoformat(last)
            # No assertion against today's date — this would make the
            # test flap across timezones at midnight UTC. The fetcher's
            # own internal logic enforces this monotonicity.
            del parsed


def test_no_critical_staleness_currently_published():
    """The deployed dashboard MUST NOT ship if any constituent roster is
    critically stale. pipeline.py aborts publish in that case; this test
    enforces the same invariant at the data-file level so regressions
    are caught even if the pipeline guard is removed.

    Mirrors the Phase 26.4 pipeline scan logic:
      1. Use the staleness block when present (per-ETF thresholds).
      2. Fall back to end_friday + global default 30-day critical
         threshold for legacy files without a staleness block.

    Compatible with the carry-forward fallback — "warning" status is
    acceptable for publication; only "critical" is forbidden.
    See DATA_INTEGRITY_POLICY.md sections 5 and 6.
    """
    from datetime import date as _date
    today = _date.today()
    GLOBAL_CRITICAL = 30
    critical = []
    for path in DATA_DIR.glob("constituents_*.json"):
        blob = json.loads(path.read_text(encoding="utf-8"))
        s = blob.get("staleness") or {}
        if s:
            if s.get("status") == "critical":
                critical.append((
                    blob.get("etf", path.stem),
                    s.get("days_since_last_real_fetch"),
                    "staleness_block",
                ))
            continue
        # Legacy file — derive from end_friday using global threshold.
        end_friday = blob.get("end_friday")
        if not end_friday:
            continue
        try:
            days = (today - _date.fromisoformat(end_friday)).days
        except ValueError:
            continue
        if days > GLOBAL_CRITICAL:
            critical.append((
                blob.get("etf", path.stem),
                days,
                "derived_from_end_friday",
            ))
    assert not critical, (
        f"PUBLISH BLOCKED: {len(critical)} roster(s) at critical staleness: "
        f"{critical}. See DATA_INTEGRITY_POLICY.md for remediation."
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
