"""WS17 shadow — log a realised fill (probe or signal trade) into the shadow log.

Manual companion to run_ws17_shadow_evaluator.py: the evaluator logs quotes and
alerts; the operator executes in the 07:30-09:30 SGT window and records the fill
here. Append-only — corrections are new rows with --corrects.

Examples:
    python scripts/ws17_log_fill.py --kind probe-entry --side long --px 588.40 --sz 0.51
    python scripts/ws17_log_fill.py --kind probe-exit  --side long --px 590.10 --sz 0.51
    python scripts/ws17_log_fill.py --kind entry --side long --px 601.2 --sz 0.50 --note "signal 2026-09-02"
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "ws17_shadow_log.json"
KINDS = ("probe-entry", "probe-exit", "entry", "exit", "hold-funding")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--side", required=True, choices=("long", "short"))
    p.add_argument("--px", required=True, type=float, help="realised fill price")
    p.add_argument("--sz", required=True, type=float, help="size in SMH units")
    p.add_argument("--note", default=None)
    p.add_argument("--corrects", default=None,
                   help="ts_utc of a prior row this row corrects (append-only discipline)")
    a = p.parse_args()

    row = {
        "type": "execution", "ts_utc": datetime.now(timezone.utc).isoformat(),
        "kind": a.kind, "side": a.side, "px": a.px, "sz": a.sz,
        "notional_usd": round(a.px * a.sz, 2),
    }
    if a.note:
        row["note"] = a.note
    if a.corrects:
        row["corrects"] = a.corrects

    log = json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else []
    log.append(row)
    LOG_PATH.write_text(json.dumps(log, indent=1), encoding="utf-8")
    print(f"logged: {row}")
    print("Commit is handled by the next evaluator run, or commit manually if urgent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
