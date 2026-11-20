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


@pytest.fixture
def crash_reporting(tmp_path, monkeypatch):
    """cli()'s exception path pointed at the throwaway clone: git status,
    the run log and the run ledger all resolve through REPO_ROOT / LOG_DIR,
    and the email is captured rather than sent."""
    sent = []
    monkeypatch.setattr(sr, "_email",
                        lambda subject, body, log: sent.append((subject, body)))
    monkeypatch.setattr(sr, "log_path_for", lambda now: sr.LOG_DIR / "run.log")
    return sent


def test_the_marker_is_released_on_return_and_kept_on_a_crash(
        tmp_path, monkeypatch, crash_reporting):
    """A crash that left OUTPUTS on disk keeps its marker. Since 2026-11-03
    the crash also leaves the tree dirty here, because cli() now releases the
    marker of a crash whose tree is clean (test below)."""
    repo = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "REPO_ROOT", repo)

    def returns(argv):
        sr.claim_in_flight(io.StringIO(), repo, cadence="weekend", component="auto")
        return 3

    def crashes(argv):
        sr.claim_in_flight(io.StringIO(), repo, cadence="weekend", component="auto")
        _killed_run_debris(repo)
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


# ---------------------------------------------------------------------------
# Adversarial review of the recovery, 2026-11-02 (items 3, 4, 5 and 2).
# ---------------------------------------------------------------------------

def test_restore_clears_outputs_a_run_had_already_staged(tmp_path, monkeypatch):
    """Item 3. A run stopped between `git add` and `git commit` leaves its
    outputs STAGED. `git checkout -- <path>` restores from the index, so it
    left every `M ` entry in place and each later firing refused; a file the
    run created stays `A ` unless the checkout is --no-overlay."""
    repo = _git_repo(tmp_path, monkeypatch)
    _killed_run_debris(repo)
    _git(repo, "add", "data/", "docs/", "build/portfolio.html", "template.html")
    staged = _porcelain(repo)
    assert "M  data/breadth_iuis.json" in staged
    assert "A  data/constituents_new.json" in staged
    assert sr.restore_tracked_outputs(io.StringIO(), repo) is True
    assert _porcelain(repo).strip() == ""
    assert (repo / "data" / "breadth_iuis.json").read_text(encoding="utf-8") == "old"
    assert not (repo / "data" / "constituents_new.json").exists()


def test_restore_tolerates_a_path_absent_from_head(tmp_path, monkeypatch):
    """build/portfolio.html need not exist in every tree: its checkout fails
    on its own and the other paths are still restored."""
    repo = _git_repo(tmp_path, monkeypatch)
    _git(repo, "rm", "-q", "build/portfolio.html")
    _git(repo, "commit", "-q", "-m", "no portfolio page")
    _killed_run_debris(repo)
    _git(repo, "add", "data/")
    log = io.StringIO()
    assert sr.restore_tracked_outputs(log, repo) is True
    assert _porcelain(repo).strip() == ""
    assert "build/portfolio.html" in log.getvalue(), "the failed call is logged"


def test_restore_after_a_landed_commit_is_a_no_op(tmp_path, monkeypatch):
    """Item 4, the push-failure call sites: the run's commit is in place and
    the tree is clean, so the restore must neither touch it nor move HEAD."""
    repo = _git_repo(tmp_path, monkeypatch)
    _killed_run_debris(repo)
    _git(repo, "add", "data/", "docs/")
    _git(repo, "commit", "-q", "-m", "Local weekly refresh (scheduled)")
    head = _git(repo, "rev-parse", "HEAD")
    log = io.StringIO()
    assert sr.restore_tracked_outputs(log, repo) is True
    assert "already clean" in log.getvalue()
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / "data" / "breadth_iuis.json").read_text(encoding="utf-8") == "new"


def _drive_to_commit(tmp_path, monkeypatch, repo, *, git_fails=None,
                     durable=False):
    """Run the real main() on the throwaway clone as far as the --commit
    step. The refresh child writes the 2026-10-27 debris and exits 0; the
    anchor, roster and gate checks are stubbed green. ``git_fails`` names a
    git subcommand that returns 1 as it would on an index.lock or a hook.

    Every git call is forced into the clone whatever cwd it asks for:
    _git's default cwd and restore_tracked_outputs' default repo_root were
    bound to the REAL repository at import, and a stray restore there would
    discard a working checkout."""
    real_run = subprocess.run
    calls = {"refresh": 0}

    def git_in_clone(args, log, cwd=None):
        if args[0] == "pull":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == git_fails:
            log.write(f"\n$ git {' '.join(args)}\nfatal: fixture failure\n")
            return subprocess.CompletedProcess(args, 1, "", "fatal: fixture failure")
        cp = real_run(["git", *args], cwd=repo, capture_output=True, text=True)
        log.write(f"\n$ git {' '.join(args)}\n{cp.stdout}{cp.stderr}")
        return cp

    def run(cmd, **kw):
        if cmd and cmd[0] == "git":
            return real_run(cmd, **kw)
        assert any("refresh_all.py" in str(c) for c in cmd), cmd
        calls["refresh"] += 1
        _killed_run_debris(repo)
        return subprocess.CompletedProcess(cmd, 0)

    panel = tmp_path / "panel.json"
    panel.write_text('{"end_date": "2026-10-30"}', encoding="utf-8")
    sent = []
    monkeypatch.setattr(sr, "_durable_state_enabled", lambda: durable)
    monkeypatch.setattr(sr, "REPO_ROOT", repo)
    monkeypatch.setattr(sr, "PANEL", panel)
    monkeypatch.setattr(sr, "GREEN_MARKER", tmp_path / "last_green_run.json")
    monkeypatch.setattr(sr, "log_path_for", lambda now: tmp_path / "run.log")
    monkeypatch.setattr(sr, "_git", git_in_clone)
    monkeypatch.setattr(sr.subprocess, "run", run)
    monkeypatch.setattr(sr, "price_source_preflight", lambda s: (True, "fixture"))
    monkeypatch.setattr(sr, "reconcile_unpushed_commits",
                        lambda log, **kw: (True, "fixture: none", 0))
    monkeypatch.setattr(sr.publication_debt, "current_debt", lambda *a, **kw: None)
    monkeypatch.setattr(sr, "panel_is_current", lambda end, now: True)
    monkeypatch.setattr(sr, "roster_integrity", lambda root: {"ok": True})
    monkeypatch.setattr(sr, "build_gate_report", lambda *a, **kw: {"detail": "fixture"})
    monkeypatch.setattr(sr, "refusal_report", lambda *a, **kw: "")
    monkeypatch.setattr(sr, "_email",
                        lambda subject, body, log: sent.append((subject, body)))
    rc = sr.main(["--component", "all", "--commit"])
    return rc, calls["refresh"], sent, (tmp_path / "run.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("step", ["add", "commit"])
def test_a_failed_add_or_commit_restores_the_clone(tmp_path, monkeypatch, step):
    """Item 4. Exit 5 from `git add` (or from `git commit` after the add
    staged everything) used to leave this run's outputs on disk while cli()
    released the marker, so nothing could ever tell them from a person's
    work. Exit 5 now restores, and the tree is clean for the next firing."""
    repo = _git_repo(tmp_path, monkeypatch)
    rc, refreshed, sent, log = _drive_to_commit(tmp_path, monkeypatch, repo,
                                                git_fails=step)
    assert refreshed == 1
    assert rc == 5
    assert _porcelain(repo).strip() == "", log
    assert (repo / "data" / "breadth_iuis.json").read_text(encoding="utf-8") == "old"
    assert not (repo / "data" / "constituents_new.json").exists()
    assert "discarding the failed run's outputs" in log
    assert sent and sent[0][0] == f"[FAIL] Scheduled refresh - git {step} failed"


def test_the_commit_branch_still_commits_when_git_works(tmp_path, monkeypatch):
    """The harness above is a real run: with no failure injected it commits."""
    repo = _git_repo(tmp_path, monkeypatch)
    rc, _, _, log = _drive_to_commit(tmp_path, monkeypatch, repo)
    assert rc == 0, log
    assert _porcelain(repo).strip() == ""
    assert "(scheduled)" in _git(repo, "log", "-1", "--format=%s")


def test_a_crash_on_a_clean_tree_releases_the_marker_and_reports(
        tmp_path, monkeypatch, crash_reporting):
    """Item 5. An exception out of main() sent no email and wrote no run
    record. It now does both, writes the traceback to the run's log, and
    releases the marker because a clean tree leaves nothing to restore."""
    repo = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "REPO_ROOT", repo)

    def crashes(argv):
        sr.claim_in_flight(io.StringIO(), repo, cadence="post-fill", component="auto")
        raise RuntimeError("boom before any write")

    monkeypatch.setattr(sr, "main", crashes)
    with pytest.raises(RuntimeError, match="boom before any write"):
        sr.cli(["--cadence", "post-fill", "--push"])
    assert not sr.in_flight_marker_path().exists()
    assert [s for s, _ in crash_reporting] == \
        ["[FAIL] Scheduled refresh - unhandled exception"]
    assert "RuntimeError: boom before any write" in crash_reporting[0][1]
    log = (repo / "logs" / "run.log").read_text(encoding="utf-8")
    assert "UNHANDLED EXCEPTION" in log and "Traceback" in log
    assert "marker released" in log
    ledger = sr.run_status.run_ledger_path(repo)
    rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["exit_code"] == 1 and rec["cadence"] == "post-fill"
    assert "RuntimeError" in rec["outcome"]


def test_a_crash_on_a_dirty_tree_reports_and_keeps_the_marker(
        tmp_path, monkeypatch, crash_reporting):
    repo = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "REPO_ROOT", repo)

    def crashes(argv):
        sr.claim_in_flight(io.StringIO(), repo, cadence="weekend", component="auto")
        _killed_run_debris(repo)
        raise RuntimeError("boom mid-write")

    monkeypatch.setattr(sr, "main", crashes)
    with pytest.raises(RuntimeError):
        sr.cli([])
    assert sr.in_flight_marker_path().exists()
    assert crash_reporting and "marker is kept" in crash_reporting[0][1]
    assert "marker KEPT" in (repo / "logs" / "run.log").read_text(encoding="utf-8")


def test_the_crash_report_never_replaces_the_original_exception(
        tmp_path, monkeypatch):
    """Everything in the handler is best-effort: an alert channel that
    raises must not turn the run's traceback into one about the alert."""
    repo = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "REPO_ROOT", repo)
    monkeypatch.setattr(sr, "log_path_for", lambda now: sr.LOG_DIR / "run.log")

    def broken_email(*a, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr(sr, "_email", broken_email)
    monkeypatch.setattr(sr, "main", lambda argv: (_ for _ in ()).throw(KeyError("orig")))
    with pytest.raises(KeyError, match="orig"):
        sr.cli([])


def test_help_is_not_reported_as_a_failure(tmp_path, monkeypatch, crash_reporting):
    repo = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "REPO_ROOT", repo)
    with pytest.raises(SystemExit) as exc:
        sr.cli(["--help"])
    assert exc.value.code == 0
    assert crash_reporting == []


def test_a_live_foreign_marker_refuses_the_claim(tmp_path, monkeypatch):
    """Item 2. A second live run (a delayed weekend firing still going when
    the Tuesday task fires) used to overwrite the first run's marker; if the
    second was then killed, the next firing restored over the FIRST run's
    live outputs."""
    repo = _git_repo(tmp_path, monkeypatch)
    path = _dead_marker(repo, identity="111", pid=999_999)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(sr, "_process_identity",
                        lambda pid: "111" if pid == 999_999 else "me")
    log = io.StringIO()
    assert sr.claim_in_flight(log, repo, cadence="post-fill", component="auto") is False
    assert path.read_text(encoding="utf-8") == before
    assert "REFUSED" in log.getvalue() and "still in flight" in log.getvalue()


def test_an_uninspectable_marker_refuses_the_claim(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)
    path = _dead_marker(repo, pid=999_999)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(sr, "_process_identity",
                        lambda pid: sr._UNINSPECTABLE if pid == 999_999 else "me")
    assert sr.claim_in_flight(io.StringIO(), repo, cadence="weekend",
                              component="auto") is False
    assert path.read_text(encoding="utf-8") == before


def test_the_waiting_parents_marker_is_taken_over_by_its_child(tmp_path, monkeypatch):
    """The weekend auto parent and the re-exec parent claim, then wait for a
    child that claims in turn. The parent is alive with its recorded
    identity, and the child must take over rather than refuse."""
    repo = _git_repo(tmp_path, monkeypatch)
    parent = __import__("os").getppid()
    _dead_marker(repo, pid=parent, identity=sr._process_identity(parent))
    monkeypatch.delenv(sr.IN_FLIGHT_PARENT_ENV, raising=False)
    assert sr.claim_in_flight(io.StringIO(), repo, cadence="weekend",
                              component="core") is True
    record = json.loads(sr.in_flight_marker_path().read_text(encoding="utf-8"))
    assert record["pid"] == __import__("os").getpid()
    assert record["component"] == "core"


def test_the_declared_parent_is_taken_over_through_a_venv_launcher(
        tmp_path, monkeypatch):
    """On Windows a venv python.exe is a launcher, so the child's getppid()
    is the launcher, not the parent that holds the marker. The parent's pid
    travels in IN_FLIGHT_PARENT_ENV instead."""
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo, pid=424_242, identity="parent")
    monkeypatch.setattr(sr, "_process_identity",
                        lambda pid: "parent" if pid == 424_242 else "me")
    monkeypatch.setenv(sr.IN_FLIGHT_PARENT_ENV, "424242")
    assert sr.claim_in_flight(io.StringIO(), repo, cadence="weekend",
                              component="europe") is True
    record = json.loads(sr.in_flight_marker_path().read_text(encoding="utf-8"))
    assert record["pid"] == __import__("os").getpid()


def test_the_children_are_told_who_their_parent_is():
    src = inspect.getsource(sr.main)
    run_child = src[src.index("def run_child("):src.index("return component_sequence(")]
    assert "IN_FLIGHT_PARENT_ENV" in run_child and "env=env" in run_child
    reexec = src[src.index('BTE_SCHED_REEXEC="1"'):src.index("return child.returncode")]
    assert "IN_FLIGHT_PARENT_ENV" in reexec and "env=env" in reexec


@pytest.mark.parametrize("identity", [None, "222"], ids=["gone", "pid-reused"])
def test_a_dead_marker_is_overwritten(tmp_path, monkeypatch, identity):
    repo = _git_repo(tmp_path, monkeypatch)
    _dead_marker(repo, identity="111", pid=999_999)
    monkeypatch.setattr(sr, "_process_identity",
                        lambda pid: identity if pid == 999_999 else "me")
    log = io.StringIO()
    assert sr.claim_in_flight(log, repo, cadence="weekend", component="auto") is True
    record = json.loads(sr.in_flight_marker_path().read_text(encoding="utf-8"))
    assert record["pid"] == __import__("os").getpid()
    assert "is gone; overwriting" in log.getvalue()


def test_a_marker_that_cannot_be_written_is_still_a_claim(tmp_path, monkeypatch):
    """The write is best-effort; only a REFUSAL refuses."""
    repo = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "in_flight_marker_path",
                        lambda: repo / "missing" / "\0bad" / "m.json")
    log = io.StringIO()
    assert sr.claim_in_flight(log, repo, cadence="weekend", component="auto") is True
    assert "could not be written" in log.getvalue()


def test_a_refused_claim_refuses_the_run_without_restoring(tmp_path, monkeypatch):
    """The wiring in main(): exit 2, the refresh never starts, the other
    run's marker and outputs are untouched."""
    repo = _git_repo(tmp_path, monkeypatch)
    path = _dead_marker(repo, identity="111", pid=999_999)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(sr, "_process_identity",
                        lambda pid: "111" if pid == 999_999 else "me")
    rc, refreshed, sent, log = _drive_to_commit(tmp_path, monkeypatch, repo,
                                                durable=True)
    assert rc == 2
    assert refreshed == 0, "a refused run must not start the refresh"
    assert 2 not in sr.RESTORE_ON_EXIT_CODES
    assert path.read_text(encoding="utf-8") == before
    assert sent and sent[0][0] == "[FAIL] Scheduled refresh - another refresh is in flight"
    assert "999999" in sent[0][1], "the refusal names the holder"
