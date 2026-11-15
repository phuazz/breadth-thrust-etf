"""Scheduling priority of the refresh: the running process, and the tasks.

Added 2026-11-15. The three refresh tasks ran at Task Scheduler priority 7
(below-normal CPU, low I/O and memory priority). The regression suite took
489 s by hand and about 1,390 s under the task, and on 2026-11-15 the 1200 s
step ceiling killed it on three weekend runs in a row with nothing failing.
The tasks were set to priority 4 that day. Registering or re-importing a
task resets it to 7 without warning, so the setting is checked, not
remembered: scheduled_refresh logs and warns on its own process class, and
the Saturday dry run reads the tasks' configured priority before the window.
"""
from __future__ import annotations

import re
import subprocess
import sys

REFRESH_TASKS = ("BreadthThrust-WeeklyRefresh",
                 "BreadthThrust-PostFillRefresh",
                 "BreadthThrust-PostFillCatchup")

# Task Scheduler's <Priority> runs 0 (realtime) to 10 (idle); 4-6 map to a
# normal process class and 7-8 to below normal. A missing element means 7.
TASK_DEFAULT_PRIORITY = 7
TASK_MAX_ACCEPTABLE = 6

# Windows process priority classes (GetPriorityClass).
_CLASS_NAMES = {0x40: "idle", 0x4000: "below normal", 0x20: "normal",
                0x8000: "above normal", 0x80: "high", 0x100: "realtime"}
_LOW_CLASSES = {0x40, 0x4000}


def process_priority() -> tuple[str, bool] | None:
    """(class name, is_low) for this process, or None off Windows or when
    the class cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
        cls = k32.GetPriorityClass(k32.GetCurrentProcess())
    except (AttributeError, OSError):
        return None
    if not cls:
        return None
    return _CLASS_NAMES.get(cls, hex(cls)), cls in _LOW_CLASSES


def process_priority_warning() -> str | None:
    got = process_priority()
    if got is None or not got[1]:
        return None
    return (f"this run is at {got[0]} process priority. The refresh tasks should "
            f"be at Task Scheduler priority {TASK_MAX_ACCEPTABLE} or lower (4 is "
            "set); at 7 the regression suite runs about three times slower and "
            "has timed out. Re-registering a task resets it to 7.")


def priority_from_task_xml(xml: str) -> int:
    """The <Priority> in a task's exported XML; Task Scheduler's default when
    the element is absent."""
    m = re.search(r"<Priority>\s*(\d+)\s*</Priority>", xml)
    return int(m.group(1)) if m else TASK_DEFAULT_PRIORITY


def configured_priorities(tasks=REFRESH_TASKS, run=subprocess.run) -> dict:
    """{task: priority int, or an error string when it cannot be read}."""
    out = {}
    for name in tasks:
        try:
            cp = run(["schtasks", "/query", "/tn", name, "/xml"],
                     capture_output=True, text=True, errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            out[name] = f"unreadable: {type(exc).__name__}"
            continue
        if cp.returncode != 0:
            out[name] = "unreadable: " + ((cp.stderr or cp.stdout).strip()[:120] or "schtasks failed")
            continue
        out[name] = priority_from_task_xml(cp.stdout)
    return out


def task_priority_findings(priorities: dict) -> list[str]:
    """One line per task that is too low or unreadable."""
    findings = []
    for name, p in priorities.items():
        if isinstance(p, int):
            if p > TASK_MAX_ACCEPTABLE:
                findings.append(f"{name} is at Task Scheduler priority {p}; set it "
                                f"to 4 (see scripts/task_priority.py)")
        else:
            findings.append(f"{name}: priority {p}")
    return findings
