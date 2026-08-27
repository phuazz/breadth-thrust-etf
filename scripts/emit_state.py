"""emit_state.py — publish this repo's signal states in the STATE_CONTRACT shape.

WHAT THIS IS FOR
----------------
A private consumer (the command centre) renders this engine's gate, EM tilt and
deployed blend beside signals from seven other projects. Until now it did that
by reaching INTO this repo and reading exact JSON pointers out of
`risk_overlay.json` and `live_track.json` from its own side. That works, and it
is guarded there, but it puts the knowledge of this repo's field names in
somebody else's codebase: rename a key here and the break surfaces over there,
later, in a file nobody was editing at the time.

This script moves that knowledge back beside the data it describes. It reads
this repo's own outputs and writes `data/state.json` in the agreed shape, so a
rename breaks HERE, in this repo's CI, at the moment of the rename.

WHAT IT IS NOT
--------------
  * NOT a new signal, and not a calculation. Every value is copied from a file
    this repo already publishes. If this script and `risk_overlay.json` ever
    disagree, `risk_overlay.json` is right and this is broken.
  * NOT load-bearing for anything in this repo. Nothing here reads
    `data/state.json`; the engine, the factsheet, the scanner and the monitor
    are all untouched by it. It runs in its own workflow precisely so that a
    failure here can never block a publish that matters.
  * NOT a place to put anything non-public. Everything emitted is already
    visible in this repo's committed data files.

THE FIELDS ARE COPIED, NOT DERIVED
----------------------------------
`role`, `horizon`, `evidence_grade` and `licence` describe how the CONSUMER is
permitted to use each signal; they are its vocabulary, not this repo's, and are
repeated here verbatim so a state never travels without them. The consumer
validates them against its own registry and REJECTS the emission if they
disagree — so this file drifting out of step is a loud failure there, not a
silently wrong grade. Do not "fix" a rejection by editing the values here
without checking which side is actually wrong.

Usage:
    python scripts/emit_state.py           # write data/state.json
    python scripts/emit_state.py --check   # validate and print, write nothing
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

# Windows consoles default to cp1252 and cannot encode the dashes this script
# prints. Without this a SUCCESSFUL run raises UnicodeEncodeError inside print()
# and exits non-zero, which reads as a failed emission.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
OUT = DATA / "state.json"

CONTRACT_VERSION = "1"
SOURCE = "breadth-thrust-etf"

# Consumer vocabulary, repeated verbatim. See the module docstring: the consumer
# rejects the emission if these drift from its registry.
COMMON = {"role": "engine-internal", "evidence_grade": "deployed-engine",
          "licence": "public", "cadence": "daily", "action_hint": "none"}


class EmitError(Exception):
    """A required input was missing or malformed. Never emit a guess."""


def require(obj, path: str, kind=None):
    """Fetch a dotted pointer, hard-asserting each hop. A missing key must stop
    the emission, not produce a null that reads downstream as a state."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise EmitError(f"missing key `{part}` at pointer `{path}`")
        cur = cur[part]
    if cur is None:
        raise EmitError(f"pointer `{path}` is null")
    if kind is not None and not isinstance(cur, kind):
        # `kind` is often a tuple of accepted types, which has no __name__ —
        # naming it naively made the ERROR PATH raise AttributeError, turning a
        # clear "wrong type" into a traceback from the wrong line.
        want = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
        raise EmitError(f"pointer `{path}` is {type(cur).__name__}, expected {want}")
    return cur


def load(name: str):
    p = DATA / name
    if not p.exists():
        raise EmitError(f"source file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EmitError(f"{name} is not valid JSON: {exc}") from exc


def build() -> dict:
    ro = load("risk_overlay.json")
    lt = load("live_track.json")

    panel_end = require(ro, "panel_end_date", str)
    gate_state = require(ro, "current_state", str)
    gate_since = require(ro, "current_state_since", str)
    breadth = require(ro, "current_breadth", (int, float))
    ro_computed = ro.get("computed_at_utc")

    tilt_state = require(ro, "phase22_eem_tilt.current_state", str)
    tilt_since = require(ro, "phase22_eem_tilt.current_state_since", str)
    ratio = require(ro, "phase22_eem_tilt.current_ratio", (int, float))

    anchor = require(lt, "anchor_date", str)
    key = require(lt, "deployed_key", str)
    weights = require(lt, "effective_weights", dict)

    # State vocabularies are fixed on the consumer side. Emitting something
    # outside them would be rejected there; catching it here names the file.
    if gate_state not in ("RISK_ON", "RISK_OFF"):
        raise EmitError(f"risk_overlay.current_state {gate_state!r} outside {{RISK_ON, RISK_OFF}}")
    if tilt_state not in ("EM_TILT_ON", "OFF"):
        raise EmitError(
            f"phase22_eem_tilt.current_state {tilt_state!r} outside {{EM_TILT_ON, OFF}}")
    if not weights:
        raise EmitError("live_track.effective_weights is empty — nothing is deployed")

    signals = {
        "engine_phase19_gate": {
            "as_of": panel_end, "state": gate_state, "value": breadth,
            "zone": f"since {gate_since}", "horizon": "weeks-months",
            "source_file": "data/risk_overlay.json", "computed_at": ro_computed, **COMMON,
        },
        "engine_phase22_em_tilt": {
            "as_of": panel_end, "state": tilt_state, "value": ratio,
            "zone": f"since {tilt_since}", "horizon": "weeks-months",
            "source_file": "data/risk_overlay.json", "computed_at": ro_computed, **COMMON,
        },
        "engine_live_blend": {
            "as_of": anchor, "state": "DEPLOYED", "value": len(weights),
            "zone": key, "horizon": "weekly rebalance",
            "source_file": "data/live_track.json",
            "computed_at": lt.get("computed_at_utc"), **COMMON,
        },
    }

    return {
        "contract_version": CONTRACT_VERSION,
        "emitted_by": SOURCE,
        "emitted_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "signals": signals,
    }


def unchanged(payload: dict) -> bool:
    """Is this emission the same as the one already on disk, apart from the
    timestamp of the run that produced it?

    `emitted_at` moves on every run, so writing unconditionally would leave a
    diff every time and the workflow would commit a no-op to a public repo each
    weekday, forever. Liveness does not need that commit: the consumer judges
    freshness from `as_of`, which advances whenever the engine actually
    produces a new session, so a genuinely dead emitter still shows up there as
    a stale state. A day with nothing new to say is better said by silence.
    """
    if not OUT.exists():
        return False
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False  # unreadable previous emission — rewrite it
    strip = lambda d: {k: v for k, v in d.items() if k != "emitted_at"}
    return strip(prev) == strip(payload)


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    try:
        payload = build()
    except EmitError as exc:
        print(f"emit_state: FAILED — {exc}", file=sys.stderr)
        print("emit_state: nothing written; the previous state.json is left as it was.",
              file=sys.stderr)
        return 1

    s = payload["signals"]
    print(f"emit_state: {len(s)} signal(s) — "
          f"gate {s['engine_phase19_gate']['state']} @ {s['engine_phase19_gate']['as_of']}, "
          f"tilt {s['engine_phase22_em_tilt']['state']}, "
          f"blend {s['engine_live_blend']['zone']} "
          f"({s['engine_live_blend']['value']} holdings @ {s['engine_live_blend']['as_of']})")

    if check_only:
        print("emit_state: --check, nothing written.")
        return 0

    if unchanged(payload):
        print("emit_state: state unchanged since the last emission — leaving it as it is.")
        return 0

    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        shown = OUT.relative_to(REPO)
    except ValueError:  # an output path outside the repo (tests) is not an error
        shown = OUT
    print(f"emit_state: wrote {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
