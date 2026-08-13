"""WS16 step 2 — era fills the general backfill cannot express, and a
basis-consistency audit over every WS16 fill.

TWO-ERA FILL. Chesapeake Energy lived twice under CHK: the original line
(Norgate CHKAQ-202102, 1993 to 2021-02-09, wiped in bankruptcy) and the
relisted company whose lineage lives under EXE. RENAMED can carry only one
target per ticker, and the backfill's ambiguity guard rightly refuses to
choose — so the OLD era is filled here explicitly, NaN-only, capped at the
era barrier, name-verified. (OPI needs no such treatment: its roster life
ended with the old OPITQ line, which RENAMED covers.)

BASIS AUDIT. A NaN-only fill into a column that already held SOME bars for
the same era can interleave two adjustment bases (a frozen pre-delisting
yfinance basis beside Norgate's current TOTALRETURN basis), which bends
every moving average computed across the seams. For every WS16-filled name,
on every date where the column AND Norgate both have a value inside the
held era, the ratio must be one constant: if its spread exceeds tolerance,
the whole era is rewritten from Norgate (one consistent basis), loudly.
A constant ratio different from 1.0 is fine — scale cancels per column.

Run: python scripts/run_ws16_era_fixups.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_ws15_residual_fill import _assert_name, _norgate_tr, fill_column  # noqa: E402
from norgate_symbols import RENAMED, resolve  # noqa: E402
from backfill_delisted_prices import US_CONSTITUENT_PANELS, _held_dates  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ticker -> (norgate symbol, required security_name substring, era end (excl))
ERA_FILLS = {
    "CHK":  ("CHKAQ-202102", "Chesapeake Energy", "2021-02-10"),
    # 21st Century Fox before the 2019 split — the same era fills WS15 gave
    # CNDX, needed wherever else the rosters held the classes (IUCD).
    "FOXA": ("TFCFA-201903", "Twenty-First Century Fox Inc Class A", "2019-03-12"),
    "FOX":  ("TFCF-201903",  "Twenty-First Century Fox Inc Class B", "2019-03-13"),
    # Old Arconic before the April-2020 split: the lineage lives under
    # Howmet Aerospace; a NEW Arconic Corp took ARNC from 2020-04-01.
    "ARNC": ("HWM", "Howmet Aerospace", "2020-04-01"),
}
# Names filled by the WS16 sweep (RENAMED additions) whose eras get audited.
AUDIT = ["SIVB", "FRC", "LB", "COG", "DWDP", "APY", "SATS", "VSCO", "MPW",
         "AHH", "PEI", "AFIN", "HTA", "IRET", "OPI", "BFB", "CHK"]
RATIO_REL_STD_MAX = 2e-3
REWRITES_LOG: list[str] = []


def audit_and_repair(px: pd.DataFrame, t: str, series: pd.Series,
                     held: list[str], panel: str) -> bool:
    """True when the column's held-era bars sit on ONE basis vs Norgate;
    on failure the era is rewritten wholesale from Norgate."""
    if not held:
        return True
    lo, hi = pd.Timestamp(held[0]), pd.Timestamp(held[-1])
    col = px[t].loc[lo:hi].dropna()
    s = series.loc[lo:hi]
    common = col.index.intersection(s.dropna().index)
    if len(common) < 10:
        return True
    r = (col.reindex(common) / s.reindex(common)).astype(float)
    rel = float(r.std() / abs(r.median())) if r.median() else 0.0
    if rel <= RATIO_REL_STD_MAX:
        return True
    idx = s.index.intersection(px.index)
    px.loc[idx.intersection(pd.date_range(lo, hi)), t] = s.reindex(
        idx.intersection(pd.date_range(lo, hi)))
    REWRITES_LOG.append(
        f"{panel}:{t} era rewritten from Norgate (ratio rel std {rel:.1e} — "
        f"mixed bases detected across {len(common)} shared sessions)")
    return False


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    for etf in sorted(US_CONSTITUENT_PANELS):
        ppath = DATA / f"prices_cache_{etf.lower()}.parquet"
        cpath = DATA / f"constituents_{etf.lower()}.json"
        if not ppath.exists() or not cpath.exists():
            continue
        snaps = json.loads(cpath.read_text(encoding="utf-8")).get("snapshots", {})
        px = pd.read_parquet(ppath)
        changed = False

        # -- explicit two-era fills --------------------------------------
        for t, (sym, want, era_end) in ERA_FILLS.items():
            held = sorted(_held_dates(snaps, t))
            held_old = [d for d in held if d < era_end]
            if t not in px.columns or not held_old:
                continue
            _assert_name(sym, want)
            s = _norgate_tr(sym, "2017-07-10")
            added = fill_column(px, t, s, pd.Timestamp("2017-07-10"),
                                pd.Timestamp(era_end) - pd.Timedelta(days=1))
            if added:
                print(f"{etf:5s} {t}: old era filled from {sym} (+{added} bars)")
                changed = True

        # -- basis audit over the WS16 fill set --------------------------
        for t in AUDIT:
            if t not in px.columns:
                continue
            held = sorted(_held_dates(snaps, t))
            if not held:
                continue
            sym = (resolve(t, pd.Timestamp(held[0]).date())
                   or RENAMED.get(t))
            if not sym:
                continue
            try:
                s = _norgate_tr(sym, "2017-07-10")
            except Exception:
                continue
            if not audit_and_repair(px, t, s, held, etf):
                changed = True

        if changed:
            px.sort_index().to_parquet(ppath)

    print("\nBasis audit: " + (f"{len(REWRITES_LOG)} era(s) rewritten:"
                               if REWRITES_LOG else
                               "every filled era sits on one basis."))
    for line in REWRITES_LOG:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
