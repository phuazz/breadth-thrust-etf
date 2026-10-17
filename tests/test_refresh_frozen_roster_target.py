"""A long refresh must judge every roster against its starting Friday."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import refresh_all as ra  # noqa: E402


@pytest.mark.parametrize(
    "start,after_midnight,expected",
    [
        # Python's date months are 1-indexed.
        (date(2026, 1, 30), date(2026, 1, 31), date(2026, 1, 23)),
        (date(2027, 1, 1), date(2027, 1, 2), date(2026, 12, 25)),
    ],
)
def test_refresh_keeps_its_roster_target_after_midnight(
    monkeypatch, start, after_midnight, expected,
):
    class ClockDate(date):
        current = start

        @classmethod
        def today(cls):
            return cls.current

    commands = []

    def run_step(label, command):
        commands.append((label, command))
        if "fetch_constituents" in label:
            ClockDate.current = after_midnight
        return True, 0.0

    monkeypatch.setattr(ra, "date", ClockDate)
    monkeypatch.setattr(ra, "ETFS_REFRESH", ["SOXX"])
    monkeypatch.setattr(ra, "ETFS_ALL", ["SOXX"])
    monkeypatch.setattr(ra, "_check_refusals_on_disk", lambda panels: [])
    monkeypatch.setattr(ra, "run_step", run_step)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--throttle", "0", "--no-tests"])

    assert ra.main() == 0
    fetch = next(cmd for label, cmd in commands if "fetch_constituents" in label)
    guard = next(cmd for label, cmd in commands if label == "VERIFY refresh guard (cross-panel coherence)")
    for command in (fetch, guard):
        index = command.index("--end-friday")
        assert command[index + 1] == expected.isoformat()
