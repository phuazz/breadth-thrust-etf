"""The automation clone publishes over a moved origin without hunk-merging
generated outputs (2026-10-01).

Real git repositories under tmp_path: a bare origin, the automation clone,
and a second clone standing in for the daily live-track workflow, which
rewrites the same generated files the refresh rewrites and now lands inside
the refresh's window. The isolated git config carries core.longpaths for the
Windows tmp path and init.defaultBranch=main so the ref names match
production. Python date months are 1-indexed (January = 1).
"""

from __future__ import annotations

import inspect
import io
import subprocess
from datetime import date
from pathlib import Path

import pytest

from scripts import scheduled_refresh as _sr
from scripts.scheduled_refresh import (
    abort_stuck_operation,
    is_scheduled_commit_subject,
    reconcile_unpushed_commits,
    replay_onto_origin,
    scheduled_commit_message,
)

SCHEDULED = ("Local post-fill refresh 2026-09-30 (scheduled): panels current "
             "to 2026-09-29, all steps OK")
CI_SUBJECT = "Daily live track refresh 2026-09-30"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=check)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _commit_all(cwd: Path, subject: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", subject)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _push(cwd: Path) -> None:
    _git(cwd, "push", "-q", "origin", "main")


def _sha(cwd: Path, rev: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", rev).stdout.strip()


def _subject(cwd: Path, rev: str = "HEAD") -> str:
    return _git(cwd, "log", "-1", "--format=%s", rev).stdout.strip()


def _origin_tip(cwd: Path) -> str:
    return _git(cwd, "ls-remote", "origin", "main").stdout.split()[0]


def _clean(cwd: Path) -> bool:
    return _git(cwd, "status", "--porcelain").stdout.strip() == ""


def _mid_operation(cwd: Path) -> bool:
    git_dir = cwd / ".git"
    return any((git_dir / m).exists()
               for m in ("rebase-merge", "rebase-apply", "MERGE_HEAD"))


@pytest.fixture
def repos(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t.test\n"
                   "[core]\n\tlongpaths = true\n\tautocrlf = false\n"
                   "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _write(clone, "data/live_track.json",
           '{"anchor_date": "2026-09-25", "live_dates": ["2026-09-26"]}\n')
    _write(clone, "data/retired.json", "{}\n")
    _write(clone, "docs/index.html", "page: base\n")
    _write(clone, "docs/scanner.html", "scanner: base\n")
    _write(clone, "build/portfolio.html", "portfolio: base\n")
    _write(clone, "template.html", "line A\nline B\nline C\n")
    _commit_all(clone, "base")
    _push(clone)
    ci = tmp_path / "ci"
    _git(tmp_path, "clone", "-q", str(origin), str(ci))
    return clone, ci


def _ci_rewrites_the_same_outputs(ci: Path) -> str:
    """What the daily live-track workflow commits: the same generated files,
    regenerated from the older anchor, plus files of its own."""
    _write(ci, "data/live_track.json",
           '{"anchor_date": "2026-09-25", "live_dates": ["2026-09-26", "2026-09-29"]}\n')
    _write(ci, "docs/index.html", "page: ci\n")
    _write(ci, "docs/scanner.html", "scanner: ci\n")
    _write(ci, "data/scanner_latest.json", "{}\n")
    sha = _commit_all(ci, CI_SUBJECT)
    _push(ci)
    return sha


def _local_refresh(clone: Path) -> str:
    """What the refresh commits: a re-anchored set of generated outputs, a
    retired file removed, a new file, and a literal change in the template."""
    _write(clone, "data/live_track.json",
           '{"anchor_date": "2026-09-29", "live_dates": []}\n')
    _write(clone, "docs/index.html", "page: local\n")
    _write(clone, "build/portfolio.html", "portfolio: local\n")
    _write(clone, "data/strategy_freshness.json", '{"all_current": true}\n')
    (clone / "data" / "retired.json").unlink()
    _write(clone, "template.html", "line A\nline B\nline C2\n")
    return _commit_all(clone, SCHEDULED)


# ---------------------------------------------------------------------------
# replay_onto_origin
# ---------------------------------------------------------------------------

def test_generated_outputs_are_taken_whole_and_origins_other_files_kept(repos):
    clone, ci = repos
    ci_sha = _ci_rewrites_the_same_outputs(ci)
    local_sha = _local_refresh(clone)
    assert _git(clone, "push", "origin", "main", check=False).returncode != 0, (
        "the race must be real: origin moved under the run")
    log = io.StringIO()
    ok, detail = replay_onto_origin(log, clone)
    assert ok, detail
    # Every generated output is the run's version, whole - never a merge of
    # two regenerated versions.
    assert _read(clone, "data/live_track.json") == \
        '{"anchor_date": "2026-09-29", "live_dates": []}\n'
    assert _read(clone, "docs/index.html") == "page: local\n"
    assert _read(clone, "build/portfolio.html") == "portfolio: local\n"
    assert _read(clone, "data/strategy_freshness.json") == '{"all_current": true}\n'
    assert not (clone / "data" / "retired.json").exists()
    # What the run did not touch is origin's.
    assert _read(clone, "docs/scanner.html") == "scanner: ci\n"
    assert (clone / "data" / "scanner_latest.json").exists()
    # One commit with the run's message on top of the moved origin, a clean
    # tree, and the push now lands.
    assert _subject(clone) == SCHEDULED
    assert _sha(clone, "HEAD~1") == ci_sha
    assert _sha(clone) != local_sha
    assert _clean(clone)
    assert "replayed 1 of 1" in detail
    _push(clone)
    assert _origin_tip(ci) == _sha(clone)


def test_template_merges_a_concurrent_hand_edit_three_way(repos):
    """template.html is the one output a person edits; the run's literal
    update and a hand edit elsewhere in the file must both survive."""
    clone, ci = repos
    _write(ci, "template.html", "line A (hand edit)\nline B\nline C\n")
    _commit_all(ci, "Tweak the template by hand")
    _push(ci)
    _local_refresh(clone)
    log = io.StringIO()
    ok, detail = replay_onto_origin(log, clone)
    assert ok, detail
    assert _read(clone, "template.html") == "line A (hand edit)\nline B\nline C2\n"
    assert "WARN" not in detail
    assert _clean(clone)


def test_a_template_collision_takes_the_runs_version_and_says_so(repos):
    clone, ci = repos
    _write(ci, "template.html", "line A\nline B (hand)\nline C\n")
    _commit_all(ci, "Hand edit on the same line")
    _push(ci)
    _write(clone, "template.html", "line A\nline B (literal)\nline C\n")
    _commit_all(clone, SCHEDULED)
    log = io.StringIO()
    ok, detail = replay_onto_origin(log, clone)
    assert ok, detail
    assert _read(clone, "template.html") == "line A\nline B (literal)\nline C\n"
    assert "<<<<<<<" not in _read(clone, "template.html")
    assert "WARN" in detail and "template.html" in detail
    assert _clean(clone)


def test_nothing_to_replay_when_origin_has_not_moved(repos):
    clone, _ = repos
    sha = _local_refresh(clone)
    ok, detail = replay_onto_origin(io.StringIO(), clone)
    assert ok and "has not moved" in detail
    assert _sha(clone) == sha


def test_a_commit_origin_already_carries_is_dropped_not_duplicated(repos):
    """An operator who pushed the clone's work by hand must not get it twice."""
    clone, ci = repos
    _write(ci, "docs/index.html", "page: same\n")
    _commit_all(ci, "Operator pushed the same page from the main tree")
    _push(ci)
    _write(clone, "docs/index.html", "page: same\n")
    _commit_all(clone, SCHEDULED)
    ok, detail = replay_onto_origin(io.StringIO(), clone)
    assert ok and "replayed 0 of 1" in detail
    assert _sha(clone) == _sha(clone, "origin/main")
    assert _clean(clone)


def test_a_failed_replay_puts_the_clone_back_as_it_was(repos, monkeypatch):
    clone, ci = repos
    _ci_rewrites_the_same_outputs(ci)
    local_sha = _local_refresh(clone)
    real = _sr._git

    def refusing_hook(args, log, cwd=_sr.REPO_ROOT):
        if args[0] == "commit":
            return subprocess.CompletedProcess(args, 1, "", "simulated hook refusal")
        return real(args, log, cwd=cwd)

    monkeypatch.setattr(_sr, "_git", refusing_hook)
    ok, detail = replay_onto_origin(io.StringIO(), clone)
    assert not ok and "undone" in detail
    assert _sha(clone) == local_sha, "the run's commit must survive a failed replay"
    assert _clean(clone)
    assert not _mid_operation(clone)


# ---------------------------------------------------------------------------
# reconcile_unpushed_commits — the preflight half
# ---------------------------------------------------------------------------

def test_the_preflight_publishes_an_earlier_firings_unpushed_commit(repos):
    clone, ci = repos
    _ci_rewrites_the_same_outputs(ci)
    _local_refresh(clone)        # committed, never pushed: the exit-5 state
    log = io.StringIO()
    ok, detail, n = reconcile_unpushed_commits(log, clone, push=True)
    assert ok, detail
    assert n == 1 and "published 1" in detail
    assert _origin_tip(ci) == _sha(clone)
    assert _subject(clone) == SCHEDULED
    assert _read(clone, "docs/scanner.html") == "scanner: ci\n"
    assert _clean(clone)


def test_an_unarmed_preflight_replays_but_publishes_nothing(repos):
    clone, ci = repos
    _ci_rewrites_the_same_outputs(ci)
    _local_refresh(clone)
    ok, detail, n = reconcile_unpushed_commits(io.StringIO(), clone, push=False)
    assert ok and n == 1 and "not pushed" in detail
    tip = _origin_tip(ci)
    assert _sha(clone, "HEAD~1") == tip, "on top of origin, ready for a hand push"
    assert _sha(clone) != tip


def test_the_preflight_refuses_a_commit_it_did_not_write(repos):
    clone, _ = repos
    _write(clone, "template.html", "a person's work\n")
    sha = _commit_all(clone, "WIP by hand in the automation clone")
    ok, detail, n = reconcile_unpushed_commits(io.StringIO(), clone, push=True)
    assert not ok and n == 1 and "not written by this script" in detail
    assert _sha(clone) == sha
    assert _clean(clone)


def test_the_preflight_with_nothing_unpushed_is_a_no_op(repos):
    clone, _ = repos
    ok, detail, n = reconcile_unpushed_commits(io.StringIO(), clone, push=True)
    assert ok and n == 0 and "no unpushed" in detail


# ---------------------------------------------------------------------------
# abort_stuck_operation — the old retry's failure mode, undone
# ---------------------------------------------------------------------------

def test_a_rebase_an_earlier_firing_left_in_progress_is_aborted(repos):
    clone, ci = repos
    _write(ci, "template.html", "line A\nline B (hand)\nline C\n")
    _commit_all(ci, "hand")
    _push(ci)
    _write(clone, "template.html", "line A\nline B (literal)\nline C\n")
    sha = _commit_all(clone, SCHEDULED)
    pull = _git(clone, "pull", "--rebase", "origin", "main", check=False)
    assert pull.returncode != 0 and _mid_operation(clone), (
        "the fixture must reproduce the wedge: a conflicted rebase in progress")
    assert not _clean(clone)
    log = io.StringIO()
    assert abort_stuck_operation(log, clone) == ["rebase aborted"]
    assert not _mid_operation(clone)
    assert _clean(clone)
    assert _sha(clone) == sha, "aborting must give the run's commit back"
    assert abort_stuck_operation(log, clone) == []


# ---------------------------------------------------------------------------
# Contracts and wiring
# ---------------------------------------------------------------------------

def test_only_this_scripts_subjects_count_as_recoverable():
    for cadence in ("weekend", "post-fill"):
        assert is_scheduled_commit_subject(
            scheduled_commit_message(date(2026, 9, 30), date(2026, 9, 29), cadence))
    assert not is_scheduled_commit_subject(CI_SUBJECT)
    assert not is_scheduled_commit_subject("WIP by hand in the automation clone")
    assert not is_scheduled_commit_subject("Restate the core book (scheduled)")


def test_main_recovers_before_the_clean_tree_check_and_the_pull():
    """The order is the contract: abort a stuck operation, THEN judge the
    tree, THEN publish what an earlier firing could not, THEN pull. The
    script's own bytes are captured before any of it so the re-exec guard
    still sees a recovery that rewrote this file."""
    src = inspect.getsource(_sr.main)
    captured = src.index("_self_before = ")
    abort = src.index("abort_stuck_operation(")
    clean = src.index('"status", "--porcelain"')
    reconcile = src.index("reconcile_unpushed_commits(")
    pull = src.index('"pull", "--rebase"')
    assert captured < abort < clean < reconcile < pull
    assert "push=args.push" in src[reconcile:reconcile + 200], (
        "only an armed run may publish an earlier firing's commits")
