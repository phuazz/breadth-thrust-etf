"""One-off repair (2026-08-13, owner-authorised): MNST's split-window hole.

yfinance served nothing for MNST from 2026-07-18 while processing its
2-for-1 split of 2026-08-11, so the CNDX and IUCS caches carry no MNST bars
after 2026-07-17 — a live constituent silently absent from breadth for
~three weeks. The vendor's series is currently MIS-ADJUSTED (split factor
unapplied), so a plain refetch would make things worse, and the WS15 guard
in compute_breadth now refuses it. This script closes the hole from Norgate
TOTALRETURN, rescaled onto each cache column's own PRE-SPLIT basis via the
median overlap ratio, which must come out at the split factor (~2.0) —
asserted, so a cache that has already been re-based cannot be double-scaled.

When yfinance repairs its series, the next refresh replaces the column
wholesale on the new basis (a constant re-basing, which the guard rightly
accepts) and this repair is superseded. Until then, breadth sees a complete
MNST on a single consistent basis.

Run: python scripts/repair_mnst_split_hole.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_ws15_residual_fill import (  # noqa: E402
    _assert_name, _norgate_tr, fill_column, overlap_ratio,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

PANELS = ["cndx", "iucs"]          # the two panels holding MNST
FILL_LO, FILL_HI = "2026-07-18", "2026-08-07"   # the observed hole
EXPECTED_RATIO = (1.98, 2.02)      # cache is pre-split basis; Norgate is
                                   # adjusted through the 2-for-1 split


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    _assert_name("MNST", "Monster Beverage")
    s = _norgate_tr("MNST", "2026-04-01")

    for panel in PANELS:
        path = DATA / f"prices_cache_{panel}.parquet"
        px = pd.read_parquet(path)
        assert "MNST" in px.columns, f"{panel}: no MNST column"
        before = px["MNST"].dropna()
        med, rel_std, n = overlap_ratio(px["MNST"], s)
        assert n >= 20, f"{panel}: overlap only {n} sessions"
        assert rel_std < 1e-3, f"{panel}: overlap ratio unstable ({rel_std:.2e})"
        assert EXPECTED_RATIO[0] <= med <= EXPECTED_RATIO[1], (
            f"{panel}: overlap ratio {med:.6f} is not the expected split "
            f"factor — the cache basis is not what this repair assumes; "
            f"REFUSING to write")
        px0 = px.copy()
        added = fill_column(px, "MNST", s * med,
                            pd.Timestamp(FILL_LO), pd.Timestamp(FILL_HI))
        mask = px0.notna()
        unchanged = (px.where(mask) == px0.where(mask)) | ~mask
        assert bool(unchanged.all().all()), f"{panel}: an existing bar changed"
        px.sort_index().to_parquet(path)
        after = pd.read_parquet(path)["MNST"].dropna()
        print(f"{panel.upper():5s} MNST: last bar {before.index.max().date()} "
              f"-> {after.index.max().date()}, +{added} bars, "
              f"ratio x{med:.6f} (rel std {rel_std:.1e}, {n} overlap sessions)")
    print("\nDone. The WS15 vendor step-defect guard protects these columns "
          "from the mis-adjusted refetch; a correctly re-based vendor series "
          "will replace them wholesale, as intended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
