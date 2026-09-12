"""Exercise the shipped JavaScript clock renderer through Node's test runner."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_execution_timing_javascript():
    node = shutil.which("node")
    if node is None:
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        node = str(bundled) if bundled.exists() else None
    if node is None:
        pytest.skip("Node.js is required for the execution-timing renderer tests")
    result = subprocess.run(
        [node, "--test", "tests/test_execution_timing_display.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
