"""Vendor revision evidence: what it can see, and what it must refuse to say.

The first version of this module measured the CACHE after the
cell-preservation merge had already refilled whatever the vendor withdrew,
so the one category that bears on the append-versus-revise question could
not fire. These tests pin the ordering, the categories and - as much as
anything - the refusal to read zero cycles as reassurance.

Python datetime months are 1-indexed (January = 1).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import price_revisions as pr            # noqa: E402

RUN = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)
IDX = pd.to_datetime(["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"])


def _frame(values: dict, index=IDX) -> pd.DataFrame:
    return pd.DataFrame(values, index=index)


def _observe(df, root, *, panel="exv1", now=RUN, **kw):
    return pr.record_vendor_observation(df, panel=panel, root=root, now_utc=now,
                                        **kw)


# ---------------------------------------------------------------------------
# 1. The ordering defect: preservation masks a raw withdrawal
# ---------------------------------------------------------------------------
def test_preservation_masks_a_withdrawal_that_the_raw_observation_catches(tmp_path):
    """THE REPRODUCED DEFECT, both halves in one test.

    ``download_prices`` refills a cell the vendor withdrew from the cache's
    own previous value before the cache is written. A diff taken at the
    write therefore sees no change at all, while the raw frame shows the
    withdrawal plainly.
    """
    prior = _frame({"HSBA.L": [10.0, 11.0, 12.0, np.nan]})
    raw = _frame({"HSBA.L": [10.0, 11.0, np.nan, np.nan]})   # 14 Sep withdrawn
    merged = raw.copy()                                       # the preservation
    fill = merged["HSBA.L"].isna() & prior["HSBA.L"].notna()
    merged.loc[fill, "HSBA.L"] = prior.loc[fill, "HSBA.L"]

    sidecar = {"source": "yfinance", "columns_from_norgate": []}
    at_write = pr.diff_frames(prior, merged, old_sidecar=sidecar,
                              new_sidecar=sidecar)
    assert at_write["withdrawals"] == 0, "fixture no longer reproduces the mask"

    # The raw observation, taken before any of that, does see it.
    _observe(_frame({"HSBA.L": [10.0, 11.0, 12.0, np.nan]}), tmp_path,
             now=RUN - timedelta(days=1))
    out = _observe(raw, tmp_path)
    assert out["counters"]["withdrawals"] == 1
    events = [json.loads(ln) for ln in
              pr.events_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in events] == ["withdrawal"]
    assert events[0]["date"] == "2026-09-14" and events[0]["old"] == 12.0


# ---------------------------------------------------------------------------
# 2. Complete cycles, and comparing across them
# ---------------------------------------------------------------------------
def _cycle(tmp_path, restored_value):
    served = _frame({"SAN.MC": [10.0, 11.0, 12.0, np.nan]})
    withheld = _frame({"SAN.MC": [10.0, 11.0, np.nan, np.nan]})
    restored = _frame({"SAN.MC": [10.0, 11.0, restored_value, np.nan]})
    for i, f in enumerate((served, withheld, restored)):
        _observe(f, tmp_path, now=RUN + timedelta(days=i))
    return pr.cycle_summary(tmp_path)


def test_a_complete_cycle_that_restores_the_same_value(tmp_path):
    s = _cycle(tmp_path, 12.0)
    t = s["totals"]
    assert (t["withdrawals"], t["restorations"], t["restorations_identical"],
            t["restorations_changed"]) == (1, 1, 1, 0)
    assert s["verdict"].startswith("BOUNDED, NOT PROVEN")
    assert "does not establish that it never" in s["verdict"]


def test_a_complete_cycle_that_restores_a_DIFFERENT_value(tmp_path):
    s = _cycle(tmp_path, 12.5)
    t = s["totals"]
    assert (t["restorations"], t["restorations_changed"]) == (1, 1)
    assert s["verdict"].startswith("REVISION OBSERVED")


def test_zero_cycles_is_never_read_as_append_only(tmp_path):
    _observe(_frame({"SAN.MC": [10.0, 11.0, 12.0, np.nan]}), tmp_path)
    _observe(_frame({"SAN.MC": [10.0, 11.0, np.nan, np.nan]}), tmp_path,
             now=RUN + timedelta(days=1))
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["withdrawals"] == 1
    assert s["totals"]["restorations"] == 0
    assert s["verdict"].startswith("INSUFFICIENT EVIDENCE")
    assert "not evidence of append-only" in s["verdict"]
    assert s["missing_evidence"]["withdrawals_still_open"] == 1


def test_no_observations_at_all_is_stated_as_no_evidence(tmp_path):
    s = pr.cycle_summary(tmp_path)
    assert s["verdict"].startswith("NO EVIDENCE")
    assert s["totals"]["runs"] == 0


def test_a_populated_history_cell_that_changes_is_a_revision(tmp_path):
    _observe(_frame({"BNP.PA": [10.0, 11.0, 12.0, np.nan]}), tmp_path)
    _observe(_frame({"BNP.PA": [10.0, 11.5, 12.0, np.nan]}), tmp_path,
             now=RUN + timedelta(days=1))
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["revisions"] == 1
    assert s["verdict"].startswith("REVISION OBSERVED")


# ---------------------------------------------------------------------------
# 3. Unanswered requests are not missing bars
# ---------------------------------------------------------------------------
def test_an_empty_frame_is_an_unanswered_run_not_a_mass_withdrawal(tmp_path):
    _observe(_frame({"DBK.DE": [10.0, 11.0, 12.0, np.nan]}), tmp_path)
    out = _observe(pd.DataFrame(), tmp_path, now=RUN + timedelta(days=1))
    assert out["answered"] is False
    assert out["counters"]["withdrawals"] == 0
    s = pr.cycle_summary(tmp_path)
    assert s["missing_evidence"]["runs_the_vendor_did_not_answer"] == 1


def test_an_all_nan_column_is_an_unanswered_ticker(tmp_path):
    _observe(_frame({"INGA.AS": [10.0, 11.0, 12.0, np.nan],
                     "DEAD.L": [1.0, 1.0, 1.0, np.nan]}), tmp_path)
    out = _observe(_frame({"INGA.AS": [10.0, 11.0, 12.0, np.nan],
                           "DEAD.L": [np.nan] * 4}), tmp_path,
                   now=RUN + timedelta(days=1))
    assert out["counters"]["withdrawals"] == 0, \
        "a ticker the vendor did not answer for is not three withdrawals"
    state = json.loads(pr.state_path(tmp_path, "exv1").read_text(encoding="utf-8"))
    assert state["cells"]["2026-09-11|DEAD.L"]["status"] == "served"


def test_an_explicit_missing_bar_on_an_answering_column_is_a_withdrawal(tmp_path):
    _observe(_frame({"A.L": [10.0, 11.0, 12.0, np.nan],
                     "B.L": [1.0, 2.0, 3.0, np.nan]}), tmp_path)
    out = _observe(_frame({"A.L": [10.0, 11.0, np.nan, np.nan],
                           "B.L": [1.0, 2.0, 3.0, np.nan]}), tmp_path,
                   now=RUN + timedelta(days=1))
    assert out["counters"]["withdrawals"] == 1


# ---------------------------------------------------------------------------
# 4. Unfinished sessions, and date boundaries
# ---------------------------------------------------------------------------
def test_the_current_session_is_never_admitted(tmp_path):
    # RUN is 01:00 UTC on 16 September; the 15th is finished, the 16th is not.
    idx = pd.to_datetime(["2026-09-14", "2026-09-15", "2026-09-16"])
    dates = pr.admitted_dates(idx, RUN)
    assert dates == ["2026-09-14", "2026-09-15"]


def test_required_through_bounds_the_window_further():
    idx = pd.to_datetime(["2026-09-11", "2026-09-14", "2026-09-15"])
    assert pr.admitted_dates(idx, RUN, required_through="2026-09-14") == [
        "2026-09-11", "2026-09-14"]


def test_month_boundary_window():
    idx = pd.bdate_range("2026-09-25", "2026-10-02")
    now = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
    dates = pr.admitted_dates(idx, now, window=4)
    assert dates == ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]


def test_year_boundary_window():
    idx = pd.bdate_range("2026-12-28", "2027-01-04")
    now = datetime(2027, 1, 4, 1, 0, tzinfo=timezone.utc)
    dates = pr.admitted_dates(idx, now, window=3)
    assert dates == ["2026-12-30", "2026-12-31", "2027-01-01"]


def test_a_run_with_no_finished_session_admits_nothing(tmp_path):
    idx = pd.to_datetime(["2026-09-16"])
    out = _observe(pd.DataFrame({"A.L": [1.0]}, index=idx), tmp_path)
    assert out["answered"] is False
    assert out["dates"] == []


# ---------------------------------------------------------------------------
# 5. Provenance: "auto" is not a vendor
# ---------------------------------------------------------------------------
def test_auto_resolves_to_the_incumbent_vendor_not_to_auto():
    """THE REPRODUCED DEFECT. ``source: auto`` was recorded as the basis, so
    a yfinance -> auto policy flip reclassified every genuine change as a
    basis change while nothing about the data had moved."""
    basis = pr.resolve_column_basis({"source": "auto",
                                     "columns_from_norgate": ["AAPL"]})
    assert pr.basis_of(basis, "AAPL") == pr.NORGATE
    assert pr.basis_of(basis, "MSFT") == pr.YFINANCE
    assert pr.YFINANCE not in ("auto",)
    assert "auto" not in set(basis.values())


def test_a_policy_flip_yfinance_to_auto_is_not_a_basis_change():
    old = _frame({"MSFT": [10.0, 11.0, 12.0, 13.0]})
    new = _frame({"MSFT": [10.0, 11.0, 12.0, 13.5]})
    rec = pr.diff_frames(old, new,
                         old_sidecar={"source": "yfinance"},
                         new_sidecar={"source": "auto",
                                      "columns_from_norgate": []})
    assert rec["basis_changes"] == 0
    assert rec["revisions"] == 1


def test_a_column_that_moved_to_norgate_is_a_basis_change_not_a_revision():
    old = _frame({"AAPL": [10.0, 11.0, 12.0, 13.0]})
    new = _frame({"AAPL": [10.1, 11.1, 12.1, 13.1]})
    rec = pr.diff_frames(old, new,
                         old_sidecar={"source": "yfinance"},
                         new_sidecar={"source": "auto",
                                      "columns_from_norgate": ["AAPL"]})
    assert rec["revisions"] == 0 and rec["adjustments"] == 0
    assert rec["basis_changes"] == 4


def test_unknown_provenance_is_withheld_from_the_revision_evidence():
    old = _frame({"X": [10.0, 11.0, 12.0, 13.0]})
    new = _frame({"X": [10.0, 11.0, 12.0, 13.5]})
    rec = pr.diff_frames(old, new, old_sidecar=None, new_sidecar=None)
    assert rec["revisions"] == 0
    assert rec["basis_changes"] == 1


def test_a_strict_norgate_run_leaves_unresolved_columns_unknown():
    basis = pr.resolve_column_basis({"source": "norgate",
                                     "columns_from_norgate": ["AAPL"]})
    assert pr.basis_of(basis, "AAPL") == pr.NORGATE
    assert pr.basis_of(basis, "EXV1.DE") == pr.UNKNOWN_BASIS


# ---------------------------------------------------------------------------
# 6. Adjustments are separated from revisions
# ---------------------------------------------------------------------------
def test_a_whole_column_re_adjustment_is_not_counted_as_revisions(tmp_path):
    _observe(_frame({"VOD.L": [10.0, 20.0, 40.0, np.nan]}), tmp_path)
    _observe(_frame({"VOD.L": [5.0, 10.0, 20.0, np.nan]}), tmp_path,
             now=RUN + timedelta(days=1))          # a clean 2:1 split
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["adjustments"] == 3
    assert s["totals"]["revisions"] == 0
    # An adjustment is not a revision and must not move the verdict towards
    # one, nor away from the honest "no cycle has completed".
    assert s["verdict"].startswith("INSUFFICIENT EVIDENCE")
    assert "adjustment(s) are excluded" in s["verdict"]


def test_one_restated_close_among_many_is_still_a_revision(tmp_path):
    _observe(_frame({"VOD.L": [10.0, 20.0, 40.0, np.nan]}), tmp_path)
    _observe(_frame({"VOD.L": [5.0, 10.0, 21.0, np.nan]}), tmp_path,
             now=RUN + timedelta(days=1))          # ratios disagree
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["adjustments"] == 0
    assert s["totals"]["revisions"] == 3


def test_two_changed_cells_are_too_few_to_call_an_adjustment(tmp_path):
    _observe(_frame({"VOD.L": [10.0, 20.0, 40.0, np.nan]}), tmp_path)
    _observe(_frame({"VOD.L": [10.0, 10.0, 20.0, np.nan]}), tmp_path,
             now=RUN + timedelta(days=1))
    assert pr.cycle_summary(tmp_path)["totals"]["revisions"] == 2


# ---------------------------------------------------------------------------
# 7. Removed rows and columns
# ---------------------------------------------------------------------------
def test_a_removed_row_loses_populated_cells_and_is_counted():
    """THE REPRODUCED DEFECT. Comparing only the intersection scored a
    vanished populated row as a shape change and zero withdrawals."""
    old = _frame({"A": [1.0, 2.0, 3.0, 4.0], "B": [1.0, 2.0, 3.0, 4.0]})
    new = old.iloc[:-1]
    rec = pr.diff_frames(old, new, old_sidecar={"source": "yfinance"},
                         new_sidecar={"source": "yfinance"})
    assert rec["rows_removed"] == 1
    assert rec["removed_cells"] == 2


def test_a_removed_column_is_counted_cell_by_cell():
    old = _frame({"A": [1.0, 2.0, 3.0, 4.0], "B": [1.0, 2.0, np.nan, np.nan]})
    new = old[["A"]]
    rec = pr.diff_frames(old, new, old_sidecar={"source": "yfinance"},
                         new_sidecar={"source": "yfinance"})
    assert rec["columns_removed"] == 1
    assert rec["removed_cells"] == 2, "only the POPULATED cells are a loss"


def test_the_cache_diff_carries_its_own_caveat():
    rec = pr.diff_frames(_frame({"A": [1.0, 2.0, 3.0, 4.0]}),
                         _frame({"A": [1.0, 2.0, 3.0, 4.0]}),
                         old_sidecar={"source": "yfinance"},
                         new_sidecar={"source": "yfinance"})
    assert "post-preservation" in rec["caveat"]
    assert rec["kind"] == "cache_diff"


# ---------------------------------------------------------------------------
# 8. Bounds, retention and honesty about both
# ---------------------------------------------------------------------------
def test_the_cell_budget_drops_the_oldest_dates_and_says_so(tmp_path):
    idx = pd.bdate_range("2026-09-01", "2026-09-15")
    df = pd.DataFrame(1.0, index=idx, columns=["A", "B", "C"])
    obs = pr.observe_vendor_frame(df, panel="p", now_utc=RUN, window=8,
                                  max_cells=6)
    assert obs["truncated_dates"] > 0
    assert len(obs["dates"]) * 3 <= 6 + 3


def test_a_cell_evicted_while_withheld_is_a_cycle_we_will_never_close(tmp_path):
    early = datetime(2026, 8, 1, 1, tzinfo=timezone.utc)
    old_idx = pd.to_datetime(["2026-07-29", "2026-07-30"])
    _observe(pd.DataFrame({"A": [1.0, 2.0]}, index=old_idx), tmp_path, now=early)
    _observe(pd.DataFrame({"A": [1.0, np.nan]}, index=old_idx), tmp_path,
             now=early + timedelta(days=1))
    # Months later the window has moved on entirely.
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, now=RUN)
    s = pr.cycle_summary(tmp_path)
    assert s["missing_evidence"]["cycles_lost_to_retention"] == 1


def test_the_summary_reports_its_retention_limits(tmp_path):
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path)
    s = pr.cycle_summary(tmp_path)
    assert s["retention_limits"]["window_sessions"] == pr.WINDOW_SESSIONS
    assert s["retention_limits"]["withheld_retention_days"] == \
        pr.WITHHELD_RETENTION_DAYS
    assert "served entirely from cache" in s["missing_evidence"]["note"]


def test_panels_are_reported_separately(tmp_path):
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="exv1")
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="csp1")
    s = pr.cycle_summary(tmp_path)
    assert set(s["panels"]) == {"exv1", "csp1"}


def test_the_event_ledger_is_trimmed(tmp_path):
    p = pr.events_path(tmp_path)
    for i in range(5):
        pr._append_bounded(p, {"n": i}, cap=3)
    lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
    assert [r["n"] for r in lines] == [2, 3, 4]


# ---------------------------------------------------------------------------
# 9. It never raises into the build
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, "not a frame", 42, object()])
def test_an_unusable_frame_returns_rather_than_raises(tmp_path, bad):
    assert pr.record_vendor_observation(bad, panel="p", root=tmp_path) is not None
    assert pr.diff_frames(bad if isinstance(bad, pd.DataFrame) else None,
                          _frame({"A": [1.0, 2.0, 3.0, 4.0]}))["comparable"] \
        is False


def test_a_corrupt_state_file_is_started_over_not_raised(tmp_path):
    sp = pr.state_path(tmp_path, "exv1")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("[[[", encoding="utf-8")
    out = _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path)
    assert out is not None and out["counters"]["first_served"] == 3


def test_a_corrupt_state_file_is_reported_by_the_summary(tmp_path):
    sp = pr.state_path(tmp_path, "broken")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("[[[", encoding="utf-8")
    assert pr.cycle_summary(tmp_path)["panels"]["broken"]["error"]


def test_an_unwritable_cache_path_does_not_raise(tmp_path):
    out = pr.capture_cache_change(tmp_path / "missing.parquet",
                                  _frame({"A": [1.0, 2.0, 3.0, 4.0]}),
                                  {"source": "yfinance"},
                                  ledger=tmp_path / "logs" / "c.jsonl")
    assert out["comparable"] is False


def test_state_writes_leave_no_temp_files(tmp_path):
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path)
    assert not list((tmp_path / "logs" / "vendor_state").glob("*.tmp"))
