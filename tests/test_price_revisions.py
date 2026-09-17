"""Vendor revision evidence: what it can see, and what it must refuse to say.

The first version of this module measured the CACHE after the
cell-preservation merge had already refilled whatever the vendor withdrew,
so the one category that bears on the append-versus-revise question could
not fire. These tests pin the ordering, the categories and - as much as
anything - the refusal to read zero cycles as reassurance.

Python datetime months are 1-indexed (January = 1).

PROVENANCE OF THE LABELS BELOW. Tests marked "DEFECT (predecessor)" pin a
defect of the UNCOMMITTED draft that preceded commit 47d1b3a. All three
modules are absent at 47d1b3a^, so those cases cannot be demonstrated as
behavioural failures against any commit: the draft existed only as untracked
working-tree files, and the reproductions were run against it in session on
2026-09-16 before it was overwritten. Tests marked "REPRODUCED 2026-09-16"
are different - they were reproduced against 47d1b3a itself, which is in the
history, and they fail there.
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
    """DEFECT (predecessor), both halves in one test.

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
    """DEFECT (predecessor). ``source: auto`` was recorded as the basis, so
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
def test_a_clean_ratio_across_a_column_is_not_counted_as_revisions(tmp_path):
    """A 2:1 rescale is not a restatement of individual closes - and, with
    no independent evidence, not a demonstrated corporate action either."""
    _observe(_frame({"VOD.L": [10.0, 20.0, 40.0, np.nan]}), tmp_path)
    _observe(_frame({"VOD.L": [5.0, 10.0, 20.0, np.nan]}), tmp_path,
             now=RUN + timedelta(days=1))
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["revisions"] == 0
    assert s["totals"]["adjustments"] == 0
    assert s["totals"]["ambiguous"] == 3
    assert s["verdict"].startswith("UNRESOLVED")


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
    """DEFECT (predecessor). Comparing only the intersection scored a
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


# ---------------------------------------------------------------------------
# 10. A retained cycle must be able to CLOSE (finding 3, 2026-09-16)
# ---------------------------------------------------------------------------
def _long_frame(values=None):
    idx = pd.bdate_range("2026-09-01", "2026-09-15")        # 11 sessions
    return pd.DataFrame({"A": values or [float(i + 1) for i in range(len(idx))]},
                        index=idx)


def test_an_open_cycle_closes_after_its_date_leaves_the_window(tmp_path):
    """REPRODUCED 2026-09-16 against 47d1b3a: a cell withdrawn at a date that then fell out of the
    eight-session window stayed "withheld" for ever. The vendor served the
    restored value in every later frame and nothing read it, so the cycle
    was retained for thirty days and could never close."""
    full = _long_frame()
    _observe(full, tmp_path, panel="p", now=RUN - timedelta(days=6))
    withheld = full.copy()
    withheld.loc[pd.Timestamp("2026-09-03"), "A"] = np.nan
    _observe(withheld, tmp_path, panel="p", now=RUN - timedelta(days=5))
    assert pr.cycle_summary(tmp_path)["totals"]["withdrawals"] == 1

    out = _observe(full, tmp_path, panel="p", now=RUN)      # window has moved on
    assert "2026-09-03" in (out["reopened"] or []), "the open cycle was not re-read"
    totals = pr.cycle_summary(tmp_path)["totals"]
    assert totals["restorations"] == 1
    assert totals["restorations_identical"] == 1
    assert totals["open_withdrawals"] == 0


def test_a_reopened_date_does_not_widen_the_ordinary_window(tmp_path):
    full = _long_frame()
    _observe(full, tmp_path, panel="p", now=RUN - timedelta(days=6))
    obs = pr.observe_vendor_frame(full, panel="p", now_utc=RUN,
                                  also_admit={"2026-09-02"})
    assert obs["window_dates"] == obs["dates"][len(obs["reopened_dates"]):]
    assert len(obs["window_dates"]) <= pr.WINDOW_SESSIONS
    assert obs["reopened_dates"] == ["2026-09-02"]


# ---------------------------------------------------------------------------
# 11. Adjustment classification, both directions (finding 4)
# ---------------------------------------------------------------------------
IDX8 = pd.bdate_range("2026-09-02", "2026-09-11")


def _flat(value=100.0):
    return pd.DataFrame({"A": [value] * len(IDX8)}, index=IDX8)


def test_scattered_cells_at_one_ratio_are_not_an_adjustment(tmp_path):
    """REPRODUCED 2026-09-16 against 47d1b3a: three non-contiguous cells moving 100 -> 101 with five
    unchanged were counted as three adjustments. No corporate action does
    that, and calling it one removed three changes from the evidence."""
    _observe(_flat(), tmp_path, panel="p")
    after = _flat()
    for d in (IDX8[0], IDX8[3], IDX8[6]):
        after.loc[d, "A"] = 101.0
    _observe(after, tmp_path, panel="p", now=RUN + timedelta(days=1))
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["adjustments"] == 0
    assert s["totals"]["ambiguous"] == 3
    assert s["verdict"].startswith("UNRESOLVED")
    assert s["missing_evidence"]["ambiguous_changes_unresolved"] == 3


def test_a_uniform_prefix_is_AMBIGUOUS_without_independent_evidence(tmp_path):
    """REPRODUCED 2026-09-16 against the second pass: a constant ratio over a
    contiguous prefix is the SHAPE of a split, and the shape was taken as
    proof of one. It is not. The change is recorded, its shape is recorded,
    and the cause is left unestablished."""
    _observe(_flat(), tmp_path, panel="p")
    split = _flat()
    split.iloc[:5, 0] = 50.0
    _observe(split, tmp_path, panel="p", now=RUN + timedelta(days=1))
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["adjustments"] == 0
    assert s["totals"]["ambiguous"] == 5
    assert s["verdict"].startswith("UNRESOLVED")
    events = [json.loads(ln) for ln in pr.events_path(tmp_path)
              .read_text(encoding="utf-8").splitlines()]
    assert {e["shape"] for e in events} == {"prefix"},         "the shape is still recorded for an investigator"


def test_independent_evidence_is_what_makes_an_adjustment(tmp_path):
    """ADJUSTMENT exists, and only a source outside the prices can assign
    it. Nothing is wired to this in the deployed configuration."""
    changes = {"A": [("2026-09-02", 100.0, 50.0)] * 3}
    assert pr._classify_changes(changes) == {"A": pr.AMBIGUOUS}
    assert pr._classify_changes(changes, adjustment_evidence=lambda *a: True)         == {"A": pr.ADJUSTMENT}
    # Evidence that raises is evidence that did not arrive.
    def boom(*a):
        raise RuntimeError("feed down")
    assert pr._classify_changes(changes, adjustment_evidence=boom) ==         {"A": pr.AMBIGUOUS}


def test_a_whole_column_halving_across_a_restoration_is_not_a_revision(tmp_path):
    """REPRODUCED 2026-09-16 against 47d1b3a: restorations bypassed
    classification entirely, so a column rescaled while seven of eight bars
    were withheld came back as seven changed restorations and read as
    REVISION OBSERVED. It is now UNRESOLVED - not a revision, and not clean
    either."""
    _observe(_flat(), tmp_path, panel="p")
    withheld = _flat()
    withheld.iloc[:7, 0] = np.nan
    _observe(withheld, tmp_path, panel="p", now=RUN + timedelta(days=1))
    _observe(_flat(50.0), tmp_path, panel="p", now=RUN + timedelta(days=2))
    t = pr.cycle_summary(tmp_path)["totals"]
    assert t["restorations"] == 7 and t["restorations_changed"] == 7
    assert t["restorations_changed_ambiguous"] == 7
    assert t["revisions"] == 0
    verdict = pr.cycle_summary(tmp_path)["verdict"]
    assert not verdict.startswith("REVISION OBSERVED"), \
        "an unexplained uniform change was read as proof of restatement"
    assert verdict.startswith("UNRESOLVED"), \
        "an unexplained uniform change was read as a clean result"


def test_a_genuine_restatement_across_a_restoration_still_counts(tmp_path):
    """The other direction: the adjustment carve-out must not swallow a real
    change. One restored bar comes back at a ratio of its own."""
    _observe(_flat(), tmp_path, panel="p")
    withheld = _flat()
    withheld.iloc[:7, 0] = np.nan
    _observe(withheld, tmp_path, panel="p", now=RUN + timedelta(days=1))
    restored = _flat(50.0)
    restored.iloc[3, 0] = 47.0                     # not the column's ratio
    _observe(restored, tmp_path, panel="p", now=RUN + timedelta(days=2))
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["restorations_changed_by_adjustment"] == 0
    assert s["verdict"].startswith("REVISION OBSERVED")


@pytest.mark.parametrize("changed,expected", [(2, "revisions"), (3, "ambiguous")])
def test_the_minimum_cell_count_before_a_ratio_is_even_a_question(
        tmp_path, changed, expected):
    """Below three cells a uniform ratio is not a pattern, so the change is
    simply a revision; at three it becomes a question nothing here answers."""
    _observe(_flat(), tmp_path, panel="p")
    after = _flat()
    after.iloc[:changed, 0] = 50.0
    _observe(after, tmp_path, panel="p", now=RUN + timedelta(days=1))
    assert pr.cycle_summary(tmp_path)["totals"][expected] == changed


# ---------------------------------------------------------------------------
# 12. The cap binds ACCUMULATED state (finding 5)
# ---------------------------------------------------------------------------
def test_the_cell_cap_binds_the_state_not_just_one_observation(tmp_path):
    """REPRODUCED 2026-09-16 against 47d1b3a: eight sessions x 750 columns, withdraw the oldest session,
    advance one session -> 6,750 cells retained against a reported ceiling of
    6,000, and a megabyte of state per panel."""
    cols = [f"T{i}" for i in range(750)]
    idx = pd.bdate_range(end="2026-09-11", periods=8)
    served = pd.DataFrame(1.0, index=idx, columns=cols)
    _observe(served, tmp_path, panel="p", now=RUN - timedelta(days=2))
    withheld = served.copy()
    withheld.loc[idx[0]] = np.nan
    _observe(withheld, tmp_path, panel="p", now=RUN - timedelta(days=1))
    later = pd.DataFrame(1.0, index=pd.bdate_range(end="2026-09-14", periods=8),
                         columns=cols)
    _observe(later, tmp_path, panel="p", now=RUN)
    state = json.loads(pr.state_path(tmp_path, "p").read_text(encoding="utf-8"))
    # The WORKING SET is never cut: the window plus one retained session.
    assert len(state["cells"]) <= (pr.MAX_CELLS + pr.MAX_RETAINED_CELLS)
    assert state["counters"]["working_set_evictions"] == 0
    assert state["retention"]["max_retained_cells"] == pr.MAX_RETAINED_CELLS


def test_an_evicted_cell_does_not_hide_a_later_withdrawal(tmp_path):
    """REPRODUCED 2026-09-16 against the second pass: at the 6,000-cell cap,
    advancing one session evicted 750 SERVED cells that were still inside
    the observation window. Withdrawing those cells afterwards left the
    count at 750 instead of 1,500 - forgotten prior state read as "never
    served"."""
    cols = [f"T{i}" for i in range(750)]
    first = pd.bdate_range(end="2026-09-11", periods=8)
    served = pd.DataFrame(1.0, index=first, columns=cols)
    _observe(served, tmp_path, panel="p", now=RUN - timedelta(days=3))
    withheld = served.copy()
    withheld.loc[first[0]] = np.nan                 # 750 withdrawals
    _observe(withheld, tmp_path, panel="p", now=RUN - timedelta(days=2))

    second = pd.bdate_range(end="2026-09-14", periods=8)
    advanced = pd.DataFrame(1.0, index=second, columns=cols)
    _observe(advanced, tmp_path, panel="p", now=RUN - timedelta(days=1))
    later = advanced.copy()
    later.loc[second[0]] = np.nan                   # 750 more
    _observe(later, tmp_path, panel="p", now=RUN)

    t = pr.cycle_summary(tmp_path)["totals"]
    assert t["withdrawals"] == 1500
    assert t["withdrawals_undetectable"] == 0
    assert t["working_set_evictions"] == 0


def test_forgotten_prior_state_is_counted_and_constrains_the_verdict(tmp_path):
    """When the hard ceiling does cut the working set, the loss of DETECTION
    is what matters, and an eviction count alone does not say it."""
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="p")
    state = json.loads(pr.state_path(tmp_path, "p").read_text(encoding="utf-8"))
    state["cells"] = {}                             # as if the cap had cut them
    pr._atomic_write(pr.state_path(tmp_path, "p"), json.dumps(state))
    _observe(_frame({"A": [1.0, 2.0, np.nan, np.nan]}), tmp_path, panel="p",
             now=RUN + timedelta(days=1))
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["prior_state_forgotten"] == 4
    # Two of the four come back empty, and for BOTH the prior state is gone:
    # the cell that was priced and the cell that never was are now
    # indistinguishable, which is exactly the uncertainty being surfaced.
    assert s["totals"]["withdrawals_undetectable"] == 2
    assert s["missing_evidence"]["withdrawals_that_could_not_be_detected"] == 2
    assert s["verdict"].startswith("UNRESOLVED")
    assert "detection was incomplete" in s["verdict"]


def test_a_ticker_first_answered_on_run_two_is_not_a_lost_opportunity(tmp_path):
    """REPRODUCED 2026-09-16: a comparison OPPORTUNITY counted any cell whose
    DATE the previous window carried, so a ticker the vendor did not answer
    for on run one - and whose prior state therefore never existed - had its
    first answer on run two recorded as four cells of prior state lost. The
    panel had lost nothing and read 0.5."""
    idx = pd.to_datetime(["2026-09-08", "2026-09-09", "2026-09-10",
                          "2026-09-11"])
    quiet_first = pd.DataFrame({"LIVE": [1.0] * 4, "QUIET": [np.nan] * 4},
                               index=idx)
    _observe(quiet_first, tmp_path, panel="p")
    answered = pd.DataFrame({"LIVE": [1.0] * 4, "QUIET": [2.0] * 4}, index=idx)
    _observe(answered, tmp_path, panel="p", now=RUN + timedelta(days=1))

    t = pr.cycle_summary(tmp_path)["totals"]
    assert t["prior_state_forgotten"] == 0
    assert t["comparison_opportunities"] == 4, "only LIVE could be compared"
    panel = pr.cycle_summary(tmp_path)["panels"]["p"]
    assert panel["detection_coverage"] == 1.0
    assert panel["detection_coverage_last_run"] == 1.0
    assert not pr.cycle_summary(tmp_path)["verdict"].startswith("UNRESOLVED")


def test_a_genuinely_forgotten_cell_is_still_counted(tmp_path):
    """The other direction: a cell we DID hold and no longer do is still an
    opportunity, and still a loss."""
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="p")
    state = json.loads(pr.state_path(tmp_path, "p").read_text(encoding="utf-8"))
    state["cells"] = {}
    pr._atomic_write(pr.state_path(tmp_path, "p"), json.dumps(state))
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="p",
             now=RUN + timedelta(days=1))
    t = pr.cycle_summary(tmp_path)["totals"]
    assert t["comparison_opportunities"] == 4
    assert t["prior_state_forgotten"] == 4
    assert pr.cycle_summary(tmp_path)["panels"]["p"]["detection_coverage"] == 0.0


def test_detection_coverage_is_separate_from_firing_coverage(tmp_path):
    """Two different questions: how often the vendor was ASKED, and how much
    of what came back could be compared against a prior state."""
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="p")
    pr.record_cache_skip("p", tmp_path, now_utc=RUN)
    panel = pr.cycle_summary(tmp_path)["panels"]["p"]
    assert panel["observation_coverage"] == 0.5
    # A first observation has no prior window, so it offers no comparison
    # opportunity and detection coverage is undefined rather than perfect.
    assert panel["detection_coverage"] is None
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="p",
             now=RUN + timedelta(days=1))
    panel = pr.cycle_summary(tmp_path)["panels"]["p"]
    assert panel["detection_coverage"] == 1.0


def test_the_cap_evicts_ordinary_cells_before_open_cycles(tmp_path):
    cols = [f"T{i}" for i in range(400)]
    idx = pd.bdate_range(end="2026-09-11", periods=8)
    served = pd.DataFrame(1.0, index=idx, columns=cols)
    _observe(served, tmp_path, panel="p", now=RUN - timedelta(days=1))
    withheld = served.copy()
    withheld.loc[idx[0], "T0"] = np.nan            # one open cycle
    _observe(withheld, tmp_path, panel="p", now=RUN)
    state = json.loads(pr.state_path(tmp_path, "p").read_text(encoding="utf-8"))
    open_cells = [k for k, v in state["cells"].items()
                  if v.get("status") == "withheld"]
    assert open_cells, "the open cycle was evicted ahead of ordinary cells"
    assert state["counters"]["cells_evicted_while_withheld"] == 0


# ---------------------------------------------------------------------------
# 13. Missing evidence is measured, not asserted
# ---------------------------------------------------------------------------
def test_an_unanswered_ticker_is_retained_as_missing_evidence(tmp_path):
    """REPRODUCED 2026-09-16 against 47d1b3a: a wholly unanswered ticker produced runs:1,
    runs_unanswered:0 and zero tracked cells - the fact that the vendor gave
    nothing for it was discarded entirely. It is missing evidence, and it is
    still not a withdrawal."""
    _observe(_frame({"DEAD.L": [np.nan] * 4}), tmp_path, panel="p")
    s = pr.cycle_summary(tmp_path)
    assert s["totals"]["unanswered_ticker_observations"] == 1
    assert s["missing_evidence"]["unanswered_ticker_observations"] == 1
    assert s["totals"]["withdrawals"] == 0


def test_cache_skips_are_counted_and_coverage_is_measured(tmp_path):
    """A run served from cache asks the vendor nothing. The summary used to
    carry a footnote about that; it now carries the measured share."""
    _observe(_frame({"A": [1.0, 2.0, 3.0, np.nan]}), tmp_path, panel="p")
    for _ in range(3):
        pr.record_cache_skip("p", tmp_path, now_utc=RUN)
    s = pr.cycle_summary(tmp_path)
    panel = s["panels"]["p"]
    assert panel["runs"] == 1 and panel["runs_cache_skipped"] == 3
    assert panel["observation_coverage"] == 0.25
    assert s["missing_evidence"]["runs_served_from_cache_no_observation"] == 3
    assert panel["last_observation_utc"] and panel["last_cache_skip_utc"]


def test_a_cache_skip_on_a_panel_never_observed_still_records(tmp_path):
    pr.record_cache_skip("fresh", tmp_path, now_utc=RUN)
    s = pr.cycle_summary(tmp_path)
    assert s["panels"]["fresh"]["runs"] == 0
    assert s["panels"]["fresh"]["observation_coverage"] == 0.0
    assert s["verdict"].startswith("INSUFFICIENT EVIDENCE") or \
        s["verdict"].startswith("NO EVIDENCE")


# ---------------------------------------------------------------------------
# 14. The secondary diagnostic is bounded, and says so
# ---------------------------------------------------------------------------
def test_the_cache_diff_cell_walk_is_bounded_but_loss_accounting_is_not():
    idx = pd.bdate_range(end="2026-09-11", periods=200)
    old = pd.DataFrame(1.0, index=idx, columns=["A", "B"])
    new = old.iloc[:-1].drop(columns=["B"])
    rec = pr.diff_frames(old, new, old_sidecar={"source": "yfinance"},
                         new_sidecar={"source": "yfinance"},
                         window_sessions=10)
    assert rec["rows_compared"] == 10
    assert rec["rows_in_common"] == 199
    assert rec["cell_walk_window_sessions"] == 10
    # Whole-frame accounting is unaffected by the walk bound.
    assert rec["columns_removed"] == 1
    assert rec["removed_cells"] == 200 + 1


# ---------------------------------------------------------------------------
# 15. The hooks, in the REAL download_prices
#
# Unit tests pin what the module does with a frame. These pin WHERE it is
# called from, and they fail if either hook moves or disappears - which is
# the failure that made the first version of this module unable to answer
# the question it was written for.
# ---------------------------------------------------------------------------
@pytest.fixture
def cb(monkeypatch):
    import compute_breadth
    return compute_breadth


def _recent_sessions(n=3):
    """Business days ending two days ago, so every one is a finished
    session whatever day this test runs."""
    end = pd.Timestamp(datetime.now(timezone.utc).date()) - pd.Timedelta(days=2)
    return pd.bdate_range(end=end, periods=n)


def _multi(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = pd.MultiIndex.from_product([["Close"], frame.columns])
    return out


def test_the_raw_observation_runs_BEFORE_the_preservation_merge(cb, tmp_path,
                                                                monkeypatch):
    """INTEGRATION. The vendor withdraws a bar it served last run. The
    cell-preservation merge refills it from the cache, so a hook placed after
    that point sees nothing - which is exactly the defect this module was
    rewritten to remove. The observation must record the withdrawal AND the
    written cache must still carry the preserved value.
    """
    idx = _recent_sessions(3)
    cache = tmp_path / "data" / "prices_cache_itest.parquet"
    cache.parent.mkdir(parents=True)
    served = pd.DataFrame({"AAA": [10.0, 11.0, 12.0]}, index=idx)
    withdrawn = pd.DataFrame({"AAA": [10.0, 11.0, np.nan]}, index=idx)
    start, end = str(idx[0].date()), str((idx[-1] + pd.Timedelta(days=1)).date())

    def _run(frame):
        monkeypatch.setattr(cb.yf, "download",
                            lambda *a, **kw: _multi(frame), raising=False)
        return cb.download_prices(["AAA"], start, end, cache_path=cache,
                                  price_source="yfinance", roster=None,
                                  tail_probe=False)

    _run(served)                     # the vendor serves the bar, and it caches
    out = _run(withdrawn)            # and then takes it back

    state = json.loads(pr.state_path(tmp_path, "prices_cache_itest")
                       .read_text(encoding="utf-8"))
    assert state["counters"]["withdrawals"] == 1, \
        "the raw observation hook is missing, or it now runs after preservation"
    # ...and preservation really did mask it downstream, which is the point.
    assert float(out.loc[idx[-1], "AAA"]) == 12.0
    assert float(pd.read_parquet(cache).loc[idx[-1], "AAA"]) == 12.0


def test_a_cache_hit_records_a_skip_and_never_breaks_the_hit(cb, tmp_path,
                                                             monkeypatch):
    """INTEGRATION. The early return asks the vendor nothing. It must be
    counted, and the recorder sits inside the cache-read try block, so it
    must not be able to turn a healthy cache hit into a re-download."""
    idx = _recent_sessions(3)
    cache = tmp_path / "data" / "prices_cache_hit.parquet"
    cache.parent.mkdir(parents=True)
    pd.DataFrame({"AAA": [10.0, 11.0, 12.0]}, index=idx).to_parquet(cache)
    monkeypatch.setattr(cb.yf, "download",
                        lambda *a, **kw: pytest.fail("a cache hit downloaded"),
                        raising=False)
    cb.download_prices(["AAA"], str(idx[0].date()), str(idx[-1].date()),
                       cache_path=cache, price_source="yfinance", roster=None,
                       tail_probe=False)
    s = pr.cycle_summary(tmp_path)
    assert s["panels"]["prices_cache_hit"]["runs_cache_skipped"] == 1

    # A recorder that raised would be swallowed by the cache-read except
    # clause and silently force a download. It cannot raise.
    monkeypatch.setattr(pr, "state_path",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert pr.record_cache_skip("prices_cache_hit", tmp_path) is False
    cb.download_prices(["AAA"], str(idx[0].date()), str(idx[-1].date()),
                       cache_path=cache, price_source="yfinance", roster=None,
                       tail_probe=False)


def test_the_cache_change_diagnostic_still_runs_before_the_write(cb, tmp_path,
                                                                 monkeypatch):
    """The secondary record is bounded now, not removed."""
    idx = _recent_sessions(3)
    cache = tmp_path / "data" / "prices_cache_diag.parquet"
    cache.parent.mkdir(parents=True)
    pd.DataFrame({"AAA": [10.0, 11.0, 12.0]}, index=idx).to_parquet(cache)
    monkeypatch.setattr(
        cb.yf, "download",
        lambda *a, **kw: _multi(pd.DataFrame({"AAA": [10.0, 11.0, 13.0]},
                                             index=idx)), raising=False)
    cb.download_prices(["AAA"], str(idx[0].date()),
                       str((idx[-1] + pd.Timedelta(days=1)).date()),
                       cache_path=cache, price_source="yfinance", roster=None,
                       tail_probe=False)
    ledger = pr.cache_ledger_path(tmp_path)
    assert ledger.exists()
    rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["kind"] == "cache_diff" and rec["panel"] == "prices_cache_diag"
    assert rec["cell_walk_window_sessions"] == pr.CACHE_DIFF_SESSIONS


def test_the_event_ledger_takes_a_batch_in_one_rewrite(tmp_path):
    p = pr.events_path(tmp_path)
    assert pr._append_bounded(p, [{"n": i} for i in range(5)], cap=3)
    lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
    assert [r["n"] for r in lines] == [2, 3, 4]
    assert pr._append_bounded(p, [], cap=3) is True
