"""The approved JMAT.L basis matches the incumbent cache seam, not the ex-date."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import basis_guard  # noqa: E402


def test_jmat_mixed_cache_rebase_requires_the_declared_adjusted_basis():
    # Python/pandas date months are 1-indexed. The cache began carrying
    # split-adjusted cells on 6 August, before the issuer's 17 August event.
    dates = pd.bdate_range("2026-07-29", "2026-08-14")
    incoming = pd.Series([100.0 + i for i in range(len(dates))], index=dates)
    incumbent = incoming.copy()
    incumbent.loc[:"2026-08-05"] /= 1.331163
    incumbent.loc["2026-08-06":] /= 0.998373

    finding = basis_guard.detect_rebasing(incumbent, incoming)
    assert finding is not None
    assert finding["through"] == "2026-08-05"
    assert finding["first_suffix"] == "2026-08-06"

    declarations, warnings = basis_guard.load_declarations(
        ROOT / "data" / "corporate_action_basis.json")
    assert not warnings
    chosen = basis_guard.decide(
        "JMAT.L", finding, declarations=declarations,
        incoming_end=pd.Timestamp("2026-10-16"),
    )
    assert chosen["decision"] == basis_guard.ADMITTED_BY_DECLARATION
    assert chosen["evidence"]["effective"] == "2026-08-06"
    assert chosen["evidence"]["factor"] == 0.75

    wrong_basis = [{**entry, "basis": "unadjusted"}
                   if entry["ticker"] == "JMAT.L" else entry
                   for entry in declarations]
    rejected = basis_guard.decide(
        "JMAT.L", finding, declarations=wrong_basis,
        incoming_end=pd.Timestamp("2026-10-16"),
    )
    assert rejected["decision"] == basis_guard.REFUSED_BY_DECLARATION

    issuer_date_instead_of_cache_seam = [
        {**entry, "effective": "2026-08-17"}
        if entry["ticker"] == "JMAT.L" else entry
        for entry in declarations
    ]
    no_match = basis_guard.decide(
        "JMAT.L", finding, declarations=issuer_date_instead_of_cache_seam,
        incoming_end=pd.Timestamp("2026-10-16"),
    )
    assert no_match["decision"] == basis_guard.REFUSED
