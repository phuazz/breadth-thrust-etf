"""Tests for scripts/check_coverage_depth.py -- the coverage-depth guard.

REPRODUCES 2026-09-02. The post-fill refresh ran from the automation clone,
whose gitignored price caches had never received the WS11 / WS16 Norgate
delisted-archive backfills, and rebuilt all fifteen US panels on the
survivor basis. 2018 coverage fell on every one (SOXX 0.9997 -> 0.8193,
IUCM 0.9978 -> 0.5385), sleeve A's Sharpe rose 0.9196 -> 0.9623, and the
refresh guard, the price-panel guard and 1,752 tests all passed. The
incident table below is the measured regression, pinned so that the
tolerance has to argue with the data rather than with a comment.

Three layers:
  1. the pure verdict logic on synthetic panels and caches;
  2. calibration -- both bounds of the tolerance against the measured
     same-basis drift and the measured regression, and the committed
     baseline's provenance, scope and filed depth (so it cannot be quietly
     regenerated from a survivor tree);
  3. the committed panels themselves, for panels built after the baseline
     was adopted.

Calendar facts below come from pandas.bdate_range, never from memory.
Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_coverage_depth as g  # noqa: E402

DATA = ROOT / "data"
BASELINE = DATA / "coverage_baseline.json"

BASIS = {"ref": "abc1234", "commit": "a" * 40,
         "committed_at": "2026-08-31 17:26:36 +0800", "subject": "test basis"}
ADOPTED = datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _series(dates: pd.DatetimeIndex, priced, roster) -> dict:
    priced = list(priced) if hasattr(priced, "__iter__") else [priced] * len(dates)
    roster = list(roster) if hasattr(roster, "__iter__") else [roster] * len(dates)
    return {"dates": [d.strftime("%Y-%m-%d") for d in dates],
            "n_with_price": priced, "n_constituents": roster}


def _panel(dates: pd.DatetimeIndex, priced, roster,
           computed_at: str = "2026-09-05T02:00:00+00:00") -> dict:
    return {"computed_at_utc": computed_at,
            "start_date": dates[0].strftime("%Y-%m-%d"),
            "end_date": dates[-1].strftime("%Y-%m-%d"),
            "n_trading_days": len(dates), "trading_calendar": "NYSE",
            "series": _series(dates, priced, roster)}


def _entry(panel: dict) -> dict:
    """The baseline entry build_baseline would write for ``panel``."""
    return g.build_baseline({"SOXX": panel}, BASIS, "why", now=ADOPTED)["panels"]["SOXX"]


TWO_YEARS = pd.bdate_range("2018-01-02", "2019-12-31")   # 521 sessions


# ---------------------------------------------------------------------------
# 1. per_year_coverage
# ---------------------------------------------------------------------------
def test_coverage_is_the_ratio_of_sums_not_the_mean_of_ratios():
    dates = pd.bdate_range("2018-01-02", periods=2)
    out = g.per_year_coverage(_series(dates, [1, 100], [10, 100]))
    assert out["2018"]["n_with_price"] == 101
    assert out["2018"]["n_constituents"] == 110
    assert out["2018"]["sessions"] == 2
    assert out["2018"]["coverage"] == pytest.approx(101 / 110)
    assert out["2018"]["coverage"] != pytest.approx((0.1 + 1.0) / 2)


def test_window_is_inclusive_across_the_year_boundary():
    # bdate_range gives 2025-12-30, 2025-12-31, 2026-01-01, 2026-01-02; the
    # 1st is a weekday, so four sessions, two in each year.
    dates = pd.bdate_range("2025-12-30", "2026-01-02")
    assert len(dates) == 4
    out = g.per_year_coverage(_series(dates, 5, 10),
                              start="2025-12-31", end="2026-01-01")
    assert out["2025"]["sessions"] == 1
    assert out["2026"]["sessions"] == 1


def test_window_end_excludes_sessions_past_it_across_the_month_boundary():
    # 2026-01-28 (Wed) .. 2026-02-03 (Tue): five sessions, three in January.
    dates = pd.bdate_range("2026-01-28", "2026-02-03")
    assert len(dates) == 5
    out = g.per_year_coverage(_series(dates, 5, 10), end="2026-01-30")
    assert out["2026"]["sessions"] == 3


def test_no_bounds_means_every_session_counts():
    out = g.per_year_coverage(_series(TWO_YEARS, 5, 10))
    assert sum(v["sessions"] for v in out.values()) == len(TWO_YEARS)
    assert set(out) == {"2018", "2019"}


def test_mismatched_series_lengths_raise():
    dates = pd.bdate_range("2018-01-02", periods=3)
    bad = {"dates": [d.strftime("%Y-%m-%d") for d in dates],
           "n_with_price": [1, 2], "n_constituents": [3, 3, 3]}
    with pytest.raises(ValueError):
        g.per_year_coverage(bad)


def test_empty_roster_year_has_no_coverage_rather_than_dividing_by_zero():
    dates = pd.bdate_range("2018-01-02", periods=2)
    out = g.per_year_coverage(_series(dates, 0, 0))
    assert out["2018"]["coverage"] is None


# ---------------------------------------------------------------------------
# 1. compare_panel
# ---------------------------------------------------------------------------
def test_identical_panel_passes_every_year():
    panel = _panel(TWO_YEARS, 30, 30)
    rows = g.compare_panel("SOXX", _entry(panel), panel)
    assert [r["status"] for r in rows] == [g.OK, g.OK]
    assert [r["year"] for r in rows] == ["2018", "2019"]


def test_fall_beyond_tolerance_fails_and_names_the_year():
    base = _panel(TWO_YEARS, 100, 100)
    # 2018 loses 5 names of 100 all year; 2019 untouched.
    priced = [95 if d.year == 2018 else 100 for d in TWO_YEARS]
    rows = g.compare_panel("SOXX", _entry(base), _panel(TWO_YEARS, priced, 100))
    by_year = {r["year"]: r for r in rows}
    assert by_year["2018"]["status"] == g.FAIL
    assert "BELOW" in by_year["2018"]["evidence"]
    assert by_year["2018"]["current"] == pytest.approx(0.95)
    assert by_year["2019"]["status"] == g.OK


def test_fall_within_tolerance_passes():
    base = _panel(TWO_YEARS, 1000, 1000)
    priced = [995 if d.year == 2018 else 1000 for d in TWO_YEARS]   # -0.005
    rows = g.compare_panel("SOXX", _entry(base), _panel(TWO_YEARS, priced, 1000))
    assert all(r["status"] == g.OK for r in rows)


def test_fall_of_exactly_the_tolerance_is_inside_the_band():
    """0.99 - 1.0 is not representable exactly; the band must be inclusive
    in decimal, not in binary."""
    base = _panel(TWO_YEARS, 100, 100)
    priced = [99] * len(TWO_YEARS)                                   # -0.01
    rows = g.compare_panel("SOXX", _entry(base), _panel(TWO_YEARS, priced, 100),
                           tolerance=0.01)
    assert all(r["status"] == g.OK for r in rows)


def test_one_roster_day_past_the_tolerance_fails():
    """One name short on every 2018 session is exactly 1% of the year's
    roster-days (inside the band); one more roster-day is outside it."""
    base = _panel(TWO_YEARS, 100, 100)
    priced = [99 if d.year == 2018 else 100 for d in TWO_YEARS]
    priced[0] = 98
    rows = g.compare_panel("SOXX", _entry(base), _panel(TWO_YEARS, priced, 100),
                           tolerance=0.01)
    assert {r["year"]: r["status"] for r in rows}["2018"] == g.FAIL


def test_rise_beyond_tolerance_warns_not_fails():
    base = _panel(TWO_YEARS, 80, 100)
    rows = g.compare_panel("SOXX", _entry(base), _panel(TWO_YEARS, 100, 100))
    assert all(r["status"] == g.WARN for r in rows)
    assert all("re-baseline" in r["evidence"] for r in rows)


def test_missing_panel_is_a_single_fail():
    rows = g.compare_panel("SOXX", _entry(_panel(TWO_YEARS, 30, 30)), None)
    assert len(rows) == 1
    assert rows[0]["status"] == g.FAIL
    assert rows[0]["year"] is None


def test_vanished_baseline_year_fails():
    base = _panel(TWO_YEARS, 30, 30)
    only_2019 = pd.bdate_range("2019-01-02", "2019-12-31")
    rows = g.compare_panel("SOXX", _entry(base), _panel(only_2019, 30, 30))
    by_year = {r["year"]: r for r in rows}
    assert by_year["2018"]["status"] == g.FAIL
    assert "vanished" in by_year["2018"]["evidence"]
    assert by_year["2019"]["status"] == g.OK


def test_unreadable_series_fails_rather_than_crashing():
    base = _panel(TWO_YEARS, 30, 30)
    broken = {"series": {"dates": ["2018-01-02"], "n_with_price": [],
                         "n_constituents": [1]}}
    rows = g.compare_panel("SOXX", _entry(base), broken)
    assert rows[0]["status"] == g.FAIL
    assert "unreadable" in rows[0]["evidence"]


# --- like for like: the open year is compared on the sessions both hold ----
def test_sessions_beyond_the_baseline_end_are_not_compared_month_boundary():
    """Baseline ends Friday 2026-01-30; the new panel runs into February with
    NOTHING priced there. February is the tail, not a regression."""
    base_dates = pd.bdate_range("2025-01-02", "2026-01-30")
    base = _panel(base_dates, 30, 30)
    new_dates = pd.bdate_range("2025-01-02", "2026-02-27")
    priced = [30 if d <= pd.Timestamp("2026-01-30") else 0 for d in new_dates]
    rows = g.compare_panel("SOXX", _entry(base), _panel(new_dates, priced, 30))
    assert {r["year"]: r["status"] for r in rows} == {"2025": g.OK, "2026": g.OK}


def test_sessions_beyond_the_baseline_end_are_not_compared_year_boundary():
    """Baseline ends 2025-12-31; the new panel carries a 2026 with nothing
    priced. 2026 is not a baseline year, so nothing is compared there, and
    2025 must be untouched."""
    base = _panel(pd.bdate_range("2025-01-02", "2025-12-31"), 30, 30)
    new_dates = pd.bdate_range("2025-01-02", "2026-01-30")
    priced = [30 if d.year == 2025 else 0 for d in new_dates]
    rows = g.compare_panel("SOXX", _entry(base), _panel(new_dates, priced, 30))
    assert [r["year"] for r in rows] == ["2025"]
    assert rows[0]["status"] == g.OK


def test_a_regression_inside_the_open_year_window_is_still_caught():
    """The window protects the tail, not the body: sessions the baseline
    holds are compared even in the open year."""
    base_dates = pd.bdate_range("2025-01-02", "2026-01-30")
    base = _panel(base_dates, 100, 100)
    priced = [80 if d.year == 2026 else 100 for d in base_dates]
    rows = g.compare_panel("SOXX", _entry(base), _panel(base_dates, priced, 100))
    assert {r["year"]: r["status"] for r in rows} == {"2025": g.OK, "2026": g.FAIL}


# ---------------------------------------------------------------------------
# 1. probes
# ---------------------------------------------------------------------------
def _cache(tmp_path: Path) -> Path:
    idx = pd.bdate_range("2022-02-09", periods=3)
    frame = pd.DataFrame({"XLNX": [1.0, 2.0, np.nan],
                          "MXIM": [np.nan, np.nan, np.nan],
                          "AMD": [10.0, 11.0, 12.0]}, index=idx)
    path = tmp_path / "prices_cache_soxx.parquet"
    frame.to_parquet(path)
    return path


def test_probe_carrying_prices_is_ok_with_its_span(tmp_path):
    frame = g.load_probe_columns(_cache(tmp_path), ("XLNX",))
    (r,) = g.probe_frame("SOXX", frame, ("XLNX",))
    assert r["status"] == g.OK
    assert "2 obs" in r["evidence"]
    assert "2022-02-09..2022-02-10" in r["evidence"]


def test_empty_probe_column_fails_and_says_survivor(tmp_path):
    frame = g.load_probe_columns(_cache(tmp_path), ("MXIM",))
    (r,) = g.probe_frame("SOXX", frame, ("MXIM",))
    assert r["status"] == g.FAIL
    assert "EMPTY" in r["evidence"]
    assert "survivor" in r["evidence"]


def test_absent_probe_column_fails(tmp_path):
    frame = g.load_probe_columns(_cache(tmp_path), ("ZZZZ",))
    (r,) = g.probe_frame("SOXX", frame, ("ZZZZ",))
    assert r["status"] == g.FAIL
    assert "not a column" in r["evidence"]


def test_absent_cache_skips_rather_than_fails(tmp_path):
    frame = g.load_probe_columns(tmp_path / "prices_cache_soxx.parquet", ("XLNX",))
    assert frame is None
    (r,) = g.probe_frame("SOXX", frame, ("XLNX", "MXIM"))
    assert r["status"] == g.SKIP
    assert "absent" in r["evidence"]


def test_load_probe_columns_reads_only_what_is_there(tmp_path):
    frame = g.load_probe_columns(_cache(tmp_path), ("XLNX", "MXIM", "ZZZZ"))
    assert list(frame.columns) == ["XLNX", "MXIM"]
    assert isinstance(frame.index, pd.DatetimeIndex)


def test_probe_table_names_the_incident_names():
    """The probes are the names WS11 / WS16 were measured on; if the table
    changes, the record that cites it should change too."""
    assert g.PROBES["SOXX"] == ("XLNX", "MXIM")
    assert g.PROBES["CSP1"] == ("SIVB", "FRC")
    assert g.PROBES["IUFS"] == ("SIVB", "FRC")
    assert g.PROBES["IUCM"] == ("TWTR", "ATVI")
    assert set(g.PROBES) <= set(g.us_panels())


# ---------------------------------------------------------------------------
# 1. scope and orchestration
# ---------------------------------------------------------------------------
US_PANELS_2026_09_02 = ["SOXX", "CSP1", "CNDX", "IUES", "IUFS", "IUIT", "IUHC",
                        "IUIS", "IUCS", "IUCD", "IUUS", "IUMS", "IUCM", "IUSP",
                        "IDP6"]


def test_us_panels_is_the_no_suffix_rule():
    assert g.us_panels(["SOXX", "EXV1", "IJPN", "IUIT", "ITWN"]) == ["SOXX", "IUIT"]


def test_us_panels_from_the_deployed_set_is_the_pinned_fifteen():
    """Fifteen, not fourteen: IUIT is out of sleeve A's universe but still
    deployed and regressed identically. Registry drift lands here by name."""
    assert g.us_panels() == US_PANELS_2026_09_02


def _write_state(tmp_path: Path, panels: dict[str, dict]) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    for etf, doc in panels.items():
        (data / f"breadth_{etf.lower()}.json").write_text(json.dumps(doc),
                                                           encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    g.write_baseline(g.build_baseline(panels, BASIS, "why", now=ADOPTED), baseline)
    return data, baseline


def test_main_all_clear_exits_zero(tmp_path):
    data, baseline = _write_state(tmp_path, {"SOXX": _panel(TWO_YEARS, 30, 30)})
    rc = g.main(["--baseline", str(baseline), "--data-dir", str(data),
                 "--no-probes"])
    assert rc == 0


def test_main_regression_exits_one(tmp_path):
    data, baseline = _write_state(tmp_path, {"SOXX": _panel(TWO_YEARS, 30, 30)})
    regressed = _panel(TWO_YEARS, [25 if d.year == 2018 else 30 for d in TWO_YEARS], 30)
    (data / "breadth_soxx.json").write_text(json.dumps(regressed), encoding="utf-8")
    rc = g.main(["--baseline", str(baseline), "--data-dir", str(data),
                 "--no-probes"])
    assert rc == 1


def test_main_without_a_baseline_exits_two(tmp_path):
    rc = g.main(["--baseline", str(tmp_path / "missing.json"),
                 "--data-dir", str(tmp_path)])
    assert rc == 2


def test_main_runs_probes_from_the_cache_dir(tmp_path):
    data, baseline = _write_state(tmp_path, {"SOXX": _panel(TWO_YEARS, 30, 30)})
    caches = tmp_path / "caches"
    caches.mkdir()
    _cache(caches)                       # XLNX priced, MXIM empty -> FAIL
    rc = g.main(["--baseline", str(baseline), "--data-dir", str(data),
                 "--cache-dir", str(caches)])
    assert rc == 1
    rc = g.main(["--baseline", str(baseline), "--data-dir", str(data),
                 "--cache-dir", str(caches), "--no-probes"])
    assert rc == 0


def test_write_baseline_needs_ref_and_why(tmp_path):
    target = tmp_path / "b.json"
    assert g.main(["--write-baseline", "--baseline", str(target)]) == 2
    assert g.main(["--write-baseline", "--baseline", str(target),
                   "--ref", "HEAD"]) == 2
    assert not target.exists()


def test_write_baseline_refuses_to_overwrite_without_force(tmp_path):
    target = tmp_path / "b.json"
    target.write_text("{}", encoding="utf-8")
    rc = g.main(["--write-baseline", "--baseline", str(target),
                 "--ref", "HEAD", "--why", "test"])
    assert rc == 2
    assert target.read_text(encoding="utf-8") == "{}"


def test_build_baseline_records_provenance_and_rounds_coverage():
    doc = g.build_baseline({"SOXX": _panel(TWO_YEARS, 7442, 7444)}, BASIS,
                           "because", now=ADOPTED)
    assert doc["schema_version"] == g.BASELINE_SCHEMA
    assert doc["adopted_at_utc"] == ADOPTED.isoformat()
    assert doc["basis"]["commit"] == "a" * 40
    assert doc["basis"]["why"] == "because"
    assert "--ref abc1234" in doc["generated_by"]
    year = doc["panels"]["SOXX"]["years"]["2018"]
    assert year["coverage"] == round(7442 / 7444, 6)
    assert year["n_with_price"] == 7442 * sum(1 for d in TWO_YEARS if d.year == 2018)


def test_refresh_all_runs_the_guard_as_a_verify_step():
    """A guard that is not wired is not a guard."""
    src = (ROOT / "scripts" / "refresh_all.py").read_text(encoding="utf-8")
    assert "scripts/check_coverage_depth.py" in src
    # The list closes with a bracket on its own line at four spaces; the
    # inner command lists close inline.
    verify = src.split("verify_steps = [", 1)[1].split("\n    ]", 1)[0]
    assert "check_coverage_depth.py" in verify
    assert verify.index("check_engine_price_panels.py") < verify.index(
        "check_coverage_depth.py")


# ---------------------------------------------------------------------------
# 2. Calibration -- measured, not invented
# ---------------------------------------------------------------------------
# 2018 coverage per panel at 670ca1c (the filed basis) and at 62292ed (the
# survivor-basis rebuild), measured 2026-09-02 with sum(n_with_price) /
# sum(n_constituents) over the year's sessions.
INCIDENT_2018 = [
    ("SOXX", 0.9997, 0.8193),
    ("CSP1", 0.9998, 0.8249),
    ("CNDX", 1.0000, 0.8191),
    ("IUES", 1.0000, 0.6282),
    ("IUFS", 0.9999, 0.8242),
    ("IUIT", 0.9996, 0.8230),
    ("IUHC", 0.9999, 0.8071),
    ("IUIS", 0.9998, 0.8437),
    ("IUCS", 1.0000, 0.9244),
    ("IUCD", 0.9998, 0.8187),
    ("IUUS", 1.0000, 0.9650),    # one delisted name in a 31-name panel
    ("IUMS", 0.9995, 0.7845),
    ("IUCM", 0.9978, 0.5385),
    ("IUSP", 0.9912, 0.6589),
    ("IDP6", 0.8349, 0.6230),
]

# Worst drift of a panel-year across the five same-basis rebuilds between
# 2026-08-15 and 2026-08-31 (1237546, faf9a22, 3718550, 43a21d1, 670ca1c):
# closed years identical to four decimals on all fifteen panels; the open
# year moved at most this much (IUCS 2026, 0.9979 -> 1.0000) as the tail
# filled in.
SAME_BASIS_DRIFT_CLOSED_YEARS = 0.0000
SAME_BASIS_DRIFT_OPEN_YEAR = 0.0021


def test_tolerance_sits_between_healthy_drift_and_the_smallest_regression():
    smallest_fall = min(b - r for _, b, r in INCIDENT_2018)
    assert smallest_fall == pytest.approx(0.0350, abs=1e-4)
    assert SAME_BASIS_DRIFT_OPEN_YEAR < g.COVERAGE_TOLERANCE < smallest_fall
    # Margin on both sides, so a small retune does not land on either bound.
    assert g.COVERAGE_TOLERANCE >= 3 * SAME_BASIS_DRIFT_OPEN_YEAR
    assert g.COVERAGE_TOLERANCE <= smallest_fall / 3


@pytest.mark.parametrize("etf,filed,regressed", INCIDENT_2018)
def test_the_incident_would_have_been_caught_on_every_panel(etf, filed, regressed):
    """THE POINT. Each panel's 2018 fall, replayed through compare_panel at
    the deployed tolerance, fails."""
    dates = pd.bdate_range("2018-01-02", "2018-12-31")
    roster = 10000
    base = _panel(dates, int(round(filed * roster)), roster)
    now = _panel(dates, int(round(regressed * roster)), roster)
    rows = g.compare_panel(etf, _entry(base), now)
    assert rows[0]["status"] == g.FAIL, f"{etf}: {rows[0]['evidence']}"


def test_healthy_open_year_drift_does_not_fire():
    dates = pd.bdate_range("2026-01-02", "2026-08-28")
    roster = 10000
    base = _panel(dates, roster, roster)
    now = _panel(dates, int(round((1 - SAME_BASIS_DRIFT_OPEN_YEAR) * roster)), roster)
    rows = g.compare_panel("IUCS", _entry(base), now)
    assert rows[0]["status"] == g.OK


# ---------------------------------------------------------------------------
# 2. The committed baseline
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def committed_baseline() -> dict:
    if not BASELINE.exists():
        pytest.skip("data/coverage_baseline.json not present")
    return g.load_baseline(BASELINE)


def test_committed_baseline_carries_its_provenance(committed_baseline):
    b = committed_baseline["basis"]
    assert re.fullmatch(r"[0-9a-f]{40}", b["commit"])
    assert b["commit"].startswith(b["ref"])
    assert b["subject"]
    assert len(b["why"]) > 100, "the reason for the basis must be stated"
    assert "section 13" in b["why"]
    # Dates parse with a date library; nothing is computed from them here.
    datetime.fromisoformat(committed_baseline["adopted_at_utc"])
    datetime.strptime(b["committed_at"], "%Y-%m-%d %H:%M:%S %z")
    assert committed_baseline["generated_by"].startswith(
        "scripts/check_coverage_depth.py --write-baseline")


def test_committed_baseline_scope_is_exactly_the_us_panels(committed_baseline):
    """Both directions: a US panel missing from the baseline is unguarded;
    a non-US panel in it would be held to a basis it never had."""
    assert set(committed_baseline["panels"]) == set(g.us_panels())


def test_committed_baseline_values_are_internally_consistent(committed_baseline):
    for etf, entry in committed_baseline["panels"].items():
        years = entry["years"]
        assert years, etf
        first, last = int(entry["start_date"][:4]), int(entry["end_date"][:4])
        assert sorted(int(y) for y in years) == list(range(first, last + 1)), etf
        for y, v in years.items():
            assert v["sessions"] > 0, (etf, y)
            assert 0 < v["n_with_price"] <= v["n_constituents"], (etf, y)
            assert v["coverage"] == round(v["n_with_price"] / v["n_constituents"], 6)


def test_committed_baseline_is_the_filed_basis_not_the_survivor_one(committed_baseline):
    """The guard on the guard. A baseline regenerated from a survivor tree
    would read SOXX 2018 at 0.8193 and IUCM at 0.5385; the filed basis reads
    0.9997 and 0.9978. If these move, someone re-baselined -- which is a
    sign-off act, and the sign-off should update this test with it."""
    years = {etf: committed_baseline["panels"][etf]["years"]["2018"]["coverage"]
             for etf in ("SOXX", "IUCM", "IUES", "IDP6")}
    assert years["SOXX"] == pytest.approx(0.9997, abs=1e-4)
    assert years["IUCM"] == pytest.approx(0.9978, abs=1e-4)
    assert years["IUES"] == pytest.approx(1.0000, abs=1e-4)
    assert years["IDP6"] == pytest.approx(0.8349, abs=1e-4)
    assert committed_baseline["basis"]["ref"] == "670ca1c"


# ---------------------------------------------------------------------------
# 3. The committed panels, for those built after the baseline was adopted
# ---------------------------------------------------------------------------
def test_committed_us_panels_hold_the_filed_coverage_depth(committed_baseline):
    """THE GUARD, on the committed state.

    A guard governs what is built after it lands. Panels whose
    computed_at_utc predates the baseline's adoption are the pre-guard
    state -- on 2026-09-02 that is the 62292ed survivor-basis book, which
    stands until the owner reverts or re-runs it -- and are reported, not
    judged here; refresh_all's VERIFY step judges every panel it rebuilds.
    Once a refresh lands, every rebuilt panel is in scope, and a rebuild that
    lost the delisted names fails this test in CI as well as in the refresh.
    """
    adopted = datetime.fromisoformat(committed_baseline["adopted_at_utc"])
    in_scope: dict[str, dict | None] = {}
    predate: list[str] = []
    for etf in committed_baseline["panels"]:
        doc = g.panel_from_disk(etf, DATA)
        if doc is None:
            in_scope[etf] = None              # missing panel: judged, fails
            continue
        stamp = doc.get("computed_at_utc")
        try:
            built = datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            built = None
        if built is not None and built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        if built is not None and built < adopted:
            predate.append(f"{etf} {built:%Y-%m-%dT%H:%MZ}")
        else:
            in_scope[etf] = doc
    if not in_scope:
        pytest.skip(
            f"all {len(predate)} US panels predate the baseline's adoption "
            f"({adopted:%Y-%m-%d %H:%MZ}): the committed book is the "
            f"pre-guard state (WS19 section 13); the guard governs what is "
            f"built after it")

    failures = []
    for etf, doc in in_scope.items():
        base = committed_baseline["panels"][etf]
        failures += [r for r in g.compare_panel(etf, base, doc)
                     if r["status"] == g.FAIL]
        names = g.PROBES.get(etf)
        if names:
            frame = g.load_probe_columns(
                DATA / f"prices_cache_{etf.lower()}.parquet", names)
            failures += [r for r in g.probe_frame(etf, frame, names)
                         if r["status"] == g.FAIL]
    assert not failures, (
        f"{len(failures)} panel-year(s) below the filed basis "
        f"(baseline {committed_baseline['basis']['ref']}):\n  "
        + "\n  ".join(f"{r['panel']} {r['year'] or ''} {r['evidence']}"
                      for r in failures[:12]))
