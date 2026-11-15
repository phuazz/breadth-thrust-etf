"""The 2026-11-15 refresh guards: task priority and the Saturday dry run.

On 2026-11-14/15 the weekend refresh failed five times: twice on a rounding
contract live_targets and the release audit implemented differently, three
times on a pytest gate killed at 1200 s because the tasks ran at Task
Scheduler priority 7. Both would have shown on a Saturday-morning rehearsal.
These pin that the rehearsal publishes nothing, always restores the clone,
and reports only what needs attention, and that a low priority is seen.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import task_priority as tp  # noqa: E402
import weekend_dry_run as wd  # noqa: E402


# ----- task_priority -------------------------------------------------------
def test_priority_read_from_task_xml():
    xml = "<Task><Settings><Priority>4</Priority></Settings></Task>"
    assert tp.priority_from_task_xml(xml) == 4
    # Task Scheduler omits the element at its default, which is 7.
    assert tp.priority_from_task_xml("<Task><Settings/></Task>") == 7


def test_findings_name_low_and_unreadable_tasks_only():
    found = tp.task_priority_findings({"A": 4, "B": 6, "C": 7, "D": "unreadable: x"})
    assert len(found) == 2
    assert found[0].startswith("C is at Task Scheduler priority 7")
    assert found[1].startswith("D:")


def test_configured_priorities_tolerates_a_missing_task():
    def fake_run(cmd, **kw):
        name = cmd[3]
        if name == "missing":
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: The system cannot find the file")
        return subprocess.CompletedProcess(cmd, 0, "<Priority>7</Priority>", "")
    got = tp.configured_priorities(("present", "missing"), run=fake_run)
    assert got["present"] == 7
    assert got["missing"].startswith("unreadable: ERROR")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority classes")
def test_process_priority_sees_below_normal():
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    me = k32.GetCurrentProcess()
    original = k32.GetPriorityClass(me)
    try:
        assert k32.SetPriorityClass(me, 0x4000)          # BELOW_NORMAL
        assert tp.process_priority() == ("below normal", True)
        assert "below normal" in tp.process_priority_warning()
        assert k32.SetPriorityClass(me, 0x20)            # NORMAL
        assert tp.process_priority() == ("normal", False)
        assert tp.process_priority_warning() is None
    finally:
        k32.SetPriorityClass(me, original)


def test_scheduled_refresh_never_mails_the_priority_warning_from_a_test():
    """The suite itself runs inside the refresh task, so under priority 7
    every test driving main() in-process would otherwise send a [WARN]."""
    src = (ROOT / "scripts" / "scheduled_refresh.py").read_text(encoding="utf-8")
    block = src[src.index("# ----- Process priority (2026-11-15)"):]
    block = block[:block.index("Scheduled refresh running at low priority")]
    assert "_durable_state_enabled()" in block


# ----- deadline arithmetic (local time; months are 1-indexed in Python) -----
@pytest.mark.parametrize("now", [datetime(2026, 11, 30, 7, 15), datetime(2026, 12, 31, 7, 15),
                                 datetime(2027, 1, 1, 7, 15)])
def test_deadline_is_the_same_day_across_month_and_year_ends(now):
    d = wd.deadline_for(now, "08:40")
    assert (d.year, d.month, d.day, d.hour, d.minute) == (now.year, now.month, now.day, 8, 40)
    assert d - now == timedelta(minutes=85)


# ----- the dry run's flow ----------------------------------------------------
class Harness:
    """Fake git, feed, refresh and mail around weekend_dry_run.main."""

    def __init__(self, monkeypatch, tmp_path, *, porcelain="", ahead="0",
                 refresh=("ok", 0), recovered=(True, "restored")):
        self.git_calls, self.mail, self.restored, self.ran = [], [], 0, 0
        self.porcelain, self.ahead, self.refresh, self.recovered = porcelain, ahead, refresh, recovered
        sr = wd.sr
        monkeypatch.setattr(wd, "LOG_DIR", tmp_path)
        monkeypatch.setattr(wd, "HEARTBEAT", tmp_path / "last_dry_run.json")
        monkeypatch.setattr(sr, "price_source_preflight", lambda s: (True, "price source ok"))
        monkeypatch.setattr(sr, "_git", self.git)
        monkeypatch.setattr(sr, "_email", lambda subj, body, log: self.mail.append((subj, body)))
        monkeypatch.setattr(sr, "recover_interrupted_run", lambda log, p: self.recovered)
        monkeypatch.setattr(sr, "restore_tracked_outputs", self.restore)
        monkeypatch.setattr(sr, "claim_in_flight", lambda *a, **k: None)
        monkeypatch.setattr(sr, "release_in_flight", lambda: None)
        monkeypatch.setattr(wd, "run_refresh", self.run_refresh)
        monkeypatch.setattr(wd, "panel_end_date", lambda: "2026-11-13")
        monkeypatch.setattr(wd.task_priority, "configured_priorities",
                            lambda: {t: 4 for t in tp.REFRESH_TASKS})
        monkeypatch.setattr(wd.task_priority, "process_priority_warning", lambda: None)
        # A deadline comfortably ahead of whatever the clock says.
        monkeypatch.setattr(wd, "deadline_for",
                            lambda now, hhmm: now + timedelta(hours=2))

    def git(self, args, log, cwd=None):
        self.git_calls.append(args)
        out = {"status": self.porcelain, "rev-list": self.ahead,
               "rev-parse": "abc1234"}.get(args[0], "")
        return subprocess.CompletedProcess(["git", *args], 0, out, "")

    def restore(self, log, repo_root=None):
        self.restored += 1
        return True

    def run_refresh(self, log, budget, price_source):
        self.ran += 1
        return self.refresh[0], self.refresh[1], log.tell()

    def published(self):
        return [c for c in self.git_calls if c[0] in ("commit", "push", "add")]


def test_green_run_is_silent_restores_and_publishes_nothing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    assert wd.main([]) == 0
    assert h.ran == 1 and h.restored == 1
    assert h.mail == [] and h.published() == []
    beat = json.loads((tmp_path / "last_dry_run.json").read_text(encoding="utf-8"))
    assert beat["status"] == "green" and beat["panel_end"] == "2026-11-13"


def test_failed_refresh_mails_and_still_restores(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, refresh=("failed", 1))
    assert wd.main([]) == 3
    assert h.restored == 1 and h.published() == []
    assert len(h.mail) == 1 and h.mail[0][0].startswith("[DRY-RUN FAIL]")
    assert "09:00 firing will most likely fail" in h.mail[0][1]


def test_timeout_mails_and_restores(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, refresh=("timeout", None))
    assert wd.main([]) == 3
    assert h.restored == 1
    assert "did not finish by" in h.mail[0][0]


def test_a_dirty_clone_it_cannot_attribute_is_left_alone(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, porcelain=" M data/x.json\n",
                recovered=(False, "no in-flight marker"))
    assert wd.main([]) == 2
    assert h.ran == 0 and h.restored == 0
    assert not any(c[0] in ("pull", "checkout", "clean") for c in h.git_calls)
    assert "not clean" in h.mail[0][0]


def test_unpushed_commits_are_left_for_the_real_firing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, ahead="2")
    assert wd.main([]) == 0
    assert h.ran == 0
    assert not any(c[0] == "pull" for c in h.git_calls)
    assert "[DRY-RUN WARN]" in h.mail[0][0]


def test_a_low_priority_task_is_reported_even_when_green(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    monkeypatch.setattr(wd.task_priority, "configured_priorities",
                        lambda: {"BreadthThrust-WeeklyRefresh": 7})
    assert wd.main([]) == 0
    assert h.mail and h.mail[0][0].startswith("[DRY-RUN WARN]")
    assert "priority 7" in h.mail[0][1]


def test_the_dry_run_never_reaches_publication_or_run_state():
    """No commit, push, green marker, run ledger, debt attempt or release."""
    tree = ast.parse((ROOT / "scripts" / "weekend_dry_run.py").read_text(encoding="utf-8"))
    called = {getattr(n.func, "attr", getattr(n.func, "id", None))
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("record_green_run", "record_run", "record_debt_attempt",
                      "release_if_ready", "reconcile_unpushed_commits"):
        assert forbidden not in called
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
               and isinstance(n.value, str)}
    assert not {"push", "commit"} & strings
