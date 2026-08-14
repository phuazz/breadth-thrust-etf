"""Tests for scripts/check_roster_integrity.py — the outage-hole guard.

The failure it exists for: an upstream drop mid-refresh leaves honest holes in
every roster fetched AFTER it, recorded in endpoint_unavailable. breadth_csp1
is written early, so the wrapper's anchor guard can pass while later sleeves
compute breadth on incomplete rosters. Those are different questions and need
different checks.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts.check_roster_integrity import evaluate


def _repo(tmp_path, rosters: dict[str, int]):
    """A git repo with committed rosters, each carrying `n` outage entries."""
    (tmp_path / "data").mkdir()
    for etf, n in rosters.items():
        blob = {
            "snapshots": {"2026-08-07": {"tickers": ["AAA"]}},
            "endpoint_unavailable": [
                {"target_friday": f"2026-0{i + 1}-01"} for i in range(n)
            ],
        }
        (tmp_path / "data" / f"constituents_{etf}.json").write_text(
            json.dumps(blob), encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True,
                       capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=tmp_path, check=True,
                   capture_output=True)
    return tmp_path


def _set_holes(repo, etf: str, n: int):
    p = repo / "data" / f"constituents_{etf}.json"
    blob = json.loads(p.read_text(encoding="utf-8"))
    blob["endpoint_unavailable"] = [
        {"target_friday": f"2026-0{i + 1}-01"} for i in range(n)]
    p.write_text(json.dumps(blob), encoding="utf-8")


def test_clean_tree_passes(tmp_path):
    r = evaluate(_repo(tmp_path, {"csp1": 0, "cndx": 0}))
    assert r["ok"] is True
    assert r["new_holes"] == 0


def test_a_new_hole_fails(tmp_path):
    """The mid-run drop: a roster gains an outage entry this run."""
    repo = _repo(tmp_path, {"csp1": 0, "cndx": 0})
    _set_holes(repo, "cndx", 3)
    r = evaluate(repo)
    assert r["ok"] is False
    assert r["new_holes"] == 3
    assert any(x["etf"] == "CNDX" and x["new"] == 3 for x in r["rows"])
    # The clean roster must not be implicated.
    assert all(x["new"] == 0 for x in r["rows"] if x["etf"] == "CSP1")


def test_pre_existing_holes_do_not_fail(tmp_path):
    """A past outage leaves a permanent, already-committed gap. Demanding zero
    would fail forever on history nobody intends to repair."""
    repo = _repo(tmp_path, {"csp1": 5})
    r = evaluate(repo)
    assert r["ok"] is True
    assert r["rows"][0]["holes_now"] == 5
    assert r["rows"][0]["new"] == 0


def test_a_hole_removed_is_not_a_failure(tmp_path):
    """Refetching a previously-missing Friday is a repair, not a regression."""
    repo = _repo(tmp_path, {"csp1": 4})
    _set_holes(repo, "csp1", 1)
    r = evaluate(repo)
    assert r["ok"] is True
    assert r["rows"][0]["new"] == -3


def test_unreadable_roster_is_undetermined_not_pass(tmp_path):
    """A roster that cannot be parsed is not evidence of health."""
    repo = _repo(tmp_path, {"csp1": 0})
    (repo / "data" / "constituents_csp1.json").write_text(
        "{ truncated", encoding="utf-8")
    r = evaluate(repo)
    assert r["undetermined"] is True
    assert r["ok"] is False


def test_no_rosters_is_undetermined(tmp_path):
    (tmp_path / "data").mkdir()
    r = evaluate(tmp_path)
    assert r["undetermined"] is True
