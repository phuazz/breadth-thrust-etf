"""Keep the machine awake for the length of a scheduled refresh.

Added 2026-11-26. On 2026-11-22 the weekend Europe run started at 09:49
SGT on mains power. The laptop was unplugged at 10:00, entered Modern
Standby at 10:09 ("Connectivity state in standby: Disconnected, Reason:
Policy Setting") and slept from 11:34 to 16:15. The EXH2 holdings walk was
in flight when the network dropped. The fetcher logged it as "ENDPOINT
DOWN", the step clock recorded 22,052 s, and the run failed with nothing
committed. EXH2's price cache stayed on 13 Nov until the next weekend run.

The refresh did nothing to stop idle sleep. This module asks Windows to
keep the system and display on while it runs, through
SetThreadExecutionState with ES_CONTINUOUS. The request is per thread and
lapses when the thread or process ends, so a crashed or killed run cannot
leave the machine pinned awake.

Why the display flag as well: this machine uses Modern Standby, where the
idle path into standby runs through the display timeout. A system-required
request alone is not relied on to block that entry, so the display is held
on too. It costs a lit screen during a run.

What it cannot do: no execution-state request overrides closing the lid,
pressing the power button, a user choosing Sleep, or a critical-battery
action. Those still end the run; the in-flight marker and the next
firing's recovery handle the debris as before.

``BTE_KEEP_AWAKE=0`` turns it off for a run.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

# winbase.h execution-state flags.
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000

HOLD_FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
RELEASE_FLAGS = ES_CONTINUOUS

Setter = Callable[[int], int]


@dataclass
class KeepAwakeState:
    held: bool
    detail: str

    def describe(self) -> str:
        return ("held (system and display required) for this run" if self.held
                else f"NOT held: {self.detail}")


def _windows_setter() -> Setter | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes = [ctypes.c_uint32]
        fn.restype = ctypes.c_uint32
        return fn
    except Exception:  # noqa: BLE001 - absence is reported, never raised
        return None


@contextmanager
def held(setter: Setter | None = None,
         environ: dict | None = None) -> Iterator[KeepAwakeState]:
    """Hold the keep-awake request for the body of the ``with``.

    Never raises on its own account: a run must not fail because the
    request could not be made. The returned state says what happened so
    the caller can log it.
    """
    env = os.environ if environ is None else environ
    if env.get("BTE_KEEP_AWAKE", "1").strip() == "0":
        yield KeepAwakeState(False, "disabled by BTE_KEEP_AWAKE=0")
        return
    fn = setter if setter is not None else _windows_setter()
    if fn is None:
        yield KeepAwakeState(False, "SetThreadExecutionState unavailable on this platform")
        return
    try:
        prev = fn(HOLD_FLAGS)
    except Exception as e:  # noqa: BLE001
        yield KeepAwakeState(False, f"request raised {type(e).__name__}: {e}")
        return
    if not prev:
        # A zero return is the documented failure; nothing was set.
        yield KeepAwakeState(False, "SetThreadExecutionState returned 0")
        return
    try:
        yield KeepAwakeState(True, "")
    finally:
        try:
            fn(RELEASE_FLAGS)
        except Exception:  # noqa: BLE001 - thread exit releases it anyway
            pass
