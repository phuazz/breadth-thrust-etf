"""keep_awake: the scheduled refresh holds the machine awake for its run.

2026-11-22: the weekend Europe run lost its network to Modern Standby
mid-walk, logged the local outage as "ENDPOINT DOWN" and committed nothing.
These tests pin the request, its release, and its failure modes with an
injected setter, so the suite never changes the machine's power state.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import keep_awake  # noqa: E402
import scheduled_refresh  # noqa: E402


class FakeSetter:
    def __init__(self, ret: int = 0x80000000, raise_on_hold: bool = False):
        self.calls: list[int] = []
        self.ret = ret
        self.raise_on_hold = raise_on_hold

    def __call__(self, flags: int) -> int:
        self.calls.append(flags)
        if self.raise_on_hold and flags == keep_awake.HOLD_FLAGS:
            raise OSError("boom")
        return self.ret


def test_hold_requests_system_and_display_continuously_then_releases():
    s = FakeSetter()
    with keep_awake.held(setter=s, environ={}) as state:
        assert state.held
        assert s.calls == [keep_awake.HOLD_FLAGS]
    assert s.calls == [keep_awake.HOLD_FLAGS, keep_awake.ES_CONTINUOUS]
    flags = keep_awake.HOLD_FLAGS
    assert flags & keep_awake.ES_CONTINUOUS
    assert flags & keep_awake.ES_SYSTEM_REQUIRED
    assert flags & keep_awake.ES_DISPLAY_REQUIRED
    assert "held" in state.describe()


def test_release_runs_when_the_body_raises():
    s = FakeSetter()
    with pytest.raises(RuntimeError):
        with keep_awake.held(setter=s, environ={}):
            raise RuntimeError("refresh blew up")
    assert s.calls[-1] == keep_awake.ES_CONTINUOUS


def test_zero_return_is_reported_and_nothing_is_released():
    s = FakeSetter(ret=0)
    with keep_awake.held(setter=s, environ={}) as state:
        assert not state.held
        assert "returned 0" in state.describe()
    assert s.calls == [keep_awake.HOLD_FLAGS]


def test_a_raising_setter_never_fails_the_run():
    s = FakeSetter(raise_on_hold=True)
    with keep_awake.held(setter=s, environ={}) as state:
        assert not state.held
        assert "OSError" in state.detail


def test_env_switch_disables_without_calling_windows():
    s = FakeSetter()
    with keep_awake.held(setter=s, environ={"BTE_KEEP_AWAKE": "0"}) as state:
        assert not state.held
        assert "BTE_KEEP_AWAKE=0" in state.describe()
    assert s.calls == []


def test_no_platform_api_is_reported(monkeypatch):
    monkeypatch.setattr(keep_awake, "_windows_setter", lambda: None)
    with keep_awake.held(environ={}) as state:
        assert not state.held
        assert "unavailable" in state.detail


def test_cli_holds_keep_awake_around_main(monkeypatch):
    """cli() runs main() inside the hold and releases it afterwards; the
    state is published for the log header while main() runs."""
    s = FakeSetter()
    real_held = keep_awake.held
    monkeypatch.setattr(keep_awake, "held",
                        lambda: real_held(setter=s, environ={}))
    seen = {}

    def fake_main(argv=None):
        seen["calls_during_main"] = list(s.calls)
        seen["state"] = scheduled_refresh.KEEP_AWAKE_STATE
        return 7

    monkeypatch.setattr(scheduled_refresh, "main", fake_main)
    monkeypatch.setattr(scheduled_refresh, "release_in_flight", lambda: None)
    monkeypatch.setattr(scheduled_refresh, "KEEP_AWAKE_STATE", None)
    assert scheduled_refresh.cli([]) == 7
    assert seen["calls_during_main"] == [keep_awake.HOLD_FLAGS]
    assert seen["state"].held
    assert s.calls[-1] == keep_awake.ES_CONTINUOUS


def test_main_logs_the_keep_awake_state():
    src = Path(scheduled_refresh.__file__).read_text(encoding="utf-8")
    assert 'log.write(f"\\nkeep-awake: {KEEP_AWAKE_STATE.describe()}\\n")' in src
