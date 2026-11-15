"""The regression-suite gate's own ceiling and its duration record.

On 2026-11-15 three weekend runs passed live_targets and the release
preflight and were then killed by the 1200s vendor-step ceiling with pytest
86% done and nothing failing. The suite ran 489s by hand and about 1,390s
under Task Scheduler at priority 7. These pin the separation: vendor steps
keep 20 minutes, pytest gets its own ceiling, and its duration is recorded
and warned on before it fails a run.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_all as ra  # noqa: E402


def test_vendor_steps_keep_their_ceiling_and_pytest_has_more():
    assert ra.STEP_TIMEOUT_S == 20 * 60
    # The 2026-11-15 scheduled runs needed about 1,390s; the ceiling must
    # clear that with room, not merely exceed the old one.
    assert ra.PYTEST_TIMEOUT_S >= 1.5 * 1390


def test_main_passes_the_pytest_ceiling_to_the_pytest_step():
    """The constant is useless unless main() hands it to run_step."""
    tree = ast.parse((ROOT / "scripts" / "refresh_all.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "run_step"
             and n.args and isinstance(n.args[0], ast.Constant)
             and "pytest" in str(n.args[0].value)]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert getattr(kw.get("timeout_s"), "id", None) == "PYTEST_TIMEOUT_S"


def test_timing_note_is_silent_with_headroom():
    assert ra.pytest_timing_note(489.0, True) is None
    assert ra.pytest_timing_note(0.69 * ra.PYTEST_TIMEOUT_S, True) is None


def test_timing_note_warns_near_the_ceiling():
    note = ra.pytest_timing_note(0.75 * ra.PYTEST_TIMEOUT_S, True)
    assert note and note.startswith("WARNING") and "75%" in note


def test_timing_note_names_a_timeout():
    note = ra.pytest_timing_note(ra.PYTEST_TIMEOUT_S + 0.1, False)
    assert note and "ceiling" in note and "priority" in note


def test_a_failing_suite_well_inside_the_ceiling_adds_no_timing_note():
    """A red suite is reported as a failed step; it is not a timing problem."""
    assert ra.pytest_timing_note(300.0, False) is None


def test_duration_rows_append(tmp_path):
    log = tmp_path / "logs" / "pytest_durations.jsonl"
    ra.record_pytest_duration(489.04, True, path=log)
    ra.record_pytest_duration(1400.0, False, path=log)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["seconds"] for r in rows] == [489.0, 1400.0]
    assert [r["ok"] for r in rows] == [True, False]
    assert rows[0]["timeout_s"] == ra.PYTEST_TIMEOUT_S
    assert rows[0]["recorded_at_utc"].endswith("+00:00")


def test_an_unwritable_duration_log_never_raises(tmp_path, capsys):
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    ra.record_pytest_duration(10.0, True, path=blocker / "pytest_durations.jsonl")
    assert "not recorded" in capsys.readouterr().out
