"""A run killed from outside (2026-11-01).

On 2026-10-27 the machine restarted sixteen minutes into the 09:27 post-fill
firing. The killed run had rewritten eight panels, and the clean-tree
preflight then refused 33 firings over five days as a person's work. These
pin the in-flight marker that lets a later firing tell a dead run's debris
from a person's edits, and the conditions under which it may discard it.
"""

from __future__ import annotations

import inspect
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scheduled_refresh as sr  # noqa: E402


def _git_repo(tmp_path, monkeypatch):
    """A throwaway clone with tracked outputs, an ignored logs/ and a base
    commit; isolated git config (see test_scheduled_refresh._git_repo)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t.test\n"
                   "[core]\n\tlongpaths = true\n\tautocrlf = false\n",
                   encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "clone"
    for d in ("data", "docs", "build", "scripts"):
        (repo / d).mkdir(parents=True)
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
    (repo / "data" / "breadth_iuis.json").write_text("old", encoding="utf-8")
    (repo / "docs" / "index.html").write_text("old", encoding="utf-8")
    (repo / "build" / "portfolio.html").write_text("old", encoding="utf-8")
    (repo / "template.html").write_text("old", encoding="utf-8")
    (repo / "scripts" / "tool.py").write_text("old", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    monkeypatch.setattr(sr, "LOG_DIR", repo / "logs")
    monkeypatch.setattr(sr, "_durable_state_enabled", lambda: True)
    return repo


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


def _porcelain(repo):
    return _git(repo, "status", "--porcelain")


def _dead_marker(repo, *, head=None, identity="111", pid=999_999):
    """The marker a killed run leaves: its process no longer exists."""
    path = sr.in_flight_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": pid, "identity": identity,
        "head": head or _git(repo, "rev-parse", "HEAD").strip(),
        "cadence": "post-fill", "component": "auto",
        "started_utc": "2026-10-27T01:27:32+00:00"}), encoding="utf-8")
    return path


def _killed_run_debris(repo):
    """What the 2026-10-27 09:27 firing left: rewritten panels, a new file."""
    (repo / "data" / "breadth_iuis.json").write_text("new", encoding="utf-8")
    (repo / "docs" / "index.html").write_text("new", encoding="utf-8")
    (repo / "data" / "constituents_new.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def no_such_process(monkeypatch):
    monkeypatch.setattr(sr, "_process_identity", lambda pid: None)


def test_a_dead_runs_outputs_are_restored(tmp_path, monkeypatch, no_such_process):
    repo = _git_repo(tmp_path, monkeypatch)
    marker = _dead_marker(repo)
    _killed_run_debris(repo)
    log = io.StringIO()
    ok, why = sr.recover_interrupted_run(log, _porcelain(repo), repo)
    assert ok, why
    assert _porcelain(repo).strip() == ""
    assert (repo / "data" / "breadth_iuis.json").read_text(encoding="utf-8") == "old"
    assert not (repo / "data" / "constituents_new.json").exists()
    assert not marker.exists(), "a spent marker must not authorise a second restore"
    assert "breadth_iuis.json" in log.getvalue(), "what was discarded must be logged"
    assert "pid 999999" in why


def test_a_reused_pid_is_still_a_dead_run(tmp_path, monkeypatch):
    """After a restart the pid may belong to something else; its creation
    time differs from the one the marker recorded."""
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo, identity="111")
    monkeypatch.setattr(sr, "_process_identity", lambda pid: "222")
    _killed_run_debris(repo)
    ok, why = sr.recover_interrupted_run(io.StringIO(), _porcelain(repo), repo)
    assert ok, why


def test_a_live_run_is_never_restored_over(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo, identity="111")
    monkeypatch.setattr(sr, "_process_identity", lambda pid: "111")
    _killed_run_debris(repo)
    before = _porcelain(repo)
    ok, why = sr.recover_interrupted_run(io.StringIO(), before, repo)
    assert not ok and "still in flight" in why
    assert _porcelain(repo) == before


def test_a_process_that_cannot_be_inspected_counts_as_alive(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo)
    monkeypatch.setattr(sr, "_process_identity", lambda pid: sr._UNINSPECTABLE)
    _killed_run_debris(repo)
    ok, why = sr.recover_interrupted_run(io.StringIO(), _porcelain(repo), repo)
    assert not ok and "cannot be inspected" in why


def test_no_marker_means_a_persons_work(tmp_path, monkeypatch, no_such_process):
    repo = _git_repo(tmp_path, monkeypatch)
    _killed_run_debris(repo)
    before = _porcelain(repo)
    ok, why = sr.recover_interrupted_run(io.StringIO(), before, repo)
    assert not ok and "no in-flight marker" in why
    assert _porcelain(repo) == before


def test_a_commit_on_top_of_the_debris_refuses(tmp_path, monkeypatch, no_such_process):
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo)
    (repo / "scripts" / "tool.py").write_text("person", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "a person's commit")
    _killed_run_debris(repo)
    before = _porcelain(repo)
    ok, why = sr.recover_interrupted_run(io.StringIO(), before, repo)
    assert not ok and "HEAD moved" in why
    assert _porcelain(repo) == before


@pytest.mark.parametrize("path", ["template.html", "scripts/tool.py"])
def test_a_path_the_refresh_does_not_write_refuses_everything(
        tmp_path, monkeypatch, no_such_process, path):
    """template.html is restored after a FAILED run, but a dead run's marker
    says nothing about who edited it, so here it blocks the restore."""
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo)
    _killed_run_debris(repo)
    (repo / path).write_text("person", encoding="utf-8")
    before = _porcelain(repo)
    ok, why = sr.recover_interrupted_run(io.StringIO(), before, repo)
    assert not ok and path in why
    assert _porcelain(repo) == before, "nothing is discarded when one path is foreign"


def test_a_rename_refuses(tmp_path, monkeypatch, no_such_process):
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo)
    _git(repo, "mv", "data/breadth_iuis.json", "data/breadth_moved.json")
    ok, why = sr.recover_interrupted_run(io.StringIO(), _porcelain(repo), repo)
    assert not ok and "rename" in why


def test_claim_and_release_round_trip(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)
    sr.claim_in_flight(io.StringIO(), repo, cadence="weekend", component="auto")
    record = json.loads(sr.in_flight_marker_path().read_text(encoding="utf-8"))
    assert record["head"] == _git(repo, "rev-parse", "HEAD").strip()
    assert record["identity"] == sr._process_identity(record["pid"])
    assert _porcelain(repo).strip() == "", "the marker must never dirty the tree"
    sr.release_in_flight()
    assert not sr.in_flight_marker_path().exists()


def test_release_leaves_another_processes_marker(tmp_path, monkeypatch):
    """A waited child claims after its parent; the parent must not remove
    the child's marker, nor a later run's."""
    repo = _git_repo(tmp_path, monkeypatch)
    path = _dead_marker(repo, pid=999_999)
    sr.release_in_flight()
    assert path.exists()


def test_under_pytest_nothing_is_claimed_or_restored(tmp_path, monkeypatch):
    """The refresh runs the suite inside the clone; a test driving main()
    against the real REPO_ROOT must not overwrite the live run's marker."""
    repo = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "_durable_state_enabled", lambda: False)
    sr.claim_in_flight(io.StringIO(), repo, cadence="weekend", component="auto")
    assert not sr.in_flight_marker_path().exists()
    ok, why = sr.recover_interrupted_run(io.StringIO(), " M data/x.json\n", repo)
    assert not ok and "pytest" in why


def test_process_identity_tells_a_live_process_from_a_finished_one():
    me = sr._process_identity(__import__("os").getpid())
    assert me not in (None, sr._UNINSPECTABLE)
    assert sr._process_identity(__import__("os").getpid()) == me, "must be stable"
    child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                           capture_output=True, text=True, check=True)
    assert sr._process_identity(int(child.stdout)) is None
    assert sr._process_identity(0) is None


def test_the_preflight_consults_the_marker_before_refusing():
    src = inspect.getsource(sr.main)
    preflight = src[src.index('_git(["status", "--porcelain"], log)'):
                    src.index("reconcile_unpushed_commits(")]
    assert preflight.index("recover_interrupted_run(") < \
        preflight.index('"working tree not clean"')
    # Claimed only once the run is past the preflight and any re-exec, so a
    # refusal or a parent handing over to its re-exec child claims nothing.
    assert src.index("claim_in_flight") > src.index("BTE_SCHED_REEXEC")


def test_the_marker_is_released_on_return_and_kept_on_a_crash(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)

    def returns(argv):
        sr.claim_in_flight(io.StringIO(), repo, cadence="weekend", component="auto")
        return 3

    def crashes(argv):
        sr.claim_in_flight(io.StringIO(), repo, cadence="weekend", component="auto")
        raise RuntimeError("killed mid-run")

    monkeypatch.setattr(sr, "main", returns)
    assert sr.cli([]) == 3
    assert not sr.in_flight_marker_path().exists()
    monkeypatch.setattr(sr, "main", crashes)
    with pytest.raises(RuntimeError):
        sr.cli([])
    assert sr.in_flight_marker_path().exists(), \
        "a crashed run's marker is what lets the next firing restore its debris"
    src = Path(sr.__file__).read_text(encoding="utf-8")
    assert src.rstrip().endswith("raise SystemExit(cli())")
