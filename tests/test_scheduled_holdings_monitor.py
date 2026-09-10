"""Tests for scripts/scheduled_holdings_monitor.py: the restore that keeps a
failed capture from stalling the next firing.

THE STALL (2026-09-09 to 09-10). The capture writes its snapshots and rewrites
both payloads before the guard reads them. On 2026-09-09 yfinance returned
nothing for 136 of 166 names, G5 failed on price coverage — the guard doing its
job — and the run published nothing. But the two unpriced payloads and two new
snapshots were already in the tree, so the 2026-09-10 firing refused at its own
preflight ("monitor-owned paths are dirty before the run"), as every firing
after it would have. Same class as the 2026-08-28 to 09-02 stall, cleared by
hand at 0ea4208; different trigger.

The wrapper now records the owned paths' state after the preflight and, on a
failure inside the capture, restores exactly what the run itself dirtied. In a
SHARED working tree three properties matter, and these tests pin them: the
restore touches only the run's own dirt; a pre-existing dirty owned path still
refuses at preflight and is never restored over; a passing run still publishes
only the owned paths. Throwaway git repositories with isolated config, in the
style of test_scheduled_refresh.py.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts import scheduled_holdings_monitor as shm
from scripts.scheduled_holdings_monitor import (
    OWNED,
    OwnedState,
    StepFailed,
    owned_dirt,
    preflight,
    record_owned_state,
    restore_owned_paths,
)

LATEST = "data/holdings_monitor_latest.json"
SERIES = "docs/holdings-monitor-series.json"
PAGE = "docs/holdings-monitor.html"
OLD_ARKG = "data/holdings_monitor/ARKG/2026-09-04.json"
OLD_XBI = "data/holdings_monitor/XBI/2026-09-03.json"
NEW_ARKG = "data/holdings_monitor/ARKG/2026-09-08.json"
NEW_XBI = "data/holdings_monitor/XBI/2026-09-04.json"
CACHE = "data/holdings_monitor_prices.parquet"      # gitignored, as in the real tree
UNRELATED_TRACKED = "README.md"
UNRELATED_UNTRACKED = "reviews/2026-09-10_session-record.docx"

BASE_LATEST = '{"built_at_utc": "2026-09-08T01:18:10+00:00"}'
BASE_SERIES = '{"dates": ["2026-09-08"]}'
BASE_PAGE = "<html>2026-09-08</html>"
BASE_OLD_ARKG = '{"as_of": "2026-09-04"}'
BASE_OLD_XBI = '{"as_of": "2026-09-03"}'

# What a shared tree looks like once an interactive session is mid-edit.
OTHERS_DIRT = [" M README.md", "?? reviews/2026-09-10_session-record.docx"]


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True)


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _read(repo, rel):
    return (repo / rel).read_text(encoding="utf-8")


def _porcelain(repo):
    return sorted(_git(repo, "status", "--porcelain",
                       "--untracked-files=all").stdout.splitlines())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway tree shaped like the real one — the four owned paths
    committed with a prior snapshot per fund, an ignored price cache, an
    unrelated tracked file — plus a bare origin so the preflight pull and
    the armed push have somewhere to go. Isolated git config, core.longpaths
    for the Windows tmp path, autocrlf off so byte comparisons are exact."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t.test\n"
                   "[core]\n\tlongpaths = true\n\tautocrlf = false\n",
                   encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    tree = tmp_path / "tree"
    tree.mkdir()
    _git(tree, "init", "-q", "-b", "main")
    _write(tree, ".gitignore", "*.parquet\nlogs/\n")
    _write(tree, LATEST, BASE_LATEST)
    _write(tree, SERIES, BASE_SERIES)
    _write(tree, PAGE, BASE_PAGE)
    _write(tree, OLD_ARKG, BASE_OLD_ARKG)
    _write(tree, OLD_XBI, BASE_OLD_XBI)
    _write(tree, UNRELATED_TRACKED, "readme")
    _write(tree, CACHE, "cache")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "base")
    _git(tree, "remote", "add", "origin", str(origin))
    _git(tree, "push", "-q", "-u", "origin", "main")
    return tree


def _someone_elses_edits(repo):
    _write(repo, UNRELATED_TRACKED, "readme, edited by a person")
    _write(repo, UNRELATED_UNTRACKED, "docx bytes")


def _what_a_failed_capture_leaves(repo):
    """Exactly what the 2026-09-09 run left: two new snapshots, both payloads
    rewritten with unpriced rows, the ignored cache overwritten. The page is
    untouched because the guard blocks the build."""
    _write(repo, NEW_ARKG, '{"as_of": "2026-09-08"}')
    _write(repo, NEW_XBI, '{"as_of": "2026-09-04"}')
    _write(repo, LATEST,
           '{"built_at_utc": "2026-09-09T02:34:51+00:00", "coverage": 0.25}')
    _write(repo, SERIES, '{"dates": ["2026-09-08", "2026-09-09"]}')
    _write(repo, CACHE, "cache, 30 of 166 columns")


def _assert_pre_run_files_untouched(repo):
    assert _read(repo, OLD_ARKG) == BASE_OLD_ARKG
    assert _read(repo, OLD_XBI) == BASE_OLD_XBI
    assert _read(repo, PAGE) == BASE_PAGE


# ---------------------------------------------------------------------------
# 1. A guard failure restores exactly the run's own dirt
# ---------------------------------------------------------------------------

def test_a_failed_capture_restores_exactly_its_own_dirt(repo):
    _someone_elses_edits(repo)
    before = record_owned_state(repo)
    assert before.dirt == (), "the preflight would have refused otherwise"
    assert {OLD_ARKG, OLD_XBI, LATEST, SERIES, PAGE} <= set(before.files)

    _what_a_failed_capture_leaves(repo)
    lines = []
    assert restore_owned_paths(before, lines.append, repo) is True

    # The run's own dirt is gone...
    assert not (repo / NEW_ARKG).exists()
    assert not (repo / NEW_XBI).exists()
    assert _read(repo, LATEST) == BASE_LATEST
    assert _read(repo, SERIES) == BASE_SERIES
    # ...what existed before the run is untouched...
    _assert_pre_run_files_untouched(repo)
    # ...the ignored cache is not git's to restore and is left alone...
    assert _read(repo, CACHE) == "cache, 30 of 166 columns"
    # ...and the other session's work is exactly as they left it.
    assert _read(repo, UNRELATED_TRACKED) == "readme, edited by a person"
    assert _read(repo, UNRELATED_UNTRACKED) == "docx bytes"
    assert owned_dirt(repo) == []
    assert _porcelain(repo) == OTHERS_DIRT
    # Everything restored is named.
    joined = "\n".join(lines)
    for path in (NEW_ARKG, NEW_XBI):
        assert f"removed {path}" in joined
    for path in (LATEST, SERIES):
        assert f"reverted {path}" in joined
    assert "owned paths clean" in joined


def test_restore_on_clean_owned_paths_is_a_no_op(repo):
    _someone_elses_edits(repo)
    before = record_owned_state(repo)
    lines = []
    assert restore_owned_paths(before, lines.append, repo) is True
    assert "already clean" in "\n".join(lines)
    assert _porcelain(repo) == OTHERS_DIRT


# ---------------------------------------------------------------------------
# 2. A pre-existing dirty owned path still refuses at preflight
# ---------------------------------------------------------------------------

def test_a_pre_existing_dirty_owned_path_still_refuses_at_preflight(repo):
    """The preflight is the guard against a manual run already in flight,
    and the restore must never become a way round it: what was dirty before
    this run started is somebody else's work."""
    _what_a_failed_capture_leaves(repo)       # a manual run's output, uncommitted
    with pytest.raises(StepFailed) as info:
        preflight(lambda _msg: None, repo)
    msg = str(info.value)
    assert "dirty before the run" in msg
    # Named one file at a time, so the operator sees each snapshot.
    for path in (NEW_ARKG, NEW_XBI, LATEST, SERIES):
        assert path in msg
    # And the refusal touched nothing.
    assert (repo / NEW_ARKG).exists() and (repo / NEW_XBI).exists()
    assert "coverage" in _read(repo, LATEST)


def test_restore_never_touches_what_was_dirty_before_the_run(repo):
    """Belt and braces behind the preflight. Even with the owned paths
    already dirty when the state is recorded — a manual run's snapshot
    sitting untracked, a payload mid-edit — the restore acts only on what
    appeared AFTER the record, and reports the rest as still dirty."""
    _write(repo, NEW_ARKG, '{"as_of": "2026-09-08", "by": "a person"}')
    _write(repo, LATEST, '{"by": "a person"}')
    before = record_owned_state(repo)
    assert len(before.dirt) == 2
    assert NEW_ARKG in before.files

    _write(repo, NEW_XBI, '{"as_of": "2026-09-04"}')     # this run's snapshot
    _write(repo, SERIES, '{"by": "this run"}')           # this run's rewrite
    lines = []
    assert restore_owned_paths(before, lines.append, repo) is False
    assert _read(repo, NEW_ARKG) == '{"as_of": "2026-09-08", "by": "a person"}'
    assert _read(repo, LATEST) == '{"by": "a person"}'
    assert not (repo / NEW_XBI).exists()
    assert _read(repo, SERIES) == BASE_SERIES
    _assert_pre_run_files_untouched(repo)
    joined = "\n".join(lines)
    assert "left alone" in joined
    assert NEW_ARKG in joined and LATEST in joined
    assert "STILL dirty" in joined


def test_the_file_record_is_an_independent_guard(repo):
    """Two records taken at the same moment, one question. Should the
    porcelain diff ever miss a pre-existing snapshot, the file listing still
    refuses to remove it."""
    _write(repo, NEW_ARKG, '{"as_of": "2026-09-08"}')
    files = record_owned_state(repo).files
    assert NEW_ARKG in files
    before = OwnedState(dirt=(), files=files)      # the porcelain half blind
    lines = []
    restore_owned_paths(before, lines.append, repo)
    assert (repo / NEW_ARKG).exists()
    assert any("existed before this run started" in ln for ln in lines)


# ---------------------------------------------------------------------------
# 3. End to end through main(): a failing run restores, a passing run
#    publishes only the owned paths
# ---------------------------------------------------------------------------

def _stub_scripts(repo, guard_exit):
    """Stand-ins for the three scripts main() invokes, committed so the tree
    starts clean: the capture writes what the real one writes, the guard
    exits as told, the build writes the page."""
    _write(repo, "scripts/run_holdings_monitor.py", textwrap.dedent(f"""
        from pathlib import Path
        for rel, text in (
            ({NEW_ARKG!r}, '{{"as_of": "2026-09-08"}}'),
            ({NEW_XBI!r}, '{{"as_of": "2026-09-04"}}'),
            ({LATEST!r}, '{{"built_at_utc": "2026-09-11T01:00:20+00:00"}}'),
            ({SERIES!r}, '{{"dates": ["2026-09-08", "2026-09-10"]}}'),
            ({CACHE!r}, "cache, refetched"),
        ):
            Path(rel).parent.mkdir(parents=True, exist_ok=True)
            Path(rel).write_text(text, encoding="utf-8")
        print("[out] stub capture")
        """))
    _write(repo, "scripts/check_holdings_monitor_guard.py", textwrap.dedent(f"""
        import sys
        print("stub guard: exit {guard_exit}")
        sys.exit({guard_exit})
        """))
    _write(repo, "scripts/build_holdings_monitor_page.py", textwrap.dedent(f"""
        from pathlib import Path
        Path({PAGE!r}).write_text("<html>2026-09-10</html>", encoding="utf-8")
        print("Wrote docs/holdings-monitor.html (stub)")
        """))
    _git(repo, "add", "scripts")
    _git(repo, "commit", "-q", "-m", "stubs")
    _git(repo, "push", "-q", "origin", "main")


def _point_the_wrapper_at(repo, monkeypatch):
    logs = repo / "logs"
    monkeypatch.setattr(shm, "ROOT", repo)
    monkeypatch.setattr(shm, "LOG_DIR", logs)
    monkeypatch.setattr(shm, "SENTINEL", logs / "holdings_monitor_last_success.txt")
    return logs


def _log_text(logs):
    files = list(logs.glob("holdings_monitor_*.log"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


def test_main_restores_after_a_guard_failure(repo, monkeypatch):
    """The 2026-09-09 shape, end to end, in a tree with somebody else's
    edits. Exit 1, nothing published, the sentinel untouched — and the
    owned paths clean, so the next firing is a retry."""
    _stub_scripts(repo, guard_exit=1)
    _someone_elses_edits(repo)
    logs = _point_the_wrapper_at(repo, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD").stdout

    assert shm.main(["--push"]) == 1

    assert owned_dirt(repo) == []
    assert _porcelain(repo) == OTHERS_DIRT
    assert _read(repo, LATEST) == BASE_LATEST
    assert _read(repo, SERIES) == BASE_SERIES
    assert not (repo / NEW_ARKG).exists() and not (repo / NEW_XBI).exists()
    _assert_pre_run_files_untouched(repo)
    assert _read(repo, UNRELATED_TRACKED) == "readme, edited by a person"
    assert not (logs / "holdings_monitor_last_success.txt").exists()
    # Nothing was committed and nothing reached origin.
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _git(repo, "rev-parse", "origin/main").stdout == head
    log = _log_text(logs)
    assert "RESULT: FAILED — guard exited 1" in log
    for path in (NEW_ARKG, NEW_XBI):
        assert f"restore: removed {path}" in log
    for path in (LATEST, SERIES):
        assert f"restore: reverted {path} to HEAD" in log
    assert "restore: owned paths clean" in log
    # The stubbed steps ran and the guard blocked the build.
    assert "[out] stub capture" in log and "stub guard: exit 1" in log
    assert "Wrote docs/holdings-monitor.html" not in log


def test_a_passing_armed_run_publishes_only_the_owned_paths(repo, monkeypatch):
    """The other half of the contract, unchanged by the restore: a green run
    commits the owned paths and nothing else, with somebody else's edits
    left in the tree exactly as found, and the sentinel moves."""
    _stub_scripts(repo, guard_exit=0)
    _someone_elses_edits(repo)
    logs = _point_the_wrapper_at(repo, monkeypatch)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert shm.main(["--push"]) == 0

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head != base
    assert _git(repo, "rev-parse", "origin/main").stdout.strip() == head
    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject.startswith("monitor: holdings capture "), (
        "fleet_watch greps this prefix")
    committed = sorted(_git(repo, "show", "--name-only", "--format=",
                            "HEAD").stdout.split())
    assert committed == sorted([NEW_ARKG, NEW_XBI, LATEST, SERIES, PAGE])
    assert _read(repo, PAGE) == "<html>2026-09-10</html>"
    # The other session's edits survive the autostash round trip, uncommitted.
    assert _read(repo, UNRELATED_TRACKED) == "readme, edited by a person"
    assert _porcelain(repo) == OTHERS_DIRT
    assert owned_dirt(repo) == []
    assert (logs / "holdings_monitor_last_success.txt").exists()
    log = _log_text(logs)
    assert "RESULT: OK" in log
    assert "restore:" not in log, "a passing run has nothing to restore"


def test_a_passing_soak_run_leaves_its_outputs_for_the_operator(repo, monkeypatch):
    """Soak mode stops after the build and commits nothing, so a GREEN soak
    run leaves the owned paths dirty on purpose: that is the operator's
    review copy, and clearing it is their job (the 08-28 to 09-02 stall was
    that job left undone). The restore must not mistake it for a failure."""
    _stub_scripts(repo, guard_exit=0)
    logs = _point_the_wrapper_at(repo, monkeypatch)
    base = _git(repo, "rev-parse", "HEAD").stdout

    assert shm.main([]) == 0

    assert _git(repo, "rev-parse", "HEAD").stdout == base
    assert (repo / NEW_ARKG).exists() and (repo / NEW_XBI).exists()
    assert _read(repo, PAGE) == "<html>2026-09-10</html>"
    assert len(owned_dirt(repo)) == 5
    assert (logs / "holdings_monitor_last_success.txt").exists()
    assert "restore:" not in _log_text(logs)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_restore_is_wired_around_the_capture_only():
    """Never on a preflight refusal (that dirt is a person's), never once the
    publish has started (the commit may exist), and always per path — the
    tree is shared, so `git clean` over a directory is not an option."""
    src = inspect.getsource(shm.main)
    record = src.index("record_owned_state(")
    restore = src.index("restore_owned_paths(")
    assert src.index("preflight(") < record < src.index('"capture"')
    assert src.index('"build page"') < restore < src.index("publish(")
    body = inspect.getsource(shm.restore_owned_paths)
    assert '"clean"' not in body and "-fd" not in body
    assert '"checkout", "HEAD", "--", path' in body, "one path per checkout"


def test_every_path_the_capture_writes_is_owned():
    """The deadlock test_scheduled_refresh pins for the refresh, here. A path
    the capture writes but the wrapper does not own is dirt the preflight
    refuses on and the restore cannot see. Asserted against the scripts' own
    constants, not a literal list."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import build_holdings_monitor_page as page
    import run_holdings_monitor as rhm
    root = rhm.PROJECT_ROOT
    assert page.ROOT == root
    for written in (rhm.SNAP_DIR, rhm.LATEST_PATH, rhm.SERIES_PATH,
                    page.OUT_PATH, page.SERIES_PATH):
        rel = written.relative_to(root).as_posix()
        assert any(rel == o.rstrip("/") or rel.startswith(o) for o in OWNED), rel
    # The price cache is written too and is deliberately NOT owned: it is
    # gitignored, so it can neither dirty the preflight nor be restored. The
    # trailing slash on the snapshot prefix is what keeps it out.
    cache = rhm.PRICE_CACHE.relative_to(root).as_posix()
    assert not any(cache.startswith(o) for o in OWNED), cache
