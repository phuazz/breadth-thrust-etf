"""Sleeve C candidate gate — thin wrapper over check_universe_candidates.

This script used to carry its own copy of the gate logic, and that copy
had drifted from the authoritative screens in two ways: it correlated
weekly RETURNS while comparing against 0.85, a threshold Phase 5 defined
on weekly SIGNAL correlation, and it screened only against sleeve C's own
members even though Phase 5 screened C candidates against sleeve A's
sector slate too (which is how XOP, OIH and AMLP were rejected, all three
on their correlation with XLE).

Rather than fix the same logic twice and let the copies drift again, the
gate now lives in one place. This entry point is kept because it is what
the Phase 15 / Phase 17 / Phase 25 notes reference by name.

Usage:
    python scripts/check_thematic_candidates.py ITA AIQ IBIT

Equivalent to:
    python scripts/check_universe_candidates.py --strategy C ITA AIQ IBIT
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_universe_candidates import (  # noqa: E402
    deployed_panel,
    gate_candidates,
)


def main() -> int:
    candidates = sys.argv[1:]
    if not candidates:
        print(__doc__)
        print("error: give at least one candidate ticker")
        return 2
    panel, sleeves = deployed_panel()
    return gate_candidates(candidates, "C", panel, sleeves)


if __name__ == "__main__":
    raise SystemExit(main())
