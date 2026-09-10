"""Data Health observation-date semantics.

Regression guard for the 2026-09-10 "88 OK over a stale panel" bug.

``_compute_data_health`` used to derive each feed's "Last Data" date by
deep-walking the JSON and taking the maximum YYYY-MM-DD string found
anywhere. Four kinds of date share that shape and only one of them is an
observation:

  * fetch timestamps  (``computed_at_utc``)
  * forward weekly labels (``ma200_sweep`` resamples to Friday buckets, so
    the week in progress carries the NEXT Friday's label)
  * calendar month-end labels (``phase8_right_tail.common_monthly_window``)
  * scheduled decision / fill dates (``headline.latest_rebalance``)

On the 2026-09-09 23:24 UTC build every one of the 88 rows reported a
non-observation date, two of them dated after the build itself
(``ma200_sweep`` 2026-09-11 at -2d, ``phase8_right_tail`` 2026-09-30 at
-21d), and all 88 classified OK. A negative age must never read as fresh.

These tests exercise the extraction and classification helpers against
synthetic blobs and a tmp data directory. They do not touch real data/ files
and do not assert anything about strategy outputs.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from dateutil.relativedelta import relativedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pipeline as P  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _health_for(tmp_path: Path, monkeypatch, files: dict[str, dict],
                checks: list[dict], today: date) -> dict:
    """Run _compute_data_health over a synthetic data dir holding only
    ``files``, with ``checks`` as the registry and no dynamic breadth /
    roster globs matching."""
    for name, blob in files.items():
        (tmp_path / name).write_text(json.dumps(blob), encoding="utf-8")
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)
    monkeypatch.setattr(P, "DATA_HEALTH_CHECKS", checks)
    return P._compute_data_health(today)


def _row(health: dict, file_name: str) -> dict:
    return next(r for r in health["rows"] if r["file"] == file_name)


BUILD = date(2026, 9, 9)  # Wednesday — the build that shipped the bug


# ---------------------------------------------------------------------------
# 1. phase8_right_tail: a month-end window label must not become Last Data
# ---------------------------------------------------------------------------

def test_phase8_monthly_window_end_after_build_is_not_the_health_date(
        tmp_path, monkeypatch):
    """common_monthly_window ends at the calendar end of the month in
    progress (2026-09-30 on a 2026-09-09 build). The observation basis is
    per_strategy.*.date_range, which ends 2026-09-08."""
    blob = {
        "computed_at_utc": "2026-09-09T07:58:29.217498+00:00",
        "common_monthly_window": ["2018-11-30", "2026-09-30"],
        "per_strategy": {
            "strategy_a": {"date_range": ["2018-10-12", "2026-09-08"]},
            "strategy_d": {"date_range": ["2018-10-02", "2026-09-07"]},
        },
        "regime_decomposition": {
            "ai_surge_2024": {"start": "2024-01-02", "end": "2024-12-31"},
        },
    }
    check = {"key": "p8", "file": "phase8.json", "group": "derived",
             "label": "Phase 8", "warn": 8, "stale": 14,
             "obs": ["per_strategy.*.date_range.-1"]}
    health = _health_for(tmp_path, monkeypatch, {"phase8.json": blob},
                         [check], BUILD)
    row = _row(health, "phase8.json")

    assert row["last_data_date"] == "2026-09-08"
    assert row["last_data_date"] != "2026-09-30"
    assert row["days_old"] == 1
    assert row["status"] == "ok"


def test_phase8_month_end_label_would_have_been_picked_by_a_max_walk(
        tmp_path, monkeypatch):
    """The failure is only interesting if the month-end label really is the
    maximum date in the blob — pin that, so this test still means something
    if the fixture is edited later."""
    blob = {
        "common_monthly_window": ["2018-11-30", "2026-09-30"],
        "per_strategy": {"strategy_a": {"date_range": ["2018-10-12", "2026-09-08"]}},
    }
    every_date = []

    def walk(o):
        if isinstance(o, str) and len(o) == 10:
            every_date.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(blob)
    assert max(every_date) == "2026-09-30"          # what the old walk took
    assert P._observation_date_from(
        blob, ["per_strategy.*.date_range.-1"]) == "2026-09-08"


# ---------------------------------------------------------------------------
# 2. ma200_sweep: forward Friday labels and scheduled dates
# ---------------------------------------------------------------------------

def test_ma200_forward_friday_label_does_not_produce_negative_days_old(
        tmp_path, monkeypatch):
    """breadth_dates is a weekly-resampled Friday LABEL series; the week in
    progress carries the forward Friday (2026-09-11) over a Tuesday
    2026-09-08 observation. monitor.*.as_of is the observation."""
    blob = {
        "computed_at_utc": "2026-09-09T08:07:33.329070+00:00",
        "per_etf_detail": {
            "SOXX": {
                "breadth_dates": ["2026-08-28", "2026-09-04", "2026-09-11"],
                "breadth_pct": [68.75, 66.67, 68.75],
                "episodes": [{"entry": "2018-05-07", "exit": "2018-06-26"}],
            },
        },
        "monitor": {"SOXX": {"as_of": "2026-09-08", "ma200_breadth_pct": 68.8}},
    }
    check = {"key": "ma200", "file": "ma200.json", "group": "derived",
             "label": "MA200 sweep", "warn": 8, "stale": 14,
             "obs": ["monitor.*.as_of"]}
    health = _health_for(tmp_path, monkeypatch, {"ma200.json": blob},
                         [check], BUILD)
    row = _row(health, "ma200.json")

    assert row["last_data_date"] == "2026-09-08"
    assert row["days_old"] == 1
    assert row["days_old"] >= 0
    assert row["status"] == "ok"


def test_scheduled_rebalance_date_is_not_selected(tmp_path, monkeypatch):
    """latest_rebalance names a scheduled decision session. It sits beside
    headline_equity_dates and is later than it; only the equity curve is an
    observation."""
    blob = {
        "headline": {
            "headline_equity_dates": ["2026-09-04", "2026-09-08"],
            "latest_rebalance": "2026-09-11",
            "weekly_allocation_dates": ["2026-09-08"],
        },
    }
    assert P._observation_date_from(
        blob, ["headline.headline_equity_dates.*"]) == "2026-09-08"


def test_fetch_timestamp_cannot_become_last_data(tmp_path, monkeypatch):
    """Structural half of the guard: a datetime stamp is rejected outright,
    not truncated to its date prefix, even if a selector is aimed at it.

    The 28 Europe breadth rows reported Sunday 2026-09-06 because that was
    their computed_at_utc; their real basis was Friday 2026-09-04.
    """
    blob = {"computed_at_utc": "2026-09-06T07:32:57.839417+00:00",
            "fetched_at_utc": "2026-09-09T07:32:04.585179+00:00",
            "end_date": "2026-09-04"}
    assert P._iso_observation_date("2026-09-06T07:32:57.839417+00:00") is None
    assert P._observation_date_from(blob, ["computed_at_utc"]) is None
    assert P._observation_date_from(blob, ["end_date"]) == "2026-09-04"


def test_weekend_observation_date_is_flagged(tmp_path, monkeypatch):
    """Secondary tripwire: every monitored feed prices off an exchange
    session, so a Sunday 'observation' means a compute clock was selected."""
    blob = {"end_date": "2026-09-06"}   # Sunday
    assert date.fromisoformat("2026-09-06").strftime("%A") == "Sunday"
    check = {"key": "b", "file": "b.json", "group": "breadth", "label": "B",
             "warn": 8, "stale": 14, "obs": ["end_date"]}
    health = _health_for(tmp_path, monkeypatch, {"b.json": blob}, [check], BUILD)
    row = _row(health, "b.json")

    assert row["status"] == "warn"
    assert "Sunday" in row["note"]


# ---------------------------------------------------------------------------
# 3. Future dates are broken, not OK
# ---------------------------------------------------------------------------

def test_future_observation_date_is_broken_not_ok(tmp_path, monkeypatch):
    blob = {"end_date": "2026-09-11"}
    check = {"key": "f", "file": "f.json", "group": "derived", "label": "F",
             "warn": 8, "stale": 14, "obs": ["end_date"]}
    health = _health_for(tmp_path, monkeypatch, {"f.json": blob}, [check], BUILD)
    row = _row(health, "f.json")

    assert row["days_old"] == -2
    assert row["status"] == "broken"
    assert health["overall_status"] == "stale"      # badge must not read OK
    assert health["counts"]["ok"] == 0
    assert "AFTER" in row["note"]


def test_named_forecast_feed_may_be_future_but_is_at_least_warn(
        tmp_path, monkeypatch):
    """A feed that legitimately looks forward must opt in by name, and even
    then does not get to read OK."""
    blob = {"end_date": "2026-09-11"}
    check = {"key": "f", "file": "f.json", "group": "derived", "label": "F",
             "warn": 8, "stale": 14, "obs": ["end_date"],
             "allow_future": "scheduled fill calendar"}
    health = _health_for(tmp_path, monkeypatch, {"f.json": blob}, [check], BUILD)
    row = _row(health, "f.json")

    assert row["status"] == "warn"
    assert row["status"] != "ok"
    assert "scheduled fill calendar" in row["note"]


@pytest.mark.parametrize("days_old", [-1, -2, -21, -365])
def test_classify_never_returns_ok_for_a_negative_age(days_old):
    """Backstop: no caller may derive OK from a date after the build."""
    assert P._classify(days_old, warn=8, stale=14) == "broken"


# ---------------------------------------------------------------------------
# 4. True observation dates still classify correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("last,expected", [
    ("2026-09-08", "ok"),      # 1d
    ("2026-09-02", "ok"),      # 7d, below warn
    ("2026-09-01", "warn"),    # 8d, at warn
    ("2026-08-26", "stale"),   # 14d, at stale
    ("2026-07-01", "stale"),
])
def test_true_observation_dates_classify_correctly(
        tmp_path, monkeypatch, last, expected):
    check = {"key": "t", "file": "t.json", "group": "strategy", "label": "T",
             "warn": 8, "stale": 14, "obs": ["end_date"]}
    health = _health_for(tmp_path, monkeypatch, {"t.json": {"end_date": last}},
                         [check], BUILD)
    row = _row(health, "t.json")
    assert row["last_data_date"] == last
    assert row["days_old"] == (BUILD - date.fromisoformat(last)).days
    assert row["status"] == expected


def test_missing_selector_is_broken_not_guessed(tmp_path, monkeypatch):
    """A feed added without naming its observation field must fail closed.
    Silently falling back to file mtime is what let a stale feed read fresh."""
    check = {"key": "n", "file": "n.json", "group": "derived", "label": "N",
             "warn": 8, "stale": 14}
    health = _health_for(tmp_path, monkeypatch,
                         {"n.json": {"end_date": "2026-09-08"}}, [check], BUILD)
    row = _row(health, "n.json")
    assert row["status"] == "broken"
    assert row["last_data_date"] is None
    assert "no observation-date selector" in row["note"]


def test_renamed_field_is_broken_not_mtime_fallback(tmp_path, monkeypatch):
    """The file is freshly written, so an mtime fallback would report it as
    0 days old and green while its contents are unverified."""
    check = {"key": "r", "file": "r.json", "group": "derived", "label": "R",
             "warn": 8, "stale": 14, "obs": ["end_date"]}
    health = _health_for(tmp_path, monkeypatch,
                         {"r.json": {"final_date": "2026-09-08"}}, [check], BUILD)
    row = _row(health, "r.json")
    assert row["status"] == "broken"
    assert row["days_old"] is None
    assert "not found at end_date" in row["note"]


# ---------------------------------------------------------------------------
# 5. Boundary cases — month and year, computed with a date library
# ---------------------------------------------------------------------------
# Python's datetime months are 1-indexed (January == 1), unlike JavaScript's
# 0-indexed months. All arithmetic below goes through datetime/dateutil; no
# day offsets are computed by hand.

def test_month_boundary_age_spans_a_short_month(tmp_path, monkeypatch):
    """Build on Monday 2 March, observation on the last session of February
    (Friday the 27th). The span must come from the calendar, not from an
    assumed 30-day month. 2026 is not a leap year: February ends on the 28th,
    which is a Saturday, so the last session is the 27th."""
    build = date(2026, 3, 2)
    assert build.strftime("%A") == "Monday"
    month_end = build - relativedelta(days=1) - relativedelta(days=1)
    assert month_end == date(2026, 2, 28)
    assert month_end.month == 2                 # 1-indexed: February
    last = date(2026, 2, 27)                    # last session of the month
    assert last.strftime("%A") == "Friday"
    expected_days = (build - last).days
    assert expected_days == 3

    check = {"key": "m", "file": "m.json", "group": "strategy", "label": "M",
             "warn": 8, "stale": 14, "obs": ["end_date"]}
    health = _health_for(tmp_path, monkeypatch,
                         {"m.json": {"end_date": last.isoformat()}},
                         [check], build)
    row = _row(health, "m.json")
    assert row["last_data_date"] == "2026-02-27"
    assert row["days_old"] == expected_days
    assert row["status"] == "ok"

    # And a month-END label for the month in progress must still break: on
    # 2 March, a feed reporting 2026-03-31 is 29 days ahead of the build.
    ahead = build + relativedelta(day=31)       # last day of March
    assert ahead == date(2026, 3, 31)
    health2 = _health_for(tmp_path, monkeypatch,
                          {"m.json": {"end_date": ahead.isoformat()}},
                          [check], build)
    row2 = _row(health2, "m.json")
    assert row2["days_old"] == (build - ahead).days == -29
    assert row2["status"] == "broken"


def test_year_boundary_age_spans_new_year(tmp_path, monkeypatch):
    """Build on Monday 4 January, observation on Thursday 31 December of the
    prior year — both exchange sessions, spanning the year boundary."""
    build = date(2027, 1, 4)
    assert build.strftime("%A") == "Monday"
    last = date(2026, 12, 31)
    assert last.strftime("%A") == "Thursday"
    expected_days = (build - last).days
    assert expected_days == 4
    assert (build - relativedelta(years=1)).year == 2026

    check = {"key": "y", "file": "y.json", "group": "strategy", "label": "Y",
             "warn": 8, "stale": 14, "obs": ["end_date"]}
    health = _health_for(tmp_path, monkeypatch,
                         {"y.json": {"end_date": last.isoformat()}},
                         [check], build)
    row = _row(health, "y.json")
    assert row["last_data_date"] == "2026-12-31"
    assert row["days_old"] == expected_days
    assert row["status"] == "ok"

    # A next-year label on a 2 January build is a future date, not fresh data.
    forward = date(2027, 12, 31)
    health2 = _health_for(tmp_path, monkeypatch,
                          {"y.json": {"end_date": forward.isoformat()}},
                          [check], build)
    row2 = _row(health2, "y.json")
    assert row2["days_old"] < 0
    assert row2["status"] == "broken"


# ---------------------------------------------------------------------------
# 6. Registry contract
# ---------------------------------------------------------------------------

def test_every_registered_check_declares_an_observation_selector():
    """The registry is the contract. A new feed without ``obs`` would be
    reported broken at build time; catch it here instead."""
    missing = [c["key"] for c in P.DATA_HEALTH_CHECKS if not c.get("obs")]
    assert missing == [], f"checks without an obs selector: {missing}"


def test_no_registered_selector_points_at_a_known_non_observation_field():
    """Named blocklist of the four date kinds that caused the bug."""
    banned = (
        "computed_at_utc", "fetched_at_utc", "checked_at_utc",
        "latest_rebalance", "common_monthly_window", "breadth_dates",
        "next_fill", "anchor_date",
    )
    for check in P.DATA_HEALTH_CHECKS:
        for sel in check.get("obs", []):
            for seg in sel.split("."):
                assert seg not in banned, (
                    f"{check['key']} selects {seg!r}, which is a fetch clock, "
                    f"period label or scheduled date — not an observation"
                )


def test_live_health_report_has_no_future_or_weekend_dates():
    """End-to-end over the real data/ directory: the shipped report must
    contain no negative age and no weekend observation date.

    Skips when data/ is not populated (a clean checkout in CI).
    """
    if not (P.DATA_DIR / "multi_strategy.json").exists():
        pytest.skip("data/ not populated")
    health = P._compute_data_health(date.today())
    future = [(r["file"], r["last_data_date"], r["days_old"])
              for r in health["rows"]
              if r["days_old"] is not None and r["days_old"] < 0]
    assert future == [], f"future-dated health rows: {future}"

    weekend = [(r["file"], r["last_data_date"])
               for r in health["rows"]
               if r["last_data_date"]
               and date.fromisoformat(r["last_data_date"]).weekday() >= 5]
    assert weekend == [], f"weekend observation dates: {weekend}"
