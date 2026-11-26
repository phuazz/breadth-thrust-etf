"""Weekly combined-book risk rebuild wrapper (scripts/scheduled_cbr_refresh.py).

Offline: no git, no vendor fetch. The exporter is replaced by a fake that
writes an FX export and returns the "raw vendor" series it was derived from,
so the forward-fill guard can be exercised both ways. Python datetime months
are 1-indexed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import combined_book_risk as cbr  # noqa: E402
import scheduled_cbr_refresh as job  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_cbr_fixtures", ROOT / "tests" / "test_combined_book_risk.py")
_fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fx)
write_fixture, edit_json, CUTOFF, NOW = _fx.write_fixture, _fx.edit_json, _fx.CUTOFF, _fx.NOW

TODAY = date(2026, 10, 27)


def noop(*_a, **_k):
    pass


def save_current_snapshot(d: Path) -> dict:
    blob = cbr.build(d, NOW)
    assert blob["risk_status"] == "available", blob["reasons"]
    cbr.write_atomic(blob, d / job.ARTEFACT)
    return blob


# --------------------------------------------------------------------------
# probe: the window is read from the data
# --------------------------------------------------------------------------

def test_probe_open_when_anchors_match_and_book_is_pre_fill(tmp_path):
    d = write_fixture(tmp_path)
    v = job.probe(d, TODAY)
    assert v["state"] == "open" and v["anchor"] == CUTOFF


def test_probe_skips_before_live_track_re_anchors(tmp_path):
    # The Sunday 09:48 shape: held basis on the new Friday, live_track on the old one.
    d = write_fixture(tmp_path)
    edit_json(d / "live_track.json", lambda o: o.update(anchor_date="2026-10-19"))
    v = job.probe(d, TODAY)
    assert v["state"] == "skip" and "2026-10-19" in v["reason"]


def test_probe_skips_once_post_fill_weights_are_published(tmp_path):
    # The Tuesday post-fill shape: same anchor, a different book (30pp on 24 Nov).
    d = write_fixture(tmp_path)

    def post_fill(o):
        w = o["effective_weights"]
        w["DBC"], w["SPY"] = w["DBC"] - 0.15, w["SPY"] + 0.15
    edit_json(d / "live_track.json", post_fill)
    v = job.probe(d, TODAY)
    assert v["state"] == "skip" and v["reconciliation"] == "fail"
    assert "window has closed" in v["reason"]


def test_probe_skips_after_the_fill_re_anchors_live_track(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "live_track.json", lambda o: o.update(anchor_date="2026-10-26"))
    assert job.probe(d, TODAY)["state"] == "skip"


def test_probe_skips_when_snapshot_already_current(tmp_path):
    d = write_fixture(tmp_path)
    save_current_snapshot(d)
    v = job.probe(d, TODAY)
    assert v["state"] == "skip" and "already current" in v["reason"]


def test_probe_stays_open_over_a_failed_run_record(tmp_path):
    d = write_fixture(tmp_path)
    save_current_snapshot(d)
    # A failed-run record for the same file is not "current": the window stays open.
    cbr.write_atomic(cbr.failed_run(RuntimeError("x"), NOW, "r1"), d / job.ARTEFACT)
    assert job.probe(d, TODAY)["state"] == "open"


def test_probe_skips_on_an_invalid_held_basis(tmp_path):
    d = write_fixture(tmp_path)
    (d / "component_held_basis.json").unlink()
    v = job.probe(d, TODAY)
    assert v["state"] == "skip" and v["anchor"] is None


def test_probe_writes_nothing(tmp_path):
    d = write_fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    job.probe(d, TODAY)
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before


# --------------------------------------------------------------------------
# FX: never forward-filled
# --------------------------------------------------------------------------

def raw_series(days: list[str], values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.to_datetime(days))


def export(days, values):
    return {"dates": list(days), "close": list(values)}


def test_fx_guard_passes_when_every_rate_is_a_vendor_bar():
    days = ["2026-10-21", "2026-10-22", "2026-10-23"]
    vals = [1.10, 1.11, 1.12]
    assert job.fx_forward_fill_violations(export(days, vals), raw_series(days, vals), CUTOFF) == []


def test_fx_guard_flags_a_forward_filled_anchor():
    # Friday's EURUSD bar not yet published: the converter carries Thursday's rate.
    days = ["2026-10-21", "2026-10-22", "2026-10-23"]
    out = job.fx_forward_fill_violations(
        export(days, [1.10, 1.11, 1.11]), raw_series(days[:2], [1.10, 1.11]), CUTOFF)
    assert len(out) == 1 and out[0].startswith("2026-10-23: no vendor")


def test_fx_guard_flags_a_value_that_is_not_the_vendor_close():
    days = ["2026-10-22", "2026-10-23"]
    out = job.fx_forward_fill_violations(
        export(days, [1.11, 1.1201]), raw_series(days, [1.11, 1.12]), CUTOFF)
    assert len(out) == 1 and "not the vendor close" in out[0]


def test_fx_guard_ignores_dates_after_the_anchor():
    days = ["2026-10-23", "2026-10-26"]
    out = job.fx_forward_fill_violations(
        export(days, [1.12, 1.12]), raw_series(days[:1], [1.12]), CUTOFF)
    assert out == []


def test_fx_guard_month_boundary():
    # Friday 31 Oct 2025 -> Monday 3 Nov 2025; a hole on the month's first session.
    days = ["2025-10-30", "2025-10-31", "2025-11-03"]
    out = job.fx_forward_fill_violations(
        export(days, [1.15, 1.16, 1.16]), raw_series(days[:2], [1.15, 1.16]), "2025-11-03")
    assert [o[:10] for o in out] == ["2025-11-03"]


def test_fx_guard_year_boundary():
    # Wednesday 31 Dec 2025 -> Friday 2 Jan 2026; 1 Jan is a holiday on both sides.
    days = ["2025-12-30", "2025-12-31", "2026-01-02"]
    vals = [1.17, 1.18, 1.19]
    assert job.fx_forward_fill_violations(export(days, vals), raw_series(days, vals),
                                          "2026-01-02") == []
    out = job.fx_forward_fill_violations(
        export(days, [1.17, 1.18, 1.18]), raw_series(days[:2], vals[:2]), "2026-01-02")
    assert [o[:10] for o in out] == ["2026-01-02"]


# --------------------------------------------------------------------------
# rebuild: promote only on success; fail closed otherwise
# --------------------------------------------------------------------------

def fake_exporter(drop_last: bool = False):
    """Rewrite the fixture's FX export in place (as the operator export does)
    and return the raw vendor series; optionally hide the last bar so the
    exported last rate is a forward fill."""
    def exporter(data_dir: Path):
        obj = json.loads((data_dir / cbr.ENGINE_FX_EXPORT).read_text())
        obj["provenance"]["exported_at_utc"] = "2026-10-27T02:00:00Z"
        (data_dir / cbr.ENGINE_FX_EXPORT).write_text(json.dumps(obj))
        days, vals = obj["dates"], obj["close"]
        if drop_last:
            days, vals = days[:-1], vals[:-1]
        return raw_series(days, vals)
    return exporter


def test_rebuild_in_window_promotes_a_current_snapshot(tmp_path):
    d = write_fixture(tmp_path / "data")
    res = job.rebuild(CUTOFF, noop, data_dir=d, staging_dir=tmp_path / "stage",
                      exporter=fake_exporter())
    assert res["risk_status"] == "available" and res["anchor"] == CUTOFF
    _, check = cbr.read_snapshot(d / job.ARTEFACT, d)
    assert check["state"] == "current"
    # The calculator's lock and marker live beside the staging file, not in data/.
    assert not (d / (job.ARTEFACT + ".pending")).exists()
    assert not (d / (job.ARTEFACT + ".lock")).exists()


def test_forward_filled_fx_fails_and_restore_leaves_previous_artefact(tmp_path):
    d = write_fixture(tmp_path / "data")
    save_current_snapshot(d)
    before = job.snapshot_bytes(d)
    with pytest.raises(job.StepFailed, match="not vendor observations"):
        job.rebuild(CUTOFF, noop, data_dir=d, staging_dir=tmp_path / "stage",
                    exporter=fake_exporter(drop_last=True))
    assert job.snapshot_bytes(d) != before          # the export wrote in place
    job.restore_bytes(before, d, noop)
    assert job.snapshot_bytes(d) == before


def test_unavailable_risk_never_replaces_the_committed_artefact(tmp_path):
    d = write_fixture(tmp_path / "data")
    save_current_snapshot(d)
    art = (d / job.ARTEFACT).read_bytes()
    # Break the prices after the snapshot: the new build is unavailable.
    edit_json(d / "holdings_prices_1y.json",
              lambda o: o["prices"]["DBC"].update(dates=o["prices"]["DBC"]["dates"][:-5],
                                                  prices=o["prices"]["DBC"]["prices"][:-5]))
    with pytest.raises(job.StepFailed, match="risk unavailable"):
        job.rebuild(CUTOFF, noop, data_dir=d, staging_dir=tmp_path / "stage",
                    exporter=fake_exporter())
    assert (d / job.ARTEFACT).read_bytes() == art


def test_restore_removes_a_file_that_did_not_exist(tmp_path):
    d = tmp_path
    before = {job.ARTEFACT: None, job.FX_EXPORT: b"x"}
    (d / job.FX_EXPORT).write_bytes(b"y")
    (d / job.ARTEFACT).write_text("{}")
    job.restore_bytes(before, d, noop)
    assert not (d / job.ARTEFACT).exists() and (d / job.FX_EXPORT).read_bytes() == b"x"


def test_commit_subject_matches_the_fleet_watch_grep():
    fw = Path("C:/dev/scripts/fleet_watch.json")
    if not fw.exists():
        pytest.skip("vault fleet_watch.json not present on this machine")
    greps = [c.get("grep") for c in json.loads(fw.read_text(encoding="utf-8"))["checks"]
             if c.get("repo") == "C:/dev/breadth-thrust-etf"]
    assert job.COMMIT_PREFIX in greps
