"""The approved 688110.SS basis matches the observed partial-cache seam."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import basis_guard  # noqa: E402


def test_688110_partial_cache_rebase_is_admitted_only_on_declared_adjusted_basis():
    # Python/pandas date months are 1-indexed. The cache began carrying
    # split-adjusted cells on 30 September, before the 12 October ex-date.
    dates = pd.to_datetime([
        "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28",
        "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09",
    ])
    incoming = pd.Series([70.0, 71.0, 72.0, 73.0, 74.0, 75.0, 76.0, 77.0], index=dates)
    incumbent = incoming.copy()
    incumbent.iloc[:5] /= 0.6653927
    incumbent.iloc[5:] /= 0.9914352
    finding = basis_guard.detect_rebasing(incumbent, incoming)
    assert finding is not None
    assert finding["through"] == "2026-09-29"
    assert finding["first_suffix"] == "2026-09-30"

    declarations, warnings = basis_guard.load_declarations(
        ROOT / "data" / "corporate_action_basis.json")
    assert not warnings
    verdict = basis_guard.decide(
        "688110.SS", finding, declarations=declarations,
        incoming_end=pd.Timestamp("2026-10-15"),
    )
    assert verdict["decision"] == basis_guard.ADMITTED_BY_DECLARATION
    assert verdict["evidence"]["effective"] == "2026-09-30"
    assert verdict["evidence"]["factor"] == 1.49
