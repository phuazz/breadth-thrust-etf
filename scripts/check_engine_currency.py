"""Should CI re-run sleeves B and C, or would that throw work away?

WHY (2026-08-30). CI re-runs both ETF-level sleeves from yfinance on every
publish. That is right when the committed outputs are stale — it is how B and C
stay current between local refreshes, and the workflow says so. It is WRONG
when the committed output is already further along than CI can reach, because
the re-run then silently replaces good data with worse.

That stopped being hypothetical over 2026-08-28/30. yfinance withheld Friday's
closes for more than 43 hours, having served them once and retracted them. A
local run sourcing the same ETFs from Norgate reaches Friday; a CI run cannot,
because no GitHub runner has that feed. Publishing would have dragged both
sleeves back a session.

THE RULE. Re-run a sleeve only when its committed output ends BEFORE the last
completed NYSE session. If it already reaches that session, CI has nothing to
add and skips. If it somehow ends later, skip — CI must never move a sleeve
backwards.

WHAT THIS COSTS, stated plainly. The re-run also propagates code and config
changes, not just newer data. Skipping it means a change to either engine does
not reach the published JSON until the next local refresh. That is the price of
not overwriting, and it is the right trade only because the local refresh is
already the anchor for sleeves A and D for exactly the same reason. Set
BTE_FORCE_ENGINE_RERUN=1 to force both re-runs regardless — the escape hatch
for a deliberate engine change.

Outputs (stdout always; appended to $GITHUB_OUTPUT when set):
  rerun_b  'true' | 'false'
  rerun_c  'true' | 'false'
  summary  one line, for the log and the step name

Exit 0 whenever a decision could be made, including "skip both". Exit 2 when
the state cannot be read at all, which is fail-safe: an unreadable committed
output means CI should re-run rather than silently publish something unknown,
so the flags come back true in that case.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# (flag, label, file, path to the equity date list)
SLEEVES = (
    ("b", "Strategy B (asset-class)", "asset_class_rotation.json",
     ("headline", "headline_equity_dates")),
    ("c", "Strategy C (thematic)", "thematic_rotation.json",
     ("headline", "headline_equity_dates")),
)


def _dig(blob: dict, path: tuple[str, ...]):
    node = blob
    for key in path:
        node = node[key]
    return node


def evaluate(now_utc: datetime | None = None) -> dict:
    from nyse_sessions import last_completed_session  # noqa: PLC0415

    now = now_utc or datetime.now(timezone.utc)
    try:
        expected = last_completed_session(now)
    except Exception as exc:  # noqa: BLE001
        return {"undetermined": True, "rerun": {k: True for k, *_ in SLEEVES},
                "summary": f"cannot resolve the last completed session ({exc}) "
                           f"— re-running both, fail-safe"}
    expected_d = str(getattr(expected, "date", lambda: expected)())

    rerun, notes = {}, []
    for flag, label, fname, dpath in SLEEVES:
        p = DATA_DIR / fname
        try:
            dates = _dig(json.loads(p.read_text(encoding="utf-8")), dpath)
            ends = str(dates[-1])[:10]
        except Exception as exc:  # noqa: BLE001
            rerun[flag] = True
            notes.append(f"{label}: unreadable ({type(exc).__name__}) — re-run")
            continue
        if ends < expected_d:
            rerun[flag] = True
            notes.append(f"{label}: ends {ends} < {expected_d} — re-run")
        else:
            rerun[flag] = False
            notes.append(f"{label}: ends {ends} >= {expected_d} — SKIP, CI "
                         f"cannot add to it and must not move it back")
    return {"undetermined": False, "expected": expected_d, "rerun": rerun,
            "summary": "; ".join(notes)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="report both sleeves as needing a re-run")
    args = ap.parse_args(argv)

    forced = args.force or os.environ.get("BTE_FORCE_ENGINE_RERUN", "") == "1"
    r = evaluate()
    if forced:
        r["rerun"] = {k: True for k, *_ in SLEEVES}
        r["summary"] = "FORCED: re-running both regardless of currency"

    print("Engine currency check — last completed NYSE session: "
          f"{r.get('expected', 'unresolved')}")
    print(f"VERDICT: {r['summary']}")
    for flag in ("b", "c"):
        print(f"  rerun_{flag} = {str(r['rerun'][flag]).lower()}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            for flag in ("b", "c"):
                fh.write(f"rerun_{flag}={str(r['rerun'][flag]).lower()}\n")
            fh.write(f"summary={r['summary']}\n")
    return 2 if r["undetermined"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
